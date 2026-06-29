"""Shared raw segment constants, locks, and row scanning helpers."""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import json
import os
import pathlib
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterator

MANIFEST_RELATIVE_PATH = pathlib.Path("state") / "raw-segments-manifest.json"
CURRENT_POINTER_RELATIVE_PATH = pathlib.Path("state") / "current-raw-segments.json"
PENDING_ROTATION_RELATIVE_PATH = pathlib.Path("state") / "raw-segment-rotation-pending.json"
PENDING_APPLY_RELATIVE_PATH = pathlib.Path("state") / "raw-segment-apply-pending.json"
RAW_SEGMENT_LOCK_RELATIVE_PATH = pathlib.Path("state") / "raw-segment.lock"
RAW_SEGMENT_MANIFEST_LOCK_RELATIVE_PATH = pathlib.Path("state") / "raw-segment-manifest.lock"
PROMPT_RAW_NAME = "prompt-usage.raw.jsonl"
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class ManifestError(RuntimeError):
    pass


def manifest_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / MANIFEST_RELATIVE_PATH


def current_pointer_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / CURRENT_POINTER_RELATIVE_PATH


def pending_rotation_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / PENDING_ROTATION_RELATIVE_PATH


def segment_apply_marker_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / PENDING_APPLY_RELATIVE_PATH


def raw_segment_lock_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / RAW_SEGMENT_LOCK_RELATIVE_PATH


def raw_segment_manifest_lock_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / RAW_SEGMENT_MANIFEST_LOCK_RELATIVE_PATH


def fsync_dir(path: pathlib.Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomic(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        tmp.replace(path)
        path.chmod(0o600)
        fsync_dir(path.parent)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _thread_lock(path: pathlib.Path) -> threading.Lock:
    key = str(path.expanduser().resolve(strict=False))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _file_lock(path: pathlib.Path) -> Iterator[None]:
    lock = _thread_lock(path)
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


@contextlib.contextmanager
def acquire_raw_segment_lock(base: pathlib.Path) -> Iterator[None]:
    with _file_lock(raw_segment_lock_path(base)):
        yield


@contextlib.contextmanager
def acquire_raw_segment_manifest_lock(base: pathlib.Path) -> Iterator[None]:
    with _file_lock(raw_segment_manifest_lock_path(base)):
        yield


def raw_segment_lock_available(base: pathlib.Path) -> bool:
    path = raw_segment_lock_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _thread_lock(path)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        return False
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        return True
    finally:
        os.close(fd)
        lock.release()



def read_segment_payload(path: pathlib.Path) -> bytes:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as handle:
            return handle.read()
    return path.read_bytes()



def _parse_time(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def row_time(row: dict[str, Any], *, kind: str) -> float | None:
    if kind != "prompt_usage":
        raise ManifestError(f"unsupported raw segment kind: {kind}")
    keys = ("captured_at", "stopped_at", "started_at", "timestamp")
    for key in keys:
        parsed = _parse_time(row.get(key))
        if parsed is not None:
            return parsed
    return None


def scan_jsonl_bytes(payload: bytes, *, kind: str) -> dict[str, Any]:
    rows = 0
    undated_rows = 0
    corrupt_rows = 0
    unknown_rows = 0
    min_time: float | None = None
    max_time: float | None = None
    days: dict[int, int] = {}
    day_bytes: dict[int, int] = {}
    for raw_line in payload.splitlines():
        line_bytes = len(raw_line) + 1
        if not raw_line:
            continue
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            rows += 1
            corrupt_rows += 1
            continue
        if not isinstance(parsed, dict):
            rows += 1
            unknown_rows += 1
            continue
        rows += 1
        parsed_time = row_time(parsed, kind=kind)
        if parsed_time is None:
            undated_rows += 1
            continue
        min_time = parsed_time if min_time is None else min(min_time, parsed_time)
        max_time = parsed_time if max_time is None else max(max_time, parsed_time)
        day = int(datetime.fromtimestamp(parsed_time, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        days[day] = days.get(day, 0) + 1
        day_bytes[day] = day_bytes.get(day, 0) + line_bytes
    return {
        "rows": rows,
        "undated_rows": undated_rows,
        "corrupt_rows": corrupt_rows,
        "unknown_rows": unknown_rows,
        "min_time_unix": min_time,
        "max_time_unix": max_time,
        "days": [[day, count, day_bytes.get(day, 0)] for day, count in sorted(days.items())],
    }
