"""Thread-safe Dashboard operation ownership and lifecycle state."""

from __future__ import annotations

import json
import pathlib
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any


TRANSIENT_PROGRESS_PATTERNS = (
    "cleanup-progress.*.json",
    "rebuild-progress.*.json",
    "rebuild-cancel.*.json",
)


class OperationBusy(RuntimeError):
    pass


class ServerShuttingDown(RuntimeError):
    pass


class AnalysisNotRunning(RuntimeError):
    pass


class OperationIdMismatch(RuntimeError):
    pass


@dataclass
class OperationRecord:
    operation_id: str
    kind: str
    output_dir: pathlib.Path
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    cancel_file: pathlib.Path | None = None
    progress_file: pathlib.Path | None = None
    process: Any | None = None


class OperationLease:
    def __init__(self, manager: "DashboardOperationManager", operation_id: str) -> None:
        self.manager = manager
        self.operation_id = operation_id
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.manager.finish(self.operation_id)

    def __enter__(self) -> "OperationLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class DashboardOperationManager:
    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._active: OperationRecord | None = None
        self._shutting_down = False

    @property
    def shutting_down(self) -> bool:
        with self._condition:
            return self._shutting_down

    def begin(
        self,
        kind: str,
        output_dir: pathlib.Path | str,
        *,
        operation_id: str | None = None,
    ) -> OperationLease:
        if kind not in {"analysis", "cleanup", "cost_recalculation"}:
            raise ValueError(f"unsupported dashboard operation: {kind}")
        resolved_id = operation_id or str(uuid.uuid4())
        with self._condition:
            if self._shutting_down:
                raise ServerShuttingDown("dashboard server is shutting down")
            if self._active is not None:
                raise OperationBusy("dashboard operation is already active")
            self._active = OperationRecord(
                operation_id=resolved_id,
                kind=kind,
                output_dir=pathlib.Path(output_dir).expanduser(),
            )
            return OperationLease(self, resolved_id)

    def finish(self, operation_id: str) -> bool:
        with self._condition:
            if self._active is None or self._active.operation_id != operation_id:
                return False
            self._active = None
            self._condition.notify_all()
            return True

    def active_record(self) -> OperationRecord | None:
        with self._condition:
            return self._active

    def active_snapshot(self) -> dict[str, Any] | None:
        """Return immutable fields needed by read-only status endpoints."""
        with self._condition:
            active = self._active
            if active is None:
                return None
            return {
                "operation_id": active.operation_id,
                "kind": active.kind,
                "progress_file": active.progress_file,
            }

    def has_active_operation(self) -> bool:
        with self._condition:
            return self._active is not None

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._active is None, timeout=timeout)

    def set_files(
        self,
        operation_id: str,
        *,
        cancel_file: pathlib.Path | None = None,
        progress_file: pathlib.Path | None = None,
    ) -> bool:
        with self._condition:
            if self._active is None or self._active.operation_id != operation_id:
                return False
            if cancel_file is not None:
                self._active.cancel_file = pathlib.Path(cancel_file)
            if progress_file is not None:
                self._active.progress_file = pathlib.Path(progress_file)
            return True

    def attach_process(self, operation_id: str, process: Any) -> bool:
        with self._condition:
            if self._active is None or self._active.operation_id != operation_id:
                return False
            self._active.process = process
            return True

    def detach_process(self, operation_id: str, process: Any) -> bool:
        with self._condition:
            if self._active is None or self._active.operation_id != operation_id or self._active.process is not process:
                return False
            self._active.process = None
            return True

    def request_analysis_cancel(self, operation_id: str) -> OperationRecord:
        with self._condition:
            active = self._active
            if active is None or active.kind != "analysis":
                raise AnalysisNotRunning("analysis is not running")
            if active.operation_id != operation_id:
                raise OperationIdMismatch("operation id does not own the active analysis")
            active.cancel_requested.set()
            return active

    def begin_shutdown(self) -> OperationRecord | None:
        with self._condition:
            self._shutting_down = True
            active = self._active
            if active is not None and active.kind == "analysis":
                active.cancel_requested.set()
            return active

    def busy_payload(self) -> dict[str, Any]:
        with self._condition:
            active = self._active
            operation = active.kind if active is not None else "analysis"
            progress_available = bool(active and active.kind == "analysis" and active.progress_file is not None)
            payload: dict[str, Any] = {
                "error": "analysis_or_cleanup_running",
                "operation": operation,
                "progress_available": progress_available,
            }
            if active is not None:
                payload["operation_id"] = active.operation_id
            return payload

    def progress_snapshot(self, kind: str) -> tuple[OperationRecord | None, pathlib.Path | None, bool]:
        with self._condition:
            active = self._active if self._active is not None and self._active.kind == kind else None
            return active, active.progress_file if active else None, active is not None


def sweep_transient_progress_files(token_usage_root: pathlib.Path | str) -> list[dict[str, Any]]:
    state_dir = pathlib.Path(token_usage_root).expanduser() / "state"
    removed: list[dict[str, Any]] = []
    for pattern in TRANSIENT_PROGRESS_PATTERNS:
        for path in sorted(state_dir.glob(pattern), key=lambda item: item.name):
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            removed.append({"name": path.name, "path": str(path), "deleted_bytes": size})
    return removed


def lock_operation(lock_path: pathlib.Path | str | None) -> str:
    if not lock_path:
        return "analysis"
    path = pathlib.Path(lock_path).expanduser()
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.loads(handle.read(4096))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return "analysis"
    reason = str(payload.get("reason") or "").lower() if isinstance(payload, dict) else ""
    if "cost-recalculation" in reason:
        return "cost_recalculation"
    if any(fragment in reason for fragment in ("cleanup", "compact", "delete", "retention")):
        return "cleanup"
    return "analysis"


def service_busy_payload(
    *,
    operation: str | None = None,
    progress_available: bool = False,
    lock_path: pathlib.Path | str | None = None,
) -> dict[str, Any]:
    resolved_operation = operation or lock_operation(lock_path)
    return {
        "error": "analysis_or_cleanup_running",
        "operation": resolved_operation if resolved_operation in {"analysis", "cleanup", "cost_recalculation"} else "analysis",
        "progress_available": bool(progress_available and resolved_operation == "analysis"),
        **({"lock_path": str(lock_path)} if lock_path else {}),
    }


# Compatibility manager for direct handler unit tests. Production servers attach
# their own manager instance to ThreadingHTTPServer.
DEFAULT_MANAGER = DashboardOperationManager()


def begin_exclusive_operation(operation: str) -> bool:
    try:
        DEFAULT_MANAGER.begin(operation, pathlib.Path("."))
    except (OperationBusy, ServerShuttingDown):
        return False
    return True


def end_exclusive_operation() -> None:
    active = DEFAULT_MANAGER.active_record()
    if active is not None:
        DEFAULT_MANAGER.finish(active.operation_id)


def active_operation_busy_payload() -> dict[str, Any]:
    return DEFAULT_MANAGER.busy_payload()
