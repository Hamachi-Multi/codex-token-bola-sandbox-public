"""Cleanup dashboard payload builders and file deletion helpers."""

from __future__ import annotations

import pathlib
import shutil
import sys
import time
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import raw_segments
import service_lock

from dashboard_cleanup_contract import cleanup_group_for_label, cleanup_row_definitions
from dashboard_cleanup_common import (
    existing_target_paths,
    impact_payload,
    read_run_metadata,
    safe_file_size,
    safe_tree_size,
    target_paths_count,
)
from dashboard_cleanup_recovery import read_cleanup_retention_job
from dashboard_cleanup_retention import (
    RETENTION_PREVIEW_CACHE,
    default_retention_cutoff_unix,
    pending_turn_state_paths,
    plan_pending_turn_state_for_retention,
    retention_preview,
    retention_preview_signature,
)

def _delete_all_target_size(path: pathlib.Path) -> int:
    try:
        if path.is_symlink():
            return path.lstat().st_size
    except OSError:
        return 0
    return safe_tree_size(path)


def _delete_service_owned_path(path: pathlib.Path, base_resolved: pathlib.Path, target: str) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        path.parent.resolve(strict=False).relative_to(base_resolved)
    except (OSError, ValueError):
        return {"target": target, "path": str(path), "deleted_bytes": 0, "skipped": "outside_service_root"}
    before = _delete_all_target_size(path)
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return {"target": target, "path": str(path), "deleted_bytes": 0}
    except OSError as exc:
        return {"target": target, "path": str(path), "deleted_bytes": 0, "failed": repr(exc)}
    return {"target": target, "path": str(path), "deleted_bytes": before}


def delete_all_logs(token_usage_root: pathlib.Path | str, db_path: pathlib.Path | str | None = None) -> dict[str, Any]:
    RETENTION_PREVIEW_CACHE.clear()
    base = pathlib.Path(token_usage_root).expanduser()
    base_resolved = base.resolve(strict=False)
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen: set[pathlib.Path] = set()

    def delete_path(path: pathlib.Path, target: str) -> None:
        key = path.resolve(strict=False)
        if key in seen:
            return
        seen.add(key)
        result = _delete_service_owned_path(path, base_resolved, target)
        if result is None:
            return
        if result.get("failed"):
            failed.append(result)
        elif result.get("skipped"):
            skipped.append(result)
        else:
            deleted.append(result)

    with service_lock.acquire_service_lock(base / "state" / "service.lock", reason="delete-all"):
        with raw_segments.acquire_raw_segment_lock(base):
            for name in ("raw", "normalized", "analytics", "tmp", "bad"):
                delete_path(base / name, name)

            for pattern in ("prompt-usage*.jsonl", "hook-probe-events.jsonl"):
                for path in sorted(base.glob(pattern)):
                    if path.parent == base:
                        delete_path(path, "top_level_log")

            if db_path is not None:
                db = pathlib.Path(db_path).expanduser()
                try:
                    db.resolve(strict=False).relative_to(base_resolved)
                except (OSError, ValueError):
                    pass
                else:
                    delete_path(db, "analytics_database")

            state_dir = base / "state"
            try:
                state_children = sorted(state_dir.iterdir(), key=lambda item: item.name)
            except FileNotFoundError:
                state_children = []
            for child in state_children:
                if child.name.endswith(".lock"):
                    continue
                delete_path(child, "state")

    deleted_bytes = sum(int(item.get("deleted_bytes") or 0) for item in deleted)
    return {
        "deleted_bytes": deleted_bytes,
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "delete_failed": bool(failed),
        "partial_mutation": bool(failed and deleted_bytes > 0),
    }


def int_metadata(metadata: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(metadata.get(key, default) or 0)
    except (TypeError, ValueError):
        return default


def cleanup_payload(
    token_usage_root: pathlib.Path,
    db_path: pathlib.Path | str,
    base_dir: pathlib.Path | str | None = None,
    retention_cutoff_unix: float | None = None,
    *,
    include_targets: bool = False,
    row_group_id: str | None = None,
    refresh_retention_index: bool = True,
) -> dict[str, Any]:
    base = pathlib.Path(base_dir).expanduser() if base_dir is not None else pathlib.Path(token_usage_root).expanduser()
    db = pathlib.Path(db_path).expanduser()
    metadata = read_run_metadata(db)
    archive_dir = base / "raw" / "archive"
    current_dir = base / "raw" / "current"
    normalized_prompt = base / "normalized" / "prompt-usage.normalized.jsonl"
    normalized_prompt_archive = base / "normalized" / "prompt-usage.normalized.jsonl.gz"
    normalize_state = base / "normalized" / "normalize-state.json"
    state_dir = base / "state"

    tree_size_cache: dict[str, int] = {}

    def cached_tree_size(path: pathlib.Path) -> int:
        key = str(path.expanduser())
        if key not in tree_size_cache:
            tree_size_cache[key] = safe_tree_size(path)
        return tree_size_cache[key]

    def group_size(paths: list[pathlib.Path]) -> int:
        return sum(cached_tree_size(path) for path in paths)

    def cached_target_paths_size(paths: list[pathlib.Path]) -> int:
        return sum(cached_tree_size(path) for path in paths)

    def proportional_affected_size_cached(paths: list[pathlib.Path], total_rows: int, affected_rows: int) -> int:
        size = cached_target_paths_size(paths)
        total = max(0, int(total_rows or 0))
        affected = max(0, int(affected_rows or 0))
        if size <= 0 or total <= 0 or affected <= 0:
            return 0
        if affected >= total:
            return size
        return max(1, (size * affected + total - 1) // total)

    hook_probe_logs = sorted(base.glob("hook-probe-events.jsonl"))
    retention_reset_targets = {
        "Normalized Outputs": [normalized_prompt, normalize_state],
        "Analytics Database": [db],
    }
    delete_all_targets = {
        "Normalized Outputs": [normalized_prompt, normalized_prompt_archive, normalize_state],
        "Analytics Database": [db],
        "Archived Raw Logs": [archive_dir],
        "Raw Current Segments": [current_dir],
        "State Files": hook_probe_logs,
    }
    now_unix = time.time()
    cutoff_unix = float(retention_cutoff_unix) if retention_cutoff_unix is not None else default_retention_cutoff_unix(now_unix)
    selected_retention = retention_preview(base, cutoff_unix, refresh_index=refresh_retention_index)
    selected_retention["preview_signature"] = retention_preview_signature(base, cutoff_unix)
    selected_retention["source_files"] = len(selected_retention.get("files", []))
    selected_retention["all_files"] = sum(
        1
        for file in selected_retention.get("files", [])
        if int(file.get("scanned_rows") or 0) > 0 or int(file.get("source_size") or 0) > 0
    )
    cleanup_job = read_cleanup_retention_job(base)
    try:
        state_delete_all_targets = [path for path in sorted(state_dir.iterdir(), key=lambda item: item.name) if not path.name.endswith(".lock")]
    except FileNotFoundError:
        state_delete_all_targets = []
    pending_turn_state_targets = pending_turn_state_paths(state_dir)
    pending_turn_state_plan = plan_pending_turn_state_for_retention(base, cutoff_unix)
    pending_deleted_files = int(pending_turn_state_plan.get("deleted_files") or 0)
    pending_scanned_files = int(pending_turn_state_plan.get("scanned_files") or len(pending_turn_state_targets))
    pending_deleted_bytes = int(pending_turn_state_plan.get("deleted_bytes") or 0)
    selected_retention["affected_files"] = int(selected_retention.get("affected_files") or 0) + pending_deleted_files
    selected_retention["pending_turn_state_scanned_files"] = pending_scanned_files
    selected_retention["pending_turn_state_deletable_files"] = pending_deleted_files
    selected_retention["pending_turn_state_deletable_bytes"] = pending_deleted_bytes
    pending_turn_state_keys = {path.resolve(strict=False) for path in pending_turn_state_targets}
    service_state_targets = [path for path in state_delete_all_targets if path.resolve(strict=False) not in pending_turn_state_keys]
    state_file_targets = [*service_state_targets, *hook_probe_logs]
    delete_all_targets["Pending Turn State"] = pending_turn_state_targets
    delete_all_targets["State Files"] = state_file_targets
    paths_by_label = {
        "Normalized Outputs": [normalized_prompt, normalized_prompt_archive, normalize_state],
        "Analytics Database": [db],
        "Archived Raw Logs": [archive_dir],
        "Raw Current Segments": [current_dir],
        "Pending Turn State": pending_turn_state_targets,
        "State Files": state_file_targets,
    }
    compactable_by_label: dict[str, int] = {}

    def retention_matches(paths: list[pathlib.Path]) -> list[dict[str, Any]]:
        path_texts = [str(path) for path in paths]
        return [
            file
            for file in selected_retention.get("files", [])
            if any(str(file.get("path") or "") == path or str(file.get("path") or "").startswith(path + "/") for path in path_texts)
        ]

    def source_retention_impact(paths: list[pathlib.Path]) -> dict[str, Any]:
        matches = retention_matches(paths)
        affected_file_paths = [pathlib.Path(str(file.get("path") or "")) for file in matches if file.get("affected")]
        source_count = sum(1 for file in matches if file.get("affected"))
        delete_files = sum(
            1
            for file in matches
            if file.get("affected") and int(file.get("deletable_rows") or 0) >= int(file.get("scanned_rows") or 0)
        )
        rewrite_files = sum(
            1
            for file in matches
            if file.get("affected") and 0 < int(file.get("deletable_rows") or 0) < int(file.get("scanned_rows") or 0)
        )
        impact = impact_payload(
            total_rows=sum(int(file.get("scanned_rows") or 0) for file in matches),
            affected_rows=sum(int(file.get("deletable_rows") or 0) for file in matches),
            delete_size=sum(int(file.get("deletable_bytes") or 0) for file in matches),
            affected_files=source_count,
            source_files=source_count,
            targets=affected_file_paths,
            include_targets=include_targets,
        )
        if include_targets:
            impact["items"] = [dict(file) for file in matches if file.get("affected")]
        impact["delete_files"] = delete_files
        impact["rewrite_files"] = rewrite_files
        return impact

    def retention_source_files_for_label(label: str) -> list[dict[str, Any]]:
        return list(selected_retention.get("files", []))

    def derived_total_rows(label: str) -> int:
        return sum(int(file.get("scanned_rows") or 0) for file in retention_source_files_for_label(label))

    def derived_retention_impact(label: str) -> dict[str, Any]:
        source_files_all = retention_source_files_for_label(label)
        total_rows = sum(int(file.get("scanned_rows") or 0) for file in source_files_all)
        affected_rows = sum(int(file.get("deletable_rows") or 0) for file in source_files_all)
        candidate_paths = retention_reset_targets.get(label, []) if affected_rows > 0 else []
        affected_file_paths = existing_target_paths(candidate_paths) if include_targets else []
        affected_file_count = len(affected_file_paths) if include_targets else target_paths_count(candidate_paths)
        source_files = derived_source_files(label, delete_all=False)
        return impact_payload(
            total_rows=total_rows,
            affected_rows=affected_rows,
            delete_size=proportional_affected_size_cached(candidate_paths, total_rows, affected_rows),
            affected_files=affected_file_count,
            source_files=len(source_files),
            targets=affected_file_paths,
            include_targets=include_targets,
        )

    def derived_source_files(label: str, *, delete_all: bool) -> list[dict[str, Any]]:
        def has_rows(file: dict[str, Any]) -> bool:
            if delete_all:
                return int(file.get("scanned_rows") or 0) > 0 or int(file.get("source_size") or 0) > 0
            return int(file.get("deletable_rows") or 0) > 0

        return [file for file in retention_source_files_for_label(label) if has_rows(file)]

    def pending_turn_state_retention_impact() -> dict[str, Any]:
        affected_file_paths = [pathlib.Path(str(path)) for path in pending_turn_state_plan.get("targets") or []]
        return impact_payload(
            total_rows=0,
            affected_rows=0,
            delete_size=pending_deleted_bytes,
            affected_files=pending_deleted_files,
            targets=affected_file_paths,
            include_targets=include_targets,
        )

    def source_delete_all_impact(label: str, paths: list[pathlib.Path]) -> dict[str, Any]:
        matches = retention_matches(paths)
        candidate_paths = delete_all_targets.get(label, paths)
        affected_file_paths = existing_target_paths(candidate_paths) if include_targets else []
        affected_file_count = len(affected_file_paths) if include_targets else target_paths_count(candidate_paths)
        source_count = sum(
            1
            for file in matches
            if int(file.get("scanned_rows") or 0) > 0 or int(file.get("source_size") or 0) > 0
        )
        affected_files = source_count if source_count > 0 else affected_file_count
        impact = impact_payload(
            total_rows=sum(int(file.get("scanned_rows") or 0) for file in matches),
            affected_rows=sum(int(file.get("scanned_rows") or 0) for file in matches),
            delete_size=cached_target_paths_size(candidate_paths),
            affected_files=affected_files,
            source_files=source_count,
            targets=affected_file_paths,
            include_targets=include_targets,
        )
        impact["delete_files"] = affected_files
        impact["rewrite_files"] = 0
        return impact

    def file_delete_all_impact(label: str, *, total_rows: int = 0) -> dict[str, Any]:
        candidate_paths = delete_all_targets.get(label, [])
        affected_file_paths = existing_target_paths(candidate_paths) if include_targets else []
        affected_file_count = len(affected_file_paths) if include_targets else target_paths_count(candidate_paths)
        return impact_payload(
            total_rows=total_rows,
            affected_rows=total_rows,
            delete_size=cached_target_paths_size(candidate_paths),
            affected_files=affected_file_count,
            targets=affected_file_paths,
            include_targets=include_targets,
        )

    def derived_delete_all_impact(label: str, *, total_rows: int = 0) -> dict[str, Any]:
        candidate_paths = delete_all_targets.get(label, [])
        affected_file_paths = existing_target_paths(candidate_paths) if include_targets else []
        affected_file_count = len(affected_file_paths) if include_targets else target_paths_count(candidate_paths)
        source_files = derived_source_files(label, delete_all=True)
        return impact_payload(
            total_rows=total_rows,
            affected_rows=total_rows,
            delete_size=cached_target_paths_size(candidate_paths),
            affected_files=affected_file_count,
            source_files=len(source_files),
            targets=affected_file_paths,
            include_targets=include_targets,
        )

    def impacts_for_row(label: str, paths: list[pathlib.Path], role: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if role == "derived_rebuild":
            total_rows = derived_total_rows(label)
            return derived_retention_impact(label), derived_delete_all_impact(label, total_rows=total_rows)
        if role == "pending_turn_state":
            total_rows = len(pending_turn_state_targets)
            return pending_turn_state_retention_impact(), file_delete_all_impact(label, total_rows=total_rows)
        if role == "source_prune":
            return source_retention_impact(paths), source_delete_all_impact(label, paths)
        return impact_payload(include_targets=include_targets), file_delete_all_impact(label)

    def detail_items_kind(role: str) -> str:
        if role == "derived_rebuild":
            return "derived_outputs"
        if role == "source_prune":
            return "source_files"
        if role in {"pending_turn_state", "state"}:
            return "file_targets"
        return "empty"

    def action_file_counts(role: str, impact: dict[str, Any], summary: dict[str, Any]) -> dict[str, int]:
        operation = str(summary.get("operation") or "-")
        affected_files = max(0, int(impact.get("affected_files") or 0))
        counts = {"Delete": 0, "Rewrite": 0, "Rebuild": 0}
        if operation == "-" or affected_files <= 0:
            return counts
        if operation == "Rebuild":
            counts["Rebuild"] = affected_files
            return counts
        if role == "source_prune":
            delete_files = max(0, int(impact.get("delete_files") or 0))
            rewrite_files = max(0, int(impact.get("rewrite_files") or 0))
            counts["Delete"] = min(delete_files, affected_files)
            counts["Rewrite"] = min(rewrite_files, max(0, affected_files - counts["Delete"]))
            assigned = counts["Delete"] + counts["Rewrite"]
            if assigned <= 0 and operation in {"Delete", "Rewrite"}:
                counts[operation] = affected_files
            return counts
        if operation == "Delete":
            counts["Delete"] = affected_files
        elif operation == "Rewrite":
            counts["Rewrite"] = affected_files
        return counts

    def display_payload(role: str, impact: dict[str, Any], summary: dict[str, Any], *, total_size: int) -> dict[str, Any]:
        detail_kind = detail_items_kind(role)
        affected_files = int(impact.get("affected_files") or 0)
        if detail_kind == "empty":
            affected_files = 0
        payload = {
            "action": str(summary.get("operation") or "-"),
            "total_size": max(0, int(total_size or 0)),
            "total_rows": max(0, int(impact.get("total_rows") or 0)),
            "delete_size": max(0, int(summary.get("delete_size") if summary.get("delete_size") is not None else impact.get("delete_size") or 0)),
            "affected_rows": max(0, int(impact.get("affected_rows") or 0)),
            "affected_files": max(0, affected_files),
            "detail_title": "Affected Files",
            "detail_items_kind": detail_kind,
            "scope_label": str(summary.get("scope_label") or "0 files"),
            "scope_count": max(0, int(summary.get("scope_count") or 0)),
            "scope_unit": str(summary.get("scope_unit") or "none"),
            "action_file_counts": action_file_counts(role, impact, summary),
        }
        if include_targets and impact.get("targets"):
            payload["targets"] = list(impact.get("targets") or [])
        if include_targets and impact.get("items"):
            payload["items"] = [dict(item) for item in impact.get("items") or []]
        return payload

    rows = []
    for row_definition in cleanup_row_definitions():
        label = str(row_definition["label"])
        group_info = cleanup_group_for_label(label)
        if row_group_id is not None and group_info.get("group_id") != row_group_id:
            continue
        paths = paths_by_label[label]
        compactable = compactable_by_label.get(label, 0)
        size = group_size(paths)
        deletable_bytes = 0
        role = str(group_info["role"])
        effect = str(group_info["retention_effect"])
        public_group_info = {key: value for key, value in group_info.items() if key not in {"label", "role", "retention_effect"}}
        retention_impact, delete_all_impact = impacts_for_row(label, paths, role)
        retention_summary = cleanup_impact_summary(role, retention_impact)
        delete_all_summary = cleanup_impact_summary(role, delete_all_impact)
        if size > 0:
            status = "protected"
        else:
            status = "missing"
        rows.append(
            {
                **public_group_info,
                "label": label,
                "path": ", ".join(str(path) for path in paths[:5]) + (f", ... ({len(paths)} files)" if len(paths) > 5 else ""),
                "bytes": size,
                "compactable_bytes": compactable,
                "deletable_bytes": deletable_bytes,
                "status": status,
                "retention_effect": effect,
                "display": display_payload(role, retention_impact, retention_summary, total_size=size),
                "delete_all_display": display_payload(role, delete_all_impact, delete_all_summary, total_size=size),
            }
        )
    archive_bytes = next((row["bytes"] for row in rows if row["label"] == "Archived Raw Logs"), 0)
    active_raw_bytes = group_size([current_dir])
    compactable_bytes = 0
    deletable_bytes = sum(row["deletable_bytes"] for row in rows)
    selected_retention_payload = dict(selected_retention)
    if not include_targets:
        selected_retention_payload.pop("files", None)
    return {
        "summary": {
            "service_bytes": sum(row["bytes"] for row in rows),
            "active_raw_bytes": active_raw_bytes,
            "compactable_bytes": compactable_bytes,
            "deletable_bytes": deletable_bytes,
            "archive_bytes": archive_bytes,
            "last_compacted_at_unix": int_metadata(metadata, "last_compacted_at_unix"),
        },
        "retention": {
            "now_unix": now_unix,
            "selected": selected_retention_payload,
            "job": cleanup_job,
        },
        "rows": rows,
    }


def cleanup_impact_summary(role: str, impact: dict[str, Any]) -> dict[str, Any]:
    rows = int(impact.get("affected_rows") or 0)
    files = int(impact.get("affected_files") or 0)
    delete_size = int(impact.get("delete_size") or 0)
    if role == "derived_rebuild":
        if rows > 0 or delete_size > 0:
            operation = "Rebuild"
            scope_unit = "row"
            scope_count = rows
            scope_label = f"{rows:,} row{'s' if rows != 1 else ''} affected"
        else:
            operation = "-"
            scope_unit = "none"
            scope_count = 0
            scope_label = "0 files"
    elif role == "source_prune" and rows > 0:
        rewrite_files = int(impact.get("rewrite_files") or 0)
        if rewrite_files > 0:
            operation = "Rewrite"
        else:
            operation = "Delete"
        scope_unit = "row"
        scope_count = rows
        scope_label = f"{rows:,} row{'s' if rows != 1 else ''} · {files:,} file{'s' if files != 1 else ''}"
    elif files > 0 or delete_size > 0:
        operation = "Delete"
        scope_unit = "file"
        scope_count = files
        scope_label = f"{files:,} file{'s' if files != 1 else ''}"
    else:
        operation = "-"
        scope_unit = "none"
        scope_count = 0
        scope_label = "0 files"
    return {
        "operation": operation,
        "delete_size": max(0, delete_size),
        "scope_label": scope_label,
        "scope_count": max(0, scope_count),
        "scope_unit": scope_unit,
    }


def cleanup_detail_payload(
    token_usage_root: pathlib.Path,
    db_path: pathlib.Path | str,
    group_id: str,
    base_dir: pathlib.Path | str | None = None,
    retention_cutoff_unix: float | None = None,
    preview_signature: str | None = None,
) -> dict[str, Any]:
    base = pathlib.Path(base_dir).expanduser() if base_dir is not None else pathlib.Path(token_usage_root).expanduser()
    db = pathlib.Path(db_path).expanduser()
    cutoff_unix = float(retention_cutoff_unix) if retention_cutoff_unix is not None else default_retention_cutoff_unix(time.time())
    if preview_signature is not None and retention_preview_signature(base, cutoff_unix) != str(preview_signature):
        return {"error": "cleanup_preview_stale"}

    row_definition = next((row for row in cleanup_row_definitions() if str(row.get("group_id") or "") == group_id), None)
    if row_definition is None:
        return {"error": "cleanup_row_not_found", "message": f"Unknown cleanup row group: {group_id}"}
    label = str(row_definition["label"])
    group_info = cleanup_group_for_label(label)
    role = str(group_info["role"])

    archive_dir = base / "raw" / "archive"
    current_dir = base / "raw" / "current"
    normalized_prompt = base / "normalized" / "prompt-usage.normalized.jsonl"
    normalized_prompt_archive = base / "normalized" / "prompt-usage.normalized.jsonl.gz"
    normalize_state = base / "normalized" / "normalize-state.json"
    state_dir = base / "state"
    retention_reset_targets = {
        "Normalized Outputs": [normalized_prompt, normalize_state],
        "Analytics Database": [db],
    }

    def state_file_targets() -> list[pathlib.Path]:
        pending_targets = pending_turn_state_paths(state_dir)
        pending_keys = {path.resolve(strict=False) for path in pending_targets}
        try:
            state_children = [path for path in sorted(state_dir.iterdir(), key=lambda item: item.name) if not path.name.endswith(".lock")]
        except FileNotFoundError:
            state_children = []
        return [path for path in state_children if path.resolve(strict=False) not in pending_keys] + sorted(base.glob("hook-probe-events.jsonl"))

    def paths_for_label(row_label: str) -> list[pathlib.Path]:
        if row_label == "Normalized Outputs":
            return [normalized_prompt, normalized_prompt_archive, normalize_state]
        if row_label == "Analytics Database":
            return [db]
        if row_label == "Archived Raw Logs":
            return [archive_dir]
        if row_label == "Raw Current Segments":
            return [current_dir]
        if row_label == "Pending Turn State":
            return pending_turn_state_paths(state_dir)
        if row_label == "State Files":
            return state_file_targets()
        return []

    paths = paths_for_label(label)

    def empty_detail_display() -> dict[str, Any]:
        return {"targets": [], "targets_truncated": 0}

    def source_files_for_paths(selected_retention: dict[str, Any], source_paths: list[pathlib.Path], *, delete_all: bool) -> list[dict[str, Any]]:
        path_texts = [str(path) for path in source_paths]
        files = [
            dict(file)
            for file in selected_retention.get("files", [])
            if any(str(file.get("path") or "") == path or str(file.get("path") or "").startswith(path + "/") for path in path_texts)
        ]
        if delete_all:
            return [file for file in files if int(file.get("scanned_rows") or 0) > 0 or int(file.get("source_size") or 0) > 0]
        return [file for file in files if file.get("affected")]

    def detail_display_from_files(files: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "targets": [str(file.get("path") or "") for file in files if file.get("path")],
            "targets_truncated": 0,
        }
        if files:
            payload["items"] = [dict(file) for file in files]
        return payload

    display = empty_detail_display()
    delete_all_display = empty_detail_display()
    if role == "source_prune":
        selected_retention = retention_preview(base, cutoff_unix)
        display = detail_display_from_files(source_files_for_paths(selected_retention, paths, delete_all=False))
        delete_all_display = detail_display_from_files(source_files_for_paths(selected_retention, paths, delete_all=True))
    elif role == "derived_rebuild":
        selected_retention = retention_preview(base, cutoff_unix)
        affected_rows = sum(int(file.get("deletable_rows") or 0) for file in selected_retention.get("files", []))
        affected_file_paths = existing_target_paths(retention_reset_targets.get(label, [])) if affected_rows > 0 else []
        delete_all_file_paths = existing_target_paths(paths)
        display = {"targets": [str(path) for path in affected_file_paths], "targets_truncated": 0}
        delete_all_display = {"targets": [str(path) for path in delete_all_file_paths], "targets_truncated": 0}
    elif role == "pending_turn_state":
        plan = plan_pending_turn_state_for_retention(base, cutoff_unix)
        affected_file_paths = [pathlib.Path(str(path)) for path in plan.get("targets") or []]
        delete_all_file_paths = existing_target_paths(paths)
        display = {"targets": [str(path) for path in affected_file_paths], "targets_truncated": 0}
        delete_all_display = {"targets": [str(path) for path in delete_all_file_paths], "targets_truncated": 0}
    public_group_info = {key: value for key, value in group_info.items() if key not in {"label", "role", "retention_effect"}}
    return {
        "row": {
            **public_group_info,
            "label": label,
            "path": ", ".join(str(path) for path in paths[:5]) + (f", ... ({len(paths)} files)" if len(paths) > 5 else ""),
            "retention_effect": str(group_info["retention_effect"]),
            "display": display,
            "delete_all_display": delete_all_display,
        }
    }
