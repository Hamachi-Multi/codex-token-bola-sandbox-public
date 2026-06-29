#!/usr/bin/env python3
"""Build a compact SQLite analytics database for Codex Token Bola."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time
from datetime import datetime
from typing import Any, Iterable


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_lock
import raw_segments
import cancel_control
import progress_control
import analysis_inputs
import service_paths
from build_analytics_io import file_size, iter_jsonl, iter_jsonl_from_offset, parse_time
from build_analytics_rows import safe_int, usage
from build_analytics_schema import ensure_indexes, setup_db
import build_analytics_tool_calls

CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
BASE_DIR = service_paths.service_root(CODEX_HOME)
NORMALIZED_LOG = BASE_DIR / "normalized" / "prompt-usage.normalized.jsonl"
ANALYTICS_DB = pathlib.Path(os.environ.get("CODEX_TOKEN_USAGE_ANALYTICS_DB", str(BASE_DIR / "analytics" / "token-usage.sqlite"))).expanduser()
STATE_DB = pathlib.Path(os.environ.get("CODEX_TOKEN_USAGE_STATE_DB", str(CODEX_HOME / "state_5.sqlite"))).expanduser()
SESSION_INDEX = pathlib.Path(os.environ.get("CODEX_TOKEN_USAGE_SESSION_INDEX", str(CODEX_HOME / "session_index.jsonl"))).expanduser()
RETENTION_PRUNED_TURNS_FILE = BASE_DIR / "state" / "retention-pruned-turns.json"
PROJECT_ROOTS = [
    pathlib.Path(value).expanduser()
    for value in os.environ.get("CODEX_TOKEN_USAGE_PROJECT_ROOTS", "").split(os.pathsep)
    if value
]


class BuildInputError(RuntimeError):
    def __init__(self, error: str, **payload: Any) -> None:
        super().__init__(error)
        self.payload = {"error": error, **payload}


def token_usage_root() -> pathlib.Path:
    return NORMALIZED_LOG.parent.parent


def float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


NON_CACHED_INPUT_WEIGHT = float_env("CODEX_TOKEN_USAGE_NON_CACHED_INPUT_WEIGHT", 1.0)
CACHED_INPUT_WEIGHT = float_env("CODEX_TOKEN_USAGE_CACHED_INPUT_WEIGHT", 0.1)
OUTPUT_WEIGHT = float_env("CODEX_TOKEN_USAGE_OUTPUT_WEIGHT", 6.0)
TOOL_OUTPUT_PREVIEW_CHARS = int_env("CODEX_TOKEN_USAGE_TOOL_OUTPUT_PREVIEW_CHARS", 0)


def weighted_credits(non_cached_input: int, cached_input: int, output: int) -> float:
    return (
        non_cached_input * NON_CACHED_INPUT_WEIGHT
        + cached_input * CACHED_INPUT_WEIGHT
        + output * OUTPUT_WEIGHT
    )


def project_from_cwd(cwd: Any) -> str:
    text = str(cwd or "")
    if not text:
        return "unknown"
    path = pathlib.Path(text).expanduser()
    home = pathlib.Path.home()
    for root in (*PROJECT_ROOTS, home / "src", home):
        try:
            rel = path.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError:
            continue
        if not rel.parts:
            return "home"
        return rel.parts[0]
    return text or "unknown"


def classify(prompt: str, cwd: Any) -> tuple[str, str]:
    text = (prompt or "").lower()
    project = project_from_cwd(cwd)
    if "duni audit" in text and ("duni mend" in text or "duni slice" in text or "다음 작업" in text):
        return "audit", "implementation_slice_audit_mend"
    if "duni audit" in text:
        return "audit", "duni_audit"
    if "duni mend" in text and "duni slice" in text:
        return "maintenance", "duni_mend_slice"
    if "duni slice" in text:
        return "maintenance", "duni_slice"
    if "duni mend" in text:
        return "maintenance", "duni_mend"
    if "리뷰" in text or "review" in text or "audit" in text:
        return "review", "review"
    if "구현" in text or "수정" in text or "만들" in text or "작업 진행" in text:
        return "implementation", f"{project}_implementation"
    if "확인" in text or "조사" in text or "분석" in text:
        return "investigation", "investigation"
    if "?" in text or "뭐" in text or "왜" in text:
        return "question", "question"
    return "other", "other"


def metadata_value(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    try:
        row = con.execute("select value from run_metadata where key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return default


def upsert_turn_row(
    con: sqlite3.Connection,
    row: dict[str, Any],
    threads: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    u = usage(row)
    session_id = str(row.get("session_id") or "")
    turn_id = str(row.get("turn_id") or "")
    if not session_id or not turn_id:
        return None
    if should_keep_existing_turn(con, row, session_id, turn_id):
        return None
    prompt = row.get("prompt") if isinstance(row.get("prompt"), dict) else {}
    assistant = row.get("assistant") if isinstance(row.get("assistant"), dict) else {}
    project = project_from_cwd(row.get("cwd"))
    category, workflow = classify(str(prompt.get("prompt_preview") or ""), row.get("cwd"))
    cached_ratio = (u["cached_input_tokens"] / u["input_tokens"]) if u["input_tokens"] else 0.0
    credits = weighted_credits(u["non_cached_input_tokens"], u["cached_input_tokens"], u["output_tokens"])
    equivalent = credits / NON_CACHED_INPUT_WEIGHT if NON_CACHED_INPUT_WEIGHT else 0.0
    thread = threads.get(session_id, {})
    thread_name = str(thread.get("thread_name") or "").strip()
    reasoning_effort = thread.get("reasoning_effort")
    con.execute(
        """
        insert or replace into turns (
          session_id, turn_id, captured_at, captured_at_unix, started_at, stopped_at,
          cwd, project, thread_name, model, reasoning_effort, turn_status, estimated, schema_version, source_priority,
          prompt_preview, prompt_sha256, prompt_chars, prompt_lines, code_block_chars,
          assistant_chars, input_tokens, cached_input_tokens, non_cached_input_tokens,
          output_tokens, reasoning_output_tokens, total_tokens, cached_ratio, model_call_count,
          weighted_credits, uncached_input_equivalent, category, workflow, transcript_path
        ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            session_id,
            turn_id,
            row.get("captured_at"),
            parse_time(row.get("captured_at")),
            row.get("started_at"),
            row.get("stopped_at"),
            row.get("cwd"),
            project,
            thread_name,
            row.get("model") or thread.get("model"),
            reasoning_effort,
            row.get("turn_status"),
            1 if row.get("estimated") else 0,
            safe_int(row.get("schema_version")),
            safe_int(row.get("_source_priority")),
            prompt.get("prompt_preview"),
            prompt.get("prompt_sha256"),
            safe_int(prompt.get("prompt_chars")),
            safe_int(prompt.get("prompt_lines")),
            safe_int((prompt.get("payload_stats") or {}).get("code_block_chars")),
            safe_int(assistant.get("assistant_chars")),
            u["input_tokens"],
            u["cached_input_tokens"],
            u["non_cached_input_tokens"],
            u["output_tokens"],
            u["reasoning_output_tokens"],
            u["total_tokens"],
            cached_ratio,
            safe_int(row.get("model_call_count")),
            credits,
            equivalent,
            category,
            workflow,
            row.get("transcript_path"),
        ),
    )
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "usage": {**u, "weighted_credits": credits},
        "range": {
            "turn_id": turn_id,
            "start_ts": parse_time(row.get("started_at")) or parse_time(row.get("captured_at")) or 0,
            "stop_ts": parse_time(row.get("stopped_at")) or parse_time(row.get("captured_at")) or 0,
        },
        "transcript_path": str(row.get("transcript_path") or ""),
    }


def turn_rank_values(status: Any, estimated: Any, schema_version: Any = 0, source_priority: Any = 0) -> tuple[int, int, int, int]:
    status_rank = {"completed": 3, "aborted": 2, "incomplete": 1}.get(str(status or ""), 0)
    estimated_rank = 0 if bool(estimated) else 1
    return (status_rank, estimated_rank, safe_int(schema_version), safe_int(source_priority))


def row_turn_rank(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return turn_rank_values(
        row.get("turn_status"),
        row.get("estimated"),
        row.get("schema_version"),
        row.get("_source_priority"),
    )


def existing_turn_rank(con: sqlite3.Connection, session_id: str, turn_id: str) -> tuple[int, int, int, int] | None:
    existing = con.execute(
        "select turn_status, estimated, schema_version, source_priority from turns where session_id=? and turn_id=?",
        (session_id, turn_id),
    ).fetchone()
    if existing is None:
        return None
    return turn_rank_values(existing[0], bool(existing[1]), existing[2], existing[3])


def should_keep_existing_turn(con: sqlite3.Connection, row: dict[str, Any], session_id: str, turn_id: str) -> bool:
    existing_rank = existing_turn_rank(con, session_id, turn_id)
    if existing_rank is None:
        return False
    return row_turn_rank(row) < existing_rank


def refresh_turn_thread_names(con: sqlite3.Connection, threads: dict[str, dict[str, Any]]) -> None:
    updates = [
        (str(thread.get("thread_name") or "").strip(), session_id)
        for session_id, thread in threads.items()
        if str(thread.get("thread_name") or "").strip()
    ]
    if updates:
        con.executemany("update turns set thread_name=? where session_id=?", updates)


def tool_output_tokens(row: dict[str, Any]) -> int:
    reported = row.get("output_reported_tokens")
    if reported is not None and safe_int(reported) > 0:
        return safe_int(reported)
    return (safe_int(row.get("output_chars")) + 3) // 4


def upsert_model_call_summary_delta(con: sqlite3.Connection, row: dict[str, Any]) -> None:
    non_cached = safe_int(row.get("non_cached_input_tokens"))
    cached = safe_int(row.get("cached_input_tokens"))
    output = safe_int(row.get("output_tokens"))
    input_tokens = safe_int(row.get("input_tokens"))
    reasoning = safe_int(row.get("reasoning_output_tokens"))
    total = safe_int(row.get("total_tokens"))
    call_index = safe_int(row.get("call_index"))
    credits = weighted_credits(non_cached, cached, output)
    con.execute(
        """
        insert into model_call_summaries (
          session_id, turn_id, calls, input_tokens, cached_input_tokens, non_cached_input_tokens,
          output_tokens, reasoning_output_tokens, total_tokens, weighted_credits,
          max_total_tokens, max_output_tokens, first_call_index, last_call_index
        ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(session_id, turn_id) do update set
          calls = coalesce(calls,0) + excluded.calls,
          input_tokens = coalesce(input_tokens,0) + excluded.input_tokens,
          cached_input_tokens = coalesce(cached_input_tokens,0) + excluded.cached_input_tokens,
          non_cached_input_tokens = coalesce(non_cached_input_tokens,0) + excluded.non_cached_input_tokens,
          output_tokens = coalesce(output_tokens,0) + excluded.output_tokens,
          reasoning_output_tokens = coalesce(reasoning_output_tokens,0) + excluded.reasoning_output_tokens,
          total_tokens = coalesce(total_tokens,0) + excluded.total_tokens,
          weighted_credits = coalesce(weighted_credits,0) + excluded.weighted_credits,
          max_total_tokens = max(coalesce(max_total_tokens,0), excluded.max_total_tokens),
          max_output_tokens = max(coalesce(max_output_tokens,0), excluded.max_output_tokens),
          first_call_index = min(coalesce(first_call_index, excluded.first_call_index), excluded.first_call_index),
          last_call_index = max(coalesce(last_call_index, excluded.last_call_index), excluded.last_call_index)
        """,
        (
            row.get("session_id"),
            row.get("turn_id"),
            1,
            input_tokens,
            cached,
            non_cached,
            output,
            reasoning,
            total,
            credits,
            total,
            output,
            call_index,
            call_index,
        ),
    )


def replace_model_call_summaries_for_turns(con: sqlite3.Connection, changed_turns: set[tuple[str, str]]) -> int:
    if not changed_turns:
        return 0
    con.executemany("delete from model_call_summaries where session_id=? and turn_id=?", sorted(changed_turns))
    return 0


def replace_tool_call_rollups(
    con: sqlite3.Connection,
    rows: list[dict[str, Any]],
    changed_turns: set[tuple[str, str]] | None = None,
) -> None:
    replace_tool_call_rollups_from_batches(con, [rows], changed_turns)


def replace_tool_call_rollups_from_batches(
    con: sqlite3.Connection,
    row_batches: Iterable[Iterable[dict[str, Any]]],
    changed_turns: set[tuple[str, str]] | None = None,
) -> None:
    cancel_control.check_cancelled("build", "replace-tool-rollups")
    if changed_turns is None:
        con.execute("delete from tool_call_summaries")
        con.execute("delete from tool_call_samples")
    else:
        for session_id, turn_id in changed_turns:
            con.execute("delete from tool_call_summaries where session_id=? and turn_id=?", (session_id, turn_id))
            con.execute("delete from tool_call_samples where session_id=? and turn_id=?", (session_id, turn_id))

    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    samples: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    row_index = 0
    for rows in row_batches:
        for row in rows:
            row_index += 1
            if row_index % 100 == 0:
                cancel_control.check_cancelled("build", f"tool-rollups:{row_index}")
            session_id = str(row.get("session_id") or "")
            turn_id = str(row.get("turn_id") or "")
            tool_name = str(row.get("tool_name") or "")
            if not session_id or not turn_id or not tool_name:
                continue
            namespace = str(row.get("tool_namespace") or "")
            output_tokens = tool_output_tokens(row)
            duration = safe_int(row.get("duration_ms")) if row.get("duration_ms") is not None else 0
            status = str(row.get("status") or "")
            failed = 1 if status and status != "completed" else 0
            key = (session_id, turn_id, tool_name, namespace)
            current = groups.setdefault(
                key,
                {
                    "calls": 0,
                    "output_chars": 0,
                    "output_reported_tokens": 0,
                    "output_tokens": 0,
                    "failed_calls": 0,
                    "total_duration_ms": 0,
                    "max_duration_ms": 0,
                    "max_output_tokens": 0,
                },
            )
            current["calls"] += 1
            current["output_chars"] += safe_int(row.get("output_chars"))
            current["output_reported_tokens"] += safe_int(row.get("output_reported_tokens"))
            current["output_tokens"] += output_tokens
            current["failed_calls"] += failed
            current["total_duration_ms"] += duration
            current["max_duration_ms"] = max(current["max_duration_ms"], duration)
            current["max_output_tokens"] = max(current["max_output_tokens"], output_tokens)
            sample = samples.get(key)
            if sample is None or output_tokens > safe_int(sample.get("output_tokens")):
                samples[key] = {**row, "output_tokens": output_tokens}

    for (session_id, turn_id, tool_name, namespace), row in groups.items():
        con.execute(
            "insert or replace into tool_call_summaries values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                turn_id,
                tool_name,
                namespace,
                row["calls"],
                row["output_chars"],
                row["output_reported_tokens"],
                row["output_tokens"],
                row["failed_calls"],
                row["total_duration_ms"],
                row["max_duration_ms"],
                row["max_output_tokens"],
            ),
        )

    ranked = sorted(samples.values(), key=lambda item: tool_output_tokens(item), reverse=True)
    for rank, row in enumerate(ranked, start=1):
        con.execute(
            "insert or replace into tool_call_samples values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.get("session_id"),
                row.get("turn_id"),
                row.get("call_id"),
                row.get("tool_name"),
                row.get("tool_namespace"),
                "largest_output",
                rank,
                row.get("started_at"),
                row.get("completed_at"),
                row.get("duration_ms"),
                row.get("output_chars"),
                row.get("output_reported_tokens"),
                row.get("output_tokens", tool_output_tokens(row)),
                row.get("status"),
                row.get("exit_code"),
                row.get("output_preview"),
            ),
        )


def load_turn_usage_context(con: sqlite3.Connection) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    turn_usage_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    turn_ranges: dict[str, list[dict[str, Any]]] = {}
    for row in con.execute("select session_id, turn_id, started_at, captured_at, stopped_at, total_tokens, weighted_credits from turns"):
        session_id = str(row["session_id"])
        turn_id = str(row["turn_id"])
        turn_usage_by_key[(session_id, turn_id)] = {
            "total_tokens": safe_int(row["total_tokens"]),
            "weighted_credits": float(row["weighted_credits"] or 0.0),
        }
        turn_ranges.setdefault(session_id, []).append(
            {
                "turn_id": turn_id,
                "start_ts": parse_time(row["started_at"]) or parse_time(row["captured_at"]) or 0,
                "stop_ts": parse_time(row["stopped_at"]) or parse_time(row["captured_at"]) or 0,
            }
        )
    return turn_usage_by_key, turn_ranges


def read_retention_pruned_turns() -> dict[str, list[dict[str, Any]]]:
    state_files = [
        RETENTION_PRUNED_TURNS_FILE,
        RETENTION_PRUNED_TURNS_FILE.with_name("retention-pruned-turns.pending.json"),
    ]
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for path in state_files:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        parsed_rows = parsed.get("pruned_turns") if isinstance(parsed, dict) else []
        if isinstance(parsed_rows, list):
            for item in parsed_rows:
                if not isinstance(item, dict):
                    continue
                session_id = str(item.get("session_id") or "")
                turn_id = str(item.get("turn_id") or "")
                if session_id and turn_id:
                    rows_by_key[(session_id, turn_id)] = item
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows_by_key.values():
        session_id = str(row.get("session_id") or "")
        turn_id = str(row.get("turn_id") or "")
        if not session_id or not turn_id:
            continue
        captured_ts = parse_time(row.get("captured_at")) or safe_float(row.get("captured_at_unix"))
        start_ts = parse_time(row.get("started_at")) or captured_ts
        stop_ts = parse_time(row.get("stopped_at")) or captured_ts
        if stop_ts < start_ts:
            stop_ts = start_ts
        result.setdefault(session_id, []).append(
            {
                "turn_id": turn_id,
                "start_ts": start_ts,
                "stop_ts": stop_ts,
            }
        )
    return result


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pruned_turn_key(pruned_turns: dict[str, list[dict[str, Any]]], session_id: str, turn_id: str) -> bool:
    return any(str(row.get("turn_id") or "") == turn_id for row in pruned_turns.get(session_id, []))


def nearest_pruned_parent_turn(pruned_turns: dict[str, list[dict[str, Any]]], session_id: str, child_started_ts: float) -> dict[str, Any] | None:
    candidates = pruned_turns.get(session_id, [])
    if not candidates:
        return None
    before = [row for row in candidates if safe_float(row.get("start_ts")) <= child_started_ts]
    return max(before or candidates, key=lambda item: safe_float(item.get("start_ts")))


def child_usage_totals_by_session(turn_usage_by_key: dict[tuple[str, str], dict[str, Any]]) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for (session_id, _turn_id), value in turn_usage_by_key.items():
        current = totals.setdefault(session_id, {"rows": 0, "total_tokens": 0, "weighted_credits": 0.0})
        current["rows"] += 1
        current["total_tokens"] += safe_int(value.get("total_tokens"))
        current["weighted_credits"] += float(value.get("weighted_credits") or 0.0)
    return totals


def rebuild_task_rollups(
    con: sqlite3.Connection,
    threads: dict[str, dict[str, Any]],
    spawn_contexts: dict[tuple[str, str], dict[str, Any]],
    turn_usage_by_key: dict[tuple[str, str], dict[str, Any]],
    turn_ranges: dict[str, list[dict[str, Any]]],
    affected_sessions: set[str] | None = None,
    edges: list[tuple[str, str, str]] | None = None,
    progress_start_units: int = 0,
    progress_total_units: int = 0,
) -> None:
    cancel_control.check_cancelled("build", "rebuild-task-rollups")
    if affected_sessions is None:
        con.execute("delete from task_rollups")
    else:
        delete_affected_rollups(con, affected_sessions)
    pruned_turns = read_retention_pruned_turns()
    edge_rows = read_edges() if edges is None else edges
    edge_total = len(edge_rows)
    child_usage_by_session = child_usage_totals_by_session(turn_usage_by_key)
    for index, (parent, child, _status) in enumerate(edge_rows, 1):
        if index % 100 == 0:
            cancel_control.check_cancelled("build", f"task-rollups:{index}")
            if progress_total_units > 0:
                write_build_work_progress(
                    f"task-rollups:{index}",
                    progress_start_units + index,
                    progress_total_units,
                    processed=index,
                    total=edge_total,
                )
        if affected_sessions is not None and parent not in affected_sessions and child not in affected_sessions:
            continue
        child_thread = threads.get(child, {})
        child_started_ts = child_task_started_ts(child_thread) or (child_thread.get("created_at_ms") or 0) / 1000
        candidates = turn_ranges.get(parent, [])
        chosen = None
        confidence = "orphan"
        spawn_context = spawn_contexts.get((parent, child))
        if spawn_context:
            chosen = {
                "turn_id": spawn_context["turn_id"],
                "start_ts": parse_time(spawn_context.get("spawn_started_at")) or child_started_ts,
                "stop_ts": parse_time(spawn_context.get("spawn_completed_at")) or child_started_ts,
            }
            confidence = "spawn_call_turn_context"
        else:
            for turn in candidates:
                if turn["start_ts"] <= child_started_ts <= max(turn["stop_ts"], turn["start_ts"]):
                    chosen = turn
                    confidence = "child_task_time_overlap"
                    break
        if chosen is None:
            before = [turn for turn in candidates if turn["start_ts"] <= child_started_ts]
            if before:
                chosen = max(before, key=lambda item: item["start_ts"])
                confidence = "spawn_edge_nearest_parent_turn"
        if chosen is None:
            chosen = nearest_pruned_parent_turn(pruned_turns, parent, child_started_ts)
            if chosen is not None:
                confidence = "parent_pruned_by_retention"
        if chosen is None:
            continue
        if pruned_turn_key(pruned_turns, parent, str(chosen["turn_id"])) and (parent, chosen["turn_id"]) not in turn_usage_by_key:
            confidence = "parent_pruned_by_retention"
        child_usage = child_usage_by_session.get(child, {})
        child_total = safe_int(child_usage.get("total_tokens"))
        child_credits = float(child_usage.get("weighted_credits") or 0.0)
        if safe_int(child_usage.get("rows")) <= 0 or (child_total == 0 and child_credits == 0.0):
            continue
        own = turn_usage_by_key.get((parent, chosen["turn_id"]), {})
        own_total = safe_int(own.get("total_tokens"))
        own_credits = float(own.get("weighted_credits") or 0.0)
        con.execute(
            "insert or replace into task_rollups values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                parent,
                chosen["turn_id"],
                child,
                child_thread.get("agent_role"),
                child_thread.get("agent_nickname"),
                datetime.fromtimestamp(child_started_ts).isoformat() if child_started_ts else None,
                child_started_ts,
                confidence,
                own_total,
                child_total,
                own_total + child_total,
                own_credits,
                child_credits,
                own_credits + child_credits,
            ),
        )
    if edge_total and progress_total_units > 0:
        write_build_work_progress(
            f"task-rollups:{edge_total}",
            progress_start_units + edge_total,
            progress_total_units,
            processed=edge_total,
            total=edge_total,
        )


def affected_rollup_sessions(changed_turns: set[tuple[str, str]]) -> set[str]:
    sessions = {session_id for session_id, _turn_id in changed_turns}
    edges = read_edges()
    changed = True
    while changed:
        changed = False
        for parent, child, _status in edges:
            if parent in sessions or child in sessions:
                before = len(sessions)
                sessions.add(parent)
                sessions.add(child)
                changed = changed or len(sessions) != before
    return sessions


def delete_affected_rollups(con: sqlite3.Connection, sessions: set[str]) -> None:
    if not sessions:
        return
    ordered = sorted(sessions)
    placeholders = ",".join("?" for _ in ordered)
    con.execute(
        f"""
        delete from task_rollups
        where parent_session_id in ({placeholders})
           or child_session_id in ({placeholders})
        """,
        [*ordered, *ordered],
    )


def db_metadata(con: sqlite3.Connection) -> dict[str, Any]:
    return {
        "turn_rows": con.execute("select count(*) from turns").fetchone()[0],
        "tool_call_rows": con.execute("select coalesce(sum(calls),0) from tool_call_summaries").fetchone()[0],
        "tool_call_summary_rows": con.execute("select count(*) from tool_call_summaries").fetchone()[0],
        "tool_call_sample_rows": con.execute("select count(*) from tool_call_samples").fetchone()[0],
        "task_rollup_rows": con.execute("select count(*) from task_rollups").fetchone()[0],
        "non_cached_input_weight": NON_CACHED_INPUT_WEIGHT,
        "cached_input_weight": CACHED_INPUT_WEIGHT,
        "output_weight": OUTPUT_WEIGHT,
    }


def write_metadata(con: sqlite3.Connection, metadata: dict[str, Any]) -> None:
    for key, value in metadata.items():
        con.execute("insert or replace into run_metadata values (?,?)", (key, json.dumps(value, ensure_ascii=False)))


def applied_offset_metadata() -> dict[str, Any]:
    return {
        "applied_normalized_turns_size": file_size(NORMALIZED_LOG),
        "applied_input_fingerprint": analysis_input_fingerprint(),
        "applied_at_unix": time.time(),
    }


def analysis_input_fingerprint() -> str:
    return analysis_inputs.paths_fingerprint([STATE_DB, SESSION_INDEX, RETENTION_PRUNED_TURNS_FILE])


def write_build_work_progress(
    checkpoint: str,
    done_units: int,
    total_units: int,
    *,
    processed: int | None = None,
    total: int | None = None,
) -> None:
    progress_control.write_progress(
        phase="build",
        phase_index=1,
        checkpoint=checkpoint,
        phase_progress=done_units / max(1, total_units),
        processed=processed if processed is not None else done_units,
        total=total if total is not None else total_units,
    )


def scan_normalized_build_inputs(path: pathlib.Path, *, offset: int = 0) -> tuple[int, set[str]]:
    rows = 0
    transcript_paths: set[str] = set()
    iterator = iter_jsonl_from_offset(path, max(0, offset)) if offset > 0 else iter_jsonl(path)
    for row in iterator or ():
        rows += 1
        transcript_path = str(row.get("transcript_path") or "")
        if transcript_path:
            transcript_paths.add(transcript_path)
    return rows, transcript_paths


def read_session_index() -> dict[str, str]:
    names: dict[str, str] = {}
    for row in iter_jsonl(SESSION_INDEX) or []:
        session_id = str(row.get("id") or "")
        thread_name = str(row.get("thread_name") or "").strip()
        if session_id and thread_name:
            names[session_id] = thread_name
    return names


def read_threads() -> dict[str, dict[str, Any]]:
    thread_names = read_session_index()
    if not STATE_DB.exists():
        return {session_id: {"thread_name": name} for session_id, name in thread_names.items()}
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        threads = {
            row["id"]: dict(row)
            for row in con.execute(
                "select id, rollout_path, created_at_ms, updated_at_ms, agent_role, agent_nickname, reasoning_effort, model, thread_source from threads"
            )
        }
        for session_id, thread_name in thread_names.items():
            threads.setdefault(session_id, {})["thread_name"] = thread_name
        return threads
    except sqlite3.Error:
        return {session_id: {"thread_name": name} for session_id, name in thread_names.items()}
    finally:
        con.close()


def read_edges() -> list[tuple[str, str, str]]:
    if not STATE_DB.exists():
        return []
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        return [(str(p), str(c), str(s)) for p, c, s in con.execute("select parent_thread_id, child_thread_id, status from thread_spawn_edges")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def child_task_started_ts(thread: dict[str, Any]) -> float | None:
    path_text = thread.get("rollout_path")
    if not path_text:
        return None
    path = pathlib.Path(str(path_text))
    if not path.exists():
        return None
    for item in iter_jsonl(path) or []:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item.get("type") == "event_msg" and payload.get("msg") == "task_started":
            return parse_time(item.get("timestamp"))
    return None


def spawn_turn_contexts(
    threads: dict[str, dict[str, Any]],
    *,
    progress_start_units: int = 0,
    progress_total_units: int = 0,
) -> dict[tuple[str, str], dict[str, Any]]:
    mappings: dict[tuple[str, str], dict[str, Any]] = {}
    thread_total = len(threads)
    for index, (parent_id, thread) in enumerate(threads.items(), 1):
        if index % 25 == 0 or index == thread_total:
            cancel_control.check_cancelled("build", f"spawn-contexts:{index}")
            if progress_total_units > 0:
                write_build_work_progress(
                    f"spawn-contexts:{index}",
                    progress_start_units + index,
                    progress_total_units,
                    processed=index,
                    total=thread_total,
                )
        path_text = thread.get("rollout_path")
        if not path_text:
            continue
        path = pathlib.Path(str(path_text))
        if not path.exists():
            continue
        current_turn_id = ""
        pending: dict[str, dict[str, Any]] = {}
        for item in iter_jsonl(path) or []:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item.get("type") == "turn_context":
                current_turn_id = str(payload.get("turn_id") or "")
            elif item.get("type") == "response_item" and payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                call_id = str(payload.get("call_id") or "")
                if call_id:
                    pending[call_id] = {
                        "turn_id": current_turn_id,
                        "spawn_started_at": item.get("timestamp"),
                    }
            elif item.get("type") == "response_item" and payload.get("type") == "function_call_output":
                call_id = str(payload.get("call_id") or "")
                base = pending.pop(call_id, None)
                if not base:
                    continue
                output = parse_json_object(payload.get("output"))
                child_id = str(output.get("agent_id") or "")
                if child_id and base.get("turn_id"):
                    mappings[(parent_id, child_id)] = {
                        **base,
                        "spawn_completed_at": item.get("timestamp"),
                    }
    return mappings


def spawn_context_threads_for_affected_sessions(
    threads: dict[str, dict[str, Any]],
    affected_sessions: set[str],
) -> dict[str, dict[str, Any]]:
    if not affected_sessions:
        return {}
    return {session_id: thread for session_id, thread in threads.items() if session_id in affected_sessions}


def extract_tool_calls(paths: set[str], turn_ids_by_session: dict[str, set[str]]) -> list[dict[str, Any]]:
    return build_analytics_tool_calls.extract_tool_calls(
        paths,
        turn_ids_by_session,
        iter_jsonl=iter_jsonl,
        safe_int=safe_int,
        cancel_checker=cancel_control.check_cancelled,
        output_preview_chars=TOOL_OUTPUT_PREVIEW_CHARS,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a SQLite analytics database from normalized Codex Token Bola logs.")
    parser.add_argument("--normalized-log", default=str(NORMALIZED_LOG))
    parser.add_argument("--state-db", default=str(STATE_DB))
    parser.add_argument("--output", default=str(ANALYTICS_DB))
    parser.add_argument("--project-root", action="append", default=[], help="Root whose first path segment identifies a project. May be repeated.")
    parser.add_argument("--incremental", action="store_true", help="Upsert rows appended after the supplied normalized-log offsets.")
    parser.add_argument("--turns-offset", type=int, default=0)
    return parser.parse_args()


def configure_paths(args: argparse.Namespace) -> None:
    global NORMALIZED_LOG, STATE_DB, ANALYTICS_DB, SESSION_INDEX, RETENTION_PRUNED_TURNS_FILE, PROJECT_ROOTS
    NORMALIZED_LOG = pathlib.Path(args.normalized_log).expanduser()
    STATE_DB = pathlib.Path(args.state_db).expanduser()
    SESSION_INDEX = pathlib.Path(os.environ.get("CODEX_TOKEN_USAGE_SESSION_INDEX", str(STATE_DB.parent / "session_index.jsonl"))).expanduser()
    ANALYTICS_DB = pathlib.Path(args.output).expanduser()
    RETENTION_PRUNED_TURNS_FILE = NORMALIZED_LOG.parent.parent / "state" / "retention-pruned-turns.json"
    if args.project_root:
        PROJECT_ROOTS = [pathlib.Path(value).expanduser() for value in args.project_root]


def incremental_build(args: argparse.Namespace) -> dict[str, Any] | None:
    cancel_control.check_cancelled("build", "start-incremental")
    normalized_size = file_size(NORMALIZED_LOG)
    if args.turns_offset > normalized_size:
        raise BuildInputError(
            "turns_offset_beyond_normalized_size",
            turns_offset=args.turns_offset,
            normalized_turns_size=normalized_size,
        )
    if not ANALYTICS_DB.exists():
        return None
    con = sqlite3.connect(ANALYTICS_DB)
    con.row_factory = sqlite3.Row
    try:
        con.execute("select 1 from turns limit 1")
        con.execute("select 1 from run_metadata limit 1")
        ensure_indexes(con)
        existing_tables = {str(row[0]) for row in con.execute("select name from sqlite_master where type='table'")}
        required_tables = {"model_call_summaries", "tool_call_summaries", "tool_call_samples"}
        if not required_tables.issubset(existing_tables):
            con.close()
            return None
    except sqlite3.Error:
        con.close()
        return None

    started = time.monotonic()
    threads = read_threads()
    refresh_turn_thread_names(con, threads)
    edges = read_edges()
    turn_row_count, _planned_transcript_paths = scan_normalized_build_inputs(NORMALIZED_LOG, offset=max(0, args.turns_offset))
    total_units = max(1, len(threads) + turn_row_count + turn_row_count + len(edges) + 2)
    write_build_work_progress("start-incremental", 0, total_units)
    changed_turns: set[tuple[str, str]] = set()
    changed_tool_turns: set[tuple[str, str]] = set()
    changed_transcripts: dict[str, dict[str, set[str]]] = {}

    for index, row in enumerate(iter_jsonl_from_offset(NORMALIZED_LOG, max(0, args.turns_offset)) or (), 1):
        if index % 100 == 0 or index == turn_row_count:
            cancel_control.check_cancelled("build", f"turns:{index}")
            write_build_work_progress(
                checkpoint=f"turns:{index}",
                done_units=len(threads) + index,
                total_units=total_units,
                processed=index,
                total=turn_row_count,
            )
        info = upsert_turn_row(con, row, threads)
        if info is None:
            continue
        key = (info["session_id"], info["turn_id"])
        changed_turns.add(key)
        path = info["transcript_path"]
        changed_tool_turns.add(key)
        if path:
            changed_transcripts.setdefault(path, {}).setdefault(info["session_id"], set()).add(info["turn_id"])

    transcript_items = list(changed_transcripts.items())
    tool_start_units = len(threads) + turn_row_count

    def changed_tool_call_batches() -> Iterable[list[dict[str, Any]]]:
        for path_index, (path, turns_by_session) in enumerate(transcript_items, 1):
            cancel_control.check_cancelled("build", f"tool-extract:{pathlib.Path(path).name}")
            write_build_work_progress(
                checkpoint=f"tool-extract:{pathlib.Path(path).name}",
                done_units=tool_start_units + path_index,
                total_units=total_units,
                processed=path_index,
                total=len(transcript_items),
            )
            yield extract_tool_calls({path}, turns_by_session)

    replace_tool_call_rollups_from_batches(con, changed_tool_call_batches(), changed_tool_turns)

    turn_usage_by_key, turn_ranges = load_turn_usage_context(con)
    affected_sessions = affected_rollup_sessions(changed_turns)
    spawn_threads = spawn_context_threads_for_affected_sessions(threads, affected_sessions)
    spawn_contexts = spawn_turn_contexts(
        spawn_threads,
        progress_start_units=tool_start_units + len(transcript_items),
        progress_total_units=total_units,
    )
    rebuild_task_rollups(
        con,
        threads,
        spawn_contexts,
        turn_usage_by_key,
        turn_ranges,
        affected_sessions,
        edges=edges,
        progress_start_units=tool_start_units + len(transcript_items) + len(spawn_threads),
        progress_total_units=total_units,
    )
    metadata = db_metadata(con)
    metadata.update(
        {
            "analysis_mode": "incremental",
            "new_turn_rows": len({key for key in changed_turns}),
            "processed_turn_log_rows": turn_row_count,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "normalized_turns_offset": args.turns_offset,
            **applied_offset_metadata(),
        }
    )
    write_metadata(con, metadata)
    cancel_control.check_cancelled("build", "commit-incremental")
    write_build_work_progress("commit-incremental", total_units - 1, total_units)
    con.commit()
    con.close()
    ANALYTICS_DB.chmod(0o600)
    return {"output": str(ANALYTICS_DB), **metadata}


def build(output: pathlib.Path | None = None) -> dict[str, Any]:
    cancel_control.check_cancelled("build", "start-full")
    global ANALYTICS_DB
    if output is not None:
        ANALYTICS_DB = pathlib.Path(output).expanduser()
    raw_root = token_usage_root()
    raw_segments.reconcile_apply_marker(raw_root)
    raw_segments.reconcile_pending_rotation(raw_root)
    turn_row_count, planned_transcript_paths = scan_normalized_build_inputs(NORMALIZED_LOG)
    threads = read_threads()
    edges = read_edges()
    total_units = max(1, len(threads) + turn_row_count + len(planned_transcript_paths) + len(edges) + 2)
    write_build_work_progress("start-full", 0, total_units)
    spawn_contexts = spawn_turn_contexts(threads, progress_start_units=0, progress_total_units=total_units)

    ANALYTICS_DB.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = ANALYTICS_DB.with_name(f".{ANALYTICS_DB.name}.{os.getpid()}.{time.time_ns()}.tmp")
    if tmp_db.exists():
        tmp_db.unlink()
    fd = os.open(tmp_db, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    con = sqlite3.connect(tmp_db)
    setup_db(con)

    turn_usage_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    turn_ranges: dict[str, list[dict[str, Any]]] = {}
    transcript_paths: set[str] = set()
    turn_ids_by_session: dict[str, set[str]] = {}

    for index, row in enumerate(iter_jsonl(NORMALIZED_LOG) or (), 1):
        if index % 100 == 0 or index == turn_row_count:
            cancel_control.check_cancelled("build", f"turns:{index}")
            write_build_work_progress(
                checkpoint=f"turns:{index}",
                done_units=len(threads) + index,
                total_units=total_units,
                processed=index,
                total=turn_row_count,
            )
        info = upsert_turn_row(con, row, threads)
        if info is None:
            continue
        session_id = info["session_id"]
        turn_id = info["turn_id"]
        turn_usage_by_key[(session_id, turn_id)] = info["usage"]
        turn_ranges.setdefault(session_id, []).append(info["range"])
        turn_ids_by_session.setdefault(session_id, set()).add(turn_id)
        if info["transcript_path"]:
            transcript_paths.add(info["transcript_path"])

    tool_start_units = len(threads) + turn_row_count
    transcript_list = sorted(transcript_paths)

    def tool_call_batches() -> Iterable[list[dict[str, Any]]]:
        for path_index, path in enumerate(transcript_list, 1):
            write_build_work_progress(
                f"tool-extract:{pathlib.Path(path).name}",
                tool_start_units + path_index,
                total_units,
                processed=path_index,
                total=len(transcript_list),
            )
            yield extract_tool_calls({path}, turn_ids_by_session)

    replace_tool_call_rollups_from_batches(con, tool_call_batches())

    task_start_units = tool_start_units + len(transcript_list)
    rebuild_task_rollups(
        con,
        threads,
        spawn_contexts,
        turn_usage_by_key,
        turn_ranges,
        edges=edges,
        progress_start_units=task_start_units,
        progress_total_units=total_units,
    )

    metadata = db_metadata(con)
    metadata.update({"analysis_mode": "full", "new_turn_rows": turn_row_count, **applied_offset_metadata()})
    write_metadata(con, metadata)
    cancel_control.check_cancelled("build", "publish-full")
    write_build_work_progress("publish-full", total_units - 1, total_units)
    con.commit()
    con.close()
    tmp_db.chmod(0o600)
    tmp_db.replace(ANALYTICS_DB)
    ANALYTICS_DB.chmod(0o600)
    return {"output": str(ANALYTICS_DB), **metadata}


def main() -> int:
    args = parse_args()
    configure_paths(args)
    try:
        service_paths.assert_migrated(CODEX_HOME)
        with service_lock.acquire_service_lock(reason="build"):
            if args.incremental:
                result = incremental_build(args)
                if result is not None:
                    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
                    return 0
            result = build()
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except cancel_control.Cancelled as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
        return cancel_control.CANCEL_EXIT_CODE
    except BuildInputError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
