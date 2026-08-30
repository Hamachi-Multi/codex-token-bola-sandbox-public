"""Human-readable rendering for quarantine command results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _timestamp(value: object) -> str | None:
    if not isinstance(value, int) or value <= 0:
        return None
    try:
        rendered = datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None
    return rendered.replace("+00:00", "Z")


def render_list(payload: dict[str, object], *, include_acknowledged: bool = False) -> str:
    quarantine = _mapping(payload.get("quarantine"))
    unresolved = int(quarantine.get("unacknowledged_events") or 0)
    occurrences = int(quarantine.get("unacknowledged_occurrences") or 0)
    acknowledged = int(quarantine.get("acknowledged_events") or 0)
    events = [event for event in quarantine.get("events", []) if isinstance(event, dict)] if isinstance(quarantine.get("events"), list) else []
    status = "NEEDS REVIEW" if unresolved else "HEALTHY"
    lines = [
        f"BOLA Quarantine: {status}",
        "",
        f"Unacknowledged events: {unresolved}",
        f"Unacknowledged occurrences: {occurrences}",
        f"Acknowledged events: {acknowledged}",
        "",
        "Events",
    ]
    if not events:
        lines.append("No events need review" if not include_acknowledged else "No quarantine events found")
    for index, event in enumerate(events, start=1):
        event_id = str(event.get("event_id") or "unknown")
        event_status = "ACKNOWLEDGED" if event.get("acknowledged_at_ns") is not None else "UNRESOLVED"
        lines.extend(
            (
                f"[{index}] [{event_status}] {event.get('kind') or 'unknown'}",
                f"  Event ID: {event_id}",
                f"  Source: {event.get('source') or 'unknown'}",
                f"  Reason: {event.get('error_type') or 'unknown'}",
                f"  Occurrences: {int(event.get('occurrences') or 0)}",
            )
        )
        if event.get("line_no") is not None:
            lines.append(f"  Line: {event['line_no']}")
        last_seen = _timestamp(event.get("last_seen_at_ns"))
        if last_seen:
            lines.append(f"  Last seen: {last_seen}")
        lines.append(f"  Evidence: {event.get('evidence_path') or 'unknown'}")
        if event_status == "UNRESOLVED":
            lines.append(f"  Review: bola quarantine acknowledge --event-id {event_id}")
    full_command = "bola quarantine list --include-acknowledged --json" if include_acknowledged else "bola quarantine list --json"
    lines.extend(("", f"Full report: {full_command}"))
    return "\n".join(lines)


def render_acknowledge(payload: dict[str, object]) -> str:
    acknowledged = int(payload.get("acknowledged_events") or 0)
    status = "UPDATED" if acknowledged else "UNCHANGED"
    return "\n".join(
        (
            f"BOLA Quarantine: {status}",
            "",
            f"Selected events: {int(payload.get('selected_events') or 0)}",
            f"Acknowledged now: {acknowledged}",
            f"Already acknowledged: {int(payload.get('already_acknowledged_events') or 0)}",
            f"Remaining unacknowledged: {int(payload.get('remaining_unacknowledged_events') or 0)}",
            "Evidence retained: yes",
            "",
            "Full report: bola quarantine list --json",
        )
    )


def render_error(message: str) -> str:
    return "\n".join(("BOLA Quarantine: FAILED", "", f"Error: {message}", "Rerun the command with --json for machine-readable output"))
