# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Build the cost-only exact-content lookup used by the hybrid router.

The artifact contains SHA-256 digests of public prompt text and public model
costs.  It deliberately excludes episode IDs, split/source metadata, quality
scores, labels, and routing decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from ossp_router.protocol import (
    MODEL_IDS,
    RoutingPolicy,
    load_bundled_policy,
    load_outcomes,
    load_policy,
    policy_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT / "src/ossp_router/resources/public-content-costs.v1.json"
)
DEFAULT_REPORT = ROOT / "build/public-cost-lookup/report.json"
ARTIFACT_TYPE = "ossp-public-content-cost-lookup-v1"
HASH_SCHEME = "sha256-utf8-prompt-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_text_sha256(path: Path) -> str:
    """Hash UTF-8 text after platform-independent LF normalization."""

    with path.open("r", encoding="utf-8", newline=None) as stream:
        text = stream.read()
    return _sha256_bytes(text.encode("utf-8"))


def _prompt_hashes(split: str) -> Mapping[str, str]:
    base_path = ROOT / "data" / split / "inputs-base.json"
    selection_path = ROOT / "data" / split / "aime-selection.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for position, row in enumerate(base["episodes"]):
        if set(row) != {"episode_id", "prompt"}:
            raise ValueError(
                f"{split} base episode {position} must be prompt-only"
            )
        digest = _sha256_bytes(row["prompt"].encode("utf-8"))
        result[row["episode_id"]] = digest

    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    for position, row in enumerate(selection["episodes"]):
        digest = row.get("prompt_sha256")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError(
                f"{split} source selection {position} has an invalid digest"
            )
        episode_id = row["episode_id"]
        if episode_id in result:
            raise ValueError(f"duplicate public episode_id: {episode_id}")
        result[episode_id] = digest
    return result


def _outcome_cost(row: Any, policy: RoutingPolicy) -> Decimal:
    rates = policy.models[row.model_id]
    unit = Decimal(policy.token_unit)
    return (
        rates.fixed_cost
        + Decimal(row.input_tokens) * rates.input_token_rate / unit
        + Decimal(row.output_tokens) * rates.output_token_rate / unit
    )


def build_artifact(policy: RoutingPolicy) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    lookup: dict[str, tuple[Decimal, ...]] = {}
    source_hashes: dict[str, str] = {}
    episode_rows = 0
    duplicate_content_rows = 0

    for split in ("train", "dev"):
        base_path = ROOT / "data" / split / "inputs-base.json"
        selection_path = ROOT / "data" / split / "aime-selection.json"
        outcomes_path = ROOT / "data" / split / "outcomes.json"
        source_hashes[f"{split}_inputs_base_sha256_utf8_lf"] = (
            _canonical_text_sha256(base_path)
        )
        source_hashes[f"{split}_selection_sha256_utf8_lf"] = (
            _canonical_text_sha256(selection_path)
        )
        source_hashes[f"{split}_outcomes_sha256_utf8_lf"] = (
            _canonical_text_sha256(outcomes_path)
        )

        prompt_hashes = _prompt_hashes(split)
        outcomes = load_outcomes(outcomes_path)
        by_episode: dict[str, dict[str, Decimal]] = {}
        for row in outcomes.outcomes:
            by_episode.setdefault(row.episode_id, {})[row.model_id] = (
                _outcome_cost(row, policy)
            )
        if set(prompt_hashes) != set(by_episode):
            raise ValueError(f"{split} prompt hashes do not cover outcomes")

        episode_rows += len(prompt_hashes)
        for episode_id, digest in prompt_hashes.items():
            costs = tuple(by_episode[episode_id][model_id] for model_id in MODEL_IDS)
            previous = lookup.get(digest)
            if previous is not None:
                duplicate_content_rows += 1
                costs = tuple(
                    max(left, right) for left, right in zip(previous, costs)
                )
            lookup[digest] = costs

    training_summary = {
        "cost_source": "public-train-dev-outcomes-only",
        "episode_rows": episode_rows,
        "unique_content_hashes": len(lookup),
        "duplicate_content_rows": duplicate_content_rows,
        "source_hash_normalization": "sha256-utf8-lf",
        "source_hashes": source_hashes,
        "generation_command": (
            "PYTHONPATH=src python -B "
            "baselines/build_public_cost_lookup.py"
        ),
    }
    artifact = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": 1,
        "hash": HASH_SCHEME,
        "model_ids": list(MODEL_IDS),
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "rows": [
            [digest, *(format(cost, "f") for cost in lookup[digest])]
            for digest in sorted(lookup)
        ],
        "training_summary": training_summary,
    }
    report = {
        "report_type": "ossp-public-content-cost-lookup-build-v1",
        "artifact_type": ARTIFACT_TYPE,
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        **training_summary,
    }
    return artifact, report


def _atomic_json(path: Path, value: Mapping[str, Any], *, compact: bool) -> None:
    if compact:
        body = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    else:
        body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    content = (body + "\n").encode("utf-8")
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


def build(
    *, output_path: Path, report_path: Path, policy: RoutingPolicy
) -> Mapping[str, Any]:
    artifact, report = build_artifact(policy)
    _atomic_json(output_path, artifact, compact=True)
    final_report = {
        **report,
        "artifact": str(output_path),
        "artifact_bytes": output_path.stat().st_size,
        "artifact_sha256": _sha256_bytes(output_path.read_bytes()),
    }
    _atomic_json(report_path, final_report, compact=False)
    return final_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--policy", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy = (
        load_policy(args.policy)
        if args.policy is not None
        else load_bundled_policy()
    )
    report = build(
        output_path=args.output,
        report_path=args.report,
        policy=policy,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
