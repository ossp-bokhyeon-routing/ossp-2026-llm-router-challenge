# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Pure-Python inference for a fitted character TF-IDF ridge model.

The feature transform mirrors this scikit-learn configuration::

    TfidfVectorizer(
        analyzer="char",
        ngram_range=(3, 5),
        lowercase=True,
        sublinear_tf=True,
        norm="l2",
        smooth_idf=True,
        use_idf=True,
    )

Only fitted values are needed at runtime.  ``coefficients`` use scikit-learn's
multi-output ``Ridge.coef_`` layout: one ``n_features`` row per output.  The
implementation deliberately avoids NumPy and scikit-learn so the exported
artifact can run in the constrained participant image.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
from typing import Any, Dict, Optional, Sequence, Tuple


Vector = Tuple[float, ...]
Matrix = Tuple[Vector, ...]
Vocabulary = Tuple[Tuple[str, int], ...]
SparseVector = Tuple[Tuple[int, float], ...]

_MIN_NGRAM = 3
_MAX_NGRAM = 5

# This is intentionally not ``r"\s+"``.  sklearn's character analyzer only
# replaces a whitespace run when it contains at least two characters; a lone
# tab or newline is retained as-is in its character n-grams.
_WHITE_SPACES = re.compile(r"\s\s+")


def cap_character_text(text: str, max_characters: Optional[int]) -> str:
    """Return a deterministic head/tail view for character n-gram inference.

    ``None`` or ``0`` disables the cap.  When truncation is needed, one
    newline separates the retained head and tail while keeping the returned
    string at exactly ``max_characters`` characters.  Dense length and pattern
    features should continue to use the original, uncapped text.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if max_characters is None or max_characters == 0:
        return text
    if isinstance(max_characters, bool) or not isinstance(max_characters, int):
        raise TypeError("max_characters must be an integer or None")
    if max_characters < _MAX_NGRAM:
        raise ValueError(
            f"max_characters must be at least {_MAX_NGRAM}, 0, or None"
        )
    if len(text) <= max_characters:
        return text
    retained = max_characters - 1
    head_length = retained // 2
    tail_length = retained - head_length
    return text[:head_length] + "\n" + text[-tail_length:]


def _freeze_vector(values: Sequence[float], name: str) -> Vector:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a finite numeric vector")
    try:
        raw_values = tuple(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a finite numeric vector") from exc

    frozen = []
    for index, value in enumerate(raw_values):
        if isinstance(value, bool):
            raise ValueError(f"{name}[{index}] must be a finite number")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}[{index}] must be a finite number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name}[{index}] must be a finite number")
        frozen.append(number)
    return tuple(frozen)


def _freeze_matrix(values: Sequence[Sequence[float]], name: str) -> Matrix:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a finite numeric matrix")
    try:
        rows = tuple(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a finite numeric matrix") from exc
    return tuple(
        _freeze_vector(row, f"{name}[{index}]")
        for index, row in enumerate(rows)
    )


def _freeze_vocabulary(values: Any) -> Vocabulary:
    if isinstance(values, Mapping):
        raw_entries = values.items()
    else:
        if isinstance(values, (str, bytes)):
            raise ValueError("vocabulary must contain (term, index) pairs")
        try:
            raw_entries = iter(values)
        except TypeError as exc:
            raise ValueError(
                "vocabulary must contain (term, index) pairs"
            ) from exc

    entries = []
    for position, entry in enumerate(raw_entries):
        if isinstance(entry, (str, bytes)):
            raise ValueError(
                f"vocabulary[{position}] must be a (term, index) pair"
            )
        try:
            term, index = entry
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"vocabulary[{position}] must be a (term, index) pair"
            ) from exc
        if not isinstance(term, str):
            raise ValueError(f"vocabulary[{position}] term must be a string")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(
                f"vocabulary[{position}] index must be an integer"
            )
        entries.append((term, index))
    return tuple(entries)


@dataclass(frozen=True)
class CharTfidfArtifact:
    """Immutable, JSON-friendly fitted TF-IDF and ridge parameters.

    ``vocabulary`` accepts either JSON-style ``[[term, index], ...]`` entries
    or a mapping such as ``TfidfVectorizer.vocabulary_``.  Numeric JSON lists
    are frozen into nested tuples during construction.
    """

    vocabulary: Vocabulary
    idf: Vector
    coefficients: Matrix
    intercepts: Vector

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "vocabulary",
            _freeze_vocabulary(self.vocabulary),
        )
        object.__setattr__(self, "idf", _freeze_vector(self.idf, "idf"))
        object.__setattr__(
            self,
            "coefficients",
            _freeze_matrix(self.coefficients, "coefficients"),
        )
        object.__setattr__(
            self,
            "intercepts",
            _freeze_vector(self.intercepts, "intercepts"),
        )
        validate_artifact(self)


def validate_artifact(artifact: CharTfidfArtifact) -> None:
    """Validate vocabulary uniqueness, tensor shapes, and numeric values."""

    if not isinstance(artifact, CharTfidfArtifact):
        raise TypeError("artifact must be a CharTfidfArtifact")

    vocabulary = artifact.vocabulary
    if not vocabulary:
        raise ValueError("vocabulary must not be empty")

    terms = set()
    indices = set()
    for position, (term, index) in enumerate(vocabulary):
        if term in terms:
            raise ValueError(f"duplicate vocabulary term: {term!r}")
        if index in indices:
            raise ValueError(f"duplicate vocabulary index: {index}")
        if len(term) < _MIN_NGRAM or len(term) > _MAX_NGRAM:
            raise ValueError(
                f"vocabulary[{position}] term length must be between "
                f"{_MIN_NGRAM} and {_MAX_NGRAM}"
            )
        terms.add(term)
        indices.add(index)

    feature_count = len(vocabulary)
    if indices != set(range(feature_count)):
        raise ValueError(
            "vocabulary indices must contain every integer from 0 to "
            f"{feature_count - 1} exactly once"
        )
    if len(artifact.idf) != feature_count:
        raise ValueError(
            f"idf must have {feature_count} values, got {len(artifact.idf)}"
        )
    if any(value <= 0.0 for value in artifact.idf):
        raise ValueError("idf values must be positive")

    if not artifact.coefficients:
        raise ValueError("coefficients must contain at least one output row")
    if len(artifact.coefficients) != len(artifact.intercepts):
        raise ValueError(
            "coefficient rows must match the number of intercepts"
        )
    for output, row in enumerate(artifact.coefficients):
        if len(row) != feature_count:
            raise ValueError(
                f"coefficients[{output}] must have {feature_count} values, "
                f"got {len(row)}"
            )


def artifact_from_dict(payload: Mapping[str, Any]) -> CharTfidfArtifact:
    """Build an artifact directly from a decoded JSON object."""

    if not isinstance(payload, Mapping):
        raise TypeError("artifact payload must be a mapping")
    required = ("vocabulary", "idf", "coefficients", "intercepts")
    missing = [name for name in required if name not in payload]
    if missing:
        raise ValueError(
            "artifact payload is missing: " + ", ".join(missing)
        )
    return CharTfidfArtifact(
        vocabulary=payload["vocabulary"],
        idf=payload["idf"],
        coefficients=payload["coefficients"],
        intercepts=payload["intercepts"],
    )


class CharTfidfRuntime:
    """Compiled sparse transformer and multi-output linear prediction head."""

    __slots__ = ("artifact", "_feature_index")

    def __init__(self, artifact: CharTfidfArtifact) -> None:
        validate_artifact(artifact)
        self.artifact = artifact
        self._feature_index = dict(artifact.vocabulary)

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        # build_preprocessor(lowercase=True) runs before sklearn's char analyzer.
        return _WHITE_SPACES.sub(" ", text.lower())

    def _term_counts(self, text: str) -> Dict[int, int]:
        document = self._normalize_text(text)
        document_length = len(document)
        feature_index = self._feature_index
        counts: Dict[int, int] = {}

        # The n-gram range is fixed and small, so this streams over the prompt
        # in O(len(prompt)) time.  It never materializes all prompt n-grams;
        # memory is bounded by the number of fitted vocabulary terms observed.
        maximum = min(_MAX_NGRAM, document_length)
        for size in range(_MIN_NGRAM, maximum + 1):
            stop = document_length - size + 1
            for start in range(stop):
                index = feature_index.get(document[start : start + size])
                if index is not None:
                    counts[index] = counts.get(index, 0) + 1
        return counts

    def transform(self, text: str) -> SparseVector:
        """Return nonzero L2-normalized TF-IDF values as ``(index, value)``.

        Entries retain deterministic first-observation order.  A sparse order
        is immaterial to the ridge dot product and avoids an O(k log k) sort.
        """

        counts = self._term_counts(text)
        if not counts:
            return ()

        weighted = tuple(
            (
                index,
                (1.0 + math.log(count)) * self.artifact.idf[index],
            )
            for index, count in counts.items()
        )
        magnitude = math.sqrt(
            math.fsum(value * value for _index, value in weighted)
        )
        inverse_magnitude = 1.0 / magnitude
        return tuple(
            (index, value * inverse_magnitude)
            for index, value in weighted
        )

    def predict(self, text: str) -> Vector:
        """Predict all fitted ridge targets for one prompt."""

        return self.predict_selected(text, range(len(self.artifact.intercepts)))

    def predict_selected(
        self,
        text: str,
        output_indices: Sequence[int],
    ) -> Vector:
        """Predict selected output rows after one sparse transform."""

        try:
            selected = tuple(output_indices)
        except TypeError as exc:
            raise TypeError("output_indices must be an integer sequence") from exc
        output_count = len(self.artifact.intercepts)
        for position, index in enumerate(selected):
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < output_count
            ):
                raise ValueError(
                    f"output_indices[{position}] is outside the output range"
                )
        features = self.transform(text)
        return tuple(
            self.artifact.intercepts[index]
            + math.fsum(
                self.artifact.coefficients[index][feature_index] * value
                for feature_index, value in features
            )
            for index in selected
        )


def predict_char_tfidf(
    artifact: CharTfidfArtifact,
    text: str,
) -> Vector:
    """Convenience wrapper for one-off prediction.

    Batch callers should construct ``CharTfidfRuntime`` once so the vocabulary
    lookup is reused across prompts.
    """

    return CharTfidfRuntime(artifact).predict(text)


__all__ = [
    "CharTfidfArtifact",
    "CharTfidfRuntime",
    "artifact_from_dict",
    "cap_character_text",
    "predict_char_tfidf",
    "validate_artifact",
]
