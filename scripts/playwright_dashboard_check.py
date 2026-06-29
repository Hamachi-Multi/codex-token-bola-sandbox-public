#!/usr/bin/env python3
"""Browser-level checks for the Codex Token Bola dashboard."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from dashboard_fixture_data import write_dashboard_fixture
import service_paths
from playwright_dashboard_cleanup import check_cleanup_short_desktop
from playwright_dashboard_desktop import check_desktop
from playwright_dashboard_mobile import check_mobile

ROOT = pathlib.Path(__file__).resolve().parents[1]


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


def copy_dashboard_assets(codex_home: pathlib.Path) -> None:
    source = ROOT / "assets"
    target = service_paths.service_root(codex_home) / "assets"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def attach_runtime_error_collector(page) -> list[str]:
    errors: list[str] = []

    def on_page_error(exc) -> None:
        errors.append(f"pageerror: {exc}")

    def on_console(message) -> None:
        if message.type == "error":
            errors.append(f"console.error: {message.text}")

    def on_request_failed(request) -> None:
        if request.resource_type not in {"document", "script", "xhr", "fetch"}:
            return
        failure = request.failure or "unknown failure"
        errors.append(f"requestfailed: {request.method} {request.url} {failure}")

    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    page.on("requestfailed", on_request_failed)
    return errors


def run_browser_checks(base_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            runtime_errors = attach_runtime_error_collector(page)
            check_desktop(page, base_url)
            check_cleanup_short_desktop(page, base_url)
            check_mobile(page, base_url)
            if runtime_errors:
                raise RuntimeError("browser runtime errors detected:\n" + "\n".join(runtime_errors))
        finally:
            browser.close()


def run_fixture_checks() -> None:
    with tempfile.TemporaryDirectory(prefix="codex-token-dashboard-ui-") as tmp:
        codex_home = pathlib.Path(tmp) / "codex-home"
        copy_dashboard_assets(codex_home)
        db_path = write_dashboard_fixture(codex_home)
        port = free_loopback_port()
        base_url = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        env["CODEX_TOKEN_USAGE_ANALYTICS_DB"] = str(db_path)
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
                run_browser_checks(base_url)
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
    args = parser.parse_args()

    try:
        if args.url:
            run_browser_checks(args.url.rstrip("/"))
        else:
            run_fixture_checks()
    except PlaywrightTimeoutError as exc:
        raise SystemExit(f"Playwright check timed out: {exc}") from exc

    print("playwright dashboard check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
