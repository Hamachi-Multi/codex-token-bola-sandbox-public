#!/usr/bin/env python3
"""Developer CLI for Codex Token Bola."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_lock
import service_paths
import dashboard_cleanup
import cancel_control
import progress_control
import analysis_inputs


def run_script(name: str, extra_args: list[str], env: dict[str, str] | None = None) -> int:
    script = SCRIPT_DIR / name
    merged_env = service_lock.scrub_lock_env(os.environ.copy())
    if env:
        merged_env.update(env)
    return subprocess.call([sys.executable, str(script), *extra_args], env=merged_env, pass_fds=service_lock.lock_pass_fds(merged_env))


def run_script_json(name: str, extra_args: list[str], env: dict[str, str] | None = None) -> tuple[int, dict[str, object], str, str]:
    script = SCRIPT_DIR / name
    merged_env = service_lock.scrub_lock_env(os.environ.copy())
    if env:
        merged_env.update(env)
    result = subprocess.run(
        [sys.executable, str(script), *extra_args],
        env=merged_env,
        text=True,
        capture_output=True,
        pass_fds=service_lock.lock_pass_fds(merged_env),
    )
    metadata: dict[str, object] = {}
    for line in reversed(result.stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            metadata = parsed
            break
    return result.returncode, metadata, result.stdout, result.stderr


def analytics_db_path(output: str | None) -> pathlib.Path:
    return pathlib.Path(output).expanduser() if output else pathlib.Path(os.environ.get("CODEX_TOKEN_USAGE_ANALYTICS_DB", str(service_paths.service_root() / "analytics" / "token-usage.sqlite"))).expanduser()


def effective_codex_home(codex_home: str | pathlib.Path | None = None) -> pathlib.Path:
    return service_paths.codex_home_path(codex_home)


def token_bola_root(codex_home: str | pathlib.Path | None = None) -> pathlib.Path:
    return service_paths.service_root(codex_home)


def print_path_migration_required(exc: service_paths.PathMigrationRequired) -> None:
    print(
        json.dumps(
            {
                "error": "path_migration_required",
                "legacy": str(exc.legacy),
                "destination": str(exc.destination),
                "message": str(exc),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def ensure_path_migrated(codex_home: str | pathlib.Path | None = None) -> None:
    service_paths.assert_migrated(effective_codex_home(codex_home))


def pipeline_output_path(codex_home: str | None, output: str | None) -> pathlib.Path:
    base = token_bola_root(codex_home)
    if output:
        return dashboard_cleanup.ensure_service_owned_output(base, pathlib.Path(output).expanduser())
    env_output = os.environ.get("CODEX_TOKEN_USAGE_ANALYTICS_DB")
    if env_output:
        return dashboard_cleanup.ensure_service_owned_output(base, pathlib.Path(env_output).expanduser())
    return dashboard_cleanup.ensure_service_owned_output(base, base / "analytics" / "token-usage.sqlite")


def analysis_input_fingerprint(codex_home: str | None = None, state_db: str | None = None) -> str:
    return analysis_inputs.analysis_input_fingerprint(codex_home, state_db)


def parse_cutoff(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def retention_db_path(codex_home: str | None, output: str | None) -> pathlib.Path:
    base = token_bola_root(codex_home)
    if output:
        return dashboard_cleanup.ensure_service_owned_output(base, pathlib.Path(output).expanduser())
    return dashboard_cleanup.ensure_service_owned_output(base, base / "analytics" / "token-usage.sqlite")


def raw_segment_state_checkpoint(base: pathlib.Path) -> dict[str, object]:
    raw_segments = dashboard_cleanup.raw_segments
    state_paths = [
        raw_segments.manifest_path(base),
        raw_segments.current_pointer_path(base),
        raw_segments.pending_rotation_path(base),
        raw_segments.segment_apply_marker_path(base),
    ]
    current_dir = base / "raw" / "current"
    current_files = {path.resolve() for path in current_dir.iterdir()} if current_dir.exists() else set()
    current_file_bytes: dict[pathlib.Path, bytes] = {}
    if current_dir.exists():
        for path in current_dir.iterdir():
            if not path.is_file():
                continue
            try:
                current_file_bytes[path.resolve()] = path.read_bytes()
            except OSError:
                continue
    files: dict[pathlib.Path, bytes | None] = {}
    for path in state_paths:
        try:
            files[path] = path.read_bytes()
        except FileNotFoundError:
            files[path] = None
    return {"files": files, "current_files": current_files, "current_file_bytes": current_file_bytes}


def restore_raw_segment_state_checkpoint(base: pathlib.Path, checkpoint: dict[str, object]) -> None:
    current_files = checkpoint.get("current_files") if isinstance(checkpoint, dict) else set()
    current_dir = base / "raw" / "current"
    if current_dir.exists() and isinstance(current_files, set):
        for path in current_dir.iterdir():
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved not in current_files and path.is_file() and path.stat().st_size > 0:
                return
    files = checkpoint.get("files") if isinstance(checkpoint, dict) else {}
    if isinstance(files, dict):
        for path, content in files.items():
            if not isinstance(path, pathlib.Path):
                continue
            if content is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o600)
    current_file_bytes = checkpoint.get("current_file_bytes") if isinstance(checkpoint, dict) else {}
    if isinstance(current_file_bytes, dict):
        for path, content in current_file_bytes.items():
            if not isinstance(path, pathlib.Path) or not isinstance(content, bytes):
                continue
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o600)
    if current_dir.exists() and isinstance(current_files, set):
        for path in current_dir.iterdir():
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved not in current_files and path.is_file() and path.stat().st_size == 0:
                path.unlink(missing_ok=True)


def read_analytics_metadata(output: str | None) -> dict[str, object]:
    db_path = analytics_db_path(output)
    if not db_path.exists():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            rows = con.execute("select key, value from run_metadata").fetchall()
        except sqlite3.Error:
            return {}
        metadata: dict[str, object] = {}
        for key, value in rows:
            try:
                metadata[str(key)] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                metadata[str(key)] = value
        return metadata
    finally:
        con.close()


def sha256_file(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def hook_install_status(codex_home: pathlib.Path) -> dict[str, object]:
    repo_hook = SCRIPT_DIR.parent / "hooks" / "token-usage.py"
    installed_hook = codex_home / "hooks" / "token-usage.py"
    repo_sha = sha256_file(repo_hook)
    installed_sha = sha256_file(installed_hook)
    return {
        "path": str(installed_hook),
        "exists": installed_hook.exists(),
        "matches_repo": bool(repo_sha and installed_sha and repo_sha == installed_sha),
        "repo_sha256": repo_sha,
        "installed_sha256": installed_sha,
    }


def hooks_json_status(codex_home: pathlib.Path) -> dict[str, object]:
    path = codex_home / "hooks.json"
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
    installed_hook = str(codex_home / "hooks" / "token-usage.py")
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
            "registered": any(installed_hook in command for command in commands),
            "commands": commands,
        }
    return {"path": str(path), "exists": path.exists(), "error": error, "events": events}


def hook_command(destination: pathlib.Path) -> str:
    return f"python3 {shlex.quote(str(destination))}"


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


def merge_hooks_json_registration(codex_home: pathlib.Path, destination: pathlib.Path) -> dict[str, object]:
    path = codex_home / "hooks.json"
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
    command = hook_command(destination)
    updated = False
    for event in ("UserPromptSubmit", "Stop"):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        existing_commands: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("command"):
                existing_commands.append(str(entry.get("command")))
            nested_hooks = entry.get("hooks")
            if isinstance(nested_hooks, list):
                existing_commands.extend(str(nested.get("command")) for nested in nested_hooks if isinstance(nested, dict) and nested.get("command"))
        if not any(existing == command for existing in existing_commands):
            entries.append({"hooks": [{"type": "command", "command": command}]})
            updated = True
    if updated or not path.exists():
        write_text_atomic_owner_only(path, json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", 0o600)
    return {"path": str(path), "updated": updated, "events": hooks_json_status(codex_home)["events"]}


def current_segments_status(codex_home: pathlib.Path) -> dict[str, object]:
    base = token_bola_root(codex_home)
    raw_segments = dashboard_cleanup.raw_segments
    status: dict[str, object] = {}
    try:
        pointer = raw_segments.strict_read_current_pointer(base)
    except raw_segments.ManifestError as exc:
        return {"error": str(exc)}
    for kind, current in sorted(pointer.get("current", {}).items()):
        if not isinstance(current, dict):
            status[str(kind)] = {"error": "current entry is not an object"}
            continue
        try:
            segment = raw_segments.validate_current_segment_entry(base, current, kind=str(kind))
            path = pathlib.Path(str(segment.get("path") or ""))
            rows = 0
            try:
                with path.open("rt", encoding="utf-8") as handle:
                    rows = sum(1 for line in handle if line.strip())
            except OSError:
                rows = 0
            status[str(kind)] = {
                "path": str(path),
                "exists": path.exists(),
                "bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
                "rows": rows,
            }
        except raw_segments.ManifestError as exc:
            status[str(kind)] = {"error": str(exc)}
    return status


def current_analytics_metadata(output: str | None) -> dict[str, object] | None:
    db_path = analytics_db_path(output)
    if not db_path.exists():
        return None
    started = time.monotonic()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {str(row[0]) for row in con.execute("select name from sqlite_master where type='table'")}
        if "tool_call_summaries" in tables:
            tool_call_rows = con.execute("select coalesce(sum(calls),0) from tool_call_summaries").fetchone()[0]
        else:
            tool_call_rows = con.execute("select count(*) from tool_calls").fetchone()[0]
        metadata: dict[str, object] = {
            "output": str(db_path),
            "turn_rows": con.execute("select count(*) from turns").fetchone()[0],
            "tool_call_rows": tool_call_rows,
            "task_rollup_rows": con.execute("select count(*) from task_rollups").fetchone()[0],
            "analysis_mode": "incremental",
            "new_turn_rows": 0,
            "processed_turn_log_rows": 0,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
        return metadata
    except sqlite3.Error:
        return None
    finally:
        con.close()


def int_metadata(metadata: dict[str, object], key: str, default: int = 0) -> int:
    try:
        return int(metadata.get(key, default) or 0)
    except (TypeError, ValueError):
        return default


def rotation_closed_rows(rotation: dict[str, object]) -> int:
    total = 0
    for value in rotation.values():
        if not isinstance(value, dict):
            continue
        closed = value.get("closed_segment")
        if isinstance(closed, dict):
            total += int_metadata(closed, "rows")
    return total


RECOVERY_RECORD_TYPES = {"turn_start", "turn_stop_missing_start"}


def pending_recovery_state_summary(base: pathlib.Path) -> dict[str, object]:
    state_dir = base / "state"
    files: list[str] = []
    try:
        candidates = sorted(state_dir.glob("*.json"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("record_type") in RECOVERY_RECORD_TYPES:
            files.append(str(path))
    return {"pending_state_files": len(files), "pending_state_paths": files[:20], "pending_state_paths_truncated": len(files) > 20}


def error_log_counts(base: pathlib.Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    path = base / "prompt-usage-errors.jsonl"
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return counts
    with handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                counts["invalid_json"] = counts.get("invalid_json", 0) + 1
                continue
            if not isinstance(payload, dict):
                counts["wrong_type"] = counts.get("wrong_type", 0) + 1
                continue
            code = payload.get("error") or payload.get("warning")
            if not code:
                continue
            prefix = "error" if payload.get("error") else "warning"
            key = f"{prefix}:{code}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def analytics_tmp_file_summary(base: pathlib.Path) -> dict[str, object]:
    analytics_dir = base / "analytics"
    files: list[dict[str, object]] = []
    try:
        candidates = sorted(analytics_dir.glob(".token-usage.sqlite.*.tmp"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            stat_result = path.stat()
        except OSError:
            continue
        files.append({"path": str(path), "bytes": stat_result.st_size, "mtime_unix": stat_result.st_mtime})
    return {
        "count": len(files),
        "bytes": sum(int(item["bytes"]) for item in files),
        "files": files[:20],
        "files_truncated": len(files) > 20,
    }


def normalize_pending_publish_summary(base: pathlib.Path) -> dict[str, object]:
    path = base / "normalized" / "normalize-state.json.pending"
    if not path.exists():
        return {"exists": False, "path": str(path), "recovery_required": False}
    summary: dict[str, object] = {"exists": True, "path": str(path), "recovery_required": True}
    try:
        summary["bytes"] = path.stat().st_size
    except OSError:
        summary["bytes"] = None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summary["valid"] = False
        summary["error"] = type(exc).__name__
        return summary
    summary["valid"] = isinstance(payload, dict)
    summary["full_publish"] = bool(payload.get("full_publish")) if isinstance(payload, dict) else False
    return summary


def doctor(args: argparse.Namespace) -> int:
    codex_home = pathlib.Path(args.codex_home).expanduser()
    try:
        ensure_path_migrated(codex_home)
    except service_paths.PathMigrationRequired as exc:
        print_path_migration_required(exc)
        return 2
    base = token_bola_root(codex_home)
    paths = {
        "codex_home": codex_home,
        "service_root": base,
        "normalized_log": base / "normalized" / "prompt-usage.normalized.jsonl",
        "analytics_db": base / "analytics" / "token-usage.sqlite",
        "state_db": codex_home / "state_5.sqlite",
    }
    report = {
        key: {
            "path": str(path),
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() and path.is_file() else None,
        }
        for key, path in paths.items()
    }
    report["installed_hook"] = hook_install_status(codex_home)
    report["runtime"] = {
        "current_segments": current_segments_status(codex_home),
        "hooks_json": hooks_json_status(codex_home),
        "recovery": {**pending_recovery_state_summary(base), "error_log_counts": error_log_counts(base)},
        "normalize_pending_publish": normalize_pending_publish_summary(base),
        "analytics_tmp_files": analytics_tmp_file_summary(base),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def install_hook(args: argparse.Namespace) -> dict[str, object]:
    codex_home = pathlib.Path(args.codex_home or "~/.codex").expanduser()
    source = SCRIPT_DIR.parent / "hooks" / "token-usage.py"
    destination = codex_home / "hooks" / "token-usage.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o700)
        tmp.replace(destination)
        destination.chmod(0o700)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    hooks_json = merge_hooks_json_registration(codex_home, destination)
    return {"installed_hook": str(destination), "source_hook": str(source), "sha256": sha256_file(destination), "hooks_json": hooks_json}


def pipeline(args: argparse.Namespace) -> int:
    ensure_path_migrated(args.codex_home)
    env = {"CODEX_HOME": str(pathlib.Path(args.codex_home).expanduser())} if args.codex_home else None
    effective_output = str(pipeline_output_path(args.codex_home, args.output))
    with service_lock.acquire_service_lock(reason="pipeline", codex_home=args.codex_home) as lock:
        child_env = service_lock.child_lock_env(env, lock.path, lock.fd)
        build_args = ["--output", effective_output]
        if args.state_db:
            build_args.extend(["--state-db", args.state_db])
        for value in args.project_root or []:
            build_args.extend(["--project-root", value])
        cancel_control.check_cancelled("pipeline", "start")
        progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="start", phase_progress=0.0)
        if args.recover:
            reconcile_code = run_script("reconcile.py", [], env=child_env)
            if reconcile_code != 0:
                return reconcile_code
            cancel_control.check_cancelled("pipeline", "after-reconcile")
        pre_analysis_rotate: dict[str, object] = {"skipped": True}
        if not getattr(args, "skip_rotate", False):
            cancel_control.check_cancelled("pipeline", "before-rotate")
            progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="rotate-current", phase_progress=0.02)
            rotate_code, rotate_metadata, rotate_stdout, rotate_stderr = run_script_json("compact_raw.py", ["--rotate-current"], env=child_env)
            pre_analysis_rotate = rotate_metadata
            if rotate_code != 0:
                if rotate_stdout:
                    print(rotate_stdout, end="")
                if rotate_stderr:
                    print(rotate_stderr, end="", file=sys.stderr)
                print(json.dumps({"error": "rotation failed", "pre_analysis_rotate": pre_analysis_rotate}, ensure_ascii=False, separators=(",", ":")))
                return rotate_code
        cancel_control.check_cancelled("pipeline", "after-rotate")
        progress_control.write_progress(phase="normalize", phase_index=0, checkpoint="after-rotate", phase_progress=0.05)
        force_full_after_rotation = bool(getattr(args, "skip_rotate", False))
        if args.incremental:
            normalize_args = [] if force_full_after_rotation else ["--incremental"]
            normalize_code, normalize_metadata, stdout, stderr = run_script_json("normalize.py", normalize_args, env=child_env)
            if normalize_code == cancel_control.CANCEL_EXIT_CODE or normalize_metadata.get("cancelled"):
                print(json.dumps({**normalize_metadata, "pre_analysis_rotate": pre_analysis_rotate}, ensure_ascii=False, separators=(",", ":")))
                return cancel_control.CANCEL_EXIT_CODE
            if normalize_code != 0:
                if stdout:
                    print(stdout, end="")
                if stderr:
                    print(stderr, end="", file=sys.stderr)
                return normalize_code
            cancel_control.check_cancelled("pipeline", "after-normalize")
            progress_control.write_progress(phase="build", phase_index=1, checkpoint="after-normalize", phase_progress=0.0)
            db_metadata = read_analytics_metadata(effective_output)
            input_fingerprint = analysis_input_fingerprint(args.codex_home, args.state_db)
            applied_turns = int_metadata(db_metadata, "applied_normalized_turns_size")
            normalized_turns_size = int_metadata(normalize_metadata, "normalized_turns_size")
            has_unapplied_rows = normalized_turns_size > applied_turns
            has_oversized_applied_offset = applied_turns > normalized_turns_size
            has_context_changes = db_metadata.get("applied_input_fingerprint") != input_fingerprint
            rebuild_reasons: list[str] = []
            if has_oversized_applied_offset:
                rebuild_reasons.append("applied_offset_beyond_normalized_size")
            if has_context_changes:
                rebuild_reasons.append("input_fingerprint_changed")
            if not force_full_after_rotation and normalize_metadata.get("mode") == "incremental" and not has_unapplied_rows and not rebuild_reasons:
                current_metadata = current_analytics_metadata(effective_output)
                if current_metadata is not None:
                    print(json.dumps({"normalize": normalize_metadata, **current_metadata, "applied_input_fingerprint": input_fingerprint, "pre_analysis_rotate": pre_analysis_rotate}, ensure_ascii=False, separators=(",", ":")))
                    return 0
            if force_full_after_rotation or normalize_metadata.get("mode") == "full" or rebuild_reasons:
                effective_build_args = build_args
            else:
                effective_build_args = [
                    *build_args,
                    "--incremental",
                    "--turns-offset",
                    str(applied_turns),
                ]
            build_code, build_metadata, build_stdout, build_stderr = run_script_json("build_analytics.py", effective_build_args, env=child_env)
            if build_code == cancel_control.CANCEL_EXIT_CODE or build_metadata.get("cancelled"):
                print(json.dumps({"normalize": normalize_metadata, **build_metadata, "pre_analysis_rotate": pre_analysis_rotate}, ensure_ascii=False, separators=(",", ":")))
                return cancel_control.CANCEL_EXIT_CODE
            if build_code != 0:
                if build_stdout:
                    print(build_stdout, end="")
                if build_stderr:
                    print(build_stderr, end="", file=sys.stderr)
                return build_code
            if rebuild_reasons:
                build_metadata["analysis_rebuild_reason"] = ",".join(rebuild_reasons)
            print(json.dumps({"normalize": normalize_metadata, **build_metadata, "pre_analysis_rotate": pre_analysis_rotate}, ensure_ascii=False, separators=(",", ":")))
            progress_control.write_progress(phase="refresh", phase_index=2, checkpoint="pipeline-complete", phase_progress=0.2)
            return 0
        normalize_code, normalize_metadata, normalize_stdout, normalize_stderr = run_script_json("normalize.py", [], env=child_env)
        if normalize_code == cancel_control.CANCEL_EXIT_CODE or normalize_metadata.get("cancelled"):
            print(json.dumps({**normalize_metadata, "pre_analysis_rotate": pre_analysis_rotate}, ensure_ascii=False, separators=(",", ":")))
            return cancel_control.CANCEL_EXIT_CODE
        if normalize_code != 0:
            if normalize_stdout:
                print(normalize_stdout, end="")
            if normalize_stderr:
                print(normalize_stderr, end="", file=sys.stderr)
            return normalize_code
        cancel_control.check_cancelled("pipeline", "after-normalize")
        progress_control.write_progress(phase="build", phase_index=1, checkpoint="after-normalize", phase_progress=0.0)
        build_code, build_metadata, build_stdout, build_stderr = run_script_json("build_analytics.py", build_args, env=child_env)
        if build_code == cancel_control.CANCEL_EXIT_CODE or build_metadata.get("cancelled"):
            print(json.dumps({"normalize": normalize_metadata, **build_metadata, "pre_analysis_rotate": pre_analysis_rotate}, ensure_ascii=False, separators=(",", ":")))
            return cancel_control.CANCEL_EXIT_CODE
        if build_code != 0:
            if build_stdout:
                print(build_stdout, end="")
            if build_stderr:
                print(build_stderr, end="", file=sys.stderr)
            return build_code
        print(json.dumps({"normalize": normalize_metadata, **build_metadata, "pre_analysis_rotate": pre_analysis_rotate}, ensure_ascii=False, separators=(",", ":")))
        progress_control.write_progress(phase="refresh", phase_index=2, checkpoint="pipeline-complete", phase_progress=0.2)
        return 0


def retention_prune(args: argparse.Namespace) -> int:
    try:
        cutoff = parse_cutoff(args.cutoff)
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "error": "cutoff_date_invalid",
                    "stage": "preview",
                    "cutoff": args.cutoff,
                    "message": str(exc),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2
    codex_home = effective_codex_home(args.codex_home)
    ensure_path_migrated(codex_home)
    base = token_bola_root(codex_home)
    db_path = retention_db_path(str(codex_home), args.output)
    env = service_lock.scrub_lock_env(os.environ.copy())
    env["CODEX_HOME"] = str(codex_home)
    with service_lock.acquire_service_lock(reason="retention-prune", codex_home=codex_home) as lock:
        child_env = service_lock.child_lock_env(env, lock.path, lock.fd)
        child_env.pop(progress_control.PROGRESS_ENV, None)
        preview_signature = str(getattr(args, "preview_signature", "") or "")
        if not preview_signature:
            print(json.dumps({"error": "cleanup_preview_signature_required", "stage": "preview"}, ensure_ascii=False, separators=(",", ":")))
            return 2
        if dashboard_cleanup.retention_preview_signature(base, cutoff) != preview_signature:
            print(json.dumps({"error": "cleanup_preview_stale", "stage": "preview"}, ensure_ascii=False, separators=(",", ":")))
            return 2
        progress_control.write_progress(phase="cleanup-prepare", phase_index=0, phase_count=4, checkpoint="preflight", phase_progress=0.05)
        dashboard_cleanup.preflight_delete_logs_older_than(base, cutoff)
        progress_control.write_progress(phase="cleanup-prepare", phase_index=0, phase_count=4, checkpoint="checkpoint", phase_progress=0.25)
        checkpoint = raw_segment_state_checkpoint(base)
        try:
            progress_control.write_progress(phase="cleanup-prepare", phase_index=0, phase_count=4, checkpoint="plan-retention", phase_progress=0.55)
            delete_plan = dashboard_cleanup.plan_delete_logs_older_than(base, cutoff)
            planned_rows = int((delete_plan.get("segments") or {}).get("deleted_rows") or 0) + sum(
                int(item.get("deleted_rows") or 0)
                for item in delete_plan.get("untracked", [])
                if isinstance(item, dict)
            )
            progress_control.write_progress(
                phase="cleanup-prepare",
                phase_index=0,
                phase_count=4,
                checkpoint="reset-derived",
                phase_progress=0.9,
                processed=planned_rows,
                total=max(1, planned_rows),
            )
            dashboard_cleanup.validate_delete_logs_older_than_plan(delete_plan)
            reset_result = dashboard_cleanup.reset_derived_outputs(base, db_path)
        except KeyboardInterrupt:
            progress_control.write_progress(phase="cleanup-prepare", phase_index=0, phase_count=4, status="failed", checkpoint="restore-checkpoint", phase_progress=0.0)
            restore_raw_segment_state_checkpoint(base, checkpoint)
            dashboard_cleanup.write_cleanup_retention_job(
                base,
                {
                    "phase": "failed",
                    "failed_stage": "interrupted",
                    "recovery_required": True,
                    "derived_rebuild_required": False,
                    "physical_delete_pending": False,
                    "cutoff_unix": cutoff,
                    "deleted_rows": 0,
                },
            )
            return 130
        except Exception:
            progress_control.write_progress(phase="cleanup-prepare", phase_index=0, phase_count=4, status="failed", checkpoint="restore-checkpoint", phase_progress=0.0)
            restore_raw_segment_state_checkpoint(base, checkpoint)
            raise
        delete_result: dict[str, Any] | None = None
        try:
            progress_control.write_progress(
                phase="cleanup-delete",
                phase_index=1,
                phase_count=4,
                checkpoint="apply-retention",
                phase_progress=0.05,
                processed=0,
                total=max(1, planned_rows),
            )
            delete_result = dashboard_cleanup.apply_delete_logs_older_than_plan(delete_plan)
            progress_control.write_progress(
                phase="cleanup-delete",
                phase_index=1,
                phase_count=4,
                checkpoint="retention-applied",
                phase_progress=0.9,
                processed=int(delete_result.get("deleted_rows") or 0),
                total=max(1, int(delete_result.get("scanned_rows") or 0)),
            )
            dashboard_cleanup.write_cleanup_retention_job(
                base,
                {
                    "phase": "derived_rebuild_required",
                    "cutoff_unix": cutoff,
                    "deleted_rows": delete_result["deleted_rows"],
                    "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                    "pending_files": int(delete_result.get("pending_files") or 0),
                    "delete": delete_result,
                },
            )
            progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, checkpoint="normalize", phase_progress=0.0)
            normalize_code, normalize_result, normalize_stdout, normalize_stderr = run_script_json("normalize.py", [], env=child_env)
            if normalize_code != 0:
                progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, status="failed", checkpoint="normalize-failed", phase_progress=0.1)
                dashboard_cleanup.write_cleanup_retention_job(
                    base,
                    {
                        "phase": "failed",
                        "failed_stage": "normalize",
                        "derived_rebuild_required": True,
                        "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                        "pending_files": int(delete_result.get("pending_files") or 0),
                        "cutoff_unix": cutoff,
                        "deleted_rows": delete_result["deleted_rows"],
                    },
                )
                if normalize_stdout:
                    print(normalize_stdout, end="")
                if normalize_stderr:
                    print(normalize_stderr, end="", file=sys.stderr)
                print(
                    json.dumps(
                        {
                            "error": "retention_rebuild_failed",
                            "stage": "normalize",
                            "partial_mutation": True,
                            "recovery_required": True,
                            "derived_rebuild_required": True,
                            "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                            "pending_files": int(delete_result.get("pending_files") or 0),
                            "deleted_rows": delete_result["deleted_rows"],
                            "delete": delete_result,
                            "reset": reset_result,
                            "normalize": normalize_result,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return normalize_code
            progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, checkpoint="build", phase_progress=0.45)
            build_code, build_result, build_stdout, build_stderr = run_script_json("build_analytics.py", ["--output", str(db_path)], env=child_env)
            if build_code != 0:
                progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, status="failed", checkpoint="build-failed", phase_progress=0.6)
                dashboard_cleanup.write_cleanup_retention_job(
                    base,
                    {
                        "phase": "failed",
                        "failed_stage": "build",
                        "derived_rebuild_required": True,
                        "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                        "pending_files": int(delete_result.get("pending_files") or 0),
                        "cutoff_unix": cutoff,
                        "deleted_rows": delete_result["deleted_rows"],
                    },
                )
                if build_stdout:
                    print(build_stdout, end="")
                if build_stderr:
                    print(build_stderr, end="", file=sys.stderr)
                print(
                    json.dumps(
                        {
                            "error": "retention_rebuild_failed",
                            "stage": "build",
                            "partial_mutation": True,
                            "recovery_required": True,
                            "derived_rebuild_required": True,
                            "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                            "pending_files": int(delete_result.get("pending_files") or 0),
                            "deleted_rows": delete_result["deleted_rows"],
                            "delete": delete_result,
                            "reset": reset_result,
                            "normalize": normalize_result,
                            "build": build_result,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return build_code
        except KeyboardInterrupt:
            progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, status="failed", checkpoint="interrupted", phase_progress=0.0)
            pending_files = int((delete_result or {}).get("pending_files") or 0)
            deleted_rows = int((delete_result or {}).get("deleted_rows") or 0)
            dashboard_cleanup.write_cleanup_retention_job(
                base,
                {
                    "phase": "failed",
                    "failed_stage": "interrupted",
                    "derived_rebuild_required": True,
                    "recovery_required": True,
                    "physical_delete_pending": bool((delete_result or {}).get("physical_delete_pending")),
                    "pending_files": pending_files,
                    "cutoff_unix": cutoff,
                    "deleted_rows": deleted_rows,
                },
            )
            return 130
        progress_control.write_progress(phase="cleanup-rebuild", phase_index=2, phase_count=4, checkpoint="rebuild-complete", phase_progress=1.0)
        if bool(delete_result.get("physical_delete_pending")):
            dashboard_cleanup.write_cleanup_retention_job(
                base,
                {
                    "phase": "physical_delete_pending",
                    "physical_delete_pending": True,
                    "derived_rebuild_required": False,
                    "pending_files": int(delete_result.get("pending_files") or 0),
                    "cutoff_unix": cutoff,
                    "deleted_rows": delete_result["deleted_rows"],
                },
            )
        else:
            dashboard_cleanup.clear_cleanup_retention_job(base)
        print(
            json.dumps(
                {
                    "deleted_rows": delete_result["deleted_rows"],
                    "physical_delete_pending": bool(delete_result.get("physical_delete_pending")),
                    "pending_files": int(delete_result.get("pending_files") or 0),
                    "delete": delete_result,
                    "reset": reset_result,
                    "normalize": normalize_result,
                    "build": build_result,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0


def migrate_path(args: argparse.Namespace) -> int:
    codex_home = effective_codex_home(args.codex_home)
    plan = service_paths.migration_plan(codex_home)
    if plan.action == "conflict":
        print(json.dumps({"error": "path_migration_conflict", **plan.as_dict()}, ensure_ascii=False, separators=(",", ":")))
        return 2
    if plan.action == "noop":
        print(json.dumps({"migrated": False, **plan.as_dict()}, ensure_ascii=False, separators=(",", ":")))
        return 0
    if not args.apply:
        print(json.dumps({"migrated": False, "dry_run": True, **plan.as_dict()}, ensure_ascii=False, separators=(",", ":")))
        return 0
    with service_lock.acquire_service_lock(lock_path=plan.legacy / "state" / "service.lock", reason="migrate-path"):
        refreshed = service_paths.migration_plan(codex_home)
        if refreshed.action == "conflict":
            print(json.dumps({"error": "path_migration_conflict", **refreshed.as_dict()}, ensure_ascii=False, separators=(",", ":")))
            return 2
        if refreshed.action == "noop":
            print(json.dumps({"migrated": False, **refreshed.as_dict()}, ensure_ascii=False, separators=(",", ":")))
            return 0
        refreshed.destination.parent.mkdir(parents=True, exist_ok=True)
        refreshed.legacy.replace(refreshed.destination)
    completed = service_paths.migration_plan(codex_home)
    print(
        json.dumps(
            {
                "migrated": True,
                "legacy": str(plan.legacy),
                "destination": str(plan.destination),
                "post_migration": completed.as_dict(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Token Bola capture, analytics, and dashboard commands.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("reconcile", help="Recover pending turns from saved hook state.")
    sub.add_parser("normalize", help="Normalize raw JSONL logs.")
    sub.add_parser("compact", help="Rotate current raw segments with pointer handoff.")

    build = sub.add_parser("build", help="Build the SQLite analytics database.")
    build.add_argument("--normalized-log")
    build.add_argument("--state-db")
    build.add_argument("--output")
    build.add_argument("--project-root", action="append")

    serve = sub.add_parser("serve", help="Serve the local dashboard.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default="8766")
    serve.add_argument("--allow-network", action="store_true", help="Allow binding the dashboard to a non-loopback host.")

    pipe = sub.add_parser("pipeline", help="Run normalize and analytics build; add --recover to recover pending turns first.")
    pipe.add_argument("--codex-home")
    pipe.add_argument("--state-db")
    pipe.add_argument("--output")
    pipe.add_argument("--project-root", action="append")
    pipe.add_argument("--incremental", action="store_true")
    pipe.add_argument("--recover", action="store_true", help="Recover pending turns before analysis.")
    pipe.add_argument("--skip-rotate", action="store_true", help="Do not rotate current raw segments before analysis.")

    retention = sub.add_parser("retention-prune", help="Delete service data older than a cutoff and rebuild analytics.")
    retention.add_argument("--cutoff", required=True, help="ISO timestamp or unix seconds cutoff.")
    retention.add_argument("--codex-home")
    retention.add_argument("--output")
    retention.add_argument("--preview-signature")

    doc = sub.add_parser("doctor", help="Print local file and database availability.")
    doc.add_argument("--codex-home", default="~/.codex")

    install = sub.add_parser("install-hook", help="Install the repository hook into a Codex home.")
    install.add_argument("--codex-home", default="~/.codex")

    migrate = sub.add_parser("migrate-path", help="Move legacy ~/.codex/token-usage data to ~/.codex/codex-token-bola.")
    migrate.add_argument("--codex-home", default="~/.codex")
    migrate.add_argument("--apply", action="store_true", help="Move the legacy service directory. Omit for dry-run.")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def reject_unknown(parser: argparse.ArgumentParser, unknown: list[str]) -> None:
    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")


def main() -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args()
    try:
        if args.command == "reconcile":
            ensure_path_migrated()
            with service_lock.acquire_service_lock(reason="reconcile") as lock:
                return run_script("reconcile.py", unknown, env=service_lock.child_lock_env(lock_path=lock.path, lock_fd=lock.fd))
        if args.command == "normalize":
            ensure_path_migrated()
            with service_lock.acquire_service_lock(reason="normalize") as lock:
                return run_script("normalize.py", unknown, env=service_lock.child_lock_env(lock_path=lock.path, lock_fd=lock.fd))
        if args.command == "compact":
            ensure_path_migrated()
            with service_lock.acquire_service_lock(reason="compact") as lock:
                return run_script("compact_raw.py", unknown, env=service_lock.child_lock_env(lock_path=lock.path, lock_fd=lock.fd))
        if args.command == "build":
            ensure_path_migrated()
            extra = []
            for name in ("normalized_log", "state_db", "output"):
                value = getattr(args, name)
                if value:
                    extra.extend(["--" + name.replace("_", "-"), value])
            for value in args.project_root or []:
                extra.extend(["--project-root", value])
            with service_lock.acquire_service_lock(reason="build") as lock:
                return run_script("build_analytics.py", [*extra, *unknown], env=service_lock.child_lock_env(lock_path=lock.path, lock_fd=lock.fd))
        if args.command == "serve":
            ensure_path_migrated()
            reject_unknown(parser, unknown)
            extra = ["--host", args.host, "--port", str(args.port)]
            if args.allow_network:
                extra.append("--allow-network")
            return run_script("serve_dashboard.py", extra)
        if args.command == "pipeline":
            reject_unknown(parser, unknown)
            try:
                return pipeline(args)
            except cancel_control.Cancelled as exc:
                print(json.dumps(exc.payload(), ensure_ascii=False, separators=(",", ":")))
                return cancel_control.CANCEL_EXIT_CODE
        if args.command == "retention-prune":
            reject_unknown(parser, unknown)
            return retention_prune(args)
        if args.command == "doctor":
            reject_unknown(parser, unknown)
            return doctor(args)
        if args.command == "install-hook":
            reject_unknown(parser, unknown)
            print(json.dumps(install_hook(args), ensure_ascii=False, separators=(",", ":")))
            return 0
        if args.command == "migrate-path":
            reject_unknown(parser, unknown)
            return migrate_path(args)
    except service_paths.PathMigrationRequired as exc:
        print_path_migration_required(exc)
        return 2
    except service_lock.ServiceLockBusy as exc:
        print(
            json.dumps(
                {"error": "analysis_or_cleanup_running", "lock_path": str(exc.path)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 75
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
