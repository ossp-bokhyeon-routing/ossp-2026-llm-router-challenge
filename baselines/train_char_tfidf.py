# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Train the character TF-IDF heads used by the BERT-style hybrid router."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import scipy
from scipy import sparse
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from ossp_router.heuristic import episode_text
from ossp_router.char_tfidf import cap_character_text
from ossp_router.protocol import (
    MODEL_IDS,
    InputBatch,
    OutcomeBatch,
    RoutingPolicy,
    load_bundled_policy,
    load_input,
    load_outcomes,
    policy_sha256,
)


ARTIFACT_TYPE = "ossp-char-tfidf-ridge-v1"
FEATURE_VERSION = 1
MAX_FEATURES = 60_000
SCORE_ALPHAS = (0.3, 3.0)
COST_ALPHA = 1.0
_TOKEN = re.compile(r"[A-Za-z]+|[가-힣]+|\d+|[^\w\s]", re.UNICODE)
_PATTERN_SOURCES = (
    r"```|\b(?:def|class|function|return|import|SELECT|FROM|Traceback)\b",
    r"[=+*/^<>≤≥∑∫√]|\\(?:frac|sum|sqrt|begin)",
    r"\b(?:prove|derive|theorem|lemma|proof|증명|유도|정리|귀납)\b",
    r"\b(?:exactly|at least|at most|must|only|without|정확히|이상|이하|반드시|오직)\b",
    r"\b(?:summari[sz]e|rewrite|translate|extract|요약|바꾸|번역|추출)\b",
    r"(?:^|\n)\s*(?:[A-E][.)]|[1-5][.)]|①|②|③|④|⑤)",
    r"\b(?:answer|solution|reasoning|explain|calculate|solve|정답|해설|풀이|설명|계산)\b",
    r"\b(?:json|xml|yaml|csv|markdown|format|형식)\b",
)
_PATTERNS = tuple(re.compile(source, re.IGNORECASE | re.MULTILINE) for source in _PATTERN_SOURCES)
DENSE_FEATURE_NAMES = (
    "log_character_count",
    "log_word_count",
    "log_token_count",
    "log_newline_count",
    "log_period_count",
    "hangul_ratio",
    "digit_ratio",
    "uppercase_ratio",
    "symbol_ratio",
    "context_over_2k",
    "context_over_8k",
    "log_code_pattern_count",
    "log_math_pattern_count",
    "log_formal_reasoning_count",
    "log_multi_constraint_count",
    "log_simple_transform_count",
    "log_multiple_choice_count",
    "log_solution_request_count",
    "log_format_request_count",
)


def dense_feature_vector(text: str) -> tuple[float, ...]:
    length = max(1, len(text))
    words = re.findall(r"\w+", text, re.UNICODE)
    tokens = _TOKEN.findall(text)
    return (
        math.log1p(len(text)),
        math.log1p(len(words)),
        math.log1p(len(tokens)),
        math.log1p(text.count("\n")),
        math.log1p(text.count(".")),
        sum("가" <= character <= "힣" for character in text) / length,
        sum(character.isdigit() for character in text) / length,
        sum(character.isupper() for character in text) / length,
        sum(
            not character.isalnum() and not character.isspace()
            for character in text
        )
        / length,
        float(len(text) > 2_000),
        float(len(text) > 8_000),
        *(math.log1p(len(pattern.findall(text))) for pattern in _PATTERNS),
    )


def _file_sha256(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def _subset_outcomes(inputs: InputBatch, outcomes: OutcomeBatch) -> OutcomeBatch:
    episode_ids = {episode.episode_id for episode in inputs.episodes}
    rows = tuple(
        outcome for outcome in outcomes.outcomes if outcome.episode_id in episode_ids
    )
    if len(rows) != len(inputs.episodes) * len(MODEL_IDS):
        raise ValueError("입력에 대응하는 공개 outcome 행렬이 완전하지 않습니다.")
    return OutcomeBatch(
        outcomes.schema_version,
        outcomes.challenge_id,
        outcomes.split,
        rows,
    )


def _cost(outcome: Any, policy: RoutingPolicy) -> float:
    rates = policy.models[outcome.model_id]
    unit = Decimal(policy.token_unit)
    return float(
        rates.fixed_cost
        + Decimal(outcome.input_tokens) * rates.input_token_rate / unit
        + Decimal(outcome.output_tokens) * rates.output_token_rate / unit
    )


def _training_targets(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
) -> tuple[np.ndarray, np.ndarray]:
    by_key = {
        (outcome.episode_id, outcome.model_id): outcome
        for outcome in outcomes.outcomes
    }
    scores = []
    log_costs = []
    for episode in inputs.episodes:
        rows = [by_key[(episode.episode_id, model_id)] for model_id in MODEL_IDS]
        scores.append([float(row.score) for row in rows])
        log_costs.append([math.log(_cost(row, policy)) for row in rows])
    return np.asarray(scores, dtype=np.float64), np.asarray(log_costs, dtype=np.float64)


def _fit_head(
    matrix: Any,
    targets: np.ndarray,
    alpha: float,
) -> Ridge:
    model = Ridge(
        alpha=alpha,
        solver="lsqr",
        fit_intercept=True,
        tol=1e-5,
    )
    model.fit(matrix, targets)
    return model


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    content = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
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


def train(
    *,
    input_path: Path,
    outcomes_path: Path,
    artifact_path: Path,
    report_path: Path,
    policy: RoutingPolicy,
    max_features: int = MAX_FEATURES,
    max_characters: int = 0,
) -> Mapping[str, Any]:
    inputs = load_input(input_path)
    outcomes = _subset_outcomes(inputs, load_outcomes(outcomes_path))
    texts = [episode_text(episode) for episode in inputs.episodes]
    char_texts = [cap_character_text(text, max_characters) for text in texts]
    scores, log_costs = _training_targets(inputs, outcomes, policy)

    vectorizer = TfidfVectorizer(
        analyzer="char",
        ngram_range=(3, 5),
        min_df=2,
        max_features=max_features,
        sublinear_tf=True,
        lowercase=True,
        dtype=np.float64,
    )
    char_matrix = vectorizer.fit_transform(char_texts)
    scaler = StandardScaler()
    dense_matrix = scaler.fit_transform(
        np.asarray([dense_feature_vector(text) for text in texts], dtype=np.float64)
    )
    matrix = sparse.hstack(
        (char_matrix, sparse.csr_matrix(dense_matrix)),
        format="csr",
    )
    fitted = (
        ("score_alpha_0_3", _fit_head(matrix, scores, SCORE_ALPHAS[0])),
        ("score_alpha_3", _fit_head(matrix, scores, SCORE_ALPHAS[1])),
        ("log_cost_alpha_1", _fit_head(matrix, log_costs, COST_ALPHA)),
    )
    char_dimension = char_matrix.shape[1]
    head_names = []
    char_coefficients = []
    dense_coefficients = []
    intercepts = []
    train_mse = {}
    for head_name, model in fitted:
        predictions = model.predict(matrix)
        targets = log_costs if head_name.startswith("log_cost") else scores
        train_mse[head_name] = float(np.mean((predictions - targets) ** 2))
        for model_index, model_id in enumerate(MODEL_IDS):
            head_names.append(f"{head_name}:{model_id}")
            char_coefficients.append(
                [float(value) for value in model.coef_[model_index, :char_dimension]]
            )
            dense_coefficients.append(
                [float(value) for value in model.coef_[model_index, char_dimension:]]
            )
            intercepts.append(float(model.intercept_[model_index]))

    vocabulary = sorted(vectorizer.vocabulary_.items(), key=lambda item: item[1])
    training_summary = {
        "num_episodes": len(inputs.episodes),
        "input_sha256": _file_sha256(input_path),
        "outcomes_sha256": _file_sha256(outcomes_path),
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
        "sklearn_version": sklearn.__version__,
        "optimizer": "sklearn-ridge-lsqr-char-tfidf-v1",
        "max_characters": max_characters,
        "character_view": "head-tail-newline-v1" if max_characters else "full",
    }
    artifact = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": 1,
        "feature_version": FEATURE_VERSION,
        "model_ids": list(MODEL_IDS),
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "head_names": head_names,
        "tfidf": {
            "analyzer": "char",
            "ngram_range": [3, 5],
            "min_df": 2,
            "max_features": max_features,
            "lowercase": True,
            "sublinear_tf": True,
            "norm": "l2",
            "smooth_idf": True,
            "use_idf": True,
            "max_characters": max_characters,
            "character_view": "head-tail-newline-v1" if max_characters else "full",
            "vocabulary": [[term, int(index)] for term, index in vocabulary],
            "idf": [float(value) for value in vectorizer.idf_],
            "coefficients": char_coefficients,
            "intercepts": intercepts,
        },
        "dense": {
            "feature_names": list(DENSE_FEATURE_NAMES),
            "mean": [float(value) for value in scaler.mean_],
            "scale": [float(value) for value in scaler.scale_],
            "coefficients": dense_coefficients,
        },
        "training_summary": training_summary,
    }
    _atomic_json(artifact_path, artifact)
    report = {
        "report_type": "ossp-char-tfidf-training-v1",
        "artifact_type": ARTIFACT_TYPE,
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "training_summary": training_summary,
        "char_dimension": char_dimension,
        "dense_dimension": len(DENSE_FEATURE_NAMES),
        "head_names": head_names,
        "train_mse": train_mse,
        "artifact_sha256": _file_sha256(artifact_path),
        "artifact_bytes": artifact_path.stat().st_size,
    }
    _atomic_json(report_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-features", type=int, default=MAX_FEATURES)
    parser.add_argument(
        "--max-characters",
        type=int,
        default=0,
        help="Head+tail character cap; 0 keeps the full text.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = train(
        input_path=args.input,
        outcomes_path=args.outcomes,
        artifact_path=args.artifact,
        report_path=args.report,
        policy=load_bundled_policy(),
        max_features=args.max_features,
        max_characters=args.max_characters,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
