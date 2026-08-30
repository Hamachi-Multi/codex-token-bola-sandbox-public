"""Application services for retention preview and prune commands."""

from __future__ import annotations

import os
import pathlib
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dashboard_cleanup
import pipeline_service
import progress_control
import raw_segments
import retention_checkpoints
import service_lock
import service_paths
from retention_models import RetentionJob, RetentionJobValidationError, RetentionPhase
from runtime_command_runner import ProcessResult, RuntimeCommand


@dataclass(frozen=True)
class RetentionPreviewOptions:
    cutoff: str
    codex_dir: str | None = None
    output_dir: str | None = None


@dataclass(frozen=True)
class RetentionPruneOptions:
    cutoff: str
    preview_signature: str | None = None
    codex_dir: str | None = None
    output_dir: str | None = None


@dataclass(frozen=True)
class RetentionResult:
    exit_code: int
    payload: dict[str, object] | None = None
    process_output: ProcessResult | None = None


@dataclass(frozen=True)
class RetentionDependencies:
    resolve_paths: Callable[[str | None, str | None], service_paths.RuntimePaths]
    db_path: Callable[[str | None, str | None], pathlib.Path]
    run_command: Callable[[RuntimeCommand, list[str], dict[str, str]], ProcessResult]
    create_checkpoint: Callable[[pathlib.Path, str], retention_checkpoints.RetentionCheckpoint | dict[str, object]]
    restore_checkpoint: Callable[[pathlib.Path, retention_checkpoints.RetentionCheckpoint | dict[str, object]], None]
    discard_checkpoint: Callable[[retention_checkpoints.RetentionCheckpoint | dict[str, object]], None]


class RetentionExecutionError(RuntimeError):
    def __init__(self, cause: Exception, payload: dict[str, object] | None = None) -> None:
        self.cause = cause
        self.payload = payload
        super().__init__(str(cause))


def parse_cutoff(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def run_retention_preview(options: RetentionPreviewOptions, resolve_paths: Callable[[str | None, str | None], service_paths.RuntimePaths]) -> RetentionResult:
    try:
        cutoff = parse_cutoff(options.cutoff)
    except ValueError as exc:
        return RetentionResult(
            exit_code=2,
            payload={"error": "cutoff_date_invalid", "cutoff": options.cutoff, "message": str(exc)},
        )
    paths = resolve_paths(options.codex_dir, options.output_dir)
    try:
        preview = dashboard_cleanup.retention_preview(paths.output_dir, cutoff, refresh_index=False)
        signature = dashboard_cleanup.retention_preview_signature(paths.output_dir, cutoff)
    except raw_segments.ManifestError as exc:
        return RetentionResult(exit_code=2, payload={"error": "cleanup_preview_failed", "message": str(exc)})
    affected = [item for item in preview.get("files", []) if isinstance(item, dict) and item.get("affected")]
    delete_files = sum(1 for item in affected if int(item.get("deletable_rows") or 0) >= int(item.get("scanned_rows") or 0))
    return RetentionResult(
        exit_code=0,
        payload={
            "cutoff_unix": float(cutoff),
            "preview_signature": signature,
            "scanned_rows": int(preview.get("scanned_rows") or 0),
            "deletable_rows": int(preview.get("deletable_rows") or 0),
            "deletable_bytes": int(preview.get("deletable_bytes") or 0),
            "affected_files": int(preview.get("affected_files") or 0),
            "delete_files": delete_files,
            "rewrite_files": max(0, len(affected) - delete_files),
        },
    )


def run_retention_prune(options: RetentionPruneOptions, dependencies: RetentionDependencies) -> RetentionResult:
    try:
        cutoff = parse_cutoff(options.cutoff)
    except ValueError as exc:
        return RetentionResult(
            exit_code=2,
            payload={
                "error": "cutoff_date_invalid",
                "stage": "preview",
                "cutoff": options.cutoff,
                "message": str(exc),
            },
        )
    paths = dependencies.resolve_paths(options.codex_dir, options.output_dir)
    base = paths.output_dir
    db_path = dependencies.db_path(str(paths.codex_dir), str(paths.output_dir))
    env = service_lock.scrub_lock_env(os.environ.copy())
    env.update({"CODEX_HOME": str(paths.codex_dir), service_paths.OUTPUT_DIR_ENV: str(paths.output_dir)})
    with service_lock.acquire_service_lock(reason="retention-prune", output_dir=paths.output_dir) as lock:
        child_env = service_lock.child_lock_env(env, lock.path, lock.fd)
        child_env.pop(progress_control.PROGRESS_ENV, None)
        degraded = False
        preview_signature = str(options.preview_signature or "")
        if not preview_signature:
            return RetentionResult(exit_code=2, payload={"error": "cleanup_preview_signature_required", "stage": "preview"})
        if dashboard_cleanup.retention_preview_signature(base, cutoff) != preview_signature:
            return RetentionResult(exit_code=2, payload={"error": "cleanup_preview_stale", "stage": "preview"})
        progress_control.write_progress(phase="cleanup-prepare", phase_index=0, phase_count=4, checkpoint="preflight", phase_progress=0.05)
        dashboard_cleanup.preflight_delete_logs_older_than(base, cutoff)
        operation_job_id = f"retention:{uuid.uuid4().hex}"
        try:
            retention_checkpoints.sweep(base)
        except retention_checkpoints.RetentionCheckpointError as exc:
            return RetentionResult(
                exit_code=2,
                payload={"error": "retention_checkpoint_cleanup_failed", "stage": "checkpoint", "message": str(exc)},
            )

        operation_job: RetentionJob | None = None

        def write_operation_job(phase: RetentionPhase, **fields: Any) -> None:
            nonlocal operation_job
            if operation_job is None:
                if phase is not RetentionPhase.PREPARING_SNAPSHOT:
                    raise RetentionJobValidationError(
                        f"cannot start retention job at phase {phase.value!r}"
                    )
                cutoff_value = fields.pop("cutoff_unix")
                job = RetentionJob.begin(
                    operation_job_id=operation_job_id,
                    cutoff_unix=cutoff_value,
                    **fields,
                )
            else:
                job = operation_job.transition(phase, **fields)
            dashboard_cleanup.write_cleanup_retention_job(
                base,
                job,
            )
            operation_job = job

        write_operation_job(
            RetentionPhase.PREPARING_SNAPSHOT,
            cutoff_unix=cutoff,
            derived_rebuild_required=False,
            recovery_required=True,
            physical_delete_pending=False,
        )
        progress_control.write_progress(phase="cleanup-prepare", phase_index=0, phase_count=4, checkpoint="checkpoint", phase_progress=0.25)
        try:
            checkpoint = dependencies.create_checkpoint(base, operation_job_id)
        except Exception:
            dashboard_cleanup.clear_cleanup_retention_job(base)
            raise
        write_operation_job(
            RetentionPhase.SNAPSHOT_PREPARED,
            cutoff_unix=cutoff,
            derived_rebuild_required=False,
            recovery_required=True,
            physical_delete_pending=False,
        )
        reset_started = False
        delete_plan: dict[str, Any] | None = None
        try:
            progress_control.write_progress(phase="cleanup-prepare", phase_index=0, phase_count=4, checkpoint="plan-retention", phase_progress=0.55)
            write_operation_job(
                RetentionPhase.PLANNING,
                cutoff_unix=cutoff,
                derived_rebuild_required=False,
                recovery_required=True,
                physical_delete_pending=False,
            )
            delete_plan = dashboard_cleanup.plan_delete_logs_older_than(
                base,
                cutoff,
                expected_preview_signature=preview_signature,
                operation_job_id=operation_job_id,
            )
            planned_rows = int((delete_plan.get("segments") or {}).get("deleted_rows") or 0) + sum(
                int(item.get("deleted_rows") or 0) for item in delete_plan.get("untracked", []) if isinstance(item, dict)
            )
            progress_control.write_progress(
                phase="cleanup-prepare",
                phase_index=0,
                phase_count=4,
                checkpoint="reset-derived",
                phase_progress=0.9,
                processed=planned_rows,
                total=max(1, planned_rows),
            )
            dashboard_cleanup.validate_delete_logs_older_than_plan(delete_plan)
            write_operation_job(
                RetentionPhase.DERIVED_RESET_PENDING,
                pruned_state_job_id=delete_plan.get("pruned_stage_job_id"),
                cutoff_unix=cutoff,
                deleted_rows=planned_rows,
                derived_rebuild_required=True,
                recovery_required=True,
                physical_delete_pending=False,
            )
            reset_started = True
            reset_result = dashboard_cleanup.reset_derived_outputs(base, db_path)
            write_operation_job(
                RetentionPhase.DERIVED_RESET_COMPLETE,
                pruned_state_job_id=delete_plan.get("pruned_stage_job_id"),
                cutoff_unix=cutoff,
                deleted_rows=planned_rows,
                derived_rebuild_required=True,
                recovery_required=True,
                physical_delete_pending=False,
                reset=reset_result,
            )
        except dashboard_cleanup.RetentionPreviewStale:
            dashboard_cleanup.discard_delete_logs_older_than_plan(delete_plan)
            dependencies.restore_checkpoint(base, checkpoint)
            dashboard_cleanup.clear_cleanup_retention_job(base)
            return RetentionResult(exit_code=2, payload={"error": "cleanup_preview_stale", "stage": "preview"})
        except KeyboardInterrupt:
            progress_control.write_progress(
                phase="cleanup-prepare", phase_index=0, phase_count=4, status="failed", checkpoint="restore-checkpoint", phase_progress=0.0
            )
            dashboard_cleanup.discard_delete_logs_older_than_plan(delete_plan)
            dependencies.restore_checkpoint(base, checkpoint)
            payload = None
            if reset_started:
                write_operation_job(
                    RetentionPhase.FAILED,
                    failed_stage="interrupted",
                    recovery_required=True,
                    derived_rebuild_required=True,
                    physical_delete_pending=False,
                    cutoff_unix=cutoff,
                    deleted_rows=0,
                )
                payload = {
                    "error": "retention_reset_interrupted",
                    "stage": "reset",
                    "partial_mutation": True,
                    "recovery_required": True,
                    "derived_rebuild_required": True,
                    "deleted_rows": 0,
                }
            else:
                dashboard_cleanup.clear_cleanup_retention_job(base)
            return RetentionResult(exit_code=130, payload=payload)
        except Exception as exc:
            progress_control.write_progress(
                phase="cleanup-prepare", phase_index=0, phase_count=4, status="failed", checkpoint="restore-checkpoint", phase_progress=0.0
            )
            dashboard_cleanup.discard_delete_logs_older_than_plan(delete_plan)
            dependencies.restore_checkpoint(base, checkpoint)
            payload = None
            if reset_started:
                write_operation_job(
                    RetentionPhase.FAILED,
                    failed_stage="reset",
                    error=repr(exc),
                    cutoff_unix=cutoff,
                    deleted_rows=0,
                    derived_rebuild_required=True,
                    recovery_required=True,
                    physical_delete_pending=False,
                )
                payload = {
                    "error": "retention_reset_failed",
                    "stage": "reset",
                    "message": str(exc),
                    "partial_mutation": True,
                    "recovery_required": True,
                    "derived_rebuild_required": True,
                    "deleted_rows": 0,
                }
            else:
                dashboard_cleanup.clear_cleanup_retention_job(base)
            raise RetentionExecutionError(exc, payload) from exc

        dependencies.discard_checkpoint(checkpoint)
        delete_result: dict[str, Any] | None = None
        try:
            progress_control.write_progress(
                phase="cleanup-delete",
                phase_index=1,
                phase_count=4,
                checkpoint="apply-retention",
                phase_progress=0.05,
                processed=0,
                total=max(1, planned_rows),
            )
            delete_result = dashboard_cleanup.apply_delete_logs_older_than_plan(delete_plan)
            persisted_job = dashboard_cleanup.read_cleanup_retention_job_model(base)
            if persisted_job is not None and persisted_job.operation_job_id == operation_job_id:
                operation_job = persisted_job
            elif operation_job is not None and operation_job.phase is RetentionPhase.DERIVED_RESET_COMPLETE:
                operation_job = operation_job.transition(
                    RetentionPhase.PLANNED,
                    deleted_rows=int(delete_result.get("deleted_rows") or 0),
                    derived_rebuild_required=True,
                    physical_delete_pending=False,
                    pending_files=0,
                )
                if bool(delete_result.get("physical_delete_pending")):
                    operation_job = operation_job.transition(
                        RetentionPhase.PHYSICAL_DELETE_PENDING,
                        physical_delete_pending=True,
                        pending_files=int(delete_result.get("pending_files") or 0),
                    )
                else:
                    operation_job = operation_job.transition(RetentionPhase.LOGICAL_DELETE_COMMITTED)
            progress_control.write_progress(
                phase="cleanup-delete",
                phase_index=1,
                phase_count=4,
                checkpoint="retention-applied",
                phase_progress=0.9,
                processed=int(delete_result.get("deleted_rows") or 0),
                total=max(1, int(delete_result.get("scanned_rows") or 0)),
            )
            write_operation_job(
                RetentionPhase.DERIVED_REBUILD_REQUIRED,
                cutoff_unix=cutoff,
                deleted_rows=delete_result["deleted_rows"],
                physical_delete_pending=bool(delete_result.get("physical_delete_pending")),
                pending_files=int(delete_result.get("pending_files") or 0),
                delete=delete_result,
            )
            progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, checkpoint="normalize", phase_progress=0.0)
            normalize = dependencies.run_command(RuntimeCommand.NORMALIZE, [], child_env)
            normalize_result = normalize.payload or {}
            normalize_degraded = pipeline_service.completed_degraded(normalize)
            if normalize.exit_code != 0 and not normalize_degraded:
                progress_control.write_progress(
                    phase="cleanup-rebuild", phase_index=2, phase_count=4, status="failed", checkpoint="normalize-failed", phase_progress=0.1
                )
                write_operation_job(
                    RetentionPhase.FAILED,
                    failed_stage="normalize",
                    derived_rebuild_required=True,
                    physical_delete_pending=bool(delete_result.get("physical_delete_pending")),
                    pending_files=int(delete_result.get("pending_files") or 0),
                    cutoff_unix=cutoff,
                    deleted_rows=delete_result["deleted_rows"],
                )
                return RetentionResult(
                    exit_code=normalize.exit_code,
                    process_output=normalize,
                    payload={
                        "error": "retention_rebuild_failed",
                        "stage": "normalize",
                        "partial_mutation": True,
                        "recovery_required": True,
                        "derived_rebuild_required": True,
                        "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                        "pending_files": int(delete_result.get("pending_files") or 0),
                        "deleted_rows": delete_result["deleted_rows"],
                        "delete": delete_result,
                        "reset": reset_result,
                        "normalize": normalize_result,
                    },
                )
            degraded = degraded or normalize_degraded
            progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, checkpoint="build", phase_progress=0.45)
            build = dependencies.run_command(RuntimeCommand.BUILD, [], child_env)
            build_result = build.payload or {}
            if build.exit_code != 0:
                progress_control.write_progress(
                    phase="cleanup-rebuild", phase_index=2, phase_count=4, status="failed", checkpoint="build-failed", phase_progress=0.6
                )
                write_operation_job(
                    RetentionPhase.FAILED,
                    failed_stage="build",
                    derived_rebuild_required=True,
                    physical_delete_pending=bool(delete_result.get("physical_delete_pending")),
                    pending_files=int(delete_result.get("pending_files") or 0),
                    cutoff_unix=cutoff,
                    deleted_rows=delete_result["deleted_rows"],
                )
                return RetentionResult(
                    exit_code=build.exit_code,
                    process_output=build,
                    payload={
                        "error": "retention_rebuild_failed",
                        "stage": "build",
                        "partial_mutation": True,
                        "recovery_required": True,
                        "derived_rebuild_required": True,
                        "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                        "pending_files": int(delete_result.get("pending_files") or 0),
                        "deleted_rows": delete_result["deleted_rows"],
                        "delete": delete_result,
                        "reset": reset_result,
                        "normalize": normalize_result,
                        "build": build_result,
                    },
                )
        except KeyboardInterrupt:
            progress_control.write_progress(
                phase="cleanup-rebuild", phase_index=2, phase_count=4, status="failed", checkpoint="interrupted", phase_progress=0.0
            )
            pending_files = int((delete_result or {}).get("pending_files") or 0)
            deleted_rows = int((delete_result or {}).get("deleted_rows") or 0)
            write_operation_job(
                RetentionPhase.FAILED,
                failed_stage="interrupted",
                derived_rebuild_required=True,
                recovery_required=True,
                physical_delete_pending=bool((delete_result or {}).get("physical_delete_pending")),
                pending_files=pending_files,
                cutoff_unix=cutoff,
                deleted_rows=deleted_rows,
            )
            return RetentionResult(exit_code=130)

        progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, checkpoint="rebuild-complete", phase_progress=1.0)
        assert delete_result is not None
        if bool(delete_result.get("physical_delete_pending")):
            write_operation_job(
                RetentionPhase.PHYSICAL_DELETE_PENDING,
                physical_delete_pending=True,
                derived_rebuild_required=False,
                pending_files=int(delete_result.get("pending_files") or 0),
                cutoff_unix=cutoff,
                deleted_rows=delete_result["deleted_rows"],
            )
        else:
            dashboard_cleanup.clear_cleanup_retention_job(base)
        return RetentionResult(
            exit_code=1 if degraded else 0,
            payload={
                "status": "degraded" if degraded else "healthy",
                "quarantine": pipeline_service.combined_quarantine(normalize_result),
                "deleted_rows": delete_result["deleted_rows"],
                "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                "pending_files": int(delete_result.get("pending_files") or 0),
                "delete": delete_result,
                "reset": reset_result,
                "normalize": normalize_result,
                "build": build_result,
            },
        )
