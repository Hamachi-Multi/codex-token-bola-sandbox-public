"""Raw segment manifest, pointer, marker, and validation helpers."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from datetime import datetime, timezone
from typing import Any

from raw_segments_common import (
    PROMPT_RAW_NAME,
    ManifestError,
    acquire_raw_segment_manifest_lock,
    current_pointer_path,
    fsync_dir,
    manifest_path,
    pending_rotation_path,
    read_segment_payload,
    scan_jsonl_bytes,
    segment_apply_marker_path,
    write_json_atomic,
)

def empty_manifest(base: pathlib.Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "base": str(pathlib.Path(base).expanduser().resolve()),
        "updated_at_unix": time.time(),
        "segments": [],
    }


def strict_read_manifest(base: pathlib.Path) -> dict[str, Any]:
    path = manifest_path(base)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_manifest(base)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read raw segment manifest: {path}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        raise ManifestError(f"unsupported raw segment manifest schema: {path}")
    if parsed.get("base") != str(pathlib.Path(base).expanduser().resolve()):
        raise ManifestError(f"raw segment manifest base mismatch: {path}")
    if not isinstance(parsed.get("segments"), list):
        raise ManifestError(f"raw segment manifest segments must be a list: {path}")
    for segment in parsed["segments"]:
        if not isinstance(segment, dict):
            raise ManifestError(f"raw segment manifest entries must be objects: {path}")
    return parsed


def read_manifest(base: pathlib.Path) -> dict[str, Any]:
    try:
        return strict_read_manifest(base)
    except ManifestError:
        return empty_manifest(base)


def write_manifest(base: pathlib.Path, manifest: dict[str, Any]) -> None:
    path = manifest_path(base)
    payload = dict(manifest)
    payload["schema_version"] = 1
    payload["base"] = str(pathlib.Path(base).expanduser().resolve())
    payload["updated_at_unix"] = time.time()
    write_json_atomic(path, payload)


def empty_current_pointer(base: pathlib.Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "base": str(pathlib.Path(base).expanduser().resolve()),
        "updated_at_unix": time.time(),
        "current": {},
    }


def strict_read_current_pointer(base: pathlib.Path) -> dict[str, Any]:
    path = current_pointer_path(base)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_current_pointer(base)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read current raw segment pointer: {path}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version", 1) != 1:
        raise ManifestError(f"unsupported current raw segment pointer schema: {path}")
    if parsed.get("base", str(pathlib.Path(base).expanduser().resolve())) != str(pathlib.Path(base).expanduser().resolve()):
        raise ManifestError(f"current raw segment pointer base mismatch: {path}")
    if not isinstance(parsed.get("current"), dict):
        raise ManifestError(f"current raw segment pointer current must be an object: {path}")
    parsed["schema_version"] = 1
    parsed["base"] = str(pathlib.Path(base).expanduser().resolve())
    return parsed


def read_current_pointer(base: pathlib.Path) -> dict[str, Any]:
    try:
        return strict_read_current_pointer(base)
    except ManifestError:
        return empty_current_pointer(base)


def write_current_pointer(base: pathlib.Path, pointer: dict[str, Any]) -> None:
    path = current_pointer_path(base)
    payload = dict(pointer)
    payload["schema_version"] = 1
    payload["base"] = str(pathlib.Path(base).expanduser().resolve())
    payload["updated_at_unix"] = time.time()
    payload.setdefault("current", {})
    write_json_atomic(path, payload)


def validate_current_pointer_entries(base: pathlib.Path) -> list[dict[str, Any]]:
    pointer = strict_read_current_pointer(base)
    entries: list[dict[str, Any]] = []
    for kind, segment in sorted(pointer.get("current", {}).items()):
        if not isinstance(segment, dict):
            raise ManifestError(f"current segment entry must be an object: {kind}")
        entries.append(validate_current_segment_entry(base, segment, kind=str(kind)))
    return entries


def write_pending_rotation(base: pathlib.Path, marker: dict[str, Any]) -> None:
    path = pending_rotation_path(base)
    payload = dict(marker)
    payload["schema_version"] = 1
    payload["base"] = str(pathlib.Path(base).expanduser().resolve())
    write_json_atomic(path, payload)


def read_pending_rotation(base: pathlib.Path) -> dict[str, Any] | None:
    path = pending_rotation_path(base)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read pending raw segment rotation marker: {path}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version", 1) != 1:
        raise ManifestError(f"unsupported pending raw segment rotation marker schema: {path}")
    if parsed.get("base", str(pathlib.Path(base).expanduser().resolve())) != str(pathlib.Path(base).expanduser().resolve()):
        raise ManifestError(f"pending raw segment rotation marker base mismatch: {path}")
    return parsed


def clear_pending_rotation(base: pathlib.Path) -> None:
    try:
        path = pending_rotation_path(base)
        path.unlink()
        fsync_dir(path.parent)
    except FileNotFoundError:
        pass


def source_name_for_kind(kind: str) -> str:
    if kind == "prompt_usage":
        return PROMPT_RAW_NAME
    raise ManifestError(f"unsupported raw segment kind: {kind}")


def expected_source_name(kind: str) -> str:
    return source_name_for_kind(kind)


def format_for_path(path: pathlib.Path) -> str:
    return "jsonl.gz" if path.suffix == ".gz" else "jsonl"


def compact_timestamp(value: float | None) -> str:
    if value is None:
        return "undated"
    return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y%m%d%H%M%S")


def validate_segment_path(base: pathlib.Path, segment: dict[str, Any], *, path_must_exist: bool = True) -> pathlib.Path:
    if not isinstance(segment, dict):
        raise ManifestError("raw segment entry must be an object")
    kind = str(segment.get("kind") or "")
    source_name = source_name_for_kind(kind)
    if segment.get("source_name") != source_name:
        raise ManifestError(f"raw segment source mismatch: {segment.get('source_name')}")
    if segment.get("format") not in {"jsonl", "jsonl.gz"}:
        raise ManifestError(f"raw segment format mismatch: {segment.get('format')}")
    path = pathlib.Path(str(segment.get("path") or "")).expanduser()
    base_path = pathlib.Path(base).expanduser()
    raw_dir = base_path / "raw"
    current_dir = raw_dir / "current"
    archive_dir = raw_dir / "archive"
    if raw_dir.is_symlink() or current_dir.is_symlink() or archive_dir.is_symlink():
        raise ManifestError(f"raw segment roots must not be symlinks: {raw_dir}")
    if path.parent.is_symlink() or path.is_symlink():
        raise ManifestError(f"raw segment path must not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=path_must_exist)
    except FileNotFoundError as exc:
        raise ManifestError(f"raw segment path missing: {path}") from exc
    roots = (current_dir.resolve(strict=False), archive_dir.resolve(strict=False))
    if not any(root in resolved.parents for root in roots):
        raise ManifestError(f"raw segment path outside managed roots: {path}")
    if not path.name.startswith(f"{source_name}."):
        raise ManifestError(f"raw segment filename/source mismatch: {path}")
    if format_for_path(path) != segment.get("format"):
        raise ManifestError(f"raw segment file format mismatch: {path}")
    if path_must_exist and not path.is_file():
        raise ManifestError(f"raw segment path must be a regular file: {path}")
    return path


def manifest_segments(base: pathlib.Path, *, kind: str) -> list[pathlib.Path]:
    manifest = strict_read_manifest(base)
    paths: list[pathlib.Path] = []
    seen: set[str] = set()
    for segment in manifest.get("segments", []):
        if not isinstance(segment, dict) or segment.get("kind") != kind or segment.get("status", "closed") != "closed":
            continue
        path = validate_segment_path(base, segment)
        resolved = str(path.expanduser().resolve(strict=True))
        if resolved not in seen:
            paths.append(path)
            seen.add(resolved)
    return sorted(paths)


def current_segment_paths(base: pathlib.Path, *, kind: str) -> list[pathlib.Path]:
    pointer = strict_read_current_pointer(base)
    current = pointer.get("current", {}).get(kind)
    if not isinstance(current, dict):
        return []
    segment = validate_current_segment_entry(base, current, kind=kind)
    return [pathlib.Path(str(segment["path"]))]


def write_apply_marker(base: pathlib.Path, marker: dict[str, Any]) -> None:
    path = segment_apply_marker_path(base)
    payload = dict(marker)
    payload["schema_version"] = 1
    payload["base"] = str(pathlib.Path(base).expanduser().resolve())
    write_json_atomic(path, payload)


def read_apply_marker(base: pathlib.Path) -> dict[str, Any] | None:
    path = segment_apply_marker_path(base)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read pending raw segment apply marker: {path}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version", 1) != 1:
        raise ManifestError(f"unsupported pending raw segment apply marker schema: {path}")
    if parsed.get("base", str(pathlib.Path(base).expanduser().resolve())) != str(pathlib.Path(base).expanduser().resolve()):
        raise ManifestError(f"pending raw segment apply marker base mismatch: {path}")
    if parsed.get("phase") not in {"manifest_pending", "unlink_pending"}:
        raise ManifestError(f"unsupported pending raw segment apply phase: {parsed.get('phase')}")
    for key in ("previous_manifest", "next_manifest", "source_segments", "retained_segments"):
        if key not in parsed:
            raise ManifestError(f"pending raw segment apply marker missing {key}")
    return parsed


def clear_apply_marker(base: pathlib.Path) -> None:
    try:
        path = segment_apply_marker_path(base)
        path.unlink()
        fsync_dir(path.parent)
    except FileNotFoundError:
        pass


def manifest_segments_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left.get("segments", []) == right.get("segments", [])


def segment_map(segments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(segment.get("id") or ""): segment for segment in segments}


def retained_staging_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.stage")


def validate_retained_staging_path(base: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    candidate = pathlib.Path(path).expanduser()
    archive_dir = pathlib.Path(base).expanduser() / "raw" / "archive"
    if (pathlib.Path(base).expanduser() / "raw").is_symlink() or archive_dir.is_symlink():
        raise ManifestError(f"raw/archive must not be a symlink: {archive_dir}")
    if candidate.parent.is_symlink() or candidate.is_symlink():
        raise ManifestError(f"retained staging path must not be a symlink: {candidate}")
    resolved = candidate.resolve(strict=False)
    if archive_dir.resolve(strict=False) not in resolved.parents:
        raise ManifestError(f"retained staging path outside raw/archive: {candidate}")
    if not candidate.name.startswith(".") or candidate.suffix != ".stage":
        raise ManifestError(f"retained staging path must be compatibility-invisible: {candidate}")
    return candidate


def marker_staging_paths(base: pathlib.Path, marker: dict[str, Any]) -> dict[str, pathlib.Path]:
    entries = marker.get("retained_staging_paths") or []
    if not isinstance(entries, list):
        raise ManifestError("pending raw segment apply marker retained staging paths must be a list")
    paths: dict[str, pathlib.Path] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ManifestError("pending raw segment apply marker staging entry must be an object")
        segment_id = str(item.get("id") or "")
        if not segment_id:
            raise ManifestError("pending raw segment apply marker staging entry missing id")
        if segment_id in paths:
            raise ManifestError(f"pending raw segment apply marker staging id duplicated: {segment_id}")
        paths[segment_id] = validate_retained_staging_path(base, pathlib.Path(str(item.get("path") or "")))
    return paths


def unlink_segment_sources(base: pathlib.Path, source_segments: list[dict[str, Any]]) -> int:
    result = sweep_segment_sources(base, source_segments)
    if result["pending_source_segments"]:
        raise ManifestError("raw segment source unlink failed")
    return int(result["deleted_files"])


def sweep_segment_sources(base: pathlib.Path, source_segments: list[dict[str, Any]]) -> dict[str, Any]:
    deleted = 0
    pending: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for segment in source_segments:
        path = validate_segment_path(base, segment, path_must_exist=False)
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            pending.append(segment)
            errors.append({"path": str(path), "error": str(exc)})
    return {"deleted_files": deleted, "pending_source_segments": pending, "errors": errors}


def verify_retained_segments_for_marker(base: pathlib.Path, retained_segments: list[dict[str, Any]]) -> None:
    for segment in retained_segments:
        path = validate_segment_path(base, segment)
        payload = read_segment_payload(path)
        if hashlib.sha256(payload).hexdigest() != segment.get("sha256"):
            raise ManifestError(f"retained raw segment checksum mismatch: {path}")
        scan = scan_jsonl_bytes(payload, kind=str(segment.get("kind") or ""))
        for key in ("rows", "undated_rows", "corrupt_rows", "unknown_rows"):
            if int(scan[key]) != int(segment.get(key) or 0):
                raise ManifestError(f"retained raw segment {key} mismatch: {path}")
        if scan.get("days") != segment.get("days", []):
            raise ManifestError(f"retained raw segment day histogram mismatch: {path}")


def validate_apply_marker_delta(
    base: pathlib.Path,
    previous_manifest: dict[str, Any],
    source_segments: list[dict[str, Any]],
    retained_segments: list[dict[str, Any]],
    next_manifest: dict[str, Any],
    *,
    source_must_exist: bool,
) -> None:
    previous_segments = validate_retention_manifest_segments(previous_manifest.get("segments", []), base=base, validate_paths=True, path_must_exist=False)
    next_segments = validate_retention_manifest_segments(next_manifest.get("segments", []), base=base, validate_paths=True, path_must_exist=False)
    source_segments = validate_retention_manifest_segments(source_segments, base=base, validate_paths=True, path_must_exist=source_must_exist)
    retained_segments = validate_retention_manifest_segments(retained_segments, base=base, validate_paths=True, path_must_exist=False)
    previous_by_id = segment_map(previous_segments)
    next_by_id = segment_map(next_segments)
    source_by_id = segment_map(source_segments)
    retained_by_id = segment_map(retained_segments)
    removed_ids = set(previous_by_id) - set(next_by_id)
    added_ids = set(next_by_id) - set(previous_by_id)
    if set(source_by_id) != removed_ids:
        raise ManifestError("pending raw segment apply marker source delta mismatch")
    if set(retained_by_id) != added_ids:
        raise ManifestError("pending raw segment apply marker retained delta mismatch")
    for segment_id in removed_ids:
        if source_by_id[segment_id] != previous_by_id[segment_id]:
            raise ManifestError(f"pending raw segment apply marker source mismatch: {segment_id}")
    for segment_id in added_ids:
        if retained_by_id[segment_id] != next_by_id[segment_id]:
            raise ManifestError(f"pending raw segment apply marker retained mismatch: {segment_id}")
    expected_next = [
        segment
        for segment in previous_segments
        if str(segment.get("id") or "") not in removed_ids
    ]
    expected_next.extend(retained_segments)
    expected_next = sorted(expected_next, key=lambda item: (str(item.get("kind") or ""), str(item.get("path") or "")))
    if expected_next != next_segments:
        raise ManifestError("pending raw segment apply marker next manifest mismatch")


def publish_retained_staging(base: pathlib.Path, marker: dict[str, Any], retained_segments: list[dict[str, Any]]) -> None:
    staging_paths = marker_staging_paths(base, marker)
    for segment in retained_segments:
        segment_id = str(segment.get("id") or "")
        final_path = validate_segment_path(base, segment, path_must_exist=False)
        if final_path.exists():
            continue
        staging_path = staging_paths.get(segment_id)
        if staging_path is None:
            raise ManifestError(f"pending raw segment apply marker missing retained staging path: {segment_id}")
        if not staging_path.is_file():
            raise ManifestError(f"pending raw segment apply retained staging path missing: {staging_path}")
        staging_path.replace(final_path)
        final_path.chmod(0o600)


def reconcile_apply_marker(base: pathlib.Path) -> None:
    sweep_apply_marker(base)


def sweep_apply_marker(base: pathlib.Path) -> dict[str, Any]:
    with acquire_raw_segment_manifest_lock(base):
        return reconcile_apply_marker_unlocked(base)


def reconcile_apply_marker_unlocked(base: pathlib.Path) -> dict[str, Any]:
    marker = read_apply_marker(base)
    if marker is None:
        return {"deleted_files": 0, "pending_files": 0, "pending_source_segments": [], "errors": []}
    previous_manifest = marker["previous_manifest"]
    next_manifest = marker["next_manifest"]
    source_segments = validate_retention_manifest_segments(marker["source_segments"], base=base, validate_paths=True, path_must_exist=False)
    retained_segments = validate_retention_manifest_segments(marker["retained_segments"], base=base, validate_paths=True, path_must_exist=False)
    current_manifest = strict_read_manifest(base)
    if manifest_segments_match(current_manifest, next_manifest):
        validate_apply_marker_delta(base, previous_manifest, source_segments, retained_segments, next_manifest, source_must_exist=False)
        verify_retained_segments_for_marker(base, retained_segments)
    else:
        if not manifest_segments_match(current_manifest, previous_manifest):
            raise ManifestError("pending raw segment apply marker does not match current manifest")
        validate_apply_marker_delta(base, previous_manifest, source_segments, retained_segments, next_manifest, source_must_exist=True)
        publish_retained_staging(base, marker, retained_segments)
        verify_retained_segments_for_marker(base, retained_segments)
        write_manifest(base, next_manifest)
        marker["phase"] = "unlink_pending"
        marker["retained_staging_paths"] = []
        write_apply_marker(base, marker)
    pending_segments = marker.get("unlink_pending_segments")
    if not isinstance(pending_segments, list):
        pending_segments = source_segments
    pending_segments = validate_retention_manifest_segments(pending_segments, base=base, validate_paths=True, path_must_exist=False)
    sweep = sweep_segment_sources(base, pending_segments)
    if sweep["pending_source_segments"]:
        marker["phase"] = "unlink_pending"
        marker["unlink_pending_segments"] = sweep["pending_source_segments"]
        marker["unlink_errors"] = sweep["errors"]
        write_apply_marker(base, marker)
    else:
        clear_apply_marker(base)
    return {
        "deleted_files": int(sweep["deleted_files"]),
        "pending_files": len(sweep["pending_source_segments"]),
        "pending_source_segments": sweep["pending_source_segments"],
        "errors": sweep["errors"],
    }



def is_utc_day_start(value: float) -> bool:
    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    return dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0


def require_segment_text(segment: dict[str, Any], field: str) -> str:
    value = segment.get(field)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"raw segment {field} missing or invalid: {segment.get('path')}")
    return value


def require_segment_number(segment: dict[str, Any], field: str) -> float:
    value = segment.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"raw segment {field} missing or invalid: {segment.get('path')}")
    return float(value)


def require_segment_int(segment: dict[str, Any], field: str) -> int:
    value = segment.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestError(f"raw segment {field} missing or invalid: {segment.get('path')}")
    return int(value)


def validate_retention_segment_days(segment: dict[str, Any]) -> list[list[int]]:
    rows = int(segment["rows"])
    unverifiable_rows = int(segment["undated_rows"]) + int(segment["corrupt_rows"]) + int(segment["unknown_rows"])
    dated_rows = rows - unverifiable_rows
    days = segment.get("days")
    if dated_rows == 0:
        if days not in (None, []):
            raise ManifestError(f"raw segment day histogram invalid for undated segment: {segment.get('path')}")
        return []
    if not isinstance(days, list):
        raise ManifestError(f"raw segment day histogram missing or invalid: {segment.get('path')}")
    validated: list[list[int]] = []
    seen_days: set[int] = set()
    total_rows = 0
    for item in days:
        if not isinstance(item, list) or len(item) != 3:
            raise ManifestError(f"raw segment day histogram entry invalid: {segment.get('path')}")
        day, count, byte_count = item
        if (
            isinstance(day, bool) or not isinstance(day, int) or not is_utc_day_start(float(day))
            or isinstance(count, bool) or not isinstance(count, int) or count <= 0
            or isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0
        ):
            raise ManifestError(f"raw segment day histogram entry invalid: {segment.get('path')}")
        if day in seen_days:
            raise ManifestError(f"raw segment day histogram duplicate day: {segment.get('path')}")
        seen_days.add(day)
        total_rows += count
        validated.append([day, count, byte_count])
    if total_rows != dated_rows:
        raise ManifestError(f"raw segment day histogram row coverage mismatch: {segment.get('path')}")
    min_day = int(datetime.fromtimestamp(float(segment["min_time_unix"]), tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    max_day = int(datetime.fromtimestamp(float(segment["max_time_unix"]), tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    if not validated or validated[0][0] != min_day or validated[-1][0] != max_day:
        raise ManifestError(f"raw segment day histogram bounds mismatch: {segment.get('path')}")
    return validated


def validate_retention_segment_metadata(segment: dict[str, Any]) -> None:
    require_segment_text(segment, "id")
    require_segment_text(segment, "kind")
    require_segment_text(segment, "path")
    require_segment_text(segment, "format")
    require_segment_text(segment, "source_name")
    require_segment_text(segment, "sha256")
    if require_segment_text(segment, "status") != "closed":
        raise ManifestError(f"raw segment status must be closed: {segment.get('path')}")
    if segment.get("format") not in {"jsonl", "jsonl.gz"}:
        raise ManifestError(f"raw segment format invalid: {segment.get('path')}")
    rows = require_segment_int(segment, "rows")
    undated_rows = require_segment_int(segment, "undated_rows")
    corrupt_rows = require_segment_int(segment, "corrupt_rows")
    unknown_rows = require_segment_int(segment, "unknown_rows")
    for field in ("bytes", "uncompressed_bytes"):
        require_segment_int(segment, field)
    min_time = segment.get("min_time_unix")
    max_time = segment.get("max_time_unix")
    if min_time is None or max_time is None:
        if min_time is not None or max_time is not None or rows != undated_rows + corrupt_rows + unknown_rows:
            raise ManifestError(f"raw segment time bounds missing or invalid: {segment.get('path')}")
    else:
        min_value = require_segment_number(segment, "min_time_unix")
        max_value = require_segment_number(segment, "max_time_unix")
        if min_value > max_value:
            raise ManifestError(f"raw segment time bounds inverted: {segment.get('path')}")


def validate_retention_manifest_segments(
    segments: Any,
    *,
    base: pathlib.Path | None = None,
    validate_paths: bool = False,
    path_must_exist: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(segments, list):
        raise ManifestError("raw segment manifest segments must be a list")
    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise ManifestError("raw segment manifest entries must be objects")
        validate_retention_segment_metadata(segment)
        segment_id = str(segment.get("id") or "")
        if segment_id in seen_ids:
            raise ManifestError(f"raw segment id duplicated: {segment_id}")
        seen_ids.add(segment_id)
        if validate_paths:
            if base is None:
                raise ManifestError("raw segment path validation requires service base")
            path = validate_segment_path(base, segment, path_must_exist=path_must_exist)
            resolved = str(path.resolve(strict=False))
            if resolved in seen_paths:
                raise ManifestError(f"raw segment path duplicated: {path}")
            seen_paths.add(resolved)
        validated.append(segment)
    return validated


def manifest_signature(base: pathlib.Path) -> tuple[tuple[str, int, int], ...]:
    items: list[tuple[str, int, int]] = []
    for path in (manifest_path(base), current_pointer_path(base), pending_rotation_path(base), segment_apply_marker_path(base)):
        try:
            stat = path.stat()
            items.append((str(path), stat.st_size if path.is_file() else 0, stat.st_mtime_ns))
        except OSError:
            items.append((str(path), 0, 0))
    manifest = strict_read_manifest(base)
    for segment in manifest.get("segments", []):
        if not isinstance(segment, dict):
            continue
        path = pathlib.Path(str(segment.get("path") or ""))
        try:
            stat = path.stat()
            items.append((str(path), stat.st_size if path.is_file() else 0, stat.st_mtime_ns))
        except OSError:
            items.append((str(path), 0, 0))
    pointer = strict_read_current_pointer(base)
    for current in pointer.get("current", {}).values():
        if not isinstance(current, dict):
            continue
        path = pathlib.Path(str(current.get("path") or ""))
        try:
            stat = path.stat()
            items.append((str(path), stat.st_size if path.is_file() else 0, stat.st_mtime_ns))
        except OSError:
            items.append((str(path), 0, 0))
    return tuple(sorted(items))


def expected_segment_id_prefix(kind: str) -> str:
    return source_name_for_kind(kind) + ".current."


def validate_current_segment_entry(base: pathlib.Path, segment: dict[str, Any], *, kind: str, path_must_exist: bool = True) -> dict[str, Any]:
    if not isinstance(segment, dict):
        raise ManifestError("current segment entry must be an object")
    if segment.get("kind") != kind:
        raise ManifestError(f"current segment kind mismatch: {segment.get('kind')}")
    source_name = source_name_for_kind(kind)
    if segment.get("source_name") != source_name:
        raise ManifestError(f"current segment source mismatch: {segment.get('source_name')}")
    segment_id = str(segment.get("id") or "")
    if not segment_id.startswith(expected_segment_id_prefix(kind)):
        raise ManifestError(f"current segment id mismatch: {segment_id}")
    base_path = pathlib.Path(base).expanduser()
    raw_current_path = base_path / "raw" / "current"
    if (base_path / "raw").is_symlink() or raw_current_path.is_symlink():
        raise ManifestError(f"raw/current must not be a symlink: {raw_current_path}")
    path = pathlib.Path(str(segment.get("path") or "")).expanduser()
    if path.parent.is_symlink() or path.is_symlink():
        raise ManifestError(f"current segment path must not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=path_must_exist)
    except FileNotFoundError as exc:
        raise ManifestError(f"current segment path missing: {path}") from exc
    raw_current = raw_current_path.resolve(strict=False)
    if raw_current not in resolved.parents:
        raise ManifestError(f"current segment path outside raw/current: {path}")
    if path.name != f"{segment_id}.jsonl":
        raise ManifestError(f"current segment filename mismatch: {path}")
    if path_must_exist:
        if not path.is_file():
            raise ManifestError(f"current segment path must be a regular file: {path}")
        if not os.access(path, os.R_OK | os.W_OK):
            raise ManifestError(f"current segment path must be readable and writable: {path}")
    result = dict(segment)
    result["path"] = str(path)
    result["kind"] = kind
    return result
