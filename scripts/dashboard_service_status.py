"""Read-only service activity endpoint for the local dashboard."""

from __future__ import annotations

import math
from typing import Any

import dashboard_operation_state
import progress_control
import service_lock


def _progress_value(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(100.0, number))


def _running_payload(
    operation: str,
    *,
    operation_id: str | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    progress = progress or {}
    overall_progress = _progress_value(progress.get("overall_progress"))
    return {
        "running": True,
        "operation": operation,
        "status": "running",
        "progress_available": overall_progress is not None,
        "phase": str(progress.get("phase") or ""),
        "checkpoint": str(progress.get("checkpoint") or ""),
        "overall_progress": overall_progress,
        "operation_id": operation_id,
    }


def service_status_payload(*, manager, output_dir) -> dict[str, Any]:
    active = manager.active_snapshot()
    if active is not None:
        progress_file = active.get("progress_file")
        progress = progress_control.read_progress(progress_file) if progress_file is not None and progress_file.is_file() else {}
        return _running_payload(
            str(active.get("kind") or "analysis"),
            operation_id=str(active.get("operation_id") or "") or None,
            progress=progress,
        )

    lock_path = service_lock.default_lock_path(output_dir=output_dir)
    inspected = service_lock.inspect_service_lock(lock_path)
    if bool(inspected.get("held")):
        return _running_payload(dashboard_operation_state.lock_operation(lock_path))

    return {
        "running": False,
        "operation": None,
        "status": "idle",
        "progress_available": False,
        "phase": "",
        "checkpoint": "",
        "overall_progress": None,
        "operation_id": None,
    }


class DashboardServiceStatusApiMixin:
    def handle_service_status(self) -> None:
        self.send_json(
            service_status_payload(
                manager=self.dashboard_operation_manager(),
                output_dir=self.dashboard_output_dir(),
            )
        )
