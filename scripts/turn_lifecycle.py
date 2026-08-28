"""Pure transcript turn lifecycle reduction shared by all runtime paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import pathlib
import sys
from typing import Any, Iterable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import turn_capture

TERMINAL_TURN_EVENT_TYPES = frozenset({"task_complete", "task_aborted", "turn_aborted"})


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


def event_payload(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "event_msg":
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    return item, payload


def terminal_turn_event(event: dict[str, Any], turn_id: str | None = None) -> dict[str, Any] | None:
    parsed = event_payload(event)
    if parsed is None:
        return None
    item, payload = parsed
    payload_type = payload.get("type")
    event_turn_id = str(payload.get("turn_id") or "")
    if payload_type not in TERMINAL_TURN_EVENT_TYPES or not event_turn_id:
        return None
    if turn_id is not None and event_turn_id != turn_id:
        return None
    return {
        "type": payload_type,
        "timestamp": item.get("timestamp"),
        "turn_id": event_turn_id,
        "reason": payload.get("reason"),
        "completed_at": payload.get("completed_at"),
        "duration_ms": payload.get("duration_ms"),
        "event_offset": int(event.get("line_start") or 0),
        "bounded_file_offset": int(event.get("next_offset") or 0),
    }


@dataclass
class TurnLifecycleAccumulator:
    turn_id: str
    assume_active: bool = False
    active: bool = field(init=False)
    started_at: str | None = None
    terminal_event: dict[str, Any] | None = None
    terminal_stopped_at: str | None = None
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    usages: list[dict[str, int]] = field(default_factory=list)
    latest_total_usage: dict[str, int] | None = None
    latest_last_usage: dict[str, int] | None = None
    latest_timestamp: Any = None
    latest_context_window: Any = None
    parse_error_seen: bool = False

    def __post_init__(self) -> None:
        self.active = self.assume_active

    def feed(self, event: dict[str, Any]) -> bool:
        parsed = event_payload(event)
        if parsed is None:
            return False
        item, payload = parsed
        payload_type = payload.get("type")
        if payload_type == "task_started" and str(payload.get("turn_id") or "") == self.turn_id:
            self.active = True
            self.started_at = unix_or_timestamp_to_iso(payload.get("started_at"), item.get("timestamp"))
            self.terminal_event = None
            self.terminal_stopped_at = None
            self.model_calls.clear()
            self.usages.clear()
            self.latest_total_usage = None
            self.latest_last_usage = None
            self.latest_timestamp = None
            self.latest_context_window = None
            return False
        if not self.active:
            return False
        terminal = terminal_turn_event(event, self.turn_id)
        if terminal is not None:
            self.terminal_event = terminal
            self.terminal_stopped_at = unix_or_timestamp_to_iso(
                payload.get("aborted_at") or payload.get("completed_at"),
                item.get("timestamp"),
            )
            self.active = False
            return True
        if payload_type != "token_count":
            return False
        info = payload.get("info")
        if not isinstance(info, dict):
            return False
        last_usage = turn_capture.normalize_usage(info.get("last_token_usage"))
        self.usages.append(last_usage)
        self.model_calls.append(
            {
                "index": len(self.model_calls) + 1,
                "timestamp": item.get("timestamp"),
                "usage": last_usage,
                "model_context_window": info.get("model_context_window"),
            }
        )
        self.latest_total_usage = turn_capture.normalize_usage(info.get("total_token_usage"))
        self.latest_last_usage = last_usage
        self.latest_timestamp = item.get("timestamp")
        self.latest_context_window = info.get("model_context_window")
        return False

    @property
    def status(self) -> str | None:
        if self.terminal_event is None:
            return None
        return "aborted" if self.terminal_event["type"] in {"task_aborted", "turn_aborted"} else "completed"

    @property
    def stopped_at(self) -> str | None:
        if self.terminal_event is None:
            return None
        return self.terminal_stopped_at


def reduce_target_events(
    events: Iterable[dict[str, Any]],
    turn_id: str,
    *,
    assume_active: bool,
) -> TurnLifecycleAccumulator:
    accumulator = TurnLifecycleAccumulator(turn_id, assume_active=assume_active)
    for event in events:
        if accumulator.feed(event):
            break
    return accumulator


def full_lifecycle_snapshot(
    accumulator: TurnLifecycleAccumulator,
    *,
    path: str,
    file_size: int | None = None,
    parse_error_seen: bool = False,
    fallback_stopped_at: str | None = None,
    include_model_calls: bool = True,
    include_latest_fields: bool = True,
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "path": path,
        "event_count": len(accumulator.model_calls),
        "parse_error_seen": parse_error_seen,
    }
    if include_model_calls:
        common["model_calls"] = list(accumulator.model_calls)
    if file_size is not None:
        common["file_size"] = file_size
    if not accumulator.started_at:
        return {"found": False, "reason": "task_started_missing", **common}
    if accumulator.terminal_event is None:
        return {
            "found": False,
            "reason": "task_terminal_missing",
            "turn_started_at": accumulator.started_at,
            **common,
        }
    snapshot = {
        "found": True,
        "turn_status": accumulator.status,
        "turn_started_at": accumulator.started_at,
        "turn_stopped_at": accumulator.stopped_at or fallback_stopped_at,
        "total_token_usage": turn_capture.usage_sum(accumulator.usages),
        "token_source": "transcript_path task lifecycle token_count.info.last_token_usage aggregate",
        **common,
    }
    if include_latest_fields:
        snapshot.update(
            {
                "last_token_usage": turn_capture.normalize_usage(accumulator.latest_last_usage),
                "model_context_window": accumulator.latest_context_window,
                "timestamp": accumulator.latest_timestamp,
            }
        )
    return snapshot


def bounded_usage_snapshot(
    accumulator: TurnLifecycleAccumulator,
    *,
    path: str,
    file_size: int,
    parse_error_seen: bool,
    scan_start: int = 0,
    scan_limit: int | None = None,
    scan_limit_reached: bool = False,
) -> dict[str, Any]:
    common = {
        "path": path,
        "file_size": file_size,
        "event_count": len(accumulator.model_calls),
        "model_calls": list(accumulator.model_calls),
        "parse_error_seen": parse_error_seen,
        "scan_start": scan_start,
        "scan_limit": scan_limit,
        "scan_limit_reached": scan_limit_reached,
    }
    latest: dict[str, Any] = {}
    if accumulator.latest_total_usage is not None:
        latest = {
            "found": True,
            "timestamp": accumulator.latest_timestamp,
            "total_token_usage": accumulator.latest_total_usage,
            "last_token_usage": turn_capture.normalize_usage(accumulator.latest_last_usage),
            "model_context_window": accumulator.latest_context_window,
        }
    if accumulator.terminal_event is None:
        if scan_limit_reached:
            return {"found": bool(latest), "reason": "scan_limit_reached", **common, **latest}
        return {"found": False, "reason": "turn_end_not_found", **common}
    terminal = accumulator.terminal_event
    if not latest:
        return {
            "found": False,
            "reason": f"no_token_count_before_{terminal['type']}",
            "turn_end_event": terminal,
            **common,
        }
    return {
        **latest,
        "turn_end_event": terminal,
        "bounded_at_event_type": terminal["type"],
        "bounded_at_timestamp": terminal["timestamp"],
        "bounded_at_file_offset": terminal["bounded_file_offset"],
        "turn_end_event_offset": terminal["event_offset"],
        **common,
    }


class LifecycleIndexReducer:
    def __init__(self) -> None:
        self.current: TurnLifecycleAccumulator | None = None
        self.turns: dict[str, TurnLifecycleAccumulator] = {}
        self.parse_error_seen = False

    def mark_parse_error(self) -> None:
        self.parse_error_seen = True

    def _store_current(self) -> None:
        if self.current is None:
            return
        self.current.parse_error_seen = self.parse_error_seen
        self.turns[self.current.turn_id] = self.current

    def feed(self, event: dict[str, Any]) -> None:
        parsed = event_payload(event)
        if parsed is None:
            return
        _item, payload = parsed
        if payload.get("type") == "task_started":
            turn_id = str(payload.get("turn_id") or "")
            if not turn_id:
                return
            if self.current is not None:
                self._store_current()
            self.current = TurnLifecycleAccumulator(turn_id)
        if self.current is None:
            return
        if self.current.feed(event):
            self._store_current()
            self.current = None

    def finish(self) -> dict[str, TurnLifecycleAccumulator]:
        if self.current is not None:
            self._store_current()
            self.current = None
        return self.turns
