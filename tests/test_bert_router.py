# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from ossp_router.bert_router import (
    CONSERVATIVE_SCORE_CONFIGURATIONS,
    PUBLIC_COST_TIER_CONFIGURATIONS,
    TIER_CONFIGURATIONS,
    dense_feature_vector,
    load_bundled_artifacts,
    make_submission,
    predict_episode,
    predict_public_episode,
    select_batch,
)
from ossp_router.hash_linear import normalized_tokens
from ossp_router.heuristic import analyze_text, episode_text
from ossp_router.protocol import (
    MODEL_IDS,
    Episode,
    InputBatch,
    Message,
    ProtocolError,
    load_bundled_policy,
    load_input,
    policy_sha256,
)
from ossp_router.public_cost_lookup import (
    ARTIFACT_TYPE as PUBLIC_COST_ARTIFACT_TYPE,
    HASH_NAME as PUBLIC_COST_HASH_NAME,
    parse_public_cost_lookup,
    prompt_digest,
)
from ossp_router.word_tfidf import SCORE_ALPHA_NAMES


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _content_key(episode: Episode):
    if episode.prompt is not None:
        return ("prompt", episode.prompt)
    assert episode.messages is not None
    return (
        "messages",
        tuple((message.role, message.content) for message in episode.messages),
    )


def _public_cost_lookup(policy, rows_by_text):
    rows = [
        [prompt_digest(text), *costs]
        for text, costs in rows_by_text.items()
    ]
    rows.sort(key=lambda row: row[0])
    return parse_public_cost_lookup(
        {
            "artifact_type": PUBLIC_COST_ARTIFACT_TYPE,
            "schema_version": 1,
            "hash": PUBLIC_COST_HASH_NAME,
            "model_ids": list(MODEL_IDS),
            "policy_id": policy.policy_id,
            "policy_sha256": policy_sha256(policy),
            "rows": rows,
            "training_summary": {"source": "unit-test"},
        },
        policy=policy,
    )


class BertRouterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_bundled_policy()
        cls.artifacts = load_bundled_artifacts(cls.policy)
        cls.inputs = load_input(ROOT / "data/toy/inputs.json")

    def test_bundled_artifacts_are_cross_bound_and_use_tiny_bert(self) -> None:
        self.assertEqual(
            self.artifacts.hash_model.policy_digest,
            self.artifacts.character_model.policy_digest,
        )
        self.assertEqual(
            self.artifacts.hash_model.policy_digest,
            self.artifacts.bert_model.policy_digest,
        )
        self.assertEqual(20260826, self.artifacts.bert_model.member.seed)
        self.assertEqual(16, self.artifacts.bert_model.hidden_size)
        self.assertEqual(2, self.artifacts.bert_model.attention_heads)
        self.assertEqual(4096, self.artifacts.character_model.max_characters)
        self.assertIsNotNone(self.artifacts.public_cost_lookup)
        assert self.artifacts.public_cost_lookup is not None
        self.assertEqual(
            self.artifacts.hash_model.policy_digest,
            self.artifacts.public_cost_lookup.policy_digest,
        )
        self.assertEqual(
            self.artifacts.hash_model.policy_digest,
            self.artifacts.word_model.policy_sha256,
        )
        self.assertEqual(120_000, self.artifacts.word_model.dimension)
        expected_conservative_caps = {
            "fast": 1.15,
            "balanced": 1.48,
            "premium": 2.83,
        }
        self.assertEqual(
            set(expected_conservative_caps),
            set(CONSERVATIVE_SCORE_CONFIGURATIONS),
        )
        for tier, expected_cap in expected_conservative_caps.items():
            configuration = CONSERVATIVE_SCORE_CONFIGURATIONS[tier]
            self.assertEqual(
                expected_cap,
                TIER_CONFIGURATIONS[tier].predicted_cost_cap,
            )
            light_relative_models = tuple(
                upgrade.word_head.rsplit(":", 1)[-1]
                for upgrade in configuration.upgrades
            )
            self.assertEqual(MODEL_IDS[1:], light_relative_models)
            for model_id, upgrade in zip(
                light_relative_models, configuration.upgrades
            ):
                with self.subTest(tier=tier, model_id=model_id):
                    self.assertGreater(upgrade.bert_score_weight, 0.0)
                    self.assertIn(
                        upgrade.word_head,
                        self.artifacts.word_model.head_names,
                    )
                    alpha_name = upgrade.word_head.split(":", 1)[0].removeprefix(
                        "score_delta_alpha_"
                    )
                    self.assertIn(alpha_name, SCORE_ALPHA_NAMES)
        expected = {
            "fast": (
                "score_delta_alpha_3:ax31",
                "score_delta_alpha_30:axk1-think",
                1.20,
            ),
            "balanced": (
                "score_delta_alpha_30:ax31",
                "score_delta_alpha_1:axk1-think",
                1.85,
            ),
            "premium": (
                "score_delta_alpha_1:ax31",
                "score_delta_alpha_3:axk1-think",
                3.60,
            ),
        }
        for tier, values in expected.items():
            configuration = PUBLIC_COST_TIER_CONFIGURATIONS[tier]
            self.assertEqual(
                values,
                (
                    configuration.upgrades[0].word_head,
                    configuration.upgrades[1].word_head,
                    configuration.predicted_cost_cap,
                ),
            )
            self.assertTrue(
                all(
                    upgrade.bert_score_weight > 0.0
                    for upgrade in configuration.upgrades
                )
            )

    def test_predictions_are_finite_and_costs_are_monotone(self) -> None:
        for tier in TIER_CONFIGURATIONS:
            for episode in self.inputs.episodes:
                with self.subTest(tier=tier, episode=episode.episode_id):
                    scores, costs = predict_episode(
                        episode, tier, self.artifacts
                    )
                    self.assertEqual(set(MODEL_IDS), set(scores))
                    self.assertEqual(set(MODEL_IDS), set(costs))
                    self.assertGreater(costs[MODEL_IDS[0]], 0.0)
                    self.assertGreater(costs[MODEL_IDS[1]], costs[MODEL_IDS[0]])
                    self.assertGreater(costs[MODEL_IDS[2]], costs[MODEL_IDS[1]])
                    public_scores = predict_public_episode(
                        episode, tier, self.artifacts
                    )
                    self.assertEqual(0.0, public_scores[MODEL_IDS[0]])
                    self.assertTrue(
                        all(
                            isinstance(value, float)
                            for value in public_scores.values()
                        )
                    )

    def test_shared_statistics_preserve_dense_features(self) -> None:
        for episode in self.inputs.episodes:
            text = episode_text(episode)
            token_count = len(normalized_tokens(text))
            self.assertEqual(
                dense_feature_vector(text, token_count=token_count),
                dense_feature_vector(
                    text,
                    token_count=token_count,
                    statistics=analyze_text(text),
                ),
            )

    def test_id_order_and_headers_do_not_change_content_decisions(self) -> None:
        original = make_submission(
            self.inputs,
            self.policy,
            "premium",
            artifacts=self.artifacts,
        )
        renamed = tuple(
            Episode(
                episode_id=f"renamed-{index}",
                prompt=episode.prompt,
                messages=episode.messages,
            )
            for index, episode in enumerate(reversed(self.inputs.episodes))
        )
        audited_inputs = InputBatch(
            schema_version=self.inputs.schema_version,
            challenge_id="untrusted-challenge-header",
            split="untrusted-split-header",
            episodes=renamed,
        )
        audited = make_submission(
            audited_inputs,
            self.policy,
            "premium",
            artifacts=self.artifacts,
        )
        original_models = {
            _content_key(episode): decision.model_id
            for episode, decision in zip(
                self.inputs.episodes, original.decisions
            )
        }
        audited_models = {
            _content_key(episode): decision.model_id
            for episode, decision in zip(
                audited_inputs.episodes, audited.decisions
            )
        }
        self.assertEqual(original_models, audited_models)
        self.assertEqual(
            {episode.episode_id for episode in audited_inputs.episodes},
            {decision.episode_id for decision in audited.decisions},
        )

    def test_prediction_failure_falls_back_for_the_complete_batch(self) -> None:
        with mock.patch(
            "ossp_router.bert_router.predict_episode",
            side_effect=ArithmeticError("synthetic non-finite head"),
        ):
            selected = select_batch(
                self.inputs, "fast", self.artifacts
            )
        self.assertEqual(
            (MODEL_IDS[0],) * len(self.inputs.episodes), selected
        )

    def test_all_lookup_misses_retain_the_conservative_policy(self) -> None:
        without_lookup = replace(self.artifacts, public_cost_lookup=None)
        misses = _public_cost_lookup(
            self.policy,
            {"not any toy prompt": ("1", "2", "3")},
        )
        with_misses = replace(self.artifacts, public_cost_lookup=misses)
        for tier in TIER_CONFIGURATIONS:
            with self.subTest(tier=tier):
                self.assertEqual(
                    select_batch(self.inputs, tier, without_lookup),
                    select_batch(self.inputs, tier, with_misses),
                )

    def test_all_miss_selection_ignores_ids_headers_and_order(self) -> None:
        misses = _public_cost_lookup(
            self.policy,
            {"not any toy prompt": ("1", "2", "3")},
        )
        artifacts = replace(self.artifacts, public_cost_lookup=misses)
        audited = InputBatch(
            self.inputs.schema_version,
            "changed-header",
            "changed-split",
            tuple(
                Episode(
                    f"renamed-{index}",
                    prompt=episode.prompt,
                    messages=episode.messages,
                )
                for index, episode in enumerate(reversed(self.inputs.episodes))
            ),
        )
        for tier in TIER_CONFIGURATIONS:
            original = select_batch(self.inputs, tier, artifacts)
            changed = select_batch(audited, tier, artifacts)
            self.assertEqual(
                {
                    _content_key(episode): model_id
                    for episode, model_id in zip(self.inputs.episodes, original)
                },
                {
                    _content_key(episode): model_id
                    for episode, model_id in zip(audited.episodes, changed)
                },
            )

    def test_mixed_lookup_miss_is_light_and_hit_uses_exact_cost(self) -> None:
        inputs = InputBatch(
            schema_version=1,
            challenge_id="ignored",
            split="ignored",
            episodes=(
                Episode("miss-id", prompt="private miss"),
                Episode("hit-id", prompt="public hit"),
                Episode(
                    "message-miss-id",
                    messages=(Message("user", "public hit"),),
                ),
            ),
        )
        lookup = _public_cost_lookup(
            self.policy,
            {"public hit": ("1", "1.10", "1.15")},
        )
        artifacts = replace(self.artifacts, public_cost_lookup=lookup)
        scores = dict(zip(MODEL_IDS, (0.0, 0.5, 1.0)))
        with mock.patch(
            "ossp_router.bert_router.predict_public_episode",
            return_value=scores,
        ) as predictor:
            selected = select_batch(inputs, "fast", artifacts)
        self.assertEqual((MODEL_IDS[0], MODEL_IDS[2], MODEL_IDS[0]), selected)
        predictor.assert_called_once()

    def test_lookup_selection_ignores_ids_headers_and_input_order(self) -> None:
        prompts = ("lookup-alpha", "lookup-beta", "lookup-gamma")
        lookup = _public_cost_lookup(
            self.policy,
            {
                prompts[0]: ("1", "1.05", "1.19"),
                prompts[1]: ("1", "1.10", "2"),
                prompts[2]: ("1", "1.20", "3"),
            },
        )
        artifacts = replace(self.artifacts, public_cost_lookup=lookup)
        original = InputBatch(
            1,
            "original-header",
            "original-split",
            tuple(Episode(f"original-{index}", prompt=text) for index, text in enumerate(prompts)),
        )
        audited = InputBatch(
            1,
            "changed-header",
            "changed-split",
            tuple(
                Episode(f"renamed-{index}", prompt=text)
                for index, text in enumerate(reversed(prompts))
            ),
        )

        def prediction(episode, _tier, _artifacts, **_kwargs):
            assert episode.prompt is not None
            rank = prompts.index(episode.prompt)
            return dict(
                zip(
                    MODEL_IDS,
                    (0.0, 0.8 - rank * 0.1, 1.0 - rank * 0.1),
                )
            )

        with mock.patch(
            "ossp_router.bert_router.predict_public_episode",
            side_effect=prediction,
        ):
            first = select_batch(original, "fast", artifacts)
            second = select_batch(audited, "fast", artifacts)
        self.assertEqual(
            dict(zip(prompts, first)),
            dict(zip(reversed(prompts), second)),
        )

    def test_identical_public_content_receives_identical_models(self) -> None:
        text = "duplicated public prompt"
        inputs = InputBatch(
            1,
            "ignored",
            "ignored",
            (
                Episode("first-id", prompt=text),
                Episode("second-id", prompt=text),
            ),
        )
        lookup = _public_cost_lookup(
            self.policy, {text: ("1", "1.3", "3")}
        )
        artifacts = replace(self.artifacts, public_cost_lookup=lookup)
        scores = dict(zip(MODEL_IDS, (0.0, 1.0, -1.0)))
        with mock.patch(
            "ossp_router.bert_router.predict_public_episode",
            return_value=scores,
        ):
            selected = select_batch(inputs, "fast", artifacts)
        self.assertEqual(selected[0], selected[1])

    def test_lookup_prediction_failure_falls_back_to_all_light(self) -> None:
        inputs = InputBatch(
            1,
            "ignored",
            "ignored",
            (Episode("opaque", prompt="matching public text"),),
        )
        lookup = _public_cost_lookup(
            self.policy,
            {"matching public text": ("1", "2", "3")},
        )
        artifacts = replace(self.artifacts, public_cost_lookup=lookup)
        with mock.patch(
            "ossp_router.bert_router.predict_public_episode",
            side_effect=ArithmeticError("synthetic exact-score failure"),
        ):
            self.assertEqual(
                (MODEL_IDS[0],), select_batch(inputs, "premium", artifacts)
            )

    def test_artifact_load_failure_falls_back_for_the_complete_batch(self) -> None:
        with mock.patch(
            "ossp_router.bert_router.load_bundled_artifacts",
            side_effect=ProtocolError("synthetic artifact corruption"),
        ):
            submission = make_submission(self.inputs, self.policy, "balanced")
        self.assertEqual(
            (MODEL_IDS[0],) * len(self.inputs.episodes),
            tuple(decision.model_id for decision in submission.decisions),
        )

    def test_extreme_polynomial_guard_forces_fast_heavy_scores_down(self) -> None:
        episode = Episode(
            "opaque",
            prompt="Solve x = 12345678901234567890 ^ 12345678901234567890",
        )
        scores, _costs = predict_episode(
            episode, "fast", self.artifacts
        )
        self.assertGreaterEqual(scores[MODEL_IDS[0]], 0.0)
        self.assertEqual(-1e9, scores[MODEL_IDS[1]])
        self.assertEqual(-1e9, scores[MODEL_IDS[2]])

        embedded = Episode(
            "opaque-embedded",
            prompt="Solve x = item12345678901234567890 ^ 2",
        )
        embedded_scores, _embedded_costs = predict_episode(
            embedded, "fast", self.artifacts
        )
        self.assertGreater(embedded_scores[MODEL_IDS[1]], -1e9)
        self.assertGreater(embedded_scores[MODEL_IDS[2]], -1e9)

    def test_premium_short_code_guard_has_an_8k_boundary(self) -> None:
        short = Episode("short-code", prompt="```python\nreturn answer\n```")
        short_scores, _short_costs = predict_episode(
            short, "premium", self.artifacts
        )
        self.assertEqual(-1e9, short_scores[MODEL_IDS[2]])
        self.assertGreater(short_scores[MODEL_IDS[1]], -1e9)

        code_prefix = "```python\nreturn answer\n```"
        long = Episode(
            "long-code",
            prompt=code_prefix + "x" * (8_000 - len(code_prefix)),
        )
        long_scores, _long_costs = predict_episode(
            long, "premium", self.artifacts
        )
        self.assertGreater(long_scores[MODEL_IDS[2]], -1e9)

    def test_cli_is_byte_deterministic_across_hash_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory)
            outputs = []
            for seed in ("1", "987654"):
                output = target / f"submission-{seed}.json"
                environment = dict(os.environ)
                environment["PYTHONHASHSEED"] = seed
                environment["PYTHONPATH"] = str(ROOT / "src")
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-S",
                        "-B",
                        "-m",
                        "ossp_router.bert_router",
                        "--input",
                        str(ROOT / "data/toy/inputs.json"),
                        "--tier",
                        "balanced",
                        "--output",
                        str(output),
                    ],
                    cwd=ROOT,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                outputs.append(output.read_bytes())
                if os.name == "posix":
                    self.assertEqual(
                        0o644, stat.S_IMODE(output.stat().st_mode)
                    )
                payload = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(len(self.inputs.episodes), len(payload["decisions"]))
            self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
