#!/usr/bin/env python3
"""Recover incomplete token-usage hook states.

This script is intentionally outside the hook path. It may scan large
transcripts without blocking a Codex turn.
"""

from __future__ import annotations

import gzip
import json
import pathlib
import sys
import time
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import raw_segments
import service_lock
import service_paths
import transcript_parser
import quarantine_health
import turn_capture
import turn_lifecycle
import turn_resolution

RUNTIME_PATHS = service_paths.resolve_runtime_paths()
CODEX_DIR = RUNTIME_PATHS.codex_dir
BASE_DIR = RUNTIME_PATHS.output_dir
OUTPUT_LAYOUT = service_paths.OutputLayout(BASE_DIR)
STATE_DIR = OUTPUT_LAYOUT.state_dir
BAD_DIR = OUTPUT_LAYOUT.bad_dir
ERROR_LOG = OUTPUT_LAYOUT.error_log
QUARANTINE_RESULTS: list[dict[str, Any]] = []


def move_bad_state(path: pathlib.Path, reason: str) -> None:
    captured_at_ns = time.time_ns()
    try:
        if path.is_symlink() or BAD_DIR.is_symlink():
            raise OSError("quarantine source and destination must not be symlinks")
        content = path.read_bytes()
        event = quarantine_health.event_id(kind="reconcile_state", source=path.name, content=content, error=reason)
        BAD_DIR.mkdir(parents=True, exist_ok=True)
        target = BAD_DIR / f"{path.stem}.{event}{path.suffix}"
        path.replace(target)
    except OSError as exc:
        raise quarantine_health.QuarantineError(f"cannot move bad recovery state: {path}: {type(exc).__name__}") from exc
    if not turn_capture.safe_append_jsonl(
        ERROR_LOG,
        {
            "captured_at": turn_capture.utc_now(),
            "event": "reconcile",
            "warning": "bad_state",
            "reason": reason,
            "moved_to": str(target),
            "quarantine_event_id": event,
        },
    ):
        raise quarantine_health.QuarantineError(f"cannot write bad recovery state evidence log: {ERROR_LOG}")
    QUARANTINE_RESULTS.append(
        quarantine_health.record_event(
            BASE_DIR,
            event=event,
            kind="reconcile_state",
            source=path.name,
            error=reason,
            evidence_path=target,
            captured_at_ns=captured_at_ns,
        )
    )


def iter_jsonl(path: pathlib.Path):
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def archived_prompt_logs() -> list[pathlib.Path]:
    return raw_segments.manifest_segments(BASE_DIR, kind="prompt_usage")


def current_prompt_logs() -> list[pathlib.Path]:
    pointer = raw_segments.strict_read_current_pointer(BASE_DIR)
    current = pointer.get("current", {}).get("prompt_usage")
    if not isinstance(current, dict):
        return []
    segment = raw_segments.validate_current_segment_entry(BASE_DIR, current, kind="prompt_usage")
    return [pathlib.Path(str(segment["path"]))]


def prepare_raw_segment_sources() -> None:
    raw_segments.reconcile_apply_marker(BASE_DIR)
    raw_segments.reconcile_pending_rotation(BASE_DIR)


def completed_turn_index() -> set[tuple[str, str]]:
    prepare_raw_segment_sources()
    completed: set[tuple[str, str]] = set()
    for source in (*archived_prompt_logs(), *current_prompt_logs()):
        for row in iter_jsonl(source) or []:
            completed_turn = completed_turn_from_row(row)
            if completed_turn is not None:
                completed.add(completed_turn)
    return completed


def completed_turn_from_row(row: dict[str, Any]) -> tuple[str, str] | None:
    if turn_resolution.status_from_row(row) == turn_resolution.PENDING:
        return None
    if row.get("turn_status") not in {"completed", "aborted", "incomplete"}:
        return None
    session_id = str(row.get("session_id") or "")
    turn_id = str(row.get("turn_id") or "")
    if session_id and turn_id:
        return (session_id, turn_id)
    return None


def record_unavailable_event(row: dict[str, Any]) -> None:
    event, evidence_path, captured_at_ns = turn_resolution.write_unavailable_evidence(BASE_DIR, row)
    row["token_resolution_event_id"] = event
    QUARANTINE_RESULTS.append(
        quarantine_health.record_event(
            BASE_DIR,
            event=event,
            kind=turn_resolution.UNAVAILABLE_KIND,
            source=str(row.get("transcript_path") or "unknown"),
            error=str(row.get("token_resolution_reason") or "unknown"),
            evidence_path=evidence_path,
            captured_at_ns=captured_at_ns,
        )
    )


def completed_turn_exists_in_current_segments(session_id: str, turn_id: str) -> bool:
    try:
        current_sources = raw_segments.current_segment_paths(BASE_DIR, kind="prompt_usage")
    except (OSError, raw_segments.ManifestError):
        return False
    wanted = (session_id, turn_id)
    for source in current_sources:
        for row in iter_jsonl(source) or []:
            if completed_turn_from_row(row) == wanted:
                return True
    return False


def latest_token_until_turn_end(
    transcript_path: str | None, turn_id: str, offset: int | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    stream, error = transcript_parser.transcript_event_stream(transcript_path, offset)
    if error is not None:
        return (error, None)
    try:
        accumulator = turn_lifecycle.reduce_target_events(stream, turn_id, assume_active=True)
    except OSError as exc:
        return ({"found": False, "reason": "read_error", "error": repr(exc), "path": str(stream.path)}, None)
    snapshot = turn_lifecycle.bounded_usage_snapshot(
        accumulator,
        path=str(stream.path),
        file_size=stream.file_size,
        parse_error_seen=stream.parse_error_seen,
        scan_start=stream.offset or 0,
    )
    if accumulator.terminal_event is not None and not snapshot.get("found"):
        snapshot["bounded_at_file_offset"] = accumulator.terminal_event["bounded_file_offset"]
    return snapshot, accumulator.terminal_event


def reconcile_one(path: pathlib.Path, completed_turns: set[tuple[str, str]]) -> str:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        move_bad_state(path, repr(exc))
        return "bad"

    record_type = state.get("record_type")
    if record_type == "turn_stop_missing_start":
        return reconcile_missing_start_stop(path, state, completed_turns)

    if record_type != "turn_start":
        return "ignored"

    session_id = str(state.get("session_id") or "")
    turn_id = str(state.get("turn_id") or "")
    if not session_id or not turn_id:
        move_bad_state(path, "missing ids")
        return "bad"
    if (session_id, turn_id) in completed_turns:
        path.unlink(missing_ok=True)
        return "duplicate"

    if not state.get("transcript_path"):
        path.unlink(missing_ok=True)
        return "excluded_missing_transcript_path"

    offset = state.get("start_file_size")
    offset = offset if isinstance(offset, int) else None
    end_snapshot, turn_end = latest_token_until_turn_end(state.get("transcript_path"), turn_id, offset)
    if turn_end is None and offset is not None:
        end_snapshot, turn_end = latest_token_until_turn_end(state.get("transcript_path"), turn_id, None)
    if turn_end is None:
        return "pending"

    if not end_snapshot.get("found") and offset is not None:
        full_snapshot, full_turn_end = latest_token_until_turn_end(state.get("transcript_path"), turn_id, None)
        if full_turn_end is not None:
            end_snapshot, turn_end = full_snapshot, full_turn_end

    start_usage = turn_capture.normalize_usage(state.get("start_token_usage"))
    end_usage = turn_capture.normalize_usage(end_snapshot.get("total_token_usage")) if end_snapshot.get("found") else start_usage
    turn_type = turn_end.get("type")
    status = "aborted" if turn_type in {"task_aborted", "turn_aborted"} else "completed"
    lifecycle_reason = turn_end.get("reason") if status == "aborted" else None
    resolution_status = turn_resolution.RESOLVED if end_snapshot.get("found") else turn_resolution.UNAVAILABLE
    resolution_reason = None if resolution_status == turn_resolution.RESOLVED else str(end_snapshot.get("reason") or f"no_token_count_before_{turn_type}")
    record = {
        "schema_version": 2,
        "record_type": "turn_usage_raw",
        "captured_at": turn_capture.utc_now(),
        "captured_at_ns": time.time_ns(),
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": state.get("cwd"),
        "model": state.get("model"),
        "transcript_path": state.get("transcript_path"),
        "turn_status": status,
        "lifecycle_end_reason": lifecycle_reason,
        "token_resolution_status": resolution_status,
        "token_resolution_reason": resolution_reason,
        "started_at": state.get("captured_at"),
        "stopped_at": None,
        "usage": turn_capture.usage_delta(start_usage, end_usage),
        "start_token_usage": start_usage,
        "end_token_usage": end_usage,
        "start_token_snapshot": turn_capture.compact_snapshot(state.get("start_token_snapshot")),
        "end_token_snapshot": turn_capture.compact_snapshot(end_snapshot),
        "turn_end_event": turn_end,
        "prompt": turn_capture.without_instruction_excerpt(
            state.get("prompt") or turn_capture.prompt_metadata("", preview_chars=0)
        ),
        "assistant": turn_capture.assistant_metadata({}),
        "model_call_count": len(end_snapshot.get("model_calls") or []),
        "hook_input": state.get("hook_input"),
        "token_source": "reconcile: transcript token_count diff bounded by turn end event",
        "sqlite_token_source_used": False,
        "estimated": True,
        "start_state_found": True,
    }
    if completed_turn_exists_in_current_segments(session_id, turn_id):
        path.unlink(missing_ok=True)
        completed_turns.add((session_id, turn_id))
        return "duplicate"
    if resolution_status == turn_resolution.UNAVAILABLE:
        record_unavailable_event(record)
    if turn_capture.append_prompt_usage(record, base_dir=BASE_DIR):
        completed_turns.add((session_id, turn_id))
        path.unlink(missing_ok=True)
        return resolution_status if resolution_status == turn_resolution.UNAVAILABLE else status
    return "write_failed"


def reconcile_missing_start_stop(path: pathlib.Path, state: dict[str, Any], completed_turns: set[tuple[str, str]]) -> str:
    session_id = str(state.get("session_id") or "")
    turn_id = str(state.get("turn_id") or "")
    if not session_id or not turn_id:
        move_bad_state(path, "missing ids")
        return "bad"
    if (session_id, turn_id) in completed_turns:
        path.unlink(missing_ok=True)
        return "duplicate"
    if not state.get("transcript_path"):
        path.unlink(missing_ok=True)
        return "excluded_missing_transcript_path"

    stream, error = transcript_parser.transcript_event_stream(state.get("transcript_path"))
    if error is not None:
        return "pending"
    try:
        accumulator = turn_lifecycle.reduce_target_events(stream, turn_id, assume_active=False)
    except OSError:
        return "pending"
    snapshot = turn_lifecycle.full_lifecycle_snapshot(
        accumulator,
        path=str(stream.path),
        file_size=stream.file_size,
        parse_error_seen=stream.parse_error_seen,
        fallback_stopped_at=turn_capture.utc_now(),
    )
    if not snapshot.get("found"):
        return "pending"

    status = str(snapshot.get("turn_status") or "completed")
    usage = turn_capture.usage_delta(turn_capture.zero_usage(), turn_capture.normalize_usage(snapshot.get("total_token_usage")))
    record = {
        "schema_version": 2,
        "record_type": "turn_usage_raw",
        "captured_at": turn_capture.utc_now(),
        "captured_at_ns": time.time_ns(),
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": state.get("cwd"),
        "model": state.get("model"),
        "transcript_path": state.get("transcript_path"),
        "turn_status": status,
        "lifecycle_end_reason": f"goal_auto_{status}",
        "started_at": snapshot.get("turn_started_at"),
        "stopped_at": snapshot.get("turn_stopped_at") or state.get("stopped_at"),
        "usage": usage,
        "start_token_usage": None,
        "end_token_usage": turn_capture.normalize_usage(snapshot.get("total_token_usage")),
        "start_token_snapshot": None,
        "end_token_snapshot": turn_capture.compact_snapshot(snapshot),
        "prompt": turn_capture.prompt_metadata("", preview_chars=0),
        "assistant": state.get("assistant") or turn_capture.assistant_metadata({}),
        "model_call_count": len(snapshot.get("model_calls") or []),
        "hook_input": state.get("hook_input"),
        "token_source": snapshot.get("token_source"),
        "sqlite_token_source_used": False,
        "estimated": True,
        "start_state_found": False,
    }
    if completed_turn_exists_in_current_segments(session_id, turn_id):
        path.unlink(missing_ok=True)
        completed_turns.add((session_id, turn_id))
        return "duplicate"
    if turn_capture.append_prompt_usage(record, base_dir=BASE_DIR):
        completed_turns.add((session_id, turn_id))
        path.unlink(missing_ok=True)
        return status
    return "write_failed"


def run_reconcile() -> int:
    QUARANTINE_RESULTS.clear()
    counts: dict[str, int] = {}
    try:
        completed_turns = completed_turn_index()
    except (OSError, raw_segments.ManifestError, turn_resolution.TokenResolutionError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error": "raw_segment_discovery_failed", "detail": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1
    for path in sorted(STATE_DIR.glob("*.json")):
        result = reconcile_one(path, completed_turns)
        counts[result] = counts.get(result, 0) + 1
    quarantine = quarantine_health.operation_summary(QUARANTINE_RESULTS)
    if counts.get("write_failed"):
        print(json.dumps({"status": "failed", "counts": counts, "quarantine": quarantine}, ensure_ascii=False, separators=(",", ":")))
        return 1
    status = "degraded" if quarantine["unacknowledged_events"] else "healthy"
    print(json.dumps({"status": status, "counts": counts, "quarantine": quarantine}, ensure_ascii=False, separators=(",", ":")))
    return 1 if status == "degraded" else 0


def main() -> int:
    try:
        with service_lock.acquire_service_lock(reason="reconcile", codex_dir=CODEX_DIR):
            return run_reconcile()
    except service_lock.ServiceLockBusy as exc:
        print(
            json.dumps(
                {"error": "analysis_or_cleanup_running", "lock_path": str(exc.path)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 75
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
