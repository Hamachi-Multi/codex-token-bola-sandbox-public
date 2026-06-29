#!/usr/bin/env python3
"""Recover incomplete token-usage hook states.

This script is intentionally outside the hook path. It may scan large
transcripts without blocking a Codex turn.
"""

from __future__ import annotations

import importlib.util
import gzip
import json
import os
import pathlib
import sys
import time
from typing import Any


CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
REPO_HOOK_PATH = SCRIPT_DIR.parent / "hooks" / "token-usage.py"
HOOK_PATH = REPO_HOOK_PATH if REPO_HOOK_PATH.exists() else CODEX_HOME / "hooks" / "token-usage.py"

import raw_segments
import service_lock
import service_paths
import transcript_parser

BASE_DIR = service_paths.service_root(CODEX_HOME)
STATE_DIR = BASE_DIR / "state"
BAD_DIR = BASE_DIR / "bad"


def load_hook():
    spec = importlib.util.spec_from_file_location("token_usage_hook", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


hook = load_hook()


def move_bad_state(path: pathlib.Path, reason: str) -> None:
    BAD_DIR.mkdir(parents=True, exist_ok=True)
    target = BAD_DIR / f"{path.stem}.{time.time_ns()}{path.suffix}"
    try:
        path.replace(target)
    except OSError:
        return
    hook.safe_append_jsonl(
        hook.ERROR_LOG,
        {"captured_at": hook.utc_now(), "event": "reconcile", "warning": "bad_state", "reason": reason, "moved_to": str(target)},
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
    if row.get("lifecycle_end_reason") == "pending_token_count" and row.get("estimated"):
        return None
    if row.get("turn_status") not in {"completed", "aborted", "incomplete"}:
        return None
    session_id = str(row.get("session_id") or "")
    turn_id = str(row.get("turn_id") or "")
    if session_id and turn_id:
        return (session_id, turn_id)
    return None


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

    latest: dict[str, Any] | None = None
    model_calls: list[dict[str, Any]] = []
    event_count = 0
    file_size = stream.file_size
    try:
        for event in stream:
            item = event["item"]
            line_start = int(event["line_start"])
            next_offset = int(event["next_offset"])
            payload = item.get("payload") or {}
            if item.get("type") != "event_msg":
                continue
            payload_type = payload.get("type")
            if payload_type == "task_started" and payload.get("turn_id") == turn_id:
                latest = None
                model_calls = []
                event_count = 0
                continue
            if payload_type in {"task_complete", "task_aborted", "turn_aborted"} and payload.get("turn_id") == turn_id:
                turn_end = {
                    "type": payload_type,
                    "timestamp": item.get("timestamp"),
                    "turn_id": payload.get("turn_id"),
                    "reason": payload.get("reason"),
                    "completed_at": payload.get("completed_at"),
                    "duration_ms": payload.get("duration_ms"),
                    "event_offset": line_start,
                    "bounded_file_offset": next_offset,
                }
                if latest is None:
                    return (
                        {
                            "found": False,
                            "reason": f"no_token_count_before_{payload_type}",
                            "path": str(stream.path),
                            "file_size": file_size,
                            "model_calls": [],
                            "bounded_at_file_offset": next_offset,
                        },
                        turn_end,
                    )
                latest.update(
                    {
                        "found": True,
                        "path": str(stream.path),
                        "file_size": file_size,
                        "event_count": event_count,
                        "model_calls": model_calls,
                        "bounded_at_event_type": payload_type,
                        "bounded_at_timestamp": item.get("timestamp"),
                        "bounded_at_file_offset": next_offset,
                        "turn_end_event_offset": line_start,
                    }
                )
                return (latest, turn_end)
            if payload_type != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            event_count += 1
            last_usage = hook.normalize_usage(info.get("last_token_usage"))
            model_calls.append(
                {
                    "index": event_count,
                    "timestamp": item.get("timestamp"),
                    "usage": last_usage,
                    "model_context_window": info.get("model_context_window"),
                }
            )
            latest = {
                "timestamp": item.get("timestamp"),
                "total_token_usage": hook.normalize_usage(info.get("total_token_usage")),
                "last_token_usage": last_usage,
                "model_context_window": info.get("model_context_window"),
            }
    except OSError as exc:
        return ({"found": False, "reason": "read_error", "error": repr(exc), "path": str(stream.path)}, None)

    return ({"found": False, "reason": "turn_end_not_found", "path": str(stream.path), "file_size": file_size, "model_calls": []}, None)


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

    start_usage = hook.normalize_usage(state.get("start_token_usage"))
    end_usage = hook.normalize_usage(end_snapshot.get("total_token_usage")) if end_snapshot.get("found") else start_usage
    turn_type = turn_end.get("type")
    status = "aborted" if turn_type in {"task_aborted", "turn_aborted"} else "completed"
    reason = turn_end.get("reason") if status == "aborted" else "missed_stop_hook"
    record = {
        "schema_version": 2,
        "record_type": "turn_usage_raw",
        "captured_at": hook.utc_now(),
        "captured_at_ns": time.time_ns(),
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": state.get("cwd"),
        "model": state.get("model"),
        "transcript_path": state.get("transcript_path"),
        "turn_status": status,
        "lifecycle_end_reason": reason,
        "started_at": state.get("captured_at"),
        "stopped_at": None,
        "usage": hook.usage_delta(start_usage, end_usage),
        "start_token_usage": start_usage,
        "end_token_usage": end_usage,
        "start_token_snapshot": hook.compact_snapshot(state.get("start_token_snapshot")),
        "end_token_snapshot": hook.compact_snapshot(end_snapshot),
        "turn_end_event": turn_end,
        "prompt": state.get("prompt") or hook.prompt_metadata(""),
        "assistant": hook.assistant_metadata({}),
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
    if hook.append_prompt_usage(record, base_dir=BASE_DIR):
        completed_turns.add((session_id, turn_id))
        path.unlink(missing_ok=True)
        return status
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

    snapshot = hook.task_lifecycle_token_usage(state.get("transcript_path"), turn_id)
    if not snapshot.get("found"):
        return "pending"

    status = str(snapshot.get("turn_status") or "completed")
    usage = hook.usage_delta(hook.zero_usage(), hook.normalize_usage(snapshot.get("total_token_usage")))
    record = {
        "schema_version": 2,
        "record_type": "turn_usage_raw",
        "captured_at": hook.utc_now(),
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
        "end_token_usage": hook.normalize_usage(snapshot.get("total_token_usage")),
        "start_token_snapshot": None,
        "end_token_snapshot": hook.compact_snapshot(snapshot),
        "prompt": hook.prompt_metadata(""),
        "assistant": state.get("assistant") or hook.assistant_metadata({}),
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
    if hook.append_prompt_usage(record, base_dir=BASE_DIR):
        completed_turns.add((session_id, turn_id))
        path.unlink(missing_ok=True)
        return status
    return "write_failed"


def run_reconcile() -> int:
    counts: dict[str, int] = {}
    try:
        completed_turns = completed_turn_index()
    except (OSError, raw_segments.ManifestError) as exc:
        print(
            json.dumps(
                {"error": "raw_segment_discovery_failed", "detail": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1
    for path in sorted(STATE_DIR.glob("*.json")):
        result = reconcile_one(path, completed_turns)
        counts[result] = counts.get(result, 0) + 1
    print(json.dumps({"counts": counts}, ensure_ascii=False, separators=(",", ":")))
    if counts.get("write_failed"):
        return 1
    return 0


def main() -> int:
    try:
        service_paths.assert_migrated(CODEX_HOME)
        with service_lock.acquire_service_lock(reason="reconcile", codex_home=CODEX_HOME):
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


if __name__ == "__main__":
    raise SystemExit(main())
