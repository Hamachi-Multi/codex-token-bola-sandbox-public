"""Shared analysis input fingerprint helpers."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib

import service_paths


def analysis_input_paths(codex_home: str | pathlib.Path | None = None, state_db: str | pathlib.Path | None = None) -> list[pathlib.Path]:
    home = pathlib.Path(codex_home).expanduser() if codex_home else pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    state_db_path = pathlib.Path(state_db).expanduser() if state_db else home / "state_5.sqlite"
    return [
        state_db_path,
        state_db_path.parent / "session_index.jsonl",
        service_paths.service_root(home) / "state" / "retention-pruned-turns.json",
    ]


def paths_fingerprint(paths: list[pathlib.Path]) -> str:
    payload: list[dict[str, object]] = []
    for path in paths:
        try:
            stat_result = path.stat()
        except OSError:
            payload.append({"path": str(path), "exists": False})
            continue
        payload.append(
            {
                "path": str(path),
                "exists": True,
                "size": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def analysis_input_fingerprint(codex_home: str | pathlib.Path | None = None, state_db: str | pathlib.Path | None = None) -> str:
    return paths_fingerprint(analysis_input_paths(codex_home, state_db))
