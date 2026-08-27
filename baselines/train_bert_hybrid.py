# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Train and evaluate a tiny BERT-style residual router on public data.

This development trainer uses PyTorch, but the eventual routing artifact is
designed for a standard-library-only inference implementation.  It combines a
small bidirectional Transformer encoder with the released hash-regex features
and predictions.  The model learns residuals for three score heads and three
log-cost heads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

import hash_regex
import train_hash_regex
from ossp_router.protocol import (
    MODEL_IDS,
    TIERS,
    InputBatch,
    OutcomeBatch,
    RoutingPolicy,
    load_bundled_policy,
    load_input,
    load_outcomes,
    policy_sha256,
)


DEFAULT_VOCAB_SIZE = 1_024
DEFAULT_SEQUENCE_LENGTH = 32
DEFAULT_HIDDEN_SIZE = 16
DEFAULT_ATTENTION_HEADS = 2
DEFAULT_DENSE_HIDDEN_SIZE = 32
DEFAULT_FUSION_HIDDEN_SIZE = 32
DEFAULT_BATCH_SIZE = 64
TARGET_COST_RATIOS = {
    "fast": 1.20,
    "balanced": 1.85,
    "premium": 3.60,
}


def _file_sha256(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class PreparedSplit:
    inputs: InputBatch
    outcomes: OutcomeBatch
    token_ids: np.ndarray
    token_types: np.ndarray
    token_mask: np.ndarray
    dense_features: np.ndarray
    base_predictions: np.ndarray
    targets: np.ndarray


def _outcome_cost(outcome: Any, policy: RoutingPolicy) -> float:
    rates = policy.models[outcome.model_id]
    unit = Decimal(policy.token_unit)
    value = (
        rates.fixed_cost
        + Decimal(outcome.input_tokens) * rates.input_token_rate / unit
        + Decimal(outcome.output_tokens) * rates.output_token_rate / unit
    )
    return float(value)


def _subset_outcomes(inputs: InputBatch, outcomes: OutcomeBatch) -> OutcomeBatch:
    """Drop source-fetch rows when only inputs-base.json is available."""

    episode_ids = {episode.episode_id for episode in inputs.episodes}
    rows = tuple(
        outcome for outcome in outcomes.outcomes if outcome.episode_id in episode_ids
    )
    expected = len(inputs.episodes) * len(MODEL_IDS)
    if len(rows) != expected:
        raise ValueError(
            f"입력에 대응하는 outcome 행이 완전하지 않습니다: {len(rows)} != {expected}"
        )
    return OutcomeBatch(
        schema_version=outcomes.schema_version,
        challenge_id=outcomes.challenge_id,
        split=outcomes.split,
        outcomes=rows,
    )


def _token_row(
    episode: Any,
    *,
    vocab_size: int,
    sequence_length: int,
) -> tuple[list[int], list[int], list[bool]]:
    if vocab_size < 16:
        raise ValueError("vocab_size는 16 이상이어야 합니다.")
    if sequence_length < 8:
        raise ValueError("sequence_length는 8 이상이어야 합니다.")
    text = hash_regex.episode_text(episode)
    tokens = hash_regex._normalized_tokens(text)
    available = sequence_length - 1
    if len(tokens) <= available:
        sampled = list(tokens)
        types = [0] * len(sampled)
    else:
        head_count = max(1, available // 3)
        tail_count = available - head_count
        sampled = list(tokens[:head_count]) + list(tokens[-tail_count:])
        types = [0] * head_count + [1] * tail_count
    ids = [1]
    ids.extend(
        3 + hash_regex._stable_hash(token) % (vocab_size - 3)
        for token in sampled
    )
    token_types = [0] + types
    mask = [True] * len(ids)
    padding = sequence_length - len(ids)
    ids.extend([0] * padding)
    token_types.extend([0] * padding)
    mask.extend([False] * padding)
    return ids, token_types, mask


def prepare_split(
    *,
    input_path: Path,
    outcomes_path: Path,
    policy: RoutingPolicy,
    base_artifact: hash_regex.HashRegexArtifact,
    vocab_size: int,
    sequence_length: int,
) -> PreparedSplit:
    inputs = load_input(input_path)
    outcomes = _subset_outcomes(inputs, load_outcomes(outcomes_path))
    outcome_index = {
        (outcome.episode_id, outcome.model_id): outcome
        for outcome in outcomes.outcomes
    }
    token_rows = [
        _token_row(
            episode,
            vocab_size=vocab_size,
            sequence_length=sequence_length,
        )
        for episode in inputs.episodes
    ]
    dense_rows = [
        hash_regex.raw_feature_vector(episode, base_artifact.hash_bins)
        for episode in inputs.episodes
    ]
    dense = np.asarray(dense_rows, dtype=np.float32)
    base_rows: list[list[float]] = []
    target_rows: list[list[float]] = []
    for episode, raw_features in zip(inputs.episodes, dense_rows):
        standardized = tuple(
            (value - mean) / scale
            for value, mean, scale in zip(
                raw_features,
                base_artifact.feature_mean,
                base_artifact.feature_scale,
            )
        )
        predicted_scores = {
            model_id: min(
                1.0,
                max(
                    0.0,
                    hash_regex._linear(
                        base_artifact.score_heads[model_id], standardized
                    ),
                ),
            )
            for model_id in MODEL_IDS
        }
        predicted_costs = {
            model_id: math.exp(
                min(
                    50.0,
                    max(
                        -50.0,
                        hash_regex._linear(
                            base_artifact.log_cost_heads[model_id], standardized
                        ),
                    ),
                )
            )
            for model_id in MODEL_IDS
        }
        light = predicted_costs[MODEL_IDS[0]]
        predicted_costs[MODEL_IDS[1]] = max(
            predicted_costs[MODEL_IDS[1]], light * (1 + 1e-12)
        )
        predicted_costs[MODEL_IDS[2]] = max(
            predicted_costs[MODEL_IDS[2]],
            predicted_costs[MODEL_IDS[1]] * (1 + 1e-12),
        )
        base_rows.append(
            [predicted_scores[model_id] for model_id in MODEL_IDS]
            + [math.log(predicted_costs[model_id]) for model_id in MODEL_IDS]
        )
        model_outcomes = [
            outcome_index[(episode.episode_id, model_id)] for model_id in MODEL_IDS
        ]
        target_rows.append(
            [float(outcome.score) for outcome in model_outcomes]
            + [math.log(_outcome_cost(outcome, policy)) for outcome in model_outcomes]
        )
    return PreparedSplit(
        inputs=inputs,
        outcomes=outcomes,
        token_ids=np.asarray([row[0] for row in token_rows], dtype=np.int64),
        token_types=np.asarray([row[1] for row in token_rows], dtype=np.int64),
        token_mask=np.asarray([row[2] for row in token_rows], dtype=np.bool_),
        dense_features=dense,
        base_predictions=np.asarray(base_rows, dtype=np.float32),
        targets=np.asarray(target_rows, dtype=np.float32),
    )


class TinyBertHybrid(nn.Module):
    """One bidirectional Transformer block plus a dense residual branch."""

    def __init__(
        self,
        *,
        vocab_size: int,
        sequence_length: int,
        dense_dimension: int,
        hidden_size: int,
        attention_heads: int,
        dense_hidden_size: int,
        fusion_hidden_size: int,
        dropout: float,
    ) -> None:
        super().__init__()
        self.word_embeddings = nn.Embedding(vocab_size, hidden_size, padding_idx=0)
        self.position_embeddings = nn.Embedding(sequence_length, hidden_size)
        self.type_embeddings = nn.Embedding(2, hidden_size)
        self.embedding_norm = nn.LayerNorm(hidden_size)
        self.attention_norm = nn.LayerNorm(hidden_size)
        self.attention = nn.MultiheadAttention(
            hidden_size,
            attention_heads,
            dropout=dropout,
            batch_first=True,
        )
        self.attention_dropout = nn.Dropout(dropout)
        self.feedforward_norm = nn.LayerNorm(hidden_size)
        self.feedforward = nn.Sequential(
            nn.Linear(hidden_size, hidden_size * 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size * 2, hidden_size),
            nn.Dropout(dropout),
        )
        self.dense_branch = nn.Sequential(
            nn.Linear(dense_dimension + 6, dense_hidden_size),
            nn.GELU(),
            nn.Dropout(dropout),
        )
        self.fusion = nn.Sequential(
            nn.LayerNorm(hidden_size + dense_hidden_size),
            nn.Linear(hidden_size + dense_hidden_size, fusion_hidden_size),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(fusion_hidden_size, 6),
        )

    def forward(
        self,
        token_ids: torch.Tensor,
        token_types: torch.Tensor,
        token_mask: torch.Tensor,
        dense_inputs: torch.Tensor,
        base_predictions: torch.Tensor,
    ) -> torch.Tensor:
        positions = torch.arange(
            token_ids.shape[1], device=token_ids.device
        ).unsqueeze(0)
        encoded = self.embedding_norm(
            self.word_embeddings(token_ids)
            + self.position_embeddings(positions)
            + self.type_embeddings(token_types)
        )
        normalized = self.attention_norm(encoded)
        attended, _weights = self.attention(
            normalized,
            normalized,
            normalized,
            key_padding_mask=~token_mask,
            need_weights=False,
        )
        encoded = encoded + self.attention_dropout(attended)
        encoded = encoded + self.feedforward(self.feedforward_norm(encoded))
        dense_state = self.dense_branch(
            torch.cat((dense_inputs, base_predictions), dim=1)
        )
        return self.fusion(torch.cat((encoded[:, 0, :], dense_state), dim=1))


def _set_deterministic(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)


def _content_validation_indices(inputs: InputBatch) -> tuple[np.ndarray, np.ndarray]:
    validation = np.asarray(
        [
            hash_regex._stable_hash(hash_regex.episode_text(episode)) % 5 == 0
            for episode in inputs.episodes
        ],
        dtype=np.bool_,
    )
    if not validation.any() or validation.all():
        raise ValueError("결정적 validation 분할이 비어 있습니다.")
    return np.flatnonzero(~validation), np.flatnonzero(validation)


def _normalization(
    train: PreparedSplit,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    dense_mean = train.dense_features.mean(axis=0)
    dense_scale = train.dense_features.std(axis=0)
    dense_scale = np.where(dense_scale > 1e-6, dense_scale, 1.0)
    residual = train.targets - train.base_predictions
    residual_mean = residual.mean(axis=0)
    residual_scale = residual.std(axis=0)
    residual_scale = np.where(residual_scale > 1e-6, residual_scale, 1.0)
    return dense_mean, dense_scale, residual_mean, residual_scale


def _tensor_dataset(
    split: PreparedSplit,
    *,
    dense_mean: np.ndarray,
    dense_scale: np.ndarray,
    residual_mean: np.ndarray,
    residual_scale: np.ndarray,
) -> TensorDataset:
    dense = (split.dense_features - dense_mean) / dense_scale
    residual = (
        split.targets - split.base_predictions - residual_mean
    ) / residual_scale
    return TensorDataset(
        torch.from_numpy(split.token_ids),
        torch.from_numpy(split.token_types),
        torch.from_numpy(split.token_mask),
        torch.from_numpy(dense.astype(np.float32)),
        torch.from_numpy(split.base_predictions),
        torch.from_numpy(residual.astype(np.float32)),
    )


def _loss_on_loader(
    model: TinyBertHybrid,
    loader: DataLoader[Any],
    loss_function: nn.Module,
) -> float:
    model.eval()
    total = 0.0
    rows = 0
    with torch.no_grad():
        for batch in loader:
            prediction = model(*batch[:5])
            loss = loss_function(prediction, batch[5])
            total += float(loss) * batch[0].shape[0]
            rows += batch[0].shape[0]
    return total / rows


def fit_model(
    train: PreparedSplit,
    *,
    vocab_size: int,
    sequence_length: int,
    hidden_size: int,
    attention_heads: int,
    dense_hidden_size: int,
    fusion_hidden_size: int,
    batch_size: int,
    maximum_epochs: int,
    patience: int,
    seed: int,
    refit_full: bool,
) -> tuple[
    TinyBertHybrid,
    tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    Mapping[str, Any],
]:
    _set_deterministic(seed)
    normalization = _normalization(train)
    dataset = _tensor_dataset(
        train,
        dense_mean=normalization[0],
        dense_scale=normalization[1],
        residual_mean=normalization[2],
        residual_scale=normalization[3],
    )
    training_indices, validation_indices = _content_validation_indices(train.inputs)
    generator = torch.Generator().manual_seed(seed)
    training_loader = DataLoader(
        torch.utils.data.Subset(dataset, training_indices.tolist()),
        batch_size=batch_size,
        shuffle=True,
        generator=generator,
        num_workers=0,
    )
    validation_loader = DataLoader(
        torch.utils.data.Subset(dataset, validation_indices.tolist()),
        batch_size=batch_size,
        shuffle=False,
        num_workers=0,
    )
    def new_model() -> TinyBertHybrid:
        return TinyBertHybrid(
            vocab_size=vocab_size,
            sequence_length=sequence_length,
            dense_dimension=train.dense_features.shape[1],
            hidden_size=hidden_size,
            attention_heads=attention_heads,
            dense_hidden_size=dense_hidden_size,
            fusion_hidden_size=fusion_hidden_size,
            dropout=0.10,
        )

    model = new_model()
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=1e-3, weight_decay=1e-3
    )
    loss_function = nn.SmoothL1Loss(beta=0.5)
    best_loss = math.inf
    best_epoch = 0
    best_state: dict[str, torch.Tensor] | None = None
    stale = 0
    history = []
    for epoch in range(1, maximum_epochs + 1):
        model.train()
        for batch in training_loader:
            optimizer.zero_grad(set_to_none=True)
            loss = loss_function(model(*batch[:5]), batch[5])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
        validation_loss = _loss_on_loader(model, validation_loader, loss_function)
        history.append(validation_loss)
        if validation_loss < best_loss - 1e-5:
            best_loss = validation_loss
            best_epoch = epoch
            best_state = {
                name: value.detach().clone()
                for name, value in model.state_dict().items()
            }
            stale = 0
        else:
            stale += 1
        if stale >= patience:
            break
    if best_state is None:
        raise RuntimeError("유효한 BERT-style checkpoint를 만들지 못했습니다.")
    refit_rows = len(training_indices)
    if refit_full:
        # Refit from the same deterministic initialization on every public Train
        # row. The content-held validation rows choose only the epoch count.
        _set_deterministic(seed)
        model = new_model()
        full_generator = torch.Generator().manual_seed(seed)
        full_loader = DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=True,
            generator=full_generator,
            num_workers=0,
        )
        optimizer = torch.optim.AdamW(
            model.parameters(), lr=1e-3, weight_decay=1e-3
        )
        for _epoch in range(best_epoch):
            model.train()
            for batch in full_loader:
                optimizer.zero_grad(set_to_none=True)
                loss = loss_function(model(*batch[:5]), batch[5])
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
        refit_rows = len(train.inputs.episodes)
    else:
        model.load_state_dict(best_state)
    return model, normalization, {
        "best_epoch": best_epoch,
        "best_validation_loss": best_loss,
        "epochs_run": len(history),
        "train_rows": int(len(training_indices)),
        "validation_rows": int(len(validation_indices)),
        "refit_rows": refit_rows,
        "refit_epochs": best_epoch if refit_full else 0,
    }


def predict_residuals(
    model: TinyBertHybrid,
    split: PreparedSplit,
    normalization: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    *,
    batch_size: int,
) -> np.ndarray:
    dataset = _tensor_dataset(
        split,
        dense_mean=normalization[0],
        dense_scale=normalization[1],
        residual_mean=normalization[2],
        residual_scale=normalization[3],
    )
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)
    rows = []
    model.eval()
    with torch.no_grad():
        for batch in loader:
            rows.append(model(*batch[:5]).cpu().numpy())
    normalized = np.concatenate(rows, axis=0)
    return normalization[2] + normalized * normalization[3]


def _prediction_rows(
    base: np.ndarray,
    residuals: np.ndarray,
    *,
    score_blend: float,
    cost_blend: float,
) -> tuple[list[Mapping[str, float]], list[Mapping[str, float]]]:
    combined = base.copy()
    combined[:, :3] += score_blend * residuals[:, :3]
    combined[:, 3:] += cost_blend * residuals[:, 3:]
    scores: list[Mapping[str, float]] = []
    costs: list[Mapping[str, float]] = []
    for row in combined:
        score_row = {
            model_id: min(1.0, max(0.0, float(row[index])))
            for index, model_id in enumerate(MODEL_IDS)
        }
        cost_row = {
            model_id: math.exp(min(50.0, max(-50.0, float(row[3 + index]))))
            for index, model_id in enumerate(MODEL_IDS)
        }
        light = cost_row[MODEL_IDS[0]]
        cost_row[MODEL_IDS[1]] = max(cost_row[MODEL_IDS[1]], light * (1 + 1e-12))
        cost_row[MODEL_IDS[2]] = max(
            cost_row[MODEL_IDS[2]], cost_row[MODEL_IDS[1]] * (1 + 1e-12)
        )
        scores.append(score_row)
        costs.append(cost_row)
    return scores, costs


def _safety_grid(policy: RoutingPolicy, tier: str, size: int) -> Sequence[float]:
    minimum = 1.0 / float(policy.tiers[tier].budget_multiplier)
    return tuple(
        minimum + (1.0 - minimum) * index / (size - 1)
        for index in range(size)
    )


def evaluate_blend(
    split: PreparedSplit,
    policy: RoutingPolicy,
    residuals: np.ndarray,
    *,
    score_blend: float,
    cost_blend: float,
    safety_grid_size: int,
) -> Mapping[str, Any]:
    scores, costs = _prediction_rows(
        split.base_predictions,
        residuals,
        score_blend=score_blend,
        cost_blend=cost_blend,
    )
    tier_results: dict[str, Any] = {}
    weighted = 0.0
    for tier in TIERS:
        candidates = []
        for safety in _safety_grid(policy, tier, safety_grid_size):
            selected, predicted_ratio = hash_regex.select_models(
                scores,
                costs,
                budget_multiplier=float(policy.tiers[tier].budget_multiplier),
                safety_ratio=safety,
            )
            report = train_hash_regex._score_one_tier(
                split.inputs,
                split.outcomes,
                policy,
                tier,
                selected,
            )
            if float(report["budget_ratio"]) <= TARGET_COST_RATIOS[tier]:
                candidates.append((float(report["quality_score"]), safety, predicted_ratio, report))
        if not candidates:
            raise RuntimeError(f"{tier} 목표 비용을 통과하는 후보가 없습니다.")
        quality, safety, predicted_ratio, report = max(
            candidates,
            key=lambda item: (item[0], -float(item[3]["budget_ratio"]), -item[1]),
        )
        tier_results[tier] = {
            "quality_score": report["quality_score"],
            "budget_ratio": report["budget_ratio"],
            "model_counts": report["model_counts"],
            "safety_ratio": safety,
            "predicted_budget_ratio": predicted_ratio,
        }
        weighted += quality * float(policy.tiers[tier].weight)
    return {
        "score_blend": score_blend,
        "cost_blend": cost_blend,
        "final_score": weighted,
        "tiers": tier_results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-input", type=Path, required=True)
    parser.add_argument("--train-outcomes", type=Path, required=True)
    parser.add_argument("--dev-input", type=Path, required=True)
    parser.add_argument("--dev-outcomes", type=Path, required=True)
    parser.add_argument("--base-artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--model-artifact", type=Path)
    parser.add_argument("--prediction-cache", type=Path)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--patience", type=int, default=12)
    parser.add_argument(
        "--seeds",
        default="20260826,20260827,20260828",
        help="쉼표로 구분한 결정적 ensemble seed",
    )
    parser.add_argument("--safety-grid-size", type=int, default=31)
    parser.add_argument(
        "--no-refit",
        action="store_true",
        help="내부 validation 최적 checkpoint를 유지하고 전체 Train refit을 생략",
    )
    parser.add_argument(
        "--skip-evaluation",
        action="store_true",
        help="모델과 예측 cache만 내보내고 느린 tier grid 탐색을 생략",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy = load_bundled_policy()
    artifact = hash_regex.load_artifact(args.base_artifact)
    train = prepare_split(
        input_path=args.train_input,
        outcomes_path=args.train_outcomes,
        policy=policy,
        base_artifact=artifact,
        vocab_size=DEFAULT_VOCAB_SIZE,
        sequence_length=DEFAULT_SEQUENCE_LENGTH,
    )
    dev = prepare_split(
        input_path=args.dev_input,
        outcomes_path=args.dev_outcomes,
        policy=policy,
        base_artifact=artifact,
        vocab_size=DEFAULT_VOCAB_SIZE,
        sequence_length=DEFAULT_SEQUENCE_LENGTH,
    )
    try:
        seeds = tuple(int(item) for item in args.seeds.split(","))
    except ValueError as exc:
        raise ValueError("--seeds는 쉼표로 구분한 정수여야 합니다.") from exc
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("--seeds는 중복 없는 정수를 하나 이상 포함해야 합니다.")
    if args.model_artifact is not None and len(seeds) != 1:
        raise ValueError(
            "runtime model artifact export requires exactly one --seeds value"
        )
    training_reports = []
    residual_members = []
    model_members = []
    for seed in seeds:
        model, normalization, member_report = fit_model(
            train,
            vocab_size=DEFAULT_VOCAB_SIZE,
            sequence_length=DEFAULT_SEQUENCE_LENGTH,
            hidden_size=DEFAULT_HIDDEN_SIZE,
            attention_heads=DEFAULT_ATTENTION_HEADS,
            dense_hidden_size=DEFAULT_DENSE_HIDDEN_SIZE,
            fusion_hidden_size=DEFAULT_FUSION_HIDDEN_SIZE,
            batch_size=DEFAULT_BATCH_SIZE,
            maximum_epochs=args.epochs,
            patience=args.patience,
            seed=seed,
            refit_full=not args.no_refit,
        )
        training_reports.append({"seed": seed, **member_report})
        residual_members.append(
            predict_residuals(
                model,
                dev,
                normalization,
                batch_size=DEFAULT_BATCH_SIZE,
            )
        )
        model_members.append(
            {
                "seed": seed,
                "normalization": {
                    "dense_mean": normalization[0].tolist(),
                    "dense_scale": normalization[1].tolist(),
                    "residual_mean": normalization[2].tolist(),
                    "residual_scale": normalization[3].tolist(),
                },
                "state_dict": {
                    name: tensor.detach().cpu().tolist()
                    for name, tensor in model.state_dict().items()
                },
            }
        )
    residuals = np.mean(np.stack(residual_members, axis=0), axis=0)
    if args.prediction_cache is not None:
        args.prediction_cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            args.prediction_cache,
            residuals=residuals,
            base_predictions=dev.base_predictions,
            targets=dev.targets,
        )
    if args.model_artifact is not None:
        model_artifact = {
            "artifact_type": "ossp-tiny-bert-residual-v1",
            "schema_version": 1,
            "feature_version": 1,
            "model_ids": list(MODEL_IDS),
            "policy_id": policy.policy_id,
            "policy_sha256": policy_sha256(policy),
            "configuration": {
                "vocab_size": DEFAULT_VOCAB_SIZE,
                "sequence_length": DEFAULT_SEQUENCE_LENGTH,
                "hidden_size": DEFAULT_HIDDEN_SIZE,
                "attention_heads": DEFAULT_ATTENTION_HEADS,
                "dense_dimension": int(train.dense_features.shape[1]),
                "dense_hidden_size": DEFAULT_DENSE_HIDDEN_SIZE,
                "fusion_hidden_size": DEFAULT_FUSION_HIDDEN_SIZE,
                "refit_full": not args.no_refit,
            },
            "members": model_members,
            "training_summary": {
                "optimizer": "tiny-bert-residual-adamw-v1",
                "train_episodes": len(train.inputs.episodes),
                "validation_episodes": len(dev.inputs.episodes),
                "train_input_sha256": _file_sha256(args.train_input),
                "train_outcomes_sha256": _file_sha256(args.train_outcomes),
                "validation_input_sha256": _file_sha256(args.dev_input),
                "validation_outcomes_sha256": _file_sha256(args.dev_outcomes),
                "base_artifact_sha256": _file_sha256(args.base_artifact),
                "numpy_version": np.__version__,
                "torch_version": torch.__version__,
                "members": training_reports,
            },
        }
        args.model_artifact.parent.mkdir(parents=True, exist_ok=True)
        args.model_artifact.write_bytes(
            (
                json.dumps(
                    model_artifact,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        )
    if args.skip_evaluation:
        report = {
            "report_type": "ossp-tiny-bert-hybrid-training-only-v1",
            "training": training_reports,
            "configuration": {
                "vocab_size": DEFAULT_VOCAB_SIZE,
                "sequence_length": DEFAULT_SEQUENCE_LENGTH,
                "hidden_size": DEFAULT_HIDDEN_SIZE,
                "attention_heads": DEFAULT_ATTENTION_HEADS,
                "dense_hidden_size": DEFAULT_DENSE_HIDDEN_SIZE,
                "fusion_hidden_size": DEFAULT_FUSION_HIDDEN_SIZE,
                "seeds": seeds,
                "refit_full": not args.no_refit,
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        return 0
    evaluations = []
    for score_blend in (0.0, 0.25, 0.5, 0.75, 1.0):
        for cost_blend in (0.0, 0.5, 1.0):
            evaluations.append(
                evaluate_blend(
                    dev,
                    policy,
                    residuals,
                    score_blend=score_blend,
                    cost_blend=cost_blend,
                    safety_grid_size=args.safety_grid_size,
                )
            )
    best = max(
        evaluations,
        key=lambda item: (item["final_score"], -item["score_blend"], -item["cost_blend"]),
    )
    tierwise: dict[str, Any] = {}
    tierwise_score = 0.0
    for tier in TIERS:
        candidate = max(
            evaluations,
            key=lambda item: (
                float(item["tiers"][tier]["quality_score"]),
                -float(item["tiers"][tier]["budget_ratio"]),
                -item["score_blend"],
                -item["cost_blend"],
            ),
        )
        tierwise[tier] = {
            "score_blend": candidate["score_blend"],
            "cost_blend": candidate["cost_blend"],
            **candidate["tiers"][tier],
        }
        tierwise_score += float(
            candidate["tiers"][tier]["quality_score"]
        ) * float(policy.tiers[tier].weight)
    report = {
        "report_type": "ossp-tiny-bert-hybrid-experiment-v1",
        "training": training_reports,
        "configuration": {
            "vocab_size": DEFAULT_VOCAB_SIZE,
            "sequence_length": DEFAULT_SEQUENCE_LENGTH,
            "hidden_size": DEFAULT_HIDDEN_SIZE,
            "attention_heads": DEFAULT_ATTENTION_HEADS,
            "dense_hidden_size": DEFAULT_DENSE_HIDDEN_SIZE,
            "fusion_hidden_size": DEFAULT_FUSION_HIDDEN_SIZE,
            "seeds": seeds,
            "target_cost_ratios": TARGET_COST_RATIOS,
        },
        "best": best,
        "best_tierwise": {
            "final_score": tierwise_score,
            "tiers": tierwise,
        },
        "evaluations": evaluations,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
