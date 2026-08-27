# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Standard-library inference for the full-token word TF-IDF score heads."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import sys
import zlib
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from .protocol import MODEL_IDS, ProtocolError, RoutingPolicy, policy_sha256


ARTIFACT_TYPE = "ossp-word-tfidf-score-delta-v1"
FEATURE_VERSION = 1
TOKEN_PATTERN = r"(?u)\b\w+\b|[^\w\s]"
_TOKEN = re.compile(TOKEN_PATTERN)
UPGRADE_MODEL_IDS = MODEL_IDS[1:]
SCORE_ALPHA_NAMES = ("0.1", "1", "3", "10", "30")
HEAD_NAMES = tuple(
    f"score_delta_alpha_{alpha}:{model_id}"
    for alpha in SCORE_ALPHA_NAMES
    for model_id in UPGRADE_MODEL_IDS
)


@dataclass(frozen=True)
class WordTfidfArtifact:
    """Validated compact vocabulary, IDF weights, and delta-score heads."""

    vocabulary: Tuple[str, ...]
    idf: memoryview
    coefficients: memoryview
    dense_mean: Tuple[float, ...]
    dense_scale: Tuple[float, ...]
    dense_feature_names: Tuple[str, ...]
    dense_coefficients: Tuple[Tuple[float, ...], ...]
    intercepts: Tuple[float, ...]
    head_names: Tuple[str, ...]
    policy_id: str
    policy_sha256: str
    training_summary: Mapping[str, Any]

    @property
    def dimension(self) -> int:
        return len(self.vocabulary)

    @property
    def num_heads(self) -> int:
        return len(self.head_names)


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    missing = sorted(set(expected) - set(value))
    extra = sorted(set(value) - set(expected))
    if missing or extra:
        raise ProtocolError(
            f"{label} fields do not match: missing={missing}, extra={extra}"
        )


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolError(f"{label} is outside the permitted range")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError(f"{label} must be a finite number")
    return result


def _vector(value: Any, length: int, label: str) -> Tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ProtocolError(f"{label} must have length {length}")
    return tuple(_number(item, f"{label}[{index}]") for index, item in enumerate(value))


def _matrix(value: Any, rows: int, columns: int, label: str) -> Tuple[Tuple[float, ...], ...]:
    if not isinstance(value, list) or len(value) != rows:
        raise ProtocolError(f"{label} must have {rows} rows")
    return tuple(
        _vector(row, columns, f"{label}[{index}]")
        for index, row in enumerate(value)
    )


def _decode_float32(value: Any, count: int, label: str) -> memoryview:
    if not isinstance(value, str):
        raise ProtocolError(f"{label} must be base64 text")
    try:
        compressed = base64.b64decode(value.encode("ascii"), validate=True)
        expected_bytes = count * 4
        if len(compressed) > expected_bytes + 65_536:
            raise ProtocolError(f"{label} compressed payload is unreasonably large")
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected_bytes + 1)
        if (
            len(raw) > expected_bytes
            or decoder.unconsumed_tail
            or not decoder.eof
            or decoder.unused_data
        ):
            raise ProtocolError(f"{label} compressed payload exceeds its declared size")
    except (UnicodeEncodeError, binascii.Error, zlib.error) as exc:
        raise ProtocolError(f"{label} cannot be decoded: {exc}") from exc
    if len(raw) != expected_bytes:
        raise ProtocolError(
            f"{label} decoded byte length is invalid: {len(raw)} != {expected_bytes}"
        )
    if sys.byteorder != "little":
        from array import array

        swapped = array("f")
        swapped.frombytes(raw)
        swapped.byteswap()
        raw = swapped.tobytes()
    result = memoryview(raw).cast("f").toreadonly()
    if len(result) != count or not all(math.isfinite(item) for item in result):
        raise ProtocolError(f"{label} contains invalid float32 values")
    return result


def parse_word_tfidf_artifact(
    value: Any,
    *,
    policy: Optional[RoutingPolicy] = None,
    expected_dense_feature_names: Optional[Sequence[str]] = None,
) -> WordTfidfArtifact:
    """Strictly validate and decode a compact word TF-IDF artifact."""

    root = _object(value, "word artifact")
    _exact_keys(
        root,
        (
            "artifact_type",
            "dense",
            "feature_version",
            "head_names",
            "model_ids",
            "policy_id",
            "policy_sha256",
            "schema_version",
            "tfidf",
            "training_summary",
        ),
        "word artifact",
    )
    if root["artifact_type"] != ARTIFACT_TYPE:
        raise ProtocolError("unsupported word artifact_type")
    _integer(root["schema_version"], "word schema_version", 1, 1)
    _integer(root["feature_version"], "word feature_version", 1, 1)
    if root["model_ids"] != list(MODEL_IDS):
        raise ProtocolError("word artifact model_ids are invalid")
    if root["head_names"] != list(HEAD_NAMES):
        raise ProtocolError("word artifact head_names are invalid")
    policy_id = root["policy_id"]
    policy_digest = root["policy_sha256"]
    if not isinstance(policy_id, str) or not policy_id:
        raise ProtocolError("word policy_id is invalid")
    if not isinstance(policy_digest, str) or re.fullmatch(r"[0-9a-f]{64}", policy_digest) is None:
        raise ProtocolError("word policy_sha256 is invalid")
    if policy is not None and (
        policy_id != policy.policy_id or policy_digest != policy_sha256(policy)
    ):
        raise ProtocolError("word artifact policy binding is invalid")

    tfidf = _object(root["tfidf"], "word artifact.tfidf")
    _exact_keys(
        tfidf,
        (
            "coefficient_encoding",
            "coefficients",
            "dimension",
            "idf",
            "lowercase",
            "min_df",
            "ngram_range",
            "norm",
            "smooth_idf",
            "sublinear_tf",
            "token_pattern",
            "use_idf",
            "vocabulary",
        ),
        "word artifact.tfidf",
    )
    dimension = _integer(tfidf["dimension"], "word dimension", 1, 1_000_000)
    vocabulary_value = tfidf["vocabulary"]
    if not isinstance(vocabulary_value, list) or len(vocabulary_value) != dimension:
        raise ProtocolError("word vocabulary length is invalid")
    vocabulary = tuple(vocabulary_value)
    if any(not isinstance(term, str) or not term for term in vocabulary):
        raise ProtocolError("word vocabulary terms must be nonempty strings")
    if len(set(vocabulary)) != dimension:
        raise ProtocolError("word vocabulary terms must be unique")
    if tfidf["token_pattern"] != TOKEN_PATTERN:
        raise ProtocolError("word token_pattern is invalid")
    if tfidf["ngram_range"] != [1, 2]:
        raise ProtocolError("word ngram_range is invalid")
    if (
        tfidf["lowercase"] is not True
        or tfidf["sublinear_tf"] is not True
        or tfidf["smooth_idf"] is not True
        or tfidf["use_idf"] is not True
    ):
        raise ProtocolError("word TF-IDF boolean options are invalid")
    if tfidf["norm"] != "l2" or tfidf["coefficient_encoding"] != "f32le-zlib-base64-v1":
        raise ProtocolError("word TF-IDF encoding options are invalid")
    _integer(tfidf["min_df"], "word min_df", 2, 2)
    idf = _decode_float32(tfidf["idf"], dimension, "word idf")
    coefficients = _decode_float32(
        tfidf["coefficients"], len(HEAD_NAMES) * dimension, "word coefficients"
    )
    if any(item <= 0.0 for item in idf):
        raise ProtocolError("word idf values must be positive")

    dense = _object(root["dense"], "word artifact.dense")
    _exact_keys(
        dense,
        ("coefficients", "dimension", "feature_names", "intercepts", "mean", "scale"),
        "word artifact.dense",
    )
    dense_dimension = _integer(dense["dimension"], "word dense dimension", 1, 256)
    feature_names = dense["feature_names"]
    if not isinstance(feature_names, list) or len(feature_names) != dense_dimension:
        raise ProtocolError("word dense feature_names are invalid")
    if any(not isinstance(name, str) or not name for name in feature_names):
        raise ProtocolError("word dense feature names must be nonempty strings")
    dense_feature_names = tuple(feature_names)
    if (
        expected_dense_feature_names is not None
        and dense_feature_names != tuple(expected_dense_feature_names)
    ):
        raise ProtocolError("word dense feature definition differs from the runtime")
    mean = _vector(dense["mean"], dense_dimension, "word dense mean")
    scale = _vector(dense["scale"], dense_dimension, "word dense scale")
    if any(item <= 0.0 for item in scale):
        raise ProtocolError("word dense scales must be positive")
    dense_coefficients = _matrix(
        dense["coefficients"], len(HEAD_NAMES), dense_dimension, "word dense coefficients"
    )
    intercepts = _vector(dense["intercepts"], len(HEAD_NAMES), "word intercepts")
    training_summary = _object(root["training_summary"], "word training_summary")
    return WordTfidfArtifact(
        vocabulary=vocabulary,
        idf=idf,
        coefficients=coefficients,
        dense_mean=mean,
        dense_scale=scale,
        dense_feature_names=dense_feature_names,
        dense_coefficients=dense_coefficients,
        intercepts=intercepts,
        head_names=HEAD_NAMES,
        policy_id=policy_id,
        policy_sha256=policy_digest,
        training_summary=dict(training_summary),
    )


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate word artifact JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"word artifact constant is invalid: {value}")


def load_word_tfidf_artifact(
    text: str,
    *,
    policy: Optional[RoutingPolicy] = None,
    expected_dense_feature_names: Optional[Sequence[str]] = None,
) -> WordTfidfArtifact:
    """Decode an artifact JSON string while rejecting nonstandard constants."""

    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProtocolError(f"word artifact JSON is invalid: {exc}") from exc
    return parse_word_tfidf_artifact(
        value,
        policy=policy,
        expected_dense_feature_names=expected_dense_feature_names,
    )


class WordTfidfRuntime:
    """Sparse full-token unigram/bigram TF-IDF inference for selected heads."""

    def __init__(self, artifact: WordTfidfArtifact) -> None:
        self.artifact = artifact
        self.vocabulary = {
            term: index for index, term in enumerate(artifact.vocabulary)
        }
        self.head_index = {
            name: index for index, name in enumerate(artifact.head_names)
        }

    def _values(self, text: str) -> Tuple[Tuple[int, float], ...]:
        counts: dict[int, int] = {}
        previous: Optional[str] = None
        for match in _TOKEN.finditer(text.lower()):
            token = match.group(0)
            unigram = self.vocabulary.get(token)
            if unigram is not None:
                counts[unigram] = counts.get(unigram, 0) + 1
            if previous is not None:
                bigram = self.vocabulary.get(previous + " " + token)
                if bigram is not None:
                    counts[bigram] = counts.get(bigram, 0) + 1
            previous = token
        if not counts:
            return ()
        unnormalized = tuple(
            (
                index,
                (1.0 + math.log(count)) * float(self.artifact.idf[index]),
            )
            for index, count in counts.items()
        )
        norm = math.sqrt(math.fsum(value * value for _index, value in unnormalized))
        if not math.isfinite(norm) or norm <= 0.0:
            return ()
        return tuple((index, value / norm) for index, value in unnormalized)

    def predict_selected(
        self,
        text: str,
        dense_features: Sequence[float],
        head_names: Sequence[str],
    ) -> Tuple[float, ...]:
        if len(dense_features) != len(self.artifact.dense_mean):
            raise ValueError("word dense feature length is invalid")
        try:
            indices = tuple(self.head_index[name] for name in head_names)
        except KeyError as exc:
            raise ValueError(f"unknown word TF-IDF head: {exc.args[0]}") from exc
        standardized = tuple(
            (float(value) - mean) / scale
            for value, mean, scale in zip(
                dense_features,
                self.artifact.dense_mean,
                self.artifact.dense_scale,
            )
        )
        values = self._values(text)
        dimension = self.artifact.dimension
        result = []
        for head in indices:
            offset = head * dimension
            prediction = self.artifact.intercepts[head]
            prediction += math.fsum(
                coefficient * value
                for coefficient, value in zip(
                    self.artifact.dense_coefficients[head], standardized
                )
            )
            prediction += math.fsum(
                float(self.artifact.coefficients[offset + feature]) * value
                for feature, value in values
            )
            if not math.isfinite(prediction):
                raise ArithmeticError("word TF-IDF prediction is non-finite")
            result.append(prediction)
        return tuple(result)


__all__ = (
    "ARTIFACT_TYPE",
    "FEATURE_VERSION",
    "HEAD_NAMES",
    "SCORE_ALPHA_NAMES",
    "TOKEN_PATTERN",
    "WordTfidfArtifact",
    "WordTfidfRuntime",
    "load_word_tfidf_artifact",
    "parse_word_tfidf_artifact",
)
