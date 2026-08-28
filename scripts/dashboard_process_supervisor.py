#!/usr/bin/env python3
"""Linux parent-death supervisor for Dashboard-owned command groups."""

from __future__ import annotations

import argparse
import ctypes
import os
import pathlib
import signal
import subprocess
import sys
import time


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cancel_control


PR_SET_PDEATHSIG = 1
ANALYSIS_GRACE_SECONDS = 2.0
CLEANUP_RECOVERY_SECONDS = 10.0
TERM_GRACE_SECONDS = 2.0


def install_parent_death_signal(expected_parent_pid: int) -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("dashboard process supervision requires Linux")
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("dashboard parent exited before supervision started")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("dashboard parent exited while supervision started")


def wait_until_exit(process: subprocess.Popen[object], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return process.poll() is not None


def signal_group(signum: int) -> None:
    try:
        os.killpg(os.getpgrp(), signum)
    except ProcessLookupError:
        return


def shutdown_worker(process: subprocess.Popen[object], kind: str) -> None:
    if process.poll() is not None:
        return
    if kind == "analysis":
        cancel_path = os.environ.get(cancel_control.CANCEL_ENV)
        if cancel_path:
            try:
                cancel_control.request_cancel(pathlib.Path(cancel_path), reason="server_shutdown")
            except OSError:
                pass
        wait_until_exit(process, ANALYSIS_GRACE_SECONDS)
    else:
        signal_group(signal.SIGINT)
        wait_until_exit(process, CLEANUP_RECOVERY_SECONDS)
    if process.poll() is None:
        signal_group(signal.SIGTERM)
        wait_until_exit(process, TERM_GRACE_SECONDS)
    if process.poll() is None:
        signal_group(signal.SIGKILL)


def child_exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + abs(returncode)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--kind", choices=("analysis", "cleanup"), required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a supervised command is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shutdown_requested = False

    def request_shutdown(_signum, _frame) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        install_parent_death_signal(args.parent_pid)
    except (OSError, RuntimeError) as exc:
        print(f"dashboard supervisor setup failed: {exc}", file=sys.stderr)
        return 2
    if shutdown_requested:
        return 143

    process = subprocess.Popen(args.command)
    while process.poll() is None and not shutdown_requested:
        time.sleep(0.05)
    if shutdown_requested:
        shutdown_worker(process, args.kind)
    try:
        return child_exit_code(process.wait())
    except KeyboardInterrupt:
        shutdown_worker(process, args.kind)
        return child_exit_code(process.wait())


if __name__ == "__main__":
    raise SystemExit(main())
