"""Retention prune planning and application for dashboard cleanup."""

from __future__ import annotations

import pathlib
import sys
import uuid
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dashboard_retention_index as retention_index
import dashboard_retention_preview as retention_preview
import raw_segments

from dashboard_cleanup_common import (
    is_hex_state_name,
    read_json_object,
    target_paths_size,
)
from dashboard_cleanup_recovery import (
    clear_cleanup_retention_job,
    commit_pruned_turn_state,
    discard_pruned_turn_state_stage,
    read_cleanup_retention_job,
    read_cleanup_retention_job_model,
    recover_retention_cleanup,
    retention_job_requires_derived_rebuild,
    stage_pruned_turn_state,
    write_cleanup_retention_job,
)
from retention_models import RetentionJob, RetentionPhase


def pending_turn_state_payload(path: pathlib.Path) -> dict[str, Any] | None:
    if not is_hex_state_name(path) or not path.is_file():
        return None
    data = read_json_object(path)
    if data is None or data.get("record_type") != "turn_start":
        return None
    return data


def pending_turn_state_paths(state_dir: pathlib.Path) -> list[pathlib.Path]:
    try:
        candidates = sorted(state_dir.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        return []
    return [path for path in candidates if pending_turn_state_payload(path) is not None]


def plan_pending_turn_state_for_retention(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any]:
    state_dir = pathlib.Path(base).expanduser() / "state"
    protected = pending_turn_state_paths(state_dir)
    return {
        "scanned_files": len(protected),
        "deleted_files": 0,
        "deleted_bytes": 0,
        "targets": [],
        "target_signatures": {},
        "protected_files": len(protected),
        "protected_bytes": target_paths_size(protected),
    }


def apply_pending_turn_state_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "scanned_files": int(plan.get("scanned_files") or 0),
        "deleted_files": 0,
        "deleted_bytes": 0,
        "deleted": [],
        "protected_files": int(plan.get("protected_files") or plan.get("scanned_files") or 0),
        "protected_bytes": int(plan.get("protected_bytes") or 0),
    }


def ensure_service_owned_output(base: pathlib.Path, db_path: pathlib.Path) -> pathlib.Path:
    resolved_base = pathlib.Path(base).expanduser().resolve()
    resolved_db = pathlib.Path(db_path).expanduser().resolve()
    analytics_dir = resolved_base / "analytics"
    if resolved_db == analytics_dir:
        raise ValueError(f"retention output must be a database file, not a directory: {resolved_db}")
    if not resolved_db.is_relative_to(analytics_dir):
        raise ValueError(f"retention output must be under {analytics_dir}: {resolved_db}")
    if resolved_db.exists() and not resolved_db.is_file():
        raise ValueError(f"retention output must be a database file, not a directory: {resolved_db}")
    return resolved_db


def ensure_service_owned_file(base: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    resolved_base = pathlib.Path(base).expanduser().resolve(strict=False)
    expanded = pathlib.Path(path).expanduser()
    resolved = expanded.resolve(strict=False)
    try:
        resolved.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError(f"service output must be under {resolved_base}: {resolved}") from exc
    if expanded.is_symlink() or expanded.parent.is_symlink():
        raise ValueError(f"service output must not be a symlink: {expanded}")
    if expanded.exists() and not expanded.is_file():
        raise ValueError(f"service output must be a regular file: {expanded}")
    return expanded


def reset_derived_outputs(base: pathlib.Path, db_path: pathlib.Path) -> dict[str, Any]:
    base = pathlib.Path(base).expanduser()
    db_path = ensure_service_owned_output(base, pathlib.Path(db_path).expanduser())
    targets = [
        ensure_service_owned_file(base, base / "normalized" / "prompt-usage.normalized.jsonl"),
        ensure_service_owned_file(base, base / "normalized" / "normalize-state.json"),
        ensure_service_owned_file(base, db_path),
    ]
    removed: list[str] = []
    for path in targets:
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            continue
    return {"removed": removed, "count": len(removed)}


def preflight_delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    base = pathlib.Path(token_usage_root).expanduser()
    cutoff = float(cutoff_unix)
    recover_retention_cleanup(base)
    job = read_cleanup_retention_job(base)
    if retention_job_requires_derived_rebuild(job):
        raise raw_segments.ManifestError("retention derived rebuild must complete before another prune")
    raw_segments.strict_read_manifest(base)
    raw_segments.validate_current_pointer_entries(base)
    if raw_segments.read_pending_rotation(base) is not None:
        raise raw_segments.ManifestError("pending rotation must be resolved before retention prune reset")
    if raw_segments.read_apply_status(base).pending:
        raise raw_segments.ManifestError("pending segment apply must be resolved before retention prune reset")
    segment_preflight = raw_segments.preflight_segments_older_than(base, cutoff)
    return {"base": str(base), "cutoff": cutoff, "segments": segment_preflight}


def plan_delete_logs_older_than(
    token_usage_root: pathlib.Path | str,
    cutoff_unix: float,
    *,
    expected_preview_signature: str | None = None,
    operation_job_id: str | None = None,
) -> dict[str, Any]:
    retention_preview.clear_retention_preview_cache()
    base = pathlib.Path(token_usage_root).expanduser()
    active_job = read_cleanup_retention_job_model(base)
    planning_current_operation = (
        active_job is not None
        and active_job.phase is RetentionPhase.PLANNING
        and active_job.operation_job_id == operation_job_id
    )
    if not planning_current_operation:
        recover_retention_cleanup(base)
    job = read_cleanup_retention_job(base)
    if retention_job_requires_derived_rebuild(job):
        raise raw_segments.ManifestError("retention derived rebuild must complete before another prune")
    try:
        retention_index.retention_index_path(base).unlink()
    except FileNotFoundError:
        pass
    cutoff = float(cutoff_unix)
    raw_segments.reconcile_apply_marker(base)
    raw_segments.reconcile_pending_rotation(base)
    signature = expected_preview_signature or retention_preview.retention_preview_signature(base, cutoff)
    retention_preview.freeze_retention_snapshot(base, cutoff, signature)
    pruned_stage_job_id = operation_job_id or f"retention:{uuid.uuid4().hex}"
    pruned_batch: list[dict[str, Any]] = []
    pruned_turn_count = 0
    staged_pruned_turn_state: str | None = None

    def flush_pruned_turns() -> None:
        nonlocal staged_pruned_turn_state
        if not pruned_batch:
            return
        staged = stage_pruned_turn_state(
            base,
            cutoff,
            pruned_batch,
            job_id=pruned_stage_job_id,
        )
        if staged is not None:
            staged_pruned_turn_state = staged
        pruned_batch.clear()

    def record_pruned_turn(item: dict[str, Any]) -> None:
        nonlocal pruned_turn_count
        pruned_turn_count += 1
        pruned_batch.append(item)
        if len(pruned_batch) >= 500:
            flush_pruned_turns()

    segment_plan: dict[str, Any] | None = None
    try:
        segment_plan = raw_segments.plan_segments_older_than(
            base,
            cutoff,
            pruned_turn_sink=record_pruned_turn,
        )
        pending_turn_state_plan = plan_pending_turn_state_for_retention(base, cutoff)
        flush_pruned_turns()
    except BaseException:
        discard_pruned_turn_state_stage(base, staged_pruned_turn_state or pruned_stage_job_id)
        raw_segments.discard_segment_plan_artifacts(segment_plan)
        raise
    return {
        "base": str(base),
        "cutoff": cutoff,
        "segments": segment_plan,
        "untracked": [],
        "pending_turn_state": pending_turn_state_plan,
        "pruned_turns": [],
        "pruned_turn_count": pruned_turn_count,
        "pruned_stage_job_id": staged_pruned_turn_state,
        "operation_job_id": pruned_stage_job_id,
    }


def validate_delete_logs_older_than_plan(plan: dict[str, Any]) -> dict[str, Any]:
    base = pathlib.Path(str(plan["base"]))
    segment_plan = plan.get("segments", {})
    if isinstance(segment_plan, dict):
        raw_segments.validate_segment_plans(base, segment_plan)
    untracked_plans = plan.get("untracked", [])
    if not isinstance(untracked_plans, list) or untracked_plans:
        raise raw_segments.ManifestError("untracked retention plans are not supported")
    return {"ok": True}


def discard_delete_logs_older_than_plan(plan: dict[str, Any] | None) -> None:
    if not isinstance(plan, dict):
        return
    raw_segments.discard_segment_plan_artifacts(plan.get("segments") if isinstance(plan.get("segments"), dict) else None)
    base_text = plan.get("base")
    if base_text:
        discard_pruned_turn_state_stage(
            pathlib.Path(str(base_text)),
            str(plan.get("pruned_stage_job_id") or "") or None,
        )


def apply_delete_logs_older_than_plan(plan: dict[str, Any]) -> dict[str, Any]:
    base = pathlib.Path(str(plan["base"]))
    cutoff = float(plan["cutoff"])
    untracked_plans = plan.get("untracked", [])
    if not isinstance(untracked_plans, list) or untracked_plans:
        raise raw_segments.ManifestError("untracked retention plans are not supported")
    segment_plan = plan.get("segments", {})
    pending_turn_state_plan = plan.get("pending_turn_state", {})
    pruned_turns = [item for item in plan.get("pruned_turns", []) if isinstance(item, dict)]
    if "pruned_stage_job_id" in plan:
        staged_pruned_turn_state = str(plan.get("pruned_stage_job_id") or "") or None
    else:
        staged_pruned_turn_state = stage_pruned_turn_state(base, cutoff, pruned_turns)
    pruned_turn_count = int(plan.get("pruned_turn_count") or len(pruned_turns))
    operation_fields = {
        "operation_job_id": str(plan.get("operation_job_id") or "") or f"retention:{uuid.uuid4().hex}",
        "pruned_state_job_id": staged_pruned_turn_state,
    }

    def write_job(phase: RetentionPhase, **fields: Any) -> None:
        current = read_cleanup_retention_job_model(base)
        if current is None:
            if phase is not RetentionPhase.PLANNED:
                raise raw_segments.ManifestError(f"cannot start retention apply at phase {phase.value!r}")
            job = RetentionJob.create_at(phase, **operation_fields, **fields)
        else:
            job = current.transition(phase, **fields)
        write_cleanup_retention_job(base, job)

    write_job(
        RetentionPhase.PLANNED,
        cutoff_unix=cutoff,
        deleted_rows=int(segment_plan.get("deleted_rows") or 0),
        physical_delete_pending=False,
        derived_rebuild_required=True,
        recovery_required=True,
    )
    try:
        segment_apply = raw_segments.apply_segment_plans(base, segment_plan)
        commit_pruned_turn_state(base, staged_pruned_turn_state)
        if bool(segment_apply.get("physical_delete_pending")):
            write_job(
                RetentionPhase.PHYSICAL_DELETE_PENDING,
                cutoff_unix=cutoff,
                physical_delete_pending=True,
                derived_rebuild_required=True,
                recovery_required=True,
                pending_files=int(segment_apply.get("pending_files") or 0),
                unlink_errors=segment_apply.get("unlink_errors") or [],
            )
        else:
            write_job(
                RetentionPhase.LOGICAL_DELETE_COMMITTED,
                cutoff_unix=cutoff,
                physical_delete_pending=False,
                derived_rebuild_required=True,
                recovery_required=True,
            )
        pending_turn_state_apply = apply_pending_turn_state_plan(pending_turn_state_plan)
    except Exception:
        apply_state = raw_segments.inspect_segment_apply_state(base, segment_plan)
        if apply_state is raw_segments.SegmentApplyState.NOT_STARTED:
            discard_delete_logs_older_than_plan(plan)
        failed = read_cleanup_retention_job_model(base)
        if failed is None:
            raise raw_segments.ManifestError("retention apply failed without an active job")
        clear_fields: tuple[str, ...] = ()
        if apply_state is raw_segments.SegmentApplyState.NOT_STARTED:
            clear_fields = ("pruned_state_job_id", "pruned_state_commit_ready")
        changes: dict[str, Any] = {
            "failed_stage": "apply",
            "error": repr(sys.exc_info()[1]),
            "derived_rebuild_required": True,
            "recovery_required": True,
        }
        if (
            apply_state
            in {
                raw_segments.SegmentApplyState.RECOVERY_PENDING,
                raw_segments.SegmentApplyState.LOGICAL_DELETE_COMMITTED,
            }
            and staged_pruned_turn_state
        ):
            changes["pruned_state_job_id"] = staged_pruned_turn_state
            changes["pruned_state_commit_ready"] = True
        write_cleanup_retention_job(
            base,
            failed.transition(RetentionPhase.FAILED, clear_fields=clear_fields, **changes),
        )
        raise
    deleted_rows = int(segment_plan.get("deleted_rows") or 0)
    scanned_rows = int(segment_plan.get("scanned_rows") or 0)
    segment_deleted_files = int(segment_apply.get("deleted_files") or 0)
    segment_rewritten_files = int(segment_apply.get("rewritten_files") or 0)
    deleted_state_files = int(pending_turn_state_apply.get("deleted_files") or 0)
    deleted_state_bytes = int(pending_turn_state_apply.get("deleted_bytes") or 0)
    physical_delete_pending = bool(segment_apply.get("physical_delete_pending"))
    if deleted_rows > 0 or deleted_state_files > 0 or physical_delete_pending:
        write_job(
            RetentionPhase.PHYSICAL_DELETE_PENDING if physical_delete_pending else RetentionPhase.DERIVED_REBUILD_REQUIRED,
            cutoff_unix=cutoff,
            deleted_rows=deleted_rows,
            derived_rebuild_required=True,
            physical_delete_pending=physical_delete_pending,
            pending_files=int(segment_apply.get("pending_files") or 0),
        )
    else:
        clear_cleanup_retention_job(base)
    return {
        "cutoff_unix": cutoff,
        "scanned_rows": scanned_rows,
        "deleted_rows": deleted_rows,
        "kept_rows": scanned_rows - deleted_rows,
        "deleted_bytes": int(segment_plan.get("deleted_bytes") or 0) + deleted_state_bytes,
        "physical_delete_pending": physical_delete_pending,
        "pending_files": int(segment_apply.get("pending_files") or 0),
        "unlink_errors": segment_apply.get("unlink_errors") or [],
        "deleted_state_files": deleted_state_files,
        "deleted_state_bytes": deleted_state_bytes,
        "deleted_turns": pruned_turn_count,
        "rewritten_files": segment_rewritten_files,
        "deleted_files": segment_deleted_files,
        "changed_files": segment_deleted_files + segment_rewritten_files + deleted_state_files,
        "files": [],
    }


def delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    preflight_delete_logs_older_than(token_usage_root, cutoff_unix)
    return apply_delete_logs_older_than_plan(plan_delete_logs_older_than(token_usage_root, cutoff_unix))
