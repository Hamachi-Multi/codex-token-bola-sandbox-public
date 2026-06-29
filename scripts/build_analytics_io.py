"""Input file helpers for analytics builds."""

from __future__ import annotations

import gzip
import json
import pathlib
import sys
from datetime import datetime
from typing import Any, Callable, Iterator


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cancel_control


CancelChecker = Callable[[str, str], None]


def iter_jsonl(path: pathlib.Path, cancel_checker: CancelChecker = cancel_control.check_cancelled) -> Iterator[dict[str, Any]] | None:
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            cancel_checker("build", f"read:{path.name}:{line_no}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def iter_jsonl_from_offset(
    path: pathlib.Path,
    offset: int,
    cancel_checker: CancelChecker = cancel_control.check_cancelled,
) -> Iterator[dict[str, Any]] | None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        for line_no, line in enumerate(handle, 1):
            cancel_checker("build", f"read:{path.name}:{line_no}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def file_size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def iter_jsonl_chain(paths: list[pathlib.Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        for row in iter_jsonl(path) or []:
            yield row


def parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
