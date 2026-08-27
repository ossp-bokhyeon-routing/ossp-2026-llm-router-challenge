# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
import math
import pathlib
import unittest

from ossp_router.bert_residual import (
    ARTIFACT_TYPE,
    MAX_SEQUENCE_LENGTH,
    parse_bert_residual_artifact,
    predict_bert_residual,
    tokenize_episode,
)
from ossp_router.hash_linear import (
    parse_hash_artifact,
    predict_hash_linear,
    raw_feature_vector,
    stable_hash,
)
from ossp_router.protocol import (
    MODEL_IDS,
    Episode,
    ProtocolError,
    load_bundled_policy,
    load_input,
    policy_sha256,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _matrix(rows: int, columns: int, seed: int, scale: float):
    return [
        [
            (
                ((row + 1) * 17 + (column + 1) * 13 + seed * 7) % 23
                - 11
            )
            * scale
            for column in range(columns)
        ]
        for row in range(rows)
    ]


def _vector(size: int, seed: int, scale: float):
    return [
        (((index + 1) * 19 + seed * 5) % 17 - 8) * scale
        for index in range(size)
    ]


def _norm_weight(size: int, seed: int):
    return [1.0 + item for item in _vector(size, seed, 0.015)]


def _synthetic_artifact_value():
    policy = load_bundled_policy()
    vocab = 17
    sequence = 8
    hidden = 4
    dense_dimension = 3
    dense_hidden = 5
    fusion_hidden = 4
    fusion_dimension = hidden + dense_hidden
    state = {
        "word_embeddings.weight": _matrix(vocab, hidden, 1, 0.025),
        "position_embeddings.weight": _matrix(sequence, hidden, 2, 0.018),
        "type_embeddings.weight": _matrix(2, hidden, 3, 0.021),
        "embedding_norm.weight": _norm_weight(hidden, 1),
        "embedding_norm.bias": _vector(hidden, 1, 0.008),
        "attention_norm.weight": _norm_weight(hidden, 2),
        "attention_norm.bias": _vector(hidden, 2, 0.007),
        "attention.in_proj_weight": _matrix(hidden * 3, hidden, 4, 0.022),
        "attention.in_proj_bias": _vector(hidden * 3, 3, 0.006),
        "attention.out_proj.weight": _matrix(hidden, hidden, 5, 0.024),
        "attention.out_proj.bias": _vector(hidden, 4, 0.006),
        "feedforward_norm.weight": _norm_weight(hidden, 3),
        "feedforward_norm.bias": _vector(hidden, 5, 0.007),
        "feedforward.0.weight": _matrix(hidden * 2, hidden, 6, 0.019),
        "feedforward.0.bias": _vector(hidden * 2, 6, 0.005),
        "feedforward.3.weight": _matrix(hidden, hidden * 2, 7, 0.017),
        "feedforward.3.bias": _vector(hidden, 7, 0.005),
        "dense_branch.0.weight": _matrix(
            dense_hidden, dense_dimension + 6, 8, 0.016
        ),
        "dense_branch.0.bias": _vector(dense_hidden, 8, 0.006),
        "fusion.0.weight": _norm_weight(fusion_dimension, 4),
        "fusion.0.bias": _vector(fusion_dimension, 9, 0.006),
        "fusion.1.weight": _matrix(
            fusion_hidden, fusion_dimension, 10, 0.018
        ),
        "fusion.1.bias": _vector(fusion_hidden, 10, 0.006),
        "fusion.4.weight": _matrix(6, fusion_hidden, 11, 0.020),
        "fusion.4.bias": _vector(6, 11, 0.007),
    }
    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": 1,
        "feature_version": 1,
        "model_ids": list(MODEL_IDS),
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "configuration": {
            "vocab_size": vocab,
            "sequence_length": sequence,
            "hidden_size": hidden,
            "attention_heads": 2,
            "dense_dimension": dense_dimension,
            "dense_hidden_size": dense_hidden,
            "fusion_hidden_size": fusion_hidden,
            "refit_full": False,
        },
        "members": [
            {
                "seed": 20260826,
                "normalization": {
                    "dense_mean": [0.1, -0.2, 0.3],
                    "dense_scale": [0.8, 1.3, 0.6],
                    "residual_mean": [0.01, -0.02, 0.03, 0.1, -0.2, 0.05],
                    "residual_scale": [0.4, 0.6, 0.5, 1.1, 0.9, 1.3],
                },
                "state_dict": state,
            }
        ],
        "training_summary": {
            "fixture": "torch.float64 cross-check",
            "seed": 20260826,
        },
    }


def _finalize_development_artifact(value):
    policy = load_bundled_policy()
    value = copy.deepcopy(value)
    value["artifact_type"] = ARTIFACT_TYPE
    value["feature_version"] = 1
    value["model_ids"] = list(MODEL_IDS)
    value["policy_id"] = policy.policy_id
    value["policy_sha256"] = policy_sha256(policy)
    value["training_summary"] = {"source": "bert-exp-3"}
    return value


class BertResidualRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_bundled_policy()
        cls.value = _synthetic_artifact_value()
        cls.artifact = parse_bert_residual_artifact(
            cls.value, policy=cls.policy
        )
        cls.episode = Episode(
            "opaque-id",
            prompt=(
                "Alpha beta gamma delta epsilon zeta eta theta iota kappa "
                "lambda"
            ),
        )
        cls.dense = (0.2, -1.1, 2.5)
        cls.base = (0.15, 0.35, 0.55, -0.7, 0.2, 1.1)

    def assertVectorClose(self, actual, expected, tolerance=1e-10):
        self.assertEqual(len(actual), len(expected))
        for index, (left, right) in enumerate(zip(actual, expected)):
            self.assertAlmostEqual(
                left,
                right,
                delta=tolerance,
                msg=f"vector element {index} differs",
            )

    def test_tokenizer_matches_head_tail_and_type_rules(self) -> None:
        ids, types, mask = tokenize_episode(self.episode, self.artifact)
        tokens = (
            "alpha",
            "beta",
            "eta",
            "theta",
            "iota",
            "kappa",
            "lambda",
        )
        expected_ids = (1,) + tuple(
            3 + stable_hash(token) % 14 for token in tokens
        )
        self.assertEqual(expected_ids, ids)
        self.assertEqual((0, 0, 0, 1, 1, 1, 1, 1), types)
        self.assertEqual((True,) * 8, mask)

        short = Episode("different", prompt="One two")
        short_ids, short_types, short_mask = tokenize_episode(
            short, self.artifact
        )
        self.assertEqual(8, len(short_ids))
        self.assertEqual((0,) * 8, short_types)
        self.assertEqual((True, True, True) + (False,) * 5, short_mask)

    def test_full_hybrid_matches_pytorch_float64_fixture(self) -> None:
        # Generated once using the identical nn.Embedding/LayerNorm,
        # nn.MultiheadAttention, dense branch, fusion branch, and exact GELU
        # graph in baselines/train_bert_hybrid.py with torch.float64.
        expected = (
            -0.0017718676526499982,
            0.022206413185891293,
            0.014861206635339689,
            0.23597890325545667,
            -0.18214259014984716,
            0.20549983489076445,
        )
        self.assertVectorClose(
            predict_bert_residual(
                self.episode, self.dense, self.base, self.artifact
            ),
            expected,
        )

    def test_inference_is_deterministic_and_id_independent(self) -> None:
        expected = predict_bert_residual(
            self.episode, self.dense, self.base, self.artifact
        )
        for _iteration in range(10):
            self.assertEqual(
                expected,
                predict_bert_residual(
                    Episode("changed-id", prompt=self.episode.prompt),
                    self.dense,
                    self.base,
                    self.artifact,
                ),
            )

    def test_strict_artifact_validation(self) -> None:
        mutations = []

        extra = copy.deepcopy(self.value)
        extra["extra"] = True
        mutations.append(extra)

        development = copy.deepcopy(self.value)
        development["artifact_type"] = (
            "ossp-tiny-bert-residual-development-v1"
        )
        mutations.append(development)

        wrong_version = copy.deepcopy(self.value)
        wrong_version["feature_version"] = 2
        mutations.append(wrong_version)

        wrong_models = copy.deepcopy(self.value)
        wrong_models["model_ids"] = list(reversed(MODEL_IDS))
        mutations.append(wrong_models)

        long_sequence = copy.deepcopy(self.value)
        long_sequence["configuration"]["sequence_length"] = (
            MAX_SEQUENCE_LENGTH + 1
        )
        mutations.append(long_sequence)

        ensemble = copy.deepcopy(self.value)
        ensemble["members"].append(copy.deepcopy(ensemble["members"][0]))
        mutations.append(ensemble)

        bad_shape = copy.deepcopy(self.value)
        bad_shape["members"][0]["state_dict"][
            "attention.in_proj_weight"
        ][0].pop()
        mutations.append(bad_shape)

        non_finite = copy.deepcopy(self.value)
        non_finite["members"][0]["state_dict"][
            "fusion.4.bias"
        ][0] = math.inf
        mutations.append(non_finite)

        zero_scale = copy.deepcopy(self.value)
        zero_scale["members"][0]["normalization"]["dense_scale"][0] = 0
        mutations.append(zero_scale)

        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                with self.assertRaises(ProtocolError):
                    parse_bert_residual_artifact(mutation)

    def test_policy_binding_is_verified(self) -> None:
        for field, replacement in (
            ("policy_id", "different-policy"),
            ("policy_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                value = copy.deepcopy(self.value)
                value[field] = replacement
                with self.assertRaises(ProtocolError):
                    parse_bert_residual_artifact(value, policy=self.policy)

    def test_invalid_inference_vectors_are_rejected(self) -> None:
        cases = (
            ((1.0, 2.0), self.base),
            (self.dense, (1.0, 2.0)),
            ((math.nan,) + self.dense[1:], self.base),
            (self.dense, (math.inf,) + self.base[1:]),
        )
        for dense, base in cases:
            with self.subTest(dense=dense, base=base):
                with self.assertRaises(ValueError):
                    predict_bert_residual(
                        self.episode, dense, base, self.artifact
                    )

    @unittest.skipUnless(
        (ROOT / "build/bert-exp-3/bert-residual.json").is_file(),
        "development checkpoint is not present",
    )
    def test_selected_checkpoint_matches_cached_dev_prediction(self) -> None:
        development = json.loads(
            (ROOT / "build/bert-exp-3/bert-residual.json").read_text(
                encoding="utf-8"
            )
        )
        artifact = parse_bert_residual_artifact(
            _finalize_development_artifact(development), policy=self.policy
        )
        inputs = load_input(ROOT / "data/dev/inputs-base.json")
        episode = inputs.episodes[0]
        public_value = json.loads(
            (ROOT / "baselines/hash-regex-public.v1.json").read_text(
                encoding="utf-8"
            )
        )
        public = parse_hash_artifact(public_value, policy=self.policy)
        scores, costs = predict_hash_linear(episode, public)
        base = tuple(scores[model_id] for model_id in MODEL_IDS) + tuple(
            math.log(costs[model_id]) for model_id in MODEL_IDS
        )
        dense = raw_feature_vector(episode, public.hash_bins)
        # First row of build/bert-exp-3/dev-predictions.npz, recorded once to
        # keep this cross-check runnable under ``python -S`` without NumPy.
        expected = (
            0.04993767291307449,
            0.032487429678440094,
            0.0895104631781578,
            -0.058085836470127106,
            -0.029057670384645462,
            -0.11604627966880798,
        )
        self.assertVectorClose(
            predict_bert_residual(episode, dense, base, artifact),
            expected,
            tolerance=2e-5,
        )


if __name__ == "__main__":
    unittest.main()
