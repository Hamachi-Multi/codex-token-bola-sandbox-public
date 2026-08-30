"""Application services for doctor and quarantine commands."""

from __future__ import annotations

import json
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dashboard_cleanup
import quarantine_health
import raw_segments
import retention_checkpoints
import retention_pruned_store
import service_lock
import service_paths

DOCTOR_RECENT_ERROR_WINDOW_SECONDS = 24 * 60 * 60
DOCTOR_STALE_PENDING_STATE_SECONDS = 24 * 60 * 60
DOCTOR_STALE_ANALYTICS_TMP_SECONDS = 60 * 60
RECOVERY_RECORD_TYPES = frozenset({"turn_start", "turn_stop_missing_start"})


@dataclass(frozen=True)
class DoctorOptions:
    codex_dir: str | None = None
    output_dir: str | None = None


@dataclass(frozen=True)
class DoctorResult:
    report: dict[str, object]
    exit_code: int


@dataclass(frozen=True)
class DoctorDependencies:
    resolve_paths: Callable[[str | None, str | None], service_paths.RuntimePaths]
    codex_dir_status: Callable[[pathlib.Path], dict[str, object]]
    codex_cli_status: Callable[[], dict[str, object]]
    hook_install_status: Callable[[pathlib.Path], dict[str, object]]
    hooks_json_status: Callable[[pathlib.Path], dict[str, object]]
    now: Callable[[], float] = time.time


@dataclass(frozen=True)
class QuarantineOptions:
    action: str
    codex_dir: str | None = None
    output_dir: str | None = None
    include_acknowledged: bool = False
    event_ids: tuple[str, ...] = ()
    acknowledge_all: bool = False


@dataclass(frozen=True)
class QuarantineResult:
    payload: dict[str, object]
    exit_code: int
    pretty: bool = False


def int_metadata(metadata: dict[str, object], key: str, default: int = 0) -> int:
    try:
        return int(metadata.get(key, default) or 0)
    except (TypeError, ValueError):
        return default


def current_segments_status(base: pathlib.Path) -> dict[str, object]:
    status: dict[str, object] = {}
    try:
        pointer = raw_segments.strict_read_current_pointer(base)
    except raw_segments.ManifestError as exc:
        return {"error": str(exc)}
    for kind, current in sorted(pointer.get("current", {}).items()):
        if not isinstance(current, dict):
            status[str(kind)] = {"error": "current entry is not an object"}
            continue
        try:
            segment = raw_segments.validate_current_segment_entry(base, current, kind=str(kind))
            path = pathlib.Path(str(segment.get("path") or ""))
            try:
                with path.open("rt", encoding="utf-8") as handle:
                    rows = sum(1 for line in handle if line.strip())
            except OSError:
                rows = 0
            status[str(kind)] = {
                "path": str(path),
                "exists": path.exists(),
                "bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
                "rows": rows,
            }
        except raw_segments.ManifestError as exc:
            status[str(kind)] = {"error": str(exc)}
    return status


def age_seconds(timestamp: float, now_unix: float) -> int:
    return max(0, int(now_unix - timestamp))


def pending_recovery_state_summary(base: pathlib.Path, *, now_unix: float | None = None) -> dict[str, object]:
    now = time.time() if now_unix is None else now_unix
    state_dir = base / "state"
    files: list[str] = []
    recovery_required_files: list[str] = []
    counts_by_type: dict[str, int] = {}
    oldest_age = 0
    try:
        candidates = sorted(state_dir.glob("*.json"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("record_type") not in RECOVERY_RECORD_TYPES:
            continue
        record_type = str(payload["record_type"])
        files.append(str(path))
        counts_by_type[record_type] = counts_by_type.get(record_type, 0) + 1
        try:
            pending_age = age_seconds(path.stat().st_mtime, now)
        except OSError:
            pending_age = DOCTOR_STALE_PENDING_STATE_SECONDS
        oldest_age = max(oldest_age, pending_age)
        if record_type == "turn_stop_missing_start" or pending_age >= DOCTOR_STALE_PENDING_STATE_SECONDS:
            recovery_required_files.append(str(path))
    return {
        "pending_state_files": len(files),
        "pending_state_paths": files[:20],
        "pending_state_paths_truncated": len(files) > 20,
        "pending_state_counts_by_type": counts_by_type,
        "stale_after_seconds": DOCTOR_STALE_PENDING_STATE_SECONDS,
        "oldest_pending_state_age_seconds": oldest_age if files else None,
        "recovery_required_state_files": len(recovery_required_files),
        "recovery_required_state_paths": recovery_required_files[:20],
        "recovery_required_state_paths_truncated": len(recovery_required_files) > 20,
    }


def parse_captured_at_unix(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def error_log_summary(base: pathlib.Path, *, now_unix: float | None = None) -> dict[str, object]:
    now = time.time() if now_unix is None else now_unix
    counts: dict[str, int] = {}
    recent_error_counts: dict[str, int] = {}
    last_error: dict[str, object] | None = None
    last_error_unix: float | None = None
    path = base / "prompt-usage-errors.jsonl"
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return {
            "counts": counts,
            "recent_error_counts": recent_error_counts,
            "recent_window_seconds": DOCTOR_RECENT_ERROR_WINDOW_SECONDS,
            "last_error": last_error,
        }
    with handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                counts["invalid_json"] = counts.get("invalid_json", 0) + 1
                continue
            if not isinstance(payload, dict):
                counts["wrong_type"] = counts.get("wrong_type", 0) + 1
                continue
            code = payload.get("error") or payload.get("warning")
            if not code:
                continue
            prefix = "error" if payload.get("error") else "warning"
            key = f"{prefix}:{code}"
            counts[key] = counts.get(key, 0) + 1
            if prefix != "error":
                continue
            captured_at_unix = parse_captured_at_unix(payload.get("captured_at"))
            if captured_at_unix is None:
                continue
            event_age = age_seconds(captured_at_unix, now)
            if event_age <= DOCTOR_RECENT_ERROR_WINDOW_SECONDS:
                recent_error_counts[key] = recent_error_counts.get(key, 0) + 1
            if last_error_unix is None or captured_at_unix > last_error_unix:
                last_error_unix = captured_at_unix
                last_error = {"code": key, "captured_at": payload.get("captured_at"), "age_seconds": event_age}
    return {
        "counts": counts,
        "recent_error_counts": recent_error_counts,
        "recent_window_seconds": DOCTOR_RECENT_ERROR_WINDOW_SECONDS,
        "last_error": last_error,
    }


def analytics_tmp_file_summary(base: pathlib.Path, *, now_unix: float | None = None) -> dict[str, object]:
    now = time.time() if now_unix is None else now_unix
    files: list[dict[str, object]] = []
    try:
        candidates = sorted((base / "analytics").glob(".bola.sqlite.*.tmp*"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            stat_result = path.lstat()
        except OSError:
            continue
        suffix = next((value for value in ("-journal", "-wal", "-shm") if path.name.endswith(value)), "")
        file_age = age_seconds(stat_result.st_mtime, now)
        files.append(
            {
                "path": str(path),
                "bytes": stat_result.st_size,
                "mtime_unix": stat_result.st_mtime,
                "age_seconds": file_age,
                "stale": file_age >= DOCTOR_STALE_ANALYTICS_TMP_SECONDS,
                "sidecar": suffix.removeprefix("-") or None,
            }
        )
    stale_files = [item for item in files if item["stale"]]
    return {
        "count": len(files),
        "bytes": sum(int(item["bytes"]) for item in files),
        "files": files[:20],
        "files_truncated": len(files) > 20,
        "stale_after_seconds": DOCTOR_STALE_ANALYTICS_TMP_SECONDS,
        "stale_count": len(stale_files),
        "stale_bytes": sum(int(item["bytes"]) for item in stale_files),
        "oldest_age_seconds": max((int(item["age_seconds"]) for item in files), default=None),
    }


def normalize_pending_publish_summary(base: pathlib.Path) -> dict[str, object]:
    path = base / "normalized" / "normalize-state.json.pending"
    if not path.exists():
        return {"exists": False, "path": str(path), "recovery_required": False}
    summary: dict[str, object] = {"exists": True, "path": str(path), "recovery_required": True}
    try:
        summary["bytes"] = path.stat().st_size
    except OSError:
        summary["bytes"] = None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summary["valid"] = False
        summary["error"] = type(exc).__name__
        return summary
    summary["valid"] = isinstance(payload, dict)
    summary["full_publish"] = bool(payload.get("full_publish")) if isinstance(payload, dict) else False
    return summary


def retention_pruned_store_summary(base: pathlib.Path) -> dict[str, object]:
    legacy = [base / relative for relative in retention_pruned_store.LEGACY_RELATIVE_PATHS]
    existing_legacy = [path for path in legacy if path.exists()]
    return {
        **retention_pruned_store.inspect_summary(base),
        "legacy_files": [str(path) for path in existing_legacy],
        "migration_required": bool(existing_legacy),
    }


def cleanup_retention_job_summary(base: pathlib.Path) -> dict[str, object]:
    path = dashboard_cleanup.cleanup_retention_job_path(base)
    try:
        job = dashboard_cleanup.read_cleanup_retention_job(base)
    except raw_segments.ManifestError as exc:
        return {"path": str(path), "exists": path.exists(), "valid": False, "error": str(exc), "job": None}
    return {"path": str(path), "exists": job is not None, "valid": True, "job": job}


def path_transition_summary() -> dict[str, object]:
    path = service_paths.path_transition_path()
    try:
        transition = service_paths.read_path_transition(path)
    except service_paths.ConfigurationError as exc:
        return {"path": str(path), "exists": path.exists(), "valid": False, "error": str(exc), "transition": None}
    return {"path": str(path), "exists": transition is not None, "valid": True, "transition": transition}


def doctor_health(report: dict[str, object]) -> dict[str, object]:
    issues: list[dict[str, object]] = []

    def add_issue(code: str, severity: str, **fields: object) -> None:
        issues.append({"code": code, "severity": severity, **fields})

    dir_status = report["codex_dir"]
    cli_status = report["codex_cli"]
    config_status = report.get("config")
    runtime = report["runtime"]
    if isinstance(config_status, dict) and not config_status.get("configured"):
        add_issue(
            "runtime_config_missing",
            "failed",
            path=config_status.get("path") if isinstance(config_status, dict) else None,
        )
    if isinstance(dir_status, dict) and not dir_status.get("valid"):
        add_issue("codex_dir_invalid", "failed", reason=dir_status.get("reason"))
    if isinstance(cli_status, dict) and not cli_status.get("valid"):
        add_issue("codex_cli_invalid", "failed", reason=cli_status.get("reason"))
    if not isinstance(runtime, dict):
        add_issue("runtime_status_invalid", "failed")
    else:
        current_segments = runtime.get("current_segments")
        if isinstance(current_segments, dict):
            segment_errors = [str(value.get("error")) for value in current_segments.values() if isinstance(value, dict) and value.get("error")]
            if current_segments.get("error"):
                segment_errors.append(str(current_segments["error"]))
            if segment_errors:
                add_issue("current_segment_state_invalid", "failed", count=len(segment_errors), errors=segment_errors[:5])

        hooks_status = runtime.get("hooks_json")
        if isinstance(hooks_status, dict):
            if hooks_status.get("error"):
                add_issue("hooks_config_invalid", "failed", error=hooks_status.get("error"))
            events = hooks_status.get("events")
            if isinstance(events, dict):
                missing = sorted(str(name) for name, value in events.items() if isinstance(value, dict) and not value.get("registered"))
                stale = sorted(str(name) for name, value in events.items() if isinstance(value, dict) and value.get("stale_commands"))
                if missing:
                    add_issue("hook_registration_missing", "degraded", events=missing)
                if stale:
                    add_issue("stale_hook_registration", "degraded", events=stale)

        pending_publish = runtime.get("normalize_pending_publish")
        if isinstance(pending_publish, dict) and pending_publish.get("recovery_required"):
            add_issue("normalize_pending_publish_recovery_required", "failed", path=pending_publish.get("path"))

        recovery = runtime.get("recovery")
        if isinstance(recovery, dict):
            recovery_required = int_metadata(recovery, "recovery_required_state_files")
            if recovery_required:
                add_issue("pending_recovery_state", "degraded", count=recovery_required)
            recent_errors = recovery.get("recent_error_log_counts")
            if isinstance(recent_errors, dict) and recent_errors:
                add_issue("recent_hook_errors", "degraded", count=sum(int(value) for value in recent_errors.values()), errors=recent_errors)

        analytics_tmp = runtime.get("analytics_tmp_files")
        if isinstance(analytics_tmp, dict):
            stale_count = int_metadata(analytics_tmp, "stale_count")
            if stale_count:
                add_issue("stale_analytics_temp_files", "degraded", count=stale_count, bytes=int_metadata(analytics_tmp, "stale_bytes"))

        pruned_store = runtime.get("retention_pruned_store")
        cleanup_job_status = runtime.get("cleanup_retention_job")
        checkpoint_status = runtime.get("retention_checkpoints")
        lock_status = runtime.get("service_lock")
        transition_status = runtime.get("path_transition")
        for code, value in (
            ("retention_pruned_store_invalid", pruned_store),
            ("cleanup_retention_job_invalid", cleanup_job_status),
            ("retention_checkpoint_invalid", checkpoint_status),
            ("service_lock_state_invalid", lock_status),
            ("path_transition_invalid", transition_status),
        ):
            if isinstance(value, dict) and not value.get("valid", True):
                add_issue(code, "failed", error=value.get("error"))
        if isinstance(pruned_store, dict):
            if pruned_store.get("migration_required"):
                add_issue("retention_pruned_store_migration_required", "degraded", files=pruned_store.get("legacy_files"))
            pending_rows = int_metadata(pruned_store, "pending_rows")
            if pending_rows and pruned_store.get("valid", True):
                pending_jobs = {str(value) for value in pruned_store.get("pending_job_ids") or []}
                cleanup_job = cleanup_job_status.get("job") if isinstance(cleanup_job_status, dict) else None
                cleanup_ids: set[str] = set()
                if isinstance(cleanup_job, dict):
                    cleanup_ids = {
                        str(cleanup_job.get("operation_job_id") or ""),
                        str(cleanup_job.get("pruned_state_job_id") or ""),
                    } - {""}
                matching_cleanup = bool(pending_jobs & cleanup_ids) or bool(pruned_store.get("pending_job_ids_truncated") and cleanup_ids)
                transition = transition_status.get("transition") if isinstance(transition_status, dict) else None
                matching_migration = bool(
                    isinstance(transition, dict)
                    and pending_jobs
                    and not pruned_store.get("pending_job_ids_truncated")
                    and all(value.startswith("migration:") for value in pending_jobs)
                )
                lock_held = bool(isinstance(lock_status, dict) and lock_status.get("held"))
                if matching_cleanup and isinstance(cleanup_job, dict) and cleanup_job.get("pruned_state_commit_ready"):
                    add_issue("retention_pruned_state_recovery_ready", "degraded", count=pending_rows, jobs=sorted(pending_jobs))
                elif matching_cleanup and isinstance(cleanup_job, dict) and cleanup_job.get("phase") == "failed":
                    add_issue("retention_pruned_state_resolution_required", "failed", count=pending_rows, jobs=sorted(pending_jobs))
                elif lock_held or matching_cleanup or matching_migration:
                    add_issue("retention_pruned_state_pending", "degraded", count=pending_rows, jobs=sorted(pending_jobs))
                else:
                    add_issue("retention_pruned_state_orphaned", "failed", count=pending_rows, jobs=sorted(pending_jobs))
        if isinstance(checkpoint_status, dict) and checkpoint_status.get("valid", True) and int_metadata(checkpoint_status, "count"):
            add_issue(
                "stale_retention_checkpoints",
                "degraded",
                count=int_metadata(checkpoint_status, "count"),
                bytes=int_metadata(checkpoint_status, "bytes"),
                jobs=checkpoint_status.get("operation_job_ids"),
            )

        quarantine = runtime.get("quarantine")
        if isinstance(quarantine, dict):
            if quarantine.get("error"):
                add_issue("quarantine_state_invalid", "failed", error=quarantine.get("error"))
            else:
                unacknowledged = int_metadata(quarantine, "unacknowledged_events")
                if unacknowledged:
                    add_issue(
                        "unacknowledged_quarantine",
                        "degraded",
                        count=unacknowledged,
                        occurrences=int_metadata(quarantine, "unacknowledged_occurrences"),
                        by_kind=quarantine.get("by_kind"),
                    )

    if any(issue["severity"] == "failed" for issue in issues):
        return {"status": "failed", "exit_code": 2, "issues": issues}
    if issues:
        return {"status": "degraded", "exit_code": 1, "issues": issues}
    return {"status": "healthy", "exit_code": 0, "issues": []}


def run_doctor(options: DoctorOptions, dependencies: DoctorDependencies) -> DoctorResult:
    paths = dependencies.resolve_paths(options.codex_dir, options.output_dir)
    codex_dir = paths.codex_dir
    base = paths.output_dir
    inspected_paths = {
        "codex_dir": codex_dir,
        "output_dir": base,
        "project_root": paths.project_root,
        "config": paths.runtime_config_path,
        "normalized_log": base / "normalized" / "prompt-usage.normalized.jsonl",
        "analytics_db": base / "analytics" / "bola.sqlite",
        "state_db": codex_dir / "state_5.sqlite",
    }
    report: dict[str, object] = {
        key: {
            "path": str(path),
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() and path.is_file() else None,
        }
        for key, path in inspected_paths.items()
    }
    report["codex_dir"] = dependencies.codex_dir_status(codex_dir)
    report["codex_cli"] = dependencies.codex_cli_status()
    config = report["config"]
    assert isinstance(config, dict)
    config["configured"] = paths.runtime_config_path.exists()
    report["installed_hook"] = dependencies.hook_install_status(codex_dir)
    now_unix = dependencies.now()
    error_summary = error_log_summary(base, now_unix=now_unix)
    try:
        quarantine_summary = quarantine_health.summary(base)
    except quarantine_health.QuarantineError as exc:
        quarantine_summary = {"error": str(exc), "state_path": str(quarantine_health.state_path(base))}
    report["runtime"] = {
        "current_segments": current_segments_status(base),
        "hooks_json": dependencies.hooks_json_status(codex_dir),
        "recovery": {
            **pending_recovery_state_summary(base, now_unix=now_unix),
            "error_log_counts": error_summary["counts"],
            "recent_error_log_counts": error_summary["recent_error_counts"],
            "recent_error_window_seconds": error_summary["recent_window_seconds"],
            "last_error": error_summary["last_error"],
        },
        "normalize_pending_publish": normalize_pending_publish_summary(base),
        "analytics_tmp_files": analytics_tmp_file_summary(base, now_unix=now_unix),
        "retention_pruned_store": retention_pruned_store_summary(base),
        "cleanup_retention_job": cleanup_retention_job_summary(base),
        "retention_checkpoints": retention_checkpoints.inspect(base),
        "service_lock": service_lock.inspect_service_lock(service_lock.default_lock_path(output_dir=base)),
        "path_transition": path_transition_summary(),
        "quarantine": quarantine_summary,
    }
    report["health"] = doctor_health(report)
    health = report["health"]
    assert isinstance(health, dict)
    return DoctorResult(report=report, exit_code=int(health["exit_code"]))


def run_quarantine(
    options: QuarantineOptions,
    resolve_paths: Callable[[str | None, str | None], service_paths.RuntimePaths],
) -> QuarantineResult:
    paths = resolve_paths(options.codex_dir, options.output_dir)
    if options.action == "list":
        report = quarantine_health.summary(
            paths.output_dir,
            include_entries=True,
            include_acknowledged=options.include_acknowledged,
        )
        status = "degraded" if report["unacknowledged_events"] else "healthy"
        return QuarantineResult(
            payload={"status": status, "quarantine": report},
            exit_code=1 if status == "degraded" else 0,
            pretty=True,
        )
    if options.action != "acknowledge":
        raise ValueError(f"unsupported quarantine action: {options.action}")
    with service_lock.acquire_service_lock(reason="quarantine-acknowledge", output_dir=paths.output_dir):
        result = quarantine_health.acknowledge(
            paths.output_dir,
            event_ids=list(options.event_ids) or None,
            acknowledge_all=options.acknowledge_all,
        )
    return QuarantineResult(payload=result, exit_code=0)
