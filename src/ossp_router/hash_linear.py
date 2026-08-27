# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Standard-library inference core for the public hash-regex linear model.

This module intentionally mirrors the feature extraction, artifact contract,
linear predictions, and batch-level Lagrangian selector in
``baselines/hash_regex.py``.  It excludes the baseline CLI and submission
assembly so the same deterministic predictor can be composed with other
runtime representations.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Mapping, Optional, Sequence, Tuple

from .heuristic import PromptFeatures, episode_text, extract_features
from .protocol import (
    MODEL_IDS,
    TIERS,
    Episode,
    ProtocolError,
    RoutingPolicy,
    policy_sha256,
)


ARTIFACT_TYPE = "ossp-hash-regex-linear-v1"
FEATURE_VERSION = 1
DEFAULT_HASH_BINS = 256
MIN_HASH_BINS = 16
MAX_HASH_BINS = 16_384
FEATURE_HASH_CACHE_SIZE = 262_144
_FNV_OFFSET = 14_695_981_039_346_656_037
_FNV_PRIME = 1_099_511_628_211
_UINT64_MASK = (1 << 64) - 1
_TOKEN = re.compile(r"[A-Za-z]+|[가-힣]+|\d+|[^\w\s]", re.UNICODE)
_FORMAL_REASONING = re.compile(
    r"\b(?:prove|derive|theorem|lemma|counterexample|induction|"
    r"증명|유도|정리|보조정리|반례|귀납)\b",
    re.IGNORECASE,
)
_PROGRAM_ANALYSIS = re.compile(
    r"```|\b(?:traceback|exception|complexity|big[- ]?o|"
    r"시간\s*복잡도|공간\s*복잡도|예외|스택\s*추적)\b",
    re.IGNORECASE,
)
_MULTI_CONSTRAINT = re.compile(
    r"\b(?:exactly|at least|at most|must|only|without|"
    r"정확히|이상|이하|반드시|오직|제외하고)\b",
    re.IGNORECASE,
)
_SIMPLE_TRANSFORM = re.compile(
    r"\b(?:summari[sz]e|rewrite|translate|list|extract|"
    r"요약|바꾸|번역|나열|추출)\b",
    re.IGNORECASE,
)

DENSE_FEATURE_NAMES = (
    "log_character_count",
    "log_word_count",
    "log_sentence_count",
    "log_message_count",
    "hangul_ratio",
    "log_code_marker_count",
    "log_math_marker_count",
    "numeric_density",
    "long_context",
    "log_reasoning_marker_count",
    "formal_reasoning",
    "program_analysis",
    "log_multi_constraint_count",
    "simple_transform",
)


@dataclass(frozen=True)
class LinearHead:
    """One standardized-feature linear prediction head."""

    intercept: float
    coefficients: Tuple[float, ...]


@dataclass(frozen=True)
class HashLinearArtifact:
    """Validated public hash-regex artifact needed for inference."""

    hash_bins: int
    feature_mean: Tuple[float, ...]
    feature_scale: Tuple[float, ...]
    score_heads: Mapping[str, LinearHead]
    log_cost_heads: Mapping[str, LinearHead]
    tier_safety_ratios: Mapping[str, float]
    policy_id: str
    policy_digest: str
    training_summary: Mapping[str, Any]


def stable_hash(value: str) -> int:
    """Return the baseline's deterministic unsigned FNV-1a 64-bit hash."""

    digest = _FNV_OFFSET
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * _FNV_PRIME) & _UINT64_MASK
    return digest


def normalized_tokens(text: str) -> Tuple[str, ...]:
    """Tokenize text exactly as the public hash-regex baseline does."""

    result = []
    for token in _TOKEN.findall(text):
        normalized = token.casefold()
        if normalized.isdecimal():
            normalized = "<number>"
        result.append(normalized)
    return tuple(result)


def raw_feature_vector(episode: Episode, hash_bins: int) -> Tuple[float, ...]:
    """Build 14 dense features plus signed word unigram/bigram hash bins."""

    return raw_feature_vector_from_tokens(
        episode,
        hash_bins,
        normalized_tokens(episode_text(episode)),
    )


@lru_cache(maxsize=FEATURE_HASH_CACHE_SIZE)
def _cached_feature_hash(value: str) -> int:
    """Cache common token hashes without changing the stable decision hash."""

    return stable_hash(value)


def raw_feature_vector_from_tokens(
    episode: Episode,
    hash_bins: int,
    tokens: Sequence[str],
    *,
    prompt_features: Optional[PromptFeatures] = None,
) -> Tuple[float, ...]:
    """Build the public feature row while reusing normalized prompt tokens."""

    if (
        isinstance(hash_bins, bool)
        or not isinstance(hash_bins, int)
        or not MIN_HASH_BINS <= hash_bins <= MAX_HASH_BINS
        or hash_bins & (hash_bins - 1)
    ):
        raise ValueError("hash_bins must be a supported power of two")
    features = prompt_features or extract_features(episode)
    text = episode_text(episode)
    dense = (
        math.log1p(features.character_count),
        math.log1p(features.word_count),
        math.log1p(features.sentence_count),
        math.log1p(features.message_count),
        features.hangul_ratio,
        math.log1p(features.code_marker_count),
        math.log1p(features.math_marker_count),
        features.numeric_density,
        float(features.long_context),
        math.log1p(features.reasoning_marker_count),
        float(bool(_FORMAL_REASONING.search(text))),
        float(bool(_PROGRAM_ANALYSIS.search(text))),
        math.log1p(len(_MULTI_CONSTRAINT.findall(text))),
        float(bool(_SIMPLE_TRANSFORM.search(text))),
    )
    bins = [0.0] * hash_bins
    for token in tokens:
        digest = _cached_feature_hash(f"w1:{token}")
        index = digest & (hash_bins - 1)
        bins[index] += -1.0 if digest & (1 << 63) else 1.0
    for left, right in zip(tokens, tokens[1:]):
        digest = _cached_feature_hash(f"w2:{left}\x1f{right}")
        index = digest & (hash_bins - 1)
        bins[index] += -1.0 if digest & (1 << 63) else 1.0
    norm = math.sqrt(sum(value * value for value in bins))
    if norm:
        bins = [value / norm for value in bins]
    return dense + tuple(bins)


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


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(f"{label} is outside the permitted range")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ProtocolError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError(f"{label} must be a finite number")
    return result


def _vector(value: Any, length: int, label: str) -> Tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ProtocolError(f"{label} must be an array of length {length}")
    return tuple(
        _number(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    )


def _head(value: Any, length: int, label: str) -> LinearHead:
    raw = _object(value, label)
    _exact_keys(raw, ("intercept", "coefficients"), label)
    return LinearHead(
        intercept=_number(raw["intercept"], f"{label}.intercept"),
        coefficients=_vector(
            raw["coefficients"], length, f"{label}.coefficients"
        ),
    )


def _validate_policy_binding(
    artifact: HashLinearArtifact, policy: RoutingPolicy
) -> None:
    if artifact.policy_id != policy.policy_id:
        raise ProtocolError("artifact and policy policy_id values differ")
    if artifact.policy_digest != policy_sha256(policy):
        raise ProtocolError("artifact and policy SHA-256 values differ")


def parse_hash_artifact(
    value: Any, *, policy: Optional[RoutingPolicy] = None
) -> HashLinearArtifact:
    """Parse a public artifact and optionally verify its exact policy binding."""

    root = _object(value, "artifact")
    expected = (
        "artifact_type",
        "schema_version",
        "feature_version",
        "hash_algorithm",
        "hash_bins",
        "dense_feature_names",
        "model_ids",
        "policy_id",
        "policy_sha256",
        "feature_mean",
        "feature_scale",
        "score_heads",
        "log_cost_heads",
        "tier_safety_ratios",
        "training_summary",
    )
    _exact_keys(root, expected, "artifact")
    if root["artifact_type"] != ARTIFACT_TYPE:
        raise ProtocolError("unsupported hash-regex artifact_type")
    if (
        _integer(root["schema_version"], "artifact.schema_version", 1, 1)
        != 1
        or _integer(
            root["feature_version"],
            "artifact.feature_version",
            FEATURE_VERSION,
            FEATURE_VERSION,
        )
        != FEATURE_VERSION
    ):
        raise ProtocolError("unsupported hash-regex artifact version")
    if root["hash_algorithm"] != "fnv1a64-signed-word-1-2":
        raise ProtocolError("unsupported feature hash algorithm")
    hash_bins = _integer(
        root["hash_bins"],
        "artifact.hash_bins",
        MIN_HASH_BINS,
        MAX_HASH_BINS,
    )
    if hash_bins & (hash_bins - 1):
        raise ProtocolError("artifact.hash_bins must be a power of two")
    if root["dense_feature_names"] != list(DENSE_FEATURE_NAMES):
        raise ProtocolError("dense feature definition differs from the runtime")
    if root["model_ids"] != list(MODEL_IDS):
        raise ProtocolError("artifact.model_ids differs from the policy models")
    length = len(DENSE_FEATURE_NAMES) + hash_bins
    mean = _vector(root["feature_mean"], length, "artifact.feature_mean")
    scale = _vector(root["feature_scale"], length, "artifact.feature_scale")
    if any(item <= 0 for item in scale):
        raise ProtocolError("artifact.feature_scale values must be positive")
    score_raw = _object(root["score_heads"], "artifact.score_heads")
    cost_raw = _object(root["log_cost_heads"], "artifact.log_cost_heads")
    if set(score_raw) != set(MODEL_IDS) or set(cost_raw) != set(MODEL_IDS):
        raise ProtocolError("artifact linear head model sets are invalid")
    safety_raw = _object(
        root["tier_safety_ratios"], "artifact.tier_safety_ratios"
    )
    if set(safety_raw) != set(TIERS):
        raise ProtocolError("artifact tier safety ratios are incomplete")
    safety = {
        tier: _number(
            safety_raw[tier], f"artifact.tier_safety_ratios.{tier}"
        )
        for tier in TIERS
    }
    if any(not 0 < item <= 1 for item in safety.values()):
        raise ProtocolError("artifact safety ratios must be in (0, 1]")
    policy_id = root["policy_id"]
    policy_digest = root["policy_sha256"]
    if not isinstance(policy_id, str) or not policy_id:
        raise ProtocolError("artifact.policy_id is invalid")
    if (
        not isinstance(policy_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", policy_digest) is None
    ):
        raise ProtocolError("artifact.policy_sha256 is invalid")
    training_summary = _object(
        root["training_summary"], "artifact.training_summary"
    )
    artifact = HashLinearArtifact(
        hash_bins=hash_bins,
        feature_mean=mean,
        feature_scale=scale,
        score_heads={
            model_id: _head(
                score_raw[model_id], length, f"score_heads.{model_id}"
            )
            for model_id in MODEL_IDS
        },
        log_cost_heads={
            model_id: _head(
                cost_raw[model_id], length, f"log_cost_heads.{model_id}"
            )
            for model_id in MODEL_IDS
        },
        tier_safety_ratios=safety,
        policy_id=policy_id,
        policy_digest=policy_digest,
        training_summary=dict(training_summary),
    )
    if policy is not None:
        _validate_policy_binding(artifact, policy)
    return artifact


def _linear(head: LinearHead, values: Sequence[float]) -> float:
    return head.intercept + math.fsum(
        coefficient * value
        for coefficient, value in zip(head.coefficients, values)
    )


def predict_hash_linear(
    episode: Episode, artifact: HashLinearArtifact
) -> Tuple[Mapping[str, float], Mapping[str, float]]:
    """Predict clipped score and monotone positive cost for every model."""

    raw = raw_feature_vector(episode, artifact.hash_bins)
    standardized = tuple(
        (value - mean) / scale
        for value, mean, scale in zip(
            raw, artifact.feature_mean, artifact.feature_scale
        )
    )
    scores = {
        model_id: min(
            1.0,
            max(
                0.0,
                _linear(artifact.score_heads[model_id], standardized),
            ),
        )
        for model_id in MODEL_IDS
    }
    costs = {
        model_id: math.exp(
            min(
                50.0,
                max(
                    -50.0,
                    _linear(artifact.log_cost_heads[model_id], standardized),
                ),
            )
        )
        for model_id in MODEL_IDS
    }
    light = costs[MODEL_IDS[0]]
    costs[MODEL_IDS[1]] = max(
        costs[MODEL_IDS[1]], light * (1.0 + 1e-12)
    )
    costs[MODEL_IDS[2]] = max(
        costs[MODEL_IDS[2]], costs[MODEL_IDS[1]] * (1.0 + 1e-12)
    )
    return scores, costs


def select_models(
    predicted_scores: Sequence[Mapping[str, float]],
    predicted_costs: Sequence[Mapping[str, float]],
    *,
    budget_multiplier: float,
    safety_ratio: float,
) -> Tuple[Tuple[str, ...], float]:
    """Select one model per row with the baseline Lagrangian budget rule."""

    if len(predicted_scores) != len(predicted_costs) or not predicted_scores:
        raise ValueError(
            "predicted scores and costs must be non-empty and equally sized"
        )
    light_total = math.fsum(row[MODEL_IDS[0]] for row in predicted_costs)
    effective_ratio = max(1.0, budget_multiplier * safety_ratio)
    cap = light_total * effective_ratio

    def choose(penalty: float) -> Tuple[Tuple[str, ...], float]:
        selected = []
        for scores, costs in zip(predicted_scores, predicted_costs):
            model_id = max(
                MODEL_IDS,
                key=lambda candidate: (
                    scores[candidate]
                    - penalty * costs[candidate] / light_total,
                    -MODEL_IDS.index(candidate),
                ),
            )
            selected.append(model_id)
        total = math.fsum(
            costs[model_id]
            for costs, model_id in zip(predicted_costs, selected)
        )
        return tuple(selected), total

    selected, total = choose(0.0)
    if total > cap:
        low = 0.0
        high = 1.0
        selected, total = choose(high)
        while total > cap and high < 2**60:
            low = high
            high *= 2.0
            selected, total = choose(high)
        for _iteration in range(80):
            middle = (low + high) / 2.0
            candidate, candidate_total = choose(middle)
            if candidate_total <= cap:
                high = middle
                selected, total = candidate, candidate_total
            else:
                low = middle
    if total > cap:
        selected = tuple(MODEL_IDS[0] for _row in predicted_scores)
        total = light_total
    return selected, total / light_total


__all__ = (
    "ARTIFACT_TYPE",
    "DEFAULT_HASH_BINS",
    "DENSE_FEATURE_NAMES",
    "FEATURE_HASH_CACHE_SIZE",
    "FEATURE_VERSION",
    "HashLinearArtifact",
    "LinearHead",
    "normalized_tokens",
    "parse_hash_artifact",
    "predict_hash_linear",
    "raw_feature_vector",
    "raw_feature_vector_from_tokens",
    "select_models",
    "stable_hash",
)
