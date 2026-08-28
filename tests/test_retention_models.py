from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, pathlib, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, tempfile, unittest

from scripts import retention_models


class RetentionModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.models = retention_models

    def valid_payload(self, phase: retention_models.RetentionPhase) -> dict[str, object]:
        payload: dict[str, object] = {
            "phase": phase.value,
            "operation_job_id": "retention:test",
            "cutoff_unix": 123.5,
            "recovery_required": True,
        }
        if phase in self.models.PRE_DERIVED_RESET_PHASES:
            payload.update(derived_rebuild_required=False, physical_delete_pending=False, pending_files=0)
        elif phase is self.models.RetentionPhase.PHYSICAL_DELETE_PENDING:
            payload.update(
                deleted_rows=1,
                derived_rebuild_required=False,
                physical_delete_pending=True,
                pending_files=2,
            )
        elif phase is self.models.RetentionPhase.FAILED:
            payload.update(
                deleted_rows=0,
                failed_stage="build",
                derived_rebuild_required=True,
                physical_delete_pending=False,
                pending_files=0,
            )
        elif phase is not self.models.RetentionPhase.COMPLETE:
            payload.update(
                deleted_rows=0,
                derived_rebuild_required=True,
                physical_delete_pending=False,
                pending_files=0,
            )
        return payload

    def test_all_persisted_phases_have_a_typed_round_trip(self) -> None:
        expected = {
            "preparing_snapshot",
            "snapshot_prepared",
            "planning",
            "derived_reset_pending",
            "derived_reset_complete",
            "planned",
            "logical_delete_committed",
            "derived_rebuild_required",
            "physical_delete_pending",
            "failed",
            "complete",
        }

        self.assertEqual({phase.value for phase in self.models.RetentionPhase}, expected)
        for phase in self.models.RetentionPhase:
            payload = self.valid_payload(phase)
            job = self.models.RetentionJob.from_payload(payload)
            self.assertEqual(job.to_payload(), payload)

    def test_round_trip_preserves_optional_nulls_and_extension_fields(self) -> None:
        payload = {
            "schema_version": 1,
            "phase": "derived_reset_complete",
            "operation_job_id": "retention:test",
            "pruned_state_job_id": None,
            "cutoff_unix": 123.5,
            "deleted_rows": 7,
            "derived_rebuild_required": True,
            "reset": {"deleted": ["normalized"]},
        }

        job = self.models.RetentionJob.from_payload(payload)

        self.assertIs(job.phase, self.models.RetentionPhase.DERIVED_RESET_COMPLETE)
        self.assertEqual(job.to_payload(), payload)

    def test_updated_job_preserves_extensions_and_without_removes_fields(self) -> None:
        job = self.models.RetentionJob.from_payload(
            self.valid_payload(self.models.RetentionPhase.FAILED)
            | {
                "pruned_state_job_id": "retention:test",
                "pruned_state_commit_ready": True,
                "error": "boom",
            }
        )

        updated = job.transition(
            self.models.RetentionPhase.DERIVED_REBUILD_REQUIRED,
            clear_fields=("pruned_state_job_id", "pruned_state_commit_ready"),
            pruned_state_commit_recovered=True,
        )

        self.assertEqual(updated.phase, self.models.RetentionPhase.DERIVED_REBUILD_REQUIRED)
        self.assertNotIn("pruned_state_job_id", updated.to_payload())
        self.assertTrue(updated.pruned_state_commit_recovered)
        self.assertEqual(updated.extra["error"], "boom")

    def test_invalid_phase_and_core_field_types_are_rejected(self) -> None:
        invalid_payloads = (
            {"phase": "future_unknown_phase"},
            {"phase": "planned", "derived_rebuild_required": "yes"},
            {"phase": "planned", "deleted_rows": -1},
            {"phase": "planned", "pending_files": True},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(self.models.RetentionJobValidationError):
                    self.models.RetentionJob.from_payload(payload)

    def test_persisted_marker_keeps_public_dict_contract(self) -> None:
        recovery = load_module("retention_models_recovery_test", ROOT / "scripts" / "dashboard_cleanup_recovery.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            (base / "state").mkdir(parents=True)
            recovery.write_cleanup_retention_job(
                base,
                self.models.RetentionJob.create_at(
                    self.models.RetentionPhase.PHYSICAL_DELETE_PENDING,
                    **{
                        key: value
                        for key, value in self.valid_payload(self.models.RetentionPhase.PHYSICAL_DELETE_PENDING).items()
                        if key != "phase"
                    },
                    unlink_errors=["locked"],
                ),
            )

            public_job = recovery.read_cleanup_retention_job(base)
            typed_job = recovery.read_cleanup_retention_job_model(base)
            stored = json.loads(recovery.cleanup_retention_job_path(base).read_text(encoding="utf-8"))

        self.assertIsInstance(public_job, dict)
        self.assertEqual(public_job["phase"], "physical_delete_pending")
        self.assertEqual(public_job["unlink_errors"], ["locked"])
        self.assertIs(typed_job.phase, recovery.RetentionPhase.PHYSICAL_DELETE_PENDING)
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(stored["phase"], "physical_delete_pending")

    def test_transition_table_accepts_only_declared_edges(self) -> None:
        for source, targets in self.models.ALLOWED_TRANSITIONS.items():
            if source is self.models.RetentionPhase.COMPLETE:
                source_job = self.models.RetentionJob.from_payload(self.valid_payload(source))
            else:
                source_job = self.models.RetentionJob.from_payload(self.valid_payload(source)).validate_for_write()
            for target in self.models.RetentionPhase:
                if target is self.models.RetentionPhase.COMPLETE:
                    continue
                target_fields = self.valid_payload(target)
                target_fields.pop("phase")
                if target is source or target in targets:
                    transitioned = source_job.transition(target, **target_fields)
                    self.assertIs(transitioned.phase, target)
                else:
                    with self.assertRaises(self.models.RetentionJobValidationError):
                        source_job.transition(target, **target_fields)

    def test_write_invariants_reject_impossible_states(self) -> None:
        invalid_payloads = (
            self.valid_payload(self.models.RetentionPhase.PLANNED) | {"operation_job_id": None},
            self.valid_payload(self.models.RetentionPhase.PLANNED) | {"recovery_required": False},
            self.valid_payload(self.models.RetentionPhase.PLANNED) | {"derived_rebuild_required": False},
            self.valid_payload(self.models.RetentionPhase.PHYSICAL_DELETE_PENDING) | {"pending_files": 0},
            self.valid_payload(self.models.RetentionPhase.FAILED) | {"failed_stage": None},
            self.valid_payload(self.models.RetentionPhase.PLANNED)
            | {"pruned_state_commit_ready": True, "pruned_state_job_id": None},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(self.models.RetentionJobValidationError):
                    self.models.RetentionJob.from_payload(payload).validate_for_write()

    def test_complete_is_readable_but_never_writable(self) -> None:
        legacy = self.models.RetentionJob.from_payload({"phase": "complete"})

        self.assertIs(legacy.phase, self.models.RetentionPhase.COMPLETE)
        with self.assertRaises(self.models.RetentionJobValidationError):
            legacy.validate_for_write()

    def test_invalid_persisted_marker_fails_closed(self) -> None:
        recovery = load_module("retention_models_invalid_marker_test", ROOT / "scripts" / "dashboard_cleanup_recovery.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            recovery.cleanup_retention_job_path(base).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "base": str(base.resolve()),
                        "phase": "unknown",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(recovery.raw_segments.ManifestError):
                recovery.read_cleanup_retention_job(base)


if __name__ == "__main__":
    unittest.main()
