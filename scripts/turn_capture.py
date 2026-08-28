"""Shared turn capture primitives without runtime path side effects."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import raw_segments

USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
CODE_FENCE_RE = re.compile(r"```([A-Za-z0-9_+.#-]*)[^\n]*\n([\s\S]*?)```", re.MULTILINE)


@dataclass(frozen=True)
class AppendResult:
    ok: bool
    failure_stage: str | None = None
    failure_reason: str | None = None
    error_number: int | None = None

    def __bool__(self) -> bool:
        return self.ok


def _append_failure(stage: str, reason: str, exc: OSError | None = None) -> AppendResult:
    return AppendResult(False, failure_stage=stage, failure_reason=reason, error_number=exc.errno if exc is not None else None)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl_result(path: pathlib.Path, record: dict[str, Any]) -> AppendResult:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _append_failure("append", "append_parent_prepare_failed", exc)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as exc:
        return _append_failure("append", "append_open_failed", exc)
    descriptor: int | None = fd
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            descriptor = None
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as exc:
        return _append_failure("append", "append_write_failed", exc)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return AppendResult(True)


def safe_append_jsonl(path: pathlib.Path, record: dict[str, Any]) -> bool:
    return bool(_append_jsonl_result(path, record))


def append_current_segment_jsonl_result(
    record: dict[str, Any],
    *,
    base_dir: pathlib.Path | str,
    kind: str,
    source_name: str,
    lock_timeout_ms: int = 500,
) -> AppendResult:
    base = pathlib.Path(base_dir).expanduser()
    deadline = time.monotonic() + max(0, lock_timeout_ms) / 1000
    lock_path = raw_segments.raw_segment_lock_path(base)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _append_failure("lock", "lock_parent_prepare_failed", exc)
    fd: int | None = None
    try:
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            return _append_failure("lock", "lock_open_failed", exc)
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return _append_failure("lock", "lock_timeout")
                time.sleep(0.025)
            except OSError as exc:
                return _append_failure("lock", "lock_acquire_failed", exc)
        try:
            current = raw_segments.ensure_current_segment(
                base,
                kind=kind,
                source_name=source_name,
            )
        except raw_segments.ManifestError:
            return _append_failure("segment", "segment_manifest_error")
        except OSError as exc:
            return _append_failure("segment", "segment_io_error", exc)
        return _append_jsonl_result(pathlib.Path(current["path"]), record)
    except OSError as exc:
        return _append_failure("lock", "lock_prepare_failed", exc)
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


def append_current_segment_jsonl(
    record: dict[str, Any],
    *,
    base_dir: pathlib.Path | str,
    kind: str,
    source_name: str,
    lock_timeout_ms: int = 500,
) -> bool:
    return bool(
        append_current_segment_jsonl_result(
            record,
            base_dir=base_dir,
            kind=kind,
            source_name=source_name,
            lock_timeout_ms=lock_timeout_ms,
        )
    )


def append_prompt_usage_result(
    record: dict[str, Any],
    *,
    base_dir: pathlib.Path | str,
    lock_timeout_ms: int = 500,
) -> AppendResult:
    return append_current_segment_jsonl_result(
        record,
        base_dir=base_dir,
        kind="prompt_usage",
        source_name=raw_segments.PROMPT_RAW_NAME,
        lock_timeout_ms=lock_timeout_ms,
    )


def append_prompt_usage(
    record: dict[str, Any],
    *,
    base_dir: pathlib.Path | str,
    lock_timeout_ms: int = 500,
) -> bool:
    return bool(append_prompt_usage_result(record, base_dir=base_dir, lock_timeout_ms=lock_timeout_ms))


def zero_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_KEYS}


def normalize_usage(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {key: safe_int(source.get(key)) for key in USAGE_KEYS}


def usage_delta(start: dict[str, int], end: dict[str, int]) -> dict[str, Any]:
    usage: dict[str, Any] = {key: safe_int(end.get(key)) - safe_int(start.get(key)) for key in USAGE_KEYS}
    usage["non_cached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
    usage["consistency_total_equals_input_plus_output"] = (
        usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    )
    return usage


def usage_sum(items: list[dict[str, Any]]) -> dict[str, int]:
    total = zero_usage()
    for item in items:
        usage = normalize_usage(item)
        for key in USAGE_KEYS:
            total[key] += usage[key]
    return total


def compact_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    compact = dict(snapshot)
    compact.pop("model_calls", None)
    return compact


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_name(value: str) -> str:
    return sha256_text(value)[:32]


def prompt_metadata(text: str, *, preview_chars: int, instruction_excerpt_chars: int) -> dict[str, Any]:
    code_blocks = list(CODE_FENCE_RE.finditer(text))
    code_chars = sum(len(match.group(0)) for match in code_blocks)
    code_lines = sum(match.group(0).count("\n") + 1 for match in code_blocks)
    languages = sorted({match.group(1).strip().lower() for match in code_blocks if match.group(1).strip()})
    instruction_text = CODE_FENCE_RE.sub("", text).strip()
    chars = len(text)
    preview = text if preview_chars < 0 else (text[:preview_chars] if preview_chars > 0 else "")
    excerpt = instruction_text[:instruction_excerpt_chars] if instruction_excerpt_chars > 0 else ""
    return {
        "prompt_preview": preview,
        "prompt_preview_chars": len(preview),
        "prompt_chars": chars,
        "prompt_lines": text.count("\n") + 1 if text else 0,
        "prompt_sha256": sha256_text(text) if text else None,
        "prompt_truncated": len(preview) < chars,
        "instruction_excerpt": excerpt,
        "instruction_excerpt_chars": min(len(instruction_text), instruction_excerpt_chars),
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
