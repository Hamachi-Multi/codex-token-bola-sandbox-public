from __future__ import annotations

import shutil

try:
    from tests.support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest


BOOTSTRAP_PATH = ROOT / "scripts" / "public_repo_bootstrap.py"
BOOTSTRAP_ROOT = ROOT / "release" / "public-bootstrap"
REQUIRED_PUBLIC_CONTEXTS = [
    "compile-test",
    "asset-static-sanity",
    "public-sensitive-guard",
    "candidate-snapshot-guard",
    "product-snapshot-guard",
    "semantic-release",
]


def load_bootstrap_module():
    return load_module("public_repo_bootstrap_test", BOOTSTRAP_PATH)


def copy_bootstrap_fixture(target: pathlib.Path) -> pathlib.Path:
    copied = target / "public-bootstrap"
    shutil.copytree(BOOTSTRAP_ROOT, copied)
    return copied


class PublicRepoBootstrapTests(unittest.TestCase):
    def test_current_public_bootstrap_artifacts_pass(self) -> None:
        bootstrap = load_bootstrap_module()

        result = bootstrap.validate_public_bootstrap(BOOTSTRAP_ROOT)

        self.assertEqual(result, {"ok": True, "errors": []})

    def test_required_inventory_files_must_exist(self) -> None:
        bootstrap = load_bootstrap_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = copy_bootstrap_fixture(pathlib.Path(tmp_dir))
            (root / ".github" / "workflows" / "codeql.yml").unlink()

            result = bootstrap.validate_public_bootstrap(root)

        self.assertFalse(result["ok"])
        self.assertIn("missing public bootstrap file: .github/workflows/codeql.yml", result["errors"])

    def test_public_guard_scripts_are_bootstrap_synced(self) -> None:
        for relative in (
            "scripts/public_main_release_guard.py",
            "scripts/public_snapshot_commit_policy.py",
        ):
            with self.subTest(path=relative):
                self.assertEqual(
                    (BOOTSTRAP_ROOT / relative).read_text(encoding="utf-8"),
                    (ROOT / relative).read_text(encoding="utf-8"),
                )

    def test_public_workflow_required_context_names_are_locked(self) -> None:
        bootstrap = load_bootstrap_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = copy_bootstrap_fixture(pathlib.Path(tmp_dir))
            workflow = root / ".github" / "workflows" / "release.yml"
            workflow.write_text(workflow.read_text(encoding="utf-8").replace("asset-static-sanity", "asset sanity"), encoding="utf-8")

            result = bootstrap.validate_public_bootstrap(root)

        self.assertFalse(result["ok"])
        self.assertIn("public release workflow missing required job display name: asset-static-sanity", result["errors"])

    def test_semantic_release_dependencies_and_config_are_locked(self) -> None:
        bootstrap = load_bootstrap_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = copy_bootstrap_fixture(pathlib.Path(tmp_dir))
            package_json = root / "package.json"
            payload = json.loads(package_json.read_text(encoding="utf-8"))
            payload["devDependencies"].pop("@semantic-release/github")
            package_json.write_text(json.dumps(payload), encoding="utf-8")
            release_config = root / ".releaserc.json"
            config = json.loads(release_config.read_text(encoding="utf-8"))
            config["plugins"].append("@semantic-release/git")
            release_config.write_text(json.dumps(config), encoding="utf-8")

            result = bootstrap.validate_public_bootstrap(root)

        self.assertFalse(result["ok"])
        self.assertIn("package.json missing semantic-release dependency: @semantic-release/github", result["errors"])
        self.assertIn("semantic-release baseline must not include plugin: @semantic-release/git", result["errors"])

    def test_codeql_workflow_requires_job_display_name_separately(self) -> None:
        bootstrap = load_bootstrap_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = copy_bootstrap_fixture(pathlib.Path(tmp_dir))
            workflow = root / ".github" / "workflows" / "codeql.yml"
            workflow.write_text(workflow.read_text(encoding="utf-8").replace("    name: codeql", "    name: code-scanning"), encoding="utf-8")

            result = bootstrap.validate_public_bootstrap(root)

        self.assertFalse(result["ok"])
        self.assertIn("codeql workflow missing fragment:     name: codeql", result["errors"])

    def test_codeql_workflow_requires_python_analysis_steps(self) -> None:
        bootstrap = load_bootstrap_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = copy_bootstrap_fixture(pathlib.Path(tmp_dir))
            workflow = root / ".github" / "workflows" / "codeql.yml"
            text = workflow.read_text(encoding="utf-8")
            text = text.replace("github/codeql-action/init@v3", "github/codeql-action/upload-sarif@v3")
            text = text.replace("languages: python", "languages: javascript")
            text = text.replace("github/codeql-action/analyze@v3", "actions/setup-python@v5")
            workflow.write_text(text, encoding="utf-8")

            result = bootstrap.validate_public_bootstrap(root)

        self.assertFalse(result["ok"])
        self.assertIn("codeql workflow missing fragment: github/codeql-action/init@v3", result["errors"])
        self.assertIn("codeql workflow missing fragment: languages: python", result["errors"])
        self.assertIn("codeql workflow missing fragment: github/codeql-action/analyze@v3", result["errors"])

    def test_public_main_guard_script_contract_is_locked(self) -> None:
        bootstrap = load_bootstrap_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = copy_bootstrap_fixture(pathlib.Path(tmp_dir))
            workflow = root / ".github" / "workflows" / "release.yml"
            workflow.write_text(workflow.read_text(encoding="utf-8").replace("scripts/public_main_release_guard.py", "echo guard"), encoding="utf-8")

            result = bootstrap.validate_public_bootstrap(root)

        self.assertFalse(result["ok"])
        self.assertIn("public release workflow must call public main release guard", result["errors"])

    def test_product_snapshot_placeholder_must_be_removed(self) -> None:
        bootstrap = load_bootstrap_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = copy_bootstrap_fixture(pathlib.Path(tmp_dir))
            workflow = root / ".github" / "workflows" / "release.yml"
            text = workflow.read_text(encoding="utf-8")
            text = text.replace("scripts/public_main_release_guard.py", "scripts/public_main_release_guard.py")
            text += "\n# product snapshot actor, subject, and codeql polling run here\n"
            workflow.write_text(text, encoding="utf-8")

            result = bootstrap.validate_public_bootstrap(root)

        self.assertFalse(result["ok"])
        self.assertIn("public release workflow must not keep product snapshot guard placeholder", result["errors"])

    def test_cli_outputs_json_and_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = copy_bootstrap_fixture(pathlib.Path(tmp_dir))
            (root / ".releaserc.json").unlink()

            result = subprocess.run(
                [
                    sys.executable,
                    str(BOOTSTRAP_PATH),
                    "--bootstrap-root",
                    str(root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("missing public bootstrap file: .releaserc.json", payload["errors"])

    def test_makefile_exposes_public_bootstrap_check(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertIn("public-repo-bootstrap-check:", makefile)
        self.assertIn("scripts/public_repo_bootstrap.py --bootstrap-root release/public-bootstrap", makefile)


if __name__ == "__main__":
    unittest.main()
