from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    from tests.support import ROOT, load_module, mock, pathlib, tempfile, types, unittest
except ModuleNotFoundError:
    from support import ROOT, load_module, mock, pathlib, tempfile, types, unittest


SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dashboard_operation_state as operation_state
import dashboard_server_runtime as server_runtime
import service_paths


class DashboardOperationManagerTests(unittest.TestCase):
    def test_immediate_cancel_survives_file_and_process_attachment(self) -> None:
        manager = operation_state.DashboardOperationManager()
        operation_id = "11111111-1111-4111-8111-111111111111"
        lease = manager.begin("analysis", "/tmp/output", operation_id=operation_id)

        manager.request_analysis_cancel(operation_id)
        manager.set_files(operation_id, cancel_file=pathlib.Path("/tmp/cancel.json"))
        process = object()
        manager.attach_process(operation_id, process)

        active = manager.active_record()
        self.assertIsNotNone(active)
        self.assertTrue(active.cancel_requested.is_set())
        self.assertEqual(active.cancel_file, pathlib.Path("/tmp/cancel.json"))
        self.assertIs(active.process, process)
        lease.close()

    def test_cancel_endpoint_rejects_missing_and_mismatched_ownership(self) -> None:
        serve = load_module("serve_dashboard_operation_cancel_contract_test", SCRIPTS / "serve_dashboard.py")
        manager = serve.dashboard_operation_state.DashboardOperationManager()
        handler = serve.Handler.__new__(serve.Handler)
        handler.server = types.SimpleNamespace(operation_manager=manager)
        sent: list[tuple[dict[str, object], int]] = []
        handler.send_json = lambda payload, status=200: sent.append((payload, status))
        first_id = "11111111-1111-4111-8111-111111111111"
        second_id = "22222222-2222-4222-8222-222222222222"

        handler.read_json_body = lambda: {"operation_id": first_id}
        handler.handle_rebuild_cancel()
        self.assertEqual(sent.pop(), ({"error": "analysis_not_running"}, 409))

        lease = manager.begin("analysis", "/tmp/output", operation_id=second_id)
        try:
            handler.handle_rebuild_cancel()
        finally:
            lease.close()
        self.assertEqual(sent.pop(), ({"error": "operation_id_mismatch"}, 409))

    def test_rebuild_requires_canonical_operation_id(self) -> None:
        serve = load_module("serve_dashboard_operation_id_contract_test", SCRIPTS / "serve_dashboard.py")
        handler = serve.Handler.__new__(serve.Handler)
        handler.read_json_body = lambda: {"operation_id": "not-a-uuid"}
        sent: list[tuple[dict[str, object], int]] = []
        handler.send_json = lambda payload, status=200: sent.append((payload, status))

        handler.handle_rebuild()

        self.assertEqual(sent, [({"error": "invalid_operation_id"}, 400)])

    def test_stale_cancel_and_finish_cannot_mutate_new_operation(self) -> None:
        manager = operation_state.DashboardOperationManager()
        first_id = "11111111-1111-4111-8111-111111111111"
        second_id = "22222222-2222-4222-8222-222222222222"
        first = manager.begin("analysis", "/tmp/output", operation_id=first_id)
        first.close()
        second = manager.begin("analysis", "/tmp/output", operation_id=second_id)

        with self.assertRaises(operation_state.OperationIdMismatch):
            manager.request_analysis_cancel(first_id)
        self.assertFalse(manager.finish(first_id))
        self.assertEqual(manager.active_record().operation_id, second_id)
        self.assertFalse(manager.active_record().cancel_requested.is_set())
        second.close()

    def test_shutdown_rejects_new_operations_and_marks_analysis_cancelled(self) -> None:
        manager = operation_state.DashboardOperationManager()
        operation_id = "11111111-1111-4111-8111-111111111111"
        lease = manager.begin("analysis", "/tmp/output", operation_id=operation_id)

        active = manager.begin_shutdown()

        self.assertTrue(active.cancel_requested.is_set())
        with self.assertRaises(operation_state.ServerShuttingDown):
            manager.begin("cleanup", "/tmp/output")
        lease.close()

    def test_service_status_reports_idle_without_exposing_lock_details(self) -> None:
        serve = load_module("serve_dashboard_service_status_idle_test", SCRIPTS / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as temporary:
            payload = serve.dashboard_service_status.service_status_payload(
                manager=serve.dashboard_operation_state.DashboardOperationManager(),
                output_dir=pathlib.Path(temporary),
            )

        self.assertEqual(
            payload,
            {
                "running": False,
                "operation": None,
                "status": "idle",
                "progress_available": False,
                "phase": "",
                "checkpoint": "",
                "overall_progress": None,
                "operation_id": None,
            },
        )
        self.assertNotIn("pid", payload)
        self.assertNotIn("lock_path", payload)

    def test_service_status_reports_dashboard_progress(self) -> None:
        serve = load_module("serve_dashboard_service_status_progress_test", SCRIPTS / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = pathlib.Path(temporary)
            progress_file = output_dir / "progress.json"
            operation_id = "11111111-1111-4111-8111-111111111111"
            manager = serve.dashboard_operation_state.DashboardOperationManager()
            lease = manager.begin("analysis", output_dir, operation_id=operation_id)
            manager.set_files(operation_id, progress_file=progress_file)
            serve.dashboard_service_status.progress_control.write_progress_to_path(
                progress_file,
                operation_id=operation_id,
                status="running",
                phase="build",
                checkpoint="turns",
                overall_progress=42.5,
            )
            try:
                payload = serve.dashboard_service_status.service_status_payload(manager=manager, output_dir=output_dir)
            finally:
                lease.close()

        self.assertTrue(payload["running"])
        self.assertEqual(payload["operation"], "analysis")
        self.assertEqual(payload["phase"], "build")
        self.assertEqual(payload["checkpoint"], "turns")
        self.assertEqual(payload["overall_progress"], 42.5)
        self.assertTrue(payload["progress_available"])
        self.assertEqual(payload["operation_id"], operation_id)

    def test_service_status_reports_external_cleanup_without_progress(self) -> None:
        serve = load_module("serve_dashboard_service_status_external_test", SCRIPTS / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = pathlib.Path(temporary)
            manager = serve.dashboard_operation_state.DashboardOperationManager()
            with serve.dashboard_service_status.service_lock.acquire_service_lock(
                reason="retention-prune",
                output_dir=output_dir,
            ):
                payload = serve.dashboard_service_status.service_status_payload(manager=manager, output_dir=output_dir)

        self.assertTrue(payload["running"])
        self.assertEqual(payload["operation"], "cleanup")
        self.assertFalse(payload["progress_available"])
        self.assertIsNone(payload["overall_progress"])
        self.assertIsNone(payload["operation_id"])
        self.assertNotIn("pid", payload)
        self.assertNotIn("lock_path", payload)

    def test_service_status_reports_cost_recalculation_without_progress(self) -> None:
        serve = load_module("serve_dashboard_service_status_cost_recalculation_test", SCRIPTS / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = pathlib.Path(temporary)
            manager = serve.dashboard_operation_state.DashboardOperationManager()
            lease = manager.begin("cost_recalculation", output_dir)
            try:
                payload = serve.dashboard_service_status.service_status_payload(manager=manager, output_dir=output_dir)
            finally:
                lease.close()

        self.assertTrue(payload["running"])
        self.assertEqual(payload["operation"], "cost_recalculation")
        self.assertFalse(payload["progress_available"])

    def test_external_cost_recalculation_lock_is_classified(self) -> None:
        serve = load_module("serve_dashboard_external_cost_recalculation_test", SCRIPTS / "serve_dashboard.py")
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = pathlib.Path(temporary)
            manager = serve.dashboard_operation_state.DashboardOperationManager()
            with serve.dashboard_service_status.service_lock.acquire_service_lock(
                reason="cost-recalculation",
                output_dir=output_dir,
            ):
                payload = serve.dashboard_service_status.service_status_payload(manager=manager, output_dir=output_dir)

        self.assertTrue(payload["running"])
        self.assertEqual(payload["operation"], "cost_recalculation")
        self.assertFalse(payload["progress_available"])


class DashboardServerRuntimeTests(unittest.TestCase):
    @staticmethod
    def paths(root: pathlib.Path, name: str) -> service_paths.RuntimePaths:
        return service_paths.RuntimePaths(
            project_root=root,
            runtime_config_path=root / "runtime.conf",
            codex_dir=root / ".codex",
            output_dir=root / name,
        )

    def test_same_output_dir_allows_only_one_server_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "output"
            first = server_runtime.DashboardServerLease.acquire(output, "first")
            try:
                with self.assertRaises(server_runtime.DashboardServerBusy):
                    server_runtime.DashboardServerLease.acquire(output, "second")
            finally:
                first.close()

            replacement = server_runtime.DashboardServerLease.acquire(output, "replacement")
            replacement.close()

    def test_different_output_dirs_allow_parallel_server_leases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            first = server_runtime.DashboardServerLease.acquire(root / "a", "first")
            second = server_runtime.DashboardServerLease.acquire(root / "b", "second")
            second.close()
            first.close()

    def test_dynamic_path_handoff_acquires_destination_before_releasing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            first_paths = self.paths(root, "a")
            second_paths = self.paths(root, "b")
            operations = operation_state.DashboardOperationManager()
            runtime = server_runtime.DashboardRuntimeManager(first_paths, dynamic=True, operation_manager=operations)
            no_path_lock = contextlib.nullcontext()
            try:
                with (
                    mock.patch.object(server_runtime.service_paths, "acquire_path_lock", return_value=no_path_lock),
                    mock.patch.object(server_runtime.service_paths, "resolve_runtime_paths", return_value=second_paths),
                ):
                    lease = operations.begin("analysis", first_paths.output_dir)
                    with self.assertRaises(server_runtime.DashboardPathTransitionBusy):
                        runtime.snapshot()
                    lease.close()
                    self.assertEqual(runtime.snapshot(), second_paths)

                source_replacement = server_runtime.DashboardServerLease.acquire(first_paths.output_dir, "source-replacement")
                source_replacement.close()
                with self.assertRaises(server_runtime.DashboardServerBusy):
                    server_runtime.DashboardServerLease.acquire(second_paths.output_dir, "destination-conflict")
            finally:
                runtime.close()

    def test_dynamic_path_handoff_fails_closed_when_destination_is_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            first_paths = self.paths(root, "a")
            second_paths = self.paths(root, "b")
            operations = operation_state.DashboardOperationManager()
            runtime = server_runtime.DashboardRuntimeManager(first_paths, dynamic=True, operation_manager=operations)
            destination = server_runtime.DashboardServerLease.acquire(second_paths.output_dir, "other")
            try:
                with (
                    mock.patch.object(server_runtime.service_paths, "acquire_path_lock", return_value=contextlib.nullcontext()),
                    mock.patch.object(server_runtime.service_paths, "resolve_runtime_paths", return_value=second_paths),
                ):
                    with self.assertRaises(server_runtime.DashboardOutputConflict):
                        runtime.snapshot()
                with self.assertRaises(server_runtime.DashboardServerBusy):
                    server_runtime.DashboardServerLease.acquire(first_paths.output_dir, "source-still-owned")
            finally:
                destination.close()
                runtime.close()


@unittest.skipUnless(sys.platform.startswith("linux"), "parent-death supervision requires Linux")
class DashboardProcessSupervisorTests(unittest.TestCase):
    @staticmethod
    def process_gone(pid: int) -> bool:
        stat_path = pathlib.Path(f"/proc/{pid}/stat")
        try:
            fields = stat_path.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return True
        return len(fields) > 2 and fields[2] == "Z"

    def wait_for_file(self, path: pathlib.Path, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                value = ""
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"timed out waiting for {path}")

    def test_parent_sigkill_terminates_supervised_child_and_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            parent_script = root / "parent.py"
            worker_script = root / "worker.py"
            worker_pid_path = root / "worker.pid"
            grandchild_pid_path = root / "grandchild.pid"
            supervisor_pid_path = root / "supervisor.pid"
            log_path = root / "process.log"
            output_dir = root / "output"
            worker_script.write_text(
                "import os,pathlib,subprocess,sys,time\n"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
                "child=subprocess.Popen([sys.executable,'-c',\"import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()),encoding='utf-8');time.sleep(60)\",sys.argv[2]])\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            parent_script.write_text(
                "import os,pathlib,sys,time\n"
                f"sys.path.insert(0,{str(SCRIPTS)!r})\n"
                "from dashboard_managed_process import ManagedProcess\n"
                "from dashboard_server_runtime import DashboardServerLease\n"
                "log=open(sys.argv[4],'w+',encoding='utf-8')\n"
                "lease=DashboardServerLease.acquire(sys.argv[6],'parent')\n"
                "process=ManagedProcess.start([sys.executable,sys.argv[1],sys.argv[2],sys.argv[3]],kind='analysis',cwd=pathlib.Path(sys.argv[1]).parent,env=os.environ.copy(),stdout=log,stderr=log,lifetime_lock_fd=lease.descriptor)\n"
                "pathlib.Path(sys.argv[5]).write_text(str(process.pid),encoding='utf-8')\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            parent = subprocess.Popen(
                [
                    sys.executable,
                    str(parent_script),
                    str(worker_script),
                    str(worker_pid_path),
                    str(grandchild_pid_path),
                    str(log_path),
                    str(supervisor_pid_path),
                    str(output_dir),
                ]
            )
            supervisor_pid = 0
            try:
                supervisor_pid = int(self.wait_for_file(supervisor_pid_path))
                worker_pid = int(self.wait_for_file(worker_pid_path))
                grandchild_pid = int(self.wait_for_file(grandchild_pid_path))
                os.kill(parent.pid, signal.SIGKILL)
                parent.wait(timeout=5)
                with self.assertRaises(server_runtime.DashboardServerBusy):
                    server_runtime.DashboardServerLease.acquire(output_dir, "too-early")
                deadline = time.monotonic() + 12.0
                while time.monotonic() < deadline:
                    if all(self.process_gone(pid) for pid in (supervisor_pid, worker_pid, grandchild_pid)):
                        break
                    time.sleep(0.05)
                self.assertTrue(self.process_gone(supervisor_pid))
                self.assertTrue(self.process_gone(worker_pid))
                self.assertTrue(self.process_gone(grandchild_pid))
                replacement = server_runtime.DashboardServerLease.acquire(output_dir, "replacement")
                replacement.close()
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                if supervisor_pid and not self.process_gone(supervisor_pid):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(supervisor_pid, signal.SIGKILL)

    def test_official_cli_sigterm_stops_server_and_releases_lifetime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            codex_dir = root / "codex"
            output_dir = root / "output"
            config_home = root / "config"
            codex_dir.mkdir()
            service_paths.write_config(
                {"codex_dir": codex_dir, "output_dir": output_dir},
                config_home / "bola" / "runtime.conf",
            )
            environment = os.environ.copy()
            environment["XDG_CONFIG_HOME"] = str(config_home)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = int(listener.getsockname()[1])
            base_url = f"http://127.0.0.1:{port}"
            log_path = root / "server.log"
            with log_path.open("w+", encoding="utf-8") as log:
                server = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "codex_token_bola",
                        "serve",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--codex-dir",
                        str(codex_dir),
                        "--output-dir",
                        str(output_dir),
                    ],
                    cwd=str(ROOT),
                    stdout=log,
                    stderr=log,
                    env=environment,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 5.0
                    while True:
                        try:
                            with urllib.request.urlopen(base_url, timeout=0.2):
                                break
                        except (OSError, urllib.error.URLError):
                            if server.poll() is not None or time.monotonic() >= deadline:
                                log.seek(0)
                                self.fail(f"dashboard server did not start: {log.read()}")
                            time.sleep(0.05)
                    operation_id = "11111111-1111-4111-8111-111111111111"
                    body = json.dumps({"operation_id": operation_id}).encode("utf-8")
                    request = urllib.request.Request(
                        f"{base_url}/api/rebuild",
                        data=body,
                        headers={"Origin": base_url, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=15) as response:
                        payload = json.loads(response.read())
                    self.assertTrue(payload["ok"])
                    server.terminate()
                    self.assertEqual(server.wait(timeout=5), 0)
                    with self.assertRaises((OSError, urllib.error.URLError)):
                        urllib.request.urlopen(base_url, timeout=0.2)
                    replacement = server_runtime.DashboardServerLease.acquire(output_dir, "replacement")
                    replacement.close()
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(server.pid, signal.SIGKILL)
                    if server.poll() is None:
                        server.wait(timeout=5)
