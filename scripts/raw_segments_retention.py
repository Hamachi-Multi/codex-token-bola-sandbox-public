"""Raw segment retention planning, apply, and preview helpers."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import pathlib
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from raw_segments_common import (
    JsonlScanAccumulator,
    ManifestError,
    PROMPT_TIME_BASIS,
    acquire_raw_segment_manifest_lock,
    open_segment_payload,
    read_segment_payload,
    row_time,
    scan_jsonl_bytes,
)
from raw_segments_state import (
    compact_timestamp,
    format_for_path,
    is_utc_day_start,
    manifest_segments_match,
    read_apply_marker,
    retained_staging_path,
    source_name_for_kind,
    strict_read_current_pointer,
    strict_read_manifest,
    sweep_apply_marker,
    validate_apply_marker_delta,
    validate_current_segment_entry,
    validate_retention_manifest_segments,
    validate_retention_segment_days,
    validate_retention_segment_metadata,
    validate_retained_staging_path,
    validate_segment_path,
    verify_retained_segments_for_marker,
    write_apply_marker,
    write_manifest,
)


class SegmentApplyState(str, Enum):
    NOT_STARTED = "not_started"
    RECOVERY_PENDING = "recovery_pending"
    LOGICAL_DELETE_COMMITTED = "logical_delete_committed"
    UNKNOWN = "unknown"


def inspect_segment_apply_state(base: pathlib.Path, segment_plan: dict[str, Any]) -> SegmentApplyState:
    previous_manifest = segment_plan.get("previous_manifest")
    next_manifest = segment_plan.get("next_manifest")
    if not isinstance(previous_manifest, dict) or not isinstance(next_manifest, dict):
        return SegmentApplyState.UNKNOWN
    try:
        current_manifest = strict_read_manifest(base)
        marker = read_apply_marker(base)
    except (OSError, ManifestError):
        return SegmentApplyState.UNKNOWN
    if marker is not None:
        marker_previous = marker.get("previous_manifest")
        marker_next = marker.get("next_manifest")
        if (
            not isinstance(marker_previous, dict)
            or not isinstance(marker_next, dict)
            or not manifest_segments_match(marker_previous, previous_manifest)
            or not manifest_segments_match(marker_next, next_manifest)
            or marker.get("source_segments") != segment_plan.get("source_segments")
            or marker.get("retained_segments") != segment_plan.get("retained_segments")
        ):
            return SegmentApplyState.UNKNOWN
        if not (
            manifest_segments_match(current_manifest, previous_manifest)
            or manifest_segments_match(current_manifest, next_manifest)
        ):
            return SegmentApplyState.UNKNOWN
        return SegmentApplyState.RECOVERY_PENDING
    if manifest_segments_match(current_manifest, previous_manifest):
        return SegmentApplyState.NOT_STARTED
    if manifest_segments_match(current_manifest, next_manifest):
        return SegmentApplyState.LOGICAL_DELETE_COMMITTED
    return SegmentApplyState.UNKNOWN

def classify_segment_for_cutoff(segment: dict[str, Any], cutoff_unix: float) -> str:
    validate_retention_segment_metadata(segment)
    if segment.get("time_basis") != PROMPT_TIME_BASIS:
        return "rewrite_mixed"
    min_time = segment.get("min_time_unix")
    max_time = segment.get("max_time_unix")
    undated_rows = int(segment["undated_rows"])
    corrupt_rows = int(segment["corrupt_rows"])
    unknown_rows = int(segment["unknown_rows"])
    unverifiable_rows = undated_rows + corrupt_rows + unknown_rows
    if min_time is None and max_time is None:
        return "rewrite_mixed" if unverifiable_rows > 0 else "retain"
    if unverifiable_rows > 0 and float(max_time) < float(cutoff_unix):
        return "rewrite_mixed"
    if float(max_time) < float(cutoff_unix):
        return "delete_whole"
    if float(min_time) >= float(cutoff_unix):
        return "retain"
    return "rewrite_mixed"



def validate_segment_plan_ids(plans: list[dict[str, Any]]) -> None:
    seen_sources: set[str] = set()
    seen_retained: set[str] = set()
    for plan in plans:
        source = plan.get("source_segment")
        if not isinstance(source, dict):
            raise ManifestError("raw segment plan missing source segment")
        source_id = str(source.get("id") or "")
        if not source_id or source_id in seen_sources:
            raise ManifestError(f"raw segment plan source id duplicated: {source_id}")
        seen_sources.add(source_id)
        retained = plan.get("retained_segment")
        if retained is None:
            continue
        if not isinstance(retained, dict):
            raise ManifestError("raw segment plan retained segment invalid")
        retained_id = str(retained.get("id") or "")
        if not retained_id or retained_id in seen_retained or retained_id in seen_sources:
            raise ManifestError(f"raw segment plan retained id duplicated: {retained_id}")
        seen_retained.add(retained_id)


def manifest_tracked_paths(base: pathlib.Path, manifest: dict[str, Any] | None = None) -> set[str]:
    manifest_data = strict_read_manifest(base) if manifest is None else manifest
    segments = validate_retention_manifest_segments(manifest_data.get("segments", []), base=base, validate_paths=True)
    return {str(validate_segment_path(base, segment).resolve(strict=False)) for segment in segments}



def verify_segment_payload(base: pathlib.Path, segment: dict[str, Any]) -> tuple[pathlib.Path, bytes, dict[str, Any]]:
    path = validate_segment_path(base, segment)
    try:
        payload = read_segment_payload(path)
    except OSError as exc:
        raise ManifestError(f"cannot read raw segment payload: {path}") from exc
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != segment.get("sha256"):
        raise ManifestError(f"raw segment checksum mismatch: {path}")
    scan = scan_jsonl_bytes(payload, kind=str(segment.get("kind") or ""))
    for key in ("rows", "undated_rows", "corrupt_rows", "unknown_rows"):
        if int(scan[key]) != int(segment.get(key) or 0):
            raise ManifestError(f"raw segment {key} mismatch: {path}")
    if segment.get("time_basis") != PROMPT_TIME_BASIS:
        return path, payload, scan
    for key in ("min_time_unix", "max_time_unix"):
        expected = segment.get(key)
        actual = scan.get(key)
        if expected is None or actual is None:
            if expected is not None or actual is not None:
                raise ManifestError(f"raw segment {key} mismatch: {path}")
        elif abs(float(expected) - float(actual)) > 0.001:
            raise ManifestError(f"raw segment {key} mismatch: {path}")
    return path, payload, scan


def segment_source_signature(path: pathlib.Path) -> dict[str, int]:
    stat_result = path.stat()
    return {
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def verify_segment_source_signature(path: pathlib.Path, signature: dict[str, Any]) -> None:
    if segment_source_signature(path) != {key: int(signature.get(key) or 0) for key in ("device", "inode", "size", "mtime_ns")}:
        raise ManifestError(f"raw segment changed after retention planning: {path}")


def sha256_path(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_segment_scan(path: pathlib.Path, segment: dict[str, Any], scan: dict[str, Any], payload_sha256: str) -> None:
    if payload_sha256 != segment.get("sha256"):
        raise ManifestError(f"raw segment checksum mismatch: {path}")
    for key in ("rows", "undated_rows", "corrupt_rows", "unknown_rows"):
        if int(scan[key]) != int(segment.get(key) or 0):
            raise ManifestError(f"raw segment {key} mismatch: {path}")
    if segment.get("time_basis") != PROMPT_TIME_BASIS:
        return
    for key in ("min_time_unix", "max_time_unix"):
        expected = segment.get(key)
        actual = scan.get(key)
        if expected is None or actual is None:
            if expected is not None or actual is not None:
                raise ManifestError(f"raw segment {key} mismatch: {path}")
        elif abs(float(expected) - float(actual)) > 0.001:
            raise ManifestError(f"raw segment {key} mismatch: {path}")


def planned_turn_from_row(row: dict[str, Any], row_time_value: float | None) -> dict[str, Any] | None:
    session_id = str(row.get("session_id") or "")
    turn_id = str(row.get("turn_id") or "")
    if not session_id or not turn_id:
        return None
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "captured_at": row.get("captured_at"),
        "started_at": row.get("started_at"),
        "stopped_at": row.get("stopped_at"),
        "captured_at_unix": row_time_value,
    }


def retained_segment_path(base: pathlib.Path, segment: dict[str, Any], scan: dict[str, Any], *, create_archive_dir: bool = True) -> pathlib.Path:
    archive_dir = pathlib.Path(base).expanduser() / "raw" / "archive"
    if create_archive_dir:
        archive_dir.mkdir(parents=True, exist_ok=True)
    source_name = source_name_for_kind(str(segment.get("kind") or ""))
    min_label = compact_timestamp(scan.get("min_time_unix"))
    max_label = compact_timestamp(scan.get("max_time_unix"))
    digest = hashlib.sha256((str(segment.get("id") or "") + str(time.time_ns())).encode("utf-8")).hexdigest()[:12]
    return archive_dir / f"{source_name}.{min_label}.{max_label}.{digest}.retained.jsonl.gz"


def segment_from_payload(path: pathlib.Path, *, kind: str, payload: bytes, segment_id: str | None = None) -> dict[str, Any]:
    scan = scan_jsonl_bytes(payload, kind=kind)
    return {
        "id": segment_id or path.name.removesuffix(".jsonl.gz").removesuffix(".jsonl"),
        "kind": kind,
        "path": str(path),
        "format": format_for_path(path),
        "source_name": source_name_for_kind(kind),
        "time_basis": PROMPT_TIME_BASIS,
        "min_time_unix": scan["min_time_unix"],
        "max_time_unix": scan["max_time_unix"],
        "rows": scan["rows"],
        "undated_rows": scan["undated_rows"],
        "corrupt_rows": scan["corrupt_rows"],
        "unknown_rows": scan["unknown_rows"],
        "days": scan["days"],
        "bytes": path.stat().st_size if path.exists() else len(payload),
        "uncompressed_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "status": "closed",
    }


def retained_payload_for_cutoff(payload: bytes, *, kind: str, cutoff_unix: float) -> bytes:
    retained_lines: list[bytes] = []
    for raw_line in payload.splitlines():
        line = raw_line + b"\n"
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            retained_lines.append(line)
            continue
        if not isinstance(parsed, dict):
            retained_lines.append(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            continue
        parsed_time = row_time(parsed, kind=kind)
        if parsed_time is not None and float(parsed_time) < float(cutoff_unix):
            continue
        retained_lines.append(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    return b"".join(retained_lines)


def segment_from_scan(
    path: pathlib.Path,
    *,
    kind: str,
    scan: dict[str, Any],
    uncompressed_bytes: int,
    payload_sha256: str,
) -> dict[str, Any]:
    return {
        "id": path.name.removesuffix(".jsonl.gz").removesuffix(".jsonl"),
        "kind": kind,
        "path": str(path),
        "format": format_for_path(path),
        "source_name": source_name_for_kind(kind),
        "time_basis": PROMPT_TIME_BASIS,
        "min_time_unix": scan["min_time_unix"],
        "max_time_unix": scan["max_time_unix"],
        "rows": scan["rows"],
        "undated_rows": scan["undated_rows"],
        "corrupt_rows": scan["corrupt_rows"],
        "unknown_rows": scan["unknown_rows"],
        "days": scan["days"],
        "bytes": 0,
        "uncompressed_bytes": uncompressed_bytes,
        "sha256": payload_sha256,
        "status": "closed",
    }


def plan_segment_for_cutoff(
    base: pathlib.Path,
    segment: dict[str, Any],
    cutoff_unix: float,
    *,
    create_output_dirs: bool = True,
    pruned_turn_sink: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    validate_retention_segment_metadata(segment)
    source_path = validate_segment_path(base, segment)
    source_signature = segment_source_signature(source_path)
    kind = str(segment.get("kind") or "")
    archive_dir = pathlib.Path(base).expanduser() / "raw" / "archive"
    if create_output_dirs:
        archive_dir.mkdir(parents=True, exist_ok=True)
    temporary = archive_dir / f".retention-plan.{os.getpid()}.{time.time_ns()}.jsonl.gz.tmp"
    source_digest = hashlib.sha256()
    retained_digest = hashlib.sha256()
    source_scan = JsonlScanAccumulator(kind=kind)
    retained_scan = JsonlScanAccumulator(kind=kind)
    retained_line_count = 0
    deleted_turns: list[dict[str, Any]] = []
    deleted_rows = 0
    deleted_bytes = 0
    scanned_rows = 0
    retained_uncompressed_bytes = 0
    raw_target = None
    gzip_target = None
    try:
        if create_output_dirs:
            raw_target = temporary.open("xb")
            gzip_target = gzip.GzipFile(fileobj=raw_target, mode="wb")
        with open_segment_payload(source_path) as source:
            for line in source:
                source_digest.update(line)
                source_scan.add(line)
                raw_line = line.rstrip(b"\r\n")
                try:
                    parsed = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    output_line = raw_line + b"\n"
                    retained_line_count += 1
                else:
                    if not isinstance(parsed, dict):
                        output_line = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                        retained_line_count += 1
                    else:
                        scanned_rows += 1
                        parsed_time = row_time(parsed, kind=kind)
                        if parsed_time is not None and float(parsed_time) < float(cutoff_unix):
                            deleted_rows += 1
                            deleted_bytes += len(raw_line) + 1
                            pruned = planned_turn_from_row(parsed, parsed_time) if kind == "prompt_usage" else None
                            if pruned is not None:
                                if pruned_turn_sink is None:
                                    deleted_turns.append(pruned)
                                else:
                                    pruned_turn_sink(pruned)
                            continue
                        output_line = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                        retained_line_count += 1
                retained_digest.update(output_line)
                retained_scan.add(output_line)
                retained_uncompressed_bytes += len(output_line)
                if gzip_target is not None:
                    gzip_target.write(output_line)
        if gzip_target is not None:
            gzip_target.close()
            gzip_target = None
        if raw_target is not None:
            raw_target.flush()
            os.fsync(raw_target.fileno())
            raw_target.close()
            raw_target = None
    except Exception:
        if gzip_target is not None:
            gzip_target.close()
        if raw_target is not None:
            raw_target.close()
        temporary.unlink(missing_ok=True)
        raise
    scan = source_scan.result()
    validate_segment_scan(source_path, segment, scan, source_digest.hexdigest())
    verify_segment_source_signature(source_path, source_signature)
    if deleted_rows <= 0:
        temporary.unlink(missing_ok=True)
        return None
    source_segment = dict(segment)
    source_size = int(segment.get("bytes") or source_path.stat().st_size)
    if retained_line_count <= 0:
        temporary.unlink(missing_ok=True)
        return {
            "action": "delete_whole",
            "source_segment": source_segment,
            "source_path": str(source_path),
            "deleted_rows": deleted_rows,
            "scanned_rows": max(scanned_rows, int(scan["rows"])),
            "kept_rows": 0,
            "deleted_turns": deleted_turns,
            "source_bytes": source_size,
            "deleted_bytes": source_size,
            "_source_signature": source_signature,
        }
    retained_scan_result = retained_scan.result()
    retained_path = retained_segment_path(base, segment, retained_scan_result, create_archive_dir=create_output_dirs)
    retained_segment = segment_from_scan(
        retained_path,
        kind=kind,
        scan=retained_scan_result,
        uncompressed_bytes=retained_uncompressed_bytes,
        payload_sha256=retained_digest.hexdigest(),
    )
    staged_path = retained_staging_path(retained_path)
    if create_output_dirs:
        temporary.replace(staged_path)
        staged_path.chmod(0o600)
        retained_segment["bytes"] = staged_path.stat().st_size
    return {
        "action": "rewrite_mixed",
        "source_segment": source_segment,
        "source_path": str(source_path),
        "retained_segment": retained_segment,
        "_cutoff_unix": float(cutoff_unix),
        "deleted_rows": deleted_rows,
        "scanned_rows": max(scanned_rows, int(scan["rows"])),
        "kept_rows": int(retained_segment["rows"]),
        "deleted_turns": deleted_turns,
        "source_bytes": source_size,
        "deleted_bytes": min(source_size, deleted_bytes),
        "_source_signature": source_signature,
        "_staged_retained_path": str(staged_path) if create_output_dirs else None,
        "_staged_retained_sha256": sha256_path(staged_path) if create_output_dirs else None,
    }


def plan_segments_older_than(
    base: pathlib.Path,
    cutoff_unix: float,
    *,
    create_output_dirs: bool = True,
    pruned_turn_sink: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    manifest = strict_read_manifest(base)
    segments = validate_retention_manifest_segments(manifest.get("segments", []), base=base, validate_paths=True)
    plans: list[dict[str, Any]] = []
    for segment in segments:
        plan = plan_segment_for_cutoff(
            base,
            segment,
            cutoff_unix,
            create_output_dirs=create_output_dirs,
            pruned_turn_sink=pruned_turn_sink,
        )
        if plan is not None:
            plans.append(plan)
    validate_segment_plan_ids(plans)
    source_ids = {str(plan["source_segment"]["id"]) for plan in plans}
    retained_segments = [plan["retained_segment"] for plan in plans if isinstance(plan.get("retained_segment"), dict)]
    next_segments = [
        segment
        for segment in manifest.get("segments", [])
        if isinstance(segment, dict) and str(segment.get("id") or "") not in source_ids
    ]
    next_segments.extend(retained_segments)
    next_manifest = dict(manifest)
    next_manifest["segments"] = sorted(next_segments, key=lambda item: (str(item.get("kind") or ""), str(item.get("path") or "")))
    return {
        "cutoff_unix": float(cutoff_unix),
        "plans": plans,
        "previous_manifest": manifest,
        "next_manifest": next_manifest,
        "source_segments": [plan["source_segment"] for plan in plans],
        "retained_segments": retained_segments,
        "deleted_turns": [turn for plan in plans for turn in plan.get("deleted_turns", []) if isinstance(turn, dict)],
        "deleted_rows": sum(int(plan.get("deleted_rows") or 0) for plan in plans),
        "scanned_rows": sum(int(plan.get("scanned_rows") or 0) for plan in plans),
        "deleted_bytes": sum(int(plan.get("deleted_bytes") or 0) for plan in plans),
    }


def preflight_segments_older_than(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any]:
    manifest = strict_read_manifest(base)
    segments = validate_retention_manifest_segments(manifest.get("segments", []), base=base, validate_paths=True)
    for segment in segments:
        validate_retention_segment_metadata(segment)
    return {"cutoff_unix": float(cutoff_unix), "segments": len(segments)}


def validate_segment_plans(base: pathlib.Path, segment_plan: dict[str, Any]) -> dict[str, Any]:
    plans = segment_plan.get("plans") or []
    if not isinstance(plans, list):
        raise ManifestError("raw segment apply plan must contain a plan list")
    if not plans:
        return {"plans": 0}
    previous_manifest = segment_plan["previous_manifest"]
    next_manifest = segment_plan["next_manifest"]
    source_segments = validate_retention_manifest_segments(segment_plan["source_segments"], base=base, validate_paths=True)
    retained_segments = validate_retention_manifest_segments(segment_plan["retained_segments"], base=base, validate_paths=True, path_must_exist=False)
    validate_apply_marker_delta(base, previous_manifest, source_segments, retained_segments, next_manifest, source_must_exist=True)
    validate_segment_plan_ids(plans)
    for plan in plans:
        source_segment = plan.get("source_segment")
        if not isinstance(source_segment, dict):
            raise ManifestError("raw segment plan missing source segment")
        source_path = validate_segment_path(base, source_segment)
        signature = plan.get("_source_signature")
        if not isinstance(signature, dict):
            raise ManifestError("raw segment plan missing source signature")
        verify_segment_source_signature(source_path, signature)
        retained_segment = plan.get("retained_segment")
        if isinstance(retained_segment, dict):
            stage_path = pathlib.Path(str(plan.get("_staged_retained_path") or ""))
            validate_retained_staging_path(base, stage_path)
            if not stage_path.is_file():
                raise ManifestError(f"raw segment retained stage missing: {stage_path}")
            if sha256_path(stage_path) != str(plan.get("_staged_retained_sha256") or ""):
                raise ManifestError(f"raw segment retained stage checksum mismatch: {stage_path}")
    return {"plans": len(plans)}


def discard_segment_plan_artifacts(segment_plan: dict[str, Any] | None) -> None:
    if not isinstance(segment_plan, dict):
        return
    for plan in segment_plan.get("plans") or []:
        if not isinstance(plan, dict):
            continue
        path_text = plan.get("_staged_retained_path")
        if path_text:
            pathlib.Path(str(path_text)).unlink(missing_ok=True)


def apply_segment_plans(base: pathlib.Path, segment_plan: dict[str, Any]) -> dict[str, Any]:
    plans = segment_plan.get("plans") or []
    if not isinstance(plans, list):
        raise ManifestError("raw segment apply plan must contain a plan list")
    if not plans:
        return {"deleted_files": 0, "rewritten_files": 0, "changed_files": 0}
    previous_manifest = segment_plan["previous_manifest"]
    next_manifest = segment_plan["next_manifest"]
    source_segments = validate_retention_manifest_segments(segment_plan["source_segments"], base=base, validate_paths=True)
    retained_segments = validate_retention_manifest_segments(segment_plan["retained_segments"], base=base, validate_paths=True, path_must_exist=False)
    validate_apply_marker_delta(base, previous_manifest, source_segments, retained_segments, next_manifest, source_must_exist=True)
    staged_retained: list[tuple[str, pathlib.Path, pathlib.Path]] = []
    for plan in plans:
        source_segment = plan.get("source_segment")
        if not isinstance(source_segment, dict):
            raise ManifestError("raw segment rewrite plan missing source segment")
        source_path = validate_segment_path(base, source_segment)
        signature = plan.get("_source_signature")
        if not isinstance(signature, dict):
            raise ManifestError("raw segment plan missing source signature")
        verify_segment_source_signature(source_path, signature)
        retained_segment = plan.get("retained_segment")
        if not isinstance(retained_segment, dict):
            continue
        retained_path = validate_segment_path(base, retained_segment, path_must_exist=False)
        retained_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path = pathlib.Path(str(plan.get("_staged_retained_path") or ""))
        validate_retained_staging_path(base, stage_path)
        if not stage_path.is_file():
            raise ManifestError(f"raw segment retained stage missing: {stage_path}")
        if sha256_path(stage_path) != str(plan.get("_staged_retained_sha256") or ""):
            raise ManifestError(f"raw segment retained stage checksum mismatch: {stage_path}")
        staged_retained.append((str(retained_segment["id"]), stage_path, retained_path))
    marker = {
        "phase": "manifest_pending",
        "previous_manifest": previous_manifest,
        "source_segments": source_segments,
        "retained_segments": retained_segments,
        "next_manifest": next_manifest,
        "retained_staging_paths": [{"id": segment_id, "path": str(stage_path)} for segment_id, stage_path, _retained_path in staged_retained],
    }
    with acquire_raw_segment_manifest_lock(base):
        current_manifest = strict_read_manifest(base)
        if not manifest_segments_match(current_manifest, previous_manifest):
            for _segment_id, stage_path, _retained_path in staged_retained:
                with contextlib.suppress(FileNotFoundError):
                    stage_path.unlink()
            raise ManifestError("raw segment manifest changed before applying retention segment plan")
        try:
            write_apply_marker(base, marker)
        except Exception:
            for _segment_id, stage_path, _retained_path in staged_retained:
                with contextlib.suppress(FileNotFoundError):
                    stage_path.unlink()
            raise
        for _segment_id, stage_path, retained_path in staged_retained:
            stage_path.replace(retained_path)
            retained_path.chmod(0o600)
        verify_retained_segments_for_marker(base, retained_segments)
        write_manifest(base, next_manifest)
        marker["phase"] = "unlink_pending"
        marker["retained_staging_paths"] = []
        write_apply_marker(base, marker)
    sweep = sweep_apply_marker(base)
    deleted_files = int(sweep.get("deleted_files") or 0)
    rewritten_files = sum(1 for plan in plans if plan.get("action") == "rewrite_mixed")
    return {
        "deleted_files": deleted_files,
        "pending_files": int(sweep.get("pending_files") or 0),
        "physical_delete_pending": bool(sweep.get("pending_files")),
        "unlink_errors": sweep.get("errors") or [],
        "rewritten_files": rewritten_files,
        "changed_files": deleted_files + rewritten_files,
        "deleted_bytes": sum(int(plan.get("deleted_bytes") or 0) for plan in plans),
    }


def scan_segment_file_for_cutoff(path: pathlib.Path, *, kind: str, cutoff_unix: float) -> dict[str, int]:
    payload = read_segment_payload(path)
    scanned_rows = 0
    deletable_rows = 0
    deletable_bytes = 0
    for line in payload.splitlines():
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            scanned_rows += 1
            continue
        scanned_rows += 1
        parsed_time = row_time(parsed, kind=kind)
        if parsed_time is not None and float(parsed_time) < float(cutoff_unix):
            deletable_rows += 1
            deletable_bytes += len(line) + 1
    return {"scanned_rows": scanned_rows, "deletable_rows": deletable_rows, "deletable_bytes": deletable_bytes}


def exact_preview_for_segment(base: pathlib.Path, segment: dict[str, Any], cutoff_unix: float) -> dict[str, int]:
    path = validate_segment_path(base, segment)
    scan = scan_segment_file_for_cutoff(path, kind=str(segment.get("kind") or ""), cutoff_unix=cutoff_unix)
    return {
        "scanned_rows": int(scan["scanned_rows"]),
        "deletable_rows": int(scan["deletable_rows"]),
        "deletable_bytes": min(int(segment.get("bytes") or 0), int(scan["deletable_bytes"])),
    }


def retention_preview_from_manifest(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any] | None:
    manifest = strict_read_manifest(base)
    segments = validate_retention_manifest_segments(manifest.get("segments", []), base=base, validate_paths=True)
    tracked_paths = manifest_tracked_paths(base, manifest)
    if not segments:
        return None
    files = []
    for segment in segments:
        action = classify_segment_for_cutoff(segment, cutoff_unix)
        rows = int(segment.get("rows") or 0)
        bytes_count = int(segment.get("bytes") or 0)
        deletable_rows = rows if action == "delete_whole" else 0
        deletable_bytes = bytes_count if action == "delete_whole" else 0
        if action == "rewrite_mixed":
            if segment.get("time_basis") != PROMPT_TIME_BASIS or not is_utc_day_start(cutoff_unix):
                exact = exact_preview_for_segment(base, segment, cutoff_unix)
                rows = exact["scanned_rows"]
                deletable_rows = exact["deletable_rows"]
                deletable_bytes = exact["deletable_bytes"]
            else:
                for day, count, day_bytes in validate_retention_segment_days(segment):
                    if float(day) < float(cutoff_unix):
                        deletable_rows += int(count)
                        deletable_bytes += int(day_bytes)
        files.append({
            "path": str(segment.get("path") or ""),
            "kind": str(segment.get("kind") or ""),
            "source_size": bytes_count,
            "scanned_rows": rows,
            "deletable_rows": deletable_rows,
            "deletable_bytes": min(bytes_count, deletable_bytes),
            "affected": action in {"delete_whole", "rewrite_mixed"} and deletable_rows > 0,
            "segment_action": action,
            "segment_id": str(segment.get("id") or ""),
        })
    scanned_rows = sum(int(item["scanned_rows"]) for item in files)
    deletable_rows = sum(int(item["deletable_rows"]) for item in files)
    deletable_bytes = sum(int(item["deletable_bytes"]) for item in files)
    return {
        "cutoff_unix": float(cutoff_unix),
        "scanned_rows": scanned_rows,
        "deletable_rows": deletable_rows,
        "deletable_bytes": deletable_bytes,
        "kept_rows": scanned_rows - deletable_rows,
        "affected_files": sum(1 for item in files if item["affected"]),
        "mixed_files": sum(1 for item in files if item["segment_action"] == "rewrite_mixed"),
        "files": files,
        "from_manifest": True,
        "_tracked_paths": sorted(tracked_paths),
    }


def retention_preview_from_current(base: pathlib.Path, cutoff_unix: float) -> dict[str, Any] | None:
    pointer = strict_read_current_pointer(base)
    current_entries = []
    for kind, current in sorted(pointer.get("current", {}).items()):
        if not isinstance(current, dict):
            raise ManifestError(f"current segment entry must be an object: {kind}")
        current_entries.append((str(kind), validate_current_segment_entry(base, current, kind=str(kind))))
    if not current_entries:
        return None
    files = []
    tracked_paths = set()
    for kind, segment in current_entries:
        path = pathlib.Path(str(segment["path"]))
        stat = path.stat()
        scan = scan_segment_file_for_cutoff(path, kind=kind, cutoff_unix=cutoff_unix)
        tracked_paths.add(str(path.expanduser().resolve(strict=True)))
        files.append({
            "path": str(path),
            "kind": kind,
            "source_size": stat.st_size,
            "scanned_rows": int(scan["scanned_rows"]),
            "deletable_rows": int(scan["deletable_rows"]),
            "deletable_bytes": min(stat.st_size, int(scan["deletable_bytes"])),
            "affected": int(scan["deletable_rows"]) > 0,
            "segment_action": "current_scan",
            "segment_id": str(segment.get("id") or ""),
        })
    scanned_rows = sum(int(item["scanned_rows"]) for item in files)
    deletable_rows = sum(int(item["deletable_rows"]) for item in files)
    deletable_bytes = sum(int(item["deletable_bytes"]) for item in files)
    return {
        "cutoff_unix": float(cutoff_unix),
        "scanned_rows": scanned_rows,
        "deletable_rows": deletable_rows,
        "deletable_bytes": deletable_bytes,
        "kept_rows": scanned_rows - deletable_rows,
        "affected_files": sum(1 for item in files if item["affected"]),
        "files": files,
        "from_current": True,
        "_tracked_paths": sorted(tracked_paths),
    }
