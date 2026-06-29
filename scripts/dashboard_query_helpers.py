"""Shared helpers for dashboard query payload builders."""

from __future__ import annotations

import sqlite3
from typing import Any


SUBAGENT_CONFIDENCE_ORDER = (
    "spawn_call_turn_context",
    "child_task_time_overlap",
    "spawn_edge_nearest_parent_turn",
    "parent_pruned_by_retention",
    "orphan",
)

TURN_SORT_COLUMNS = {
    "date": "captured_at_unix",
    "time": "captured_at_unix",
    "clock": "coalesce(substr(captured_at, 12, 8),'')",
    "project": "coalesce(project,'') collate nocase",
    "session": (
        "coalesce(nullif(thread_name,''), "
        "nullif(case when length(replace(session_id,'-','')) >= 16 "
        "then substr(replace(session_id,'-',''),9,8) "
        "else substr(replace(session_id,'-',''),1,8) end,''), '') collate nocase"
    ),
    "prompt": "coalesce(prompt_preview,'') collate nocase",
    "credits": "weighted_credits",
    "raw": "total_tokens",
}

_UNSET = object()


class ApiError(Exception):
    def __init__(self, error: str, status: int = 400) -> None:
        super().__init__(error)
        self.error = error
        self.status = status


def rows_to_dicts(cursor: sqlite3.Cursor):
    return [dict(row) for row in cursor.fetchall()]


def int_query(query, key: str, default: int, minimum: int, maximum: int) -> int:
    raw = (query.get(key) or [str(default)])[0]
    try:
        value = int(raw or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def empty_summary() -> dict[str, Any]:
    return {
        "turns": 0,
        "total_tokens": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "non_cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "weighted_credits": 0.0,
        "cached_ratio": 0.0,
    }


def empty_turns_payload(query) -> dict[str, Any]:
    focused = bool((query.get("focus_session_id") or [""])[0] and (query.get("focus_turn_id") or [""])[0])
    return {
        "rows": [],
        "total": 0,
        "page": 1 if focused else int_query(query, "page", 1, 1, 100000),
        "per_page": int_query(query, "per_page", 25, 1, 100),
        "focused": focused,
    }


def empty_session_detail_payload(query) -> dict[str, Any]:
    session_id = (query.get("selected_session_id") or [""])[0]
    summary = {
        "session_id": session_id,
        "thread_name": "",
        "turns": 0,
        "raw": 0,
        "credits": 0.0,
        "model_calls": 0,
        "non_cached_input_tokens": 0,
        "cached_ratio": 0.0,
    }
    return {"summary": summary, "workflows": [], "tools": [], "turns": [], "subagents": complete_subagent_rows([])}


def empty_subagent_payload(query) -> dict[str, Any]:
    confidence = (query.get("confidence") or [""])[0]
    if confidence not in SUBAGENT_CONFIDENCE_ORDER:
        raise ApiError("confidence_required", 400)
    return {
        "summary": {"confidence": confidence, "rows": 0, "child_raw": 0, "child_credits": 0.0},
        "sessions": [],
        "rows": [],
    }


def empty_payload(path: str, query) -> dict[str, Any]:
    if path == "/api/dashboard":
        return {
            "summary": empty_summary(),
            "projects": {"rows": []},
            "sessions": {"rows": []},
            "turns": empty_turns_payload(query),
            "tools": {"rows": []},
            "subagents": {"rows": complete_subagent_rows([])},
        }
    if path == "/api/session-detail":
        return empty_session_detail_payload(query)
    if path == "/api/summary":
        return empty_summary()
    if path in {"/api/projects", "/api/project-options", "/api/session-options", "/api/categories"}:
        return {"rows": []}
    if path == "/api/sessions":
        return {"rows": [], "total": 0, "page": int_query(query, "sessions_page", 1, 1, 100000), "per_page": int_query(query, "per_page", 25, 1, 100)}
    if path == "/api/tools":
        return {"rows": [], "total": 0, "page": int_query(query, "tools_page", 1, 1, 100000), "per_page": int_query(query, "per_page", 25, 1, 100), "output_tokens_total": 0}
    if path == "/api/turns":
        return empty_turns_payload(query)
    if path == "/api/subagents":
        return {"rows": complete_subagent_rows([])}
    if path == "/api/subagent":
        return empty_subagent_payload(query)
    if path == "/api/tool":
        if not (query.get("tool_name") or [""])[0]:
            raise ApiError("tool_name_required", 400)
        raise ApiError("tool_not_found", 404)
    if path == "/api/turn":
        raise ApiError("turn_not_found", 404)
    raise ApiError("not_found", 404)


def complete_subagent_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_confidence = {str(row.get("confidence") or ""): row for row in rows}
    completed = []
    for confidence in SUBAGENT_CONFIDENCE_ORDER:
        row = by_confidence.get(confidence, {})
        completed.append(
            {
                "confidence": confidence,
                "rows": int(row.get("rows") or 0),
                "child_raw": int(row.get("child_raw") or 0),
                "child_credits": float(row.get("child_credits") or 0.0),
            }
        )
    for confidence, row in sorted(by_confidence.items()):
        if confidence not in SUBAGENT_CONFIDENCE_ORDER:
            completed.append(
                {
                    "confidence": confidence or "unknown",
                    "rows": int(row.get("rows") or 0),
                    "child_raw": int(row.get("child_raw") or 0),
                    "child_credits": float(row.get("child_credits") or 0.0),
                }
            )
    return completed
