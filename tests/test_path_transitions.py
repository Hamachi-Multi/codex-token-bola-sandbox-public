from __future__ import annotations

import typing

try:
    from tests.support import json, pathlib, tempfile, unittest
except ModuleNotFoundError:
    from support import json, pathlib, tempfile, unittest

from scripts import service_paths


class PathTransitionTests(unittest.TestCase):
    def transition(self, source: pathlib.Path, active: pathlib.Path) -> service_paths.PathTransition:
        return service_paths.PathTransition.prepare_set(
            source,
            active,
            transition_id="transition-1",
            created_at_ns=1,
            transferred_state_files=("turn.json",),
            created_state_files=("turn.json",),
        )

    def test_typed_transition_round_trip_preserves_schema_v1(self) -> None:
        source = pathlib.Path("/tmp/source")
        active = pathlib.Path("/tmp/active")
        pending = self.transition(source, active).mark_pending()

        payload = pending.to_payload()
        restored = service_paths.PathTransition.from_payload(payload)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(restored, pending)
        self.assertEqual(restored["phase"], "pending")

    def test_typed_transition_annotations_resolve_without_test_loader_workarounds(self) -> None:
        hints = typing.get_type_hints(service_paths.PathTransition)

        self.assertIs(hints["phase"], service_paths.PathTransitionPhase)
        self.assertEqual(hints["previous_transition"], service_paths.PathTransition | None)
        self.assertEqual(hints["transferred_state_files"], tuple[str, ...])

    def test_only_declared_phase_edges_are_allowed(self) -> None:
        pending = self.transition(pathlib.Path("/tmp/source"), pathlib.Path("/tmp/active")).mark_pending()
        applying = pending.begin_migration()
        recovery = applying.mark_recovery_required()

        self.assertIs(recovery.begin_migration().phase, service_paths.PathTransitionPhase.APPLYING)
        with self.assertRaises(service_paths.ConfigurationError):
            pending.mark_pending()
        with self.assertRaises(service_paths.ConfigurationError):
            pending.mark_recovery_required()

    def test_rollback_requires_exactly_one_reversed_pending_transition(self) -> None:
        source = pathlib.Path("/tmp/source")
        active = pathlib.Path("/tmp/active")
        previous = self.transition(source, active).mark_pending()
        rollback = service_paths.PathTransition.prepare_set(
            active,
            source,
            transition_id="transition-2",
            created_at_ns=2,
            rollback=True,
            previous_transition=previous,
        )

        self.assertEqual(service_paths.PathTransition.from_payload(rollback.to_payload()), rollback)
        with self.assertRaises(service_paths.ConfigurationError):
            service_paths.PathTransition.prepare_set(
                source,
                pathlib.Path("/tmp/third"),
                transition_id="transition-3",
                created_at_ns=3,
                rollback=True,
                previous_transition=previous,
            )

        nested = rollback.mark_pending().to_payload()
        outer = service_paths.PathTransition.prepare_set(
            source,
            active,
            transition_id="transition-4",
            created_at_ns=4,
        ).to_payload()
        outer["rollback"] = True
        outer["previous_transition"] = nested
        with self.assertRaises(service_paths.ConfigurationError):
            service_paths.PathTransition.from_payload(outer)

    def test_persistence_rejects_inconsistent_payload_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "path-transition.json"
            payload = {
                "schema_version": 1,
                "transition_id": "bad",
                "source_output_dir": "/tmp/same",
                "active_output_dir": "/tmp/same",
                "created_at_ns": 1,
                "phase": "pending",
            }

            with self.assertRaises(service_paths.ConfigurationError):
                service_paths.write_path_transition(payload, path)

            self.assertFalse(path.exists())

    def test_legacy_dict_reader_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "path-transition.json"
            transition = self.transition(pathlib.Path(tmp) / "source", pathlib.Path(tmp) / "active").mark_pending()
            service_paths.write_path_transition(transition, path)

            payload = service_paths.read_path_transition(path)
            stored = json.loads(path.read_text(encoding="utf-8"))

        self.assertIsInstance(payload, dict)
        self.assertEqual(payload, stored)


if __name__ == "__main__":
    unittest.main()
