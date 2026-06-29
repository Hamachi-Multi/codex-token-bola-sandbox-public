"""Cooperative cancellation helpers for token usage analysis jobs."""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any


CANCEL_ENV = "CODEX_TOKEN_USAGE_CANCEL_FILE"
CANCEL_EXIT_CODE = 130


class Cancelled(RuntimeError):
    def __init__(self, phase: str = "", checkpoint: str = "") -> None:
        self.phase = phase
        self.checkpoint = checkpoint
        message = "analysis cancelled"
        if phase or checkpoint:
            message = f"{message}: {phase or 'unknown'} {checkpoint or ''}".strip()
        super().__init__(message)

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "cancelled": True, "phase": self.phase, "checkpoint": self.checkpoint}


def cancel_file() -> pathlib.Path | None:
    value = os.environ.get(CANCEL_ENV)
    return pathlib.Path(value).expanduser() if value else None


def cancel_requested() -> bool:
    path = cancel_file()
    return bool(path and path.exists())


def check_cancelled(phase: str = "", checkpoint: str = "") -> None:
    if cancel_requested():
        raise Cancelled(phase, checkpoint)


def request_cancel(path: pathlib.Path, *, reason: str = "user") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(
        json.dumps({"cancel_requested_at_unix": time.time(), "reason": reason}, ensure_ascii=False, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)

