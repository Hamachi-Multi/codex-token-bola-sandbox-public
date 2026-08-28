"""Typed subprocess boundary for invoking bundled runtime commands."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, NoReturn, Protocol

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_lock


class RuntimeCommand(str, Enum):
    RECONCILE = "reconcile.py"
    NORMALIZE = "normalize.py"
    COMPACT = "compact_raw.py"
    BUILD = "build_analytics.py"
    SERVE = "serve_dashboard.py"

    @classmethod
    def from_script_name(cls, name: str) -> "RuntimeCommand":
        try:
            return cls(name)
        except ValueError as exc:
            raise ValueError(f"unsupported runtime command: {name}") from exc


@dataclass(frozen=True)
class ProcessResult:
    command: RuntimeCommand
    exit_code: int
    payload: dict[str, object] | None = None
    stdout: str = ""
    stderr: str = ""
    parse_error: str | None = None


class RuntimeCommandRunner(Protocol):
    def run(
        self,
        command: RuntimeCommand,
        args: list[str],
        *,
        env: Mapping[str, str] | None = None,
        capture_json: bool = True,
    ) -> ProcessResult: ...

    def replace(
        self,
        command: RuntimeCommand,
        args: list[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> NoReturn: ...


def parse_last_json_object(stdout: str) -> tuple[dict[str, object] | None, str | None]:
    saw_nonempty = False
    for line in reversed(stdout.splitlines()):
        if not line.strip():
            continue
        saw_nonempty = True
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, None
    if not saw_nonempty:
        return None, "stdout_empty"
    return None, "json_object_missing"


class SubprocessRuntimeCommandRunner:
    def __init__(self, script_dir: pathlib.Path | None = None, interpreter: str | None = None) -> None:
        self.script_dir = pathlib.Path(script_dir or pathlib.Path(__file__).resolve().parent)
        self.interpreter = interpreter or sys.executable

    def _invocation(
        self,
        command: RuntimeCommand,
        args: list[str],
        env: Mapping[str, str] | None,
    ) -> tuple[list[str], dict[str, str]]:
        merged_env = service_lock.scrub_lock_env(os.environ.copy())
        if env:
            merged_env.update(env)
        argv = [self.interpreter, str(self.script_dir / command.value), *args]
        return argv, merged_env

    def run(
        self,
        command: RuntimeCommand,
        args: list[str],
        *,
        env: Mapping[str, str] | None = None,
        capture_json: bool = True,
    ) -> ProcessResult:
        argv, merged_env = self._invocation(command, args, env)
        pass_fds = service_lock.lock_pass_fds(merged_env)
        if not capture_json:
            return ProcessResult(
                command=command,
                exit_code=subprocess.call(argv, env=merged_env, pass_fds=pass_fds),
            )
        completed = subprocess.run(
            argv,
            env=merged_env,
            text=True,
            capture_output=True,
            pass_fds=pass_fds,
        )
        payload, parse_error = parse_last_json_object(completed.stdout)
        return ProcessResult(
            command=command,
            exit_code=completed.returncode,
            payload=payload,
            stdout=completed.stdout,
            stderr=completed.stderr,
            parse_error=parse_error,
        )

    def replace(
        self,
        command: RuntimeCommand,
        args: list[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> NoReturn:
        argv, merged_env = self._invocation(command, args, env)
        os.execve(self.interpreter, argv, merged_env)
