from __future__ import annotations

try:
    from tests.support import ROOT, _turn_raw, json, load_module, mock, pathlib, stat, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, _turn_raw, json, load_module, mock, pathlib, stat, tempfile, unittest


class NormalizeTests(unittest.TestCase):
    def test_complete_jsonl_offset_scans_back_to_last_complete_row(self) -> None:
        normalize = load_module("normalize_complete_offset_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "raw.jsonl"
            complete_payload = b'{"row":1}\n' + (b"x" * 20) + b"\n"
            path.write_bytes(complete_payload + b'{"partial":')

            with mock.patch.object(normalize, "JSONL_OFFSET_SCAN_CHUNK_BYTES", 8):
                offset = normalize.complete_jsonl_offset(path)

        self.assertEqual(offset, len(complete_payload))

    def test_complete_jsonl_offset_respects_bounded_size(self) -> None:
        normalize = load_module("normalize_complete_offset_bound_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "raw.jsonl"
            path.write_bytes(b'{"row":1}\n{"row":2}\n{"partial":')
            bounded_size = len(b'{"row":1}\n{"row":2}')

            offset = normalize.complete_jsonl_offset(path, bounded_size)

        self.assertEqual(offset, len(b'{"row":1}\n'))

    def test_full_normalize_ignores_legacy_flat_raw_log(self) -> None:
        normalize = load_module("normalize_segment_only_full_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            flat_raw = base / "raw" / "prompt-usage.raw.jsonl"
            flat_raw.parent.mkdir(parents=True)
            flat_raw.write_text(json.dumps(_turn_raw("legacy", "flat", total=999)) + "\n", encoding="utf-8")
            normalize.BASE_DIR = base
            normalize.RAW_LOG = flat_raw
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"

            result = normalize.full_normalize()
            rows = normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result["rows"], 0)
        self.assertEqual(rows, [])

    def test_incremental_normalize_reads_tail_of_rotated_closed_segment(self) -> None:
        normalize = load_module("normalize_closed_segment_incremental_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            normalize.BASE_DIR = base
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(current["path"])
            current_path.write_text(json.dumps(_turn_raw("s1", "t1", total=100)) + "\n", encoding="utf-8")
            first = normalize.full_normalize()
            self.assertEqual(first["rows"], 1)
            first_size = current_path.stat().st_size

            current_path.write_text(
                current_path.read_text(encoding="utf-8") + json.dumps(_turn_raw("s2", "t2", total=200)) + "\n",
                encoding="utf-8",
            )
            normalize.raw_segments.rotate_current_segment(base=base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

            second = normalize.incremental_normalize()
            rows = [json.loads(line) for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]
            state = json.loads(normalize.STATE_FILE.read_text(encoding="utf-8"))
            final_size = current_path.stat().st_size

        self.assertEqual(second["mode"], "incremental")
        self.assertEqual(second["new_rows"], 1)
        self.assertEqual([(row["session_id"], row["turn_id"], row["usage"]["total_tokens"]) for row in rows], [("s1", "t1", 100), ("s2", "t2", 200)])
        self.assertEqual(state["processed_segments"][current["id"]]["bytes"], final_size)
        self.assertGreater(state["processed_segments"][current["id"]]["bytes"], first_size)

    def test_raw_rows_win_over_lower_priority_rows_at_equal_quality(self) -> None:
        normalize = load_module("normalize_test", ROOT / "scripts" / "normalize.py")
        lower_priority = {
            "turn_status": "completed",
            "estimated": False,
            "schema_version": 2,
            "_source_priority": 1,
        }
        raw = dict(lower_priority)
        raw["_source_priority"] = 2
        self.assertGreater(normalize.rank(raw), normalize.rank(lower_priority))

    def test_full_normalize_skips_unresolved_zero_estimate_rows(self) -> None:
        normalize = load_module("normalize_unresolved_zero_estimate_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(current["path"])
            current_path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "schema_version": 2,
                            "record_type": "turn_usage_raw",
                            "captured_at": "2026-06-02T02:57:31+00:00",
                            "session_id": "s-missing",
                            "turn_id": "t-missing",
                            "transcript_path": None,
                            "turn_status": "completed",
                            "lifecycle_end_reason": "pending_token_count",
                            "usage": {"total_tokens": 0},
                            "end_token_snapshot": {"found": False, "reason": "missing_transcript_path"},
                            "estimated": True,
                        },
                        {
                            "schema_version": 2,
                            "record_type": "turn_usage_raw",
                            "captured_at": "2026-06-02T02:57:32+00:00",
                            "session_id": "s-missing",
                            "turn_id": "t-missing",
                            "transcript_path": None,
                            "turn_status": "incomplete",
                            "lifecycle_end_reason": "missing_start_state",
                            "usage": {"total_tokens": 0},
                            "end_token_snapshot": {"found": False, "reason": "missing_transcript_path"},
                            "estimated": True,
                        },
                        {
                            "schema_version": 2,
                            "record_type": "turn_usage_raw",
                            "captured_at": "2026-06-02T02:57:32.500000+00:00",
                            "session_id": "s-side",
                            "turn_id": "t-side",
                            "transcript_path": None,
                            "turn_status": "incomplete",
                            "lifecycle_end_reason": "unresolved_transcript_path",
                            "usage": {"total_tokens": 0},
                            "end_token_snapshot": {"found": False, "reason": "missing_transcript_path"},
                            "estimated": True,
                        },
                        _turn_raw("s-good", "t-good", total=123) | {"captured_at": "2026-06-02T02:57:33+00:00"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"

            normalize.full_normalize()
            rows = [json.loads(line) for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(
            [(row["session_id"], row["turn_id"], row["usage"]["total_tokens"]) for row in rows],
            [("s-good", "t-good", 123)],
        )

    def test_incremental_normalize_skips_unresolved_zero_estimate_rows(self) -> None:
        normalize = load_module("normalize_incremental_unresolved_zero_estimate_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(current["path"])
            current_path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "schema_version": 2,
                            "record_type": "turn_usage_raw",
                            "captured_at": "2026-06-02T02:57:31+00:00",
                            "session_id": "s-side",
                            "turn_id": "t-side",
                            "transcript_path": None,
                            "turn_status": "incomplete",
                            "lifecycle_end_reason": "unresolved_transcript_path",
                            "usage": {"total_tokens": 0},
                            "end_token_snapshot": {"found": False, "reason": "missing_transcript_path"},
                            "estimated": True,
                        },
                        _turn_raw("s-good", "t-good", total=123) | {"captured_at": "2026-06-02T02:57:33+00:00"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.NORMALIZED_LOG.parent.mkdir(parents=True)
            normalize.NORMALIZED_LOG.write_text("", encoding="utf-8")
            normalize.write_state(normalize.normalize_state({str(current_path): 0}))

            result = normalize.incremental_normalize()
            rows = [json.loads(line) for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(
            [(row["session_id"], row["turn_id"], row["usage"]["total_tokens"]) for row in rows],
            [("s-good", "t-good", 123)],
        )

    def test_missing_start_state_row_recovers_goal_auto_task_lifecycle(self) -> None:
        normalize = load_module("normalize_goal_auto_recovery_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            turn_id = "t-goal"
            transcript.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "timestamp": "2026-05-31T10:00:00.000Z",
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": turn_id, "started_at": 1780221600},
                        },
                        {
                            "timestamp": "2026-05-31T10:00:03.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 30,
                                        "cached_input_tokens": 10,
                                        "output_tokens": 7,
                                        "reasoning_output_tokens": 1,
                                        "total_tokens": 37,
                                    },
                                },
                            },
                        },
                        {
                            "timestamp": "2026-05-31T10:00:04.000Z",
                            "type": "event_msg",
                            "payload": {"type": "task_complete", "turn_id": turn_id, "completed_at": 1780221604},
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            row = {
                "schema_version": 2,
                "record_type": "turn_usage_raw",
                "session_id": "s-goal",
                "turn_id": turn_id,
                "transcript_path": str(transcript),
                "turn_status": "incomplete",
                "lifecycle_end_reason": "missing_start_state",
                "started_at": None,
                "stopped_at": "2026-05-31T10:00:05+00:00",
                "usage": {},
                "end_token_usage": {},
                "estimated": True,
                "start_state_found": False,
            }

            normalized = normalize.normalize_row(row)

        self.assertEqual(normalized["turn_status"], "completed")
        self.assertEqual(normalized["lifecycle_end_reason"], "goal_auto_completed")
        self.assertEqual(normalized["started_at"], "2026-05-31T10:00:00+00:00")
        self.assertEqual(normalized["stopped_at"], "2026-05-31T10:00:04+00:00")
        self.assertEqual(normalized["usage"]["input_tokens"], 30)
        self.assertEqual(normalized["usage"]["cached_input_tokens"], 10)
        self.assertEqual(normalized["usage"]["output_tokens"], 7)
        self.assertEqual(normalized["usage"]["total_tokens"], 37)
        self.assertEqual(normalized["model_call_count"], 1)
        self.assertTrue(normalized["estimated"])

    def test_goal_auto_lifecycle_recovery_scans_each_transcript_once(self) -> None:
        normalize = load_module("normalize_goal_auto_recovery_cache_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {"timestamp": "2026-05-31T10:00:00.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
                        {"timestamp": "2026-05-31T10:00:01.000Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 10, "total_tokens": 10}}}},
                        {"timestamp": "2026-05-31T10:00:02.000Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}},
                        {"timestamp": "2026-05-31T10:00:03.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"}},
                        {"timestamp": "2026-05-31T10:00:04.000Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 20, "total_tokens": 20}}}},
                        {"timestamp": "2026-05-31T10:00:05.000Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t2"}},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            rows = [
                {
                    "schema_version": 2,
                    "record_type": "turn_usage_raw",
                    "session_id": "s-goal",
                    "turn_id": turn_id,
                    "transcript_path": str(transcript),
                    "turn_status": "incomplete",
                    "lifecycle_end_reason": "missing_start_state",
                    "usage": {},
                    "end_token_usage": {},
                }
                for turn_id in ("t1", "t2")
            ]

            real_loads = normalize.json.loads
            load_count = 0

            def counting_loads(*args: object, **kwargs: object) -> object:
                nonlocal load_count
                load_count += 1
                return real_loads(*args, **kwargs)

            with mock.patch.object(normalize.json, "loads", side_effect=counting_loads):
                normalized = [normalize.normalize_row(row) for row in rows]

        self.assertEqual([row["usage"]["total_tokens"] for row in normalized], [10, 20])
        self.assertEqual(load_count, 6)

    def test_goal_auto_lifecycle_recovery_cache_does_not_evict_many_transcripts(self) -> None:
        normalize = load_module("normalize_goal_auto_recovery_many_cache_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            rows = []
            for index in range(129):
                transcript = base / f"rollout-{index}.jsonl"
                turn_id = f"t-{index}"
                transcript.write_text(
                    "\n".join(
                        json.dumps(row)
                        for row in [
                            {"timestamp": "2026-05-31T10:00:00.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
                            {"timestamp": "2026-05-31T10:00:01.000Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}},
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                rows.append(
                    {
                        "schema_version": 2,
                        "record_type": "turn_usage_raw",
                        "session_id": f"s-{index}",
                        "turn_id": turn_id,
                        "transcript_path": str(transcript),
                        "turn_status": "incomplete",
                        "lifecycle_end_reason": "missing_start_state",
                        "usage": {},
                        "end_token_usage": {},
                    }
                )

            real_loads = normalize.json.loads
            load_count = 0

            def counting_loads(*args: object, **kwargs: object) -> object:
                nonlocal load_count
                load_count += 1
                return real_loads(*args, **kwargs)

            with mock.patch.object(normalize.json, "loads", side_effect=counting_loads):
                for row in rows:
                    normalize.normalize_row(row)
                for row in rows:
                    normalize.normalize_row(row)

        self.assertEqual(load_count, 258)

    def test_lifecycle_recovery_scan_checks_cancel_during_transcript_read(self) -> None:
        normalize = load_module("normalize_goal_auto_recovery_cancel_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "timestamp": "2026-05-31T10:00:00.000Z",
                            "type": "event_msg",
                            "payload": {"type": "token_count", "info": {"last_token_usage": {"total_tokens": index}}},
                        }
                    )
                    for index in range(3)
                )
                + "\n",
                encoding="utf-8",
            )
            checkpoints: list[tuple[str, str]] = []

            def record_checkpoint(phase: str, checkpoint: str) -> None:
                checkpoints.append((phase, checkpoint))

            with mock.patch.object(normalize.cancel_control, "check_cancelled", side_effect=record_checkpoint):
                normalize.transcript_lifecycle_index(str(transcript))

        self.assertTrue(any(phase == "normalize" and checkpoint.startswith("lifecycle:") for phase, checkpoint in checkpoints))

    def test_full_normalize_records_source_size_snapshot_not_later_appends(self) -> None:
        normalize = load_module("normalize_source_snapshot_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(current["path"])
            first = _turn_raw("s1", "t1", total=100) | {"captured_at": "2026-05-31T10:00:00+00:00"}
            second = _turn_raw("s2", "t2", total=200) | {"captured_at": "2026-05-31T10:00:01+00:00"}
            current_path.write_text(json.dumps(first) + "\n", encoding="utf-8")
            snapshot_size = current_path.stat().st_size
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            original_iter_rows = normalize.iter_rows
            appended = False

            def append_after_current(path: pathlib.Path, *args: object, **kwargs: object):
                nonlocal appended
                yield from original_iter_rows(path, *args, **kwargs)
                if path == current_path and not appended:
                    appended = True
                    with current_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(second) + "\n")

            with mock.patch.object(normalize, "iter_rows", side_effect=append_after_current):
                normalize.full_normalize()

            state = json.loads(normalize.STATE_FILE.read_text(encoding="utf-8"))
            normalized_rows = [json.loads(line) for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(state["sources"][str(current_path)], snapshot_size)
        self.assertEqual([(row["session_id"], row["turn_id"]) for row in normalized_rows], [("s1", "t1")])

    def test_incremental_normalize_runs_full_when_logic_version_is_stale(self) -> None:
        normalize = load_module("normalize_logic_version_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.NORMALIZED_LOG.parent.mkdir(parents=True)
            normalize.NORMALIZED_LOG.write_text("{}\n", encoding="utf-8")
            normalize.STATE_FILE.write_text(json.dumps({"sources": {str(base / "raw.jsonl"): 0}}) + "\n", encoding="utf-8")

            with mock.patch.object(normalize, "full_normalize", return_value={"mode": "full"}) as full_normalize:
                result = normalize.incremental_normalize()

        self.assertEqual(result["mode"], "full")
        full_normalize.assert_called_once_with()

    def test_private_jsonl_writer_uses_owner_only_mode(self) -> None:
        normalize = load_module("normalize_mode_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "out.jsonl"
            normalize.write_jsonl_private(path, [{"ok": True}])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
