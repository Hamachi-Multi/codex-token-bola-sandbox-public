"""Transcript tool-call extraction for analytics builds."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Callable, Iterable


ORIGINAL_TOKEN_COUNT_RE = re.compile(r"Original token count:\s*(\d+)")
WALL_TIME_RE = re.compile(r"Wall time:\s*([0-9.]+)\s*seconds")
EXIT_CODE_RE = re.compile(r"Process exited with code\s+(-?\d+)")

JsonlReader = Callable[[pathlib.Path], Iterable[dict[str, Any]] | None]
SafeInt = Callable[[Any], int]
CancelChecker = Callable[[str, str], None]


def extract_tool_calls(
    paths: set[str],
    turn_ids_by_session: dict[str, set[str]],
    *,
    iter_jsonl: JsonlReader,
    safe_int: SafeInt,
    cancel_checker: CancelChecker,
    output_preview_chars: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path_index, path_text in enumerate(sorted(paths), 1):
        cancel_checker("build", f"tool-extract-path:{path_index}")
        path = pathlib.Path(path_text)
        if not path.exists():
            continue
        session_id = ""
        current_turn_id = ""
        pending: dict[str, dict[str, Any]] = {}
        model_counts_by_turn: dict[tuple[str, str], int] = {}
        path_results: list[dict[str, Any]] = []
        for item in iter_jsonl(path) or []:
            timestamp = item.get("timestamp")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item.get("type") == "session_meta":
                session_id = str(payload.get("id") or session_id)
            elif item.get("type") == "turn_context":
                current_turn_id = str(payload.get("turn_id") or "")
            elif item.get("type") == "event_msg" and payload.get("type") == "token_count":
                key = (session_id, current_turn_id)
                model_counts_by_turn[key] = model_counts_by_turn.get(key, 0) + 1
            elif item.get("type") == "response_item":
                payload_type = payload.get("type")
                if payload_type == "function_call":
                    call_id = str(payload.get("call_id") or "")
                    if not call_id:
                        continue
                    name = str(payload.get("name") or "")
                    key = (session_id, current_turn_id)
                    pending[call_id] = {
                        "session_id": session_id,
                        "turn_id": current_turn_id,
                        "call_id": call_id,
                        "issued_by_model_call_index": model_counts_by_turn.get(key, 0) + 1,
                        "tool_name": name,
                        "tool_namespace": name.split("__", 2)[1] if name.startswith("mcp__") and "__" in name[5:] else name.split("_", 1)[0],
                        "started_at": timestamp,
                    }
                elif payload_type == "function_call_output":
                    call_id = str(payload.get("call_id") or "")
                    base = pending.pop(call_id, {"session_id": session_id, "turn_id": current_turn_id, "call_id": call_id})
                    key = (str(base.get("session_id") or session_id), str(base.get("turn_id") or current_turn_id))
                    output = payload.get("output")
                    output_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, separators=(",", ":"))
                    token_match = ORIGINAL_TOKEN_COUNT_RE.search(output_text)
                    wall_match = WALL_TIME_RE.search(output_text)
                    exit_match = EXIT_CODE_RE.search(output_text)
                    base.update(
                        {
                            "completed_at": timestamp,
                            "consumed_by_model_call_index": model_counts_by_turn.get(key, 0) + 1,
                            "duration_ms": int(float(wall_match.group(1)) * 1000) if wall_match else None,
                            "output_chars": len(output_text),
                            "output_reported_tokens": safe_int(token_match.group(1)) if token_match else None,
                            "status": str(payload.get("status") or "completed"),
                            "exit_code": safe_int(exit_match.group(1)) if exit_match else None,
                            "output_preview": output_text[:output_preview_chars] if output_preview_chars > 0 else "",
                        }
                    )
                    if base.get("turn_id") in turn_ids_by_session.get(str(base.get("session_id") or ""), set()):
                        path_results.append(base)
        for row in path_results:
            key = (str(row.get("session_id") or ""), str(row.get("turn_id") or ""))
            total_model_calls = model_counts_by_turn.get(key, 0)
            for field in ("issued_by_model_call_index", "consumed_by_model_call_index"):
                value = row.get(field)
                if value is not None and safe_int(value) > total_model_calls:
                    row[field] = None
            results.append(row)
    return results
