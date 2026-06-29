#!/usr/bin/env python3
"""Rotate current raw token usage segments without rewriting active logs."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import service_lock
import service_paths
import raw_segments

CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
BASE_DIR = service_paths.service_root(CODEX_HOME)

def rotate_current_logs(base: pathlib.Path = BASE_DIR) -> dict[str, object]:
    prompt_base = pathlib.Path(base).expanduser()
    result = raw_segments.rotate_current_segment(base=prompt_base, kind="prompt_usage", source_name=raw_segments.PROMPT_RAW_NAME)
    raw_segments.unlink_empty_closed_segment(prompt_base, result["closed_segment"])
    return {
        "prompt_usage": {
            **result,
        },
        "metadata": {
            "raw_rotation_mode": "current_segment_pointer",
            "last_compacted_at_unix": time.time(),
        },
    }


def compact(args: argparse.Namespace) -> dict[str, object]:
    return rotate_current_logs(BASE_DIR)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rotate current raw segments with pointer handoff.")
    parser.add_argument("--rotate-current", action="store_true", help="Close current raw segments and open new current segments.")
    return parser.parse_args()


def main() -> int:
    service_paths.assert_migrated(CODEX_HOME)
    with service_lock.acquire_service_lock(reason="compact"):
        result = compact(parse_args())
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
