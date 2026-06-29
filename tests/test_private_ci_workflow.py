from __future__ import annotations

import re

try:
    from tests.support import ROOT, unittest
except ModuleNotFoundError:
    from support import ROOT, unittest


WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def assert_order(testcase: unittest.TestCase, text: str, *needles: str) -> None:
    cursor = -1
    for needle in needles:
        position = text.find(needle)
        testcase.assertNotEqual(position, -1, f"missing workflow fragment: {needle}")
        testcase.assertGreater(position, cursor, f"workflow fragment out of order: {needle}")
        cursor = position


class PrivateCiWorkflowTests(unittest.TestCase):
    def test_private_ci_workflow_has_read_only_permissions(self) -> None:
        text = workflow_text()

        self.assertIn("name: private-ci", text)
        self.assertIn("push:", text)
        self.assertIn("pull_request:", text)
        self.assertRegex(text, r"permissions:\n  contents: read\n  id-token: none")

    def test_compile_test_job_uses_make_gates(self) -> None:
        text = workflow_text()
        job = re.search(r"  compile_test:\n(?P<body>.*?)(?:\n  [a-z_]+:\n|\Z)", text, re.S)

        self.assertIsNotNone(job)
        body = job.group("body")
        self.assertIn("name: compile-test", body)
        self.assertIn("uses: actions/checkout@v4", body)
        self.assertIn("uses: actions/setup-python@v5", body)
        assert_order(self, body, "make compile", "make test")

    def test_dashboard_ui_job_installs_ui_dependencies_and_runs_fixture_check(self) -> None:
        text = workflow_text()
        job = re.search(r"  dashboard_ui:\n(?P<body>.*?)(?:\n  [a-z_]+:\n|\Z)", text, re.S)

        self.assertIsNotNone(job)
        body = job.group("body")
        self.assertIn("name: dashboard-ui", body)
        self.assertIn('python3 -m pip install ".[ui]"', body)
        assert_order(self, body, "make playwright-install", "make ui-check")
        self.assertNotIn("ui-check-live", body)


if __name__ == "__main__":
    unittest.main()
