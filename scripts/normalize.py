#!/usr/bin/env python3
"""Normalize raw Codex Token Bola logs for analysis."""

from __future__ import annotations

import json
import os
import pathlib
import time
import argparse
import gzip
import sys
from functools import lru_cache
from typing import Any, Callable


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_lock
import service_paths
import raw_segments
import cancel_control
import progress_control
import quarantine_health
import normalize_publish
import transcript_parser
import turn_capture
import turn_lifecycle
import turn_resolution

RUNTIME_PATHS = service_paths.resolve_runtime_paths()
CODEX_DIR = RUNTIME_PATHS.codex_dir
BASE_DIR = RUNTIME_PATHS.output_dir
OUTPUT_LAYOUT = service_paths.OutputLayout(BASE_DIR)
NORMALIZED_LOG = OUTPUT_LAYOUT.normalized_log
BAD_LOG = OUTPUT_LAYOUT.bad_dir / "prompt-usage.bad.jsonl"
STATE_FILE = OUTPUT_LAYOUT.normalize_state
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
NORMALIZE_LOGIC_VERSION = 7
TRANSCRIPT_LIFECYCLE_CACHE_MAXSIZE = 256
QUARANTINE_RESULTS: list[dict[str, Any]] = []


class PendingPublishRecoveryError(RuntimeError):
    pass


class TranscriptChangedDuringScan(RuntimeError):
    pass


def pending_publish_recovery_error_payload(exc: PendingPublishRecoveryError) -> dict[str, Any]:
    return {
        "error": "normalize_pending_publish_recovery_failed",
        "message": str(exc),
        "marker_path": str(pending_publish_file()),
        "recovery_required": True,
    }


def token_usage_root() -> pathlib.Path:
    return STATE_FILE.parent.parent


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def source_priority_for_path(path: pathlib.Path) -> int:
    if path.name.startswith(f"{raw_segments.PROMPT_RAW_NAME}."):
        return 2
    return 0


def zero_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_KEYS}


def normalize_usage(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    usage = {key: safe_int(source.get(key)) for key in USAGE_KEYS}
    usage["non_cached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    usage["consistency_total_equals_input_plus_output"] = (
        usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    )
    return usage


def transcript_file_version(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def transcript_snapshot_matches(
    stat_result: os.stat_result,
    expected: tuple[int, int, int, int, int],
) -> bool:
    device, inode, size, mtime_ns, ctime_ns = expected
    if stat_result.st_dev != device or stat_result.st_ino != inode or stat_result.st_size < size:
        return False
    if stat_result.st_size > size:
        return True
    return stat_result.st_mtime_ns == mtime_ns and stat_result.st_ctime_ns == ctime_ns


@lru_cache(maxsize=TRANSCRIPT_LIFECYCLE_CACHE_MAXSIZE)
def cached_transcript_lifecycle_index(
    transcript_path: str,
    version: tuple[int, int, int, int, int],
) -> dict[str, Any]:
    path = pathlib.Path(transcript_path)
    expected_size = version[2]
    reducer = turn_lifecycle.LifecycleIndexReducer()

    with path.open("rb") as handle:
        if not transcript_snapshot_matches(os.fstat(handle.fileno()), version):
            raise TranscriptChangedDuringScan(str(path))
        line_no = 0
        while handle.tell() < expected_size:
            line_start = handle.tell()
            line = handle.readline(expected_size - line_start + 1)
            if not line:
                break
            if handle.tell() > expected_size:
                break
            line_no += 1
            if line_no == 1 or line_no % 200 == 0:
                cancel_control.check_cancelled("normalize", f"lifecycle:{path.name}:{line_no}")
                source_progress = handle.tell() / expected_size if expected_size > 0 else 0.0
                progress_control.write_progress(
                    phase="normalize",
                    phase_index=0,
                    checkpoint=f"lifecycle:{path.name}:{line_no}",
                    phase_progress=progress_control.clamp(source_progress),
                )
            item, parse_error = transcript_parser.parse_transcript_object(line)
            if parse_error:
                reducer.mark_parse_error()
                continue
            if item.get("type") != "event_msg":
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                reducer.mark_parse_error()
                continue
            reducer.feed({"item": item, "line_start": line_start, "next_offset": handle.tell()})
        if not transcript_snapshot_matches(os.fstat(handle.fileno()), version):
            raise TranscriptChangedDuringScan(str(path))

    accumulators = reducer.finish()
    turns = {
        turn_id: turn_lifecycle.full_lifecycle_snapshot(
            accumulator,
            path=str(path),
            parse_error_seen=accumulator.parse_error_seen,
            include_model_calls=False,
            include_latest_fields=False,
        )
        for turn_id, accumulator in accumulators.items()
    }
    return {"_turns": turns, "_parse_error_seen": reducer.parse_error_seen}


def transcript_lifecycle_index(transcript_path: str) -> dict[str, Any]:
    path = pathlib.Path(transcript_path).expanduser().resolve(strict=False)
    for _attempt in range(2):
        try:
            version = transcript_file_version(path.stat())
        except FileNotFoundError:
            return {"_error": {"found": False, "reason": "transcript_missing", "path": str(path)}}
        except OSError as exc:
            return {"_error": {"found": False, "reason": "read_error", "error": repr(exc), "path": str(path)}}
        try:
            return cached_transcript_lifecycle_index(str(path), version)
        except TranscriptChangedDuringScan:
            continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            return {"_error": {"found": False, "reason": "read_error", "error": repr(exc), "path": str(path)}}
    return {
        "_error": {
            "found": False,
            "reason": "transcript_changed_during_scan",
            "path": str(path),
        }
    }


def task_lifecycle_token_usage(transcript_path: str | None, turn_id: str) -> dict[str, Any]:
    if not transcript_path:
        return {"found": False, "reason": "missing_transcript_path"}
    index = transcript_lifecycle_index(transcript_path)
    error = index.get("_error")
    if isinstance(error, dict):
        return error
    turns = index.get("_turns")
    if not isinstance(turns, dict):
        return {"found": False, "reason": "task_started_missing", "path": str(pathlib.Path(transcript_path).expanduser())}
    snapshot = turns.get(turn_id)
    if isinstance(snapshot, dict):
        return snapshot
    return {
        "found": False,
        "reason": "task_started_missing",
        "path": str(pathlib.Path(transcript_path).expanduser()),
        "parse_error_seen": bool(index.get("_parse_error_seen")),
    }


def recover_missing_start_state_lifecycle(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("turn_status") != "incomplete" or row.get("lifecycle_end_reason") != "missing_start_state":
        return row
    turn_id = str(row.get("turn_id") or "")
    if not turn_id:
        return row
    snapshot = task_lifecycle_token_usage(row.get("transcript_path"), turn_id)
    if not snapshot.get("found"):
        return row
    status = str(snapshot.get("turn_status") or "completed")
    resolution_status = turn_resolution.RESOLVED if safe_int(snapshot.get("event_count")) > 0 else turn_resolution.UNAVAILABLE
    resolution_reason = None if resolution_status == turn_resolution.RESOLVED else f"no_token_count_before_{'task_aborted' if status == 'aborted' else 'task_complete'}"
    recovered = dict(row)
    recovered.update(
        {
            "turn_status": status,
            "lifecycle_end_reason": f"goal_auto_{status}",
            "token_resolution_status": resolution_status,
            "token_resolution_reason": resolution_reason,
            "started_at": snapshot.get("turn_started_at"),
            "stopped_at": snapshot.get("turn_stopped_at") or row.get("stopped_at"),
            "usage": snapshot.get("total_token_usage"),
            "end_token_usage": snapshot.get("total_token_usage"),
            "end_token_snapshot": {
                "found": True,
                "path": snapshot.get("path"),
                "event_count": snapshot.get("event_count"),
                "parse_error_seen": snapshot.get("parse_error_seen"),
                "turn_status": status,
                "turn_started_at": snapshot.get("turn_started_at"),
                "turn_stopped_at": snapshot.get("turn_stopped_at"),
                "total_token_usage": snapshot.get("total_token_usage"),
                "token_source": snapshot.get("token_source"),
            },
            "model_call_count": safe_int(snapshot.get("event_count")),
            "token_source": snapshot.get("token_source"),
        }
    )
    return recovered


def recover_pending_token_resolution(row: dict[str, Any]) -> dict[str, Any]:
    if turn_resolution.status_from_row(row) != turn_resolution.PENDING:
        return row
    turn_id = str(row.get("turn_id") or "")
    if not turn_id:
        return row
    snapshot = task_lifecycle_token_usage(row.get("transcript_path"), turn_id)
    if not snapshot.get("found"):
        reason = str(snapshot.get("reason") or "token_resolution_pending")
        if reason not in {"missing_transcript_path", "transcript_missing", "task_started_missing"}:
            return row
        unavailable = dict(row)
        unavailable.update(
            {
                "token_resolution_status": turn_resolution.UNAVAILABLE,
                "token_resolution_reason": reason,
            }
        )
        return unavailable
    status = str(snapshot.get("turn_status") or row.get("turn_status") or "completed")
    event_count = safe_int(snapshot.get("event_count"))
    resolution_status = turn_resolution.RESOLVED if event_count > 0 else turn_resolution.UNAVAILABLE
    resolution_reason = None if resolution_status == turn_resolution.RESOLVED else f"no_token_count_before_{'task_aborted' if status == 'aborted' else 'task_complete'}"
    recovered = dict(row)
    recovered.update(
        {
            "turn_status": status,
            "lifecycle_end_reason": f"goal_auto_{status}" if status != "completed" else None,
            "token_resolution_status": resolution_status,
            "token_resolution_reason": resolution_reason,
            "started_at": snapshot.get("turn_started_at") or row.get("started_at"),
            "stopped_at": snapshot.get("turn_stopped_at") or row.get("stopped_at"),
            "usage": snapshot.get("total_token_usage"),
            "end_token_usage": snapshot.get("total_token_usage"),
            "end_token_snapshot": dict(snapshot),
            "model_call_count": event_count,
            "token_source": snapshot.get("token_source"),
            "estimated": True,
        }
    )
    return recovered


def unresolved_zero_estimate(row: dict[str, Any]) -> bool:
    return turn_resolution.status_from_row(row) == turn_resolution.PENDING


def record_unavailable_event(row: dict[str, Any]) -> None:
    event, evidence_path, captured_at_ns = turn_resolution.write_unavailable_evidence(token_usage_root(), row)
    row["token_resolution_event_id"] = event
    QUARANTINE_RESULTS.append(
        quarantine_health.record_event(
            token_usage_root(),
            event=event,
            kind=turn_resolution.UNAVAILABLE_KIND,
            source=str(row.get("transcript_path") or "unknown"),
            error=str(row.get("token_resolution_reason") or "unknown"),
            evidence_path=evidence_path,
            captured_at_ns=captured_at_ns,
        )
    )


def append_bad(source: str, line_no: int, line: str, error: str) -> None:
    captured_at_ns = time.time_ns()
    event = quarantine_health.event_id(kind="normalize_raw", source=source, content=line, error=error)
    try:
        if BAD_LOG.parent.is_symlink() or BAD_LOG.is_symlink():
            raise OSError("quarantine evidence path must not be a symlink")
        BAD_LOG.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(BAD_LOG, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "captured_at_ns": captured_at_ns,
                        "event_id": event,
                        "kind": "normalize_raw",
                        "source": source,
                        "line_no": line_no,
                        "error": error,
                        "line": line,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise quarantine_health.QuarantineError(f"cannot write quarantine evidence: {BAD_LOG}: {type(exc).__name__}") from exc
    QUARANTINE_RESULTS.append(
        quarantine_health.record_event(
            token_usage_root(),
            event=event,
            kind="normalize_raw",
            source=source,
            error=error,
            evidence_path=BAD_LOG,
            captured_at_ns=captured_at_ns,
            line_no=line_no,
        )
    )


JSONL_OFFSET_SCAN_CHUNK_BYTES = 64 * 1024


def complete_jsonl_offset(path: pathlib.Path, size: int | None = None) -> int:
    if not path.exists() or path.suffix == ".gz":
        return file_size(path)
    actual_size = file_size(path)
    limit = actual_size if size is None else max(0, min(actual_size, int(size)))
    if limit == 0:
        return 0
    with path.open("rb") as handle:
        handle.seek(limit - 1)
        if handle.read(1) == b"\n":
            return limit
        position = limit
        chunk_size = max(1, int(JSONL_OFFSET_SCAN_CHUNK_BYTES))
        while position > 0:
            chunk_start = max(0, position - chunk_size)
            handle.seek(chunk_start)
            payload = handle.read(position - chunk_start)
            last_newline = payload.rfind(b"\n")
            if last_newline >= 0:
                return chunk_start + last_newline + 1
            position = chunk_start
    return 0


def complete_source_offsets(offsets: dict[str, int]) -> dict[str, int]:
    completed: dict[str, int] = {}
    for path_text, size in offsets.items():
        completed[path_text] = complete_jsonl_offset(pathlib.Path(path_text), size)
    return completed


def iter_rows(path: pathlib.Path, *, source_index: int = 0, source_count: int = 1, byte_limit: int | None = None):
    if not path.exists():
        return
    size = file_size(path)
    limit = size if byte_limit is None else max(0, min(size, int(byte_limit)))

    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            line_no = 0
            while True:
                line = handle.readline()
                if not line:
                    break
                line_no += 1
                cancel_control.check_cancelled("normalize", f"read:{path.name}:{line_no}")
                if line_no == 1 or line_no % 200 == 0:
                    progress_control.write_progress(
                        phase="normalize",
                        phase_index=0,
                        checkpoint=f"read:{path.name}:{line_no}",
                        phase_progress=source_index / max(1, source_count),
                        processed=source_index + 1,
                        total=source_count,
                    )
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    append_bad(str(path), line_no, line.rstrip("\n"), repr(exc))
                    continue
                if isinstance(row, dict):
                    row["_source_priority"] = source_priority_for_path(path)
                    yield row
        return

    with path.open("rb") as handle:
        line_no = 0
        while True:
            if handle.tell() >= limit:
                break
            line_bytes = handle.readline()
            if not line_bytes:
                break
            if handle.tell() > limit:
                break
            if handle.tell() == limit and limit == size and not line_bytes.endswith(b"\n"):
                break
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                line_no += 1
                append_bad(str(path), line_no, line_bytes.decode("utf-8", errors="replace").rstrip("\n"), repr(exc))
                continue
            if not line:
                break
            line_no += 1
            cancel_control.check_cancelled("normalize", f"read:{path.name}:{line_no}")
            if line_no == 1 or line_no % 200 == 0:
                source_progress = 0.0
                if limit > 0:
                    source_progress = handle.tell() / limit
                phase_progress = (source_index + progress_control.clamp(source_progress)) / max(1, source_count)
                progress_control.write_progress(
                    phase="normalize",
                    phase_index=0,
                    checkpoint=f"read:{path.name}:{line_no}",
                    phase_progress=phase_progress,
                    processed=source_index + 1,
                    total=source_count,
                )
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                append_bad(str(path), line_no, line.rstrip("\n"), repr(exc))
                continue
            if isinstance(row, dict):
                row["_source_priority"] = source_priority_for_path(path)
                yield row


def iter_rows_from_offset(path: pathlib.Path, offset: int, *, source_index: int = 0, source_count: int = 1, byte_limit: int | None = None):
    if not path.exists():
        return
    size = file_size(path)
    limit = size if byte_limit is None else max(0, min(size, int(byte_limit)))
    with path.open("rb") as handle:
        handle.seek(offset)
        line_no = 0
        while True:
            if handle.tell() >= limit:
                break
            line_bytes = handle.readline()
            if not line_bytes:
                break
            if handle.tell() > limit:
                break
            if handle.tell() == limit and limit == size and not line_bytes.endswith(b"\n"):
                break
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                line_no += 1
                append_bad(str(path), line_no, line_bytes.decode("utf-8", errors="replace").rstrip("\n"), repr(exc))
                continue
            if not line:
                break
            line_no += 1
            cancel_control.check_cancelled("normalize", f"read:{path.name}:{line_no}")
            if line_no == 1 or line_no % 200 == 0:
                source_progress = ((handle.tell() - offset) / max(1, limit - offset)) if limit > offset else 1.0
                phase_progress = (source_index + progress_control.clamp(source_progress)) / max(1, source_count)
                progress_control.write_progress(
                    phase="normalize",
                    phase_index=0,
                    checkpoint=f"read:{path.name}:{line_no}",
                    phase_progress=phase_progress,
                    processed=source_index + 1,
                    total=source_count,
                )
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                append_bad(str(path), line_no, line.rstrip("\n"), repr(exc))
                continue
            if isinstance(row, dict):
                row["_source_priority"] = source_priority_for_path(path)
                yield row


def archived_prompt_logs() -> list[pathlib.Path]:
    return raw_segments.manifest_segments(token_usage_root(), kind="prompt_usage")


def prepare_raw_segment_sources() -> None:
    base = token_usage_root()
    raw_segments.reconcile_apply_marker(base)
    raw_segments.reconcile_pending_rotation(base)


def current_prompt_logs() -> list[pathlib.Path]:
    return raw_segments.current_segment_paths(token_usage_root(), kind="prompt_usage")


def full_turn_sources() -> list[pathlib.Path]:
    prepare_raw_segment_sources()
    return [*archived_prompt_logs(), *current_prompt_logs()]


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    row = recover_missing_start_state_lifecycle(row)
    row = recover_pending_token_resolution(row)
    status = row.get("turn_status") or "completed"
    normalized = dict(row)
    normalized["schema_version"] = 2
    normalized["record_type"] = "turn_usage_normalized"
    normalized["turn_status"] = status
    normalized.setdefault("lifecycle_end_reason", None)
    normalized.setdefault("turn_end_event", None)
    normalized.setdefault("aborted_at", None)
    normalized.setdefault("aborted_event", None)
    normalized["usage"] = normalize_usage(row.get("usage"))
    if "prompt" in normalized:
        normalized["prompt"] = turn_capture.without_instruction_excerpt(row.get("prompt"))
    if normalized.get("start_token_usage") is None:
        normalized["start_token_usage"] = None
    else:
        normalized["start_token_usage"] = normalize_usage(normalized.get("start_token_usage"))
    normalized["end_token_usage"] = normalize_usage(normalized.get("end_token_usage"))
    model_calls = normalized.get("model_calls")
    if not isinstance(model_calls, list):
        model_calls = []
    normalized["model_call_count"] = safe_int(normalized.get("model_call_count"), len(model_calls))
    normalized.pop("model_calls", None)
    normalized.setdefault("estimated", status != "completed")
    normalized.setdefault("labels", None)
    for snapshot_key in ("start_token_snapshot", "end_token_snapshot"):
        snapshot = normalized.get(snapshot_key)
        if isinstance(snapshot, dict):
            snapshot = dict(snapshot)
            snapshot.pop("model_calls", None)
            normalized[snapshot_key] = snapshot
    return normalized


def rank(row: dict[str, Any]) -> tuple[int, int, int, int, int]:
    status = row.get("turn_status")
    estimated = bool(row.get("estimated"))
    status_rank = {"completed": 3, "aborted": 2, "incomplete": 1}.get(status, 0)
    resolution_rank = {turn_resolution.RESOLVED: 2, turn_resolution.UNAVAILABLE: 1, turn_resolution.PENDING: 0}[turn_resolution.status_from_row(row)]
    return (
        resolution_rank,
        status_rank,
        0 if estimated else 1,
        safe_int(row.get("schema_version")),
        safe_int(row.get("_source_priority")),
    )


def write_jsonl_private(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)
    path.chmod(0o600)


def append_jsonl_private(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def file_size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_state() -> dict[str, Any]:
    try:
        parsed = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(STATE_FILE)
    STATE_FILE.chmod(0o600)


def normalize_identity(
    sources: dict[str, int],
    processed_segments: dict[str, dict[str, Any]] | None = None,
) -> normalize_publish.NormalizeCommitIdentity:
    return normalize_publish.NormalizeCommitIdentity.from_state(
        {
            "logic_version": NORMALIZE_LOGIC_VERSION,
            "sources": sources,
            "processed_segments": processed_segments or {},
        }
    )


def normalize_state(sources: dict[str, int], processed_segments: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    return normalize_identity(sources, processed_segments).to_state(normalized_log_size=file_size(NORMALIZED_LOG))


def pending_publish_file() -> pathlib.Path:
    return STATE_FILE.with_name(f"{STATE_FILE.name}.pending")


def truncate_file(path: pathlib.Path, size: int) -> None:
    if not path.exists():
        return
    with path.open("r+b") as handle:
        handle.truncate(max(0, size))
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def write_pending_publish(turns_offset: int, state: dict[str, Any], *, full_publish: bool = False) -> None:
    path = pending_publish_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = normalize_publish.NormalizePendingPublish.create(
        created_at_unix=time.time(),
        output_path=NORMALIZED_LOG,
        rollback_offset=turns_offset,
        state=state,
        full_publish=full_publish,
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(marker.to_payload(), ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)
    path.chmod(0o600)


def recover_pending_publish() -> None:
    path = pending_publish_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingPublishRecoveryError(f"failed to read pending normalize publish marker: {path}") from exc
    if not isinstance(payload, dict):
        raise PendingPublishRecoveryError(f"invalid pending normalize publish marker: {path}")
    try:
        marker = normalize_publish.NormalizePendingPublish.from_payload(
            payload,
            expected_output_path=NORMALIZED_LOG,
        )
    except normalize_publish.NormalizePublishValidationError as exc:
        raise PendingPublishRecoveryError(f"invalid pending normalize publish marker: {path}: {exc}") from exc
    current_state = read_state()
    if marker.identity.matches_state(current_state):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise PendingPublishRecoveryError(f"failed to clear pending normalize publish marker: {path}") from exc
        return
    try:
        truncate_file(marker.output_path, marker.rollback_offset)
    except OSError as exc:
        raise PendingPublishRecoveryError(f"failed to recover normalized output: {marker.output_path}") from exc
    if marker.full_publish:
        try:
            STATE_FILE.unlink(missing_ok=True)
        except OSError as exc:
            raise PendingPublishRecoveryError(f"failed to clear normalize state during recovery: {STATE_FILE}") from exc
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise PendingPublishRecoveryError(f"failed to clear pending normalize publish marker: {path}") from exc


def commit_normalized_publish(
    *,
    rollback_offset: int,
    identity: normalize_publish.NormalizeCommitIdentity,
    full_publish: bool,
    publish_output: Callable[[], None],
) -> None:
    pending_state = identity.to_state(normalized_log_size=file_size(NORMALIZED_LOG))
    write_pending_publish(rollback_offset, pending_state, full_publish=full_publish)
    publish_output()
    write_state(identity.to_state(normalized_log_size=file_size(NORMALIZED_LOG)))
    pending_publish_file().unlink(missing_ok=True)


def source_offsets() -> dict[str, int]:
    prepare_raw_segment_sources()
    return {str(path): file_size(path) for path in current_prompt_logs()}


def closed_segment_fingerprints() -> dict[str, dict[str, Any]]:
    prepare_raw_segment_sources()
    base = token_usage_root()
    manifest = raw_segments.strict_read_manifest(base)
    fingerprints: dict[str, dict[str, Any]] = {}
    for segment in manifest.get("segments", []):
        if not isinstance(segment, dict) or segment.get("kind") != "prompt_usage" or segment.get("status", "closed") != "closed":
            continue
        segment_id = str(segment.get("id") or "")
        if not segment_id:
            continue
        path = raw_segments.validate_segment_path(base, segment)
        fingerprints[segment_id] = {
            "path": str(path),
            "bytes": safe_int(segment.get("bytes"), file_size(path)),
            "sha256": segment.get("sha256"),
            "rows": safe_int(segment.get("rows")),
        }
    return fingerprints


def output_metadata() -> dict[str, Any]:
    return {
        "output": str(NORMALIZED_LOG),
        "normalized_turns_size": file_size(NORMALIZED_LOG),
    }


def processed_segment_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("path") or "") == str(right.get("path") or "")
        and safe_int(left.get("bytes")) == safe_int(right.get("bytes"))
        and safe_int(left.get("rows")) == safe_int(right.get("rows"))
        and (left.get("sha256") or None) == (right.get("sha256") or None)
    )


def incremental_source_plan(
    previous_sources: dict[str, Any],
    current_sizes: dict[str, int],
    previous_segments: dict[str, Any],
    current_segments: dict[str, dict[str, Any]],
) -> list[tuple[pathlib.Path, int, int]] | None:
    plan: list[tuple[pathlib.Path, int, int]] = []
    previous_paths = set(previous_sources)
    closed_by_path = {str(item.get("path") or ""): (segment_id, item) for segment_id, item in current_segments.items()}

    for segment_id, previous in previous_segments.items():
        if not isinstance(previous, dict):
            return None
        current = current_segments.get(str(segment_id))
        if current is None or not processed_segment_matches(previous, current):
            return None

    for source_text, previous_offset_raw in previous_sources.items():
        previous_offset = safe_int(previous_offset_raw)
        if source_text in current_sizes:
            current_size = current_sizes[source_text]
            if current_size < previous_offset:
                return None
            if current_size > previous_offset:
                plan.append((pathlib.Path(source_text), previous_offset, current_size))
            continue
        closed = closed_by_path.get(source_text)
        if closed is None:
            if previous_offset == 0:
                continue
            return None
        segment_id, fingerprint = closed
        if segment_id in previous_segments:
            continue
        segment_size = safe_int(fingerprint.get("bytes"))
        if segment_size < previous_offset:
            return None
        if segment_size > previous_offset:
            plan.append((pathlib.Path(source_text), previous_offset, segment_size))

    for source_text, current_size in current_sizes.items():
        if source_text not in previous_paths and current_size > 0:
            plan.append((pathlib.Path(source_text), 0, current_size))

    for segment_id, fingerprint in current_segments.items():
        if segment_id in previous_segments:
            continue
        segment_path = str(fingerprint.get("path") or "")
        if segment_path in previous_paths:
            continue
        segment_size = safe_int(fingerprint.get("bytes"))
        if segment_size > 0:
            plan.append((pathlib.Path(segment_path), 0, segment_size))

    return plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize raw Codex Token Bola logs.")
    parser.add_argument("--incremental", action="store_true", help="Append rows from new raw-log bytes instead of rewriting outputs.")
    return parser.parse_args()


def full_normalize() -> dict[str, Any]:
    recover_pending_publish()
    cancel_control.check_cancelled("normalize", "start-full")
    progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="start-full", phase_progress=0.0)
    by_turn: dict[tuple[str, str], dict[str, Any]] = {}
    state_sources = complete_source_offsets(source_offsets())
    state_segments = closed_segment_fingerprints()
    sources = full_turn_sources()
    source_count = max(1, len(sources))
    for source_index, source in enumerate(sources):
        cancel_control.check_cancelled("normalize", f"source:{source.name}")
        progress_control.write_progress(
            phase="normalize",
            phase_index=0,
            checkpoint=f"source:{source.name}",
            phase_progress=source_index / source_count,
            processed=source_index,
            total=source_count,
        )
        for row in iter_rows(source, source_index=source_index, source_count=source_count, byte_limit=state_sources.get(str(source))) or []:
            if row.get("record_type") not in {"turn_usage_raw", "turn_usage", "turn_usage_normalized"}:
                continue
            key = (str(row.get("session_id") or ""), str(row.get("turn_id") or ""))
            if not key[0] or not key[1]:
                continue
            normalized = normalize_row(row)
            if unresolved_zero_estimate(normalized):
                continue
            previous = by_turn.get(key)
            if previous is None or rank(normalized) >= rank(previous):
                by_turn[key] = normalized

    NORMALIZED_LOG.parent.mkdir(parents=True, exist_ok=True)
    cancel_control.check_cancelled("normalize", "publish-full")
    progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="publish-full", phase_progress=0.98)
    turns_offset = file_size(NORMALIZED_LOG)
    identity = normalize_identity(state_sources, state_segments)
    rows = sorted(by_turn.values(), key=lambda item: (str(item.get("captured_at") or ""), str(item.get("turn_id") or "")))
    for row in rows:
        if turn_resolution.status_from_row(row) == turn_resolution.UNAVAILABLE:
            record_unavailable_event(row)
        row.pop("_source_priority", None)
    commit_normalized_publish(
        rollback_offset=0,
        identity=identity,
        full_publish=True,
        publish_output=lambda: write_jsonl_private(NORMALIZED_LOG, rows),
    )
    return {
        "mode": "full",
        "rows": len(rows),
        "new_rows": len(rows),
        "turns_offset": turns_offset,
        **output_metadata(),
    }


def incremental_normalize() -> dict[str, Any]:
    recover_pending_publish()
    cancel_control.check_cancelled("normalize", "start-incremental")
    progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="start-incremental", phase_progress=0.0)
    state = read_state()
    if safe_int(state.get("logic_version")) != NORMALIZE_LOGIC_VERSION:
        return full_normalize()
    sources = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    processed_segments = state.get("processed_segments") if isinstance(state.get("processed_segments"), dict) else {}
    if not NORMALIZED_LOG.exists() or not sources:
        return full_normalize()
    current_sizes = source_offsets()
    current_complete_sizes = complete_source_offsets(current_sizes)
    current_segments = closed_segment_fingerprints()
    source_plan = incremental_source_plan(sources, current_sizes, processed_segments, current_segments)
    if source_plan is None:
        return full_normalize()

    turns_offset = file_size(NORMALIZED_LOG)
    by_turn: dict[tuple[str, str], dict[str, Any]] = {}

    source_count = max(1, len(source_plan))
    for source_index, (source, offset, byte_limit) in enumerate(source_plan):
        cancel_control.check_cancelled("normalize", f"source:{source.name}")
        progress_control.write_progress(
            phase="normalize",
            phase_index=0,
            checkpoint=f"source:{source.name}",
            phase_progress=source_index / source_count,
            processed=source_index,
            total=source_count,
        )
        for row in iter_rows_from_offset(source, offset, source_index=source_index, source_count=source_count, byte_limit=byte_limit) or []:
            if row.get("record_type") not in {"turn_usage_raw", "turn_usage", "turn_usage_normalized"}:
                continue
            key = (str(row.get("session_id") or ""), str(row.get("turn_id") or ""))
            if not key[0] or not key[1]:
                continue
            normalized = normalize_row(row)
            if unresolved_zero_estimate(normalized):
                continue
            previous = by_turn.get(key)
            if previous is None or rank(normalized) >= rank(previous):
                by_turn[key] = normalized

    rows = sorted(by_turn.values(), key=lambda item: (str(item.get("captured_at") or ""), str(item.get("turn_id") or "")))
    for row in rows:
        if turn_resolution.status_from_row(row) == turn_resolution.UNAVAILABLE:
            record_unavailable_event(row)
        row.pop("_source_priority", None)
    cancel_control.check_cancelled("normalize", "publish-incremental")
    progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="publish-incremental", phase_progress=0.98)
    identity = normalize_identity(current_complete_sizes, current_segments)
    commit_normalized_publish(
        rollback_offset=turns_offset,
        identity=identity,
        full_publish=False,
        publish_output=lambda: append_jsonl_private(NORMALIZED_LOG, rows),
    )
    return {
        "mode": "incremental",
        "rows": len(rows),
        "new_rows": len(rows),
        "turns_offset": turns_offset,
        **output_metadata(),
    }


def main() -> int:
    args = parse_args()
    QUARANTINE_RESULTS.clear()
    try:
        with service_lock.acquire_service_lock(reason="normalize"):
            result = incremental_normalize() if args.incremental else full_normalize()
        quarantine = quarantine_health.operation_summary(QUARANTINE_RESULTS)
        result["quarantine"] = quarantine
        result["status"] = "degraded" if quarantine["unacknowledged_events"] else "healthy"
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 1 if result["status"] == "degraded" else 0
    except cancel_control.Cancelled as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
        return cancel_control.CANCEL_EXIT_CODE
    except PendingPublishRecoveryError as exc:
        print(json.dumps(pending_publish_recovery_error_payload(exc), ensure_ascii=False, separators=(",", ":")))
        return 2
    except quarantine_health.QuarantineError as exc:
        print(
            json.dumps(
                {"status": "failed", "error": "quarantine_record_failed", "message": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
