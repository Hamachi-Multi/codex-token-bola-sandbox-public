from __future__ import annotations

import shutil

try:
    from tests.support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest


READINESS_PATH = ROOT / "scripts" / "sandbox_dry_run_readiness.py"
CONFIG_PATH = ROOT / "release" / "sandbox-dry-run.example.json"
BOOTSTRAP_ROOT = ROOT / "release" / "public-bootstrap"
REQUIRED_PUBLIC_CHECKS = [
    "public-ci / compile-test",
    "public-ci / asset-static-sanity",
    "public-ci / public-sensitive-guard",
    "public-ci / candidate-snapshot-guard",
    "codeql",
]
REQUIRED_ENVIRONMENT_SECRETS = {
    "private": {
        "production-release-export": ["SNAPSHOT_APP_ID", "SNAPSHOT_PRIVATE_KEY"],
        "production-main-promotion": ["PROMOTION_APP_ID", "PROMOTION_PRIVATE_KEY"],
        "production-release-record-update": ["RELEASE_RECORD_APP_ID", "RELEASE_RECORD_PRIVATE_KEY"],
    },
    "public": {
        "public-release-publish": ["RELEASE_TAG_APP_ID", "RELEASE_TAG_PRIVATE_KEY"],
        "public-orphan-tag-recovery": ["ORPHAN_TAG_RECOVERY_APP_ID", "ORPHAN_TAG_RECOVERY_PRIVATE_KEY"],
    },
}
REQUIRED_PUBLIC_REPO_VARIABLES = ["PROMOTION_APP_ACTOR", "SNAPSHOT_AUTHOR_EMAIL", "PUBLIC_OPS_ACTOR"]


def load_readiness_module():
    return load_module("sandbox_dry_run_readiness_test", READINESS_PATH)


def valid_config(**overrides) -> dict[str, object]:
    config: dict[str, object] = {
        "sandbox_public_repo": "example/token-usage-public-sandbox",
        "public_main_branch": "main",
        "candidate": "2026-07-01",
        "attempt": 1,
        "public_candidate_branch": "release-candidate/2026-07-01-attempt-001",
        "public_candidate_base_sha": "a" * 40,
        "expected_public_candidate_sha": "b" * 40,
        "required_public_checks": list(REQUIRED_PUBLIC_CHECKS),
        "protected_environments": {
            "release_export": "production-release-export",
            "main_promotion": "production-main-promotion",
            "release_publish": "public-release-publish",
        },
        "github_apps": {
            "snapshot": {"app_slug": "token-usage-snapshot-sandbox", "actor": "token-usage-snapshot[bot]"},
            "promotion": {"app_slug": "token-usage-promotion-sandbox", "actor": "token-usage-promotion[bot]"},
            "release_tag": {"app_slug": "token-usage-release-tag-sandbox", "actor": "token-usage-release-tag[bot]"},
        },
        "environment_secrets": REQUIRED_ENVIRONMENT_SECRETS,
        "public_repo_variables": list(REQUIRED_PUBLIC_REPO_VARIABLES),
        "run_mode": "readiness_only",
    }
    config.update(overrides)
    return config


def write_config(path: pathlib.Path, payload: dict[str, object]) -> pathlib.Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class SandboxDryRunReadinessTests(unittest.TestCase):
    def test_readiness_script_exists(self) -> None:
        self.assertTrue(READINESS_PATH.is_file())

    def test_current_example_config_passes(self) -> None:
        readiness = load_readiness_module()

        result = readiness.validate_sandbox_dry_run_readiness(ROOT, CONFIG_PATH)

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["errors"], [])
        self.assertIn("replace example sandbox_public_repo with the live sandbox repo before live run", result["next_inputs"])

    def test_required_fields_must_exist(self) -> None:
        readiness = load_readiness_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = valid_config()
            config.pop("run_mode")
            config_path = write_config(pathlib.Path(tmp_dir) / "config.json", config)

            result = readiness.validate_sandbox_dry_run_readiness(ROOT, config_path)

        self.assertFalse(result["ok"])
        self.assertIn("missing config field: run_mode", result["errors"])

    def test_sandbox_repo_must_be_owner_repo(self) -> None:
        readiness = load_readiness_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = write_config(pathlib.Path(tmp_dir) / "config.json", valid_config(sandbox_public_repo="token-usage-public-sandbox"))

            result = readiness.validate_sandbox_dry_run_readiness(ROOT, config_path)

        self.assertFalse(result["ok"])
        self.assertIn("sandbox_public_repo must use owner/repo format", result["errors"])

    def test_candidate_attempt_and_branch_must_match(self) -> None:
        readiness = load_readiness_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = write_config(
                pathlib.Path(tmp_dir) / "config.json",
                valid_config(public_candidate_branch="release-candidate/2026-07-01-attempt-002"),
            )

            result = readiness.validate_sandbox_dry_run_readiness(ROOT, config_path)

        self.assertFalse(result["ok"])
        self.assertIn(
            "public candidate branch must match candidate and attempt: expected release-candidate/2026-07-01-attempt-001",
            result["errors"],
        )

    def test_sha_fields_must_be_lowercase_hex(self) -> None:
        readiness = load_readiness_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = write_config(pathlib.Path(tmp_dir) / "config.json", valid_config(expected_public_candidate_sha="B" * 40))

            result = readiness.validate_sandbox_dry_run_readiness(ROOT, config_path)

        self.assertFalse(result["ok"])
        self.assertIn("expected public candidate SHA must be a 40 character lowercase hex SHA", result["errors"])

    def test_required_public_checks_must_match_promotion_contract(self) -> None:
        readiness = load_readiness_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            checks = list(REQUIRED_PUBLIC_CHECKS)
            checks.pop()
            config_path = write_config(pathlib.Path(tmp_dir) / "config.json", valid_config(required_public_checks=checks))

            result = readiness.validate_sandbox_dry_run_readiness(ROOT, config_path)

        self.assertFalse(result["ok"])
        self.assertIn("required_public_checks must match promotion eligibility required checks", result["errors"])

    def test_environment_secret_contract_must_match_current_github_layout(self) -> None:
        readiness = load_readiness_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = valid_config()
            environment_secrets = dict(config["environment_secrets"])
            private_secrets = dict(environment_secrets["private"])
            private_secrets["production-release-export"] = ["SNAPSHOT_APP_ID", "SNAPSHOT_APP_PRIVATE_KEY"]
            environment_secrets["private"] = private_secrets
            config["environment_secrets"] = environment_secrets
            config_path = write_config(pathlib.Path(tmp_dir) / "config.json", config)

            result = readiness.validate_sandbox_dry_run_readiness(ROOT, config_path)

        self.assertFalse(result["ok"])
        self.assertIn(
            "environment_secrets.private.production-release-export must be: SNAPSHOT_APP_ID, SNAPSHOT_PRIVATE_KEY",
            result["errors"],
        )

    def test_public_guard_variables_must_be_declared(self) -> None:
        readiness = load_readiness_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            variables = list(REQUIRED_PUBLIC_REPO_VARIABLES)
            variables.remove("PUBLIC_OPS_ACTOR")
            config_path = write_config(pathlib.Path(tmp_dir) / "config.json", valid_config(public_repo_variables=variables))

            result = readiness.validate_sandbox_dry_run_readiness(ROOT, config_path)

        self.assertFalse(result["ok"])
        self.assertIn("public_repo_variables must be: PROMOTION_APP_ACTOR, SNAPSHOT_AUTHOR_EMAIL, PUBLIC_OPS_ACTOR", result["errors"])

    def test_public_bootstrap_errors_are_reported(self) -> None:
        readiness = load_readiness_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            bootstrap = root / "release" / "public-bootstrap"
            shutil.copytree(BOOTSTRAP_ROOT, bootstrap)
            (bootstrap / ".releaserc.json").unlink()
            config_path = write_config(root / "config.json", valid_config())

            result = readiness.validate_sandbox_dry_run_readiness(root, config_path)

        self.assertFalse(result["ok"])
        self.assertIn("public bootstrap: missing public bootstrap file: .releaserc.json", result["errors"])

    def test_cli_outputs_json_and_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = write_config(pathlib.Path(tmp_dir) / "config.json", valid_config(sandbox_public_repo="bad"))

            result = subprocess.run(
                [
                    sys.executable,
                    str(READINESS_PATH),
                    "--repo-root",
                    str(ROOT),
                    "--config",
                    str(config_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("sandbox_public_repo must use owner/repo format", payload["errors"])

    def test_makefile_exposes_readiness_target(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertIn("sandbox-dry-run-readiness:", makefile)
        self.assertIn("scripts/sandbox_dry_run_readiness.py --repo-root . --config release/sandbox-dry-run.example.json", makefile)


if __name__ == "__main__":
    unittest.main()
