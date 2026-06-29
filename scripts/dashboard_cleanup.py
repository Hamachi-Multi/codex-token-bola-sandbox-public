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

import dashboard_cleanup_payload as _payload
import dashboard_cleanup_retention as _retention

from dashboard_cleanup_common import *  # noqa: F401,F403
from dashboard_cleanup_recovery import *  # noqa: F401,F403
from dashboard_cleanup_retention import *  # noqa: F401,F403
from dashboard_cleanup_payload import *  # noqa: F401,F403

_SERVICE_LOCK_CONTRACT = service_lock.acquire_service_lock


class CleanupDependencies:
    __slots__ = ("retention", "payload")

    def __init__(self, *, retention: Any, payload: Any) -> None:
        self.retention = retention
        self.payload = payload


DEFAULT_CLEANUP_DEPENDENCIES = CleanupDependencies(
    retention=_retention,
    payload=_payload,
)


def rebuild_retention_index(token_usage_root: pathlib.Path | str) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.rebuild_retention_index(token_usage_root)


def refresh_retention_index_for_current_sources(token_usage_root: pathlib.Path | str) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.refresh_retention_index_for_current_sources(token_usage_root)


def retention_preview(token_usage_root: pathlib.Path | str, cutoff_unix: float, *, refresh_index: bool = True) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.retention_preview(token_usage_root, cutoff_unix, refresh_index=refresh_index)


def preflight_delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.preflight_delete_logs_older_than(token_usage_root, cutoff_unix)


def plan_delete_logs_older_than(token_usage_root: pathlib.Path | str, cutoff_unix: float) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.retention.plan_delete_logs_older_than(token_usage_root, cutoff_unix)


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
) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.payload.cleanup_detail_payload(token_usage_root, db_path, group_id, base_dir, retention_cutoff_unix, preview_signature)


def delete_all_logs(token_usage_root: pathlib.Path | str, db_path: pathlib.Path | str | None = None) -> dict[str, Any]:
    return DEFAULT_CLEANUP_DEPENDENCIES.payload.delete_all_logs(token_usage_root, db_path)


__all__ = [name for name in globals() if not name.startswith("_")]
