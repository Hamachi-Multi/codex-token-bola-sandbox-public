from __future__ import annotations
import shlex

try:
    from tests.support import ROOT, argparse, io, json, load_module, mock, pathlib, stat, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, argparse, io, json, load_module, mock, pathlib, stat, tempfile, unittest


class CliContractTests(unittest.TestCase):
    def test_service_paths_use_codex_token_bola_root(self) -> None:
        service_paths = load_module("service_paths_root_test", ROOT / "scripts" / "service_paths.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"

            self.assertEqual(service_paths.service_root(codex_home), codex_home / "codex-token-bola")
            self.assertEqual(service_paths.legacy_root(codex_home), codex_home / "token-usage")

    def test_service_paths_require_explicit_migration_for_legacy_only_home(self) -> None:
        service_paths = load_module("service_paths_legacy_only_test", ROOT / "scripts" / "service_paths.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            (codex_home / "token-usage" / "state").mkdir(parents=True)

            with self.assertRaises(service_paths.PathMigrationRequired):
                service_paths.assert_migrated(codex_home)

    def test_migrate_path_dry_run_does_not_move_legacy_root(self) -> None:
        cli = load_module("migrate_path_dry_run_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            legacy = codex_home / "token-usage"
            legacy.mkdir(parents=True)
            (legacy / "marker.txt").write_text("legacy", encoding="utf-8")
            captured = io.StringIO()

            with mock.patch.object(cli.sys, "stdout", captured):
                code = cli.migrate_path(argparse.Namespace(codex_home=str(codex_home), apply=False))

            self.assertEqual(code, 0)
            self.assertTrue((legacy / "marker.txt").exists())
            self.assertFalse((codex_home / "codex-token-bola").exists())

    def test_migrate_path_apply_moves_legacy_root_to_codex_token_bola(self) -> None:
        cli = load_module("migrate_path_apply_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            legacy = codex_home / "token-usage"
            legacy.mkdir(parents=True)
            (legacy / "marker.txt").write_text("legacy", encoding="utf-8")
            captured = io.StringIO()

            with mock.patch.object(cli.sys, "stdout", captured):
                code = cli.migrate_path(argparse.Namespace(codex_home=str(codex_home), apply=True))

            self.assertEqual(code, 0)
            self.assertFalse(legacy.exists())
            self.assertEqual((codex_home / "codex-token-bola" / "marker.txt").read_text(encoding="utf-8"), "legacy")

    def test_doctor_blocks_legacy_only_home_with_migration_message(self) -> None:
        cli = load_module("doctor_legacy_only_migration_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            (codex_home / "token-usage" / "state").mkdir(parents=True)
            captured = io.StringIO()
            with mock.patch.object(cli.sys, "stdout", captured):
                code = cli.doctor(argparse.Namespace(codex_home=str(codex_home)))

        report = json.loads(captured.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report["error"], "path_migration_required")
        self.assertIn("migrate-path --apply", report["message"])

    def test_install_hook_copies_repo_hook_and_registers_hooks_json_owner_only(self) -> None:
        cli = load_module("install_hook_cli_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            result = cli.install_hook(argparse.Namespace(codex_home=str(codex_home)))
            installed = codex_home / "hooks" / "token-usage.py"
            hooks_json = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            status = cli.hooks_json_status(codex_home)

            self.assertEqual(result["installed_hook"], str(installed))
            self.assertEqual(installed.read_text(encoding="utf-8"), (ROOT / "hooks" / "token-usage.py").read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((codex_home / "hooks.json").stat().st_mode), 0o600)
            self.assertIn("hooks_json", result)
            self.assertTrue(status["events"]["UserPromptSubmit"]["registered"])
            self.assertTrue(status["events"]["Stop"]["registered"])
            self.assertIn("hooks", hooks_json)

    def test_install_hook_preserves_existing_hooks_and_deduplicates_registration(self) -> None:
        cli = load_module("install_hook_merge_hooks_json_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            hooks_path = codex_home / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /tmp/existing.py"}]}],
                            "Stop": [{"hooks": [{"type": "command", "command": "python3 /tmp/existing-stop.py"}]}],
                        }
                    }
                ),
                encoding="utf-8",
            )

            first = cli.install_hook(argparse.Namespace(codex_home=str(codex_home)))
            second = cli.install_hook(argparse.Namespace(codex_home=str(codex_home)))
            parsed = json.loads(hooks_path.read_text(encoding="utf-8"))

        self.assertTrue(first["hooks_json"]["updated"])
        self.assertFalse(second["hooks_json"]["updated"])
        user_commands = [
            nested["command"]
            for entry in parsed["hooks"]["UserPromptSubmit"]
            for nested in entry.get("hooks", [])
            if isinstance(nested, dict) and nested.get("command")
        ]
        stop_commands = [
            nested["command"]
            for entry in parsed["hooks"]["Stop"]
            for nested in entry.get("hooks", [])
            if isinstance(nested, dict) and nested.get("command")
        ]
        self.assertIn("python3 /tmp/existing.py", user_commands)
        self.assertIn("python3 /tmp/existing-stop.py", stop_commands)
        self.assertEqual(sum("token-usage.py" in command for command in user_commands), 1)
        self.assertEqual(sum("token-usage.py" in command for command in stop_commands), 1)

    def test_install_hook_quotes_hooks_json_command_for_spaced_codex_home(self) -> None:
        cli = load_module("install_hook_quoted_command_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex with spaces"
            result = cli.install_hook(argparse.Namespace(codex_home=str(codex_home)))
            command = result["hooks_json"]["events"]["Stop"]["commands"][0]

        self.assertEqual(shlex.split(command), ["python3", str(codex_home / "hooks" / "token-usage.py")])
        self.assertNotIn(f"python3 {codex_home}", command)

    def test_install_hook_does_not_dedupe_unrelated_command_containing_hook_path(self) -> None:
        cli = load_module("install_hook_substring_dedupe_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            installed = codex_home / "hooks" / "token-usage.py"
            hooks_path = codex_home / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"echo {installed}"}]}]}}),
                encoding="utf-8",
            )

            result = cli.install_hook(argparse.Namespace(codex_home=str(codex_home)))
            commands = result["hooks_json"]["events"]["Stop"]["commands"]

        self.assertTrue(result["hooks_json"]["updated"])
        self.assertIn(f"echo {installed}", commands)
        self.assertIn(cli.hook_command(installed), commands)

    def test_doctor_reports_current_segments_and_hook_registration(self) -> None:
        cli = load_module("doctor_runtime_current_segments_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            base = codex_home / "codex-token-bola"
            raw_segments = cli.dashboard_cleanup.raw_segments
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text("{}\n", encoding="utf-8")
            hooks_dir = codex_home / "hooks"
            hooks_dir.mkdir(parents=True)
            installed = hooks_dir / "token-usage.py"
            installed.write_text((ROOT / "hooks" / "token-usage.py").read_text(encoding="utf-8"), encoding="utf-8")
            (codex_home / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"python3 {installed}"}]}],
                            "Stop": [{"hooks": [{"type": "command", "command": f"python3 {installed}"}]}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            captured = io.StringIO()
            with mock.patch.object(cli.sys, "stdout", captured):
                code = cli.doctor(argparse.Namespace(codex_home=str(codex_home)))

        report = json.loads(captured.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["runtime"]["current_segments"]["prompt_usage"]["rows"], 1)
        self.assertTrue(report["runtime"]["hooks_json"]["events"]["UserPromptSubmit"]["registered"])
        self.assertTrue(report["runtime"]["hooks_json"]["events"]["Stop"]["registered"])

    def test_doctor_reports_recovery_state_errors_and_analytics_temp_files(self) -> None:
        cli = load_module("doctor_recovery_state_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            base = codex_home / "codex-token-bola"
            state_dir = base / "state"
            analytics_dir = base / "analytics"
            normalized_dir = base / "normalized"
            state_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            (state_dir / "pending-turn.json").write_text(
                json.dumps({"record_type": "turn_stop_missing_start", "session_id": "s1", "turn_id": "t1"}),
                encoding="utf-8",
            )
            (state_dir / "current-raw-segments.json").write_text("{}", encoding="utf-8")
            (base / "prompt-usage-errors.jsonl").write_text(
                json.dumps({"warning": "deferred_stop_recovery", "reason": "hook_scan_limit_reached"}) + "\n"
                + json.dumps({"error": "raw_append_failed"}) + "\n",
                encoding="utf-8",
            )
            tmp_db = analytics_dir / ".token-usage.sqlite.123.tmp"
            tmp_db.write_bytes(b"abc")
            pending_publish = normalized_dir / "normalize-state.json.pending"
            pending_publish.write_text("{broken", encoding="utf-8")
            captured = io.StringIO()

            with mock.patch.object(cli.sys, "stdout", captured):
                code = cli.doctor(argparse.Namespace(codex_home=str(codex_home)))

        report = json.loads(captured.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["runtime"]["recovery"]["pending_state_files"], 1)
        self.assertEqual(report["runtime"]["recovery"]["error_log_counts"]["warning:deferred_stop_recovery"], 1)
        self.assertEqual(report["runtime"]["recovery"]["error_log_counts"]["error:raw_append_failed"], 1)
        self.assertTrue(report["runtime"]["normalize_pending_publish"]["exists"])
        self.assertTrue(report["runtime"]["normalize_pending_publish"]["recovery_required"])
        self.assertFalse(report["runtime"]["normalize_pending_publish"]["valid"])
        self.assertEqual(report["runtime"]["normalize_pending_publish"]["path"], str(pending_publish))
        self.assertEqual(report["runtime"]["analytics_tmp_files"]["count"], 1)
        self.assertEqual(report["runtime"]["analytics_tmp_files"]["bytes"], 3)

    def test_cli_serve_default_port_matches_makefile(self) -> None:
        cli = load_module("serve_default_port_test", ROOT / "scripts" / "codex_token_usage.py")
        args = cli.parse_args(["serve"])
        self.assertEqual(args.port, "8766")

    def test_cli_serve_passes_allow_network_to_dashboard_server(self) -> None:
        cli = load_module("serve_allow_network_test", ROOT / "scripts" / "codex_token_usage.py")
        calls: list[tuple[str, list[str]]] = []

        def fake_run_script(name, extra_args, env=None):
            calls.append((name, list(extra_args)))
            return 0

        with (
            mock.patch.object(cli, "ensure_path_migrated"),
            mock.patch.object(cli, "run_script", fake_run_script),
            mock.patch.object(cli.sys, "argv", ["codex_token_usage.py", "serve", "--host", "0.0.0.0", "--allow-network"]),
        ):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(calls, [("serve_dashboard.py", ["--host", "0.0.0.0", "--port", "8766", "--allow-network"])])

    def test_cli_serve_rejects_db_override(self) -> None:
        cli = load_module("serve_rejects_db_override_test", ROOT / "scripts" / "codex_token_usage.py")
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["serve", "--db", "/tmp/custom.sqlite"])

    def test_high_level_cli_commands_reject_unknown_options(self) -> None:
        cli = load_module("high_level_unknown_args_test", ROOT / "scripts" / "codex_token_usage.py")
        cases = [
            ["pipeline", "--codex-hmoe", "/tmp/nope"],
            ["retention-prune", "--cutoff", "0", "--outptu", "/tmp/nope"],
            ["doctor", "--codex-hmoe", "/tmp/nope"],
            ["install-hook", "--codex-hmoe", "/tmp/nope"],
            ["migrate-path", "--aply"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with mock.patch.object(cli.sys, "argv", ["codex_token_usage.py", *argv]):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main()
                self.assertEqual(raised.exception.code, 2)

    def test_pipeline_help_makes_recovery_explicit(self) -> None:
        cli = load_module("pipeline_help_recovery_contract_test", ROOT / "scripts" / "codex_token_usage.py")
        help_text = cli.build_parser().format_help()

        self.assertIn("pipeline", help_text)
        self.assertNotIn("Run reconcile, normalize, then build.", help_text)

    def test_retention_prune_invalid_cutoff_returns_structured_error(self) -> None:
        cli = load_module("retention_invalid_cutoff_test", ROOT / "scripts" / "codex_token_usage.py")
        captured = io.StringIO()

        with mock.patch.object(cli.sys, "stdout", captured):
            code = cli.retention_prune(argparse.Namespace(codex_home=None, output=None, cutoff="not-a-date", preview_signature="sig"))

        payload = json.loads(captured.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "cutoff_date_invalid")
        self.assertEqual(payload["stage"], "preview")

    def test_parse_cutoff_date_only_uses_utc_midnight(self) -> None:
        cli = load_module("retention_cutoff_date_only_utc_test", ROOT / "scripts" / "codex_token_usage.py")

        self.assertEqual(cli.parse_cutoff("2026-05-20"), cli.parse_cutoff("2026-05-20T00:00:00+00:00"))

    def test_pipeline_rejects_output_outside_service_analytics_before_lock(self) -> None:
        cli = load_module("pipeline_output_owner_guard_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            external = pathlib.Path(tmp_dir) / "external.sqlite"
            args = argparse.Namespace(
                codex_home=str(codex_home),
                output=str(external),
                state_db=None,
                project_root=None,
                incremental=True,
                recover=False,
                skip_rotate=False,
            )

            with mock.patch.object(cli.service_lock, "acquire_service_lock", side_effect=AssertionError("pipeline must reject output before acquiring lock")):
                with self.assertRaises(ValueError):
                    cli.pipeline(args)

    def test_retention_prune_rejects_output_outside_service_analytics_before_lock(self) -> None:
        cli = load_module("retention_prune_output_owner_guard_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            external = pathlib.Path(tmp_dir) / "external.sqlite"
            args = argparse.Namespace(codex_home=str(codex_home), output=str(external), cutoff="2026-05-20", preview_signature="sig")

            with mock.patch.object(cli.service_lock, "acquire_service_lock", side_effect=AssertionError("retention-prune must reject output before acquiring lock")):
                with self.assertRaises(ValueError):
                    cli.retention_prune(args)

    def test_retention_prune_outputs_partial_mutation_envelope_last_after_normalize_failure(self) -> None:
        cli = load_module("retention_prune_partial_mutation_last_json_test", ROOT / "scripts" / "codex_token_usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = pathlib.Path(tmp_dir) / ".codex"
            base = codex_home / "codex-token-bola"
            base.mkdir(parents=True)
            captured = io.StringIO()
            lock_context = mock.MagicMock(__enter__=lambda _self: mock.Mock(path=base / "state" / "service.lock", fd=None), __exit__=lambda *_args: None)

            with (
                mock.patch.object(cli.service_lock, "acquire_service_lock", return_value=lock_context),
                mock.patch.object(cli.service_lock, "child_lock_env", return_value={}),
                mock.patch.object(cli.dashboard_cleanup, "retention_preview_signature", return_value="sig"),
                mock.patch.object(cli.dashboard_cleanup, "preflight_delete_logs_older_than", return_value=None),
                mock.patch.object(cli, "raw_segment_state_checkpoint", return_value={"checkpoint": True}),
                mock.patch.object(cli.dashboard_cleanup, "plan_delete_logs_older_than", return_value={"segments": {"deleted_rows": 1}, "untracked": []}),
                mock.patch.object(cli.dashboard_cleanup, "validate_delete_logs_older_than_plan", return_value=None),
                mock.patch.object(cli.dashboard_cleanup, "reset_derived_outputs", return_value={"reset": True}),
                mock.patch.object(
                    cli.dashboard_cleanup,
                    "apply_delete_logs_older_than_plan",
                    return_value={"deleted_rows": 1, "scanned_rows": 1, "physical_delete_pending": True, "pending_files": 2},
                ),
                mock.patch.object(cli.dashboard_cleanup, "write_cleanup_retention_job", return_value=None),
                mock.patch.object(
                    cli,
                    "run_script_json",
                    return_value=(
                        2,
                        {"error": "normalize_pending_publish_recovery_failed", "recovery_required": True},
                        '{"error":"normalize_pending_publish_recovery_failed","recovery_required":true}\n',
                        "",
                    ),
                ),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.retention_prune(
                    argparse.Namespace(codex_home=str(codex_home), output=None, cutoff="2026-05-20", preview_signature="sig")
                )

        json_lines = [json.loads(line) for line in captured.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual(code, 2)
        self.assertGreaterEqual(len(json_lines), 2)
        self.assertEqual(json_lines[-1]["error"], "retention_rebuild_failed")
        self.assertTrue(json_lines[-1]["partial_mutation"])
        self.assertEqual(json_lines[-1]["stage"], "normalize")
        self.assertEqual(json_lines[-1]["deleted_rows"], 1)
        self.assertTrue(json_lines[-1]["physical_delete_pending"])
        self.assertEqual(json_lines[-1]["pending_files"], 2)

    def test_release_check_command_is_removed(self) -> None:
        cli = load_module("release_check_removed_test", ROOT / "scripts" / "codex_token_usage.py")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["release-check"])

        self.assertFalse((ROOT / "scripts" / "check_release.py").exists())
        self.assertNotIn("release-check", makefile)

    def test_build_cli_rejects_removed_model_call_inputs(self) -> None:
        build = load_module("build_rejects_model_call_inputs_test", ROOT / "scripts" / "build_analytics.py")
        with self.assertRaises(SystemExit):
            with mock.patch.object(build.sys, "argv", ["build_analytics.py", "--model-calls-log", "/tmp/model-calls.jsonl"]):
                build.parse_args()
        with self.assertRaises(SystemExit):
            with mock.patch.object(build.sys, "argv", ["build_analytics.py", "--raw-model-calls-log", "/tmp/raw-model-calls.jsonl"]):
                build.parse_args()
        with self.assertRaises(SystemExit):
            with mock.patch.object(build.sys, "argv", ["build_analytics.py", "--model-calls-offset", "1"]):
                build.parse_args()

    def test_dashboard_rebuild_does_not_buffer_child_output_in_pipes(self) -> None:
        source = (ROOT / "scripts" / "serve_dashboard.py").read_text(encoding="utf-8")
        self.assertNotIn("stdout=subprocess.PIPE", source)
        self.assertNotIn("stderr=subprocess.PIPE", source)
        self.assertIn("tempfile.TemporaryFile", source)

    def test_compat_facades_do_not_mutate_submodule_globals(self) -> None:
        for relative in ("scripts/raw_segments.py", "scripts/dashboard_cleanup.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("_sync_module_bindings", source)
            self.assertNotIn("_restore_module_bindings", source)
            self.assertNotIn("setattr(", source)

    def test_ui_check_defaults_to_fixture_and_live_is_explicit(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        runner = (ROOT / "scripts" / "playwright_dashboard_check.py").read_text(encoding="utf-8")
        self.assertIn("ui-check:\n\t$(PYTHON) scripts/playwright_dashboard_check.py", makefile)
        self.assertIn("ui-check-live:\n\t$(PYTHON) scripts/playwright_dashboard_check.py --url http://127.0.0.1:8766", makefile)
        self.assertIn("write_dashboard_fixture", runner)
        self.assertIn("Omit to run an isolated fixture server", runner)

    def test_ui_check_fails_on_browser_runtime_errors(self) -> None:
        runner = (ROOT / "scripts" / "playwright_dashboard_check.py").read_text(encoding="utf-8")
        self.assertIn('page.on("pageerror"', runner)
        self.assertIn('page.on("console"', runner)
        self.assertIn('page.on("requestfailed"', runner)
        self.assertIn("raise RuntimeError(\"browser runtime errors detected", runner)

    def test_cleanup_ui_contract_reads_asset_files_not_server_bundle(self) -> None:
        for relative in (
            "tests/test_dashboard_cleanup_ui.py",
            "tests/test_dashboard_api_queries.py",
            "tests/test_dashboard_ui_contract.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("dashboard_asset_bundle", source)
            self.assertNotIn("DASHBOARD_SOURCE_BUNDLE", source)

    def test_cleanup_row_groups_are_explicit_contract(self) -> None:
        contract = load_module("dashboard_cleanup_contract_explicit_test", ROOT / "scripts" / "dashboard_cleanup_contract.py")
        definitions = contract.cleanup_row_definitions()
        labels = [row["label"] for row in definitions]
        group_ids = [row["group_id"] for row in definitions]

        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(len(group_ids), len(set(group_ids)))
        self.assertNotIn("Raw Usage Logs", labels)
        self.assertIn("Raw Current Segments", labels)
        self.assertNotIn("Raw Model Calls", labels)
        with self.assertRaises(KeyError):
            contract.cleanup_group_for_label("Made Up Cleanup Group")

    def test_playwright_desktop_checks_are_split_by_area(self) -> None:
        desktop = (ROOT / "scripts" / "playwright_dashboard_desktop.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(desktop.splitlines()), 80)
        for module_name, function_name in (
            ("playwright_dashboard_toolbar.py", "check_toolbar"),
            ("playwright_dashboard_turns.py", "check_turns_and_selected_turn"),
            ("playwright_dashboard_cleanup.py", "check_cleanup_desktop"),
            ("playwright_dashboard_tools.py", "check_tools_and_subagents"),
        ):
            source = (ROOT / "scripts" / module_name).read_text(encoding="utf-8")
            self.assertIn(f"def {function_name}", source)
            self.assertIn(function_name, desktop)

        cleanup_source = (ROOT / "scripts" / "playwright_dashboard_cleanup.py").read_text(encoding="utf-8")
        for function_name in (
            "check_cleanup_table_contract",
            "check_cleanup_selection_state",
            "check_cleanup_all_preset",
            "check_cleanup_retention_preset",
            "check_cleanup_detail_modal",
            "check_cleanup_refresh_stability",
        ):
            self.assertIn(f"def {function_name}", cleanup_source)
