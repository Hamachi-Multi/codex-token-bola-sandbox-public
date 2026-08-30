#!/usr/bin/env python3
"""Developer CLI for Codex Token Bola."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import NoReturn

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
for import_dir in (SCRIPT_DIR, SCRIPT_DIR.parent):
    if str(import_dir) not in sys.path:
        sys.path.insert(0, str(import_dir))

from codex_token_bola import __version__

import service_lock
import service_paths
import dashboard_cleanup  # noqa: F401 - compatibility patch point for command tests
import cancel_control
import progress_control  # noqa: F401 - compatibility patch point for command tests
import raw_segments  # noqa: F401 - compatibility facade for command tests and integrations
import analysis_inputs
import quarantine_health
import quarantine_renderer
import retention_checkpoints
import doctor_renderer
import doctor_service
import hook_service
import paths_service
import pipeline_service
import retention_service
import runtime_command_service
from runtime_command_runner import ProcessResult, RuntimeCommand, SubprocessRuntimeCommandRunner, parse_last_json_object


CODEX_DIR_FILE_MARKERS = ("auth.json", "config.toml", "history.jsonl")
CODEX_DIR_DIRECTORY_MARKERS = ("sessions",)
CODEX_CLI_CHECK_TIMEOUT_SECONDS = 5
HOOK_RUNTIME_CHECK_TIMEOUT_SECONDS = 5
DOCTOR_RECENT_ERROR_WINDOW_SECONDS = 24 * 60 * 60
DOCTOR_STALE_PENDING_STATE_SECONDS = 24 * 60 * 60
DOCTOR_STALE_ANALYTICS_TMP_SECONDS = 60 * 60


class CodexDirValidationError(RuntimeError):
    def __init__(self, status: dict[str, object]):
        self.status = status
        super().__init__(str(status["message"]))

    def payload(self) -> dict[str, object]:
        return {
            "error": "codex_dir_invalid",
            "reason": self.status["reason"],
            "path": self.status["path"],
            "message": self.status["message"],
        }


class CodexCliValidationError(RuntimeError):
    def __init__(self, status: dict[str, object]):
        self.status = status
        super().__init__(str(status["message"]))

    def payload(self) -> dict[str, object]:
        return {
            "error": "codex_cli_invalid",
            "reason": self.status["reason"],
            "path": self.status["path"],
            "message": self.status["message"],
        }


class HookRuntimeValidationError(RuntimeError):
    def __init__(self, status: dict[str, object]):
        self.status = status
        super().__init__(str(status["message"]))

    def payload(self) -> dict[str, object]:
        return {
            "error": "hook_runtime_invalid",
            "reason": self.status["reason"],
            "interpreter": self.status["interpreter"],
            "module": self.status["module"],
            "message": self.status["message"],
        }


PathMigrationBlocked = paths_service.PathMigrationBlocked


def invalid_codex_dir_status(path: pathlib.Path, reason: str, message: str, **fields: object) -> dict[str, object]:
    issue = {"reason": reason, "message": message}
    return {
        "path": str(path),
        "exists": path.exists(),
        "bytes": None,
        "is_directory": path.is_dir(),
        "initialized": False,
        "writable": False,
        "markers": [],
        "valid": False,
        "reason": reason,
        "message": message,
        "issues": [issue],
        **fields,
    }


def codex_dir_status(codex_dir: str | pathlib.Path) -> dict[str, object]:
    path = pathlib.Path(codex_dir).expanduser().resolve(strict=False)
    if not path.exists():
        return invalid_codex_dir_status(path, "not_found", f"CODEX_HOME does not exist: {path}")
    if not path.is_dir():
        return invalid_codex_dir_status(path, "not_directory", f"CODEX_HOME is not a directory: {path}")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return invalid_codex_dir_status(path, "not_writable", f"CODEX_HOME is not readable, searchable, and writable: {path}")

    markers = [name for name in CODEX_DIR_FILE_MARKERS if (path / name).is_file()]
    markers.extend(name for name in CODEX_DIR_DIRECTORY_MARKERS if (path / name).is_dir())
    try:
        markers.extend(candidate.name for candidate in sorted(path.glob("state_*.sqlite")) if candidate.is_file())
    except OSError as exc:
        return invalid_codex_dir_status(path, "not_writable", f"CODEX_HOME cannot be inspected: {path}: {exc}")
    markers = sorted(set(markers))
    if not markers:
        return invalid_codex_dir_status(
            path,
            "not_initialized",
            f"CODEX_HOME does not contain Codex initialization markers; run Codex once before installing the hook: {path}",
            writable=True,
        )

    hooks_path = path / "hooks.json"
    if hooks_path.is_symlink() or (hooks_path.exists() and not hooks_path.is_file()):
        return invalid_codex_dir_status(
            path,
            "hooks_json_invalid",
            f"Codex hooks file must be a regular JSON file: {hooks_path}",
            initialized=True,
            writable=True,
            markers=markers,
        )
    if hooks_path.exists():
        try:
            hooks_payload = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return invalid_codex_dir_status(
                path,
                "hooks_json_invalid",
                f"Codex hooks file is not readable valid JSON: {hooks_path}: {exc}",
                initialized=True,
                writable=True,
                markers=markers,
            )
        if not isinstance(hooks_payload, dict):
            return invalid_codex_dir_status(
                path,
                "hooks_json_invalid",
                f"Codex hooks file must contain a JSON object: {hooks_path}",
                initialized=True,
                writable=True,
                markers=markers,
            )

    return {
        "path": str(path),
        "exists": True,
        "bytes": None,
        "is_directory": True,
        "initialized": True,
        "writable": True,
        "markers": markers,
        "valid": True,
        "reason": None,
        "message": None,
        "issues": [],
    }


def require_valid_codex_dir(codex_dir: str | pathlib.Path) -> dict[str, object]:
    status = codex_dir_status(codex_dir)
    if not status["valid"]:
        raise CodexDirValidationError(status)
    return status


def codex_cli_status() -> dict[str, object]:
    executable = shutil.which("codex")
    if not executable:
        return {
            "valid": False,
            "path": None,
            "version": None,
            "reason": "not_found",
            "message": "Codex CLI was not found in PATH",
        }
    path = str(pathlib.Path(executable).expanduser().resolve(strict=False))
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=CODEX_CLI_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "valid": False,
            "path": path,
            "version": None,
            "reason": "timeout",
            "message": f"Codex CLI version check timed out after {CODEX_CLI_CHECK_TIMEOUT_SECONDS} seconds: {path}",
        }
    except OSError as exc:
        return {
            "valid": False,
            "path": path,
            "version": None,
            "reason": "execution_failed",
            "message": f"Codex CLI could not be executed: {path}: {exc}",
        }
    version = next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)
    if result.returncode != 0:
        return {
            "valid": False,
            "path": path,
            "version": version,
            "reason": "execution_failed",
            "message": f"Codex CLI version check failed with exit code {result.returncode}: {path}",
        }
    return {"valid": True, "path": path, "version": version, "reason": None, "message": None}


def require_valid_codex_cli() -> dict[str, object]:
    status = codex_cli_status()
    if not status["valid"]:
        raise CodexCliValidationError(status)
    return status


def hook_runtime_status() -> dict[str, object]:
    module = "codex_token_bola.hook"
    interpreter = os.path.abspath(os.path.expanduser(sys.executable))
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    try:
        with tempfile.TemporaryDirectory(prefix="bola-hook-runtime-") as tmp_dir:
            result = subprocess.run(
                [interpreter, "-c", f"import {module}"],
                cwd=tmp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=HOOK_RUNTIME_CHECK_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired:
        return {
            "valid": False,
            "interpreter": interpreter,
            "module": module,
            "reason": "timeout",
            "message": f"Hook runtime import timed out after {HOOK_RUNTIME_CHECK_TIMEOUT_SECONDS} seconds: {module}",
        }
    except OSError as exc:
        return {
            "valid": False,
            "interpreter": interpreter,
            "module": module,
            "reason": "execution_failed",
            "message": f"Hook interpreter could not be executed: {interpreter}: {exc}",
        }
    if result.returncode != 0:
        return {
            "valid": False,
            "interpreter": interpreter,
            "module": module,
            "reason": "module_not_importable",
            "message": (
                f"{module} is not importable by {interpreter} outside this checkout; "
                f"install it first with: {shlex.join([interpreter, '-m', 'pip', 'install', '.'])}"
            ),
        }
    return {
        "valid": True,
        "interpreter": interpreter,
        "module": module,
        "reason": None,
        "message": None,
    }


def require_valid_hook_runtime() -> dict[str, object]:
    status = hook_runtime_status()
    if not status["valid"]:
        raise HookRuntimeValidationError(status)
    return status


DEFAULT_RUNTIME_COMMAND_RUNNER = SubprocessRuntimeCommandRunner(SCRIPT_DIR, sys.executable)


def run_script(name: str, extra_args: list[str], env: dict[str, str] | None = None) -> int:
    result = DEFAULT_RUNTIME_COMMAND_RUNNER.run(
        RuntimeCommand.from_script_name(name),
        extra_args,
        env=env,
        capture_json=False,
    )
    return result.exit_code


def replace_script(name: str, extra_args: list[str], env: dict[str, str] | None = None) -> NoReturn:
    DEFAULT_RUNTIME_COMMAND_RUNNER.replace(
        RuntimeCommand.from_script_name(name),
        extra_args,
        env=env,
    )


def run_script_json(name: str, extra_args: list[str], env: dict[str, str] | None = None) -> tuple[int, dict[str, object], str, str]:
    result = DEFAULT_RUNTIME_COMMAND_RUNNER.run(
        RuntimeCommand.from_script_name(name),
        extra_args,
        env=env,
        capture_json=True,
    )
    return result.exit_code, result.payload or {}, result.stdout, result.stderr


def run_typed_script_json(command: RuntimeCommand, extra_args: list[str], env: dict[str, str]) -> ProcessResult:
    code, payload, stdout, stderr = run_script_json(command.value, extra_args, env=env)
    parsed_payload, parse_error = parse_last_json_object(stdout)
    if payload:
        parsed_payload = payload
        parse_error = None
    return ProcessResult(
        command=command,
        exit_code=code,
        payload=parsed_payload,
        stdout=stdout,
        stderr=stderr,
        parse_error=parse_error,
    )


def emit_process_output(result: ProcessResult | None) -> None:
    if result is None:
        return
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def completed_degraded(returncode: int, metadata: dict[str, object]) -> bool:
    return returncode == 1 and metadata.get("status") == "degraded"


def combined_quarantine(*metadata_items: dict[str, object]) -> dict[str, object]:
    summaries = [item.get("quarantine") for item in metadata_items if isinstance(item.get("quarantine"), dict)]
    event_ids = sorted(
        {
            str(event)
            for summary in summaries
            if isinstance(summary, dict)
            for event in (summary.get("event_ids") if isinstance(summary.get("event_ids"), list) else [])
        }
    )
    return {
        "occurrences": sum(int(summary.get("occurrences") or 0) for summary in summaries if isinstance(summary, dict)),
        "new_events": sum(int(summary.get("new_events") or 0) for summary in summaries if isinstance(summary, dict)),
        "unacknowledged_events": sum(int(summary.get("unacknowledged_events") or 0) for summary in summaries if isinstance(summary, dict)),
        "acknowledged_occurrences": sum(int(summary.get("acknowledged_occurrences") or 0) for summary in summaries if isinstance(summary, dict)),
        "event_ids": event_ids[:20],
        "event_ids_truncated": len(event_ids) > 20 or any(bool(summary.get("event_ids_truncated")) for summary in summaries if isinstance(summary, dict)),
    }


def runtime_paths(
    codex_dir: str | pathlib.Path | None = None,
    output_dir: str | pathlib.Path | None = None,
) -> service_paths.RuntimePaths:
    return service_paths.resolve_runtime_paths(codex_dir=codex_dir, output_dir=output_dir)


def runtime_env(
    codex_dir: str | pathlib.Path | None = None,
    output_dir: str | pathlib.Path | None = None,
) -> dict[str, str]:
    paths = runtime_paths(codex_dir, output_dir)
    return {"CODEX_HOME": str(paths.codex_dir), service_paths.OUTPUT_DIR_ENV: str(paths.output_dir)}


def analytics_db_path(output_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    return service_paths.output_layout(output_dir).analytics_db


def effective_codex_dir(codex_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    return service_paths.codex_dir_path(codex_dir)


def token_bola_root(
    codex_dir: str | pathlib.Path | None = None,
    output_dir: str | pathlib.Path | None = None,
) -> pathlib.Path:
    return runtime_paths(codex_dir, output_dir).output_dir


def pipeline_output_path(codex_dir: str | None, output_dir: str | None = None) -> pathlib.Path:
    return service_paths.OutputLayout(token_bola_root(codex_dir, output_dir)).analytics_db


def analysis_input_fingerprint(codex_dir: str | None = None, state_db: str | None = None, output_dir: str | None = None) -> str:
    return analysis_inputs.analysis_input_fingerprint(codex_dir, state_db, output_dir)


def parse_cutoff(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def retention_db_path(codex_dir: str | None, output_dir: str | None = None) -> pathlib.Path:
    return service_paths.OutputLayout(token_bola_root(codex_dir, output_dir)).analytics_db


raw_segment_state_checkpoint = retention_checkpoints.create
discard_raw_segment_state_checkpoint = retention_checkpoints.discard
restore_raw_segment_state_checkpoint = retention_checkpoints.restore


def read_analytics_metadata(output: str | None) -> dict[str, object]:
    db_path = pathlib.Path(output).expanduser() if output else analytics_db_path()
    if not db_path.exists():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            rows = con.execute("select key, value from run_metadata").fetchall()
        except sqlite3.Error:
            return {}
        metadata: dict[str, object] = {}
        for key, value in rows:
            try:
                metadata[str(key)] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                metadata[str(key)] = value
        return metadata
    finally:
        con.close()


sha256_file = paths_service.sha256_file
hook_install_status = hook_service.hook_install_status
hook_command = hook_service.hook_command
is_owned_hook_command = hook_service.is_owned_hook_command
hooks_json_status = hook_service.hooks_json_status
write_text_atomic_owner_only = hook_service.write_text_atomic_owner_only
merge_hooks_json_registration = hook_service.merge_hooks_json_registration
remove_hooks_json_registration = hook_service.remove_hooks_json_registration


current_segments_status = doctor_service.current_segments_status
int_metadata = doctor_service.int_metadata
age_seconds = doctor_service.age_seconds
pending_recovery_state_summary = doctor_service.pending_recovery_state_summary
parse_captured_at_unix = doctor_service.parse_captured_at_unix
error_log_summary = doctor_service.error_log_summary
analytics_tmp_file_summary = doctor_service.analytics_tmp_file_summary
normalize_pending_publish_summary = doctor_service.normalize_pending_publish_summary
retention_pruned_store_summary = doctor_service.retention_pruned_store_summary
cleanup_retention_job_summary = doctor_service.cleanup_retention_job_summary
path_transition_summary = doctor_service.path_transition_summary
doctor_health = doctor_service.doctor_health


def doctor(args: argparse.Namespace) -> int:
    result = doctor_service.run_doctor(
        doctor_service.DoctorOptions(
            codex_dir=getattr(args, "codex_dir", None),
            output_dir=getattr(args, "output_dir", None),
        ),
        doctor_service.DoctorDependencies(
            resolve_paths=runtime_paths,
            codex_dir_status=codex_dir_status,
            codex_cli_status=codex_cli_status,
            hook_install_status=hook_install_status,
            hooks_json_status=hooks_json_status,
            now=time.time,
        ),
    )
    if bool(getattr(args, "json_output", False)):
        print(json.dumps(result.report, ensure_ascii=False, indent=2))
    else:
        print(doctor_renderer.render_doctor_report(result.report))
    return result.exit_code


def quarantine_command(args: argparse.Namespace) -> int:
    result = doctor_service.run_quarantine(
        doctor_service.QuarantineOptions(
            action=args.quarantine_action,
            codex_dir=getattr(args, "codex_dir", None),
            output_dir=getattr(args, "output_dir", None),
            include_acknowledged=bool(getattr(args, "include_acknowledged", False)),
            event_ids=tuple(getattr(args, "event_id", None) or ()),
            acknowledge_all=bool(getattr(args, "acknowledge_all", False)),
        ),
        runtime_paths,
    )
    if bool(getattr(args, "json_output", False)) and result.pretty:
        print(json.dumps(result.payload, ensure_ascii=False, indent=2))
    elif bool(getattr(args, "json_output", False)):
        print(json.dumps(result.payload, ensure_ascii=False, separators=(",", ":")))
    elif args.quarantine_action == "list":
        print(quarantine_renderer.render_list(result.payload, include_acknowledged=bool(getattr(args, "include_acknowledged", False))))
    else:
        print(quarantine_renderer.render_acknowledge(result.payload))
    return result.exit_code


def install_hook(args: argparse.Namespace) -> dict[str, object]:
    result = hook_service.run_install_hook(
        hook_service.InstallHookOptions(
            codex_dir=getattr(args, "codex_dir", None),
            output_dir=getattr(args, "output_dir", None),
            persist_config=bool(getattr(args, "persist_config", False)),
        ),
        hook_service.InstallHookDependencies(
            resolve_paths=runtime_paths,
            validate_codex_dir=require_valid_codex_dir,
            validate_codex_cli=require_valid_codex_cli,
            validate_hook_runtime=require_valid_hook_runtime,
            persist_paths=lambda updates: paths_service.run_paths_set(
                paths_service.PathsSetOptions(
                    codex_dir=updates.get("codex_dir"),
                    output_dir=updates.get("output_dir"),
                ),
                path_set_dependencies(),
            ),
        ),
    )
    return result.payload


def pipeline(args: argparse.Namespace) -> int:
    result = pipeline_service.run_pipeline(
        pipeline_service.PipelineOptions(
            codex_dir=getattr(args, "codex_dir", None),
            output_dir=getattr(args, "output_dir", None),
            state_db=getattr(args, "state_db", None),
            project_roots=tuple(getattr(args, "project_root", None) or ()),
            incremental=bool(getattr(args, "incremental", False)),
            recover=bool(getattr(args, "recover", False)),
            skip_rotate=bool(getattr(args, "skip_rotate", False)),
        ),
        pipeline_service.PipelineDependencies(
            resolve_paths=runtime_paths,
            output_path=pipeline_output_path,
            run_command=run_typed_script_json,
            read_analytics_metadata=read_analytics_metadata,
        ),
    )
    emit_process_output(result.process_output)
    if result.payload is not None:
        print(json.dumps(result.payload, ensure_ascii=False, separators=(",", ":")))
    return result.exit_code


def retention_prune(args: argparse.Namespace) -> int:
    dependencies = retention_service.RetentionDependencies(
        resolve_paths=runtime_paths,
        db_path=retention_db_path,
        run_command=run_typed_script_json,
        create_checkpoint=raw_segment_state_checkpoint,
        restore_checkpoint=restore_raw_segment_state_checkpoint,
        discard_checkpoint=discard_raw_segment_state_checkpoint,
    )
    try:
        result = retention_service.run_retention_prune(
            retention_service.RetentionPruneOptions(
                cutoff=args.cutoff,
                preview_signature=getattr(args, "preview_signature", None),
                codex_dir=getattr(args, "codex_dir", None),
                output_dir=getattr(args, "output_dir", None),
            ),
            dependencies,
        )
    except retention_service.RetentionExecutionError as exc:
        if exc.payload is not None:
            print(json.dumps(exc.payload, ensure_ascii=False, separators=(",", ":")))
        raise exc.cause
    emit_process_output(result.process_output)
    if result.payload is not None:
        print(json.dumps(result.payload, ensure_ascii=False, separators=(",", ":")))
    return result.exit_code


def retention_preview_command(args: argparse.Namespace) -> int:
    result = retention_service.run_retention_preview(
        retention_service.RetentionPreviewOptions(
            cutoff=args.cutoff,
            codex_dir=getattr(args, "codex_dir", None),
            output_dir=getattr(args, "output_dir", None),
        ),
        runtime_paths,
    )
    if result.payload is not None:
        print(json.dumps(result.payload, ensure_ascii=False, separators=(",", ":")))
    return result.exit_code


public_runtime_paths = paths_service.public_runtime_paths
public_config = paths_service.public_config
managed_content_files = paths_service.managed_content_files
validate_output_dir_target = paths_service.validate_output_dir_target
paths_report = paths_service.paths_report
validate_persistent_path_updates = paths_service.validate_persistent_path_updates
read_hook_recovery_state = paths_service.read_hook_recovery_state
hook_recovery_state_paths = paths_service.hook_recovery_state_paths
copy_file_atomic = paths_service.copy_file_atomic
handoff_hook_recovery_states = paths_service.handoff_hook_recovery_states
restore_handoff_source_states = paths_service.restore_handoff_source_states
finish_transferred_state_handoff = paths_service.finish_transferred_state_handoff
recover_preparing_path_transition = paths_service.recover_preparing_path_transition
restore_file_snapshot = paths_service.restore_file_snapshot
acquire_output_service_locks = paths_service.acquire_output_service_locks
output_transition_payload = paths_service.output_transition_payload
validate_migration_roots = paths_service.validate_migration_roots
remove_migrated_source = paths_service.remove_migrated_source
pending_output_migration = paths_service.pending_output_migration
pending_physical_delete_payload = paths_service.pending_physical_delete_payload
resolve_source_physical_deletes = paths_service.resolve_source_physical_deletes
raw_migration_sources = paths_service.raw_migration_sources
read_migration_pruned_turn_state = paths_service.read_migration_pruned_turn_state
retention_pruned_conflict_payload = paths_service.retention_pruned_conflict_payload
plan_retention_pruned_turn_merge = paths_service.plan_retention_pruned_turn_merge
stage_migration_pruned_turn_state = paths_service.stage_migration_pruned_turn_state
commit_migration_pruned_turn_state = paths_service.commit_migration_pruned_turn_state
output_migration_preview = paths_service.output_migration_preview
imported_segment_id = paths_service.imported_segment_id
import_raw_segment = paths_service.import_raw_segment
copy_migration_evidence = paths_service.copy_migration_evidence
remove_source_managed_data = paths_service.remove_source_managed_data


def path_set_dependencies() -> paths_service.PathsSetDependencies:
    return paths_service.PathsSetDependencies(
        validate_codex_dir=require_valid_codex_dir,
        validate_codex_cli=require_valid_codex_cli,
        merge_hook_registration=merge_hooks_json_registration,
        remove_hook_registration=remove_hooks_json_registration,
        managed_files=managed_content_files,
    )


def migration_dependencies(*, adapter_callbacks: bool = False) -> paths_service.MigrationDependencies:
    return paths_service.MigrationDependencies(
        run_command=run_typed_script_json,
        resolve_physical_deletes=resolve_source_physical_deletes,
        preview_migration=output_migration_preview if adapter_callbacks else None,
        apply_migration=apply_output_migration if adapter_callbacks else None,
    )


def paths_set(args: argparse.Namespace, *, emit: bool = True) -> int:
    result = paths_service.run_paths_set(
        paths_service.PathsSetOptions(
            codex_dir=getattr(args, "codex_dir", None),
            output_dir=getattr(args, "output_dir", None),
        ),
        path_set_dependencies(),
    )
    if emit:
        print(json.dumps(result.payload, ensure_ascii=False, indent=2))
    return result.exit_code


def apply_output_migration(
    source: pathlib.Path,
    destination: pathlib.Path,
    transition: dict[str, object] | None,
) -> tuple[int, dict[str, object]]:
    return paths_service.apply_output_migration(
        source,
        destination,
        transition,
        migration_dependencies(),
    )


def paths_migrate(args: argparse.Namespace) -> int:
    result = paths_service.run_paths_migrate(
        paths_service.PathsMigrateOptions(
            output_dir=bool(getattr(args, "output_dir", False)),
            apply=bool(getattr(args, "apply", False)),
        ),
        migration_dependencies(adapter_callbacks=True),
    )
    print(json.dumps(result.payload, ensure_ascii=False, separators=(",", ":")))
    return result.exit_code


def paths_command(args: argparse.Namespace) -> int:
    if args.paths_action == "show":
        print(json.dumps(paths_report(), ensure_ascii=False, indent=2))
        return 0
    if args.paths_action == "set":
        return paths_set(args)
    if args.paths_action == "migrate":
        return paths_migrate(args)
    raise service_paths.ConfigurationError(f"unsupported paths action: {args.paths_action}")


def runtime_command_dependencies() -> runtime_command_service.RuntimeCommandDependencies:
    return runtime_command_service.RuntimeCommandDependencies(
        resolve_paths=runtime_paths,
        run_command=run_script,
    )


def serve_dependencies() -> runtime_command_service.ServeDependencies:
    return runtime_command_service.ServeDependencies(
        resolve_paths=runtime_paths,
        require_runtime_config=service_paths.require_runtime_config,
        replace_command=replace_script,
    )


def handle_forwarded_runtime(
    args: argparse.Namespace,
    unknown: list[str],
    _parser: argparse.ArgumentParser,
) -> int:
    command = {
        "reconcile": RuntimeCommand.RECONCILE,
        "normalize": RuntimeCommand.NORMALIZE,
        "compact": RuntimeCommand.COMPACT,
    }[args.command]
    result = runtime_command_service.run_runtime_command(
        runtime_command_service.RuntimeCommandOptions(
            command=command,
            codex_dir=args.codex_dir,
            output_dir=args.output_dir,
            arguments=tuple(unknown),
        ),
        runtime_command_dependencies(),
    )
    return result.exit_code


def handle_build(args: argparse.Namespace, unknown: list[str], _parser: argparse.ArgumentParser) -> int:
    if any(value == "--output" or value.startswith("--output=") for value in unknown):
        _parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    result = runtime_command_service.run_build(
        runtime_command_service.BuildOptions(
            codex_dir=args.codex_dir,
            output_dir=args.output_dir,
            normalized_log=args.normalized_log,
            state_db=args.state_db,
            project_roots=tuple(args.project_root or ()),
            extra_arguments=tuple(unknown),
        ),
        runtime_command_dependencies(),
    )
    return result.exit_code


def handle_serve(args: argparse.Namespace, unknown: list[str], parser: argparse.ArgumentParser) -> int:
    reject_unknown(parser, unknown)
    return runtime_command_service.run_serve(
        runtime_command_service.ServeOptions(
            host=args.host,
            port=int(args.port),
            codex_dir=args.codex_dir,
            output_dir=args.output_dir,
            pin_runtime_paths=bool(
                args.codex_dir
                or args.output_dir
                or os.environ.get("CODEX_HOME")
                or os.environ.get(service_paths.OUTPUT_DIR_ENV)
            ),
        ),
        serve_dependencies(),
    )


def handle_pipeline(args: argparse.Namespace, unknown: list[str], parser: argparse.ArgumentParser) -> int:
    reject_unknown(parser, unknown)
    try:
        return pipeline(args)
    except cancel_control.Cancelled as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
        return cancel_control.CANCEL_EXIT_CODE


def handle_retention_prune(args: argparse.Namespace, unknown: list[str], parser: argparse.ArgumentParser) -> int:
    reject_unknown(parser, unknown)
    return retention_prune(args)


def handle_retention_preview(args: argparse.Namespace, unknown: list[str], parser: argparse.ArgumentParser) -> int:
    reject_unknown(parser, unknown)
    return retention_preview_command(args)


def handle_doctor(args: argparse.Namespace, unknown: list[str], parser: argparse.ArgumentParser) -> int:
    reject_unknown(parser, unknown)
    return doctor(args)


def handle_quarantine(args: argparse.Namespace, unknown: list[str], parser: argparse.ArgumentParser) -> int:
    reject_unknown(parser, unknown)
    return quarantine_command(args)


def handle_install_hook(args: argparse.Namespace, unknown: list[str], parser: argparse.ArgumentParser) -> int:
    reject_unknown(parser, unknown)
    args.persist_config = True
    print(json.dumps(install_hook(args), ensure_ascii=False, separators=(",", ":")))
    return 0


def handle_paths(args: argparse.Namespace, unknown: list[str], parser: argparse.ArgumentParser) -> int:
    reject_unknown(parser, unknown)
    return paths_command(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bola",
        description="Codex Token Bola capture, analytics, and dashboard commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        epilog="""Common commands:
  install-hook      Register the BOLA hook in a Codex directory
  doctor            Check configuration and runtime health
  serve             Serve the local dashboard
  paths             Show or update runtime paths
  pipeline          Run analysis without the dashboard

Advanced and recovery commands:
  quarantine        Inspect or acknowledge quarantined input
  reconcile         Recover pending turns from hook state
  normalize         Normalize raw JSONL logs
  compact           Rotate current raw segments
  build             Build the SQLite analytics database
  retention-preview Preview old data and mint a prune signature
  retention-prune   Delete old data and rebuild analytics""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        help="Run `bola COMMAND --help` for command-specific options.",
        metavar="COMMAND",
    )

    def add_output_dir_arg(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--output-dir",
            dest="output_dir",
            metavar="OUTPUT_DIR",
            help="Directory for all files generated by Codex Token Bola.",
        )

    def add_runtime_path_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--codex-dir")
        add_output_dir_arg(command_parser)

    reconcile_parser = sub.add_parser("reconcile", description="Recover pending turns from saved hook state.")
    normalize_parser = sub.add_parser("normalize", description="Normalize raw JSONL logs.")
    compact_parser = sub.add_parser("compact", description="Rotate current raw segments with pointer handoff.")
    for command_parser in (reconcile_parser, normalize_parser, compact_parser):
        add_runtime_path_args(command_parser)
        command_parser.set_defaults(handler=handle_forwarded_runtime)

    build = sub.add_parser("build", description="Build the SQLite analytics database.")
    add_runtime_path_args(build)
    build.add_argument("--normalized-log")
    build.add_argument("--state-db")
    build.add_argument("--project-root", action="append")
    build.set_defaults(handler=handle_build)

    serve = sub.add_parser("serve", description="Serve the local dashboard.")
    add_runtime_path_args(serve)
    serve.add_argument("--host", default="127.0.0.1", help="IPv4 loopback address or localhost")
    serve.add_argument("--port", default="8766")
    serve.set_defaults(handler=handle_serve)

    pipe = sub.add_parser("pipeline", description="Run normalize and analytics build; add --recover to recover pending turns first.")
    add_runtime_path_args(pipe)
    pipe.add_argument("--state-db")
    pipe.add_argument("--project-root", action="append")
    pipe.add_argument("--incremental", action="store_true")
    pipe.add_argument("--recover", action="store_true", help="Recover pending turns before analysis.")
    pipe.add_argument("--skip-rotate", action="store_true", help="Do not rotate current raw segments before analysis.")
    pipe.set_defaults(handler=handle_pipeline)

    retention_preview_parser = sub.add_parser("retention-preview", description="Preview service data older than a cutoff without writing files.")
    retention_preview_parser.add_argument("--cutoff", required=True, help="ISO timestamp or unix seconds cutoff.")
    add_runtime_path_args(retention_preview_parser)
    retention_preview_parser.set_defaults(handler=handle_retention_preview)

    retention = sub.add_parser("retention-prune", description="Delete service data older than a cutoff and rebuild analytics.")
    retention.add_argument("--cutoff", required=True, help="ISO timestamp or unix seconds cutoff.")
    add_runtime_path_args(retention)
    retention.add_argument("--preview-signature")
    retention.set_defaults(handler=handle_retention_prune)

    doc = sub.add_parser("doctor", description="Summarize configuration, hook, recovery, and analytics health.")
    add_runtime_path_args(doc)
    doc.add_argument("--json", dest="json_output", action="store_true", help="Print the complete machine-readable diagnostic report.")
    doc.set_defaults(handler=handle_doctor)

    quarantine = sub.add_parser(
        "quarantine",
        description="List or acknowledge quarantined input records.",
    )
    add_runtime_path_args(quarantine)
    quarantine_sub = quarantine.add_subparsers(
        dest="quarantine_action",
        required=True,
        metavar="ACTION",
    )
    quarantine_list = quarantine_sub.add_parser("list", help="List unacknowledged quarantined input records.")
    quarantine_list.add_argument("--include-acknowledged", action="store_true")
    quarantine_list.add_argument("--json", dest="json_output", action="store_true", help="Print the complete machine-readable quarantine report.")
    quarantine_acknowledge = quarantine_sub.add_parser("acknowledge", help="Acknowledge quarantined input records without deleting evidence.")
    acknowledge_scope = quarantine_acknowledge.add_mutually_exclusive_group(required=True)
    acknowledge_scope.add_argument("--event-id", action="append")
    acknowledge_scope.add_argument("--all", dest="acknowledge_all", action="store_true")
    quarantine_acknowledge.add_argument("--json", dest="json_output", action="store_true", help="Print the machine-readable acknowledgement result.")
    quarantine.set_defaults(handler=handle_quarantine)

    install = sub.add_parser("install-hook", description="Register the installed BOLA hook in a Codex directory.")
    install.add_argument(
        "--codex-dir",
        help="Existing initialized Codex configuration and state directory.",
    )
    add_output_dir_arg(install)
    install.set_defaults(handler=handle_install_hook)

    paths = sub.add_parser(
        "paths",
        description="Show, update, or migrate runtime paths.",
    )
    paths_sub = paths.add_subparsers(
        dest="paths_action",
        required=True,
        metavar="ACTION",
    )
    paths_sub.add_parser("show", help="Show persistent and effective runtime paths.")
    paths_set_parser = paths_sub.add_parser("set", help="Persist paths and hand off in-flight hook state.")
    paths_set_parser.add_argument(
        "--codex-dir",
        help="Existing initialized Codex configuration and state directory.",
    )
    add_output_dir_arg(paths_set_parser)
    paths_migrate_parser = paths_sub.add_parser("migrate", help="Merge prior generated data into the active output directory.")
    paths_migrate_parser.add_argument("--output-dir", action="store_true", required=True, help="Migrate the pending output directory.")
    paths_migrate_parser.add_argument("--apply", action="store_true", help="Apply migration. Omit for preview.")
    paths.set_defaults(handler=handle_paths)

    for command_parser in sub.choices.values():
        command_parser.allow_abbrev = False
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def reject_unknown(parser: argparse.ArgumentParser, unknown: list[str]) -> None:
    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")


def main() -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args()
    try:
        return args.handler(args, unknown, parser)
    except CodexDirValidationError as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
        return 2
    except CodexCliValidationError as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
        return 2
    except HookRuntimeValidationError as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
        return 2
    except service_lock.ServiceLockBusy as exc:
        print(
            json.dumps(
                {"error": "analysis_or_cleanup_running", "lock_path": str(exc.path)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 75
    except service_paths.PathLockBusy as exc:
        print(
            json.dumps(
                {"error": "path_operation_busy", "lock_path": str(exc.path)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 75
    except service_paths.LegacyNameUnsupported as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
        return 2
    except service_paths.ConfigurationError as exc:
        print(json.dumps({"error": "configuration_invalid", "message": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 2
    except quarantine_health.QuarantineError as exc:
        if getattr(args, "command", None) == "quarantine" and not bool(getattr(args, "json_output", False)):
            print(quarantine_renderer.render_error(str(exc)))
        else:
            print(json.dumps({"status": "failed", "error": "quarantine_state_invalid", "message": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
