from __future__ import annotations

try:
    from tests.support import ROOT, load_module, mock, os, pathlib, stat, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, load_module, mock, os, pathlib, stat, tempfile, unittest


class PrivacyDefaultTests(unittest.TestCase):
    def test_hook_stores_user_prompt_text_by_default(self) -> None:
        hook = load_module("token_usage_hook_test", ROOT / "hooks" / "token-usage.py")
        prompt = "secret prompt\n```python\nprint('secret')\n```"
        meta = hook.prompt_metadata(prompt)
        self.assertEqual(meta["prompt_preview"], prompt)
        self.assertEqual(meta["prompt_preview_chars"], len(prompt))
        self.assertFalse(meta["prompt_truncated"])
        self.assertEqual(meta["instruction_excerpt"], "secret prompt")
        self.assertEqual(meta["prompt_chars"], len(prompt))
        self.assertTrue(meta["prompt_sha256"])

    def test_hook_limits_user_prompt_preview_to_800_chars_by_default(self) -> None:
        hook = load_module("token_usage_hook_preview_limit_test", ROOT / "hooks" / "token-usage.py")
        prompt = "a" * 900
        meta = hook.prompt_metadata(prompt)
        self.assertEqual(meta["prompt_preview"], "a" * 800)
        self.assertEqual(meta["prompt_preview_chars"], 800)
        self.assertTrue(meta["prompt_truncated"])
        self.assertEqual(meta["prompt_chars"], 900)

    def test_hook_can_disable_user_prompt_text_by_env(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_TOKEN_USAGE_STORE_TEXT": "0"}, clear=False):
            hook = load_module("token_usage_hook_text_disabled_test", ROOT / "hooks" / "token-usage.py")
        prompt = "secret prompt"
        meta = hook.prompt_metadata(prompt)
        self.assertEqual(meta["prompt_preview"], "")
        self.assertEqual(meta["instruction_excerpt"], "")
        self.assertEqual(meta["prompt_chars"], len(prompt))
        self.assertTrue(meta["prompt_sha256"])

    def test_security_notes_match_text_capture_defaults(self) -> None:
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("prompt text previews are enabled", security)
        self.assertIn("instruction excerpts are enabled", security)
        self.assertIn("CODEX_TOKEN_USAGE_STORE_TEXT=0", security)
        self.assertIn("user prompt preview text: enabled, first 800 characters by default", readme)
        self.assertIn("does not provide secret detection, masking, or scrub/export", readme)
        self.assertNotIn("Default behavior for new captures is metadata-only", security)
        self.assertNotIn("prompt text previews are disabled", security)

    def test_hook_append_tightens_existing_file_mode(self) -> None:
        hook = load_module("token_usage_hook_permission_test", ROOT / "hooks" / "token-usage.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = pathlib.Path(tmp_dir) / "prompt-usage-errors.jsonl"
            path.write_text("", encoding="utf-8")
            path.chmod(0o664)

            self.assertTrue(hook.safe_append_jsonl(path, {"ok": True}))

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
