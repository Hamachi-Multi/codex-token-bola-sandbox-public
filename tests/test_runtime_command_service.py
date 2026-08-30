from __future__ import annotations

try:
    from tests.support import mock, pathlib, tempfile, unittest
except ModuleNotFoundError:
    from support import mock, pathlib, tempfile, unittest

from scripts import runtime_command_service as service
from scripts import service_paths
from scripts.runtime_command_runner import RuntimeCommand


class RuntimeCommandServiceTests(unittest.TestCase):
    @staticmethod
    def paths(root: pathlib.Path) -> service_paths.RuntimePaths:
        return service_paths.RuntimePaths(
            project_root=root,
            runtime_config_path=root / "runtime.conf",
            codex_dir=root / ".codex",
            output_dir=root / "output",
        )

    def test_build_translates_typed_options_and_holds_service_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            paths = self.paths(root)
            calls: list[tuple[str, list[str], dict[str, str]]] = []

            def invoke(command: str, arguments: list[str], *, env: dict[str, str]) -> int:
                calls.append((command, arguments, env))
                return 7

            result = service.run_build(
                service.BuildOptions(
                    normalized_log="turns.jsonl",
                    state_db="state.sqlite",
                    project_roots=("/work/a", "/work/b"),
                    extra_arguments=("--incremental",),
                ),
                service.RuntimeCommandDependencies(
                    resolve_paths=lambda _codex, _output: paths,
                    run_command=invoke,
                ),
            )

        self.assertEqual(result.exit_code, 7)
        command, arguments, environment = calls[0]
        self.assertEqual(command, RuntimeCommand.BUILD.value)
        self.assertEqual(
            arguments,
            [
                "--normalized-log",
                "turns.jsonl",
                "--state-db",
                "state.sqlite",
                "--project-root",
                "/work/a",
                "--project-root",
                "/work/b",
                "--incremental",
            ],
        )
        self.assertEqual(environment["CODEX_HOME"], str(paths.codex_dir))
        self.assertEqual(environment[service_paths.OUTPUT_DIR_ENV], str(paths.output_dir))
        self.assertIn("BOLA_LOCK_FD", environment)

    def test_serve_preserves_process_environment_and_pins_explicit_paths(self) -> None:
        class Replaced(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            paths = self.paths(root)
            replace = mock.Mock(side_effect=Replaced)
            with (
                mock.patch.dict(service.os.environ, {"BOLA_TEST_SENTINEL": "kept"}, clear=True),
                self.assertRaises(Replaced),
            ):
                service.run_serve(
                    service.ServeOptions(
                        host="127.0.0.1",
                        port=8766,
                        codex_dir=paths.codex_dir,
                        output_dir=paths.output_dir,
                        pin_runtime_paths=True,
                    ),
                    service.ServeDependencies(
                        resolve_paths=lambda _codex, _output: paths,
                        require_runtime_config=lambda: {"schema_version": 1},
                        replace_command=replace,
                    ),
                )

        self.assertEqual(
            replace.call_args.args,
            (
                RuntimeCommand.SERVE.value,
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8766",
                    "--codex-dir",
                    str(paths.codex_dir),
                    "--output-dir",
                    str(paths.output_dir),
                    "--pin-runtime-paths",
                ],
            ),
        )
        self.assertEqual(replace.call_args.kwargs["env"]["BOLA_TEST_SENTINEL"], "kept")

    def test_serve_requires_runtime_config_before_replacing_process(self) -> None:
        def missing_config() -> dict[str, object]:
            raise service_paths.ConfigurationError("run bola install-hook first")

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            paths = self.paths(root)
            replace = mock.Mock()

            with self.assertRaisesRegex(service_paths.ConfigurationError, "run bola install-hook first"):
                service.run_serve(
                    service.ServeOptions(host="127.0.0.1", port=8766),
                    service.ServeDependencies(
                        resolve_paths=lambda _codex, _output: paths,
                        require_runtime_config=missing_config,
                        replace_command=replace,
                    ),
                )

        replace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
