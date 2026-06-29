"""Retention cleanup recovery and pruned turn state helpers."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import raw_segments

RETENTION_PRUNED_TURNS_RELATIVE_PATH = pathlib.Path("state") / "retention-pruned-turns.json"
RETENTION_PRUNED_TURNS_PENDING_RELATIVE_PATH = pathlib.Path("state") / "retention-pruned-turns.pending.json"
CLEANUP_RETENTION_JOB_RELATIVE_PATH = pathlib.Path("state") / "cleanup-retention-job.json"

def pruned_turn_state_path(base: pathlib.Path) -> pathlib.Path:
    return base / RETENTION_PRUNED_TURNS_RELATIVE_PATH


def pending_pruned_turn_state_path(base: pathlib.Path) -> pathlib.Path:
    return base / RETENTION_PRUNED_TURNS_PENDING_RELATIVE_PATH


def cleanup_retention_job_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / CLEANUP_RETENTION_JOB_RELATIVE_PATH


def write_cleanup_retention_job(base: pathlib.Path, job: dict[str, Any]) -> None:
    payload = dict(job)
    payload["schema_version"] = 1
    payload["base"] = str(pathlib.Path(base).expanduser().resolve())
    payload["updated_at_unix"] = time.time()
    raw_segments.write_json_atomic(cleanup_retention_job_path(base), payload)


def read_cleanup_retention_job(base: pathlib.Path) -> dict[str, Any] | None:
    path = cleanup_retention_job_path(base)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise raw_segments.ManifestError(f"cannot read cleanup retention job marker: {path}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version", 1) != 1:
        raise raw_segments.ManifestError(f"unsupported cleanup retention job marker schema: {path}")
    if parsed.get("base", str(pathlib.Path(base).expanduser().resolve())) != str(pathlib.Path(base).expanduser().resolve()):
        raise raw_segments.ManifestError(f"cleanup retention job marker base mismatch: {path}")
    return parsed


def clear_cleanup_retention_job(base: pathlib.Path) -> None:
    try:
        path = cleanup_retention_job_path(base)
        path.unlink()
        raw_segments.fsync_dir(path.parent)
    except FileNotFoundError:
        pass


def recover_retention_cleanup(base: pathlib.Path | str) -> dict[str, Any]:
    root = pathlib.Path(base).expanduser()
    sweep = raw_segments.sweep_apply_marker(root)
    job = read_cleanup_retention_job(root)
    if job is None:
        return {"raw_sweep": sweep, "job": None}
    if int(sweep.get("pending_files") or 0) > 0:
        job["phase"] = "physical_delete_pending"
        job["physical_delete_pending"] = True
        job["pending_files"] = int(sweep.get("pending_files") or 0)
        job["unlink_errors"] = sweep.get("errors") or []
        write_cleanup_retention_job(root, job)
    elif job.get("phase") in {"physical_delete_pending", "complete"}:
        clear_cleanup_retention_job(root)
    return {"raw_sweep": sweep, "job": job}


def merge_pruned_turn_state_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    cutoff_unix = 0.0
    updated_at_unix = 0.0
    for payload in payloads:
        try:
            cutoff_unix = max(cutoff_unix, float(payload.get("cutoff_unix") or 0.0))
        except (TypeError, ValueError):
            pass
        try:
            updated_at_unix = max(updated_at_unix, float(payload.get("updated_at_unix") or 0.0))
        except (TypeError, ValueError):
            pass
        for item in payload.get("pruned_turns") or []:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or "")
            turn_id = str(item.get("turn_id") or "")
            if session_id and turn_id:
                by_key[(session_id, turn_id)] = item
    if not by_key:
        return {}
    return {
        "schema_version": 1,
        "cutoff_unix": cutoff_unix,
        "updated_at_unix": updated_at_unix,
        "pruned_turns": [by_key[key] for key in sorted(by_key)],
    }


def read_pruned_turn_state(base: pathlib.Path) -> dict[str, Any]:
    payloads: list[dict[str, Any]] = []
    for path in (pruned_turn_state_path(base), pending_pruned_turn_state_path(base)):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return merge_pruned_turn_state_payloads(payloads)


def write_pruned_turn_state(base: pathlib.Path, cutoff_unix: float, turns: list[dict[str, Any]]) -> None:
    staged = stage_pruned_turn_state(base, cutoff_unix, turns)
    commit_pruned_turn_state(base, staged)


def stage_pruned_turn_state(base: pathlib.Path, cutoff_unix: float, turns: list[dict[str, Any]]) -> pathlib.Path | None:
    if not turns:
        return None
    previous = read_pruned_turn_state(base)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in previous.get("pruned_turns") or []:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "")
        turn_id = str(item.get("turn_id") or "")
        if session_id and turn_id:
            by_key[(session_id, turn_id)] = item
    for item in turns:
        session_id = str(item.get("session_id") or "")
        turn_id = str(item.get("turn_id") or "")
        if session_id and turn_id:
            by_key[(session_id, turn_id)] = item
    path = pruned_turn_state_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = pending_pruned_turn_state_path(base)
    staged = pending.with_name(f".{pending.name}.{os.getpid()}.{time.time_ns()}.tmp")
    payload = {
        "schema_version": 1,
        "cutoff_unix": float(cutoff_unix),
        "updated_at_unix": time.time(),
        "pruned_turns": [by_key[key] for key in sorted(by_key)],
    }
    staged.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    staged.chmod(0o600)
    staged.replace(pending)
    pending.chmod(0o600)
    return pending


def commit_pruned_turn_state(base: pathlib.Path, staged: pathlib.Path | None) -> None:
    if staged is None:
        return
    path = pruned_turn_state_path(base)
    staged.replace(path)
    path.chmod(0o600)


def discard_pruned_turn_state_stage(staged: pathlib.Path | None) -> None:
    if staged is None:
        return
    staged.unlink(missing_ok=True)


def pruned_turn_from_row(row: dict[str, Any], row_time: float | None) -> dict[str, Any] | None:
    session_id = str(row.get("session_id") or "")
    turn_id = str(row.get("turn_id") or "")
    if not session_id or not turn_id:
        return None
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "captured_at": row.get("captured_at"),
        "started_at": row.get("started_at"),
        "stopped_at": row.get("stopped_at"),
        "captured_at_unix": row_time,
    }

