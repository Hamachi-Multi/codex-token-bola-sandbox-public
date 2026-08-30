#!/usr/bin/env python3
"""Browser-level checks for the Codex Token Bola dashboard."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from dashboard_fixture_data import write_dashboard_fixture
import service_paths
from playwright_dashboard_analyze import check_analyze_cancel_failure_is_local
from playwright_dashboard_cleanup import check_cleanup_short_desktop
from playwright_dashboard_cache import (
    check_atomic_turn_pagination,
    check_query_cache_runtime,
    check_stale_dashboard_cannot_overwrite_sort,
    check_stale_failure_and_detail_failure_are_local,
)
from playwright_dashboard_desktop import (
    check_desktop_cleanup,
    check_desktop_toolbar,
    check_desktop_tools,
    check_desktop_turns,
)
from playwright_dashboard_mobile import check_mobile
from playwright_dashboard_settings import check_cost_rate_settings

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class BrowserScenario:
    name: str
    check: Callable[[object, str], None]
    viewport: tuple[int, int]


@dataclass
class PageDiagnostics:
    runtime_errors: list[str] = field(default_factory=list)
    inflight_requests: dict[int, str] = field(default_factory=dict)


SCENARIOS = (
    BrowserScenario("desktop-toolbar", check_desktop_toolbar, (1440, 900)),
    BrowserScenario("desktop-turns", check_desktop_turns, (1440, 900)),
    BrowserScenario("desktop-cleanup", check_desktop_cleanup, (1440, 900)),
    BrowserScenario("desktop-tools-subagents", check_desktop_tools, (1440, 900)),
    BrowserScenario("cleanup-compact", check_cleanup_short_desktop, (1280, 720)),
    BrowserScenario("mobile", check_mobile, (390, 844)),
    BrowserScenario("query-cache", check_query_cache_runtime, (1280, 720)),
    BrowserScenario("atomic-pagination", check_atomic_turn_pagination, (1440, 1000)),
    BrowserScenario("stale-sort", check_stale_dashboard_cannot_overwrite_sort, (1440, 1000)),
    BrowserScenario("local-failures", check_stale_failure_and_detail_failure_are_local, (1440, 1000)),
    BrowserScenario("analyze-cancel", check_analyze_cancel_failure_is_local, (1440, 900)),
    BrowserScenario("settings-cost-rates", check_cost_rate_settings, (1280, 900)),
)
SCENARIO_NAMES = tuple(scenario.name for scenario in SCENARIOS)


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(base_url: str, process: subprocess.Popen[object], *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"fixture dashboard server exited early with code {process.returncode}")
        try:
            with urllib.request.urlopen(base_url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"fixture dashboard server did not start at {base_url}: {last_error!r}")


def copy_dashboard_assets(output_dir: pathlib.Path) -> None:
    source = ROOT / "scripts" / "assets"
    target = pathlib.Path(output_dir).expanduser().resolve(strict=False) / "assets"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def safe_request_label(request) -> str:
    parsed = urllib.parse.urlsplit(request.url)
    query_keys = sorted({key for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)})
    query = f"?{','.join(query_keys)}" if query_keys else ""
    return f"{request.method} {parsed.path}{query}"


def attach_page_diagnostics(page) -> PageDiagnostics:
    diagnostics = PageDiagnostics()

    def on_page_error(exc) -> None:
        diagnostics.runtime_errors.append(f"pageerror: {exc}")

    def on_console(message) -> None:
        if message.type == "error":
            diagnostics.runtime_errors.append(f"console.error: {message.text}")

    def on_request(request) -> None:
        if request.resource_type in {"document", "script", "xhr", "fetch"}:
            diagnostics.inflight_requests[id(request)] = safe_request_label(request)

    def on_request_finished(request) -> None:
        diagnostics.inflight_requests.pop(id(request), None)

    def on_request_failed(request) -> None:
        if request.resource_type not in {"document", "script", "xhr", "fetch"}:
            return
        diagnostics.inflight_requests.pop(id(request), None)
        failure = request.failure or "unknown failure"
        diagnostics.runtime_errors.append(f"requestfailed: {safe_request_label(request)} {failure}")

    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    page.on("request", on_request)
    page.on("requestfinished", on_request_finished)
    page.on("requestfailed", on_request_failed)
    return diagnostics


def page_failure_state(page, diagnostics: PageDiagnostics) -> str:
    try:
        state = page.evaluate(
            """
            () => {
              const active = document.activeElement;
              return {
                activeView: document.querySelector('.view.active')?.dataset.view || '',
                activeElement: active ? {
                  tag: active.tagName,
                  id: active.id || '',
                  classes: String(active.className || ''),
                } : null,
                hash: location.hash,
              };
            }
            """
        )
    except Exception as exc:
        state = {"unavailable": str(exc)}
    inflight = sorted(diagnostics.inflight_requests.values())
    return f"state={state!r}\ninflight={inflight!r}\nruntime_errors={diagnostics.runtime_errors!r}"


def run_scenario(browser, scenario: BrowserScenario, base_url: str, *, iteration: int) -> str | None:
    context = browser.new_context(
        timezone_id="Asia/Seoul",
        viewport={"width": scenario.viewport[0], "height": scenario.viewport[1]},
    )
    page = context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT_MS)
    page.set_default_navigation_timeout(15_000)
    diagnostics = attach_page_diagnostics(page)
    label = scenario.name if iteration == 1 else f"{scenario.name}#{iteration}"
    try:
        scenario.check(page, base_url)
        if diagnostics.runtime_errors:
            raise RuntimeError("browser runtime errors detected:\n" + "\n".join(diagnostics.runtime_errors))
        print(f"ui-check {label}: passed")
        return None
    except Exception:
        details = page_failure_state(page, diagnostics)
        return f"[{label}]\n{traceback.format_exc().rstrip()}\n{details}"
    finally:
        context.close()


def run_browser_checks(base_url: str, scenarios: tuple[BrowserScenario, ...], *, repeat: int = 1) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            failures = [
                failure
                for iteration in range(1, repeat + 1)
                for scenario in scenarios
                if (failure := run_scenario(browser, scenario, base_url, iteration=iteration)) is not None
            ]
            if failures:
                raise RuntimeError(f"{len(failures)} UI scenario(s) failed:\n\n" + "\n\n".join(failures))
        finally:
            browser.close()


def run_fixture_checks(scenarios: tuple[BrowserScenario, ...], *, repeat: int = 1) -> None:
    with tempfile.TemporaryDirectory(prefix="codex-token-dashboard-ui-") as tmp:
        codex_dir = pathlib.Path(tmp) / "codex-dir"
        output_dir = pathlib.Path(tmp) / "data"
        copy_dashboard_assets(output_dir)
        write_dashboard_fixture(output_dir)
        port = free_loopback_port()
        base_url = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_dir)
        env[service_paths.OUTPUT_DIR_ENV] = str(output_dir)
        config_home = pathlib.Path(tmp) / "config"
        env["XDG_CONFIG_HOME"] = str(config_home)
        service_paths.write_config(
            {"codex_dir": codex_dir, "output_dir": output_dir},
            config_home / "bola" / "runtime.conf",
        )
        log_path = pathlib.Path(tmp) / "dashboard-server.log"
        with log_path.open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "serve_dashboard.py"), "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(ROOT),
                stdout=log,
                stderr=log,
                env=env,
            )
            try:
                wait_for_server(base_url, process)
                run_browser_checks(base_url, scenarios, repeat=repeat)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Playwright checks against the Codex Token Bola dashboard.")
    parser.add_argument("--url", help="Dashboard base URL for live-server checks. Omit to run an isolated fixture server.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIO_NAMES,
        help="Run only the named scenario. Repeat the option to select more than one.",
    )
    parser.add_argument("--repeat", type=int, default=1, help="Repeat each selected scenario N times.")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    selected_names = set(args.scenario or SCENARIO_NAMES)
    scenarios = tuple(scenario for scenario in SCENARIOS if scenario.name in selected_names)

    try:
        if args.url:
            run_browser_checks(args.url.rstrip("/"), scenarios, repeat=args.repeat)
        else:
            run_fixture_checks(scenarios, repeat=args.repeat)
    except PlaywrightTimeoutError as exc:
        raise SystemExit(f"Playwright check timed out: {exc}") from exc
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print("playwright dashboard check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
