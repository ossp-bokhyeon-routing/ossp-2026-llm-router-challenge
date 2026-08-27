# SPDX-FileCopyrightText: Copyright 2026 ossp-bokhyeon-routing contributors
# SPDX-License-Identifier: Apache-2.0

"""Deterministically hash every source file that can affect router images."""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any, Dict, List


SOURCE_MANIFEST_LABEL = "io.sktelecom.ossp.source-manifest-sha256"
SOURCE_MANIFEST_SCOPE = (
    ".dockerignore",
    "container/Dockerfile",
    "container/measurement.Dockerfile",
    "container/entrypoint.py",
    "src",
    "baselines/feature_budget.py",
    "baselines/hash_regex.py",
    "baselines/hash-regex-public.v1.json",
)


def _sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_tree_manifest(root: pathlib.Path) -> Dict[str, Any]:
    """Return a stable manifest of the Docker and measurement build inputs."""

    root = pathlib.Path(root).resolve()
    files: List[pathlib.Path] = []
    for relative in SOURCE_MANIFEST_SCOPE:
        candidate = root / relative
        if candidate.is_symlink():
            raise RuntimeError(
                f"소스 파일 목록에 심볼릭 링크를 포함할 수 없습니다: {candidate}"
            )
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
            for path in candidate.rglob("*"):
                if path.is_symlink():
                    raise RuntimeError(
                        "소스 파일 목록에 심볼릭 링크를 포함할 수 없습니다: "
                        f"{path}"
                    )
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and not any(
                        part.endswith(".egg-info") for part in path.parts
                    )
                    and not path.name.endswith((".pyc", ".pyo"))
                ):
                    files.append(path)
        else:
            raise RuntimeError(f"소스 manifest 경로가 없습니다: {candidate}")
    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256_file(path),
        }
        for path in sorted(set(files))
    ]
    encoded = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "algorithm": "sha256",
        "scope": list(SOURCE_MANIFEST_SCOPE),
        "entries": entries,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
