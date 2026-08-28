from __future__ import annotations

try:
    from tests.support import ROOT, unittest
except ModuleNotFoundError:
    from support import ROOT, unittest

from scripts import turn_lifecycle


def event(payload: object, *, offset: int = 0, timestamp: str = "2026-05-31T10:00:00.000Z") -> dict[str, object]:
    return {
        "item": {"timestamp": timestamp, "type": "event_msg", "payload": payload},
        "line_start": offset,
        "next_offset": offset + 10,
    }


class TurnLifecycleTests(unittest.TestCase):
    def test_full_lifecycle_aggregates_model_calls_until_terminal(self) -> None:
        events = [
            event({"type": "task_started", "turn_id": "t1", "started_at": 100}),
            event(
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                        "total_token_usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                    },
                }
            ),
            event(
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                        "total_token_usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                    },
                }
            ),
            event({"type": "task_complete", "turn_id": "t1", "completed_at": 101}, offset=40),
        ]

        accumulator = turn_lifecycle.reduce_target_events(events, "t1", assume_active=False)
        snapshot = turn_lifecycle.full_lifecycle_snapshot(accumulator, path="/tmp/rollout.jsonl")

        self.assertTrue(snapshot["found"])
        self.assertEqual(snapshot["turn_status"], "completed")
        self.assertEqual(snapshot["event_count"], 2)
        self.assertEqual(snapshot["total_token_usage"]["total_tokens"], 10)
        self.assertEqual(accumulator.terminal_event["event_offset"], 40)

    def test_abort_variants_share_terminal_classification(self) -> None:
        for terminal_type in ("task_aborted", "turn_aborted"):
            with self.subTest(terminal_type=terminal_type):
                events = [
                    event({"type": "task_started", "turn_id": "t1"}),
                    event({"type": terminal_type, "turn_id": "t1", "aborted_at": 101}, offset=20),
                ]
                accumulator = turn_lifecycle.reduce_target_events(events, "t1", assume_active=False)
                snapshot = turn_lifecycle.full_lifecycle_snapshot(accumulator, path="/tmp/rollout.jsonl")

                self.assertEqual(snapshot["turn_status"], "aborted")
                self.assertEqual(turn_lifecycle.terminal_turn_event(events[-1])["type"], terminal_type)

    def test_missing_start_and_terminal_are_distinct(self) -> None:
        missing_start = turn_lifecycle.reduce_target_events(
            [event({"type": "task_complete", "turn_id": "t1"})],
            "t1",
            assume_active=False,
        )
        missing_terminal = turn_lifecycle.reduce_target_events(
            [event({"type": "task_started", "turn_id": "t1"})],
            "t1",
            assume_active=False,
        )

        self.assertEqual(
            turn_lifecycle.full_lifecycle_snapshot(missing_start, path="/tmp/a")["reason"],
            "task_started_missing",
        )
        self.assertEqual(
            turn_lifecycle.full_lifecycle_snapshot(missing_terminal, path="/tmp/a")["reason"],
            "task_terminal_missing",
        )

    def test_bounded_scan_resets_usage_at_matching_start(self) -> None:
        events = [
            event(
                {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 999}, "last_token_usage": {"total_tokens": 999}},
                }
            ),
            event({"type": "task_started", "turn_id": "t1"}, offset=10),
            event(
                {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 12}, "last_token_usage": {"total_tokens": 12}},
                },
                offset=20,
            ),
            event({"type": "task_complete", "turn_id": "t1"}, offset=30),
        ]

        accumulator = turn_lifecycle.reduce_target_events(events, "t1", assume_active=True)
        snapshot = turn_lifecycle.bounded_usage_snapshot(
            accumulator,
            path="/tmp/rollout.jsonl",
            file_size=40,
            parse_error_seen=False,
        )

        self.assertEqual(snapshot["total_token_usage"]["total_tokens"], 12)
        self.assertEqual(snapshot["event_count"], 1)
        self.assertEqual(snapshot["bounded_at_file_offset"], 40)

    def test_index_reducer_keeps_turn_boundaries(self) -> None:
        reducer = turn_lifecycle.LifecycleIndexReducer()
        for item in (
            event({"type": "task_started", "turn_id": "t1"}),
            event({"type": "task_complete", "turn_id": "t1"}),
            event({"type": "task_started", "turn_id": "t2"}),
            event({"type": "turn_aborted", "turn_id": "t2"}),
        ):
            reducer.feed(item)

        turns = reducer.finish()

        self.assertEqual(turns["t1"].status, "completed")
        self.assertEqual(turns["t2"].status, "aborted")

    def test_malformed_payload_is_not_a_terminal_event(self) -> None:
        self.assertIsNone(turn_lifecycle.terminal_turn_event(event([])))
        self.assertIsNone(turn_lifecycle.terminal_turn_event({"item": []}))

    def test_reconcile_no_longer_imports_hook_entrypoint(self) -> None:
        source = (ROOT / "scripts" / "reconcile.py").read_text(encoding="utf-8")

        self.assertNotIn("importlib.util", source)
        self.assertNotIn("HOOK_PATH", source)
        self.assertNotIn("hook.", source)


if __name__ == "__main__":
    unittest.main()
