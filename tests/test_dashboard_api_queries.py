from __future__ import annotations

try:
    from tests.support import DashboardFixtureMixin, ROOT, _raw_segment, concurrent, dashboard_asset_bundle, io, json, load_module, mock, pathlib, sqlite3, tempfile, time, types, unittest
except ModuleNotFoundError:
    from support import DashboardFixtureMixin, ROOT, _raw_segment, concurrent, dashboard_asset_bundle, io, json, load_module, mock, pathlib, sqlite3, tempfile, time, types, unittest


DASHBOARD_ASSET_BUNDLE = dashboard_asset_bundle()


class DashboardApiQueryTests(DashboardFixtureMixin, unittest.TestCase):
    def secure_post_handler(self, serve, body: bytes = b"{}"):
        handler = serve.Handler.__new__(serve.Handler)
        handler.server = types.SimpleNamespace(
            allowed_authority="127.0.0.1:8766",
            allowed_origin="http://127.0.0.1:8766",
        )
        handler.path = "/api/rebuild"
        handler.headers = {
            "Host": "127.0.0.1:8766",
            "Origin": "http://127.0.0.1:8766",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body)
        return handler

    def write_empty_freshness_fixture(self, base: pathlib.Path) -> pathlib.Path:
        state_dir = base / "state"
        normalized_dir = base / "normalized"
        analytics_dir = base / "analytics"
        state_dir.mkdir(parents=True, exist_ok=True)
        normalized_dir.mkdir(parents=True, exist_ok=True)
        analytics_dir.mkdir(parents=True, exist_ok=True)
        (normalized_dir / "normalize-state.json").write_text(
            json.dumps({"logic_version": 5, "sources": {}, "processed_segments": {}}),
            encoding="utf-8",
        )
        (state_dir / "current-raw-segments.json").write_text(
            json.dumps({"schema_version": 1, "base": str(base.resolve()), "current": {}}),
            encoding="utf-8",
        )
        (state_dir / "raw-segments-manifest.json").write_text(
            json.dumps({"schema_version": 1, "base": str(base.resolve()), "segments": []}),
            encoding="utf-8",
        )
        db_path = analytics_dir / "bola.sqlite"
        db_path.write_text("", encoding="utf-8")
        return db_path

    def test_int_query_falls_back_and_clamps(self) -> None:
        queries = load_module("dashboard_queries_test", ROOT / "scripts" / "dashboard_queries.py")
        self.assertEqual(queries.int_query({"days": ["bad"]}, "days", 7, 0, 3650), 7)
        self.assertEqual(queries.int_query({"page": ["-2"]}, "page", 1, 1, 100), 1)
        self.assertEqual(queries.int_query({"per_page": ["500"]}, "per_page", 25, 1, 100), 100)

    def test_cost_rates_payload_prioritizes_detected_models_and_reports_coverage(self) -> None:
        api = load_module("dashboard_cost_rates_payload_test", ROOT / "scripts" / "dashboard_cost_rates_api.py")
        schema = load_module("dashboard_cost_rates_schema_test", ROOT / "scripts" / "build_analytics_schema.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            db_path = root / "analytics.sqlite"
            config_path = root / "cost-rates.json"
            con = sqlite3.connect(db_path)
            schema.setup_db(con)
            con.executemany(
                """
                insert into turns (
                  session_id, turn_id, captured_at_unix, started_at_unix,
                  model, estimated, weighted_credits, cost_rate_status
                ) values (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                [
                    ("s1", "t1", 1_775_001_600, 1_775_001_600, "gpt-5.5", None, "unconfigured"),
                    ("s2", "t2", 1_785_542_400, 1_785_542_400, "gpt-5.6-terra", 2.0, "configured"),
                    ("s3", "t3", 1_785_542_400, 1_785_542_400, None, None, "unconfigured"),
                ],
            )
            con.commit()
            con.close()

            payload = api.cost_rates_payload(config_path=config_path, db_path=db_path)
            by_model = {row["model_id"]: row for row in payload["models"]}

            self.assertEqual([row["model_id"] for row in payload["models"][:3]], ["gpt-5.5", "gpt-5.6-terra", "unknown"])
            self.assertEqual(by_model["gpt-5.5"]["status"], "configured")
            self.assertFalse(by_model["gpt-5.5"]["coverage_required"])
            self.assertTrue(by_model["gpt-5.5"]["current"]["is_default"])
            self.assertIsNone(by_model["gpt-5.5"]["current"]["effective_from"])
            self.assertEqual(by_model["gpt-5.6-terra"]["status"], "configured")
            self.assertFalse(by_model["gpt-5.6-terra"]["coverage_required"])
            self.assertEqual(by_model["unknown"]["status"], "unavailable")
            gpt_56_history = by_model["gpt-5.6"]["history"]
            self.assertFalse(next(rate for rate in gpt_56_history if rate["is_default"])["deletable"])
            self.assertTrue(next(rate for rate in gpt_56_history if rate["effective_from"] == "2026-08-21")["deletable"])
            self.assertTrue(payload["rebuild_required"])

            con = sqlite3.connect(db_path)
            con.execute(
                "insert or replace into run_metadata values (?, ?)",
                ("cost_rate_catalog_digest", json.dumps(payload["catalog_digest"])),
            )
            con.commit()
            con.close()
            current = api.cost_rates_payload(config_path=config_path, db_path=db_path)
            self.assertFalse(current["rebuild_required"])

    def test_dashboard_cost_totals_use_exact_per_turn_cost_units(self) -> None:
        queries = load_module("dashboard_queries_exact_cost_test", ROOT / "scripts" / "dashboard_queries.py")
        schema = load_module("dashboard_schema_exact_cost_test", ROOT / "scripts" / "build_analytics_schema.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            con = sqlite3.connect(db_path)
            schema.setup_db(con)
            con.executemany(
                """
                insert into turns (
                  session_id, turn_id, started_at_unix, estimated,
                  weighted_credits, cost_pico_usd, cost_rate_status
                ) values (?, ?, ?, 0, ?, ?, 'configured')
                """,
                [
                    ("s1", "t1", 1.0, 99.0, 1_000_001),
                    ("s1", "t2", 2.0, 88.0, 2_000_002),
                ],
            )
            con.commit()
            con.row_factory = sqlite3.Row
            payload = queries.DashboardQueries(con, {"days": ["0"]}).summary_payload()
            con.close()

        self.assertEqual(payload["weighted_credits"], 3.000003)
        self.assertTrue(payload["cost_complete"])

    def test_turn_date_scope_and_order_use_prompt_start_not_recovery_capture(self) -> None:
        queries = load_module("dashboard_queries_prompt_time_test", ROOT / "scripts" / "dashboard_queries.py")
        schema = load_module("dashboard_schema_prompt_time_test", ROOT / "scripts" / "build_analytics_schema.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            con = sqlite3.connect(db_path)
            schema.setup_db(con)
            now = int(time.time())
            con.executemany(
                """
                insert into turns (
                  session_id, turn_id, captured_at, captured_at_unix, started_at, started_at_unix,
                  turn_status, estimated, prompt_preview, weighted_credits, total_tokens, model_call_count
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("old", "recovered", "2026-08-23T07:57:02+00:00", now, "2026-07-19T17:39:06+00:00", now - 35 * 86400, "completed", 1, "old recovered prompt", 1.0, 100, 1),
                    ("new", "normal", "2026-08-20T00:00:00+00:00", now - 3 * 86400, "2026-08-23T06:57:02+00:00", now - 3600, "completed", 0, "recent prompt", 2.0, 200, 1),
                ],
            )
            con.commit()
            con.row_factory = sqlite3.Row
            scoped = queries.DashboardQueries(con, {"days": ["1"], "page": ["1"], "per_page": ["25"]}).turns_payload()
            ordered = queries.DashboardQueries(con, {"days": ["0"], "page": ["1"], "per_page": ["25"]}).turns_payload()
            con.close()

        self.assertEqual([row["turn_id"] for row in scoped["rows"]], ["normal"])
        self.assertEqual([row["turn_id"] for row in ordered["rows"]], ["normal", "recovered"])
        self.assertEqual(ordered["rows"][0]["started_at"], "2026-08-23T06:57:02+00:00")

    def test_turn_page_is_clamped_after_filter_count(self) -> None:
        queries = load_module("dashboard_queries_turn_page_clamp_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            session_id, filtered_total = con.execute(
                "select session_id, count(*) from turns group by session_id order by count(*) asc limit 1"
            ).fetchone()
            payload = queries.DashboardQueries(
                con,
                {
                    "days": ["0"],
                    "session_id": [session_id],
                    "page": ["999"],
                    "per_page": ["1"],
                    "sort": ["date"],
                    "sort_dir": ["desc"],
                },
            ).turns_payload()
            con.close()

        self.assertEqual(payload["total"], filtered_total)
        self.assertEqual(payload["page"], filtered_total)
        self.assertEqual(payload["per_page"], 1)
        self.assertEqual(len(payload["rows"]), 1)

    def test_server_rejects_non_loopback_host(self) -> None:
        serve = load_module("serve_dashboard_network_guard_test", ROOT / "scripts" / "serve_dashboard.py")

        def fail_server(*_args, **_kwargs):
            raise AssertionError("server must not bind before network policy check")

        with (
            mock.patch.object(serve.sys, "argv", ["serve_dashboard.py", "--host", "0.0.0.0"]),
            mock.patch.object(serve, "ThreadingHTTPServer", side_effect=fail_server),
        ):
            result = serve.main()

        self.assertEqual(result, 2)

    def test_server_rejects_ipv6_loopback_before_bind(self) -> None:
        serve = load_module("serve_dashboard_ipv6_guard_test", ROOT / "scripts" / "serve_dashboard.py")

        def fail_server(*_args, **_kwargs):
            raise AssertionError("server must not bind before IPv4 loopback policy check")

        stderr = io.StringIO()
        with (
            mock.patch.object(serve.sys, "argv", ["serve_dashboard.py", "--host", "::1"]),
            mock.patch.object(serve, "ThreadingHTTPServer", side_effect=fail_server),
            mock.patch.object(serve.sys, "stderr", stderr),
        ):
            result = serve.main()

        self.assertEqual(result, 2)
        self.assertIn("use localhost or an IPv4 loopback address", stderr.getvalue())

    def test_server_accepts_only_supported_ipv4_loopback_hosts(self) -> None:
        serve = load_module("serve_dashboard_loopback_policy_test", ROOT / "scripts" / "serve_dashboard.py")

        self.assertTrue(serve.is_loopback_host("127.0.0.1"))
        self.assertTrue(serve.is_loopback_host("127.1.2.3"))
        self.assertTrue(serve.is_loopback_host("localhost"))
        self.assertFalse(serve.is_loopback_host("::1"))
        self.assertFalse(serve.is_loopback_host("0.0.0.0"))

    def test_server_rejects_removed_allow_network_option(self) -> None:
        serve = load_module("serve_dashboard_network_option_test", ROOT / "scripts" / "serve_dashboard.py")
        with (
            mock.patch.object(serve.sys, "argv", ["serve_dashboard.py", "--host", "0.0.0.0", "--allow-network"]),
            self.assertRaises(SystemExit),
        ):
            serve.main()

    def test_server_ignores_removed_analytics_db_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = pathlib.Path(tmp_dir) / ".codex"
            output_dir = pathlib.Path(tmp_dir) / "output"
            external_db = pathlib.Path(tmp_dir) / "outside.sqlite"
            with mock.patch.dict(
                "os.environ",
                {
                    "CODEX_HOME": str(codex_dir),
                    "BOLA_OUTPUT_DIR": str(output_dir),
                    "BOLA_ANALYTICS_DB": str(external_db),
                },
                clear=False,
            ):
                serve = load_module("serve_dashboard_external_db_guard_test", ROOT / "scripts" / "serve_dashboard.py")

        self.assertEqual(serve.DB_PATH, output_dir / "analytics" / "bola.sqlite")

    def test_terminate_rebuild_process_kills_after_grace_timeout(self) -> None:
        serve = load_module("serve_dashboard_cancel_kill_test", ROOT / "scripts" / "serve_dashboard.py")
        calls: list[str] = []

        class StubbornProcess:
            def poll(self):
                return None

            def terminate(self):
                calls.append("terminate")

            def wait(self, timeout=None):
                calls.append(f"wait:{timeout}")
                raise serve.dashboard_rebuild_api.subprocess.TimeoutExpired("cmd", timeout)

            def kill(self):
                calls.append("kill")

        result = serve.terminate_rebuild_process(StubbornProcess(), grace_seconds=0.01)

        self.assertEqual(result, "killed")
        self.assertEqual(calls, ["terminate", "wait:0.01", "kill"])

    def test_missing_analytics_db_serves_empty_initial_dashboard_payload(self) -> None:
        serve = load_module("serve_dashboard_missing_db_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics" / "bola.sqlite"
            serve.OUTPUT_DIR = pathlib.Path(tmp_dir)
            handler = serve.Handler.__new__(serve.Handler)
            handler.server = types.SimpleNamespace(db_path=db_path)
            captured: dict[str, object] = {}

            def send_json(data, status=200):
                captured["status"] = status
                captured["data"] = data

            handler.send_json = send_json
            handler.handle_api("/api/dashboard", {"days": ["7"], "limit": ["100"], "page": ["2"], "per_page": ["5"]})

        self.assertEqual(captured["status"], 200)
        payload = captured["data"]
        self.assertEqual(payload["summary"]["turns"], 0)
        self.assertEqual(payload["summary"]["total_tokens"], 0)
        self.assertEqual(payload["summary"]["tool_calls"], 0)
        self.assertEqual(payload["projects"]["rows"], [])
        self.assertEqual(payload["sessions"]["rows"], [])
        self.assertEqual(payload["turns"], {"rows": [], "total": 0, "page": 1, "per_page": 5, "focused": False})
        self.assertEqual(payload["tools"]["rows"], [])
        self.assertEqual([row["rows"] for row in payload["subagents"]["rows"]], [0, 0, 0, 0, 0])
        self.assertEqual(payload["freshness"]["status"], "missing_db")
        self.assertFalse(payload["freshness"]["needs_analyze"])

    def test_stale_analytics_db_serves_empty_dashboard_payload_instead_of_500(self) -> None:
        serve = load_module("serve_dashboard_stale_db_dashboard_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    create table turns (
                      session_id text,
                      turn_id text,
                      captured_at_unix real,
                      total_tokens integer
                    );
                    create table run_metadata (key text primary key, value text);
                    """
                )
                con.commit()
            finally:
                con.close()
            serve.OUTPUT_DIR = base
            handler = serve.Handler.__new__(serve.Handler)
            handler.server = types.SimpleNamespace(db_path=db_path)
            sent: list[tuple[dict[str, object], int]] = []
            handler.send_json = lambda payload, status=200: sent.append((payload, status))

            handler.handle_api("/api/dashboard", {"days": ["7"], "page": ["1"], "per_page": ["25"]})

        self.assertEqual(sent[0][1], 200)
        payload = sent[0][0]
        self.assertEqual(payload["summary"]["turns"], 0)
        self.assertEqual(payload["turns"]["rows"], [])
        self.assertEqual(payload["freshness"]["data_health"], "degraded")
        self.assertIn("analytics_schema_stale", [warning["code"] for warning in payload["freshness"]["warnings"]])

    def test_stale_analytics_db_serves_empty_turns_payload_instead_of_500(self) -> None:
        serve = load_module("serve_dashboard_stale_db_turns_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    create table turns (
                      session_id text,
                      turn_id text,
                      captured_at_unix real,
                      total_tokens integer
                    );
                    create table run_metadata (key text primary key, value text);
                    """
                )
                con.commit()
            finally:
                con.close()
            serve.OUTPUT_DIR = base
            handler = serve.Handler.__new__(serve.Handler)
            handler.server = types.SimpleNamespace(db_path=db_path)
            sent: list[tuple[dict[str, object], int]] = []
            handler.send_json = lambda payload, status=200: sent.append((payload, status))

            handler.handle_api("/api/turns", {"page": ["2"], "per_page": ["5"]})

        self.assertEqual(sent, [({"rows": [], "total": 0, "page": 1, "per_page": 5, "focused": False}, 200)])

    def test_dashboard_freshness_counts_pending_raw_rows_since_normalize_state(self) -> None:
        freshness = load_module("dashboard_freshness_pending_rows_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            current_dir = base / "raw" / "current"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            current_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_path = current_dir / "prompt-usage.raw.jsonl.current.1.jsonl"
            first = json.dumps({"record_type": "turn_usage_raw", "turn_id": "t1"}) + "\n"
            second = json.dumps({"record_type": "turn_usage_raw", "turn_id": "t2"}) + "\n"
            third = json.dumps({"record_type": "turn_usage_raw", "turn_id": "t3"}) + "\n"
            raw_path.write_text(first + second + third, encoding="utf-8")
            (normalized_dir / "normalize-state.json").write_text(
                json.dumps({"logic_version": 5, "sources": {str(raw_path): len(first)}, "processed_segments": {}}),
                encoding="utf-8",
            )
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertTrue(payload["needs_analyze"])
        self.assertEqual(payload["pending_raw_rows"], 2)
        self.assertEqual(payload["pending_raw_files"], 1)
        self.assertGreater(payload["latest_raw_mtime_unix"], 0)
        self.assertGreater(payload["analytics_db_mtime_unix"], 0)

    def test_dashboard_freshness_degrades_when_current_raw_cannot_be_read(self) -> None:
        freshness = load_module("dashboard_freshness_unreadable_raw_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            raw_dir = base / "raw" / "current"
            state_dir = base / "state"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir()
            normalized_dir.mkdir()
            analytics_dir.mkdir()
            raw_path = raw_dir / "prompt-usage.raw.jsonl.current.unreadable.jsonl"
            raw_path.write_text(json.dumps({"record_type": "turn_usage_raw", "turn_id": "pending"}) + "\n", encoding="utf-8")
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "base": str(base.resolve()),
                        "current": {
                            "prompt_usage": {
                                "id": "unreadable",
                                "kind": "prompt_usage",
                                "path": str(raw_path),
                                "source_name": "prompt-usage.raw.jsonl",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "raw-segments-manifest.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "segments": []}),
                encoding="utf-8",
            )
            (normalized_dir / "normalize-state.json").write_text(
                json.dumps({"logic_version": 5, "sources": {}, "processed_segments": {}}),
                encoding="utf-8",
            )
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")
            raw_path.chmod(0)
            try:
                payload = freshness.freshness_payload(base, db_path)
            finally:
                raw_path.chmod(0o600)

        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["needs_analyze"])
        self.assertEqual(payload["data_health"], "degraded")
        self.assertEqual(payload["pending_raw_rows"], 0)
        self.assertIn("freshness_source_read_error", [warning["code"] for warning in payload["warnings"]])

    def test_dashboard_freshness_degrades_when_normalized_log_cannot_be_read(self) -> None:
        freshness = load_module("dashboard_freshness_unreadable_normalized_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            normalized_path = base / "normalized" / "prompt-usage.normalized.jsonl"
            normalized_path.write_text(json.dumps({"turn_id": "pending"}) + "\n", encoding="utf-8")
            normalized_path.chmod(0)
            try:
                payload = freshness.freshness_payload(base, db_path)
            finally:
                normalized_path.chmod(0o600)

        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["needs_analyze"])
        self.assertEqual(payload["data_health"], "degraded")
        self.assertEqual(payload["pending_normalized_rows"], 0)
        self.assertIn("freshness_source_read_error", [warning["code"] for warning in payload["warnings"]])

    def test_dashboard_freshness_counts_missing_start_recovery_state_as_analyze_needed(self) -> None:
        freshness = load_module("dashboard_freshness_pending_recovery_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            state_dir = base / "state"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            state_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            (state_dir / "pending-turn.json").write_text(
                json.dumps({"record_type": "turn_stop_missing_start", "session_id": "s1", "turn_id": "t1"}),
                encoding="utf-8",
            )
            (state_dir / "rebuild-progress.1.1.json").write_text(
                json.dumps({"status": "running"}),
                encoding="utf-8",
            )
            (normalized_dir / "normalize-state.json").write_text(
                json.dumps({"logic_version": 5, "sources": {}, "processed_segments": {}}),
                encoding="utf-8",
            )
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "current": {}}),
                encoding="utf-8",
            )
            (state_dir / "raw-segments-manifest.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "segments": []}),
                encoding="utf-8",
            )
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertTrue(payload["needs_analyze"])
        self.assertEqual(payload["pending_recovery_files"], 1)
        self.assertEqual(payload["pending_analysis_rows"], 0)

    def test_dashboard_freshness_ignores_stale_turn_start_without_terminal_evidence(self) -> None:
        freshness = load_module("dashboard_freshness_stale_active_turn_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            (base / "state" / "active-turn.json").write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-active",
                        "turn_id": "t-active",
                        "captured_at": "2000-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "current")
        self.assertFalse(payload["needs_analyze"])
        self.assertEqual(payload["pending_recovery_files"], 0)

    def test_dashboard_freshness_counts_turn_start_with_terminal_event_as_recovery_pending(self) -> None:
        freshness = load_module("dashboard_freshness_terminal_turn_start_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            transcript = base / "rollout.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-22T00:00:00+00:00",
                        "payload": {"type": "task_complete", "turn_id": "t-terminal"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (base / "state" / "pending-turn.json").write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-terminal",
                        "turn_id": "t-terminal",
                        "transcript_path": str(transcript),
                        "captured_at": "2026-06-21T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertTrue(payload["needs_analyze"])
        self.assertEqual(payload["pending_recovery_files"], 1)

    def test_dashboard_freshness_does_not_match_terminal_event_before_turn_start_offset(self) -> None:
        freshness = load_module("dashboard_freshness_terminal_turn_start_fallback_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            transcript = base / "rollout.jsonl"
            terminal = json.dumps({"type": "event_msg", "timestamp": "2026-06-22T00:00:00+00:00", "payload": {"type": "task_complete", "turn_id": "t-terminal"}}) + "\n"
            transcript.write_text(terminal + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}}}) + "\n", encoding="utf-8")
            (base / "state" / "pending-turn.json").write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-terminal",
                        "turn_id": "t-terminal",
                        "transcript_path": str(transcript),
                        "start_file_size": len(terminal),
                        "captured_at": "2026-06-21T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "current")
        self.assertFalse(payload["needs_analyze"])
        self.assertEqual(payload["pending_recovery_files"], 0)

    def test_dashboard_freshness_ignores_malformed_transcript_bytes_for_recovery(self) -> None:
        freshness = load_module("dashboard_freshness_malformed_recovery_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            transcript = base / "rollout.jsonl"
            transcript.write_bytes(b"\xff\n")
            (base / "state" / "pending-turn.json").write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-bad",
                        "turn_id": "t-bad",
                        "transcript_path": str(transcript),
                        "start_file_size": 0,
                        "captured_at": "2026-06-21T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "current")
        self.assertFalse(payload["needs_analyze"])
        self.assertEqual(payload["pending_recovery_files"], 0)

    def test_dashboard_freshness_caches_terminal_scan_per_transcript(self) -> None:
        freshness = load_module("dashboard_freshness_terminal_scan_cache_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            transcript = base / "rollout.jsonl"
            prefix = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}}}) + "\n"
            terminal_one = json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-one"}}) + "\n"
            terminal_two = json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-two"}}) + "\n"
            transcript.write_text(prefix + terminal_one + terminal_two, encoding="utf-8")
            start_file_size = len(prefix.encode("utf-8"))
            for name, turn_id in (("one", "t-one"), ("two", "t-two")):
                (base / "state" / f"pending-{name}.json").write_text(
                    json.dumps(
                        {
                            "record_type": "turn_start",
                            "session_id": f"s-{name}",
                            "turn_id": turn_id,
                            "transcript_path": str(transcript),
                            "start_file_size": start_file_size,
                            "captured_at": "2026-06-21T00:00:00+00:00",
                        }
                    ),
                    encoding="utf-8",
                )
            original_open = pathlib.Path.open
            transcript_opens = 0

            def counting_open(path: pathlib.Path, *args: object, **kwargs: object):
                nonlocal transcript_opens
                if pathlib.Path(path) == transcript:
                    transcript_opens += 1
                return original_open(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "open", counting_open):
                payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["pending_recovery_files"], 2)
        self.assertLessEqual(transcript_opens, 2)

    def test_dashboard_freshness_scans_only_new_complete_transcript_suffixes(self) -> None:
        freshness = load_module("dashboard_freshness_terminal_suffix_cache_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            transcript = pathlib.Path(tmp_dir) / "rollout.jsonl"
            prefix = json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n"
            terminal_one = json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-one"}}) + "\n"
            transcript.write_text(prefix + terminal_one, encoding="utf-8")
            states = [{"turn_id": "t-one", "start_file_size": len(prefix.encode("utf-8"))}]

            with mock.patch.object(freshness, "_scan_terminal_turn_ids", wraps=freshness._scan_terminal_turn_ids) as scan:
                self.assertEqual(freshness._terminal_turn_ids_for_pending_states(transcript, states), {"t-one"})
                self.assertEqual(freshness._terminal_turn_ids_for_pending_states(transcript, states), {"t-one"})
                self.assertEqual(scan.call_count, 1)

                partial = json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-two"}})
                with transcript.open("ab") as handle:
                    handle.write(partial.encode("utf-8"))
                self.assertEqual(freshness._terminal_turn_ids_for_pending_states(transcript, states), {"t-one"})
                self.assertEqual(scan.call_count, 1)

                with transcript.open("ab") as handle:
                    handle.write(b"\n")
                states.append({"turn_id": "t-two", "start_file_size": len(prefix.encode("utf-8"))})
                self.assertEqual(freshness._terminal_turn_ids_for_pending_states(transcript, states), {"t-one", "t-two"})
                self.assertEqual(scan.call_count, 2)

    def test_dashboard_freshness_invalidates_same_size_transcript_rewrite(self) -> None:
        freshness = load_module("dashboard_freshness_terminal_rewrite_cache_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            transcript = pathlib.Path(tmp_dir) / "rollout.jsonl"
            one = json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-one"}}) + "\n"
            two = json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-two"}}) + "\n"
            self.assertEqual(len(one), len(two))
            transcript.write_text(one, encoding="utf-8")
            states = [{"turn_id": "t-one", "start_file_size": 0}]
            self.assertEqual(freshness._terminal_turn_ids_for_pending_states(transcript, states), {"t-one"})

            time.sleep(0.002)
            transcript.write_text(two, encoding="utf-8")
            self.assertEqual(
                freshness._terminal_turn_ids_for_pending_states(transcript, [{"turn_id": "t-two", "start_file_size": 0}]),
                {"t-two"},
            )

    def test_dashboard_freshness_coalesces_concurrent_terminal_scans(self) -> None:
        freshness = load_module("dashboard_freshness_terminal_singleflight_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            transcript = pathlib.Path(tmp_dir) / "rollout.jsonl"
            transcript.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-one"}}) + "\n",
                encoding="utf-8",
            )
            states = [{"turn_id": "t-one", "start_file_size": 0}]
            original_scan = freshness._scan_terminal_turn_ids

            def slow_scan(*args, **kwargs):
                time.sleep(0.02)
                return original_scan(*args, **kwargs)

            with mock.patch.object(freshness, "_scan_terminal_turn_ids", side_effect=slow_scan) as scan:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(lambda _index: freshness._terminal_turn_ids_for_pending_states(transcript, states), range(4)))

            self.assertEqual(results, [{"t-one"}] * 4)
            self.assertEqual(scan.call_count, 1)

    def test_dashboard_freshness_ignores_recent_turn_start_recovery_state(self) -> None:
        freshness = load_module("dashboard_freshness_recent_turn_start_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            (base / "state" / "active-turn.json").write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-active",
                        "turn_id": "t-active",
                        "captured_at_ns": 9_999_999_999_999_999_999,
                    }
                ),
                encoding="utf-8",
            )

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "current")
        self.assertFalse(payload["needs_analyze"])
        self.assertEqual(payload["pending_recovery_files"], 0)

    def test_dashboard_freshness_counts_missing_start_but_not_stale_unknown_turn_start_recovery_state(self) -> None:
        freshness = load_module("dashboard_freshness_stale_turn_start_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = self.write_empty_freshness_fixture(base)
            state_dir = base / "state"
            (state_dir / "stale-turn.json").write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "session_id": "s-stale",
                        "turn_id": "t-stale",
                        "captured_at": "2000-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "unknown-age-turn.json").write_text(
                json.dumps({"record_type": "turn_start", "session_id": "s-unknown", "turn_id": "t-unknown"}),
                encoding="utf-8",
            )
            (state_dir / "missing-start.json").write_text(
                json.dumps(
                    {
                        "record_type": "turn_stop_missing_start",
                        "session_id": "s-missing",
                        "turn_id": "t-missing",
                        "captured_at_ns": 9_999_999_999_999_999_999,
                    }
                ),
                encoding="utf-8",
            )

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertTrue(payload["needs_analyze"])
        self.assertEqual(payload["pending_recovery_files"], 1)

    def test_dashboard_freshness_excludes_closed_current_segments_from_pending_rows(self) -> None:
        freshness = load_module("dashboard_freshness_closed_current_segments_test", ROOT / "scripts" / "dashboard_freshness.py")
        raw_segments = load_module("dashboard_freshness_closed_current_raw_segments_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            closed_payload = json.dumps({"record_type": "turn_usage_raw", "turn_id": "closed"}) + "\n"
            active_first = json.dumps({"record_type": "turn_usage_raw", "turn_id": "active-1"}) + "\n"
            active_second = json.dumps({"record_type": "turn_usage_raw", "turn_id": "active-2"}) + "\n"

            closed_current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(closed_current["path"]).write_text(closed_payload, encoding="utf-8")
            rotation = raw_segments.rotate_all_current_segments(base)
            closed_segment = rotation["prompt_usage"]["closed_segment"]
            active_segment = rotation["prompt_usage"]["current_segment"]
            closed_path = pathlib.Path(closed_segment["path"])
            active_path = pathlib.Path(active_segment["path"])
            active_path.write_text(active_first + active_second, encoding="utf-8")
            raw_segments.strict_read_manifest(base)
            raw_segments.validate_current_pointer_entries(base)
            (normalized_dir / "normalize-state.json").write_text(
                json.dumps(
                    {
                        "logic_version": 5,
                        "sources": {str(active_path): len(active_first)},
                        "processed_segments": {closed_segment["id"]: {"path": str(closed_path), "bytes": closed_segment["bytes"], "rows": closed_segment["rows"]}},
                    }
                ),
                encoding="utf-8",
            )
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            payload = freshness.freshness_payload(base, db_path)
            active_mtime = active_path.stat().st_mtime

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertTrue(payload["needs_analyze"])
        self.assertEqual(payload["pending_raw_rows"], 1)
        self.assertEqual(payload["pending_raw_files"], 1)
        self.assertEqual(payload["pending_analysis_rows"], 1)
        self.assertEqual(payload["data_health"], "ok")
        self.assertEqual(payload["warnings"], [])
        self.assertGreaterEqual(payload["latest_raw_mtime_unix"], active_mtime)

    def test_dashboard_freshness_missing_pointer_falls_back_to_orphan_current(self) -> None:
        freshness = load_module("dashboard_freshness_missing_pointer_fallback_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            raw_dir = base / "raw" / "current"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_path = raw_dir / "prompt-usage.raw.jsonl.current.orphan.jsonl"
            raw_path.write_text(json.dumps({"record_type": "turn_usage_raw", "turn_id": "orphan"}) + "\n", encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertTrue(payload["needs_analyze"])
        self.assertEqual(payload["data_health"], "degraded")
        self.assertEqual(payload["pending_raw_rows"], 1)
        self.assertEqual(payload["pending_raw_files"], 1)
        self.assertEqual(payload["pending_analysis_rows"], 1)
        self.assertEqual([warning["code"] for warning in payload["warnings"]], ["current_pointer_missing", "normalize_state_missing", "raw_manifest_missing"])
        self.assertGreater(payload["latest_raw_mtime_unix"], 0)

    def test_dashboard_freshness_corrupt_pointer_reports_degraded_fallback(self) -> None:
        freshness = load_module("dashboard_freshness_corrupt_pointer_fallback_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            raw_dir = base / "raw" / "current"
            state_dir = base / "state"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_path = raw_dir / "prompt-usage.raw.jsonl.current.orphan.jsonl"
            raw_path.write_text(json.dumps({"record_type": "turn_usage_raw", "turn_id": "orphan"}) + "\n", encoding="utf-8")
            (state_dir / "current-raw-segments.json").write_text("{bad\n", encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertEqual(payload["data_health"], "degraded")
        self.assertEqual(payload["pending_raw_rows"], 1)
        self.assertIn("current_pointer_invalid_json", [warning["code"] for warning in payload["warnings"]])

    def test_dashboard_freshness_pointer_base_mismatch_is_degraded_fallback(self) -> None:
        freshness = load_module("dashboard_freshness_pointer_base_mismatch_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            raw_dir = base / "raw" / "current"
            state_dir = base / "state"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_path = raw_dir / "prompt-usage.raw.jsonl.current.orphan.jsonl"
            raw_path.write_text(json.dumps({"record_type": "turn_usage_raw", "turn_id": "orphan"}) + "\n", encoding="utf-8")
            (state_dir / "current-raw-segments.json").write_text(json.dumps({"schema_version": 1, "base": "/old/wrong", "current": {}}), encoding="utf-8")
            (state_dir / "raw-segments-manifest.json").write_text(json.dumps({"schema_version": 1, "base": str(base.resolve()), "segments": []}), encoding="utf-8")
            (normalized_dir / "normalize-state.json").write_text(json.dumps({"logic_version": 5, "sources": {}, "processed_segments": {}}), encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertEqual(payload["data_health"], "degraded")
        self.assertEqual(payload["pending_raw_rows"], 1)
        self.assertIn("current_pointer_base_mismatch", [warning["code"] for warning in payload["warnings"]])

    def test_dashboard_freshness_manifest_base_mismatch_does_not_exclude_fallback_current(self) -> None:
        freshness = load_module("dashboard_freshness_manifest_base_mismatch_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            raw_dir = base / "raw" / "current"
            state_dir = base / "state"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_path = raw_dir / "prompt-usage.raw.jsonl.current.orphan.jsonl"
            payload = (json.dumps({"record_type": "turn_usage_raw", "turn_id": "orphan"}) + "\n").encode("utf-8")
            raw_path.write_bytes(payload)
            segment_id = raw_path.name.removesuffix(".jsonl")
            (state_dir / "raw-segments-manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "base": "/old/wrong",
                        "segments": [_raw_segment(raw_path, payload=payload, min_time=None, max_time=None, rows=1, segment_id=segment_id)],
                    }
                ),
                encoding="utf-8",
            )
            (normalized_dir / "normalize-state.json").write_text(json.dumps({"logic_version": 5, "sources": {}, "processed_segments": {segment_id: {"path": str(raw_path)}}}), encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            result = freshness.freshness_payload(base, db_path)

        self.assertEqual(result["status"], "needs_analyze")
        self.assertEqual(result["data_health"], "degraded")
        self.assertEqual(result["pending_raw_rows"], 1)
        self.assertIn("raw_manifest_base_mismatch", [warning["code"] for warning in result["warnings"]])

    def test_dashboard_freshness_stale_pointer_missing_segment_falls_back_to_orphan_current(self) -> None:
        freshness = load_module("dashboard_freshness_stale_pointer_missing_segment_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            raw_dir = base / "raw" / "current"
            state_dir = base / "state"
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            missing_path = raw_dir / "prompt-usage.raw.jsonl.current.missing.jsonl"
            orphan_path = raw_dir / "prompt-usage.raw.jsonl.current.orphan.jsonl"
            orphan_path.write_text(json.dumps({"record_type": "turn_usage_raw", "turn_id": "orphan"}) + "\n", encoding="utf-8")
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "base": str(base.resolve()),
                        "current": {"prompt_usage": {"id": "missing", "kind": "prompt_usage", "path": str(missing_path), "source_name": "prompt-usage.raw.jsonl"}},
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "raw-segments-manifest.json").write_text(json.dumps({"schema_version": 1, "base": str(base.resolve()), "segments": []}), encoding="utf-8")
            (normalized_dir / "normalize-state.json").write_text(json.dumps({"logic_version": 5, "sources": {}, "processed_segments": {}}), encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            result = freshness.freshness_payload(base, db_path)

        self.assertEqual(result["status"], "needs_analyze")
        self.assertEqual(result["data_health"], "degraded")
        self.assertEqual(result["pending_raw_rows"], 1)
        self.assertIn("current_pointer_segment_missing", [warning["code"] for warning in result["warnings"]])

    def test_dashboard_freshness_stale_normalize_state_uses_source_from_zero_offset(self) -> None:
        freshness = load_module("dashboard_freshness_stale_normalize_state_test", ROOT / "scripts" / "dashboard_freshness.py")
        raw_segments = load_module("dashboard_freshness_stale_normalize_raw_segments_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_path = pathlib.Path(raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")["path"])
            first = json.dumps({"record_type": "turn_usage_raw", "turn_id": "first"}) + "\n"
            second = json.dumps({"record_type": "turn_usage_raw", "turn_id": "second"}) + "\n"
            raw_path.write_text(first + second, encoding="utf-8")
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base))
            (normalized_dir / "normalize-state.json").write_text(json.dumps({"logic_version": 4, "sources": {str(raw_path): len(first)}, "processed_segments": {}}), encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            result = freshness.freshness_payload(base, db_path)

        self.assertEqual(result["status"], "needs_analyze")
        self.assertEqual(result["data_health"], "degraded")
        self.assertEqual(result["pending_raw_rows"], 2)
        self.assertIn("normalize_state_logic_version_mismatch", [warning["code"] for warning in result["warnings"]])

    def test_dashboard_freshness_valid_empty_pointer_does_not_scan_orphan_current_glob(self) -> None:
        freshness = load_module("dashboard_freshness_empty_pointer_no_glob_test", ROOT / "scripts" / "dashboard_freshness.py")
        raw_segments = load_module("dashboard_freshness_empty_pointer_raw_segments_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            raw_dir = base / "raw" / "current"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_path = raw_dir / "prompt-usage.raw.jsonl.current.orphan.jsonl"
            payload = (json.dumps({"record_type": "turn_usage_raw", "turn_id": "closed"}) + "\n").encode("utf-8")
            raw_path.write_bytes(payload)
            raw_segments.write_current_pointer(base, raw_segments.empty_current_pointer(base))
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [_raw_segment(raw_path, payload=payload, min_time=None, max_time=None, rows=1)]})
            (base / "normalized").mkdir(parents=True)
            (base / "normalized" / "normalize-state.json").write_text(
                json.dumps({"logic_version": 5, "sources": {}, "processed_segments": {raw_path.name.removesuffix(".jsonl"): {"path": str(raw_path)}}}),
                encoding="utf-8",
            )
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")

            result = freshness.freshness_payload(base, db_path)

        self.assertEqual(result["status"], "current")
        self.assertFalse(result["needs_analyze"])
        self.assertEqual(result["pending_raw_rows"], 0)
        self.assertEqual(result["pending_raw_files"], 0)
        self.assertEqual(result["data_health"], "ok")
        self.assertEqual(result["warnings"], [])

    def test_dashboard_api_injects_freshness_health(self) -> None:
        serve = load_module("serve_dashboard_freshness_health_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            raw_dir = base / "raw" / "current"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_path = raw_dir / "prompt-usage.raw.jsonl.current.orphan.jsonl"
            raw_path.write_text(json.dumps({"record_type": "turn_usage_raw", "turn_id": "orphan"}) + "\n", encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"
            db_path.write_text("", encoding="utf-8")
            serve.OUTPUT_DIR = base
            handler = serve.Handler.__new__(serve.Handler)
            handler.server = types.SimpleNamespace(db_path=db_path)

            payload = handler.with_freshness("/api/dashboard", {"summary": {"turns": 0}})

        self.assertEqual(payload["freshness"]["status"], "needs_analyze")
        self.assertEqual(payload["freshness"]["data_health"], "degraded")
        self.assertEqual(payload["freshness"]["pending_raw_rows"], 1)
        self.assertIn("warnings", payload["freshness"])
        self.assertIn("current_pointer_missing", [warning["code"] for warning in payload["freshness"]["warnings"]])

    def test_dynamic_dashboard_uses_new_output_on_next_request(self) -> None:
        serve = load_module("serve_dashboard_dynamic_paths_test", ROOT / "scripts" / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            first = root / "A"
            second = root / "B"
            handler = serve.Handler.__new__(serve.Handler)
            handler.server = types.SimpleNamespace(dynamic_runtime_paths=True, db_override=None)
            with mock.patch.dict(serve.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                serve.service_paths.write_config({"output_dir": first})
                first_snapshot = handler.dashboard_output_dir()
                serve.service_paths.write_config({"output_dir": second})
                same_request_snapshot = handler.dashboard_output_dir()
                handler._runtime_paths_snapshot = None
                next_request_snapshot = handler.dashboard_output_dir()

        self.assertEqual(first_snapshot, first)
        self.assertEqual(same_request_snapshot, first)
        self.assertEqual(next_request_snapshot, second)

    def test_dashboard_freshness_detects_normalized_rows_not_built_into_db(self) -> None:
        freshness = load_module("dashboard_freshness_pending_normalized_test", ROOT / "scripts" / "dashboard_freshness.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            normalized = normalized_dir / "prompt-usage.normalized.jsonl"
            first = json.dumps({"record_type": "turn_usage_normalized", "turn_id": "t1"}) + "\n"
            second = json.dumps({"record_type": "turn_usage_normalized", "turn_id": "t2"}) + "\n"
            normalized.write_text(first + second, encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"
            con = sqlite3.connect(db_path)
            con.execute("create table run_metadata(key text primary key, value text not null)")
            con.execute("insert into run_metadata values (?,?)", ("applied_normalized_turns_size", json.dumps(len(first))))
            con.commit()
            con.close()

            payload = freshness.freshness_payload(base, db_path)

        self.assertEqual(payload["status"], "needs_analyze")
        self.assertTrue(payload["needs_analyze"])
        self.assertEqual(payload["pending_raw_rows"], 0)
        self.assertEqual(payload["pending_normalized_rows"], 1)
        self.assertEqual(payload["pending_analysis_rows"], 1)

    def test_post_api_errors_are_returned_as_json(self) -> None:
        serve = load_module("serve_dashboard_post_error_json_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = self.secure_post_handler(serve)
        sent: list[tuple[dict[str, object], int]] = []
        handler.handle_rebuild = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.do_POST()

        self.assertEqual(sent, [({"error": "internal_error"}, 500)])

    def test_post_api_rejects_malformed_json_body_before_mutation(self) -> None:
        serve = load_module("serve_dashboard_invalid_json_body_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = self.secure_post_handler(serve, b"{bad\n")
        sent: list[tuple[dict[str, object], int]] = []
        handler.handle_rebuild = lambda: (_ for _ in ()).throw(AssertionError("mutation must not start"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.do_POST()

        self.assertEqual(sent, [({"error": "invalid_json"}, 400)])

    def test_post_security_headers_block_before_mutation(self) -> None:
        serve = load_module("serve_dashboard_post_security_test", ROOT / "scripts" / "serve_dashboard.py")
        cases = (
            ("Host", "attacker.example", "request_host_forbidden", 403),
            ("Origin", "https://attacker.example", "request_origin_forbidden", 403),
            ("Sec-Fetch-Site", "cross-site", "cross_site_request_forbidden", 403),
            ("Content-Type", "text/plain", "application_json_required", 415),
        )

        for header, value, error, status in cases:
            with self.subTest(header=header):
                handler = self.secure_post_handler(serve, b'{"confirm_all_logs":true}')
                sent: list[tuple[dict[str, object], int]] = []
                mutations: list[bool] = []
                handler.headers[header] = value
                handler.handle_rebuild = lambda: mutations.append(True)
                handler.send_json = lambda payload, response_status=200: sent.append((payload, response_status))

                handler.do_POST()

                self.assertEqual(mutations, [])
                self.assertEqual(sent, [({"error": error}, status)])

    def test_post_requires_origin_even_without_sec_fetch_site(self) -> None:
        serve = load_module("serve_dashboard_post_origin_required_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = self.secure_post_handler(serve)
        sent: list[tuple[dict[str, object], int]] = []
        mutations: list[bool] = []
        del handler.headers["Origin"]
        del handler.headers["Sec-Fetch-Site"]
        handler.handle_rebuild = lambda: mutations.append(True)
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.do_POST()

        self.assertEqual(mutations, [])
        self.assertEqual(sent, [({"error": "request_origin_forbidden"}, 403)])

    def test_post_accepts_json_charset_and_missing_sec_fetch_site(self) -> None:
        serve = load_module("serve_dashboard_post_json_charset_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = self.secure_post_handler(serve)
        sent: list[tuple[dict[str, object], int]] = []
        mutations: list[bool] = []
        del handler.headers["Sec-Fetch-Site"]
        handler.headers["Content-Type"] = "application/json; charset=UTF-8"
        handler.handle_rebuild = lambda: mutations.append(True)
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.do_POST()

        self.assertEqual(mutations, [True])
        self.assertEqual(sent, [])

    def test_post_rejects_oversized_body_before_read_or_mutation(self) -> None:
        serve = load_module("serve_dashboard_post_body_limit_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = self.secure_post_handler(serve)
        sent: list[tuple[dict[str, object], int]] = []
        mutations: list[bool] = []
        handler.headers["Content-Length"] = str(serve.MAX_JSON_BODY_BYTES + 1)
        handler.rfile = None
        handler.handle_rebuild = lambda: mutations.append(True)
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.do_POST()

        self.assertEqual(mutations, [])
        self.assertEqual(sent, [({"error": "request_body_too_large"}, 413)])

    def test_post_rejects_missing_and_invalid_content_length_before_mutation(self) -> None:
        serve = load_module("serve_dashboard_post_content_length_test", ROOT / "scripts" / "serve_dashboard.py")
        cases = (
            (None, "content_length_required", 411),
            ("not-a-number", "invalid_content_length", 400),
            ("-1", "invalid_content_length", 400),
        )

        for length, error, status in cases:
            with self.subTest(length=length):
                handler = self.secure_post_handler(serve)
                sent: list[tuple[dict[str, object], int]] = []
                mutations: list[bool] = []
                if length is None:
                    del handler.headers["Content-Length"]
                else:
                    handler.headers["Content-Length"] = length
                handler.handle_rebuild = lambda: mutations.append(True)
                handler.send_json = lambda payload, response_status=200: sent.append((payload, response_status))

                handler.do_POST()

                self.assertEqual(mutations, [])
                self.assertEqual(sent, [({"error": error}, status)])

    def test_dashboard_post_helper_always_sends_json_object(self) -> None:
        source = (ROOT / "scripts" / "assets" / "dashboard" / "api.js").read_text(encoding="utf-8")
        self.assertIn("headers: { 'Content-Type': 'application/json' }", source)
        self.assertIn("body: JSON.stringify(body === null ? {} : body)", source)

    def test_root_dashboard_html_is_not_cached(self) -> None:
        serve = load_module("serve_dashboard_root_cache_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        headers: list[tuple[str, str]] = []
        handler.server = types.SimpleNamespace(allowed_authority="127.0.0.1:8766")
        handler.headers = {"Host": "127.0.0.1:8766"}
        handler.path = "/"
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: headers.append((name, value))
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()

        handler.do_GET()

        self.assertIn(("Cache-Control", "no-cache"), headers)

    def test_get_rejects_unexpected_host_before_routing(self) -> None:
        serve = load_module("serve_dashboard_get_host_test", ROOT / "scripts" / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        sent: list[tuple[dict[str, object], int]] = []
        handler.server = types.SimpleNamespace(allowed_authority="127.0.0.1:8766")
        handler.headers = {"Host": "localhost:8766"}
        handler.path = "/api/dashboard"
        handler.handle_api = lambda *_args: (_ for _ in ()).throw(AssertionError("route must not run"))
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.do_GET()

        self.assertEqual(sent, [({"error": "request_host_forbidden"}, 403)])
    def test_dashboard_payload_can_focus_a_specific_turn(self) -> None:
        queries = load_module("dashboard_queries_focus_turn_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            payload = queries.DashboardQueries(
                con,
                {
                    "days": ["0"],
                    "limit": ["0"],
                    "focus_session_id": ["s2"],
                    "focus_turn_id": ["t2"],
                },
            ).dashboard_payload()
            con.close()

        self.assertEqual(payload["turns"]["total"], 1)
        self.assertEqual(payload["turns"]["rows"][0]["session_id"], "s2")
        self.assertEqual(payload["turns"]["rows"][0]["turn_id"], "t2")
        self.assertTrue(payload["turns"]["focused"])
        self.assertEqual(payload["summary"]["turns"], 1)
        self.assertEqual(payload["summary"]["total_tokens"], 900)
        self.assertEqual(payload["summary"]["weighted_credits"], 9.0)
        self.assertEqual(payload["summary"]["model_calls"], 2)

    def test_dashboard_focus_turn_keeps_summary_in_scope(self) -> None:
        queries = load_module("dashboard_queries_focus_scope_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            payload = queries.DashboardQueries(
                con,
                {
                    "days": ["0"],
                    "session_id": ["s1"],
                    "focus_session_id": ["s2"],
                    "focus_turn_id": ["t2"],
                },
            ).dashboard_payload()
            con.close()

        self.assertTrue(payload["turns"]["focused"])
        self.assertEqual(payload["turns"]["total"], 0)
        self.assertEqual(payload["turns"]["rows"], [])
        self.assertEqual(payload["summary"]["turns"], 0)

    def test_empty_dashboard_summaries_return_zero_numbers(self) -> None:
        queries = load_module("dashboard_queries_empty_summary_zero_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            dashboard = queries.DashboardQueries(con, {"days": ["0"], "session_id": ["missing"]}).dashboard_payload()
            summary = queries.DashboardQueries(con, {"days": ["0"], "session_id": ["missing"]}).summary_payload()
            session_detail = queries.DashboardQueries(con, {"days": ["0"], "selected_session_id": ["missing"]}).session_detail_payload()
            con.close()

        for payload in (dashboard["summary"], summary):
            self.assertEqual(payload["turns"], 0)
            self.assertEqual(payload["total_tokens"], 0)
            self.assertEqual(payload["input_tokens"], 0)
            self.assertEqual(payload["cached_input_tokens"], 0)
            self.assertEqual(payload["non_cached_input_tokens"], 0)
            self.assertEqual(payload["output_tokens"], 0)
            self.assertEqual(payload["reasoning_output_tokens"], 0)
            self.assertEqual(payload["model_calls"], 0)
            self.assertEqual(payload["tool_calls"], 0)
            self.assertEqual(payload["weighted_credits"], 0)
            self.assertEqual(payload["cached_ratio"], 0)

        for detail in (session_detail["summary"],):
            self.assertEqual(detail["turns"], 0)
            self.assertEqual(detail["raw"], 0)
            self.assertEqual(detail["credits"], 0)
            self.assertEqual(detail["model_calls"], 0)
            self.assertEqual(detail["non_cached_input_tokens"], 0)
            self.assertEqual(detail["cached_ratio"], 0)

    def test_session_detail_rollups_preserve_unpriced_costs(self) -> None:
        queries = load_module("dashboard_queries_session_detail_null_rollup_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.execute("update turns set total_tokens=null, weighted_credits=null where session_id='s2'")
            con.execute(
                """
                insert into tool_call_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("s2", "t2", "null-tool", "shell", None, None, None, None, 0, None, None, None),
            )
            con.commit()
            con.row_factory = sqlite3.Row
            detail = queries.DashboardQueries(con, {"days": ["0"], "selected_session_id": ["s2"]}).session_detail_payload()
            con.close()

        self.assertEqual(detail["workflows"][0]["raw"], 0)
        self.assertIsNone(detail["workflows"][0]["credits"])
        null_tool = next(row for row in detail["tools"] if row["tool_name"] == "null-tool")
        self.assertEqual(null_tool["calls"], 0)
        self.assertEqual(null_tool["output_tokens"], 0)

    def test_turn_payload_exposes_summaries_without_legacy_detail_arrays(self) -> None:
        queries = load_module("dashboard_queries_turn_contract_test", ROOT / "scripts" / "dashboard_queries.py")
        fixture = load_module("dashboard_fixture_data_turn_contract_test", ROOT / "scripts" / "dashboard_fixture_data.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = pathlib.Path(tmp_dir) / "codex-dir"
            db_path = fixture.write_dashboard_fixture(codex_dir)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row

            payload = queries.DashboardQueries(
                con,
                {
                    "days": ["0"],
                    "session_id": ["11111111-2222-3333-4444-555555555555"],
                    "turn_id": ["turn-00"],
                },
            ).payload("/api/turn")
            con.close()

        self.assertIn("model_call_summary", payload)
        self.assertIn("tool_call_summary", payload)
        self.assertIn("model_call_total", payload)
        self.assertIn("tool_call_total", payload)
        self.assertNotIn("model_calls", payload)
        self.assertNotIn("tool_calls", payload)
        self.assertNotIn("limited", payload)

    def test_dashboard_lite_payload_defers_heavy_rollup_lists(self) -> None:
        queries = load_module("dashboard_queries_lite_payload_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row

            lite = queries.DashboardQueries(con, {"days": ["0"], "lite": ["1"], "page": ["1"], "per_page": ["2"]}).payload("/api/dashboard")
            full = queries.DashboardQueries(con, {"days": ["0"], "page": ["1"], "per_page": ["2"]}).payload("/api/dashboard")
            sessions = queries.DashboardQueries(con, {"days": ["0"], "sessions_page": ["1"], "per_page": ["1"]}).payload("/api/sessions")
            tools = queries.DashboardQueries(con, {"days": ["0"], "tools_page": ["1"], "per_page": ["1"]}).payload("/api/tools")
            con.close()

        self.assertEqual(lite["summary"]["turns"], 2)
        self.assertEqual(len(lite["turns"]["rows"]), 2)
        self.assertEqual(lite["projects"]["rows"], [])
        self.assertEqual(lite["sessions"]["rows"], [])
        self.assertEqual(lite["tools"]["rows"], [])
        self.assertEqual([row["rows"] for row in lite["subagents"]["rows"]], [0, 0, 0, 0, 0])
        self.assertEqual([row["session_id"] for row in full["sessions"]["rows"]], ["s2", "s1"])
        self.assertEqual([row["session_id"] for row in sessions["rows"]], ["s2"])
        self.assertEqual(sessions["total"], 2)
        self.assertEqual(sessions["page"], 1)
        self.assertEqual(sessions["per_page"], 1)
        self.assertEqual(len(tools["rows"]), 1)
        self.assertEqual(tools["total"], 1)
        self.assertGreater(tools["output_tokens_total"], 0)

    def test_tools_payload_materializes_selected_scope_once(self) -> None:
        queries = load_module("dashboard_queries_tools_scope_once_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            query_builder = queries.DashboardQueries(con, {"days": ["0"], "tools_page": ["1"], "per_page": ["25"]})

            with mock.patch.object(query_builder, "create_selected_turns_temp", wraps=query_builder.create_selected_turns_temp) as create_scope:
                payload = query_builder.tools_payload()
            con.close()

        self.assertEqual(create_scope.call_count, 1)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["rows"][0]["tool_name"], "exec_command")

    def test_tool_payload_materializes_selected_scope_once(self) -> None:
        queries = load_module("dashboard_queries_tool_scope_once_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            con.executemany(
                "insert into tool_call_samples values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("s2", "t2", f"largest-{index}", "exec_command", "exec", "largest_output", 1, None, None, 20 + index, 400 + index, 0, 1000 + index, "completed", 0)
                    for index in range(12)
                ],
            )
            con.commit()
            query_builder = queries.DashboardQueries(con, {"days": ["0"], "tool_name": ["exec_command"]})

            with mock.patch.object(query_builder, "create_selected_turns_temp", wraps=query_builder.create_selected_turns_temp) as create_scope:
                payload = query_builder.tool_payload()
            temp_tables = {row[0] for row in con.execute("select name from sqlite_temp_master where type='table'")}
            session_plan = "\n".join(
                str(tuple(row))
                for row in con.execute(
                    """
                    explain query plan
                    select session_id, thread_name, cwd, calls, output_chars, reported_tokens, output_tokens
                    from selected_tool_detail_sessions
                    order by output_tokens desc, session_id desc
                    limit 12
                    """
                )
            )
            con.close()

        self.assertEqual(create_scope.call_count, 1)
        self.assertIn("selected_tool_detail_sessions", temp_tables)
        self.assertIn("idx_selected_tool_detail_sessions_output", session_plan)
        self.assertNotIn("USE TEMP B-TREE", session_plan)
        self.assertEqual(payload["summary"]["tool_name"], "exec_command")
        self.assertEqual(payload["summary"]["calls"], 2)
        self.assertEqual(payload["summary"]["output_tokens"], 110)
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["s2", "s1"])
        self.assertEqual(payload["sessions"][0]["project"], "beta")
        self.assertEqual(len(payload["calls"]), 10)
        self.assertTrue(all(row["project"] == "beta" for row in payload["calls"]))
        self.assertEqual([row["output_tokens"] for row in payload["calls"]], list(range(1011, 1001, -1)))

    def test_tool_payload_returns_zero_for_null_numeric_sums(self) -> None:
        queries = load_module("dashboard_queries_tool_null_sums_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.execute(
                """
                insert into tool_call_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("s2", "t2", "null-tool", "exec", None, None, None, None, 0, None, None, None),
            )
            con.commit()
            con.row_factory = sqlite3.Row

            dashboard = queries.DashboardQueries(con, {"days": ["0"]}).dashboard_payload()
            payload = queries.DashboardQueries(con, {"days": ["0"], "tool_name": ["null-tool"]}).tool_payload()
            con.close()

        dashboard_row = next(row for row in dashboard["tools"]["rows"] if row["tool_name"] == "null-tool")
        self.assertEqual(dashboard_row["calls"], 0)
        self.assertEqual(dashboard_row["output_chars"], 0)
        self.assertEqual(dashboard_row["reported_tokens"], 0)
        self.assertEqual(dashboard_row["output_tokens"], 0)
        self.assertEqual(payload["summary"]["tool_name"], "null-tool")
        self.assertEqual(payload["summary"]["calls"], 0)
        self.assertEqual(payload["summary"]["output_chars"], 0)
        self.assertEqual(payload["summary"]["reported_tokens"], 0)
        self.assertEqual(payload["summary"]["output_tokens"], 0)
        self.assertEqual(payload["summary"]["avg_output_chars"], 0)
        self.assertEqual(payload["summary"]["avg_output_tokens"], 0)
        self.assertEqual(payload["summary"]["avg_duration_ms"], 0)
        self.assertEqual(payload["sessions"][0]["calls"], 0)
        self.assertEqual(payload["sessions"][0]["output_chars"], 0)
        self.assertEqual(payload["sessions"][0]["reported_tokens"], 0)
        self.assertEqual(payload["sessions"][0]["output_tokens"], 0)

    def test_subagents_payload_keeps_all_confidence_methods(self) -> None:
        queries = load_module("dashboard_queries_subagents_complete_rows_test", ROOT / "scripts" / "dashboard_queries.py")
        fixture = load_module("dashboard_fixture_data_subagents_complete_rows_test", ROOT / "scripts" / "dashboard_fixture_data.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = fixture.write_dashboard_fixture(pathlib.Path(tmp_dir))
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row

            payload = queries.DashboardQueries(con, {"days": ["0"]}).subagents_payload()
            con.close()

        self.assertEqual(
            [row["confidence"] for row in payload["rows"]],
            [
                "child_task_time_overlap",
                "orphan",
                "parent_pruned_by_retention",
                "spawn_call_turn_context",
                "spawn_edge_nearest_parent_turn",
            ],
        )
        self.assertEqual(len(payload["rows"]), 5)
        self.assertEqual(next(row for row in payload["rows"] if row["confidence"] == "child_task_time_overlap")["rows"], 1)

    def test_dashboard_ignores_removed_analysis_limit_percent(self) -> None:
        queries = load_module("dashboard_queries_removed_limit_percent_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.executemany(
                """
                insert into turns values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("s3", "t3", 3, "2026-01-01T00:00:03+00:00", "/example/src/gamma", "gamma", "", "medium prompt", "completed", 4.0, 400, 300, 200, 100, 100, 0, 1),
                    ("s4", "t4", 4, "2026-01-01T00:00:04+00:00", "/example/src/delta", "delta", "", "tiny prompt", "completed", 0.5, 50, 40, 20, 20, 10, 0, 1),
                ],
            )
            con.commit()
            con.row_factory = sqlite3.Row

            pct_50 = queries.DashboardQueries(con, {"days": ["0"], "limit_percent": ["50"]}).dashboard_payload()
            pct_25 = queries.DashboardQueries(con, {"days": ["0"], "limit_percent": ["25"]}).dashboard_payload()
            pct_100 = queries.DashboardQueries(con, {"days": ["0"], "limit_percent": ["100"]}).dashboard_payload()
            con.close()

        self.assertEqual(pct_50["summary"]["turns"], 4)
        self.assertEqual(pct_50["summary"]["weighted_credits"], 14.5)
        self.assertEqual(pct_50["summary"]["tool_calls"], 2)
        self.assertEqual(pct_25["summary"], pct_50["summary"])
        self.assertEqual(pct_100["summary"], pct_50["summary"])

    def test_dashboard_first_column_lists_are_not_fixed_to_twenty_rows(self) -> None:
        queries = load_module("dashboard_queries_first_column_limit_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            extra_turns = []
            extra_tools = []
            for index in range(3, 28):
                session_id = f"s{index}"
                turn_id = f"t{index}"
                extra_turns.append(
                    (
                        session_id,
                        turn_id,
                        index,
                        f"2026-01-01T00:00:{index:02d}+00:00",
                        f"/example/src/session-{index}",
                        "many",
                        f"thread {index}",
                        f"prompt {index}",
                        "completed",
                        float(index),
                        index * 100,
                        index * 80,
                        index * 50,
                        index * 30,
                        index * 20,
                        0,
                        1,
                    )
                )
                extra_tools.append(
                    (
                        session_id,
                        turn_id,
                        f"tool_{index:02d}",
                        "exec",
                        1,
                        index * 10,
                        0,
                        index * 10,
                        0,
                        10,
                        10,
                        index * 10,
                    )
                )
            con.executemany("insert into turns values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", extra_turns)
            con.executemany("insert into tool_call_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", extra_tools)
            con.commit()
            con.row_factory = sqlite3.Row
            payload = queries.DashboardQueries(
                con,
                {
                    "days": ["0"],
                    "per_page": ["10"],
                    "sessions_page": ["2"],
                    "tools_page": ["2"],
                },
            ).dashboard_payload()
            con.close()

        self.assertEqual(len(payload["sessions"]["rows"]), 10)
        self.assertGreater(payload["sessions"]["total"], 20)
        self.assertEqual(payload["sessions"]["page"], 2)
        self.assertEqual(payload["sessions"]["per_page"], 10)
        self.assertEqual(len(payload["tools"]["rows"]), 10)
        self.assertGreater(payload["tools"]["total"], 20)
        self.assertEqual(payload["tools"]["page"], 2)
        self.assertEqual(payload["tools"]["per_page"], 10)
        self.assertGreater(payload["tools"]["output_tokens_total"], 0)

    def test_rollup_payloads_apply_column_sort_parameters_before_pagination(self) -> None:
        queries = load_module("dashboard_queries_rollup_sort_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            con.execute(
                "insert into tool_call_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("s1", "t1", "apply_patch", "functions", 5, 300, 0, 50, 0, 25, 25, 50),
            )
            con.execute(
                "insert into tool_call_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("s2", "t2", "view_image", "functions", 2, 900, 0, 150, 0, 30, 30, 150),
            )
            con.commit()

            sessions_by_raw = queries.DashboardQueries(
                con,
                {"days": ["0"], "session_sort": ["raw"], "session_sort_dir": ["asc"], "sessions_page": ["1"], "per_page": ["1"]},
            ).sessions_payload()
            sessions_by_name = queries.DashboardQueries(
                con,
                {"days": ["0"], "session_label_mode": ["project"], "session_sort": ["session"], "session_sort_dir": ["asc"], "sessions_page": ["1"], "per_page": ["2"]},
            ).sessions_payload()
            sessions_by_thread = queries.DashboardQueries(
                con,
                {"days": ["0"], "session_label_mode": ["thread"], "session_sort": ["session"], "session_sort_dir": ["asc"], "sessions_page": ["1"], "per_page": ["2"]},
            ).sessions_payload()
            tools_by_calls = queries.DashboardQueries(
                con,
                {"days": ["0"], "tool_sort": ["calls"], "tool_sort_dir": ["desc"], "tools_page": ["1"], "per_page": ["3"]},
            ).tools_payload()
            tools_by_share = queries.DashboardQueries(
                con,
                {"days": ["0"], "tool_sort": ["share"], "tool_sort_dir": ["asc"], "tools_page": ["1"], "per_page": ["3"]},
            ).tools_payload()
            con.close()

        self.assertEqual([row["session_id"] for row in sessions_by_raw["rows"]], ["s1"])
        self.assertEqual(sessions_by_raw["total"], 2)
        self.assertEqual([row["session_id"] for row in sessions_by_name["rows"]], ["s1", "s2"])
        self.assertEqual([row["session_id"] for row in sessions_by_thread["rows"]], ["s2", "s1"])
        self.assertEqual([row["tool_name"] for row in tools_by_calls["rows"]], ["apply_patch", "exec_command", "view_image"])
        self.assertEqual([row["tool_name"] for row in tools_by_share["rows"]], ["apply_patch", "exec_command", "view_image"])

    def test_subagent_payload_applies_column_sort_after_completing_methods(self) -> None:
        queries = load_module("dashboard_queries_subagent_sort_test", ROOT / "scripts" / "dashboard_queries.py")
        fixture = load_module("dashboard_fixture_data_subagent_sort_test", ROOT / "scripts" / "dashboard_fixture_data.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = fixture.write_dashboard_fixture(pathlib.Path(tmp_dir))
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row

            default_rows = queries.DashboardQueries(con, {"days": ["0"]}).subagents_payload()["rows"]
            confidence_rows = queries.DashboardQueries(
                con,
                {"days": ["0"], "subagent_sort": ["confidence"], "subagent_sort_dir": ["asc"]},
            ).subagents_payload()["rows"]
            credits_asc = queries.DashboardQueries(
                con,
                {"days": ["0"], "subagent_sort": ["child_credits"], "subagent_sort_dir": ["asc"]},
            ).subagents_payload()["rows"]
            con.close()

        self.assertEqual(default_rows[0]["confidence"], "child_task_time_overlap")
        self.assertEqual(confidence_rows[0]["confidence"], "child_task_time_overlap")
        self.assertEqual(confidence_rows[-1]["confidence"], "spawn_edge_nearest_parent_turn")
        self.assertEqual(credits_asc[0]["child_credits"], 0.0)
        self.assertEqual(credits_asc[-1]["confidence"], "child_task_time_overlap")

    def test_analysis_scope_percent_control_is_removed(self) -> None:
        self.assertNotIn('id="rows"', DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("Analysis rollup scope", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("Top 10%", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("Top 25%", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("custom-percent", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("limit_percent", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("analysisPercentValue", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("appliedRowsMode", DASHBOARD_ASSET_BUNDLE)
    def test_overview_uses_real_session_rows_not_inferred_projects(self) -> None:
        queries = load_module("dashboard_queries_overview_session_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            payload = queries.DashboardQueries(con, {"days": ["0"]}).dashboard_payload()
            detail = queries.DashboardQueries(con, {"days": ["0"], "selected_session_id": ["s2"]}).session_detail_payload()
            con.close()

        self.assertEqual([row["session_id"] for row in payload["sessions"]["rows"]], ["s2", "s1"])
        self.assertEqual(payload["sessions"]["rows"][0]["thread_name"], "")
        self.assertEqual(payload["sessions"]["rows"][0]["cwd"], "/example/.codex/codex-token-bola")
        self.assertEqual(payload["sessions"]["rows"][0]["project"], "beta")
        self.assertEqual(payload["sessions"]["rows"][1]["thread_name"], "zulu")
        self.assertEqual(payload["turns"]["rows"][0]["cwd"], "/example/.codex/codex-token-bola")
        self.assertEqual(detail["summary"]["session_id"], "s2")
        self.assertEqual(detail["summary"]["thread_name"], "")
        self.assertEqual(detail["summary"]["project"], "beta")
        self.assertEqual(detail["summary"]["cwd"], "/example/.codex/codex-token-bola")
        self.assertEqual(detail["summary"]["turns"], 1)
        self.assertIn("<h2>Overview</h2>", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("<h2>Session Detail</h2>", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("<h2>Sessions</h2>", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("<h2>Session Cost</h2>", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("<h2>Project Cost</h2>", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("<h2>Project Detail</h2>", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("q.set('lite', '1');", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("const { summary, turns } = dashboard;", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("async function loadOverviewData(seq = state.requestSeq, page = state.listPages.projects || 1, busy = false)", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("const path = sessionsPath(page);", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("async function loadToolsData(seq = state.requestSeq, page = state.listPages.tools || 1, busy = false)", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("const path = toolsPath(page);", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("async function loadSubagentData(seq = state.requestSeq)", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("const subagents = await cachedValue(subagentsPath());", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("loadVisibleRollupData(seq);", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("return prepareDetail(key, sessionDetailPath(key));", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("/api/project-detail", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("project_detail_payload", (ROOT / "scripts" / "dashboard_queries.py").read_text(encoding="utf-8"))
        self.assertIn("{label:'Session', sort:'session'}, {label:'Cost Units', sort:'credits', cls:'num'}, {label:'Total Tokens', sort:'raw', cls:'num'}, {label:'Turns', sort:'turns', cls:'num'}", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn("data-project-key", DASHBOARD_ASSET_BUNDLE)
    def test_session_filter_payload_and_options(self) -> None:
        queries = load_module("dashboard_queries_session_filter_payload_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            filtered = queries.DashboardQueries(con, {"days": ["0"], "session_id": ["s1"], "page": ["1"], "per_page": ["10"]}).turns_payload()
            options = queries.DashboardQueries(con, {}).session_options_payload()
            con.close()

        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["rows"][0]["session_id"], "s1")
        self.assertEqual([row["session_id"] for row in options["rows"]], ["s2", "s1"])
        self.assertEqual(options["rows"][1]["thread_name"], "zulu")
        self.assertEqual(options["rows"][1]["project"], "alpha")
        self.assertEqual(options["limit"], 50)
        self.assertFalse(options["has_more"])
    def test_session_options_are_server_filtered_and_limited(self) -> None:
        queries = load_module("dashboard_queries_session_options_search_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            self._write_dashboard_fixture(db_path)
            con = sqlite3.connect(db_path)
            con.executemany(
                "insert into turns values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("s3", "t3", 3, "2026-01-01T00:00:03+00:00", "/example/src/alpha", "alpha", "quant", "prompt", "completed", 3.0, 300, 200, 100, 100, 100, 0, 1),
                    ("s4", "t4", 4, "2026-01-01T00:00:04+00:00", "/example/src/beta", "beta", "research", "prompt", "completed", 4.0, 400, 300, 200, 100, 100, 0, 1),
                    ("s5", "t5", 5, "2026-01-01T00:00:05+00:00", "/example/src/quant-tools", "quant-tools", "", "prompt", "completed", 5.0, 500, 400, 300, 100, 100, 0, 1),
                ],
            )
            con.commit()
            con.row_factory = sqlite3.Row
            limited = queries.DashboardQueries(con, {"limit": ["2"]}).session_options_payload()
            searched = queries.DashboardQueries(con, {"q": ["quant"], "limit": ["50"]}).session_options_payload()
            con.close()

        self.assertEqual([row["session_id"] for row in limited["rows"]], ["s5", "s4"])
        self.assertEqual(limited["limit"], 2)
        self.assertTrue(limited["has_more"])
        self.assertEqual({row["session_id"] for row in searched["rows"]}, {"s3", "s5"})
        self.assertFalse(searched["has_more"])
    def test_subagent_detail_includes_rollups_whose_parent_turn_was_pruned(self) -> None:
        queries = load_module("dashboard_queries_subagent_pruned_detail_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            con.executescript(
                """
                create table turns (
                  session_id text,
                  turn_id text,
                  captured_at_unix real,
                  cwd text,
                  project text,
                  thread_name text,
                  prompt_preview text,
                  weighted_credits real,
                  started_at_unix real generated always as (captured_at_unix) virtual
                );
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
                """
            )
            con.execute("insert into turns values (?, ?, ?, ?, ?, ?, ?, ?)", ("child", "child-turn", 10.0, "/example/.codex/codex-token-bola", "alpha", "child thread", "child prompt", 2.0))
            con.execute(
                "insert into task_rollups values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "parent",
                    "pruned-parent",
                    "child",
                    "reviewer",
                    "r1",
                    "2026-01-10T00:00:00",
                    10.0,
                    "parent_pruned_by_retention",
                    0,
                    200,
                    200,
                    0.0,
                    2.0,
                    2.0,
                ),
            )
            con.commit()

            payload = queries.DashboardQueries(
                con,
                {"days": ["0"], "limit": ["0"], "confidence": ["parent_pruned_by_retention"]},
            ).subagent_payload()
            con.close()

        self.assertEqual(payload["summary"]["rows"], 1)
        self.assertEqual(payload["sessions"][0]["session_id"], "parent")
        self.assertEqual(payload["sessions"][0]["thread_name"], "")
        self.assertEqual(payload["sessions"][0]["cwd"], "/example/.codex/codex-token-bola")
        self.assertEqual(payload["sessions"][0]["project"], "alpha")
        self.assertEqual(payload["rows"][0]["parent_turn_id"], "pruned-parent")
        self.assertEqual(payload["rows"][0]["session_id"], "parent")
        self.assertEqual(payload["rows"][0]["thread_name"], "")
        self.assertEqual(payload["rows"][0]["cwd"], "/example/.codex/codex-token-bola")
        self.assertEqual(payload["rows"][0]["project"], "alpha")
        self.assertEqual(payload["rows"][0]["prompt_preview"], "")

    def test_subagent_payload_uses_full_filtered_scope(self) -> None:
        queries = load_module("dashboard_queries_subagent_full_scope_test", ROOT / "scripts" / "dashboard_queries.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = pathlib.Path(tmp_dir) / "analytics.sqlite"
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            con.executescript(
                """
                create table turns (
                  session_id text,
                  turn_id text,
                  captured_at_unix real,
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
                  model_call_count integer,
                  started_at_unix real generated always as (captured_at_unix) virtual,
                  started_at text generated always as (captured_at) virtual
                );
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
                """
            )
            con.executemany(
                "insert into turns values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("selected", "turn", 2.0, "2026-01-01T00:00:02+00:00", "/tmp", "p", "", "selected", "completed", 100.0, 100, 80, 0, 80, 20, 0, 1),
                    ("excluded", "turn", 1.0, "2026-01-01T00:00:01+00:00", "/tmp", "p", "", "excluded", "completed", 1.0, 100, 80, 0, 80, 20, 0, 1),
                ],
            )
            con.executemany(
                "insert into task_rollups values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("selected", "turn", "child-a", "reviewer", "a", "2026-01-01T00:00:03+00:00", 3.0, "child_task_time_overlap", 0, 10, 10, 0.0, 10.0, 10.0),
                    ("excluded", "turn", "child-b", "reviewer", "b", "2026-01-01T00:00:04+00:00", 4.0, "child_task_time_overlap", 0, 1000, 1000, 0.0, 1000.0, 1000.0),
                ],
            )
            con.commit()

            dashboard = queries.DashboardQueries(con, {"days": ["0"], "limit": ["1"]}).dashboard_payload()
            subagents = queries.DashboardQueries(con, {"days": ["0"], "limit": ["1"]}).subagents_payload()
            con.close()

        dashboard_row = next(row for row in dashboard["subagents"]["rows"] if row["confidence"] == "child_task_time_overlap")
        subagent_row = next(row for row in subagents["rows"] if row["confidence"] == "child_task_time_overlap")
        self.assertEqual(dashboard_row["child_credits"], 1010.0)
        self.assertEqual(subagent_row["child_credits"], 1010.0)

    def test_tool_detail_selected_tool_includes_description(self) -> None:
        self.assertIn("function toolDescription(value)", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("exec_command: 'shell command execution output captured from terminal runs'", DASHBOARD_ASSET_BUNDLE)
        self.assertIn("toolDisplay(toolName)", DASHBOARD_ASSET_BUNDLE)
        self.assertIn('<span class="method-name">${esc(toolName)}</span><span class="method-desc">${esc(toolDescription(toolName))}</span>', DASHBOARD_ASSET_BUNDLE)
        self.assertIn(".tool-name-cell .value.attribution-method-value {\n      display: grid;\n      gap: 4px;", DASHBOARD_ASSET_BUNDLE)
        self.assertIn(".attribution-method-value .method-name {\n      display: block;", DASHBOARD_ASSET_BUNDLE)
        self.assertIn(".attribution-method-value .method-desc {\n      display: block;", DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn('method-desc"> - ', DASHBOARD_ASSET_BUNDLE)
        self.assertNotIn('<div class="label">Selected tool</div>', DASHBOARD_ASSET_BUNDLE)
    def test_analyze_endpoint_runs_incremental_pipeline(self) -> None:
        serve_source = (ROOT / "scripts" / "serve_dashboard.py").read_text(encoding="utf-8")
        rebuild_source = (ROOT / "scripts" / "dashboard_rebuild_api.py").read_text(encoding="utf-8")
        cleanup_source = (ROOT / "scripts" / "dashboard_cleanup_api.py").read_text(encoding="utf-8")
        state_source = (ROOT / "scripts" / "dashboard_operation_state.py").read_text(encoding="utf-8")
        self.assertIn('"--incremental",', rebuild_source)
        self.assertIn('"--recover",', rebuild_source)
        self.assertIn('if parsed.path == "/api/rebuild/cancel":', serve_source)
        self.assertIn('env["BOLA_PROGRESS_FILE"] = str(progress_file)', rebuild_source)
        self.assertIn('if path == "/api/rebuild/progress":', serve_source)
        self.assertIn("def handle_rebuild_progress(self):", rebuild_source)
        self.assertIn('if path == "/api/log-cleanup/progress":', serve_source)
        self.assertIn("def handle_cleanup_progress(self):", cleanup_source)
        self.assertIn('env[progress_control.PROGRESS_ENV] = str(progress_file)', cleanup_source)
        self.assertIn("class DashboardOperationManager", state_source)
        self.assertIn("operation_id", state_source)
        self.assertIn("ManagedProcess.start", rebuild_source)
        self.assertIn('metadata["analysis_elapsed_ms"] = metadata.pop("elapsed_ms")', rebuild_source)
        self.assertIn("AUTO_COMPACT_MIN_BYTES = 64 * 1024 * 1024", rebuild_source)
        self.assertIn('metadata["pre_analysis_rotate"]', rebuild_source)
        self.assertNotIn("self.run_compact_command(output, AUTO_COMPACT_MIN_BYTES)", rebuild_source)
        self.assertIn("dashboard_cleanup.refresh_retention_index_for_current_sources(self.dashboard_output_dir())", rebuild_source)
        self.assertIn('degraded = process.returncode == 1 and metadata.get("status") == "degraded"', rebuild_source)
        self.assertIn('"data_health": "degraded" if degraded else "ok"', rebuild_source)
        self.assertIn('"ok": True,', rebuild_source)
        for operation_source in (rebuild_source, cleanup_source, state_source):
            self.assertNotIn("import serve_dashboard", operation_source)
            self.assertNotIn("from serve_dashboard", operation_source)
