# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Train and export compact full-token word TF-IDF score-delta heads."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
import zlib
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import scipy
from scipy import sparse
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from ossp_router.bert_router import DENSE_FEATURE_NAMES, dense_feature_vector
from ossp_router.heuristic import episode_text
from ossp_router.protocol import (
    MODEL_IDS,
    load_bundled_policy,
    load_input,
    load_outcomes,
    policy_sha256,
)
from ossp_router.word_tfidf import (
    ARTIFACT_TYPE,
    FEATURE_VERSION,
    HEAD_NAMES,
    SCORE_ALPHA_NAMES,
    TOKEN_PATTERN,
)


SCORE_ALPHAS = tuple(float(value) for value in SCORE_ALPHA_NAMES)
MAX_FEATURES = 120_000


def _lf_sha256(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    content = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _encode_float32(values: np.ndarray) -> tuple[str, Mapping[str, int]]:
    little_endian = np.ascontiguousarray(values, dtype="<f4")
    raw = little_endian.tobytes(order="C")
    compressed = zlib.compress(raw, level=9)
    return base64.b64encode(compressed).decode("ascii"), {
        "raw_bytes": len(raw),
        "zlib_bytes": len(compressed),
        "base64_bytes": 4 * ((len(compressed) + 2) // 3),
    }


def _training_scores(inputs, outcomes) -> np.ndarray:
    index = {(row.episode_id, row.model_id): row for row in outcomes.outcomes}
    rows = []
    for episode in inputs.episodes:
        try:
            rows.append(
                [float(index[(episode.episode_id, model_id)].score) for model_id in MODEL_IDS]
            )
        except KeyError as exc:
            raise ValueError(f"missing public outcome row: {exc.args[0]}") from exc
    if len(index) != len(inputs.episodes) * len(MODEL_IDS):
        raise ValueError("outcomes do not exactly cover the full materialized Train input")
    return np.asarray(rows, dtype=np.float64)


def train(
    *,
    input_path: Path,
    outcomes_path: Path,
    artifact_path: Path,
    report_path: Path,
    max_features: int,
    command: str,
) -> Mapping[str, Any]:
    policy = load_bundled_policy()
    inputs = load_input(input_path)
    outcomes = load_outcomes(outcomes_path)
    if len(inputs.episodes) != 1760:
        raise ValueError(f"full materialized Train must have 1760 rows, got {len(inputs.episodes)}")
    texts = [episode_text(episode) for episode in inputs.episodes]
    dense_values = np.asarray(
        [dense_feature_vector(text) for text in texts], dtype=np.float64
    )
    targets = _training_scores(inputs, outcomes)

    vectorizer = TfidfVectorizer(
        analyzer="word",
        token_pattern=TOKEN_PATTERN,
        ngram_range=(1, 2),
        min_df=2,
        max_features=max_features,
        sublinear_tf=True,
        lowercase=True,
        dtype=np.float64,
    )
    word_matrix = vectorizer.fit_transform(texts)
    scaler = StandardScaler()
    dense_matrix = scaler.fit_transform(dense_values)
    matrix = sparse.hstack(
        (word_matrix, sparse.csr_matrix(dense_matrix)), format="csr"
    )
    word_dimension = word_matrix.shape[1]

    coefficient_rows = []
    dense_rows = []
    intercepts = []
    train_mse = {}
    for alpha in SCORE_ALPHAS:
        model = Ridge(
            alpha=alpha,
            solver="lsqr",
            fit_intercept=True,
            tol=1e-6,
        )
        model.fit(matrix, targets)
        predictions = model.predict(matrix)
        train_mse[f"score_alpha_{alpha:g}"] = float(
            np.mean((predictions - targets) ** 2)
        )
        for model_index in (1, 2):
            delta = model.coef_[model_index] - model.coef_[0]
            coefficient_rows.append(delta[:word_dimension])
            dense_rows.append(delta[word_dimension:])
            intercepts.append(float(model.intercept_[model_index] - model.intercept_[0]))

    vocabulary = sorted(vectorizer.vocabulary_.items(), key=lambda item: item[1])
    if [index for _term, index in vocabulary] != list(range(word_dimension)):
        raise AssertionError("TfidfVectorizer vocabulary indices are not contiguous")
    idf_text, idf_sizes = _encode_float32(np.asarray(vectorizer.idf_))
    coefficients_text, coefficient_sizes = _encode_float32(
        np.asarray(coefficient_rows)
    )
    training_summary = {
        "num_episodes": len(inputs.episodes),
        "input_sha256_lf_normalized": _lf_sha256(input_path),
        "outcomes_sha256_lf_normalized": _lf_sha256(outcomes_path),
        "policy_sha256": policy_sha256(policy),
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
        "sklearn_version": sklearn.__version__,
        "optimizer": "sklearn-ridge-lsqr-word-tfidf-v1",
        "ridge_solver": "lsqr",
        "ridge_tolerance": 1e-6,
        "score_alphas": list(SCORE_ALPHAS),
        "tfidf": {
            "analyzer": "word",
            "token_pattern": TOKEN_PATTERN,
            "ngram_range": [1, 2],
            "min_df": 2,
            "max_features": max_features,
            "sublinear_tf": True,
            "smooth_idf": True,
            "use_idf": True,
            "lowercase": True,
            "norm": "l2",
            "dtype_during_fit": "float64",
        },
        "export_encoding": "NumPy <f4 row-major -> zlib level 9 -> base64",
        "zlib_build_version": zlib.ZLIB_VERSION,
        "zlib_runtime_version": zlib.ZLIB_RUNTIME_VERSION,
        "transformation_command": command,
        "train_mse": train_mse,
    }
    artifact = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": 1,
        "feature_version": FEATURE_VERSION,
        "model_ids": list(MODEL_IDS),
        "head_names": list(HEAD_NAMES),
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "tfidf": {
            "dimension": word_dimension,
            "vocabulary": [term for term, _index in vocabulary],
            "idf": idf_text,
            "coefficients": coefficients_text,
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
            "dimension": len(DENSE_FEATURE_NAMES),
            "feature_names": list(DENSE_FEATURE_NAMES),
            "mean": [float(value) for value in scaler.mean_],
            "scale": [float(value) for value in scaler.scale_],
            "coefficients": [
                [float(value) for value in row] for row in dense_rows
            ],
            "intercepts": intercepts,
        },
        "training_summary": training_summary,
    }
    _atomic_json(artifact_path, artifact)
    report = {
        "report_type": "ossp-word-tfidf-training-v1",
        "artifact_type": ARTIFACT_TYPE,
        "artifact_path": str(artifact_path),
        "artifact_bytes": artifact_path.stat().st_size,
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "word_dimension": word_dimension,
        "dense_dimension": len(DENSE_FEATURE_NAMES),
        "head_names": list(HEAD_NAMES),
        "matrix_nnz": int(matrix.nnz),
        "idf_encoding_sizes": idf_sizes,
        "coefficient_encoding_sizes": coefficient_sizes,
        "training_summary": training_summary,
    }
    _atomic_json(report_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--train-input",
        type=Path,
        default=Path("data/materialized/train/inputs.json"),
    )
    parser.add_argument(
        "--train-outcomes", type=Path, default=Path("data/train/outcomes.json")
    )
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-features", type=int, default=MAX_FEATURES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_features < 1 or args.max_features > 250_000:
        raise ValueError("max_features must be in [1, 250000]")
    command = (
        "PYTHONPATH=src python -B baselines/train_word_tfidf.py "
        f"--train-input {args.train_input.as_posix()} "
        f"--train-outcomes {args.train_outcomes.as_posix()} "
        f"--artifact {args.artifact.as_posix()} "
        f"--report {args.report.as_posix()} "
        f"--max-features {args.max_features}"
    )
    report = train(
        input_path=args.train_input,
        outcomes_path=args.train_outcomes,
        artifact_path=args.artifact,
        report_path=args.report,
        max_features=args.max_features,
        command=command,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
