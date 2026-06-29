from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import gzip
import io
import json
import os
import pathlib
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest import mock

__all__ = [
    "Any",
    "DashboardFixtureMixin",
    "ROOT",
    "_raw_segment",
    "_turn_normalized",
    "_turn_raw",
    "argparse",
    "assert_retention_derived_outputs_unchanged",
    "concurrent",
    "dashboard_asset_bundle",
    "datetime",
    "gzip",
    "hashlib",
    "io",
    "json",
    "load_module",
    "mock",
    "os",
    "pathlib",
    "seed_retention_derived_outputs",
    "sqlite3",
    "stat",
    "subprocess",
    "sys",
    "tempfile",
    "time",
    "timezone",
    "types",
    "unittest",
]

ROOT = pathlib.Path(__file__).resolve().parents[1]

def dashboard_asset_bundle() -> str:
    assets = ROOT / "assets"
    dashboard_js = assets / "dashboard.js"
    module_paths = sorted(path for path in (assets / "dashboard").rglob("*.js") if path.is_file())
    paths = [assets / "dashboard.html", assets / "dashboard.css", dashboard_js, *module_paths]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)

def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

def _turn_raw(session_id: str, turn_id: str, total: int) -> dict[str, object]:
    return {
        "schema_version": 2,
        "record_type": "turn_usage_raw",
        "captured_at": "2026-01-01T00:00:00Z",
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": "/example/src/demo",
        "turn_status": "completed",
        "usage": {"input_tokens": total - 10, "cached_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 0, "total_tokens": total},
        "prompt": {"prompt_preview": "분석 테스트", "prompt_chars": 5, "prompt_lines": 1, "payload_stats": {"code_block_chars": 0}},
        "assistant": {"assistant_chars": 0},
        "model_call_count": 1,
        "estimated": False,
    }

def _turn_normalized(session_id: str, turn_id: str, total: int) -> dict[str, object]:
    row = _turn_raw(session_id, turn_id, total)
    row["record_type"] = "turn_usage_normalized"
    row["usage"] = {
        "input_tokens": total - 10,
        "cached_input_tokens": 0,
        "non_cached_input_tokens": total - 10,
        "output_tokens": 10,
        "reasoning_output_tokens": 0,
        "total_tokens": total,
    }
    return row

def _raw_segment(
    path: pathlib.Path,
    *,
    kind: str = "prompt_usage",
    segment_id: str | None = None,
    payload: bytes,
    min_time: float | None,
    max_time: float | None,
    rows: int,
    undated: int = 0,
    corrupt: int = 0,
    unknown: int = 0,
    days: list[list[int]] | None = None,
    sha256: str | None = None,
) -> dict[str, object]:
    source = "prompt-usage.raw.jsonl"
    return {
        "id": segment_id or path.name.removesuffix(".jsonl.gz").removesuffix(".jsonl"),
        "kind": kind,
        "path": str(path),
        "format": "jsonl.gz" if path.name.endswith(".gz") else "jsonl",
        "source_name": source,
        "min_time_unix": min_time,
        "max_time_unix": max_time,
        "rows": rows,
        "undated_rows": undated,
        "corrupt_rows": corrupt,
        "unknown_rows": unknown,
        "days": [] if days is None else days,
        "bytes": path.stat().st_size,
        "uncompressed_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest() if sha256 is None else sha256,
        "status": "closed",
    }

def seed_retention_derived_outputs(base: pathlib.Path) -> dict[str, pathlib.Path]:
    normalized_dir = base / "normalized"
    analytics_dir = base / "analytics"
    normalized_dir.mkdir(parents=True)
    analytics_dir.mkdir(parents=True)
    paths = {
        "prompt": normalized_dir / "prompt-usage.normalized.jsonl",
        "state": normalized_dir / "normalize-state.json",
        "db": analytics_dir / "token-usage.sqlite",
    }
    paths["prompt"].write_text("existing prompt derived\n", encoding="utf-8")
    paths["state"].write_text('{"existing":true}\n', encoding="utf-8")
    paths["db"].write_bytes(b"existing db")
    return paths

def assert_retention_derived_outputs_unchanged(testcase: unittest.TestCase, paths: dict[str, pathlib.Path]) -> None:
    testcase.assertEqual(paths["prompt"].read_text(encoding="utf-8"), "existing prompt derived\n")
    testcase.assertEqual(paths["state"].read_text(encoding="utf-8"), '{"existing":true}\n')
    testcase.assertEqual(paths["db"].read_bytes(), b"existing db")


class DashboardFixtureMixin:
    def _write_dashboard_fixture(self, db_path: pathlib.Path) -> None:
        con = sqlite3.connect(db_path)
        con.executescript(
            """
            create table turns (
              session_id text,
              turn_id text,
              captured_at_unix integer,
              captured_at text,
              cwd text,
              project text,
              thread_name text,
              prompt_preview text,
              turn_status text,
              weighted_credits real,
              total_tokens integer,
              input_tokens integer,
              cached_input_tokens integer,
              non_cached_input_tokens integer,
              output_tokens integer,
              reasoning_output_tokens integer,
              model_call_count integer
            );
            create table model_call_summaries (
              session_id text,
              turn_id text,
              calls integer,
              input_tokens integer,
              cached_input_tokens integer,
              non_cached_input_tokens integer,
              output_tokens integer,
              reasoning_output_tokens integer,
              total_tokens integer,
              weighted_credits real,
              max_total_tokens integer,
              max_output_tokens integer,
              first_call_index integer,
              last_call_index integer
            );
            create table tool_call_summaries (
              session_id text,
              turn_id text,
              tool_name text,
              tool_namespace text,
              calls integer,
              output_chars integer,
              output_reported_tokens integer,
              output_tokens integer,
              failed_calls integer,
              total_duration_ms integer,
              max_duration_ms integer,
              max_output_tokens integer
            );
            create table tool_call_samples (
              session_id text,
              turn_id text,
              call_id text,
              tool_name text,
              tool_namespace text,
              sample_reason text,
              sample_rank integer,
              started_at text,
              completed_at text,
              duration_ms integer,
              output_chars integer,
              output_reported_tokens integer,
              output_tokens integer,
              status text,
              exit_code integer,
              output_preview text
            );
            create table task_rollups (
              parent_session_id text,
              parent_turn_id text,
              confidence text,
              child_total_tokens integer,
              child_weighted_credits real
            );
            """
        )
        con.executemany(
            """
            insert into turns values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("s1", "t1", 1, "2026-01-01T00:00:01+00:00", "/example/src/streaming", "alpha", "zulu", "small prompt", "completed", 1.0, 100, 80, 50, 30, 20, 0, 1),
                ("s2", "t2", 2, "2026-01-01T00:00:02+00:00", "/example/.codex/codex-token-bola", "beta", "", "large prompt", "completed", 9.0, 900, 700, 500, 200, 200, 0, 2),
            ],
        )
        con.executemany(
            "insert into model_call_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("s1", "t1", 1, 80, 50, 30, 20, 0, 100, 1.0, 100, 20, 1, 1),
                ("s2", "t2", 2, 700, 500, 200, 200, 0, 900, 9.0, 500, 100, 1, 2),
            ],
        )
        con.executemany(
            "insert into tool_call_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("s1", "t1", "exec_command", "exec", 1, 40, 0, 10, 0, 10, 10, 10),
                ("s2", "t2", "exec_command", "exec", 1, 400, 0, 100, 0, 20, 20, 100),
            ],
        )
        con.executemany(
            "insert into tool_call_samples values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("s1", "t1", "c1", "exec_command", "exec", "largest_output", 1, None, None, 10, 40, 0, 10, "completed", 0, ""),
                ("s2", "t2", "c2", "exec_command", "exec", "largest_output", 1, None, None, 20, 400, 0, 100, "completed", 0, ""),
            ],
        )
        con.commit()
        con.close()

__all__ = [name for name in globals() if not name.startswith("__")]
