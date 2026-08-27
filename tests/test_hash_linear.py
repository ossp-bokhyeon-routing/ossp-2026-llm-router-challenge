# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys
import unittest

import ossp_router.hash_linear as hash_linear
from ossp_router.hash_linear import (
    FEATURE_HASH_CACHE_SIZE,
    normalized_tokens,
    parse_hash_artifact,
    predict_hash_linear,
    raw_feature_vector,
    raw_feature_vector_from_tokens,
    select_models,
    stable_hash,
)
from ossp_router.heuristic import analyze_text, episode_text, extract_features
from ossp_router.protocol import (
    MODEL_IDS,
    ProtocolError,
    load_bundled_policy,
    load_input,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_reference_module():
    name = "_hash_regex_runtime_reference"
    path = ROOT / "baselines/hash_regex.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REFERENCE = _load_reference_module()


class HashLinearRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.artifact_value = json.loads(
            (ROOT / "baselines/hash-regex-public.v1.json").read_text(
                encoding="utf-8"
            )
        )
        cls.policy = load_bundled_policy()
        cls.artifact = parse_hash_artifact(
            cls.artifact_value, policy=cls.policy
        )
        cls.reference_artifact = REFERENCE.parse_artifact(cls.artifact_value)
        cls.inputs = load_input(ROOT / "data/toy/inputs.json")

    def test_hash_and_tokenization_match_public_baseline(self) -> None:
        cases = (
            "",
            "Hello HELLO 17 25!",
            "증명하고 Python 코드도 분석하세요.",
            "İ Unicode １２ and 12",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(REFERENCE._stable_hash(text), stable_hash(text))
                self.assertEqual(
                    REFERENCE._normalized_tokens(text), normalized_tokens(text)
                )

    def test_feature_hash_cache_has_a_fixed_memory_bound(self) -> None:
        parameters = hash_linear._cached_feature_hash.cache_parameters()
        self.assertEqual(FEATURE_HASH_CACHE_SIZE, parameters["maxsize"])
        self.assertFalse(parameters["typed"])

    def test_raw_features_match_public_baseline_on_toy_episodes(self) -> None:
        for episode in self.inputs.episodes:
            with self.subTest(episode=episode.episode_id):
                expected = REFERENCE.raw_feature_vector(
                    episode, self.reference_artifact.hash_bins
                )
                actual = raw_feature_vector(episode, self.artifact.hash_bins)
                self.assertEqual(expected, actual)
                self.assertEqual(14 + self.artifact.hash_bins, len(actual))

    def test_precomputed_prompt_statistics_preserve_raw_features(self) -> None:
        for episode in self.inputs.episodes:
            text = episode_text(episode)
            tokens = normalized_tokens(text)
            expected = raw_feature_vector_from_tokens(
                episode, self.artifact.hash_bins, tokens
            )
            statistics = analyze_text(text)
            actual = raw_feature_vector_from_tokens(
                episode,
                self.artifact.hash_bins,
                tokens,
                prompt_features=extract_features(
                    episode, statistics=statistics
                ),
            )
            self.assertEqual(expected, actual)

    def test_predictions_and_selection_match_public_baseline(self) -> None:
        expected_predictions = [
            REFERENCE.predict_episode(episode, self.reference_artifact)
            for episode in self.inputs.episodes
        ]
        actual_predictions = [
            predict_hash_linear(episode, self.artifact)
            for episode in self.inputs.episodes
        ]
        self.assertEqual(expected_predictions, actual_predictions)

        expected_scores = [item[0] for item in expected_predictions]
        expected_costs = [item[1] for item in expected_predictions]
        actual_scores = [item[0] for item in actual_predictions]
        actual_costs = [item[1] for item in actual_predictions]
        for tier in ("fast", "balanced", "premium"):
            with self.subTest(tier=tier):
                kwargs = {
                    "budget_multiplier": float(
                        self.policy.tiers[tier].budget_multiplier
                    ),
                    "safety_ratio": self.artifact.tier_safety_ratios[tier],
                }
                self.assertEqual(
                    REFERENCE.select_models(
                        expected_scores, expected_costs, **kwargs
                    ),
                    select_models(actual_scores, actual_costs, **kwargs),
                )

    def test_invalid_artifacts_are_rejected(self) -> None:
        mutations = []

        extra = copy.deepcopy(self.artifact_value)
        extra["undeclared_extension"] = True
        mutations.append(extra)

        wrong_models = copy.deepcopy(self.artifact_value)
        wrong_models["model_ids"] = list(reversed(MODEL_IDS))
        mutations.append(wrong_models)

        bad_scale = copy.deepcopy(self.artifact_value)
        bad_scale["feature_scale"][0] = 0.0
        mutations.append(bad_scale)

        bad_hash_bins = copy.deepcopy(self.artifact_value)
        bad_hash_bins["hash_bins"] = 24
        mutations.append(bad_hash_bins)

        for index, value in enumerate(mutations):
            with self.subTest(mutation=index):
                with self.assertRaises(ProtocolError):
                    parse_hash_artifact(value)

    def test_policy_id_and_sha_are_verified(self) -> None:
        for field, replacement in (
            ("policy_id", "different-policy"),
            ("policy_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                value = copy.deepcopy(self.artifact_value)
                value[field] = replacement
                with self.assertRaises(ProtocolError):
                    parse_hash_artifact(value, policy=self.policy)

    def test_model_and_row_ties_do_not_depend_on_position(self) -> None:
        tied_scores = {
            "ax31-light": 0.0,
            "ax31": 1.0,
            "axk1-think": 1.0,
        }
        tied_costs = {
            "ax31-light": 1.0,
            "ax31": 2.0,
            "axk1-think": 2.0,
        }
        light_scores = {
            "ax31-light": 1.0,
            "ax31": 0.0,
            "axk1-think": 0.0,
        }
        light_costs = {
            "ax31-light": 1.0,
            "ax31": 2.0,
            "axk1-think": 3.0,
        }
        rows = (
            ("tie-a", tied_scores, tied_costs),
            ("light", light_scores, light_costs),
            ("tie-b", tied_scores, tied_costs),
        )

        def route(order, budget_multiplier):
            selected, ratio = select_models(
                tuple(row[1] for row in order),
                tuple(row[2] for row in order),
                budget_multiplier=budget_multiplier,
                safety_ratio=1.0,
            )
            return {
                row[0]: model_id
                for row, model_id in zip(order, selected)
            }, ratio

        # With ample budget, AX31 wins an exact AX31/K1 utility tie because
        # MODEL_IDS is the fixed tie-break order.
        ample, _ample_ratio = route(rows, 2.0)
        self.assertEqual("ax31", ample["tie-a"])
        self.assertEqual("ax31", ample["tie-b"])

        # A cap that could fit only one of two identical upgrades must not use
        # row position to split the tie: the entire tied group remains Light.
        selected_by_name, ratio = route(
            rows,
            4.0 / 3.0,
        )
        reversed_by_name, reversed_ratio = route(
            tuple(reversed(rows)),
            4.0 / 3.0,
        )
        self.assertEqual(selected_by_name, reversed_by_name)
        self.assertEqual(
            {
                "tie-a": "ax31-light",
                "light": "ax31-light",
                "tie-b": "ax31-light",
            },
            selected_by_name,
        )
        self.assertEqual(ratio, reversed_ratio)

        reference_selected, reference_ratio = REFERENCE.select_models(
            tuple(row[1] for row in rows),
            tuple(row[2] for row in rows),
            budget_multiplier=4.0 / 3.0,
            safety_ratio=1.0,
        )
        self.assertEqual(
            tuple(selected_by_name[row[0]] for row in rows),
            reference_selected,
        )
        self.assertEqual(ratio, reference_ratio)

    def test_raw_feature_vector_rejects_invalid_hash_dimensions(self) -> None:
        episode = self.inputs.episodes[0]
        for hash_bins in (True, 0, 15, 24, 32_768):
            with self.subTest(hash_bins=hash_bins):
                with self.assertRaises(ValueError):
                    raw_feature_vector(episode, hash_bins)


if __name__ == "__main__":
    unittest.main()
