"""Dashboard server lifetime ownership and runtime-path handoff."""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import threading
import time
import uuid

import dashboard_operation_state
import service_paths


class DashboardServerBusy(RuntimeError):
    def __init__(self, path: pathlib.Path, owner: dict[str, object] | None = None) -> None:
        super().__init__(f"dashboard server already owns output directory: {path.parent.parent}")
        self.path = path
        self.owner = owner


class DashboardOutputConflict(RuntimeError):
    def __init__(self, path: pathlib.Path) -> None:
        super().__init__(f"dashboard output directory is owned by another server: {path.parent.parent}")
        self.path = path


class DashboardPathTransitionBusy(RuntimeError):
    pass


class DashboardServerLease:
    def __init__(self, path: pathlib.Path, descriptor: int, server_id: str) -> None:
        self.path = path
        self.descriptor = descriptor
        self.server_id = server_id
        self._closed = False

    @classmethod
    def acquire(cls, output_dir: pathlib.Path | str, server_id: str) -> "DashboardServerLease":
        root = pathlib.Path(output_dir).expanduser().resolve(strict=False)
        path = root / "state" / "dashboard-server.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                owner = None
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    parsed = json.loads(os.read(descriptor, 64 * 1024).decode("utf-8"))
                    owner = parsed if isinstance(parsed, dict) else None
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
                raise DashboardServerBusy(path, owner) from exc
            payload = {
                "pid": os.getpid(),
                "server_id": server_id,
                "output_dir": str(root),
                "started_at_unix": time.time(),
            }
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            os.fsync(descriptor)
            return cls(path, descriptor, server_id)
        except Exception:
            os.close(descriptor)
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


class DashboardRuntimeManager:
    def __init__(
        self,
        runtime_paths: service_paths.RuntimePaths,
        *,
        dynamic: bool,
        operation_manager: dashboard_operation_state.DashboardOperationManager,
    ) -> None:
        self.server_id = str(uuid.uuid4())
        self.dynamic = dynamic
        self.operation_manager = operation_manager
        self._lock = threading.RLock()
        self._runtime_paths = runtime_paths
        self._lease = DashboardServerLease.acquire(runtime_paths.output_dir, self.server_id)
        dashboard_operation_state.sweep_transient_progress_files(runtime_paths.output_dir)

    def snapshot(self) -> service_paths.RuntimePaths:
        if not self.dynamic:
            with self._lock:
                return self._runtime_paths
        with service_paths.acquire_path_lock():
            resolved = service_paths.resolve_runtime_paths()
        with self._lock:
            current = self._runtime_paths
            if resolved == current:
                return current
            if self.operation_manager.has_active_operation():
                raise DashboardPathTransitionBusy("runtime paths changed while a dashboard operation is active")
            if resolved.output_dir == current.output_dir:
                self._runtime_paths = resolved
                return resolved
            try:
                next_lease = DashboardServerLease.acquire(resolved.output_dir, self.server_id)
            except DashboardServerBusy as exc:
                raise DashboardOutputConflict(exc.path) from exc
            dashboard_operation_state.sweep_transient_progress_files(resolved.output_dir)
            previous_lease = self._lease
            self._runtime_paths = resolved
            self._lease = next_lease
            previous_lease.close()
            return resolved

    def lifetime_lock_fd(self) -> int:
        with self._lock:
            return self._lease.descriptor

    def close(self) -> None:
        with self._lock:
            self._lease.close()
