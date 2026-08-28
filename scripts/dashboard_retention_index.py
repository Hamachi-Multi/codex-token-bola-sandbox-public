"""Persistent retention preview index and read-only source scanning."""

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

from dashboard_cleanup_common import parse_row_time

RETENTION_INDEX_SCHEMA_VERSION = 4
RETENTION_INDEX_RELATIVE_PATH = pathlib.Path("state") / "cleanup-retention-index.json"


def retention_row_time(row: dict[str, Any]) -> float | None:
    keys = ("started_at", "captured_at", "stopped_at", "timestamp")
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


def preview_retention_source(path: pathlib.Path, cutoff_unix: float) -> dict[str, Any]:
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


def retention_index_signature(
    base: pathlib.Path,
    tracked_paths: set[str] | None = None,
) -> tuple[tuple[str, bool, int, int], ...]:
    excluded = tracked_paths or set()
    signature = current_retention_source_signature(base)
    return tuple(item for item in signature if str(pathlib.Path(item[0]).resolve(strict=False)) not in excluded)


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
        scan_retention_source_for_index(pathlib.Path(path), delete_when_empty=delete_when_empty) for path, delete_when_empty, _size, _mtime_ns in signature
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


def load_retention_index(base: pathlib.Path, signature: tuple[tuple[str, bool, int, int], ...]) -> dict[str, Any] | None:
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
    return str(previous.get("path") or "") == str(path) and bool(previous.get("delete_when_empty")) == delete_when_empty and previous_size <= source_size


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
