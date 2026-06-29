"""Progress snapshot helpers for token usage analysis jobs."""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any


PROGRESS_ENV = "CODEX_TOKEN_USAGE_PROGRESS_FILE"
DEFAULT_PHASE_COUNT = 3
DEFAULT_WRITE_THROTTLE_SECONDS = 0.25
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_LAST_WRITE_BY_PATH: dict[str, tuple[float, str]] = {}
PHASE_RANGES = {
    "normalize": (0.0, 12.0),
    "build": (12.0, 98.0),
    "refresh": (98.0, 100.0),
    "cleanup-prepare": (0.0, 15.0),
    "cleanup-delete": (15.0, 70.0),
    "cleanup-rebuild": (70.0, 95.0),
    "cleanup-refresh": (95.0, 100.0),
}


def progress_file() -> pathlib.Path | None:
    value = os.environ.get(PROGRESS_ENV)
    return pathlib.Path(value).expanduser() if value else None


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def progress_payload(
    *,
    status: str = "running",
    phase: str = "",
    phase_index: int = 0,
    phase_count: int = DEFAULT_PHASE_COUNT,
    checkpoint: str = "",
    phase_progress: float | None = None,
    overall_progress: float | None = None,
    processed: int | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    if phase_progress is None and processed is not None and total:
        phase_progress = processed / max(1, total)
    phase_progress = clamp(float(phase_progress or 0.0))
    phase_index = max(0, min(max(0, phase_count - 1), int(phase_index)))
    if overall_progress is None:
        if status == "completed":
            overall_progress = 100.0
        else:
            start, end = PHASE_RANGES.get(phase, (0.0, 100.0))
            overall_progress = start + (end - start) * phase_progress
    return {
        "status": status,
        "running": status == "running",
        "phase": phase,
        "phase_index": phase_index,
        "phase_count": phase_count,
        "checkpoint": checkpoint,
        "phase_progress": round(phase_progress, 4),
        "overall_progress": round(clamp(overall_progress, 0.0, 100.0), 2),
        "processed": processed,
        "total": total,
        "updated_at_unix": time.time(),
    }


def should_write_progress_snapshot(path: pathlib.Path, payload: dict[str, Any], *, now: float) -> bool:
    path_key = str(path)
    status = str(payload.get("status") or "")
    phase = str(payload.get("phase") or "")
    previous = _LAST_WRITE_BY_PATH.get(path_key)
    if previous is None:
        return True
    previous_written_at, previous_phase = previous
    if status in TERMINAL_STATUSES:
        return True
    if phase != previous_phase:
        return True
    return now - previous_written_at >= DEFAULT_WRITE_THROTTLE_SECONDS


def write_progress_to_path(path: pathlib.Path, **kwargs: Any) -> dict[str, Any] | None:
    payload = progress_payload(**kwargs)
    now = time.monotonic()
    if not should_write_progress_snapshot(path, payload, now=now):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)
    _LAST_WRITE_BY_PATH[str(path)] = (now, str(payload.get("phase") or ""))
    return payload


def write_progress(**kwargs: Any) -> dict[str, Any] | None:
    path = progress_file()
    if path is None:
        return None
    return write_progress_to_path(path, **kwargs)


def read_progress(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return progress_payload(status="idle", phase="", checkpoint="")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return progress_payload(status="unknown", phase="", checkpoint="")
    return parsed if isinstance(parsed, dict) else progress_payload(status="unknown", phase="", checkpoint="")
