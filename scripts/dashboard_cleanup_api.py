"""Cleanup request handlers for the local dashboard server."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dashboard_cleanup
import dashboard_managed_process
import dashboard_operation_state as operation_state
import progress_control
import raw_segments
import service_lock
import service_paths


class DashboardCleanupApiMixin:
    def begin_cleanup_operation(self):
        manager = self.dashboard_operation_manager()
        try:
            return manager.begin("cleanup", self.dashboard_output_dir())
        except operation_state.ServerShuttingDown:
            self.send_json({"error": "server_shutting_down"}, 503)
        except operation_state.OperationBusy:
            self.send_json(manager.busy_payload(), 409)
        return None

    def _dashboard_db_path(self) -> pathlib.Path:
        resolver = getattr(self, "dashboard_db_path", None)
        if callable(resolver):
            return pathlib.Path(resolver())
        return pathlib.Path(self.server.db_path)

    def run_managed_cleanup_command(self, cmd: list[str], *, env: dict[str, str]) -> dict[str, Any]:
        manager = self.dashboard_operation_manager()
        active = manager.active_record()
        if active is None or active.kind != "cleanup":
            raise RuntimeError("managed cleanup command requires an active cleanup operation")
        script = self.dashboard_script_dir() / "bola.py"
        tmp_dir = service_paths.ensure_output_tmp_dir(self.dashboard_output_dir())
        with tempfile.TemporaryFile("w+", encoding="utf-8", dir=tmp_dir) as stdout_file, tempfile.TemporaryFile(
            "w+", encoding="utf-8", dir=tmp_dir
        ) as stderr_file:
            process = dashboard_managed_process.ManagedProcess.start(
                cmd,
                kind="cleanup",
                cwd=str(script.parent),
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
                lifetime_lock_fd=getattr(self, "dashboard_lifetime_lock_fd", lambda: None)(),
            )
            manager.attach_process(active.operation_id, process)
            try:
                returncode = process.wait()
            finally:
                manager.detach_process(active.operation_id, process)
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read().strip()
                stderr = stderr_file.read().strip()
        return {
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "metadata": self.parse_last_json(stdout),
        }

    def run_compact_command(self, output: pathlib.Path, min_bytes: int):
        script = self.dashboard_script_dir() / "bola.py"
        cmd = [
            sys.executable,
            str(script),
            "compact",
            "--codex-dir",
            str(self.dashboard_codex_dir()),
            "--output-dir",
            str(self.dashboard_output_dir()),
        ]
        return self.run_managed_cleanup_command(cmd, env=service_lock.scrub_lock_env(os.environ.copy()))

    def begin_cleanup_progress(self, *, phase: str, phase_index: int, checkpoint: str, phase_progress: float = 0.0) -> pathlib.Path:
        manager = self.dashboard_operation_manager()
        active = manager.active_record()
        if active is None or active.kind != "cleanup":
            raise RuntimeError("cleanup progress requires an active cleanup operation")
        progress_file = self.dashboard_output_dir() / "state" / f"cleanup-progress.{active.operation_id}.json"
        progress_file.unlink(missing_ok=True)
        manager.set_files(active.operation_id, progress_file=progress_file)
        progress_control.write_progress_to_path(
            progress_file,
            operation_id=active.operation_id,
            status="running",
            phase=phase,
            phase_index=phase_index,
            phase_count=4,
            checkpoint=checkpoint,
            phase_progress=phase_progress,
        )
        return progress_file

    def write_cleanup_progress(self, progress_file: pathlib.Path | None, **kwargs: Any) -> None:
        if progress_file is None:
            return
        active = self.dashboard_operation_manager().active_record()
        if active is not None:
            kwargs.setdefault("operation_id", active.operation_id)
        progress_control.write_progress_to_path(progress_file, phase_count=4, **kwargs)

    def close_cleanup_progress(self, progress_file: pathlib.Path | None) -> None:
        if progress_file is None:
            return
        progress_file.unlink(missing_ok=True)
        progress_control.forget_progress(progress_file)

    def handle_cleanup_progress(self):
        active, progress_file, cleanup_running = self.dashboard_operation_manager().progress_snapshot("cleanup")
        payload = progress_control.read_progress(progress_file)
        if active is not None:
            payload["operation_id"] = active.operation_id
        payload["cleanup_running"] = cleanup_running
        self.send_json(payload)

    def run_pipeline_command(self, *, incremental: bool) -> dict[str, Any]:
        script = self.dashboard_script_dir() / "bola.py"
        cmd = [
            sys.executable,
            str(script),
            "pipeline",
            "--codex-dir",
            str(self.dashboard_codex_dir()),
            "--output-dir",
            str(self.dashboard_output_dir()),
        ]
        if incremental:
            cmd.append("--incremental")
        return self.run_managed_cleanup_command(cmd, env=service_lock.scrub_lock_env(os.environ.copy()))

    def run_retention_prune_command(self, cutoff_unix: float, preview_signature: str) -> dict[str, Any]:
        script = self.dashboard_script_dir() / "bola.py"
        cmd = [
            sys.executable,
            str(script),
            "retention-prune",
            "--codex-dir",
            str(self.dashboard_codex_dir()),
            "--output-dir",
            str(self.dashboard_output_dir()),
            "--cutoff",
            str(float(cutoff_unix)),
            "--preview-signature",
            preview_signature,
        ]
        env = service_lock.scrub_lock_env(os.environ.copy())
        _active, progress_file, _running = self.dashboard_operation_manager().progress_snapshot("cleanup")
        if progress_file is not None:
            env[progress_control.PROGRESS_ENV] = str(progress_file)
        return self.run_managed_cleanup_command(cmd, env=env)

    @staticmethod
    def cleanup_option_value(value: Any = None) -> Any:
        return value[0] if isinstance(value, list) and value else value

    def required_cleanup_timezone(self, value: Any = None) -> tuple[str, ZoneInfo]:
        raw = self.cleanup_option_value(value)
        if not isinstance(raw, str) or not raw:
            raise ValueError("cutoff_timezone_required")
        try:
            return raw, ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("cutoff_timezone_invalid") from exc

    def cleanup_selection(self, value: Any, timezone_value: Any, *, required_date: bool) -> dict[str, Any]:
        raw = self.cleanup_option_value(value)
        if required_date and (not isinstance(raw, str) or not raw):
            raise ValueError("cutoff_date_required")
        selected_date: date | None = None
        if not isinstance(raw, str) or not raw:
            pass
        else:
            if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
                raise ValueError("cutoff_date_invalid")
            try:
                selected_date = date.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError("cutoff_date_invalid") from exc
            if selected_date.isoformat() != raw:
                raise ValueError("cutoff_date_invalid")
        timezone_name, timezone_info = self.required_cleanup_timezone(timezone_value)
        if selected_date is None:
            selected_date = datetime.now(timezone_info).date() - timedelta(days=7)
        try:
            exclusive_date = selected_date + timedelta(days=1)
        except OverflowError as exc:
            raise ValueError("cutoff_date_invalid") from exc
        cutoff = datetime.combine(exclusive_date, datetime_time.min, tzinfo=timezone_info)
        return {
            "cutoff_date": selected_date.isoformat(),
            "timezone": timezone_name,
            "cutoff_unix": cutoff.timestamp(),
        }

    def required_cleanup_selection(self, value: Any = None, timezone_value: Any = None) -> dict[str, Any]:
        return self.cleanup_selection(value, timezone_value, required_date=True)

    def cleanup_preview_selection(self, value: Any = None, timezone_value: Any = None) -> dict[str, Any]:
        return self.cleanup_selection(value, timezone_value, required_date=False)

    @staticmethod
    def attach_cleanup_selection(payload: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
        retention = payload.get("retention")
        selected = retention.get("selected") if isinstance(retention, dict) else None
        if isinstance(selected, dict):
            selected.update(selection)
        return payload

    def handle_cleanup_compact(self):
        lease = self.begin_cleanup_operation()
        if lease is None:
            return
        started = time.monotonic()
        try:
            options = self.read_json_body()
            try:
                min_bytes = int(options.get("min_bytes", 1))
            except (TypeError, ValueError):
                min_bytes = 1
            min_bytes = max(1, min(min_bytes, 1024 * 1024 * 1024))
            output = self._dashboard_db_path().expanduser().resolve()
            result = self.run_compact_command(output, min_bytes)
            if result["returncode"] != 0:
                self.send_json(
                    {
                        "error": "cleanup_failed",
                        "returncode": result["returncode"],
                        "stderr": str(result["stderr"])[-4000:],
                        "stdout": str(result["stdout"])[-4000:],
                    },
                    500,
                )
                return
            dashboard_cleanup.refresh_retention_index_for_current_sources(self.dashboard_output_dir())
            self.send_json(
                {
                    "ok": True,
                    "compact": result["metadata"],
                    "cleanup": self.cleanup_payload(db_path=output),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            )
        finally:
            lease.close()

    def handle_cleanup_delete_all(self):
        lease = self.begin_cleanup_operation()
        if lease is None:
            return
        started = time.monotonic()
        progress_file: pathlib.Path | None = None
        try:
            options = self.read_json_body()
            if options.get("confirm_all_logs") is not True:
                self.send_json({"error": "delete_all_confirmation_required"}, 400)
                return
            output = self._dashboard_db_path().expanduser().resolve()
            progress_file = self.begin_cleanup_progress(
                phase="cleanup-delete",
                phase_index=1,
                checkpoint="delete-all",
                phase_progress=0.0,
            )
            try:
                result = self.delete_all_logs(self.dashboard_output_dir(), output)
            except service_lock.ServiceLockBusy as exc:
                self.write_cleanup_progress(progress_file, status="failed", phase="cleanup-delete", phase_index=1, checkpoint="busy", phase_progress=0.0)
                self.send_json(operation_state.service_busy_payload(lock_path=exc.path), 409)
                return
            failed = bool(result.get("delete_failed") or result.get("failed"))
            if failed:
                self.write_cleanup_progress(
                    progress_file, status="failed", phase="cleanup-delete", phase_index=1, checkpoint="partial-failure", phase_progress=1.0
                )
            else:
                self.write_cleanup_progress(
                    progress_file, status="running", phase="cleanup-refresh", phase_index=3, checkpoint="refresh-preview", phase_progress=0.2
                )
            cleanup_payload = self.cleanup_payload(db_path=output)
            if not failed:
                self.write_cleanup_progress(
                    progress_file, status="completed", phase="cleanup-refresh", phase_index=3, checkpoint="completed", phase_progress=1.0
                )
            self.send_json(
                {
                    "ok": not failed,
                    **({"error": "cleanup_delete_failed"} if failed else {}),
                    **result,
                    "cleanup": cleanup_payload,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
                500 if failed else 200,
            )
        finally:
            self.close_cleanup_progress(progress_file)
            lease.close()

    def handle_cleanup_retention(self):
        lease = self.begin_cleanup_operation()
        if lease is None:
            return
        started = time.monotonic()
        progress_file: pathlib.Path | None = None
        try:
            options = self.read_json_body()
            try:
                selection = self.required_cleanup_selection(options.get("cutoff_date"), options.get("timezone"))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            cutoff_date = str(selection["cutoff_date"])
            timezone_name = str(selection["timezone"])
            cutoff_unix = float(selection["cutoff_unix"])
            output = self._dashboard_db_path().expanduser().resolve()
            preview_signature = options.get("preview_signature")
            if not isinstance(preview_signature, str) or not preview_signature:
                self.send_json({"error": "cleanup_preview_signature_required"}, 400)
                return
            try:
                current_cleanup = self.cleanup_payload(db_path=output, retention_cutoff_unix=cutoff_unix)
            except raw_segments.ManifestError as exc:
                self.send_json({"error": "cleanup_preview_failed", "message": str(exc)}, 409)
                return
            self.attach_cleanup_selection(current_cleanup, selection)
            current_signature = str((((current_cleanup.get("retention") or {}).get("selected") or {}).get("preview_signature")) or "")
            if preview_signature != current_signature:
                self.send_json({"error": "cleanup_preview_stale"}, 409)
                return
            selected_retention = (current_cleanup.get("retention") or {}).get("selected") or {}
            selected_rows = int(selected_retention.get("deletable_rows") or 0)
            if selected_rows <= 0:
                scanned_rows = int(selected_retention.get("scanned_rows") or 0)
                self.send_json(
                    {
                        "ok": True,
                        "noop": True,
                        "cutoff_date": cutoff_date,
                        "timezone": timezone_name,
                        "retention": {
                            "cutoff_date": cutoff_date,
                            "timezone": timezone_name,
                            "cutoff_unix": cutoff_unix,
                            "scanned_rows": scanned_rows,
                            "deleted_rows": 0,
                            "kept_rows": scanned_rows,
                            "deleted_bytes": 0,
                            "deleted_state_files": 0,
                        },
                        "cleanup": current_cleanup,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
                )
                return
            progress_file = self.begin_cleanup_progress(
                phase="cleanup-prepare",
                phase_index=0,
                checkpoint="start-retention-prune",
                phase_progress=0.0,
            )
            prune_result = self.run_retention_prune_command(cutoff_unix, preview_signature)
            metadata = prune_result.get("metadata") if isinstance(prune_result.get("metadata"), dict) else {}
            degraded = prune_result["returncode"] == 1 and metadata.get("status") == "degraded"
            if prune_result["returncode"] != 0 and not degraded:
                if metadata.get("error") == "cleanup_preview_stale":
                    self.write_cleanup_progress(
                        progress_file, status="failed", phase="cleanup-prepare", phase_index=0, checkpoint="stale-preview", phase_progress=0.0
                    )
                    self.send_json({"error": "cleanup_preview_stale"}, 409)
                    return
                if metadata.get("error") == "analysis_or_cleanup_running":
                    self.write_cleanup_progress(progress_file, status="failed", phase="cleanup-prepare", phase_index=0, checkpoint="busy", phase_progress=0.0)
                    self.send_json(
                        {
                            **operation_state.service_busy_payload(lock_path=metadata.get("lock_path")),
                            "returncode": prune_result["returncode"],
                        },
                        409,
                    )
                    return
                self.write_cleanup_progress(
                    progress_file,
                    status="failed",
                    phase="cleanup-rebuild" if metadata.get("stage") in {"normalize", "build"} else "cleanup-delete",
                    phase_index=2 if metadata.get("stage") in {"normalize", "build"} else 1,
                    checkpoint=str(metadata.get("stage") or "failed"),
                    phase_progress=0.0,
                )
                self.send_json(
                    {
                        "error": "retention_prune_failed",
                        "returncode": prune_result["returncode"],
                        "partial_mutation": bool(metadata.get("partial_mutation")),
                        "recovery_required": bool(metadata.get("recovery_required")),
                        "derived_rebuild_required": bool(metadata.get("derived_rebuild_required")),
                        "physical_delete_pending": bool(metadata.get("physical_delete_pending")),
                        "pending_files": int(metadata.get("pending_files") or 0),
                        "stage": metadata.get("stage"),
                        "deleted_rows": metadata.get("deleted_rows", 0),
                        "stderr": str(prune_result["stderr"])[-4000:],
                        "stdout": str(prune_result["stdout"])[-4000:],
                    },
                    500,
                )
                return
            retention_result = metadata.get("delete") if isinstance(metadata.get("delete"), dict) else {"deleted_rows": metadata.get("deleted_rows", 0)}
            retention_result = {
                **retention_result,
                "cutoff_date": cutoff_date,
                "timezone": timezone_name,
                "cutoff_unix": cutoff_unix,
            }
            self.write_cleanup_progress(
                progress_file, status="running", phase="cleanup-refresh", phase_index=3, checkpoint="retention-index", phase_progress=0.2
            )
            dashboard_cleanup.refresh_retention_index_for_current_sources(self.dashboard_output_dir())
            self.write_cleanup_progress(
                progress_file, status="running", phase="cleanup-refresh", phase_index=3, checkpoint="preview-payload", phase_progress=0.75
            )
            cleanup_payload = self.cleanup_payload(db_path=output, retention_cutoff_unix=cutoff_unix)
            self.attach_cleanup_selection(cleanup_payload, selection)
            self.write_cleanup_progress(progress_file, status="completed", phase="cleanup-refresh", phase_index=3, checkpoint="completed", phase_progress=1.0)
            self.send_json(
                {
                    "ok": True,
                    "status": "degraded" if degraded else "healthy",
                    "data_health": "degraded" if degraded else "ok",
                    "quarantine": metadata.get("quarantine") if isinstance(metadata.get("quarantine"), dict) else {},
                    "cutoff_date": cutoff_date,
                    "timezone": timezone_name,
                    "retention": retention_result,
                    "cleanup": cleanup_payload,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            )
        finally:
            self.close_cleanup_progress(progress_file)
            lease.close()

    def cleanup_payload(
        self,
        db_path: pathlib.Path | str | None = None,
        base_dir: pathlib.Path | str | None = None,
        retention_cutoff_unix: float | None = None,
        *,
        refresh_retention_index: bool = True,
    ):
        db = pathlib.Path(db_path).expanduser() if db_path is not None else self._dashboard_db_path().expanduser()
        return dashboard_cleanup.cleanup_payload(
            self.dashboard_output_dir(), db, base_dir, retention_cutoff_unix, refresh_retention_index=refresh_retention_index
        )

    def cleanup_detail_payload(
        self,
        group_id: str,
        db_path: pathlib.Path | str | None = None,
        base_dir: pathlib.Path | str | None = None,
        retention_cutoff_unix: float | None = None,
        preview_signature: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ):
        db = pathlib.Path(db_path).expanduser() if db_path is not None else self._dashboard_db_path().expanduser()
        return dashboard_cleanup.cleanup_detail_payload(
            self.dashboard_output_dir(), db, group_id, base_dir, retention_cutoff_unix, preview_signature, page, page_size
        )

    def delete_all_logs(self, base_dir: pathlib.Path | str | None = None, db_path: pathlib.Path | str | None = None):
        base = pathlib.Path(base_dir).expanduser() if base_dir is not None else self.dashboard_output_dir()
        return dashboard_cleanup.delete_all_logs(base, db_path)
