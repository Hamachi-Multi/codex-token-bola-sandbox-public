from __future__ import annotations

try:
    from tests.support import pathlib, unittest
except ModuleNotFoundError:
    from support import pathlib, unittest

from scripts import normalize_publish


class NormalizePublishContractTests(unittest.TestCase):
    def state(self, *, normalized_log_size: int = 10) -> dict[str, object]:
        return {
            "logic_version": 7,
            "sources": {"/tmp/raw.jsonl": 12},
            "processed_segments": {"segment-1": {"path": "/tmp/raw.jsonl", "bytes": 12}},
            "normalized_log_size": normalized_log_size,
        }

    def test_commit_identity_explicitly_excludes_output_size(self) -> None:
        before = normalize_publish.NormalizeCommitIdentity.from_state(self.state(normalized_log_size=10))
        after = normalize_publish.NormalizeCommitIdentity.from_state(self.state(normalized_log_size=20))

        self.assertEqual(before, after)
        self.assertTrue(before.matches_state(self.state(normalized_log_size=30)))

    def test_pending_marker_round_trip_keeps_schema_v1_shape(self) -> None:
        output = pathlib.Path("/tmp/normalized.jsonl")
        marker = normalize_publish.NormalizePendingPublish.create(
            created_at_unix=1.5,
            output_path=output,
            rollback_offset=10,
            state=self.state(),
            full_publish=False,
        )

        payload = marker.to_payload()
        restored = normalize_publish.NormalizePendingPublish.from_payload(payload, expected_output_path=output)

        self.assertEqual(restored, marker)
        self.assertEqual(set(payload), {"schema_version", "created_at_unix", "outputs", "state", "full_publish"})
        self.assertEqual(payload["schema_version"], 1)

    def test_marker_rejects_unexpected_output_and_negative_offset(self) -> None:
        output = pathlib.Path("/tmp/normalized.jsonl")
        payload = {
            "schema_version": 1,
            "created_at_unix": 1.0,
            "outputs": {"/tmp/other.jsonl": 0},
            "state": self.state(),
            "full_publish": False,
        }

        with self.assertRaises(normalize_publish.NormalizePublishValidationError):
            normalize_publish.NormalizePendingPublish.from_payload(payload, expected_output_path=output)

        payload["outputs"] = {str(output): -1}
        with self.assertRaises(normalize_publish.NormalizePublishValidationError):
            normalize_publish.NormalizePendingPublish.from_payload(payload, expected_output_path=output)

    def test_identity_rejects_implicit_new_core_field_shapes(self) -> None:
        invalid = self.state()
        invalid["sources"] = {"/tmp/raw.jsonl": "12"}

        with self.assertRaises(normalize_publish.NormalizePublishValidationError):
            normalize_publish.NormalizeCommitIdentity.from_state(invalid)


if __name__ == "__main__":
    unittest.main()
