# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import json
import math
import struct
import unittest
import zlib

from ossp_router.protocol import MODEL_IDS, ProtocolError, load_bundled_policy, policy_sha256
from ossp_router.word_tfidf import (
    ARTIFACT_TYPE,
    HEAD_NAMES,
    TOKEN_PATTERN,
    WordTfidfRuntime,
    load_word_tfidf_artifact,
    parse_word_tfidf_artifact,
)


def encoded(values):
    raw = struct.pack(f"<{len(values)}f", *values)
    return base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")


def artifact_value():
    dimension = 2
    coefficients = []
    for head in range(len(HEAD_NAMES)):
        coefficients.extend((1.0 + head, -0.25 * head))
    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": 1,
        "feature_version": 1,
        "model_ids": list(MODEL_IDS),
        "head_names": list(HEAD_NAMES),
        "policy_id": "policy",
        "policy_sha256": "a" * 64,
        "tfidf": {
            "dimension": dimension,
            "vocabulary": ["hello", "hello world"],
            "idf": encoded((1.0, 2.0)),
            "coefficients": encoded(coefficients),
            "coefficient_encoding": "f32le-zlib-base64-v1",
            "token_pattern": TOKEN_PATTERN,
            "ngram_range": [1, 2],
            "min_df": 2,
            "sublinear_tf": True,
            "smooth_idf": True,
            "use_idf": True,
            "lowercase": True,
            "norm": "l2",
        },
        "dense": {
            "dimension": 1,
            "feature_names": ["side"],
            "mean": [1.0],
            "scale": [2.0],
            "coefficients": [[0.5 + head] for head in range(len(HEAD_NAMES))],
            "intercepts": [0.25 * head for head in range(len(HEAD_NAMES))],
        },
        "training_summary": {"num_episodes": 2},
    }


class WordTfidfTests(unittest.TestCase):
    def test_runtime_matches_sublinear_tfidf_formula(self):
        fitted = parse_word_tfidf_artifact(artifact_value())
        runtime = WordTfidfRuntime(fitted)
        actual = runtime.predict_selected(
            "Hello world hello", [3.0], [HEAD_NAMES[0], HEAD_NAMES[1]]
        )
        hello = 1.0 + math.log(2.0)
        bigram = 2.0
        norm = math.sqrt(hello * hello + bigram * bigram)
        expected_zero = hello / norm + 0.5
        expected_one = (
            2.0 * hello / norm
            - 0.25 * bigram / norm
            + 1.5
            + 0.25
        )
        self.assertAlmostEqual(actual[0], expected_zero, places=7)
        self.assertAlmostEqual(actual[1], expected_one, places=7)

    def test_json_loader_and_prediction_are_deterministic(self):
        text = json.dumps(artifact_value(), sort_keys=True)
        runtime = WordTfidfRuntime(load_word_tfidf_artifact(text))
        first = runtime.predict_selected("hello world", [1.0], HEAD_NAMES)
        second = runtime.predict_selected("hello world", [1.0], HEAD_NAMES)
        self.assertEqual(first, second)
        self.assertTrue(runtime.artifact.idf.readonly)
        self.assertTrue(runtime.artifact.coefficients.readonly)
        with self.assertRaises(TypeError):
            runtime.artifact.idf[0] = 2.0
        with self.assertRaises(TypeError):
            runtime.artifact.coefficients[0] = 2.0

    def test_rejects_duplicate_vocabulary(self):
        value = artifact_value()
        value["tfidf"]["vocabulary"] = ["hello", "hello"]
        with self.assertRaises(ProtocolError):
            parse_word_tfidf_artifact(value)

    def test_rejects_decompression_size_mismatch(self):
        value = artifact_value()
        value["tfidf"]["idf"] = encoded((1.0,))
        with self.assertRaises(ProtocolError):
            parse_word_tfidf_artifact(value)

    def test_rejects_unknown_head(self):
        runtime = WordTfidfRuntime(parse_word_tfidf_artifact(artifact_value()))
        with self.assertRaises(ValueError):
            runtime.predict_selected("hello", [1.0], ["missing"])

    def test_rejects_duplicate_json_keys_and_nan(self):
        with self.assertRaises(ProtocolError):
            load_word_tfidf_artifact('{"x": 1, "x": 2}')
        with self.assertRaises(ProtocolError):
            load_word_tfidf_artifact('{"x": NaN}')

    def test_policy_and_dense_feature_binding(self):
        value = artifact_value()
        policy = load_bundled_policy()
        with self.assertRaises(ProtocolError):
            parse_word_tfidf_artifact(value, policy=policy)
        value["policy_id"] = policy.policy_id
        value["policy_sha256"] = policy_sha256(policy)
        fitted = parse_word_tfidf_artifact(
            value, policy=policy, expected_dense_feature_names=["side"]
        )
        self.assertEqual(fitted.dense_feature_names, ("side",))
        with self.assertRaises(ProtocolError):
            parse_word_tfidf_artifact(
                value, policy=policy, expected_dense_feature_names=["different"]
            )


if __name__ == "__main__":
    unittest.main()
