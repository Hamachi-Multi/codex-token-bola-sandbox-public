"""Raw current segment creation and rotation helpers."""

from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import time
from typing import Any

from raw_segments_common import (
    ManifestError,
    acquire_raw_segment_lock,
    acquire_raw_segment_manifest_lock,
    fsync_dir,
    scan_jsonl_bytes,
)
from raw_segments_state import (
    clear_pending_rotation,
    read_pending_rotation,
    source_name_for_kind,
    strict_read_current_pointer,
    strict_read_manifest,
    validate_current_segment_entry,
    validate_segment_path,
    write_current_pointer,
    write_manifest,
    write_pending_rotation,
)

def new_current_segment(base: pathlib.Path, *, kind: str, source_name: str | None = None) -> dict[str, Any]:
    source_name = source_name or source_name_for_kind(kind)
    if source_name != source_name_for_kind(kind):
        raise ManifestError(f"current segment source mismatch: {source_name}")
    base_path = pathlib.Path(base).expanduser()
    raw_current = base_path / "raw" / "current"
    if (base_path / "raw").is_symlink() or raw_current.is_symlink():
        raise ManifestError(f"raw/current must not be a symlink: {raw_current}")
    raw_current.mkdir(parents=True, exist_ok=True)
    segment_id = f"{source_name}.current.{time.time_ns()}"
    path = raw_current / f"{segment_id}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return {
        "id": segment_id,
        "kind": kind,
        "path": str(path),
        "source_name": source_name,
        "created_at_unix": time.time(),
    }


def ensure_current_segment(base: pathlib.Path, *, kind: str, source_name: str | None = None) -> dict[str, Any]:
    source_name = source_name or source_name_for_kind(kind)
    if source_name != source_name_for_kind(kind):
        raise ManifestError(f"current segment source mismatch: {source_name}")
    pointer = strict_read_current_pointer(base)
    current = pointer.get("current", {}).get(kind)
    if current is not None:
        return validate_current_segment_entry(base, current, kind=kind)
    segment = new_current_segment(base, kind=kind, source_name=source_name)
    pointer.setdefault("current", {})[kind] = segment
    write_current_pointer(base, pointer)
    return segment



def scan_segment_file(path: pathlib.Path, *, kind: str) -> dict[str, Any]:
    try:
        payload = pathlib.Path(path).read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read raw current segment before closing it: {path}") from exc
    scan = scan_jsonl_bytes(payload, kind=kind)
    scan["bytes"] = pathlib.Path(path).stat().st_size
    scan["uncompressed_bytes"] = len(payload)
    scan["sha256"] = hashlib.sha256(payload).hexdigest()
    return scan


def closed_segment_from_current(current: dict[str, Any], scan: dict[str, Any], *, kind: str) -> dict[str, Any]:
    return {
        "id": current["id"],
        "kind": kind,
        "path": str(pathlib.Path(str(current["path"]))),
        "format": "jsonl",
        "source_name": current.get("source_name") or source_name_for_kind(kind),
        "created_at_unix": current.get("created_at_unix"),
        "closed_at_unix": time.time(),
        "min_time_unix": scan["min_time_unix"],
        "max_time_unix": scan["max_time_unix"],
        "rows": scan["rows"],
        "undated_rows": scan["undated_rows"],
        "corrupt_rows": scan["corrupt_rows"],
        "unknown_rows": scan["unknown_rows"],
        "days": scan["days"],
        "bytes": scan["bytes"],
        "uncompressed_bytes": scan["uncompressed_bytes"],
        "sha256": scan["sha256"],
        "status": "closed",
    }


def append_closed_segment(base: pathlib.Path, closed_segment: dict[str, Any]) -> None:
    if int(closed_segment.get("rows") or 0) <= 0:
        return
    with acquire_raw_segment_manifest_lock(base):
        manifest = strict_read_manifest(base)
        segments = [
            item
            for item in manifest.get("segments", [])
            if isinstance(item, dict) and item.get("id") != closed_segment["id"]
        ]
        segments.append(closed_segment)
        manifest["segments"] = sorted(segments, key=lambda item: (str(item.get("kind") or ""), str(item.get("path") or "")))
        write_manifest(base, manifest)


def unlink_empty_closed_segment(base: pathlib.Path, closed_segment: dict[str, Any]) -> None:
    if int(closed_segment.get("rows") or 0) > 0:
        return
    path = validate_segment_path(base, closed_segment)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def pending_rotation_entries(marker: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    if marker.get("operation") == "rotate_current_segment":
        kind = str(marker.get("kind") or "")
        old_segment = marker.get("old_segment")
        new_segment = marker.get("new_segment")
        if not isinstance(old_segment, dict) or not isinstance(new_segment, dict):
            raise ManifestError("pending raw segment rotation marker is missing segment identity")
        return [(kind, old_segment, new_segment)]
    if marker.get("operation") == "rotate_current_segments":
        segments = marker.get("segments")
        if not isinstance(segments, dict) or not segments:
            raise ManifestError("pending raw segment rotation marker is missing segment identities")
        entries = []
        for kind, pair in sorted(segments.items()):
            if not isinstance(pair, dict):
                raise ManifestError("pending raw segment rotation marker has invalid segment pair")
            old_segment = pair.get("old_segment")
            new_segment = pair.get("new_segment")
            if not isinstance(old_segment, dict) or not isinstance(new_segment, dict):
                raise ManifestError("pending raw segment rotation marker is missing segment identity")
            entries.append((str(kind), old_segment, new_segment))
        return entries
    raise ManifestError(f"unsupported pending raw segment rotation operation: {marker.get('operation')}")


def finish_rotated_segment(base: pathlib.Path, marker: dict[str, Any], *, clear_marker: bool = True, unlink_empty: bool = False) -> dict[str, Any]:
    kind = str(marker.get("kind") or "")
    old_segment = validate_current_segment_entry(base, marker.get("old_segment") or {}, kind=kind)
    scan = scan_segment_file(pathlib.Path(str(old_segment["path"])), kind=kind)
    closed_segment = closed_segment_from_current(old_segment, scan, kind=kind)
    append_closed_segment(base, closed_segment)
    if unlink_empty:
        unlink_empty_closed_segment(base, closed_segment)
    if clear_marker:
        clear_pending_rotation(base)
    return closed_segment


def reconcile_pending_rotation(base: pathlib.Path) -> None:
    marker = read_pending_rotation(base)
    if marker is None:
        return
    closed_segments = marker.get("closed_segments") if isinstance(marker.get("closed_segments"), dict) else {}
    entries = []
    for kind, old_segment, new_segment in pending_rotation_entries(marker):
        new_valid = validate_current_segment_entry(base, new_segment, kind=kind)
        closed_segment = closed_segments.get(kind) if isinstance(closed_segments, dict) else None
        allow_missing_empty_old = (
            marker.get("operation") == "rotate_current_segments"
            and isinstance(closed_segment, dict)
            and int(closed_segment.get("rows") or 0) == 0
        )
        try:
            old_valid = validate_current_segment_entry(base, old_segment, kind=kind)
            skip_finish = False
        except ManifestError:
            if not allow_missing_empty_old:
                raise
            old_valid = validate_current_segment_entry(base, old_segment, kind=kind, path_must_exist=False)
            skip_finish = True
        entries.append((kind, old_valid, new_valid, skip_finish))
    with acquire_raw_segment_lock(base):
        strict_read_manifest(base)
        pointer = strict_read_current_pointer(base)
        old_still_current = []
        for kind, old_segment, _new_segment, _skip_finish in entries:
            current = pointer.get("current", {}).get(kind)
            if isinstance(current, dict) and current.get("path") == str(old_segment["path"]):
                old_still_current.append(kind)
        if old_still_current:
            if marker.get("phase") == "pointer_pending" and len(old_still_current) == len(entries):
                for _kind, _old_segment, new_segment, _skip_finish in entries:
                    new_path = pathlib.Path(str(new_segment.get("path") or ""))
                    try:
                        if new_path.exists() and new_path.stat().st_size == 0:
                            new_path.unlink()
                            fsync_dir(new_path.parent)
                    except OSError as exc:
                        raise ManifestError(f"cannot remove empty pending current segment: {new_path}") from exc
                clear_pending_rotation(base)
                return
            raise ManifestError(f"pending rotation old segment is still current: {old_still_current}")
        marker["phase"] = "manifest_pending"
        write_pending_rotation(base, marker)
    unlink_empty = marker.get("operation") == "rotate_current_segments"
    for kind, old_segment, _new_segment, skip_finish in entries:
        if skip_finish:
            continue
        finish_rotated_segment(base, {"kind": kind, "old_segment": old_segment}, clear_marker=False, unlink_empty=unlink_empty)
    clear_pending_rotation(base)


def rotate_current_segment(base: pathlib.Path, *, kind: str, source_name: str | None = None) -> dict[str, Any]:
    reconcile_pending_rotation(base)
    with acquire_raw_segment_lock(base):
        strict_read_manifest(base)
        pointer = strict_read_current_pointer(base)
        old_current = ensure_current_segment(base, kind=kind, source_name=source_name)
        new_current = new_current_segment(base, kind=kind, source_name=source_name or str(old_current.get("source_name") or source_name_for_kind(kind)))
        pointer = strict_read_current_pointer(base)
        pointer.setdefault("current", {})[kind] = new_current
        marker = {
            "operation": "rotate_current_segment",
            "phase": "pointer_pending",
            "kind": kind,
            "old_segment": old_current,
            "new_segment": new_current,
            "created_at_unix": time.time(),
        }
        write_pending_rotation(base, marker)
        write_current_pointer(base, pointer)
        marker["phase"] = "manifest_pending"
        write_pending_rotation(base, marker)
    closed_segment = finish_rotated_segment(base, marker)
    return {"closed_segment": closed_segment, "current_segment": new_current}


def rotate_all_current_segments(base: pathlib.Path) -> dict[str, Any]:
    reconcile_pending_rotation(base)
    kinds = ("prompt_usage",)
    old_segments: dict[str, dict[str, Any]] = {}
    new_segments: dict[str, dict[str, Any]] = {}
    with acquire_raw_segment_lock(base):
        strict_read_manifest(base)
        pointer = strict_read_current_pointer(base)
        for kind in kinds:
            old_segments[kind] = ensure_current_segment(base, kind=kind, source_name=source_name_for_kind(kind))
            new_segments[kind] = new_current_segment(base, kind=kind, source_name=source_name_for_kind(kind))
            pointer.setdefault("current", {})[kind] = new_segments[kind]
        marker = {
            "operation": "rotate_current_segments",
            "phase": "pointer_pending",
            "segments": {kind: {"old_segment": old_segments[kind], "new_segment": new_segments[kind]} for kind in kinds},
            "created_at_unix": time.time(),
        }
        write_pending_rotation(base, marker)
        write_current_pointer(base, pointer)
        marker["phase"] = "manifest_pending"
        write_pending_rotation(base, marker)
    closed_segments = {}
    for kind in kinds:
        scan = scan_segment_file(pathlib.Path(str(old_segments[kind]["path"])), kind=kind)
        closed_segment = closed_segment_from_current(old_segments[kind], scan, kind=kind)
        append_closed_segment(base, closed_segment)
        marker.setdefault("closed_segments", {})[kind] = closed_segment
        write_pending_rotation(base, marker)
        unlink_empty_closed_segment(base, closed_segment)
        closed_segments[kind] = closed_segment
    clear_pending_rotation(base)
    return {kind: {"closed_segment": closed_segments[kind], "current_segment": new_segments[kind]} for kind in kinds}
