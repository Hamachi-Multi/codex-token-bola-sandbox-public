from __future__ import annotations

import contextlib

try:
    from tests.support import ROOT, _turn_raw, io, json, load_module, mock, pathlib, tempfile, types, unittest
except ModuleNotFoundError:
    from support import ROOT, _turn_raw, io, json, load_module, mock, pathlib, tempfile, types, unittest


class QuarantineHealthTests(unittest.TestCase):
    def test_event_is_deduplicated_and_acknowledgement_survives_repeat(self) -> None:
        health = load_module("quarantine_health_registry_test", ROOT / "scripts" / "quarantine_health.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            evidence = base / "bad" / "prompt-usage.bad.jsonl"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("", encoding="utf-8")
            event = health.event_id(kind="normalize_raw", source="segment.jsonl", content="{", error="JSONDecodeError('bad')")
            first = health.record_event(
                base,
                event=event,
                kind="normalize_raw",
                source="segment.jsonl",
                error="JSONDecodeError('bad')",
                evidence_path=evidence,
                captured_at_ns=1,
                line_no=3,
            )
            second = health.record_event(
                base,
                event=event,
                kind="normalize_raw",
                source="segment.jsonl",
                error="JSONDecodeError('bad')",
                evidence_path=evidence,
                captured_at_ns=2,
                line_no=3,
            )
            acknowledged = health.acknowledge(base, event_ids=[event])
            repeated = health.record_event(
                base,
                event=event,
                kind="normalize_raw",
                source="segment.jsonl",
                error="JSONDecodeError('bad')",
                evidence_path=evidence,
                captured_at_ns=3,
                line_no=3,
            )
            report = health.summary(base, include_entries=True)

        self.assertTrue(first["new_event"])
        self.assertFalse(second["new_event"])
        self.assertEqual(acknowledged["acknowledged_events"], 1)
        self.assertTrue(repeated["acknowledged"])
        self.assertEqual(report["unacknowledged_events"], 0)
        self.assertEqual(report["events"][0]["occurrences"], 3)

    def test_legacy_bad_log_is_visible_and_acknowledgeable(self) -> None:
        health = load_module("quarantine_health_legacy_test", ROOT / "scripts" / "quarantine_health.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            bad_log = base / "bad" / "prompt-usage.bad.jsonl"
            bad_log.parent.mkdir(parents=True)
            bad_log.write_text(
                json.dumps(
                    {
                        "captured_at_ns": 10,
                        "source": "/data/prompt-usage.raw.jsonl",
                        "line_no": 4,
                        "error": "JSONDecodeError('bad')",
                        "line": "{",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            before = health.summary(base, include_entries=True)
            result = health.acknowledge(base, acknowledge_all=True)
            after = health.summary(base)

        self.assertEqual(before["unacknowledged_events"], 1)
        self.assertEqual(result["acknowledged_events"], 1)
        self.assertEqual(after["unacknowledged_events"], 0)
        self.assertEqual(after["acknowledged_events"], 1)

    def test_corrupt_registry_fails_closed(self) -> None:
        health = load_module("quarantine_health_corrupt_test", ROOT / "scripts" / "quarantine_health.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            path = health.state_path(base)
            path.parent.mkdir(parents=True)
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(health.QuarantineError):
                health.summary(base)

    def test_evidence_symlink_fails_closed_without_reading_target(self) -> None:
        health = load_module("quarantine_health_symlink_test", ROOT / "scripts" / "quarantine_health.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            outside = base / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            bad = base / "bad"
            bad.mkdir()
            (bad / "linked.json").symlink_to(outside)
            with self.assertRaises(health.QuarantineError):
                health.summary(base)

    def test_unknown_acknowledgement_id_does_not_partially_mutate_state(self) -> None:
        health = load_module("quarantine_health_atomic_ack_test", ROOT / "scripts" / "quarantine_health.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            evidence = base / "bad" / "prompt-usage.bad.jsonl"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("", encoding="utf-8")
            event = health.event_id(kind="normalize_raw", source="segment.jsonl", content="{", error="JSONDecodeError")
            health.record_event(
                base,
                event=event,
                kind="normalize_raw",
                source="segment.jsonl",
                error="JSONDecodeError",
                evidence_path=evidence,
                captured_at_ns=1,
            )
            with self.assertRaises(health.QuarantineError):
                health.acknowledge(base, event_ids=[event, "missing"])
            report = health.summary(base)

        self.assertEqual(report["unacknowledged_events"], 1)
        self.assertEqual(report["acknowledged_events"], 0)

    def test_normalize_returns_degraded_until_exact_event_is_acknowledged(self) -> None:
        normalize = load_module("normalize_quarantine_contract_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.BASE_DIR = base
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"

            def fake_full_normalize():
                normalize.append_bad(str(base / "raw" / "segment.jsonl"), 1, "{", "JSONDecodeError('bad')")
                return {"mode": "full", "rows": 0, "new_rows": 0}

            stdout = io.StringIO()
            with (
                mock.patch.object(normalize, "full_normalize", side_effect=fake_full_normalize),
                mock.patch.object(normalize.service_lock, "acquire_service_lock", return_value=contextlib.nullcontext()),
                mock.patch.object(normalize.sys, "argv", ["normalize.py"]),
                contextlib.redirect_stdout(stdout),
            ):
                first_code = normalize.main()
            first = json.loads(stdout.getvalue().splitlines()[-1])
            normalize.quarantine_health.acknowledge(base, acknowledge_all=True)

            stdout = io.StringIO()
            with (
                mock.patch.object(normalize, "full_normalize", side_effect=fake_full_normalize),
                mock.patch.object(normalize.service_lock, "acquire_service_lock", return_value=contextlib.nullcontext()),
                mock.patch.object(normalize.sys, "argv", ["normalize.py"]),
                contextlib.redirect_stdout(stdout),
            ):
                second_code = normalize.main()
            second = json.loads(stdout.getvalue().splitlines()[-1])

        self.assertEqual(first_code, 1)
        self.assertEqual(first["status"], "degraded")
        self.assertEqual(first["quarantine"]["unacknowledged_events"], 1)
        self.assertEqual(second_code, 0)
        self.assertEqual(second["status"], "healthy")
        self.assertEqual(second["quarantine"]["acknowledged_occurrences"], 1)

    def test_full_normalize_keeps_valid_rows_while_quarantining_malformed_rows(self) -> None:
        normalize = load_module("normalize_valid_and_quarantine_test", ROOT / "scripts" / "normalize.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            normalize.BASE_DIR = base
            normalize.BAD_LOG = base / "bad" / "prompt-usage.bad.jsonl"
            normalize.STATE_FILE = base / "normalized" / "normalize-state.json"
            normalize.NORMALIZED_LOG = base / "normalized" / "prompt-usage.normalized.jsonl"
            current = normalize.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            current_path = pathlib.Path(current["path"])
            current_path.write_text(json.dumps(_turn_raw("session", "valid", total=73)) + "\n{\n", encoding="utf-8")
            normalize.QUARANTINE_RESULTS.clear()

            result = normalize.full_normalize()
            quarantine = normalize.quarantine_health.operation_summary(normalize.QUARANTINE_RESULTS)
            rows = [json.loads(line) for line in normalize.NORMALIZED_LOG.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result["rows"], 1)
        self.assertEqual([row["turn_id"] for row in rows], ["valid"])
        self.assertEqual(quarantine["unacknowledged_events"], 1)

    def test_reconcile_moves_corrupt_state_and_reports_degraded(self) -> None:
        reconcile = load_module("reconcile_quarantine_contract_test", ROOT / "scripts" / "reconcile.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            pending = state_dir / "pending.json"
            pending.write_text("{", encoding="utf-8")
            reconcile.BASE_DIR = base
            reconcile.STATE_DIR = state_dir
            reconcile.BAD_DIR = base / "bad"
            reconcile.ERROR_LOG = state_dir / "prompt-usage-errors.jsonl"
            stdout = io.StringIO()
            with mock.patch.object(reconcile, "completed_turn_index", return_value=set()), contextlib.redirect_stdout(stdout):
                code = reconcile.run_reconcile()
            payload = json.loads(stdout.getvalue().splitlines()[-1])

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["counts"]["bad"], 1)
        self.assertEqual(payload["quarantine"]["unacknowledged_events"], 1)
        self.assertFalse(pending.exists())

    def test_pipeline_builds_after_degraded_children_and_returns_degraded(self) -> None:
        cli = load_module("pipeline_quarantine_contract_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            codex_dir = base / "codex"
            output_dir = base / "data"
            output = output_dir / "analytics" / "bola.sqlite"
            calls: list[str] = []

            def fake_run_script_json(name, extra_args, env=None):
                calls.append(name)
                if name == "reconcile.py":
                    return 1, {"status": "degraded", "quarantine": {"occurrences": 1, "new_events": 1, "unacknowledged_events": 1, "acknowledged_occurrences": 0, "event_ids": ["r"]}}, "", ""
                if name == "normalize.py":
                    return 1, {"status": "degraded", "mode": "incremental", "normalized_turns_size": 0, "quarantine": {"occurrences": 1, "new_events": 1, "unacknowledged_events": 1, "acknowledged_occurrences": 0, "event_ids": ["n"]}}, "", ""
                if name == "compact_raw.py":
                    return 0, {}, "", ""
                if name == "build_analytics.py":
                    return 0, {"turn_rows": 2}, "", ""
                raise AssertionError(name)

            args = types.SimpleNamespace(
                codex_dir=str(codex_dir),
                output_dir=str(output_dir),
                state_db=None,
                output=str(output),
                project_root=None,
                incremental=True,
                recover=True,
                skip_rotate=False,
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(cli, "run_script_json", side_effect=fake_run_script_json),
                mock.patch.object(cli, "read_analytics_metadata", return_value={}),
                mock.patch.object(cli.dashboard_cleanup, "complete_retention_derived_rebuild", return_value={"updated": False}),
                contextlib.redirect_stdout(stdout),
            ):
                code = cli.pipeline(args)
            payload = json.loads(stdout.getvalue().splitlines()[-1])

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["quarantine"]["unacknowledged_events"], 2)
        self.assertIn("build_analytics.py", calls)

    def test_doctor_health_distinguishes_quarantine_warning_from_corrupt_state(self) -> None:
        cli = load_module("doctor_quarantine_health_test", ROOT / "scripts" / "bola.py")
        base_report = {
            "codex_dir": {"valid": True},
            "codex_cli": {"valid": True},
            "runtime": {
                "current_segments": {},
                "hooks_json": {},
                "recovery": {},
                "normalize_pending_publish": {},
                "analytics_tmp_files": {},
                "quarantine": {"unacknowledged_events": 2, "unacknowledged_occurrences": 4, "by_kind": {"normalize_raw": 2}},
            },
        }
        degraded = cli.doctor_health(base_report)
        base_report["runtime"]["quarantine"] = {"error": "invalid"}
        failed = cli.doctor_health(base_report)

        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["exit_code"], 1)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 2)

    def test_quarantine_cli_lists_and_acknowledges_without_deleting_evidence(self) -> None:
        cli = load_module("quarantine_cli_contract_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            event = cli.quarantine_health.event_id(kind="normalize_raw", source="segment.jsonl", content="{", error="JSONDecodeError")
            evidence = base / "bad" / "prompt-usage.bad.jsonl"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "event_id": event,
                        "kind": "normalize_raw",
                        "captured_at_ns": 1,
                        "source": "segment.jsonl",
                        "line": "{",
                        "error": "JSONDecodeError",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cli.quarantine_health.record_event(
                base,
                event=event,
                kind="normalize_raw",
                source="segment.jsonl",
                error="JSONDecodeError",
                evidence_path=evidence,
                captured_at_ns=1,
            )
            paths = types.SimpleNamespace(codex_dir=base / "codex", output_dir=base)
            list_args = types.SimpleNamespace(
                codex_dir=None,
                output_dir=None,
                quarantine_action="list",
                include_acknowledged=False,
                json_output=False,
            )
            acknowledge_args = types.SimpleNamespace(
                codex_dir=None,
                output_dir=None,
                quarantine_action="acknowledge",
                event_id=[event],
                acknowledge_all=False,
                json_output=False,
            )
            before_output = io.StringIO()
            acknowledge_output = io.StringIO()
            after_output = io.StringIO()
            with mock.patch.object(cli, "runtime_paths", return_value=paths):
                with contextlib.redirect_stdout(before_output):
                    before_code = cli.quarantine_command(list_args)
                with contextlib.redirect_stdout(acknowledge_output):
                    acknowledge_code = cli.quarantine_command(acknowledge_args)
                with contextlib.redirect_stdout(after_output):
                    after_code = cli.quarantine_command(list_args)
                evidence_retained = evidence.exists()

        self.assertEqual(before_code, 1)
        self.assertEqual(acknowledge_code, 0)
        self.assertEqual(after_code, 0)
        self.assertTrue(evidence_retained)
        self.assertIn("BOLA Quarantine: NEEDS REVIEW", before_output.getvalue())
        self.assertIn(f"Event ID: {event}", before_output.getvalue())
        self.assertIn(f"Review: bola quarantine acknowledge --event-id {event}", before_output.getvalue())
        self.assertIn("Evidence: bad/prompt-usage.bad.jsonl", before_output.getvalue())
        self.assertIn("BOLA Quarantine: UPDATED", acknowledge_output.getvalue())
        self.assertIn("Acknowledged now: 1", acknowledge_output.getvalue())
        self.assertIn("Evidence retained: yes", acknowledge_output.getvalue())
        self.assertIn("BOLA Quarantine: HEALTHY", after_output.getvalue())
        self.assertIn("No events need review", after_output.getvalue())
        self.assertNotIn("\x1b", before_output.getvalue() + acknowledge_output.getvalue() + after_output.getvalue())

    def test_quarantine_cli_json_modes_preserve_payloads(self) -> None:
        cli = load_module("quarantine_cli_json_contract_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            evidence = base / "bad" / "prompt-usage.bad.jsonl"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("", encoding="utf-8")
            event = cli.quarantine_health.event_id(kind="normalize_raw", source="segment.jsonl", content="{", error="JSONDecodeError")
            cli.quarantine_health.record_event(
                base,
                event=event,
                kind="normalize_raw",
                source="segment.jsonl",
                error="JSONDecodeError",
                evidence_path=evidence,
                captured_at_ns=1,
            )
            paths = types.SimpleNamespace(codex_dir=base / "codex", output_dir=base)
            list_output = io.StringIO()
            acknowledge_output = io.StringIO()
            with mock.patch.object(cli, "runtime_paths", return_value=paths):
                with contextlib.redirect_stdout(list_output):
                    list_code = cli.quarantine_command(
                        types.SimpleNamespace(
                            codex_dir=None,
                            output_dir=None,
                            quarantine_action="list",
                            include_acknowledged=False,
                            json_output=True,
                        )
                    )
                with contextlib.redirect_stdout(acknowledge_output):
                    acknowledge_code = cli.quarantine_command(
                        types.SimpleNamespace(
                            codex_dir=None,
                            output_dir=None,
                            quarantine_action="acknowledge",
                            event_id=[event],
                            acknowledge_all=False,
                            json_output=True,
                        )
                    )

        listed = json.loads(list_output.getvalue())
        acknowledged = json.loads(acknowledge_output.getvalue())
        self.assertEqual(list_code, 1)
        self.assertEqual(listed["status"], "degraded")
        self.assertEqual(listed["quarantine"]["events"][0]["event_id"], event)
        self.assertEqual(acknowledge_code, 0)
        self.assertEqual(acknowledged["acknowledged_events"], 1)
        self.assertEqual(acknowledged["remaining_unacknowledged_events"], 0)

    def test_quarantine_acknowledge_all_reports_updates_and_noop_repeat(self) -> None:
        cli = load_module("quarantine_cli_acknowledge_all_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            evidence = base / "bad" / "prompt-usage.bad.jsonl"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("", encoding="utf-8")
            for index in range(2):
                event = cli.quarantine_health.event_id(
                    kind="normalize_raw",
                    source=f"segment-{index}.jsonl",
                    content=f"{{{index}",
                    error="JSONDecodeError",
                )
                cli.quarantine_health.record_event(
                    base,
                    event=event,
                    kind="normalize_raw",
                    source=f"segment-{index}.jsonl",
                    error="JSONDecodeError",
                    evidence_path=evidence,
                    captured_at_ns=index + 1,
                )
            paths = types.SimpleNamespace(codex_dir=base / "codex", output_dir=base)
            args = types.SimpleNamespace(
                codex_dir=None,
                output_dir=None,
                quarantine_action="acknowledge",
                event_id=[],
                acknowledge_all=True,
                json_output=False,
            )
            first_output = io.StringIO()
            second_output = io.StringIO()
            with mock.patch.object(cli, "runtime_paths", return_value=paths):
                with contextlib.redirect_stdout(first_output):
                    first_code = cli.quarantine_command(args)
                with contextlib.redirect_stdout(second_output):
                    second_code = cli.quarantine_command(args)

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertIn("BOLA Quarantine: UPDATED", first_output.getvalue())
        self.assertIn("Selected events: 2", first_output.getvalue())
        self.assertIn("Acknowledged now: 2", first_output.getvalue())
        self.assertIn("Remaining unacknowledged: 0", first_output.getvalue())
        self.assertIn("BOLA Quarantine: UNCHANGED", second_output.getvalue())
        self.assertIn("Already acknowledged: 2", second_output.getvalue())

    def test_quarantine_include_acknowledged_marks_event_status(self) -> None:
        cli = load_module("quarantine_cli_acknowledged_render_test", ROOT / "scripts" / "bola.py")
        event = "a" * 64
        payload = {
            "status": "healthy",
            "quarantine": {
                "unacknowledged_events": 0,
                "unacknowledged_occurrences": 0,
                "acknowledged_events": 1,
                "events": [
                    {
                        "event_id": event,
                        "kind": "normalize_raw",
                        "source": "segment.jsonl",
                        "error_type": "JSONDecodeError",
                        "evidence_path": "bad/prompt-usage.bad.jsonl",
                        "occurrences": 1,
                        "last_seen_at_ns": 1_000_000_000,
                        "acknowledged_at_ns": 2_000_000_000,
                    }
                ],
            },
        }

        output = cli.quarantine_renderer.render_list(payload, include_acknowledged=True)

        self.assertIn("[ACKNOWLEDGED] normalize_raw", output)
        self.assertIn(f"Event ID: {event}", output)
        self.assertNotIn("Review: bola quarantine acknowledge", output)
        self.assertIn("Full report: bola quarantine list --include-acknowledged --json", output)

    def test_quarantine_cli_errors_follow_human_and_json_modes(self) -> None:
        cli = load_module("quarantine_cli_error_output_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            human_output = io.StringIO()
            with (
                mock.patch.object(
                    cli.sys,
                    "argv",
                    ["bola", "quarantine", "--output-dir", str(base), "acknowledge", "--event-id", "missing"],
                ),
                contextlib.redirect_stdout(human_output),
            ):
                human_code = cli.main()
            json_output = io.StringIO()
            with (
                mock.patch.object(
                    cli.sys,
                    "argv",
                    ["bola", "quarantine", "--output-dir", str(base), "acknowledge", "--event-id", "missing", "--json"],
                ),
                contextlib.redirect_stdout(json_output),
            ):
                json_code = cli.main()

        self.assertEqual(human_code, 2)
        self.assertIn("BOLA Quarantine: FAILED", human_output.getvalue())
        self.assertIn("Error: unknown quarantine event ids: missing", human_output.getvalue())
        self.assertEqual(json_code, 2)
        self.assertEqual(json.loads(json_output.getvalue())["error"], "quarantine_state_invalid")

    def test_quarantine_cli_requires_explicit_acknowledgement_scope(self) -> None:
        cli = load_module("quarantine_cli_parser_test", ROOT / "scripts" / "bola.py")
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["quarantine", "acknowledge"])
        parsed = cli.build_parser().parse_args(["quarantine", "acknowledge", "--event-id", "event-1"])
        self.assertEqual(parsed.event_id, ["event-1"])
        self.assertFalse(parsed.json_output)
        multiple = cli.build_parser().parse_args(
            ["quarantine", "acknowledge", "--event-id", "event-1", "--event-id", "event-2"]
        )
        self.assertEqual(multiple.event_id, ["event-1", "event-2"])
        self.assertTrue(cli.build_parser().parse_args(["quarantine", "list", "--json"]).json_output)
        self.assertTrue(cli.build_parser().parse_args(["quarantine", "acknowledge", "--all", "--json"]).json_output)

    def test_dashboard_treats_degraded_pipeline_as_completed_warning(self) -> None:
        serve = load_module("dashboard_rebuild_quarantine_test", ROOT / "scripts" / "serve_dashboard.py")
        rebuild = serve.dashboard_rebuild_api
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            db_path = base / "analytics" / "bola.sqlite"
            db_path.parent.mkdir(parents=True)
            db_path.write_text("", encoding="utf-8")
            captured: dict[str, object] = {}
            manager = rebuild.operation_state.DashboardOperationManager()
            operation_id = "11111111-1111-4111-8111-111111111111"

            class Handler(rebuild.DashboardRebuildApiMixin):
                server = types.SimpleNamespace(db_path=db_path, operation_manager=manager)

                def dashboard_operation_manager(self):
                    return manager

                @staticmethod
                def read_json_body():
                    return {"operation_id": operation_id}

                def dashboard_output_dir(self):
                    return base

                def dashboard_script_dir(self):
                    return ROOT / "scripts"

                def dashboard_codex_dir(self):
                    return base / "codex"

                @staticmethod
                def parse_last_json(stdout):
                    return json.loads(stdout.splitlines()[-1])

                @staticmethod
                def int_metadata(metadata, key):
                    return int(metadata.get(key) or 0)

                @staticmethod
                def numeric_metadata(metadata, key):
                    return float(metadata.get(key) or 0)

                def send_json(self, data, status=200):
                    captured["data"] = data
                    captured["status"] = status

            class Process:
                returncode = 1

                def poll(self):
                    return self.returncode

                def wait(self, timeout=None):
                    return self.returncode

            def fake_popen(*args, **kwargs):
                kwargs["stdout"].write(
                    json.dumps(
                        {
                            "status": "degraded",
                            "quarantine": {"unacknowledged_events": 1},
                            "new_turn_rows": 2,
                        }
                    )
                )
                kwargs["stdout"].flush()
                return Process()

            with (
                mock.patch.object(rebuild.dashboard_managed_process.ManagedProcess, "start", side_effect=fake_popen),
                mock.patch.object(rebuild.dashboard_cleanup, "refresh_retention_index_for_current_sources", return_value={"sources": []}),
            ):
                Handler().handle_rebuild()

        self.assertEqual(captured["status"], 200)
        self.assertTrue(captured["data"]["ok"])
        self.assertEqual(captured["data"]["data_health"], "degraded")
        self.assertEqual(captured["data"]["quarantine"]["unacknowledged_events"], 1)


if __name__ == "__main__":
    unittest.main()
