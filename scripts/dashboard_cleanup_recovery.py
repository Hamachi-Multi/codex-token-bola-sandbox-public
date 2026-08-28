"""Retention cleanup recovery and pruned turn state helpers."""

from __future__ import annotations

import json
import pathlib
import sys
import time
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import raw_segments
import retention_pruned_store
from retention_models import RetentionJob, RetentionJobValidationError, RetentionPhase

RETENTION_PRUNED_TURNS_RELATIVE_PATH = pathlib.Path("state") / "retention-pruned-turns.json"
RETENTION_PRUNED_TURNS_PENDING_RELATIVE_PATH = pathlib.Path("state") / "retention-pruned-turns.pending.json"
CLEANUP_RETENTION_JOB_RELATIVE_PATH = pathlib.Path("state") / "cleanup-retention-job.json"


class RetentionJobPayload(Protocol):
    def to_payload(self) -> dict[str, Any]: ...

def pruned_turn_state_path(base: pathlib.Path) -> pathlib.Path:
    return base / RETENTION_PRUNED_TURNS_RELATIVE_PATH


def pending_pruned_turn_state_path(base: pathlib.Path) -> pathlib.Path:
    return base / RETENTION_PRUNED_TURNS_PENDING_RELATIVE_PATH


def cleanup_retention_job_path(base: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / CLEANUP_RETENTION_JOB_RELATIVE_PATH


def retention_job_model(job: RetentionJob | Mapping[str, Any] | RetentionJobPayload) -> RetentionJob:
    if isinstance(job, RetentionJob):
        return job
    payload = job if isinstance(job, Mapping) else job.to_payload()
    try:
        return RetentionJob.from_payload(payload)
    except RetentionJobValidationError as exc:
        raise raw_segments.ManifestError(f"invalid cleanup retention job marker: {exc}") from exc


def write_cleanup_retention_job(base: pathlib.Path, job: RetentionJob | Mapping[str, Any] | RetentionJobPayload) -> None:
    payload = retention_job_model(job).validate_for_write().to_payload()
    payload["schema_version"] = 1
    payload["base"] = str(pathlib.Path(base).expanduser().resolve())
    payload["updated_at_unix"] = time.time()
    raw_segments.write_json_atomic(cleanup_retention_job_path(base), payload)


def read_cleanup_retention_job_model(base: pathlib.Path) -> RetentionJob | None:
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
    return retention_job_model(parsed)


def read_cleanup_retention_job(base: pathlib.Path) -> dict[str, Any] | None:
    job = read_cleanup_retention_job_model(base)
    return None if job is None else job.to_payload()


def clear_cleanup_retention_job(base: pathlib.Path) -> None:
    try:
        path = cleanup_retention_job_path(base)
        path.unlink()
        raw_segments.fsync_dir(path.parent)
    except FileNotFoundError:
        pass


def retention_job_requires_derived_rebuild(job: RetentionJob | Mapping[str, Any] | None) -> bool:
    if job is None:
        return False
    return retention_job_model(job).requires_derived_rebuild


def recover_ready_pruned_turn_state(base: pathlib.Path, job: RetentionJob) -> RetentionJob:
    pruned_state_job_id = job.pruned_state_job_id or ""
    if job.pruned_state_commit_ready is not True or not pruned_state_job_id:
        return job
    try:
        retention_pruned_store.commit_stage(base, pruned_state_job_id)
    except retention_pruned_store.RetentionPrunedStoreError as exc:
        raise raw_segments.ManifestError(f"cannot recover retention pruned turn state: {exc}") from exc
    updated = job.transition(
        job.phase,
        clear_fields=("pruned_state_job_id", "pruned_state_commit_ready"),
        pruned_state_commit_recovered=True,
    )
    write_cleanup_retention_job(base, updated)
    return updated


def complete_retention_derived_rebuild(base: pathlib.Path | str) -> dict[str, Any]:
    root = pathlib.Path(base).expanduser()
    job = read_cleanup_retention_job_model(root)
    if not retention_job_requires_derived_rebuild(job):
        return {"updated": False, "job": None if job is None else job.to_payload()}
    assert job is not None
    job = recover_ready_pruned_turn_state(root, job)
    if job.physical_delete_pending is True:
        updated = job.transition(
            RetentionPhase.PHYSICAL_DELETE_PENDING,
            derived_rebuild_required=False,
            recovery_required=True,
        )
        write_cleanup_retention_job(root, updated)
        return {"updated": True, "job": updated.to_payload()}
    clear_cleanup_retention_job(root)
    return {"updated": True, "job": None}


def recover_retention_cleanup(base: pathlib.Path | str) -> dict[str, Any]:
    root = pathlib.Path(base).expanduser()
    sweep = raw_segments.sweep_apply_marker(root)
    job = read_cleanup_retention_job_model(root)
    if job is None:
        return {"raw_sweep": sweep, "job": None}
    if job.is_pre_derived_reset and not retention_job_requires_derived_rebuild(job):
        raw_segments.reconcile_pending_rotation(root)
        operation_job_id = job.operation_job_id or None
        pruned_state_job_id = job.pruned_state_job_id or operation_job_id
        try:
            retention_pruned_store.discard_stage(root, pruned_state_job_id)
        except retention_pruned_store.RetentionPrunedStoreError as exc:
            raise raw_segments.ManifestError(f"cannot discard interrupted retention state: {exc}") from exc
        clear_cleanup_retention_job(root)
        return {"raw_sweep": sweep, "job": None, "recovered_phase": job.phase.value}
    job = recover_ready_pruned_turn_state(root, job)
    if int(sweep.get("pending_files") or 0) > 0:
        job = job.transition(
            RetentionPhase.PHYSICAL_DELETE_PENDING,
            physical_delete_pending=True,
            pending_files=int(sweep.get("pending_files") or 0),
            unlink_errors=sweep.get("errors") or [],
        )
        write_cleanup_retention_job(root, job)
    elif job.phase in {RetentionPhase.PHYSICAL_DELETE_PENDING, RetentionPhase.COMPLETE}:
        if retention_job_requires_derived_rebuild(job):
            job = job.transition(
                RetentionPhase.DERIVED_REBUILD_REQUIRED,
                physical_delete_pending=False,
                pending_files=0,
            )
            write_cleanup_retention_job(root, job)
        else:
            clear_cleanup_retention_job(root)
    return {"raw_sweep": sweep, "job": job.to_payload()}


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


def stage_pruned_turn_state(
    base: pathlib.Path,
    cutoff_unix: float,
    turns: Iterable[dict[str, Any]],
    *,
    job_id: str | None = None,
) -> str | None:
    return retention_pruned_store.stage_rows(
        base,
        turns,
        pruned_at_unix=time.time(),
        job_id=job_id,
    )


def commit_pruned_turn_state(base: pathlib.Path, staged: str | None) -> None:
    retention_pruned_store.commit_stage(base, staged)


def discard_pruned_turn_state_stage(base: pathlib.Path, staged: str | None) -> None:
    retention_pruned_store.discard_stage(base, staged)


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
