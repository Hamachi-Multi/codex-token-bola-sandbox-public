"""Cleanup payload and deletion helpers for the Codex Token Bola dashboard.

This module is the public compatibility facade. Implementation lives in the
cleanup submodules, while existing imports and tests can keep using
``dashboard_cleanup`` directly.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_lock

import dashboard_cleanup_recovery as _recovery
import dashboard_cleanup_payload as _payload
import dashboard_cleanup_retention as _retention
import dashboard_retention_index as _index
import dashboard_retention_preview as _preview
from raw_segments_common import ManifestError

_SERVICE_LOCK_CONTRACT = service_lock.acquire_service_lock


class CleanupDependencies:
    __slots__ = ("index", "payload", "preview", "recovery", "retention")

    def __init__(self, *, index: Any, payload: Any, preview: Any, recovery: Any, retention: Any) -> None:
        self.index = index
        self.retention = retention
        self.payload = payload
        self.preview = preview
        self.recovery = recovery


DEFAULT_CLEANUP_DEPENDENCIES = CleanupDependencies(
    index=_index,
    retention=_retention,
    payload=_payload,
    preview=_preview,
    recovery=_recovery,
)

RetentionPreviewStale = _preview.RetentionPreviewStale
cleanup_retention_job_path = _recovery.cleanup_retention_job_path
clear_cleanup_retention_job = _recovery.clear_cleanup_retention_job
complete_retention_derived_rebuild = _recovery.complete_retention_derived_rebuild
read_cleanup_retention_job = _recovery.read_cleanup_retention_job
read_cleanup_retention_job_model = _recovery.read_cleanup_retention_job_model
reset_derived_outputs = _retention.reset_derived_outputs
retention_preview_signature = _preview.retention_preview_signature
write_cleanup_retention_job = _recovery.write_cleanup_retention_job
discard_delete_logs_older_than_plan = _retention.discard_delete_logs_older_than_plan
ensure_service_owned_output = _retention.ensure_service_owned_output
clear_retention_preview_cache = _preview.clear_retention_preview_cache


def rebuild_retention_index(token_usage_root: pathlib.Path | str) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.index.rebuild_retention_index(token_usage_root)


def refresh_retention_index_for_current_sources(token_usage_root: pathlib.Path | str) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.index.refresh_retention_index_for_current_sources(token_usage_root)


def retention_preview(token_usage_root: pathlib.Path | str, cutoff_unix: float, *, refresh_index: bool = True) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.preview.retention_preview(token_usage_root, cutoff_unix, refresh_index=refresh_index)


def preflight_delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.preflight_delete_logs_older_than(token_usage_root, cutoff_unix)


def plan_delete_logs_older_than(
    token_usage_root: pathlib.Path | str,
    cutoff_unix: float,
    *,
    expected_preview_signature: str | None = None,
    operation_job_id: str | None = None,
) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.plan_delete_logs_older_than(
        token_usage_root,
        cutoff_unix,
        expected_preview_signature=expected_preview_signature,
        operation_job_id=operation_job_id,
    )


def validate_delete_logs_older_than_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.validate_delete_logs_older_than_plan(plan)


def apply_delete_logs_older_than_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.apply_delete_logs_older_than_plan(plan)


def delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    preflight_delete_logs_older_than(token_usage_root, cutoff_unix)
    return apply_delete_logs_older_than_plan(plan_delete_logs_older_than(token_usage_root, cutoff_unix))


def cleanup_payload(
    token_usage_root: pathlib.Path,
    db_path: pathlib.Path | str,
    base_dir: pathlib.Path | str | None = None,
    retention_cutoff_unix: float | None = None,
    *,
    refresh_retention_index: bool = True,
) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.payload.cleanup_payload(
        token_usage_root,
        db_path,
        base_dir,
        retention_cutoff_unix,
        refresh_retention_index=refresh_retention_index,
    )


def cleanup_detail_payload(
    token_usage_root: pathlib.Path,
    db_path: pathlib.Path | str,
    group_id: str,
    base_dir: pathlib.Path | str | None = None,
    retention_cutoff_unix: float | None = None,
    preview_signature: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.payload.cleanup_detail_payload(
        token_usage_root, db_path, group_id, base_dir, retention_cutoff_unix, preview_signature, page, page_size
    )


def delete_all_logs(token_usage_root: pathlib.Path | str, db_path: pathlib.Path | str | None = None) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.payload.delete_all_logs(token_usage_root, db_path)


__all__ = [
    "ManifestError",
    "RetentionPreviewStale",
    "apply_delete_logs_older_than_plan",
    "cleanup_detail_payload",
    "cleanup_payload",
    "cleanup_retention_job_path",
    "clear_cleanup_retention_job",
    "clear_retention_preview_cache",
    "complete_retention_derived_rebuild",
    "delete_all_logs",
    "delete_logs_older_than",
    "discard_delete_logs_older_than_plan",
    "ensure_service_owned_output",
    "plan_delete_logs_older_than",
    "preflight_delete_logs_older_than",
    "read_cleanup_retention_job",
    "rebuild_retention_index",
    "refresh_retention_index_for_current_sources",
    "reset_derived_outputs",
    "retention_preview",
    "retention_preview_signature",
    "validate_delete_logs_older_than_plan",
    "write_cleanup_retention_job",
]
