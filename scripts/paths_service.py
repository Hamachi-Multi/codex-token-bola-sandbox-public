"""Runtime path configuration and output migration application services."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import raw_segments
import retention_pruned_store
import service_lock
import service_paths
from runtime_command_runner import ProcessResult, RuntimeCommand

HOOK_RECOVERY_RECORD_TYPES = frozenset({"turn_start", "turn_stop_missing_start"})


class PathMigrationBlocked(RuntimeError):
    def __init__(self, payload: dict[str, object]):
        self.status = payload
        super().__init__(str(payload.get("error") or "path_migration_blocked"))

    def payload(self) -> dict[str, object]:
        return dict(self.status)


@dataclass(frozen=True)
class PathsSetOptions:
    codex_dir: str | pathlib.Path | None = None
    output_dir: str | pathlib.Path | None = None


@dataclass(frozen=True)
class PathsMigrateOptions:
    output_dir: bool
    apply: bool = False


@dataclass(frozen=True)
class PathsResult:
    exit_code: int
    payload: dict[str, object]
    pretty: bool = False


@dataclass(frozen=True)
class PathsSetDependencies:
    validate_codex_dir: Callable[[str | pathlib.Path], None]
    validate_codex_cli: Callable[[], None]
    merge_hook_registration: Callable[[pathlib.Path], dict[str, object]]
    remove_hook_registration: Callable[[pathlib.Path], dict[str, object]]
    managed_files: Callable[..., list[pathlib.Path]]


@dataclass(frozen=True)
class MigrationDependencies:
    run_command: Callable[[RuntimeCommand, list[str], dict[str, str]], ProcessResult]
    resolve_physical_deletes: Callable[[pathlib.Path], dict[str, Any]]
    preview_migration: Callable[[pathlib.Path, pathlib.Path, dict[str, object] | None], dict[str, object]] | None = None
    apply_migration: (
        Callable[
            [pathlib.Path, pathlib.Path, dict[str, object] | None],
            tuple[int, dict[str, object]],
        ]
        | None
    ) = None


def sha256_file(path: pathlib.Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def write_text_atomic_owner_only(path: pathlib.Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
        path.chmod(mode)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


def runtime_env(codex_dir: pathlib.Path, output_dir: pathlib.Path) -> dict[str, str]:
    return {"CODEX_HOME": str(codex_dir), service_paths.OUTPUT_DIR_ENV: str(output_dir)}


def require_migration_process_result(
    result: ProcessResult,
    *,
    allow_degraded: bool,
) -> dict[str, object]:
    payload = result.payload
    status = str(payload.get("status") or "") if payload is not None else ""
    valid = result.parse_error is None and payload is not None and result.exit_code == 0
    if allow_degraded:
        valid = (valid and status == "healthy") or (
            result.parse_error is None
            and payload is not None
            and result.exit_code == 1
            and status == "degraded"
        )
    if valid:
        return payload
    details = [
        f"command={result.command.value}",
        f"exit_code={result.exit_code}",
        f"status={status or 'missing'}",
    ]
    if result.parse_error:
        details.append(f"parse_error={result.parse_error}")
    raise service_paths.ConfigurationError(f"migration child command failed: {' '.join(details)}")


def public_runtime_paths(paths: service_paths.RuntimePaths) -> dict[str, str]:
    return {
        "project_root": str(paths.project_root),
        "runtime_config_path": str(paths.runtime_config_path),
        "codex_dir": str(paths.codex_dir),
        "output_dir": str(paths.output_dir),
    }


def public_config(config: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {"schema_version": config.get("schema_version")}
    if config.get("codex_dir") is not None:
        result["codex_dir"] = config["codex_dir"]
    if config.get("output_dir") is not None:
        result["output_dir"] = config["output_dir"]
    return result


def managed_content_files(root: pathlib.Path, *, limit: int | None = None) -> list[pathlib.Path]:
    base = root.resolve(strict=False)
    ignored_names = {"service.lock", "raw-segment.lock", "raw-segment-manifest.lock"}
    files: list[pathlib.Path] = []
    for entry in service_paths.managed_paths(base):
        try:
            candidates = [entry] if entry.is_file() or entry.is_symlink() else sorted(entry.rglob("*")) if entry.is_dir() else []
        except OSError:
            continue
        for candidate in candidates:
            if candidate.name in ignored_names or not candidate.is_file() or candidate.is_symlink():
                continue
            files.append(candidate)
            if limit is not None and len(files) >= limit:
                return files
    return sorted(set(files))


def validate_output_dir_target(path: pathlib.Path) -> None:
    expanded = path.expanduser().absolute()
    if expanded.is_symlink():
        raise service_paths.ConfigurationError(f"output directory must not be a symbolic link: {expanded}")
    if expanded.exists():
        if not expanded.is_dir():
            raise service_paths.ConfigurationError(f"output directory is not a directory: {expanded}")
        if not os.access(expanded, os.R_OK | os.W_OK | os.X_OK):
            raise service_paths.ConfigurationError(f"output directory is not readable and writable: {expanded}")
        return
    parent = expanded.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise service_paths.ConfigurationError(f"output directory cannot be created below: {parent}")


def paths_report() -> dict[str, object]:
    configured = service_paths.read_config()
    effective = service_paths.resolve_runtime_paths()
    transition = service_paths.load_path_transition()
    transition_report: dict[str, object] = {"pending": False}
    if transition:
        source = transition.source_output_dir
        active = transition.active_output_dir
        transition_report = {
            "pending": True,
            "transition_id": transition.transition_id,
            "phase": transition.phase.value,
            "source_output_dir": str(source),
            "active_output_dir": str(active),
            "rollback_output_dir": str(source),
            "source_file_count": len(managed_content_files(source)),
        }
    return {
        "runtime_config_path": str(service_paths.runtime_config_path()),
        "exists": service_paths.runtime_config_path().exists(),
        "configured": public_config(configured),
        "effective": public_runtime_paths(effective),
        "precedence": ["cli", "environment", "runtime_config", "defaults"],
        "overrides": {
            "codex_dir": "CODEX_HOME" if os.environ.get("CODEX_HOME") else None,
            "output_dir": service_paths.OUTPUT_DIR_ENV if os.environ.get(service_paths.OUTPUT_DIR_ENV) else None,
        },
        "output_transition": transition_report,
    }


def validate_persistent_path_updates(updates: dict[str, str | pathlib.Path]) -> None:
    environment_keys = {"codex_dir": "CODEX_HOME", "output_dir": service_paths.OUTPUT_DIR_ENV}
    for key, value in updates.items():
        environment_value = os.environ.get(environment_keys[key])
        if environment_value and pathlib.Path(environment_value).expanduser().resolve(strict=False) != pathlib.Path(value).expanduser().resolve(strict=False):
            raise service_paths.ConfigurationError(
                f"{environment_keys[key]} overrides persistent {key}; unset the environment variable before storing a different value"
            )


def read_hook_recovery_state(path: pathlib.Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("record_type") not in HOOK_RECOVERY_RECORD_TYPES:
        return None
    if not payload.get("session_id") or not payload.get("turn_id"):
        return None
    return payload


def hook_recovery_state_paths(root: pathlib.Path) -> list[pathlib.Path]:
    state_dir = root / "state"
    try:
        candidates = sorted(state_dir.glob("*.json"))
    except OSError:
        return []
    return [path for path in candidates if read_hook_recovery_state(path) is not None]


def copy_file_atomic(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{time.time_ns()}.handoff.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if sha256_file(source) != sha256_file(temporary):
            raise service_paths.ConfigurationError(f"hook state handoff verification failed: {source} -> {destination}")
        os.replace(temporary, destination)
        destination.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def handoff_hook_recovery_states(source: pathlib.Path, destination: pathlib.Path) -> dict[str, object]:
    if source == destination:
        return {"files": 0, "bytes": 0, "created": [], "sources": []}
    created: list[pathlib.Path] = []
    sources: list[pathlib.Path] = []
    total_bytes = 0
    try:
        for source_path in hook_recovery_state_paths(source):
            target_path = destination / "state" / source_path.name
            if target_path.exists():
                if sha256_file(source_path) != sha256_file(target_path):
                    raise service_paths.ConfigurationError(f"hook state handoff conflict: {target_path}")
            else:
                copy_file_atomic(source_path, target_path)
                created.append(target_path)
            sources.append(source_path)
            total_bytes += source_path.stat().st_size
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return {
        "files": len(sources),
        "bytes": total_bytes,
        "created": created,
        "sources": sources,
    }


def restore_handoff_source_states(handoff: dict[str, object], destination: pathlib.Path) -> None:
    for value in handoff.get("sources") or []:
        source_path = pathlib.Path(value)
        destination_path = destination / "state" / source_path.name
        if source_path.exists():
            if not destination_path.is_file() or sha256_file(source_path) != sha256_file(destination_path):
                raise service_paths.ConfigurationError(f"cannot verify hook state rollback: {source_path}")
            continue
        if not destination_path.is_file():
            raise service_paths.ConfigurationError(f"cannot restore hook state during rollback: {source_path}")
        copy_file_atomic(destination_path, source_path)


def finish_transferred_state_handoff(transition: service_paths.PathTransition) -> None:
    source = transition.source_output_dir
    destination = transition.active_output_dir
    pairs: list[tuple[pathlib.Path, pathlib.Path]] = []
    for name in transition.transferred_state_files:
        source_path = source / "state" / pathlib.Path(str(name)).name
        destination_path = destination / "state" / source_path.name
        if not source_path.exists():
            continue
        if not destination_path.is_file() or sha256_file(source_path) != sha256_file(destination_path):
            raise service_paths.ConfigurationError(f"hook state handoff is incomplete: {source_path} -> {destination_path}")
        pairs.append((source_path, destination_path))
    for source_path, _destination_path in pairs:
        source_path.unlink()


def recover_preparing_path_transition() -> service_paths.PathTransition | None:
    transition = service_paths.load_path_transition()
    if not transition or transition.phase is not service_paths.PathTransitionPhase.PREPARING:
        return transition
    current = service_paths.resolve_runtime_paths()
    source = transition.source_output_dir
    active = transition.active_output_dir
    if current.output_dir == active:
        pending = transition.mark_pending()
        finish_transferred_state_handoff(pending)
        service_paths.write_path_transition(pending)
        return pending
    if current.output_dir != source:
        raise service_paths.ConfigurationError(f"preparing path transition does not match active output directory: {current.output_dir}")
    for name in transition.created_state_files:
        source_path = source / "state" / pathlib.Path(str(name)).name
        destination_path = active / "state" / source_path.name
        if not destination_path.exists():
            continue
        if not source_path.is_file() or sha256_file(source_path) != sha256_file(destination_path):
            raise service_paths.ConfigurationError(f"cannot roll back hook state handoff safely: {destination_path}")
        destination_path.unlink()
    previous = transition.previous_transition
    if previous is not None:
        service_paths.write_path_transition(previous)
        return previous
    service_paths.clear_path_transition()
    return None


def restore_file_snapshot(path: pathlib.Path, snapshot: bytes | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    write_text_atomic_owner_only(path, snapshot.decode("utf-8"), 0o600)


@contextlib.contextmanager
def acquire_output_service_locks(*roots: pathlib.Path):
    unique = sorted({root.resolve(strict=False) for root in roots}, key=str)
    with contextlib.ExitStack() as stack:
        for root in unique:
            stack.enter_context(service_lock.acquire_service_lock(reason="paths-set", output_dir=root))
        yield


def output_transition_payload(source: pathlib.Path, active: pathlib.Path, *, phase: str) -> dict[str, object]:
    try:
        transition_phase = service_paths.PathTransitionPhase(phase)
    except ValueError as exc:
        raise service_paths.ConfigurationError(f"unsupported path transition phase: {phase}") from exc
    transition = service_paths.PathTransition(
        transition_id=uuid.uuid4().hex,
        source_output_dir=source,
        active_output_dir=active,
        created_at_ns=time.time_ns(),
        phase=transition_phase,
    )
    return transition.to_payload()


def run_paths_set(options: PathsSetOptions, dependencies: PathsSetDependencies) -> PathsResult:
    requested = {key: value for key, value in {"codex_dir": options.codex_dir, "output_dir": options.output_dir}.items() if value is not None}
    if not requested:
        raise service_paths.ConfigurationError("paths set requires --codex-dir or --output-dir")
    validate_persistent_path_updates(requested)
    with service_paths.acquire_path_lock(blocking=False):
        recover_preparing_path_transition()
        runtime_config_path = service_paths.runtime_config_path()
        runtime_config_exists = runtime_config_path.exists()
        current = service_paths.resolve_runtime_paths()
        target_codex = pathlib.Path(requested.get("codex_dir", current.codex_dir)).expanduser().resolve(strict=False)
        requested_output = pathlib.Path(requested.get("output_dir", current.output_dir)).expanduser()
        target_output = requested_output.resolve(strict=False)
        codex_changed = runtime_config_exists and target_codex != current.codex_dir
        output_changed = runtime_config_exists and target_output != current.output_dir
        if not runtime_config_exists or "codex_dir" in requested:
            dependencies.validate_codex_dir(target_codex)
            dependencies.validate_codex_cli()
        if not runtime_config_exists or "output_dir" in requested:
            validate_output_dir_target(requested_output)
        if output_changed:
            validate_migration_roots(current.output_dir, target_output)

        previous_transition = service_paths.load_path_transition()
        rollback = False
        if output_changed and previous_transition:
            previous_source = previous_transition.source_output_dir
            previous_active = previous_transition.active_output_dir
            if previous_transition.phase is not service_paths.PathTransitionPhase.PENDING:
                raise service_paths.ConfigurationError(f"output transition cannot be changed while phase is {previous_transition.phase.value}")
            if current.output_dir != previous_active:
                raise service_paths.ConfigurationError("path transition active output does not match current configuration")
            if target_output == previous_source:
                rollback = True
            else:
                raise service_paths.ConfigurationError(
                    f"output transition pending: {previous_source} -> {previous_active}; only rollback to {previous_source} is allowed"
                )

        roots = (current.output_dir, target_output) if output_changed and target_output.exists() else (current.output_dir,) if output_changed else ()
        source_hook_path = current.codex_dir / "hooks.json"
        target_hook_path = target_codex / "hooks.json"
        source_hook_snapshot = source_hook_path.read_bytes() if source_hook_path.exists() else None
        target_hook_snapshot = target_hook_path.read_bytes() if target_hook_path.exists() else None
        runtime_config_snapshot = runtime_config_path.read_bytes() if runtime_config_path.exists() else None
        transition_before = previous_transition
        handoff: dict[str, object] = {"files": 0, "bytes": 0, "created": [], "sources": []}
        next_transition: service_paths.PathTransition | None = None
        with acquire_output_service_locks(*roots):
            try:
                if output_changed:
                    handoff = handoff_hook_recovery_states(current.output_dir, target_output)
                if codex_changed:
                    dependencies.merge_hook_registration(target_codex)
                    dependencies.remove_hook_registration(current.codex_dir)

                if output_changed:
                    next_transition = service_paths.PathTransition.prepare_set(
                        current.output_dir,
                        target_output,
                        transition_id=uuid.uuid4().hex,
                        created_at_ns=time.time_ns(),
                        rollback=rollback,
                        transferred_state_files=tuple(path.name for path in handoff["sources"]),
                        created_state_files=tuple(path.name for path in handoff["created"]),
                        previous_transition=transition_before,
                    )
                    service_paths.write_path_transition(next_transition)

                service_paths.write_config(
                    {
                        "codex_dir": target_codex,
                        "output_dir": target_output,
                    }
                )

                if next_transition is not None:
                    pending_transition = next_transition.mark_pending()
                    finish_transferred_state_handoff(pending_transition)
                    service_paths.write_path_transition(pending_transition)
                    next_transition = pending_transition
                if next_transition is not None and not dependencies.managed_files(current.output_dir, limit=1):
                    service_paths.clear_path_transition()
            except Exception as exc:
                effective_after_error = service_paths.resolve_runtime_paths()
                committed = ("codex_dir" not in requested or effective_after_error.codex_dir == target_codex) and (
                    "output_dir" not in requested or effective_after_error.output_dir == target_output
                )
                try:
                    if output_changed:
                        restore_handoff_source_states(handoff, target_output)
                except Exception as recovery_exc:
                    if committed and next_transition is not None:
                        pending = (
                            next_transition
                            if next_transition.phase is service_paths.PathTransitionPhase.PENDING
                            else next_transition.mark_pending()
                        )
                        service_paths.write_path_transition(pending)
                    raise service_paths.ConfigurationError(f"path update failed and hook state rollback requires recovery: {recovery_exc}") from exc
                restore_file_snapshot(runtime_config_path, runtime_config_snapshot)
                if transition_before is None:
                    service_paths.clear_path_transition()
                else:
                    service_paths.write_path_transition(transition_before)
                if codex_changed:
                    restore_file_snapshot(source_hook_path, source_hook_snapshot)
                    restore_file_snapshot(target_hook_path, target_hook_snapshot)
                for path in reversed(handoff["created"]):
                    pathlib.Path(path).unlink(missing_ok=True)
                if committed:
                    effective_after_rollback = service_paths.resolve_runtime_paths()
                    if effective_after_rollback.output_dir == target_output and output_changed:
                        if next_transition is not None:
                            pending = (
                                next_transition
                                if next_transition.phase is service_paths.PathTransitionPhase.PENDING
                                else next_transition.mark_pending()
                            )
                            service_paths.write_path_transition(pending)
                        raise service_paths.ConfigurationError("path update was committed and could not be rolled back") from exc
                raise

    report = paths_report()
    report["status"] = "rolled_back" if rollback else "applied"
    report["state_handoff"] = {"files": handoff["files"], "bytes": handoff["bytes"]}
    return PathsResult(exit_code=0, payload=report, pretty=True)


def validate_migration_roots(source: pathlib.Path, destination: pathlib.Path) -> None:
    if source == destination:
        return
    if source in destination.parents or destination in source.parents:
        raise service_paths.ConfigurationError(f"migration source and destination cannot contain each other: {source} -> {destination}")


def remove_migrated_source(path: pathlib.Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def pending_output_migration() -> tuple[service_paths.PathTransition | None, pathlib.Path, pathlib.Path]:
    paths = service_paths.resolve_runtime_paths()
    transition = service_paths.load_path_transition()
    if transition:
        source = transition.source_output_dir
        destination = transition.active_output_dir
        if destination != paths.output_dir:
            raise service_paths.ConfigurationError(f"path transition target does not match active output directory: {destination} != {paths.output_dir}")
        return transition, source, destination
    return None, paths.output_dir, paths.output_dir


def pending_physical_delete_payload(source: pathlib.Path, sweep: dict[str, Any]) -> dict[str, object]:
    pending_segments = [item for item in sweep.get("pending_source_segments") or [] if isinstance(item, dict)]
    pending_paths = [str(item.get("path") or "") for item in pending_segments if item.get("path")]
    pending_files = max(int(sweep.get("pending_files") or 0), len(pending_paths))
    return {
        "status": "blocked",
        "migrated": False,
        "error": "source_physical_delete_pending",
        "phase": "unlink_pending",
        "source_output_dir": str(source),
        "pending_files": pending_files,
        "pending_paths": pending_paths[:20],
        "pending_paths_truncated": len(pending_paths) > 20,
        "unlink_errors": list(sweep.get("errors") or [])[:20],
        "retryable": True,
    }


def resolve_source_physical_deletes(source: pathlib.Path) -> dict[str, Any]:
    try:
        sweep = raw_segments.sweep_apply_marker(source)
        status = raw_segments.read_apply_status(source)
    except raw_segments.ManifestError as exc:
        raise service_paths.ConfigurationError(f"invalid source raw segment state at {source}: {exc}") from exc
    if int(sweep.get("pending_files") or 0) > 0 or status.pending:
        if status.pending and not sweep.get("pending_source_segments"):
            pending_segments = list(status.pending_source_segments)
            sweep = {**sweep, "pending_source_segments": pending_segments, "pending_files": len(pending_segments)}
        raise PathMigrationBlocked(pending_physical_delete_payload(source, sweep))
    return sweep


def raw_migration_sources(source: pathlib.Path, *, recover: bool = True) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    try:
        if recover:
            resolve_source_physical_deletes(source)
        elif raw_segments.read_apply_status(source).pending:
            raise raw_segments.ManifestError("pending physical retention deletion must be resolved before migration")
        raw_segments.reconcile_pending_rotation(source)
        paths.extend(raw_segments.manifest_segments(source, kind="prompt_usage"))
        paths.extend(raw_segments.current_segment_paths(source, kind="prompt_usage"))
    except raw_segments.ManifestError as exc:
        raise service_paths.ConfigurationError(f"invalid source raw segment state at {source}: {exc}") from exc
    legacy_root_raw = source / raw_segments.PROMPT_RAW_NAME
    if legacy_root_raw.is_file() and not legacy_root_raw.is_symlink():
        paths.append(legacy_root_raw)
    raw_root = source / "raw"
    if raw_root.exists():
        paths.extend(sorted(raw_root.rglob("*.jsonl")))
        paths.extend(sorted(raw_root.rglob("*.jsonl.gz")))
    unique: dict[pathlib.Path, None] = {}
    for path in paths:
        if path.is_file() and not path.is_symlink():
            unique[path.resolve(strict=True)] = None
    return list(unique)


def read_migration_pruned_turn_state(root: pathlib.Path) -> dict[str, Any]:
    try:
        rows = retention_pruned_store.snapshot_rows(root)
    except retention_pruned_store.RetentionPrunedStoreError as exc:
        raise service_paths.ConfigurationError(f"invalid retention pruned turn state at {root}: {exc}") from exc
    return {
        "rows": rows,
        "has_pending": any(str(row.get("state") or "") == "pending" for row in rows.values()),
    }


def retention_pruned_conflict_payload(
    source: pathlib.Path,
    destination: pathlib.Path,
    conflicts: list[tuple[str, str]],
) -> dict[str, object]:
    public_conflicts = [{"session_id": session_id, "turn_id": turn_id} for session_id, turn_id in conflicts[:20]]
    return {
        "status": "blocked",
        "migrated": False,
        "error": "retention_pruned_turn_conflict",
        "source_output_dir": str(source),
        "active_output_dir": str(destination),
        "conflicts": len(conflicts),
        "conflict_keys": public_conflicts,
        "conflict_keys_truncated": len(conflicts) > len(public_conflicts),
        "retryable": False,
    }


def plan_retention_pruned_turn_merge(source: pathlib.Path, destination: pathlib.Path) -> dict[str, Any]:
    source_state = read_migration_pruned_turn_state(source)
    destination_state = read_migration_pruned_turn_state(destination)
    source_rows = source_state["rows"]
    destination_rows = destination_state["rows"]
    conflicts = sorted(
        key
        for key in source_rows.keys() & destination_rows.keys()
        if any(
            float(source_rows[key].get(field) or 0.0) != float(destination_rows[key].get(field) or 0.0) for field in ("start_ts", "stop_ts", "captured_at_unix")
        )
    )
    if conflicts:
        raise PathMigrationBlocked(retention_pruned_conflict_payload(source, destination, conflicts))
    merged_rows = dict(destination_rows)
    merged_rows.update(source_rows)
    changed = bool(destination_state["has_pending"]) or any(
        key not in destination_rows or destination_rows[key].get("state") != "committed" for key in source_rows
    )
    return {
        "rows": [merged_rows[key] for key in sorted(merged_rows)],
        "changed": changed,
        "summary": {
            "source_rows": len(source_rows),
            "destination_rows": len(destination_rows),
            "merged_rows": len(merged_rows),
            "deduplicated_rows": len(source_rows) + len(destination_rows) - len(merged_rows),
        },
    }


def stage_migration_pruned_turn_state(destination: pathlib.Path, merge: dict[str, Any]) -> str | None:
    if not merge.get("changed"):
        return None
    try:
        return retention_pruned_store.stage_rows(
            destination,
            merge.get("rows") or [],
            job_id=f"migration:{uuid.uuid4().hex}",
        )
    except retention_pruned_store.RetentionPrunedStoreError as exc:
        raise service_paths.ConfigurationError(f"cannot stage retention pruned turn migration: {exc}") from exc


def commit_migration_pruned_turn_state(destination: pathlib.Path, pending: str | None) -> None:
    try:
        retention_pruned_store.commit_stage(destination, pending)
    except retention_pruned_store.RetentionPrunedStoreError as exc:
        raise service_paths.ConfigurationError(f"cannot commit retention pruned turn migration: {exc}") from exc


def output_migration_preview(
    source: pathlib.Path,
    destination: pathlib.Path,
    transition: service_paths.PathTransition | dict[str, object] | None,
) -> dict[str, object]:
    validate_migration_roots(source, destination)
    files = managed_content_files(source) if source != destination else []
    raw_sources = raw_migration_sources(source) if source != destination else []
    retention_pruned_turns = (
        plan_retention_pruned_turn_merge(source, destination)["summary"]
        if source != destination
        else {"source_rows": 0, "destination_rows": 0, "merged_rows": 0, "deduplicated_rows": 0}
    )
    derived_without_raw = bool(not raw_sources and any((source / name).exists() for name in ("normalized", "analytics")))
    return {
        "transition_id": (transition or {}).get("transition_id"),
        "source_output_dir": str(source),
        "active_output_dir": str(destination),
        "source_file_count": len(files),
        "source_bytes": sum(path.stat().st_size for path in files),
        "raw_source_count": len(raw_sources),
        "retention_pruned_turns": retention_pruned_turns,
        "derived_rebuild": True,
        "source_evidence_incomplete": derived_without_raw,
    }


def imported_segment_id(payload_sha256: str) -> str:
    return f"{raw_segments.PROMPT_RAW_NAME}.import.{payload_sha256[:24]}"


def import_raw_segment(source_path: pathlib.Path, destination: pathlib.Path) -> dict[str, object]:
    archive = destination / "raw" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    temporary = archive / f".import.{os.getpid()}.{time.time_ns()}.jsonl.tmp"
    digest = hashlib.sha256()
    scan_accumulator = raw_segments.JsonlScanAccumulator(kind="prompt_usage")
    uncompressed_bytes = 0
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target, raw_segments.open_segment_payload(source_path) as source:
            for line in source:
                digest.update(line)
                scan_accumulator.add(line)
                target.write(line)
                uncompressed_bytes += len(line)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    payload_sha256 = digest.hexdigest()
    segment_id = imported_segment_id(payload_sha256)
    target = archive / f"{segment_id}.jsonl"
    if target.exists():
        if target.stat().st_size != uncompressed_bytes or sha256_file(target) != payload_sha256:
            temporary.unlink(missing_ok=True)
            raise service_paths.ConfigurationError(f"imported raw segment conflict: {target}")
        temporary.unlink(missing_ok=True)
    else:
        os.replace(temporary, target)
        target.chmod(0o600)
    scan = scan_accumulator.result()
    scan["bytes"] = uncompressed_bytes
    scan["uncompressed_bytes"] = uncompressed_bytes
    scan["sha256"] = payload_sha256
    closed = raw_segments.closed_segment_from_current(
        {
            "id": segment_id,
            "kind": "prompt_usage",
            "path": str(target),
            "source_name": raw_segments.PROMPT_RAW_NAME,
            "created_at_unix": source_path.stat().st_mtime,
        },
        scan,
        kind="prompt_usage",
    )
    raw_segments.append_closed_segment(destination, closed)
    if int(closed.get("rows") or 0) == 0:
        target.unlink(missing_ok=True)
    return {
        "source": str(source_path),
        "destination": str(target),
        "segment_id": segment_id,
        "rows": int(closed.get("rows") or 0),
        "bytes": uncompressed_bytes,
        "sha256": scan["sha256"],
    }


def copy_migration_evidence(source: pathlib.Path, destination: pathlib.Path, transition_id: str) -> dict[str, object]:
    archive = destination / "reports" / "migrations" / transition_id
    candidates: list[tuple[pathlib.Path, pathlib.Path]] = []
    for directory_name in ("bad", "reports"):
        directory = source / directory_name
        if directory.is_dir() and not directory.is_symlink():
            for path in sorted(directory.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    candidates.append((path, pathlib.Path(directory_name) / path.relative_to(directory)))
    for name in service_paths.MANAGED_ROOT_FILE_NAMES:
        path = source / name
        if path.is_file() and not path.is_symlink():
            candidates.append((path, pathlib.Path("root") / name))
    state_dir = source / "state"
    if state_dir.is_dir() and not state_dir.is_symlink():
        for path in sorted(state_dir.glob("*.json")):
            if path.is_file() and not path.is_symlink():
                candidates.append((path, pathlib.Path("state") / path.name))
    copied = 0
    total_bytes = 0
    for source_path, relative in candidates:
        target = archive / relative
        if target.exists():
            if sha256_file(source_path) != sha256_file(target):
                raise service_paths.ConfigurationError(f"migration evidence conflict: {target}")
        else:
            copy_file_atomic(source_path, target)
        copied += 1
        total_bytes += source_path.stat().st_size
    return {"path": str(archive), "files": copied, "bytes": total_bytes}


def remove_source_managed_data(source: pathlib.Path) -> None:
    for path in service_paths.managed_paths(source):
        if not path.exists() and not path.is_symlink():
            continue
        remove_migrated_source(path)


def apply_output_migration(
    source: pathlib.Path,
    destination: pathlib.Path,
    transition: service_paths.PathTransition | dict[str, object] | None,
    dependencies: MigrationDependencies,
) -> tuple[int, dict[str, object]]:
    typed_transition = (
        transition
        if isinstance(transition, service_paths.PathTransition)
        else service_paths.PathTransition.from_payload(transition)
        if transition is not None
        else None
    )
    if typed_transition is None:
        transaction = service_paths.PathTransition(
            transition_id=uuid.uuid4().hex,
            source_output_dir=source,
            active_output_dir=destination,
            created_at_ns=time.time_ns(),
            phase=service_paths.PathTransitionPhase.APPLYING,
        )
    else:
        transaction = typed_transition.begin_migration()
    transition_id = transaction.transition_id
    with service_paths.acquire_path_lock(blocking=False):
        recover_preparing_path_transition()
        current_transition = service_paths.load_path_transition()
        current_paths = service_paths.resolve_runtime_paths()
        if current_paths.output_dir != destination:
            raise service_paths.ConfigurationError(f"active output directory changed before migration: {current_paths.output_dir} != {destination}")
        if typed_transition is not None:
            if not current_transition or current_transition.transition_id != transition_id:
                raise service_paths.ConfigurationError("output transition changed before migration")
        elif current_transition is not None:
            raise service_paths.ConfigurationError("a new output transition started before migration")
        active_paths = current_paths

    roots = sorted({source.resolve(strict=False), destination.resolve(strict=False)}, key=str)
    locks: dict[pathlib.Path, service_lock.ServiceLock] = {}
    applying_started = False
    retention_pruned_turns: dict[str, Any] = {
        "summary": {"source_rows": 0, "destination_rows": 0, "merged_rows": 0, "deduplicated_rows": 0},
        "changed": False,
    }
    try:
        with contextlib.ExitStack() as stack:
            for root in roots:
                locks[root] = stack.enter_context(service_lock.acquire_service_lock(reason="paths-migrate", output_dir=root))
            dependencies.resolve_physical_deletes(source)
            retention_pruned_turns = plan_retention_pruned_turn_merge(source, destination)
            with service_paths.acquire_path_lock():
                current_transition = service_paths.load_path_transition()
                current_paths = service_paths.resolve_runtime_paths()
                if current_paths.output_dir != destination:
                    raise service_paths.ConfigurationError(f"active output directory changed before migration: {current_paths.output_dir} != {destination}")
                if typed_transition is not None:
                    if not current_transition or current_transition.transition_id != transition_id:
                        raise service_paths.ConfigurationError("output transition changed before migration")
                elif current_transition is not None:
                    raise service_paths.ConfigurationError("a new output transition started before migration")
                service_paths.write_path_transition(transaction)
                applying_started = True
            handoff = handoff_hook_recovery_states(source, destination)
            source_lock = locks[source.resolve(strict=False)]
            source_env = service_lock.child_lock_env(
                runtime_env(active_paths.codex_dir, source),
                source_lock.path,
                source_lock.fd,
            )
            require_migration_process_result(
                dependencies.run_command(RuntimeCommand.RECONCILE, [], source_env),
                allow_degraded=True,
            )

            imports = [import_raw_segment(path, destination) for path in raw_migration_sources(source, recover=False)]
            evidence = copy_migration_evidence(source, destination, transition_id)
            staged_pruned_turns = stage_migration_pruned_turn_state(destination, retention_pruned_turns)
            destination_lock = locks[destination.resolve(strict=False)]
            destination_env = service_lock.child_lock_env(
                runtime_env(active_paths.codex_dir, destination),
                destination_lock.path,
                destination_lock.fd,
            )
            normalize_result = require_migration_process_result(
                dependencies.run_command(RuntimeCommand.NORMALIZE, [], destination_env),
                allow_degraded=True,
            )
            build_result = require_migration_process_result(
                dependencies.run_command(
                    RuntimeCommand.BUILD,
                    [],
                    destination_env,
                ),
                allow_degraded=False,
            )
            commit_migration_pruned_turn_state(destination, staged_pruned_turns)
            for source_path in handoff["sources"]:
                pathlib.Path(source_path).unlink(missing_ok=True)
            remove_source_managed_data(source)
    except Exception:
        if applying_started:
            transaction = transaction.mark_recovery_required()
            with service_paths.acquire_path_lock():
                service_paths.write_path_transition(transaction)
        raise

    with service_paths.acquire_path_lock():
        service_paths.clear_path_transition()
    return 0, {
        "status": "applied",
        "migrated": True,
        "transition_id": transition_id,
        "source_output_dir": str(source),
        "active_output_dir": str(destination),
        "imported_segments": imports,
        "imported_rows": sum(int(item["rows"]) for item in imports),
        "evidence": evidence,
        "retention_pruned_turns": retention_pruned_turns["summary"],
        "normalize": normalize_result,
        "build": build_result,
    }


def run_paths_migrate(options: PathsMigrateOptions, dependencies: MigrationDependencies) -> PathsResult:
    if not options.output_dir:
        raise service_paths.ConfigurationError("paths migrate requires --output-dir")
    with service_paths.acquire_path_lock(blocking=False):
        recover_preparing_path_transition()
        transition, source, destination = pending_output_migration()
    try:
        preview_fn = dependencies.preview_migration or output_migration_preview
        preview_transition = transition.to_payload() if transition is not None else None
        preview = preview_fn(source, destination, preview_transition)
    except PathMigrationBlocked as exc:
        return PathsResult(exit_code=2, payload=exc.payload())
    if source == destination or preview["source_file_count"] == 0:
        if options.apply and transition:
            with service_paths.acquire_path_lock():
                current = service_paths.load_path_transition()
                if not current or current.transition_id != transition.transition_id:
                    raise service_paths.ConfigurationError("output transition changed before migration cleanup")
                if service_paths.resolve_runtime_paths().output_dir != destination:
                    raise service_paths.ConfigurationError("active output directory changed before migration cleanup")
                service_paths.clear_path_transition()
        return PathsResult(
            exit_code=0,
            payload={**preview, "status": "noop", "migrated": False, "dry_run": not options.apply},
        )
    if preview["source_evidence_incomplete"]:
        return PathsResult(
            exit_code=2,
            payload={**preview, "status": "failed", "migrated": False, "error": "source_evidence_incomplete"},
        )
    if not options.apply:
        return PathsResult(
            exit_code=0,
            payload={**preview, "status": "preview", "migrated": False, "dry_run": True},
        )
    try:
        if dependencies.apply_migration is None:
            code, result = apply_output_migration(source, destination, transition, dependencies)
        else:
            code, result = dependencies.apply_migration(source, destination, transition.to_payload() if transition is not None else None)
    except PathMigrationBlocked as exc:
        return PathsResult(exit_code=2, payload=exc.payload())
    return PathsResult(exit_code=code, payload=result)
