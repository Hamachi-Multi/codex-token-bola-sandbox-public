from __future__ import annotations

import re

try:
    from tests.support import ROOT, unittest
except ModuleNotFoundError:
    from support import ROOT, unittest


WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-export.yml"


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def assert_order(testcase: unittest.TestCase, text: str, *needles: str) -> None:
    cursor = -1
    for needle in needles:
        position = text.find(needle)
        testcase.assertNotEqual(position, -1, f"missing workflow fragment: {needle}")
        testcase.assertGreater(position, cursor, f"workflow fragment out of order: {needle}")
        cursor = position


class ReleaseExportWorkflowTests(unittest.TestCase):
    def test_release_export_workflow_is_manual_and_read_only_by_default(self) -> None:
        text = workflow_text()

        self.assertIn("name: private-release-export", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("push:", text)
        self.assertRegex(text, r"permissions:\n  contents: read\n  id-token: none")

    def test_release_export_inputs_are_explicit_release_contract(self) -> None:
        text = workflow_text()

        for input_name in (
            "candidate:",
            "attempt:",
            "release_ref:",
            "public_repo:",
            "snapshot_subject:",
            "snapshot_author_email:",
            "public_candidate_base_sha:",
            "snapshot_manifest_digest:",
        ):
            with self.subTest(input=input_name):
                self.assertIn(input_name, text)

    def test_candidate_checkout_is_read_only_without_environment_or_persisted_credentials(self) -> None:
        text = workflow_text()
        candidate_job = re.search(r"  candidate_input:\n(?P<body>.*?)(?:\n  release_export:|\Z)", text, re.S)
        self.assertIsNotNone(candidate_job)
        body = candidate_job.group("body")

        self.assertIn("uses: actions/checkout@v4", body)
        self.assertIn("ref: ${{ inputs.release_ref }}", body)
        self.assertIn("persist-credentials: false", body)
        self.assertNotIn("environment:", body)
        self.assertNotIn("secrets.", body)

    def test_release_export_job_has_protected_environment_and_main_checkout(self) -> None:
        text = workflow_text()
        release_job = re.search(r"  release_export:\n(?P<body>.*)", text, re.S)
        self.assertIsNotNone(release_job)
        body = release_job.group("body")

        self.assertIn("needs: candidate_input", body)
        self.assertIn("environment: production-release-export", body)
        self.assertIn("uses: actions/checkout@v4", body)
        self.assertIn("persist-credentials: false", body)

    def test_guard_steps_run_before_snapshot_token_mint(self) -> None:
        text = workflow_text()

        assert_order(
            self,
            text,
            "name: Prepare export staging",
            "scripts/release_export_staging.py",
            "scripts/private_export_guard.py",
            "name: Checkout sandbox public main",
            "scripts/public_candidate_sync.py",
            "scripts/public_candidate_surface_review.py",
            "scripts/public_snapshot_commit_policy.py",
            "scripts/release_records.py candidate-prepared",
            "name: Mint snapshot GitHub App token",
            "actions/create-github-app-token@v3",
            "name: Push public candidate branch",
            "scripts/release_records.py candidate-pushed",
        )

    def test_prepare_export_staging_does_not_sync_public_worktree(self) -> None:
        text = workflow_text()
        step = re.search(
            r"      - name: Prepare export staging\n(?P<body>.*?)(?:\n      - name:|\Z)",
            text,
            re.S,
        )
        self.assertIsNotNone(step)

        body = step.group("body")
        self.assertIn("scripts/release_export_staging.py", body)
        self.assertNotIn("scripts/public_candidate_sync.py", body)

    def test_snapshot_token_and_candidate_push_use_environment_secret_contract(self) -> None:
        text = workflow_text()

        self.assertNotIn("snapshot App token mint is not implemented in this skeleton", text)
        self.assertNotIn("public candidate branch push is not implemented in this skeleton", text)
        self.assertIn("uses: actions/create-github-app-token@v3", text)
        self.assertIn("app-id: ${{ secrets.SNAPSHOT_APP_ID }}", text)
        self.assertIn("private-key: ${{ secrets.SNAPSHOT_PRIVATE_KEY }}", text)
        self.assertIn("permission-contents: write", text)
        self.assertIn("repositories: ${{ inputs.public_repo }}", text)
        self.assertIn("git push origin \"HEAD:$PUBLIC_CANDIDATE_BRANCH\"", text)
        self.assertIn("--public-candidate-sha \"$PUBLIC_CANDIDATE_SHA\"", text)
        self.assertIn("--public-worktree public-candidate", text)


if __name__ == "__main__":
    unittest.main()
