from __future__ import annotations
import contextlib
import shlex

try:
    from tests.support import ROOT, argparse, io, json, load_module, mock, os, pathlib, sqlite3, stat, subprocess, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, argparse, io, json, load_module, mock, os, pathlib, sqlite3, stat, subprocess, tempfile, unittest

from scripts import service_paths


def assert_order(testcase: unittest.TestCase, text: str, *needles: str) -> None:
    cursor = -1
    for needle in needles:
        position = text.find(needle)
        testcase.assertNotEqual(position, -1, f"missing fragment: {needle}")
        testcase.assertGreater(position, cursor, f"fragment out of order: {needle}")
        cursor = position


class CliContractTests(unittest.TestCase):
    def test_retention_preview_command_emits_machine_readable_signature(self) -> None:
        cli = load_module("retention_preview_command_test", ROOT / "scripts" / "bola.py")
        paths = mock.Mock(output_dir=pathlib.Path("/tmp/bola-preview"))
        preview = {
            "scanned_rows": 12,
            "deletable_rows": 7,
            "deletable_bytes": 900,
            "affected_files": 2,
            "files": [
                {"affected": True, "scanned_rows": 4, "deletable_rows": 4},
                {"affected": True, "scanned_rows": 8, "deletable_rows": 3},
            ],
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(cli, "runtime_paths", return_value=paths),
            mock.patch.object(cli.dashboard_cleanup, "retention_preview", return_value=preview) as preview_call,
            mock.patch.object(cli.dashboard_cleanup, "retention_preview_signature", return_value="fresh-signature"),
            contextlib.redirect_stdout(stdout),
        ):
            code = cli.retention_preview_command(argparse.Namespace(codex_dir=None, output_dir=None, cutoff="2026-05-20"))

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["preview_signature"], "fresh-signature")
        self.assertEqual(payload["delete_files"], 1)
        self.assertEqual(payload["rewrite_files"], 1)
        preview_call.assert_called_once()
        self.assertFalse(preview_call.call_args.kwargs["refresh_index"])

    def test_output_migration_import_streams_segment_payload(self) -> None:
        cli = load_module("migration_segment_stream_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "source.jsonl"
            destination = root / "destination"
            source.write_text(
                json.dumps(
                    {
                        "record_type": "turn_usage_raw",
                        "session_id": "session",
                        "turn_id": "turn",
                        "captured_at": "2026-05-01T00:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(pathlib.Path, "read_bytes", side_effect=AssertionError("full payload read must not run")):
                result = cli.import_raw_segment(source, destination)

        self.assertEqual(result["rows"], 1)
        self.assertGreater(result["bytes"], 0)

    @staticmethod
    def initialize_codex_dir(path: pathlib.Path) -> pathlib.Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "config.toml").write_text("\n", encoding="utf-8")
        return path

    @staticmethod
    def valid_codex_cli_status() -> dict[str, object]:
        return {"valid": True, "path": "/usr/bin/codex", "version": "codex-cli 1.0.0", "reason": None, "message": None}

    @staticmethod
    def valid_hook_runtime_status() -> dict[str, object]:
        return {
            "valid": True,
            "interpreter": "/usr/bin/python3",
            "module": "codex_token_bola.hook",
            "reason": None,
            "message": None,
        }

    def test_readme_uses_supported_hook_install_and_verification_commands(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("bola install-hook\nbola doctor", readme)
        self.assertIn("Register the hook with the effective paths", readme)
        self.assertIn("#### `install-hook` options", readme)
        self.assertIn("| `--codex-dir` |", readme)
        self.assertIn("| `--output-dir` |", readme)
        self.assertIn("bola install-hook \\\n  --codex-dir ~/private/codex-dir \\\n  --output-dir ~/private/codex-token-data", readme)
        self.assertNotIn('${CODEX_HOME:-$HOME/.codex}', readme)
        assert_order(
            self,
            readme,
            "python3 -m venv .venv",
            "source .venv/bin/activate",
            "python -m pip install .",
            "bola --version",
            "bola install-hook\nbola doctor",
            "#### `install-hook` options",
            "| `--codex-dir` |",
            "| `--output-dir` |",
            "### 3. Capture a Codex turn",
            "### 4. Open the dashboard",
            "bola serve\n```",
            "bola serve --host 127.0.0.1 --port 9000",
        )
        self.assertIn("![Codex Token Bola dashboard overview with sample data](./docs/assets/dashboard/overview.png)", readme)
        self.assertIn("*All screenshots use synthetic sample data.*", readme)
        self.assertIn("<summary>Internal processing details</summary>", readme)
        self.assertIn("<summary>Internal processing details</summary>\n\n<br>\n\n| Step", readme)
        assert_order(
            self,
            readme,
            "## How it works",
            "<summary>Internal processing details</summary>",
            "| 1 | Prompt start |",
            "| 2 | Turn stop |",
            "| 3 | Segment handoff |",
            "| 4 | Reconcile |",
            "| 5 | Normalize |",
            "| 6 | Build |",
            "| 7 | Query |",
            "| 8 | Rebuild |",
            "Execution constraints:",
            "## Command guide",
        )
        self.assertNotIn("python3 -m pip install .", readme)
        self.assertNotIn("CODEX_HOME=~/private/codex-dir codex", readme)
        self.assertNotIn("The default command uses `~/.codex`", readme)
        self.assertIn("#### Codex hooks", readme)
        self.assertIn("BOLA uses `UserPromptSubmit` for the turn baseline and `Stop`", readme)
        self.assertNotIn("Codex hook behavior:", readme)
        self.assertIn("> [!IMPORTANT]\n> Run `bola install-hook` again after moving the checkout or replacing its", readme)
        self.assertIn("| Option | Purpose | Environment override | Default on WSL/Linux |", readme)
        self.assertNotIn("| Requirement |", readme)
        self.assertIn("| `--codex-dir` | Codex state input and hook registration | `CODEX_HOME` | `~/.codex` |", readme)
        self.assertIn("| `--output-dir` | BOLA-generated data | `BOLA_OUTPUT_DIR` |", readme)
        self.assertEqual(readme.count("Codex state input and hook registration"), 1)
        self.assertEqual(readme.count("BOLA-generated data"), 1)
        self.assertIn("`doctor`, `quarantine list`, and `quarantine acknowledge` support `--json`", readme)
        self.assertIn("bola quarantine list --include-acknowledged", readme)
        self.assertIn("`list` exits with `1` when records need review", readme)
        self.assertNotIn("XDG_DATA_HOME", readme)
        self.assertNotIn("XDG_CONFIG_HOME", readme)
        self.assertNotIn("Hook scan and append tuning", readme)
        self.assertNotIn("BOLA_HOOK_TAIL_SCAN_BYTES", readme)
        self.assertNotIn("BOLA_HOOK_FORWARD_SCAN_BYTES", readme)
        self.assertNotIn("BOLA_HOOK_APPEND_LOCK_TIMEOUT_MS", readme)
        self.assertNotIn("BOLA-owned file permissions", readme)
        self.assertIn("## Change paths later", readme)
        self.assertNotIn("#### Change paths later", readme)
        self.assertIn("Migrate only while Codex is stopped and no BOLA data operation is running", readme)
        self.assertIn("bola paths migrate --output-dir --apply", readme)
        self.assertNotIn("\n## Runtime paths\n", readme)
        self.assertNotIn("### Path change recommendation", readme)
        self.assertNotIn("Output migration safety guarantees", readme)
        self.assertIn("## Measured storage footprint", readme)
        self.assertIn("6,318 analyzed turns", readme)
        self.assertIn("**10.26 KiB per analyzed turn**", readme)
        self.assertIn("| 100,000 | about 0.98 GiB |", readme)
        self.assertIn("observations, not a storage guarantee", readme)
        assert_order(
            self,
            readme,
            "### 2. Register and verify the hook",
            "#### Codex hooks",
            "Register the hook with the effective paths",
            "bola install-hook\nbola doctor",
            "### 3. Capture a Codex turn",
            "## Command guide",
            "## Change paths later",
            "## Privacy and capture policy",
        )
        assert_order(
            self,
            readme,
            "## Privacy and capture policy",
            "## Measured storage footprint",
            "## Operations and analytics",
        )
        self.assertNotIn("cp hooks/token-usage.py", readme)

    def test_root_help_groups_common_and_advanced_commands(self) -> None:
        cli = load_module("root_help_command_spacing_test", ROOT / "scripts" / "bola.py")
        help_text = cli.build_parser().format_help()

        self.assertIn("usage: bola [-h] [--version] COMMAND ...", help_text)
        self.assertIn("COMMAND", help_text)
        self.assertNotIn("{reconcile, normalize, compact", help_text)
        self.assertIn("Common commands:\n  install-hook", help_text)
        self.assertIn("Advanced and recovery commands:\n  quarantine", help_text)
        self.assertEqual(help_text.count("Register the BOLA hook in a Codex directory"), 1)
        self.assertNotIn("[ install-hook,", help_text)

    def test_nested_command_help_uses_required_action_metavars(self) -> None:
        cli = load_module("nested_help_action_metavar_test", ROOT / "scripts" / "bola.py")
        parser = cli.build_parser()
        command_parsers = parser._subparsers._group_actions[0].choices

        quarantine_help = command_parsers["quarantine"].format_help()
        paths_help = command_parsers["paths"].format_help()

        self.assertIn("usage: bola quarantine", quarantine_help)
        self.assertIn("ACTION ...", quarantine_help)
        self.assertIn("usage: bola paths [-h] ACTION ...", paths_help)
        self.assertNotIn("{list,acknowledge}", quarantine_help)
        self.assertNotIn("{show,set,migrate}", paths_help)
        self.assertNotIn("[ list,", quarantine_help)
        self.assertNotIn("[ show,", paths_help)

    def test_service_paths_separate_codex_dir_and_default_user_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = pathlib.Path(tmp_dir) / ".codex"
            project_root = pathlib.Path(tmp_dir) / "checkout"
            data_home = pathlib.Path(tmp_dir) / "data-home"
            paths = service_paths.resolve_runtime_paths(
                codex_dir=codex_dir,
                env={"XDG_DATA_HOME": str(data_home)},
                config={},
                project_root=project_root,
            )

            self.assertEqual(paths.codex_dir, codex_dir)
            self.assertEqual(paths.output_dir, data_home / "bola")

    def test_output_layout_is_fixed_lazy_and_owner_only_for_temporary_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir) / "output"
            layout = service_paths.OutputLayout(root)

            self.assertEqual(layout.analytics_db, root / "analytics" / "bola.sqlite")
            self.assertEqual(layout.normalized_log, root / "normalized" / "prompt-usage.normalized.jsonl")
            self.assertEqual(layout.error_log, root / "prompt-usage-errors.jsonl")
            self.assertFalse(root.exists())

            tmp_path = service_paths.ensure_output_tmp_dir(root)

            self.assertEqual(tmp_path, root / "tmp")
            self.assertEqual(stat.S_IMODE(tmp_path.stat().st_mode), 0o700)

    def test_runtime_path_precedence_is_cli_environment_config_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            env = {
                "XDG_CONFIG_HOME": str(root / "config-home"),
                "CODEX_HOME": str(root / "env-codex"),
                "BOLA_OUTPUT_DIR": str(root / "env-data"),
            }
            config_path = service_paths.runtime_config_path(env)
            service_paths.write_config(
                {"codex_dir": root / "config-codex", "output_dir": root / "config-data"},
                config_path,
            )
            configured = service_paths.read_config(config_path)
            from_environment = service_paths.resolve_runtime_paths(env=env, config=configured, project_root=root / "project")
            from_cli = service_paths.resolve_runtime_paths(
                codex_dir=root / "cli-codex",
                output_dir=root / "cli-data",
                env=env,
                config=configured,
                project_root=root / "project",
            )

        self.assertEqual(from_environment.codex_dir, root / "env-codex")
        self.assertEqual(from_environment.output_dir, root / "env-data")
        self.assertEqual(from_cli.codex_dir, root / "cli-codex")
        self.assertEqual(from_cli.output_dir, root / "cli-data")

    def test_invalid_config_schema_fails_instead_of_using_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "runtime.conf"
            path.write_text("schema_version=99\ncodex_dir=/tmp/codex\noutput_dir=/tmp/output\n", encoding="utf-8")

            with self.assertRaises(service_paths.ConfigurationError):
                service_paths.read_config(path)

    def test_runtime_config_parser_accepts_comments_and_canonicalizes_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            path = root / "runtime.conf"
            path.write_text(
                "# BOLA runtime paths\n\n schema_version = 1 \n"
                f"codex_dir={root}/codex\noutput_dir={root}/output\n",
                encoding="utf-8",
            )

            configured = service_paths.read_config(path)

        self.assertEqual(configured["schema_version"], 1)
        self.assertEqual(configured["codex_dir"], str(root / "codex"))
        self.assertEqual(configured["output_dir"], str(root / "output"))

    def test_runtime_config_parser_rejects_invalid_structure(self) -> None:
        cases = {
            "missing": "schema_version=1\ncodex_dir=/tmp/codex\n",
            "duplicate": "schema_version=1\ncodex_dir=/tmp/a\ncodex_dir=/tmp/b\noutput_dir=/tmp/output\n",
            "unknown": "schema_version=1\ncodex_dir=/tmp/codex\noutput_dir=/tmp/output\nextra=true\n",
            "relative": "schema_version=1\ncodex_dir=relative\noutput_dir=/tmp/output\n",
            "malformed": "schema_version=1\ncodex_dir=/tmp/codex\noutput_dir\n",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "runtime.conf"
            for name, text in cases.items():
                with self.subTest(name=name):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(service_paths.ConfigurationError):
                        service_paths.read_config(path)

    def test_runtime_config_write_is_complete_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            path = root / "config" / "bola" / "runtime.conf"
            service_paths.write_config(
                {"codex_dir": root / "codex", "output_dir": root / "output"},
                path,
            )
            text = path.read_text(encoding="utf-8")
            directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
            file_mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(
            text,
            f"schema_version=1\ncodex_dir={root / 'codex'}\noutput_dir={root / 'output'}\n",
        )
        self.assertEqual(directory_mode, 0o700)
        self.assertEqual(file_mode, 0o600)

    def test_runtime_config_failed_replace_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            path = root / "runtime.conf"
            service_paths.write_config({"codex_dir": root / "a", "output_dir": root / "output-a"}, path)
            before = path.read_bytes()

            with (
                mock.patch.object(service_paths.os, "replace", side_effect=OSError("simulated replace failure")),
                self.assertRaises(OSError),
            ):
                service_paths.write_config({"codex_dir": root / "b", "output_dir": root / "output-b"}, path)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(root.glob(".runtime.conf.*.tmp")), [])

    def test_paths_show_names_runtime_config_explicitly(self) -> None:
        cli = load_module("paths_runtime_config_name_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_home = pathlib.Path(tmp_dir) / "config"
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=True):
                report = cli.paths_report()

        self.assertEqual(report["runtime_config_path"], str(config_home / "bola" / "runtime.conf"))
        self.assertEqual(report["effective"]["runtime_config_path"], str(config_home / "bola" / "runtime.conf"))
        self.assertFalse(report["exists"])

    def test_legacy_environment_name_fails_closed_with_mapping(self) -> None:
        cli = load_module("legacy_environment_name_test", ROOT / "scripts" / "bola.py")
        captured = io.StringIO()
        with (
            mock.patch.dict(cli.os.environ, {"CODEX_TOKEN_USAGE_DATA_ROOT": "/tmp/legacy"}, clear=True),
            mock.patch.object(cli.sys, "argv", ["bola", "paths", "show"]),
            mock.patch.object(cli.sys, "stdout", captured),
        ):
            code = cli.main()

        payload = json.loads(captured.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "legacy_name_unsupported")
        self.assertEqual(payload["mappings"], {"CODEX_TOKEN_USAGE_DATA_ROOT": "BOLA_OUTPUT_DIR"})

    def test_legacy_config_fails_closed_without_touching_neighbor_files(self) -> None:
        cli = load_module("legacy_config_name_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_home = pathlib.Path(tmp_dir) / "config"
            legacy = config_home / "codex-token-bola" / "config.json"
            neighbor = config_home / "codex-token-bola" / "github-app-keys" / "private.pem"
            legacy.parent.mkdir(parents=True)
            legacy.write_text('{"schema_version":2,"data_root":"/tmp/legacy"}\n', encoding="utf-8")
            neighbor.parent.mkdir(parents=True)
            neighbor.write_text("do-not-touch\n", encoding="utf-8")
            captured = io.StringIO()
            with (
                mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=True),
                mock.patch.object(cli.sys, "argv", ["bola", "paths", "show"]),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.main()

            payload = json.loads(captured.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "legacy_config_unsupported")
            self.assertEqual(payload["mappings"], {str(legacy): str(config_home / "bola" / "runtime.conf")})
            self.assertEqual(neighbor.read_text(encoding="utf-8"), "do-not-touch\n")

    def test_paths_set_switches_output_and_records_pending_migration(self) -> None:
        cli = load_module("paths_output_transition_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "source"
            target = root / "target"
            (source / "raw").mkdir(parents=True)
            (source / "raw" / "event.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                code = cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(target)), emit=False)
                configured = cli.service_paths.read_config()
                transition = cli.service_paths.read_path_transition()

        self.assertEqual(code, 0)
        self.assertEqual(pathlib.Path(str(configured["output_dir"])), target)
        self.assertEqual(pathlib.Path(str(transition["source_output_dir"])), source)
        self.assertEqual(pathlib.Path(str(transition["active_output_dir"])), target)

    def test_paths_set_hands_off_recovery_state_before_switch(self) -> None:
        cli = load_module("paths_state_handoff_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            target = root / "B"
            state_dir = source / "state"
            state_dir.mkdir(parents=True)
            state_path = state_dir / ("a" * 32 + ".json")
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "record_type": "turn_start",
                        "session_id": "session-1",
                        "turn_id": "turn-1",
                        "captured_at": "2026-08-24T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            persisted_phases = []
            finish_handoff = cli.paths_service.finish_transferred_state_handoff

            def inspect_persisted_phase(transition):
                persisted = cli.service_paths.load_path_transition()
                persisted_phases.append(persisted.phase.value if persisted is not None else None)
                return finish_handoff(transition)

            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                with mock.patch.object(cli.paths_service, "finish_transferred_state_handoff", side_effect=inspect_persisted_phase):
                    code = cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(target)), emit=False)
                configured = cli.service_paths.read_config()

            self.assertEqual(code, 0)
            self.assertEqual(persisted_phases, ["preparing"])
            self.assertFalse(state_path.exists())
            self.assertTrue((target / "state" / state_path.name).exists())
            self.assertEqual(pathlib.Path(str(configured["output_dir"])), target)

    def test_turn_started_in_old_output_completes_in_new_output(self) -> None:
        cli = load_module("paths_live_turn_handoff_cli_test", ROOT / "scripts" / "bola.py")
        hook = load_module("paths_live_turn_handoff_hook_test", ROOT / "scripts" / "hook.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            target = root / "B"
            transcript = root / "rollout.jsonl"
            session_id = "session-live"
            turn_id = "turn-live"

            def event(payload: dict[str, object]) -> str:
                return json.dumps({"timestamp": "2026-08-24T00:00:00.000Z", "type": "event_msg", "payload": payload}) + "\n"

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
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                hook.configure_runtime_paths()
                hook.handle_start(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "transcript_path": str(transcript),
                        "prompt": "continue live turn",
                    }
                )
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(target)), emit=False)
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write(event({"type": "task_started", "turn_id": turn_id}))
                    handle.write(
                        event(
                            {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
                                    "last_token_usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
                                },
                            }
                        )
                    )
                    handle.write(event({"type": "task_complete", "turn_id": turn_id}))
                hook.configure_runtime_paths()
                hook.handle_stop(
                    {
                        "hook_event_name": "Stop",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "transcript_path": str(transcript),
                    }
                )
                current = hook.raw_segments.strict_read_current_pointer(target)["current"]["prompt_usage"]
                rows = [json.loads(line) for line in pathlib.Path(current["path"]).read_text(encoding="utf-8").splitlines()]

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["turn_status"], "completed")
            self.assertTrue(rows[0]["start_state_found"])
            self.assertNotEqual(rows[0]["lifecycle_end_reason"], "missing_start_state")
            self.assertEqual(rows[0]["usage"]["total_tokens"], 13)

    def test_paths_set_recovers_preparing_transition_on_either_side_of_config_commit(self) -> None:
        cli = load_module("paths_preparing_recovery_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            target = root / "B"
            name = "turn.json"
            for directory in (source / "state", target / "state"):
                directory.mkdir(parents=True)
                (directory / name).write_text('{"record_type":"turn_start"}\n', encoding="utf-8")
            transition = cli.output_transition_payload(source, target, phase="preparing")
            transition["transferred_state_files"] = [name]
            transition["created_state_files"] = [name]
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.service_paths.write_path_transition(transition)
                self.assertIsNone(cli.recover_preparing_path_transition())
                self.assertFalse((target / "state" / name).exists())

                (target / "state" / name).write_text((source / "state" / name).read_text(encoding="utf-8"), encoding="utf-8")
                cli.service_paths.write_path_transition(transition)
                cli.service_paths.write_config({"output_dir": target})
                recovered = cli.recover_preparing_path_transition()

            self.assertEqual(recovered["phase"], "pending")
            self.assertFalse((source / "state" / name).exists())
            self.assertTrue((target / "state" / name).exists())

    def test_paths_set_recovery_retries_pending_persist_after_handoff_cleanup(self) -> None:
        cli = load_module("paths_handoff_pending_persist_recovery_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            target = root / "B"
            name = "turn.json"
            for directory in (source / "state", target / "state"):
                directory.mkdir(parents=True)
                (directory / name).write_text('{"record_type":"turn_start"}\n', encoding="utf-8")
            transition = cli.output_transition_payload(source, target, phase="preparing")
            transition["transferred_state_files"] = [name]
            transition["created_state_files"] = [name]

            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": target})
                cli.service_paths.write_path_transition(transition)
                write_transition = cli.paths_service.service_paths.write_path_transition

                def fail_pending_persist(payload, path=None):
                    typed = payload if isinstance(payload, cli.service_paths.PathTransition) else cli.service_paths.PathTransition.from_payload(payload)
                    if typed.phase is cli.service_paths.PathTransitionPhase.PENDING:
                        raise OSError("simulated pending persist failure")
                    return write_transition(payload, path)

                with mock.patch.object(cli.paths_service.service_paths, "write_path_transition", side_effect=fail_pending_persist):
                    with self.assertRaisesRegex(OSError, "simulated pending persist failure"):
                        cli.recover_preparing_path_transition()

                persisted = cli.service_paths.load_path_transition()
                self.assertEqual(persisted.phase, cli.service_paths.PathTransitionPhase.PREPARING)
                self.assertFalse((source / "state" / name).exists())
                recovered = cli.recover_preparing_path_transition()

            self.assertEqual(recovered["phase"], "pending")
            self.assertFalse((source / "state" / name).exists())
            self.assertTrue((target / "state" / name).exists())

    def test_paths_set_rejects_invalid_output_target_without_mutating_config(self) -> None:
        cli = load_module("paths_invalid_output_target_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            invalid = root / "not-a-directory"
            invalid.write_text("file\n", encoding="utf-8")
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                with self.assertRaises(cli.service_paths.ConfigurationError):
                    cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(invalid)), emit=False)
                configured = cli.service_paths.read_config()

            self.assertEqual(pathlib.Path(str(configured["output_dir"])), source)

    def test_paths_set_refuses_to_switch_while_source_service_is_locked(self) -> None:
        cli = load_module("paths_source_service_lock_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            target = root / "B"
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                with cli.service_lock.acquire_service_lock(output_dir=source):
                    with self.assertRaises(cli.service_lock.ServiceLockBusy):
                        cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(target)), emit=False)
                configured = cli.service_paths.read_config()

            self.assertEqual(pathlib.Path(str(configured["output_dir"])), source)

    def test_paths_set_restores_handed_off_state_when_post_commit_step_fails(self) -> None:
        cli = load_module("paths_state_handoff_rollback_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            target = root / "B"
            state_path = source / "state" / "turn.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps({"record_type": "turn_start", "session_id": "session", "turn_id": "turn"}),
                encoding="utf-8",
            )
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                with mock.patch.object(cli, "managed_content_files", side_effect=OSError("scan failed")):
                    with self.assertRaises(OSError):
                        cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(target)), emit=False)
                configured = cli.service_paths.read_config()
                transition = cli.service_paths.read_path_transition()

            self.assertEqual(pathlib.Path(str(configured["output_dir"])), source)
            self.assertTrue(state_path.exists())
            self.assertFalse((target / "state" / state_path.name).exists())
            self.assertIsNone(transition)

    def test_paths_set_allows_direct_rollback_but_rejects_third_output(self) -> None:
        cli = load_module("paths_direct_rollback_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            active = root / "B"
            third = root / "C"
            (source / "raw").mkdir(parents=True)
            (source / "raw" / "old.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(active)), emit=False)
                with self.assertRaises(cli.service_paths.ConfigurationError):
                    cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(third)), emit=False)
                (active / "raw").mkdir(parents=True)
                (active / "raw" / "new.jsonl").write_text("{}\n", encoding="utf-8")
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(source)), emit=False)
                transition = cli.service_paths.read_path_transition()
                configured = cli.service_paths.read_config()

            self.assertEqual(pathlib.Path(str(configured["output_dir"])), source)
            self.assertEqual(pathlib.Path(str(transition["source_output_dir"])), active)
            self.assertEqual(pathlib.Path(str(transition["active_output_dir"])), source)

    def test_paths_migrate_imports_raw_into_nonempty_active_output(self) -> None:
        cli = load_module("paths_merge_migration_test", ROOT / "scripts" / "bola.py")
        row = {
            "schema_version": 2,
            "record_type": "turn_usage_raw",
            "session_id": "session-old",
            "turn_id": "turn-old",
            "captured_at": "2026-08-24T00:00:00+00:00",
            "started_at": "2026-08-24T00:00:00+00:00",
            "stopped_at": "2026-08-24T00:01:00+00:00",
            "turn_status": "completed",
            "estimated": False,
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0, "total_tokens": 3},
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            active = root / "B"
            (source / "raw").mkdir(parents=True)
            (source / "raw" / "old.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            (active / "reports").mkdir(parents=True)
            (active / "reports" / "new.txt").write_text("keep\n", encoding="utf-8")
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(active)), emit=False)
                with mock.patch.object(cli, "run_script_json", return_value=(0, {"status": "healthy"}, "", "")):
                    code, result = cli.apply_output_migration(source, active, cli.service_paths.read_path_transition())
                manifest = cli.raw_segments.strict_read_manifest(active)
                transition = cli.service_paths.read_path_transition()

            self.assertEqual(code, 0)
            self.assertEqual(result["imported_rows"], 1)
            self.assertEqual(len(manifest["segments"]), 1)
            self.assertTrue((active / "reports" / "new.txt").exists())
            self.assertFalse((source / "raw").exists())
            self.assertIsNone(transition)

    def test_paths_migrate_excludes_root_error_log_from_raw_sources(self) -> None:
        cli = load_module("paths_migration_source_filter_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = pathlib.Path(tmp_dir) / "A"
            source.mkdir(parents=True)
            raw_log = source / "prompt-usage.raw.jsonl"
            error_log = source / "prompt-usage-errors.jsonl"
            raw_log.write_text("{}\n", encoding="utf-8")
            error_log.write_text('{"error":"append_failed"}\n', encoding="utf-8")

            sources = cli.raw_migration_sources(source)

        self.assertEqual(sources, [raw_log.resolve()])

    def test_paths_migrate_accepts_only_explicit_degraded_exit_one(self) -> None:
        cli = load_module("paths_migration_process_result_test", ROOT / "scripts" / "bola.py")

        degraded = cli.ProcessResult(
            command=cli.RuntimeCommand.RECONCILE,
            exit_code=1,
            payload={"status": "degraded"},
        )
        self.assertEqual(
            cli.paths_service.require_migration_process_result(degraded, allow_degraded=True),
            {"status": "degraded"},
        )

        for result in (
            cli.ProcessResult(command=cli.RuntimeCommand.RECONCILE, exit_code=1, payload={"status": "failed"}),
            cli.ProcessResult(command=cli.RuntimeCommand.RECONCILE, exit_code=1, payload=None, parse_error="stdout_empty"),
            cli.ProcessResult(command=cli.RuntimeCommand.RECONCILE, exit_code=0, payload={"status": "failed"}),
        ):
            with self.subTest(result=result):
                with self.assertRaises(cli.service_paths.ConfigurationError):
                    cli.paths_service.require_migration_process_result(result, allow_degraded=True)

    def test_paths_migrate_failed_reconcile_preserves_source_and_requires_recovery(self) -> None:
        cli = load_module("paths_failed_reconcile_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            active = root / "B"
            source_raw = source / "raw" / "old.jsonl"
            source_raw.parent.mkdir(parents=True)
            source_raw.write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(active)), emit=False)
                with mock.patch.object(
                    cli,
                    "run_script_json",
                    return_value=(1, {"status": "failed", "error": "write_failed"}, "", ""),
                ):
                    with self.assertRaises(cli.service_paths.ConfigurationError):
                        cli.apply_output_migration(source, active, cli.service_paths.read_path_transition())
                transition = cli.service_paths.read_path_transition()

            self.assertTrue(source_raw.exists())
            self.assertFalse((active / "raw").exists())
            self.assertEqual(transition["phase"], "recovery_required")

    def test_paths_migrate_failed_normalize_skips_build_and_preserves_source(self) -> None:
        cli = load_module("paths_failed_normalize_test", ROOT / "scripts" / "bola.py")
        calls: list[str] = []

        def fail_normalize(script_name: str, _args: list[str], **_kwargs: object) -> tuple[int, dict[str, object], str, str]:
            calls.append(script_name)
            if script_name == "normalize.py":
                return 1, {"status": "failed", "error": "normalize_failed"}, "", ""
            return 0, {"status": "healthy"}, "", ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            active = root / "B"
            source_raw = source / "raw" / "old.jsonl"
            source_raw.parent.mkdir(parents=True)
            source_raw.write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(active)), emit=False)
                with mock.patch.object(cli, "run_script_json", side_effect=fail_normalize):
                    with self.assertRaises(cli.service_paths.ConfigurationError):
                        cli.apply_output_migration(source, active, cli.service_paths.read_path_transition())
                transition = cli.service_paths.read_path_transition()

            self.assertTrue(source_raw.exists())
            self.assertNotIn("build_analytics.py", calls)
            self.assertEqual(transition["phase"], "recovery_required")

    def test_paths_migrate_blocks_unresolved_physical_retention_deletion(self) -> None:
        cli = load_module("paths_pending_physical_delete_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = pathlib.Path(tmp_dir) / "A"
            pending_segments = [{"id": f"old-{index}", "path": str(source / "raw" / "archive" / f"old-{index}.jsonl.gz")} for index in range(25)]
            sweep = {
                "deleted_files": 0,
                "pending_files": len(pending_segments),
                "pending_source_segments": pending_segments,
                "errors": [{"path": item["path"], "error": "busy"} for item in pending_segments],
            }
            marker = {"phase": "unlink_pending", "unlink_pending_segments": pending_segments}
            with (
                mock.patch.object(cli.raw_segments, "sweep_apply_marker", return_value=sweep),
                mock.patch.object(cli.raw_segments, "read_apply_marker", return_value=marker),
            ):
                with self.assertRaises(cli.PathMigrationBlocked) as raised:
                    cli.raw_migration_sources(source)

        payload = raised.exception.payload()
        self.assertEqual(payload["error"], "source_physical_delete_pending")
        self.assertEqual(payload["pending_files"], 25)
        self.assertEqual(len(payload["pending_paths"]), 20)
        self.assertTrue(payload["pending_paths_truncated"])
        self.assertTrue(payload["retryable"])

    def test_paths_migrate_physical_delete_blocker_keeps_pending_transition(self) -> None:
        cli = load_module("paths_pending_transition_retention_delete_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            active = root / "B"
            (source / "raw").mkdir(parents=True)
            (source / "raw" / "old.jsonl").write_text("{}\n", encoding="utf-8")
            blocker = cli.PathMigrationBlocked(
                {
                    "status": "blocked",
                    "migrated": False,
                    "error": "source_physical_delete_pending",
                    "pending_files": 1,
                    "retryable": True,
                }
            )
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(active)), emit=False)
                transition = cli.service_paths.read_path_transition()
                with mock.patch.object(cli, "resolve_source_physical_deletes", side_effect=blocker):
                    with self.assertRaises(cli.PathMigrationBlocked):
                        cli.apply_output_migration(source, active, transition)
                current = cli.service_paths.read_path_transition()

            self.assertEqual(current["phase"], "pending")
            self.assertTrue((source / "raw" / "old.jsonl").exists())
            self.assertFalse((active / "raw").exists())

    def test_paths_migrate_prints_structured_physical_delete_blocker(self) -> None:
        cli = load_module("paths_pending_delete_payload_test", ROOT / "scripts" / "bola.py")
        blocker = cli.PathMigrationBlocked(
            {
                "status": "blocked",
                "migrated": False,
                "error": "source_physical_delete_pending",
                "pending_files": 1,
                "pending_paths": ["/old/segment.jsonl.gz"],
                "pending_paths_truncated": False,
                "retryable": True,
            }
        )
        with (
            mock.patch.object(cli, "pending_output_migration", return_value=(None, pathlib.Path("/old"), pathlib.Path("/new"))),
            mock.patch.object(cli, "output_migration_preview", side_effect=blocker),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.paths_migrate(argparse.Namespace(output_dir=True, apply=False))

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"], "source_physical_delete_pending")

    def test_paths_migrate_merges_retention_pruned_turn_state(self) -> None:
        cli = load_module("paths_retention_pruned_merge_test", ROOT / "scripts" / "bola.py")

        def state_payload(session_id: str, turn_id: str, cutoff: float) -> dict[str, object]:
            return {
                "schema_version": 1,
                "cutoff_unix": cutoff,
                "updated_at_unix": cutoff,
                "pruned_turns": [{"session_id": session_id, "turn_id": turn_id, "captured_at_unix": cutoff}],
            }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            active = root / "B"
            (source / "raw").mkdir(parents=True)
            (source / "raw" / "old.jsonl").write_text("{}\n", encoding="utf-8")
            (source / "state").mkdir(parents=True)
            (active / "state").mkdir(parents=True)
            (source / "state" / "retention-pruned-turns.json").write_text(
                json.dumps(state_payload("source-session", "source-turn", 10.0)) + "\n",
                encoding="utf-8",
            )
            (active / "state" / "retention-pruned-turns.pending.json").write_text(
                json.dumps(state_payload("active-session", "active-turn", 20.0)) + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(active)), emit=False)
                with mock.patch.object(cli, "run_script_json", return_value=(0, {"status": "healthy"}, "", "")):
                    code, result = cli.apply_output_migration(source, active, cli.service_paths.read_path_transition())
                retention_db = active / "state" / "retention-pruned-turns.sqlite"
                with sqlite3.connect(retention_db) as con:
                    merged_rows = con.execute("select session_id, turn_id, state from pruned_turns order by session_id, turn_id").fetchall()
                pending_exists = (active / "state" / "retention-pruned-turns.pending.json").exists()

        self.assertEqual(code, 0)
        self.assertEqual(result["retention_pruned_turns"], {"source_rows": 1, "destination_rows": 1, "merged_rows": 2, "deduplicated_rows": 0})
        self.assertEqual(
            merged_rows,
            [("active-session", "active-turn", "committed"), ("source-session", "source-turn", "committed")],
        )
        self.assertFalse(pending_exists)

    def test_paths_migrate_blocks_conflicting_retention_pruned_turn_state(self) -> None:
        cli = load_module("paths_retention_pruned_conflict_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            active = root / "B"
            for directory, captured_at in ((source, 10.0), (active, 20.0)):
                (directory / "state").mkdir(parents=True)
                (directory / "state" / "retention-pruned-turns.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "cutoff_unix": captured_at,
                            "updated_at_unix": captured_at,
                            "pruned_turns": [{"session_id": "same-session", "turn_id": "same-turn", "captured_at_unix": captured_at}],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            (source / "raw").mkdir(parents=True)
            (source / "raw" / "old.jsonl").write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(active)), emit=False)
                transition = cli.service_paths.read_path_transition()
                with self.assertRaises(cli.PathMigrationBlocked) as raised:
                    cli.apply_output_migration(source, active, transition)
                current = cli.service_paths.read_path_transition()
                source_raw_exists = (source / "raw" / "old.jsonl").exists()
                active_raw_exists = (active / "raw").exists()

        payload = raised.exception.payload()
        self.assertEqual(payload["error"], "retention_pruned_turn_conflict")
        self.assertEqual(payload["conflicts"], 1)
        self.assertFalse(payload["retryable"])
        self.assertEqual(current["phase"], "pending")
        self.assertTrue(source_raw_exists)
        self.assertFalse(active_raw_exists)

    def test_paths_migrate_retries_staged_retention_pruned_turn_state_after_build_failure(self) -> None:
        cli = load_module("paths_retention_pruned_retry_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = root / "A"
            active = root / "B"
            (source / "raw").mkdir(parents=True)
            (source / "raw" / "old.jsonl").write_text("{}\n", encoding="utf-8")
            (source / "state").mkdir(parents=True)
            (source / "state" / "retention-pruned-turns.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cutoff_unix": 10.0,
                        "updated_at_unix": 10.0,
                        "pruned_turns": [{"session_id": "source", "turn_id": "turn", "captured_at_unix": 10.0}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            def fail_build(script_name: str, _args: list[str], **_kwargs: object) -> tuple[int, dict[str, object], str, str]:
                if script_name == "build_analytics.py":
                    return 2, {"status": "failed"}, "", ""
                return 0, {"status": "healthy"}, "", ""

            with mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(root / "config")}, clear=True):
                cli.service_paths.write_config({"output_dir": source})
                cli.paths_set(argparse.Namespace(codex_dir=None, output_dir=str(active)), emit=False)
                with mock.patch.object(cli, "run_script_json", side_effect=fail_build):
                    with self.assertRaises(cli.service_paths.ConfigurationError):
                        cli.apply_output_migration(source, active, cli.service_paths.read_path_transition())
                retention_db = active / "state" / "retention-pruned-turns.sqlite"
                with sqlite3.connect(retention_db) as con:
                    pending_after_failure = con.execute("select count(*) from pruned_turns where state='pending'").fetchone()[0]
                transition_after_failure = cli.service_paths.read_path_transition()

                with mock.patch.object(cli, "run_script_json", return_value=(0, {"status": "healthy"}, "", "")):
                    code, _result = cli.apply_output_migration(source, active, transition_after_failure)
                final_after_retry = retention_db.exists()
                with sqlite3.connect(retention_db) as con:
                    pending_after_retry = con.execute("select count(*) from pruned_turns where state='pending'").fetchone()[0]
                transition_after_retry = cli.service_paths.read_path_transition()

        self.assertEqual(pending_after_failure, 1)
        self.assertEqual(transition_after_failure["phase"], "recovery_required")
        self.assertEqual(code, 0)
        self.assertTrue(final_after_retry)
        self.assertEqual(pending_after_retry, 0)
        self.assertIsNone(transition_after_retry)

    def test_temporary_migration_entrypoints_are_removed(self) -> None:
        cli = load_module("removed_temporary_migration_test", ROOT / "scripts" / "bola.py")

        self.assertFalse(hasattr(cli, "migrate_data"))
        self.assertFalse(hasattr(cli, "migrate_path"))

    def test_paths_show_ignores_legacy_data(self) -> None:
        cli = load_module("paths_legacy_migration_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = pathlib.Path(tmp_dir) / ".codex"
            (codex_dir / "token-usage" / "state").mkdir(parents=True)
            (codex_dir / "token-usage" / "state" / "pending.json").write_text("{}\n", encoding="utf-8")
            output_dir = pathlib.Path(tmp_dir) / "data"
            with mock.patch.dict(
                cli.os.environ,
                {"CODEX_HOME": str(codex_dir), "BOLA_OUTPUT_DIR": str(output_dir)},
                clear=True,
            ):
                report = cli.paths_report()

            self.assertFalse(report["output_transition"]["pending"])

    def test_hook_keeps_writing_active_output_when_legacy_data_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            codex_dir = root / ".codex"
            output_dir = root / "data"
            (codex_dir / "token-usage" / "state").mkdir(parents=True)
            stderr = io.StringIO()
            stdout = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_dir), "BOLA_OUTPUT_DIR": str(output_dir)},
                clear=False,
            ):
                hook = load_module("hook_migration_required_test", ROOT / "scripts" / "hook.py")
                with (
                    mock.patch.object(
                        hook.sys,
                        "stdin",
                        io.StringIO(
                            json.dumps(
                                {
                                    "hook_event_name": "Stop",
                                    "session_id": "s1",
                                    "turn_id": "t1",
                                    "transcript_path": str(root / "missing.jsonl"),
                                }
                            )
                        ),
                    ),
                    mock.patch.object(hook.sys, "stderr", stderr),
                    mock.patch.object(hook.sys, "stdout", stdout),
                ):
                    code = hook.main()

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertTrue(hook.service_paths.has_managed_data(output_dir))
            self.assertFalse((codex_dir / "codex-token-bola-migration-required.jsonl").exists())

    def test_codex_dir_status_requires_existing_initialized_writable_directory(self) -> None:
        cli = load_module("codex_dir_status_contract_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            missing = cli.codex_dir_status(root / "missing")
            regular_file = root / "regular-file"
            regular_file.write_text("not a directory\n", encoding="utf-8")
            wrong_type = cli.codex_dir_status(regular_file)
            empty = root / "empty"
            empty.mkdir()
            uninitialized = cli.codex_dir_status(empty)
            initialized = self.initialize_codex_dir(root / "initialized")
            valid = cli.codex_dir_status(initialized)
            with mock.patch.object(cli.os, "access", return_value=False):
                unwritable = cli.codex_dir_status(initialized)

        self.assertEqual(missing["reason"], "not_found")
        self.assertEqual(wrong_type["reason"], "not_directory")
        self.assertEqual(uninitialized["reason"], "not_initialized")
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["markers"], ["config.toml"])
        self.assertEqual(unwritable["reason"], "not_writable")

    def test_codex_dir_status_accepts_supported_markers_and_dir_symlink(self) -> None:
        cli = load_module("codex_dir_marker_contract_test", ROOT / "scripts" / "bola.py")
        markers = ("config.toml", "auth.json", "state_5.sqlite", "history.jsonl", "sessions")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            for marker in markers:
                with self.subTest(marker=marker):
                    codex_dir = root / marker.replace(".", "-")
                    codex_dir.mkdir()
                    target = codex_dir / marker
                    if marker == "sessions":
                        target.mkdir()
                    else:
                        target.write_text("{}\n", encoding="utf-8")
                    status = cli.codex_dir_status(codex_dir)
                    self.assertTrue(status["valid"])
                    self.assertEqual(status["markers"], [marker])

            symlink = root / "linked-dir"
            symlink.symlink_to(root / "config-toml", target_is_directory=True)
            self.assertTrue(cli.codex_dir_status(symlink)["valid"])

    def test_codex_dir_status_rejects_invalid_hooks_json(self) -> None:
        cli = load_module("codex_dir_hooks_contract_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = self.initialize_codex_dir(pathlib.Path(tmp_dir) / ".codex")
            hooks_path = codex_dir / "hooks.json"
            hooks_path.write_text("{broken", encoding="utf-8")
            malformed = cli.codex_dir_status(codex_dir)
            hooks_path.write_text("[]\n", encoding="utf-8")
            wrong_shape = cli.codex_dir_status(codex_dir)

        self.assertEqual(malformed["reason"], "hooks_json_invalid")
        self.assertEqual(wrong_shape["reason"], "hooks_json_invalid")

    def test_codex_cli_status_reports_missing_timeout_and_execution_failure(self) -> None:
        cli = load_module("codex_cli_status_contract_test", ROOT / "scripts" / "bola.py")
        with mock.patch.object(cli.shutil, "which", return_value=None):
            missing = cli.codex_cli_status()
        with (
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(cli.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 5)),
        ):
            timeout = cli.codex_cli_status()
        failed_result = subprocess.CompletedProcess(["codex", "--version"], 1, stdout="", stderr="broken\n")
        with (
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(cli.subprocess, "run", return_value=failed_result),
        ):
            failed = cli.codex_cli_status()

        self.assertEqual(missing["reason"], "not_found")
        self.assertEqual(timeout["reason"], "timeout")
        self.assertEqual(failed["reason"], "execution_failed")

    def test_install_hook_rejects_invalid_home_before_any_mutation(self) -> None:
        cli = load_module("install_hook_invalid_home_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            codex_dir = root / "missing-codex-dir"
            output_dir = root / "output"
            config_home = root / "config"
            captured = io.StringIO()
            with (
                mock.patch.dict(
                    cli.os.environ,
                    {"XDG_CONFIG_HOME": str(config_home), "BOLA_OUTPUT_DIR": str(output_dir)},
                    clear=True,
                ),
                mock.patch.object(cli.sys, "argv", ["bola.py", "install-hook", "--codex-dir", str(codex_dir), "--output-dir", str(output_dir)]),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.main()

            payload = json.loads(captured.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "codex_dir_invalid")
            self.assertEqual(payload["reason"], "not_found")
            self.assertFalse(codex_dir.exists())
            self.assertFalse(output_dir.exists())
            self.assertFalse((config_home / "bola" / "runtime.conf").exists())

    def test_install_hook_rejects_missing_codex_cli_before_any_mutation(self) -> None:
        cli = load_module("install_hook_invalid_cli_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            codex_dir = self.initialize_codex_dir(root / ".codex")
            config_home = root / "config"
            captured = io.StringIO()
            with (
                mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=True),
                mock.patch.object(cli.shutil, "which", return_value=None),
                mock.patch.object(cli.sys, "argv", ["bola.py", "install-hook", "--codex-dir", str(codex_dir)]),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.main()

            payload = json.loads(captured.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "codex_cli_invalid")
            self.assertEqual(payload["reason"], "not_found")
            self.assertFalse((codex_dir / "hooks.json").exists())
            self.assertFalse((config_home / "bola" / "runtime.conf").exists())

    def test_hook_runtime_status_checks_import_outside_checkout_without_pythonpath(self) -> None:
        cli = load_module("hook_runtime_status_test", ROOT / "scripts" / "bola.py")
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="not importable")
        with (
            mock.patch.dict(cli.os.environ, {"PYTHONPATH": str(ROOT)}, clear=False),
            mock.patch.object(cli.subprocess, "run", return_value=completed) as run,
        ):
            status = cli.hook_runtime_status()

        call = run.call_args
        self.assertEqual(call.args[0], [cli.os.path.abspath(cli.os.path.expanduser(cli.sys.executable)), "-c", "import codex_token_bola.hook"])
        self.assertNotIn("PYTHONPATH", call.kwargs["env"])
        self.assertNotEqual(pathlib.Path(call.kwargs["cwd"]).resolve(), ROOT.resolve())
        self.assertFalse(status["valid"])
        self.assertEqual(status["reason"], "module_not_importable")

    def test_install_hook_rejects_unimportable_runtime_before_any_mutation(self) -> None:
        cli = load_module("install_hook_invalid_runtime_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            codex_dir = self.initialize_codex_dir(root / ".codex")
            config_home = root / "config"
            captured = io.StringIO()
            invalid_runtime = {
                "valid": False,
                "interpreter": cli.sys.executable,
                "module": "codex_token_bola.hook",
                "reason": "module_not_importable",
                "message": "install the package first",
            }
            with (
                mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=True),
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli, "hook_runtime_status", return_value=invalid_runtime),
                mock.patch.object(cli.sys, "argv", ["bola.py", "install-hook", "--codex-dir", str(codex_dir)]),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.main()

            payload = json.loads(captured.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "hook_runtime_invalid")
            self.assertEqual(payload["reason"], "module_not_importable")
            self.assertFalse((codex_dir / "hooks.json").exists())
            self.assertFalse((config_home / "bola" / "runtime.conf").exists())

    def test_paths_set_rejects_uninitialized_codex_dir_without_writing_config(self) -> None:
        cli = load_module("paths_set_invalid_codex_dir_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            codex_dir = root / ".codex"
            codex_dir.mkdir()
            source = root / "source"
            target = root / "target"
            (source / "raw").mkdir(parents=True)
            (source / "raw" / "event.jsonl").write_text("{}\n", encoding="utf-8")
            config_home = root / "config"
            captured = io.StringIO()
            with (
                mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(config_home)}, clear=True),
                mock.patch.object(
                    cli.sys,
                    "argv",
                    [
                        "bola.py",
                        "paths",
                        "set",
                        "--codex-dir",
                        str(codex_dir),
                        "--output-dir",
                        str(target),
                    ],
                ),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                cli.service_paths.write_config({"output_dir": source})
                code = cli.main()
                configured = cli.service_paths.read_config()

            payload = json.loads(captured.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "codex_dir_invalid")
            self.assertEqual(payload["reason"], "not_initialized")
            self.assertEqual(pathlib.Path(str(configured["output_dir"])), source)
            self.assertFalse(target.exists())

    def test_install_hook_accepts_missing_output_dir_and_persists_it(self) -> None:
        cli = load_module("install_hook_output_dir_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            codex_dir = self.initialize_codex_dir(root / ".codex")
            output_dir = root / "not-created-yet"
            config_home = root / "config"
            captured = io.StringIO()
            with (
                mock.patch.dict(
                    cli.os.environ,
                    {"XDG_CONFIG_HOME": str(config_home), "BOLA_OUTPUT_DIR": str(output_dir)},
                    clear=True,
                ),
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli, "hook_runtime_status", return_value=self.valid_hook_runtime_status()),
                mock.patch.object(cli.sys, "argv", ["bola.py", "install-hook", "--codex-dir", str(codex_dir), "--output-dir", str(output_dir)]),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.main()

            configured = cli.service_paths.read_config(config_home / "bola" / "runtime.conf")
            self.assertEqual(code, 0)
            self.assertEqual(configured["codex_dir"], str(codex_dir))
            self.assertEqual(configured["output_dir"], str(output_dir))
            self.assertFalse(output_dir.exists())
            self.assertTrue((codex_dir / "hooks.json").exists())

    def test_install_hook_restores_hooks_when_runtime_config_write_fails(self) -> None:
        cli = load_module("install_hook_config_rollback_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            codex_dir = self.initialize_codex_dir(root / ".codex")
            output_dir = root / "output"
            config_home = root / "config"
            hooks_path = codex_dir / "hooks.json"
            original = '{"hooks":{"Notification":[{"command":"keep"}]}}\n'
            hooks_path.write_text(original, encoding="utf-8")
            captured = io.StringIO()
            with (
                mock.patch.dict(
                    cli.os.environ,
                    {
                        "XDG_CONFIG_HOME": str(config_home),
                        "CODEX_HOME": str(codex_dir),
                        "BOLA_OUTPUT_DIR": str(output_dir),
                    },
                    clear=True,
                ),
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli, "hook_runtime_status", return_value=self.valid_hook_runtime_status()),
                mock.patch.object(
                    cli.service_paths,
                    "write_config",
                    side_effect=cli.service_paths.ConfigurationError("simulated config write failure"),
                ),
                mock.patch.object(cli.sys, "argv", ["bola.py", "install-hook"]),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.main()
            final_hooks = hooks_path.read_text(encoding="utf-8")
            config_exists = (config_home / "bola" / "runtime.conf").exists()

        self.assertEqual(code, 2)
        self.assertEqual(final_hooks, original)
        self.assertFalse(config_exists)

    def test_install_hook_registers_repo_hook_and_keeps_hooks_json_owner_only(self) -> None:
        cli = load_module("install_hook_cli_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = self.initialize_codex_dir(pathlib.Path(tmp_dir) / ".codex")
            with (
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli, "hook_runtime_status", return_value=self.valid_hook_runtime_status()),
            ):
                result = cli.install_hook(argparse.Namespace(codex_dir=str(codex_dir)))
            hooks_json = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
            status = cli.hooks_json_status(codex_dir)

            self.assertEqual(result["installed_hook"], "codex_token_bola.hook")
            self.assertEqual(shlex.split(result["command"]), [cli.sys.executable, "-m", "codex_token_bola.hook", "--bola-hook"])
            self.assertEqual(stat.S_IMODE((codex_dir / "hooks.json").stat().st_mode), 0o600)
            self.assertIn("hooks_json", result)
            self.assertTrue(status["events"]["UserPromptSubmit"]["registered"])
            self.assertTrue(status["events"]["Stop"]["registered"])
            self.assertIn("hooks", hooks_json)

    def test_install_hook_preserves_existing_hooks_and_deduplicates_registration(self) -> None:
        cli = load_module("install_hook_merge_hooks_json_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = self.initialize_codex_dir(pathlib.Path(tmp_dir) / ".codex")
            hooks_path = codex_dir / "hooks.json"
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

            with (
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli, "hook_runtime_status", return_value=self.valid_hook_runtime_status()),
            ):
                first = cli.install_hook(argparse.Namespace(codex_dir=str(codex_dir)))
                second = cli.install_hook(argparse.Namespace(codex_dir=str(codex_dir)))
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
            nested["command"] for entry in parsed["hooks"]["Stop"] for nested in entry.get("hooks", []) if isinstance(nested, dict) and nested.get("command")
        ]
        self.assertIn("python3 /tmp/existing.py", user_commands)
        self.assertIn("python3 /tmp/existing-stop.py", stop_commands)
        self.assertEqual(sum("codex_token_bola.hook" in command for command in user_commands), 1)
        self.assertEqual(sum("codex_token_bola.hook" in command for command in stop_commands), 1)

    def test_install_hook_replaces_stale_owned_checkout_registration(self) -> None:
        cli = load_module("install_hook_stale_checkout_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = self.initialize_codex_dir(pathlib.Path(tmp_dir) / ".codex")
            hooks_path = codex_dir / "hooks.json"
            stale = "python3 /old/checkout/hooks/token-usage.py --codex-token-bola-hook"
            unrelated = "python3 /tmp/unrelated.py"
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {"hooks": [{"type": "command", "command": stale}]},
                                {"hooks": [{"type": "command", "command": unrelated}]},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli, "hook_runtime_status", return_value=self.valid_hook_runtime_status()),
            ):
                result = cli.install_hook(argparse.Namespace(codex_dir=str(codex_dir)))
            commands = result["hooks_json"]["events"]["Stop"]["commands"]

        self.assertNotIn(stale, commands)
        self.assertIn(unrelated, commands)
        self.assertIn(cli.hook_command(), commands)

    def test_install_hook_uses_exact_interpreter_and_module(self) -> None:
        cli = load_module("install_hook_quoted_command_test", ROOT / "scripts" / "bola.py")
        command = cli.hook_command()

        self.assertEqual(shlex.split(command), [cli.sys.executable, "-m", "codex_token_bola.hook", "--bola-hook"])

    def test_install_hook_does_not_dedupe_unrelated_command_containing_hook_path(self) -> None:
        cli = load_module("install_hook_substring_dedupe_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = self.initialize_codex_dir(pathlib.Path(tmp_dir) / ".codex")
            installed = codex_dir / "hooks" / "token-usage.py"
            hooks_path = codex_dir / "hooks.json"
            hooks_path.write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"echo {installed}"}]}]}}),
                encoding="utf-8",
            )

            with (
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli, "hook_runtime_status", return_value=self.valid_hook_runtime_status()),
            ):
                result = cli.install_hook(argparse.Namespace(codex_dir=str(codex_dir)))
            commands = result["hooks_json"]["events"]["Stop"]["commands"]

        self.assertTrue(result["hooks_json"]["updated"])
        self.assertIn(f"echo {installed}", commands)
        self.assertIn(cli.hook_command(), commands)

    def test_doctor_reports_current_segments_and_hook_registration(self) -> None:
        cli = load_module("doctor_runtime_current_segments_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = pathlib.Path(tmp_dir) / ".codex"
            base = codex_dir / "bola"
            raw_segments = cli.raw_segments
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text("{}\n", encoding="utf-8")
            self.initialize_codex_dir(codex_dir)
            (codex_dir / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cli.hook_command()}]}],
                            "Stop": [{"hooks": [{"type": "command", "command": cli.hook_command()}]}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            before_files = {str(path.relative_to(codex_dir)): (path.read_bytes(), path.stat().st_mtime_ns) for path in codex_dir.rglob("*") if path.is_file()}
            captured = io.StringIO()
            with (
                mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(pathlib.Path(tmp_dir) / "config")}, clear=False),
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                cli.service_paths.write_config({"codex_dir": codex_dir, "output_dir": base})
                code = cli.doctor(argparse.Namespace(codex_dir=str(codex_dir), output_dir=str(base), json_output=True))
            after_files = {str(path.relative_to(codex_dir)): (path.read_bytes(), path.stat().st_mtime_ns) for path in codex_dir.rglob("*") if path.is_file()}

        report = json.loads(captured.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["health"], {"status": "healthy", "exit_code": 0, "issues": []})
        self.assertEqual(report["runtime"]["current_segments"]["prompt_usage"]["rows"], 1)
        self.assertTrue(report["runtime"]["hooks_json"]["events"]["UserPromptSubmit"]["registered"])
        self.assertTrue(report["runtime"]["hooks_json"]["events"]["Stop"]["registered"])
        self.assertEqual(before_files, after_files)

    def test_doctor_defaults_to_human_summary_with_actions(self) -> None:
        cli = load_module("doctor_human_output_test", ROOT / "scripts" / "bola.py")
        report = {
            "codex_dir": {"path": "/tmp/codex", "valid": True},
            "codex_cli": {"valid": True, "version": "codex-cli 1.0.0"},
            "output_dir": {"path": "/tmp/output", "exists": True},
            "analytics_db": {"path": "/tmp/output/analytics/bola.sqlite", "exists": True, "bytes": 1024},
            "runtime": {
                "hooks_json": {
                    "events": {
                        "UserPromptSubmit": {"registered": True},
                        "Stop": {"registered": True},
                    }
                },
                "recovery": {
                    "last_error": {"code": "error:raw_append_failed", "age_seconds": 3600},
                },
            },
            "health": {
                "status": "degraded",
                "exit_code": 1,
                "issues": [
                    {
                        "code": "recent_hook_errors",
                        "severity": "degraded",
                        "count": 2,
                        "errors": {"error:raw_append_failed": 2},
                    },
                    {
                        "code": "unacknowledged_quarantine",
                        "severity": "degraded",
                        "count": 1,
                        "occurrences": 3,
                        "by_kind": {"invalid_json": 1},
                    },
                ],
            },
        }
        captured = io.StringIO()
        result = cli.doctor_service.DoctorResult(report=report, exit_code=1)

        with (
            mock.patch.object(cli.doctor_service, "run_doctor", return_value=result),
            mock.patch.object(cli.sys, "stdout", captured),
        ):
            code = cli.doctor(argparse.Namespace(codex_dir=None, output_dir=None, json_output=False))

        output = captured.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("BOLA Doctor: DEGRADED", output)
        self.assertIn("[OK] Codex hooks: Stop, UserPromptSubmit", output)
        self.assertIn("[WARN] Recent hook writes failed", output)
        self.assertIn("Errors: raw_append_failed (2)", output)
        self.assertIn("Last occurrence: raw_append_failed, 1h ago", output)
        self.assertIn("Run: bola quarantine list", output)
        self.assertIn("Full report: bola doctor --json", output)
        self.assertNotIn('"runtime":', output)
        self.assertNotIn("\x1b", output)

    def test_doctor_json_preserves_complete_report(self) -> None:
        cli = load_module("doctor_json_output_test", ROOT / "scripts" / "bola.py")
        report = {
            "runtime": {"detail": {"nested": True}},
            "health": {"status": "healthy", "exit_code": 0, "issues": []},
        }
        captured = io.StringIO()
        result = cli.doctor_service.DoctorResult(report=report, exit_code=0)

        with (
            mock.patch.object(cli.doctor_service, "run_doctor", return_value=result),
            mock.patch.object(cli.sys, "stdout", captured),
        ):
            code = cli.doctor(argparse.Namespace(codex_dir=None, output_dir=None, json_output=True))

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(captured.getvalue()), report)

    def test_doctor_renderer_covers_all_health_issue_codes_and_unknown_fallback(self) -> None:
        cli = load_module("doctor_issue_rendering_contract_test", ROOT / "scripts" / "bola.py")
        expected = {
            "runtime_config_missing",
            "codex_dir_invalid",
            "codex_cli_invalid",
            "runtime_status_invalid",
            "current_segment_state_invalid",
            "hooks_config_invalid",
            "hook_registration_missing",
            "stale_hook_registration",
            "normalize_pending_publish_recovery_required",
            "pending_recovery_state",
            "recent_hook_errors",
            "stale_analytics_temp_files",
            "retention_pruned_store_invalid",
            "cleanup_retention_job_invalid",
            "retention_checkpoint_invalid",
            "service_lock_state_invalid",
            "path_transition_invalid",
            "retention_pruned_store_migration_required",
            "retention_pruned_state_recovery_ready",
            "retention_pruned_state_resolution_required",
            "retention_pruned_state_pending",
            "retention_pruned_state_orphaned",
            "stale_retention_checkpoints",
            "quarantine_state_invalid",
            "unacknowledged_quarantine",
        }
        self.assertEqual(cli.doctor_renderer.known_issue_codes(), expected)

        output = cli.doctor_renderer.render_doctor_report(
            {
                "health": {
                    "status": "failed",
                    "issues": [{"code": "future_health_signal", "severity": "failed", "count": 7}],
                }
            }
        )
        self.assertIn("Future health signal (future_health_signal)", output)
        self.assertIn("Count: 7", output)

    def test_doctor_parser_exposes_explicit_json_mode(self) -> None:
        cli = load_module("doctor_json_parser_test", ROOT / "scripts" / "bola.py")

        self.assertFalse(cli.parse_args(["doctor"]).json_output)
        self.assertTrue(cli.parse_args(["doctor", "--json"]).json_output)

    def test_doctor_classifies_retention_pending_lifecycle(self) -> None:
        cli = load_module("doctor_retention_lifecycle_test", ROOT / "scripts" / "bola.py")

        def report(*, job: dict[str, object] | None, held: bool = False) -> dict[str, object]:
            return {
                "codex_dir": {"valid": True},
                "codex_cli": {"valid": True},
                "runtime": {
                    "current_segments": {},
                    "hooks_json": {},
                    "recovery": {},
                    "analytics_tmp_files": {},
                    "quarantine": {},
                    "retention_pruned_store": {
                        "valid": True,
                        "pending_rows": 2,
                        "pending_job_ids": ["retention:test"],
                        "migration_required": False,
                    },
                    "cleanup_retention_job": {"valid": True, "job": job},
                    "retention_checkpoints": {"valid": True, "count": 0},
                    "service_lock": {"valid": True, "held": held},
                    "path_transition": {"valid": True, "transition": None},
                },
            }

        pending = cli.doctor_health(report(job={"operation_job_id": "retention:test"}))
        ready = cli.doctor_health(report(job={"pruned_state_job_id": "retention:test", "pruned_state_commit_ready": True}))
        unresolved = cli.doctor_health(report(job={"phase": "failed", "pruned_state_job_id": "retention:test"}))
        orphaned = cli.doctor_health(report(job=None))

        self.assertEqual((pending["status"], pending["exit_code"]), ("degraded", 1))
        self.assertEqual(pending["issues"][0]["code"], "retention_pruned_state_pending")
        self.assertEqual((ready["status"], ready["exit_code"]), ("degraded", 1))
        self.assertEqual(ready["issues"][0]["code"], "retention_pruned_state_recovery_ready")
        self.assertEqual((unresolved["status"], unresolved["exit_code"]), ("failed", 2))
        self.assertEqual(unresolved["issues"][0]["code"], "retention_pruned_state_resolution_required")
        self.assertEqual((orphaned["status"], orphaned["exit_code"]), ("failed", 2))
        self.assertEqual(orphaned["issues"][0]["code"], "retention_pruned_state_orphaned")

    def test_doctor_reports_recovery_state_errors_and_analytics_temp_files(self) -> None:
        cli = load_module("doctor_recovery_state_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = pathlib.Path(tmp_dir) / ".codex"
            base = codex_dir / "bola"
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
                json.dumps({"warning": "deferred_stop_recovery", "reason": "hook_scan_limit_reached"})
                + "\n"
                + json.dumps({"error": "raw_append_failed"})
                + "\n",
                encoding="utf-8",
            )
            tmp_db = analytics_dir / ".bola.sqlite.123.tmp"
            tmp_db.write_bytes(b"abc")
            tmp_journal = analytics_dir / ".bola.sqlite.123.tmp-journal"
            tmp_journal.write_bytes(b"de")
            pending_publish = normalized_dir / "normalize-state.json.pending"
            pending_publish.write_text("{broken", encoding="utf-8")
            self.initialize_codex_dir(codex_dir)
            captured = io.StringIO()

            with (
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.doctor(argparse.Namespace(codex_dir=str(codex_dir), output_dir=str(base), json_output=True))

        report = json.loads(captured.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report["health"]["status"], "failed")
        self.assertIn("normalize_pending_publish_recovery_required", {issue["code"] for issue in report["health"]["issues"]})
        self.assertEqual(report["runtime"]["recovery"]["pending_state_files"], 1)
        self.assertEqual(report["runtime"]["recovery"]["error_log_counts"]["warning:deferred_stop_recovery"], 1)
        self.assertEqual(report["runtime"]["recovery"]["error_log_counts"]["error:raw_append_failed"], 1)
        self.assertTrue(report["runtime"]["normalize_pending_publish"]["exists"])
        self.assertTrue(report["runtime"]["normalize_pending_publish"]["recovery_required"])
        self.assertFalse(report["runtime"]["normalize_pending_publish"]["valid"])
        self.assertEqual(report["runtime"]["normalize_pending_publish"]["path"], str(pending_publish))
        self.assertEqual(report["runtime"]["analytics_tmp_files"]["count"], 2)
        self.assertEqual(report["runtime"]["analytics_tmp_files"]["bytes"], 5)
        self.assertEqual({item["sidecar"] for item in report["runtime"]["analytics_tmp_files"]["files"]}, {None, "journal"})

    def test_doctor_exits_degraded_for_unresolved_runtime_signals(self) -> None:
        cli = load_module("doctor_degraded_runtime_test", ROOT / "scripts" / "bola.py")
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = pathlib.Path(tmp_dir) / ".codex"
            base = codex_dir / "bola"
            state_dir = base / "state"
            analytics_dir = base / "analytics"
            state_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            (state_dir / "pending-stop.json").write_text(
                json.dumps({"record_type": "turn_stop_missing_start", "session_id": "s1", "turn_id": "t1"}),
                encoding="utf-8",
            )
            (base / "prompt-usage-errors.jsonl").write_text(
                json.dumps(
                    {
                        "captured_at": cli.datetime.fromtimestamp(now - 60, cli.timezone.utc).isoformat(),
                        "error": "raw_append_failed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            tmp_db = analytics_dir / ".bola.sqlite.123.tmp"
            tmp_db.write_bytes(b"abc")
            stale_mtime = now - cli.DOCTOR_STALE_ANALYTICS_TMP_SECONDS - 1
            os.utime(tmp_db, (stale_mtime, stale_mtime))
            self.initialize_codex_dir(codex_dir)
            (codex_dir / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cli.hook_command()}]}],
                            "Stop": [{"hooks": [{"type": "command", "command": cli.hook_command()}]}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            captured = io.StringIO()

            with (
                mock.patch.dict(cli.os.environ, {"XDG_CONFIG_HOME": str(pathlib.Path(tmp_dir) / "config")}, clear=False),
                mock.patch.object(cli, "codex_cli_status", return_value=self.valid_codex_cli_status()),
                mock.patch.object(cli.time, "time", return_value=now),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                cli.service_paths.write_config({"codex_dir": codex_dir, "output_dir": base})
                code = cli.doctor(argparse.Namespace(codex_dir=str(codex_dir), output_dir=str(base), json_output=True))

        report = json.loads(captured.getvalue())
        issue_codes = {issue["code"] for issue in report["health"]["issues"]}
        self.assertEqual(code, 1)
        self.assertEqual(report["health"]["status"], "degraded")
        self.assertEqual(report["health"]["exit_code"], 1)
        self.assertEqual(
            issue_codes,
            {"pending_recovery_state", "recent_hook_errors", "stale_analytics_temp_files"},
        )
        self.assertEqual(report["runtime"]["recovery"]["recovery_required_state_files"], 1)
        self.assertEqual(report["runtime"]["recovery"]["recent_error_log_counts"]["error:raw_append_failed"], 1)
        self.assertEqual(report["runtime"]["analytics_tmp_files"]["stale_count"], 1)

    def test_doctor_runtime_windows_ignore_active_and_historical_artifacts(self) -> None:
        cli = load_module("doctor_runtime_windows_test", ROOT / "scripts" / "bola.py")
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            state_dir = base / "state"
            analytics_dir = base / "analytics"
            state_dir.mkdir()
            analytics_dir.mkdir()
            active_state = state_dir / "active-turn.json"
            active_state.write_text(json.dumps({"record_type": "turn_start"}), encoding="utf-8")
            os.utime(active_state, (now - 60, now - 60))
            (base / "prompt-usage-errors.jsonl").write_text(
                json.dumps(
                    {
                        "captured_at": cli.datetime.fromtimestamp(
                            now - cli.DOCTOR_RECENT_ERROR_WINDOW_SECONDS - 1,
                            cli.timezone.utc,
                        ).isoformat(),
                        "error": "raw_append_failed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            active_tmp = analytics_dir / ".bola.sqlite.123.tmp"
            active_tmp.write_bytes(b"abc")
            os.utime(active_tmp, (now - 60, now - 60))

            pending = cli.pending_recovery_state_summary(base, now_unix=now)
            errors = cli.error_log_summary(base, now_unix=now)
            analytics_tmp = cli.analytics_tmp_file_summary(base, now_unix=now)

        self.assertEqual(pending["pending_state_files"], 1)
        self.assertEqual(pending["recovery_required_state_files"], 0)
        self.assertEqual(errors["counts"]["error:raw_append_failed"], 1)
        self.assertEqual(errors["recent_error_counts"], {})
        self.assertEqual(analytics_tmp["count"], 1)
        self.assertEqual(analytics_tmp["stale_count"], 0)

    def test_doctor_reports_invalid_codex_dir_and_cli_with_failure_exit(self) -> None:
        cli = load_module("doctor_invalid_codex_environment_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            codex_dir = root / "empty-codex-dir"
            codex_dir.mkdir()
            captured = io.StringIO()
            invalid_cli = {"valid": False, "path": None, "version": None, "reason": "not_found", "message": "Codex CLI was not found in PATH"}
            with (
                mock.patch.object(cli, "codex_cli_status", return_value=invalid_cli),
                mock.patch.object(cli.sys, "stdout", captured),
            ):
                code = cli.doctor(argparse.Namespace(codex_dir=str(codex_dir), output_dir=str(root / "output"), json_output=True))

        report = json.loads(captured.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report["health"]["status"], "failed")
        self.assertEqual(report["health"]["exit_code"], 2)
        self.assertFalse(report["codex_dir"]["valid"])
        self.assertEqual(report["codex_dir"]["reason"], "not_initialized")
        self.assertEqual(report["codex_cli"]["reason"], "not_found")

    def test_cli_serve_default_port_matches_makefile(self) -> None:
        cli = load_module("serve_default_port_test", ROOT / "scripts" / "bola.py")
        args = cli.parse_args(["serve"])
        self.assertEqual(args.port, "8766")

    def test_output_dir_is_canonical_and_data_root_is_rejected(self) -> None:
        cli = load_module("output_dir_cli_contract_test", ROOT / "scripts" / "bola.py")
        canonical = cli.parse_args(["install-hook", "--output-dir", "/tmp/output"])
        with self.assertRaises(SystemExit):
            cli.parse_args(["install-hook", "--data-root", "/tmp/legacy"])
        install = next(action for action in cli.build_parser()._actions if isinstance(action, argparse._SubParsersAction)).choices["install-hook"]
        help_text = install.format_help()

        self.assertEqual(canonical.output_dir, "/tmp/output")
        self.assertIn("--output-dir OUTPUT_DIR", help_text)
        self.assertNotIn("--data-root", help_text)

        with self.assertRaises(SystemExit):
            cli.parse_args(["install-hook", "--output-dir", "/tmp/output", "--data-root", "/tmp/legacy"])

    def test_codex_dir_is_the_public_runtime_path_option(self) -> None:
        cli = load_module("codex_dir_cli_contract_test", ROOT / "scripts" / "bola.py")
        parsed = cli.parse_args(["install-hook", "--codex-dir", "/tmp/codex"])
        install = next(action for action in cli.build_parser()._actions if isinstance(action, argparse._SubParsersAction)).choices["install-hook"]

        self.assertEqual(parsed.codex_dir, "/tmp/codex")
        self.assertIn("--codex-dir CODEX_DIR", install.format_help())

    def test_config_schema_persists_codex_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            path = root / "runtime.conf"
            service_paths.write_config({"codex_dir": root / "codex"}, path)
            payload = service_paths.read_config(path)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["codex_dir"], str(root / "codex"))

    def test_cli_serve_rejects_removed_allow_network_option(self) -> None:
        cli = load_module("serve_allow_network_removed_test", ROOT / "scripts" / "bola.py")
        with self.assertRaises(SystemExit):
            cli.parse_args(["serve", "--host", "0.0.0.0", "--allow-network"])

    def test_cli_serve_rejects_db_override(self) -> None:
        cli = load_module("serve_rejects_db_override_test", ROOT / "scripts" / "bola.py")
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["serve", "--db", "/tmp/custom.sqlite"])

    def test_high_level_cli_commands_reject_unknown_options(self) -> None:
        cli = load_module("high_level_unknown_args_test", ROOT / "scripts" / "bola.py")
        cases = [
            ["pipeline", "--codex-dri", "/tmp/nope"],
            ["retention-prune", "--cutoff", "0", "--outptu", "/tmp/nope"],
            ["doctor", "--codex-dri", "/tmp/nope"],
            ["install-hook", "--codex-dri", "/tmp/nope"],
            ["migrate-path", "--aply"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with mock.patch.object(cli.sys, "argv", ["bola.py", *argv]):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main()
                self.assertEqual(raised.exception.code, 2)

    def test_pipeline_help_makes_recovery_explicit(self) -> None:
        cli = load_module("pipeline_help_recovery_contract_test", ROOT / "scripts" / "bola.py")
        help_text = cli.build_parser().format_help()

        self.assertIn("pipeline", help_text)
        self.assertNotIn("Run reconcile, normalize, then build.", help_text)

    def test_retention_prune_invalid_cutoff_returns_structured_error(self) -> None:
        cli = load_module("retention_invalid_cutoff_test", ROOT / "scripts" / "bola.py")
        captured = io.StringIO()

        with mock.patch.object(cli.sys, "stdout", captured):
            code = cli.retention_prune(argparse.Namespace(codex_dir=None, output_dir=None, cutoff="not-a-date", preview_signature="sig"))

        payload = json.loads(captured.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "cutoff_date_invalid")
        self.assertEqual(payload["stage"], "preview")

    def test_parse_cutoff_date_only_uses_utc_midnight(self) -> None:
        cli = load_module("retention_cutoff_date_only_utc_test", ROOT / "scripts" / "bola.py")

        self.assertEqual(cli.parse_cutoff("2026-05-20"), cli.parse_cutoff("2026-05-20T00:00:00+00:00"))

    def test_per_file_analytics_output_options_are_not_public(self) -> None:
        cli = load_module("analytics_output_option_contract_test", ROOT / "scripts" / "bola.py")
        for command in ("build", "pipeline", "retention-prune"):
            arguments = [command]
            if command == "retention-prune":
                arguments.extend(("--cutoff", "2026-05-20"))
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as raised:
                    cli.parse_args([*arguments, "--output", "/tmp/external.sqlite"])
                self.assertEqual(raised.exception.code, 2)

    def test_retention_prune_outputs_partial_mutation_envelope_last_after_normalize_failure(self) -> None:
        cli = load_module("retention_prune_partial_mutation_last_json_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_dir = pathlib.Path(tmp_dir) / ".codex"
            base = codex_dir / "bola"
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
                code = cli.retention_prune(argparse.Namespace(codex_dir=str(codex_dir), output=None, cutoff="2026-05-20", preview_signature="sig"))

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
        cli = load_module("release_check_removed_test", ROOT / "scripts" / "bola.py")
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
        for relative in ("scripts/dashboard_rebuild_api.py", "scripts/dashboard_cleanup_api.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("stdout=subprocess.PIPE", source)
            self.assertNotIn("stderr=subprocess.PIPE", source)
            self.assertIn("tempfile.TemporaryFile", source)
            self.assertIn("dir=tmp_dir", source)

    def test_compat_facades_do_not_mutate_submodule_globals(self) -> None:
        for relative in ("scripts/raw_segments.py", "scripts/dashboard_cleanup.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("import *", source)
            self.assertNotIn("_sync_module_bindings", source)
            self.assertNotIn("_restore_module_bindings", source)
            self.assertNotIn("setattr(", source)

        cleanup = load_module("dashboard_cleanup_exports_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_exports_test", ROOT / "scripts" / "raw_segments.py")
        self.assertEqual(
            set(cleanup.__all__),
            {
                "ManifestError",
                "RetentionPreviewStale",
                "apply_delete_logs_older_than_plan",
                "cleanup_detail_payload",
                "cleanup_payload",
                "cleanup_retention_job_path",
                "clear_cleanup_retention_job",
                "clear_retention_preview_cache",
                "complete_retention_derived_rebuild",
                "delete_all_logs",
                "delete_logs_older_than",
                "discard_delete_logs_older_than_plan",
                "ensure_service_owned_output",
                "plan_delete_logs_older_than",
                "preflight_delete_logs_older_than",
                "read_cleanup_retention_job",
                "rebuild_retention_index",
                "refresh_retention_index_for_current_sources",
                "reset_derived_outputs",
                "retention_preview",
                "retention_preview_signature",
                "validate_delete_logs_older_than_plan",
                "write_cleanup_retention_job",
            },
        )
        self.assertNotIn("raw_segments", cleanup.__all__)
        self.assertNotIn("time", cleanup.__all__)
        self.assertNotIn("RETENTION_PREVIEW_CACHE", cleanup.__all__)
        self.assertEqual(
            set(raw_segments.__all__),
            {
                "ApplyMarkerPhase",
                "ApplyMarkerStatus",
                "JsonlScanAccumulator",
                "ManifestError",
                "PROMPT_RAW_NAME",
                "RotationPhase",
                "SegmentApplyState",
                "acquire_raw_segment_lock",
                "append_closed_segment",
                "apply_segment_plans",
                "begin_rotate_all_current_segments_unlocked",
                "clear_apply_marker",
                "closed_segment_from_current",
                "current_pointer_path",
                "current_segment_paths",
                "discard_segment_plan_artifacts",
                "empty_current_pointer",
                "empty_manifest",
                "ensure_current_segment",
                "finish_rotate_all_current_segments",
                "fsync_dir",
                "inspect_segment_apply_state",
                "manifest_path",
                "manifest_segments",
                "manifest_signature",
                "load_pending_rotation",
                "new_current_segment",
                "open_segment_payload",
                "pending_rotation_path",
                "plan_segments_older_than",
                "preflight_segments_older_than",
                "raw_segment_lock_available",
                "raw_segment_lock_path",
                "read_apply_marker",
                "read_apply_status",
                "read_current_pointer",
                "read_manifest",
                "read_pending_rotation",
                "reconcile_apply_marker",
                "reconcile_apply_marker_unlocked",
                "reconcile_pending_rotation",
                "retention_preview_from_current",
                "retention_preview_from_manifest",
                "rotate_all_current_segments",
                "rotate_current_segment",
                "row_time",
                "segment_apply_marker_path",
                "strict_read_current_pointer",
                "strict_read_manifest",
                "sweep_apply_marker",
                "unlink_empty_closed_segment",
                "validate_current_pointer_entries",
                "validate_current_segment_entry",
                "validate_segment_path",
                "validate_segment_plans",
                "write_apply_marker",
                "write_current_pointer",
                "write_json_atomic",
                "write_manifest",
                "write_pending_rotation",
            },
        )

        retention_sources = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "scripts/dashboard_cleanup_retention.py",
                "scripts/dashboard_retention_index.py",
                "scripts/dashboard_retention_preview.py",
            )
        )
        for removed_name in (
            "plan_jsonl_for_retention",
            "apply_retention_plan",
            "rewrite_jsonl_for_retention",
            "write_retained_jsonl_for_retention",
        ):
            self.assertNotIn(removed_name, retention_sources)

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
        self.assertIn('raise RuntimeError("browser runtime errors detected', runner)

    def test_ui_check_runs_named_scenarios_in_isolated_contexts(self) -> None:
        runner = (ROOT / "scripts" / "playwright_dashboard_check.py").read_text(encoding="utf-8")
        self.assertIn('BrowserScenario("desktop-tools-subagents"', runner)
        self.assertIn('BrowserScenario("analyze-cancel"', runner)
        self.assertIn("context = browser.new_context(", runner)
        self.assertIn('parser.add_argument("--repeat"', runner)
        self.assertIn('"--scenario",', runner)

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
        runner = (ROOT / "scripts" / "playwright_dashboard_check.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(desktop.splitlines()), 80)
        for module_name, function_name, scenario_function in (
            ("playwright_dashboard_toolbar.py", "check_toolbar", "check_desktop_toolbar"),
            ("playwright_dashboard_turns.py", "check_turns_and_selected_turn", "check_desktop_turns"),
            ("playwright_dashboard_cleanup.py", "check_cleanup_desktop", "check_desktop_cleanup"),
            ("playwright_dashboard_tools.py", "check_tools_and_subagents", "check_desktop_tools"),
        ):
            source = (ROOT / "scripts" / module_name).read_text(encoding="utf-8")
            self.assertIn(f"def {function_name}", source)
            self.assertIn(function_name, desktop)
            self.assertIn(f"def {scenario_function}", desktop)
            self.assertIn(scenario_function, runner)

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
