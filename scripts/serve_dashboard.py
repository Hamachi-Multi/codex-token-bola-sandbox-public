#!/usr/bin/env python3
"""Serve a local token-usage analytics dashboard."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dashboard_cleanup
import dashboard_freshness
import dashboard_queries
import cancel_control
import progress_control
import service_lock
import service_paths


CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
TOKEN_USAGE_ROOT = service_paths.service_root(CODEX_HOME)
DB_PATH = pathlib.Path(
    os.environ.get("CODEX_TOKEN_USAGE_ANALYTICS_DB", str(TOKEN_USAGE_ROOT / "analytics" / "token-usage.sqlite"))
).expanduser()
REPO_STATIC_ROOT = SCRIPT_DIR.parent / "assets"
STATIC_ROOT = pathlib.Path(
    os.environ.get("CODEX_TOKEN_USAGE_STATIC_ROOT", str(REPO_STATIC_ROOT if REPO_STATIC_ROOT.exists() else TOKEN_USAGE_ROOT / "assets"))
).expanduser()
REBUILD_LOCK = threading.Lock()
REBUILD_CANCEL_EVENT = threading.Event()
REBUILD_PROCESS_LOCK = threading.Lock()
REBUILD_PROCESS: subprocess.Popen[str] | None = None
REBUILD_CANCEL_FILE: pathlib.Path | None = None
REBUILD_PROGRESS_FILE: pathlib.Path | None = None
CLEANUP_PROGRESS_LOCK = threading.Lock()
CLEANUP_PROGRESS_FILE: pathlib.Path | None = None
CLEANUP_RUNNING = False
AUTO_COMPACT_MIN_BYTES = 64 * 1024 * 1024
TRANSIENT_PROGRESS_PATTERNS = (
    "cleanup-progress.*.json",
    "rebuild-progress.*.json",
    "rebuild-cancel.*.json",
)

DASHBOARD_HTML_PATH = STATIC_ROOT / "dashboard.html"
DASHBOARD_CSS_PATH = STATIC_ROOT / "dashboard.css"
DASHBOARD_JS_PATH = STATIC_ROOT / "dashboard.js"


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def terminate_rebuild_process(process: subprocess.Popen[str], grace_seconds: float = 2.0) -> str:
    if process.poll() is not None:
        return "completed"
    process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return "terminated"
    except subprocess.TimeoutExpired:
        process.kill()
        return "killed"


def read_dashboard_asset(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read dashboard asset at {path}") from exc


def dashboard_js_paths() -> list[pathlib.Path]:
    module_root = STATIC_ROOT / "dashboard"
    module_paths = sorted(path for path in module_root.rglob("*.js") if path.is_file()) if module_root.exists() else []
    return [DASHBOARD_JS_PATH, *module_paths]


HTML = read_dashboard_asset(DASHBOARD_HTML_PATH)
DASHBOARD_CSS = read_dashboard_asset(DASHBOARD_CSS_PATH)
DASHBOARD_JS = "\n".join(read_dashboard_asset(path) for path in dashboard_js_paths())
DASHBOARD_SOURCE_BUNDLE = "\n".join((HTML, DASHBOARD_CSS, DASHBOARD_JS))


class BadJsonBody(ValueError):
    pass


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


class Handler(BaseHTTPRequestHandler):
    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.server.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def send_empty_analytics_payload(self, path, query) -> None:
        try:
            self.send_json(self.with_freshness(path, dashboard_queries.empty_payload(path, query)))
        except dashboard_queries.ApiError as exc:
            self.send_json({"error": exc.error}, exc.status)

    def is_uninitialized_database_error(self, exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return "unable to open database file" in message or "no such table" in message

    def required_analytics_schema(self, path: str) -> dict[str, set[str]]:
        dashboard_turns = {
            "session_id",
            "turn_id",
            "captured_at",
            "captured_at_unix",
            "cwd",
            "project",
            "thread_name",
            "turn_status",
            "prompt_preview",
            "input_tokens",
            "cached_input_tokens",
            "non_cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
            "model_call_count",
            "weighted_credits",
        }
        turns_only = {
            "session_id",
            "turn_id",
            "captured_at",
            "captured_at_unix",
            "cwd",
            "project",
            "thread_name",
            "turn_status",
            "prompt_preview",
            "total_tokens",
            "model_call_count",
            "weighted_credits",
        }
        dashboard_schema = {
            "turns": dashboard_turns,
            "tool_call_summaries": {
                "session_id",
                "turn_id",
                "tool_name",
                "calls",
                "output_chars",
                "output_reported_tokens",
                "output_tokens",
            },
            "task_rollups": {
                "parent_session_id",
                "parent_turn_id",
                "confidence",
                "child_total_tokens",
                "child_weighted_credits",
            },
        }
        if path == "/api/dashboard":
            return dashboard_schema
        if path == "/api/turns":
            return {"turns": turns_only}
        return {}

    def analytics_schema_warning(self, con: sqlite3.Connection, path: str) -> dict[str, str] | None:
        try:
            required_schema = self.required_analytics_schema(path)
            if not required_schema:
                return None
            tables = {str(row[0]) for row in con.execute("select name from sqlite_master where type='table'")}
            for table, required_columns in required_schema.items():
                if table not in tables:
                    return {"code": "analytics_schema_stale", "table": table}
                existing_columns = {str(row[1]) for row in con.execute(f"pragma table_info({table})")}
                missing_columns = sorted(required_columns - existing_columns)
                if missing_columns:
                    return {"code": "analytics_schema_stale", "table": table, "column": missing_columns[0]}
        except sqlite3.Error:
            return {"code": "analytics_schema_stale", "table": "unknown"}
        return None

    def send_stale_analytics_payload(self, path, query, warning: dict[str, str]) -> None:
        try:
            payload = dashboard_queries.empty_payload(path, query)
            enriched = self.with_freshness(path, payload)
            if path == "/api/dashboard":
                freshness = dict(enriched.get("freshness") or {})
                warnings = list(freshness.get("warnings") or [])
                warnings.append(warning)
                freshness["warnings"] = warnings
                freshness["data_health"] = "degraded"
                if freshness.get("status") == "current":
                    freshness["status"] = "degraded"
                enriched["freshness"] = freshness
            self.send_json(enriched)
        except dashboard_queries.ApiError as exc:
            self.send_json({"error": exc.error}, exc.status)

    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_json_body(self):
        if hasattr(self, "_json_body_cache"):
            return self._json_body_cache
        headers = getattr(self, "headers", {})
        try:
            length = int(headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._json_body_cache = {}
            return self._json_body_cache
        try:
            payload = self.rfile.read(length).decode("utf-8")
            data = json.loads(payload or "{}")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise BadJsonBody("invalid json body")
        if not isinstance(data, dict):
            raise BadJsonBody("json body must be an object")
        self._json_body_cache = data
        return self._json_body_cache

    def with_freshness(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path != "/api/dashboard":
            return payload
        enriched = dict(payload)
        enriched["freshness"] = dashboard_freshness.freshness_payload(TOKEN_USAGE_ROOT, pathlib.Path(self.server.db_path).expanduser())
        return enriched

    def send_static(self, parsed):
        relative = pathlib.PurePosixPath(unquote(parsed.path).lstrip("/"))
        if ".." in relative.parts:
            self.send_json({"error": "not_found"}, 404)
            return
        root = STATIC_ROOT.resolve()
        target = (STATIC_ROOT.parent / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self.send_json({"error": "not_found"}, 404)
            return
        if not target.is_file():
            self.send_json({"error": "not_found"}, 404)
            return
        content_type = {
            ".woff2": "font/woff2",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        cache_control = "no-cache" if target.suffix in {".css", ".js"} else "public, max-age=31536000, immutable"
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            payload = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path.startswith("/assets/"):
            self.send_static(parsed)
            return
        if not parsed.path.startswith("/api/"):
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            self.handle_api(parsed.path, parse_qs(parsed.query))
        except Exception as exc:
            logging.exception("dashboard api error path=%s query=%s", parsed.path, parsed.query)
            self.send_json({"error": "internal_error"}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            self.read_json_body()
            if parsed.path == "/api/rebuild":
                self.handle_rebuild()
                return
            if parsed.path == "/api/rebuild/cancel":
                self.handle_rebuild_cancel()
                return
            if parsed.path == "/api/log-cleanup/compact":
                self.handle_cleanup_compact()
                return
            if parsed.path == "/api/log-cleanup/all":
                self.handle_cleanup_delete_all()
                return
            if parsed.path == "/api/log-cleanup/retention":
                self.handle_cleanup_retention()
                return
            self.send_json({"error": "not_found"}, 404)
        except BadJsonBody:
            self.send_json({"error": "invalid_json"}, 400)
        except Exception as exc:
            logging.exception("dashboard post api error path=%s", parsed.path)
            self.send_json({"error": "internal_error"}, 500)

    def parse_last_json(self, stdout: str) -> dict[str, Any]:
        for line in reversed(stdout.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def numeric_metadata(self, metadata: dict[str, Any], key: str, default: float = 0.0) -> float:
        try:
            return float(metadata.get(key, default))
        except (TypeError, ValueError):
            return default

    def int_metadata(self, metadata: dict[str, Any], key: str, default: int = 0) -> int:
        try:
            return int(metadata.get(key, default))
        except (TypeError, ValueError):
            return default

    def run_compact_command(self, output: pathlib.Path, min_bytes: int):
        script = pathlib.Path(__file__).resolve().parent / "codex_token_usage.py"
        cmd = [
            sys.executable,
            str(script),
            "compact",
        ]
        result = subprocess.run(cmd, cwd=str(script.parent), text=True, capture_output=True, env=service_lock.scrub_lock_env(os.environ.copy()))
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        return {
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "metadata": self.parse_last_json(stdout),
        }

    def handle_rebuild(self):
        if not REBUILD_LOCK.acquire(blocking=False):
            self.send_json({"error": "rebuild_already_running"}, 409)
            return
        global REBUILD_PROCESS, REBUILD_CANCEL_FILE, REBUILD_PROGRESS_FILE
        started = time.monotonic()
        cancel_file = TOKEN_USAGE_ROOT / "state" / f"rebuild-cancel.{os.getpid()}.{time.time_ns()}.json"
        progress_file = TOKEN_USAGE_ROOT / "state" / f"rebuild-progress.{os.getpid()}.{time.time_ns()}.json"
        try:
            REBUILD_CANCEL_EVENT.clear()
            sweep_transient_progress_files(TOKEN_USAGE_ROOT)
            previous_progress_file = REBUILD_PROGRESS_FILE
            cancel_file.unlink(missing_ok=True)
            progress_file.unlink(missing_ok=True)
            if (
                previous_progress_file is not None
                and previous_progress_file != progress_file
                and previous_progress_file.name.startswith("rebuild-progress.")
            ):
                previous_progress_file.unlink(missing_ok=True)
            progress_control.write_progress_to_path(progress_file, status="running", phase="normalize", phase_index=0, checkpoint="queued", phase_progress=0.0)
            script = pathlib.Path(__file__).resolve().parent / "codex_token_usage.py"
            output = pathlib.Path(self.server.db_path).expanduser().resolve()
            cmd = [
                sys.executable,
                str(script),
                "pipeline",
                "--codex-home",
                str(CODEX_HOME),
                "--output",
                str(output),
                "--incremental",
                "--recover",
            ]
            env = service_lock.scrub_lock_env(os.environ.copy())
            env["CODEX_TOKEN_USAGE_CANCEL_FILE"] = str(cancel_file)
            env["CODEX_TOKEN_USAGE_PROGRESS_FILE"] = str(progress_file)
            with REBUILD_PROCESS_LOCK:
                REBUILD_PROCESS = None
                REBUILD_CANCEL_FILE = cancel_file
                REBUILD_PROGRESS_FILE = progress_file
            with tempfile.TemporaryFile("w+", encoding="utf-8") as stdout_file, tempfile.TemporaryFile("w+", encoding="utf-8") as stderr_file:
                process = subprocess.Popen(
                    cmd,
                    cwd=str(script.parent),
                    text=True,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=env,
                )
                with REBUILD_PROCESS_LOCK:
                    REBUILD_PROCESS = process
                    REBUILD_CANCEL_FILE = cancel_file
                    REBUILD_PROGRESS_FILE = progress_file
                if REBUILD_CANCEL_EVENT.is_set():
                    cancel_control.request_cancel(cancel_file, reason="user")
                try:
                    cancel_requested_at: float | None = None
                    while process.poll() is None:
                        if REBUILD_CANCEL_EVENT.is_set():
                            cancel_control.request_cancel(cancel_file, reason="user")
                            if cancel_requested_at is None:
                                cancel_requested_at = time.monotonic()
                                progress_control.write_progress_to_path(progress_file, status="running", phase="cancel", phase_index=1, checkpoint="cancel_requested", phase_progress=0.0)
                            elif time.monotonic() - cancel_requested_at >= 2.0:
                                status = terminate_rebuild_process(process)
                                progress_control.write_progress_to_path(progress_file, status="cancelled", phase="cancel", phase_index=1, checkpoint=status, phase_progress=1.0)
                                break
                        time.sleep(0.1)
                    process.wait(timeout=2)
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout = stdout_file.read()
                    stderr = stderr_file.read()
                finally:
                    with REBUILD_PROCESS_LOCK:
                        if REBUILD_PROCESS is process:
                            REBUILD_PROCESS = None
                            REBUILD_CANCEL_FILE = None
                            REBUILD_PROGRESS_FILE = progress_file
            stdout = (stdout or "").strip()
            stderr = (stderr or "").strip()
            metadata = self.parse_last_json(stdout)
            if (
                process.returncode == cancel_control.CANCEL_EXIT_CODE
                or bool(metadata.get("cancelled"))
            ):
                progress_control.write_progress_to_path(
                    progress_file,
                    status="cancelled",
                    phase=str(metadata.get("phase") or "cancelled"),
                    phase_index=self.int_metadata(metadata, "phase_index"),
                    checkpoint=str(metadata.get("checkpoint") or ""),
                    phase_progress=self.numeric_metadata(metadata, "phase_progress"),
                )
                self.send_json(
                    {
                        "ok": False,
                        "cancelled": True,
                        **metadata,
                        "returncode": process.returncode,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
                )
                return
            if process.returncode != 0:
                progress_control.write_progress_to_path(progress_file, status="failed", phase="failed", phase_index=0, checkpoint="failed")
                if metadata.get("error") == "analysis_or_cleanup_running":
                    self.send_json(
                        {
                            "error": "analysis_or_cleanup_running",
                            "returncode": process.returncode,
                            "lock_path": metadata.get("lock_path"),
                        },
                        409,
                    )
                    return
                if metadata.get("error") == "normalize_pending_publish_recovery_failed":
                    self.send_json(
                        {
                            "error": "normalize_pending_publish_recovery_failed",
                            "returncode": process.returncode,
                            "message": metadata.get("message"),
                            "marker_path": metadata.get("marker_path"),
                            "recovery_required": bool(metadata.get("recovery_required")),
                        },
                        409,
                    )
                    return
                self.send_json(
                    {
                        "error": "rebuild_failed",
                        "returncode": process.returncode,
                        "stderr": stderr[-4000:],
                        "stdout": stdout[-4000:],
                    },
                    500,
                )
                return
            if "elapsed_ms" in metadata:
                metadata["analysis_elapsed_ms"] = metadata.pop("elapsed_ms")
            metadata["pre_analysis_rotate"] = metadata.get("pre_analysis_rotate", {"skipped": True})
            try:
                progress_control.write_progress_to_path(progress_file, status="running", phase="refresh", phase_index=2, checkpoint="cleanup-retention-index", phase_progress=0.65)
                retention_index = dashboard_cleanup.refresh_retention_index_for_current_sources(TOKEN_USAGE_ROOT)
                metadata["cleanup_retention_index"] = {
                    "sources": len(retention_index.get("sources", [])),
                    "scanned_rows": sum(int(source.get("scanned_rows") or 0) for source in retention_index.get("sources", [])),
                }
            except Exception as exc:
                metadata["cleanup_retention_index"] = {"error": repr(exc)}
            progress_control.write_progress_to_path(progress_file, status="completed", phase="refresh", phase_index=2, checkpoint="completed", phase_progress=1.0)
            self.send_json({"ok": True, **metadata, "elapsed_ms": round((time.monotonic() - started) * 1000)})
        finally:
            with REBUILD_PROCESS_LOCK:
                if REBUILD_PROGRESS_FILE == progress_file:
                    REBUILD_PROGRESS_FILE = None
                if REBUILD_CANCEL_FILE == cancel_file:
                    REBUILD_CANCEL_FILE = None
            REBUILD_CANCEL_EVENT.clear()
            cancel_file.unlink(missing_ok=True)
            progress_file.unlink(missing_ok=True)
            REBUILD_LOCK.release()

    def handle_rebuild_cancel(self):
        REBUILD_CANCEL_EVENT.set()
        graceful = False
        with REBUILD_PROCESS_LOCK:
            process = REBUILD_PROCESS
            cancel_file = REBUILD_CANCEL_FILE
        if cancel_file is not None:
            try:
                cancel_control.request_cancel(cancel_file, reason="user")
                graceful = True
            except OSError:
                graceful = False
        self.send_json(
            {
                "ok": True,
                "cancel_requested": True,
                "graceful": graceful,
                "process_running": process is not None and process.poll() is None,
            }
        )

    def handle_rebuild_progress(self):
        with REBUILD_PROCESS_LOCK:
            process = REBUILD_PROCESS
            progress_file = REBUILD_PROGRESS_FILE
        payload = progress_control.read_progress(progress_file)
        payload["process_running"] = process is not None and process.poll() is None
        self.send_json(payload)

    def begin_cleanup_progress(self, *, phase: str, phase_index: int, checkpoint: str, phase_progress: float = 0.0) -> pathlib.Path:
        global CLEANUP_PROGRESS_FILE, CLEANUP_RUNNING
        progress_file = TOKEN_USAGE_ROOT / "state" / f"cleanup-progress.{os.getpid()}.{time.time_ns()}.json"
        previous_progress_file: pathlib.Path | None
        sweep_transient_progress_files(TOKEN_USAGE_ROOT)
        with CLEANUP_PROGRESS_LOCK:
            previous_progress_file = CLEANUP_PROGRESS_FILE
            CLEANUP_PROGRESS_FILE = progress_file
            CLEANUP_RUNNING = True
        progress_file.unlink(missing_ok=True)
        if (
            previous_progress_file is not None
            and previous_progress_file != progress_file
            and previous_progress_file.name.startswith("cleanup-progress.")
        ):
            previous_progress_file.unlink(missing_ok=True)
        progress_control.write_progress_to_path(
            progress_file,
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
        progress_control.write_progress_to_path(progress_file, phase_count=4, **kwargs)

    def close_cleanup_progress(self, progress_file: pathlib.Path | None) -> None:
        global CLEANUP_PROGRESS_FILE, CLEANUP_RUNNING
        if progress_file is None:
            return
        with CLEANUP_PROGRESS_LOCK:
            if CLEANUP_PROGRESS_FILE == progress_file:
                CLEANUP_PROGRESS_FILE = None
                CLEANUP_RUNNING = False
        progress_file.unlink(missing_ok=True)

    def handle_cleanup_progress(self):
        with CLEANUP_PROGRESS_LOCK:
            progress_file = CLEANUP_PROGRESS_FILE
            cleanup_running = CLEANUP_RUNNING
        payload = progress_control.read_progress(progress_file)
        payload["cleanup_running"] = cleanup_running
        self.send_json(payload)

    def run_pipeline_command(self, output: pathlib.Path, *, incremental: bool) -> dict[str, Any]:
        script = pathlib.Path(__file__).resolve().parent / "codex_token_usage.py"
        cmd = [
            sys.executable,
            str(script),
            "pipeline",
            "--codex-home",
            str(CODEX_HOME),
            "--output",
            str(output),
        ]
        if incremental:
            cmd.append("--incremental")
        result = subprocess.run(cmd, cwd=str(script.parent), text=True, capture_output=True, env=service_lock.scrub_lock_env(os.environ.copy()))
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        return {
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "metadata": self.parse_last_json(stdout),
        }

    def run_retention_prune_command(self, output: pathlib.Path, cutoff_unix: float, preview_signature: str) -> dict[str, Any]:
        script = pathlib.Path(__file__).resolve().parent / "codex_token_usage.py"
        cmd = [
            sys.executable,
            str(script),
            "retention-prune",
            "--codex-home",
            str(CODEX_HOME),
            "--output",
            str(output),
            "--cutoff",
            str(float(cutoff_unix)),
            "--preview-signature",
            preview_signature,
        ]
        env = service_lock.scrub_lock_env(os.environ.copy())
        with CLEANUP_PROGRESS_LOCK:
            progress_file = CLEANUP_PROGRESS_FILE
        if progress_file is not None:
            env[progress_control.PROGRESS_ENV] = str(progress_file)
        result = subprocess.run(cmd, cwd=str(script.parent), text=True, capture_output=True, env=env)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        return {
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "metadata": self.parse_last_json(stdout),
        }

    def cleanup_cutoff_unix(self, value: Any = None) -> float:
        raw = value[0] if isinstance(value, list) and value else value
        if isinstance(raw, str) and raw:
            try:
                return datetime.fromisoformat(raw + "T00:00:00+00:00").timestamp()
            except ValueError:
                pass
        return dashboard_cleanup.default_retention_cutoff_unix()

    def required_cleanup_cutoff_unix(self, value: Any = None) -> float:
        raw = value[0] if isinstance(value, list) and value else value
        if not isinstance(raw, str) or not raw:
            raise ValueError("cutoff_date_required")
        try:
            return datetime.fromisoformat(raw + "T00:00:00+00:00").timestamp()
        except ValueError as exc:
            raise ValueError("cutoff_date_invalid") from exc

    def cleanup_preview_cutoff_unix(self, value: Any = None) -> float:
        raw = value[0] if isinstance(value, list) and value else value
        if isinstance(raw, str) and raw:
            return self.required_cleanup_cutoff_unix(raw)
        return self.cleanup_cutoff_unix(value)

    def handle_cleanup_compact(self):
        if not REBUILD_LOCK.acquire(blocking=False):
            self.send_json({"error": "analysis_or_cleanup_running"}, 409)
            return
        started = time.monotonic()
        try:
            options = self.read_json_body()
            try:
                min_bytes = int(options.get("min_bytes", 1))
            except (TypeError, ValueError):
                min_bytes = 1
            min_bytes = max(1, min(min_bytes, 1024 * 1024 * 1024))
            output = pathlib.Path(self.server.db_path).expanduser().resolve()
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
            dashboard_cleanup.refresh_retention_index_for_current_sources(TOKEN_USAGE_ROOT)
            self.send_json(
                {
                    "ok": True,
                    "compact": result["metadata"],
                    "cleanup": self.cleanup_payload(db_path=output),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            )
        finally:
            REBUILD_LOCK.release()

    def handle_cleanup_delete_all(self):
        if not REBUILD_LOCK.acquire(blocking=False):
            self.send_json({"error": "analysis_or_cleanup_running"}, 409)
            return
        started = time.monotonic()
        progress_file: pathlib.Path | None = None
        try:
            options = self.read_json_body()
            if options.get("confirm_all_logs") is not True:
                self.send_json({"error": "delete_all_confirmation_required"}, 400)
                return
            output = pathlib.Path(self.server.db_path).expanduser().resolve()
            progress_file = self.begin_cleanup_progress(
                phase="cleanup-delete",
                phase_index=1,
                checkpoint="delete-all",
                phase_progress=0.0,
            )
            try:
                result = self.delete_all_logs(TOKEN_USAGE_ROOT, output)
            except service_lock.ServiceLockBusy:
                self.write_cleanup_progress(progress_file, status="failed", phase="cleanup-delete", phase_index=1, checkpoint="busy", phase_progress=0.0)
                self.send_json({"error": "analysis_or_cleanup_running"}, 409)
                return
            failed = bool(result.get("delete_failed") or result.get("failed"))
            if failed:
                self.write_cleanup_progress(progress_file, status="failed", phase="cleanup-delete", phase_index=1, checkpoint="partial-failure", phase_progress=1.0)
            else:
                self.write_cleanup_progress(progress_file, status="running", phase="cleanup-refresh", phase_index=3, checkpoint="refresh-preview", phase_progress=0.2)
            cleanup_payload = self.cleanup_payload(db_path=output)
            if not failed:
                self.write_cleanup_progress(progress_file, status="completed", phase="cleanup-refresh", phase_index=3, checkpoint="completed", phase_progress=1.0)
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
            REBUILD_LOCK.release()

    def handle_cleanup_retention(self):
        if not REBUILD_LOCK.acquire(blocking=False):
            self.send_json({"error": "analysis_or_cleanup_running"}, 409)
            return
        started = time.monotonic()
        progress_file: pathlib.Path | None = None
        try:
            options = self.read_json_body()
            cutoff_date = str(options.get("cutoff_date") or "")
            try:
                cutoff_unix = self.required_cleanup_cutoff_unix(cutoff_date)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            output = pathlib.Path(self.server.db_path).expanduser().resolve()
            preview_signature = options.get("preview_signature")
            if not isinstance(preview_signature, str) or not preview_signature:
                self.send_json({"error": "cleanup_preview_signature_required"}, 400)
                return
            try:
                current_cleanup = self.cleanup_payload(db_path=output, retention_cutoff_unix=cutoff_unix)
            except dashboard_cleanup.raw_segments.ManifestError as exc:
                self.send_json({"error": "cleanup_preview_failed", "message": str(exc)}, 409)
                return
            current_signature = str((((current_cleanup.get("retention") or {}).get("selected") or {}).get("preview_signature")) or "")
            if preview_signature != current_signature:
                self.send_json({"error": "cleanup_preview_stale"}, 409)
                return
            selected_retention = ((current_cleanup.get("retention") or {}).get("selected") or {})
            selected_rows = int(selected_retention.get("deletable_rows") or 0)
            selected_pending_state_files = int(selected_retention.get("pending_turn_state_deletable_files") or 0)
            if selected_rows <= 0 and selected_pending_state_files <= 0:
                scanned_rows = int(selected_retention.get("scanned_rows") or 0)
                self.send_json(
                    {
                        "ok": True,
                        "noop": True,
                        "cutoff_date": cutoff_date,
                        "retention": {
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
            prune_result = self.run_retention_prune_command(output, cutoff_unix, preview_signature)
            if prune_result["returncode"] != 0:
                metadata = prune_result.get("metadata") if isinstance(prune_result.get("metadata"), dict) else {}
                if metadata.get("error") == "cleanup_preview_stale":
                    self.write_cleanup_progress(progress_file, status="failed", phase="cleanup-prepare", phase_index=0, checkpoint="stale-preview", phase_progress=0.0)
                    self.send_json({"error": "cleanup_preview_stale"}, 409)
                    return
                if metadata.get("error") == "analysis_or_cleanup_running":
                    self.write_cleanup_progress(progress_file, status="failed", phase="cleanup-prepare", phase_index=0, checkpoint="busy", phase_progress=0.0)
                    self.send_json(
                        {
                            "error": "analysis_or_cleanup_running",
                            "returncode": prune_result["returncode"],
                            "lock_path": metadata.get("lock_path"),
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
            metadata = prune_result["metadata"]
            retention_result = metadata.get("delete") if isinstance(metadata.get("delete"), dict) else {"deleted_rows": metadata.get("deleted_rows", 0)}
            self.write_cleanup_progress(progress_file, status="running", phase="cleanup-refresh", phase_index=3, checkpoint="retention-index", phase_progress=0.2)
            dashboard_cleanup.refresh_retention_index_for_current_sources(TOKEN_USAGE_ROOT)
            self.write_cleanup_progress(progress_file, status="running", phase="cleanup-refresh", phase_index=3, checkpoint="preview-payload", phase_progress=0.75)
            cleanup_payload = self.cleanup_payload(db_path=output, retention_cutoff_unix=cutoff_unix)
            self.write_cleanup_progress(progress_file, status="completed", phase="cleanup-refresh", phase_index=3, checkpoint="completed", phase_progress=1.0)
            self.send_json(
                {
                    "ok": True,
                    "cutoff_date": cutoff_date,
                    "retention": retention_result,
                    "cleanup": cleanup_payload,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            )
        finally:
            self.close_cleanup_progress(progress_file)
            REBUILD_LOCK.release()

    def cleanup_payload(
        self,
        db_path: pathlib.Path | str | None = None,
        base_dir: pathlib.Path | str | None = None,
        retention_cutoff_unix: float | None = None,
        *,
        refresh_retention_index: bool = True,
    ):
        db = pathlib.Path(db_path).expanduser() if db_path is not None else pathlib.Path(self.server.db_path).expanduser()
        return dashboard_cleanup.cleanup_payload(TOKEN_USAGE_ROOT, db, base_dir, retention_cutoff_unix, refresh_retention_index=refresh_retention_index)

    def cleanup_detail_payload(
        self,
        group_id: str,
        db_path: pathlib.Path | str | None = None,
        base_dir: pathlib.Path | str | None = None,
        retention_cutoff_unix: float | None = None,
        preview_signature: str | None = None,
    ):
        db = pathlib.Path(db_path).expanduser() if db_path is not None else pathlib.Path(self.server.db_path).expanduser()
        return dashboard_cleanup.cleanup_detail_payload(TOKEN_USAGE_ROOT, db, group_id, base_dir, retention_cutoff_unix, preview_signature)

    def delete_all_logs(self, base_dir: pathlib.Path | str | None = None, db_path: pathlib.Path | str | None = None):
        base = pathlib.Path(base_dir).expanduser() if base_dir is not None else TOKEN_USAGE_ROOT
        return dashboard_cleanup.delete_all_logs(base, db_path)

    def handle_api(self, path, query):
        if path == "/api/rebuild/progress":
            self.handle_rebuild_progress()
            return
        if path == "/api/log-cleanup/progress":
            self.handle_cleanup_progress()
            return
        if path == "/api/log-cleanup":
            try:
                self.send_json(
                    self.cleanup_payload(
                        retention_cutoff_unix=self.cleanup_preview_cutoff_unix(query.get("cutoff_date")),
                        refresh_retention_index=False,
                    )
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except dashboard_cleanup.raw_segments.ManifestError as exc:
                self.send_json({"error": "cleanup_preview_failed", "message": str(exc)}, 409)
            return
        if path == "/api/log-cleanup/detail":
            group_id = str((query.get("group_id") or [""])[0])
            if not group_id:
                self.send_json({"error": "cleanup_group_id_required"}, 400)
                return
            preview_signature = str((query.get("preview_signature") or [""])[0])
            if not preview_signature:
                self.send_json({"error": "cleanup_preview_signature_required"}, 400)
                return
            try:
                detail = self.cleanup_detail_payload(
                    group_id,
                    retention_cutoff_unix=self.cleanup_preview_cutoff_unix(query.get("cutoff_date")),
                    preview_signature=preview_signature,
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            except dashboard_cleanup.raw_segments.ManifestError as exc:
                self.send_json({"error": "cleanup_preview_failed", "message": str(exc)}, 409)
                return
            if detail.get("error"):
                status = 409 if detail.get("error") == "cleanup_preview_stale" else 404
                self.send_json(detail, status)
                return
            self.send_json(detail)
            return
        db_path = pathlib.Path(self.server.db_path).expanduser()
        if not db_path.is_file():
            self.send_empty_analytics_payload(path, query)
            return
        con = None
        try:
            con = self.db()
            schema_warning = self.analytics_schema_warning(con, path)
            if schema_warning is not None:
                self.send_stale_analytics_payload(path, query, schema_warning)
                return
            queries = dashboard_queries.DashboardQueries(con, query)
            self.send_json(self.with_freshness(path, queries.payload(path)))
        except dashboard_queries.ApiError as exc:
            self.send_json({"error": exc.error}, exc.status)
        except sqlite3.Error as exc:
            if not self.is_uninitialized_database_error(exc):
                raise
            self.send_empty_analytics_payload(path, query)
        finally:
            if con is not None:
                con.close()

    def log_message(self, fmt, *args):
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    if not is_loopback_host(args.host) and not args.allow_network:
        print("refusing to bind dashboard to non-loopback host without --allow-network", file=sys.stderr)
        return 2
    if not is_loopback_host(args.host):
        print("warning: dashboard is bound to a non-loopback host and may expose local usage data", file=sys.stderr)
    service_paths.assert_migrated(CODEX_HOME)
    try:
        dashboard_cleanup.ensure_service_owned_output(TOKEN_USAGE_ROOT, DB_PATH)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sweep_transient_progress_files(TOKEN_USAGE_ROOT)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.db_path = DB_PATH
    print(f"http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
