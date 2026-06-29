from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, subprocess, sys, tempfile, unittest


GUARD_PATH = ROOT / "scripts" / "public_main_release_guard.py"


def load_guard_module():
    return load_module("public_main_release_guard_test", GUARD_PATH)


def product_snapshot_kwargs(**overrides) -> dict[str, str]:
    kwargs = {
        "ref": "refs/heads/main",
        "message": "feat: add dashboard release\n\nPublic changes:\n- Add dashboard release\n",
        "actor": "ctb-sandbox-promotion-app[bot]",
        "expected_promotion_actor": "ctb-sandbox-promotion-app[bot]",
        "author_email": "ctb-sandbox-snapshot-app[bot]@users.noreply.github.com",
        "expected_snapshot_author_email": "ctb-sandbox-snapshot-app[bot]@users.noreply.github.com",
        "codeql_conclusion": "success",
        "public_ops_actor": "public-ops-maintainer",
    }
    kwargs.update(overrides)
    return kwargs


class PublicMainReleaseGuardTests(unittest.TestCase):
    def test_product_snapshot_allows_semantic_release(self) -> None:
        guard = load_guard_module()

        result = guard.validate_public_main_release(**product_snapshot_kwargs())

        self.assertEqual(
            result,
            {
                "ok": True,
                "errors": [],
                "release_kind": "product_snapshot",
                "semantic_release": True,
            },
        )

    def test_fix_scope_breaking_snapshot_allows_semantic_release(self) -> None:
        guard = load_guard_module()

        result = guard.validate_public_main_release(**product_snapshot_kwargs(message="fix(api)!: repair export\n"))

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["release_kind"], "product_snapshot")
        self.assertTrue(result["semantic_release"])

    def test_public_ops_skips_semantic_release(self) -> None:
        guard = load_guard_module()

        result = guard.validate_public_main_release(
            **product_snapshot_kwargs(
                message="chore(public-ops): update release workflow\n",
                actor="public-ops-maintainer",
                codeql_conclusion="",
            )
        )

        self.assertEqual(
            result,
            {
                "ok": True,
                "errors": [],
                "release_kind": "public_ops",
                "semantic_release": False,
            },
        )

    def test_product_snapshot_rejects_wrong_actor_author_and_codeql(self) -> None:
        guard = load_guard_module()

        result = guard.validate_public_main_release(
            **product_snapshot_kwargs(
                actor="ctb-sandbox-snapshot-app[bot]",
                author_email="human@example.com",
                codeql_conclusion="failure",
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["release_kind"], "product_snapshot")
        self.assertFalse(result["semantic_release"])
        self.assertIn("product snapshot push actor must match expected promotion actor", result["errors"])
        self.assertIn("product snapshot author email must match expected snapshot author email", result["errors"])
        self.assertIn("product snapshot codeql conclusion must be success", result["errors"])

    def test_invalid_subject_fails_without_semantic_release(self) -> None:
        guard = load_guard_module()

        result = guard.validate_public_main_release(**product_snapshot_kwargs(message="docs: update public readme\n"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["release_kind"], "invalid")
        self.assertFalse(result["semantic_release"])
        self.assertIn("snapshot subject type must be one of: feat, fix", result["errors"])

    def test_public_ops_requires_public_ops_actor(self) -> None:
        guard = load_guard_module()

        result = guard.validate_public_main_release(
            **product_snapshot_kwargs(message="chore(public-ops): update release workflow\n", actor="ctb-sandbox-promotion-app[bot]")
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["release_kind"], "public_ops")
        self.assertIn("public ops push actor must match expected public ops actor", result["errors"])

    def test_cli_outputs_json_and_github_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            message_file = base / "message.txt"
            output_file = base / "github-output.txt"
            message_file.write_text("feat: add dashboard release\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(GUARD_PATH),
                    "--ref",
                    "refs/heads/main",
                    "--message-file",
                    str(message_file),
                    "--actor",
                    "ctb-sandbox-promotion-app[bot]",
                    "--expected-promotion-actor",
                    "ctb-sandbox-promotion-app[bot]",
                    "--author-email",
                    "ctb-sandbox-snapshot-app[bot]@users.noreply.github.com",
                    "--expected-snapshot-author-email",
                    "ctb-sandbox-snapshot-app[bot]@users.noreply.github.com",
                    "--codeql-conclusion",
                    "success",
                    "--public-ops-actor",
                    "public-ops-maintainer",
                    "--github-output",
                    str(output_file),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            output_text = output_file.read_text(encoding="utf-8")

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(output_text, "release_kind=product_snapshot\nsemantic_release=true\n")

    def test_cli_uses_failure_exit_code(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(GUARD_PATH),
                "--ref",
                "refs/heads/main",
                "--subject",
                "docs: update public readme",
                "--actor",
                "ctb-sandbox-promotion-app[bot]",
                "--expected-promotion-actor",
                "ctb-sandbox-promotion-app[bot]",
                "--author-email",
                "ctb-sandbox-snapshot-app[bot]@users.noreply.github.com",
                "--expected-snapshot-author-email",
                "ctb-sandbox-snapshot-app[bot]@users.noreply.github.com",
                "--codeql-conclusion",
                "success",
                "--public-ops-actor",
                "public-ops-maintainer",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["release_kind"], "invalid")


if __name__ == "__main__":
    unittest.main()
