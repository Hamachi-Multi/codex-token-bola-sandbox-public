#!/usr/bin/env python3
"""Raw segment manifest and rotation helpers for token usage logs.

This module is the public compatibility facade. Implementation lives in the
raw segment submodules, while existing imports can keep using ``raw_segments``.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import raw_segments_retention as _retention
import raw_segments_rotation as _rotation
import raw_segments_state as _state

from raw_segments_common import *  # noqa: F401,F403
from raw_segments_state import *  # noqa: F401,F403
from raw_segments_retention import *  # noqa: F401,F403
from raw_segments_rotation import *  # noqa: F401,F403


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


def plan_segments_older_than(base: pathlib.Path, cutoff_unix: float, *, create_output_dirs: bool = True) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.plan_segments_older_than(base, cutoff_unix, create_output_dirs=create_output_dirs)


def preflight_segments_older_than(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.preflight_segments_older_than(base, cutoff_unix)


def validate_segment_plans(base: pathlib.Path, segment_plan: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.validate_segment_plans(base, segment_plan)


def apply_segment_plans(base: pathlib.Path, segment_plan: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_RAW_SEGMENT_DEPENDENCIES.retention.apply_segment_plans(base, segment_plan)


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


__all__ = [name for name in globals() if not name.startswith("_")]
