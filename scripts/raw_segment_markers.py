"""Typed raw segment marker contracts."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping


class MarkerValidationError(ValueError):
    pass


class ApplyMarkerPhase(str, Enum):
    MANIFEST_PENDING = "manifest_pending"
    UNLINK_PENDING = "unlink_pending"


@dataclass(frozen=True)
class ApplyMarkerStatus:
    phase: ApplyMarkerPhase | None = None
    pending_source_segments: tuple[dict[str, Any], ...] = ()

    @property
    def pending(self) -> bool:
        return self.phase is not None


class RotationPhase(str, Enum):
    POINTER_PENDING = "pointer_pending"
    MANIFEST_PENDING = "manifest_pending"


def _phase(value: object) -> RotationPhase:
    try:
        return RotationPhase(str(value))
    except ValueError as exc:
        raise MarkerValidationError(f"unsupported pending raw segment rotation phase: {value}") from exc


def _created_at(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise MarkerValidationError("pending raw segment rotation created_at_unix must be non-negative")
    return float(value)


def _segment(value: object, *, kind: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MarkerValidationError(f"pending raw segment rotation {field} must be an object")
    if value.get("kind") != kind:
        raise MarkerValidationError(f"pending raw segment rotation {field} kind mismatch")
    for key in ("id", "path", "source_name"):
        if not isinstance(value.get(key), str) or not value.get(key):
            raise MarkerValidationError(f"pending raw segment rotation {field} missing {key}")
    return copy.deepcopy(value)


@dataclass(frozen=True)
class SingleRotationMarker:
    phase: RotationPhase
    kind: str
    old_segment: dict[str, Any]
    new_segment: dict[str, Any]
    created_at_unix: float

    @property
    def operation(self) -> str:
        return "rotate_current_segment"

    def mark_manifest_pending(self) -> SingleRotationMarker:
        if self.phase is not RotationPhase.POINTER_PENDING:
            raise MarkerValidationError(f"rotation cannot move from {self.phase.value} to manifest_pending")
        return replace(self, phase=RotationPhase.MANIFEST_PENDING)

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "phase": self.phase.value,
            "kind": self.kind,
            "old_segment": copy.deepcopy(self.old_segment),
            "new_segment": copy.deepcopy(self.new_segment),
            "created_at_unix": self.created_at_unix,
        }


@dataclass(frozen=True)
class BatchRotationMarker:
    phase: RotationPhase
    segments: dict[str, dict[str, dict[str, Any]]]
    created_at_unix: float
    closed_segments: dict[str, dict[str, Any]]

    @property
    def operation(self) -> str:
        return "rotate_current_segments"

    def mark_manifest_pending(self) -> BatchRotationMarker:
        if self.phase is not RotationPhase.POINTER_PENDING:
            raise MarkerValidationError(f"rotation cannot move from {self.phase.value} to manifest_pending")
        return replace(self, phase=RotationPhase.MANIFEST_PENDING)

    def record_closed_segment(self, kind: str, segment: Mapping[str, Any]) -> BatchRotationMarker:
        if self.phase is not RotationPhase.MANIFEST_PENDING or kind not in self.segments:
            raise MarkerValidationError("closed segment does not match pending batch rotation")
        closed = copy.deepcopy(self.closed_segments)
        closed[kind] = copy.deepcopy(dict(segment))
        return replace(self, closed_segments=closed)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operation": self.operation,
            "phase": self.phase.value,
            "segments": copy.deepcopy(self.segments),
            "created_at_unix": self.created_at_unix,
        }
        if self.closed_segments:
            payload["closed_segments"] = copy.deepcopy(self.closed_segments)
        return payload


RotationMarker = SingleRotationMarker | BatchRotationMarker


def parse_rotation_marker(payload: Mapping[str, object]) -> RotationMarker:
    operation = payload.get("operation")
    phase = _phase(payload.get("phase"))
    created_at_unix = _created_at(payload.get("created_at_unix"))
    if operation == "rotate_current_segment":
        if "segments" in payload or "closed_segments" in payload:
            raise MarkerValidationError("single rotation marker contains batch fields")
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind:
            raise MarkerValidationError("pending raw segment rotation kind must be text")
        return SingleRotationMarker(
            phase=phase,
            kind=kind,
            old_segment=_segment(payload.get("old_segment"), kind=kind, field="old_segment"),
            new_segment=_segment(payload.get("new_segment"), kind=kind, field="new_segment"),
            created_at_unix=created_at_unix,
        )
    if operation == "rotate_current_segments":
        if any(key in payload for key in ("kind", "old_segment", "new_segment")):
            raise MarkerValidationError("batch rotation marker contains single fields")
        segments_payload = payload.get("segments")
        if not isinstance(segments_payload, dict) or not segments_payload:
            raise MarkerValidationError("pending raw segment rotation segments must be a non-empty object")
        segments: dict[str, dict[str, dict[str, Any]]] = {}
        for kind, pair in segments_payload.items():
            if not isinstance(kind, str) or not kind or not isinstance(pair, dict):
                raise MarkerValidationError("pending raw segment rotation segment pair is invalid")
            segments[kind] = {
                "old_segment": _segment(pair.get("old_segment"), kind=kind, field="old_segment"),
                "new_segment": _segment(pair.get("new_segment"), kind=kind, field="new_segment"),
            }
        closed_payload = payload.get("closed_segments", {})
        if not isinstance(closed_payload, dict) or not set(closed_payload).issubset(segments):
            raise MarkerValidationError("pending raw segment rotation closed_segments mismatch")
        if phase is RotationPhase.POINTER_PENDING and closed_payload:
            raise MarkerValidationError("pointer-pending raw segment rotation cannot contain closed_segments")
        closed_segments: dict[str, dict[str, Any]] = {}
        for kind, segment in closed_payload.items():
            if not isinstance(segment, dict) or segment.get("kind") != kind:
                raise MarkerValidationError("pending raw segment rotation closed segment is invalid")
            closed_segments[kind] = copy.deepcopy(segment)
        return BatchRotationMarker(
            phase=phase,
            segments=segments,
            created_at_unix=created_at_unix,
            closed_segments=closed_segments,
        )
    raise MarkerValidationError(f"unsupported pending raw segment rotation operation: {operation}")


def apply_marker_status(payload: Mapping[str, object] | None) -> ApplyMarkerStatus:
    if payload is None:
        return ApplyMarkerStatus()
    try:
        phase = ApplyMarkerPhase(str(payload.get("phase")))
    except ValueError as exc:
        raise MarkerValidationError(f"unsupported pending raw segment apply phase: {payload.get('phase')}") from exc
    pending = payload.get("unlink_pending_segments") or payload.get("source_segments") or []
    if not isinstance(pending, list) or any(not isinstance(item, dict) for item in pending):
        raise MarkerValidationError("pending raw segment apply sources must be a list")
    return ApplyMarkerStatus(phase=phase, pending_source_segments=tuple(copy.deepcopy(pending)))
