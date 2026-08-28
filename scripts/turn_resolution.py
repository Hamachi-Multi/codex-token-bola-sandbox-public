"""Shared token-resolution state contract for turn usage records."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from collections.abc import Mapping
from typing import Any


RESOLVED = "resolved"
PENDING = "pending"
UNAVAILABLE = "unavailable"
VALID_STATUSES = frozenset({RESOLVED, PENDING, UNAVAILABLE})
TERMINAL_STATUSES = frozenset({RESOLVED, UNAVAILABLE})
ALLOWED_TRANSITIONS = {PENDING: TERMINAL_STATUSES}
UNAVAILABLE_KIND = "token_resolution_unavailable"
LEGACY_PENDING_REASONS = frozenset({"pending_token_count", "missing_start_state", "unresolved_transcript_path"})


class TokenResolutionError(ValueError):
    pass


def status_from_row(row: Mapping[str, Any]) -> str:
    explicit = row.get("token_resolution_status")
    if explicit is not None:
        value = str(explicit)
        if value not in VALID_STATUSES:
            raise TokenResolutionError(f"unsupported token resolution status: {value}")
        return value
    if row.get("lifecycle_end_reason") in LEGACY_PENDING_REASONS and bool(row.get("estimated")):
        return PENDING
    return RESOLVED


def transition(current: str, target: str) -> str:
    if current not in VALID_STATUSES:
        raise TokenResolutionError(f"unsupported token resolution status: {current}")
    if target not in VALID_STATUSES:
        raise TokenResolutionError(f"unsupported token resolution status: {target}")
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise TokenResolutionError(f"unsupported token resolution transition: {current}->{target}")
    return target


def analytics_eligible(row: Mapping[str, Any]) -> bool:
    return status_from_row(row) == RESOLVED


def unavailable_event_id(session_id: str, turn_id: str, reason: str) -> str:
    identity = "\x00".join((UNAVAILABLE_KIND, str(session_id), str(turn_id), str(reason)))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def unavailable_evidence_path(base: pathlib.Path | str, event_id: str) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / "bad" / f"token-resolution-unavailable.{event_id}.json"


def write_unavailable_evidence(base: pathlib.Path | str, row: Mapping[str, Any]) -> tuple[str, pathlib.Path, int]:
    session_id = str(row.get("session_id") or "")
    turn_id = str(row.get("turn_id") or "")
    reason = str(row.get("token_resolution_reason") or "unknown")
    if not session_id or not turn_id:
        raise TokenResolutionError("unavailable token resolution evidence requires turn ids")
    event = unavailable_event_id(session_id, turn_id, reason)
    path = unavailable_evidence_path(base, event)
    captured_at_ns = int(row.get("captured_at_ns") or time.time_ns())
    payload = {
        "schema_version": 1,
        "event_id": event,
        "kind": UNAVAILABLE_KIND,
        "captured_at_ns": captured_at_ns,
        "session_id": session_id,
        "turn_id": turn_id,
        "source": str(row.get("transcript_path") or "unknown"),
        "error": reason,
        "token_resolution_reason": reason,
        "transcript_path": row.get("transcript_path"),
    }
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError(f"token resolution evidence path must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        return event, path, captured_at_ns
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
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
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return event, path, captured_at_ns
