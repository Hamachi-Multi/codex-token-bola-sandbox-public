from __future__ import annotations

try:
    from tests.support import (
        Any,
        DashboardFixtureMixin,
        ROOT,
        load_module,
        mock,
        pathlib,
        tempfile,
        types,
        unittest,
    )
except ModuleNotFoundError:
    from support import (
        Any,
        DashboardFixtureMixin,
        ROOT,
        load_module,
        mock,
        pathlib,
        tempfile,
        types,
        unittest,
    )

class DashboardCleanupApiTests(DashboardFixtureMixin, unittest.TestCase):
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

    def test_log_cleanup_api_does_not_open_dashboard_database(self) -> None:
        serve = load_module("serve_dashboard_cleanup_api_db_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []

        def fail_db():
            raise AssertionError("cleanup API should not open dashboard DB")

        handler.db = fail_db
        handler.cleanup_payload = lambda **kwargs: kwargs
        handler.cleanup_cutoff_unix = lambda value=None: 123.0
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_api("/api/log-cleanup", {})

        self.assertEqual(sent, [({"retention_cutoff_unix": 123.0, "refresh_retention_index": False}, 200)])

    def test_log_cleanup_get_uses_read_only_retention_preview(self) -> None:
        serve = load_module("serve_dashboard_cleanup_read_only_get_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        calls: list[dict[str, object]] = []
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        def fake_cleanup_payload(**kwargs):
            calls.append(kwargs)
            return {"summary": {}, "rows": []}

        handler.cleanup_payload = fake_cleanup_payload
        handler.cleanup_preview_cutoff_unix = lambda value=None: 123.0

        handler.handle_api("/api/log-cleanup", {})

        self.assertEqual(sent, [({"summary": {}, "rows": []}, 200)])
        self.assertEqual(calls, [{"retention_cutoff_unix": 123.0, "refresh_retention_index": False}])

    def test_log_cleanup_preview_rejects_invalid_cutoff_date(self) -> None:
        serve = load_module("serve_dashboard_cleanup_preview_invalid_cutoff_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.cleanup_payload = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("cleanup payload must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_api("/api/log-cleanup", {"cutoff_date": ["not-a-date"]})

        self.assertEqual(sent, [({"error": "cutoff_date_invalid"}, 400)])

    def test_log_cleanup_compact_ignores_removed_model_scope_options(self) -> None:
        serve = load_module("serve_dashboard_compact_removed_scope_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        calls: list[tuple[pathlib.Path, int]] = []
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {"min_bytes": 2048, "include_prompt": False, "include_model": False}
        handler.send_json = lambda payload, status=200: sent.append((payload, status))
        handler.cleanup_payload = lambda db_path=None: {"rows": []}

        def fake_compact(output: pathlib.Path, min_bytes: int) -> dict[str, object]:
            calls.append((output, min_bytes))
            return {"returncode": 0, "stdout": "{}", "stderr": "", "metadata": {"ok": True}}

        handler.run_compact_command = fake_compact
        with mock.patch.object(serve.dashboard_cleanup, "refresh_retention_index_for_current_sources", return_value={}):
            handler.handle_cleanup_compact()

        self.assertEqual(sent[0][1], 200)
        self.assertEqual(calls, [(pathlib.Path("/tmp/token-usage.sqlite"), 2048)])

    def test_log_cleanup_detail_api_does_not_open_dashboard_database(self) -> None:
        serve = load_module("serve_dashboard_cleanup_detail_api_db_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []

        def fail_db():
            raise AssertionError("cleanup detail API should not open dashboard DB")

        handler.db = fail_db
        handler.cleanup_detail_payload = lambda group_id, retention_cutoff_unix=None, preview_signature=None: {"group_id": group_id, "retention_cutoff_unix": retention_cutoff_unix, "preview_signature": preview_signature}
        handler.cleanup_cutoff_unix = lambda value=None: 456.0
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_api("/api/log-cleanup/detail", {"group_id": ["state_files"], "preview_signature": ["fresh"]})

        self.assertEqual(sent, [({"group_id": "state_files", "retention_cutoff_unix": 456.0, "preview_signature": "fresh"}, 200)])

    def test_log_cleanup_detail_preview_rejects_invalid_cutoff_date(self) -> None:
        serve = load_module("serve_dashboard_cleanup_detail_invalid_cutoff_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.cleanup_detail_payload = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("detail payload must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_api("/api/log-cleanup/detail", {"group_id": ["state_files"], "preview_signature": ["fresh"], "cutoff_date": ["not-a-date"]})

        self.assertEqual(sent, [({"error": "cutoff_date_invalid"}, 400)])

    def test_log_cleanup_detail_api_requires_preview_signature(self) -> None:
        serve = load_module("serve_dashboard_cleanup_detail_signature_required_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.cleanup_detail_payload = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("detail payload must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_api("/api/log-cleanup/detail", {"group_id": ["state_files"]})

        self.assertEqual(sent, [({"error": "cleanup_preview_signature_required"}, 400)])

    def test_log_cleanup_detail_api_requires_group_id(self) -> None:
        serve = load_module("serve_dashboard_cleanup_detail_group_required_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.cleanup_detail_payload = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("detail payload must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_api("/api/log-cleanup/detail", {"preview_signature": ["fresh"]})

        self.assertEqual(sent, [({"error": "cleanup_group_id_required"}, 400)])

    def test_log_cleanup_detail_api_rejects_stale_preview_signature(self) -> None:
        serve = load_module("serve_dashboard_cleanup_detail_stale_signature_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.cleanup_cutoff_unix = lambda value=None: 1779235200.0
        handler.cleanup_detail_payload = lambda group_id, retention_cutoff_unix=None, preview_signature=None: {"error": "cleanup_preview_stale"}
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_api("/api/log-cleanup/detail", {"group_id": ["state_files"], "preview_signature": ["old"], "cutoff_date": ["2026-05-20"]})

        self.assertEqual(sent, [({"error": "cleanup_preview_stale"}, 409)])

    def test_log_cleanup_detail_api_reports_unknown_group(self) -> None:
        serve = load_module("serve_dashboard_cleanup_detail_unknown_group_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.cleanup_cutoff_unix = lambda value=None: 1779235200.0
        handler.cleanup_detail_payload = lambda group_id, retention_cutoff_unix=None, preview_signature=None: {"error": "cleanup_row_not_found", "message": f"Unknown cleanup row group: {group_id}"}
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_api("/api/log-cleanup/detail", {"group_id": ["unknown"], "preview_signature": ["fresh"]})

        self.assertEqual(sent, [({"error": "cleanup_row_not_found", "message": "Unknown cleanup row group: unknown"}, 404)])

    def test_log_cleanup_detail_api_revalidates_real_preview_signature(self) -> None:
        serve = load_module("serve_dashboard_cleanup_detail_real_signature_test", ROOT / "scripts" / "serve_dashboard.py")
        fixture = load_module("dashboard_fixture_data_cleanup_detail_signature_test", ROOT / "scripts" / "dashboard_fixture_data.py")
        raw_segments = serve.dashboard_cleanup.raw_segments
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / "codex-home"
            db_path = fixture.write_dashboard_fixture(codex_home, now_unix=1_782_000_000.0)
            base = codex_home / "codex-token-bola"
            handler = serve.Handler.__new__(serve.Handler)
            handler.server = types.SimpleNamespace(db_path=db_path)
            sent: list[tuple[dict[str, object], int]] = []
            handler.send_json = lambda payload, status=200: sent.append((payload, status))

            with mock.patch.object(serve, "TOKEN_USAGE_ROOT", base), mock.patch.object(serve, "CODEX_HOME", codex_home):
                handler.handle_api("/api/log-cleanup", {})
                preview = sent.pop()[0]
                preview_signature = str(((preview.get("retention") or {}).get("selected") or {}).get("preview_signature") or "")
                self.assertTrue(preview_signature)

                handler.handle_api(
                    "/api/log-cleanup/detail",
                    {"group_id": ["raw_current_segments"], "preview_signature": [preview_signature]},
                )
                fresh_payload, fresh_status = sent.pop()
                self.assertEqual(fresh_status, 200)
                self.assertEqual((fresh_payload.get("row") or {}).get("group_id"), "raw_current_segments")

                current = raw_segments.read_current_pointer(base)["current"]["prompt_usage"]
                pathlib.Path(current["path"]).write_text('{"record_type":"turn_usage_raw"}\n', encoding="utf-8")
                handler.handle_api(
                    "/api/log-cleanup/detail",
                    {"group_id": ["raw_current_segments"], "preview_signature": [preview_signature]},
                )
                stale_payload, stale_status = sent.pop()

        self.assertEqual(stale_status, 409)
        self.assertEqual(stale_payload, {"error": "cleanup_preview_stale"})

    def test_log_cleanup_retention_requires_explicit_valid_cutoff_date(self) -> None:
        serve = load_module("serve_dashboard_retention_cutoff_required_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {}
        handler.run_retention_prune_command = lambda _db_path, _cutoff: (_ for _ in ()).throw(AssertionError("prune must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(sent[0][1], 400)
        self.assertEqual(sent[0][0]["error"], "cutoff_date_required")

    def test_log_cleanup_retention_rejects_malformed_cutoff_date(self) -> None:
        serve = load_module("serve_dashboard_retention_cutoff_invalid_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {"cutoff_date": "not-a-date"}
        handler.run_retention_prune_command = lambda _db_path, _cutoff: (_ for _ in ()).throw(AssertionError("prune must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(sent[0][1], 400)
        self.assertEqual(sent[0][0]["error"], "cutoff_date_invalid")

    def test_log_cleanup_retention_rejects_stale_preview_signature(self) -> None:
        serve = load_module("serve_dashboard_retention_stale_preview_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {"cutoff_date": "2026-05-20", "preview_signature": "old"}
        handler.cleanup_payload = lambda db_path=None, retention_cutoff_unix=None: {"retention": {"selected": {"preview_signature": "fresh", "deletable_rows": 1}}}
        handler.run_retention_prune_command = lambda _db_path, _cutoff: (_ for _ in ()).throw(AssertionError("prune must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(sent[0][1], 409)
        self.assertEqual(sent[0][0]["error"], "cleanup_preview_stale")

    def test_log_cleanup_retention_noops_when_preview_has_no_rows(self) -> None:
        serve = load_module("serve_dashboard_retention_empty_preview_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        cleanup_payload = {
            "summary": {},
            "rows": [],
            "retention": {
                "selected": {
                    "preview_signature": "fresh",
                    "scanned_rows": 12,
                    "deletable_rows": 0,
                }
            },
        }
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {"cutoff_date": "2026-05-20", "preview_signature": "fresh"}
        handler.cleanup_cutoff_unix = lambda value=None: 1779235200.0
        handler.cleanup_payload = lambda db_path=None, retention_cutoff_unix=None: cleanup_payload | {"retention_cutoff_unix": retention_cutoff_unix}
        handler.run_retention_prune_command = lambda _db_path, _cutoff: (_ for _ in ()).throw(AssertionError("prune must not run for empty preview"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(sent[0][1], 200)
        self.assertTrue(sent[0][0]["noop"])
        self.assertEqual(sent[0][0]["retention"]["deleted_rows"], 0)
        self.assertEqual(sent[0][0]["retention"]["scanned_rows"], 12)
        self.assertIs(sent[0][0]["cleanup"]["retention"], cleanup_payload["retention"])

    def test_log_cleanup_retention_runs_when_pending_state_files_are_selected(self) -> None:
        serve = load_module("serve_dashboard_retention_pending_state_api_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        output = pathlib.Path("/tmp/token-usage.sqlite")
        sent: list[tuple[dict[str, object], int]] = []
        calls: list[tuple[pathlib.Path, float, str]] = []
        cleanup_payload = {
            "summary": {},
            "rows": [],
            "retention": {
                "selected": {
                    "preview_signature": "fresh",
                    "scanned_rows": 12,
                    "deletable_rows": 0,
                    "pending_turn_state_deletable_files": 1,
                }
            },
        }
        handler.server = types.SimpleNamespace(db_path=str(output))
        handler.read_json_body = lambda: {"cutoff_date": "2026-05-20", "preview_signature": "fresh"}
        handler.cleanup_cutoff_unix = lambda value=None: 1779235200.0
        handler.cleanup_payload = lambda db_path=None, retention_cutoff_unix=None: cleanup_payload | {"retention_cutoff_unix": retention_cutoff_unix}
        handler.run_retention_prune_command = lambda db_path, cutoff, preview_signature: calls.append((db_path, cutoff, preview_signature)) or {
            "returncode": 0,
            "stdout": '{"deleted_rows":0,"deleted_state_files":1}',
            "stderr": "",
            "metadata": {"delete": {"deleted_rows": 0, "deleted_state_files": 1}},
        }
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(calls, [(output, 1779235200.0, "fresh")])
        self.assertEqual(sent[0][1], 200)
        self.assertNotIn("noop", sent[0][0])
        self.assertEqual(sent[0][0]["retention"]["deleted_state_files"], 1)

    def test_log_cleanup_retention_reports_preview_manifest_failure(self) -> None:
        serve = load_module("serve_dashboard_retention_manifest_failure_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {"cutoff_date": "2026-05-20", "preview_signature": "fresh"}
        handler.cleanup_cutoff_unix = lambda value=None: 1779235200.0
        handler.cleanup_payload = lambda db_path=None, retention_cutoff_unix=None: (_ for _ in ()).throw(
            serve.dashboard_cleanup.raw_segments.ManifestError("pending rotation must be resolved")
        )
        handler.run_retention_prune_command = lambda _db_path, _cutoff: (_ for _ in ()).throw(AssertionError("prune must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(sent[0][1], 409)
        self.assertEqual(sent[0][0]["error"], "cleanup_preview_failed")
        self.assertIn("pending rotation", sent[0][0]["message"])

    def test_log_cleanup_retention_reports_cli_stale_preview_as_conflict(self) -> None:
        serve = load_module("serve_dashboard_retention_cli_stale_preview_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {"cutoff_date": "2026-05-20", "preview_signature": "fresh"}
        handler.cleanup_payload = lambda db_path=None, retention_cutoff_unix=None: {
            "summary": {},
            "rows": [],
            "retention": {"selected": {"preview_signature": "fresh", "deletable_rows": 1}},
        }
        handler.run_retention_prune_command = lambda _db_path, _cutoff, _preview_signature: {
            "returncode": 2,
            "stdout": '{"error":"cleanup_preview_stale"}',
            "stderr": "",
            "metadata": {"error": "cleanup_preview_stale"},
        }
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(sent[0][1], 409)
        self.assertEqual(sent[0][0]["error"], "cleanup_preview_stale")

    def test_log_cleanup_retention_reports_partial_mutation_failure(self) -> None:
        serve = load_module("serve_dashboard_retention_partial_failure_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {"cutoff_date": "2026-05-20", "preview_signature": "fresh"}
        handler.cleanup_payload = lambda db_path=None, retention_cutoff_unix=None: {"retention": {"selected": {"preview_signature": "fresh", "deletable_rows": 1}}}
        handler.run_retention_prune_command = lambda _db_path, _cutoff, _preview_signature: {
            "returncode": 1,
            "stdout": '{"partial_mutation":true,"stage":"build","deleted_rows":3}',
            "stderr": "build failed",
            "metadata": {
                "partial_mutation": True,
                "recovery_required": True,
                "derived_rebuild_required": True,
                "physical_delete_pending": True,
                "pending_files": 2,
                "stage": "build",
                "deleted_rows": 3,
            },
        }
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(sent[0][1], 500)
        self.assertEqual(sent[0][0]["error"], "retention_prune_failed")
        self.assertTrue(sent[0][0]["partial_mutation"])
        self.assertTrue(sent[0][0]["recovery_required"])
        self.assertTrue(sent[0][0]["derived_rebuild_required"])
        self.assertTrue(sent[0][0]["physical_delete_pending"])
        self.assertEqual(sent[0][0]["pending_files"], 2)
        self.assertEqual(sent[0][0]["stage"], "build")
        self.assertEqual(sent[0][0]["deleted_rows"], 3)

    def test_log_cleanup_delete_all_removes_generated_service_data(self) -> None:
        cleanup = load_module("dashboard_cleanup_delete_all_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            raw_current = base / "raw" / "current"
            raw_archive = base / "raw" / "archive"
            normalized = base / "normalized"
            analytics = base / "analytics"
            reports = base / "reports"
            state = base / "state"
            tmp = base / "tmp"
            bad = base / "bad"
            for directory in (raw_current, raw_archive, normalized, analytics, reports, state, tmp, bad):
                directory.mkdir(parents=True, exist_ok=True)
            report = reports / "report.json"
            report.write_bytes(b"x\n")
            files = [
                base / "raw" / "prompt-usage.raw.jsonl",
                raw_current / "prompt-usage.raw.jsonl.current.1.jsonl",
                raw_archive / "prompt-usage.raw.jsonl.20260101.gz",
                normalized / "prompt-usage.normalized.jsonl",
                analytics / "token-usage.sqlite",
                state / "current-raw-segments.json",
                tmp / "work.tmp",
                bad / "bad.jsonl",
                base / "prompt-usage.jsonl",
                base / "hook-probe-events.jsonl",
            ]
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x\n")
            lock = state / "raw-segment.lock"
            lock.write_bytes(b"")
            outside = pathlib.Path(tmp_dir) / "outside.txt"
            outside.write_text("keep", encoding="utf-8")

            result = cleanup.delete_all_logs(base, analytics / "token-usage.sqlite")

            self.assertGreater(result["deleted_bytes"], 0)
            for path in files:
                self.assertFalse(path.exists(), str(path))
            self.assertTrue(report.exists())
            self.assertTrue(lock.exists())
            self.assertTrue(outside.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_log_cleanup_delete_all_uses_service_lock(self) -> None:
        source = (ROOT / "scripts" / "dashboard_cleanup.py").read_text(encoding="utf-8")

        self.assertIn("import service_lock", source)
        self.assertIn("service_lock.acquire_service_lock", source)

    def test_log_cleanup_delete_all_api_requires_confirmation(self) -> None:
        serve = load_module("serve_dashboard_delete_all_confirm_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.server = types.SimpleNamespace(db_path="/tmp/token-usage.sqlite")
        handler.read_json_body = lambda: {}
        handler.send_json = lambda payload, status=200: sent.append((payload, status))
        handler.delete_all_logs = lambda _base, _db_path: (_ for _ in ()).throw(AssertionError("delete all must not run"))

        handler.handle_cleanup_delete_all()

        self.assertEqual(sent[0][1], 400)
        self.assertEqual(sent[0][0]["error"], "delete_all_confirmation_required")

    def test_log_cleanup_delete_all_api_deletes_and_refreshes_payload(self) -> None:
        serve = load_module("serve_dashboard_delete_all_api_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        db_path = pathlib.Path("/tmp/token-usage.sqlite")
        handler.server = types.SimpleNamespace(db_path=db_path)
        handler.read_json_body = lambda: {"confirm_all_logs": True}
        handler.delete_all_logs = lambda base, output: {"deleted_bytes": 12, "deleted": [{"path": str(base), "deleted_bytes": 12}]}
        handler.cleanup_payload = lambda db_path=None: {"summary": {"service_bytes": 0}, "rows": []}
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_delete_all()

        self.assertEqual(sent[0][1], 200)
        self.assertTrue(sent[0][0]["ok"])
        self.assertEqual(sent[0][0]["deleted_bytes"], 12)
        self.assertEqual(sent[0][0]["cleanup"]["summary"]["service_bytes"], 0)

    def test_log_cleanup_delete_all_api_reports_partial_delete_failure(self) -> None:
        serve = load_module("serve_dashboard_delete_all_partial_failed_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        db_path = pathlib.Path("/tmp/token-usage.sqlite")
        handler.server = types.SimpleNamespace(db_path=db_path)
        handler.read_json_body = lambda: {"confirm_all_logs": True}
        handler.delete_all_logs = lambda base, output: {
            "deleted_bytes": 12,
            "deleted": [{"path": str(base / "raw"), "deleted_bytes": 12}],
            "failed": [{"target": "tmp", "path": str(base / "tmp"), "deleted_bytes": 0, "failed": "OSError('blocked')"}],
            "delete_failed": True,
            "partial_mutation": True,
        }
        handler.cleanup_payload = lambda db_path=None: {"summary": {"service_bytes": 4}, "rows": []}
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_delete_all()

        self.assertEqual(sent[0][1], 500)
        self.assertFalse(sent[0][0]["ok"])
        self.assertEqual(sent[0][0]["error"], "cleanup_delete_failed")
        self.assertTrue(sent[0][0]["partial_mutation"])
        self.assertEqual(sent[0][0]["failed"][0]["target"], "tmp")
        self.assertEqual(sent[0][0]["deleted_bytes"], 12)

    def test_log_cleanup_delete_all_api_reports_busy_service_lock(self) -> None:
        serve = load_module("serve_dashboard_delete_all_busy_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        db_path = pathlib.Path("/tmp/token-usage.sqlite")
        handler.server = types.SimpleNamespace(db_path=db_path)
        handler.read_json_body = lambda: {"confirm_all_logs": True}
        handler.delete_all_logs = lambda _base, _output: (_ for _ in ()).throw(serve.service_lock.ServiceLockBusy(pathlib.Path("/tmp/service.lock")))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_delete_all()

        self.assertEqual(sent[0][1], 409)
        self.assertEqual(sent[0][0]["error"], "analysis_or_cleanup_running")

    def test_log_cleanup_retention_api_uses_full_prune_command(self) -> None:
        serve = load_module("serve_dashboard_retention_prune_command_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        output = pathlib.Path("/tmp/token-usage.sqlite")
        sent: list[tuple[dict[str, object], int]] = []
        calls: list[tuple[pathlib.Path, float, str]] = []
        handler.server = types.SimpleNamespace(db_path=str(output))
        handler.read_json_body = lambda: {"cutoff_date": "2026-05-20", "preview_signature": "fresh"}
        handler.cleanup_cutoff_unix = lambda value=None: 1779235200.0
        handler.run_retention_prune_command = lambda db_path, cutoff, preview_signature: calls.append((db_path, cutoff, preview_signature)) or {"returncode": 0, "stdout": '{"deleted_rows":1}', "stderr": "", "metadata": {"deleted_rows": 1, "delete": {"deleted_rows": 1}}}
        handler.cleanup_payload = lambda db_path=None, retention_cutoff_unix=None: {"summary": {}, "rows": [], "retention_cutoff_unix": retention_cutoff_unix, "retention": {"selected": {"preview_signature": "fresh", "deletable_rows": 1}}}
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_cleanup_retention()

        self.assertEqual(calls, [(output, 1779235200.0, "fresh")])
        self.assertEqual(sent[0][1], 200)
        self.assertEqual(sent[0][0]["retention"]["deleted_rows"], 1)
        self.assertIn("cleanup", sent[0][0])

    def test_log_cleanup_progress_endpoint_reads_cleanup_snapshot(self) -> None:
        serve = load_module("serve_dashboard_cleanup_progress_endpoint_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            progress_path = pathlib.Path(tmp_dir) / "cleanup-progress.json"
            serve.progress_control.write_progress_to_path(
                progress_path,
                status="running",
                phase="cleanup-delete",
                phase_index=1,
                phase_count=4,
                checkpoint="apply-retention",
                processed=3,
                total=8,
            )
            with serve.CLEANUP_PROGRESS_LOCK:
                previous_path = serve.CLEANUP_PROGRESS_FILE
                previous_running = serve.CLEANUP_RUNNING
                serve.CLEANUP_PROGRESS_FILE = progress_path
                serve.CLEANUP_RUNNING = True
            try:
                handler.send_json = lambda payload, status=200: sent.append((payload, status))
                handler.handle_cleanup_progress()
            finally:
                with serve.CLEANUP_PROGRESS_LOCK:
                    serve.CLEANUP_PROGRESS_FILE = previous_path
                    serve.CLEANUP_RUNNING = previous_running

        self.assertEqual(sent[0][1], 200)
        self.assertEqual(sent[0][0]["phase"], "cleanup-delete")
        self.assertTrue(sent[0][0]["cleanup_running"])
        self.assertEqual(sent[0][0]["processed"], 3)
        self.assertEqual(sent[0][0]["total"], 8)

    def test_cleanup_progress_close_removes_snapshot_file(self) -> None:
        serve = load_module("serve_dashboard_cleanup_progress_close_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        with tempfile.TemporaryDirectory() as tmp_dir:
            progress_path = pathlib.Path(tmp_dir) / "cleanup-progress.1.2.json"
            serve.progress_control.write_progress_to_path(progress_path, status="completed", phase="cleanup-refresh", checkpoint="completed")
            with serve.CLEANUP_PROGRESS_LOCK:
                previous_path = serve.CLEANUP_PROGRESS_FILE
                previous_running = serve.CLEANUP_RUNNING
                serve.CLEANUP_PROGRESS_FILE = progress_path
                serve.CLEANUP_RUNNING = True
            try:
                handler.close_cleanup_progress(progress_path)
                with serve.CLEANUP_PROGRESS_LOCK:
                    current_path = serve.CLEANUP_PROGRESS_FILE
                    current_running = serve.CLEANUP_RUNNING
            finally:
                with serve.CLEANUP_PROGRESS_LOCK:
                    serve.CLEANUP_PROGRESS_FILE = previous_path
                    serve.CLEANUP_RUNNING = previous_running

        self.assertFalse(progress_path.exists())
        self.assertIsNone(current_path)
        self.assertFalse(current_running)

    def test_transient_progress_sweep_removes_stale_progress_files_only(self) -> None:
        serve = load_module("serve_dashboard_progress_sweep_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir) / "token-usage"
            state = base / "state"
            state.mkdir(parents=True)
            stale_files = [
                state / "cleanup-progress.1.1.json",
                state / "rebuild-progress.1.1.json",
                state / "rebuild-cancel.1.1.json",
            ]
            kept_files = [
                state / "raw-segments-manifest.json",
                state / "current-raw-segments.json",
                state / "cleanup-retention-index.json",
                state / "service.lock",
            ]
            for path in [*stale_files, *kept_files]:
                path.write_text("{}\n", encoding="utf-8")

            removed = serve.sweep_transient_progress_files(base)
            stale_removed = all(not path.exists() for path in stale_files)
            kept_present = all(path.exists() for path in kept_files)

        self.assertEqual(sorted(item["name"] for item in removed), sorted(path.name for path in stale_files))
        self.assertTrue(stale_removed)
        self.assertTrue(kept_present)

    def test_log_cleanup_post_analyze_refreshes_retention_index_incrementally(self) -> None:
        source = (ROOT / "scripts" / "serve_dashboard.py").read_text(encoding="utf-8")
        self.assertIn("dashboard_cleanup.refresh_retention_index_for_current_sources(TOKEN_USAGE_ROOT)", source)
        self.assertNotIn("dashboard_cleanup.rebuild_retention_index(TOKEN_USAGE_ROOT)", source)
