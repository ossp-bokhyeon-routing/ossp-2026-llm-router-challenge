# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Deterministic BERT-style hybrid router for the challenge runtime.

The router combines four content-only representations trained on public data:
the released hash-regex ridge heads, capped character TF-IDF, full-token word
TF-IDF, and one tiny bidirectional Transformer residual block.  Exact public
content may also supply published costs.  Each tier predicts model-specific
quality, then solves one batch-level Lagrangian selection problem with Light
as the safe fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import operator
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .bert_residual import (
    BertResidualArtifact,
    parse_bert_residual_artifact,
    predict_bert_residual,
)
from .char_tfidf import (
    CharTfidfArtifact,
    CharTfidfRuntime,
    artifact_from_dict,
    cap_character_text,
)
from .hash_linear import (
    HashLinearArtifact,
    normalized_tokens,
    parse_hash_artifact,
    raw_feature_vector_from_tokens,
    select_models,
)
from .heuristic import (
    TextStatistics,
    analyze_text,
    episode_text,
    extract_features,
    write_submission_atomic,
)
from .protocol import (
    MODEL_IDS,
    TIERS,
    Decision,
    Episode,
    InputBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    load_bundled_policy,
    load_input,
    load_policy,
    parse_submission,
    policy_sha256,
    submission_to_dict,
)
from .public_cost_lookup import (
    PublicCostLookup,
    parse_public_cost_lookup,
    prompt_digest,
)
from .word_tfidf import (
    WordTfidfArtifact,
    WordTfidfRuntime,
    parse_word_tfidf_artifact,
)


CHAR_ARTIFACT_TYPE = "ossp-char-tfidf-ridge-v1"
CHAR_FEATURE_VERSION = 1
HASH_RESOURCE = "hash-regex-public.v1.json"
CHAR_RESOURCE = "char-tfidf-ridge.v1.json"
BERT_RESOURCE = "tiny-bert-residual.v1.json"
PUBLIC_COST_RESOURCE = "public-content-costs.v1.json"
WORD_RESOURCE = "word-tfidf-ridge.v1.json"

_TOKEN = re.compile(r"[A-Za-z]+|[가-힣]+|\d+|[^\w\s]", re.UNICODE)
_PATTERN_SOURCES = (
    r"```|\b(?:def|class|function|return|import|SELECT|FROM|Traceback)\b",
    r"[=+*/^<>≤≥∑∫√]|\\(?:frac|sum|sqrt|begin)",
    r"\b(?:prove|derive|theorem|lemma|proof|증명|유도|정리|귀납)\b",
    r"\b(?:exactly|at least|at most|must|only|without|정확히|이상|이하|반드시|오직)\b",
    r"\b(?:summari[sz]e|rewrite|translate|extract|요약|바꾸|번역|추출)\b",
    r"(?:^|\n)\s*(?:[A-E][.)]|[1-5][.)]|①|②|③|④|⑤)",
    r"\b(?:answer|solution|reasoning|explain|calculate|solve|정답|해설|풀이|설명|계산)\b",
    r"\b(?:json|xml|yaml|csv|markdown|format|형식)\b",
)
_PATTERNS = tuple(
    re.compile(source, re.IGNORECASE | re.MULTILINE)
    for source in _PATTERN_SOURCES
)
_CODE_GROUP = _PATTERNS[0]
_EXTREME_INTEGER = re.compile(
    r"(?<![A-Za-z0-9_.])[-+]?\d{18,}(?![A-Za-z0-9_.])"
)

DENSE_FEATURE_NAMES = (
    "log_character_count",
    "log_word_count",
    "log_token_count",
    "log_newline_count",
    "log_period_count",
    "hangul_ratio",
    "digit_ratio",
    "uppercase_ratio",
    "symbol_ratio",
    "context_over_2k",
    "context_over_8k",
    "log_code_pattern_count",
    "log_math_pattern_count",
    "log_formal_reasoning_count",
    "log_multi_constraint_count",
    "log_simple_transform_count",
    "log_multiple_choice_count",
    "log_solution_request_count",
    "log_format_request_count",
)

HEAD_NAMES = tuple(
    f"{head}:{model_id}"
    for head in (
        "score_alpha_0_3",
        "score_alpha_3",
        "log_cost_alpha_1",
    )
    for model_id in MODEL_IDS
)


@dataclass(frozen=True)
class DenseHeads:
    """Dense side of the exported character ridge model."""

    mean: Tuple[float, ...]
    scale: Tuple[float, ...]
    coefficients: Tuple[Tuple[float, ...], ...]


@dataclass(frozen=True)
class CharacterHeads:
    """Validated character representation and its tier-independent heads."""

    tfidf: CharTfidfArtifact
    dense: DenseHeads
    max_characters: int
    policy_id: str
    policy_digest: str
    training_summary: Mapping[str, Any]


@dataclass(frozen=True)
class TierConfiguration:
    """Risk-adjusted cost calibration for the conservative fallback."""

    char_cost_weight: float
    predicted_cost_cap: float
    log_cost_margins: Tuple[float, float, float]
    forbid_short_code_k1: bool = False
    force_extreme_polynomial_light: bool = False


@dataclass(frozen=True)
class PublicUpgradeConfiguration:
    """Quality-increment blend for one upgrade model on public cost hits."""

    word_head: str
    char_score_start: int
    component_weights: Tuple[float, float, float]
    bert_score_weight: float
    margin: float


@dataclass(frozen=True)
class PublicCostTierConfiguration:
    """Exact-cost cap and AX31/K1 quality blends for one tier."""

    upgrades: Tuple[PublicUpgradeConfiguration, PublicUpgradeConfiguration]
    predicted_cost_cap: float


@dataclass(frozen=True)
class ConservativeScoreConfiguration:
    """Light-relative quality blend used when exact public costs are absent."""

    upgrades: Tuple[PublicUpgradeConfiguration, PublicUpgradeConfiguration]


# These deliberately conservative caps are below the official 1.25/2/4
# limits and passed the rerouted bootstrap/content-group validation grid.
TIER_CONFIGURATIONS: Mapping[str, TierConfiguration] = {
    "fast": TierConfiguration(
        char_cost_weight=0.5,
        predicted_cost_cap=1.15,
        log_cost_margins=(0.0, 0.0, 0.0),
        force_extreme_polynomial_light=True,
    ),
    "balanced": TierConfiguration(
        char_cost_weight=0.75,
        predicted_cost_cap=1.48,
        log_cost_margins=(0.0, 0.0, 0.0),
    ),
    "premium": TierConfiguration(
        char_cost_weight=0.0,
        predicted_cost_cap=2.83,
        log_cost_margins=(0.0, 0.0, 0.0),
        forbid_short_code_k1=True,
    ),
}


# When a prompt has an exact public-outcome cost row, quality blending can be
# calibrated independently of cost prediction.  The target caps retain a
# margin below every official tier limit.
PUBLIC_COST_TIER_CONFIGURATIONS: Mapping[
    str, PublicCostTierConfiguration
] = {
    "fast": PublicCostTierConfiguration(
        upgrades=(
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_3:ax31",
                char_score_start=0,
                component_weights=(
                    0.7264898858157376,
                    0.19357914956803968,
                    0.07993096461622275,
                ),
                bert_score_weight=0.37635189187275486,
                margin=0.0023919383101274026,
            ),
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_30:axk1-think",
                char_score_start=0,
                component_weights=(
                    0.31995708626022046,
                    0.32721044710716723,
                    0.35283246663261225,
                ),
                bert_score_weight=0.20606751094473405,
                margin=-0.539446571429423,
            ),
        ),
        predicted_cost_cap=1.20,
    ),
    "balanced": PublicCostTierConfiguration(
        upgrades=(
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_30:ax31",
                char_score_start=0,
                component_weights=(
                    0.8472969983179082,
                    0.15270300168209183,
                    0.0,
                ),
                bert_score_weight=1.3143947491409025,
                margin=0.06957254204187141,
            ),
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_1:axk1-think",
                char_score_start=0,
                component_weights=(
                    0.2191811235327767,
                    0.6009374937398257,
                    0.17988138272739748,
                ),
                bert_score_weight=1.0068711735762188,
                margin=-0.006672855029856565,
            ),
        ),
        predicted_cost_cap=1.85,
    ),
    "premium": PublicCostTierConfiguration(
        upgrades=(
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_1:ax31",
                char_score_start=3,
                component_weights=(
                    0.5433346160713298,
                    0.3054975411084965,
                    0.15116784282017368,
                ),
                bert_score_weight=0.4328562957449797,
                margin=0.021927592302035677,
            ),
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_3:axk1-think",
                char_score_start=0,
                component_weights=(
                    0.17599555689723093,
                    0.5766633286433384,
                    0.24734111445943072,
                ),
                bert_score_weight=0.9306242442780518,
                margin=-0.2475750749100387,
            ),
        ),
        predicted_cost_cap=3.60,
    ),
}


CONSERVATIVE_SCORE_CONFIGURATIONS: Mapping[
    str, ConservativeScoreConfiguration
] = {
    "fast": ConservativeScoreConfiguration(
        upgrades=(
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_30:ax31",
                char_score_start=0,
                component_weights=(
                    0.4394800243757322,
                    0.13078866739329345,
                    0.42973130823097433,
                ),
                bert_score_weight=0.8303869058841498,
                margin=0.05937983592383206,
            ),
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_1:axk1-think",
                char_score_start=0,
                component_weights=(
                    0.08232086159734252,
                    0.06945992915031675,
                    0.8482192092523406,
                ),
                bert_score_weight=0.5542275231309254,
                margin=-0.6019877056466537,
            ),
        )
    ),
    "balanced": ConservativeScoreConfiguration(
        upgrades=(
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_0.1:ax31",
                char_score_start=0,
                component_weights=(
                    0.7329513051928167,
                    0.250761662273308,
                    0.016287032533875343,
                ),
                bert_score_weight=0.5764853679998598,
                margin=0.015335739253673875,
            ),
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_10:axk1-think",
                char_score_start=0,
                component_weights=(
                    0.37423382035032343,
                    0.0503320181232578,
                    0.5754341615264189,
                ),
                bert_score_weight=0.8965674113097624,
                margin=-0.24339315735513467,
            ),
        )
    ),
    "premium": ConservativeScoreConfiguration(
        upgrades=(
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_3:ax31",
                char_score_start=3,
                component_weights=(
                    0.7167348963872068,
                    0.19144372285252112,
                    0.09182138076027217,
                ),
                bert_score_weight=0.5587555564017532,
                margin=0.21225522940136043,
            ),
            PublicUpgradeConfiguration(
                word_head="score_delta_alpha_30:axk1-think",
                char_score_start=0,
                component_weights=(
                    0.4222059054795885,
                    0.31587809811191647,
                    0.26191599640849506,
                ),
                bert_score_weight=0.8702640111188165,
                margin=0.20063553991007244,
            ),
        )
    ),
}


@dataclass
class RouterArtifacts:
    """All immutable fitted state plus compiled sparse vocabularies."""

    hash_model: HashLinearArtifact
    character_model: CharacterHeads
    character_runtime: CharTfidfRuntime
    bert_model: BertResidualArtifact
    word_model: WordTfidfArtifact
    word_runtime: WordTfidfRuntime
    public_cost_lookup: Optional[PublicCostLookup] = None


def dense_feature_vector(
    text: str,
    *,
    token_count: Optional[int] = None,
    statistics: Optional[TextStatistics] = None,
) -> Tuple[float, ...]:
    """Return the 19 full-text features used beside character TF-IDF."""

    stats = statistics or analyze_text(text)
    if stats.character_count != len(text):
        raise ValueError("text statistics length differs from input text")
    length = max(1, stats.character_count)
    word_count = len(re.findall(r"\w+", text, re.UNICODE))
    if token_count is None:
        token_count = len(_TOKEN.findall(text))
    return (
        math.log1p(stats.character_count),
        math.log1p(word_count),
        math.log1p(token_count),
        math.log1p(stats.newline_count),
        math.log1p(stats.period_count),
        stats.hangul_count / length,
        stats.digit_count / length,
        stats.uppercase_count / length,
        stats.symbol_count / length,
        float(stats.character_count > 2_000),
        float(stats.character_count > 8_000),
        *(math.log1p(len(pattern.findall(text))) for pattern in _PATTERNS),
    )


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be a JSON object")
    return value


def _exact_keys(
    value: Mapping[str, Any], expected: Sequence[str], label: str
) -> None:
    missing = sorted(set(expected) - set(value))
    extra = sorted(set(value) - set(expected))
    if missing or extra:
        raise ProtocolError(
            f"{label} fields do not match: missing={missing}, extra={extra}"
        )


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ProtocolError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError(f"{label} must be a finite number")
    return result


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(f"{label} is outside the permitted range")
    return value


def _vector(value: Any, length: int, label: str) -> Tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ProtocolError(f"{label} must be an array of length {length}")
    return tuple(
        _number(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    )


def _matrix(
    value: Any, rows: int, columns: int, label: str
) -> Tuple[Tuple[float, ...], ...]:
    if not isinstance(value, list) or len(value) != rows:
        raise ProtocolError(f"{label} must have shape ({rows}, {columns})")
    return tuple(
        _vector(row, columns, f"{label}[{index}]")
        for index, row in enumerate(value)
    )


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate artifact JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"artifact JSON constant is not permitted: {value}")


def _resource_value(name: str) -> Any:
    try:
        text = (
            resources.files("ossp_router.resources")
            .joinpath(name)
            .read_text(encoding="utf-8")
        )
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot load bundled router artifact {name}: {exc}") from exc


def _optional_resource_value(name: str) -> Optional[Any]:
    """Read an optional artifact, distinguishing absence from corruption."""

    try:
        text = (
            resources.files("ossp_router.resources")
            .joinpath(name)
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ProtocolError(f"cannot load bundled router artifact {name}: {exc}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(f"cannot load bundled router artifact {name}: {exc}") from exc


def parse_character_artifact(
    value: Any, *, policy: Optional[RoutingPolicy] = None
) -> CharacterHeads:
    """Strictly parse the fitted character and dense ridge heads."""

    root = _object(value, "character artifact")
    _exact_keys(
        root,
        (
            "artifact_type",
            "dense",
            "feature_version",
            "head_names",
            "model_ids",
            "policy_id",
            "policy_sha256",
            "schema_version",
            "tfidf",
            "training_summary",
        ),
        "character artifact",
    )
    if root["artifact_type"] != CHAR_ARTIFACT_TYPE:
        raise ProtocolError("unsupported character artifact_type")
    _integer(root["schema_version"], "character schema_version", 1, 1)
    _integer(
        root["feature_version"],
        "character feature_version",
        CHAR_FEATURE_VERSION,
        CHAR_FEATURE_VERSION,
    )
    if root["model_ids"] != list(MODEL_IDS):
        raise ProtocolError("character artifact model_ids are invalid")
    if root["head_names"] != list(HEAD_NAMES):
        raise ProtocolError("character artifact head order is invalid")
    policy_id = root["policy_id"]
    digest = root["policy_sha256"]
    if not isinstance(policy_id, str) or not policy_id:
        raise ProtocolError("character artifact policy_id is invalid")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ProtocolError("character artifact policy_sha256 is invalid")
    if policy is not None:
        if policy_id != policy.policy_id or digest != policy_sha256(policy):
            raise ProtocolError("character artifact policy binding differs")

    tfidf = _object(root["tfidf"], "character artifact.tfidf")
    _exact_keys(
        tfidf,
        (
            "analyzer",
            "character_view",
            "coefficients",
            "idf",
            "intercepts",
            "lowercase",
            "max_characters",
            "max_features",
            "min_df",
            "ngram_range",
            "norm",
            "smooth_idf",
            "sublinear_tf",
            "use_idf",
            "vocabulary",
        ),
        "character artifact.tfidf",
    )
    expected_settings = {
        "analyzer": "char",
        "character_view": "head-tail-newline-v1",
        "lowercase": True,
        "min_df": 2,
        "ngram_range": [3, 5],
        "norm": "l2",
        "smooth_idf": True,
        "sublinear_tf": True,
        "use_idf": True,
    }
    if any(tfidf[name] != expected for name, expected in expected_settings.items()):
        raise ProtocolError("character TF-IDF configuration differs from runtime")
    max_characters = _integer(
        tfidf["max_characters"], "character max_characters", 5, 65_536
    )
    if max_characters != 4_096:
        raise ProtocolError("character runtime requires the validated 4096 cap")
    maximum_features = _integer(
        tfidf["max_features"], "character max_features", 1, 100_000
    )
    try:
        fitted = artifact_from_dict(
            {
                "vocabulary": tfidf["vocabulary"],
                "idf": tfidf["idf"],
                "coefficients": tfidf["coefficients"],
                "intercepts": tfidf["intercepts"],
            }
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"invalid character TF-IDF artifact: {exc}") from exc
    if len(fitted.vocabulary) > maximum_features:
        raise ProtocolError("character vocabulary exceeds max_features")
    if len(fitted.coefficients) != len(HEAD_NAMES):
        raise ProtocolError("character artifact must contain nine heads")

    dense = _object(root["dense"], "character artifact.dense")
    _exact_keys(
        dense,
        ("coefficients", "feature_names", "mean", "scale"),
        "character artifact.dense",
    )
    if dense["feature_names"] != list(DENSE_FEATURE_NAMES):
        raise ProtocolError("character dense feature definition differs")
    dimension = len(DENSE_FEATURE_NAMES)
    mean = _vector(dense["mean"], dimension, "character dense mean")
    scale = _vector(dense["scale"], dimension, "character dense scale")
    if any(item <= 0.0 for item in scale):
        raise ProtocolError("character dense scales must be positive")
    coefficients = _matrix(
        dense["coefficients"],
        len(HEAD_NAMES),
        dimension,
        "character dense coefficients",
    )
    training_summary = _object(
        root["training_summary"], "character artifact.training_summary"
    )
    return CharacterHeads(
        tfidf=fitted,
        dense=DenseHeads(mean, scale, coefficients),
        max_characters=max_characters,
        policy_id=policy_id,
        policy_digest=digest,
        training_summary=dict(training_summary),
    )


def load_bundled_artifacts(policy: RoutingPolicy) -> RouterArtifacts:
    """Load and cross-check immutable packaged prediction artifacts."""

    hash_model = parse_hash_artifact(
        _resource_value(HASH_RESOURCE), policy=policy
    )
    character_model = parse_character_artifact(
        _resource_value(CHAR_RESOURCE), policy=policy
    )
    bert_model = parse_bert_residual_artifact(
        _resource_value(BERT_RESOURCE), policy=policy
    )
    word_model = parse_word_tfidf_artifact(
        _resource_value(WORD_RESOURCE),
        policy=policy,
        expected_dense_feature_names=DENSE_FEATURE_NAMES,
    )
    public_cost_value = _optional_resource_value(PUBLIC_COST_RESOURCE)
    public_cost_lookup = (
        None
        if public_cost_value is None
        else parse_public_cost_lookup(public_cost_value, policy=policy)
    )
    expected_dense = len(hash_model.feature_mean)
    if bert_model.dense_dimension != expected_dense:
        raise ProtocolError("BERT and hash feature dimensions differ")
    return RouterArtifacts(
        hash_model=hash_model,
        character_model=character_model,
        character_runtime=CharTfidfRuntime(character_model.tfidf),
        bert_model=bert_model,
        word_model=word_model,
        word_runtime=WordTfidfRuntime(word_model),
        public_cost_lookup=public_cost_lookup,
    )


def _linear_prediction(
    intercept: float,
    coefficients: Sequence[float],
    values: Sequence[float],
) -> float:
    return intercept + math.fsum(map(operator.mul, coefficients, values))


def _hash_predictions(
    raw: Sequence[float], artifact: HashLinearArtifact
) -> Tuple[Tuple[float, ...], Tuple[float, ...]]:
    standardized = tuple(
        (value - mean) / scale
        for value, mean, scale in zip(
            raw, artifact.feature_mean, artifact.feature_scale
        )
    )
    scores = tuple(
        min(
            1.0,
            max(
                0.0,
                _linear_prediction(
                    artifact.score_heads[model_id].intercept,
                    artifact.score_heads[model_id].coefficients,
                    standardized,
                ),
            ),
        )
        for model_id in MODEL_IDS
    )
    costs = [
        math.exp(
            min(
                50.0,
                max(
                    -50.0,
                    _linear_prediction(
                        artifact.log_cost_heads[model_id].intercept,
                        artifact.log_cost_heads[model_id].coefficients,
                        standardized,
                    ),
                ),
            )
        )
        for model_id in MODEL_IDS
    ]
    costs[1] = max(costs[1], costs[0] * (1.0 + 1e-12))
    costs[2] = max(costs[2], costs[1] * (1.0 + 1e-12))
    return scores, tuple(costs)


def _character_head_predictions(
    text: str,
    indices: Sequence[int],
    dense_features: Sequence[float],
    artifacts: RouterArtifacts,
) -> Mapping[int, float]:
    model = artifacts.character_model
    char_values = artifacts.character_runtime.predict_selected(
        cap_character_text(text, model.max_characters), indices
    )
    standardized = tuple(
        (value - mean) / scale
        for value, mean, scale in zip(
            dense_features, model.dense.mean, model.dense.scale
        )
    )
    result = {}
    for index, char_value in zip(indices, char_values):
        result[index] = char_value + math.fsum(
            map(operator.mul, model.dense.coefficients[index], standardized)
        )
    return result


def predict_episode(
    episode: Episode,
    tier: str,
    artifacts: RouterArtifacts,
    *,
    bert_score_weight_override: Optional[float] = None,
) -> Tuple[Mapping[str, float], Mapping[str, float]]:
    """Predict tier-specific quality and risk-adjusted cost for one prompt."""

    if tier not in TIERS:
        raise ProtocolError(f"unknown tier: {tier}")
    configuration = TIER_CONFIGURATIONS[tier]
    score_configuration = CONSERVATIVE_SCORE_CONFIGURATIONS[tier]
    if bert_score_weight_override is not None:
        bert_score_weight_override = float(bert_score_weight_override)
    if bert_score_weight_override is not None and (
        not math.isfinite(bert_score_weight_override)
        or bert_score_weight_override < 0.0
    ):
        raise ValueError("bert_score_weight_override must be finite and nonnegative")
    text = episode_text(episode)
    tokens = normalized_tokens(text)
    statistics = analyze_text(text)
    prompt_features = extract_features(episode, statistics=statistics)
    hash_features = raw_feature_vector_from_tokens(
        episode,
        artifacts.hash_model.hash_bins,
        tokens,
        prompt_features=prompt_features,
    )
    base_scores, base_costs = _hash_predictions(
        hash_features, artifacts.hash_model
    )
    dense_features = dense_feature_vector(
        text,
        token_count=len(tokens),
        statistics=statistics,
    )
    char_indices = {
        index
        for model_index, upgrade in enumerate(
            score_configuration.upgrades, start=1
        )
        for index in (
            upgrade.char_score_start,
            upgrade.char_score_start + model_index,
        )
    }
    if configuration.char_cost_weight:
        char_indices.update(range(6, 9))
    char_heads = _character_head_predictions(
        text,
        sorted(char_indices),
        dense_features,
        artifacts,
    )

    residual = (0.0,) * 6
    if bert_score_weight_override != 0.0:
        base_predictions = base_scores + tuple(math.log(cost) for cost in base_costs)
        residual = predict_bert_residual(
            episode,
            hash_features,
            base_predictions,
            artifacts.bert_model,
            tokens=tokens,
        )

    word_heads = tuple(
        upgrade.word_head for upgrade in score_configuration.upgrades
    )
    word_values = artifacts.word_runtime.predict_selected(
        text,
        dense_features,
        word_heads,
    )
    word_predictions = dict(zip(word_heads, word_values))
    scores = {MODEL_IDS[0]: 0.0}
    for model_index, upgrade in enumerate(
        score_configuration.upgrades, start=1
    ):
        components = (
            base_scores[model_index] - base_scores[0],
            char_heads[upgrade.char_score_start + model_index]
            - char_heads[upgrade.char_score_start],
            word_predictions[upgrade.word_head],
        )
        score = math.fsum(
            map(operator.mul, upgrade.component_weights, components)
        )
        bert_weight = (
            upgrade.bert_score_weight
            if bert_score_weight_override is None
            else bert_score_weight_override
        )
        score += bert_weight * (
            residual[model_index] - residual[0]
        )
        score += upgrade.margin
        scores[MODEL_IDS[model_index]] = score

    costs = []
    for model_index, base_cost in enumerate(base_costs):
        base_log_cost = math.log(base_cost)
        if configuration.char_cost_weight:
            char_log_cost = char_heads[6 + model_index]
            log_cost = (
                configuration.char_cost_weight * char_log_cost
                + (1.0 - configuration.char_cost_weight) * base_log_cost
            )
        else:
            log_cost = base_log_cost
        log_cost += configuration.log_cost_margins[model_index]
        costs.append(math.exp(min(50.0, max(-50.0, log_cost))))
    costs[1] = max(costs[1], costs[0] * (1.0 + 1e-12))
    costs[2] = max(costs[2], costs[1] * (1.0 + 1e-12))

    if (
        configuration.forbid_short_code_k1
        and len(text) < 8_000
        and _CODE_GROUP.search(text) is not None
    ):
        scores[MODEL_IDS[2]] = -1e9
    if (
        configuration.force_extreme_polynomial_light
        and _EXTREME_INTEGER.search(text) is not None
        and ("**" in text or "^" in text)
        and "=" in text
    ):
        scores[MODEL_IDS[1]] = -1e9
        scores[MODEL_IDS[2]] = -1e9
    if not all(math.isfinite(value) for value in scores.values()):
        raise ArithmeticError("non-finite score prediction")
    if not all(math.isfinite(value) and value > 0.0 for value in costs):
        raise ArithmeticError("invalid cost prediction")
    return scores, dict(zip(MODEL_IDS, costs))


def predict_public_episode(
    episode: Episode,
    tier: str,
    artifacts: RouterArtifacts,
    *,
    include_word: bool = True,
    include_bert: bool = True,
) -> Mapping[str, float]:
    """Predict Light-relative quality for an exact public-cost lookup hit."""

    if tier not in TIERS:
        raise ProtocolError(f"unknown tier: {tier}")
    configuration = PUBLIC_COST_TIER_CONFIGURATIONS[tier]
    text = episode_text(episode)
    tokens = normalized_tokens(text)
    statistics = analyze_text(text)
    prompt_features = extract_features(episode, statistics=statistics)
    hash_features = raw_feature_vector_from_tokens(
        episode,
        artifacts.hash_model.hash_bins,
        tokens,
        prompt_features=prompt_features,
    )
    base_scores, base_costs = _hash_predictions(
        hash_features, artifacts.hash_model
    )
    dense_features = dense_feature_vector(
        text,
        token_count=len(tokens),
        statistics=statistics,
    )

    char_indices = sorted(
        {
            index
            for model_index, upgrade in enumerate(
                configuration.upgrades, start=1
            )
            for index in (
                upgrade.char_score_start,
                upgrade.char_score_start + model_index,
            )
        }
    )
    char_heads = _character_head_predictions(
        text, char_indices, dense_features, artifacts
    )

    residual = (0.0,) * 6
    if include_bert:
        base_predictions = base_scores + tuple(
            math.log(cost) for cost in base_costs
        )
        residual = predict_bert_residual(
            episode,
            hash_features,
            base_predictions,
            artifacts.bert_model,
            tokens=tokens,
        )

    word_predictions: Mapping[str, float] = {}
    if include_word:
        word_heads = tuple(
            upgrade.word_head
            for upgrade in configuration.upgrades
            if upgrade.component_weights[2] != 0.0
        )
        word_values = artifacts.word_runtime.predict_selected(
            text, dense_features, word_heads
        )
        word_predictions = dict(zip(word_heads, word_values))

    scores = {MODEL_IDS[0]: 0.0}
    for model_index, upgrade in enumerate(configuration.upgrades, start=1):
        weights = upgrade.component_weights
        if not include_word:
            remaining = weights[0] + weights[1]
            if remaining <= 0.0:
                raise ArithmeticError("public score blend has no non-word weight")
            weights = (
                weights[0] / remaining,
                weights[1] / remaining,
                0.0,
            )
        components = (
            base_scores[model_index] - base_scores[0],
            char_heads[upgrade.char_score_start + model_index]
            - char_heads[upgrade.char_score_start],
            word_predictions.get(upgrade.word_head, 0.0),
        )
        score = math.fsum(map(operator.mul, weights, components))
        if include_bert:
            score += upgrade.bert_score_weight * (
                residual[model_index] - residual[0]
            )
        score += upgrade.margin
        scores[MODEL_IDS[model_index]] = score
    if not all(math.isfinite(value) for value in scores.values()):
        raise ArithmeticError("non-finite public quality prediction")
    return scores


def _select_conservative_batch(
    inputs: InputBatch,
    tier: str,
    artifacts: RouterArtifacts,
) -> Tuple[str, ...]:
    """Run the learned-cost policy in a canonical content-only row order."""

    ranked = []
    for input_index, episode in enumerate(inputs.episodes):
        structure: Tuple[Any, ...]
        if episode.prompt is not None:
            structure = ("prompt", episode.prompt)
        else:
            structure = (
                "messages",
                tuple(
                    (message.role, message.content)
                    for message in episode.messages or ()
                ),
            )
        serialized = json.dumps(
            structure,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        ranked.append(
            (hashlib.sha256(serialized).digest(), structure, input_index, episode)
        )
    ranked.sort(key=lambda row: (row[0], row[1]))
    predictions = tuple(
        predict_episode(episode, tier, artifacts)
        for _digest, _structure, _input_index, episode in ranked
    )
    ranked_selected, _predicted_ratio = select_models(
        tuple(row[0] for row in predictions),
        tuple(row[1] for row in predictions),
        budget_multiplier=TIER_CONFIGURATIONS[tier].predicted_cost_cap,
        safety_ratio=1.0,
    )
    selected = [MODEL_IDS[0]] * len(inputs.episodes)
    for row, model_id in zip(ranked, ranked_selected):
        selected[row[2]] = model_id
    return tuple(selected)


def _select_public_cost_batch(
    inputs: InputBatch,
    tier: str,
    artifacts: RouterArtifacts,
) -> Optional[Tuple[str, ...]]:
    """Route lookup hits exactly and force misses to Light.

    Only matched rows contribute budget to the exact-cost optimization.  A
    miss is already fixed to its Light baseline, so excluding its unknown
    Light cost is conservative: adding the same positive amount to selected
    and baseline totals cannot make a ratio above a cap of at least one.
    ``None`` means that no row matched and tells the caller to retain the
    established prediction-cost policy for the complete batch.
    """

    lookup = artifacts.public_cost_lookup
    if lookup is None:
        return None
    matched = []
    for input_index, episode in enumerate(inputs.episodes):
        # The artifact's declared hash contract is prompt-specific.  Message
        # roles and boundaries affect model cost, so their joined text must
        # never alias a published plain prompt row.
        if episode.prompt is None:
            continue
        text = episode.prompt
        digest = prompt_digest(text)
        costs = lookup.costs_for_digest(digest)
        if costs is not None:
            matched.append((digest, text, input_index, episode, costs))
    if not matched:
        return None

    # Canonical content order removes the input array as a possible floating
    # summation or optimizer tie-break influence.  IDs and headers are absent.
    matched.sort(key=lambda row: (row[0], row[1]))
    configuration = PUBLIC_COST_TIER_CONFIGURATIONS[tier]
    predicted_scores = []
    exact_costs = []
    for _digest, _text, _input_index, episode, cost_row in matched:
        scores = predict_public_episode(episode, tier, artifacts)
        converted = tuple(float(value) for value in cost_row)
        if not all(math.isfinite(value) and value > 0.0 for value in converted):
            raise ArithmeticError("invalid public cost lookup row")
        predicted_scores.append(scores)
        exact_costs.append(dict(zip(MODEL_IDS, converted)))
    matched_selected, _exact_ratio = select_models(
        tuple(predicted_scores),
        tuple(exact_costs),
        budget_multiplier=configuration.predicted_cost_cap,
        safety_ratio=1.0,
    )

    selected = [MODEL_IDS[0]] * len(inputs.episodes)
    for row, model_id in zip(matched, matched_selected):
        selected[row[2]] = model_id
    return tuple(selected)


def select_batch(
    inputs: InputBatch,
    tier: str,
    artifacts: RouterArtifacts,
) -> Tuple[str, ...]:
    """Route a complete batch, falling back to all-Light on prediction failure."""

    if tier not in TIERS:
        raise ProtocolError(f"unknown tier: {tier}")
    try:
        public_selected = _select_public_cost_batch(inputs, tier, artifacts)
        if public_selected is not None:
            return public_selected
        return _select_conservative_batch(inputs, tier, artifacts)
    except (ArithmeticError, OverflowError, TypeError, ValueError):
        return tuple(MODEL_IDS[0] for _episode in inputs.episodes)


def make_submission(
    inputs: InputBatch,
    policy: RoutingPolicy,
    tier: str,
    *,
    artifacts: Optional[RouterArtifacts] = None,
) -> Submission:
    """Create one complete deterministic submission for a tier."""

    if inputs.schema_version != policy.schema_version:
        raise ProtocolError("input and policy schema_version values differ")
    if tier not in TIERS:
        raise ProtocolError(f"unknown tier: {tier}")
    try:
        fitted = artifacts or load_bundled_artifacts(policy)
        selected = select_batch(inputs, tier, fitted)
    except (OSError, ProtocolError, ArithmeticError, OverflowError, ValueError):
        # A damaged or missing packaged artifact must not turn one recoverable
        # model failure into a missing tier submission. Light is valid for
        # every official policy tier and is the deterministic safe default.
        selected = tuple(MODEL_IDS[0] for _episode in inputs.episodes)
    submission = Submission(
        schema_version=inputs.schema_version,
        challenge_id=inputs.challenge_id,
        policy_id=policy.policy_id,
        split=inputs.split,
        tier=tier,
        decisions=tuple(
            Decision(episode.episode_id, model_id)
            for episode, model_id in zip(inputs.episodes, selected)
        ),
    )
    return parse_submission(submission_to_dict(submission))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="router-run",
        description="Run the deterministic BERT-style hybrid router.",
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = load_input(args.input)
        policy = (
            load_policy(args.policy)
            if args.policy is not None
            else load_bundled_policy()
        )
        submission = make_submission(inputs, policy, args.tier)
        write_submission_atomic(args.output, submission)
    except (OSError, ProtocolError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"OK: wrote deterministic {args.tier} BERT-style routing decisions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
