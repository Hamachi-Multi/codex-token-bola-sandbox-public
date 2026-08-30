"""Codex hook registration application service."""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Callable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_paths


@dataclass(frozen=True)
class InstallHookOptions:
    codex_dir: str | pathlib.Path | None = None
    output_dir: str | pathlib.Path | None = None
    persist_config: bool = False


@dataclass(frozen=True)
class InstallHookDependencies:
    resolve_paths: Callable[[str | pathlib.Path | None, str | pathlib.Path | None], service_paths.RuntimePaths]
    validate_codex_dir: Callable[[str | pathlib.Path], None]
    validate_codex_cli: Callable[[], None]
    validate_hook_runtime: Callable[[], None]
    persist_paths: Callable[[dict[str, str | pathlib.Path]], None]


@dataclass(frozen=True)
class InstallHookResult:
    payload: dict[str, object]

def hook_install_status(codex_dir: pathlib.Path) -> dict[str, object]:
    return {
        "module": "codex_token_bola.hook",
        "command": hook_command(),
        "legacy_copy": str(codex_dir / "hooks" / "token-usage.py"),
        "legacy_copy_exists": (codex_dir / "hooks" / "token-usage.py").exists(),
    }


HOOK_MARKER_ARG = "--bola-hook"
LEGACY_HOOK_MARKER_ARG = "--codex-token-bola-hook"


def hook_command() -> str:
    return shlex.join([sys.executable, "-m", "codex_token_bola.hook", HOOK_MARKER_ARG])


def is_owned_hook_command(command: str, codex_dir: pathlib.Path) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if HOOK_MARKER_ARG in argv or LEGACY_HOOK_MARKER_ARG in argv:
        return True
    legacy_hook = (codex_dir / "hooks" / "token-usage.py").resolve(strict=False)
    return (
        len(argv) == 2
        and pathlib.Path(argv[0]).name in {"python", "python3"}
        and pathlib.Path(argv[1]).expanduser().resolve(strict=False) == legacy_hook
    )


def hooks_json_status(codex_dir: pathlib.Path) -> dict[str, object]:
    path = codex_dir / "hooks.json"
    expected_command = hook_command()
    events: dict[str, dict[str, object]] = {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        parsed = {}
        error = None
    except (OSError, json.JSONDecodeError) as exc:
        parsed = {}
        error = repr(exc)
    else:
        error = None
    for event in ("UserPromptSubmit", "Stop"):
        commands: list[str] = []
        roots: list[object] = []
        if isinstance(parsed, dict):
            roots.append(parsed.get(event))
            hooks_root = parsed.get("hooks")
            if isinstance(hooks_root, dict):
                roots.append(hooks_root.get(event))
        for entries in roots:
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                command = str(entry.get("command") or "")
                if command:
                    commands.append(command)
                nested_hooks = entry.get("hooks")
                if isinstance(nested_hooks, list):
                    for nested in nested_hooks:
                        if not isinstance(nested, dict):
                            continue
                        nested_command = str(nested.get("command") or "")
                        if nested_command:
                            commands.append(nested_command)
        events[event] = {
            "registered": expected_command in commands,
            "stale_commands": [command for command in commands if is_owned_hook_command(command, codex_dir) and command != expected_command],
            "commands": commands,
        }
    return {"path": str(path), "exists": path.exists(), "error": error, "events": events}


def write_text_atomic_owner_only(path: pathlib.Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(mode)
        tmp.replace(path)
        path.chmod(mode)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def merge_hooks_json_registration(codex_dir: pathlib.Path) -> dict[str, object]:
    path = codex_dir / "hooks.json"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    hooks = parsed.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        parsed["hooks"] = hooks
    command = hook_command()
    updated = False
    for event in ("UserPromptSubmit", "Stop"):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        retained_entries: list[object] = []
        command_present = False
        for entry in entries:
            if not isinstance(entry, dict):
                retained_entries.append(entry)
                continue
            direct_command = str(entry.get("command") or "")
            if direct_command and is_owned_hook_command(direct_command, codex_dir):
                if direct_command == command:
                    command_present = True
                    retained_entries.append(entry)
                else:
                    updated = True
                continue
            nested_hooks = entry.get("hooks")
            if isinstance(nested_hooks, list):
                retained_nested: list[object] = []
                for nested in nested_hooks:
                    nested_command = str(nested.get("command") or "") if isinstance(nested, dict) else ""
                    if nested_command and is_owned_hook_command(nested_command, codex_dir):
                        if nested_command == command:
                            command_present = True
                            retained_nested.append(nested)
                        else:
                            updated = True
                    else:
                        retained_nested.append(nested)
                if retained_nested:
                    copied = dict(entry)
                    copied["hooks"] = retained_nested
                    retained_entries.append(copied)
                elif nested_hooks:
                    updated = True
            else:
                retained_entries.append(entry)
        if not command_present:
            retained_entries.append({"hooks": [{"type": "command", "command": command}]})
            updated = True
        hooks[event] = retained_entries
    if updated or not path.exists():
        write_text_atomic_owner_only(path, json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", 0o600)
    return {"path": str(path), "updated": updated, "events": hooks_json_status(codex_dir)["events"]}


def remove_hooks_json_registration(codex_dir: pathlib.Path) -> dict[str, object]:
    path = codex_dir / "hooks.json"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"path": str(path), "updated": False}
    except (OSError, json.JSONDecodeError) as exc:
        raise service_paths.ConfigurationError(f"cannot update Codex hooks at {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise service_paths.ConfigurationError(f"Codex hooks must be a JSON object: {path}")
    hooks = parsed.get("hooks")
    if not isinstance(hooks, dict):
        return {"path": str(path), "updated": False}
    updated = False
    for event in ("UserPromptSubmit", "Stop"):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        retained_entries: list[object] = []
        for entry in entries:
            if not isinstance(entry, dict):
                retained_entries.append(entry)
                continue
            direct_command = str(entry.get("command") or "")
            if direct_command and is_owned_hook_command(direct_command, codex_dir):
                updated = True
                continue
            nested = entry.get("hooks")
            if not isinstance(nested, list):
                retained_entries.append(entry)
                continue
            retained_nested = [
                item
                for item in nested
                if not (
                    isinstance(item, dict)
                    and str(item.get("command") or "")
                    and is_owned_hook_command(str(item.get("command") or ""), codex_dir)
                )
            ]
            if len(retained_nested) != len(nested):
                updated = True
            if retained_nested:
                copied = dict(entry)
                copied["hooks"] = retained_nested
                retained_entries.append(copied)
        hooks[event] = retained_entries
    if updated:
        write_text_atomic_owner_only(path, json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", 0o600)
    return {"path": str(path), "updated": updated}


def run_install_hook(
    options: InstallHookOptions,
    dependencies: InstallHookDependencies,
) -> InstallHookResult:
    requested_paths = dependencies.resolve_paths(options.codex_dir, options.output_dir)
    dependencies.validate_codex_dir(requested_paths.codex_dir)
    dependencies.validate_codex_cli()
    dependencies.validate_hook_runtime()

    hooks_path = requested_paths.codex_dir / "hooks.json"
    hooks_snapshot = hooks_path.read_bytes() if hooks_path.exists() else None
    try:
        hooks_json = merge_hooks_json_registration(requested_paths.codex_dir)
        if options.persist_config:
            dependencies.persist_paths(
                {
                    "codex_dir": requested_paths.codex_dir,
                    "output_dir": requested_paths.output_dir,
                }
            )
            paths = dependencies.resolve_paths(None, None)
        else:
            paths = requested_paths
    except Exception:
        if hooks_snapshot is None:
            hooks_path.unlink(missing_ok=True)
        else:
            write_text_atomic_owner_only(hooks_path, hooks_snapshot.decode("utf-8"), 0o600)
        raise

    return InstallHookResult(
        payload={
            "installed_hook": "codex_token_bola.hook",
            "command": hook_command(),
            "interpreter": sys.executable,
            "runtime_paths": paths.as_dict(),
            "hooks_json": hooks_json,
        }
    )
