"""Dashboard-owned Linux process groups with bounded shutdown."""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
from typing import IO, Mapping


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SUPERVISOR_PATH = SCRIPT_DIR / "dashboard_process_supervisor.py"


class ManagedProcess:
    def __init__(self, process: subprocess.Popen[str], kind: str) -> None:
        self.process = process
        self.kind = kind

    @classmethod
    def start(
        cls,
        command: list[str],
        *,
        kind: str,
        cwd: str | pathlib.Path,
        env: Mapping[str, str],
        stdout: IO[str],
        stderr: IO[str],
        lifetime_lock_fd: int | None = None,
    ) -> "ManagedProcess":
        supervisor_command = [
            sys.executable,
            str(SUPERVISOR_PATH),
            "--parent-pid",
            str(os.getpid()),
            "--kind",
            kind,
            "--",
            *command,
        ]
        process = subprocess.Popen(
            supervisor_command,
            cwd=str(cwd),
            text=True,
            stdout=stdout,
            stderr=stderr,
            env=dict(env),
            pass_fds=(lifetime_lock_fd,) if lifetime_lock_fd is not None else (),
            start_new_session=True,
        )
        return cls(process, kind)

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def request_shutdown(self) -> None:
        if self.poll() is None:
            self.process.terminate()

    def terminate_group(self, grace_seconds: float = 2.0) -> str:
        if self.poll() is not None:
            return "completed"
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return "completed"
        try:
            self.wait(timeout=grace_seconds)
            return "terminated"
        except subprocess.TimeoutExpired:
            self.kill_group()
            return "killed"

    def kill_group(self) -> None:
        if self.poll() is not None:
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            self.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
