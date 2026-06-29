from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest


RELEASE_RECORDS_PATH = ROOT / "scripts" / "release_records.py"


def load_release_records_module():
    return load_module("release_records_test", RELEASE_RECORDS_PATH)


class ReleaseRecordsTests(unittest.TestCase):
    def test_candidate_prepared_writer_creates_attempt_and_summary_index(self) -> None:
        records = load_release_records_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"

            result = records.write_candidate_prepared(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                private_main_sha="a" * 40,
                private_release_sha="b" * 40,
                public_candidate_branch="release-candidate/2026-07-01-attempt-001",
                public_candidate_base_sha="c" * 40,
                snapshot_manifest_digest="sha256:" + "d" * 64,
                approver="release-owner",
                created_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:05:00Z",
                checks={"private_compile_test": "passed", "private_ui_check": "passed"},
            )

            attempt = json.loads((records_root / "2026-07-01" / "attempt-001.json").read_text(encoding="utf-8"))
            summary = json.loads((records_root / "2026-07-01.json").read_text(encoding="utf-8"))

        self.assertEqual(result["record"], "release/records/2026-07-01/attempt-001.json")
        self.assertEqual(attempt["candidate"], "2026-07-01")
        self.assertEqual(attempt["attempt"], 1)
        self.assertEqual(attempt["status"], "candidate_prepared")
        self.assertEqual(attempt["public_candidate_sha"], None)
        self.assertEqual(attempt["checks"]["private_compile_test"], "passed")
        self.assertEqual(attempt["checks"]["public_release_candidate_checks"], "pending")
        self.assertEqual(summary["latest_attempt"], 1)
        self.assertEqual(summary["latest_status"], "candidate_prepared")
        self.assertEqual(summary["latest_record"], "release/records/2026-07-01/attempt-001.json")

    def test_candidate_pushed_updater_sets_public_candidate_sha_and_summary(self) -> None:
        records = load_release_records_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"
            records.write_candidate_prepared(
                records_root,
                candidate="2026-07-01",
                attempt=2,
                private_main_sha="a" * 40,
                private_release_sha="b" * 40,
                public_candidate_branch="release-candidate/2026-07-01-attempt-002",
                public_candidate_base_sha="c" * 40,
                snapshot_manifest_digest="sha256:" + "d" * 64,
                approver="release-owner",
                created_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:05:00Z",
            )

            result = records.mark_candidate_pushed(
                records_root,
                candidate="2026-07-01",
                attempt=2,
                public_candidate_sha="e" * 40,
                updated_at="2026-07-01T00:10:00Z",
            )

            attempt = json.loads((records_root / "2026-07-01" / "attempt-002.json").read_text(encoding="utf-8"))
            summary = json.loads((records_root / "2026-07-01.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "candidate_pushed")
        self.assertEqual(attempt["status"], "candidate_pushed")
        self.assertEqual(attempt["public_candidate_sha"], "e" * 40)
        self.assertEqual(attempt["updated_at"], "2026-07-01T00:10:00Z")
        self.assertEqual(summary["latest_status"], "candidate_pushed")
        self.assertEqual(summary["public_main_sha"], "")

    def test_promoted_updater_sets_public_main_sha_and_summary(self) -> None:
        records = load_release_records_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"
            records.write_candidate_prepared(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                private_main_sha="a" * 40,
                private_release_sha="b" * 40,
                public_candidate_branch="release-candidate/2026-07-01-attempt-001",
                public_candidate_base_sha="c" * 40,
                snapshot_manifest_digest="sha256:" + "d" * 64,
                approver="release-owner",
                created_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:05:00Z",
            )
            records.mark_candidate_pushed(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                public_candidate_sha="e" * 40,
                updated_at="2026-07-01T00:10:00Z",
            )

            result = records.mark_promoted(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                public_main_sha="e" * 40,
                updated_at="2026-07-01T00:15:00Z",
            )

            attempt = json.loads((records_root / "2026-07-01" / "attempt-001.json").read_text(encoding="utf-8"))
            summary = json.loads((records_root / "2026-07-01.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(attempt["status"], "promoted")
        self.assertEqual(attempt["public_main_sha"], "e" * 40)
        self.assertEqual(attempt["checks"]["public_main_promotion"], "passed")
        self.assertEqual(summary["latest_status"], "promoted")
        self.assertEqual(summary["public_main_sha"], "e" * 40)

    def test_published_updater_sets_release_metadata(self) -> None:
        records = load_release_records_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"
            records.write_candidate_prepared(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                private_main_sha="a" * 40,
                private_release_sha="b" * 40,
                public_candidate_branch="release-candidate/2026-07-01-attempt-001",
                public_candidate_base_sha="c" * 40,
                snapshot_manifest_digest="sha256:" + "d" * 64,
                approver="release-owner",
                created_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:05:00Z",
            )
            records.mark_candidate_pushed(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                public_candidate_sha="e" * 40,
                updated_at="2026-07-01T00:10:00Z",
            )
            records.mark_promoted(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                public_main_sha="e" * 40,
                updated_at="2026-07-01T00:15:00Z",
            )

            result = records.mark_published(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                version="1.2.3",
                tag="v1.2.3",
                github_release_url="https://github.com/example/repo/releases/tag/v1.2.3",
                updated_at="2026-07-01T00:20:00Z",
            )

            attempt = json.loads((records_root / "2026-07-01" / "attempt-001.json").read_text(encoding="utf-8"))
            summary = json.loads((records_root / "2026-07-01.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "published")
        self.assertEqual(attempt["status"], "published")
        self.assertEqual(attempt["version"], "1.2.3")
        self.assertEqual(attempt["tag"], "v1.2.3")
        self.assertEqual(attempt["checks"]["semantic_release"], "passed")
        self.assertEqual(summary["latest_status"], "published")
        self.assertEqual(summary["version"], "1.2.3")
        self.assertEqual(summary["tag"], "v1.2.3")

    def test_failed_updater_sets_failure_fields_from_candidate_pushed(self) -> None:
        records = load_release_records_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"
            records.write_candidate_prepared(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                private_main_sha="a" * 40,
                private_release_sha="b" * 40,
                public_candidate_branch="release-candidate/2026-07-01-attempt-001",
                public_candidate_base_sha="c" * 40,
                snapshot_manifest_digest="sha256:" + "d" * 64,
                approver="release-owner",
                created_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:05:00Z",
            )
            records.mark_candidate_pushed(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                public_candidate_sha="e" * 40,
                updated_at="2026-07-01T00:10:00Z",
            )

            result = records.mark_failed(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                failure_stage="promotion_eligibility",
                failure_reason="public main drifted",
                updated_at="2026-07-01T00:12:00Z",
            )

            attempt = json.loads((records_root / "2026-07-01" / "attempt-001.json").read_text(encoding="utf-8"))
            summary = json.loads((records_root / "2026-07-01.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["failure_stage"], "promotion_eligibility")
        self.assertEqual(attempt["failure_reason"], "public main drifted")
        self.assertEqual(summary["latest_status"], "failed")

    def test_promoted_requires_candidate_pushed_state(self) -> None:
        records = load_release_records_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"
            records.write_candidate_prepared(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                private_main_sha="a" * 40,
                private_release_sha="b" * 40,
                public_candidate_branch="release-candidate/2026-07-01-attempt-001",
                public_candidate_base_sha="c" * 40,
                snapshot_manifest_digest="sha256:" + "d" * 64,
                approver="release-owner",
                created_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:05:00Z",
            )

            with self.assertRaisesRegex(records.InputError, "invalid release record transition"):
                records.mark_promoted(
                    records_root,
                    candidate="2026-07-01",
                    attempt=1,
                    public_main_sha="e" * 40,
                    updated_at="2026-07-01T00:15:00Z",
                )

    def test_candidate_pushed_requires_candidate_prepared_state(self) -> None:
        records = load_release_records_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"
            records.write_candidate_prepared(
                records_root,
                candidate="2026-07-01",
                attempt=1,
                private_main_sha="a" * 40,
                private_release_sha="b" * 40,
                public_candidate_branch="release-candidate/2026-07-01-attempt-001",
                public_candidate_base_sha="c" * 40,
                snapshot_manifest_digest="sha256:" + "d" * 64,
                approver="release-owner",
                created_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:05:00Z",
            )
            attempt_path = records_root / "2026-07-01" / "attempt-001.json"
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["status"] = "approved"
            attempt_path.write_text(json.dumps(attempt), encoding="utf-8")

            with self.assertRaisesRegex(records.InputError, "invalid release record transition"):
                records.mark_candidate_pushed(
                    records_root,
                    candidate="2026-07-01",
                    attempt=1,
                    public_candidate_sha="e" * 40,
                    updated_at="2026-07-01T00:10:00Z",
                )

    def test_candidate_path_traversal_is_rejected(self) -> None:
        records = load_release_records_module()

        with self.assertRaisesRegex(records.InputError, "candidate must be a safe relative name"):
            records.attempt_record_path(pathlib.Path("/tmp/release/records"), "../escape", 1)

    def test_candidate_prepared_requires_matching_public_candidate_branch(self) -> None:
        records = load_release_records_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"

            with self.assertRaisesRegex(records.InputError, "public candidate branch must match candidate and attempt"):
                records.write_candidate_prepared(
                    records_root,
                    candidate="2026-07-01",
                    attempt=3,
                    private_main_sha="a" * 40,
                    private_release_sha="b" * 40,
                    public_candidate_branch="release-candidate/2026-07-01-attempt-002",
                    public_candidate_base_sha="c" * 40,
                    snapshot_manifest_digest="sha256:" + "d" * 64,
                    approver="release-owner",
                    created_at="2026-07-01T00:00:00Z",
                    updated_at="2026-07-01T00:05:00Z",
                )

    def test_staged_path_guard_allows_release_records_only(self) -> None:
        records = load_release_records_module()

        result = records.validate_staged_record_paths(
            [
                "release/records/2026-07-01/attempt-001.json",
                "release/records/2026-07-01.json",
            ]
        )

        self.assertEqual(result, {"ok": True, "errors": []})

    def test_staged_path_guard_rejects_paths_outside_release_records(self) -> None:
        records = load_release_records_module()

        result = records.validate_staged_record_paths(
            [
                "release/records/2026-07-01/attempt-001.json",
                "README.md",
                "release/export-manifest.json",
            ]
        )

        self.assertFalse(result["ok"])
        self.assertIn("staged path outside release records: README.md", result["errors"])
        self.assertIn("staged path outside release records: release/export-manifest.json", result["errors"])

    def test_cli_writes_candidate_prepared_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"

            result = subprocess.run(
                [
                    sys.executable,
                    str(RELEASE_RECORDS_PATH),
                    "candidate-prepared",
                    "--records-root",
                    str(records_root),
                    "--candidate",
                    "2026-07-01",
                    "--attempt",
                    "1",
                    "--private-main-sha",
                    "a" * 40,
                    "--private-release-sha",
                    "b" * 40,
                    "--public-candidate-branch",
                    "release-candidate/2026-07-01-attempt-001",
                    "--public-candidate-base-sha",
                    "c" * 40,
                    "--snapshot-manifest-digest",
                    "sha256:" + "d" * 64,
                    "--approver",
                    "release-owner",
                    "--created-at",
                    "2026-07-01T00:00:00Z",
                    "--updated-at",
                    "2026-07-01T00:05:00Z",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            attempt_exists = (records_root / "2026-07-01" / "attempt-001.json").is_file()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["ok"])
        self.assertTrue(attempt_exists)
        self.assertEqual(payload["record"], "release/records/2026-07-01/attempt-001.json")

    def test_cli_guard_staged_paths_file_uses_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            paths_file = pathlib.Path(tmp_dir) / "staged.txt"
            paths_file.write_text("release/records/2026-07-01.json\nscripts/release_records.py\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(RELEASE_RECORDS_PATH),
                    "guard-staged",
                    "--paths-file",
                    str(paths_file),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("staged path outside release records: scripts/release_records.py", payload["errors"])


if __name__ == "__main__":
    unittest.main()
