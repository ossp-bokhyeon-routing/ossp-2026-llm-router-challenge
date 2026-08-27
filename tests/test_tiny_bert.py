# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from ossp_router.tiny_bert import (
    LayerNormWeights,
    LinearWeights,
    TinyBertWeights,
    encode_cls,
)


def _matrix(rows, columns, seed, scale):
    return tuple(
        tuple(
            (
                ((row + 1) * 17 + (column + 1) * 13 + seed * 7) % 23
                - 11
            )
            * scale
            for column in range(columns)
        )
        for row in range(rows)
    )


def _vector(size, seed, scale):
    return tuple(
        (((index + 1) * 19 + seed * 5) % 17 - 8) * scale
        for index in range(size)
    )


HIDDEN_SIZE = 4
WEIGHTS = TinyBertWeights(
    word_embeddings=_matrix(7, HIDDEN_SIZE, 1, 0.03),
    position_embeddings=_matrix(5, HIDDEN_SIZE, 2, 0.02),
    type_embeddings=_matrix(2, HIDDEN_SIZE, 3, 0.025),
    embedding_norm=LayerNormWeights(
        (0.85, 1.10, 0.95, 1.20),
        (-0.02, 0.03, 0.01, -0.04),
    ),
    attention_norm=LayerNormWeights(
        (1.05, 0.90, 1.15, 0.80),
        (0.01, -0.01, 0.02, -0.02),
    ),
    in_projection=LinearWeights(
        _matrix(HIDDEN_SIZE * 3, HIDDEN_SIZE, 4, 0.025),
        _vector(HIDDEN_SIZE * 3, 2, 0.012),
    ),
    out_projection=LinearWeights(
        _matrix(HIDDEN_SIZE, HIDDEN_SIZE, 5, 0.03),
        _vector(HIDDEN_SIZE, 3, 0.01),
    ),
    feedforward_norm=LayerNormWeights(
        (0.90, 1.20, 0.80, 1.10),
        (-0.03, 0.02, -0.01, 0.04),
    ),
    feedforward_input=LinearWeights(
        _matrix(HIDDEN_SIZE * 2, HIDDEN_SIZE, 6, 0.022),
        _vector(HIDDEN_SIZE * 2, 4, 0.009),
    ),
    feedforward_output=LinearWeights(
        _matrix(HIDDEN_SIZE, HIDDEN_SIZE * 2, 7, 0.018),
        _vector(HIDDEN_SIZE, 5, 0.008),
    ),
    attention_heads=2,
)

TOKEN_IDS = (1, 5, 3, 2, 6)
TOKEN_TYPES = (0, 0, 1, 1, 0)
PADDED_MASK = (True, True, True, False, False)

# Generated once with torch.float64 using nn.LayerNorm,
# nn.MultiheadAttention(batch_first=True), nn.Linear, and exact nn.GELU.
PYTORCH_PADDED_CLS = (
    -0.4107802707334245,
    1.798806967814613,
    -0.848328131822081,
    -0.10768727130294535,
)
PYTORCH_UNMASKED_CLS = (
    -0.4204525066856257,
    1.7731555591506794,
    -0.7370838962706332,
    -0.10126202996913677,
)


class TinyBertTest(unittest.TestCase):
    def assertVectorClose(self, actual, expected, tolerance=1e-6):
        self.assertEqual(len(actual), len(expected))
        for index, (left, right) in enumerate(zip(actual, expected)):
            self.assertAlmostEqual(
                left,
                right,
                delta=tolerance,
                msg=f"vector element {index} differs",
            )

    def test_cls_matches_pytorch_float64_fixture(self):
        cases = (
            (PADDED_MASK, PYTORCH_PADDED_CLS),
            ((True,) * len(TOKEN_IDS), PYTORCH_UNMASKED_CLS),
        )
        for mask, expected in cases:
            with self.subTest(mask=mask):
                actual = encode_cls(WEIGHTS, TOKEN_IDS, TOKEN_TYPES, mask)
                self.assertVectorClose(actual, expected)

    def test_padding_tokens_do_not_affect_cls(self):
        original = encode_cls(WEIGHTS, TOKEN_IDS, TOKEN_TYPES, PADDED_MASK)
        changed_padding = encode_cls(
            WEIGHTS,
            (1, 5, 3, 6, 4),
            (0, 0, 1, 0, 1),
            PADDED_MASK,
        )
        self.assertEqual(original, changed_padding)
        self.assertVectorClose(changed_padding, PYTORCH_PADDED_CLS)

    def test_inference_is_deterministic(self):
        expected = encode_cls(WEIGHTS, TOKEN_IDS, TOKEN_TYPES, PADDED_MASK)
        for _iteration in range(20):
            self.assertEqual(
                encode_cls(WEIGHTS, TOKEN_IDS, TOKEN_TYPES, PADDED_MASK),
                expected,
            )

    def test_weight_containers_are_deeply_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            WEIGHTS.attention_heads = 1
        with self.assertRaises(TypeError):
            WEIGHTS.word_embeddings[0][0] = 99.0


if __name__ == "__main__":
    unittest.main()
