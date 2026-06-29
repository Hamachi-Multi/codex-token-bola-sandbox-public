from __future__ import annotations

try:
    from tests.support import (
        Any,
        ROOT,
        _turn_normalized,
        _turn_raw,
        argparse,
        concurrent,
        datetime,
        gzip,
        hashlib,
        io,
        json,
        load_module,
        mock,
        os,
        pathlib,
        sqlite3,
        stat,
        subprocess,
        sys,
        tempfile,
        time,
        types,
        unittest,
    )
except ModuleNotFoundError:
    from support import (
        Any,
        ROOT,
        _turn_normalized,
        _turn_raw,
        argparse,
        concurrent,
        datetime,
        gzip,
        hashlib,
        io,
        json,
        load_module,
        mock,
        os,
        pathlib,
        sqlite3,
        stat,
        subprocess,
        sys,
        tempfile,
        time,
        types,
        unittest,
    )


class ToolTimingTests(unittest.TestCase):
    def test_weighted_credits_use_non_cached_input_equivalent_units(self) -> None:
        build = load_module("build_analytics_weighted_units_test", ROOT / "scripts" / "build_analytics.py")
        self.assertEqual(build.weighted_credits(non_cached_input=2_000_000, cached_input=1_000_000, output=100_000), 2_700_000.0)

    def test_turn_rows_store_non_cached_input_equivalent_units(self) -> None:
        build = load_module("build_analytics_turn_weighted_units_test", ROOT / "scripts" / "build_analytics.py")
        con = sqlite3.connect(":memory:")
        try:
            build.setup_db(con)
            row = {
                "session_id": "s1",
                "turn_id": "t1",
                "captured_at": "2026-01-01T00:00:00Z",
                "cwd": "/example/.codex/codex-token-bola",
                "prompt": {"prompt_preview": "inspect usage"},
                "usage": {
                    "input_tokens": 3_000_000,
                    "cached_input_tokens": 1_000_000,
                    "non_cached_input_tokens": 2_000_000,
                    "output_tokens": 100_000,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 3_100_000,
                },
            }
            turn = build.upsert_turn_row(con, row, {})
            stored = con.execute("select weighted_credits, uncached_input_equivalent from turns").fetchone()
        finally:
            con.close()
        self.assertIsNotNone(turn)
        assert turn is not None
        self.assertEqual(turn["usage"]["weighted_credits"], 2_700_000.0)
        self.assertEqual(stored, (2_700_000.0, 2_700_000.0))

    def test_raw_segment_manifest_round_trips_owner_only(self) -> None:
        raw_segments = load_module("raw_segments_manifest_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            manifest_path = base / "state" / "raw-segments-manifest.json"
            segment = {
                "id": "prompt-usage.raw.jsonl.20260524000000.20260524010000.1",
                "kind": "prompt_usage",
                "path": str(base / "raw" / "archive" / "prompt-usage.raw.jsonl.20260524000000.20260524010000.1.jsonl.gz"),
                "format": "jsonl.gz",
                "source_name": "prompt-usage.raw.jsonl",
                "created_at_unix": 1779552000.0,
                "min_time_unix": 1779552000.0,
                "max_time_unix": 1779555600.0,
                "rows": 2,
                "bytes": 100,
                "uncompressed_bytes": 200,
                "sha256": None,
                "status": "closed",
            }
            raw_segments.write_manifest(
                base,
                {"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "segments": [segment]},
            )
            loaded = raw_segments.read_manifest(base)
            self.assertEqual(loaded["segments"][0]["id"], segment["id"])
            self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)

    def test_current_segment_handoff_closes_old_segment_without_rewriting_it(self) -> None:
        raw_segments = load_module("raw_segments_pointer_handoff_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            old_path = pathlib.Path(current["path"])
            old_path.write_text(json.dumps(_turn_raw("s1", "t1", total=100) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")
            before = old_path.read_bytes()

            result = raw_segments.rotate_current_segment(base=base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

            self.assertEqual(old_path.read_bytes(), before)
            self.assertEqual(result["closed_segment"]["path"], str(old_path))
            self.assertTrue(pathlib.Path(result["current_segment"]["path"]).exists())
            self.assertNotEqual(result["closed_segment"]["path"], result["current_segment"]["path"])
            manifest = raw_segments.read_manifest(base)
            self.assertEqual(len(manifest["segments"]), 1)
            self.assertEqual(manifest["segments"][0]["rows"], 1)
            self.assertEqual(manifest["segments"][0]["kind"], "prompt_usage")

    def test_current_segment_handoff_rejects_corrupt_manifest_before_pointer_change(self) -> None:
        raw_segments = load_module("raw_segments_pointer_corrupt_manifest_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pointer_path = base / "state" / "current-raw-segments.json"
            before_pointer = pointer_path.read_bytes()
            manifest = base / "state" / "raw-segments-manifest.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{broken-json", encoding="utf-8")

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.rotate_current_segment(base=base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

            self.assertEqual(pointer_path.read_bytes(), before_pointer)
            self.assertEqual(raw_segments.read_current_pointer(base)["current"]["prompt_usage"]["path"], current["path"])

    def test_current_segment_handoff_writes_pointer_before_manifest(self) -> None:
        raw_segments = load_module("raw_segments_pointer_first_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            old_path = pathlib.Path(current["path"])
            old_path.write_text(json.dumps(_turn_raw("s1", "t1", total=100) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")
            observed: list[str] = []
            original_write_current_pointer = raw_segments._rotation.write_current_pointer
            original_write_manifest = raw_segments._rotation.write_manifest

            def spy_pointer(base_arg: pathlib.Path, pointer: dict[str, Any]) -> None:
                observed.append("pointer")
                original_write_current_pointer(base_arg, pointer)

            def spy_manifest(base_arg: pathlib.Path, manifest: dict[str, Any]) -> None:
                observed.append("manifest")
                current_pointer = raw_segments.strict_read_current_pointer(base_arg)
                self.assertNotEqual(current_pointer["current"]["prompt_usage"]["path"], str(old_path))
                original_write_manifest(base_arg, manifest)

            with mock.patch.object(raw_segments._rotation, "write_current_pointer", side_effect=spy_pointer), mock.patch.object(raw_segments._rotation, "write_manifest", side_effect=spy_manifest):
                raw_segments.rotate_current_segment(base=base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

            first_pointer = observed.index("pointer")
            self.assertNotIn("manifest", observed[:first_pointer])
            self.assertEqual(observed[-2:], ["pointer", "manifest"])

    def test_current_segment_handoff_leaves_marker_when_manifest_write_fails_after_pointer(self) -> None:
        raw_segments = load_module("raw_segments_pointer_marker_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            old_path = pathlib.Path(current["path"])
            old_path.write_text(json.dumps(_turn_raw("s1", "t1", total=100) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")

            with mock.patch.object(raw_segments._rotation, "write_manifest", side_effect=OSError("manifest write failed")):
                with self.assertRaises(OSError):
                    raw_segments.rotate_current_segment(base=base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

            pointer = raw_segments.strict_read_current_pointer(base)
            self.assertNotEqual(pointer["current"]["prompt_usage"]["path"], str(old_path))
            marker = raw_segments.read_pending_rotation(base)
            self.assertEqual(marker["phase"], "manifest_pending")
            self.assertEqual(marker["old_segment"]["path"], str(old_path))
            self.assertEqual(raw_segments.strict_read_manifest(base)["segments"], [])

            raw_segments.reconcile_pending_rotation(base)
            manifest = raw_segments.strict_read_manifest(base)
            self.assertEqual(manifest["segments"][0]["path"], str(old_path))
            self.assertFalse(raw_segments.pending_rotation_path(base).exists())

    def test_pending_rotation_pointer_pending_unlinks_empty_new_segment_on_rollback(self) -> None:
        raw_segments = load_module("raw_segments_pointer_pending_orphan_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            old_segment = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            new_segment = raw_segments.new_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            new_path = pathlib.Path(new_segment["path"])
            raw_segments.write_pending_rotation(
                base,
                {
                    "operation": "rotate_current_segment",
                    "phase": "pointer_pending",
                    "kind": "prompt_usage",
                    "old_segment": old_segment,
                    "new_segment": new_segment,
                    "created_at_unix": 1.0,
                },
            )

            raw_segments.reconcile_pending_rotation(base)

            self.assertFalse(new_path.exists())
            self.assertIsNone(raw_segments.read_pending_rotation(base))

    def test_current_segment_handoff_keeps_marker_when_old_segment_missing(self) -> None:
        raw_segments = load_module("raw_segments_pointer_missing_old_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            old_path = pathlib.Path(current["path"])
            result = raw_segments.rotate_current_segment(base=base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            marker = {
                "operation": "rotate_current_segment",
                "phase": "manifest_pending",
                "kind": "prompt_usage",
                "old_segment": current,
                "new_segment": result["current_segment"],
                "created_at_unix": 1.0,
            }
            raw_segments.write_pending_rotation(base, marker)
            old_path.unlink()

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.reconcile_pending_rotation(base)

            self.assertTrue(raw_segments.pending_rotation_path(base).exists())

    def test_current_segment_scan_does_not_hold_raw_lock_after_pointer_handoff(self) -> None:
        raw_segments = load_module("raw_segments_short_lock_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text(json.dumps(_turn_raw("s1", "t1", total=100) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")
            raw_lock_released_before_scan = False
            original_scan = raw_segments._rotation.scan_segment_file

            def delayed_scan(path: pathlib.Path, *, kind: str) -> dict[str, Any]:
                nonlocal raw_lock_released_before_scan
                raw_lock_released_before_scan = raw_segments.raw_segment_lock_available(base)
                return original_scan(path, kind=kind)

            with mock.patch.object(raw_segments._rotation, "scan_segment_file", side_effect=delayed_scan):
                raw_segments.rotate_current_segment(base=base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            self.assertTrue(raw_lock_released_before_scan)

    def test_current_pointer_rejects_existing_path_outside_raw_current(self) -> None:
        raw_segments = load_module("raw_segments_current_pointer_validate_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            outside = base / "raw" / "prompt-usage.raw.jsonl"
            outside.parent.mkdir(parents=True)
            outside.write_text("", encoding="utf-8")
            raw_segments.write_current_pointer(base, {"current": {"prompt_usage": {"id": "prompt-usage.raw.jsonl.current.bad", "kind": "prompt_usage", "source_name": "prompt-usage.raw.jsonl", "path": str(outside)}}})
            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

    def test_current_pointer_rejects_missing_existing_segment_file(self) -> None:
        raw_segments = load_module("raw_segments_current_pointer_missing_file_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            missing = base / "raw" / "current" / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            missing.parent.mkdir(parents=True)
            pointer = {
                "current": {
                    "prompt_usage": {
                        "id": "prompt-usage.raw.jsonl.current.1777593600000000000",
                        "kind": "prompt_usage",
                        "source_name": "prompt-usage.raw.jsonl",
                        "path": str(missing),
                    }
                }
            }
            raw_segments.write_current_pointer(base, pointer)
            before_pointer = raw_segments.current_pointer_path(base).read_bytes()

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

            self.assertEqual(raw_segments.current_pointer_path(base).read_bytes(), before_pointer)
            self.assertFalse(missing.exists())

    def test_current_pointer_rejects_missing_kind(self) -> None:
        raw_segments = load_module("raw_segments_current_pointer_missing_kind_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            segment = base / "raw" / "current" / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            segment.parent.mkdir(parents=True)
            segment.write_text("", encoding="utf-8")
            raw_segments.write_current_pointer(base, {"current": {"prompt_usage": {"id": "prompt-usage.raw.jsonl.current.1777593600000000000", "source_name": "prompt-usage.raw.jsonl", "path": str(segment)}}})

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

    def test_current_pointer_rejects_symlinked_raw_current_parent(self) -> None:
        raw_segments = load_module("raw_segments_current_symlink_parent_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "service"
            outside = pathlib.Path(tmp) / "outside-current"
            outside.mkdir(parents=True)
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            (raw_dir / "current").symlink_to(outside, target_is_directory=True)
            segment = {
                "id": "prompt-usage.raw.jsonl.current.1777593600000000000",
                "kind": "prompt_usage",
                "source_name": "prompt-usage.raw.jsonl",
                "path": str(outside / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"),
            }

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.validate_current_segment_entry(base, segment, kind="prompt_usage")

    def test_hook_append_uses_raw_segment_lock_without_service_lock(self) -> None:
        hook = load_module("hook_current_segment_append_test", ROOT / "hooks" / "token-usage.py")
        raw_segments = load_module("raw_segments_hook_append_test", ROOT / "scripts" / "raw_segments.py")
        service_lock = load_module("service_lock_hook_append_test", ROOT / "scripts" / "service_lock.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            with service_lock.acquire_service_lock(reason="test", codex_home=str(codex_home)):
                hook.append_prompt_usage({"session_id": "s1", "turn_id": "t1", "captured_at": "2026-05-20T00:00:00+00:00"}, codex_home=codex_home)
            current = raw_segments.strict_read_current_pointer(base)["current"]["prompt_usage"]
            self.assertIn('"turn_id":"t1"', pathlib.Path(current["path"]).read_text(encoding="utf-8").replace(" ", ""))

    def test_hook_append_uses_raw_segment_lock_for_default_current_segment(self) -> None:
        hook = load_module("hook_default_current_append_lock_test", ROOT / "hooks" / "token-usage.py")
        raw_segments = load_module("raw_segments_default_current_append_lock_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            record = {"session_id": "s1", "turn_id": "t1", "captured_at": "2026-05-20T00:00:00+00:00"}
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                with raw_segments.acquire_raw_segment_lock(base):
                    future = executor.submit(hook.append_prompt_usage, record, codex_home=codex_home)
                    time.sleep(0.1)
                    self.assertFalse(future.done())
                self.assertTrue(future.result(timeout=5))
            current = raw_segments.strict_read_current_pointer(base)["current"]["prompt_usage"]
            self.assertIn('"turn_id":"t1"', pathlib.Path(current["path"]).read_text(encoding="utf-8").replace(" ", ""))

    def test_hook_append_survives_current_segment_rotation(self) -> None:
        hook = load_module("hook_current_segment_rotation_survival_test", ROOT / "hooks" / "token-usage.py")
        raw_segments = load_module("raw_segments_hook_rotation_survival_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            record = {"session_id": "s1", "turn_id": "t1", "captured_at": "2026-05-20T00:00:00+00:00"}

            self.assertTrue(hook.append_prompt_usage(record, codex_home=codex_home))
            result = raw_segments.rotate_current_segment(base=base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")

            closed_text = pathlib.Path(result["closed_segment"]["path"]).read_text(encoding="utf-8")
            current_text = pathlib.Path(result["current_segment"]["path"]).read_text(encoding="utf-8")
            self.assertEqual((closed_text + current_text).count('"turn_id":"t1"'), 1)

    def test_goal_auto_stop_without_user_prompt_state_defers_lifecycle_scan(self) -> None:
        raw_segments = load_module("raw_segments_goal_auto_stop_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            session_id = "s-goal"
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
                            "timestamp": "2026-05-31T10:00:05.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 100,
                                        "cached_input_tokens": 40,
                                        "output_tokens": 10,
                                        "reasoning_output_tokens": 2,
                                        "total_tokens": 110,
                                    },
                                    "total_token_usage": {
                                        "input_tokens": 1000,
                                        "cached_input_tokens": 400,
                                        "output_tokens": 100,
                                        "reasoning_output_tokens": 20,
                                        "total_tokens": 1100,
                                    },
                                },
                            },
                        },
                        {
                            "timestamp": "2026-05-31T10:00:08.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 50,
                                        "cached_input_tokens": 20,
                                        "output_tokens": 5,
                                        "reasoning_output_tokens": 1,
                                        "total_tokens": 55,
                                    },
                                    "total_token_usage": {
                                        "input_tokens": 1050,
                                        "cached_input_tokens": 420,
                                        "output_tokens": 105,
                                        "reasoning_output_tokens": 21,
                                        "total_tokens": 1155,
                                    },
                                },
                            },
                        },
                        {
                            "timestamp": "2026-05-31T10:00:10.000Z",
                            "type": "event_msg",
                            "payload": {"type": "task_complete", "turn_id": turn_id, "completed_at": 1780221610},
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                hook = load_module("hook_goal_auto_stop_test", ROOT / "hooks" / "token-usage.py")

            with (
                mock.patch.object(hook, "task_lifecycle_token_usage", side_effect=AssertionError("Stop hook must not scan full lifecycle")),
                mock.patch.object(hook, "latest_token_usage", side_effect=AssertionError("Stop hook must not scan latest token without start state")),
            ):
                hook.handle_stop(
                    {
                        "hook_event_name": "Stop",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "transcript_path": str(transcript),
                        "cwd": "/example/src/quant",
                        "model": "gpt-5.5",
                        "last_assistant_message": "done",
                    }
                )

            current = raw_segments.strict_read_current_pointer(codex_home / "codex-token-bola")["current"]["prompt_usage"]
            rows = [json.loads(line) for line in pathlib.Path(current["path"]).read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        record = rows[0]
        self.assertEqual(record["turn_status"], "incomplete")
        self.assertEqual(record["lifecycle_end_reason"], "missing_start_state")
        self.assertFalse(record["start_state_found"])
        self.assertTrue(record["estimated"])
        self.assertIsNone(record["started_at"])
        self.assertEqual(record["usage"]["total_tokens"], 0)
        self.assertEqual(record["end_token_snapshot"]["reason"], "missing_start_state_deferred")
        self.assertEqual(record["model_call_count"], 0)

    def test_stop_logs_raw_append_failure_for_missing_start_marker(self) -> None:
        hook = load_module("hook_stop_append_failure_goal_auto_test", ROOT / "hooks" / "token-usage.py")
        warnings: list[dict[str, Any]] = []

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(hook, "STATE_DIR", pathlib.Path(tmp) / "state"),
            mock.patch.object(hook, "task_lifecycle_token_usage", side_effect=AssertionError("Stop hook must not scan full lifecycle")),
            mock.patch.object(hook, "latest_token_usage", side_effect=AssertionError("Stop hook must not scan latest token without start state")),
            mock.patch.object(hook, "append_prompt_usage", return_value=False),
            mock.patch.object(hook, "safe_append_jsonl", side_effect=lambda _path, record: warnings.append(record) or True),
        ):
            hook.handle_stop({"session_id": "s-failed", "turn_id": "t-failed", "transcript_path": "/tmp/missing.jsonl"})
            marker_paths = sorted((pathlib.Path(tmp) / "state").glob("*.json"))
            marker = json.loads(marker_paths[0].read_text(encoding="utf-8")) if marker_paths else {}

        self.assertTrue(any(row.get("error") == "raw_append_failed" for row in warnings))
        self.assertEqual(len(marker_paths), 1)
        self.assertEqual(marker["record_type"], "turn_stop_missing_start")

    def test_stop_missing_start_writes_marker_when_raw_segment_manifest_is_corrupt(self) -> None:
        hook = load_module("hook_stop_manifest_error_marker_test", ROOT / "hooks" / "token-usage.py")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = pathlib.Path(tmp) / "state"
            with (
                mock.patch.object(hook, "STATE_DIR", state_dir),
                mock.patch.object(hook.raw_segments, "ensure_current_segment", side_effect=hook.raw_segments.ManifestError("bad current pointer")),
            ):
                hook.handle_stop({"session_id": "s-corrupt", "turn_id": "t-corrupt", "transcript_path": "/tmp/rollout.jsonl"})

            marker_paths = sorted(state_dir.glob("*.json"))
            marker = json.loads(marker_paths[0].read_text(encoding="utf-8")) if marker_paths else {}

        self.assertEqual(len(marker_paths), 1)
        self.assertEqual(marker["record_type"], "turn_stop_missing_start")

    def test_stop_logs_raw_append_failure_for_missing_start_state_record(self) -> None:
        hook = load_module("hook_stop_append_failure_missing_start_test", ROOT / "hooks" / "token-usage.py")
        warnings: list[dict[str, Any]] = []

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(hook, "STATE_DIR", pathlib.Path(tmp) / "state"),
            mock.patch.object(hook, "task_lifecycle_token_usage", side_effect=AssertionError("Stop hook must not scan full lifecycle")),
            mock.patch.object(hook, "latest_token_usage", side_effect=AssertionError("Stop hook must not scan latest token without start state")),
            mock.patch.object(hook, "append_prompt_usage", return_value=False),
            mock.patch.object(hook, "safe_append_jsonl", side_effect=lambda _path, record: warnings.append(record) or True),
        ):
            hook.handle_stop({"session_id": "s-missing", "turn_id": "t-missing", "transcript_path": "/tmp/missing.jsonl"})

        self.assertTrue(any(row.get("error") == "raw_append_failed" for row in warnings))

    def test_start_hook_uses_tail_snapshot_without_forward_scan(self) -> None:
        hook = load_module("hook_start_tail_snapshot_test", ROOT / "hooks" / "token-usage.py")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = pathlib.Path(tmp) / "state"
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 7, "total_tokens": 7}}}}) + "\n",
                encoding="utf-8",
            )
            transcript_size = transcript.stat().st_size

            with (
                mock.patch.object(hook, "STATE_DIR", state_dir),
                mock.patch.object(hook, "latest_token_usage", side_effect=AssertionError("UserPromptSubmit hook must not scan from transcript start")),
            ):
                hook.handle_start({"session_id": "s-start", "turn_id": "t-start", "transcript_path": str(transcript)})

            state = json.loads(next(state_dir.glob("*.json")).read_text(encoding="utf-8"))

        self.assertEqual(state["start_file_size"], transcript_size)
        self.assertEqual(state["start_token_usage"]["total_tokens"], 7)
        self.assertEqual(state["start_usage_source"], "tail_token_count")

    def test_stop_with_invalid_start_offset_defers_without_full_scan(self) -> None:
        hook = load_module("hook_invalid_start_offset_test", ROOT / "hooks" / "token-usage.py")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = pathlib.Path(tmp) / "state"
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            session_id = "s-invalid-offset"
            turn_id = "t-invalid-offset"
            state_dir.mkdir(parents=True)
            state_path = state_dir / f"{hook.safe_name(session_id + ':' + turn_id)}.json"
            state_path.write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "transcript_path": str(transcript),
                        "captured_at": "2026-05-31T10:00:00+00:00",
                        "start_file_size": 999,
                        "start_token_usage": {"input_tokens": 0, "total_tokens": 0},
                    }
                ),
                encoding="utf-8",
            )
            warnings: list[dict[str, Any]] = []

            with (
                mock.patch.object(hook, "STATE_DIR", state_dir),
                mock.patch.object(hook, "latest_token_usage", side_effect=AssertionError("Stop hook must not scan when offset is invalid")),
                mock.patch.object(hook, "safe_append_jsonl", side_effect=lambda _path, record: warnings.append(record) or True),
            ):
                hook.handle_stop({"session_id": session_id, "turn_id": turn_id, "transcript_path": str(transcript)})
                state_exists = state_path.exists()

        self.assertTrue(state_exists)
        self.assertTrue(any(row.get("warning") == "deferred_stop_recovery" and row.get("reason") == "invalid_start_file_size" for row in warnings))

    def test_stop_hook_bounds_token_usage_to_current_turn_terminal_event(self) -> None:
        raw_segments = load_module("raw_segments_stop_bounds_turn_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            transcript = pathlib.Path(tmp) / "rollout.jsonl"

            def event(payload: dict[str, Any]) -> str:
                return json.dumps({"timestamp": "2026-05-31T10:00:00.000Z", "type": "event_msg", "payload": payload}) + "\n"

            transcript.write_text(
                event(
                    {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"input_tokens": 0, "total_tokens": 0},
                            "last_token_usage": {"input_tokens": 0, "total_tokens": 0},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                hook = load_module("hook_stop_bounds_turn_test", ROOT / "hooks" / "token-usage.py")
            hook.handle_start({"session_id": "s1", "turn_id": "t1", "transcript_path": str(transcript), "cwd": "/tmp"})
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(event({"type": "task_started", "turn_id": "t1"}))
                handle.write(
                    event(
                        {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {"input_tokens": 100, "total_tokens": 100},
                                "last_token_usage": {"input_tokens": 100, "total_tokens": 100},
                            },
                        }
                    )
                )
                handle.write(event({"type": "task_complete", "turn_id": "t1"}))
                handle.write(event({"type": "task_started", "turn_id": "t2"}))
                handle.write(
                    event(
                        {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {"input_tokens": 150, "total_tokens": 150},
                                "last_token_usage": {"input_tokens": 50, "total_tokens": 50},
                            },
                        }
                    )
                )
            hook.handle_stop({"session_id": "s1", "turn_id": "t1", "transcript_path": str(transcript), "cwd": "/tmp"})

            current = raw_segments.strict_read_current_pointer(base)["current"]["prompt_usage"]
            rows = [json.loads(line) for line in pathlib.Path(current["path"]).read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["usage"]["total_tokens"], 100)
        self.assertEqual(rows[0]["end_token_usage"]["total_tokens"], 100)
        self.assertEqual(rows[0]["model_call_count"], 1)

    def test_stop_hook_defers_when_token_count_exists_without_turn_end_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            transcript = pathlib.Path(tmp) / "rollout.jsonl"

            def event(payload: dict[str, Any]) -> str:
                return json.dumps({"timestamp": "2026-05-31T10:00:00.000Z", "type": "event_msg", "payload": payload}) + "\n"

            transcript.write_text(event({"type": "token_count", "info": {"total_token_usage": {"input_tokens": 0, "total_tokens": 0}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                hook = load_module("hook_stop_turn_end_missing_test", ROOT / "hooks" / "token-usage.py")
            hook.handle_start({"session_id": "s-no-end", "turn_id": "t-no-end", "transcript_path": str(transcript), "cwd": "/tmp"})
            state_path = hook.state_path("s-no-end", "t-no-end")
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(
                    event(
                        {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {"input_tokens": 100, "total_tokens": 100},
                                "last_token_usage": {"input_tokens": 100, "total_tokens": 100},
                            },
                        }
                    )
                )
            warnings: list[dict[str, Any]] = []

            with mock.patch.object(hook, "safe_append_jsonl", side_effect=lambda _path, record: warnings.append(record) or True):
                hook.handle_stop({"session_id": "s-no-end", "turn_id": "t-no-end", "transcript_path": str(transcript), "cwd": "/tmp"})

            raw_rows = list((base / "raw" / "current").glob("*.jsonl"))
            state_exists = state_path.exists()

        self.assertTrue(state_exists)
        self.assertEqual(raw_rows, [])
        self.assertTrue(any(row.get("warning") == "deferred_stop_recovery" and row.get("reason") == "turn_end_not_found" for row in warnings))

    def test_stop_hook_captures_when_turn_end_precedes_forward_scan_limit(self) -> None:
        raw_segments = load_module("raw_segments_stop_terminal_limit_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            transcript = pathlib.Path(tmp) / "rollout.jsonl"

            def event(payload: dict[str, Any]) -> str:
                return json.dumps({"timestamp": "2026-05-31T10:00:00.000Z", "type": "event_msg", "payload": payload}) + "\n"

            transcript.write_text(event({"type": "token_count", "info": {"total_token_usage": {"input_tokens": 0, "total_tokens": 0}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                hook = load_module("hook_stop_terminal_limit_test", ROOT / "hooks" / "token-usage.py")
            hook.handle_start({"session_id": "s-limit", "turn_id": "t-limit", "transcript_path": str(transcript), "cwd": "/tmp"})
            state_path = hook.state_path("s-limit", "t-limit")
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(event({"type": "token_count", "info": {"total_token_usage": {"input_tokens": 42, "total_tokens": 42}, "last_token_usage": {"input_tokens": 42, "total_tokens": 42}}}))
                handle.write(event({"type": "task_complete", "turn_id": "t-limit"}))
                handle.write(event({"type": "token_count", "info": {"total_token_usage": {"input_tokens": 999, "total_tokens": 999}, "last_token_usage": {"input_tokens": 957, "total_tokens": 957}, "padding": "x" * 500}}))

            with mock.patch.object(hook, "HOOK_FORWARD_SCAN_BYTES", 420):
                hook.handle_stop({"session_id": "s-limit", "turn_id": "t-limit", "transcript_path": str(transcript), "cwd": "/tmp"})

            current = raw_segments.strict_read_current_pointer(base)["current"]["prompt_usage"]
            rows = [json.loads(line) for line in pathlib.Path(current["path"]).read_text(encoding="utf-8").splitlines()]

        self.assertFalse(state_path.exists())
        self.assertEqual(rows[0]["usage"]["total_tokens"], 42)

    def test_stop_with_unavailable_start_usage_sums_post_start_model_calls(self) -> None:
        raw_segments = load_module("raw_segments_unavailable_start_usage_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            state_dir = base / "state"
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            prefix = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 900, "total_tokens": 900}}}}) + "\n"
            transcript.write_text(prefix, encoding="utf-8")
            start_file_size = transcript.stat().st_size
            transcript.write_text(
                prefix
                + json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-unavailable"}})
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {"input_tokens": 11, "cached_input_tokens": 4, "output_tokens": 3, "reasoning_output_tokens": 1, "total_tokens": 14},
                                "total_token_usage": {"input_tokens": 911, "cached_input_tokens": 4, "output_tokens": 3, "reasoning_output_tokens": 1, "total_tokens": 914},
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-unavailable"}}) + "\n")
            session_id = "s-unavailable"
            turn_id = "t-unavailable"
            state_dir.mkdir(parents=True)

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                hook = load_module("hook_unavailable_start_usage_test", ROOT / "hooks" / "token-usage.py")
            hook.state_path(session_id, turn_id).write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "transcript_path": str(transcript),
                        "captured_at": "2026-05-31T10:00:00+00:00",
                        "start_file_size": start_file_size,
                        "start_token_usage": {"input_tokens": 0, "total_tokens": 0},
                        "start_usage_source": "unavailable",
                    }
                ),
                encoding="utf-8",
            )

            hook.handle_stop({"session_id": session_id, "turn_id": turn_id, "transcript_path": str(transcript)})

            current = raw_segments.strict_read_current_pointer(base)["current"]["prompt_usage"]
            records = [json.loads(line) for line in pathlib.Path(current["path"]).read_text(encoding="utf-8").splitlines()]

        self.assertEqual(records[0]["usage"]["total_tokens"], 14)
        self.assertTrue(records[0]["estimated"])
        self.assertEqual(records[0]["token_source"], "transcript_path token_count.info.last_token_usage aggregate after start offset")

    def test_transcript_event_stream_tracks_offsets_and_parse_errors(self) -> None:
        parser = load_module("transcript_parser_offsets_test", ROOT / "scripts" / "transcript_parser.py")
        with tempfile.TemporaryDirectory() as tmp:
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            first = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}}}) + "\n"
            bad = "{bad json\n"
            transcript.write_text(
                first
                + bad
                + json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}})
                + "\n",
                encoding="utf-8",
            )

            stream, error = parser.transcript_event_stream(transcript, len(first))
            events = list(stream)

        self.assertIsNone(error)
        self.assertTrue(stream.parse_error_seen)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["line_start"], len(first) + len(bad))
        self.assertEqual(events[0]["item"]["payload"]["type"], "task_complete")

    def test_latest_token_usage_respects_max_bytes_as_hard_cap(self) -> None:
        hook = load_module("hook_latest_token_hard_cap_test", ROOT / "hooks" / "token-usage.py")
        with tempfile.TemporaryDirectory() as tmp:
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {"input_tokens": 100, "total_tokens": 100},
                                "last_token_usage": {"input_tokens": 100, "total_tokens": 100},
                                "padding": "x" * 200,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = hook.latest_token_usage(str(transcript), offset=0, max_bytes=20)

        self.assertFalse(result.get("found"))
        self.assertTrue(result.get("scan_limit_reached"))

    def test_hook_removes_start_state_after_pending_token_count_record(self) -> None:
        raw_segments = load_module("raw_segments_pending_token_count_state_cleanup_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            session_id = "s-pending"
            turn_id = "t-pending"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                hook = load_module("hook_pending_token_count_state_cleanup_test", ROOT / "hooks" / "token-usage.py")
            hook.handle_start(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "transcript_path": str(transcript),
                    "cwd": "/example/.codex/codex-token-bola",
                    "model": "gpt-5.5",
                    "prompt": "pending token count",
                }
            )
            state_path = hook.state_path(session_id, turn_id)
            self.assertTrue(state_path.exists())

            hook.handle_stop(
                {
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "transcript_path": str(transcript),
                    "cwd": "/example/.codex/codex-token-bola",
                    "model": "gpt-5.5",
                }
            )

            current = raw_segments.strict_read_current_pointer(codex_home / "codex-token-bola")["current"]["prompt_usage"]
            rows = [json.loads(line) for line in pathlib.Path(current["path"]).read_text(encoding="utf-8").splitlines()]
            state_exists = state_path.exists()

        self.assertEqual(rows[0]["lifecycle_end_reason"], "pending_token_count")
        self.assertFalse(state_exists)

    def test_hook_keeps_start_state_when_stop_has_no_transcript_path(self) -> None:
        raw_segments = load_module("raw_segments_missing_transcript_state_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            session_id = "s-missing-transcript"
            turn_id = "t-missing-transcript"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                hook = load_module("hook_missing_transcript_state_test", ROOT / "hooks" / "token-usage.py")
            hook.handle_start(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "transcript_path": None,
                    "cwd": "/example/.codex/codex-token-bola",
                    "model": "gpt-5.5",
                    "prompt": "missing transcript",
                }
            )
            state_path = hook.state_path(session_id, turn_id)
            self.assertTrue(state_path.exists())

            hook.handle_stop(
                {
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "transcript_path": None,
                    "cwd": "/example/.codex/codex-token-bola",
                    "model": "gpt-5.5",
                }
            )
            hook.handle_stop(
                {
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "transcript_path": None,
                    "cwd": "/example/.codex/codex-token-bola",
                    "model": "gpt-5.5",
                }
            )
            base = codex_home / "codex-token-bola"
            current_paths = raw_segments.current_segment_paths(base, kind="prompt_usage")
            error_text = (base / "prompt-usage-errors.jsonl").read_text(encoding="utf-8")
            state_exists = state_path.exists()

        self.assertTrue(state_exists)
        self.assertEqual(current_paths, [])
        self.assertEqual(error_text.count("deferred_stop_recovery"), 2)
        self.assertEqual(error_text.count("invalid_start_file_size"), 2)

    def test_installed_hook_loads_scripts_from_codex_home_token_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            installed_hook = codex_home / "hooks" / "token-usage.py"
            installed_scripts = codex_home / "codex-token-bola" / "scripts"
            installed_hook.parent.mkdir(parents=True)
            installed_scripts.mkdir(parents=True)
            installed_hook.write_text((ROOT / "hooks" / "token-usage.py").read_text(encoding="utf-8"), encoding="utf-8")
            (installed_scripts / "raw_segments.py").write_text(
                (ROOT / "scripts" / "raw_segments.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            old_path = list(sys.path)
            sys.modules.pop("raw_segments", None)
            try:
                sys.path[:] = [
                    item
                    for item in sys.path
                    if item not in {str(ROOT / "scripts"), str(codex_home / "scripts"), str(installed_scripts)}
                ]
                with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                    hook = load_module("installed_hook_import_path_test", installed_hook)
                self.assertTrue(
                    hook.append_prompt_usage(
                        {"session_id": "s1", "turn_id": "t1", "captured_at": "2026-05-20T00:00:00+00:00"}
                    )
                )
            finally:
                sys.modules.pop("raw_segments", None)
                sys.path[:] = old_path

            pointer = json.loads((codex_home / "codex-token-bola" / "state" / "current-raw-segments.json").read_text(encoding="utf-8"))
            current = pathlib.Path(pointer["current"]["prompt_usage"]["path"])
            self.assertEqual(current.parent, codex_home / "codex-token-bola" / "raw" / "current")
            self.assertIn('"turn_id":"t1"', current.read_text(encoding="utf-8").replace(" ", ""))

    def test_all_current_segment_handoff_writes_one_prompt_pointer(self) -> None:
        raw_segments = load_module("raw_segments_all_kind_atomic_pointer_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            prompt = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            observed: list[dict[str, Any]] = []
            original_write_current_pointer = raw_segments._rotation.write_current_pointer

            def spy_pointer(base_arg: pathlib.Path, pointer: dict[str, Any]) -> None:
                observed.append(json.loads(json.dumps(pointer)))
                original_write_current_pointer(base_arg, pointer)

            with mock.patch.object(raw_segments._rotation, "write_current_pointer", side_effect=spy_pointer):
                raw_segments.rotate_all_current_segments(base)

            self.assertEqual(len(observed), 1)
            current = observed[0]["current"]
            self.assertNotEqual(current["prompt_usage"]["path"], prompt["path"])
            self.assertNotIn("model_calls", current)

    def test_pending_rotation_reconciles_empty_unlinked_old_segment_recorded_in_marker(self) -> None:
        raw_segments = load_module("raw_segments_empty_unlinked_marker_reconcile_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            old_segment = raw_segments.new_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            new_segment = raw_segments.new_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_segments.write_current_pointer(base, raw_segments.empty_current_pointer(base) | {"current": {"prompt_usage": new_segment}})
            closed_segment = raw_segments.closed_segment_from_current(
                old_segment,
                {
                    "rows": 0,
                    "undated_rows": 0,
                    "corrupt_rows": 0,
                    "unknown_rows": 0,
                    "days": [],
                    "bytes": 0,
                    "uncompressed_bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "min_time_unix": None,
                    "max_time_unix": None,
                },
                kind="prompt_usage",
            )
            pathlib.Path(old_segment["path"]).unlink()
            raw_segments.write_pending_rotation(
                base,
                {
                    "operation": "rotate_current_segments",
                    "phase": "manifest_pending",
                    "segments": {"prompt_usage": {"old_segment": old_segment, "new_segment": new_segment}},
                    "closed_segments": {"prompt_usage": closed_segment},
                    "created_at_unix": 1.0,
                },
            )

            raw_segments.reconcile_pending_rotation(base)

        self.assertFalse(raw_segments.pending_rotation_path(base).exists())

    def test_compact_can_rotate_current_segments_without_active_rewrite(self) -> None:
        compact = load_module("compact_current_segment_no_rewrite_test", ROOT / "scripts" / "compact_raw.py")
        raw_segments = load_module("raw_segments_compact_no_rewrite_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            prompt_raw = raw_dir / "prompt-usage.raw.jsonl"
            prompt_raw.write_text("flat prompt\n", encoding="utf-8")
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text("current prompt\n", encoding="utf-8")
            result = compact.rotate_current_logs(base)
            self.assertIn("prompt_usage", result)
            self.assertNotIn("model_calls", result)
            self.assertEqual(prompt_raw.read_text(encoding="utf-8"), "flat prompt\n")

    def test_compact_rotate_current_cli_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            (raw_dir / "prompt-usage.raw.jsonl").write_text("flat prompt\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "compact_raw.py"),
                    "--rotate-current",
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
            )
            parsed = json.loads(result.stdout)
            self.assertIn("prompt_usage", parsed)
            self.assertNotIn("model_calls", parsed)
            self.assertEqual((raw_dir / "prompt-usage.raw.jsonl").read_text(encoding="utf-8"), "flat prompt\n")

    def test_compact_rotate_current_removes_empty_closed_segments(self) -> None:
        compact = load_module("compact_empty_current_cleanup_test", ROOT / "scripts" / "compact_raw.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current_dir = base / "raw" / "current"
            state_dir = base / "state"
            current_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            prompt_old = current_dir / "prompt-usage.raw.jsonl.current.1.jsonl"
            prompt_old.write_text("", encoding="utf-8")
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "base": str(base.resolve()),
                        "current": {
                            "prompt_usage": {"id": "prompt-usage.raw.jsonl.current.1", "kind": "prompt_usage", "path": str(prompt_old), "source_name": "prompt-usage.raw.jsonl"},
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            result = compact.rotate_current_logs(base)

            self.assertFalse(prompt_old.exists())
            self.assertTrue(pathlib.Path(result["prompt_usage"]["current_segment"]["path"]).exists())

    def test_compact_help_describes_current_segment_rotation(self) -> None:
        compact_help = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "compact_raw.py"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        cli_help = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "codex_token_usage.py"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("Rotate current raw segments", compact_help)
        self.assertNotIn("Archive DB-applied raw JSONL prefixes", compact_help)
        self.assertIn("Rotate current raw segments", cli_help)

    def test_tool_call_records_issuing_and_consuming_steps(self) -> None:
        build = load_module("build_analytics_test", ROOT / "scripts" / "build_analytics.py")
        rows = [
            {"type": "session_meta", "payload": {"id": "s1"}},
            {"type": "turn_context", "payload": {"turn_id": "t1"}},
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "token_count", "info": {}}},
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:00:01Z",
                "payload": {"type": "function_call", "call_id": "c1", "name": "exec_command"},
            },
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:02Z", "payload": {"type": "token_count", "info": {}}},
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:00:03Z",
                "payload": {"type": "function_call_output", "call_id": "c1", "output": "Original token count: 42"},
            },
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:04Z", "payload": {"type": "token_count", "info": {}}},
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = pathlib.Path(tmp_dir) / "transcript.jsonl"
            tmp.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            calls = build.extract_tool_calls({str(tmp)}, {"s1": {"t1"}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["issued_by_model_call_index"], 2)
        self.assertEqual(calls[0]["consumed_by_model_call_index"], 3)
        self.assertEqual(calls[0]["output_reported_tokens"], 42)
        self.assertEqual(calls[0]["output_preview"], "")

    def test_project_root_can_be_configured(self) -> None:
        build = load_module("build_analytics_project_test", ROOT / "scripts" / "build_analytics.py")
        previous = build.PROJECT_ROOTS
        try:
            build.PROJECT_ROOTS = [pathlib.Path("/workspace")]
            self.assertEqual(build.project_from_cwd("/workspace/my-app/service"), "my-app")
        finally:
            build.PROJECT_ROOTS = previous

    def test_session_index_latest_thread_name_wins(self) -> None:
        build = load_module("build_analytics_session_index_test", ROOT / "scripts" / "build_analytics.py")
        previous = build.SESSION_INDEX
        with tempfile.TemporaryDirectory() as tmp_dir:
            index_path = pathlib.Path(tmp_dir) / "session_index.jsonl"
            index_path.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "s1", "thread_name": "old", "updated_at": "2026-01-01T00:00:00Z"}),
                        json.dumps({"id": "s2", "thread_name": "", "updated_at": "2026-01-01T00:00:01Z"}),
                        json.dumps({"id": "s1", "thread_name": "new", "updated_at": "2026-01-01T00:00:02Z"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                build.SESSION_INDEX = index_path
                self.assertEqual(build.read_session_index(), {"s1": "new"})
            finally:
                build.SESSION_INDEX = previous

    def test_normalize_incremental_appends_only_new_raw_rows(self) -> None:
        normalize = load_module("normalize_incremental_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.ARCHIVE_DIR = base / "raw" / "archive"
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(current["path"])
            current_path.write_text(json.dumps(_turn_raw("s1", "t1", total=100)) + "\n", encoding="utf-8")

            first = normalize.incremental_normalize()
            current_path.write_text(
                current_path.read_text(encoding="utf-8") + json.dumps(_turn_raw("s2", "t2", total=200)) + "\n",
                encoding="utf-8",
            )
            second = normalize.incremental_normalize()
            third = normalize.incremental_normalize()

            self.assertEqual(first["mode"], "full")
            self.assertEqual(first["new_rows"], 1)
            self.assertEqual(second["mode"], "incremental")
            self.assertEqual(second["new_rows"], 1)
            self.assertEqual(third["new_rows"], 0)
            with normalize.NORMALIZED_LOG.open(encoding="utf-8") as handle:
                self.assertEqual(sum(1 for _ in handle), 2)
            self.assertEqual(third["normalized_turns_size"], normalize.NORMALIZED_LOG.stat().st_size)

    def test_normalize_incremental_retries_eof_partial_jsonl_without_advancing_offset(self) -> None:
        normalize = load_module("normalize_incremental_partial_tail_retry_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.ARCHIVE_DIR = base / "raw" / "archive"
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(current["path"])
            current_path.write_text(json.dumps(_turn_raw("s1", "t1", total=100)) + "\n", encoding="utf-8")
            normalize.incremental_normalize()
            committed_size = current_path.stat().st_size

            complete = json.dumps(_turn_raw("s2", "t2", total=200))
            current_path.write_text(current_path.read_text(encoding="utf-8") + complete[:-3], encoding="utf-8")
            second = normalize.incremental_normalize()
            state_after_partial = json.loads(normalize.STATE_FILE.read_text(encoding="utf-8"))

            current_path.write_text(current_path.read_text(encoding="utf-8") + complete[-3:] + "\n", encoding="utf-8")
            third = normalize.incremental_normalize()
            rows = [json.loads(line) for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(second["new_rows"], 0)
        self.assertFalse(normalize.BAD_LOG.exists())
        self.assertEqual(state_after_partial["sources"][str(current_path)], committed_size)
        self.assertEqual(third["new_rows"], 1)
        self.assertEqual([(row["session_id"], row["turn_id"]) for row in rows], [("s1", "t1"), ("s2", "t2")])

    def test_normalize_incremental_rolls_back_published_tail_after_state_write_failure(self) -> None:
        normalize = load_module("normalize_incremental_publish_recovery_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.ARCHIVE_DIR = base / "raw" / "archive"
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            first_turn = _turn_raw("s1", "t1", total=100) | {"model_calls": [{"index": 1, "timestamp": "2026-01-01T00:00:01Z", "usage": {"input_tokens": 90, "cached_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 0, "total_tokens": 100}}]}
            second_turn = _turn_raw("s2", "t2", total=200) | {"model_calls": [{"index": 1, "timestamp": "2026-01-01T00:00:01Z", "usage": {"input_tokens": 190, "cached_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 0, "total_tokens": 200}}]}
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(current["path"])
            current_path.write_text(json.dumps(first_turn) + "\n", encoding="utf-8")
            normalize.incremental_normalize()
            current_path.write_text(
                current_path.read_text(encoding="utf-8") + json.dumps(second_turn) + "\n",
                encoding="utf-8",
            )
            original_write_state = normalize.write_state
            failed_once = False

            def fail_after_publish(state: dict[str, Any]) -> None:
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise OSError("state commit failed")
                original_write_state(state)

            with mock.patch.object(normalize, "write_state", side_effect=fail_after_publish):
                with self.assertRaises(OSError):
                    normalize.incremental_normalize()

            normalize.incremental_normalize()
            turn_ids = [json.loads(line)["turn_id"] for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(turn_ids, ["t1", "t2"])

    def test_full_normalize_publish_failure_forces_safe_recovery(self) -> None:
        normalize = load_module("normalize_full_publish_recovery_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.ARCHIVE_DIR = base / "raw" / "archive"
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.NORMALIZED_LOG.parent.mkdir(parents=True)
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text(json.dumps(_turn_raw("s1", "t1", total=100)) + "\n", encoding="utf-8")
            normalize.NORMALIZED_LOG.write_text(json.dumps(_turn_normalized("stale", "stale", total=1)) + "\n", encoding="utf-8")
            normalize.STATE_FILE.write_text(
                json.dumps(
                    {
                        "logic_version": normalize.NORMALIZE_LOGIC_VERSION,
                        "sources": {str(current["path"]): 0},
                        "normalized_log_size": normalize.NORMALIZED_LOG.stat().st_size,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            original_write_state = normalize.write_state
            with mock.patch.object(normalize, "write_state", side_effect=OSError("state write failed")):
                with self.assertRaises(OSError):
                    normalize.full_normalize()

            normalize.write_state = original_write_state
            result = normalize.incremental_normalize()
            turn_ids = [json.loads(line)["turn_id"] for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result["mode"], "full")
        self.assertEqual(turn_ids, ["t1"])

    def test_normalize_recovery_fails_on_corrupt_pending_publish_marker(self) -> None:
        normalize = load_module("normalize_corrupt_pending_publish_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.STATE_FILE.parent.mkdir(parents=True)
            pending = normalize.pending_publish_file()
            pending.write_text("{bad\n", encoding="utf-8")

            with self.assertRaises(normalize.PendingPublishRecoveryError):
                normalize.recover_pending_publish()

            self.assertTrue(pending.exists())

    def test_normalize_recovery_truncates_when_processed_segments_differ(self) -> None:
        normalize = load_module("normalize_processed_segment_recovery_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.NORMALIZED_LOG.parent.mkdir(parents=True)
            source_path = base / "raw" / "current" / "prompt-usage.raw.jsonl.current.1.jsonl"
            source_path.parent.mkdir(parents=True)
            sources = {str(source_path): 128}
            old_row = json.dumps(_turn_normalized("s-old", "t-old", total=10)) + "\n"
            crash_row = json.dumps(_turn_normalized("s-new", "t-new", total=20)) + "\n"
            normalize.NORMALIZED_LOG.write_text(old_row, encoding="utf-8")
            old_size = normalize.NORMALIZED_LOG.stat().st_size
            normalize.write_state(normalize.normalize_state(sources, {}))
            pending_state = normalize.normalize_state(
                sources,
                {
                    "closed-segment": {
                        "path": str(source_path),
                        "bytes": 128,
                        "sha256": "abc",
                        "rows": 1,
                    }
                },
            )
            normalize.write_pending_publish(old_size, pending_state)
            with normalize.NORMALIZED_LOG.open("a", encoding="utf-8") as handle:
                handle.write(crash_row)

            normalize.recover_pending_publish()

            payload = normalize.NORMALIZED_LOG.read_text(encoding="utf-8")
            self.assertEqual(payload, old_row)
            self.assertFalse(normalize.pending_publish_file().exists())

    def test_normalize_main_reports_corrupt_pending_publish_marker_as_json(self) -> None:
        normalize = load_module("normalize_corrupt_pending_publish_main_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.STATE_FILE.parent.mkdir(parents=True)
            pending = normalize.pending_publish_file()
            pending.write_text("{bad\n", encoding="utf-8")
            captured = io.StringIO()

            with (
                mock.patch.object(normalize.service_paths, "assert_migrated", return_value=None),
                mock.patch.object(normalize.service_lock, "acquire_service_lock", return_value=mock.MagicMock(__enter__=lambda _self: None, __exit__=lambda *_args: None)),
                mock.patch.object(normalize.sys, "argv", ["normalize.py", "--incremental"]),
                mock.patch.object(normalize.sys, "stdout", captured),
            ):
                code = normalize.main()

        payload = json.loads(captured.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "normalize_pending_publish_recovery_failed")
        self.assertTrue(payload["recovery_required"])
        self.assertEqual(payload["marker_path"], str(pending))

    def test_normalize_cancel_checkpoint_stops_before_publishing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            cancel_file = base / "cancel.json"
            cancel_file.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_TOKEN_USAGE_CANCEL_FILE": str(cancel_file)}, clear=False):
                normalize = load_module("normalize_cancel_checkpoint_test", ROOT / "scripts" / "normalize.py")
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.ARCHIVE_DIR = base / "raw" / "archive"
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text(json.dumps(_turn_raw("s1", "t1", total=100)) + "\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"CODEX_TOKEN_USAGE_CANCEL_FILE": str(cancel_file)}, clear=False):
                with self.assertRaises(normalize.cancel_control.Cancelled):
                    normalize.full_normalize()

            self.assertFalse(normalize.NORMALIZED_LOG.exists())
            self.assertFalse(normalize.STATE_FILE.exists())

    def test_progress_control_writes_bounded_progress_snapshot(self) -> None:
        progress = load_module("progress_control_test", ROOT / "scripts" / "progress_control.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "progress.json"
            payload = progress.write_progress_to_path(
                path,
                status="running",
                phase="build",
                phase_index=1,
                checkpoint="turns:100",
                processed=100,
                total=200,
            )
            loaded = progress.read_progress(path)

        self.assertEqual(payload["phase_progress"], 0.5)
        self.assertEqual(payload["overall_progress"], 55.0)
        self.assertEqual(loaded["checkpoint"], "turns:100")

    def test_progress_control_throttles_running_snapshots_but_writes_terminal_status(self) -> None:
        progress = load_module("progress_control_throttle_test", ROOT / "scripts" / "progress_control.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "progress.json"
            with mock.patch.object(progress.time, "monotonic", side_effect=[10.0, 10.01, 10.02]):
                first = progress.write_progress_to_path(path, status="running", phase="build", checkpoint="first")
                skipped = progress.write_progress_to_path(path, status="running", phase="build", checkpoint="second")
                terminal = progress.write_progress_to_path(path, status="completed", phase="build", checkpoint="done")
            loaded = progress.read_progress(path)

        self.assertEqual(first["checkpoint"], "first")
        self.assertIsNone(skipped)
        self.assertEqual(terminal["checkpoint"], "done")
        self.assertEqual(loaded["checkpoint"], "done")

    def test_progress_control_maps_cleanup_phases(self) -> None:
        progress = load_module("progress_control_cleanup_phase_test", ROOT / "scripts" / "progress_control.py")
        payload = progress.progress_payload(phase="cleanup-delete", phase_index=1, phase_count=4, phase_progress=0.5)

        self.assertEqual(payload["overall_progress"], 42.5)

    def test_normalize_full_reads_manifest_prompt_segments_before_active(self) -> None:
        normalize = load_module("normalize_manifest_segments_test", ROOT / "scripts" / "normalize.py")
        raw_segments = load_module("raw_segments_manifest_sources_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.ARCHIVE_DIR = base / "raw" / "archive"
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.ARCHIVE_DIR.mkdir(parents=True)
            segment_path = normalize.ARCHIVE_DIR / "prompt-usage.raw.jsonl.20260520000000.20260520000000.1.jsonl.gz"
            untracked_segment_path = normalize.ARCHIVE_DIR / "prompt-usage.raw.jsonl.20260519000000.20260519000000.untracked.jsonl.gz"
            with gzip.open(segment_path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(_turn_raw("s1", "t1", total=100) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n")
            with gzip.open(untracked_segment_path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(_turn_raw("untracked", "archive", total=50) | {"captured_at": "2026-05-19T00:00:00+00:00"}) + "\n")
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text(json.dumps(_turn_raw("s2", "t2", total=200) | {"captured_at": "2026-05-21T00:00:00+00:00"}) + "\n", encoding="utf-8")
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        {
                            "id": "prompt-usage.raw.jsonl.20260520000000.20260520000000.1",
                            "kind": "prompt_usage",
                            "path": str(segment_path),
                            "format": "jsonl.gz",
                            "source_name": normalize.RAW_LOG.name,
                            "min_time_unix": 1779235200.0,
                            "max_time_unix": 1779235200.0,
                            "rows": 1,
                            "bytes": segment_path.stat().st_size,
                            "uncompressed_bytes": 100,
                            "status": "closed",
                        }
                    ]
                },
            )

            result = normalize.full_normalize()

            self.assertEqual(result["rows"], 2)
            rows = [json.loads(line) for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["turn_id"] for row in rows], ["t1", "t2"])

    def test_normalize_ignores_root_prompt_usage_jsonl(self) -> None:
        normalize = load_module("normalize_manifest_jsonl_priority_test", ROOT / "scripts" / "normalize.py")
        raw_segments = load_module("raw_segments_manifest_jsonl_priority_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalize.ARCHIVE_DIR = base / "raw" / "archive"
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            segment_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            segment_path.write_text(json.dumps(_turn_raw("s1", "t1", total=100) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")
            (base / "prompt-usage.jsonl").write_text(json.dumps(_turn_raw("old-root", "ignored", total=50) | {"captured_at": "2026-05-19T00:00:00+00:00"}) + "\n", encoding="utf-8")
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        {
                            "id": "prompt-usage.raw.jsonl.current.1779235200000000000",
                            "kind": "prompt_usage",
                            "path": str(segment_path),
                            "format": "jsonl",
                            "source_name": normalize.RAW_LOG.name,
                            "min_time_unix": 1779235200.0,
                            "max_time_unix": 1779235200.0,
                            "rows": 1,
                            "bytes": segment_path.stat().st_size,
                            "uncompressed_bytes": segment_path.stat().st_size,
                            "status": "closed",
                        }
                    ]
                },
            )

            normalize.full_normalize()

            rows = [json.loads(line) for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(row["session_id"], row["turn_id"], row["usage"]["total_tokens"]) for row in rows], [("s1", "t1", 100)])

    def test_normalize_fails_before_archive_discovery_when_apply_marker_reconcile_fails(self) -> None:
        normalize = load_module("normalize_apply_marker_fail_test", ROOT / "scripts" / "normalize.py")
        with mock.patch.object(
            normalize.raw_segments,
            "reconcile_apply_marker",
            side_effect=normalize.raw_segments.ManifestError("bad marker"),
        ), mock.patch.object(normalize, "archived_prompt_logs", side_effect=AssertionError("archive discovery must not run")):
            with self.assertRaises(normalize.raw_segments.ManifestError):
                normalize.full_normalize()

    def test_normalize_fails_before_archive_discovery_when_rotation_reconcile_fails(self) -> None:
        normalize = load_module("normalize_rotation_marker_fail_test", ROOT / "scripts" / "normalize.py")
        with mock.patch.object(
            normalize.raw_segments,
            "reconcile_pending_rotation",
            side_effect=normalize.raw_segments.ManifestError("bad rotation marker"),
        ), mock.patch.object(normalize, "archived_prompt_logs", side_effect=AssertionError("archive discovery must not run")):
            with self.assertRaises(normalize.raw_segments.ManifestError):
                normalize.full_normalize()

    def test_normalize_rejects_manifest_segment_outside_raw_roots_before_reading(self) -> None:
        normalize = load_module("normalize_manifest_path_validation_test", ROOT / "scripts" / "normalize.py")
        raw_segments = load_module("raw_segments_normalize_path_validation_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            outside = base / "outside.jsonl.gz"
            with gzip.open(outside, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(_turn_raw("s1", "t1", total=100)) + "\n")
            normalize.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            normalize.ARCHIVE_DIR = base / "raw" / "archive"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.RAW_LOG.parent.mkdir(parents=True)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        {
                            "id": "prompt-usage.raw.jsonl.20260520000000.20260520000000.1",
                            "kind": "prompt_usage",
                            "path": str(outside),
                            "format": "jsonl.gz",
                            "source_name": "prompt-usage.raw.jsonl",
                            "status": "closed",
                        }
                    ]
                },
            )

            with self.assertRaises(normalize.raw_segments.ManifestError):
                normalize.archived_prompt_logs()

    def test_build_incremental_upserts_new_turn_without_rebuilding_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            turns = base / "turns.jsonl"
            db_path = base / "analytics.sqlite"
            state_db = base / "missing-state.sqlite"
            turns.write_text(json.dumps(_turn_normalized("s1", "t1", total=100)) + "\n", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            turns_offset = turns.stat().st_size
            turns.write_text(turns.read_text(encoding="utf-8") + json.dumps(_turn_normalized("s2", "t2", total=200)) + "\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                    "--incremental",
                    "--turns-offset",
                    str(turns_offset),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            metadata = json.loads(result.stdout)
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("select count(*) from turns").fetchone()[0], 2)
                self.assertEqual(json.loads(con.execute("select value from run_metadata where key='analysis_mode'").fetchone()[0]), "incremental")
                self.assertEqual(json.loads(con.execute("select value from run_metadata where key='applied_normalized_turns_size'").fetchone()[0]), turns.stat().st_size)
                self.assertEqual(metadata["new_turn_rows"], 1)
            finally:
                con.close()

    def test_build_incremental_keeps_existing_higher_rank_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            turns = base / "turns.jsonl"
            db_path = base / "analytics.sqlite"
            state_db = base / "missing-state.sqlite"
            turns.write_text(json.dumps(_turn_normalized("s1", "t1", total=200)) + "\n", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            turns_offset = turns.stat().st_size
            stale = _turn_normalized("s1", "t1", total=10) | {"turn_status": "incomplete", "estimated": True}
            turns.write_text(turns.read_text(encoding="utf-8") + json.dumps(stale) + "\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                    "--incremental",
                    "--turns-offset",
                    str(turns_offset),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            metadata = json.loads(result.stdout)
            con = sqlite3.connect(db_path)
            try:
                stored = con.execute("select turn_status, estimated, total_tokens from turns where session_id='s1' and turn_id='t1'").fetchone()
            finally:
                con.close()

        self.assertEqual(stored, ("completed", 0, 200))
        self.assertEqual(metadata["new_turn_rows"], 0)

    def test_build_incremental_replaces_equal_rank_turn_with_later_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            turns = base / "turns.jsonl"
            db_path = base / "analytics.sqlite"
            state_db = base / "missing-state.sqlite"
            turns.write_text(json.dumps(_turn_normalized("s1", "t1", total=10)) + "\n", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            turns_offset = turns.stat().st_size
            turns.write_text(turns.read_text(encoding="utf-8") + json.dumps(_turn_normalized("s1", "t1", total=20)) + "\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                    "--incremental",
                    "--turns-offset",
                    str(turns_offset),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            metadata = json.loads(result.stdout)
            con = sqlite3.connect(db_path)
            try:
                total = con.execute("select total_tokens from turns where session_id='s1' and turn_id='t1'").fetchone()[0]
            finally:
                con.close()

        self.assertEqual(total, 20)
        self.assertEqual(metadata["new_turn_rows"], 1)

    def test_build_incremental_replacement_without_transcript_path_deletes_stale_tool_rollups(self) -> None:
        build = load_module("build_incremental_empty_transcript_tool_cleanup_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = base / "analytics.sqlite"
            turns = base / "normalized" / "turns.jsonl"
            state_db = base / "state.sqlite"
            transcript = base / "rollout.jsonl"
            turns.parent.mkdir(parents=True)
            transcript.write_text("", encoding="utf-8")
            old_row = _turn_normalized("s1", "t1", total=10) | {"transcript_path": str(transcript)}
            new_row = _turn_normalized("s1", "t1", total=20)
            turns.write_text(json.dumps(old_row) + "\n", encoding="utf-8")
            turns_offset = turns.stat().st_size
            turns.write_text(turns.read_text(encoding="utf-8") + json.dumps(new_row) + "\n", encoding="utf-8")
            build.NORMALIZED_LOG = turns
            build.STATE_DB = state_db
            build.ANALYTICS_DB = db_path
            build.SESSION_INDEX = base / "session_index.jsonl"
            build.RETENTION_PRUNED_TURNS_FILE = base / "state" / "retention-pruned-turns.json"
            con = sqlite3.connect(db_path)
            try:
                build.setup_db(con)
                build.upsert_turn_row(con, old_row, {})
                build.replace_tool_call_rollups_from_batches(
                    con,
                    [[{"session_id": "s1", "turn_id": "t1", "tool_name": "exec_command", "tool_provider": "exec", "call_id": "c1", "output_tokens": 5, "total_tokens": 10}]],
                )
                build.write_metadata(con, {"applied_normalized_turns_size": turns_offset, "applied_input_fingerprint": "old"})
                con.commit()
            finally:
                con.close()

            result = build.incremental_build(argparse.Namespace(turns_offset=turns_offset))
            con = sqlite3.connect(db_path)
            try:
                tool_rows = con.execute("select count(*) from tool_call_summaries where session_id='s1' and turn_id='t1'").fetchone()[0]
                total = con.execute("select total_tokens from turns where session_id='s1' and turn_id='t1'").fetchone()[0]
            finally:
                con.close()

        self.assertIsNotNone(result)
        self.assertEqual(total, 20)
        self.assertEqual(tool_rows, 0)

    def test_build_full_replaces_equal_rank_turn_with_later_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            turns = base / "turns.jsonl"
            db_path = base / "analytics.sqlite"
            state_db = base / "missing-state.sqlite"
            turns.write_text(
                json.dumps(_turn_normalized("s1", "t1", total=10)) + "\n"
                + json.dumps(_turn_normalized("s1", "t1", total=20)) + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            con = sqlite3.connect(db_path)
            try:
                total = con.execute("select total_tokens from turns where session_id='s1' and turn_id='t1'").fetchone()[0]
            finally:
                con.close()

        self.assertEqual(total, 20)

    def test_build_incremental_rejects_turns_offset_beyond_normalized_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            turns = base / "turns.jsonl"
            db_path = base / "analytics.sqlite"
            state_db = base / "missing-state.sqlite"
            turns.write_text(json.dumps(_turn_normalized("s1", "t1", total=100)) + "\n", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(turns),
                    "--state-db",
                    str(state_db),
                    "--output",
                    str(db_path),
                    "--incremental",
                    "--turns-offset",
                    str(turns.stat().st_size + 1),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("turns_offset_beyond_normalized_size", result.stdout)

    def test_analytics_schema_includes_latest_turn_order_indexes(self) -> None:
        build = load_module("build_analytics_index_test", ROOT / "scripts" / "build_analytics.py")
        con = sqlite3.connect(":memory:")
        try:
            build.setup_db(con)
            tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
            self.assertNotIn("model_calls", tables)
            self.assertNotIn("tool_calls", tables)
            self.assertIn("model_call_summaries", tables)
            self.assertIn("tool_call_summaries", tables)
            self.assertIn("tool_call_samples", tables)
            columns = {row[1] for row in con.execute("pragma table_info(turns)")}
            indexes = {row[0] for row in con.execute("select name from sqlite_master where type='index' and tbl_name='turns'")}
            self.assertNotIn("thread_title", columns)
            self.assertIn("thread_name", columns)
            self.assertIn("idx_turns_latest_order", indexes)
            self.assertIn("idx_turns_project_latest_order", indexes)
            self.assertIn("idx_turns_weighted_order", indexes)
            self.assertIn("idx_turns_weighted_order_asc", indexes)
            self.assertNotIn("idx_turns_thread_title", indexes)
            self.assertIn("idx_turns_thread_name", indexes)
            plan = "\n".join(
                str(row)
                for row in con.execute(
                    """
                    explain query plan
                    select session_id, turn_id
                    from turns
                    order by captured_at_unix desc, session_id desc, turn_id desc
                    limit 25
                    """
                )
            )
            self.assertIn("idx_turns_latest_order", plan)
            self.assertNotIn("USE TEMP B-TREE", plan)
            weighted_plan = "\n".join(
                str(row)
                for row in con.execute(
                    """
                    explain query plan
                    select session_id, turn_id
                    from turns
                    order by weighted_credits desc, captured_at_unix desc, session_id desc, turn_id desc
                    limit 25
                    """
                )
            )
            self.assertIn("idx_turns_weighted_order", weighted_plan)
            self.assertNotIn("USE TEMP B-TREE", weighted_plan)
            weighted_asc_plan = "\n".join(
                str(row)
                for row in con.execute(
                    """
                    explain query plan
                    select session_id, turn_id
                    from turns
                    order by weighted_credits asc, captured_at_unix desc, session_id desc, turn_id desc
                    limit 25
                    """
                )
            )
            self.assertIn("idx_turns_weighted_order_asc", weighted_asc_plan)
            self.assertNotIn("USE TEMP B-TREE", weighted_asc_plan)
        finally:
            con.close()

    def test_build_fails_before_archive_discovery_when_apply_marker_reconcile_fails(self) -> None:
        build = load_module("build_apply_marker_fail_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "token-usage.sqlite"
            with mock.patch.object(
                build.raw_segments,
                "reconcile_apply_marker",
                side_effect=build.raw_segments.ManifestError("bad marker"),
            ):
                with self.assertRaises(build.raw_segments.ManifestError):
                    build.build(output)

    def test_build_fails_before_archive_discovery_when_rotation_reconcile_fails(self) -> None:
        build = load_module("build_rotation_marker_fail_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "token-usage.sqlite"
            with mock.patch.object(
                build.raw_segments,
                "reconcile_pending_rotation",
                side_effect=build.raw_segments.ManifestError("bad rotation marker"),
            ):
                with self.assertRaises(build.raw_segments.ManifestError):
                    build.build(output)

    def test_build_reconciles_raw_segments_from_configured_normalized_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default_home = pathlib.Path(tmp) / "default-home"
            target_home = pathlib.Path(tmp) / "target-home"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(default_home)}, clear=False):
                build = load_module("build_configured_raw_root_test", ROOT / "scripts" / "build_analytics.py")
            normalized = target_home / "codex-token-bola" / "normalized" / "prompt-usage.normalized.jsonl"
            output = target_home / "codex-token-bola" / "analytics" / "token-usage.sqlite"
            args = argparse.Namespace(
                normalized_log=str(normalized),
                state_db=str(target_home / "state_5.sqlite"),
                output=str(output),
                project_root=[],
            )
            build.configure_paths(args)
            observed: list[pathlib.Path] = []

            def record_reconcile(base: pathlib.Path) -> None:
                observed.append(pathlib.Path(base))

            with (
                mock.patch.object(build.raw_segments, "reconcile_apply_marker", side_effect=record_reconcile),
                mock.patch.object(build.raw_segments, "reconcile_pending_rotation", side_effect=record_reconcile),
                mock.patch.object(build, "scan_normalized_build_inputs", return_value=(0, set())),
                mock.patch.object(build, "read_threads", return_value={}),
                mock.patch.object(build, "read_edges", return_value=[]),
                mock.patch.object(build, "spawn_turn_contexts", return_value=[]),
                mock.patch.object(build, "iter_jsonl", return_value=[]),
                mock.patch.object(build, "extract_tool_calls", return_value=[]),
            ):
                build.build()

        self.assertEqual(observed, [target_home / "codex-token-bola", target_home / "codex-token-bola"])

    def test_incremental_pipeline_builds_when_normalized_is_ahead_of_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            base = codex_home / "codex-token-bola"
            normalized = base / "normalized" / "prompt-usage.normalized.jsonl"
            state_file = base / "normalized" / "normalize-state.json"
            db_path = base / "analytics" / "token-usage.sqlite"
            current_dir = base / "raw" / "current"
            state_dir = base / "state"
            current_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            normalized.parent.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            current_path.write_text("", encoding="utf-8")
            pointer = {
                "schema_version": 1,
                "base": str(base.resolve()),
                "current": {
                    "prompt_usage": {
                        "id": "prompt-usage.raw.jsonl.current.1779235200000000000",
                        "kind": "prompt_usage",
                        "path": str(current_path),
                        "source_name": "prompt-usage.raw.jsonl",
                    }
                },
            }
            (state_dir / "current-raw-segments.json").write_text(json.dumps(pointer, separators=(",", ":")) + "\n", encoding="utf-8")
            first_row = json.dumps(_turn_normalized("s1", "t1", total=100)) + "\n"
            second_row = json.dumps(_turn_normalized("s2", "t2", total=200)) + "\n"
            normalized.write_text(first_row, encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_analytics.py"),
                    "--normalized-log",
                    str(normalized),
                    "--state-db",
                    str(codex_home / "missing-state.sqlite"),
                    "--output",
                    str(db_path),
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            normalized.write_text(first_row + second_row, encoding="utf-8")
            state_file.write_text(
                json.dumps(
                    {
                        "logic_version": 5,
                        "sources": {
                            str(current_path): current_path.stat().st_size,
                        },
                        "processed_segments": {},
                        "normalized_log_size": normalized.stat().st_size,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "codex_token_usage.py"),
                    "pipeline",
                    "--codex-home",
                    str(codex_home),
                    "--output",
                    str(db_path),
                    "--state-db",
                    str(codex_home / "missing-state.sqlite"),
                    "--incremental",
                ],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            metadata = json.loads(result.stdout.splitlines()[-1])
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("select count(*) from turns").fetchone()[0], 2)
                self.assertEqual(metadata["processed_turn_log_rows"], 1)
            finally:
                con.close()

    def test_pipeline_recovery_is_explicit(self) -> None:
        cli = load_module("codex_token_usage_recovery_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, tuple[str, ...]]] = []

        def fake_run_script(name, extra_args, env=None):
            calls.append((name, tuple(extra_args)))
            return 0

        def fake_run_script_json(name, extra_args, env=None):
            calls.append((name, tuple(extra_args)))
            if name == "normalize.py":
                return 0, {"mode": "incremental", "normalized_turns_size": 0}, "{}", ""
            return 0, {}, "{}", ""

        args = types.SimpleNamespace(codex_home=None, state_db=None, output=None, project_root=None, incremental=True, recover=False)
        with mock.patch.object(cli, "run_script", fake_run_script), mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "current_analytics_metadata", return_value={"turn_rows": 0}), mock.patch.object(cli, "read_analytics_metadata", return_value={}):
            self.assertEqual(cli.pipeline(args), 0)
        self.assertNotIn(("reconcile.py", ()), calls)

        calls.clear()
        args.recover = True
        with mock.patch.object(cli, "run_script", fake_run_script), mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "current_analytics_metadata", return_value={"turn_rows": 0}), mock.patch.object(cli, "read_analytics_metadata", return_value={}):
            self.assertEqual(cli.pipeline(args), 0)
        self.assertEqual(calls[0], ("reconcile.py", ()))

    def test_service_lock_rejects_concurrent_owner(self) -> None:
        service_lock = load_module("service_lock_exclusive_test", ROOT / "scripts" / "service_lock.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = pathlib.Path(tmp_dir) / "token-usage.lock"
            with service_lock.acquire_service_lock(lock_path=lock_path, reason="outer"):
                with self.assertRaises(service_lock.ServiceLockBusy):
                    with service_lock.acquire_service_lock(lock_path=lock_path, reason="inner"):
                        pass
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_service_lock_ignores_stale_inherited_env_without_valid_fd(self) -> None:
        service_lock = load_module("service_lock_stale_env_test", ROOT / "scripts" / "service_lock.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = pathlib.Path(tmp_dir) / "token-usage.lock"
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_TOKEN_USAGE_LOCK_HELD": "1",
                    "CODEX_TOKEN_USAGE_LOCK_PATH": str(lock_path),
                },
                clear=False,
            ):
                with service_lock.acquire_service_lock(lock_path=lock_path, reason="owner") as lock:
                    self.assertIsNotNone(lock.fd)

    def test_service_lock_ignores_inherited_env_for_different_requested_lock_path(self) -> None:
        service_lock = load_module("service_lock_mismatched_env_test", ROOT / "scripts" / "service_lock.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            outer_path = pathlib.Path(tmp_dir) / "outer.lock"
            requested_path = pathlib.Path(tmp_dir) / "requested.lock"
            with service_lock.acquire_service_lock(lock_path=outer_path, reason="outer") as outer:
                inherited_env = service_lock.child_lock_env(lock_path=outer.path, lock_fd=outer.fd)
                with mock.patch.dict(os.environ, inherited_env, clear=False):
                    with service_lock.acquire_service_lock(lock_path=requested_path, reason="requested") as lock:
                        self.assertEqual(lock.path, requested_path)
                        self.assertNotEqual(os.fstat(lock.fd).st_ino, os.fstat(outer.fd).st_ino)

    def test_pipeline_passes_held_service_lock_to_children(self) -> None:
        cli = load_module("codex_token_usage_lock_env_test", ROOT / "scripts" / "codex_token_usage.py")
        child_envs: list[dict[str, str]] = []

        def fake_run_script_json(name, extra_args, env=None):
            child_envs.append(dict(env or {}))
            if name == "normalize.py":
                return 0, {"mode": "incremental", "normalized_turns_size": 1}, "{}", ""
            return 0, {}, "{}", ""

        args = types.SimpleNamespace(codex_home=None, state_db=None, output=None, project_root=None, incremental=True, recover=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = pathlib.Path(tmp_dir) / "token-usage.lock"
            with mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path), mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "read_analytics_metadata", return_value={}):
                self.assertEqual(cli.pipeline(args), 0)

        self.assertTrue(child_envs)
        self.assertTrue(all(env.get("CODEX_TOKEN_USAGE_LOCK_HELD") == "1" for env in child_envs))
        self.assertTrue(all(env.get("CODEX_TOKEN_USAGE_LOCK_PATH") for env in child_envs))
        self.assertTrue(all(env.get("CODEX_TOKEN_USAGE_LOCK_FD") for env in child_envs))

    def test_reconcile_cli_runs_under_service_lock(self) -> None:
        cli = load_module("codex_token_usage_reconcile_lock_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str], dict[str, str]]] = []

        def fake_run_script(name, extra_args, env=None):
            calls.append((name, list(extra_args), dict(env or {})))
            return 0

        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = pathlib.Path(tmp_dir) / "token-usage.lock"
            with mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path), mock.patch.object(cli, "run_script", fake_run_script):
                with mock.patch.object(cli.sys, "argv", ["codex_token_usage.py", "reconcile", "--flag"]):
                    self.assertEqual(cli.main(), 0)

        self.assertEqual(calls[0][0:2], ("reconcile.py", ["--flag"]))
        self.assertEqual(calls[0][2].get("CODEX_TOKEN_USAGE_LOCK_HELD"), "1")

    def test_incremental_analyze_rotates_current_segment_before_build(self) -> None:
        cli = load_module("cli_analyze_rotate_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str]]] = []
        lock_path = pathlib.Path(tempfile.gettempdir()) / f"token-usage-{time.time_ns()}.lock"
        codex_home = pathlib.Path(tempfile.gettempdir()) / f"codex-home-{time.time_ns()}"
        output_path = codex_home / "codex-token-bola" / "analytics" / f"out-{time.time_ns()}.sqlite"

        def fake_run_script_json(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            if name == "normalize.py":
                return 0, {"mode": "incremental", "normalized_turns_size": 10}, "{}", ""
            if name == "build_analytics.py":
                return 0, {"turn_rows": 1}, "{}", ""
            if name == "compact_raw.py":
                return 0, {"prompt_usage": {"closed_segment": {"id": "p1"}, "current_segment": {"id": "p2"}}}, "{}", ""
            raise AssertionError(name)

        args = argparse.Namespace(
            incremental=True,
            recover=False,
            skip_rotate=False,
            output=str(output_path),
            codex_home=str(codex_home),
            state_db=None,
            project_root=None,
        )
        with mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "read_analytics_metadata", return_value={"applied_normalized_turns_size": 0}), mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path):
            result = cli.pipeline(args)
        self.assertEqual(result, 0)
        self.assertLess(calls.index(("compact_raw.py", ["--rotate-current"])), calls.index(("normalize.py", ["--incremental"])))

    def test_incremental_analyze_keeps_incremental_build_after_non_empty_rotation(self) -> None:
        cli = load_module("cli_analyze_non_empty_rotate_incremental_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str]]] = []
        lock_path = pathlib.Path(tempfile.gettempdir()) / f"token-usage-{time.time_ns()}.lock"
        codex_home = pathlib.Path(tempfile.gettempdir()) / f"codex-home-{time.time_ns()}"
        output_path = codex_home / "codex-token-bola" / "analytics" / f"out-{time.time_ns()}.sqlite"

        def fake_run_script_json(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            if name == "compact_raw.py":
                return 0, {"prompt_usage": {"closed_segment": {"id": "p1", "rows": 1}, "current_segment": {"id": "p2"}}}, "{}", ""
            if name == "normalize.py":
                return 0, {"mode": "incremental", "normalized_turns_size": 20}, "{}", ""
            if name == "build_analytics.py":
                return 0, {"analysis_mode": "incremental", "turn_rows": 2}, "{}", ""
            raise AssertionError(name)

        args = argparse.Namespace(
            incremental=True,
            recover=False,
            skip_rotate=False,
            output=str(output_path),
            codex_home=str(codex_home),
            state_db=None,
            project_root=None,
        )
        with mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "read_analytics_metadata", return_value={"applied_normalized_turns_size": 10, "applied_input_fingerprint": "same"}), mock.patch.object(cli, "analysis_input_fingerprint", return_value="same"), mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path):
            result = cli.pipeline(args)

        self.assertEqual(result, 0)
        self.assertIn(("normalize.py", ["--incremental"]), calls)
        self.assertIn(("build_analytics.py", ["--output", str(output_path), "--incremental", "--turns-offset", "10"]), calls)

    def test_noop_incremental_analyze_rotates_current_segment_before_noop_check(self) -> None:
        cli = load_module("cli_noop_analyze_rotate_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str]]] = []
        lock_path = pathlib.Path(tempfile.gettempdir()) / f"token-usage-{time.time_ns()}.lock"
        codex_home = pathlib.Path(tempfile.gettempdir()) / f"codex-home-{time.time_ns()}"
        output_path = codex_home / "codex-token-bola" / "analytics" / f"out-{time.time_ns()}.sqlite"

        def fake_run_script_json(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            if name == "normalize.py":
                return 0, {"mode": "incremental", "normalized_turns_size": 10}, "{}", ""
            if name == "compact_raw.py":
                return 0, {"prompt_usage": {"closed_segment": {"id": "p1"}, "current_segment": {"id": "p2"}}}, "{}", ""
            raise AssertionError(name)

        args = argparse.Namespace(
            incremental=True,
            recover=False,
            skip_rotate=False,
            output=str(output_path),
            codex_home=str(codex_home),
            state_db=None,
            project_root=None,
        )
        with mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "read_analytics_metadata", return_value={"applied_normalized_turns_size": 10, "applied_input_fingerprint": "same"}), mock.patch.object(cli, "analysis_input_fingerprint", return_value="same"), mock.patch.object(cli, "current_analytics_metadata", return_value={"turn_rows": 1, "analysis_mode": "incremental"}), mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path):
            result = cli.pipeline(args)
        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("compact_raw.py", ["--rotate-current"]))

    def test_noop_incremental_analyze_rebuilds_when_context_fingerprint_changes(self) -> None:
        cli = load_module("cli_noop_context_fingerprint_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str]]] = []
        lock_path = pathlib.Path(tempfile.gettempdir()) / f"token-usage-{time.time_ns()}.lock"
        codex_home = pathlib.Path(tempfile.gettempdir()) / f"codex-home-{time.time_ns()}"
        output_path = codex_home / "codex-token-bola" / "analytics" / f"out-{time.time_ns()}.sqlite"

        def fake_run_script_json(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            if name == "compact_raw.py":
                return 0, {}, "{}", ""
            if name == "normalize.py":
                return 0, {"mode": "incremental", "normalized_turns_size": 10}, "{}", ""
            if name == "build_analytics.py":
                return 0, {"turn_rows": 1}, "{}", ""
            raise AssertionError(name)

        args = argparse.Namespace(
            incremental=True,
            recover=False,
            skip_rotate=False,
            output=str(output_path),
            codex_home=str(codex_home),
            state_db=None,
            project_root=None,
        )
        with mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "read_analytics_metadata", return_value={"applied_normalized_turns_size": 10, "applied_input_fingerprint": "old"}), mock.patch.object(cli, "analysis_input_fingerprint", return_value="new"), mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path):
            result = cli.pipeline(args)

        self.assertEqual(result, 0)
        self.assertIn(("build_analytics.py", ["--output", str(output_path)]), calls)

    def test_incremental_analyze_rebuilds_full_when_applied_offset_exceeds_normalized_size(self) -> None:
        cli = load_module("cli_oversized_applied_offset_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str]]] = []
        lock_path = pathlib.Path(tempfile.gettempdir()) / f"token-usage-{time.time_ns()}.lock"
        codex_home = pathlib.Path(tempfile.gettempdir()) / f"codex-home-{time.time_ns()}"
        output_path = codex_home / "codex-token-bola" / "analytics" / f"out-{time.time_ns()}.sqlite"

        def fake_run_script_json(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            if name == "compact_raw.py":
                return 0, {}, "{}", ""
            if name == "normalize.py":
                return 0, {"mode": "incremental", "normalized_turns_size": 10}, "{}", ""
            if name == "build_analytics.py":
                return 0, {"turn_rows": 1}, "{}", ""
            raise AssertionError(name)

        args = argparse.Namespace(
            incremental=True,
            recover=False,
            skip_rotate=False,
            output=str(output_path),
            codex_home=str(codex_home),
            state_db=None,
            project_root=None,
        )
        with mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "read_analytics_metadata", return_value={"applied_normalized_turns_size": 20, "applied_input_fingerprint": "same"}), mock.patch.object(cli, "analysis_input_fingerprint", return_value="same"), mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path):
            result = cli.pipeline(args)

        self.assertEqual(result, 0)
        self.assertIn(("build_analytics.py", ["--output", str(output_path)]), calls)

    def test_analysis_input_fingerprint_uses_shared_path_digest(self) -> None:
        helper = load_module("analysis_inputs_shared_digest_test", ROOT / "scripts" / "analysis_inputs.py")
        cli = load_module("cli_shared_digest_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            home = pathlib.Path(tmp_dir) / "codex-home"
            state_db = home / "state_5.sqlite"
            session_index = home / "session_index.jsonl"
            pruned = home / "codex-token-bola" / "state" / "retention-pruned-turns.json"
            pruned.parent.mkdir(parents=True)
            state_db.parent.mkdir(parents=True, exist_ok=True)
            state_db.write_text("state\n", encoding="utf-8")
            session_index.write_text("session\n", encoding="utf-8")
            pruned.write_text("{}\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home), "CODEX_TOKEN_USAGE_STATE_DB": str(state_db)}):
                build = load_module("build_shared_digest_test", ROOT / "scripts" / "build_analytics.py")

            expected = helper.analysis_input_fingerprint(home, state_db)

            self.assertEqual(cli.analysis_input_fingerprint(str(home), str(state_db)), expected)
            self.assertEqual(build.analysis_input_fingerprint(), expected)

    def test_incremental_pipeline_codex_home_defaults_output_to_that_home(self) -> None:
        cli = load_module("cli_codex_home_default_output_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str]]] = []
        observed_metadata_outputs: list[str | None] = []
        observed_current_outputs: list[str | None] = []
        lock_path = pathlib.Path(tempfile.gettempdir()) / f"token-usage-{time.time_ns()}.lock"
        codex_home = pathlib.Path(tempfile.gettempdir()) / f"codex-home-{time.time_ns()}"
        expected_output = str(codex_home / "codex-token-bola" / "analytics" / "token-usage.sqlite")

        def fake_run_script_json(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            if name == "compact_raw.py":
                return 0, {}, "{}", ""
            if name == "normalize.py":
                return 0, {"mode": "incremental", "normalized_turns_size": 0}, "{}", ""
            raise AssertionError(name)

        def fake_read_metadata(output):
            observed_metadata_outputs.append(output)
            return {"applied_normalized_turns_size": 0, "applied_input_fingerprint": "same"}

        def fake_current_metadata(output):
            observed_current_outputs.append(output)
            return {"turn_rows": 0, "analysis_mode": "incremental"}

        args = argparse.Namespace(
            incremental=True,
            recover=False,
            skip_rotate=False,
            output=None,
            codex_home=str(codex_home),
            state_db=None,
            project_root=None,
        )
        with mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "read_analytics_metadata", fake_read_metadata), mock.patch.object(cli, "analysis_input_fingerprint", return_value="same"), mock.patch.object(cli, "current_analytics_metadata", fake_current_metadata), mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path):
            result = cli.pipeline(args)

        self.assertEqual(result, 0)
        self.assertEqual(observed_metadata_outputs, [expected_output])
        self.assertEqual(observed_current_outputs, [expected_output])
        self.assertNotIn(("build_analytics.py", []), calls)

    def test_retention_prune_codex_home_defaults_match_pipeline(self) -> None:
        cli = load_module("cli_retention_codex_home_default_test", ROOT / "scripts" / "codex_token_usage.py")
        codex_home = pathlib.Path(tempfile.gettempdir()) / f"codex-home-{time.time_ns()}"
        expected_output = codex_home / "codex-token-bola" / "analytics" / "token-usage.sqlite"

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            retention_args = cli.parse_args(["retention-prune", "--cutoff", "0"])

            self.assertIsNone(retention_args.codex_home)
            self.assertEqual(cli.pipeline_output_path(None, None), expected_output)
            self.assertEqual(cli.retention_db_path(retention_args.codex_home, None), expected_output)

    def test_full_normalize_reads_current_prompt_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            current_dir = base / "raw" / "current"
            state_dir = base / "state"
            current_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            current_path.write_text(json.dumps(_turn_raw("s-current", "t-current", total=123) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")
            pointer = {
                "schema_version": 1,
                "base": str(base.resolve()),
                "current": {
                    "prompt_usage": {
                        "id": "prompt-usage.raw.jsonl.current.1779235200000000000",
                        "kind": "prompt_usage",
                        "path": str(current_path),
                        "source_name": "prompt-usage.raw.jsonl",
                        "created_at_unix": 1779235200.0,
                    }
                },
            }
            (state_dir / "current-raw-segments.json").write_text(json.dumps(pointer) + "\n", encoding="utf-8")

            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "normalize.py")], env={**os.environ, "CODEX_HOME": str(codex_home)}, check=True, capture_output=True, text=True)
            normalized = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalized_text = normalized.read_text(encoding="utf-8").replace(" ", "")

        self.assertIn('"rows":1', result.stdout)
        self.assertIn('"turn_id":"t-current"', normalized_text)

    def test_skip_rotate_incremental_pipeline_reads_current_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            raw_dir = base / "raw"
            current_dir = raw_dir / "current"
            state_dir = base / "state"
            current_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            prompt_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            prompt_path.write_text(json.dumps(_turn_raw("s-current", "t-current", total=123) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")
            pointer = {
                "schema_version": 1,
                "base": str(base.resolve()),
                "current": {
                    "prompt_usage": {
                        "id": "prompt-usage.raw.jsonl.current.1779235200000000000",
                        "kind": "prompt_usage",
                        "path": str(prompt_path),
                        "source_name": "prompt-usage.raw.jsonl",
                        "created_at_unix": 1779235200.0,
                    },
                },
            }
            (state_dir / "current-raw-segments.json").write_text(json.dumps(pointer) + "\n", encoding="utf-8")
            db_path = base / "analytics" / "token-usage.sqlite"

            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "codex_token_usage.py"), "pipeline", "--incremental", "--skip-rotate", "--codex-home", str(codex_home), "--output", str(db_path)], check=True, capture_output=True, text=True)
            con = sqlite3.connect(db_path)
            try:
                total = con.execute("select total_tokens from turns where session_id='s-current' and turn_id='t-current'").fetchone()[0]
            finally:
                con.close()

        self.assertIn('"analysis_mode":"full"', result.stdout)
        self.assertEqual(total, 123)

    def test_full_analyze_rotates_current_segment_before_normalize(self) -> None:
        cli = load_module("cli_full_analyze_rotate_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str]]] = []
        lock_path = pathlib.Path(tempfile.gettempdir()) / f"token-usage-{time.time_ns()}.lock"
        codex_home = pathlib.Path(tempfile.gettempdir()) / f"codex-home-{time.time_ns()}"
        output_path = codex_home / "codex-token-bola" / "analytics" / f"out-{time.time_ns()}.sqlite"

        def fake_run_script_json(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            if name == "compact_raw.py":
                return 0, {"prompt_usage": {"closed_segment": {"id": "p1"}, "current_segment": {"id": "p2"}}}, "{}", ""
            if name == "normalize.py":
                return 0, {"mode": "full", "normalized_turns_size": 2}, "{}", ""
            if name == "build_analytics.py":
                return 0, {"analysis_mode": "full", "turn_rows": 2}, "{}", ""
            raise AssertionError(name)

        def fake_run_script(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            return 0

        args = argparse.Namespace(
            incremental=False,
            recover=False,
            skip_rotate=False,
            output=str(output_path),
            codex_home=str(codex_home),
            state_db=None,
            project_root=None,
        )
        stdout = io.StringIO()
        with mock.patch.object(cli, "run_script_json", fake_run_script_json), mock.patch.object(cli, "run_script", fake_run_script), mock.patch.object(cli.service_lock, "default_lock_path", return_value=lock_path), mock.patch("sys.stdout", stdout):
            result = cli.pipeline(args)
        self.assertEqual(result, 0)
        self.assertLess(calls.index(("compact_raw.py", ["--rotate-current"])), calls.index(("normalize.py", [])))
        output = stdout.getvalue().strip()
        self.assertTrue(output, "full pipeline should print final JSON")
        payload = json.loads(output)
        self.assertEqual(payload["normalize"]["mode"], "full")
        self.assertEqual(payload["analysis_mode"], "full")
        self.assertIn("pre_analysis_rotate", payload)
        self.assertEqual(payload["pre_analysis_rotate"]["prompt_usage"]["closed_segment"]["id"], "p1")

    def test_current_segment_row_is_visible_after_one_analyze(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            current_path.write_text(json.dumps(_turn_raw("s-current", "t-current", total=123) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")
            pointer = {"current": {"prompt_usage": {"id": "prompt-usage.raw.jsonl.current.1779235200000000000", "kind": "prompt_usage", "path": str(current_path), "source_name": "prompt-usage.raw.jsonl", "created_at_unix": 1779235200.0}}}
            (base / "state").mkdir(parents=True)
            (base / "state" / "current-raw-segments.json").write_text(json.dumps({"schema_version": 1, "base": str(base.resolve()), **pointer}) + "\n", encoding="utf-8")
            db_path = base / "analytics" / "token-usage.sqlite"
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "codex_token_usage.py"), "pipeline", "--incremental", "--codex-home", str(codex_home), "--output", str(db_path)], check=True, capture_output=True, text=True)
            self.assertIn('"turn_rows":1', result.stdout)
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("select total_tokens from turns where session_id='s-current' and turn_id='t-current'").fetchone()[0], 123)
            finally:
                con.close()

    def test_current_segment_row_is_visible_after_existing_incremental_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = pathlib.Path(tmp) / ".codex"
            base = codex_home / "codex-token-bola"
            raw_dir = base / "raw"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            current_dir = raw_dir / "current"
            current_dir.mkdir(parents=True)
            raw_dir.mkdir(parents=True, exist_ok=True)
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            (raw_dir / "prompt-usage.raw.jsonl").write_text("", encoding="utf-8")
            (normalized_dir / "prompt-usage.normalized.jsonl").write_text(json.dumps(_turn_normalized("s-existing", "t-existing", total=111)) + "\n", encoding="utf-8")
            normalize = load_module("normalize_existing_incremental_state_test", ROOT / "scripts" / "normalize.py")
            (normalized_dir / "normalize-state.json").write_text(
                json.dumps(
                    {
                        "logic_version": normalize.NORMALIZE_LOGIC_VERSION,
                        "sources": {str(raw_dir / "prompt-usage.raw.jsonl"): 0},
                        "processed_segments": {},
                        "normalized_log_size": (normalized_dir / "prompt-usage.normalized.jsonl").stat().st_size,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            db_path = analytics_dir / "token-usage.sqlite"
            test_env = {**os.environ, "CODEX_HOME": str(codex_home)}
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "build_analytics.py"), "--normalized-log", str(normalized_dir / "prompt-usage.normalized.jsonl"), "--output", str(db_path)],
                check=True,
                capture_output=True,
                text=True,
                env=test_env,
            )
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            current_path.write_text(json.dumps(_turn_raw("s-current", "t-current", total=123) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n", encoding="utf-8")
            pointer = {"current": {"prompt_usage": {"id": "prompt-usage.raw.jsonl.current.1779235200000000000", "kind": "prompt_usage", "path": str(current_path), "source_name": "prompt-usage.raw.jsonl", "created_at_unix": 1779235200.0}}}
            (base / "state").mkdir(parents=True, exist_ok=True)
            (base / "state" / "current-raw-segments.json").write_text(json.dumps({"schema_version": 1, "base": str(base.resolve()), **pointer}) + "\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "codex_token_usage.py"), "pipeline", "--incremental", "--codex-home", str(codex_home), "--output", str(db_path)],
                check=True,
                capture_output=True,
                text=True,
                env=test_env,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["normalize"]["mode"], "incremental")
            self.assertEqual(payload["turn_rows"], 2)
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("select total_tokens from turns where session_id='s-current' and turn_id='t-current'").fetchone()[0], 123)
                self.assertEqual(con.execute("select total_tokens from turns where session_id='s-existing' and turn_id='t-existing'").fetchone()[0], 111)
            finally:
                con.close()

    def test_compact_custom_raw_paths_rotate_current_segments_without_active_rewrite(self) -> None:
        compact = load_module("compact_raw_selected_sources_test", ROOT / "scripts" / "compact_raw.py")
        raw_segments = load_module("raw_segments_compact_selected_sources_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            compact.BASE_DIR = base
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text('{"p":1}\n{"p":2}\n', encoding="utf-8")
            args = types.SimpleNamespace(rotate_current=True)
            result = compact.compact(args)

            self.assertEqual(result["metadata"]["raw_rotation_mode"], "current_segment_pointer")
            self.assertIn("current_segment", result["prompt_usage"])
            self.assertNotIn("model_calls", result)
            self.assertEqual(pathlib.Path(result["prompt_usage"]["closed_segment"]["path"]).read_text(encoding="utf-8"), '{"p":1}\n{"p":2}\n')

    def test_delete_affected_rollups_preserves_unrelated_rows(self) -> None:
        build = load_module("build_analytics_rollup_delete_test", ROOT / "scripts" / "build_analytics.py")
        con = sqlite3.connect(":memory:")
        try:
            con.execute(
                """
                create table task_rollups (
                  parent_session_id text,
                  parent_turn_id text,
                  child_session_id text,
                  child_agent_role text,
                  child_agent_nickname text,
                  child_started_at text,
                  child_started_unix real,
                  confidence text,
                  own_total_tokens integer,
                  child_total_tokens integer,
                  total_tokens integer,
                  own_weighted_credits real,
                  child_weighted_credits real,
                  total_weighted_credits real
                )
                """
            )
            con.executemany(
                "insert into task_rollups values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("p1", "t1", "c1", None, None, None, 0, "x", 1, 2, 3, 1.0, 2.0, 3.0),
                    ("p2", "t2", "c2", None, None, None, 0, "x", 1, 2, 3, 1.0, 2.0, 3.0),
                ],
            )
            build.delete_affected_rollups(con, {"p1"})
            self.assertEqual(con.execute("select parent_session_id from task_rollups").fetchall(), [("p2",)])
        finally:
            con.close()

    def test_affected_rollup_sessions_closes_over_nested_edges(self) -> None:
        build = load_module("build_analytics_rollup_closure_test", ROOT / "scripts" / "build_analytics.py")
        with mock.patch.object(build, "read_edges", return_value=[("grandparent", "parent", "ok"), ("parent", "child", "ok")]):
            self.assertEqual(build.affected_rollup_sessions({("child", "t1")}), {"grandparent", "parent", "child"})

    def test_retained_child_with_pruned_parent_is_not_generic_orphan(self) -> None:
        build = load_module("build_analytics_retention_pruned_parent_test", ROOT / "scripts" / "build_analytics.py")
        con = sqlite3.connect(":memory:")
        try:
            con.execute(
                """
                create table task_rollups (
                  parent_session_id text,
                  parent_turn_id text,
                  child_session_id text,
                  child_agent_role text,
                  child_agent_nickname text,
                  child_started_at text,
                  child_started_unix real,
                  confidence text,
                  own_total_tokens integer,
                  child_total_tokens integer,
                  total_tokens integer,
                  own_weighted_credits real,
                  child_weighted_credits real,
                  total_weighted_credits real
                )
                """
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                retention_state = pathlib.Path(tmp_dir) / "retention-pruned-turns.json"
                retention_state.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "cutoff_unix": datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp(),
                            "pruned_turns": [
                                {
                                    "session_id": "parent",
                                    "turn_id": "old-parent",
                                    "captured_at_unix": datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp(),
                                }
                            ],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                previous = build.RETENTION_PRUNED_TURNS_FILE
                build.RETENTION_PRUNED_TURNS_FILE = retention_state
                try:
                    with mock.patch.object(build, "read_edges", return_value=[("parent", "child", "ok")]):
                        build.rebuild_task_rollups(
                            con,
                            {"child": {"created_at_ms": int(datetime.fromisoformat("2026-01-10T00:00:00+00:00").timestamp() * 1000)}},
                            {
                                ("parent", "child"): {
                                    "turn_id": "old-parent",
                                    "spawn_started_at": "2026-01-01T00:00:00Z",
                                    "spawn_completed_at": "2026-01-01T00:00:01Z",
                                }
                            },
                            {("child", "new-child"): {"total_tokens": 200, "weighted_credits": 2.0}},
                            {},
                        )
                finally:
                    build.RETENTION_PRUNED_TURNS_FILE = previous
            row = con.execute(
                "select parent_session_id, parent_turn_id, child_session_id, confidence, own_total_tokens, total_tokens from task_rollups"
            ).fetchone()
            self.assertEqual(row, ("parent", "old-parent", "child", "parent_pruned_by_retention", 0, 200))
        finally:
            con.close()

    def test_task_rollup_indexes_child_usage_once_for_many_edges(self) -> None:
        build = load_module("build_task_rollup_child_index_test", ROOT / "scripts" / "build_analytics.py")

        class CountingDict(dict):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.items_calls = 0

            def items(self):  # type: ignore[override]
                self.items_calls += 1
                return super().items()

        con = sqlite3.connect(":memory:")
        try:
            build.setup_db(con)
            turn_usage = CountingDict(
                {
                    ("child-a", "t1"): {"total_tokens": 100, "weighted_credits": 1.0},
                    ("child-b", "t1"): {"total_tokens": 200, "weighted_credits": 2.0},
                    ("child-c", "t1"): {"total_tokens": 300, "weighted_credits": 3.0},
                    ("noise", "t1"): {"total_tokens": 999, "weighted_credits": 9.0},
                }
            )
            turn_ranges = {
                "parent": [
                    {
                        "turn_id": "parent-turn",
                        "start_ts": datetime.fromisoformat("2026-01-10T00:00:00+00:00").timestamp(),
                        "stop_ts": datetime.fromisoformat("2026-01-10T00:10:00+00:00").timestamp(),
                    }
                ]
            }
            created_at_ms = int(datetime.fromisoformat("2026-01-10T00:01:00+00:00").timestamp() * 1000)
            build.rebuild_task_rollups(
                con,
                {
                    "child-a": {"created_at_ms": created_at_ms},
                    "child-b": {"created_at_ms": created_at_ms},
                    "child-c": {"created_at_ms": created_at_ms},
                },
                {},
                turn_usage,
                turn_ranges,
                edges=[
                    ("parent", "child-a", "ok"),
                    ("parent", "child-b", "ok"),
                    ("parent", "child-c", "ok"),
                ],
            )
            totals = con.execute("select child_session_id, child_total_tokens from task_rollups order by child_session_id").fetchall()

            self.assertEqual(turn_usage.items_calls, 1)
            self.assertEqual(totals, [("child-a", 100), ("child-b", 200), ("child-c", 300)])
        finally:
            con.close()

    def test_tool_call_rollups_accept_streamed_batches_without_collecting_all_rows(self) -> None:
        build = load_module("build_tool_call_stream_batches_test", ROOT / "scripts" / "build_analytics.py")
        con = sqlite3.connect(":memory:")
        try:
            build.setup_db(con)
            batches = (
                [
                    {
                        "session_id": "s1",
                        "turn_id": "t1",
                        "call_id": "call-1",
                        "tool_name": "shell",
                        "tool_namespace": "shell",
                        "output_chars": 10,
                        "output_reported_tokens": 4,
                        "duration_ms": 20,
                        "status": "completed",
                        "output_preview": "one",
                    }
                ],
                [
                    {
                        "session_id": "s1",
                        "turn_id": "t1",
                        "call_id": "call-2",
                        "tool_name": "shell",
                        "tool_namespace": "shell",
                        "output_chars": 5,
                        "output_reported_tokens": 2,
                        "duration_ms": 30,
                        "status": "failed",
                        "output_preview": "two",
                    }
                ],
            )
            build.replace_tool_call_rollups_from_batches(con, iter(batches))
            row = con.execute(
                "select calls, output_chars, output_reported_tokens, failed_calls, total_duration_ms, max_duration_ms from tool_call_summaries"
            ).fetchone()

            self.assertEqual(row, (2, 15, 6, 1, 50, 30))
        finally:
            con.close()

    def test_log_cleanup_retention_does_not_mutate_raw_when_pruned_state_write_fails(self) -> None:
        cleanup = load_module("dashboard_cleanup_state_first_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            raw_prompt = raw_dir / "prompt-usage.raw.jsonl"
            raw_prompt.write_text(
                json.dumps(_turn_raw("parent", "old-parent", 100) | {"captured_at": "2026-01-01T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            before = raw_prompt.read_text(encoding="utf-8")
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()

            with mock.patch.object(cleanup._retention, "stage_pruned_turn_state", side_effect=OSError("state write failed")):
                with self.assertRaises(OSError):
                    cleanup.delete_logs_older_than(base, cutoff_unix)

            self.assertEqual(raw_prompt.read_text(encoding="utf-8"), before)

    def test_pruned_turn_state_uses_started_and_stopped_at_for_rollup_matching(self) -> None:
        build = load_module("build_analytics_pruned_time_range_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            retention_state = pathlib.Path(tmp_dir) / "retention-pruned-turns.json"
            retention_state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pruned_turns": [
                            {
                                "session_id": "parent",
                                "turn_id": "old-parent",
                                "captured_at_unix": datetime.fromisoformat("2026-01-02T00:00:00+00:00").timestamp(),
                                "started_at": "2026-01-01T00:00:00Z",
                                "stopped_at": "2026-01-01T00:00:10Z",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            previous = build.RETENTION_PRUNED_TURNS_FILE
            build.RETENTION_PRUNED_TURNS_FILE = retention_state
            try:
                rows = build.read_retention_pruned_turns()
            finally:
                build.RETENTION_PRUNED_TURNS_FILE = previous

        row = rows["parent"][0]
        self.assertEqual(row["start_ts"], datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp())
        self.assertEqual(row["stop_ts"], datetime.fromisoformat("2026-01-01T00:00:10+00:00").timestamp())

    def test_pruned_turn_reader_includes_pending_retention_state(self) -> None:
        build = load_module("build_analytics_pending_pruned_time_range_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            retention_state = pathlib.Path(tmp_dir) / "retention-pruned-turns.json"
            pending_state = retention_state.with_name("retention-pruned-turns.pending.json")
            pending_state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pruned_turns": [
                            {
                                "session_id": "parent",
                                "turn_id": "pending-parent",
                                "captured_at": "2026-01-01T00:00:00+00:00",
                                "started_at": "2026-01-01T00:00:00+00:00",
                                "stopped_at": "2026-01-01T00:00:10+00:00",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            previous = build.RETENTION_PRUNED_TURNS_FILE
            build.RETENTION_PRUNED_TURNS_FILE = retention_state
            try:
                rows = build.read_retention_pruned_turns()
            finally:
                build.RETENTION_PRUNED_TURNS_FILE = previous

        self.assertEqual(rows["parent"][0]["turn_id"], "pending-parent")

    def test_incremental_build_filters_spawn_context_threads_to_affected_sessions(self) -> None:
        build = load_module("build_analytics_spawn_context_filter_test", ROOT / "scripts" / "build_analytics.py")
        threads = {
            "parent": {"rollout_path": "/tmp/parent.jsonl"},
            "child": {"rollout_path": "/tmp/child.jsonl"},
            "unrelated": {"rollout_path": "/tmp/unrelated.jsonl"},
        }

        filtered = build.spawn_context_threads_for_affected_sessions(threads, {"child", "parent"})

        self.assertEqual(set(filtered), {"parent", "child"})
        self.assertNotIn("unrelated", filtered)

    def test_reconcile_completed_index_reads_current_segments(self) -> None:
        reconcile = load_module("reconcile_current_segment_index_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            raw_dir = base / "raw"
            current_dir = raw_dir / "current"
            state_dir = base / "state"
            current_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            current_path.write_text(json.dumps(_turn_raw("s-current", "t-current", total=100)) + "\n", encoding="utf-8")
            pointer = {
                "schema_version": 1,
                "base": str(base.resolve()),
                "current": {
                    "prompt_usage": {
                        "id": "prompt-usage.raw.jsonl.current.1779235200000000000",
                        "kind": "prompt_usage",
                        "path": str(current_path),
                        "source_name": "prompt-usage.raw.jsonl",
                        "created_at_unix": 1779235200.0,
                    }
                },
            }
            (state_dir / "current-raw-segments.json").write_text(json.dumps(pointer) + "\n", encoding="utf-8")
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            reconcile.RAW_LOG = raw_dir / "prompt-usage.raw.jsonl"
            reconcile.ARCHIVE_DIR = raw_dir / "archive"

            completed = reconcile.completed_turn_index()

        self.assertIn(("s-current", "t-current"), completed)

    def test_reconcile_iter_jsonl_raises_on_read_error(self) -> None:
        reconcile = load_module("reconcile_iter_jsonl_read_error_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "prompt-usage.raw.jsonl"
            path.write_text(json.dumps(_turn_raw("s1", "t1", total=1)) + "\n", encoding="utf-8")

            with mock.patch("builtins.open", side_effect=OSError("read blocked")):
                with self.assertRaises(OSError):
                    list(reconcile.iter_jsonl(path))

    def test_reconcile_writes_recovered_turn_to_current_segment(self) -> None:
        reconcile = load_module("reconcile_append_current_segment_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "timestamp": "2026-05-31T10:00:00.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {"input_tokens": 15, "total_tokens": 15},
                                    "last_token_usage": {"input_tokens": 15, "total_tokens": 15},
                                },
                            },
                        },
                        {
                            "timestamp": "2026-05-31T10:00:01.000Z",
                            "type": "event_msg",
                            "payload": {"type": "task_complete", "turn_id": "t-recovered", "completed_at": 1780221601},
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            pending = state_dir / "pending.json"
            pending.write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-recovered",
                        "turn_id": "t-recovered",
                        "cwd": str(pathlib.Path(tmp)),
                        "transcript_path": str(transcript),
                        "captured_at": "2026-05-31T10:00:00+00:00",
                        "start_token_usage": {"input_tokens": 0, "total_tokens": 0},
                    }
                ),
                encoding="utf-8",
            )
            reconcile.CODEX_HOME = pathlib.Path(tmp)
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            reconcile.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            reconcile.ARCHIVE_DIR = base / "raw" / "archive"

            completed = set()
            result = reconcile.reconcile_one(pending, completed)
            current_paths = reconcile.raw_segments.current_segment_paths(base, kind="prompt_usage")
            current_payload = current_paths[0].read_text(encoding="utf-8") if current_paths else ""

            self.assertEqual(result, "completed")
            self.assertIn(("s-recovered", "t-recovered"), completed)
            self.assertFalse((base / "raw" / "prompt-usage.raw.jsonl").exists())
            self.assertEqual(len(current_paths), 1)
            self.assertIn('"session_id":"s-recovered"', current_payload)

    def test_reconcile_rechecks_current_segments_before_append(self) -> None:
        reconcile = load_module("reconcile_append_race_recheck_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            current = reconcile.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text(json.dumps(_turn_raw("s-race", "t-race", total=10)) + "\n", encoding="utf-8")
            pending = state_dir / "pending.json"
            pending.write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-race",
                        "turn_id": "t-race",
                        "cwd": str(pathlib.Path(tmp)),
                        "transcript_path": str(pathlib.Path(tmp) / "rollout.jsonl"),
                        "captured_at": "2026-05-31T10:00:00+00:00",
                        "start_token_usage": {"input_tokens": 0, "total_tokens": 0},
                    }
                ),
                encoding="utf-8",
            )
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            with (
                mock.patch.object(
                    reconcile,
                    "latest_token_until_turn_end",
                    return_value=(
                        {"found": True, "total_token_usage": {"input_tokens": 10, "total_tokens": 10}, "model_calls": []},
                        {"type": "task_complete"},
                    ),
                ),
                mock.patch.object(reconcile, "completed_turn_index", side_effect=AssertionError("reconcile_one must not rebuild the full completed index")),
                mock.patch.object(reconcile.hook, "append_prompt_usage", side_effect=AssertionError("duplicate turn must not append")),
            ):
                completed = set()
                result = reconcile.reconcile_one(pending, completed)

        self.assertEqual(result, "duplicate")
        self.assertIn(("s-race", "t-race"), completed)

    def test_reconcile_recovers_missing_start_stop_marker(self) -> None:
        reconcile = load_module("reconcile_missing_start_stop_marker_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {"timestamp": "2026-05-31T10:00:00.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-marker"}},
                        {
                            "timestamp": "2026-05-31T10:00:01.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 12}
                                },
                            },
                        },
                        {"timestamp": "2026-05-31T10:00:02.000Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-marker"}},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            marker = state_dir / "marker.json"
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "record_type": "turn_stop_missing_start",
                        "captured_at": "2026-05-31T10:00:03+00:00",
                        "session_id": "s-marker",
                        "turn_id": "t-marker",
                        "transcript_path": str(transcript),
                        "cwd": str(pathlib.Path(tmp)),
                        "model": "gpt-5.5",
                        "hook_input": {"hook_event_name": "Stop"},
                    }
                ),
                encoding="utf-8",
            )
            reconcile.CODEX_HOME = pathlib.Path(tmp)
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            reconcile.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            reconcile.ARCHIVE_DIR = base / "raw" / "archive"

            result = reconcile.reconcile_one(marker, set())
            current_paths = reconcile.raw_segments.current_segment_paths(base, kind="prompt_usage")
            records = [json.loads(line) for line in current_paths[0].read_text(encoding="utf-8").splitlines()]
            marker_exists = marker.exists()

        self.assertEqual(result, "completed")
        self.assertFalse(marker_exists)
        self.assertEqual(records[0]["turn_status"], "completed")
        self.assertEqual(records[0]["lifecycle_end_reason"], "goal_auto_completed")
        self.assertFalse(records[0]["start_state_found"])
        self.assertEqual(records[0]["usage"]["total_tokens"], 12)

    def test_reconcile_does_not_use_token_count_before_turn_start_when_offset_missing(self) -> None:
        reconcile = load_module("reconcile_bounds_token_counts_to_turn_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "timestamp": "2026-05-31T09:59:59.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {"input_tokens": 999, "total_tokens": 999},
                                    "last_token_usage": {"input_tokens": 999, "total_tokens": 999},
                                },
                            },
                        },
                        {"timestamp": "2026-05-31T10:00:00.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-new"}},
                        {"timestamp": "2026-05-31T10:00:02.000Z", "type": "event_msg", "payload": {"type": "task_aborted", "turn_id": "t-new", "reason": "cancelled"}},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            pending = state_dir / "pending.json"
            pending.write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-new",
                        "turn_id": "t-new",
                        "transcript_path": str(transcript),
                        "captured_at": "2026-05-31T10:00:00+00:00",
                        "start_token_usage": {"input_tokens": 0, "total_tokens": 0},
                        "prompt": {"prompt_preview": "new turn"},
                    }
                ),
                encoding="utf-8",
            )
            reconcile.CODEX_HOME = pathlib.Path(tmp)
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            reconcile.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            reconcile.ARCHIVE_DIR = base / "raw" / "archive"

            result = reconcile.reconcile_one(pending, set())
            current_paths = reconcile.raw_segments.current_segment_paths(base, kind="prompt_usage")
            records = [json.loads(line) for line in current_paths[0].read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result, "aborted")
        self.assertEqual(records[0]["usage"]["total_tokens"], 0)
        self.assertEqual(records[0]["end_token_snapshot"]["reason"], "no_token_count_before_task_aborted")

    def test_reconcile_recovers_task_aborted_turns(self) -> None:
        reconcile = load_module("reconcile_task_aborted_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            transcript = pathlib.Path(tmp) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {"timestamp": "2026-05-31T10:00:00.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-abort"}},
                        {"timestamp": "2026-05-31T10:00:01.000Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "total_tokens": 10}}}},
                        {"timestamp": "2026-05-31T10:00:02.000Z", "type": "event_msg", "payload": {"type": "task_aborted", "turn_id": "t-abort", "reason": "cancelled"}},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            pending = state_dir / "pending.json"
            pending.write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-abort",
                        "turn_id": "t-abort",
                        "transcript_path": str(transcript),
                        "captured_at": "2026-05-31T10:00:00+00:00",
                        "start_file_size": 0,
                        "start_token_usage": {},
                        "start_token_snapshot": {},
                        "prompt": {"prompt_preview": "abort me"},
                    }
                ),
                encoding="utf-8",
            )
            reconcile.CODEX_HOME = pathlib.Path(tmp)
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            reconcile.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            reconcile.ARCHIVE_DIR = base / "raw" / "archive"

            result = reconcile.reconcile_one(pending, set())
            current_paths = reconcile.raw_segments.current_segment_paths(base, kind="prompt_usage")
            records = [json.loads(line) for line in current_paths[0].read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result, "aborted")
        self.assertFalse(pending.exists())
        self.assertEqual(records[0]["turn_status"], "aborted")
        self.assertEqual(records[0]["lifecycle_end_reason"], "cancelled")

    def test_reconcile_excludes_missing_transcript_state(self) -> None:
        reconcile = load_module("reconcile_missing_transcript_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            pending = state_dir / "pending.json"
            pending.write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-missing-transcript",
                        "turn_id": "t-missing-transcript",
                        "cwd": str(pathlib.Path(tmp)),
                        "transcript_path": None,
                        "captured_at": "2026-05-31T10:00:00+00:00",
                        "start_token_usage": {"input_tokens": 12, "total_tokens": 12},
                        "start_token_snapshot": {"found": False, "reason": "missing_transcript_path"},
                        "prompt": {"prompt_preview": "missing transcript"},
                    }
                ),
                encoding="utf-8",
            )
            reconcile.CODEX_HOME = pathlib.Path(tmp)
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            reconcile.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            reconcile.ARCHIVE_DIR = base / "raw" / "archive"

            result = reconcile.reconcile_one(pending, set())
            current_paths = reconcile.raw_segments.current_segment_paths(base, kind="prompt_usage")

        self.assertEqual(result, "excluded_missing_transcript_path")
        self.assertFalse(pending.exists())
        self.assertEqual(current_paths, [])

    def test_reconcile_ignores_service_state_json_files(self) -> None:
        reconcile = load_module("reconcile_ignores_service_state_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            service_state = state_dir / "current-raw-segments.json"
            service_state.write_text(
                json.dumps({"schema_version": 1, "current": {"prompt_usage": {"id": "p1"}}}),
                encoding="utf-8",
            )
            reconcile.CODEX_HOME = pathlib.Path(tmp)
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir

            result = reconcile.reconcile_one(service_state, set())
            service_state_exists = service_state.exists()

        self.assertEqual(result, "ignored")
        self.assertTrue(service_state_exists)

    def test_reconcile_completed_index_reads_pending_rotation_segment_before_recovery(self) -> None:
        reconcile = load_module("reconcile_pending_rotation_index_test", ROOT / "scripts" / "reconcile.py")
        raw_segments = reconcile.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            old_segment = raw_segments.new_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            new_segment = raw_segments.new_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(old_segment["path"]).write_text(json.dumps(_turn_raw("s-old", "t-old", total=100)) + "\n", encoding="utf-8")
            pathlib.Path(new_segment["path"]).write_text("", encoding="utf-8")
            raw_segments.write_current_pointer(base, raw_segments.empty_current_pointer(base) | {"current": {"prompt_usage": new_segment}})
            raw_segments.write_pending_rotation(
                base,
                {
                    "operation": "rotate_current_segment",
                    "phase": "manifest_pending",
                    "kind": "prompt_usage",
                    "old_segment": old_segment,
                    "new_segment": new_segment,
                    "created_at_unix": 1.0,
                },
            )
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = base / "state"
            reconcile.RAW_LOG = base / "raw" / "prompt-usage.raw.jsonl"
            reconcile.ARCHIVE_DIR = base / "raw" / "archive"

            completed = reconcile.completed_turn_index()

        self.assertIn(("s-old", "t-old"), completed)

    def test_reconcile_completed_index_fails_on_corrupt_current_pointer(self) -> None:
        reconcile = load_module("reconcile_corrupt_current_segment_index_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            raw_dir = base / "raw"
            current_dir = raw_dir / "current"
            state_dir = base / "state"
            current_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            current_path.write_text(json.dumps(_turn_raw("s-current", "t-current", total=100)) + "\n", encoding="utf-8")
            pointer = {
                "schema_version": 1,
                "base": str((base / "other").resolve()),
                "current": {
                    "prompt_usage": {
                        "id": "prompt-usage.raw.jsonl.current.1779235200000000000",
                        "kind": "prompt_usage",
                        "path": str(current_path),
                        "source_name": "prompt-usage.raw.jsonl",
                        "created_at_unix": 1779235200.0,
                    }
                },
            }
            (state_dir / "current-raw-segments.json").write_text(json.dumps(pointer) + "\n", encoding="utf-8")
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            reconcile.RAW_LOG = raw_dir / "prompt-usage.raw.jsonl"
            reconcile.ARCHIVE_DIR = raw_dir / "archive"

            with self.assertRaises(reconcile.raw_segments.ManifestError):
                reconcile.completed_turn_index()
