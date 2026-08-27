# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
import unittest

import numpy as np

from baselines.validate_bert_router import _rerouted_bootstrap


class BertRouterValidatorTest(unittest.TestCase):
    def test_vectorized_bootstrap_matches_runtime_beyond_2_pow_40(self) -> None:
        scores = np.asarray(((0.0, 2.0, -100.0),))
        costs = np.asarray(((1.0, 1.0 + 1e-12, 2.0),))
        takes = np.zeros((5, 1), dtype=np.int32)

        report = _rerouted_bootstrap(
            scores,
            costs,
            scores,
            costs,
            1.0,
            takes,
            chunk_size=2,
        )

        self.assertEqual([1.0, 0.0, 0.0], report["mean_counts"])
        self.assertEqual([1.0, 1.0, 1.0], report["cost_q95_q99_max"])
        self.assertEqual(4, report["selector_parity_checks"])

    def test_vectorized_bootstrap_uses_runtime_fsum_at_budget_edge(self) -> None:
        rng = np.random.default_rng(7)
        for _trial in range(9):
            light = np.exp(rng.uniform(-45.0, 45.0, 257))
            ax = light * (1.0 + rng.uniform(1e-12, 3.0, 257))
        runtime_ratio = math.fsum(map(float, ax)) / math.fsum(
            map(float, light)
        )
        numpy_ratio = float(ax.sum() / light.sum())
        cap = float((runtime_ratio + numpy_ratio) / 2.0)
        scores = np.column_stack(
            (
                np.zeros(257),
                np.full(257, 1e6),
                np.full(257, -1e6),
            )
        )
        costs = np.column_stack((light, ax, ax * (1.0 + 1e-12)))

        report = _rerouted_bootstrap(
            scores,
            costs,
            scores,
            costs,
            cap,
            np.arange(257, dtype=np.int32)[None, :],
        )

        self.assertEqual([1.0, 256.0, 0.0], report["mean_counts"])
        self.assertEqual(1, report["selector_parity_checks"])


if __name__ == "__main__":
    unittest.main()
