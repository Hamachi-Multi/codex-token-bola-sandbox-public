from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest


PROMOTION_ELIGIBILITY_PATH = ROOT / "scripts" / "promotion_eligibility.py"
RELEASE_RECORDS_PATH = ROOT / "scripts" / "release_records.py"
REQUIRED_CHECKS = [
    "public-ci / compile-test",
    "public-ci / asset-static-sanity",
    "public-ci / public-sensitive-guard",
    "public-ci / candidate-snapshot-guard",
    "codeql",
]


def load_promotion_module():
    return load_module("promotion_eligibility_test", PROMOTION_ELIGIBILITY_PATH)


def load_records_module():
    return load_module("release_records_for_promotion_test", RELEASE_RECORDS_PATH)


def seed_candidate_pushed_record(records_root: pathlib.Path) -> pathlib.Path:
    records = load_records_module()
    records.write_candidate_prepared(
        records_root,
        candidate="2026-07-01",
        attempt=1,
        release_ref="main",
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
    return records_root / "2026-07-01" / "attempt-001.json"


def matching_public_state(**overrides) -> dict[str, object]:
    state = {
        "public_candidate_branch": "release-candidate/2026-07-01-attempt-001",
        "public_candidate_head_sha": "e" * 40,
        "public_main_sha": "c" * 40,
        "checks": {name: "success" for name in REQUIRED_CHECKS},
    }
    state.update(overrides)
    return state


class PromotionEligibilityTests(unittest.TestCase):
    def test_matching_record_and_public_state_are_eligible(self) -> None:
        promotion = load_promotion_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"
            record_path = seed_candidate_pushed_record(records_root)

            result = promotion.validate_promotion_eligibility(
                record_path,
                matching_public_state(),
                expected_public_candidate_sha="e" * 40,
            )

        self.assertEqual(result, {"ok": True, "errors": [], "promotion_target_sha": "e" * 40})

    def test_status_must_be_candidate_pushed(self) -> None:
        promotion = load_promotion_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            records_root = pathlib.Path(tmp_dir) / "release" / "records"
            record_path = seed_candidate_pushed_record(records_root)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["status"] = "candidate_prepared"
            record_path.write_text(json.dumps(record), encoding="utf-8")

            result = promotion.validate_promotion_eligibility(
                record_path,
                matching_public_state(),
                expected_public_candidate_sha="e" * 40,
            )

        self.assertFalse(result["ok"])
        self.assertIn("release record status must be candidate_pushed", result["errors"])

    def test_expected_sha_must_match_record_and_public_head(self) -> None:
        promotion = load_promotion_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            record_path = seed_candidate_pushed_record(pathlib.Path(tmp_dir) / "release" / "records")

            result = promotion.validate_promotion_eligibility(
                record_path,
                matching_public_state(public_candidate_head_sha="f" * 40),
                expected_public_candidate_sha="f" * 40,
            )

        self.assertFalse(result["ok"])
        self.assertIn("release record public_candidate_sha does not match expected public candidate SHA", result["errors"])
        self.assertIn("public candidate head SHA does not match release record public_candidate_sha", result["errors"])

    def test_public_main_must_match_recorded_base_sha(self) -> None:
        promotion = load_promotion_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            record_path = seed_candidate_pushed_record(pathlib.Path(tmp_dir) / "release" / "records")

            result = promotion.validate_promotion_eligibility(
                record_path,
                matching_public_state(public_main_sha="f" * 40),
                expected_public_candidate_sha="e" * 40,
            )

        self.assertFalse(result["ok"])
        self.assertIn("public main SHA does not match recorded public candidate base SHA", result["errors"])

    def test_required_checks_must_be_success(self) -> None:
        promotion = load_promotion_module()
        checks = {name: "success" for name in REQUIRED_CHECKS}
        checks["public-ci / public-sensitive-guard"] = "failure"
        checks.pop("codeql")
        with tempfile.TemporaryDirectory() as tmp_dir:
            record_path = seed_candidate_pushed_record(pathlib.Path(tmp_dir) / "release" / "records")

            result = promotion.validate_promotion_eligibility(
                record_path,
                matching_public_state(checks=checks),
                expected_public_candidate_sha="e" * 40,
            )

        self.assertFalse(result["ok"])
        self.assertIn("required public check failed: public-ci / public-sensitive-guard=failure", result["errors"])
        self.assertIn("required public check missing: codeql", result["errors"])

    def test_cli_outputs_json_and_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            record_path = seed_candidate_pushed_record(base / "release" / "records")
            public_state_path = base / "public-state.json"
            public_state_path.write_text(json.dumps(matching_public_state(public_main_sha="f" * 40)), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROMOTION_ELIGIBILITY_PATH),
                    "--record",
                    str(record_path),
                    "--public-state",
                    str(public_state_path),
                    "--expected-public-candidate-sha",
                    "e" * 40,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("public main SHA does not match recorded public candidate base SHA", payload["errors"])


if __name__ == "__main__":
    unittest.main()
