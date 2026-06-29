#!/usr/bin/env python3
"""Minimal Codex Token Bola hook.

This hook only captures raw per-turn evidence. Slow recovery, schema repair,
and subagent aggregation live in ~/.codex/codex-token-bola/scripts/.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

def script_search_dirs() -> list[pathlib.Path]:
    codex_home = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    candidates = [
        pathlib.Path(__file__).resolve().parents[1] / "scripts",
        codex_home / "codex-token-bola" / "scripts",
    ]
    result: list[pathlib.Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            result.append(candidate)
    return result


for scripts_dir in reversed(script_search_dirs()):
    scripts_path = str(scripts_dir)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)

import raw_segments
import service_paths
import transcript_parser


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
BASE_DIR = service_paths.service_root(CODEX_HOME)
STATE_DIR = BASE_DIR / "state"
ERROR_LOG = pathlib.Path(
    os.environ.get("CODEX_TOKEN_USAGE_ERROR_LOG", str(BASE_DIR / "prompt-usage-errors.jsonl"))
).expanduser()
MIGRATION_ERROR_LOG = CODEX_HOME / "codex-token-bola-migration-required.jsonl"
STORE_TEXT = bool_env("CODEX_TOKEN_USAGE_STORE_TEXT", True)
PROMPT_PREVIEW_CHARS = int_env("CODEX_TOKEN_USAGE_PROMPT_PREVIEW_CHARS", 800 if STORE_TEXT else 0)
INSTRUCTION_EXCERPT_CHARS = int_env("CODEX_TOKEN_USAGE_INSTRUCTION_EXCERPT_CHARS", 600 if STORE_TEXT else 0)
HOOK_TAIL_SCAN_BYTES = int_env("CODEX_TOKEN_USAGE_HOOK_TAIL_SCAN_BYTES", 1024 * 1024)
HOOK_FORWARD_SCAN_BYTES = int_env("CODEX_TOKEN_USAGE_HOOK_FORWARD_SCAN_BYTES", 16 * 1024 * 1024)
HOOK_APPEND_LOCK_TIMEOUT_MS = int_env("CODEX_TOKEN_USAGE_HOOK_APPEND_LOCK_TIMEOUT_MS", 500)
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+.#-]*)[^\n]*\n([\s\S]*?)```", re.MULTILINE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hook_response() -> None:
    print(json.dumps({"continue": True, "suppressOutput": True}, separators=(",", ":")))


def safe_append_jsonl(path: pathlib.Path, record: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        return False
    return True


def _base_dir_for_codex_home(codex_home: pathlib.Path | str | None = None) -> pathlib.Path:
    if codex_home is None:
        return BASE_DIR
    return service_paths.service_root(codex_home)


def append_current_segment_jsonl(
    record: dict[str, Any],
    *,
    kind: str,
    source_name: str,
    codex_home: pathlib.Path | str | None = None,
    base_dir: pathlib.Path | str | None = None,
) -> bool:
    base = pathlib.Path(base_dir).expanduser() if base_dir is not None else _base_dir_for_codex_home(codex_home)
    lock_timeout = max(0, HOOK_APPEND_LOCK_TIMEOUT_MS) / 1000
    deadline = time.monotonic() + lock_timeout
    lock_path = raw_segments.raw_segment_lock_path(base)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.025)
        current = raw_segments.ensure_current_segment(base, kind=kind, source_name=source_name)
        return safe_append_jsonl(pathlib.Path(current["path"]), record)
    except (OSError, raw_segments.ManifestError):
        return False
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def append_prompt_usage(
    record: dict[str, Any],
    *,
    codex_home: pathlib.Path | str | None = None,
    base_dir: pathlib.Path | str | None = None,
) -> bool:
    return append_current_segment_jsonl(
        record,
        kind="prompt_usage",
        source_name=raw_segments.PROMPT_RAW_NAME,
        codex_home=codex_home,
        base_dir=base_dir,
    )


def unresolved_transcript_snapshot(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("reason") in {"missing_transcript_path", "transcript_missing"}


def write_json_atomic(path: pathlib.Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(path)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_name(value: str) -> str:
    return sha256_text(value)[:32]


def state_path(session_id: str, turn_id: str) -> pathlib.Path:
    return STATE_DIR / f"{safe_name(session_id + ':' + turn_id)}.json"


def zero_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_KEYS}


def normalize_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return zero_usage()
    return {key: safe_int(value.get(key)) for key in USAGE_KEYS}


def usage_delta(start: dict[str, int], end: dict[str, int]) -> dict[str, Any]:
    usage: dict[str, Any] = {key: safe_int(end.get(key)) - safe_int(start.get(key)) for key in USAGE_KEYS}
    usage["non_cached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    usage["consistency_total_equals_input_plus_output"] = (
        usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    )
    return usage


def usage_from_last_token(snapshot: dict[str, Any]) -> dict[str, Any]:
    return usage_delta(zero_usage(), normalize_usage(snapshot.get("last_token_usage")))


def usage_sum(items: list[dict[str, Any]]) -> dict[str, int]:
    total = zero_usage()
    for item in items:
        usage = normalize_usage(item)
        for key in USAGE_KEYS:
            total[key] += usage[key]
    return total


def read_hook_input() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_parse_error": True, "_raw_len": len(raw)}
    return data if isinstance(data, dict) else {"_unexpected_input_type": type(data).__name__}


def prompt_text(data: dict[str, Any]) -> str:
    value = data.get("prompt", data.get("user_prompt", ""))
    return value if isinstance(value, str) else ""


def prompt_metadata(text: str) -> dict[str, Any]:
    code_blocks = list(CODE_FENCE_RE.finditer(text))
    code_chars = sum(len(match.group(0)) for match in code_blocks)
    code_lines = sum(match.group(0).count("\n") + 1 for match in code_blocks)
    languages = sorted({match.group(1).strip().lower() for match in code_blocks if match.group(1).strip()})
    instruction_text = CODE_FENCE_RE.sub("", text).strip()
    chars = len(text)
    prompt_preview = text if PROMPT_PREVIEW_CHARS < 0 else (text[:PROMPT_PREVIEW_CHARS] if PROMPT_PREVIEW_CHARS > 0 else "")
    instruction_excerpt = instruction_text[:INSTRUCTION_EXCERPT_CHARS] if INSTRUCTION_EXCERPT_CHARS > 0 else ""
    return {
        "prompt_preview": prompt_preview,
        "prompt_preview_chars": len(prompt_preview),
        "prompt_chars": chars,
        "prompt_lines": text.count("\n") + 1 if text else 0,
        "prompt_sha256": sha256_text(text) if text else None,
        "prompt_truncated": len(prompt_preview) < chars,
        "instruction_excerpt": instruction_excerpt,
        "instruction_excerpt_chars": min(len(instruction_text), INSTRUCTION_EXCERPT_CHARS),
        "payload_stats": {
            "code_block_count": len(code_blocks),
            "code_block_chars": code_chars,
            "code_block_lines": code_lines,
            "languages": languages,
            "pasted_text_chars": chars,
            "payload_ratio": round(code_chars / chars, 4) if chars else 0.0,
        },
    }


def assistant_metadata(data: dict[str, Any]) -> dict[str, Any]:
    text = data.get("last_assistant_message", "")
    text = text if isinstance(text, str) else ""
    return {
        "assistant_chars": len(text),
        "assistant_lines": text.count("\n") + 1 if text else 0,
        "assistant_sha256": sha256_text(text) if text else None,
    }


def safe_hook_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(data.keys()),
        "hook_event_name": data.get("hook_event_name"),
        "session_id": data.get("session_id"),
        "turn_id": data.get("turn_id"),
        "transcript_path": data.get("transcript_path"),
        "cwd": data.get("cwd"),
        "model": data.get("model"),
        "permission_mode": data.get("permission_mode"),
        "has_prompt": "prompt" in data or "user_prompt" in data,
        "has_last_assistant_message": "last_assistant_message" in data,
    }


def latest_token_usage(transcript_path: str | None, offset: int | None = None, max_bytes: int | None = None) -> dict[str, Any]:
    scan_limit = None
    scan_start = offset if isinstance(offset, int) and offset >= 0 else 0
    if isinstance(max_bytes, int) and max_bytes > 0:
        scan_limit = scan_start + max_bytes
    stream, error = transcript_parser.transcript_event_stream(transcript_path, offset, scan_limit)
    if error is not None:
        return error

    latest: dict[str, Any] | None = None
    model_calls: list[dict[str, Any]] = []
    event_count = 0
    file_size = stream.file_size
    scan_start = stream.offset or 0
    if scan_limit is not None:
        scan_limit = min(file_size, scan_limit)
    scan_limit_reached = False
    try:
        for event in stream:
            if scan_limit is not None and int(event["line_start"]) >= scan_limit:
                scan_limit_reached = True
                break
            item = event["item"]
            payload = item.get("payload") or {}
            if item.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            event_count += 1
            last_usage = normalize_usage(info.get("last_token_usage"))
            model_calls.append(
                {
                    "index": event_count,
                    "timestamp": item.get("timestamp"),
                    "usage": last_usage,
                    "model_context_window": info.get("model_context_window"),
                }
            )
            latest = {
                "timestamp": item.get("timestamp"),
                "total_token_usage": normalize_usage(info.get("total_token_usage")),
                "last_token_usage": last_usage,
                "model_context_window": info.get("model_context_window"),
            }
    except OSError as exc:
        return {"found": False, "reason": "read_error", "error": repr(exc), "path": str(stream.path)}
    if scan_limit is not None and (scan_limit < file_size or stream.scan_limit_reached):
        scan_limit_reached = True

    if latest is None:
        return {
            "found": False,
            "reason": "scan_limit_reached" if scan_limit_reached else "no_token_count",
            "path": str(stream.path),
            "file_size": file_size,
            "event_count": 0,
            "model_calls": [],
            "parse_error_seen": stream.parse_error_seen,
            "scan_start": scan_start,
            "scan_limit": scan_limit,
            "scan_limit_reached": scan_limit_reached,
        }
    latest.update(
        {
            "found": True,
            "path": str(stream.path),
            "file_size": file_size,
            "event_count": event_count,
            "model_calls": model_calls,
            "parse_error_seen": stream.parse_error_seen,
            "scan_start": scan_start,
            "scan_limit": scan_limit,
            "scan_limit_reached": scan_limit_reached,
        }
    )
    return latest


def latest_token_usage_until_turn_end(
    transcript_path: str | None,
    turn_id: str,
    offset: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    scan_start = offset if isinstance(offset, int) and offset >= 0 else 0
    scan_limit = scan_start + max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else None
    stream, error = transcript_parser.transcript_event_stream(transcript_path, offset, scan_limit)
    if error is not None:
        return error

    latest: dict[str, Any] | None = None
    model_calls: list[dict[str, Any]] = []
    event_count = 0
    file_size = stream.file_size
    if scan_limit is not None:
        scan_limit = min(file_size, scan_limit)
    terminal_event: dict[str, Any] | None = None
    scan_limit_reached = False
    try:
        for event in stream:
            line_start = int(event["line_start"])
            next_offset = int(event["next_offset"])
            if scan_limit is not None and line_start >= scan_limit:
                scan_limit_reached = True
                break
            item = event["item"]
            if item.get("type") != "event_msg":
                continue
            payload = item.get("payload") or {}
            payload_type = payload.get("type")
            if payload_type in {"task_complete", "task_aborted", "turn_aborted"} and payload.get("turn_id") == turn_id:
                terminal_event = {
                    "type": payload_type,
                    "timestamp": item.get("timestamp"),
                    "turn_id": payload.get("turn_id"),
                    "reason": payload.get("reason"),
                    "completed_at": payload.get("completed_at"),
                    "duration_ms": payload.get("duration_ms"),
                    "event_offset": line_start,
                    "bounded_file_offset": next_offset,
                }
                break
            if payload_type != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            event_count += 1
            last_usage = normalize_usage(info.get("last_token_usage"))
            model_calls.append(
                {
                    "index": event_count,
                    "timestamp": item.get("timestamp"),
                    "usage": last_usage,
                    "model_context_window": info.get("model_context_window"),
                }
            )
            latest = {
                "timestamp": item.get("timestamp"),
                "total_token_usage": normalize_usage(info.get("total_token_usage")),
                "last_token_usage": last_usage,
                "model_context_window": info.get("model_context_window"),
            }
    except OSError as exc:
        return {"found": False, "reason": "read_error", "error": repr(exc), "path": str(stream.path)}
    if terminal_event is None and scan_limit is not None and (scan_limit < file_size or stream.scan_limit_reached):
        scan_limit_reached = True

    if scan_limit_reached:
        return {
            "found": bool(latest),
            "reason": "scan_limit_reached",
            "path": str(stream.path),
            "file_size": file_size,
            "event_count": event_count,
            "model_calls": model_calls,
            "parse_error_seen": stream.parse_error_seen,
            "scan_start": scan_start,
            "scan_limit": scan_limit,
            "scan_limit_reached": True,
            **(latest or {}),
        }
    if terminal_event is None:
        return {
            "found": False,
            "reason": "turn_end_not_found",
            "path": str(stream.path),
            "file_size": file_size,
            "event_count": event_count,
            "model_calls": model_calls,
            "parse_error_seen": stream.parse_error_seen,
            "scan_start": scan_start,
            "scan_limit": scan_limit,
            "scan_limit_reached": False,
        }
    if latest is None:
        return {
            "found": False,
            "reason": f"no_token_count_before_{terminal_event['type']}",
            "path": str(stream.path),
            "file_size": file_size,
            "event_count": 0,
            "model_calls": [],
            "turn_end_event": terminal_event,
            "parse_error_seen": stream.parse_error_seen,
            "scan_start": scan_start,
            "scan_limit": scan_limit,
            "scan_limit_reached": False,
        }
    latest.update(
        {
            "found": True,
            "path": str(stream.path),
            "file_size": file_size,
            "event_count": event_count,
            "model_calls": model_calls,
            "turn_end_event": terminal_event,
            "bounded_at_event_type": terminal_event["type"],
            "bounded_at_timestamp": terminal_event["timestamp"],
            "bounded_at_file_offset": terminal_event["bounded_file_offset"],
            "turn_end_event_offset": terminal_event["event_offset"],
            "parse_error_seen": stream.parse_error_seen,
            "scan_start": scan_start,
            "scan_limit": scan_limit,
            "scan_limit_reached": False,
        }
    )
    return latest


def latest_token_usage_tail(transcript_path: str | None, max_bytes: int = HOOK_TAIL_SCAN_BYTES) -> dict[str, Any]:
    if not transcript_path:
        return {"found": False, "reason": "missing_transcript_path"}
    path = pathlib.Path(transcript_path).expanduser()
    if not path.exists():
        return {"found": False, "reason": "transcript_missing", "path": str(path)}
    try:
        file_size = path.stat().st_size
        scan_bytes = min(file_size, max(0, max_bytes))
        scan_start = max(0, file_size - scan_bytes)
        latest: dict[str, Any] | None = None
        event_count = 0
        parse_error_seen = False
        with path.open("rb") as handle:
            handle.seek(scan_start)
            if scan_start > 0:
                handle.readline()
            for raw_line in handle:
                try:
                    item = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parse_error_seen = True
                    continue
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload") or {}
                if item.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                event_count += 1
                latest = {
                    "timestamp": item.get("timestamp"),
                    "total_token_usage": normalize_usage(info.get("total_token_usage")),
                    "last_token_usage": normalize_usage(info.get("last_token_usage")),
                    "model_context_window": info.get("model_context_window"),
                }
    except OSError as exc:
        return {"found": False, "reason": "read_error", "error": repr(exc), "path": str(path)}

    if latest is None:
        return {
            "found": False,
            "reason": "tail_token_count_not_found",
            "path": str(path),
            "file_size": file_size,
            "event_count": 0,
            "parse_error_seen": parse_error_seen,
            "scan_start": scan_start,
            "scan_bytes": scan_bytes,
        }
    latest.update(
        {
            "found": True,
            "path": str(path),
            "file_size": file_size,
            "event_count": event_count,
            "parse_error_seen": parse_error_seen,
            "scan_start": scan_start,
            "scan_bytes": scan_bytes,
        }
    )
    return latest


def transcript_size(transcript_path: str | None) -> int | None:
    if not transcript_path:
        return None
    try:
        return pathlib.Path(transcript_path).expanduser().stat().st_size
    except OSError:
        return None


def unix_or_timestamp_to_iso(value: Any, fallback: Any = None) -> str | None:
    raw_value = value if value is not None else fallback
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return datetime.fromtimestamp(raw_value, timezone.utc).isoformat()
    if isinstance(raw_value, str) and raw_value:
        text = raw_value[:-1] + "+00:00" if raw_value.endswith("Z") else raw_value
        try:
            return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat()
        except ValueError:
            return raw_value
    return None


def task_lifecycle_token_usage(transcript_path: str | None, turn_id: str) -> dict[str, Any]:
    stream, error = transcript_parser.transcript_event_stream(transcript_path)
    if error is not None:
        return error

    started_at: str | None = None
    stopped_at: str | None = None
    status: str | None = None
    in_turn = False
    event_count = 0
    model_calls: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    latest: dict[str, Any] | None = None
    file_size = stream.file_size

    try:
        for event in stream:
            item = event["item"]
            if item.get("type") != "event_msg":
                continue
            payload = item.get("payload") or {}
            payload_type = payload.get("type")
            if payload_type == "task_started" and payload.get("turn_id") == turn_id:
                in_turn = True
                started_at = unix_or_timestamp_to_iso(payload.get("started_at"), item.get("timestamp"))
                continue
            if not in_turn:
                continue
            if payload_type == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                event_count += 1
                last_usage = normalize_usage(info.get("last_token_usage"))
                usages.append(last_usage)
                model_calls.append(
                    {
                        "index": event_count,
                        "timestamp": item.get("timestamp"),
                        "usage": last_usage,
                        "model_context_window": info.get("model_context_window"),
                    }
                )
                latest = {
                    "timestamp": item.get("timestamp"),
                    "last_token_usage": last_usage,
                    "model_context_window": info.get("model_context_window"),
                }
                continue
            if payload.get("turn_id") != turn_id:
                continue
            if payload_type == "task_complete":
                status = "completed"
                stopped_at = unix_or_timestamp_to_iso(payload.get("completed_at"), item.get("timestamp"))
                break
            if payload_type in {"task_aborted", "turn_aborted"}:
                status = "aborted"
                stopped_at = unix_or_timestamp_to_iso(
                    payload.get("aborted_at") or payload.get("completed_at"),
                    item.get("timestamp"),
                )
                break
    except OSError as exc:
        return {"found": False, "reason": "read_error", "error": repr(exc), "path": str(stream.path)}

    if not started_at:
        return {
            "found": False,
            "reason": "task_started_missing",
            "path": str(stream.path),
            "file_size": file_size,
            "event_count": event_count,
            "model_calls": model_calls,
            "parse_error_seen": stream.parse_error_seen,
        }
    if not status:
        return {
            "found": False,
            "reason": "task_terminal_missing",
            "path": str(stream.path),
            "file_size": file_size,
            "event_count": event_count,
            "model_calls": model_calls,
            "parse_error_seen": stream.parse_error_seen,
            "turn_started_at": started_at,
        }

    aggregate_usage = usage_sum(usages)
    snapshot = {
        "found": True,
        "path": str(stream.path),
        "file_size": file_size,
        "event_count": event_count,
        "model_calls": model_calls,
        "parse_error_seen": stream.parse_error_seen,
        "turn_status": status,
        "turn_started_at": started_at,
        "turn_stopped_at": stopped_at or utc_now(),
        "total_token_usage": aggregate_usage,
        "last_token_usage": normalize_usage((latest or {}).get("last_token_usage")),
        "model_context_window": (latest or {}).get("model_context_window"),
        "timestamp": (latest or {}).get("timestamp"),
        "token_source": "transcript_path task lifecycle token_count.info.last_token_usage aggregate",
    }
    return snapshot


def model_call_breakdown(snapshot: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    calls = snapshot.get("model_calls")
    if isinstance(calls, list) and calls:
        return len(calls), calls
    if snapshot.get("found"):
        usage = normalize_usage(snapshot.get("last_token_usage"))
        return (
            1,
            [
                {
                    "index": 1,
                    "timestamp": snapshot.get("timestamp"),
                    "usage": usage,
                    "model_context_window": snapshot.get("model_context_window"),
                }
            ],
        )
    return 0, []


def compact_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    compact = dict(snapshot)
    compact.pop("model_calls", None)
    return compact


def usage_from_model_calls(snapshot: dict[str, Any]) -> dict[str, Any]:
    calls = snapshot.get("model_calls")
    if not isinstance(calls, list):
        return usage_delta(zero_usage(), zero_usage())
    return usage_delta(zero_usage(), usage_sum([call.get("usage") for call in calls if isinstance(call, dict)]))


def base_record(data: dict[str, Any], session_id: str, turn_id: str, transcript_path: str | None) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "record_type": "turn_usage_raw",
        "captured_at": utc_now(),
        "captured_at_ns": time.time_ns(),
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": data.get("cwd"),
        "model": data.get("model"),
        "transcript_path": transcript_path,
        "hook_input": safe_hook_fields(data),
        "token_source": "transcript_path token_count.info.total_token_usage diff",
        "sqlite_token_source_used": False,
        "estimated": False,
    }


def handle_start(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "")
    turn_id = str(data.get("turn_id") or "")
    if not session_id or not turn_id:
        safe_append_jsonl(ERROR_LOG, {"captured_at": utc_now(), "event": "UserPromptSubmit", "error": "missing ids"})
        return

    transcript_path = data.get("transcript_path")
    snapshot = latest_token_usage_tail(transcript_path)
    snapshot_for_state = compact_snapshot(snapshot)
    state = {
        "schema_version": 2,
        "record_type": "turn_start",
        "captured_at": utc_now(),
        "captured_at_ns": time.time_ns(),
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": data.get("cwd"),
        "model": data.get("model"),
        "transcript_path": transcript_path,
        "start_file_size": snapshot.get("file_size"),
        "start_token_usage": normalize_usage(snapshot.get("total_token_usage")) if snapshot.get("found") else zero_usage(),
        "start_usage_source": "tail_token_count" if snapshot.get("found") else "unavailable",
        "start_token_snapshot": snapshot_for_state,
        "prompt": prompt_metadata(prompt_text(data)),
        "hook_input": safe_hook_fields(data),
    }
    try:
        write_json_atomic(state_path(session_id, turn_id), state)
    except OSError as exc:
        safe_append_jsonl(
            ERROR_LOG,
            {
                "captured_at": utc_now(),
                "event": "UserPromptSubmit",
                "error": "state_write_failed",
                "exception": repr(exc),
                "session_id": session_id,
                "turn_id": turn_id,
            },
        )


def log_raw_append_failed(data: dict[str, Any], session_id: str, turn_id: str, record: dict[str, Any]) -> None:
    safe_append_jsonl(
        ERROR_LOG,
        {
            "captured_at": utc_now(),
            "event": str(data.get("hook_event_name") or "Stop"),
            "error": "raw_append_failed",
            "session_id": session_id,
            "turn_id": turn_id,
            "lifecycle_end_reason": record.get("lifecycle_end_reason"),
            "turn_status": record.get("turn_status"),
        },
    )


def missing_start_record(data: dict[str, Any], session_id: str, turn_id: str, transcript_path: str | None) -> dict[str, Any]:
    record = base_record(data, session_id, turn_id, transcript_path)
    record.update(
        {
            "turn_status": "incomplete",
            "lifecycle_end_reason": "missing_start_state",
            "started_at": None,
            "stopped_at": utc_now(),
            "usage": usage_delta(zero_usage(), zero_usage()),
            "start_token_usage": None,
            "end_token_usage": zero_usage(),
            "start_token_snapshot": None,
            "end_token_snapshot": {
                "found": False,
                "reason": "missing_start_state_deferred",
                "path": transcript_path,
            },
            "prompt": prompt_metadata(""),
            "assistant": assistant_metadata(data),
            "model_call_count": 0,
            "estimated": True,
            "start_state_found": False,
            "token_source": "hook: missing start state deferred to normalize lifecycle recovery",
        }
    )
    return record


def stop_missing_start_marker_path(session_id: str, turn_id: str) -> pathlib.Path:
    return STATE_DIR / f"{safe_name('stop:' + session_id + ':' + turn_id)}.json"


def write_stop_missing_start_marker(data: dict[str, Any], session_id: str, turn_id: str, transcript_path: str | None) -> None:
    marker = {
        "schema_version": 2,
        "record_type": "turn_stop_missing_start",
        "captured_at": utc_now(),
        "captured_at_ns": time.time_ns(),
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": data.get("cwd"),
        "model": data.get("model"),
        "transcript_path": transcript_path,
        "stopped_at": utc_now(),
        "assistant": assistant_metadata(data),
        "hook_input": safe_hook_fields(data),
    }
    try:
        write_json_atomic(stop_missing_start_marker_path(session_id, turn_id), marker)
    except OSError as exc:
        safe_append_jsonl(
            ERROR_LOG,
            {
                "captured_at": utc_now(),
                "event": str(data.get("hook_event_name") or "Stop"),
                "error": "stop_marker_write_failed",
                "exception": repr(exc),
                "session_id": session_id,
                "turn_id": turn_id,
            },
        )


def defer_stop_recovery(data: dict[str, Any], session_id: str, turn_id: str, reason: str, detail: dict[str, Any] | None = None) -> None:
    payload = {
        "captured_at": utc_now(),
        "event": str(data.get("hook_event_name") or "Stop"),
        "warning": "deferred_stop_recovery",
        "reason": reason,
        "session_id": session_id,
        "turn_id": turn_id,
    }
    if detail:
        payload.update(detail)
    safe_append_jsonl(ERROR_LOG, payload)


def handle_stop(data: dict[str, Any]) -> None:
    session_id = str(data.get("session_id") or "")
    turn_id = str(data.get("turn_id") or "")
    if not session_id or not turn_id:
        safe_append_jsonl(ERROR_LOG, {"captured_at": utc_now(), "event": "Stop", "error": "missing ids"})
        return

    path = state_path(session_id, turn_id)
    start: dict[str, Any] | None = None
    if path.exists():
        try:
            start = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            safe_append_jsonl(
                ERROR_LOG,
                {
                    "captured_at": utc_now(),
                    "event": "Stop",
                    "error": "bad_start_state",
                    "exception": repr(exc),
                    "state_path": str(path),
                },
            )

    transcript_path = data.get("transcript_path") or (start or {}).get("transcript_path")

    if start is None:
        record = missing_start_record(data, session_id, turn_id, transcript_path)
        if not append_prompt_usage(record):
            log_raw_append_failed(data, session_id, turn_id, record)
            write_stop_missing_start_marker(data, session_id, turn_id, transcript_path)
        return

    start_file_size = start.get("start_file_size")
    transcript_current_size = transcript_size(transcript_path)
    if not isinstance(start_file_size, int) or transcript_current_size is None or start_file_size > transcript_current_size:
        defer_stop_recovery(
            data,
            session_id,
            turn_id,
            "invalid_start_file_size",
            {
                "transcript_path": transcript_path,
                "start_file_size": start_file_size,
                "transcript_size": transcript_current_size,
                "state_path": str(path),
            },
        )
        return

    end_snapshot = latest_token_usage_until_turn_end(transcript_path, turn_id, start_file_size, HOOK_FORWARD_SCAN_BYTES)
    if end_snapshot.get("scan_limit_reached"):
        defer_stop_recovery(
            data,
            session_id,
            turn_id,
            "hook_scan_limit_reached",
            {
                "transcript_path": transcript_path,
                "start_file_size": start_file_size,
                "scan_limit": end_snapshot.get("scan_limit"),
                "state_path": str(path),
            },
        )
        return
    if not end_snapshot.get("found"):
        if end_snapshot.get("reason") == "turn_end_not_found" and end_snapshot.get("model_calls"):
            defer_stop_recovery(
                data,
                session_id,
                turn_id,
                "turn_end_not_found",
                {
                    "transcript_path": transcript_path,
                    "start_file_size": start_file_size,
                    "state_path": str(path),
                    "end_snapshot": end_snapshot,
                },
            )
            return
        if unresolved_transcript_snapshot(end_snapshot):
            safe_append_jsonl(
                ERROR_LOG,
                {
                    "captured_at": utc_now(),
                    "event": "Stop",
                    "warning": "unresolved_transcript_path",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "transcript_path": transcript_path,
                    "start_file_size": start_file_size,
                    "end_snapshot": end_snapshot,
                    "start_state_found": True,
                },
            )
            return
        start_usage = normalize_usage(start.get("start_token_usage"))
        record = base_record(data, session_id, turn_id, transcript_path)
        stopped_at = utc_now()
        record.update(
            {
                "turn_status": "completed",
                "lifecycle_end_reason": "pending_token_count",
                "started_at": start.get("captured_at"),
                "stopped_at": stopped_at,
                "usage": usage_delta(start_usage, start_usage),
                "start_token_usage": start_usage,
                "end_token_usage": start_usage,
                "start_token_snapshot": compact_snapshot(start.get("start_token_snapshot")),
                "end_token_snapshot": compact_snapshot(end_snapshot),
                "prompt": start.get("prompt") or prompt_metadata(""),
                "assistant": assistant_metadata(data),
                "model_call_count": 0,
                "estimated": True,
                "start_state_found": True,
            }
        )
        if append_prompt_usage(record):
            try:
                path.unlink()
            except OSError:
                pass
        else:
            log_raw_append_failed(data, session_id, turn_id, record)
        safe_append_jsonl(
            ERROR_LOG,
            {
                "captured_at": utc_now(),
                "event": "Stop",
                "warning": "missing_post_start_token_count",
                "session_id": session_id,
                "turn_id": turn_id,
                "transcript_path": transcript_path,
                "start_file_size": start_file_size,
                "end_snapshot": end_snapshot,
            },
        )
        return

    start_usage = normalize_usage(start.get("start_token_usage"))
    end_usage = normalize_usage(end_snapshot.get("total_token_usage"))
    model_call_count, model_calls = model_call_breakdown(end_snapshot)
    record = base_record(data, session_id, turn_id, transcript_path)
    stopped_at = utc_now()
    start_usage_source = start.get("start_usage_source")
    if not start_usage_source:
        start_snapshot = start.get("start_token_snapshot")
        start_usage_source = "legacy_full_scan" if isinstance(start_snapshot, dict) and start_snapshot.get("found") else "unavailable"
    start_usage_source = str(start_usage_source)
    usage = usage_delta(start_usage, end_usage)
    estimated = False
    token_source = record["token_source"]
    if start_usage_source == "unavailable":
        usage = usage_from_model_calls(end_snapshot)
        estimated = True
        token_source = "transcript_path token_count.info.last_token_usage aggregate after start offset"
    record.update(
        {
            "turn_status": "completed",
            "lifecycle_end_reason": None,
            "started_at": start.get("captured_at"),
            "stopped_at": stopped_at,
            "usage": usage,
            "start_token_usage": start_usage,
            "end_token_usage": end_usage,
            "start_token_snapshot": compact_snapshot(start.get("start_token_snapshot")),
            "end_token_snapshot": compact_snapshot(end_snapshot),
            "prompt": start.get("prompt") or prompt_metadata(""),
            "assistant": assistant_metadata(data),
            "model_call_count": model_call_count,
            "estimated": estimated,
            "start_state_found": True,
            "token_source": token_source,
        }
    )
    if append_prompt_usage(record):
        try:
            path.unlink()
        except OSError:
            pass
    else:
        log_raw_append_failed(data, session_id, turn_id, record)


def main() -> int:
    data = read_hook_input()
    event = data.get("hook_event_name")
    try:
        service_paths.assert_migrated(CODEX_HOME)
    except service_paths.PathMigrationRequired as exc:
        safe_append_jsonl(
            MIGRATION_ERROR_LOG,
            {"captured_at": utc_now(), "event": event, "error": "path_migration_required", "message": str(exc)},
        )
        hook_response()
        return 0
    try:
        if event == "UserPromptSubmit":
            handle_start(data)
        elif event == "Stop":
            handle_stop(data)
        else:
            safe_append_jsonl(ERROR_LOG, {"captured_at": utc_now(), "event": event, "error": "unsupported event"})
    except Exception as exc:
        safe_append_jsonl(ERROR_LOG, {"captured_at": utc_now(), "event": event, "error": repr(exc)})
    hook_response()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
