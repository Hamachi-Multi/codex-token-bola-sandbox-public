from __future__ import annotations

import re

try:
    from tests.support import ROOT, unittest
except ModuleNotFoundError:
    from support import ROOT, unittest


WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-promote.yml"


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def assert_order(testcase: unittest.TestCase, text: str, *needles: str) -> None:
    cursor = -1
    for needle in needles:
        position = text.find(needle)
        testcase.assertNotEqual(position, -1, f"missing workflow fragment: {needle}")
        testcase.assertGreater(position, cursor, f"workflow fragment out of order: {needle}")
        cursor = position


class ReleasePromoteWorkflowTests(unittest.TestCase):
    def test_release_promote_workflow_is_manual_and_read_only_by_default(self) -> None:
        text = workflow_text()

        self.assertIn("name: private-release-promote", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("push:", text)
        self.assertRegex(text, r"permissions:\n  contents: read\n  id-token: none")

    def test_release_promote_inputs_are_explicit(self) -> None:
        text = workflow_text()

        for input_name in ("candidate:", "attempt:", "expected_public_candidate_sha:"):
            with self.subTest(input=input_name):
                self.assertIn(input_name, text)

    def test_promote_job_has_protected_environment(self) -> None:
        text = workflow_text()
        job = re.search(r"  promote:\n(?P<body>.*)", text, re.S)

        self.assertIsNotNone(job)
        body = job.group("body")
        self.assertIn("environment: production-main-promotion", body)
        self.assertIn("uses: actions/checkout@v4", body)
        self.assertIn("persist-credentials: false", body)

    def test_eligibility_runs_before_token_mint_and_promotion_placeholders(self) -> None:
        text = workflow_text()

        assert_order(
            self,
            text,
            "name: Prepare observed public state placeholder",
            "scripts/promotion_eligibility.py",
            "name: Mint promotion GitHub App token placeholder",
            "name: Fast-forward public main placeholder",
            "scripts/release_records.py promoted",
        )

    def test_token_mint_and_fast_forward_are_placeholders(self) -> None:
        text = workflow_text()

        self.assertIn(
            'echo "::notice title=placeholder::promotion App token mint is not implemented in this skeleton"',
            text,
        )
        self.assertIn(
            'echo "::notice title=placeholder::public main fast-forward push is not implemented in this skeleton"',
            text,
        )
        self.assertNotIn("actions/create-github-app-token", text)
        self.assertNotIn("private-key", text)
        self.assertNotIn("git push", text)


if __name__ == "__main__":
    unittest.main()
