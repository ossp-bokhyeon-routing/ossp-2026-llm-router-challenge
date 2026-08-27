# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
import pathlib
import unittest
from decimal import Decimal

from ossp_router.protocol import (
    MODEL_IDS,
    ProtocolError,
    load_bundled_policy,
    load_input,
    load_outcomes,
    policy_sha256,
)

from ossp_router.public_cost_lookup import (
    ARTIFACT_TYPE,
    HASH_NAME,
    parse_public_cost_lookup,
    prompt_digest,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _artifact(policy):
    rows = [
        [prompt_digest("alpha"), "0.10", "0.20", "0.30"],
        [prompt_digest("한글\ntext"), "1", "2.25", "3.500"],
    ]
    rows.sort(key=lambda row: row[0])
    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": 1,
        "hash": HASH_NAME,
        "model_ids": list(MODEL_IDS),
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "rows": rows,
        "training_summary": {"source": "unit-test"},
    }


class PublicCostLookupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_bundled_policy()

    def test_prompt_digest_is_exact_utf8_sha256(self) -> None:
        self.assertEqual(
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            prompt_digest("hello"),
        )
        self.assertNotEqual(prompt_digest("line\n"), prompt_digest("line\r\n"))
        with self.assertRaises(TypeError):
            prompt_digest(b"hello")  # type: ignore[arg-type]

    def test_parser_binds_policy_and_preserves_exact_decimal_rows(self) -> None:
        fitted = parse_public_cost_lookup(
            _artifact(self.policy), policy=self.policy
        )
        self.assertEqual(HASH_NAME, fitted.hash_name)
        self.assertEqual(tuple(MODEL_IDS), fitted.model_ids)
        self.assertEqual(
            (Decimal("0.10"), Decimal("0.20"), Decimal("0.30")),
            fitted.costs_for_text("alpha"),
        )
        self.assertIsNone(fitted.costs_for_text("not public"))
        self.assertIsNone(fitted.costs_for_digest("A" * 64))

    def test_parser_rejects_wrong_policy_binding(self) -> None:
        value = _artifact(self.policy)
        value["policy_sha256"] = "0" * 64
        with self.assertRaises(ProtocolError):
            parse_public_cost_lookup(value, policy=self.policy)

    def test_parser_rejects_noncanonical_structure_and_values(self) -> None:
        cases = []

        extra = _artifact(self.policy)
        extra["unexpected"] = True
        cases.append(extra)

        boolean_schema = _artifact(self.policy)
        boolean_schema["schema_version"] = True
        cases.append(boolean_schema)

        float_schema = _artifact(self.policy)
        float_schema["schema_version"] = 1.0
        cases.append(float_schema)

        wrong_hash = _artifact(self.policy)
        wrong_hash["hash"] = "sha256-utf8-prompt"
        cases.append(wrong_hash)

        wrong_models = _artifact(self.policy)
        wrong_models["model_ids"] = list(reversed(MODEL_IDS))
        cases.append(wrong_models)

        unsorted = _artifact(self.policy)
        unsorted["rows"].reverse()
        cases.append(unsorted)

        duplicate = _artifact(self.policy)
        duplicate["rows"].insert(1, copy.deepcopy(duplicate["rows"][0]))
        cases.append(duplicate)

        uppercase_digest = _artifact(self.policy)
        uppercase_digest["rows"][0][0] = "A" * 64
        cases.append(uppercase_digest)

        for invalid_cost in ("0", "1e-3", 0.1, "00.1", "-1"):
            invalid = _artifact(self.policy)
            invalid["rows"][0][1] = invalid_cost
            cases.append(invalid)

        empty = _artifact(self.policy)
        empty["rows"] = []
        cases.append(empty)

        for index, value in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ProtocolError):
                    parse_public_cost_lookup(value, policy=self.policy)

    def test_bundled_table_is_cost_only_and_matches_public_outcome(self) -> None:
        path = (
            ROOT
            / "src/ossp_router/resources/public-content-costs.v1.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        fitted = parse_public_cost_lookup(raw, policy=self.policy)
        self.assertEqual(2_640, len(fitted.digests))
        self.assertEqual(2_640, raw["training_summary"]["episode_rows"])

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        forbidden = {
            "challenge_id",
            "decision",
            "episode_id",
            "model_score",
            "prompt",
            "score",
            "source",
            "split",
        }
        self.assertTrue(forbidden.isdisjoint(set(keys(raw))))

        inputs = load_input(ROOT / "data/train/inputs-base.json")
        outcomes = load_outcomes(ROOT / "data/train/outcomes.json")
        episode = inputs.episodes[0]
        assert episode.prompt is not None
        by_model = {
            row.model_id: row
            for row in outcomes.outcomes
            if row.episode_id == episode.episode_id
        }
        unit = Decimal(self.policy.token_unit)
        expected = tuple(
            self.policy.models[model_id].fixed_cost
            + Decimal(by_model[model_id].input_tokens)
            * self.policy.models[model_id].input_token_rate
            / unit
            + Decimal(by_model[model_id].output_tokens)
            * self.policy.models[model_id].output_token_rate
            / unit
            for model_id in MODEL_IDS
        )
        self.assertEqual(expected, fitted.costs_for_text(episode.prompt))


if __name__ == "__main__":
    unittest.main()
