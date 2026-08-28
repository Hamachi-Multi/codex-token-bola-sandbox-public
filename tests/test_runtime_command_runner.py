from __future__ import annotations

try:
    from tests.support import ROOT, mock, pathlib, unittest
except ModuleNotFoundError:
    from support import ROOT, mock, pathlib, unittest

from scripts import runtime_command_runner as runner


class RuntimeCommandRunnerTests(unittest.TestCase):
    def test_parse_last_json_object_uses_last_object_line(self) -> None:
        payload, error = runner.parse_last_json_object('noise\n{"first":1}\n[]\n{"last":2}\n')
        self.assertEqual(payload, {"last": 2})
        self.assertIsNone(error)

    def test_parse_last_json_object_classifies_missing_payload(self) -> None:
        self.assertEqual(runner.parse_last_json_object(""), (None, "stdout_empty"))
        self.assertEqual(runner.parse_last_json_object("noise\n[]\n"), (None, "json_object_missing"))

    def test_runner_captures_process_contract_and_lock_fds(self) -> None:
        completed = mock.Mock(returncode=1, stdout='log\n{"status":"degraded"}\n', stderr="warning\n")
        command_runner = runner.SubprocessRuntimeCommandRunner(ROOT / "scripts", "/python")
        with (
            mock.patch.object(runner.subprocess, "run", return_value=completed) as run,
            mock.patch.object(runner.service_lock, "lock_pass_fds", return_value=(9,)),
        ):
            result = command_runner.run(
                runner.RuntimeCommand.NORMALIZE,
                ["--incremental"],
                env={"BOLA_LOCK_FD": "9"},
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.payload, {"status": "degraded"})
        self.assertEqual(result.stdout, completed.stdout)
        self.assertEqual(result.stderr, completed.stderr)
        self.assertIsNone(result.parse_error)
        self.assertEqual(run.call_args.kwargs["pass_fds"], (9,))
        self.assertEqual(run.call_args.args[0][-1], "--incremental")

    def test_passthrough_runner_does_not_capture_output(self) -> None:
        command_runner = runner.SubprocessRuntimeCommandRunner(pathlib.Path("/runtime"), "/python")
        with mock.patch.object(runner.subprocess, "call", return_value=7) as call:
            result = command_runner.run(runner.RuntimeCommand.RECONCILE, ["--flag"], capture_json=False)

        self.assertEqual(result.exit_code, 7)
        self.assertIsNone(result.payload)
        self.assertNotIn("capture_output", call.call_args.kwargs)

    def test_replace_execs_runtime_command_in_current_process(self) -> None:
        class Replaced(Exception):
            pass

        command_runner = runner.SubprocessRuntimeCommandRunner(pathlib.Path("/runtime"), "/python")
        with (
            mock.patch.dict(runner.os.environ, {"BOLA_LOCK_FD": "9", "BASE": "kept"}, clear=True),
            mock.patch.object(runner.os, "execve", side_effect=Replaced) as execve,
            self.assertRaises(Replaced),
        ):
            command_runner.replace(runner.RuntimeCommand.SERVE, ["--port", "8766"], env={"EXTRA": "yes"})

        self.assertEqual(execve.call_args.args[0], "/python")
        self.assertEqual(execve.call_args.args[1], ["/python", "/runtime/serve_dashboard.py", "--port", "8766"])
        environment = execve.call_args.args[2]
        self.assertEqual(environment["BASE"], "kept")
        self.assertEqual(environment["EXTRA"], "yes")
        self.assertNotIn("BOLA_LOCK_FD", environment)

    def test_unknown_script_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.RuntimeCommand.from_script_name("arbitrary.py")


if __name__ == "__main__":
    unittest.main()
