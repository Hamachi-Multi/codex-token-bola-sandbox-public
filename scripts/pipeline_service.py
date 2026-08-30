"""Application service for the analysis pipeline command."""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from typing import Callable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cancel_control
import dashboard_cleanup
import progress_control
import service_lock
import service_paths
from runtime_command_runner import ProcessResult, RuntimeCommand


@dataclass(frozen=True)
class PipelineOptions:
    codex_dir: str | None = None
    output_dir: str | None = None
    state_db: str | None = None
    project_roots: tuple[str, ...] = ()
    incremental: bool = False
    recover: bool = False
    skip_rotate: bool = False


@dataclass(frozen=True)
class PipelineResult:
    exit_code: int
    payload: dict[str, object] | None = None
    process_output: ProcessResult | None = None


@dataclass(frozen=True)
class PipelineDependencies:
    resolve_paths: Callable[[str | None, str | None], service_paths.RuntimePaths]
    output_path: Callable[[str | None, str | None], pathlib.Path]
    run_command: Callable[[RuntimeCommand, list[str], dict[str, str]], ProcessResult]
    read_analytics_metadata: Callable[[str], dict[str, object]]


def int_metadata(metadata: dict[str, object], key: str, default: int = 0) -> int:
    try:
        return int(metadata.get(key, default) or 0)
    except (TypeError, ValueError):
        return default


def completed_degraded(result: ProcessResult) -> bool:
    return result.exit_code == 1 and bool(result.payload) and result.payload.get("status") == "degraded"


def combined_quarantine(*metadata_items: dict[str, object]) -> dict[str, object]:
    summaries = [item.get("quarantine") for item in metadata_items if isinstance(item.get("quarantine"), dict)]
    return {
        "occurrences": sum(int_metadata(item, "occurrences") for item in summaries),
        "new_events": sum(int_metadata(item, "new_events") for item in summaries),
        "unacknowledged_events": sum(int_metadata(item, "unacknowledged_events") for item in summaries),
        "acknowledged_occurrences": sum(int_metadata(item, "acknowledged_occurrences") for item in summaries),
        "event_ids": sorted({str(value) for item in summaries for value in item.get("event_ids", [])}),
        "event_ids_truncated": any(bool(item.get("event_ids_truncated")) for item in summaries),
    }


def run_pipeline(options: PipelineOptions, dependencies: PipelineDependencies) -> PipelineResult:
    paths = dependencies.resolve_paths(options.codex_dir, options.output_dir)
    env = {
        "CODEX_HOME": str(paths.codex_dir),
        service_paths.OUTPUT_DIR_ENV: str(paths.output_dir),
    }
    effective_output = str(dependencies.output_path(str(paths.codex_dir), str(paths.output_dir)))
    with service_lock.acquire_service_lock(reason="pipeline", output_dir=paths.output_dir) as lock:
        child_env = service_lock.child_lock_env(env, lock.path, lock.fd)
        build_args: list[str] = []
        if options.state_db:
            build_args.extend(["--state-db", options.state_db])
        for value in options.project_roots:
            build_args.extend(["--project-root", value])
        cancel_control.check_cancelled("pipeline", "start")
        progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="start", phase_progress=0.0)
        reconcile_metadata: dict[str, object] = {}
        degraded = False
        if options.recover:
            reconcile = dependencies.run_command(RuntimeCommand.RECONCILE, [], child_env)
            reconcile_metadata = reconcile.payload or {}
            reconcile_degraded = completed_degraded(reconcile)
            if reconcile.exit_code != 0 and not reconcile_degraded:
                return PipelineResult(exit_code=reconcile.exit_code, process_output=reconcile)
            degraded = degraded or reconcile_degraded
            cancel_control.check_cancelled("pipeline", "after-reconcile")

        pre_analysis_rotate: dict[str, object] = {"skipped": True}
        if not options.skip_rotate:
            cancel_control.check_cancelled("pipeline", "before-rotate")
            progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="rotate-current", phase_progress=0.02)
            rotation = dependencies.run_command(RuntimeCommand.COMPACT, ["--rotate-current"], child_env)
            pre_analysis_rotate = rotation.payload or {}
            if rotation.exit_code != 0:
                return PipelineResult(
                    exit_code=rotation.exit_code,
                    payload={"error": "rotation failed", "pre_analysis_rotate": pre_analysis_rotate},
                    process_output=rotation,
                )
        cancel_control.check_cancelled("pipeline", "after-rotate")
        progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="after-rotate", phase_progress=0.05)

        force_full_after_rotation = options.skip_rotate
        normalize_args = ["--incremental"] if options.incremental and not force_full_after_rotation else []
        normalize = dependencies.run_command(RuntimeCommand.NORMALIZE, normalize_args, child_env)
        normalize_metadata = normalize.payload or {}
        if normalize.exit_code == cancel_control.CANCEL_EXIT_CODE or normalize_metadata.get("cancelled"):
            return PipelineResult(
                exit_code=cancel_control.CANCEL_EXIT_CODE,
                payload={**normalize_metadata, "pre_analysis_rotate": pre_analysis_rotate},
            )
        normalize_degraded = completed_degraded(normalize)
        if normalize.exit_code != 0 and not normalize_degraded:
            return PipelineResult(exit_code=normalize.exit_code, process_output=normalize)
        degraded = degraded or normalize_degraded
        cancel_control.check_cancelled("pipeline", "after-normalize")
        progress_control.write_progress(phase="build", phase_index=1, checkpoint="after-normalize", phase_progress=0.0)

        effective_build_args = list(build_args)
        rebuild_reasons: list[str] = []
        if options.incremental:
            db_metadata = dependencies.read_analytics_metadata(effective_output)
            applied_turns = int_metadata(db_metadata, "applied_normalized_turns_size")
            normalized_turns_size = int_metadata(normalize_metadata, "normalized_turns_size")
            if applied_turns > normalized_turns_size:
                rebuild_reasons.append("applied_offset_beyond_normalized_size")
            if not force_full_after_rotation and normalize_metadata.get("mode") != "full" and not rebuild_reasons:
                effective_build_args.extend(["--incremental", "--turns-offset", str(applied_turns)])

        build = dependencies.run_command(RuntimeCommand.BUILD, effective_build_args, child_env)
        build_metadata = build.payload or {}
        if build.exit_code == cancel_control.CANCEL_EXIT_CODE or build_metadata.get("cancelled"):
            return PipelineResult(
                exit_code=cancel_control.CANCEL_EXIT_CODE,
                payload={"normalize": normalize_metadata, **build_metadata, "pre_analysis_rotate": pre_analysis_rotate},
            )
        if build.exit_code != 0:
            return PipelineResult(exit_code=build.exit_code, process_output=build)
        if rebuild_reasons:
            build_metadata["analysis_rebuild_reason"] = ",".join(rebuild_reasons)

        retention_recovery = dashboard_cleanup.complete_retention_derived_rebuild(paths.output_dir)
        payload = {
            "status": "degraded" if degraded else "healthy",
            "quarantine": combined_quarantine(reconcile_metadata, normalize_metadata),
            "reconcile": reconcile_metadata,
            "normalize": normalize_metadata,
            **build_metadata,
            "pre_analysis_rotate": pre_analysis_rotate,
            "retention_recovery": retention_recovery,
        }
        progress_control.write_progress(phase="refresh", phase_index=2, checkpoint="pipeline-complete", phase_progress=0.2)
        return PipelineResult(exit_code=1 if degraded else 0, payload=payload)
