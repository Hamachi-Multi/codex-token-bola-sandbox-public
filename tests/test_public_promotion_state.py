from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest


PUBLIC_PROMOTION_STATE_PATH = ROOT / "scripts" / "public_promotion_state.py"


def load_public_promotion_state_module():
    return load_module("public_promotion_state_test", PUBLIC_PROMOTION_STATE_PATH)


class FakeGitHubClient:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses
        self.paths: list[str] = []

    def request_json(self, path: str) -> dict[str, object]:
        self.paths.append(path)
        return self.responses[path]


def check_run(name: str, conclusion: str | None, status: str = "completed") -> dict[str, object]:
    return {"name": name, "conclusion": conclusion, "status": status}


class PublicPromotionStateTests(unittest.TestCase):
    def test_build_public_state_from_refs_and_check_runs(self) -> None:
        module = load_public_promotion_state_module()
        candidate_sha = "b" * 40
        main_sha = "a" * 40
        client = FakeGitHubClient(
            {
                "/repos/owner/repo/git/ref/heads/release-candidate/2026-07-01-attempt-002": {
                    "object": {"sha": candidate_sha}
                },
                "/repos/owner/repo/git/ref/heads/main": {"object": {"sha": main_sha}},
                f"/repos/owner/repo/commits/{candidate_sha}/check-runs?per_page=100": {
                    "check_runs": [
                        check_run("compile-test", "success"),
                        check_run("asset-static-sanity", "success"),
                        check_run("public-sensitive-guard", "success"),
                        check_run("candidate-snapshot-guard", "success"),
                        check_run("codeql", "success"),
                        check_run("semantic-release", "skipped"),
                    ]
                },
            }
        )

        state = module.build_public_state(
            client,
            repo="owner/repo",
            candidate="2026-07-01",
            attempt=2,
            expected_public_candidate_sha=candidate_sha,
        )

        self.assertEqual(
            state,
            {
                "public_candidate_branch": "release-candidate/2026-07-01-attempt-002",
                "public_candidate_head_sha": candidate_sha,
                "public_main_sha": main_sha,
                "checks": {
                    "public-ci / compile-test": "success",
                    "public-ci / asset-static-sanity": "success",
                    "public-ci / public-sensitive-guard": "success",
                    "public-ci / candidate-snapshot-guard": "success",
                    "codeql": "success",
                },
            },
        )

    def test_check_run_mapping_keeps_failures_and_missing_checks_visible(self) -> None:
        module = load_public_promotion_state_module()

        checks = module.extract_required_check_conclusions(
            {
                "check_runs": [
                    check_run("compile-test", "success"),
                    check_run("public-sensitive-guard", "failure"),
                    check_run("codeql", None, status="in_progress"),
                ]
            }
        )

        self.assertEqual(
            checks,
            {
                "public-ci / compile-test": "success",
                "public-ci / public-sensitive-guard": "failure",
                "codeql": "in_progress",
            },
        )

    def test_expected_candidate_sha_mismatch_fails_before_writing_state(self) -> None:
        module = load_public_promotion_state_module()
        client = FakeGitHubClient(
            {
                "/repos/owner/repo/git/ref/heads/release-candidate/2026-07-01-attempt-002": {
                    "object": {"sha": "b" * 40}
                },
                "/repos/owner/repo/git/ref/heads/main": {"object": {"sha": "a" * 40}},
            }
        )

        with self.assertRaisesRegex(module.InputError, "public candidate head SHA does not match expected SHA"):
            module.build_public_state(
                client,
                repo="owner/repo",
                candidate="2026-07-01",
                attempt=2,
                expected_public_candidate_sha="c" * 40,
            )

    def test_cli_writes_json_state(self) -> None:
        module = load_public_promotion_state_module()
        candidate_sha = "b" * 40
        state = {
            "public_candidate_branch": "release-candidate/2026-07-01-attempt-002",
            "public_candidate_head_sha": candidate_sha,
            "public_main_sha": "a" * 40,
            "checks": {"codeql": "success"},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            output = pathlib.Path(tmp_dir) / "public-state.json"
            original = module.build_public_state
            module.build_public_state = lambda *args, **kwargs: state
            try:
                result = module.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--candidate",
                        "2026-07-01",
                        "--attempt",
                        "2",
                        "--expected-public-candidate-sha",
                        candidate_sha,
                        "--output",
                        str(output),
                    ]
                )
            finally:
                module.build_public_state = original
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(payload, state)

    def test_cli_returns_failure_json_for_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = pathlib.Path(tmp_dir) / "public-state.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(PUBLIC_PROMOTION_STATE_PATH),
                    "--repo",
                    "owner/repo",
                    "--candidate",
                    "../bad",
                    "--attempt",
                    "2",
                    "--expected-public-candidate-sha",
                    "b" * 40,
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("candidate must be a safe relative name", payload["errors"][0])
