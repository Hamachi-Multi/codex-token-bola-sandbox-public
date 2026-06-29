"""Shared cleanup helpers for the Codex Token Bola dashboard."""

from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime
from typing import Any, TypedDict


class CleanupImpact(TypedDict, total=False):
    total_rows: int
    affected_rows: int
    delete_size: int
    affected_files: int
    source_files: int
    targets: list[str]
    targets_truncated: int
    items: list[dict[str, Any]]
    delete_files: int
    rewrite_files: int
    action_file_counts: dict[str, int]

def safe_file_size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def safe_tree_size(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return safe_file_size(path)
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += safe_file_size(child)
    except OSError:
        return total
    return total


def existing_target_paths(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    targets: list[pathlib.Path] = []
    for path in paths:
        try:
            if path.is_symlink() or path.is_file():
                targets.append(path)
            elif path.is_dir():
                targets.extend(child for child in path.rglob("*") if child.is_symlink() or child.is_file())
        except OSError:
            continue
    return targets


def target_paths_count(paths: list[pathlib.Path]) -> int:
    count = 0
    for path in paths:
        try:
            if path.is_symlink() or path.is_file():
                count += 1
            elif path.is_dir():
                count += sum(1 for child in path.rglob("*") if child.is_symlink() or child.is_file())
        except OSError:
            continue
    return count


def target_paths_size(paths: list[pathlib.Path]) -> int:
    return sum(safe_tree_size(path) for path in paths)


def is_hex_state_name(path: pathlib.Path) -> bool:
    return path.suffix == ".json" and len(path.stem) == 32 and all(char in "0123456789abcdef" for char in path.stem.lower())


def read_json_object(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

def proportional_affected_size(paths: list[pathlib.Path], total_rows: int, affected_rows: int) -> int:
    size = target_paths_size(paths)
    total = max(0, int(total_rows or 0))
    affected = max(0, int(affected_rows or 0))
    if size <= 0 or total <= 0 or affected <= 0:
        return 0
    if affected >= total:
        return size
    return max(1, (size * affected + total - 1) // total)


def impact_payload(
    *,
    total_rows: int = 0,
    affected_rows: int = 0,
    delete_size: int = 0,
    affected_files: int = 0,
    source_files: int = 0,
    targets: list[pathlib.Path] | None = None,
    include_targets: bool = True,
) -> CleanupImpact:
    """Build cleanup impact data.

    ``affected_files`` is the count of existing files affected by the cleanup
    result. Delete, rewrite, rebuild, and reset outcomes are all subtypes of
    affected files; source file counts stay separate in ``source_files``.
    """
    target_texts = [str(path) for path in (targets or [])] if include_targets else []
    normalized_affected_files = max(0, int(affected_files or 0))
    payload: CleanupImpact = {
        "total_rows": max(0, int(total_rows or 0)),
        "affected_rows": max(0, int(affected_rows or 0)),
        "delete_size": max(0, int(delete_size or 0)),
        "affected_files": normalized_affected_files,
        "source_files": max(0, int(source_files or 0)),
    }
    if include_targets:
        payload["targets"] = target_texts
        payload["targets_truncated"] = 0
    return payload


def read_run_metadata(db_path: pathlib.Path) -> dict[str, Any]:
    if not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        try:
            rows = con.execute("select key, value from run_metadata").fetchall()
        except sqlite3.Error:
            return {}
        metadata: dict[str, Any] = {}
        for key, value in rows:
            try:
                metadata[str(key)] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                metadata[str(key)] = value
        return metadata
    finally:
        con.close()


def parse_row_time(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
