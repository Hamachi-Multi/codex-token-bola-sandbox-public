#!/usr/bin/env python3
"""Serve a local token-usage analytics dashboard."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dashboard_cleanup_api
import dashboard_cost_rates_api
import dashboard_cleanup  # noqa: F401 - compatibility patch point for handler tests
import dashboard_freshness
import dashboard_operation_state
import dashboard_queries
import dashboard_rebuild_api
import dashboard_server_runtime
import dashboard_service_status
import cancel_control
import raw_segments
import service_paths


begin_exclusive_operation = dashboard_operation_state.begin_exclusive_operation
end_exclusive_operation = dashboard_operation_state.end_exclusive_operation
service_busy_payload = dashboard_operation_state.service_busy_payload
sweep_transient_progress_files = dashboard_operation_state.sweep_transient_progress_files
terminate_rebuild_process = dashboard_rebuild_api.terminate_rebuild_process


RUNTIME_PATHS = service_paths.resolve_runtime_paths()
CODEX_DIR = RUNTIME_PATHS.codex_dir
OUTPUT_DIR = RUNTIME_PATHS.output_dir
DB_PATH = service_paths.OutputLayout(OUTPUT_DIR).analytics_db
REPO_STATIC_ROOT = SCRIPT_DIR / "assets"
STATIC_ROOT = pathlib.Path(os.environ.get("BOLA_STATIC_ROOT", str(REPO_STATIC_ROOT))).expanduser()
MAX_JSON_BODY_BYTES = 64 * 1024

DASHBOARD_HTML_PATH = STATIC_ROOT / "dashboard.html"
DASHBOARD_CSS_PATH = STATIC_ROOT / "dashboard.css"
DASHBOARD_JS_PATH = STATIC_ROOT / "dashboard.js"


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.version == 4 and address.is_loopback
    except ValueError:
        return False


def dashboard_authority(host: str, port: int) -> str:
    normalized_host = host.lower()
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        authority_host = normalized_host
    else:
        authority_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{authority_host}:{port}"


def shutdown_dashboard_operations(manager: dashboard_operation_state.DashboardOperationManager) -> None:
    manager.begin_shutdown()
    while True:
        active = manager.active_record()
        if active is None:
            return
        if active.kind == "analysis" and active.cancel_file is not None:
            try:
                cancel_control.request_cancel(active.cancel_file, reason="server_shutdown")
            except OSError:
                pass
        if active.process is None:
            manager.wait_until_idle(timeout=0.05)
            continue
        active.process.request_shutdown()
        timeout = 6.0 if active.kind == "analysis" else 14.0
        try:
            active.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            active.process.kill_group()
        return


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


class Handler(
    dashboard_rebuild_api.DashboardRebuildApiMixin,
    dashboard_cleanup_api.DashboardCleanupApiMixin,
    dashboard_cost_rates_api.DashboardCostRatesApiMixin,
    dashboard_service_status.DashboardServiceStatusApiMixin,
    BaseHTTPRequestHandler,
):
    def dashboard_operation_manager(self) -> dashboard_operation_state.DashboardOperationManager:
        server = getattr(self, "server", None)
        return getattr(server, "operation_manager", dashboard_operation_state.DEFAULT_MANAGER)

    def dashboard_lifetime_lock_fd(self) -> int | None:
        server = getattr(self, "server", None)
        runtime_manager = getattr(server, "runtime_manager", None)
        return runtime_manager.lifetime_lock_fd() if runtime_manager is not None else None

    def dashboard_runtime_paths(self) -> service_paths.RuntimePaths:
        cached = getattr(self, "_runtime_paths_snapshot", None)
        if isinstance(cached, service_paths.RuntimePaths):
            return cached
        server = getattr(self, "server", None)
        if server is None:
            cached = service_paths.RuntimePaths(
                project_root=RUNTIME_PATHS.project_root,
                runtime_config_path=RUNTIME_PATHS.runtime_config_path,
                codex_dir=CODEX_DIR,
                output_dir=OUTPUT_DIR,
            )
        elif hasattr(server, "runtime_manager"):
            cached = server.runtime_manager.snapshot()
        elif getattr(server, "dynamic_runtime_paths", False):
            with service_paths.acquire_path_lock():
                cached = service_paths.resolve_runtime_paths()
        else:
            cached = getattr(
                server,
                "runtime_paths",
                service_paths.RuntimePaths(
                    project_root=RUNTIME_PATHS.project_root,
                    runtime_config_path=RUNTIME_PATHS.runtime_config_path,
                    codex_dir=CODEX_DIR,
                    output_dir=OUTPUT_DIR,
                ),
            )
        self._runtime_paths_snapshot = cached
        return cached

    def dashboard_output_dir(self) -> pathlib.Path:
        return self.dashboard_runtime_paths().output_dir

    def dashboard_codex_dir(self) -> pathlib.Path:
        return self.dashboard_runtime_paths().codex_dir

    def dashboard_db_path(self) -> pathlib.Path:
        server = getattr(self, "server", None)
        override = getattr(server, "db_override", None)
        if override is not None:
            return pathlib.Path(override)
        legacy = getattr(server, "db_path", None)
        if legacy is not None and not getattr(server, "dynamic_runtime_paths", False):
            return pathlib.Path(legacy)
        return self.dashboard_output_dir() / "analytics" / "bola.sqlite"

    def dashboard_script_dir(self) -> pathlib.Path:
        return SCRIPT_DIR

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.dashboard_db_path()}?mode=ro", uri=True)
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
            "started_at",
            "started_at_unix",
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
            "cost_pico_usd",
            "cost_rate_status",
            "cost_rate_effective_from",
        }
        turns_only = {
            "session_id",
            "turn_id",
            "captured_at",
            "captured_at_unix",
            "started_at",
            "started_at_unix",
            "cwd",
            "project",
            "thread_name",
            "turn_status",
            "prompt_preview",
            "total_tokens",
            "model_call_count",
            "weighted_credits",
            "cost_rate_status",
            "cost_rate_effective_from",
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

    def header_values(self, name: str) -> list[str]:
        headers = getattr(self, "headers", {})
        get_all = getattr(headers, "get_all", None)
        if callable(get_all):
            return [str(value) for value in (get_all(name) or [])]
        value = headers.get(name)
        return [] if value is None else [str(value)]

    def single_header(self, name: str) -> str | None:
        values = self.header_values(name)
        if len(values) != 1:
            return None
        value = values[0]
        if value != value.strip():
            return None
        return value

    def validate_request_host(self) -> bool:
        host = self.single_header("Host")
        allowed_authority = str(self.server.allowed_authority)
        if host is None or host.lower() != allowed_authority:
            self.send_json({"error": "request_host_forbidden"}, 403)
            return False
        return True

    def validate_post_request(self) -> bool:
        origin = self.single_header("Origin")
        allowed_origin = str(self.server.allowed_origin)
        if origin is None or origin.lower() != allowed_origin:
            self.send_json({"error": "request_origin_forbidden"}, 403)
            return False

        fetch_site_values = self.header_values("Sec-Fetch-Site")
        if len(fetch_site_values) > 1 or (fetch_site_values and fetch_site_values[0].lower() != "same-origin"):
            self.send_json({"error": "cross_site_request_forbidden"}, 403)
            return False

        content_type = self.single_header("Content-Type")
        media_type = content_type.split(";", 1)[0].strip().lower() if content_type is not None else ""
        if media_type != "application/json":
            self.send_json({"error": "application_json_required"}, 415)
            return False

        length_values = self.header_values("Content-Length")
        if not length_values:
            self.send_json({"error": "content_length_required"}, 411)
            return False
        if len(length_values) != 1:
            self.send_json({"error": "invalid_content_length"}, 400)
            return False
        try:
            length = int(length_values[0], 10)
        except (TypeError, ValueError):
            self.send_json({"error": "invalid_content_length"}, 400)
            return False
        if length < 0:
            self.send_json({"error": "invalid_content_length"}, 400)
            return False
        if length > MAX_JSON_BODY_BYTES:
            self.send_json({"error": "request_body_too_large"}, 413)
            return False
        self._json_body_length = length
        return True

    def read_json_body(self):
        if hasattr(self, "_json_body_cache"):
            return self._json_body_cache
        length = self._json_body_length
        try:
            payload = self.rfile.read(length).decode("utf-8")
            data = json.loads(payload)
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
        enriched["freshness"] = dashboard_freshness.freshness_payload(
            self.dashboard_output_dir(),
            self.dashboard_db_path().expanduser(),
        )
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
            ".png": "image/png",
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
        self._runtime_paths_snapshot = None
        if not self.validate_request_host():
            return
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
        except dashboard_server_runtime.DashboardPathTransitionBusy:
            self.send_json({"error": "dashboard_path_transition_busy"}, 409)
        except dashboard_server_runtime.DashboardOutputConflict as exc:
            self.send_json({"error": "dashboard_output_conflict", "lock_path": str(exc.path)}, 409)
        except Exception:
            logging.exception("dashboard api error path=%s query=%s", parsed.path, parsed.query)
            self.send_json({"error": "internal_error"}, 500)

    def do_POST(self):
        self._runtime_paths_snapshot = None
        if not self.validate_request_host() or not self.validate_post_request():
            return
        parsed = urlparse(self.path)
        try:
            self.read_json_body()
            if parsed.path == "/api/rebuild":
                self.handle_rebuild()
                return
            if parsed.path == "/api/rebuild/cancel":
                self.handle_rebuild_cancel()
                return
            if parsed.path == "/api/cost-rates":
                self.handle_cost_rates_post()
                return
            if parsed.path == "/api/cost-rates/recalculate":
                self.handle_cost_rates_recalculate()
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
        except dashboard_server_runtime.DashboardPathTransitionBusy:
            self.send_json({"error": "dashboard_path_transition_busy"}, 409)
        except dashboard_server_runtime.DashboardOutputConflict as exc:
            self.send_json({"error": "dashboard_output_conflict", "lock_path": str(exc.path)}, 409)
        except Exception:
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

    def handle_api(self, path, query):
        if path == "/api/service-status":
            self.handle_service_status()
            return
        if path == "/api/cost-rates":
            self.handle_cost_rates_get()
            return
        if path == "/api/rebuild/progress":
            self.handle_rebuild_progress()
            return
        if path == "/api/log-cleanup/progress":
            self.handle_cleanup_progress()
            return
        if path == "/api/log-cleanup":
            try:
                selection = self.cleanup_preview_selection(query.get("cutoff_date"), query.get("timezone"))
                payload = self.cleanup_payload(
                    retention_cutoff_unix=float(selection["cutoff_unix"]),
                    refresh_retention_index=False,
                )
                self.send_json(self.attach_cleanup_selection(payload, selection))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except raw_segments.ManifestError as exc:
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
                page = int((query.get("page") or ["1"])[0])
                page_size = int((query.get("page_size") or ["25"])[0])
                if page < 1 or page_size < 1 or page_size > 100:
                    raise ValueError("cleanup_detail_pagination_invalid")
                selection = self.cleanup_preview_selection(query.get("cutoff_date"), query.get("timezone"))
                detail_kwargs = {
                    "retention_cutoff_unix": float(selection["cutoff_unix"]),
                    "preview_signature": preview_signature,
                }
                if "page" in query or "page_size" in query:
                    detail_kwargs.update({"page": page, "page_size": page_size})
                detail = self.cleanup_detail_payload(group_id, **detail_kwargs)
            except (TypeError, ValueError) as exc:
                error = str(exc)
                if not error or "invalid literal" in error:
                    error = "cleanup_detail_pagination_invalid"
                self.send_json({"error": error}, 400)
                return
            except raw_segments.ManifestError as exc:
                self.send_json({"error": "cleanup_preview_failed", "message": str(exc)}, 409)
                return
            if detail.get("error"):
                status = 409 if detail.get("error") == "cleanup_preview_stale" else 404
                self.send_json(detail, status)
                return
            detail.update(selection)
            self.send_json(detail)
            return
        db_path = self.dashboard_db_path().expanduser()
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
    parser.add_argument("--host", default="127.0.0.1", help="IPv4 loopback address or localhost")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--codex-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--pin-runtime-paths", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not is_loopback_host(args.host):
        print("refusing to bind dashboard to an unsupported host; use localhost or an IPv4 loopback address", file=sys.stderr)
        return 2
    try:
        service_paths.require_runtime_config()
    except service_paths.ConfigurationError as exc:
        print(json.dumps({"error": "runtime_config_invalid", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    runtime_paths = service_paths.resolve_runtime_paths(codex_dir=args.codex_dir, output_dir=args.output_dir)
    operation_manager = dashboard_operation_state.DashboardOperationManager()
    try:
        runtime_manager = dashboard_server_runtime.DashboardRuntimeManager(
            runtime_paths,
            dynamic=not args.pin_runtime_paths,
            operation_manager=operation_manager,
        )
    except dashboard_server_runtime.DashboardServerBusy as exc:
        print(
            json.dumps(
                {
                    "error": "dashboard_server_already_running",
                    "lock_path": str(exc.path),
                    "owner": exc.owner,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except Exception:
        runtime_manager.close()
        raise
    server.runtime_paths = runtime_paths
    server.dynamic_runtime_paths = not args.pin_runtime_paths
    server.runtime_manager = runtime_manager
    server.operation_manager = operation_manager
    server.db_override = None
    server.allowed_authority = dashboard_authority(args.host, args.port)
    server.allowed_origin = f"http://{server.allowed_authority}"
    print(f"http://{args.host}:{args.port}")
    previous_handlers: dict[int, Any] = {}

    def request_shutdown(_signum, _frame) -> None:
        if operation_manager.shutting_down:
            return
        operation_manager.begin_shutdown()
        threading.Thread(target=server.shutdown, name="dashboard-shutdown", daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    try:
        server.serve_forever()
    finally:
        shutdown_dashboard_operations(operation_manager)
        server.server_close()
        runtime_manager.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
