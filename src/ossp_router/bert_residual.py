# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Strict, standard-library inference for the tiny BERT residual artifact.

The artifact is emitted by ``baselines/train_bert_hybrid.py``.  Its six
outputs are residuals in the public trainer's fixed order: the three model
score heads followed by the three model log-cost heads.  The caller supplies
the already-computed raw hash-regex feature row and the corresponding six
base predictions so those comparatively expensive features are not computed
twice by a composed router.
"""

from __future__ import annotations

import math
import operator
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence, Tuple

from .hash_linear import normalized_tokens, stable_hash
from .heuristic import episode_text
from .protocol import (
    MODEL_IDS,
    Episode,
    ProtocolError,
    RoutingPolicy,
    policy_sha256,
)
from .tiny_bert import (
    LayerNormWeights,
    LinearWeights,
    TinyBertWeights,
    encode_cls,
)


ARTIFACT_TYPE = "ossp-tiny-bert-residual-v1"
SCHEMA_VERSION = 1
FEATURE_VERSION = 1
RESIDUAL_DIMENSION = 6
MIN_VOCAB_SIZE = 16
MAX_VOCAB_SIZE = 65_536
MIN_SEQUENCE_LENGTH = 8
MAX_SEQUENCE_LENGTH = 512
MAX_HIDDEN_SIZE = 256
MAX_DENSE_DIMENSION = 16_384
MAX_MLP_WIDTH = 1_024
_LAYER_NORM_EPSILON = 1e-5

Vector = Tuple[float, ...]
TokenRow = Tuple[Tuple[int, ...], Tuple[int, ...], Tuple[bool, ...]]

_STATE_KEYS = (
    "attention.in_proj_bias",
    "attention.in_proj_weight",
    "attention.out_proj.bias",
    "attention.out_proj.weight",
    "attention_norm.bias",
    "attention_norm.weight",
    "dense_branch.0.bias",
    "dense_branch.0.weight",
    "embedding_norm.bias",
    "embedding_norm.weight",
    "feedforward.0.bias",
    "feedforward.0.weight",
    "feedforward.3.bias",
    "feedforward.3.weight",
    "feedforward_norm.bias",
    "feedforward_norm.weight",
    "fusion.0.bias",
    "fusion.0.weight",
    "fusion.1.bias",
    "fusion.1.weight",
    "fusion.4.bias",
    "fusion.4.weight",
    "position_embeddings.weight",
    "type_embeddings.weight",
    "word_embeddings.weight",
)


@dataclass(frozen=True)
class BertResidualMember:
    """Validated parameters and normalization for one trained member."""

    seed: int
    dense_mean: Vector
    dense_scale: Vector
    residual_mean: Vector
    residual_scale: Vector
    encoder: TinyBertWeights
    dense_branch: LinearWeights
    fusion_norm: LayerNormWeights
    fusion_hidden: LinearWeights
    fusion_output: LinearWeights


@dataclass(frozen=True)
class BertResidualArtifact:
    """Validated single-member tiny BERT residual artifact."""

    vocab_size: int
    sequence_length: int
    hidden_size: int
    attention_heads: int
    dense_dimension: int
    dense_hidden_size: int
    fusion_hidden_size: int
    refit_full: bool
    policy_id: str
    policy_digest: str
    training_summary: Mapping[str, Any]
    member: BertResidualMember


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


def _vector(value: Any, length: int, label: str) -> Vector:
    if not isinstance(value, list) or len(value) != length:
        raise ProtocolError(f"{label} must be an array of length {length}")
    return tuple(
        _number(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    )


def _matrix(value: Any, rows: int, columns: int, label: str) -> Tuple[Vector, ...]:
    if not isinstance(value, list) or len(value) != rows:
        raise ProtocolError(f"{label} must have shape ({rows}, {columns})")
    result = []
    for row_index, row in enumerate(value):
        if not isinstance(row, list) or len(row) != columns:
            raise ProtocolError(f"{label} must have shape ({rows}, {columns})")
        result.append(
            tuple(
                _number(item, f"{label}[{row_index}][{column_index}]")
                for column_index, item in enumerate(row)
            )
        )
    return tuple(result)


def _linear(
    state: Mapping[str, Any],
    prefix: str,
    *,
    inputs: int,
    outputs: int,
) -> LinearWeights:
    return LinearWeights(
        weight=_matrix(
            state[f"{prefix}.weight"],
            outputs,
            inputs,
            f"artifact.members[0].state_dict.{prefix}.weight",
        ),
        bias=_vector(
            state[f"{prefix}.bias"],
            outputs,
            f"artifact.members[0].state_dict.{prefix}.bias",
        ),
    )


def _layer_norm(
    state: Mapping[str, Any], prefix: str, dimension: int
) -> LayerNormWeights:
    return LayerNormWeights(
        weight=_vector(
            state[f"{prefix}.weight"],
            dimension,
            f"artifact.members[0].state_dict.{prefix}.weight",
        ),
        bias=_vector(
            state[f"{prefix}.bias"],
            dimension,
            f"artifact.members[0].state_dict.{prefix}.bias",
        ),
    )


def parse_bert_residual_artifact(
    value: Any, *, policy: Optional[RoutingPolicy] = None
) -> BertResidualArtifact:
    """Parse and deeply validate a final residual artifact.

    The runtime deliberately accepts exactly one ensemble member.  This keeps
    execution cost bounded and matches the selected ``bert-exp-3`` checkpoint.
    """

    root = _object(value, "artifact")
    _exact_keys(
        root,
        (
            "artifact_type",
            "configuration",
            "feature_version",
            "members",
            "model_ids",
            "policy_id",
            "policy_sha256",
            "schema_version",
            "training_summary",
        ),
        "artifact",
    )
    if root["artifact_type"] != ARTIFACT_TYPE:
        raise ProtocolError("unsupported BERT residual artifact_type")
    _integer(
        root["schema_version"],
        "artifact.schema_version",
        SCHEMA_VERSION,
        SCHEMA_VERSION,
    )
    _integer(
        root["feature_version"],
        "artifact.feature_version",
        FEATURE_VERSION,
        FEATURE_VERSION,
    )
    if root["model_ids"] != list(MODEL_IDS):
        raise ProtocolError("artifact.model_ids differs from the policy models")
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
    if policy is not None:
        if policy_id != policy.policy_id:
            raise ProtocolError("artifact and policy policy_id values differ")
        if policy_digest != policy_sha256(policy):
            raise ProtocolError("artifact and policy SHA-256 values differ")

    configuration = _object(root["configuration"], "artifact.configuration")
    _exact_keys(
        configuration,
        (
            "attention_heads",
            "dense_dimension",
            "dense_hidden_size",
            "fusion_hidden_size",
            "hidden_size",
            "refit_full",
            "sequence_length",
            "vocab_size",
        ),
        "artifact.configuration",
    )
    vocab_size = _integer(
        configuration["vocab_size"],
        "artifact.configuration.vocab_size",
        MIN_VOCAB_SIZE,
        MAX_VOCAB_SIZE,
    )
    sequence_length = _integer(
        configuration["sequence_length"],
        "artifact.configuration.sequence_length",
        MIN_SEQUENCE_LENGTH,
        MAX_SEQUENCE_LENGTH,
    )
    hidden_size = _integer(
        configuration["hidden_size"],
        "artifact.configuration.hidden_size",
        2,
        MAX_HIDDEN_SIZE,
    )
    attention_heads = _integer(
        configuration["attention_heads"],
        "artifact.configuration.attention_heads",
        1,
        hidden_size,
    )
    if hidden_size % attention_heads:
        raise ProtocolError(
            "artifact hidden_size must be divisible by attention_heads"
        )
    dense_dimension = _integer(
        configuration["dense_dimension"],
        "artifact.configuration.dense_dimension",
        1,
        MAX_DENSE_DIMENSION,
    )
    dense_hidden_size = _integer(
        configuration["dense_hidden_size"],
        "artifact.configuration.dense_hidden_size",
        1,
        MAX_MLP_WIDTH,
    )
    fusion_hidden_size = _integer(
        configuration["fusion_hidden_size"],
        "artifact.configuration.fusion_hidden_size",
        1,
        MAX_MLP_WIDTH,
    )
    refit_full = configuration["refit_full"]
    if not isinstance(refit_full, bool):
        raise ProtocolError("artifact.configuration.refit_full must be boolean")

    members = root["members"]
    if not isinstance(members, list) or len(members) != 1:
        raise ProtocolError("artifact.members must contain exactly one member")
    member_value = _object(members[0], "artifact.members[0]")
    _exact_keys(
        member_value,
        ("normalization", "seed", "state_dict"),
        "artifact.members[0]",
    )
    seed = _integer(
        member_value["seed"], "artifact.members[0].seed", 0, (1 << 63) - 1
    )

    normalization = _object(
        member_value["normalization"], "artifact.members[0].normalization"
    )
    _exact_keys(
        normalization,
        ("dense_mean", "dense_scale", "residual_mean", "residual_scale"),
        "artifact.members[0].normalization",
    )
    dense_mean = _vector(
        normalization["dense_mean"],
        dense_dimension,
        "artifact.members[0].normalization.dense_mean",
    )
    dense_scale = _vector(
        normalization["dense_scale"],
        dense_dimension,
        "artifact.members[0].normalization.dense_scale",
    )
    residual_mean = _vector(
        normalization["residual_mean"],
        RESIDUAL_DIMENSION,
        "artifact.members[0].normalization.residual_mean",
    )
    residual_scale = _vector(
        normalization["residual_scale"],
        RESIDUAL_DIMENSION,
        "artifact.members[0].normalization.residual_scale",
    )
    if any(item <= 0.0 for item in dense_scale):
        raise ProtocolError("artifact dense_scale values must be positive")
    if any(item <= 0.0 for item in residual_scale):
        raise ProtocolError("artifact residual_scale values must be positive")

    state = _object(member_value["state_dict"], "artifact.members[0].state_dict")
    _exact_keys(state, _STATE_KEYS, "artifact.members[0].state_dict")
    fusion_dimension = hidden_size + dense_hidden_size
    try:
        encoder = TinyBertWeights(
            word_embeddings=_matrix(
                state["word_embeddings.weight"],
                vocab_size,
                hidden_size,
                "artifact.members[0].state_dict.word_embeddings.weight",
            ),
            position_embeddings=_matrix(
                state["position_embeddings.weight"],
                sequence_length,
                hidden_size,
                "artifact.members[0].state_dict.position_embeddings.weight",
            ),
            type_embeddings=_matrix(
                state["type_embeddings.weight"],
                2,
                hidden_size,
                "artifact.members[0].state_dict.type_embeddings.weight",
            ),
            embedding_norm=_layer_norm(state, "embedding_norm", hidden_size),
            attention_norm=_layer_norm(state, "attention_norm", hidden_size),
            in_projection=LinearWeights(
                weight=_matrix(
                    state["attention.in_proj_weight"],
                    hidden_size * 3,
                    hidden_size,
                    (
                        "artifact.members[0].state_dict."
                        "attention.in_proj_weight"
                    ),
                ),
                bias=_vector(
                    state["attention.in_proj_bias"],
                    hidden_size * 3,
                    (
                        "artifact.members[0].state_dict."
                        "attention.in_proj_bias"
                    ),
                ),
            ),
            out_projection=_linear(
                state,
                "attention.out_proj",
                inputs=hidden_size,
                outputs=hidden_size,
            ),
            feedforward_norm=_layer_norm(
                state, "feedforward_norm", hidden_size
            ),
            feedforward_input=_linear(
                state,
                "feedforward.0",
                inputs=hidden_size,
                outputs=hidden_size * 2,
            ),
            feedforward_output=_linear(
                state,
                "feedforward.3",
                inputs=hidden_size * 2,
                outputs=hidden_size,
            ),
            attention_heads=attention_heads,
            layer_norm_eps=_LAYER_NORM_EPSILON,
        )
        dense_branch = _linear(
            state,
            "dense_branch.0",
            inputs=dense_dimension + RESIDUAL_DIMENSION,
            outputs=dense_hidden_size,
        )
        fusion_norm = _layer_norm(state, "fusion.0", fusion_dimension)
        fusion_hidden = _linear(
            state,
            "fusion.1",
            inputs=fusion_dimension,
            outputs=fusion_hidden_size,
        )
        fusion_output = _linear(
            state,
            "fusion.4",
            inputs=fusion_hidden_size,
            outputs=RESIDUAL_DIMENSION,
        )
    except ValueError as exc:
        raise ProtocolError(f"invalid BERT residual tensor: {exc}") from exc

    return BertResidualArtifact(
        vocab_size=vocab_size,
        sequence_length=sequence_length,
        hidden_size=hidden_size,
        attention_heads=attention_heads,
        dense_dimension=dense_dimension,
        dense_hidden_size=dense_hidden_size,
        fusion_hidden_size=fusion_hidden_size,
        refit_full=refit_full,
        policy_id=policy_id,
        policy_digest=policy_digest,
        training_summary=dict(training_summary),
        member=BertResidualMember(
            seed=seed,
            dense_mean=dense_mean,
            dense_scale=dense_scale,
            residual_mean=residual_mean,
            residual_scale=residual_scale,
            encoder=encoder,
            dense_branch=dense_branch,
            fusion_norm=fusion_norm,
            fusion_hidden=fusion_hidden,
            fusion_output=fusion_output,
        ),
    )


def tokenize_episode(
    episode: Episode,
    artifact: BertResidualArtifact,
    *,
    tokens: Optional[Sequence[str]] = None,
) -> TokenRow:
    """Apply the trainer's hashed word tokenizer and head/tail sampling."""

    normalized = (
        normalized_tokens(episode_text(episode))
        if tokens is None
        else tuple(tokens)
    )
    if any(not isinstance(token, str) for token in normalized):
        raise ValueError("tokens must contain only normalized strings")
    available = artifact.sequence_length - 1
    if len(normalized) <= available:
        sampled = normalized
        types = (0,) * len(sampled)
    else:
        head_count = max(1, available // 3)
        tail_count = available - head_count
        sampled = normalized[:head_count] + normalized[-tail_count:]
        types = (0,) * head_count + (1,) * tail_count

    token_ids = (1,) + tuple(
        3 + stable_hash(token) % (artifact.vocab_size - 3)
        for token in sampled
    )
    token_types = (0,) + types
    token_mask = (True,) * len(token_ids)
    padding = artifact.sequence_length - len(token_ids)
    return (
        token_ids + (0,) * padding,
        token_types + (0,) * padding,
        token_mask + (False,) * padding,
    )


def _freeze_input_vector(
    values: Sequence[float], length: int, label: str
) -> Vector:
    try:
        result = tuple(float(item) for item in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite numeric vector") from exc
    if len(result) != length:
        raise ValueError(f"{label} must contain exactly {length} values")
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} must contain only finite values")
    return result


def _apply_linear(values: Sequence[float], weights: LinearWeights) -> Vector:
    return tuple(
        math.fsum(map(operator.mul, row, values))
        + bias
        for row, bias in zip(weights.weight, weights.bias)
    )


def _apply_layer_norm(
    values: Sequence[float], weights: LayerNormWeights
) -> Vector:
    mean = math.fsum(values) / len(values)
    variance = math.fsum([(item - mean) ** 2 for item in values]) / len(values)
    inverse_scale = 1.0 / math.sqrt(variance + _LAYER_NORM_EPSILON)
    return tuple(
        (item - mean) * inverse_scale * scale + bias
        for item, scale, bias in zip(values, weights.weight, weights.bias)
    )


def _gelu(values: Sequence[float]) -> Vector:
    root_two = math.sqrt(2.0)
    return tuple(
        0.5 * item * (1.0 + math.erf(item / root_two)) for item in values
    )


def predict_bert_residual(
    episode: Episode,
    dense_features: Sequence[float],
    base_predictions: Sequence[float],
    artifact: BertResidualArtifact,
    *,
    tokens: Optional[Sequence[str]] = None,
) -> Vector:
    """Return the six de-normalized residual heads for one episode."""

    dense = _freeze_input_vector(
        dense_features, artifact.dense_dimension, "dense_features"
    )
    base = _freeze_input_vector(
        base_predictions, RESIDUAL_DIMENSION, "base_predictions"
    )
    member = artifact.member
    standardized = tuple(
        (item - mean) / scale
        for item, mean, scale in zip(
            dense, member.dense_mean, member.dense_scale
        )
    )
    token_ids, token_types, token_mask = tokenize_episode(
        episode, artifact, tokens=tokens
    )
    cls_state = encode_cls(
        member.encoder, token_ids, token_types, token_mask
    )
    dense_state = _gelu(
        _apply_linear(standardized + base, member.dense_branch)
    )
    fused = _apply_layer_norm(cls_state + dense_state, member.fusion_norm)
    hidden = _gelu(_apply_linear(fused, member.fusion_hidden))
    normalized = _apply_linear(hidden, member.fusion_output)
    return tuple(
        mean + item * scale
        for item, mean, scale in zip(
            normalized, member.residual_mean, member.residual_scale
        )
    )


__all__ = (
    "ARTIFACT_TYPE",
    "BertResidualArtifact",
    "BertResidualMember",
    "FEATURE_VERSION",
    "MAX_SEQUENCE_LENGTH",
    "RESIDUAL_DIMENSION",
    "SCHEMA_VERSION",
    "parse_bert_residual_artifact",
    "predict_bert_residual",
    "tokenize_episode",
)
