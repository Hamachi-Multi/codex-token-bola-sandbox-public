"""Deterministic dashboard fixture data for browser checks."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_analytics_schema import setup_db
import raw_segments
import service_paths


def _iso(unix_time: float) -> str:
    return datetime.fromtimestamp(unix_time, timezone.utc).isoformat()


def _raw_turn(session_id: str, turn_id: str, captured_at: float, total_tokens: int) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "record_type": "turn_usage_raw",
        "captured_at": _iso(captured_at),
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": "/example/src/fixture",
        "turn_status": "completed",
        "usage": {
            "input_tokens": total_tokens - 80,
            "cached_input_tokens": 30,
            "output_tokens": 80,
            "reasoning_output_tokens": 0,
            "total_tokens": total_tokens,
        },
        "prompt": {
            "prompt_preview": "fixture prompt",
            "prompt_chars": 14,
            "prompt_lines": 1,
            "payload_stats": {"code_block_chars": 0},
        },
        "assistant": {"assistant_chars": 180},
        "model_call_count": 1,
        "estimated": False,
    }


def _write_jsonl(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def write_dashboard_fixture(codex_home: pathlib.Path, *, now_unix: float | None = None) -> pathlib.Path:
    """Create a deterministic token-usage fixture under ``codex_home``."""
    now = float(now_unix if now_unix is not None else time.time())
    root = service_paths.service_root(codex_home)
    analytics_dir = root / "analytics"
    normalized_dir = root / "normalized"
    raw_dir = root / "raw"
    state_dir = root / "state"
    tmp_dir = root / "tmp"
    bad_dir = root / "bad"
    for directory in (analytics_dir, normalized_dir, raw_dir, state_dir, tmp_dir, bad_dir):
        directory.mkdir(parents=True, exist_ok=True)

    db_path = analytics_dir / "token-usage.sqlite"
    con = sqlite3.connect(db_path)
    setup_db(con)
    turns = []
    for index in range(12):
        captured = now - (index + 1) * 3600
        total = 1200 + index * 75
        cached = 300 + index * 5
        output = 180 + index * 7
        input_tokens = total - output
        non_cached = max(0, input_tokens - cached)
        session_id = "11111111-2222-3333-4444-555555555555" if index < 6 else "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        turns.append(
            (
                session_id,
                f"turn-{index:02d}",
                _iso(captured),
                captured,
                _iso(captured - 45),
                _iso(captured + 15),
                "/example/src/fixture" if index < 6 else "/example/.codex/codex-token-bola",
                "fixture-alpha" if index < 6 else "fixture-beta",
                "Fixture Thread" if index < 6 else "",
                "gpt-5.1",
                "medium",
                "completed" if index % 5 else "aborted",
                0,
                f"fixture prompt {index}",
                f"sha-{index:02d}",
                16 + index,
                1,
                0,
                200 + index,
                input_tokens,
                cached,
                non_cached,
                output,
                10,
                total,
                cached / input_tokens if input_tokens else 0.0,
                2 if index < 3 else 1,
                non_cached + cached * 0.1 + output * 6,
                non_cached + output,
                "development",
                "dashboard-check",
                f"/tmp/transcript-{index:02d}.jsonl",
            )
        )
    con.executemany(
        """
        insert into turns (
          session_id, turn_id, captured_at, captured_at_unix, started_at, stopped_at,
          cwd, project, thread_name, model, reasoning_effort, turn_status, estimated,
          prompt_preview, prompt_sha256, prompt_chars, prompt_lines, code_block_chars,
          assistant_chars, input_tokens, cached_input_tokens, non_cached_input_tokens,
          output_tokens, reasoning_output_tokens, total_tokens, cached_ratio,
          model_call_count, weighted_credits, uncached_input_equivalent, category,
          workflow, transcript_path
        ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        turns,
    )
    model_summaries = [
        (
            row[0],
            row[1],
            row[26],
            row[19],
            row[20],
            row[21],
            row[22],
            row[23],
            row[24],
            row[27],
            row[24],
            row[22],
            1,
            row[26],
        )
        for row in turns
    ]
    con.executemany("insert into model_call_summaries values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", model_summaries)
    tool_summaries = [
        (turns[0][0], turns[0][1], "exec_command", "functions", 3, 1200, 0, 310, 0, 180, 90, 160),
        (turns[0][0], turns[0][1], "apply_patch", "functions", 1, 260, 0, 80, 0, 40, 40, 80),
        (turns[1][0], turns[1][1], "exec_command", "functions", 2, 900, 0, 220, 0, 120, 70, 120),
        (turns[2][0], turns[2][1], "view_image", "functions", 1, 80, 0, 25, 0, 20, 20, 25),
    ]
    con.executemany("insert into tool_call_summaries values (?,?,?,?,?,?,?,?,?,?,?,?)", tool_summaries)
    tool_samples = [
        (turns[0][0], turns[0][1], "call-1", "exec_command", "functions", "largest_output", 1, _iso(now - 3630), _iso(now - 3610), 20, 1200, 0, 310, "completed", 0, "fixture command output"),
        (turns[0][0], turns[0][1], "call-2", "apply_patch", "functions", "largest_output", 1, _iso(now - 3605), _iso(now - 3590), 15, 260, 0, 80, "completed", 0, "fixture patch output"),
        (turns[1][0], turns[1][1], "call-3", "exec_command", "functions", "largest_output", 2, _iso(now - 7230), _iso(now - 7200), 30, 900, 0, 220, "completed", 0, "fixture second output"),
    ]
    con.executemany("insert into tool_call_samples values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tool_samples)
    con.execute(
        "insert into task_rollups values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            turns[0][0],
            turns[0][1],
            "99999999-aaaa-bbbb-cccc-dddddddddddd",
            "reviewer",
            "fixture-reviewer",
            _iso(now - 3500),
            now - 3500,
            "child_task_time_overlap",
            turns[0][24],
            640,
            turns[0][24] + 640,
            turns[0][27],
            120.0,
            turns[0][27] + 120.0,
        ),
    )
    con.executemany(
        "insert into run_metadata values (?,?)",
        [
            ("last_compacted_at_unix", json.dumps(int(now - 86400))),
        ],
    )
    con.commit()
    con.close()

    old = now - 20 * 86400
    recent = now - 3600
    current = raw_segments.ensure_current_segment(root, kind="prompt_usage", source_name=raw_segments.PROMPT_RAW_NAME)
    _write_jsonl(pathlib.Path(current["path"]), [_raw_turn("raw-old", "old", old, 500), _raw_turn("raw-new", "new", recent, 600)])
    _write_jsonl(normalized_dir / "prompt-usage.normalized.jsonl", [_raw_turn("norm", "one", recent, 700)])
    (normalized_dir / "normalize-state.json").write_text('{"offset": 1}\n', encoding="utf-8")
    (tmp_dir / "dashboard.tmp").write_text("temporary fixture\n", encoding="utf-8")
    (bad_dir / "parse-error.jsonl").write_text('{"error":"fixture"}\n', encoding="utf-8")
    (state_dir / "service-state.json").write_text('{"fixture":true}\n', encoding="utf-8")
    (state_dir / "0123456789abcdef0123456789abcdef.json").write_text(
        json.dumps({"record_type": "turn_start", "captured_at": _iso(old), "session_id": "pending", "turn_id": "pending"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return db_path


__all__ = ["write_dashboard_fixture"]
