"""Retention preview, index, and prune helpers for dashboard cleanup."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import raw_segments

from dashboard_cleanup_common import (
    is_hex_state_name,
    read_json_object,
    parse_row_time,
    safe_file_size,
    target_paths_size,
)
from dashboard_cleanup_recovery import (
    clear_cleanup_retention_job,
    commit_pruned_turn_state,
    discard_pruned_turn_state_stage,
    pruned_turn_from_row,
    read_cleanup_retention_job,
    recover_retention_cleanup,
    stage_pruned_turn_state,
    write_cleanup_retention_job,
)

RETENTION_PREVIEW_CACHE: dict[
    tuple[str, float, tuple[tuple[str, bool, int, int], ...], tuple[tuple[str, bool, int, int], ...], tuple[tuple[str, int, int], ...]],
    dict[str, Any],
] = {}
RETENTION_PREVIEW_CACHE_LIMIT = 16
RETENTION_INDEX_SCHEMA_VERSION = 3
RETENTION_INDEX_RELATIVE_PATH = pathlib.Path("state") / "cleanup-retention-index.json"

def pending_turn_state_payload(path: pathlib.Path) -> dict[str, Any] | None:
    if not is_hex_state_name(path) or not path.is_file():
        return None
    data = read_json_object(path)
    if data is None or data.get("record_type") != "turn_start":
        return None
    return data


def pending_turn_state_paths(state_dir: pathlib.Path) -> list[pathlib.Path]:
    try:
        candidates = sorted(state_dir.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        return []
    return [path for path in candidates if pending_turn_state_payload(path) is not None]


def pending_turn_state_file_signature(path: pathlib.Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "inode": int(stat.st_ino),
        "device": int(stat.st_dev),
    }


def verify_pending_turn_state_file_signature(path: pathlib.Path, signature: dict[str, Any] | None) -> None:
    if not path.exists():
        return
    if not isinstance(signature, dict):
        raise raw_segments.ManifestError(f"pending turn state signature missing: {path}")
    current = pending_turn_state_file_signature(path)
    for key, value in current.items():
        if int(signature.get(key) or -1) != value:
            raise raw_segments.ManifestError(f"pending turn state changed after planning: {path}")


def plan_pending_turn_state_for_retention(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any]:
    state_dir = pathlib.Path(base).expanduser() / "state"
    scanned: list[pathlib.Path] = []
    deletable: list[pathlib.Path] = []
    target_signatures: dict[str, dict[str, int]] = {}
    for path in pending_turn_state_paths(state_dir):
        payload = pending_turn_state_payload(path)
        if payload is None:
            continue
        scanned.append(path)
        row_time = parse_row_time(payload.get("captured_at"))
        if row_time is not None and row_time < float(cutoff_unix):
            deletable.append(path)
            target_signatures[str(path)] = pending_turn_state_file_signature(path)
    return {
        "scanned_files": len(scanned),
        "deleted_files": len(deletable),
        "deleted_bytes": target_paths_size(deletable),
        "targets": [str(path) for path in deletable],
        "target_signatures": target_signatures,
    }


def apply_pending_turn_state_plan(plan: dict[str, Any]) -> dict[str, Any]:
    deleted: list[dict[str, Any]] = []
    target_signatures = plan.get("target_signatures") if isinstance(plan.get("target_signatures"), dict) else {}
    for text in plan.get("targets") or []:
        path = pathlib.Path(str(text))
        signature = target_signatures.get(str(path)) if isinstance(target_signatures, dict) else None
        verify_pending_turn_state_file_signature(path, signature)
        before = safe_file_size(path)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append({"path": str(path), "deleted_bytes": before})
    return {
        "scanned_files": int(plan.get("scanned_files") or 0),
        "deleted_files": len(deleted),
        "deleted_bytes": sum(int(item["deleted_bytes"]) for item in deleted),
        "deleted": deleted,
    }



def retention_row_time(row: dict[str, Any]) -> float | None:
    keys = ("captured_at", "stopped_at", "started_at", "timestamp")
    for key in keys:
        parsed = parse_row_time(row.get(key))
        if parsed is not None:
            return parsed
    return None



def estimated_delete_bytes(path: pathlib.Path | str, source_size: int, deletable_row_bytes: int, scanned_row_bytes: int) -> int:
    source_size = max(0, int(source_size or 0))
    deletable_row_bytes = max(0, int(deletable_row_bytes or 0))
    scanned_row_bytes = max(0, int(scanned_row_bytes or 0))
    if source_size <= 0 or deletable_row_bytes <= 0:
        return 0
    if pathlib.Path(path).suffix == ".gz":
        if scanned_row_bytes <= 0:
            return 0
        return min(source_size, max(1, round(source_size * (deletable_row_bytes / scanned_row_bytes))))
    return min(source_size, deletable_row_bytes)


def public_retention_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def retention_source_file_signature(path: pathlib.Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "inode": int(stat.st_ino),
        "device": int(stat.st_dev),
    }


def verify_retention_source_signature(path: pathlib.Path, signature: dict[str, Any] | None) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise raw_segments.ManifestError(f"untracked retention source must not be a symlink: {path}")
    if not path.exists() or not path.is_file():
        raise raw_segments.ManifestError(f"untracked retention source missing: {path}")
    if not isinstance(signature, dict):
        raise raw_segments.ManifestError(f"untracked retention source signature missing: {path}")
    current = retention_source_file_signature(path)
    for key, value in current.items():
        if int(signature.get(key) or -1) != value:
            raise raw_segments.ManifestError(f"untracked retention source changed after planning: {path}")


def resolved_retention_source_path(path: pathlib.Path | str) -> str:
    return str(pathlib.Path(path).expanduser().resolve(strict=False))


def validate_untracked_retention_source(base: pathlib.Path, path: pathlib.Path, *, must_exist: bool = False) -> pathlib.Path:
    base = pathlib.Path(base).expanduser()
    path = pathlib.Path(path).expanduser()
    raw_dir = base / "raw"
    archive_dir = raw_dir / "archive"
    if raw_dir.is_symlink() or archive_dir.is_symlink():
        raise raw_segments.ManifestError(f"untracked retention raw roots must not be symlinks: {raw_dir}")
    if path.parent.is_symlink() or path.is_symlink():
        raise raw_segments.ManifestError(f"untracked retention source must not be a symlink: {path}")
    if not path.exists():
        if must_exist:
            raise raw_segments.ManifestError(f"untracked retention source missing: {path}")
        return path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise raw_segments.ManifestError(f"untracked retention source missing: {path}") from exc
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise raw_segments.ManifestError(f"untracked retention source outside service root: {path}") from exc
    if not path.is_file():
        raise raw_segments.ManifestError(f"untracked retention source must be a regular file: {path}")
    return path


def plan_jsonl_for_retention(path: pathlib.Path, cutoff_unix: float, *, delete_when_empty: bool) -> dict[str, Any]:
    cutoff = float(cutoff_unix)
    source_signature = retention_source_file_signature(path) if path.exists() and path.is_file() else None
    if not path.exists() or not path.is_file():
        return {
            "path": str(path),
            "scanned_rows": 0,
            "deleted_rows": 0,
            "kept_rows": 0,
            "before_bytes": 0,
            "after_bytes": 0,
            "rewritten": False,
            "deleted_file": False,
            "deleted_turns": [],
            "_cutoff_unix": cutoff,
            "_delete_when_empty": bool(delete_when_empty),
            "_source_signature": source_signature,
        }
    before_bytes = safe_file_size(path)
    opener = gzip.open if path.suffix == ".gz" else open
    retained_line_count = 0
    deleted_turns: list[dict[str, Any]] = []
    scanned_rows = 0
    deleted_rows = 0
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                retained_line_count += 1
                continue
            if not isinstance(parsed, dict):
                retained_line_count += 1
                continue
            scanned_rows += 1
            row_time = retention_row_time(parsed)
            if row_time is not None and row_time < cutoff:
                deleted_rows += 1
                pruned = pruned_turn_from_row(parsed, row_time)
                if pruned is not None:
                    deleted_turns.append(pruned)
                continue
            retained_line_count += 1

    if deleted_rows <= 0:
        return {
            "path": str(path),
            "scanned_rows": scanned_rows,
            "deleted_rows": 0,
            "kept_rows": scanned_rows,
            "before_bytes": before_bytes,
            "after_bytes": before_bytes,
            "rewritten": False,
            "deleted_file": False,
            "deleted_turns": [],
            "_cutoff_unix": cutoff,
            "_delete_when_empty": bool(delete_when_empty),
            "_source_signature": source_signature,
        }

    if retained_line_count <= 0 and delete_when_empty:
        return {
            "path": str(path),
            "scanned_rows": scanned_rows,
            "deleted_rows": deleted_rows,
            "kept_rows": 0,
            "before_bytes": before_bytes,
            "after_bytes": 0,
            "rewritten": True,
            "deleted_file": True,
            "deleted_turns": deleted_turns,
            "_cutoff_unix": cutoff,
            "_delete_when_empty": bool(delete_when_empty),
            "_source_signature": source_signature,
        }

    return {
        "path": str(path),
        "scanned_rows": scanned_rows,
        "deleted_rows": deleted_rows,
        "kept_rows": scanned_rows - deleted_rows,
        "before_bytes": before_bytes,
        "after_bytes": None,
        "rewritten": True,
        "deleted_file": False,
        "deleted_turns": deleted_turns,
        "_cutoff_unix": cutoff,
        "_delete_when_empty": bool(delete_when_empty),
        "_source_signature": source_signature,
    }


def write_retained_jsonl_for_retention(path: pathlib.Path, cutoff_unix: float, tmp: pathlib.Path) -> None:
    opener = gzip.open if path.suffix == ".gz" else open
    writer = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source, writer(tmp, "wt", encoding="utf-8") as target:
        for line in source:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                target.write(line if line.endswith("\n") else line + "\n")
                continue
            if not isinstance(parsed, dict):
                target.write(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) + "\n")
                continue
            row_time = retention_row_time(parsed)
            if row_time is not None and row_time < float(cutoff_unix):
                continue
            target.write(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) + "\n")


def apply_retention_plan(plan: dict[str, Any]) -> dict[str, Any]:
    path = pathlib.Path(str(plan.get("path") or ""))
    if int(plan.get("deleted_rows") or 0) <= 0:
        return public_retention_result(plan)
    verify_retention_source_signature(path, plan.get("_source_signature") if isinstance(plan.get("_source_signature"), dict) else None)
    if bool(plan.get("deleted_file")):
        path.unlink()
        result = dict(plan)
        result["after_bytes"] = 0
        return public_retention_result(result)

    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    write_retained_jsonl_for_retention(path, float(plan.get("_cutoff_unix") or 0.0), tmp)
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)
    after_bytes = safe_file_size(path)
    result = dict(plan)
    result["after_bytes"] = after_bytes
    return public_retention_result(result)


def rewrite_jsonl_for_retention(path: pathlib.Path, cutoff_unix: float, *, delete_when_empty: bool) -> dict[str, Any]:
    return apply_retention_plan(plan_jsonl_for_retention(path, cutoff_unix, delete_when_empty=delete_when_empty))


def preview_jsonl_for_retention(path: pathlib.Path, cutoff_unix: float) -> dict[str, Any]:
    try:
        stat = path.stat()
        source_size = stat.st_size if path.is_file() else 0
        source_mtime_ns = stat.st_mtime_ns
    except OSError:
        source_size = 0
        source_mtime_ns = 0
    if not path.exists() or not path.is_file():
        return {
            "path": str(path),
            "source_size": source_size,
            "source_mtime_ns": source_mtime_ns,
            "scanned_rows": 0,
            "deletable_rows": 0,
            "deletable_bytes": 0,
            "affected": False,
        }
    opener = gzip.open if path.suffix == ".gz" else open
    scanned_rows = 0
    scanned_bytes = 0
    deletable_rows = 0
    deletable_row_bytes = 0
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            scanned_rows += 1
            line_bytes = len(line.encode("utf-8"))
            scanned_bytes += line_bytes
            row_time = retention_row_time(parsed)
            if row_time is not None and row_time < cutoff_unix:
                deletable_rows += 1
                deletable_row_bytes += line_bytes
    return {
        "path": str(path),
        "source_size": source_size,
        "source_mtime_ns": source_mtime_ns,
        "scanned_rows": scanned_rows,
        "deletable_rows": deletable_rows,
        "deletable_bytes": estimated_delete_bytes(path, source_size, deletable_row_bytes, scanned_bytes),
        "affected": deletable_rows > 0,
    }


def retention_sources(base: pathlib.Path) -> list[tuple[pathlib.Path, bool]]:
    raw_dir = base / "raw"
    archive_dir = raw_dir / "archive"
    if raw_dir.is_symlink() or archive_dir.is_symlink():
        raise raw_segments.ManifestError(f"untracked retention raw roots must not be symlinks: {raw_dir}")
    return []


def retention_source_signature(base: pathlib.Path) -> tuple[tuple[str, bool, int, int], ...]:
    signature = []
    for path, delete_when_empty in retention_sources(base):
        try:
            stat = path.stat()
            size = stat.st_size if path.is_file() else 0
            mtime_ns = stat.st_mtime_ns
        except OSError:
            size = 0
            mtime_ns = 0
        signature.append((str(path), delete_when_empty, size, mtime_ns))
    return tuple(signature)


def current_retention_source_signature(base: pathlib.Path) -> tuple[tuple[str, bool, int, int], ...]:
    signature = []
    pointer = raw_segments.strict_read_current_pointer(base)
    for kind, current in sorted(pointer.get("current", {}).items()):
        if not isinstance(current, dict):
            raise raw_segments.ManifestError(f"current segment entry must be an object: {kind}")
        segment = raw_segments.validate_current_segment_entry(base, current, kind=str(kind))
        path = pathlib.Path(str(segment.get("path") or ""))
        stat = path.stat()
        signature.append((str(path), False, stat.st_size, stat.st_mtime_ns))
    return tuple(signature)


def pending_turn_state_signature(base: pathlib.Path) -> tuple[tuple[str, int, int], ...]:
    state_dir = pathlib.Path(base).expanduser() / "state"
    signature = []
    for path in pending_turn_state_paths(state_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(signature)


def retention_index_signature(base: pathlib.Path, tracked_paths: set[str] | None = None) -> tuple[tuple[str, bool, int, int], ...]:
    excluded = tracked_paths or set()
    signature = (*retention_source_signature(base), *current_retention_source_signature(base))
    return tuple(item for item in signature if resolved_retention_source_path(item[0]) not in excluded)


def retention_preview_signature(base: pathlib.Path, cutoff_unix: float) -> str:
    payload = {
        "cutoff_unix": float(cutoff_unix),
        "sources": retention_source_signature(base),
        "current_sources": current_retention_source_signature(base),
        "manifest": raw_segments.manifest_signature(base),
        "pending_turn_state": pending_turn_state_signature(base),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def retention_index_path(base: pathlib.Path) -> pathlib.Path:
    return base / RETENTION_INDEX_RELATIVE_PATH


def retention_signature_payload(signature: tuple[tuple[str, bool, int, int], ...]) -> list[list[Any]]:
    return [[path, delete_when_empty, size, mtime_ns] for path, delete_when_empty, size, mtime_ns in signature]


def unix_day_start(value: float) -> int:
    date = datetime.fromtimestamp(float(value), tz=timezone.utc).date()
    return int(datetime(date.year, date.month, date.day, tzinfo=timezone.utc).timestamp())


def default_retention_cutoff_unix(now_unix: float | None = None, days: int = 7) -> int:
    now = time.time() if now_unix is None else float(now_unix)
    return unix_day_start(now - (int(days) * 86400))


def scan_retention_source_for_index(path: pathlib.Path, *, delete_when_empty: bool) -> dict[str, Any]:
    return scan_retention_source_for_index_from_offset(path, 0, delete_when_empty=delete_when_empty)


def retention_source_tail(path: pathlib.Path, source_size: int, max_bytes: int = 4096) -> dict[str, Any]:
    if source_size <= 0 or path.suffix == ".gz":
        return {"tail_size": 0, "tail_sha256": None}
    tail_size = min(int(source_size), max_bytes)
    try:
        with path.open("rb") as handle:
            handle.seek(int(source_size) - tail_size)
            payload = handle.read(tail_size)
    except OSError:
        return {"tail_size": 0, "tail_sha256": None}
    return {"tail_size": tail_size, "tail_sha256": hashlib.sha256(payload).hexdigest()}


def scan_retention_source_for_index_from_offset(path: pathlib.Path, offset: int, *, delete_when_empty: bool) -> dict[str, Any]:
    days: dict[int, int] = {}
    day_bytes: dict[int, int] = {}
    scanned_rows = 0
    scanned_bytes = 0
    undated_rows = 0
    if path.exists() and path.is_file():
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            if offset > 0 and path.suffix != ".gz":
                handle.seek(offset)
            for line in handle:
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                scanned_rows += 1
                line_bytes = len(line.encode("utf-8"))
                scanned_bytes += line_bytes
                row_time = retention_row_time(parsed)
                if row_time is None:
                    undated_rows += 1
                    continue
                day = unix_day_start(row_time)
                days[day] = days.get(day, 0) + 1
                day_bytes[day] = day_bytes.get(day, 0) + line_bytes
    try:
        stat = path.stat()
        source_size = stat.st_size if path.is_file() else 0
        source_mtime_ns = stat.st_mtime_ns
    except OSError:
        source_size = 0
        source_mtime_ns = 0
    return {
        "path": str(path),
        "delete_when_empty": delete_when_empty,
        "source_size": source_size,
        "source_mtime_ns": source_mtime_ns,
        **retention_source_tail(path, source_size),
        "scanned_rows": scanned_rows,
        "scanned_bytes": scanned_bytes,
        "undated_rows": undated_rows,
        "days": [[day, count, day_bytes.get(day, 0)] for day, count in sorted(days.items())],
    }


def write_retention_index(base: pathlib.Path, data: dict[str, Any]) -> None:
    path = retention_index_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def rebuild_retention_index(token_usage_root: pathlib.Path | str) -> dict[str, Any]:
    base = pathlib.Path(token_usage_root).expanduser()
    signature = retention_index_signature(base)
    return build_retention_index(base, signature)


def refresh_retention_index_for_current_sources(token_usage_root: pathlib.Path | str) -> dict[str, Any]:
    base = pathlib.Path(token_usage_root).expanduser()
    signature = retention_index_signature(base)
    return refresh_retention_index(base, signature)


def build_retention_index(base: pathlib.Path, signature: tuple[tuple[str, bool, int, int], ...]) -> dict[str, Any]:
    sources = [
        scan_retention_source_for_index(pathlib.Path(path), delete_when_empty=delete_when_empty)
        for path, delete_when_empty, _size, _mtime_ns in signature
    ]
    data = {
        "schema_version": RETENTION_INDEX_SCHEMA_VERSION,
        "base": str(base.resolve()),
        "built_at_unix": time.time(),
        "signature": retention_signature_payload(signature),
        "sources": sources,
    }
    write_retention_index(base, data)
    return data


def read_retention_index(base: pathlib.Path) -> dict[str, Any] | None:
    path = retention_index_path(base)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != RETENTION_INDEX_SCHEMA_VERSION:
        return None
    if data.get("base") != str(base.resolve()):
        return None
    sources = data.get("sources")
    return data if isinstance(sources, list) else None


def load_retention_index(base: pathlib.Path, signature: tuple[tuple[str, bool, bool, int, int], ...]) -> dict[str, Any] | None:
    data = read_retention_index(base)
    if data is None or data.get("signature") != retention_signature_payload(signature):
        return None
    return data


def merge_retention_source(previous: dict[str, Any], appended: dict[str, Any], *, source_size: int, source_mtime_ns: int) -> dict[str, Any]:
    days: dict[int, int] = {}
    day_bytes: dict[int, int] = {}
    for source in (previous, appended):
        for item in source.get("days") or []:
            if not isinstance(item, list) or len(item) < 2:
                continue
            try:
                day = int(item[0])
                count = int(item[1])
                bytes_count = int(item[2]) if len(item) >= 3 else 0
            except (TypeError, ValueError):
                continue
            days[day] = days.get(day, 0) + count
            day_bytes[day] = day_bytes.get(day, 0) + bytes_count
    return {
        "path": str(previous.get("path") or appended.get("path") or ""),
        "delete_when_empty": bool(previous.get("delete_when_empty")),
        "source_size": source_size,
        "source_mtime_ns": source_mtime_ns,
        **retention_source_tail(pathlib.Path(str(previous.get("path") or appended.get("path") or "")), source_size),
        "scanned_rows": int(previous.get("scanned_rows") or 0) + int(appended.get("scanned_rows") or 0),
        "scanned_bytes": int(previous.get("scanned_bytes") or 0) + int(appended.get("scanned_bytes") or 0),
        "undated_rows": int(previous.get("undated_rows") or 0) + int(appended.get("undated_rows") or 0),
        "days": [[day, count, day_bytes.get(day, 0)] for day, count in sorted(days.items())],
    }


def can_increment_source(previous: dict[str, Any], path: pathlib.Path, *, delete_when_empty: bool, source_size: int) -> bool:
    if path.suffix == ".gz":
        return False
    previous_size = int(previous.get("source_size") or 0)
    previous_tail_size = int(previous.get("tail_size") or 0)
    previous_tail_sha = previous.get("tail_sha256")
    if previous_size > 0 and (previous_tail_size <= 0 or not isinstance(previous_tail_sha, str)):
        return False
    if previous_tail_size > 0:
        try:
            with path.open("rb") as handle:
                handle.seek(previous_size - previous_tail_size)
                payload = handle.read(previous_tail_size)
        except OSError:
            return False
        if hashlib.sha256(payload).hexdigest() != previous_tail_sha:
            return False
    return (
        str(previous.get("path") or "") == str(path)
        and bool(previous.get("delete_when_empty")) == delete_when_empty
        and previous_size <= source_size
    )


def refresh_retention_index(base: pathlib.Path, signature: tuple[tuple[str, bool, int, int], ...]) -> dict[str, Any]:
    previous = read_retention_index(base)
    if previous is None:
        return build_retention_index(base, signature)
    if previous.get("signature") == retention_signature_payload(signature):
        return previous

    previous_by_path = {str(source.get("path") or ""): source for source in previous.get("sources", []) if isinstance(source, dict)}
    sources = []
    for path_text, delete_when_empty, source_size, source_mtime_ns in signature:
        path = pathlib.Path(path_text)
        old_source = previous_by_path.get(path_text)
        if (
            old_source
            and int(old_source.get("source_size") or 0) == source_size
            and int(old_source.get("source_mtime_ns") or 0) == source_mtime_ns
            and bool(old_source.get("delete_when_empty")) == delete_when_empty
        ):
            sources.append(old_source)
        elif old_source and can_increment_source(old_source, path, delete_when_empty=delete_when_empty, source_size=source_size):
            offset = int(old_source.get("source_size") or 0)
            appended = scan_retention_source_for_index_from_offset(path, offset, delete_when_empty=delete_when_empty)
            sources.append(merge_retention_source(old_source, appended, source_size=source_size, source_mtime_ns=source_mtime_ns))
        else:
            sources.append(scan_retention_source_for_index(path, delete_when_empty=delete_when_empty))

    data = {
        "schema_version": RETENTION_INDEX_SCHEMA_VERSION,
        "base": str(base.resolve()),
        "built_at_unix": time.time(),
        "signature": retention_signature_payload(signature),
        "sources": sources,
    }
    write_retention_index(base, data)
    return data


def retention_preview_from_index(index: dict[str, Any], cutoff_unix: float) -> dict[str, Any] | None:
    cutoff = float(cutoff_unix)
    cutoff_day = unix_day_start(cutoff)
    if abs(cutoff - cutoff_day) > 0.000001:
        return None
    files = []
    for source in index.get("sources", []):
        if not isinstance(source, dict):
            continue
        path = str(source.get("path") or "")
        scanned_rows = int(source.get("scanned_rows") or 0)
        scanned_bytes = int(source.get("scanned_bytes") or 0)
        deletable_rows = 0
        deletable_row_bytes = 0
        for item in source.get("days") or []:
            if not isinstance(item, list) or len(item) < 2:
                continue
            try:
                day = item[0]
                count = item[1]
                bytes_count = item[2] if len(item) >= 3 else 0
                if float(day) < cutoff:
                    deletable_rows += int(count)
                    deletable_row_bytes += int(bytes_count)
            except (TypeError, ValueError):
                continue
        source_size = int(source.get("source_size") or 0)
        files.append(
            {
                "path": path,
                "source_size": source_size,
                "source_mtime_ns": int(source.get("source_mtime_ns") or 0),
                "scanned_rows": scanned_rows,
                "deletable_rows": deletable_rows,
                "deletable_bytes": estimated_delete_bytes(path, source_size, deletable_row_bytes, scanned_bytes),
                "affected": deletable_rows > 0,
            }
        )
    scanned_rows = sum(int(item["scanned_rows"]) for item in files)
    deletable_rows = sum(int(item["deletable_rows"]) for item in files)
    deletable_bytes = sum(int(item["deletable_bytes"]) for item in files)
    return {
        "cutoff_unix": cutoff,
        "scanned_rows": scanned_rows,
        "deletable_rows": deletable_rows,
        "deletable_bytes": deletable_bytes,
        "kept_rows": scanned_rows - deletable_rows,
        "affected_files": sum(1 for item in files if item["affected"]),
        "files": files,
        "from_index": True,
        "index_built_at_unix": index.get("built_at_unix"),
    }


def retention_preview_profile_fields(
    *,
    preview_source: str,
    index_refreshed: bool,
    files: list[dict[str, Any]],
    current_signature: tuple[tuple[str, bool, int, int], ...],
) -> dict[str, Any]:
    current_paths = {str(path) for path, _delete_when_empty, _size, _mtime_ns in current_signature}
    scanned_current_files = [
        file
        for file in files
        if str(file.get("path") or "") in current_paths and preview_source == "fallback_scan"
    ]
    return {
        "preview_source": preview_source,
        "retention_index_refreshed": bool(index_refreshed),
        "current_scan_files": len(scanned_current_files),
        "current_scan_bytes": sum(int(file.get("source_size") or 0) for file in scanned_current_files),
    }


def retention_preview(token_usage_root: pathlib.Path | str, cutoff_unix: float, *, refresh_index: bool = True) -> dict[str, Any]:
    base = pathlib.Path(token_usage_root).expanduser()
    cutoff = float(cutoff_unix)
    raw_segments.strict_read_manifest(base)
    if raw_segments.read_pending_rotation(base) is not None:
        raise raw_segments.ManifestError("pending rotation must be resolved before cleanup preview")
    apply_marker = raw_segments.read_apply_marker(base)
    if apply_marker is not None and apply_marker.get("phase") != "unlink_pending":
        raise raw_segments.ManifestError("pending segment apply must be resolved before cleanup preview")
    signature = retention_source_signature(base)
    current_signature = current_retention_source_signature(base)
    manifest_signature = raw_segments.manifest_signature(base)
    cache_key = (str(base.resolve()), cutoff, signature, current_signature, manifest_signature)
    cached = RETENTION_PREVIEW_CACHE.get(cache_key)
    if cached is not None:
        return json.loads(json.dumps(cached))
    manifest_preview = raw_segments.retention_preview_from_manifest(base, cutoff)
    if manifest_preview is not None:
        tracked_paths: set[str] = set()
        source_previews = []
        tracked_paths.update(str(path) for path in manifest_preview.get("_tracked_paths", []))
        source_previews.append(manifest_preview)
        index_signature = retention_index_signature(base, tracked_paths)
        existing_index = load_retention_index(base, index_signature)
        had_index = existing_index is not None
        indexed = refresh_retention_index(base, index_signature) if refresh_index else existing_index
        indexed_preview = retention_preview_from_index(indexed, cutoff) if indexed is not None else None
        preview_source = "index" if had_index else "refreshed_index"
        indexed_files = (
            indexed_preview["files"]
            if indexed_preview is not None
            else [
                preview_jsonl_for_retention(pathlib.Path(path), cutoff)
                for path, _delete_when_empty, _size, _mtime_ns in index_signature
            ]
        )
        if indexed_preview is None:
            preview_source = "fallback_scan"
        files = [file for preview in source_previews for file in preview["files"]]
        files.extend(indexed_files)
        scanned_rows = sum(int(item["scanned_rows"]) for item in files)
        deletable_rows = sum(int(item["deletable_rows"]) for item in files)
        deletable_bytes = sum(int(item["deletable_bytes"]) for item in files)
        result = {
            "cutoff_unix": cutoff,
            "scanned_rows": scanned_rows,
            "deletable_rows": deletable_rows,
            "deletable_bytes": deletable_bytes,
            "kept_rows": scanned_rows - deletable_rows,
            "affected_files": sum(1 for item in files if item["affected"]),
            "files": files,
            "from_manifest": manifest_preview is not None,
            "from_current": bool(current_signature),
            **retention_preview_profile_fields(
                preview_source=preview_source,
                index_refreshed=not had_index,
                files=files,
                current_signature=current_signature,
            ),
        }
        RETENTION_PREVIEW_CACHE[cache_key] = json.loads(json.dumps(result))
        if len(RETENTION_PREVIEW_CACHE) > RETENTION_PREVIEW_CACHE_LIMIT:
            oldest_key = next(iter(RETENTION_PREVIEW_CACHE))
            RETENTION_PREVIEW_CACHE.pop(oldest_key, None)
        return result
    index_signature = retention_index_signature(base)
    existing_index = load_retention_index(base, index_signature)
    had_index = existing_index is not None
    index = refresh_retention_index(base, index_signature) if refresh_index else existing_index
    indexed = retention_preview_from_index(index, cutoff) if index is not None else None
    if indexed is not None:
        indexed["from_current"] = bool(current_signature)
        indexed.update(
            retention_preview_profile_fields(
                preview_source="index" if had_index else "refreshed_index",
                index_refreshed=not had_index,
                files=indexed.get("files", []),
                current_signature=current_signature,
            )
        )
        RETENTION_PREVIEW_CACHE[cache_key] = json.loads(json.dumps(indexed))
        if len(RETENTION_PREVIEW_CACHE) > RETENTION_PREVIEW_CACHE_LIMIT:
            oldest_key = next(iter(RETENTION_PREVIEW_CACHE))
            RETENTION_PREVIEW_CACHE.pop(oldest_key, None)
        return indexed
    files = [
        preview_jsonl_for_retention(pathlib.Path(path), cutoff)
        for path, _delete_when_empty, _size, _mtime_ns in index_signature
    ]
    scanned_rows = sum(int(item["scanned_rows"]) for item in files)
    deletable_rows = sum(int(item["deletable_rows"]) for item in files)
    deletable_bytes = sum(int(item["deletable_bytes"]) for item in files)
    result = {
        "cutoff_unix": cutoff,
        "scanned_rows": scanned_rows,
        "deletable_rows": deletable_rows,
        "deletable_bytes": deletable_bytes,
        "kept_rows": scanned_rows - deletable_rows,
        "affected_files": sum(1 for item in files if item["affected"]),
        "files": files,
        "from_index": False,
        "from_current": bool(current_signature),
        **retention_preview_profile_fields(
            preview_source="fallback_scan",
            index_refreshed=not had_index,
            files=files,
            current_signature=current_signature,
        ),
    }
    RETENTION_PREVIEW_CACHE[cache_key] = json.loads(json.dumps(result))
    if len(RETENTION_PREVIEW_CACHE) > RETENTION_PREVIEW_CACHE_LIMIT:
        oldest_key = next(iter(RETENTION_PREVIEW_CACHE))
        RETENTION_PREVIEW_CACHE.pop(oldest_key, None)
    return result


def ensure_service_owned_output(base: pathlib.Path, db_path: pathlib.Path) -> pathlib.Path:
    resolved_base = pathlib.Path(base).expanduser().resolve()
    resolved_db = pathlib.Path(db_path).expanduser().resolve()
    analytics_dir = resolved_base / "analytics"
    if resolved_db == analytics_dir:
        raise ValueError(f"retention output must be a database file, not a directory: {resolved_db}")
    if not resolved_db.is_relative_to(analytics_dir):
        raise ValueError(f"retention output must be under {analytics_dir}: {resolved_db}")
    if resolved_db.exists() and not resolved_db.is_file():
        raise ValueError(f"retention output must be a database file, not a directory: {resolved_db}")
    return resolved_db


def ensure_service_owned_file(base: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    resolved_base = pathlib.Path(base).expanduser().resolve(strict=False)
    expanded = pathlib.Path(path).expanduser()
    resolved = expanded.resolve(strict=False)
    try:
        resolved.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError(f"service output must be under {resolved_base}: {resolved}") from exc
    if expanded.is_symlink() or expanded.parent.is_symlink():
        raise ValueError(f"service output must not be a symlink: {expanded}")
    if expanded.exists() and not expanded.is_file():
        raise ValueError(f"service output must be a regular file: {expanded}")
    return expanded


def reset_derived_outputs(base: pathlib.Path, db_path: pathlib.Path) -> dict[str, Any]:
    base = pathlib.Path(base).expanduser()
    db_path = ensure_service_owned_output(base, pathlib.Path(db_path).expanduser())
    targets = [
        ensure_service_owned_file(base, base / "normalized" / "prompt-usage.normalized.jsonl"),
        ensure_service_owned_file(base, base / "normalized" / "normalize-state.json"),
        ensure_service_owned_file(base, db_path),
    ]
    removed: list[str] = []
    for path in targets:
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            continue
    return {"removed": removed, "count": len(removed)}


def preflight_delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    base = pathlib.Path(token_usage_root).expanduser()
    cutoff = float(cutoff_unix)
    recover_retention_cleanup(base)
    raw_segments.strict_read_manifest(base)
    raw_segments.validate_current_pointer_entries(base)
    if raw_segments.read_pending_rotation(base) is not None:
        raise raw_segments.ManifestError("pending rotation must be resolved before retention prune reset")
    if raw_segments.read_apply_marker(base) is not None:
        raise raw_segments.ManifestError("pending segment apply must be resolved before retention prune reset")
    segment_preflight = raw_segments.preflight_segments_older_than(base, cutoff)
    tracked_paths = raw_segments.manifest_tracked_paths(base)
    for path, delete_when_empty in retention_sources(base):
        if resolved_retention_source_path(path) in tracked_paths:
            continue
        plan_jsonl_for_retention(path, cutoff, delete_when_empty=delete_when_empty)
    return {"base": str(base), "cutoff": cutoff, "segments": segment_preflight}


def plan_delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    RETENTION_PREVIEW_CACHE.clear()
    base = pathlib.Path(token_usage_root).expanduser()
    recover_retention_cleanup(base)
    try:
        retention_index_path(base).unlink()
    except FileNotFoundError:
        pass
    cutoff = float(cutoff_unix)
    raw_segments.reconcile_apply_marker(base)
    raw_segments.reconcile_pending_rotation(base)
    raw_segments.rotate_all_current_segments(base)
    segment_plan = raw_segments.plan_segments_older_than(base, cutoff)
    pending_turn_state_plan = plan_pending_turn_state_for_retention(base, cutoff)
    tracked_paths = raw_segments.manifest_tracked_paths(base)
    plans: list[dict[str, Any]] = []
    pruned_turns: list[dict[str, Any]] = []
    pruned_turns.extend(item for item in segment_plan.get("deleted_turns", []) if isinstance(item, dict))
    for path, delete_when_empty in retention_sources(base):
        if resolved_retention_source_path(path) in tracked_paths:
            continue
        plan = plan_jsonl_for_retention(path, cutoff, delete_when_empty=delete_when_empty)
        pruned_turns.extend(item for item in plan.get("deleted_turns", []) if isinstance(item, dict))
        plans.append(plan)
    return {"base": str(base), "cutoff": cutoff, "segments": segment_plan, "untracked": plans, "pending_turn_state": pending_turn_state_plan, "pruned_turns": pruned_turns}


def validate_delete_logs_older_than_plan(plan: dict[str, Any]) -> dict[str, Any]:
    base = pathlib.Path(str(plan["base"]))
    segment_plan = plan.get("segments", {})
    if isinstance(segment_plan, dict):
        raw_segments.validate_segment_plans(base, segment_plan)
    for item in plan.get("untracked", []):
        if not isinstance(item, dict) or int(item.get("deleted_rows") or 0) <= 0:
            continue
        path = pathlib.Path(str(item.get("path") or ""))
        verify_retention_source_signature(path, item.get("_source_signature") if isinstance(item.get("_source_signature"), dict) else None)
    pending_turn_state_plan = plan.get("pending_turn_state", {})
    if isinstance(pending_turn_state_plan, dict):
        target_signatures = pending_turn_state_plan.get("target_signatures") if isinstance(pending_turn_state_plan.get("target_signatures"), dict) else {}
        for text in pending_turn_state_plan.get("targets") or []:
            path = pathlib.Path(str(text))
            if not path.exists():
                raise raw_segments.ManifestError(f"pending turn state changed after planning: {path}")
            signature = target_signatures.get(str(path)) if isinstance(target_signatures, dict) else None
            verify_pending_turn_state_file_signature(path, signature)
    return {"ok": True}


def apply_delete_logs_older_than_plan(plan: dict[str, Any]) -> dict[str, Any]:
    base = pathlib.Path(str(plan["base"]))
    cutoff = float(plan["cutoff"])
    untracked_plans = plan.get("untracked", [])
    segment_plan = plan.get("segments", {})
    pending_turn_state_plan = plan.get("pending_turn_state", {})
    pruned_turns = [item for item in plan.get("pruned_turns", []) if isinstance(item, dict)]
    staged_pruned_turn_state = stage_pruned_turn_state(base, cutoff, pruned_turns)
    source_mutated = False
    write_cleanup_retention_job(
        base,
        {
            "phase": "planned",
            "cutoff_unix": cutoff,
            "deleted_rows": int(segment_plan.get("deleted_rows") or 0)
            + sum(int(item.get("deleted_rows") or 0) for item in untracked_plans if isinstance(item, dict)),
            "physical_delete_pending": False,
        },
    )
    try:
        segment_apply = raw_segments.apply_segment_plans(base, segment_plan)
        if int(segment_apply.get("deleted_files") or 0) > 0 or int(segment_apply.get("rewritten_files") or 0) > 0:
            source_mutated = True
        if bool(segment_apply.get("physical_delete_pending")):
            write_cleanup_retention_job(
                base,
                {
                    "phase": "physical_delete_pending",
                    "cutoff_unix": cutoff,
                    "physical_delete_pending": True,
                    "pending_files": int(segment_apply.get("pending_files") or 0),
                    "unlink_errors": segment_apply.get("unlink_errors") or [],
                },
            )
        else:
            write_cleanup_retention_job(
                base,
                {
                    "phase": "logical_delete_committed",
                    "cutoff_unix": cutoff,
                    "physical_delete_pending": False,
                },
            )
        files = []
        for item in untracked_plans:
            if int(item.get("deleted_rows") or 0) > 0:
                source_mutated = True
                files.append(apply_retention_plan(item))
            else:
                files.append(public_retention_result(item))
        pending_turn_state_apply = apply_pending_turn_state_plan(pending_turn_state_plan)
        commit_pruned_turn_state(base, staged_pruned_turn_state)
    except Exception:
        if not source_mutated:
            discard_pruned_turn_state_stage(staged_pruned_turn_state)
        failed = read_cleanup_retention_job(base) or {}
        failed["phase"] = "failed"
        failed["failed_stage"] = "apply"
        failed["error"] = repr(sys.exc_info()[1])
        write_cleanup_retention_job(base, failed)
        raise
    deleted_rows = sum(int(item["deleted_rows"]) for item in files) + int(segment_plan.get("deleted_rows") or 0)
    scanned_rows = sum(int(item["scanned_rows"]) for item in files) + int(segment_plan.get("scanned_rows") or 0)
    before_bytes = sum(int(item["before_bytes"]) for item in files)
    after_bytes = sum(int(item["after_bytes"]) for item in files)
    segment_deleted_files = int(segment_apply.get("deleted_files") or 0)
    segment_rewritten_files = int(segment_apply.get("rewritten_files") or 0)
    deleted_state_files = int(pending_turn_state_apply.get("deleted_files") or 0)
    deleted_state_bytes = int(pending_turn_state_apply.get("deleted_bytes") or 0)
    physical_delete_pending = bool(segment_apply.get("physical_delete_pending"))
    if deleted_rows > 0 or deleted_state_files > 0 or physical_delete_pending:
        write_cleanup_retention_job(
            base,
            {
                "phase": "physical_delete_pending" if physical_delete_pending else "derived_rebuild_required",
                "cutoff_unix": cutoff,
                "deleted_rows": deleted_rows,
                "derived_rebuild_required": True,
                "physical_delete_pending": physical_delete_pending,
                "pending_files": int(segment_apply.get("pending_files") or 0),
            },
        )
    else:
        clear_cleanup_retention_job(base)
    return {
        "cutoff_unix": cutoff,
        "scanned_rows": scanned_rows,
        "deleted_rows": deleted_rows,
        "kept_rows": scanned_rows - deleted_rows,
        "deleted_bytes": max(0, before_bytes - after_bytes) + int(segment_plan.get("deleted_bytes") or 0) + deleted_state_bytes,
        "physical_delete_pending": physical_delete_pending,
        "pending_files": int(segment_apply.get("pending_files") or 0),
        "unlink_errors": segment_apply.get("unlink_errors") or [],
        "deleted_state_files": deleted_state_files,
        "deleted_state_bytes": deleted_state_bytes,
        "deleted_turns": len(pruned_turns),
        "rewritten_files": sum(1 for item in files if item["rewritten"] and not item["deleted_file"]) + segment_rewritten_files,
        "deleted_files": sum(1 for item in files if item["deleted_file"]) + segment_deleted_files,
        "changed_files": sum(1 for item in files if item["rewritten"]) + segment_deleted_files + segment_rewritten_files + deleted_state_files,
        "files": files,
    }


def delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    preflight_delete_logs_older_than(token_usage_root, cutoff_unix)
    return apply_delete_logs_older_than_plan(plan_delete_logs_older_than(token_usage_root, cutoff_unix))
