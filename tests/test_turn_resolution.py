from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from tests.support import load_module


ROOT = pathlib.Path(__file__).resolve().parents[1]


class TurnResolutionTests(unittest.TestCase):
    def test_legacy_pending_is_inferred_without_changing_other_legacy_rows(self) -> None:
        resolution = load_module("turn_resolution_legacy_test", ROOT / "scripts" / "turn_resolution.py")

        self.assertEqual(resolution.status_from_row({"lifecycle_end_reason": "pending_token_count", "estimated": True}), resolution.PENDING)
        self.assertEqual(resolution.status_from_row({"lifecycle_end_reason": "missing_start_state", "estimated": True}), resolution.PENDING)
        self.assertEqual(resolution.status_from_row({"turn_status": "completed", "estimated": True}), resolution.RESOLVED)

    def test_only_pending_can_transition_to_terminal_resolution(self) -> None:
        resolution = load_module("turn_resolution_transition_test", ROOT / "scripts" / "turn_resolution.py")

        self.assertEqual(resolution.transition(resolution.PENDING, resolution.RESOLVED), resolution.RESOLVED)
        self.assertEqual(resolution.transition(resolution.PENDING, resolution.UNAVAILABLE), resolution.UNAVAILABLE)
        with self.assertRaises(resolution.TokenResolutionError):
            resolution.transition(resolution.RESOLVED, resolution.PENDING)

    def test_unavailable_evidence_is_private_stable_and_reindexable(self) -> None:
        resolution = load_module("turn_resolution_evidence_test", ROOT / "scripts" / "turn_resolution.py")
        quarantine = load_module("quarantine_resolution_evidence_test", ROOT / "scripts" / "quarantine_health.py")
        row = {
            "captured_at_ns": 123,
            "session_id": "s1",
            "turn_id": "t1",
            "transcript_path": "/tmp/rollout.jsonl",
            "token_resolution_reason": "no_token_count_before_task_complete",
        }

        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            first = resolution.write_unavailable_evidence(base, row)
            second = resolution.write_unavailable_evidence(base, row)
            payload = json.loads(first[1].read_text(encoding="utf-8"))
            mode = first[1].stat().st_mode & 0o777
            summary = quarantine.summary(base, include_entries=True)
            acknowledged = quarantine.acknowledge(base, event_ids=[first[0]])
            after_acknowledge = quarantine.summary(base, include_entries=True)
            evidence_still_exists = first[1].exists()

        self.assertEqual(first, second)
        self.assertEqual(mode, 0o600)
        self.assertEqual(payload["event_id"], first[0])
        self.assertEqual(summary["unacknowledged_events"], 1)
        self.assertEqual(summary["events"][0]["kind"], resolution.UNAVAILABLE_KIND)
        self.assertEqual(acknowledged["remaining_unacknowledged_events"], 0)
        self.assertEqual(after_acknowledge["unacknowledged_events"], 0)
        self.assertTrue(evidence_still_exists)


if __name__ == "__main__":
    unittest.main()
