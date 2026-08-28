"""Retention preview composition, caching, and snapshot handoff."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dashboard_retention_index as retention_index
import raw_segments

EMPTY_RETENTION_SOURCE_SIGNATURE: tuple[tuple[str, bool, int, int], ...] = ()
RETENTION_PREVIEW_CACHE: dict[
    tuple[
        str,
        float,
        tuple[tuple[str, bool, int, int], ...],
        tuple[tuple[str, bool, int, int], ...],
        tuple[tuple[str, int, int], ...],
    ],
    dict[str, Any],
] = {}
RETENTION_PREVIEW_CACHE_LIMIT = 16


class RetentionPreviewStale(raw_segments.ManifestError):
    pass


def clear_retention_preview_cache() -> None:
    RETENTION_PREVIEW_CACHE.clear()


def retention_preview_signature(base: pathlib.Path, cutoff_unix: float) -> str:
    payload = {
        "cutoff_unix": float(cutoff_unix),
        "sources": EMPTY_RETENTION_SOURCE_SIGNATURE,
        "current_sources": retention_index.current_retention_source_signature(base),
        "manifest": raw_segments.manifest_signature(base),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def freeze_retention_snapshot(base: pathlib.Path, cutoff_unix: float, expected_signature: str) -> dict[str, Any]:
    """Validate the preview and swap current pointers under one append lock."""
    raw_segments.reconcile_pending_rotation(base)
    with raw_segments.acquire_raw_segment_lock(base):
        if retention_preview_signature(base, cutoff_unix) != expected_signature:
            raise RetentionPreviewStale("cleanup preview changed before retention snapshot")
        rotation = raw_segments.begin_rotate_all_current_segments_unlocked(base)
    return raw_segments.finish_rotate_all_current_segments(base, rotation)


def retention_preview_profile_fields(
    *,
    preview_source: str,
    index_refreshed: bool,
    files: list[dict[str, Any]],
    current_signature: tuple[tuple[str, bool, int, int], ...],
) -> dict[str, Any]:
    current_paths = {str(path) for path, _delete_when_empty, _size, _mtime_ns in current_signature}
    scanned_current_files = [file for file in files if str(file.get("path") or "") in current_paths and preview_source == "fallback_scan"]
    return {
        "preview_source": preview_source,
        "retention_index_refreshed": bool(index_refreshed),
        "current_scan_files": len(scanned_current_files),
        "current_scan_bytes": sum(int(file.get("source_size") or 0) for file in scanned_current_files),
    }


def retention_preview(token_usage_root: pathlib.Path | str, cutoff_unix: float, *, refresh_index: bool = True) -> dict[str, Any]:
    base = pathlib.Path(token_usage_root).expanduser()
    cutoff = float(cutoff_unix)
    raw_segments.strict_read_manifest(base)
    if raw_segments.read_pending_rotation(base) is not None:
        raise raw_segments.ManifestError("pending rotation must be resolved before cleanup preview")
    apply_status = raw_segments.read_apply_status(base)
    if apply_status.pending and apply_status.phase is not raw_segments.ApplyMarkerPhase.UNLINK_PENDING:
        raise raw_segments.ManifestError("pending segment apply must be resolved before cleanup preview")
    signature = EMPTY_RETENTION_SOURCE_SIGNATURE
    current_signature = retention_index.current_retention_source_signature(base)
    manifest_signature = raw_segments.manifest_signature(base)
    cache_key = (str(base.resolve()), cutoff, signature, current_signature, manifest_signature)
    cached = RETENTION_PREVIEW_CACHE.get(cache_key)
    if cached is not None:
        return json.loads(json.dumps(cached))
    manifest_preview = raw_segments.retention_preview_from_manifest(base, cutoff)
    if manifest_preview is not None:
        tracked_paths: set[str] = set()
        source_previews = []
        tracked_paths.update(str(path) for path in manifest_preview.get("_tracked_paths", []))
        source_previews.append(manifest_preview)
        index_signature = retention_index.retention_index_signature(base, tracked_paths)
        existing_index = retention_index.load_retention_index(base, index_signature)
        had_index = existing_index is not None
        indexed = retention_index.refresh_retention_index(base, index_signature) if refresh_index else existing_index
        indexed_preview = retention_index.retention_preview_from_index(indexed, cutoff) if indexed is not None else None
        preview_source = "index" if had_index else "refreshed_index"
        indexed_files = (
            indexed_preview["files"]
            if indexed_preview is not None
            else [retention_index.preview_retention_source(pathlib.Path(path), cutoff) for path, _delete_when_empty, _size, _mtime_ns in index_signature]
        )
        if indexed_preview is None:
            preview_source = "fallback_scan"
        files = [file for preview in source_previews for file in preview["files"]]
        files.extend(indexed_files)
        scanned_rows = sum(int(item["scanned_rows"]) for item in files)
        deletable_rows = sum(int(item["deletable_rows"]) for item in files)
        deletable_bytes = sum(int(item["deletable_bytes"]) for item in files)
        result = {
            "cutoff_unix": cutoff,
            "scanned_rows": scanned_rows,
            "deletable_rows": deletable_rows,
            "deletable_bytes": deletable_bytes,
            "kept_rows": scanned_rows - deletable_rows,
            "affected_files": sum(1 for item in files if item["affected"]),
            "files": files,
            "from_manifest": manifest_preview is not None,
            "from_current": bool(current_signature),
            **retention_preview_profile_fields(
                preview_source=preview_source,
                index_refreshed=not had_index,
                files=files,
                current_signature=current_signature,
            ),
        }
        RETENTION_PREVIEW_CACHE[cache_key] = json.loads(json.dumps(result))
        if len(RETENTION_PREVIEW_CACHE) > RETENTION_PREVIEW_CACHE_LIMIT:
            oldest_key = next(iter(RETENTION_PREVIEW_CACHE))
            RETENTION_PREVIEW_CACHE.pop(oldest_key, None)
        return result
    index_signature = retention_index.retention_index_signature(base)
    existing_index = retention_index.load_retention_index(base, index_signature)
    had_index = existing_index is not None
    index = retention_index.refresh_retention_index(base, index_signature) if refresh_index else existing_index
    indexed = retention_index.retention_preview_from_index(index, cutoff) if index is not None else None
    if indexed is not None:
        indexed["from_current"] = bool(current_signature)
        indexed.update(
            retention_preview_profile_fields(
                preview_source="index" if had_index else "refreshed_index",
                index_refreshed=not had_index,
                files=indexed.get("files", []),
                current_signature=current_signature,
            )
        )
        RETENTION_PREVIEW_CACHE[cache_key] = json.loads(json.dumps(indexed))
        if len(RETENTION_PREVIEW_CACHE) > RETENTION_PREVIEW_CACHE_LIMIT:
            oldest_key = next(iter(RETENTION_PREVIEW_CACHE))
            RETENTION_PREVIEW_CACHE.pop(oldest_key, None)
        return indexed
    files = [retention_index.preview_retention_source(pathlib.Path(path), cutoff) for path, _delete_when_empty, _size, _mtime_ns in index_signature]
    scanned_rows = sum(int(item["scanned_rows"]) for item in files)
    deletable_rows = sum(int(item["deletable_rows"]) for item in files)
    deletable_bytes = sum(int(item["deletable_bytes"]) for item in files)
    result = {
        "cutoff_unix": cutoff,
        "scanned_rows": scanned_rows,
        "deletable_rows": deletable_rows,
        "deletable_bytes": deletable_bytes,
        "kept_rows": scanned_rows - deletable_rows,
        "affected_files": sum(1 for item in files if item["affected"]),
        "files": files,
        "from_index": False,
        "from_current": bool(current_signature),
        **retention_preview_profile_fields(
            preview_source="fallback_scan",
            index_refreshed=not had_index,
            files=files,
            current_signature=current_signature,
        ),
    }
    RETENTION_PREVIEW_CACHE[cache_key] = json.loads(json.dumps(result))
    if len(RETENTION_PREVIEW_CACHE) > RETENTION_PREVIEW_CACHE_LIMIT:
        oldest_key = next(iter(RETENTION_PREVIEW_CACHE))
        RETENTION_PREVIEW_CACHE.pop(oldest_key, None)
    return result
