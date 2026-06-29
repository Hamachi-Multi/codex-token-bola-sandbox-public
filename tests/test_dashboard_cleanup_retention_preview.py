from __future__ import annotations

try:
    from tests.support import (
        Any,
        DashboardFixtureMixin,
        ROOT,
        _raw_segment,
        _turn_raw,
        datetime,
        gzip,
        hashlib,
        json,
        load_module,
        mock,
        pathlib,
        tempfile,
        timezone,
        unittest,
    )
except ModuleNotFoundError:
    from support import (
        Any,
        DashboardFixtureMixin,
        ROOT,
        _raw_segment,
        _turn_raw,
        datetime,
        gzip,
        hashlib,
        json,
        load_module,
        mock,
        pathlib,
        tempfile,
        timezone,
        unittest,
    )

class DashboardCleanupRetentionPreviewTests(DashboardFixtureMixin, unittest.TestCase):
    LEGACY_CLEANUP_ROW_FIELDS = {
        "retention_role",
        "retention_impact",
        "delete_all_impact",
        "retention_summary",
        "delete_all_summary",
    }

    def assert_cleanup_row_uses_display_contract(self, row: dict[str, Any]) -> None:
        self.assertTrue(self.LEGACY_CLEANUP_ROW_FIELDS.isdisjoint(row))
        self.assertIn("display", row)
        self.assertIn("delete_all_display", row)

    def test_log_cleanup_retention_filters_raw_and_archived_rows_by_timestamp(self) -> None:
        cleanup = load_module("dashboard_cleanup_retention_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            archive_dir = raw_dir / "archive"
            archive_dir.mkdir(parents=True)
            current = cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])

            old_turn = _turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}
            new_turn = _turn_raw("s1", "new", 200) | {"captured_at": "2026-01-10T00:00:00Z"}
            unknown_turn = _turn_raw("s1", "unknown", 300) | {"captured_at": None}
            raw_prompt.write_text(
                "".join(json.dumps(row) + "\n" for row in [old_turn, new_turn, unknown_turn]),
                encoding="utf-8",
            )

            mixed_archive = archive_dir / "prompt-usage.raw.jsonl.20260110.jsonl.gz"
            archive_payload = (
                json.dumps(_turn_raw("s2", "arch-old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}) + "\n"
                + json.dumps(_turn_raw("s2", "arch-new", 200) | {"captured_at": "2026-01-10T00:00:00Z"}) + "\n"
            ).encode()
            with gzip.open(mixed_archive, "wt", encoding="utf-8") as handle:
                handle.write(archive_payload.decode())
            cleanup.raw_segments.write_manifest(
                base,
                cleanup.raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        _raw_segment(
                            mixed_archive,
                            payload=archive_payload,
                            min_time=datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp(),
                            max_time=datetime.fromisoformat("2026-01-10T00:00:00+00:00").timestamp(),
                            rows=2,
                            days=[
                                [int(datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp()), 1],
                                [int(datetime.fromisoformat("2026-01-10T00:00:00+00:00").timestamp()), 1],
                            ],
                        )
                    ]
                },
            )

            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()
            result = cleanup.delete_logs_older_than(base, cutoff_unix)

            kept_rows = []
            for segment in cleanup.raw_segments.read_manifest(base).get("segments", []):
                path = pathlib.Path(str(segment.get("path") or ""))
                if path.suffix == ".gz":
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        kept_rows.extend(json.loads(line) for line in handle)
                else:
                    kept_rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())

        self.assertEqual(result["deleted_rows"], 2)
        self.assertEqual(result["rewritten_files"], 2)
        self.assertEqual(result["deleted_files"], 2)
        self.assertEqual(sorted(row["turn_id"] for row in kept_rows), ["arch-new", "new", "unknown"])

    def test_log_cleanup_retention_records_pruned_prompt_turns(self) -> None:
        cleanup = load_module("dashboard_cleanup_retention_pruned_turns_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            archive_dir = raw_dir / "archive"
            archive_dir.mkdir(parents=True)
            current = cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])
            raw_prompt.write_text(
                "".join(
                    json.dumps(row) + "\n"
                    for row in [
                        _turn_raw("parent", "old-parent", 100) | {"captured_at": "2026-01-01T00:00:00Z"},
                        _turn_raw("child", "new-child", 200) | {"captured_at": "2026-01-10T00:00:00Z"},
                    ]
                ),
                encoding="utf-8",
            )
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()

            result = cleanup.delete_logs_older_than(base, cutoff_unix)
            state = json.loads((base / "state" / "retention-pruned-turns.json").read_text(encoding="utf-8"))

        self.assertEqual(result["deleted_turns"], 1)
        self.assertEqual(state["cutoff_unix"], cutoff_unix)
        self.assertEqual(
            [(row["session_id"], row["turn_id"]) for row in state["pruned_turns"]],
            [("parent", "old-parent")],
        )

    def test_log_cleanup_retention_deletes_old_pending_turn_state(self) -> None:
        cleanup = load_module("dashboard_cleanup_retention_pending_turn_state_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            state_dir = base / "state"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            (raw_dir / "prompt-usage.raw.jsonl").write_text("", encoding="utf-8")
            old_state = state_dir / ("a" * 32 + ".json")
            old_state.write_text(
                json.dumps({"record_type": "turn_start", "captured_at": "2026-05-01T00:00:00+00:00", "session_id": "s-old", "turn_id": "t-old"}) + "\n",
                encoding="utf-8",
            )
            new_state = state_dir / ("b" * 32 + ".json")
            new_state.write_text(
                json.dumps({"record_type": "turn_start", "captured_at": "2026-05-23T00:00:00+00:00", "session_id": "s-new", "turn_id": "t-new"}) + "\n",
                encoding="utf-8",
            )
            service_state = state_dir / "custom-state.json"
            service_state.write_text("{}\n", encoding="utf-8")
            cutoff_unix = datetime.fromisoformat("2026-05-20T00:00:00+00:00").timestamp()

            result = cleanup.delete_logs_older_than(base, cutoff_unix)
            old_exists = old_state.exists()
            new_exists = new_state.exists()
            service_exists = service_state.exists()

        self.assertEqual(result["deleted_rows"], 0)
        self.assertEqual(result["deleted_state_files"], 1)
        self.assertFalse(old_exists)
        self.assertTrue(new_exists)
        self.assertTrue(service_exists)

    def test_log_cleanup_preview_does_not_reconcile_pending_apply_marker(self) -> None:
        cleanup = load_module("cleanup_preview_readonly_apply_marker_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = cleanup.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s-old", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            old = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1, days=[[1777593600, 1, len(payload)]])
            previous_manifest = raw_segments.empty_manifest(base) | {"segments": [old]}
            next_manifest = raw_segments.empty_manifest(base) | {"segments": []}
            raw_segments.write_manifest(base, previous_manifest)
            raw_segments.write_apply_marker(base, {"phase": "manifest_pending", "previous_manifest": previous_manifest, "source_segments": [old], "retained_segments": [], "next_manifest": next_manifest})

            with self.assertRaises(raw_segments.ManifestError):
                cleanup.retention_preview(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            self.assertTrue(old_segment.exists())
            self.assertTrue(raw_segments.segment_apply_marker_path(base).exists())
            self.assertEqual(raw_segments.strict_read_manifest(base)["segments"], [old])

    def test_log_cleanup_preview_rejects_corrupt_manifest(self) -> None:
        cleanup = load_module("cleanup_preview_corrupt_manifest_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state = base / "state"
            state.mkdir(parents=True)
            (state / "raw-segments-manifest.json").write_text("{broken-json", encoding="utf-8")

            with self.assertRaises(cleanup.raw_segments.ManifestError):
                cleanup.retention_preview(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

    def test_log_cleanup_preview_includes_active_current_segment(self) -> None:
        cleanup = load_module("cleanup_preview_current_segment_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = cleanup.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            current_path.write_text(json.dumps(_turn_raw("s-current", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n", encoding="utf-8")
            raw_segments.write_current_pointer(
                base,
                {
                    "current": {
                        "prompt_usage": {
                            "id": "prompt-usage.raw.jsonl.current.1777593600000000000",
                            "kind": "prompt_usage",
                            "path": str(current_path),
                            "source_name": "prompt-usage.raw.jsonl",
                            "created_at_unix": 1777593600.0,
                        }
                    }
                },
            )

            preview = cleanup.retention_preview(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            delete_result = cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

        self.assertEqual(preview["deletable_rows"], 1)
        self.assertEqual(delete_result["deleted_rows"], 1)

    def test_log_cleanup_preview_indexes_current_segment_without_repeated_full_scan(self) -> None:
        cleanup = load_module("cleanup_preview_current_segment_index_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = cleanup.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            old_line = json.dumps(_turn_raw("s-current", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            current_path.write_text(old_line, encoding="utf-8")
            raw_segments.write_current_pointer(
                base,
                {
                    "current": {
                        "prompt_usage": {
                            "id": "prompt-usage.raw.jsonl.current.1777593600000000000",
                            "kind": "prompt_usage",
                            "path": str(current_path),
                            "source_name": "prompt-usage.raw.jsonl",
                            "created_at_unix": 1777593600.0,
                        }
                    }
                },
            )
            cutoff = datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp()
            later_cutoff = datetime(2026, 5, 21, tzinfo=timezone.utc).timestamp()
            raw_scan_calls = 0
            index_scan_calls = 0
            original_raw_scan = raw_segments._retention.scan_segment_file_for_cutoff
            original_index_scan = cleanup._retention.scan_retention_source_for_index_from_offset

            def counted_raw_scan(*args: Any, **kwargs: Any) -> dict[str, int]:
                nonlocal raw_scan_calls
                raw_scan_calls += 1
                return original_raw_scan(*args, **kwargs)

            def counted_index_scan(*args: Any, **kwargs: Any) -> dict[str, Any]:
                nonlocal index_scan_calls
                if args and pathlib.Path(args[0]) == current_path:
                    index_scan_calls += 1
                return original_index_scan(*args, **kwargs)

            with mock.patch.object(raw_segments._retention, "scan_segment_file_for_cutoff", side_effect=counted_raw_scan), mock.patch.object(cleanup._retention, "scan_retention_source_for_index_from_offset", side_effect=counted_index_scan):
                first = cleanup.retention_preview(base, cutoff)
                second = cleanup.retention_preview(base, later_cutoff)

        self.assertEqual(first["deletable_rows"], 1)
        self.assertEqual(second["deletable_rows"], 1)
        self.assertEqual(raw_scan_calls, 0)
        self.assertEqual(index_scan_calls, 1)

    def test_log_cleanup_preview_incrementally_indexes_appended_current_segment(self) -> None:
        cleanup = load_module("cleanup_preview_current_segment_append_index_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = cleanup.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            old_line = json.dumps(_turn_raw("s-current", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            new_line = json.dumps(_turn_raw("s-current", "new", total=100) | {"captured_at": "2026-05-22T00:00:00+00:00"}) + "\n"
            current_path.write_text(old_line, encoding="utf-8")
            raw_segments.write_current_pointer(
                base,
                {
                    "current": {
                        "prompt_usage": {
                            "id": "prompt-usage.raw.jsonl.current.1777593600000000000",
                            "kind": "prompt_usage",
                            "path": str(current_path),
                            "source_name": "prompt-usage.raw.jsonl",
                            "created_at_unix": 1777593600.0,
                        }
                    }
                },
            )
            cutoff = datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp()
            offsets: list[int] = []
            original_index_scan = cleanup._retention.scan_retention_source_for_index_from_offset

            def counted_index_scan(path: pathlib.Path, offset: int, **kwargs: Any) -> dict[str, Any]:
                if pathlib.Path(path) == current_path:
                    offsets.append(offset)
                return original_index_scan(path, offset, **kwargs)

            with mock.patch.object(cleanup._retention, "scan_retention_source_for_index_from_offset", side_effect=counted_index_scan):
                first = cleanup.retention_preview(base, cutoff)
                initial_size = current_path.stat().st_size
                with current_path.open("a", encoding="utf-8") as handle:
                    handle.write(new_line)
                second = cleanup.retention_preview(base, cutoff)

        self.assertEqual(first["scanned_rows"], 1)
        self.assertEqual(second["scanned_rows"], 2)
        self.assertEqual(second["deletable_rows"], 1)
        self.assertEqual(offsets, [0, initial_size])

    def test_log_cleanup_preview_rejects_pending_rotation_marker(self) -> None:
        cleanup = load_module("cleanup_preview_pending_rotation_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = cleanup.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            old_path = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            new_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            old_path.write_text(json.dumps(_turn_raw("s-current", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n", encoding="utf-8")
            new_path.write_text("", encoding="utf-8")
            old_segment = {"id": "prompt-usage.raw.jsonl.current.1777593600000000000", "kind": "prompt_usage", "path": str(old_path), "source_name": "prompt-usage.raw.jsonl", "created_at_unix": 1777593600.0}
            new_segment = {"id": "prompt-usage.raw.jsonl.current.1779235200000000000", "kind": "prompt_usage", "path": str(new_path), "source_name": "prompt-usage.raw.jsonl", "created_at_unix": 1779235200.0}
            raw_segments.write_current_pointer(base, {"current": {"prompt_usage": new_segment}})
            raw_segments.write_pending_rotation(base, {"operation": "rotate_current_segment", "phase": "manifest_pending", "kind": "prompt_usage", "old_segment": old_segment, "new_segment": new_segment, "created_at_unix": 1.0})

            with self.assertRaises(raw_segments.ManifestError):
                cleanup.retention_preview(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

    def test_log_cleanup_preview_rejects_symlinked_archive_entry(self) -> None:
        cleanup = load_module("cleanup_preview_symlink_archive_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            archive_dir = base / "raw" / "archive"
            external = pathlib.Path(tmp) / "external.jsonl.gz"
            archive_dir.mkdir(parents=True)
            payload = json.dumps(_turn_raw("s-external", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            with gzip.open(external, "wt", encoding="utf-8") as handle:
                handle.write(payload)
            segment_path = archive_dir / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            segment_path.symlink_to(external)
            cleanup.raw_segments.write_manifest(
                base,
                cleanup.raw_segments.empty_manifest(base)
                | {"segments": [_raw_segment(segment_path, payload=payload.encode(), min_time=1777593600.0, max_time=1777593600.0, rows=1)]},
            )

            with self.assertRaises(cleanup.raw_segments.ManifestError):
                cleanup.retention_preview(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

    def test_log_cleanup_retention_preview_reuses_cache_until_sources_change(self) -> None:
        cleanup = load_module("dashboard_cleanup_cache_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            current = cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])
            raw_prompt.write_text(
                json.dumps(_turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()

            with mock.patch.object(cleanup._retention, "scan_retention_source_for_index", wraps=cleanup._retention.scan_retention_source_for_index) as scan:
                first = cleanup.retention_preview(base, cutoff_unix)
                second = cleanup.retention_preview(base, cutoff_unix)
                raw_prompt.write_text(
                    raw_prompt.read_text(encoding="utf-8")
                    + json.dumps(_turn_raw("s1", "new", 200) | {"captured_at": "2026-01-10T00:00:00Z"}) + "\n",
                    encoding="utf-8",
                )
                third = cleanup.retention_preview(base, cutoff_unix)

        self.assertEqual(first["deletable_rows"], 1)
        self.assertEqual(second["deletable_rows"], 1)
        self.assertEqual(third["scanned_rows"], 2)
        self.assertEqual(scan.call_count, 1)
        self.assertTrue(first["from_index"])
        self.assertTrue(second["from_index"])
        self.assertTrue(third["from_index"])

    def test_retention_preview_signature_changes_when_current_segment_appends(self) -> None:
        cleanup = load_module("cleanup_current_signature_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_current_signature_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(str(current["path"]))
            current_path.write_text(
                json.dumps(_turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()
            first = cleanup.retention_preview_signature(base, cutoff_unix)
            current_path.write_text(
                current_path.read_text(encoding="utf-8")
                + json.dumps(_turn_raw("s1", "older", 100) | {"captured_at": "2026-01-02T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            second = cleanup.retention_preview_signature(base, cutoff_unix)

        self.assertNotEqual(first, second)

    def test_retention_preview_uses_manifest_segment_bounds_without_rescanning_gzip(self) -> None:
        cleanup = load_module("cleanup_segment_preview_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_preview_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            retained_segment = archive / "prompt-usage.raw.jsonl.20260523000000.20260523000000.2.jsonl.gz"
            old_payload = json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            retained_payload = json.dumps(_turn_raw("s2", "new", total=100) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n"
            with gzip.open(old_segment, "wt", encoding="utf-8") as handle:
                handle.write(old_payload)
            with gzip.open(retained_segment, "wt", encoding="utf-8") as handle:
                handle.write(retained_payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        {
                            "id": "prompt-usage.raw.jsonl.20260501000000.20260501000000.1",
                            "kind": "prompt_usage",
                            "path": str(old_segment),
                            "format": "jsonl.gz",
                            "source_name": "prompt-usage.raw.jsonl",
                            "min_time_unix": 1777593600.0,
                            "max_time_unix": 1777593600.0,
                            "rows": 1,
                            "undated_rows": 0,
                            "corrupt_rows": 0,
                            "unknown_rows": 0,
                            "days": [[1777593600, 1, len(old_payload.encode("utf-8"))]],
                            "bytes": old_segment.stat().st_size,
                            "uncompressed_bytes": len(old_payload.encode("utf-8")),
                            "sha256": hashlib.sha256(old_payload.encode("utf-8")).hexdigest(),
                            "status": "closed",
                        },
                        {
                            "id": "new",
                            "kind": "prompt_usage",
                            "path": str(retained_segment),
                            "format": "jsonl.gz",
                            "source_name": "prompt-usage.raw.jsonl",
                            "min_time_unix": 1779494400.0,
                            "max_time_unix": 1779494400.0,
                            "rows": 1,
                            "undated_rows": 0,
                            "corrupt_rows": 0,
                            "unknown_rows": 0,
                            "days": [[1779494400, 1, len(retained_payload.encode("utf-8"))]],
                            "bytes": retained_segment.stat().st_size,
                            "uncompressed_bytes": len(retained_payload.encode("utf-8")),
                            "sha256": hashlib.sha256(retained_payload.encode("utf-8")).hexdigest(),
                            "status": "closed",
                        },
                    ]
                },
            )

            preview = cleanup.retention_preview(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

        self.assertEqual(preview["deletable_rows"], 1)
        self.assertEqual(preview["affected_files"], 1)
        self.assertTrue(preview["from_manifest"])

    def test_retention_preview_rejects_missing_day_histogram_for_mixed_segment(self) -> None:
        cleanup = load_module("cleanup_segment_preview_missing_days_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_preview_missing_days_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            mixed_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260523000000.1.jsonl.gz"
            old_line = json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            new_line = json.dumps(_turn_raw("s2", "new", total=100) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n"
            payload = old_line + new_line
            with gzip.open(mixed_segment, "wt", encoding="utf-8") as handle:
                handle.write(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        {
                            "id": "prompt-usage.raw.jsonl.20260501000000.20260523000000.1",
                            "kind": "prompt_usage",
                            "path": str(mixed_segment),
                            "format": "jsonl.gz",
                            "source_name": "prompt-usage.raw.jsonl",
                            "min_time_unix": 1777593600.0,
                            "max_time_unix": 1779494400.0,
                            "rows": 2,
                            "undated_rows": 0,
                            "corrupt_rows": 0,
                            "unknown_rows": 0,
                            "bytes": mixed_segment.stat().st_size,
                            "uncompressed_bytes": len(payload.encode("utf-8")),
                            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                            "status": "closed",
                        }
                    ]
                },
            )

            with self.assertRaises(cleanup.raw_segments.ManifestError):
                cleanup.retention_preview(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

    def test_retention_preview_non_day_cutoff_scans_manifest_mixed_segment_exactly(self) -> None:
        cleanup = load_module("cleanup_segment_preview_exact_fallback_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_preview_exact_fallback_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            mixed_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260523000000.1.jsonl.gz"
            old_line = json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T01:00:00+00:00"}) + "\n"
            new_line = json.dumps(_turn_raw("s2", "new", total=100) | {"captured_at": "2026-05-23T01:00:00+00:00"}) + "\n"
            with gzip.open(mixed_segment, "wt", encoding="utf-8") as handle:
                handle.write(old_line)
                handle.write(new_line)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        {
                            "id": "prompt-usage.raw.jsonl.20260501000000.20260523000000.1",
                            "kind": "prompt_usage",
                            "path": str(mixed_segment),
                            "format": "jsonl.gz",
                            "source_name": "prompt-usage.raw.jsonl",
                            "min_time_unix": 1777597200.0,
                            "max_time_unix": 1779498000.0,
                            "rows": 2,
                            "undated_rows": 0,
                            "corrupt_rows": 0,
                            "unknown_rows": 0,
                            "days": [[1777593600, 1, len(old_line.encode("utf-8"))], [1779494400, 1, len(new_line.encode("utf-8"))]],
                            "bytes": mixed_segment.stat().st_size,
                            "uncompressed_bytes": len((old_line + new_line).encode("utf-8")),
                            "sha256": hashlib.sha256((old_line + new_line).encode("utf-8")).hexdigest(),
                            "status": "closed",
                        }
                    ]
                },
            )

            result = cleanup.retention_preview(base, datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc).timestamp())

        self.assertIsInstance(result, dict)
        self.assertEqual(result["deletable_rows"], 1)

    def test_retention_preview_non_day_cutoff_keeps_manifest_current_segment(self) -> None:
        cleanup = load_module("cleanup_segment_preview_current_exact_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_preview_current_exact_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = base / "raw" / "current"
            current.mkdir(parents=True)
            segment_path = current / "prompt-usage.raw.jsonl.current.1777597200000000000.jsonl"
            old_line = json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T01:00:00+00:00"}) + "\n"
            new_line = json.dumps(_turn_raw("s2", "new", total=100) | {"captured_at": "2026-05-23T01:00:00+00:00"}) + "\n"
            segment_path.write_text(old_line + new_line, encoding="utf-8")
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        {
                            "id": "prompt-usage.raw.jsonl.current.1777597200000000000",
                            "kind": "prompt_usage",
                            "path": str(segment_path),
                            "format": "jsonl",
                            "source_name": "prompt-usage.raw.jsonl",
                            "min_time_unix": 1777597200.0,
                            "max_time_unix": 1779498000.0,
                            "rows": 2,
                            "undated_rows": 0,
                            "corrupt_rows": 0,
                            "unknown_rows": 0,
                            "days": [[1777593600, 1, len(old_line.encode("utf-8"))], [1779494400, 1, len(new_line.encode("utf-8"))]],
                            "bytes": segment_path.stat().st_size,
                            "uncompressed_bytes": segment_path.stat().st_size,
                            "sha256": hashlib.sha256((old_line + new_line).encode("utf-8")).hexdigest(),
                            "status": "closed",
                        }
                    ]
                },
            )

            result = cleanup.retention_preview(base, datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc).timestamp())

        self.assertTrue(result["from_manifest"])
        self.assertEqual(result["deletable_rows"], 1)

    def test_retention_preview_cache_invalidates_when_manifest_changes(self) -> None:
        cleanup = load_module("cleanup_manifest_signature_cache_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_manifest_signature_cache_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            old_segment = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            payload = json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            old_segment.write_text(payload, encoding="utf-8")
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": []})
            cutoff = datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp()

            first = cleanup.retention_preview(base, cutoff)
            self.assertEqual(first["deletable_rows"], 0)

            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        {
                            "id": "prompt-usage.raw.jsonl.current.1777593600000000000",
                            "kind": "prompt_usage",
                            "path": str(old_segment),
                            "format": "jsonl",
                            "source_name": "prompt-usage.raw.jsonl",
                            "min_time_unix": 1777593600.0,
                            "max_time_unix": 1777593600.0,
                            "rows": 1,
                            "undated_rows": 0,
                            "corrupt_rows": 0,
                            "unknown_rows": 0,
                            "days": [[1777593600, 1, len(payload.encode("utf-8"))]],
                            "bytes": old_segment.stat().st_size,
                            "uncompressed_bytes": len(payload.encode("utf-8")),
                            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                            "status": "closed",
                        }
                    ]
                },
            )

            second = cleanup.retention_preview(base, cutoff)

        self.assertEqual(second["deletable_rows"], 1)

    def test_log_cleanup_retention_preview_uses_persistent_index_without_rescanning_jsonl(self) -> None:
        cleanup = load_module("dashboard_cleanup_index_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            archive_dir = raw_dir / "archive"
            archive_dir.mkdir(parents=True)
            current = cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])
            raw_prompt.write_text(
                "".join(
                    json.dumps(row) + "\n"
                    for row in [
                        _turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"},
                        _turn_raw("s1", "new", 100) | {"captured_at": "2026-01-10T00:00:00Z"},
                    ]
                ),
                encoding="utf-8",
            )
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()
            cleanup.rebuild_retention_index(base)
            cleanup.RETENTION_PREVIEW_CACHE.clear()

            with mock.patch.object(cleanup._retention, "preview_jsonl_for_retention", wraps=cleanup._retention.preview_jsonl_for_retention) as preview:
                result = cleanup.retention_preview(base, cutoff_unix)

        self.assertEqual(result["scanned_rows"], 2)
        self.assertEqual(result["deletable_rows"], 1)
        self.assertGreater(result["deletable_bytes"], 0)
        for file in result["files"]:
            self.assertLessEqual(file["deletable_bytes"], file["source_size"])
        self.assertEqual(result["affected_files"], 1)
        self.assertTrue(result["from_index"])
        self.assertEqual(preview.call_count, 0)

    def test_log_cleanup_read_only_retention_preview_does_not_write_index(self) -> None:
        cleanup = load_module("dashboard_cleanup_read_only_preview_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            current = cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])
            raw_prompt.write_text(
                json.dumps(_turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}) + "\n"
                + json.dumps(_turn_raw("s1", "new", 100) | {"captured_at": "2026-01-10T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()

            with mock.patch.object(cleanup._retention, "write_retention_index", side_effect=AssertionError("GET preview must not write index")):
                result = cleanup.retention_preview(base, cutoff_unix, refresh_index=False)

        self.assertEqual(result["scanned_rows"], 2)
        self.assertEqual(result["deletable_rows"], 1)
        self.assertFalse((base / "state" / "cleanup-retention-index.json").exists())
        self.assertEqual(result["preview_source"], "fallback_scan")

    def test_log_cleanup_retention_index_updates_appended_active_raw_without_full_rescan(self) -> None:
        cleanup = load_module("dashboard_cleanup_incremental_index_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            current = cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])
            raw_prompt.write_text(
                json.dumps(_turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()
            cleanup.rebuild_retention_index(base)
            cleanup.RETENTION_PREVIEW_CACHE.clear()
            raw_prompt.write_text(
                raw_prompt.read_text(encoding="utf-8")
                + json.dumps(_turn_raw("s1", "new", 200) | {"captured_at": "2026-01-10T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(cleanup._retention, "scan_retention_source_for_index", side_effect=AssertionError("full rescan")):
                result = cleanup.retention_preview(base, cutoff_unix)

        self.assertEqual(result["scanned_rows"], 2)
        self.assertEqual(result["deletable_rows"], 1)
        self.assertTrue(result["from_index"])

    def test_log_cleanup_retention_index_rescans_when_prefix_changes(self) -> None:
        cleanup = load_module("dashboard_cleanup_prefix_drift_index_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            current = cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])
            raw_prompt.write_text(
                json.dumps(_turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            cleanup.rebuild_retention_index(base)
            cleanup.RETENTION_PREVIEW_CACHE.clear()
            raw_prompt.write_text(
                json.dumps(_turn_raw("s1", "new-prefix", 200) | {"captured_at": "2026-01-10T00:00:00Z"}) + "\n"
                + json.dumps(_turn_raw("s1", "new-tail", 200) | {"captured_at": "2026-01-11T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()

            result = cleanup.retention_preview(base, cutoff_unix)

        self.assertEqual(result["scanned_rows"], 2)
        self.assertEqual(result["deletable_rows"], 0)

    def test_log_cleanup_retention_index_rebuild_includes_current_segments(self) -> None:
        cleanup = load_module("dashboard_cleanup_current_index_rebuild_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = cleanup.raw_segments
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            current_path.write_text(json.dumps(_turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}) + "\n", encoding="utf-8")
            raw_segments.write_current_pointer(
                base,
                {
                    "current": {
                        "prompt_usage": {
                            "id": "prompt-usage.raw.jsonl.current.1777593600000000000",
                            "kind": "prompt_usage",
                            "path": str(current_path),
                            "source_name": "prompt-usage.raw.jsonl",
                            "created_at_unix": 1777593600.0,
                        }
                    }
                },
            )

            index = cleanup.rebuild_retention_index(base)

        current_source = next((source for source in index["sources"] if source["path"] == str(current_path)), None)
        self.assertIsNotNone(current_source)
        self.assertEqual(current_source["scanned_rows"], 1)
