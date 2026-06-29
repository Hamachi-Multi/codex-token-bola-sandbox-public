"""Row normalization helpers for analytics builds."""

from __future__ import annotations

from typing import Any


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def usage(row: dict[str, Any]) -> dict[str, int]:
    value = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    input_tokens = safe_int(value.get("input_tokens"))
    cached = safe_int(value.get("cached_input_tokens"))
    output = safe_int(value.get("output_tokens"))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "non_cached_input_tokens": safe_int(value.get("non_cached_input_tokens"), input_tokens - cached)
        if value.get("non_cached_input_tokens") is not None
        else input_tokens - cached,
        "output_tokens": output,
        "reasoning_output_tokens": safe_int(value.get("reasoning_output_tokens")),
        "total_tokens": safe_int(value.get("total_tokens")),
    }


def normalize_model_call_row(row: dict[str, Any]) -> dict[str, Any] | None:
    session_id = str(row.get("session_id") or "")
    turn_id = str(row.get("turn_id") or "")
    if not session_id or not turn_id:
        return None
    input_tokens = safe_int(row.get("input_tokens"))
    cached = safe_int(row.get("cached_input_tokens"))
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "call_index": safe_int(row.get("call_index")),
        "timestamp": row.get("timestamp"),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "non_cached_input_tokens": safe_int(row.get("non_cached_input_tokens"), input_tokens - cached)
        if row.get("non_cached_input_tokens") is not None
        else input_tokens - cached,
        "output_tokens": safe_int(row.get("output_tokens")),
        "reasoning_output_tokens": safe_int(row.get("reasoning_output_tokens")),
        "total_tokens": safe_int(row.get("total_tokens")),
    }


def model_call_key(row: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(row.get("session_id") or ""),
        str(row.get("turn_id") or ""),
        safe_int(row.get("call_index")),
        str(row.get("timestamp") or ""),
    )
