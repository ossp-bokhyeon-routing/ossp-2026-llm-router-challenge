# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from decimal import Decimal
import unittest

import numpy as np

from baselines.validate_content_logo import (
    _assert_cached_outcome_parity,
    _candidate_bytes,
    _canonical_outcome_matrices,
    _content_group,
    _current_conservative_candidate,
    _current_public_candidate,
    _fold_seed,
    _random_candidate,
)
from ossp_router.bert_router import (
    PUBLIC_COST_TIER_CONFIGURATIONS,
    TIER_CONFIGURATIONS,
)
from ossp_router.protocol import (
    MODEL_IDS,
    Episode,
    InputBatch,
    Outcome,
    OutcomeBatch,
    load_bundled_policy,
)


class ContentLogoValidatorTest(unittest.TestCase):
    def test_canonical_outcomes_align_and_exactly_bind_cached_matrices(self) -> None:
        policy = load_bundled_policy()
        inputs = InputBatch(
            schema_version=policy.schema_version,
            challenge_id="outcome-parity-test",
            split="dev",
            episodes=(
                Episode("episode-b", prompt="second"),
                Episode("episode-a", prompt="first"),
            ),
        )
        rows = []
        for episode_index, episode_id in enumerate(("episode-a", "episode-b")):
            for model_index, model_id in enumerate(reversed(MODEL_IDS)):
                rows.append(
                    Outcome(
                        episode_id=episode_id,
                        model_id=model_id,
                        score=Decimal(episode_index * 3 + model_index) / 10,
                        num_generations=2,
                        input_tokens=100 + episode_index,
                        output_tokens=20 + model_index,
                    )
                )
        outcomes = OutcomeBatch(
            schema_version=inputs.schema_version,
            challenge_id=inputs.challenge_id,
            split=inputs.split,
            outcomes=tuple(reversed(rows)),
        )

        scores, costs = _canonical_outcome_matrices(inputs, outcomes, policy)
        np.testing.assert_array_equal(
            scores,
            np.asarray(((0.5, 0.4, 0.3), (0.2, 0.1, 0.0))),
        )
        report = _assert_cached_outcome_parity(
            {"actual_scores": scores.copy(), "actual_costs": costs.copy()},
            scores,
            costs,
        )

        self.assertEqual([2, 3], report["shape"])
        self.assertTrue(report["scores_exact"])
        self.assertTrue(report["costs_exact"])
        self.assertEqual(0.0, report["score_max_abs"])
        self.assertEqual(0.0, report["cost_max_abs"])
        changed = costs.copy()
        changed[0, 0] += 1e-9
        with self.assertRaisesRegex(ValueError, "matrix mismatch"):
            _assert_cached_outcome_parity(
                {"actual_scores": scores, "actual_costs": changed},
                scores,
                costs,
            )

    def test_canonical_outcomes_reject_incomplete_coverage(self) -> None:
        policy = load_bundled_policy()
        inputs = InputBatch(
            policy.schema_version,
            "outcome-coverage-test",
            "dev",
            (Episode("episode", prompt="value"),),
        )
        outcomes = OutcomeBatch(
            inputs.schema_version,
            inputs.challenge_id,
            inputs.split,
            (
                Outcome(
                    "episode",
                    MODEL_IDS[0],
                    Decimal("0.5"),
                    1,
                    1,
                    1,
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            _canonical_outcome_matrices(inputs, outcomes, policy)

    def test_content_families_have_explicit_precedence(self) -> None:
        cases = (
            ("```python\n" + "x" * 8_000, "long-context"),
            ("def solve(value):\n    return value", "code"),
            ("다음 보기 중 고르세요.\nA. 하나\nB. 둘 + 셋", "korean-mcq"),
            ("If someone is green, calculate whether they glow.", "logic-rules"),
            ("Calculate 17 + 25.", "math-reasoning"),
            ("Choose one.\nA. red\nB. blue", "nonko-mcq"),
            ("Explain the main idea in plain language.", "other"),
        )
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, _content_group(text))

    def test_outcome_independent_candidate_generation_is_deterministic(self) -> None:
        for tier in ("fast", "balanced", "premium"):
            with self.subTest(tier=tier):
                first = _random_candidate(np.random.default_rng(20260917), tier)
                second = _random_candidate(np.random.default_rng(20260917), tier)
                self.assertEqual(_candidate_bytes([first]), _candidate_bytes([second]))
                self.assertAlmostEqual(1.0, sum(first["ax_weights"]))
                self.assertAlmostEqual(1.0, sum(first["k1_weights"]))
                self.assertLessEqual(
                    first["all_miss_cap"],
                    TIER_CONFIGURATIONS[tier].predicted_cost_cap,
                )

    def test_fold_seed_is_stable_and_family_specific(self) -> None:
        seed = _fold_seed(20260917, "math-reasoning", "premium")
        self.assertEqual(
            seed,
            _fold_seed(20260917, "math-reasoning", "premium"),
        )
        self.assertNotEqual(
            seed,
            _fold_seed(20260917, "logic-rules", "premium"),
        )

    def test_live_reference_candidates_follow_runtime_constants(self) -> None:
        for tier in ("fast", "balanced", "premium"):
            with self.subTest(tier=tier):
                public = _current_public_candidate(tier)
                conservative = _current_conservative_candidate(tier)
                self.assertEqual(
                    list(
                        PUBLIC_COST_TIER_CONFIGURATIONS[
                            tier
                        ].upgrades[0].component_weights
                    ),
                    public["ax_weights"],
                )
                self.assertEqual(
                    TIER_CONFIGURATIONS[tier].predicted_cost_cap,
                    conservative["all_miss_cap"],
                )


if __name__ == "__main__":
    unittest.main()
