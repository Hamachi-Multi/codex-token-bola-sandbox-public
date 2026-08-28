from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, pathlib, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, tempfile, unittest

from scripts import raw_segment_markers

raw_segments = load_module("raw_segment_marker_contract_test", ROOT / "scripts" / "raw_segments.py")


def segment(kind: str, suffix: str) -> dict[str, object]:
    return {
        "id": f"segment-{suffix}",
        "kind": kind,
        "path": f"/tmp/segment-{suffix}.jsonl",
        "source_name": "prompt-usage.raw.jsonl",
        "created_at_unix": 1.0,
    }


class RawSegmentMarkerContractTests(unittest.TestCase):
    def test_single_and_batch_rotation_markers_have_typed_round_trips(self) -> None:
        single_payload = {
            "operation": "rotate_current_segment",
            "phase": "pointer_pending",
            "kind": "prompt_usage",
            "old_segment": segment("prompt_usage", "old"),
            "new_segment": segment("prompt_usage", "new"),
            "created_at_unix": 1.0,
        }
        single = raw_segment_markers.parse_rotation_marker(single_payload)
        batch = raw_segment_markers.parse_rotation_marker(
            {
                "operation": "rotate_current_segments",
                "phase": "pointer_pending",
                "segments": {
                    "prompt_usage": {
                        "old_segment": segment("prompt_usage", "old"),
                        "new_segment": segment("prompt_usage", "new"),
                    }
                },
                "created_at_unix": 1.0,
            }
        )

        self.assertIsInstance(single, raw_segment_markers.SingleRotationMarker)
        self.assertEqual(single.mark_manifest_pending().phase, raw_segment_markers.RotationPhase.MANIFEST_PENDING)
        self.assertIsInstance(batch, raw_segment_markers.BatchRotationMarker)
        self.assertEqual(batch.mark_manifest_pending().phase, raw_segment_markers.RotationPhase.MANIFEST_PENDING)

    def test_rotation_write_and_read_use_the_same_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            invalid = {
                "operation": "rotate_current_segment",
                "phase": "pointer_pending",
                "kind": "prompt_usage",
                "old_segment": segment("prompt_usage", "old"),
                "new_segment": segment("other", "new"),
                "created_at_unix": 1.0,
            }
            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.write_pending_rotation(base, invalid)
            self.assertFalse(raw_segments.pending_rotation_path(base).exists())

            path = raw_segments.pending_rotation_path(base)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        **invalid,
                        "schema_version": 1,
                        "base": str(base.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.read_pending_rotation(base)

    def test_rotation_operation_field_combinations_fail_closed(self) -> None:
        payload = {
            "operation": "rotate_current_segment",
            "phase": "pointer_pending",
            "kind": "prompt_usage",
            "old_segment": segment("prompt_usage", "old"),
            "new_segment": segment("prompt_usage", "new"),
            "segments": {},
            "created_at_unix": 1.0,
        }

        with self.assertRaises(raw_segment_markers.MarkerValidationError):
            raw_segment_markers.parse_rotation_marker(payload)
        payload.pop("segments")
        payload["phase"] = "future_phase"
        with self.assertRaises(raw_segment_markers.MarkerValidationError):
            raw_segment_markers.parse_rotation_marker(payload)

    def test_pointer_pending_batch_rejects_closed_segments_on_parse_write_and_read(self) -> None:
        payload = {
            "operation": "rotate_current_segments",
            "phase": "pointer_pending",
            "segments": {
                "prompt_usage": {
                    "old_segment": segment("prompt_usage", "old"),
                    "new_segment": segment("prompt_usage", "new"),
                }
            },
            "closed_segments": {"prompt_usage": segment("prompt_usage", "closed")},
            "created_at_unix": 1.0,
        }

        with self.assertRaises(raw_segment_markers.MarkerValidationError):
            raw_segment_markers.parse_rotation_marker(payload)

        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.write_pending_rotation(base, payload)
            path = raw_segments.pending_rotation_path(base)
            self.assertFalse(path.exists())

            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({**payload, "schema_version": 1, "base": str(base.resolve())}),
                encoding="utf-8",
            )
            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.read_pending_rotation(base)

        manifest_pending = raw_segment_markers.parse_rotation_marker({**payload, "phase": "manifest_pending"})
        self.assertEqual(manifest_pending.closed_segments["prompt_usage"]["id"], "segment-closed")

    def test_apply_status_hides_raw_marker_shape_from_callers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            source = segment("prompt_usage", "old")
            marker = {
                "phase": "unlink_pending",
                "previous_manifest": {"segments": [source]},
                "next_manifest": {"segments": []},
                "source_segments": [source],
                "retained_segments": [],
                "unlink_pending_segments": [source],
            }
            raw_segments.write_apply_marker(base, marker)

            status = raw_segments.read_apply_status(base)

            self.assertTrue(status.pending)
            self.assertIs(status.phase, raw_segments.ApplyMarkerPhase.UNLINK_PENDING)
            self.assertEqual(status.pending_source_segments, (source,))

    def test_apply_marker_is_validated_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.write_apply_marker(
                    base,
                    {
                        "phase": "future_phase",
                        "previous_manifest": {},
                        "next_manifest": {},
                        "source_segments": [],
                        "retained_segments": [],
                    },
                )

            self.assertFalse(raw_segments.segment_apply_marker_path(base).exists())


if __name__ == "__main__":
    unittest.main()
