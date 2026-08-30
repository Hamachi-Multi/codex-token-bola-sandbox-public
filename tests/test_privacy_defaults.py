from __future__ import annotations

try:
    from tests.support import ROOT, load_module, mock, os, pathlib, stat, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, load_module, mock, os, pathlib, stat, tempfile, unittest


class PrivacyDefaultTests(unittest.TestCase):
    def test_hook_stores_user_prompt_text_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOLA_PROMPT_PREVIEW_CHARS", None)
            hook = load_module("token_usage_hook_test", ROOT / "scripts" / "hook.py")
        prompt = "secret prompt\n```python\nprint('secret')\n```"
        meta = hook.prompt_metadata(prompt)
        self.assertEqual(meta["prompt_preview"], prompt)
        self.assertEqual(meta["prompt_preview_chars"], len(prompt))
        self.assertFalse(meta["prompt_truncated"])
        self.assertNotIn("instruction_excerpt", meta)
        self.assertNotIn("instruction_excerpt_chars", meta)
        self.assertEqual(meta["prompt_chars"], len(prompt))
        self.assertTrue(meta["prompt_sha256"])

    def test_hook_limits_user_prompt_preview_to_800_chars_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOLA_PROMPT_PREVIEW_CHARS", None)
            hook = load_module("token_usage_hook_preview_limit_test", ROOT / "scripts" / "hook.py")
        prompt = "a" * 900
        meta = hook.prompt_metadata(prompt)
        self.assertEqual(meta["prompt_preview"], "a" * 800)
        self.assertEqual(meta["prompt_preview_chars"], 800)
        self.assertTrue(meta["prompt_truncated"])
        self.assertEqual(meta["prompt_chars"], 900)

    def test_hook_can_disable_user_prompt_text_by_env(self) -> None:
        with mock.patch.dict(os.environ, {"BOLA_PROMPT_PREVIEW_CHARS": "0"}, clear=False):
            hook = load_module("token_usage_hook_text_disabled_test", ROOT / "scripts" / "hook.py")
        prompt = "secret prompt"
        meta = hook.prompt_metadata(prompt)
        self.assertEqual(meta["prompt_preview"], "")
        self.assertEqual(meta["prompt_preview_chars"], 0)
        self.assertEqual(meta["prompt_chars"], len(prompt))
        self.assertTrue(meta["prompt_sha256"])

    def test_hook_uses_custom_prompt_preview_limit(self) -> None:
        with mock.patch.dict(os.environ, {"BOLA_PROMPT_PREVIEW_CHARS": "6"}, clear=False):
            hook = load_module("token_usage_hook_custom_preview_limit_test", ROOT / "scripts" / "hook.py")

        meta = hook.prompt_metadata("secret prompt")

        self.assertEqual(meta["prompt_preview"], "secret")
        self.assertEqual(meta["prompt_preview_chars"], 6)
        self.assertTrue(meta["prompt_truncated"])

    def test_hook_invalid_prompt_preview_limits_fall_back_to_800(self) -> None:
        for index, value in enumerate(("-1", "invalid")):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"BOLA_PROMPT_PREVIEW_CHARS": value}, clear=False):
                    hook = load_module(
                        f"token_usage_hook_invalid_preview_limit_test_{index}",
                        ROOT / "scripts" / "hook.py",
                    )

                meta = hook.prompt_metadata("a" * 900)

                self.assertEqual(meta["prompt_preview_chars"], 800)
                self.assertTrue(meta["prompt_truncated"])

    def test_old_prompt_metadata_is_sanitized_without_mutation(self) -> None:
        hook = load_module("token_usage_hook_old_prompt_metadata_test", ROOT / "scripts" / "hook.py")
        prompt = {
            "prompt_preview": "secret prompt",
            "instruction_excerpt": "secret prompt",
            "instruction_excerpt_chars": 13,
        }

        sanitized = hook.turn_capture.without_instruction_excerpt(prompt)

        self.assertEqual(sanitized, {"prompt_preview": "secret prompt"})
        self.assertIn("instruction_excerpt", prompt)

    def test_security_notes_match_text_capture_defaults(self) -> None:
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("prompt text previews are enabled", security)
        self.assertIn("BOLA_PROMPT_PREVIEW_CHARS=0", security)
        self.assertIn("stores the first 800 characters", readme)
        self.assertIn("Set the value to `0` to disable prompt text storage", readme)
        self.assertIn("does not detect or mask secrets", readme)
        self.assertIn("does not provide a scrub/export", readme)
        self.assertNotIn("instruction excerpt", security.lower())
        self.assertNotIn("BOLA_STORE_TEXT", readme)
        self.assertNotIn("Default behavior for new captures is metadata-only", security)
        self.assertNotIn("prompt text previews are disabled", security)
        self.assertIn("bola paths show", security)
        self.assertIn("BOLA_DB=/effective/output_dir/analytics/bola.sqlite", security)
        self.assertIn('pathlib.Path(os.environ["BOLA_DB"])', security)
        self.assertIn('sqlite3.connect(f"file:{db}?mode=ro", uri=True)', security)
        self.assertNotIn("sqlite3.connect('analytics/bola.sqlite')", security)

    def test_hook_append_tightens_existing_file_mode(self) -> None:
        hook = load_module("token_usage_hook_permission_test", ROOT / "scripts" / "hook.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "prompt-usage-errors.jsonl"
            path.write_text("", encoding="utf-8")
            path.chmod(0o664)

            self.assertTrue(hook.safe_append_jsonl(path, {"ok": True}))

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
