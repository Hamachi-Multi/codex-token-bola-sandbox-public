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
from datetime import datetime, timezone
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_lock
import service_paths
import raw_segments
import cancel_control
import progress_control

CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
BASE_DIR = service_paths.service_root(CODEX_HOME)
NORMALIZED_LOG = BASE_DIR / "normalized" / "prompt-usage.normalized.jsonl"
BAD_LOG = BASE_DIR / "bad" / "prompt-usage.bad.jsonl"
STATE_FILE = BASE_DIR / "normalized" / "normalize-state.json"
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
NORMALIZE_LOGIC_VERSION = 5


class PendingPublishRecoveryError(RuntimeError):
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


def usage_sum(items: list[dict[str, Any]]) -> dict[str, int]:
    total = zero_usage()
    for item in items:
        usage = normalize_usage(item)
        for key in USAGE_KEYS:
            total[key] += safe_int(usage.get(key))
    return total


def unix_or_timestamp_to_iso(value: Any, fallback: Any = None) -> str | None:
    raw_value = value if value is not None else fallback
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return datetime.fromtimestamp(raw_value, timezone.utc).isoformat()
    if isinstance(raw_value, str) and raw_value:
        text = raw_value[:-1] + "+00:00" if raw_value.endswith("Z") else raw_value
        try:
            return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat()
        except ValueError:
            return raw_value
    return None


def lifecycle_snapshot(
    path: pathlib.Path,
    turn_id: str,
    started_at: str | None,
    stopped_at: str | None,
    status: str | None,
    event_count: int,
    usages: list[dict[str, Any]],
    parse_error_seen: bool,
) -> dict[str, Any]:
    if not started_at:
        return {"found": False, "reason": "task_started_missing", "path": str(path), "parse_error_seen": parse_error_seen}
    if not status:
        return {
            "found": False,
            "reason": "task_terminal_missing",
            "path": str(path),
            "parse_error_seen": parse_error_seen,
            "turn_started_at": started_at,
        }
    return {
        "found": True,
        "path": str(path),
        "event_count": event_count,
        "parse_error_seen": parse_error_seen,
        "turn_status": status,
        "turn_started_at": started_at,
        "turn_stopped_at": stopped_at,
        "total_token_usage": usage_sum(usages),
        "token_source": "transcript_path task lifecycle token_count.info.last_token_usage aggregate",
    }


@lru_cache(maxsize=None)
def transcript_lifecycle_index(transcript_path: str) -> dict[str, Any]:
    path = pathlib.Path(transcript_path).expanduser()
    if not path.exists():
        return {"_error": {"found": False, "reason": "transcript_missing", "path": str(path)}}

    turns: dict[str, dict[str, Any]] = {}
    current_turn_id: str | None = None
    started_at: str | None = None
    stopped_at: str | None = None
    status: str | None = None
    event_count = 0
    usages: list[dict[str, Any]] = []
    parse_error_seen = False

    try:
        size = file_size(path)
        with path.open("r", encoding="utf-8") as handle:
            line_no = 0
            for line in handle:
                line_no += 1
                if line_no == 1 or line_no % 200 == 0:
                    cancel_control.check_cancelled("normalize", f"lifecycle:{path.name}:{line_no}")
                    source_progress = 0.0
                    if size > 0:
                        try:
                            source_progress = handle.tell() / size
                        except OSError:
                            source_progress = 0.0
                    progress_control.write_progress(
                        phase="normalize",
                        phase_index=0,
                        checkpoint=f"lifecycle:{path.name}:{line_no}",
                        phase_progress=progress_control.clamp(source_progress),
                    )
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    parse_error_seen = True
                    continue
                if item.get("type") != "event_msg":
                    continue
                payload = item.get("payload") or {}
                payload_type = payload.get("type")
                if payload_type == "task_started":
                    if current_turn_id:
                        turns[current_turn_id] = lifecycle_snapshot(
                            path,
                            current_turn_id,
                            started_at,
                            stopped_at,
                            status,
                            event_count,
                            usages,
                            parse_error_seen,
                        )
                    current_turn_id = str(payload.get("turn_id") or "")
                    started_at = unix_or_timestamp_to_iso(payload.get("started_at"), item.get("timestamp"))
                    stopped_at = None
                    status = None
                    event_count = 0
                    usages = []
                    continue
                if not current_turn_id:
                    continue
                if payload_type == "token_count":
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    event_count += 1
                    usages.append(normalize_usage(info.get("last_token_usage")))
                    continue
                if payload.get("turn_id") != current_turn_id:
                    continue
                if payload_type == "task_complete":
                    status = "completed"
                    stopped_at = unix_or_timestamp_to_iso(payload.get("completed_at"), item.get("timestamp"))
                elif payload_type in {"task_aborted", "turn_aborted"}:
                    status = "aborted"
                    stopped_at = unix_or_timestamp_to_iso(
                        payload.get("aborted_at") or payload.get("completed_at"),
                        item.get("timestamp"),
                    )
                else:
                    continue
                turns[current_turn_id] = lifecycle_snapshot(
                    path,
                    current_turn_id,
                    started_at,
                    stopped_at,
                    status,
                    event_count,
                    usages,
                    parse_error_seen,
                )
                current_turn_id = None
                started_at = None
                stopped_at = None
                status = None
                event_count = 0
                usages = []
    except OSError as exc:
        return {"_error": {"found": False, "reason": "read_error", "error": repr(exc), "path": str(path)}}

    if current_turn_id:
        turns[current_turn_id] = lifecycle_snapshot(
            path,
            current_turn_id,
            started_at,
            stopped_at,
            status,
            event_count,
            usages,
            parse_error_seen,
        )
    return {"_turns": turns, "_parse_error_seen": parse_error_seen}


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
    recovered = dict(row)
    recovered.update(
        {
            "turn_status": status,
            "lifecycle_end_reason": f"goal_auto_{status}",
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


def unresolved_zero_estimate(row: dict[str, Any]) -> bool:
    if not row.get("estimated"):
        return False
    if row.get("lifecycle_end_reason") not in {
        "pending_token_count",
        "missing_start_state",
        "unresolved_transcript_path",
    }:
        return False
    if row.get("transcript_path"):
        return False
    usage = normalize_usage(row.get("usage"))
    if usage["total_tokens"] != 0:
        return False
    snapshot = row.get("end_token_snapshot")
    if isinstance(snapshot, dict) and snapshot.get("found"):
        return False
    return True


def append_bad(source: str, line_no: int, line: str, error: str) -> None:
    BAD_LOG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(BAD_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"captured_at_ns": time.time_ns(), "source": source, "line_no": line_no, "error": error, "line": line},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
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


def rank(row: dict[str, Any]) -> tuple[int, int, int, int]:
    status = row.get("turn_status")
    estimated = bool(row.get("estimated"))
    status_rank = {"completed": 3, "aborted": 2, "incomplete": 1}.get(status, 0)
    return (
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


def normalize_state(sources: dict[str, int], processed_segments: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "logic_version": NORMALIZE_LOGIC_VERSION,
        "sources": sources,
        "processed_segments": processed_segments or {},
        "normalized_log_size": file_size(NORMALIZED_LOG),
    }


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
    payload = {
        "schema_version": 1,
        "created_at_unix": time.time(),
        "outputs": {
            str(NORMALIZED_LOG): turns_offset,
        },
        "state": state,
        "full_publish": bool(full_publish),
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
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
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    current_state = read_state()
    if (
        current_state.get("logic_version") == state.get("logic_version")
        and current_state.get("sources") == state.get("sources")
        and (current_state.get("processed_segments") or {}) == (state.get("processed_segments") or {})
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise PendingPublishRecoveryError(f"failed to clear pending normalize publish marker: {path}") from exc
        return
    outputs = payload.get("outputs") if isinstance(payload.get("outputs"), dict) else {}
    for output_path, offset in outputs.items():
        try:
            truncate_file(pathlib.Path(output_path), safe_int(offset))
        except OSError as exc:
            raise PendingPublishRecoveryError(f"failed to recover normalized output: {output_path}") from exc
    if payload.get("full_publish"):
        try:
            STATE_FILE.unlink(missing_ok=True)
        except OSError as exc:
            raise PendingPublishRecoveryError(f"failed to clear normalize state during recovery: {STATE_FILE}") from exc
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise PendingPublishRecoveryError(f"failed to clear pending normalize publish marker: {path}") from exc


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
    current_paths = set(current_sizes)
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
    pending_state = normalize_state(state_sources, state_segments)
    rows = sorted(by_turn.values(), key=lambda item: (str(item.get("captured_at") or ""), str(item.get("turn_id") or "")))
    for row in rows:
        row.pop("_source_priority", None)
    write_pending_publish(0, pending_state, full_publish=True)
    write_jsonl_private(NORMALIZED_LOG, rows)
    write_state(normalize_state(state_sources, state_segments))
    pending_publish_file().unlink(missing_ok=True)
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
        row.pop("_source_priority", None)
    cancel_control.check_cancelled("normalize", "publish-incremental")
    progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="publish-incremental", phase_progress=0.98)
    pending_state = normalize_state(current_complete_sizes, current_segments)
    write_pending_publish(turns_offset, pending_state)
    append_jsonl_private(NORMALIZED_LOG, rows)
    write_state(normalize_state(current_complete_sizes, current_segments))
    pending_publish_file().unlink(missing_ok=True)
    return {
        "mode": "incremental",
        "rows": len(rows),
        "new_rows": len(rows),
        "turns_offset": turns_offset,
        **output_metadata(),
    }


def main() -> int:
    args = parse_args()
    try:
        service_paths.assert_migrated(CODEX_HOME)
        with service_lock.acquire_service_lock(reason="normalize"):
            result = incremental_normalize() if args.incremental else full_normalize()
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except cancel_control.Cancelled as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
        return cancel_control.CANCEL_EXIT_CODE
    except PendingPublishRecoveryError as exc:
        print(json.dumps(pending_publish_recovery_error_payload(exc), ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
