# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Leakage-controlled Dev content-family LOGO policy calibration.

The fitted hash, character, word and one-layer BERT-style prediction heads are
frozen.  For each outer fold, this validator hides one content family from
policy calibration, selects a tier policy only on the other Dev families, and
scores the held-out family once.  It therefore tests policy calibration across
families; it is deliberately not described as end-to-end unseen-family model
training because the frozen predictors were fitted on public Train rows that
can contain the same families.

The random candidate pool uses a new fixed seed and its values are generated
independently of Dev outcomes. Fold-local refinement and ranking index only the
Dev complement. Held-out score and actual-cost values are not used for policy
selection and are evaluated once after selection within each fold execution.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ossp_router.bert_router import (  # noqa: E402
    CONSERVATIVE_SCORE_CONFIGURATIONS,
    PUBLIC_COST_TIER_CONFIGURATIONS,
    TIER_CONFIGURATIONS,
    _CODE_GROUP,
    _EXTREME_INTEGER,
    load_bundled_artifacts,
    select_batch,
)
from ossp_router.hash_linear import select_models  # noqa: E402
from ossp_router.heuristic import episode_text  # noqa: E402
from ossp_router.protocol import (  # noqa: E402
    MODEL_IDS,
    InputBatch,
    OutcomeBatch,
    RoutingPolicy,
    load_bundled_policy,
    load_input,
    load_outcomes,
    policy_sha256,
)
from ossp_router.public_cost_lookup import prompt_digest  # noqa: E402
from ossp_router.scoring import SCORING_PRECISION  # noqa: E402
from ossp_router.source_manifest import source_tree_manifest  # noqa: E402


TIERS = ("fast", "balanced", "premium")
WEIGHTS = {"fast": 0.4, "balanced": 0.3, "premium": 0.3}
OFFICIAL_LIMITS = {"fast": 1.25, "balanced": 2.0, "premium": 4.0}
PUBLIC_TARGETS = {"fast": 1.20, "balanced": 1.85, "premium": 3.60}
ALL_MISS_ACTUAL_TARGETS = {"fast": 1.18, "balanced": 1.70, "premium": 3.40}
WORD_ALPHAS = ("0.1", "1", "3", "10", "30")
DEFAULT_SEED = 20_260_917
DEFAULT_RANDOM_CANDIDATES = 100
DEFAULT_REFINE_SEEDS = 2
DEFAULT_REFINE_PER_SEED = 10
DEFAULT_EXACT_SCREEN = 32
SEARCH_BISECTION_STEPS = 42
FINAL_BISECTION_STEPS = 80
USED_EXPERIMENT_SEEDS = {
    20_260_826,
    20_260_827,
    20_260_828,
    20_260_831,
    20_260_908,
}

DEFAULT_PRIMITIVES = ROOT / "build/agent-word/dev-full-primitives.npz"
DEFAULT_WORD = ROOT / "build/agent-word/word-predictions-120k.npz"
DEFAULT_HISTORICAL_PUBLIC_SEARCH_REPORT = (
    ROOT / "build/agent-word/cost-lookup-report.json"
)
DEFAULT_INPUT = ROOT / "data/materialized/dev/inputs.json"
DEFAULT_OUTCOMES = ROOT / "data/dev/outcomes.json"
DEFAULT_REPORT = ROOT / "build/bert-router-final/dev-family-logo-policy.json"
DEFAULT_ALL_MISS_REPORT = ROOT / "build/bert-router-final/all-miss-risk-5000.json"

_MCQ = re.compile(
    r"(?:^|\n)\s*(?:[A-E][.)]|[1-5][.)]|①|②|③|④|⑤)", re.MULTILINE
)
_CODE = re.compile(
    r"```|\b(?:def|class|function|return|import|SELECT|FROM|Traceback)\b",
    re.IGNORECASE,
)
_LOGIC = re.compile(r"\bIf (?:someone|something)\b", re.IGNORECASE)
_MATH = re.compile(
    r"[=+*/^<>≤≥∑∫√]|\\(?:frac|sum|sqrt|begin)|"
    r"\b(?:prove|theorem|calculate|solve|증명|계산|풀이)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchSettings:
    seed: int
    random_candidates: int
    refine_seeds: int
    refine_per_seed: int
    exact_screen: int


def _sha256(path: Path, *, normalize_newlines: bool = False) -> str:
    content = path.read_bytes()
    if normalize_newlines:
        content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def _load_npz(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as value:
        return {key: value[key] for key in value.files}


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _canonical_outcome_matrices(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
) -> tuple[np.ndarray, np.ndarray]:
    """Return protocol-parsed score/cost rows in input and model order."""

    if inputs.schema_version != outcomes.schema_version:
        raise ValueError("input/outcome schema_version mismatch")
    if inputs.challenge_id != outcomes.challenge_id:
        raise ValueError("input/outcome challenge_id mismatch")
    if inputs.split != outcomes.split:
        raise ValueError("input/outcome split mismatch")
    expected = {
        (episode.episode_id, model_id)
        for episode in inputs.episodes
        for model_id in MODEL_IDS
    }
    indexed = {
        (outcome.episode_id, outcome.model_id): outcome
        for outcome in outcomes.outcomes
    }
    if len(indexed) != len(outcomes.outcomes):
        raise ValueError("duplicate canonical outcome row")
    actual = set(indexed)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"canonical outcome coverage mismatch: missing={missing}, extra={extra}"
        )

    scores = np.empty((len(inputs.episodes), len(MODEL_IDS)), dtype=np.float64)
    costs = np.empty_like(scores)
    with localcontext() as context:
        context.prec = SCORING_PRECISION
        context.rounding = ROUND_HALF_EVEN
        token_unit = Decimal(policy.token_unit)
        for row_index, episode in enumerate(inputs.episodes):
            for model_index, model_id in enumerate(MODEL_IDS):
                outcome = indexed[(episode.episode_id, model_id)]
                rates = policy.models[model_id]
                cost = (
                    rates.fixed_cost
                    + Decimal(outcome.input_tokens)
                    * rates.input_token_rate
                    / token_unit
                    + Decimal(outcome.output_tokens)
                    * rates.output_token_rate
                    / token_unit
                )
                scores[row_index, model_index] = float(outcome.score)
                costs[row_index, model_index] = float(cost)
    return scores, costs


def _assert_cached_outcome_parity(
    primitives: Mapping[str, np.ndarray],
    canonical_scores: np.ndarray,
    canonical_costs: np.ndarray,
) -> Mapping[str, Any]:
    """Assert and report exact cached-matrix parity with canonical outcomes."""

    cached_scores = np.asarray(primitives["actual_scores"], dtype=np.float64)
    cached_costs = np.asarray(primitives["actual_costs"], dtype=np.float64)
    if cached_scores.shape != canonical_scores.shape:
        raise ValueError("cached/canonical actual score shape mismatch")
    if cached_costs.shape != canonical_costs.shape:
        raise ValueError("cached/canonical actual cost shape mismatch")
    score_max_abs = float(np.max(np.abs(cached_scores - canonical_scores)))
    cost_max_abs = float(np.max(np.abs(cached_costs - canonical_costs)))
    scores_exact = bool(np.array_equal(cached_scores, canonical_scores))
    costs_exact = bool(np.array_equal(cached_costs, canonical_costs))
    report = {
        "shape": list(canonical_scores.shape),
        "scores_exact": scores_exact,
        "costs_exact": costs_exact,
        "score_max_abs": score_max_abs,
        "cost_max_abs": cost_max_abs,
        "input_episode_order_used": True,
        "model_order": list(MODEL_IDS),
        "official_decimal_cost_formula_used": True,
    }
    if not scores_exact or not costs_exact:
        raise ValueError(f"cached canonical outcome matrix mismatch: {report}")
    return report


def _content_group(text: str) -> str:
    """Assign a content-only family with explicit precedence."""

    length = max(1, len(text))
    hangul_ratio = sum("가" <= item <= "힣" for item in text) / length
    multiple_choice = _MCQ.search(text) is not None
    if len(text) >= 8_000:
        return "long-context"
    if _CODE.search(text) is not None:
        return "code"
    if hangul_ratio >= 0.10 and multiple_choice:
        return "korean-mcq"
    if _LOGIC.search(text) is not None:
        return "logic-rules"
    if _MATH.search(text) is not None:
        return "math-reasoning"
    if multiple_choice:
        return "nonko-mcq"
    return "other"


def _simplex3(rng: np.random.Generator) -> list[float]:
    return rng.dirichlet(np.asarray([1.2, 1.2, 1.2])).tolist()


def _random_candidate(rng: np.random.Generator, tier: str) -> dict[str, Any]:
    """Generate a policy independently of any Dev outcome."""

    cap_bounds = {
        "fast": (1.02, TIER_CONFIGURATIONS["fast"].predicted_cost_cap),
        "balanced": (1.08, TIER_CONFIGURATIONS["balanced"].predicted_cost_cap),
        "premium": (1.40, TIER_CONFIGURATIONS["premium"].predicted_cost_cap),
    }
    lower_cap, upper_cap = cap_bounds[tier]
    return {
        "ax_score_alpha": str(rng.choice(WORD_ALPHAS)),
        "k1_score_alpha": str(rng.choice(WORD_ALPHAS)),
        "ax_char_start": int(0 if rng.random() < 0.65 else 3),
        "k1_char_start": int(0 if rng.random() < 0.65 else 3),
        "ax_weights": _simplex3(rng),
        "k1_weights": _simplex3(rng),
        "ax_bert_weight": float(rng.uniform(0.005, 1.4)),
        "k1_bert_weight": float(rng.uniform(0.005, 1.4)),
        "ax_margin": float(rng.uniform(-0.45, 0.45)),
        "k1_margin": float(rng.uniform(-0.65, 0.65)),
        "all_miss_cap": float(rng.uniform(lower_cap, upper_cap)),
    }


def _mutate_candidate(
    rng: np.random.Generator, candidate: Mapping[str, Any], tier: str
) -> dict[str, Any]:
    result = copy.deepcopy(dict(candidate))
    for prefix in ("ax", "k1"):
        weights = np.maximum(
            0.0,
            np.asarray(candidate[f"{prefix}_weights"], dtype=np.float64)
            + rng.normal(0.0, 0.08, 3),
        )
        if float(weights.sum()) == 0.0:
            weights[0] = 1.0
        result[f"{prefix}_weights"] = (weights / weights.sum()).tolist()
        result[f"{prefix}_bert_weight"] = float(
            np.clip(
                float(candidate[f"{prefix}_bert_weight"])
                + rng.normal(0.0, 0.08),
                0.002,
                1.8,
            )
        )
        margin_limit = 1.0
        result[f"{prefix}_margin"] = float(
            np.clip(
                float(candidate[f"{prefix}_margin"])
                + rng.normal(0.0, 0.055),
                -margin_limit,
                margin_limit,
            )
        )
    cap_scale = {"fast": 0.01, "balanced": 0.025, "premium": 0.08}[tier]
    result["all_miss_cap"] = float(
        np.clip(
            float(candidate["all_miss_cap"]) + rng.normal(0.0, cap_scale),
            1.0,
            TIER_CONFIGURATIONS[tier].predicted_cost_cap,
        )
    )
    return result


def _without_bert(candidate: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(candidate))
    result["ax_bert_weight"] = 0.0
    result["k1_bert_weight"] = 0.0
    return result


def _candidate_bytes(candidates: Sequence[Mapping[str, Any]]) -> bytes:
    return json.dumps(
        list(candidates),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _score_matrix(
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    candidate: Mapping[str, Any],
    take: np.ndarray,
) -> np.ndarray:
    result = np.zeros((len(take), len(MODEL_IDS)), dtype=np.float64)
    base_scores = primitives["base_scores"][take]
    char_heads = primitives["char_heads"][take]
    residuals = primitives["bert_residuals"][take]
    for model_index, prefix in ((1, "ax"), (2, "k1")):
        weights = np.asarray(candidate[f"{prefix}_weights"], dtype=np.float64)
        word_scores = word[f"dev_score_{candidate[f'{prefix}_score_alpha']}"][take]
        char_start = int(candidate[f"{prefix}_char_start"])
        components = (
            base_scores[:, model_index] - base_scores[:, 0],
            char_heads[:, char_start + model_index] - char_heads[:, char_start],
            word_scores[:, model_index] - word_scores[:, 0],
        )
        result[:, model_index] = sum(
            weight * component for weight, component in zip(weights, components)
        )
        result[:, model_index] += float(candidate[f"{prefix}_bert_weight"]) * (
            residuals[:, model_index] - residuals[:, 0]
        )
        result[:, model_index] += float(candidate[f"{prefix}_margin"])
    return result


def _current_conservative_candidate(tier: str) -> dict[str, Any]:
    configuration = CONSERVATIVE_SCORE_CONFIGURATIONS[tier]
    result: dict[str, Any] = {}
    for prefix, upgrade in zip(("ax", "k1"), configuration.upgrades):
        word_head = upgrade.word_head.split(":", 1)[0]
        alpha = word_head.removeprefix("score_delta_alpha_")
        if alpha not in WORD_ALPHAS:
            raise ValueError(f"unsupported conservative word head: {upgrade.word_head}")
        result.update(
            {
                f"{prefix}_score_alpha": alpha,
                f"{prefix}_char_start": upgrade.char_score_start,
                f"{prefix}_weights": list(upgrade.component_weights),
                f"{prefix}_bert_weight": upgrade.bert_score_weight,
                f"{prefix}_margin": upgrade.margin,
            }
        )
    result["all_miss_cap"] = TIER_CONFIGURATIONS[tier].predicted_cost_cap
    return result


def _current_public_candidate(tier: str) -> dict[str, Any]:
    """Translate the live exact-public router constants to validation form."""

    configuration = PUBLIC_COST_TIER_CONFIGURATIONS[tier]
    result: dict[str, Any] = {}
    for prefix, upgrade in zip(("ax", "k1"), configuration.upgrades):
        word_head = upgrade.word_head.split(":", 1)[0]
        alpha = word_head.removeprefix("score_delta_alpha_")
        if alpha not in WORD_ALPHAS:
            raise ValueError(f"unsupported public word head: {upgrade.word_head}")
        result.update(
            {
                f"{prefix}_score_alpha": alpha,
                f"{prefix}_char_start": upgrade.char_score_start,
                f"{prefix}_weights": list(upgrade.component_weights),
                f"{prefix}_bert_weight": upgrade.bert_score_weight,
                f"{prefix}_margin": upgrade.margin,
            }
        )
    # The exact-public selector obtains its cap from PUBLIC_TARGETS. Check that
    # this validation constant has not drifted away from the packaged router.
    if not math.isclose(
        PUBLIC_TARGETS[tier],
        configuration.predicted_cost_cap,
        rel_tol=0.0,
        abs_tol=0.0,
    ):
        raise ValueError(f"public cost cap drift for tier: {tier}")
    return result


def _all_miss_costs(
    primitives: Mapping[str, np.ndarray], tier: str
) -> np.ndarray:
    configuration = TIER_CONFIGURATIONS[tier]
    log_costs = (
        (1.0 - configuration.char_cost_weight)
        * primitives["base_log_costs"]
        + configuration.char_cost_weight * primitives["char_heads"][:, 6:9]
        + np.asarray(configuration.log_cost_margins, dtype=np.float64)
    )
    costs = np.exp(np.clip(log_costs, -50.0, 50.0))
    costs[:, 1] = np.maximum(costs[:, 1], costs[:, 0] * (1.0 + 1e-12))
    costs[:, 2] = np.maximum(costs[:, 2], costs[:, 1] * (1.0 + 1e-12))
    return costs


def _guard_masks(input_path: Path) -> Mapping[str, np.ndarray]:
    inputs = load_input(input_path)
    texts = [episode_text(episode) for episode in inputs.episodes]
    return {
        "short_code": np.asarray(
            [len(text) < 8_000 and _CODE_GROUP.search(text) is not None for text in texts]
        ),
        "extreme_polynomial": np.asarray(
            [
                _EXTREME_INTEGER.search(text) is not None
                and ("**" in text or "^" in text)
                and "=" in text
                for text in texts
            ]
        ),
    }


def _apply_all_miss_guards(
    scores: np.ndarray,
    masks: Mapping[str, np.ndarray],
    take: np.ndarray,
    tier: str,
) -> np.ndarray:
    result = scores.copy()
    configuration = TIER_CONFIGURATIONS[tier]
    if configuration.forbid_short_code_k1:
        result[masks["short_code"][take], 2] = -1e9
    if configuration.force_extreme_polynomial_light:
        result[masks["extreme_polynomial"][take], 1:] = -1e9
    return result


def _selector_inputs(
    *,
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    candidate: Mapping[str, Any],
    take: np.ndarray,
    tier: str,
    mode: str,
    all_miss_costs: Mapping[str, np.ndarray],
    guard_masks: Mapping[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, float]:
    scores = _score_matrix(primitives, word, candidate, take)
    if mode == "all-miss-learned-cost":
        scores = _apply_all_miss_guards(scores, guard_masks, take, tier)
        return (
            scores,
            all_miss_costs[tier][take],
            float(candidate["all_miss_cap"]),
        )
    if mode == "exact-public-cost":
        return scores, primitives["actual_costs"][take], PUBLIC_TARGETS[tier]
    raise ValueError(f"unknown validation mode: {mode}")


def _route_search(
    scores: np.ndarray,
    costs: np.ndarray,
    cap: float,
    *,
    iterations: int = SEARCH_BISECTION_STEPS,
) -> np.ndarray:
    """Fast NumPy search selector; finalists are rechecked by runtime code."""

    light_total = float(np.sum(costs[:, 0]))
    budget = light_total * cap
    rows = np.arange(len(scores))

    def choose(penalty: float) -> tuple[np.ndarray, float]:
        selected = np.argmax(scores - penalty * costs / light_total, axis=1)
        return selected, float(np.sum(costs[rows, selected]))

    selected, total = choose(0.0)
    if total <= budget:
        return selected
    low = 0.0
    high = 1.0
    selected, total = choose(high)
    while total > budget and high < 2.0**60:
        low = high
        high *= 2.0
        selected, total = choose(high)
    if total > budget:
        return np.zeros(len(scores), dtype=np.int64)
    for _step in range(iterations):
        middle = (low + high) * 0.5
        choices, candidate_total = choose(middle)
        if candidate_total <= budget:
            high = middle
            selected = choices
        else:
            low = middle
    return selected


def _route_exact(scores: np.ndarray, costs: np.ndarray, cap: float) -> np.ndarray:
    score_rows = tuple(dict(zip(MODEL_IDS, row)) for row in scores)
    cost_rows = tuple(dict(zip(MODEL_IDS, row)) for row in costs)
    selected, _ratio = select_models(
        score_rows,
        cost_rows,
        budget_multiplier=cap,
        safety_ratio=1.0,
    )
    return np.asarray([MODEL_IDS.index(model_id) for model_id in selected])


def _metrics(
    actual_scores: np.ndarray,
    actual_costs: np.ndarray,
    choices: np.ndarray,
) -> dict[str, Any]:
    rows = np.arange(len(choices))
    return {
        "score": float(actual_scores[rows, choices].mean()),
        "cost": float(
            math.fsum(actual_costs[rows, choices].tolist())
            / math.fsum(actual_costs[:, 0].tolist())
        ),
        "counts": [
            int(np.sum(choices == model_index))
            for model_index in range(len(MODEL_IDS))
        ],
    }


def _evaluate_search(
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    candidate: Mapping[str, Any],
    take: np.ndarray,
    tier: str,
    mode: str,
    all_miss_costs: Mapping[str, np.ndarray],
    guard_masks: Mapping[str, np.ndarray],
) -> dict[str, Any]:
    scores, costs, cap = _selector_inputs(
        primitives=primitives,
        word=word,
        candidate=candidate,
        take=take,
        tier=tier,
        mode=mode,
        all_miss_costs=all_miss_costs,
        guard_masks=guard_masks,
    )
    choices = _route_search(scores, costs, cap)
    return _metrics(
        primitives["actual_scores"][take],
        primitives["actual_costs"][take],
        choices,
    )


def _evaluate_numpy_final(
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    candidate: Mapping[str, Any],
    take: np.ndarray,
    tier: str,
    mode: str,
    all_miss_costs: Mapping[str, np.ndarray],
    guard_masks: Mapping[str, np.ndarray],
) -> dict[str, Any]:
    """Use the full search precision before the scalar runtime parity check."""

    scores, costs, cap = _selector_inputs(
        primitives=primitives,
        word=word,
        candidate=candidate,
        take=take,
        tier=tier,
        mode=mode,
        all_miss_costs=all_miss_costs,
        guard_masks=guard_masks,
    )
    choices = _route_search(
        scores,
        costs,
        cap,
        iterations=FINAL_BISECTION_STEPS,
    )
    return _metrics(
        primitives["actual_scores"][take],
        primitives["actual_costs"][take],
        choices,
    )


def _evaluate_exact(
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    candidate: Mapping[str, Any],
    take: np.ndarray,
    tier: str,
    mode: str,
    all_miss_costs: Mapping[str, np.ndarray],
    guard_masks: Mapping[str, np.ndarray],
) -> tuple[dict[str, Any], np.ndarray]:
    scores, costs, cap = _selector_inputs(
        primitives=primitives,
        word=word,
        candidate=candidate,
        take=take,
        tier=tier,
        mode=mode,
        all_miss_costs=all_miss_costs,
        guard_masks=guard_masks,
    )
    choices = _route_exact(scores, costs, cap)
    return (
        _metrics(
            primitives["actual_scores"][take],
            primitives["actual_costs"][take],
            choices,
        ),
        choices,
    )


def _rank_key(row: Mapping[str, Any]) -> tuple[bool, float, float, str]:
    return (
        not bool(row.get("calibration_cost_feasible", True)),
        -float(row["metric"]["score"]),
        float(row["metric"]["cost"]),
        str(row["digest"]),
    )


def _candidate_digest(candidate: Mapping[str, Any]) -> str:
    return hashlib.sha256(_candidate_bytes([candidate])).hexdigest()


def _fold_seed(seed: int, group: str, tier: str) -> int:
    digest = hashlib.sha256(f"{seed}:{group}:{tier}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _calibrate_fold(
    *,
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    calibration_take: np.ndarray,
    tier: str,
    group: str,
    base_candidates: Sequence[Mapping[str, Any]],
    settings: SearchSettings,
    mode: str,
    all_miss_costs: Mapping[str, np.ndarray],
    guard_masks: Mapping[str, np.ndarray],
    groups: np.ndarray,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Fit one tier policy using the outer-fold Dev complement only."""

    actual_target = _mode_actual_target(mode, tier)
    evaluated = []
    for candidate in base_candidates:
        metric = _evaluate_search(
            primitives,
            word,
            candidate,
            calibration_take,
            tier,
            mode,
            all_miss_costs,
            guard_masks,
        )
        evaluated.append(
            {
                "candidate": candidate,
                "digest": _candidate_digest(candidate),
                "metric": metric,
                "calibration_cost_feasible": (
                    float(metric["cost"]) <= actual_target + 1e-12
                ),
                "origin": "outcome-independent-random-pool",
            }
        )
    evaluated.sort(key=_rank_key)

    rng = np.random.default_rng(_fold_seed(settings.seed, group, tier))
    refined = []
    safe_seeds = [row for row in evaluated if row["calibration_cost_feasible"]]
    if not safe_seeds:
        raise RuntimeError(f"no safe random seed policy for {mode}/{group}/{tier}")
    for seed_row in safe_seeds[: settings.refine_seeds]:
        for _round in range(settings.refine_per_seed):
            candidate = _mutate_candidate(rng, seed_row["candidate"], tier)
            metric = _evaluate_search(
                primitives,
                word,
                candidate,
                calibration_take,
                tier,
                mode,
                all_miss_costs,
                guard_masks,
            )
            refined.append(
                {
                    "candidate": candidate,
                    "digest": _candidate_digest(candidate),
                    "metric": metric,
                    "calibration_cost_feasible": (
                        float(metric["cost"]) <= actual_target + 1e-12
                    ),
                    "origin": "complement-only-refinement",
                }
            )
    evaluated.extend(refined)
    evaluated.sort(key=_rank_key)

    screened_rows = []
    seen = set()
    for row in evaluated:
        if row["digest"] in seen:
            continue
        seen.add(row["digest"])
        metric = _evaluate_numpy_final(
            primitives,
            word,
            row["candidate"],
            calibration_take,
            tier,
            mode,
            all_miss_costs,
            guard_masks,
        )
        no_bert_metric = _evaluate_numpy_final(
            primitives,
            word,
            _without_bert(row["candidate"]),
            calibration_take,
            tier,
            mode,
            all_miss_costs,
            guard_masks,
        )
        calibration_group_costs = {}
        if mode == "all-miss-learned-cost":
            for calibration_group in sorted(
                set(str(value) for value in groups[calibration_take])
            ):
                group_take = np.flatnonzero(groups == calibration_group)
                group_metric = _evaluate_numpy_final(
                    primitives,
                    word,
                    row["candidate"],
                    group_take,
                    tier,
                    mode,
                    all_miss_costs,
                    guard_masks,
                )
                calibration_group_costs[calibration_group] = {
                    "rows": int(len(group_take)),
                    "cost": group_metric["cost"],
                }
        maximum_group_cost = max(
            (float(value["cost"]) for value in calibration_group_costs.values()),
            default=float(metric["cost"]),
        )
        screened_rows.append(
            {
                **row,
                "metric": metric,
                "without_bert_metric": no_bert_metric,
                "bert_nonregression": (
                    float(metric["score"]) + 1e-15
                    >= float(no_bert_metric["score"])
                ),
                "calibration_cost_feasible": (
                    float(metric["cost"]) <= actual_target + 1e-12
                ),
                "calibration_group_costs": calibration_group_costs,
                "maximum_calibration_group_cost": maximum_group_cost,
                "calibration_group_cost_feasible": (
                    maximum_group_cost <= actual_target + 1e-12
                ),
            }
        )
        if len(screened_rows) >= settings.exact_screen:
            break
    screened_rows.sort(key=_rank_key)
    feasible = [
        row
        for row in screened_rows
        if row["bert_nonregression"]
        and row["calibration_cost_feasible"]
        and row["calibration_group_cost_feasible"]
    ]
    if not feasible:
        raise RuntimeError(
            f"no complement-safe BERT-nonregressing policy for {mode}/{group}/{tier}"
        )
    ranked = feasible

    # Only finalist policies enter the slower scalar selector.  If a NumPy
    # boundary choice changes the complement BERT screen, advance to the next
    # policy; the held-out family remains untouched.
    selected = None
    runtime_checks = 0
    for row in ranked:
        runtime_metric, _choices = _evaluate_exact(
            primitives,
            word,
            row["candidate"],
            calibration_take,
            tier,
            mode,
            all_miss_costs,
            guard_masks,
        )
        runtime_no_bert, _choices = _evaluate_exact(
            primitives,
            word,
            _without_bert(row["candidate"]),
            calibration_take,
            tier,
            mode,
            all_miss_costs,
            guard_masks,
        )
        runtime_checks += 1
        runtime_group_costs = {}
        if mode == "all-miss-learned-cost":
            for calibration_group in sorted(
                set(str(value) for value in groups[calibration_take])
            ):
                group_take = np.flatnonzero(groups == calibration_group)
                group_metric, _choices = _evaluate_exact(
                    primitives,
                    word,
                    row["candidate"],
                    group_take,
                    tier,
                    mode,
                    all_miss_costs,
                    guard_masks,
                )
                runtime_group_costs[calibration_group] = {
                    "rows": int(len(group_take)),
                    "cost": group_metric["cost"],
                }
        runtime_maximum_group_cost = max(
            (float(value["cost"]) for value in runtime_group_costs.values()),
            default=float(runtime_metric["cost"]),
        )
        if (
            float(runtime_metric["score"]) + 1e-15
            >= float(runtime_no_bert["score"])
        ) and float(runtime_metric["cost"]) <= actual_target + 1e-12 and (
            runtime_maximum_group_cost <= actual_target + 1e-12
        ):
            selected = {
                **row,
                "metric": runtime_metric,
                "without_bert_metric": runtime_no_bert,
                "runtime_calibration_group_costs": runtime_group_costs,
                "runtime_maximum_calibration_group_cost": (
                    runtime_maximum_group_cost
                ),
            }
            break
    if selected is None:
        raise RuntimeError("runtime BERT screen rejected every finalist")
    audit = {
        "calibration_rows": int(len(calibration_take)),
        "random_candidates": len(base_candidates),
        "refined_candidates": len(refined),
        "numpy_final_screened_candidates": len(screened_rows),
        "bert_nonregression_candidates": len(feasible),
        "bert_nonregression_required": bool(feasible),
        "actual_calibration_cost_target": actual_target,
        "runtime_finalist_checks": runtime_checks,
        "runtime_selector_finalist_rechecked": True,
        "numpy_vs_runtime_finalist_choice_parity_claimed": False,
        "selected_origin": selected["origin"],
        "selected_digest": selected["digest"],
        "selected_calibration": selected["metric"],
        "selected_without_bert_calibration": selected["without_bert_metric"],
        "selected_calibration_group_costs": selected[
            "runtime_calibration_group_costs"
        ],
        "selected_maximum_calibration_group_cost": selected[
            "runtime_maximum_calibration_group_cost"
        ],
    }
    return dict(selected["candidate"]), audit


def _current_reference(
    *,
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    groups: np.ndarray,
    mode: str,
    all_miss_costs: Mapping[str, np.ndarray],
    guard_masks: Mapping[str, np.ndarray],
) -> tuple[Mapping[str, Any], Mapping[str, np.ndarray]]:
    """Return a clearly labelled full-Dev-tuned, in-sample reference."""

    tiers = {}
    choices_by_tier = {}
    weighted = 0.0
    all_take = np.arange(len(groups), dtype=np.int64)
    for tier in TIERS:
        candidate = (
            _current_conservative_candidate(tier)
            if mode == "all-miss-learned-cost"
            else _current_public_candidate(tier)
        )
        full, choices = _evaluate_exact(
            primitives,
            word,
            candidate,
            all_take,
            tier,
            mode,
            all_miss_costs,
            guard_masks,
        )
        choices_by_tier[tier] = choices
        weighted += WEIGHTS[tier] * float(full["score"])
        group_rows = {}
        for group in sorted(set(str(value) for value in groups)):
            take = np.flatnonzero(groups == group)
            metric, _choices = _evaluate_exact(
                primitives,
                word,
                candidate,
                take,
                tier,
                mode,
                all_miss_costs,
                guard_masks,
            )
            group_rows[group] = {"rows": int(len(take)), **metric}
        tiers[tier] = {"full_dev": full, "group_reroute": group_rows}
    return (
        {
            "mode": mode,
            "evidence_class": "in-sample-full-dev-tuned-reference-only",
            "used_for_logo_policy_selection": False,
            "weighted_score": weighted,
            "tiers": tiers,
        },
        choices_by_tier,
    )


def _runtime_all_miss_parity(
    input_path: Path,
    reconstructed: Mapping[str, np.ndarray],
) -> Mapping[str, Any]:
    """Compare cached-component reconstruction with the packaged runtime."""

    inputs = load_input(input_path)
    policy = load_bundled_policy()
    artifacts = load_bundled_artifacts(policy)
    lookup = artifacts.public_cost_lookup
    if lookup is None:
        raise ValueError("packaged public-cost lookup is missing")
    input_digests = {
        prompt_digest(episode.prompt)
        for episode in inputs.episodes
        if episode.prompt is not None
    }
    suffix = 0
    while True:
        sentinel = prompt_digest(f"ossp-logo-forced-miss-v1:{suffix}")
        if sentinel not in input_digests:
            break
        suffix += 1
    forced_lookup = replace(
        lookup,
        digests=(sentinel,),
        costs=(lookup.costs[0],),
        training_summary={"scope": "LOGO runtime parity forced miss"},
    )
    forced_artifacts = replace(artifacts, public_cost_lookup=forced_lookup)
    tiers = {}
    for tier in TIERS:
        runtime = select_batch(inputs, tier, forced_artifacts)
        runtime_choices = np.asarray(
            [MODEL_IDS.index(model_id) for model_id in runtime], dtype=np.int64
        )
        mismatch = int(np.sum(runtime_choices != reconstructed[tier]))
        tiers[tier] = {
            "rows": len(runtime_choices),
            "mismatches": mismatch,
            "matched": mismatch == 0,
        }
    return {
        "forced_lookup_rows": 1,
        "forced_lookup_hit_count": 0,
        "sentinel_digest": sentinel,
        "tiers": tiers,
        "all_tiers_matched": all(row["matched"] for row in tiers.values()),
    }


def _saved_all_miss_parity(
    reference: Mapping[str, Any], saved_report: Mapping[str, Any]
) -> Mapping[str, Any]:
    tiers = {}
    for tier in TIERS:
        reconstructed = reference["tiers"][tier]["full_dev"]
        saved = saved_report["tiers"][tier]["dev"]
        score_delta = float(reconstructed["score"]) - float(saved["score"])
        cost_delta = float(reconstructed["cost"]) - float(saved["cost"])
        counts_match = reconstructed["counts"] == saved["counts"]
        tiers[tier] = {
            "score_delta": score_delta,
            "cost_delta": cost_delta,
            "counts_match": counts_match,
            "matched": (
                abs(score_delta) <= 1e-15
                and abs(cost_delta) <= 1e-12
                and counts_match
            ),
        }
    return {
        "tiers": tiers,
        "all_tiers_matched": all(row["matched"] for row in tiers.values()),
        "row_level_parity_from_saved_report_claimed": False,
    }


def _validate_inputs(
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    input_path: Path,
) -> tuple[np.ndarray, Mapping[str, int]]:
    inputs = load_input(input_path)
    episode_ids = np.asarray(
        [episode.episode_id for episode in inputs.episodes]
    )
    if not np.array_equal(episode_ids, primitives["episode_ids"]):
        raise ValueError("primitive rows do not match materialized Dev episode order")
    rows = len(inputs.episodes)
    required_primitive = (
        "base_scores",
        "char_heads",
        "bert_residuals",
        "actual_scores",
        "actual_costs",
    )
    for name in required_primitive:
        if len(primitives[name]) != rows:
            raise ValueError(f"primitive row mismatch for {name}")
        if not np.all(np.isfinite(primitives[name])):
            raise ValueError(f"non-finite primitive values for {name}")
    for alpha in WORD_ALPHAS:
        name = f"dev_score_{alpha}"
        if name not in word or word[name].shape != (rows, len(MODEL_IDS)):
            raise ValueError(f"missing or invalid word prediction head: {name}")
        if not np.all(np.isfinite(word[name])):
            raise ValueError(f"non-finite word prediction head: {name}")
    groups = np.asarray(
        [_content_group(episode_text(episode)) for episode in inputs.episodes]
    )
    counts = {
        group: int(np.sum(groups == group))
        for group in sorted(set(str(value) for value in groups))
    }
    expected = {
        "code",
        "korean-mcq",
        "logic-rules",
        "long-context",
        "math-reasoning",
        "nonko-mcq",
        "other",
    }
    if set(counts) != expected or any(count < 40 for count in counts.values()):
        raise ValueError(f"unexpected content-family partition: {counts}")
    return groups, counts


def _mode_actual_target(mode: str, tier: str) -> float:
    if mode == "all-miss-learned-cost":
        return ALL_MISS_ACTUAL_TARGETS[tier]
    if mode == "exact-public-cost":
        return PUBLIC_TARGETS[tier]
    raise ValueError(f"unknown validation mode: {mode}")


def _run_logo_mode(
    *,
    mode: str,
    primitives: Mapping[str, np.ndarray],
    word: Mapping[str, np.ndarray],
    groups: np.ndarray,
    counts: Mapping[str, int],
    candidate_pools: Mapping[str, Sequence[Mapping[str, Any]]],
    settings: SearchSettings,
    all_miss_costs: Mapping[str, np.ndarray],
    guard_masks: Mapping[str, np.ndarray],
) -> Mapping[str, Any]:
    all_groups = tuple(sorted(counts))
    stitched = {
        tier: np.full(len(groups), -1, dtype=np.int64) for tier in TIERS
    }
    folds = {}
    print(f"mode {mode}", flush=True)
    for group in all_groups:
        heldout_take = np.flatnonzero(groups == group)
        calibration_take = np.flatnonzero(groups != group)
        fold_tiers = {}
        print(
            f"fold {group}: calibration={len(calibration_take)} "
            f"heldout={len(heldout_take)}",
            flush=True,
        )
        for tier in TIERS:
            candidate, calibration_audit = _calibrate_fold(
                primitives=primitives,
                word=word,
                calibration_take=calibration_take,
                tier=tier,
                group=group,
                base_candidates=candidate_pools[tier],
                settings=settings,
                mode=mode,
                all_miss_costs=all_miss_costs,
                guard_masks=guard_masks,
                groups=groups,
            )
            # This is the sole held-out quality evaluation for this LOGO policy.
            heldout_metric, heldout_choices = _evaluate_exact(
                primitives,
                word,
                candidate,
                heldout_take,
                tier,
                mode,
                all_miss_costs,
                guard_masks,
            )
            stitched[tier][heldout_take] = heldout_choices
            actual_target = _mode_actual_target(mode, tier)
            fold_tiers[tier] = {
                "policy": candidate,
                "calibration": calibration_audit,
                "heldout": heldout_metric,
                "actual_cost_target": actual_target,
                "selector_predicted_cost_cap": (
                    float(candidate["all_miss_cap"])
                    if mode == "all-miss-learned-cost"
                    else PUBLIC_TARGETS[tier]
                ),
                "official_cost_limit": OFFICIAL_LIMITS[tier],
                "target_passed": (
                    float(heldout_metric["cost"]) <= actual_target + 1e-12
                ),
                "official_budget_passed": (
                    float(heldout_metric["cost"])
                    <= OFFICIAL_LIMITS[tier] + 1e-12
                ),
                "heldout_quality_evaluations": 1,
            }
            print(
                f"  {tier}: score={heldout_metric['score']:.12f} "
                f"cost={heldout_metric['cost']:.12f} "
                f"counts={heldout_metric['counts']}",
                flush=True,
            )
        folds[group] = {
            "heldout_rows": int(len(heldout_take)),
            "calibration_rows": int(len(calibration_take)),
            "tiers": fold_tiers,
            "weighted_heldout_score": sum(
                WEIGHTS[tier] * float(fold_tiers[tier]["heldout"]["score"])
                for tier in TIERS
            ),
        }

    if any(np.any(choices < 0) for choices in stitched.values()):
        raise RuntimeError("LOGO stitched choices do not cover every Dev row")
    stitched_report = {}
    weighted_score = 0.0
    for tier in TIERS:
        metric = _metrics(
            primitives["actual_scores"],
            primitives["actual_costs"],
            stitched[tier],
        )
        metric["official_budget_passed"] = (
            float(metric["cost"]) <= OFFICIAL_LIMITS[tier] + 1e-12
        )
        metric["target_passed"] = (
            float(metric["cost"]) <= _mode_actual_target(mode, tier) + 1e-12
        )
        stitched_report[tier] = metric
        weighted_score += WEIGHTS[tier] * float(metric["score"])

    worst = {
        tier: {
            "lowest_score": min(
                (
                    {
                        "group": group,
                        **folds[group]["tiers"][tier]["heldout"],
                    }
                    for group in all_groups
                ),
                key=lambda row: (float(row["score"]), row["group"]),
            ),
            "highest_cost": max(
                (
                    {
                        "group": group,
                        **folds[group]["tiers"][tier]["heldout"],
                    }
                    for group in all_groups
                ),
                key=lambda row: (float(row["cost"]), row["group"]),
            ),
        }
        for tier in TIERS
    }
    lowest_weighted = min(
        (
            {
                "group": group,
                "rows": folds[group]["heldout_rows"],
                "weighted_score": folds[group]["weighted_heldout_score"],
            }
            for group in all_groups
        ),
        key=lambda row: (float(row["weighted_score"]), row["group"]),
    )
    all_light_score = float(primitives["actual_scores"][:, 0].mean())
    return {
        "mode": mode,
        "selector_cost_source": (
            "frozen-learned-hash-character-cost-heads"
            if mode == "all-miss-learned-cost"
            else "exact-public-outcome-costs"
        ),
        "calibration_feasibility_cost_source": (
            "canonical actual public outcome costs on the Dev complement"
        ),
        "heldout_metric_cost_source": (
            "canonical actual public outcome costs after policy selection"
        ),
        "folds": folds,
        "stitched": {
            "weighted_score": weighted_score,
            "all_light_weighted_score": all_light_score,
            "weighted_gain_over_all_light": weighted_score - all_light_score,
            "tiers": stitched_report,
        },
        "worst_folds": worst,
        "lowest_weighted_fold": lowest_weighted,
        "all_fold_tier_official_budgets_passed": all(
            folds[group]["tiers"][tier]["official_budget_passed"]
            for group in all_groups
            for tier in TIERS
        ),
        "all_fold_tier_targets_passed": all(
            folds[group]["tiers"][tier]["target_passed"]
            for group in all_groups
            for tier in TIERS
        ),
    }


def validate(
    *,
    primitives_path: Path,
    word_path: Path,
    input_path: Path,
    outcomes_path: Path,
    historical_public_search_report_path: Path,
    saved_all_miss_report_path: Path,
    settings: SearchSettings,
) -> Mapping[str, Any]:
    if settings.seed in USED_EXPERIMENT_SEEDS:
        raise ValueError("LOGO seed must differ from every recorded policy-search seed")
    if min(
        settings.random_candidates,
        settings.refine_seeds,
        settings.refine_per_seed,
        settings.exact_screen,
    ) < 1:
        raise ValueError("all search sizes must be positive")
    started = time.perf_counter()
    primitives = _load_npz(primitives_path)
    word = _load_npz(word_path)
    saved_all_miss_report = _load_json(saved_all_miss_report_path)
    groups, counts = _validate_inputs(primitives, word, input_path)
    inputs = load_input(input_path)
    outcomes = load_outcomes(outcomes_path)
    policy = load_bundled_policy()
    canonical_scores, canonical_costs = _canonical_outcome_matrices(
        inputs, outcomes, policy
    )
    canonical_outcome_parity = _assert_cached_outcome_parity(
        primitives, canonical_scores, canonical_costs
    )
    outcome_episode_rows = len(
        {outcome.episode_id for outcome in outcomes.outcomes}
    )
    outcome_model_rows = len(outcomes.outcomes)
    guard_masks = _guard_masks(input_path)
    learned_costs = {
        tier: _all_miss_costs(primitives, tier) for tier in TIERS
    }

    pool_rng = np.random.default_rng(settings.seed)
    candidate_pools = {
        tier: tuple(
            _random_candidate(pool_rng, tier)
            for _index in range(settings.random_candidates)
        )
        for tier in TIERS
    }
    pool_hashes = {
        tier: hashlib.sha256(_candidate_bytes(candidate_pools[tier])).hexdigest()
        for tier in TIERS
    }
    all_miss_logo = _run_logo_mode(
        mode="all-miss-learned-cost",
        primitives=primitives,
        word=word,
        groups=groups,
        counts=counts,
        candidate_pools=candidate_pools,
        settings=settings,
        all_miss_costs=learned_costs,
        guard_masks=guard_masks,
    )
    exact_cost_logo = _run_logo_mode(
        mode="exact-public-cost",
        primitives=primitives,
        word=word,
        groups=groups,
        counts=counts,
        candidate_pools=candidate_pools,
        settings=settings,
        all_miss_costs=learned_costs,
        guard_masks=guard_masks,
    )
    all_miss_reference, reconstructed_choices = _current_reference(
        primitives=primitives,
        word=word,
        groups=groups,
        mode="all-miss-learned-cost",
        all_miss_costs=learned_costs,
        guard_masks=guard_masks,
    )
    exact_reference, _choices = _current_reference(
        primitives=primitives,
        word=word,
        groups=groups,
        mode="exact-public-cost",
        all_miss_costs=learned_costs,
        guard_masks=guard_masks,
    )
    aggregate_parity = _saved_all_miss_parity(
        all_miss_reference, saved_all_miss_report
    )
    if not aggregate_parity["all_tiers_matched"]:
        raise RuntimeError("cached matrices do not reconstruct saved all-miss metrics")
    runtime_parity = _runtime_all_miss_parity(input_path, reconstructed_choices)
    if not runtime_parity["all_tiers_matched"]:
        raise RuntimeError("cached matrices differ from packaged all-miss runtime")

    resource_root = ROOT / "src/ossp_router/resources"
    artifact_names = (
        "hash-regex-public.v1.json",
        "char-tfidf-ridge.v1.json",
        "word-tfidf-ridge.v1.json",
        "tiny-bert-residual.v1.json",
        "public-content-costs.v1.json",
    )
    manifest = source_tree_manifest(ROOT)

    return {
        "schema_version": 1,
        "report_type": "ossp-dev-family-logo-policy-calibration-v1",
        "scope": "dev-family-logo-policy-calibration",
        "evidence_class": "outer-family-heldout-policy-calibration",
        "method": {
            "outer_partition": (
                "seven mutually exclusive content-only families; one complete "
                "family held out per fold"
            ),
            "policy_fit": (
                "Initial random-pool values are generated solely from the seed; "
                "refinement, ranking, actual-cost feasibility, family safety, and "
                "the BERT non-regression screen use only the Dev complement."
            ),
            "heldout_use": (
                "Held-out actual score and cost values are not used for policy "
                "selection. They are evaluated once after selection for the final "
                "held-out metric within each deterministic fold execution."
            ),
            "model_refit": False,
            "policy_recalibrated_per_fold": True,
            "current_full_dev_policy_used_as_seed": False,
            "current_full_dev_policy_read_from_live_router_constants": True,
            "recorded_search_seed_reused": False,
            "primary_mode": "all-miss-learned-cost",
            "secondary_mode": "exact-public-cost",
            "exact_costs_used_as_selector_input": False,
            "exact_costs_used_for_complement_calibration": True,
            "heldout_actual_costs_used_before_policy_selection": False,
            "heldout_actual_costs_used_for_final_evaluation": True,
            "primary_cost_reconstruction": (
                "exp((1-char_cost_weight)*base_log_costs + "
                "char_cost_weight*char_cost_heads + live_log_cost_margins), "
                "with live tier guards and candidate all-miss cap"
            ),
            "primary_calibration_cost_audit": (
                "Complement aggregate and every remaining complement family "
                "are rerouted and checked against the actual-cost target."
            ),
            "search_selector": (
                f"NumPy selector with {SEARCH_BISECTION_STEPS} bisections; "
                f"top candidates receive a {FINAL_BISECTION_STEPS}-step NumPy "
                "screen before sequential runtime finalist rechecks"
            ),
            "runtime_finalist_selector": (
                "shared select_models implementation with math.fsum; no "
                "NumPy-versus-runtime choice parity is claimed for fold policies"
            ),
            "confirmatory_claim": False,
            "method_development_used_public_dev_diagnostics": True,
        },
        "limitations": {
            "end_to_end_unseen_family_model_training": False,
            "frozen_predictors": [
                "hash-regex",
                "character-tfidf",
                "word-tfidf",
                "one-layer-bert-style-residual",
            ],
            "train_family_overlap_possible": True,
            "explanation": (
                "Frozen predictors were fitted on public Train and can contain "
                "the held-out Dev family. This report isolates Dev policy-calibration "
                "leakage; it does not estimate end-to-end unseen-family training."
            ),
            "private_split_generalization_claim": False,
            "secondary_exact_selector_caveat": (
                "Only the separately labelled secondary public-hit mode feeds "
                "exact public costs into the selector. Both modes use held-out "
                "actual score and cost only after selection to report metrics."
            ),
            "family_definition": (
                "Heuristic content families are broad proxies, not hidden source labels."
            ),
        },
        "configuration": {
            "seed": settings.seed,
            "random_candidates_per_tier": settings.random_candidates,
            "refine_seed_policies_per_fold": settings.refine_seeds,
            "refine_per_seed": settings.refine_per_seed,
            "exact_screen_per_fold": settings.exact_screen,
            "reproduction_search_arguments": [
                "--seed",
                str(settings.seed),
                "--random-candidates",
                str(settings.random_candidates),
                "--refine-seeds",
                str(settings.refine_seeds),
                "--refine-per-seed",
                str(settings.refine_per_seed),
                "--exact-screen",
                str(settings.exact_screen),
            ],
            "word_alphas": list(WORD_ALPHAS),
            "target_cost_ratios": PUBLIC_TARGETS,
            "all_miss_actual_calibration_targets": ALL_MISS_ACTUAL_TARGETS,
            "official_cost_limits": OFFICIAL_LIMITS,
            "candidate_pool_sha256": pool_hashes,
        },
        "data": {
            "rows": int(len(groups)),
            "content_group_definition_version": 2,
            "group_precedence": [
                "long-context",
                "code",
                "korean-mcq",
                "logic-rules",
                "math-reasoning",
                "nonko-mcq",
                "other",
            ],
            "group_counts": counts,
            "input_sha256_crlf_to_lf": _sha256(
                input_path, normalize_newlines=True
            ),
            "outcomes_sha256_crlf_to_lf": _sha256(
                outcomes_path, normalize_newlines=True
            ),
            "outcome_episode_rows": outcome_episode_rows,
            "outcome_model_rows": outcome_model_rows,
            "primitives_sha256": _sha256(primitives_path),
            "word_predictions_sha256": _sha256(word_path),
            "validation_script_sha256": _sha256(Path(__file__)),
            "historical_public_search_report": {
                "path": str(
                    historical_public_search_report_path.relative_to(ROOT)
                ).replace("\\", "/"),
                "sha256": _sha256(historical_public_search_report_path),
                "used_for_current_reference": False,
                "used_for_logo_policy_selection": False,
                "note": (
                    "Historical source-search evidence only; live exact-public "
                    "reference parameters come from bert_router.py."
                ),
            },
            "saved_all_miss_report_sha256": _sha256(saved_all_miss_report_path),
            "packaged_artifacts_sha256": {
                name: _sha256(resource_root / name) for name in artifact_names
            },
            "bert_router_source_sha256": _sha256(
                ROOT / "src/ossp_router/bert_router.py"
            ),
            "policy_sha256": policy_sha256(policy),
            "source_manifest_sha256": manifest["sha256"],
            "source_manifest_entries": len(manifest["entries"]),
            "cache_provenance": {
                "generator": "build/agent-word/experiment.py",
                "generator_sha256": _sha256(
                    ROOT / "build/agent-word/experiment.py"
                ),
                "policy_search": "build/agent-word/cost_lookup_search.py",
                "policy_search_sha256": _sha256(
                    ROOT / "build/agent-word/cost_lookup_search.py"
                ),
            },
        },
        "matrix_validation": {
            "canonical_outcomes_vs_cached_actual_matrices": (
                canonical_outcome_parity
            ),
            "current_all_miss_saved_aggregate_parity": aggregate_parity,
            "current_all_miss_runtime_row_parity": runtime_parity,
            "arbitrary_fold_policy_runtime_parity_claimed": False,
            "current_exact_public_runtime_row_parity_checked": False,
            "cached_prediction_matrix_numeric_max_abs_runtime_parity_checked": (
                False
            ),
            "limitation": (
                "Fold policies are research-only recalibrations not exported as "
                "runtime constants. Cached reconstruction choices are row-validated "
                "against the packaged current all-miss policy, and cached actual "
                "score/cost matrices exactly match protocol-parsed canonical "
                "outcomes. No numeric max-abs runtime comparison of prediction "
                "matrices or live exact-public row parity is claimed. Arbitrary "
                "fold policies cannot be invoked through the unmodified runtime."
            ),
        },
        "logo_primary_all_miss_learned_cost": all_miss_logo,
        "logo_secondary_exact_public_cost": exact_cost_logo,
        "in_sample_reference": {
            "all_miss_learned_cost": all_miss_reference,
            "exact_public_cost": exact_reference,
        },
        "elapsed_seconds": time.perf_counter() - started,
    }


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primitives", type=Path, default=DEFAULT_PRIMITIVES)
    parser.add_argument("--word-predictions", type=Path, default=DEFAULT_WORD)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--outcomes", type=Path, default=DEFAULT_OUTCOMES)
    parser.add_argument(
        "--historical-public-search-report",
        type=Path,
        default=DEFAULT_HISTORICAL_PUBLIC_SEARCH_REPORT,
    )
    parser.add_argument(
        "--saved-all-miss-report", type=Path, default=DEFAULT_ALL_MISS_REPORT
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--random-candidates", type=int, default=DEFAULT_RANDOM_CANDIDATES
    )
    parser.add_argument("--refine-seeds", type=int, default=DEFAULT_REFINE_SEEDS)
    parser.add_argument(
        "--refine-per-seed", type=int, default=DEFAULT_REFINE_PER_SEED
    )
    parser.add_argument("--exact-screen", type=int, default=DEFAULT_EXACT_SCREEN)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = validate(
        primitives_path=args.primitives,
        word_path=args.word_predictions,
        input_path=args.input,
        outcomes_path=args.outcomes,
        historical_public_search_report_path=(
            args.historical_public_search_report
        ),
        saved_all_miss_report_path=args.saved_all_miss_report,
        settings=SearchSettings(
            seed=args.seed,
            random_candidates=args.random_candidates,
            refine_seeds=args.refine_seeds,
            refine_per_seed=args.refine_per_seed,
            exact_screen=args.exact_screen,
        ),
    )
    _write_json_atomic(args.report, report)
    summary = {
        "scope": report["scope"],
        "primary_all_miss": {
            "stitched": report["logo_primary_all_miss_learned_cost"]["stitched"],
            "worst_folds": report["logo_primary_all_miss_learned_cost"][
                "worst_folds"
            ],
            "lowest_weighted_fold": report[
                "logo_primary_all_miss_learned_cost"
            ]["lowest_weighted_fold"],
            "all_fold_tier_official_budgets_passed": report[
                "logo_primary_all_miss_learned_cost"
            ]["all_fold_tier_official_budgets_passed"],
        },
        "secondary_exact_public": {
            "stitched": report["logo_secondary_exact_public_cost"]["stitched"],
            "all_fold_tier_official_budgets_passed": report[
                "logo_secondary_exact_public_cost"
            ]["all_fold_tier_official_budgets_passed"],
        },
        "matrix_validation": report["matrix_validation"],
        "report": str(args.report),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
