"""Typed normalize publish marker and commit identity contracts."""

from __future__ import annotations

import copy
import pathlib
from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION = 1


class NormalizePublishValidationError(ValueError):
    pass


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NormalizePublishValidationError(f"normalize publish {field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class NormalizeCommitIdentity:
    logic_version: int
    sources: dict[str, int]
    processed_segments: dict[str, dict[str, Any]]

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> NormalizeCommitIdentity:
        logic_version = _nonnegative_int(state.get("logic_version"), "logic_version")
        source_payload = state.get("sources")
        if not isinstance(source_payload, dict):
            raise NormalizePublishValidationError("normalize publish sources must be an object")
        sources: dict[str, int] = {}
        for path, offset in source_payload.items():
            if not isinstance(path, str) or not path:
                raise NormalizePublishValidationError("normalize publish source path must be text")
            sources[path] = _nonnegative_int(offset, f"source offset for {path}")
        segment_payload = state.get("processed_segments", {})
        if not isinstance(segment_payload, dict):
            raise NormalizePublishValidationError("normalize publish processed_segments must be an object")
        processed_segments: dict[str, dict[str, Any]] = {}
        for segment_id, fingerprint in segment_payload.items():
            if not isinstance(segment_id, str) or not segment_id or not isinstance(fingerprint, dict):
                raise NormalizePublishValidationError("normalize publish processed segment entry is invalid")
            processed_segments[segment_id] = copy.deepcopy(fingerprint)
        return cls(
            logic_version=logic_version,
            sources=sources,
            processed_segments=processed_segments,
        )

    def matches_state(self, state: Mapping[str, object]) -> bool:
        try:
            return self == self.from_state(state)
        except NormalizePublishValidationError:
            return False

    def to_state(self, *, normalized_log_size: int) -> dict[str, Any]:
        return {
            "logic_version": self.logic_version,
            "sources": dict(self.sources),
            "processed_segments": copy.deepcopy(self.processed_segments),
            "normalized_log_size": _nonnegative_int(normalized_log_size, "normalized_log_size"),
        }


@dataclass(frozen=True)
class NormalizePendingPublish:
    created_at_unix: float
    output_path: pathlib.Path
    rollback_offset: int
    state: dict[str, Any]
    identity: NormalizeCommitIdentity
    full_publish: bool

    @classmethod
    def create(
        cls,
        *,
        created_at_unix: float,
        output_path: pathlib.Path,
        rollback_offset: int,
        state: Mapping[str, object],
        full_publish: bool,
    ) -> NormalizePendingPublish:
        if isinstance(created_at_unix, bool) or not isinstance(created_at_unix, (int, float)) or created_at_unix < 0:
            raise NormalizePublishValidationError("normalize publish created_at_unix must be non-negative")
        if not isinstance(full_publish, bool):
            raise NormalizePublishValidationError("normalize publish full_publish must be a boolean")
        state_copy = copy.deepcopy(dict(state))
        return cls(
            created_at_unix=float(created_at_unix),
            output_path=pathlib.Path(output_path),
            rollback_offset=_nonnegative_int(rollback_offset, "rollback offset"),
            state=state_copy,
            identity=NormalizeCommitIdentity.from_state(state_copy),
            full_publish=full_publish,
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        expected_output_path: pathlib.Path,
    ) -> NormalizePendingPublish:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise NormalizePublishValidationError("unsupported normalize publish marker schema")
        outputs = payload.get("outputs")
        expected = str(expected_output_path)
        if not isinstance(outputs, dict) or set(outputs) != {expected}:
            raise NormalizePublishValidationError("normalize publish marker output path mismatch")
        state = payload.get("state")
        if not isinstance(state, dict):
            raise NormalizePublishValidationError("normalize publish marker state must be an object")
        return cls.create(
            created_at_unix=payload.get("created_at_unix"),
            output_path=expected_output_path,
            rollback_offset=outputs[expected],
            state=state,
            full_publish=payload.get("full_publish", False),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "created_at_unix": self.created_at_unix,
            "outputs": {str(self.output_path): self.rollback_offset},
            "state": copy.deepcopy(self.state),
            "full_publish": self.full_publish,
        }
