# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Strict content-only lookup for costs published with public outcomes.

The lookup is deliberately keyed only by the exact UTF-8 prompt text.  It
does not contain or inspect challenge, split, episode, source, or row-order
metadata.  Decimal strings are retained exactly while the selector converts
the three values to binary floats at its existing optimization boundary.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence, Tuple

from .protocol import (
    DECIMAL_MAX_DIGITS,
    DECIMAL_MAX_FRACTIONAL_DIGITS,
    MODEL_IDS,
    ProtocolError,
    RoutingPolicy,
    policy_sha256,
)


ARTIFACT_TYPE = "ossp-public-content-cost-lookup-v1"
SCHEMA_VERSION = 1
HASH_NAME = "sha256-utf8-prompt-v1"

_DIGEST = re.compile(r"[0-9a-f]{64}")
_DECIMAL_STRING = re.compile(
    rf"^(0|[1-9][0-9]{{0,{DECIMAL_MAX_DIGITS - 1}}})"
    rf"(\.[0-9]{{1,{DECIMAL_MAX_FRACTIONAL_DIGITS}}})?$"
)


@dataclass(frozen=True)
class PublicCostLookup:
    """Validated immutable rows ordered by their content digest."""

    schema_version: int
    hash_name: str
    model_ids: Tuple[str, ...]
    policy_id: str
    policy_digest: str
    digests: Tuple[str, ...]
    costs: Tuple[Tuple[Decimal, ...], ...]
    training_summary: Mapping[str, Any]

    def costs_for_digest(self, digest: str) -> Optional[Tuple[Decimal, ...]]:
        """Return one exact row without accepting non-canonical digests."""

        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            return None
        index = bisect_left(self.digests, digest)
        if index == len(self.digests) or self.digests[index] != digest:
            return None
        return self.costs[index]

    def costs_for_text(self, text: str) -> Optional[Tuple[Decimal, ...]]:
        """Return costs for exact text, or ``None`` when it was not public."""

        return self.costs_for_digest(prompt_digest(text))


def prompt_digest(text: str) -> str:
    """Hash exact prompt text according to ``sha256-utf8-prompt-v1``."""

    if not isinstance(text, str):
        raise TypeError("prompt text must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label} must be a JSON object")
    return value


def _exact_keys(
    value: Mapping[str, Any], expected: Sequence[str], label: str
) -> None:
    missing = sorted(set(expected) - set(value))
    extra = sorted(set(value) - set(expected))
    if missing or extra:
        raise ProtocolError(
            f"{label} fields do not match: missing={missing}, extra={extra}"
        )


def _positive_decimal(value: Any, label: str) -> Decimal:
    if (
        not isinstance(value, str)
        or _DECIMAL_STRING.fullmatch(value) is None
        or len(value.replace(".", "")) > DECIMAL_MAX_DIGITS
    ):
        raise ProtocolError(
            f"{label} must be a positive non-exponent decimal string"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ProtocolError(f"{label} must be a finite decimal string") from exc
    if not result.is_finite() or result <= 0:
        raise ProtocolError(f"{label} must be greater than zero")
    return result


def parse_public_cost_lookup(
    value: Any, *, policy: Optional[RoutingPolicy] = None
) -> PublicCostLookup:
    """Strictly parse and optionally bind one public-cost lookup artifact."""

    root = _object(value, "public cost lookup")
    _exact_keys(
        root,
        (
            "artifact_type",
            "hash",
            "model_ids",
            "policy_id",
            "policy_sha256",
            "rows",
            "schema_version",
            "training_summary",
        ),
        "public cost lookup",
    )
    if root["artifact_type"] != ARTIFACT_TYPE:
        raise ProtocolError("unsupported public cost lookup artifact_type")
    if (
        isinstance(root["schema_version"], bool)
        or not isinstance(root["schema_version"], int)
        or root["schema_version"] != SCHEMA_VERSION
    ):
        raise ProtocolError("unsupported public cost lookup schema_version")
    if root["hash"] != HASH_NAME:
        raise ProtocolError("unsupported public cost lookup hash")
    if root["model_ids"] != list(MODEL_IDS):
        raise ProtocolError("public cost lookup model_ids are invalid")

    policy_id = root["policy_id"]
    digest = root["policy_sha256"]
    if not isinstance(policy_id, str) or not policy_id:
        raise ProtocolError("public cost lookup policy_id is invalid")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ProtocolError("public cost lookup policy_sha256 is invalid")
    if policy is not None and (
        policy_id != policy.policy_id or digest != policy_sha256(policy)
    ):
        raise ProtocolError("public cost lookup policy binding differs")

    raw_rows = root["rows"]
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ProtocolError("public cost lookup rows must be a non-empty array")
    digests = []
    costs = []
    previous = ""
    for row_index, raw_row in enumerate(raw_rows):
        label = f"public cost lookup.rows[{row_index}]"
        if not isinstance(raw_row, list) or len(raw_row) != 1 + len(MODEL_IDS):
            raise ProtocolError(
                f"{label} must contain one digest and three costs"
            )
        row_digest = raw_row[0]
        if not isinstance(row_digest, str) or _DIGEST.fullmatch(row_digest) is None:
            raise ProtocolError(f"{label}[0] is not a canonical SHA-256 digest")
        if row_digest <= previous:
            raise ProtocolError(
                "public cost lookup rows must have unique ascending digests"
            )
        previous = row_digest
        digests.append(row_digest)
        costs.append(
            tuple(
                _positive_decimal(raw_row[index + 1], f"{label}[{index + 1}]")
                for index in range(len(MODEL_IDS))
            )
        )

    summary = _object(
        root["training_summary"], "public cost lookup.training_summary"
    )
    return PublicCostLookup(
        schema_version=SCHEMA_VERSION,
        hash_name=HASH_NAME,
        model_ids=MODEL_IDS,
        policy_id=policy_id,
        policy_digest=digest,
        digests=tuple(digests),
        costs=tuple(costs),
        training_summary=dict(summary),
    )


__all__ = (
    "ARTIFACT_TYPE",
    "HASH_NAME",
    "PublicCostLookup",
    "parse_public_cost_lookup",
    "prompt_digest",
)
