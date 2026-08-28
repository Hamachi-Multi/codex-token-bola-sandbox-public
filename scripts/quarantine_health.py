#!/usr/bin/env python3
"""Durable quarantine health tracking without duplicating quarantined payloads."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time
from typing import Any, Iterable


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import turn_resolution


STATE_VERSION = 1
STATE_NAME = "quarantine-status.json"
NORMALIZE_BAD_NAME = "prompt-usage.bad.jsonl"


class QuarantineError(RuntimeError):
    pass


def state_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / "state" / STATE_NAME


def _error_type(error: str) -> str:
    value = str(error or "unknown")
    return value.split("(", 1)[0].split(":", 1)[0].strip() or "unknown"


def _source_identity(source: str | pathlib.Path) -> str:
    name = pathlib.Path(str(source)).name
    return name or str(source)


def event_id(*, kind: str, source: str | pathlib.Path, content: str | bytes, error: str) -> str:
    payload = content if isinstance(content, bytes) else str(content).encode("utf-8", errors="replace")
    content_digest = hashlib.sha256(payload).hexdigest()
    identity = "\x00".join((str(kind), _source_identity(source), content_digest, _error_type(error)))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "events": {}}


def _validate_state(payload: object, path: pathlib.Path) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION or not isinstance(payload.get("events"), dict):
        raise QuarantineError(f"invalid quarantine state: {path}")
    for key, value in payload["events"].items():
        if not isinstance(key, str) or not isinstance(value, dict) or value.get("event_id") != key:
            raise QuarantineError(f"invalid quarantine event: {path}")
    return payload


def load_state(base: pathlib.Path) -> dict[str, Any]:
    path = state_path(base)
    if path.parent.is_symlink() or path.is_symlink():
        raise QuarantineError(f"quarantine state path must not be a symlink: {path}")
    if not path.exists():
        return _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuarantineError(f"cannot read quarantine state: {path}: {type(exc).__name__}") from exc
    return _validate_state(payload, path)


def _write_state(base: pathlib.Path, payload: dict[str, Any]) -> None:
    path = state_path(base)
    if path.parent.is_symlink() or path.is_symlink():
        raise QuarantineError(f"quarantine state path must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise QuarantineError(f"cannot write quarantine state: {path}: {type(exc).__name__}") from exc


def _relative_evidence(base: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return str(path.resolve(strict=False).relative_to(pathlib.Path(base).expanduser().resolve(strict=False)))
    except ValueError:
        return path.name


def _entry(
    *,
    event: str,
    kind: str,
    source: str | pathlib.Path,
    error: str,
    evidence_path: str,
    captured_at_ns: int,
    line_no: int | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event,
        "kind": str(kind),
        "source": _source_identity(source),
        "line_no": line_no,
        "error_type": _error_type(error),
        "evidence_path": evidence_path,
        "occurrences": 1,
        "first_seen_at_ns": int(captured_at_ns),
        "last_seen_at_ns": int(captured_at_ns),
        "acknowledged_at_ns": None,
    }


def _merge_event(events: dict[str, Any], incoming: dict[str, Any], *, increment: bool) -> dict[str, Any]:
    event = str(incoming["event_id"])
    current = events.get(event)
    if not isinstance(current, dict):
        events[event] = incoming
        return {"event_id": event, "new_event": True, "acknowledged": False}
    current["first_seen_at_ns"] = min(int(current.get("first_seen_at_ns") or incoming["first_seen_at_ns"]), int(incoming["first_seen_at_ns"]))
    current["last_seen_at_ns"] = max(int(current.get("last_seen_at_ns") or 0), int(incoming["last_seen_at_ns"]))
    if increment:
        current["occurrences"] = int(current.get("occurrences") or 0) + 1
    current.setdefault("line_no", incoming.get("line_no"))
    current.setdefault("evidence_path", incoming.get("evidence_path"))
    acknowledged = current.get("acknowledged_at_ns") is not None
    return {"event_id": event, "new_event": False, "acknowledged": acknowledged}


def _legacy_events(base: pathlib.Path, known_evidence: set[str]) -> list[dict[str, Any]]:
    base = pathlib.Path(base).expanduser()
    bad_dir = base / "bad"
    if bad_dir.is_symlink():
        raise QuarantineError(f"quarantine evidence directory must not be a symlink: {bad_dir}")
    if not bad_dir.exists():
        return []
    result: list[dict[str, Any]] = []
    bad_log = bad_dir / NORMALIZE_BAD_NAME
    if bad_log.exists():
        if bad_log.is_symlink():
            raise QuarantineError(f"quarantine evidence must not be a symlink: {bad_log}")
        try:
            with bad_log.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise QuarantineError(f"invalid quarantine evidence: {bad_log}") from exc
                    if not isinstance(row, dict):
                        raise QuarantineError(f"invalid quarantine evidence: {bad_log}")
                    source = str(row.get("source") or "unknown")
                    content = str(row.get("line") or "")
                    error = str(row.get("error") or "unknown")
                    kind = str(row.get("kind") or "normalize_raw")
                    event = str(row.get("event_id") or event_id(kind=kind, source=source, content=content, error=error))
                    result.append(
                        _entry(
                            event=event,
                            kind=kind,
                            source=source,
                            error=error,
                            evidence_path=_relative_evidence(base, bad_log),
                            captured_at_ns=int(row.get("captured_at_ns") or bad_log.stat().st_mtime_ns),
                            line_no=int(row.get("line_no") or 0) or None,
                        )
                    )
        except OSError as exc:
            raise QuarantineError(f"cannot read quarantine evidence: {bad_log}: {type(exc).__name__}") from exc
    for path in sorted(bad_dir.iterdir()):
        relative = _relative_evidence(base, path)
        if path.is_symlink():
            raise QuarantineError(f"quarantine evidence must not be a symlink: {path}")
        if path == bad_log or not path.is_file() or relative in known_evidence:
            continue
        try:
            content = path.read_bytes()
            captured_at_ns = path.stat().st_mtime_ns
        except OSError as exc:
            raise QuarantineError(f"cannot read quarantine evidence: {path}: {type(exc).__name__}") from exc
        if path.name.startswith("token-resolution-unavailable."):
            try:
                payload = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise QuarantineError(f"invalid token resolution evidence: {path}") from exc
            if not isinstance(payload, dict) or payload.get("kind") != turn_resolution.UNAVAILABLE_KIND:
                raise QuarantineError(f"invalid token resolution evidence: {path}")
            event = str(payload.get("event_id") or "")
            source = str(payload.get("source") or payload.get("transcript_path") or "unknown")
            error = str(payload.get("error") or payload.get("token_resolution_reason") or "unknown")
            if event != turn_resolution.unavailable_event_id(str(payload.get("session_id") or ""), str(payload.get("turn_id") or ""), error):
                raise QuarantineError(f"invalid token resolution evidence id: {path}")
            result.append(
                _entry(
                    event=event,
                    kind=turn_resolution.UNAVAILABLE_KIND,
                    source=source,
                    error=error,
                    evidence_path=relative,
                    captured_at_ns=int(payload.get("captured_at_ns") or captured_at_ns),
                )
            )
            continue
        error = "legacy_bad_state"
        possible_event = path.stem.rsplit(".", 1)[-1]
        event = possible_event if len(possible_event) == 64 and all(char in "0123456789abcdef" for char in possible_event) else event_id(kind="reconcile_state", source=path.name, content=content, error=error)
        result.append(
            _entry(
                event=event,
                kind="reconcile_state",
                source=path.name,
                error=error,
                evidence_path=relative,
                captured_at_ns=captured_at_ns,
            )
        )
    return result


def _merge_legacy_index(base: pathlib.Path, state: dict[str, Any]) -> dict[str, Any]:
    events = state["events"]
    persisted_ids = set(events)
    known_evidence = {str(value.get("evidence_path") or "") for value in events.values() if isinstance(value, dict)}
    seen_legacy: set[str] = set()
    for entry in _legacy_events(base, known_evidence):
        event = str(entry["event_id"])
        if event in persisted_ids:
            continue
        increment = event in seen_legacy
        _merge_event(events, entry, increment=increment)
        seen_legacy.add(event)
    return state


def load_index(base: pathlib.Path) -> dict[str, Any]:
    return _merge_legacy_index(base, load_state(base))


def record_event(
    base: pathlib.Path,
    *,
    event: str,
    kind: str,
    source: str | pathlib.Path,
    error: str,
    evidence_path: pathlib.Path,
    captured_at_ns: int,
    line_no: int | None = None,
) -> dict[str, Any]:
    state = load_state(base)
    persisted = event in state["events"]
    _merge_legacy_index(base, state)
    incoming = _entry(
        event=event,
        kind=kind,
        source=source,
        error=error,
        evidence_path=_relative_evidence(base, evidence_path),
        captured_at_ns=captured_at_ns,
        line_no=line_no,
    )
    if persisted:
        result = _merge_event(state["events"], incoming, increment=True)
    elif event in state["events"]:
        result = {"event_id": event, "new_event": True, "acknowledged": False}
    else:
        result = _merge_event(state["events"], incoming, increment=False)
    _write_state(base, state)
    return result


def operation_summary(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(results)
    unacknowledged = {str(item["event_id"]) for item in items if not item.get("acknowledged")}
    return {
        "occurrences": len(items),
        "new_events": sum(1 for item in items if item.get("new_event")),
        "unacknowledged_events": len(unacknowledged),
        "acknowledged_occurrences": sum(1 for item in items if item.get("acknowledged")),
        "event_ids": sorted(unacknowledged)[:20],
        "event_ids_truncated": len(unacknowledged) > 20,
    }


def summary(base: pathlib.Path, *, include_entries: bool = False, include_acknowledged: bool = True) -> dict[str, Any]:
    state = load_index(base)
    entries = sorted(state["events"].values(), key=lambda item: (int(item.get("last_seen_at_ns") or 0), str(item.get("event_id") or "")), reverse=True)
    acknowledged = [item for item in entries if item.get("acknowledged_at_ns") is not None]
    unresolved = [item for item in entries if item.get("acknowledged_at_ns") is None]
    by_kind: dict[str, int] = {}
    for item in unresolved:
        kind = str(item.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    result: dict[str, Any] = {
        "total_events": len(entries),
        "unacknowledged_events": len(unresolved),
        "acknowledged_events": len(acknowledged),
        "unacknowledged_occurrences": sum(int(item.get("occurrences") or 0) for item in unresolved),
        "by_kind": by_kind,
        "latest_seen_at_ns": max((int(item.get("last_seen_at_ns") or 0) for item in entries), default=None),
        "state_path": str(state_path(base)),
    }
    if include_entries:
        selected = entries if include_acknowledged else unresolved
        result["events"] = selected
    return result


def acknowledge(base: pathlib.Path, *, event_ids: Iterable[str] | None = None, acknowledge_all: bool = False) -> dict[str, Any]:
    state = load_index(base)
    events = state["events"]
    selected = sorted(events) if acknowledge_all else sorted(set(event_ids or []))
    if not acknowledge_all and not selected:
        raise QuarantineError("at least one quarantine event id is required")
    missing = [event for event in selected if event not in events]
    if missing:
        raise QuarantineError("unknown quarantine event ids: " + ",".join(missing))
    acknowledged_at_ns = time.time_ns()
    changed = 0
    for event in selected:
        entry = events[event]
        if entry.get("acknowledged_at_ns") is None:
            entry["acknowledged_at_ns"] = acknowledged_at_ns
            changed += 1
    _write_state(base, state)
    return {
        "status": "healthy",
        "selected_events": len(selected),
        "acknowledged_events": changed,
        "already_acknowledged_events": len(selected) - changed,
        "remaining_unacknowledged_events": sum(1 for item in events.values() if item.get("acknowledged_at_ns") is None),
    }
