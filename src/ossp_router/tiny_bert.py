# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Standard-library inference for the ``[CLS]`` row of a tiny BERT block.

The implementation mirrors the evaluation path used by PyTorch's
``MultiheadAttention(batch_first=True)`` followed by a pre-norm feed-forward
block.  Only the ``[CLS]`` query is projected.  Keys and values are streamed
through an online softmax, so attention and feed-forward rows for the other
tokens are never materialized.
"""

from __future__ import annotations

import math
import operator
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


Vector = Tuple[float, ...]
Matrix = Tuple[Vector, ...]


def _freeze_vector(values: Sequence[float], name: str) -> Vector:
    try:
        frozen = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite numeric vector") from exc
    if not frozen:
        raise ValueError(f"{name} must not be empty")
    if not all(math.isfinite(value) for value in frozen):
        raise ValueError(f"{name} must contain only finite values")
    return frozen


def _freeze_matrix(values: Sequence[Sequence[float]], name: str) -> Matrix:
    try:
        rows = tuple(
            _freeze_vector(row, f"{name}[{index}]")
            for index, row in enumerate(values)
        )
    except TypeError as exc:
        raise ValueError(f"{name} must be a numeric matrix") from exc
    if not rows:
        raise ValueError(f"{name} must not be empty")
    columns = len(rows[0])
    if any(len(row) != columns for row in rows):
        raise ValueError(f"{name} must be rectangular")
    return rows


@dataclass(frozen=True)
class LayerNormWeights:
    """Immutable affine parameters for one layer-normalization operation."""

    weight: Vector
    bias: Vector

    def __post_init__(self) -> None:
        weight = _freeze_vector(self.weight, "layer norm weight")
        bias = _freeze_vector(self.bias, "layer norm bias")
        if len(weight) != len(bias):
            raise ValueError("layer norm weight and bias dimensions must match")
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "bias", bias)


@dataclass(frozen=True)
class LinearWeights:
    """Immutable PyTorch-layout linear parameters (out_features, in_features)."""

    weight: Matrix
    bias: Vector

    def __post_init__(self) -> None:
        weight = _freeze_matrix(self.weight, "linear weight")
        bias = _freeze_vector(self.bias, "linear bias")
        if len(weight) != len(bias):
            raise ValueError("linear weight rows must match the bias dimension")
        object.__setattr__(self, "weight", weight)
        object.__setattr__(self, "bias", bias)

    @property
    def input_size(self) -> int:
        return len(self.weight[0])

    @property
    def output_size(self) -> int:
        return len(self.weight)


def _require_matrix_shape(
    matrix: Matrix,
    *,
    name: str,
    columns: int,
    rows: Optional[int] = None,
) -> None:
    if rows is not None and len(matrix) != rows:
        raise ValueError(f"{name} must have {rows} rows")
    if any(len(row) != columns for row in matrix):
        raise ValueError(f"{name} rows must have {columns} columns")


def _require_linear_shape(
    linear: LinearWeights,
    *,
    name: str,
    inputs: int,
    outputs: int,
) -> None:
    if linear.input_size != inputs or linear.output_size != outputs:
        raise ValueError(
            f"{name} must have shape ({outputs}, {inputs}), got "
            f"({linear.output_size}, {linear.input_size})"
        )


@dataclass(frozen=True)
class TinyBertWeights:
    """Immutable weights for one pre-norm BERT-style encoder block.

    ``in_projection`` follows ``torch.nn.MultiheadAttention`` and concatenates
    Q, K, and V weights in that order along its output dimension.
    """

    word_embeddings: Matrix
    position_embeddings: Matrix
    type_embeddings: Matrix
    embedding_norm: LayerNormWeights
    attention_norm: LayerNormWeights
    in_projection: LinearWeights
    out_projection: LinearWeights
    feedforward_norm: LayerNormWeights
    feedforward_input: LinearWeights
    feedforward_output: LinearWeights
    attention_heads: int
    layer_norm_eps: float = 1e-5

    def __post_init__(self) -> None:
        word_embeddings = _freeze_matrix(self.word_embeddings, "word embeddings")
        position_embeddings = _freeze_matrix(
            self.position_embeddings, "position embeddings"
        )
        type_embeddings = _freeze_matrix(self.type_embeddings, "type embeddings")
        object.__setattr__(self, "word_embeddings", word_embeddings)
        object.__setattr__(self, "position_embeddings", position_embeddings)
        object.__setattr__(self, "type_embeddings", type_embeddings)

        hidden_size = len(self.embedding_norm.weight)
        _require_matrix_shape(
            word_embeddings,
            name="word embeddings",
            columns=hidden_size,
        )
        _require_matrix_shape(
            position_embeddings,
            name="position embeddings",
            columns=hidden_size,
        )
        _require_matrix_shape(
            type_embeddings,
            name="type embeddings",
            columns=hidden_size,
        )
        for name, layer_norm in (
            ("attention norm", self.attention_norm),
            ("feed-forward norm", self.feedforward_norm),
        ):
            if len(layer_norm.weight) != hidden_size:
                raise ValueError(f"{name} dimension must be {hidden_size}")

        if (
            isinstance(self.attention_heads, bool)
            or not isinstance(self.attention_heads, int)
            or self.attention_heads <= 0
        ):
            raise ValueError("attention_heads must be a positive integer")
        if hidden_size % self.attention_heads:
            raise ValueError("hidden size must be divisible by attention_heads")
        _require_linear_shape(
            self.in_projection,
            name="in projection",
            inputs=hidden_size,
            outputs=hidden_size * 3,
        )
        _require_linear_shape(
            self.out_projection,
            name="out projection",
            inputs=hidden_size,
            outputs=hidden_size,
        )
        _require_linear_shape(
            self.feedforward_input,
            name="feed-forward input",
            inputs=hidden_size,
            outputs=hidden_size * 2,
        )
        _require_linear_shape(
            self.feedforward_output,
            name="feed-forward output",
            inputs=hidden_size * 2,
            outputs=hidden_size,
        )
        try:
            epsilon = float(self.layer_norm_eps)
        except (TypeError, ValueError) as exc:
            raise ValueError("layer_norm_eps must be a positive finite number") from exc
        if not math.isfinite(epsilon) or epsilon <= 0.0:
            raise ValueError("layer_norm_eps must be a positive finite number")
        object.__setattr__(self, "layer_norm_eps", epsilon)

    @property
    def hidden_size(self) -> int:
        return len(self.embedding_norm.weight)


def _layer_norm(
    values: Sequence[float],
    parameters: LayerNormWeights,
    epsilon: float,
) -> Vector:
    dimension = len(values)
    mean = math.fsum(values) / dimension
    variance = math.fsum([(value - mean) ** 2 for value in values]) / dimension
    inverse_scale = 1.0 / math.sqrt(variance + epsilon)
    return tuple(
        (value - mean) * inverse_scale * weight + bias
        for value, weight, bias in zip(
            values,
            parameters.weight,
            parameters.bias,
        )
    )


def _linear_slice(
    values: Sequence[float],
    parameters: LinearWeights,
    start: int,
    stop: int,
) -> Vector:
    return tuple(
        math.fsum(map(operator.mul, row, values))
        + parameters.bias[index]
        for index, row in enumerate(parameters.weight[start:stop], start=start)
    )


def _linear(values: Sequence[float], parameters: LinearWeights) -> Vector:
    return _linear_slice(values, parameters, 0, parameters.output_size)


def _embedding_row(
    weights: TinyBertWeights,
    token_id: int,
    type_id: int,
    position: int,
) -> Vector:
    combined = tuple(
        word + positional + token_type
        for word, positional, token_type in zip(
            weights.word_embeddings[token_id],
            weights.position_embeddings[position],
            weights.type_embeddings[type_id],
        )
    )
    return _layer_norm(combined, weights.embedding_norm, weights.layer_norm_eps)


def _validate_inputs(
    weights: TinyBertWeights,
    token_ids: Sequence[int],
    token_type_ids: Sequence[int],
    token_mask: Optional[Sequence[bool]],
) -> Tuple[Tuple[int, ...], Tuple[int, ...], Tuple[bool, ...]]:
    ids = tuple(token_ids)
    types = tuple(token_type_ids)
    if not ids:
        raise ValueError("token_ids must include a [CLS] token")
    if len(types) != len(ids):
        raise ValueError("token_ids and token_type_ids lengths must match")
    if len(ids) > len(weights.position_embeddings):
        raise ValueError("sequence exceeds the position embedding table")
    if token_mask is None:
        mask = (True,) * len(ids)
    else:
        mask = tuple(token_mask)
        if len(mask) != len(ids):
            raise ValueError("token_ids and token_mask lengths must match")
        if any(not isinstance(value, bool) for value in mask):
            raise ValueError("token_mask values must be bool")
    if not mask[0]:
        raise ValueError("the [CLS] token must not be padding")

    for index, token_id in enumerate(ids):
        if isinstance(token_id, bool) or not isinstance(token_id, int):
            raise ValueError(f"token_ids[{index}] must be an integer")
        if token_id < 0 or token_id >= len(weights.word_embeddings):
            raise ValueError(f"token_ids[{index}] is outside the embedding table")
    for index, type_id in enumerate(types):
        if isinstance(type_id, bool) or not isinstance(type_id, int):
            raise ValueError(f"token_type_ids[{index}] must be an integer")
        if type_id < 0 or type_id >= len(weights.type_embeddings):
            raise ValueError(f"token_type_ids[{index}] is outside the embedding table")
    return ids, types, mask


def _exact_gelu(value: float) -> float:
    return 0.5 * value * (1.0 + math.erf(value / math.sqrt(2.0)))


def encode_cls(
    weights: TinyBertWeights,
    token_ids: Sequence[int],
    token_type_ids: Sequence[int],
    token_mask: Optional[Sequence[bool]] = None,
) -> Vector:
    """Return the final ``[CLS]`` row for one token sequence.

    ``token_mask`` uses the public trainer's convention: ``True`` marks a key
    that participates in attention and ``False`` marks padding.  It maps to the
    inverse of PyTorch's ``key_padding_mask``.
    """

    ids, types, mask = _validate_inputs(
        weights,
        token_ids,
        token_type_ids,
        token_mask,
    )
    hidden_size = weights.hidden_size
    head_count = weights.attention_heads
    head_size = hidden_size // head_count

    cls_residual = _embedding_row(weights, ids[0], types[0], 0)
    cls_attention_input = _layer_norm(
        cls_residual,
        weights.attention_norm,
        weights.layer_norm_eps,
    )
    query = _linear_slice(
        cls_attention_input,
        weights.in_projection,
        0,
        hidden_size,
    )

    maxima = [-math.inf] * head_count
    denominators = [0.0] * head_count
    numerators = [[0.0] * head_size for _ in range(head_count)]
    attention_scale = 1.0 / math.sqrt(head_size)

    for position, (token_id, type_id, is_token) in enumerate(
        zip(ids, types, mask)
    ):
        if not is_token:
            continue
        if position == 0:
            attention_input = cls_attention_input
        else:
            residual = _embedding_row(weights, token_id, type_id, position)
            attention_input = _layer_norm(
                residual,
                weights.attention_norm,
                weights.layer_norm_eps,
            )
        key_and_value = _linear_slice(
            attention_input,
            weights.in_projection,
            hidden_size,
            hidden_size * 3,
        )
        key = key_and_value[:hidden_size]
        value = key_and_value[hidden_size:]

        for head in range(head_count):
            start = head * head_size
            stop = start + head_size
            score = attention_scale * math.fsum(
                map(operator.mul, query[start:stop], key[start:stop])
            )
            previous_maximum = maxima[head]
            if score <= previous_maximum:
                contribution = math.exp(score - previous_maximum)
                denominators[head] += contribution
                for offset, index in enumerate(range(start, stop)):
                    numerators[head][offset] += contribution * value[index]
            else:
                rescale = (
                    math.exp(previous_maximum - score)
                    if previous_maximum != -math.inf
                    else 0.0
                )
                denominators[head] = denominators[head] * rescale + 1.0
                for offset, index in enumerate(range(start, stop)):
                    numerators[head][offset] = (
                        numerators[head][offset] * rescale + value[index]
                    )
                maxima[head] = score

    attended = tuple(
        numerators[head][offset] / denominators[head]
        for head in range(head_count)
        for offset in range(head_size)
    )
    projected = _linear(attended, weights.out_projection)
    attention_residual = tuple(
        residual + update for residual, update in zip(cls_residual, projected)
    )

    feedforward_input = _layer_norm(
        attention_residual,
        weights.feedforward_norm,
        weights.layer_norm_eps,
    )
    expanded = _linear(feedforward_input, weights.feedforward_input)
    activated = tuple(_exact_gelu(value) for value in expanded)
    feedforward_update = _linear(activated, weights.feedforward_output)
    return tuple(
        residual + update
        for residual, update in zip(attention_residual, feedforward_update)
    )


__all__ = [
    "LayerNormWeights",
    "LinearWeights",
    "TinyBertWeights",
    "encode_cls",
]
