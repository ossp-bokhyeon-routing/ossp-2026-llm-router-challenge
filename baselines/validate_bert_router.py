# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate the packaged BERT-style router on materialized public data.

This training-side utility uses NumPy only to vectorize rerouted bootstrap
batches.  Prediction and point selection call the same standard-library
artifact parsers and inference functions used by the final container.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

import numpy as np

from ossp_router.bert_router import (
    CONSERVATIVE_SCORE_CONFIGURATIONS,
    PUBLIC_COST_TIER_CONFIGURATIONS,
    TIER_CONFIGURATIONS,
    load_bundled_artifacts,
    predict_episode,
    predict_public_episode,
    select_batch,
)
from ossp_router.hash_linear import select_models
from ossp_router.heuristic import episode_text
from ossp_router.protocol import (
    MODEL_IDS,
    TIERS,
    Decision,
    InputBatch,
    OutcomeBatch,
    RoutingPolicy,
    Submission,
    load_bundled_policy,
    load_input,
    load_outcomes,
    parse_submission,
    submission_to_dict,
    write_json,
)
from ossp_router.scoring import score_submissions
from ossp_router.public_cost_lookup import PublicCostLookup, prompt_digest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data/materialized/dev/inputs.json"
DEFAULT_OUTCOMES = ROOT / "data/dev/outcomes.json"
DEFAULT_REPORT = ROOT / "build/bert-router-final/risk-validation.json"
DEFAULT_ALL_MISS_REPORT = (
    ROOT / "build/bert-router-final/all-miss-risk-validation.json"
)
DEFAULT_REPS = 5_000
DEFAULT_SEED = 20_260_826
MAJOR_GROUP_MINIMUM = 40

_MCQ = re.compile(
    r"(?:^|\n)\s*(?:[A-E][.)]|[1-5][.)]|①|②|③|④|⑤)", re.MULTILINE
)
_CODE = re.compile(
    r"```|\b(?:def|class|function|return|import|SELECT|FROM|Traceback)\b",
    re.IGNORECASE,
)
_MATH = re.compile(
    r"[=+*/^<>≤≥∑∫√]|\\(?:frac|sum|sqrt|begin)|"
    r"\b(?:prove|theorem|calculate|solve|증명|계산|풀이)\b",
    re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def _zero_cls_state(weights: Any, *_args: Any, **_kwargs: Any) -> tuple[float, ...]:
    """Retain the dense residual branch while ablating Transformer output."""

    return (0.0,) * weights.hidden_size


def _subset_outcomes(
    inputs: InputBatch, outcomes: OutcomeBatch
) -> OutcomeBatch:
    episode_ids = {episode.episode_id for episode in inputs.episodes}
    rows = tuple(
        outcome
        for outcome in outcomes.outcomes
        if outcome.episode_id in episode_ids
    )
    if len(rows) != len(inputs.episodes) * len(MODEL_IDS):
        raise ValueError("repository input/outcome subset is incomplete")
    return OutcomeBatch(
        outcomes.schema_version,
        outcomes.challenge_id,
        outcomes.split,
        rows,
    )


def _outcome_cost(outcome: Any, policy: RoutingPolicy) -> float:
    rates = policy.models[outcome.model_id]
    unit = Decimal(policy.token_unit)
    return float(
        rates.fixed_cost
        + Decimal(outcome.input_tokens) * rates.input_token_rate / unit
        + Decimal(outcome.output_tokens) * rates.output_token_rate / unit
    )


def _actual_matrices(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
) -> tuple[np.ndarray, np.ndarray]:
    index = {
        (outcome.episode_id, outcome.model_id): outcome
        for outcome in outcomes.outcomes
    }
    scores = []
    costs = []
    for episode in inputs.episodes:
        rows = [index[(episode.episode_id, model_id)] for model_id in MODEL_IDS]
        scores.append([float(row.score) for row in rows])
        costs.append([_outcome_cost(row, policy) for row in rows])
    return np.asarray(scores), np.asarray(costs)


def _predict(
    inputs: InputBatch,
    tier: str,
    artifacts: Any,
    *,
    include_word: bool = True,
    include_bert: bool = True,
) -> tuple[np.ndarray, np.ndarray, float]:
    started = time.perf_counter()
    scores = []
    costs = []
    lookup = artifacts.public_cost_lookup
    if lookup is None:
        raise ValueError("public cost lookup artifact is unavailable")
    for episode in inputs.episodes:
        if episode.prompt is None:
            raise ValueError("materialized validation requires prompt rows")
        cost_values = lookup.costs_for_digest(prompt_digest(episode.prompt))
        if cost_values is None:
            raise ValueError("materialized validation prompt is absent from lookup")
        score_row = predict_public_episode(
            episode,
            tier,
            artifacts,
            include_word=include_word,
            include_bert=include_bert,
        )
        scores.append([score_row[model_id] for model_id in MODEL_IDS])
        costs.append([float(value) for value in cost_values])
    return (
        np.asarray(scores),
        np.asarray(costs),
        time.perf_counter() - started,
    )


def _route(
    scores: np.ndarray,
    costs: np.ndarray,
    cap: float,
) -> np.ndarray:
    selected, _ratio = select_models(
        tuple(dict(zip(MODEL_IDS, row)) for row in scores),
        tuple(dict(zip(MODEL_IDS, row)) for row in costs),
        budget_multiplier=cap,
        safety_ratio=1.0,
    )
    return np.asarray([MODEL_IDS.index(model_id) for model_id in selected])


def _metrics(
    choices: np.ndarray,
    actual_scores: np.ndarray,
    actual_costs: np.ndarray,
) -> Mapping[str, Any]:
    rows = np.arange(len(choices))
    return {
        "score": float(actual_scores[rows, choices].mean()),
        "cost": float(
            actual_costs[rows, choices].sum() / actual_costs[:, 0].sum()
        ),
        "counts": [
            int(np.sum(choices == model_index))
            for model_index in range(len(MODEL_IDS))
        ],
    }


def _rerouted_bootstrap(
    predicted_scores: np.ndarray,
    predicted_costs: np.ndarray,
    actual_scores: np.ndarray,
    actual_costs: np.ndarray,
    cap: float,
    takes: np.ndarray,
    *,
    chunk_size: int = 64,
) -> Mapping[str, Any]:
    repetitions, _rows = takes.shape
    quality = np.empty(repetitions)
    ratio = np.empty(repetitions)
    counts = np.empty((repetitions, len(MODEL_IDS)), dtype=np.int32)
    selector_parity_checks = 0
    for start in range(0, repetitions, chunk_size):
        stop = min(repetitions, start + chunk_size)
        take = takes[start:stop]
        scores = predicted_scores[take]
        costs = predicted_costs[take]
        light_total = np.fromiter(
            (math.fsum(row.tolist()) for row in costs[:, :, 0]),
            dtype=np.float64,
            count=len(take),
        )
        budget = cap * light_total
        batch_size = len(take)
        low = np.zeros(batch_size)
        high = np.ones(batch_size)

        def choose(penalty: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            utility = (
                scores
                - penalty[:, None, None]
                * costs
                / light_total[:, None, None]
            )
            # np.argmax returns the first model for an exact tie, matching the
            # runtime selector's fixed MODEL_IDS tie-break without perturbing
            # close, non-tied utilities.
            selected = np.argmax(utility, axis=2)
            chosen = np.take_along_axis(
                costs, selected[:, :, None], axis=2
            )[:, :, 0]
            total = chosen.sum(axis=1)
            # NumPy's pairwise sum can differ from runtime ``math.fsum`` by
            # an ulp.  Recompute only rows close enough to the budget for that
            # bounded summation error to change feasibility.
            tolerance = (
                np.finfo(np.float64).eps
                * (chosen.shape[1] + 2)
                * np.maximum(total, 1.0)
            )
            ambiguous = np.flatnonzero(np.abs(total - budget) <= tolerance)
            for row_index in ambiguous:
                total[row_index] = math.fsum(chosen[row_index].tolist())
            return selected, total

        unconstrained_choices, unconstrained_total = choose(low)
        unconstrained = unconstrained_total <= budget
        selected = unconstrained_choices
        # The scalar runtime probes penalties 1, 2, ..., 2**60 before
        # entering its fixed 80-step binary search.
        for _iteration in range(61):
            high_choices, high_total = choose(high)
            over = high_total > budget
            selected = high_choices
            expandable = over & (high < float(2**60))
            if not np.any(expandable):
                break
            low[expandable] = high[expandable]
            high[expandable] *= 2.0
        for _iteration in range(80):
            middle = (low + high) * 0.5
            middle_choices, middle_total = choose(middle)
            feasible = middle_total <= budget
            high[feasible] = middle[feasible]
            low[~feasible] = middle[~feasible]
            selected[feasible] = middle_choices[feasible]
        selected[unconstrained] = unconstrained_choices[unconstrained]
        selected_total = np.take_along_axis(
            costs, selected[:, :, None], axis=2
        )[:, :, 0]
        approximate_total = selected_total.sum(axis=1)
        tolerance = (
            np.finfo(np.float64).eps
            * (selected_total.shape[1] + 2)
            * np.maximum(approximate_total, 1.0)
        )
        ambiguous = np.flatnonzero(
            np.abs(approximate_total - budget) <= tolerance
        )
        for row_index in ambiguous:
            approximate_total[row_index] = math.fsum(
                selected_total[row_index].tolist()
            )
        selected[approximate_total > budget] = 0

        # Compare the vectorized solver with the actual runtime implementation
        # on complete resampled batches from every vectorized chunk before
        # accepting the risk report.  The first chunk checks three rows and
        # later chunks check one each.
        parity_indices = (
            range(min(3, len(take))) if start == 0 else range(min(1, len(take)))
        )
        for local_index in parity_indices:
            scalar = _route(scores[local_index], costs[local_index], cap)
            if not np.array_equal(scalar, selected[local_index]):
                raise RuntimeError(
                    "vectorized bootstrap selector differs from runtime"
                )
            selector_parity_checks += 1

        sampled_scores = actual_scores[take]
        sampled_costs = actual_costs[take]
        chosen_scores = np.take_along_axis(
            sampled_scores, selected[:, :, None], axis=2
        )[:, :, 0]
        chosen_costs = np.take_along_axis(
            sampled_costs, selected[:, :, None], axis=2
        )[:, :, 0]
        quality[start:stop] = chosen_scores.mean(axis=1)
        chosen_actual_total = np.fromiter(
            (math.fsum(row.tolist()) for row in chosen_costs),
            dtype=np.float64,
            count=len(take),
        )
        light_actual_total = np.fromiter(
            (math.fsum(row.tolist()) for row in sampled_costs[:, :, 0]),
            dtype=np.float64,
            count=len(take),
        )
        ratio[start:stop] = chosen_actual_total / light_actual_total
        for model_index in range(len(MODEL_IDS)):
            counts[start:stop, model_index] = np.sum(
                selected == model_index, axis=1
            )
    return {
        "score_q025_q50_q975": np.quantile(
            quality, (0.025, 0.5, 0.975)
        ).tolist(),
        "cost_q95_q99_max": np.quantile(
            ratio, (0.95, 0.99, 1.0)
        ).tolist(),
        "mean_counts": counts.mean(axis=0).tolist(),
        "selector_parity_checks": selector_parity_checks,
        "cost_samples": ratio,
    }


def _content_group(text: str) -> str:
    length = max(1, len(text))
    hangul_ratio = sum("가" <= item <= "힣" for item in text) / length
    multiple_choice = _MCQ.search(text) is not None
    if len(text) >= 8_000:
        return "long-8k+"
    if _CODE.search(text) is not None:
        return "code"
    if hangul_ratio >= 0.10 and multiple_choice:
        return "korean-mcq"
    if _MATH.search(text) is not None:
        return "math-reasoning"
    if hangul_ratio >= 0.10:
        return "korean-other"
    if multiple_choice:
        return "nonko-mcq"
    if len(text) <= 400:
        return "short-other"
    return "general-other"


def _group_metrics(
    groups: np.ndarray,
    predicted_scores: np.ndarray,
    predicted_costs: np.ndarray,
    actual_scores: np.ndarray,
    actual_costs: np.ndarray,
    cap: float,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    result = {}
    for group in sorted(set(groups)):
        take = np.flatnonzero(groups == group)
        selected = _route(
            predicted_scores[take], predicted_costs[take], cap
        )
        result[group] = {
            "episodes": int(len(take)),
            **_metrics(
                selected, actual_scores[take], actual_costs[take]
            ),
        }
    major = {
        name: values
        for name, values in result.items()
        if values["episodes"] >= MAJOR_GROUP_MINIMUM
    }
    if not major:
        return result, {
            "group": None,
            "episodes": 0,
            "cost": None,
        }
    worst_name, worst = max(
        major.items(), key=lambda item: item[1]["cost"]
    )
    return result, {
        "group": worst_name,
        "episodes": worst["episodes"],
        "cost": worst["cost"],
    }


def _submission(
    inputs: InputBatch,
    policy: RoutingPolicy,
    tier: str,
    choices: np.ndarray,
) -> Submission:
    return parse_submission(
        submission_to_dict(
            Submission(
                inputs.schema_version,
                inputs.challenge_id,
                policy.policy_id,
                inputs.split,
                tier,
                tuple(
                    Decision(episode.episode_id, MODEL_IDS[int(choice)])
                    for episode, choice in zip(inputs.episodes, choices)
                ),
            )
        )
    )


def _configuration_dict(tier: str) -> Mapping[str, Any]:
    configuration = PUBLIC_COST_TIER_CONFIGURATIONS[tier]
    return {
        "predicted_cost_cap": configuration.predicted_cost_cap,
        "upgrades": [
            {
                "model_id": MODEL_IDS[model_index],
                "word_head": upgrade.word_head,
                "char_score_start": upgrade.char_score_start,
                "component_weights": list(upgrade.component_weights),
                "bert_score_weight": upgrade.bert_score_weight,
                "margin": upgrade.margin,
            }
            for model_index, upgrade in enumerate(
                configuration.upgrades, start=1
            )
        ],
    }


def _conservative_configuration_dict(tier: str) -> Mapping[str, Any]:
    configuration = TIER_CONFIGURATIONS[tier]
    score_configuration = CONSERVATIVE_SCORE_CONFIGURATIONS[tier]
    return {
        "quality_upgrades": [
            {
                "model_id": MODEL_IDS[model_index],
                "word_head": upgrade.word_head,
                "char_score_start": upgrade.char_score_start,
                "component_weights": list(upgrade.component_weights),
                "bert_score_weight": upgrade.bert_score_weight,
                "margin": upgrade.margin,
            }
            for model_index, upgrade in enumerate(
                score_configuration.upgrades, start=1
            )
        ],
        "char_cost_weight": configuration.char_cost_weight,
        "predicted_cost_cap": configuration.predicted_cost_cap,
        "log_cost_margins": list(configuration.log_cost_margins),
        "forbid_short_code_k1": configuration.forbid_short_code_k1,
        "force_extreme_polynomial_light": (
            configuration.force_extreme_polynomial_light
        ),
    }


def _forced_miss_lookup(
    inputs: InputBatch, lookup: PublicCostLookup
) -> tuple[PublicCostLookup, str]:
    """Return a valid one-row lookup whose digest misses every input prompt."""

    input_digests = {
        prompt_digest(episode.prompt)
        for episode in inputs.episodes
        if episode.prompt is not None
    }
    suffix = 0
    while True:
        sentinel = prompt_digest(
            f"ossp-router-validator-forced-miss-sentinel-v1:{suffix}"
        )
        if sentinel not in input_digests:
            break
        suffix += 1
    forced = replace(
        lookup,
        digests=(sentinel,),
        costs=(lookup.costs[0],),
        training_summary={
            "scope": "validation-only forced lookup miss",
            "source_rows": len(lookup.digests),
        },
    )
    hit_count = sum(
        episode.prompt is not None
        and forced.costs_for_text(episode.prompt) is not None
        for episode in inputs.episodes
    )
    if hit_count:
        raise RuntimeError("forced-miss lookup unexpectedly matched an input")
    return forced, sentinel


def validate(
    *,
    input_path: Path,
    outcomes_path: Path,
    repetitions: int,
    seed: int,
    include_ablation: bool,
) -> Mapping[str, Any]:
    policy = load_bundled_policy()
    inputs = load_input(input_path)
    outcomes = _subset_outcomes(inputs, load_outcomes(outcomes_path))
    actual_scores, actual_costs = _actual_matrices(inputs, outcomes, policy)
    artifacts = load_bundled_artifacts(policy)
    groups = np.asarray(
        [_content_group(episode_text(episode)) for episode in inputs.episodes],
        dtype=object,
    )
    rng = np.random.default_rng(seed)
    takes = rng.integers(
        0,
        len(inputs.episodes),
        size=(repetitions, len(inputs.episodes)),
        dtype=np.int32,
    )
    tiers = {}
    submissions = []
    final_choices = {}
    for tier in TIERS:
        predicted_scores, predicted_costs, elapsed = _predict(
            inputs, tier, artifacts
        )
        cap = PUBLIC_COST_TIER_CONFIGURATIONS[tier].predicted_cost_cap
        choices = _route(predicted_scores, predicted_costs, cap)
        final_choices[tier] = choices
        submissions.append(_submission(inputs, policy, tier, choices))
        bootstrap = _rerouted_bootstrap(
            predicted_scores,
            predicted_costs,
            actual_scores,
            actual_costs,
            cap,
            takes,
        )
        groups_report, major_worst = _group_metrics(
            groups,
            predicted_scores,
            predicted_costs,
            actual_scores,
            actual_costs,
            cap,
        )
        cost_samples = bootstrap.pop("cost_samples")
        tiers[tier] = {
            "parameters": _configuration_dict(tier),
            "prediction_seconds": elapsed,
            "dev": _metrics(choices, actual_scores, actual_costs),
            "bootstrap": {
                "repetitions": repetitions,
                "seed": seed,
                **bootstrap,
                "official_limit": float(
                    policy.tiers[tier].budget_multiplier
                ),
                "exceed_count": int(
                    np.sum(
                        cost_samples
                        > float(policy.tiers[tier].budget_multiplier)
                    )
                ),
                "exceed_rate": float(
                    np.mean(
                        cost_samples
                        > float(policy.tiers[tier].budget_multiplier)
                    )
                ),
                "target_exceed_count": int(np.sum(cost_samples > cap)),
            },
            "groups": groups_report,
            "major_worst": major_worst,
        }

    official = score_submissions(
        inputs, outcomes, submissions, policy
    )
    ablations = None
    if include_ablation:
        ablations = {}
        for name, options, zero_transformer_cls in (
            ("without_bert", {"include_bert": False}, False),
            ("without_word", {"include_word": False}, False),
            ("zero_transformer_cls", {}, True),
        ):
            ablation_tiers = {}
            weighted = 0.0
            context = (
                mock.patch(
                    "ossp_router.bert_residual.encode_cls",
                    side_effect=_zero_cls_state,
                )
                if zero_transformer_cls
                else nullcontext()
            )
            with context:
                for tier in TIERS:
                    scores, costs, elapsed = _predict(
                        inputs, tier, artifacts, **options
                    )
                    cap = PUBLIC_COST_TIER_CONFIGURATIONS[
                        tier
                    ].predicted_cost_cap
                    choices = _route(scores, costs, cap)
                    metric = _metrics(choices, actual_scores, actual_costs)
                    weighted += metric["score"] * float(
                        policy.tiers[tier].weight
                    )
                    ablation_tiers[tier] = {
                        "prediction_seconds": elapsed,
                        "selection_changes": int(
                            np.sum(choices != final_choices[tier])
                        ),
                        "dev": metric,
                    }
            ablations[name] = {
                "weighted_score": weighted,
                "weighted_delta_from_final": float(
                    official["final_score"]
                )
                - weighted,
                "tiers": ablation_tiers,
            }

    return {
        "schema_version": 1,
        "report_type": "ossp-bert-router-risk-validation-v1",
        "input": {
            "episodes": len(inputs.episodes),
            "sha256_normalization": "crlf-to-lf",
            "input_sha256": _sha256(input_path),
            "outcomes_sha256": _sha256(outcomes_path),
        },
        "artifacts": {
            name: _sha256(ROOT / "src/ossp_router/resources" / name)
            for name in (
                "hash-regex-public.v1.json",
                "char-tfidf-ridge.v1.json",
                "public-content-costs.v1.json",
                "tiny-bert-residual.v1.json",
                "word-tfidf-ridge.v1.json",
            )
        },
        "official_score": official,
        "tiers": tiers,
        "ablations": ablations,
        "released_full_dev_baseline": "0.695369318182",
        "population_note": (
            "The materialized population has all 880 released Dev episodes, "
            "including the 12 source-fetched AIME prompts."
        ),
    }


def validate_all_miss(
    *,
    input_path: Path,
    outcomes_path: Path,
    repetitions: int,
    seed: int,
) -> Mapping[str, Any]:
    """Audit the packaged conservative path with every public lookup disabled.

    The prompts and outcomes remain public, so this is a control-flow and
    public-distribution risk check.  It is not evidence of private-split
    generalization.
    """

    policy = load_bundled_policy()
    inputs = load_input(input_path)
    outcomes = _subset_outcomes(inputs, load_outcomes(outcomes_path))
    actual_scores, actual_costs = _actual_matrices(inputs, outcomes, policy)
    artifacts = load_bundled_artifacts(policy)
    lookup = artifacts.public_cost_lookup
    if lookup is None:
        raise ValueError("public cost lookup artifact is unavailable")
    forced_lookup, sentinel = _forced_miss_lookup(inputs, lookup)
    forced_artifacts = replace(artifacts, public_cost_lookup=forced_lookup)
    groups = np.asarray(
        [_content_group(episode_text(episode)) for episode in inputs.episodes],
        dtype=object,
    )
    rng = np.random.default_rng(seed)
    takes = rng.integers(
        0,
        len(inputs.episodes),
        size=(repetitions, len(inputs.episodes)),
        dtype=np.int32,
    )

    tiers = {}
    submissions = []
    original_predict_episode = predict_episode
    input_index_by_identity = {
        id(episode): index for index, episode in enumerate(inputs.episodes)
    }
    for tier in TIERS:
        observed: list[Any | None] = [None] * len(inputs.episodes)

        def observe_prediction(
            episode: Any,
            observed_tier: str,
            observed_artifacts: Any,
            **kwargs: Any,
        ) -> Any:
            if observed_tier != tier:
                raise RuntimeError("runtime predicted an unexpected tier")
            row = original_predict_episode(
                episode,
                observed_tier,
                observed_artifacts,
                **kwargs,
            )
            observed[input_index_by_identity[id(episode)]] = row
            return row

        started = time.perf_counter()
        with mock.patch(
            "ossp_router.bert_router.predict_episode",
            side_effect=observe_prediction,
        ):
            runtime_selected = select_batch(inputs, tier, forced_artifacts)
        elapsed = time.perf_counter() - started
        if any(row is None for row in observed):
            raise RuntimeError("runtime did not execute the complete all-miss path")
        complete_observed = [row for row in observed if row is not None]

        predicted_scores = np.asarray(
            [
                [score_row[model_id] for model_id in MODEL_IDS]
                for score_row, _cost_row in complete_observed
            ]
        )
        predicted_costs = np.asarray(
            [
                [cost_row[model_id] for model_id in MODEL_IDS]
                for _score_row, cost_row in complete_observed
            ]
        )
        cap = TIER_CONFIGURATIONS[tier].predicted_cost_cap
        matrix_choices = _route(predicted_scores, predicted_costs, cap)
        runtime_choices = np.asarray(
            [MODEL_IDS.index(model_id) for model_id in runtime_selected]
        )
        if not np.array_equal(matrix_choices, runtime_choices):
            raise RuntimeError("all-miss matrix selector differs from runtime")
        reverse_choices = _route(
            predicted_scores[::-1], predicted_costs[::-1], cap
        )[::-1]
        if not np.array_equal(matrix_choices, reverse_choices):
            raise RuntimeError("all-miss selector differs after row reversal")

        submissions.append(
            _submission(inputs, policy, tier, runtime_choices)
        )
        bootstrap = _rerouted_bootstrap(
            predicted_scores,
            predicted_costs,
            actual_scores,
            actual_costs,
            cap,
            takes,
        )
        groups_report, major_worst = _group_metrics(
            groups,
            predicted_scores,
            predicted_costs,
            actual_scores,
            actual_costs,
            cap,
        )
        cost_samples = bootstrap.pop("cost_samples")
        official_limit = float(policy.tiers[tier].budget_multiplier)
        tiers[tier] = {
            "parameters": _conservative_configuration_dict(tier),
            "prediction_seconds": elapsed,
            "runtime_matrix_parity": True,
            "reverse_order_matrix_parity": True,
            "dev": _metrics(
                runtime_choices, actual_scores, actual_costs
            ),
            "bootstrap": {
                "repetitions": repetitions,
                "seed": seed,
                **bootstrap,
                "official_limit": official_limit,
                "exceed_count": int(np.sum(cost_samples > official_limit)),
                "exceed_rate": float(
                    np.mean(cost_samples > official_limit)
                ),
                "actual_target_exceed_count": int(
                    np.sum(cost_samples > cap)
                ),
            },
            "groups": groups_report,
            "major_worst": major_worst,
        }

    official = score_submissions(inputs, outcomes, submissions, policy)
    return {
        "schema_version": 1,
        "report_type": "ossp-bert-router-all-miss-risk-validation-v1",
        "scope": "user-supplied-public-proxy-forced-lookup-miss",
        "private_generalization_claim": False,
        "input": {
            "episodes": len(inputs.episodes),
            "sha256_normalization": "crlf-to-lf",
            "input_sha256": _sha256(input_path),
            "outcomes_sha256": _sha256(outcomes_path),
        },
        "artifacts": {
            name: _sha256(ROOT / "src/ossp_router/resources" / name)
            for name in (
                "hash-regex-public.v1.json",
                "char-tfidf-ridge.v1.json",
                "public-content-costs.v1.json",
                "tiny-bert-residual.v1.json",
                "word-tfidf-ridge.v1.json",
            )
        },
        "lookup_intervention": {
            "method": "valid one-row nonmatching SHA-256 sentinel",
            "sentinel_digest": sentinel,
            "original_rows": len(lookup.digests),
            "forced_rows": len(forced_lookup.digests),
            "hit_count": 0,
        },
        "prediction_path": {
            "representations": [
                "hash-regex",
                "char-tfidf",
                "word-tfidf",
                "tiny-bert",
            ],
            "costs": "learned risk-adjusted cost heads",
            "word_tfidf_used": True,
            "public_outcomes_passed_to_runtime_router": False,
            "public_outcomes_used_for_offline_scoring_and_risk": True,
            "fitted_artifacts_trained_on_public_outcomes": True,
        },
        "content_groups": {
            "definition_version": 1,
            "precedence": [
                "long-8k+",
                "code",
                "korean-mcq",
                "math-reasoning",
                "korean-other",
                "nonko-mcq",
                "short-other",
                "general-other",
            ],
            "major_group_minimum": MAJOR_GROUP_MINIMUM,
            "counts": {
                name: int(np.sum(groups == name))
                for name in sorted(set(groups))
            },
        },
        "official_score": official,
        "tiers": tiers,
        "ablations": None,
        "population_note": (
            "Public prompts and outcomes audit the fallback path and observed "
            "cost risk only; content-family holdout is required for a private-"
            "split generalization claim."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--outcomes", type=Path, default=DEFAULT_OUTCOMES)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--bootstrap-repetitions", type=int, default=DEFAULT_REPS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--skip-ablation", action="store_true")
    parser.add_argument(
        "--all-miss",
        action="store_true",
        help="force every public-cost lookup to miss and audit fallback routing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.bootstrap_repetitions < 1:
        raise ValueError("--bootstrap-repetitions must be positive")
    if args.all_miss:
        report = validate_all_miss(
            input_path=args.input,
            outcomes_path=args.outcomes,
            repetitions=args.bootstrap_repetitions,
            seed=args.seed,
        )
    else:
        report = validate(
            input_path=args.input,
            outcomes_path=args.outcomes,
            repetitions=args.bootstrap_repetitions,
            seed=args.seed,
            include_ablation=not args.skip_ablation,
        )
    report_path = args.report or (
        DEFAULT_ALL_MISS_REPORT if args.all_miss else DEFAULT_REPORT
    )
    write_json(report_path, report)
    summary = {
        "final_score": report["official_score"]["final_score"],
        "tiers": {
            tier: {
                "dev": report["tiers"][tier]["dev"],
                "bootstrap": report["tiers"][tier]["bootstrap"],
                "major_worst": report["tiers"][tier]["major_worst"],
            }
            for tier in TIERS
        },
        "ablations": report["ablations"],
        "report": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
