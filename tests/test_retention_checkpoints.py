from __future__ import annotations

try:
    from tests.support import pathlib, tempfile, unittest
except ModuleNotFoundError:
    from support import pathlib, tempfile, unittest

from scripts import retention_checkpoints
from scripts.raw_segments_common import manifest_path


class RetentionCheckpointLifecycleTests(unittest.TestCase):
    def test_create_restore_and_discard_share_one_typed_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "output"
            current = base / "raw" / "current" / "prompt-usage.raw.jsonl.current.1.jsonl"
            current.parent.mkdir(parents=True)
            current.write_bytes(b"raw")
            manifest = manifest_path(base)
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"schema_version":1}\n', encoding="utf-8")

            checkpoint = retention_checkpoints.create(base, "retention:test")
            current.unlink()
            manifest.write_text("changed\n", encoding="utf-8")
            retention_checkpoints.restore(base, checkpoint)

            self.assertIsInstance(checkpoint, retention_checkpoints.RetentionCheckpoint)
            self.assertEqual(current.read_bytes(), b"raw")
            self.assertEqual(manifest.read_text(encoding="utf-8"), '{"schema_version":1}\n')
            self.assertFalse(checkpoint.checkpoint_dir.exists())

    def test_restore_rejects_checkpoint_from_another_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = root / "source"
            other = root / "other"
            checkpoint = retention_checkpoints.create(source, "retention:test")

            with self.assertRaises(retention_checkpoints.RetentionCheckpointError):
                retention_checkpoints.restore(other, checkpoint)

            self.assertTrue(checkpoint.checkpoint_dir.exists())
            retention_checkpoints.discard(checkpoint)

    def test_new_nonempty_current_file_prevents_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "output"
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            original = current_dir / "original.jsonl"
            original.write_bytes(b"before")
            checkpoint = retention_checkpoints.create(base, "retention:test")
            original.unlink()
            new_file = current_dir / "new.jsonl"
            new_file.write_bytes(b"live")

            retention_checkpoints.restore(base, checkpoint)

            self.assertFalse(original.exists())
            self.assertEqual(new_file.read_bytes(), b"live")
            self.assertFalse(checkpoint.checkpoint_dir.exists())

    def test_missing_backup_fails_closed_and_keeps_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "output"
            manifest = manifest_path(base)
            manifest.parent.mkdir(parents=True)
            manifest.write_text("before\n", encoding="utf-8")
            checkpoint = retention_checkpoints.create(base, "retention:test")
            backup = checkpoint.state_backups[manifest.resolve()]
            self.assertIsNotNone(backup)
            backup.unlink()
            manifest.unlink()

            with self.assertRaises(retention_checkpoints.RetentionCheckpointError):
                retention_checkpoints.restore(base, checkpoint)

            self.assertTrue(checkpoint.checkpoint_dir.exists())
            retention_checkpoints.discard(checkpoint)


if __name__ == "__main__":
    unittest.main()
