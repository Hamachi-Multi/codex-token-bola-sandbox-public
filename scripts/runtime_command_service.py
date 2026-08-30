"""Application services for direct runtime command invocation."""

from __future__ import annotations

import os
import pathlib
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, NoReturn

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_lock
import service_paths
from runtime_command_runner import RuntimeCommand


@dataclass(frozen=True)
class RuntimeCommandOptions:
    command: RuntimeCommand
    codex_dir: str | pathlib.Path | None = None
    output_dir: str | pathlib.Path | None = None
    arguments: tuple[str, ...] = ()
    lock: bool = True


@dataclass(frozen=True)
class RuntimeCommandResult:
    exit_code: int


@dataclass(frozen=True)
class RuntimeCommandDependencies:
    resolve_paths: Callable[
        [str | pathlib.Path | None, str | pathlib.Path | None],
        service_paths.RuntimePaths,
    ]
    run_command: Callable[..., int]


@dataclass(frozen=True)
class ServeDependencies:
    resolve_paths: Callable[
        [str | pathlib.Path | None, str | pathlib.Path | None],
        service_paths.RuntimePaths,
    ]
    require_runtime_config: Callable[[], Mapping[str, object]]
    replace_command: Callable[..., NoReturn]


@dataclass(frozen=True)
class BuildOptions:
    codex_dir: str | pathlib.Path | None = None
    output_dir: str | pathlib.Path | None = None
    normalized_log: str | None = None
    state_db: str | None = None
    project_roots: tuple[str, ...] = ()
    extra_arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServeOptions:
    host: str
    port: int
    codex_dir: str | pathlib.Path | None = None
    output_dir: str | pathlib.Path | None = None
    pin_runtime_paths: bool = False


def runtime_environment(paths: service_paths.RuntimePaths) -> dict[str, str]:
    return {
        "CODEX_HOME": str(paths.codex_dir),
        service_paths.OUTPUT_DIR_ENV: str(paths.output_dir),
    }


def run_runtime_command(
    options: RuntimeCommandOptions,
    dependencies: RuntimeCommandDependencies,
) -> RuntimeCommandResult:
    paths = dependencies.resolve_paths(options.codex_dir, options.output_dir)
    environment = runtime_environment(paths)
    if not options.lock:
        code = dependencies.run_command(options.command.value, list(options.arguments), env=environment)
        return RuntimeCommandResult(exit_code=code)

    reason = options.command.name.lower()
    with service_lock.acquire_service_lock(reason=reason, output_dir=paths.output_dir) as lock:
        child_environment = service_lock.child_lock_env(environment, lock_path=lock.path, lock_fd=lock.fd)
        code = dependencies.run_command(options.command.value, list(options.arguments), env=child_environment)
    return RuntimeCommandResult(exit_code=code)


def run_build(options: BuildOptions, dependencies: RuntimeCommandDependencies) -> RuntimeCommandResult:
    arguments: list[str] = []
    for name, value in (
        ("normalized-log", options.normalized_log),
        ("state-db", options.state_db),
    ):
        if value:
            arguments.extend((f"--{name}", value))
    for project_root in options.project_roots:
        arguments.extend(("--project-root", project_root))
    arguments.extend(options.extra_arguments)
    return run_runtime_command(
        RuntimeCommandOptions(
            command=RuntimeCommand.BUILD,
            codex_dir=options.codex_dir,
            output_dir=options.output_dir,
            arguments=tuple(arguments),
        ),
        dependencies,
    )


def run_serve(options: ServeOptions, dependencies: ServeDependencies) -> NoReturn:
    dependencies.require_runtime_config()
    paths = dependencies.resolve_paths(options.codex_dir, options.output_dir)
    arguments = ["--host", options.host, "--port", str(options.port)]
    if options.codex_dir is not None:
        arguments.extend(("--codex-dir", str(paths.codex_dir)))
    if options.output_dir is not None:
        arguments.extend(("--output-dir", str(paths.output_dir)))
    if options.pin_runtime_paths:
        arguments.append("--pin-runtime-paths")
    environment = service_lock.scrub_lock_env(os.environ.copy())
    dependencies.replace_command(RuntimeCommand.SERVE.value, arguments, env=environment)
