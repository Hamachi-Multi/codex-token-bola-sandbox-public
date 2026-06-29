from __future__ import annotations

try:
    from tests.support import (
        Any,
        DashboardFixtureMixin,
        ROOT,
        _turn_raw,
        datetime,
        json,
        load_module,
        mock,
        pathlib,
        sqlite3,
        tempfile,
        timezone,
        unittest,
    )
except ModuleNotFoundError:
    from support import (
        Any,
        DashboardFixtureMixin,
        ROOT,
        _turn_raw,
        datetime,
        json,
        load_module,
        mock,
        pathlib,
        sqlite3,
        tempfile,
        timezone,
        unittest,
    )

class DashboardCleanupPayloadTests(DashboardFixtureMixin, unittest.TestCase):
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
        self.assertNotIn("target_files", row["display"])
        self.assertNotIn("target_files", row["delete_all_display"])
        self.assertNotIn("detail_file_label", row["display"])
        self.assertNotIn("detail_file_label", row["delete_all_display"])
        for display in (row["display"], row["delete_all_display"]):
            if "action" in display:
                self.assertEqual(set(display.get("action_file_counts", {})), {"Delete", "Rewrite", "Rebuild"})
        self.assertIsInstance(row.get("group_id"), str)
        self.assertTrue(row.get("group_id"))
        self.assertIsInstance(row.get("capabilities"), list)

    def test_cleanup_impact_payload_includes_all_detail_targets(self) -> None:
        cleanup_common = load_module(
            "dashboard_cleanup_common_targets_test",
            ROOT / "scripts" / "dashboard_cleanup_common.py",
        )
        targets = [pathlib.Path(f"/tmp/cleanup-affected-file-{index}.jsonl") for index in range(75)]

        payload = cleanup_common.impact_payload(targets=targets, include_targets=True)

        self.assertEqual(len(payload["targets"]), 75)
        self.assertEqual(payload["targets_truncated"], 0)
        self.assertEqual(payload["targets"][0], "/tmp/cleanup-affected-file-0.jsonl")
        self.assertEqual(payload["targets"][-1], "/tmp/cleanup-affected-file-74.jsonl")

    def test_cleanup_impact_payload_uses_affected_files_as_file_count_contract(self) -> None:
        cleanup_common = load_module(
            "dashboard_cleanup_common_file_count_contract_test",
            ROOT / "scripts" / "dashboard_cleanup_common.py",
        )

        payload = cleanup_common.impact_payload(
            affected_files=3,
            source_files=7,
            targets=[pathlib.Path("/tmp/affected-output.jsonl")],
            include_targets=True,
        )

        self.assertEqual(payload["affected_files"], 3)
        self.assertEqual(payload["source_files"], 7)
        self.assertNotIn("target_files", payload)

    def test_cleanup_impact_payload_keeps_file_action_counts_as_affected_subtypes(self) -> None:
        cleanup_payload = load_module(
            "dashboard_cleanup_payload_file_subtype_contract_test",
            ROOT / "scripts" / "dashboard_cleanup_payload.py",
        )

        delete_impact = {"affected_rows": 5, "affected_files": 2, "delete_files": 2, "rewrite_files": 0}
        rewrite_impact = {"affected_rows": 5, "affected_files": 2, "delete_files": 1, "rewrite_files": 1}

        delete_summary = cleanup_payload.cleanup_impact_summary("source_prune", delete_impact)
        rewrite_summary = cleanup_payload.cleanup_impact_summary("source_prune", rewrite_impact)

        self.assertLessEqual(delete_impact["delete_files"] + delete_impact["rewrite_files"], delete_impact["affected_files"])
        self.assertLessEqual(rewrite_impact["delete_files"] + rewrite_impact["rewrite_files"], rewrite_impact["affected_files"])
        self.assertEqual(delete_summary["operation"], "Delete")
        self.assertEqual(rewrite_summary["operation"], "Rewrite")
        self.assertEqual(delete_summary["scope_label"], "5 rows · 2 files")
        self.assertEqual(rewrite_summary["scope_label"], "5 rows · 2 files")

    def test_delete_all_logs_reports_failed_paths_without_dropping_deleted_paths(self) -> None:
        cleanup_payload = load_module(
            "dashboard_cleanup_payload_delete_all_failed_test",
            ROOT / "scripts" / "dashboard_cleanup_payload.py",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            tmp_work_dir = base / "tmp"
            raw_dir.mkdir(parents=True)
            tmp_work_dir.mkdir(parents=True)
            (raw_dir / "prompt-usage.raw.jsonl").write_text("{}\n", encoding="utf-8")
            (tmp_work_dir / "work.tmp").write_text("tmp\n", encoding="utf-8")

            def fail_tmp(path):
                if pathlib.Path(path).name == "tmp":
                    raise OSError("tmp delete blocked")
                return None

            with mock.patch.object(cleanup_payload.shutil, "rmtree", side_effect=fail_tmp):
                result = cleanup_payload.delete_all_logs(base)

        self.assertTrue(result["delete_failed"])
        self.assertTrue(result["partial_mutation"])
        self.assertGreater(result["deleted_bytes"], 0)
        self.assertTrue(any(item["target"] == "raw" for item in result["deleted"]))
        self.assertTrue(any(item["target"] == "tmp" and "tmp delete blocked" in item["failed"] for item in result["failed"]))

    def test_cleanup_impact_summary_suppresses_empty_derived_affected_files(self) -> None:
        cleanup_payload = load_module(
            "dashboard_cleanup_payload_summary_test",
            ROOT / "scripts" / "dashboard_cleanup_payload.py",
        )
        summary = cleanup_payload.cleanup_impact_summary(
            "derived_rebuild",
            {
                "affected_rows": 0,
                "delete_size": 0,
                "affected_files": 1,
            },
        )

        self.assertEqual(summary["operation"], "-")
        self.assertEqual(summary["scope_label"], "0 files")

    def test_cleanup_impact_summary_uses_compact_action_values(self) -> None:
        cleanup_payload = load_module(
            "dashboard_cleanup_payload_action_values_test",
            ROOT / "scripts" / "dashboard_cleanup_payload.py",
        )
        contract = load_module(
            "dashboard_cleanup_contract_action_values_test",
            ROOT / "scripts" / "dashboard_cleanup_contract.py",
        )

        summaries = [
            cleanup_payload.cleanup_impact_summary("derived_rebuild", {"affected_rows": 1, "delete_size": 0}),
            cleanup_payload.cleanup_impact_summary("source_prune", {"affected_rows": 2, "affected_files": 1, "delete_files": 1}),
            cleanup_payload.cleanup_impact_summary("source_prune", {"affected_rows": 2, "affected_files": 2, "delete_files": 1, "rewrite_files": 1}),
            cleanup_payload.cleanup_impact_summary("file_delete", {"affected_files": 1, "delete_size": 8}),
            cleanup_payload.cleanup_impact_summary("file_delete", {}),
        ]

        self.assertEqual([summary["operation"] for summary in summaries], ["Rebuild", "Delete", "Rewrite", "Delete", "-"])
        self.assertTrue(all(summary["operation"] in contract.CLEANUP_ALLOWED_ACTIONS for summary in summaries))

    def test_log_cleanup_payload_reports_service_owned_files(self) -> None:
        serve = load_module("serve_dashboard_cleanup_payload_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            normalized_dir = base / "normalized"
            archive_dir = raw_dir / "archive"
            analytics_dir = base / "analytics"
            state_dir = base / "state"
            tmp_work_dir = base / "tmp"
            bad_dir = base / "bad"
            for directory in (raw_dir, normalized_dir, archive_dir, analytics_dir, state_dir, tmp_work_dir, bad_dir):
                directory.mkdir(parents=True, exist_ok=True)
            current = serve.dashboard_cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_bytes(b'{"a":1}\n{"b":2}\n')
            (normalized_dir / "prompt-usage.normalized.jsonl").write_bytes(b"n\n")
            (state_dir / "abc.json").write_bytes(b"{}")
            (tmp_work_dir / "work.tmp").write_bytes(b"tmp\n")
            (bad_dir / "bad.jsonl").write_bytes(b"bad\n")
            (archive_dir / "prompt.gz").write_bytes(b"gz")
            db_path = analytics_dir / "token-usage.sqlite"
            con = sqlite3.connect(db_path)
            con.execute("create table run_metadata (key text primary key, value text)")
            con.executemany(
                "insert into run_metadata values (?, ?)",
                [
                    ("last_compacted_at_unix", json.dumps(1770000000)),
                ],
            )
            con.commit()
            con.close()

            handler = serve.Handler.__new__(serve.Handler)
            cutoff_unix = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()
            payload = handler.cleanup_payload(db_path=db_path, base_dir=base, retention_cutoff_unix=cutoff_unix)
            detail = handler.cleanup_detail_payload(
                "raw_current_segments",
                db_path=db_path,
                base_dir=base,
                retention_cutoff_unix=cutoff_unix,
            )

        self.assertEqual(payload["summary"]["deletable_bytes"], 0)
        self.assertEqual(payload["summary"]["compactable_bytes"], 0)
        self.assertEqual(payload["summary"]["active_raw_bytes"], 16)
        self.assertGreater(payload["summary"]["service_bytes"], payload["summary"]["active_raw_bytes"])
        self.assertEqual(payload["summary"]["last_compacted_at_unix"], 1770000000)
        self.assertIn("retention", payload)
        self.assertIn("selected", payload["retention"])
        self.assertEqual(payload["retention"]["selected"]["cutoff_unix"], cutoff_unix)
        labels = [row["label"] for row in payload["rows"]]
        self.assertNotIn("Raw Usage Logs", labels)
        self.assertNotIn("Raw Model Calls", labels)
        self.assertNotIn("Normalized Model Calls", labels)
        self.assertIn("Analytics Database", labels)
        self.assertIn("Archived Raw Logs", labels)
        self.assertIn("Raw Current Segments", labels)
        self.assertNotIn("Reports", labels)
        self.assertNotIn("Temporary Files", labels)
        self.assertNotIn("Parse Error Logs", labels)
        for row in payload["rows"]:
            self.assert_cleanup_row_uses_display_contract(row)
        analytics = next(row for row in payload["rows"] if row["label"] == "Analytics Database")
        self.assertEqual(analytics["display"]["detail_items_kind"], "derived_outputs")
        self.assertEqual(analytics["retention_effect"], "rebuilt after delete")
        archive = next(row for row in payload["rows"] if row["label"] == "Archived Raw Logs")
        self.assertEqual(archive["display"]["detail_items_kind"], "source_files")
        self.assertEqual(archive["status"], "protected")
        self.assertEqual(archive["deletable_bytes"], 0)
        current_segments = next(row for row in payload["rows"] if row["label"] == "Raw Current Segments")
        self.assertEqual(current_segments["display"]["detail_items_kind"], "source_files")
        self.assertEqual(current_segments["status"], "protected")
        self.assertEqual(current_segments["deletable_bytes"], 0)
        self.assertNotIn("files", payload["retention"]["selected"])
        self.assertGreaterEqual(payload["retention"]["selected"]["source_files"], 1)
        self.assertNotIn("retention", detail)
        self.assertNotIn("items", detail["row"]["display"])
        self.assertNotIn("current", {row["status"] for row in payload["rows"]})

    def test_log_cleanup_payload_exposes_mode_specific_row_impacts(self) -> None:
        serve = load_module("serve_dashboard_cleanup_impacts_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            state_dir = base / "state"
            tmp_work_dir = base / "tmp"
            bad_dir = base / "bad"
            for directory in (raw_dir, normalized_dir, analytics_dir, state_dir, tmp_work_dir, bad_dir):
                directory.mkdir(parents=True, exist_ok=True)
            current = serve.dashboard_cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_file = pathlib.Path(current["path"])
            raw_file.write_text(
                json.dumps(_turn_raw("s-old", "t-old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
                + json.dumps(_turn_raw("s-new", "t-new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )
            prompt_normalized = normalized_dir / "prompt-usage.normalized.jsonl"
            prompt_normalized.write_bytes(b"prompt-normalized\n")
            prompt_archive = normalized_dir / "prompt-usage.normalized.jsonl.gz"
            prompt_archive.write_bytes(b"prompt-gz\n")
            normalize_state = normalized_dir / "normalize-state.json"
            normalize_state.write_bytes(b'{"state":true}\n')
            db_path = analytics_dir / "token-usage.sqlite"
            db_path.write_bytes(b"sqlite-bytes\n")
            state_json = state_dir / "custom-state.json"
            state_json.write_bytes(b"state\n")
            state_lock = state_dir / "service.lock"
            state_lock.write_bytes(b"lock\n")
            hook_probe = base / "hook-probe-events.jsonl"
            hook_probe.write_bytes(b"hook probe\n")
            tmp_file = tmp_work_dir / "work.tmp"
            tmp_file.write_bytes(b"tmp\n")
            bad_file = bad_dir / "bad.jsonl"
            bad_file.write_bytes(b"bad\n")
            sizes = {
                "raw_current_group": raw_file.stat().st_size,
                "prompt_normalized": prompt_normalized.stat().st_size,
                "prompt_archive": prompt_archive.stat().st_size,
                "normalize_state": normalize_state.stat().st_size,
                "db": db_path.stat().st_size,
                "hook_probe": hook_probe.stat().st_size,
            }

            handler = serve.Handler.__new__(serve.Handler)
            cutoff_unix = datetime.fromisoformat("2026-05-20T00:00:00+00:00").timestamp()
            payload = handler.cleanup_payload(db_path=db_path, base_dir=base, retention_cutoff_unix=cutoff_unix)
            detail = handler.cleanup_detail_payload("state_files", db_path=db_path, base_dir=base, retention_cutoff_unix=cutoff_unix)
            sizes["state_delete_all"] = sum(
                path.stat().st_size
                for path in state_dir.iterdir()
                if path.is_file() and not path.name.endswith(".lock")
            ) + sizes["hook_probe"]

        rows = {row["label"]: row for row in payload["rows"]}
        self.assertNotIn("profile", payload)
        self.assertNotIn("profile", detail)
        self.assertIn("preview_signature", payload["retention"]["selected"])
        for row in payload["rows"]:
            self.assert_cleanup_row_uses_display_contract(row)
        self.assert_cleanup_row_uses_display_contract(detail["row"])
        self.assertEqual(rows["Raw Current Segments"]["bytes"], sizes["raw_current_group"])
        self.assertEqual(rows["Raw Current Segments"]["display"]["action"], "Rewrite")
        self.assertEqual(rows["Raw Current Segments"]["display"]["scope_label"], "1 row · 1 file")
        self.assertEqual(rows["Raw Current Segments"]["display"]["detail_items_kind"], "source_files")
        self.assertEqual(rows["Raw Current Segments"]["display"]["affected_rows"], 1)
        self.assertEqual(rows["Raw Current Segments"]["display"]["affected_files"], 1)
        self.assertGreater(rows["Raw Current Segments"]["display"]["delete_size"], 0)
        self.assertEqual(rows["Raw Current Segments"]["delete_all_display"]["action"], "Delete")
        self.assertEqual(rows["Raw Current Segments"]["delete_all_display"]["detail_items_kind"], "source_files")
        self.assertEqual(rows["Raw Current Segments"]["delete_all_display"]["affected_rows"], 2)
        self.assertEqual(rows["Raw Current Segments"]["delete_all_display"]["delete_size"], sizes["raw_current_group"])
        self.assertEqual(rows["Raw Current Segments"]["delete_all_display"]["affected_files"], 1)
        self.assertNotIn("targets", rows["State Files"]["delete_all_display"])
        self.assertNotIn("Reports", rows)
        self.assertNotIn("Temporary Files", rows)
        self.assertNotIn("Parse Error Logs", rows)
        self.assertIn("targets", detail["row"]["delete_all_display"])
        self.assertEqual(rows["State Files"]["delete_all_display"]["delete_size"], sizes["state_delete_all"])
        self.assertGreaterEqual(rows["State Files"]["delete_all_display"]["affected_files"], 1)
        self.assertLess(
            rows["Normalized Outputs"]["display"]["delete_size"],
            sizes["prompt_normalized"] + sizes["normalize_state"],
        )
        self.assertEqual(
            rows["Normalized Outputs"]["display"]["delete_size"],
            (sizes["prompt_normalized"] + sizes["normalize_state"] + 1) // 2,
        )
        self.assertEqual(rows["Normalized Outputs"]["display"]["action"], "Rebuild")
        self.assertEqual(rows["Normalized Outputs"]["display"]["detail_items_kind"], "derived_outputs")
        self.assertEqual(rows["Normalized Outputs"]["display"]["affected_rows"], 1)
        self.assertEqual(rows["Normalized Outputs"]["display"]["affected_files"], 2)
        self.assertEqual(
            rows["Normalized Outputs"]["delete_all_display"]["delete_size"],
            sizes["prompt_normalized"] + sizes["prompt_archive"] + sizes["normalize_state"],
        )
        self.assertEqual(rows["Normalized Outputs"]["delete_all_display"]["affected_files"], 3)
        self.assertNotIn("Raw Model Calls", rows)
        self.assertNotIn("Normalized Model Calls", rows)
        self.assertEqual(rows["Analytics Database"]["display"]["delete_size"], (sizes["db"] + 1) // 2)
        self.assertEqual(rows["Analytics Database"]["display"]["action"], "Rebuild")
        self.assertEqual(rows["Analytics Database"]["display"]["detail_items_kind"], "derived_outputs")
        self.assertEqual(rows["Analytics Database"]["display"]["affected_files"], 1)

    def test_log_cleanup_payload_profiles_index_retention_preview_source(self) -> None:
        cleanup = load_module("cleanup_payload_retention_preview_source_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = cleanup.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            analytics_dir = base / "analytics"
            current_dir = base / "raw" / "current"
            analytics_dir.mkdir(parents=True)
            current_dir.mkdir(parents=True)
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            current_path.write_text(
                json.dumps(_turn_raw("s-current", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )
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

            with mock.patch.object(raw_segments._retention, "scan_segment_file_for_cutoff", side_effect=AssertionError("raw current scan")):
                payload = cleanup.cleanup_payload(base, analytics_dir / "token-usage.sqlite", retention_cutoff_unix=cutoff)

        self.assertNotIn("profile", payload)
        self.assertEqual(payload["retention"]["selected"]["preview_source"], "refreshed_index")

    def test_log_cleanup_derived_retention_impact_uses_matching_source_rows(self) -> None:
        serve = load_module("serve_dashboard_cleanup_derived_split_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            for directory in (raw_dir, normalized_dir, analytics_dir):
                directory.mkdir(parents=True, exist_ok=True)
            current = serve.dashboard_cleanup.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text(
                json.dumps(_turn_raw("s-old", "t-old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
                + json.dumps(_turn_raw("s-new", "t-new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )
            prompt_normalized = normalized_dir / "prompt-usage.normalized.jsonl"
            prompt_normalized.write_bytes(b"p" * 1000)
            normalize_state = normalized_dir / "normalize-state.json"
            normalize_state.write_bytes(b"s" * 1000)
            db_path = analytics_dir / "token-usage.sqlite"
            db_path.write_bytes(b"d" * 1000)

            handler = serve.Handler.__new__(serve.Handler)
            cutoff_unix = datetime.fromisoformat("2026-05-20T00:00:00+00:00").timestamp()
            payload = handler.cleanup_payload(db_path=db_path, base_dir=base, retention_cutoff_unix=cutoff_unix)

        rows = {row["label"]: row for row in payload["rows"]}
        self.assertEqual(rows["Normalized Outputs"]["display"]["total_rows"], 2)
        self.assertEqual(rows["Normalized Outputs"]["display"]["affected_rows"], 1)
        self.assertEqual(rows["Normalized Outputs"]["display"]["delete_size"], 1000)
        self.assertNotIn("Normalized Model Calls", rows)
        self.assertEqual(rows["Analytics Database"]["display"]["total_rows"], 2)
        self.assertEqual(rows["Analytics Database"]["display"]["affected_rows"], 1)
        self.assertEqual(rows["Analytics Database"]["display"]["delete_size"], 500)

    def test_log_cleanup_payload_splits_pending_turn_state(self) -> None:
        serve = load_module("serve_dashboard_cleanup_pending_state_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            state_dir = base / "state"
            analytics_dir = base / "analytics"
            for directory in (raw_dir, state_dir, analytics_dir):
                directory.mkdir(parents=True, exist_ok=True)
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
            old_state_size = old_state.stat().st_size
            service_state = state_dir / "custom-state.json"
            service_state.write_text("{}\n", encoding="utf-8")
            service_state_size = service_state.stat().st_size
            (state_dir / "service.lock").write_text("lock\n", encoding="utf-8")

            handler = serve.Handler.__new__(serve.Handler)
            cutoff_unix = datetime.fromisoformat("2026-05-20T00:00:00+00:00").timestamp()
            payload = handler.cleanup_payload(db_path=analytics_dir / "token-usage.sqlite", base_dir=base, retention_cutoff_unix=cutoff_unix)
            detail = handler.cleanup_detail_payload("state_files", db_path=analytics_dir / "token-usage.sqlite", base_dir=base, retention_cutoff_unix=cutoff_unix)

        rows = {row["label"]: row for row in payload["rows"]}
        self.assertIn("Pending Turn State", rows)
        self.assertEqual(rows["Pending Turn State"]["display"]["detail_items_kind"], "file_targets")
        self.assertEqual(rows["Pending Turn State"]["display"]["total_rows"], 0)
        self.assertEqual(rows["Pending Turn State"]["display"]["affected_rows"], 0)
        self.assertEqual(rows["Pending Turn State"]["display"]["affected_files"], 1)
        self.assertEqual(rows["Pending Turn State"]["display"]["delete_size"], old_state_size)
        self.assertEqual(rows["Pending Turn State"]["delete_all_display"]["affected_files"], 2)
        self.assertGreaterEqual(rows["State Files"]["delete_all_display"]["affected_files"], 1)
        self.assertGreaterEqual(rows["State Files"]["delete_all_display"]["delete_size"], service_state_size)
        self.assertNotIn("targets", rows["State Files"]["delete_all_display"])
        state_targets = set(detail["row"]["delete_all_display"]["targets"])
        self.assertNotIn(str(old_state), state_targets)
        self.assertNotIn(str(new_state), state_targets)

    def test_log_cleanup_selected_retention_includes_old_pending_turn_state(self) -> None:
        serve = load_module("serve_dashboard_pending_state_selected_retention_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            state_dir = base / "state"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            (raw_dir / "prompt-usage.raw.jsonl").write_text("", encoding="utf-8")
            (state_dir / ("a" * 32 + ".json")).write_text(
                json.dumps({"record_type": "turn_start", "captured_at": "2026-05-01T00:00:00+00:00", "session_id": "s1", "turn_id": "t1"}) + "\n",
                encoding="utf-8",
            )
            handler = serve.Handler.__new__(serve.Handler)
            payload = handler.cleanup_payload(
                base_dir=base,
                db_path=base / "analytics" / "token-usage.sqlite",
                retention_cutoff_unix=datetime.fromisoformat("2026-05-10T00:00:00+00:00").timestamp(),
            )

        selected = payload["retention"]["selected"]
        self.assertEqual(selected["deletable_rows"], 0)
        self.assertEqual(selected["pending_turn_state_deletable_files"], 1)

    def test_retention_preview_signature_changes_when_pending_turn_state_changes(self) -> None:
        cleanup = load_module("cleanup_pending_state_signature_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            state_dir = base / "state"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            (raw_dir / "prompt-usage.raw.jsonl").write_text("", encoding="utf-8")
            cutoff = datetime.fromisoformat("2026-05-10T00:00:00+00:00").timestamp()

            first = cleanup.retention_preview_signature(base, cutoff)
            (state_dir / ("a" * 32 + ".json")).write_text(
                json.dumps({"record_type": "turn_start", "captured_at": "2026-05-01T00:00:00+00:00", "session_id": "s1", "turn_id": "t1"}) + "\n",
                encoding="utf-8",
            )
            second = cleanup.retention_preview_signature(base, cutoff)

        self.assertNotEqual(first, second)

    def test_log_cleanup_payload_exposes_current_segment_targets(self) -> None:
        serve = load_module("serve_dashboard_cleanup_current_segment_row_test", ROOT / "scripts" / "serve_dashboard.py")
        raw_segments = load_module("raw_segments_cleanup_current_segment_row_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            current_dir = raw_dir / "current"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            current_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            (raw_dir / "prompt-usage.raw.jsonl").write_text("", encoding="utf-8")
            current_path = current_dir / "prompt-usage.raw.jsonl.current.1779235200000000000.jsonl"
            current_path.write_text(
                json.dumps(_turn_raw("s-current", "t-old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base))
            raw_segments.write_current_pointer(
                base,
                {
                    "current": {
                        "prompt_usage": {
                            "id": "prompt-usage.raw.jsonl.current.1779235200000000000",
                            "kind": "prompt_usage",
                            "path": str(current_path),
                            "source_name": "prompt-usage.raw.jsonl",
                            "created_at_unix": 1779235200.0,
                        },
                    }
                },
            )
            db_path = analytics_dir / "token-usage.sqlite"
            sqlite3.connect(db_path).close()
            handler = serve.Handler.__new__(serve.Handler)
            payload = handler.cleanup_payload(
                db_path=db_path,
                base_dir=base,
                retention_cutoff_unix=datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp(),
            )
            detail = handler.cleanup_detail_payload(
                "raw_current_segments",
                db_path=db_path,
                base_dir=base,
                retention_cutoff_unix=datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp(),
            )

        current_row = next(row for row in payload["rows"] if row["label"] == "Raw Current Segments")
        self.assertEqual(current_row["delete_all_display"]["affected_files"], 1)
        self.assertNotIn("retention", detail)
        row_paths = [path.strip() for path in current_row["path"].split(",")]
        deletable_current_files = [
            item
            for item in detail["row"]["display"]["items"]
            if int(item.get("deletable_rows") or 0) > 0 and "/raw/current/" in str(item.get("path") or "")
        ]
        self.assertGreaterEqual(len(deletable_current_files), 1)
        self.assertTrue(
            any(
                any(file["path"] == path or file["path"].startswith(path + "/") for path in row_paths)
                for file in deletable_current_files
            )
        )

    def test_log_cleanup_default_cutoff_uses_retention_index(self) -> None:
        serve = load_module("serve_dashboard_default_cleanup_cutoff_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_dir = base / "raw"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_segments = load_module("raw_segments_payload_default_cutoff_test", ROOT / "scripts" / "raw_segments.py")
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])
            raw_prompt.write_text(
                "\n".join(
                    [
                        json.dumps(_turn_raw("s1", "old", 100) | {"captured_at": "2026-01-01T00:00:00Z"}),
                        json.dumps(_turn_raw("s1", "new", 100) | {"captured_at": "2026-01-10T00:00:00Z"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            db_path = analytics_dir / "token-usage.sqlite"
            con = sqlite3.connect(db_path)
            con.execute("create table run_metadata (key text primary key, value text)")
            con.commit()
            con.close()
            now_unix = datetime.fromisoformat("2026-01-15T12:34:56+00:00").timestamp()
            handler = serve.Handler.__new__(serve.Handler)
            serve.dashboard_cleanup.RETENTION_PREVIEW_CACHE.clear()

            with mock.patch.object(serve.dashboard_cleanup.time, "time", return_value=now_unix):
                payload = handler.cleanup_payload(db_path=db_path, base_dir=base)

        selected = payload["retention"]["selected"]
        expected_cutoff = datetime.fromisoformat("2026-01-08T00:00:00+00:00").timestamp()
        self.assertEqual(selected["cutoff_unix"], expected_cutoff)
        self.assertTrue(selected["from_index"])
        self.assertEqual(selected["deletable_rows"], 1)
