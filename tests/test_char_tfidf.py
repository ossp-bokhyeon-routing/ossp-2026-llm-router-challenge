# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError
import math
import unittest

from ossp_router.char_tfidf import (
    CharTfidfArtifact,
    CharTfidfRuntime,
    artifact_from_dict,
    cap_character_text,
    predict_char_tfidf,
    validate_artifact,
)


# Generated once with scikit-learn 1.7.0 from this fixed training fixture:
#
#   corpus = ["ABC", "abcd", "A  B", "x\t\ny", "zzzzz"]
#   targets = [[1, -1], [2, .5], [-.5, 2], [1.5, -.25], [.25, 1.25]]
#   vectorizer = TfidfVectorizer(
#       analyzer="char", ngram_range=(3, 5), lowercase=True,
#       sublinear_tf=True, norm="l2", smooth_idf=True, use_idf=True,
#   )
#   ridge = Ridge(alpha=.75).fit(vectorizer.fit_transform(corpus), targets)
#
# Values are embedded so this test module and the inference path require only
# the Python standard library, including under ``python -S``.
SKLEARN_ARTIFACT = CharTfidfArtifact(
    vocabulary=(
        ("a b", 0),
        ("abc", 1),
        ("abcd", 2),
        ("bcd", 3),
        ("x y", 4),
        ("zzz", 5),
        ("zzzz", 6),
        ("zzzzz", 7),
    ),
    idf=(
        2.09861228866811,
        1.6931471805599454,
        2.09861228866811,
        2.09861228866811,
        2.09861228866811,
        2.09861228866811,
        2.09861228866811,
        2.09861228866811,
    ),
    coefficients=(
        (
            -0.7354689347437688,
            0.27374206996002654,
            0.43979728164483917,
            0.43979728164483917,
            0.4073882081133741,
            -0.22394874702314327,
            -0.18068043900229608,
            -0.1067127778829852,
        ),
        (
            0.8156509686603926,
            -0.8494662933645429,
            0.1422004015555534,
            0.1422004015555534,
            -0.47006331705389315,
            0.2824590498709983,
            0.2278861829291208,
            0.1345932506905607,
        ),
    ),
    intercepts=(0.7870706358015954, 0.572610804844313),
)


class CharTfidfRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.runtime = CharTfidfRuntime(SKLEARN_ARTIFACT)

    def assertVectorClose(self, actual, expected, tolerance=2e-12):
        self.assertEqual(len(actual), len(expected))
        for index, (left, right) in enumerate(zip(actual, expected)):
            self.assertAlmostEqual(
                left,
                right,
                delta=tolerance,
                msg=f"vector element {index} differs",
            )

    def test_transform_and_multioutput_prediction_match_sklearn_fixture(self):
        expected_features = {
            1: 0.49552379079705033,
            2: 0.6141889663426562,
            3: 0.6141889663426562,
        }
        actual_features = dict(self.runtime.transform("ABCD"))
        self.assertEqual(actual_features.keys(), expected_features.keys())
        for index, expected in expected_features.items():
            self.assertAlmostEqual(
                actual_features[index], expected, delta=2e-12
            )
        self.assertVectorClose(
            self.runtime.predict("ABCD"),
            (1.4629536196363269, 0.3263558822918275),
        )

    def test_lowercase_matches_sklearn_preprocessor(self):
        self.assertEqual(
            self.runtime.transform("ABCD"),
            self.runtime.transform("abcd"),
        )
        self.assertEqual(
            self.runtime.predict("AbCd"),
            self.runtime.predict("abcd"),
        )

    def test_whitespace_runs_but_not_single_whitespace_are_normalized(self):
        self.assertEqual(self.runtime.transform("A   B"), ((0, 1.0),))
        self.assertEqual(self.runtime.transform("A \t\nB"), ((0, 1.0),))
        self.assertEqual(self.runtime.transform("x\t\ny"), ((4, 1.0),))

        # sklearn uses ``re.compile(r"\s\s+")``.  A single tab is therefore
        # retained, not converted into the ASCII space present in "a b".
        self.assertEqual(self.runtime.transform("A\tB"), ())
        self.assertVectorClose(
            self.runtime.predict("A\tB"),
            SKLEARN_ARTIFACT.intercepts,
        )

    def test_sublinear_tf_and_l2_norm_match_repeated_ngram_fixture(self):
        expected_features = {
            5: 0.7717364111651887,
            6: 0.5475700525422904,
            7: 0.3234036939193922,
        }
        actual_features = dict(self.runtime.transform("ZZZZZ zzz"))
        self.assertEqual(actual_features.keys(), expected_features.keys())
        for index, expected in expected_features.items():
            self.assertAlmostEqual(
                actual_features[index], expected, delta=2e-12
            )
        self.assertVectorClose(
            self.runtime.predict("ZZZZZ zzz"),
            (0.48079472945540574, 0.9589063419029931),
        )

    def test_unknown_ngrams_produce_only_the_ridge_intercepts(self):
        self.assertEqual(self.runtime.transform("?????"), ())
        self.assertEqual(
            self.runtime.predict("?????"),
            SKLEARN_ARTIFACT.intercepts,
        )

    def test_large_prompt_keeps_only_sparse_known_term_counts(self):
        features = self.runtime.transform("z" * 20_000)
        self.assertEqual({index for index, _value in features}, {5, 6, 7})
        self.assertTrue(
            math.isclose(
                math.fsum(value * value for _index, value in features),
                1.0,
                abs_tol=2e-12,
            )
        )

    def test_prediction_is_deterministic(self):
        expected_transform = self.runtime.transform("ABCD A   B zzzzz")
        expected_prediction = self.runtime.predict("ABCD A   B zzzzz")
        for _iteration in range(20):
            self.assertEqual(
                self.runtime.transform("ABCD A   B zzzzz"),
                expected_transform,
            )
            self.assertEqual(
                self.runtime.predict("ABCD A   B zzzzz"),
                expected_prediction,
            )
        self.assertEqual(
            predict_char_tfidf(SKLEARN_ARTIFACT, "ABCD A   B zzzzz"),
            expected_prediction,
        )

    def test_json_style_payload_is_frozen_and_validated(self):
        payload = {
            "vocabulary": {"abc": 0},
            "idf": [1.25],
            "coefficients": [[2.0], [-1.0]],
            "intercepts": [0.5, 3.0],
        }
        artifact = artifact_from_dict(payload)
        validate_artifact(artifact)
        self.assertEqual(artifact.vocabulary, (("abc", 0),))
        self.assertEqual(artifact.idf, (1.25,))
        self.assertEqual(artifact.coefficients, ((2.0,), (-1.0,)))
        with self.assertRaises(FrozenInstanceError):
            artifact.idf = (9.0,)

    def test_duplicate_vocabulary_terms_and_indices_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate vocabulary term"):
            CharTfidfArtifact(
                vocabulary=(("abc", 0), ("abc", 1)),
                idf=(1.0, 1.0),
                coefficients=((0.0, 0.0),),
                intercepts=(0.0,),
            )
        with self.assertRaisesRegex(ValueError, "duplicate vocabulary index"):
            CharTfidfArtifact(
                vocabulary=(("abc", 0), ("bcd", 0)),
                idf=(1.0, 1.0),
                coefficients=((0.0, 0.0),),
                intercepts=(0.0,),
            )

    def test_invalid_shapes_and_nonfinite_values_are_rejected(self):
        cases = (
            {
                "vocabulary": (("abc", 0),),
                "idf": (),
                "coefficients": ((0.0,),),
                "intercepts": (0.0,),
            },
            {
                "vocabulary": (("abc", 0),),
                "idf": (1.0,),
                "coefficients": ((0.0, 1.0),),
                "intercepts": (0.0,),
            },
            {
                "vocabulary": (("abc", 0),),
                "idf": (1.0,),
                "coefficients": ((0.0,),),
                "intercepts": (0.0, 1.0),
            },
            {
                "vocabulary": (("abc", 0),),
                "idf": (float("nan"),),
                "coefficients": ((0.0,),),
                "intercepts": (0.0,),
            },
            {
                "vocabulary": (("abc", 0),),
                "idf": (1.0,),
                "coefficients": ((float("inf"),),),
                "intercepts": (0.0,),
            },
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    CharTfidfArtifact(**case)

    def test_non_string_input_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "text must be a string"):
            self.runtime.predict(None)

    def test_character_cap_keeps_deterministic_head_and_tail(self):
        text = "abcdefghijklmno"
        self.assertEqual(cap_character_text(text, 8), "abc\nlmno")
        self.assertEqual(len(cap_character_text(text, 8)), 8)
        self.assertEqual(cap_character_text(text, len(text)), text)
        self.assertEqual(cap_character_text(text, 0), text)
        self.assertEqual(cap_character_text(text, None), text)

    def test_character_cap_rejects_invalid_limits(self):
        for value in (True, 1.5, "8"):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    cap_character_text("abcdefgh", value)
        with self.assertRaises(ValueError):
            cap_character_text("abcdefgh", 4)

    def test_selected_prediction_matches_full_rows(self):
        text = "Alpha beta alpha."
        full = self.runtime.predict(text)
        self.assertEqual(
            (full[1], full[0]),
            self.runtime.predict_selected(text, (1, 0)),
        )
        self.assertEqual((), self.runtime.predict_selected(text, ()))
        for indices in ((-1,), (len(full),), (True,)):
            with self.subTest(indices=indices):
                with self.assertRaises(ValueError):
                    self.runtime.predict_selected(text, indices)


if __name__ == "__main__":
    unittest.main()
