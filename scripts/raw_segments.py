#!/usr/bin/env python3
"""Raw segment manifest and rotation helpers for token usage logs.

This module is the public compatibility facade. Implementation lives in the
raw segment submodules, while existing imports can keep using ``raw_segments``.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Callable
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import raw_segments_retention as _retention
import raw_segments_rotation as _rotation
import raw_segments_state as _state
from raw_segment_markers import ApplyMarkerPhase, ApplyMarkerStatus, RotationPhase
from raw_segments_common import (
    JsonlScanAccumulator,
    ManifestError,
    PROMPT_RAW_NAME,
    acquire_raw_segment_lock,
    current_pointer_path,
    fsync_dir,
    manifest_path,
    open_segment_payload,
    pending_rotation_path,
    raw_segment_lock_available,
    raw_segment_lock_path,
    row_time,
    segment_apply_marker_path,
    write_json_atomic,
)
from raw_segments_retention import SegmentApplyState
from raw_segments_rotation import closed_segment_from_current, new_current_segment, unlink_empty_closed_segment
from raw_segments_state import (
    clear_apply_marker,
    current_segment_paths,
    empty_current_pointer,
    empty_manifest,
    manifest_segments,
    manifest_signature,
    load_pending_rotation,
    read_apply_marker,
    read_apply_status,
    read_current_pointer,
    read_manifest,
    read_pending_rotation,
    strict_read_current_pointer,
    strict_read_manifest,
    validate_current_segment_entry,
    validate_segment_path,
    write_apply_marker,
    write_current_pointer,
    write_manifest,
    write_pending_rotation,
)


class RawSegmentDependencies:
    __slots__ = ("state", "retention", "rotation")

    def __init__(self, *, state: Any, retention: Any, rotation: Any) -> None:
        self.state = state
        self.retention = retention
        self.rotation = rotation


DEFAULT_RAW_SEGMENT_DEPENDENCIES = RawSegmentDependencies(
    state=_state,
    retention=_retention,
    rotation=_rotation,
)


def validate_current_pointer_entries(base: pathlib.Path) -> list[dict[str, Any]]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.state.validate_current_pointer_entries(base)


def reconcile_apply_marker(base: pathlib.Path) -> None:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.state.reconcile_apply_marker(base)


def sweep_apply_marker(base: pathlib.Path) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.state.sweep_apply_marker(base)


def reconcile_apply_marker_unlocked(base: pathlib.Path) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.state.reconcile_apply_marker_unlocked(base)


def plan_segments_older_than(
    base: pathlib.Path,
    cutoff_unix: float,
    *,
    create_output_dirs: bool = True,
    pruned_turn_sink: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.plan_segments_older_than(
        base,
        cutoff_unix,
        create_output_dirs=create_output_dirs,
        pruned_turn_sink=pruned_turn_sink,
    )


def preflight_segments_older_than(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.preflight_segments_older_than(base, cutoff_unix)


def validate_segment_plans(base: pathlib.Path, segment_plan: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.validate_segment_plans(base, segment_plan)


def discard_segment_plan_artifacts(segment_plan: dict[str, Any] | None) -> None:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.discard_segment_plan_artifacts(segment_plan)


def apply_segment_plans(base: pathlib.Path, segment_plan: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.apply_segment_plans(base, segment_plan)


def inspect_segment_apply_state(base: pathlib.Path, segment_plan: dict[str, Any]) -> _retention.SegmentApplyState:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.inspect_segment_apply_state(base, segment_plan)


def retention_preview_from_manifest(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any] | None:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.retention_preview_from_manifest(base, cutoff_unix)


def retention_preview_from_current(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any] | None:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.retention_preview_from_current(base, cutoff_unix)


def ensure_current_segment(base: pathlib.Path, *, kind: str, source_name: str | None = None) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.rotation.ensure_current_segment(base, kind=kind, source_name=source_name)


def append_closed_segment(base: pathlib.Path, closed_segment: dict[str, Any]) -> None:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.rotation.append_closed_segment(base, closed_segment)


def finish_rotated_segment(base: pathlib.Path, marker: dict[str, Any], *, clear_marker: bool = True, unlink_empty: bool = False) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.rotation.finish_rotated_segment(base, marker, clear_marker=clear_marker, unlink_empty=unlink_empty)


def reconcile_pending_rotation(base: pathlib.Path) -> None:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.rotation.reconcile_pending_rotation(base)


def rotate_current_segment(base: pathlib.Path, *, kind: str, source_name: str | None = None) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.rotation.rotate_current_segment(base, kind=kind, source_name=source_name)


def rotate_all_current_segments(base: pathlib.Path) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.rotation.rotate_all_current_segments(base)


def begin_rotate_all_current_segments_unlocked(base: pathlib.Path) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.rotation.begin_rotate_all_current_segments_unlocked(base)


def finish_rotate_all_current_segments(base: pathlib.Path, rotation: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.rotation.finish_rotate_all_current_segments(base, rotation)


__all__ = [
    "JsonlScanAccumulator",
    "ManifestError",
    "PROMPT_RAW_NAME",
    "ApplyMarkerPhase",
    "ApplyMarkerStatus",
    "RotationPhase",
    "SegmentApplyState",
    "acquire_raw_segment_lock",
    "append_closed_segment",
    "apply_segment_plans",
    "begin_rotate_all_current_segments_unlocked",
    "clear_apply_marker",
    "closed_segment_from_current",
    "current_pointer_path",
    "current_segment_paths",
    "discard_segment_plan_artifacts",
    "empty_current_pointer",
    "empty_manifest",
    "ensure_current_segment",
    "finish_rotate_all_current_segments",
    "fsync_dir",
    "inspect_segment_apply_state",
    "manifest_path",
    "manifest_segments",
    "manifest_signature",
    "load_pending_rotation",
    "new_current_segment",
    "open_segment_payload",
    "pending_rotation_path",
    "plan_segments_older_than",
    "preflight_segments_older_than",
    "raw_segment_lock_available",
    "raw_segment_lock_path",
    "read_apply_marker",
    "read_apply_status",
    "read_current_pointer",
    "read_manifest",
    "read_pending_rotation",
    "reconcile_apply_marker",
    "reconcile_apply_marker_unlocked",
    "reconcile_pending_rotation",
    "retention_preview_from_current",
    "retention_preview_from_manifest",
    "rotate_all_current_segments",
    "rotate_current_segment",
    "row_time",
    "segment_apply_marker_path",
    "strict_read_current_pointer",
    "strict_read_manifest",
    "sweep_apply_marker",
    "unlink_empty_closed_segment",
    "validate_current_pointer_entries",
    "validate_current_segment_entry",
    "validate_segment_path",
    "validate_segment_plans",
    "write_apply_marker",
    "write_current_pointer",
    "write_json_atomic",
    "write_manifest",
    "write_pending_rotation",
]
