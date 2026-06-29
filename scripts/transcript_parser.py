"""Shared Codex transcript JSONL readers."""

from __future__ import annotations

import json
import pathlib
from typing import Any


class TranscriptEventStream:
    def __init__(self, path: pathlib.Path, offset: int | None = None, max_offset: int | None = None) -> None:
        self.path = pathlib.Path(path).expanduser()
        self.file_size = self.path.stat().st_size
        self.offset = offset if isinstance(offset, int) and 0 <= offset <= self.file_size else None
        self.max_offset = max_offset if isinstance(max_offset, int) and max_offset >= 0 else None
        self.parse_error_seen = False
        self.scan_limit_reached = False

    def __iter__(self):
        with self.path.open("rb") as handle:
            if self.offset is not None:
                handle.seek(self.offset)
            while True:
                line_start = handle.tell()
                if self.max_offset is not None and line_start >= self.max_offset:
                    self.scan_limit_reached = True
                    break
                if self.max_offset is None:
                    line_bytes = handle.readline()
                else:
                    remaining = max(0, self.max_offset - line_start)
                    line_bytes = handle.readline(remaining + 1)
                    if handle.tell() > self.max_offset and not line_bytes.endswith(b"\n"):
                        self.scan_limit_reached = True
                        break
                if not line_bytes:
                    break
                next_offset = handle.tell()
                try:
                    line = line_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    self.parse_error_seen = True
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    self.parse_error_seen = True
                    continue
                if isinstance(item, dict):
                    yield {"item": item, "line_start": line_start, "next_offset": next_offset}


def transcript_event_stream(transcript_path: str | pathlib.Path | None, offset: int | None = None, max_offset: int | None = None) -> tuple[TranscriptEventStream | None, dict[str, Any] | None]:
    if not transcript_path:
        return None, {"found": False, "reason": "missing_transcript_path"}
    path = pathlib.Path(transcript_path).expanduser()
    if not path.exists():
        return None, {"found": False, "reason": "transcript_missing", "path": str(path)}
    try:
        return TranscriptEventStream(path, offset, max_offset), None
    except OSError as exc:
        return None, {"found": False, "reason": "read_error", "error": repr(exc), "path": str(path)}
