"""Dashboard freshness metadata derived from raw log offsets."""

from __future__ import annotations

import gzip
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import transcript_parser

NORMALIZE_LOGIC_VERSION = 5
RECOVERY_RECORD_TYPES = {"turn_start", "turn_stop_missing_start"}
TURN_START_RECOVERY_AGE_SECONDS = 60
TERMINAL_TURN_EVENT_TYPES = {"task_complete", "task_aborted", "turn_aborted"}


def _json_state(path: pathlib.Path, label: str) -> tuple[dict[str, Any], dict[str, str] | None]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {"code": f"{label}_missing", "path": str(path)}
    except json.JSONDecodeError:
        return {}, {"code": f"{label}_invalid_json", "path": str(path)}
    except OSError:
        return {}, {"code": f"{label}_read_error", "path": str(path)}
    if not isinstance(parsed, dict):
        return {}, {"code": f"{label}_wrong_type", "path": str(path)}
    return parsed, None


def _append_warning(warnings: list[dict[str, str]], warning: dict[str, str] | None) -> None:
    if warning is not None:
        warnings.append(warning)


def _expected_base(base: pathlib.Path) -> str:
    return str(pathlib.Path(base).expanduser().resolve())


def _path_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(root.expanduser().resolve(strict=False))
    except ValueError:
        return False
    return True


def _validate_current_pointer_header(base: pathlib.Path, pointer: dict[str, Any], pointer_path: pathlib.Path) -> dict[str, str] | None:
    if pointer.get("schema_version", 1) != 1:
        return {"code": "current_pointer_schema_mismatch", "path": str(pointer_path)}
    if pointer.get("base", _expected_base(base)) != _expected_base(base):
        return {"code": "current_pointer_base_mismatch", "path": str(pointer_path)}
    return None


def _validate_current_segment_path(base: pathlib.Path, path: pathlib.Path, pointer_path: pathlib.Path) -> dict[str, str] | None:
    if not _path_under(path, pathlib.Path(base).expanduser() / "raw" / "current"):
        return {"code": "current_pointer_segment_outside_current", "path": str(pointer_path)}
    if not path.is_file():
        return {"code": "current_pointer_segment_missing", "path": str(pointer_path)}
    return None


def _validate_manifest_header(base: pathlib.Path, manifest: dict[str, Any], manifest_path: pathlib.Path) -> dict[str, str] | None:
    if manifest.get("schema_version") != 1:
        return {"code": "raw_manifest_schema_mismatch", "path": str(manifest_path)}
    if manifest.get("base") != _expected_base(base):
        return {"code": "raw_manifest_base_mismatch", "path": str(manifest_path)}
    return None


def _validate_manifest_segment(base: pathlib.Path, segment: dict[str, Any], manifest_path: pathlib.Path) -> dict[str, str] | None:
    if not str(segment.get("id") or "") or not str(segment.get("path") or ""):
        return {"code": "raw_manifest_segment_invalid", "path": str(manifest_path)}
    if segment.get("format") not in {"jsonl", "jsonl.gz"}:
        return {"code": "raw_manifest_segment_invalid", "path": str(manifest_path)}
    path = pathlib.Path(str(segment.get("path"))).expanduser()
    roots = (pathlib.Path(base).expanduser() / "raw" / "current", pathlib.Path(base).expanduser() / "raw" / "archive")
    if not any(_path_under(path, root) for root in roots):
        return {"code": "raw_manifest_segment_outside_raw", "path": str(manifest_path)}
    return None


def _file_size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime_unix(path: pathlib.Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _iso_time(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _safe_offset(value: Any, size: int) -> int:
    try:
        offset = int(value)
    except (TypeError, ValueError):
        offset = 0
    return max(0, min(offset, size))


def _count_jsonl_rows_after(path: pathlib.Path, offset: int) -> int:
    if path.suffix == ".gz":
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def _closed_manifest_segments(base: pathlib.Path, manifest: dict[str, Any], warnings: list[dict[str, str]], manifest_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    segments = manifest.get("segments", [])
    if not isinstance(segments, list):
        warnings.append({"code": "raw_manifest_segments_wrong_type", "path": str(manifest_path)})
        return rows
    for segment in segments:
        if not isinstance(segment, dict):
            warnings.append({"code": "raw_manifest_segment_invalid", "path": str(manifest_path)})
            continue
        if segment.get("kind") != "prompt_usage" or segment.get("status", "closed") != "closed":
            continue
        segment_warning = _validate_manifest_segment(base, segment, manifest_path)
        _append_warning(warnings, segment_warning)
        if segment_warning is not None:
            continue
        path_text = str(segment.get("path") or "")
        segment_id = str(segment.get("id") or "")
        if not path_text:
            continue
        rows[path_text] = {"id": segment_id, **segment}
    return rows


def _pointer_current_paths(base: pathlib.Path, pointer: dict[str, Any], warnings: list[dict[str, str]], pointer_path: pathlib.Path) -> tuple[set[pathlib.Path], bool]:
    paths: set[pathlib.Path] = set()
    current = pointer.get("current")
    if not isinstance(current, dict):
        warnings.append({"code": "current_pointer_current_wrong_type", "path": str(pointer_path)})
        return paths, True
    pointer_unavailable = False
    for segment in current.values():
        if not isinstance(segment, dict):
            warnings.append({"code": "current_pointer_segment_wrong_type", "path": str(pointer_path)})
            pointer_unavailable = True
            continue
        if segment.get("kind", "prompt_usage") != "prompt_usage":
            continue
        path_text = str(segment.get("path") or "")
        if not path_text:
            warnings.append({"code": "current_pointer_segment_missing_path", "path": str(pointer_path)})
            pointer_unavailable = True
            continue
        path = pathlib.Path(path_text).expanduser()
        segment_warning = _validate_current_segment_path(base, path, pointer_path)
        _append_warning(warnings, segment_warning)
        if segment_warning is not None:
            pointer_unavailable = True
            continue
        paths.add(path)
    return paths, pointer_unavailable


def _normalize_state_parts(base: pathlib.Path, state: dict[str, Any], warnings: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, Any]]:
    state_path = base / "normalized" / "normalize-state.json"
    sources = state.get("sources", {})
    processed_segments = state.get("processed_segments", {})
    stale_logic = state and state.get("logic_version") != NORMALIZE_LOGIC_VERSION
    if stale_logic:
        warnings.append({"code": "normalize_state_logic_version_mismatch", "path": str(state_path)})
    if not isinstance(sources, dict):
        warnings.append({"code": "normalize_state_sources_wrong_type", "path": str(state_path)})
        sources = {}
    if not isinstance(processed_segments, dict):
        warnings.append({"code": "normalize_state_processed_segments_wrong_type", "path": str(state_path)})
        processed_segments = {}
    if stale_logic:
        sources = {str(path_text): 0 for path_text in sources}
        processed_segments = {}
    return sources, processed_segments


def _fallback_current_paths(base: pathlib.Path) -> set[pathlib.Path]:
    return set((base / "raw" / "current").glob("prompt-usage.raw.jsonl.current.*.jsonl"))


def _read_freshness_state(base: pathlib.Path) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []

    pointer_path = base / "state" / "current-raw-segments.json"
    pointer, pointer_warning = _json_state(pointer_path, "current_pointer")
    if pointer_warning is None:
        pointer_warning = _validate_current_pointer_header(base, pointer, pointer_path)
    _append_warning(warnings, pointer_warning)
    if pointer_warning is None:
        current_paths, pointer_unavailable = _pointer_current_paths(base, pointer, warnings, pointer_path)
    else:
        current_paths = set()
        pointer_unavailable = True

    normalize_path = base / "normalized" / "normalize-state.json"
    normalize_state, normalize_warning = _json_state(normalize_path, "normalize_state")
    _append_warning(warnings, normalize_warning)
    sources, processed_segments = _normalize_state_parts(base, normalize_state, warnings)

    manifest_path = base / "state" / "raw-segments-manifest.json"
    manifest, manifest_warning = _json_state(manifest_path, "raw_manifest")
    if manifest_warning is None:
        manifest_warning = _validate_manifest_header(base, manifest, manifest_path)
    _append_warning(warnings, manifest_warning)
    manifest_segments = _closed_manifest_segments(base, manifest, warnings, manifest_path) if manifest_warning is None else {}

    fallback_paths: set[pathlib.Path] = set()
    if pointer_unavailable:
        fallback_paths = _fallback_current_paths(base)
        if manifest_warning is None:
            fallback_paths -= {pathlib.Path(path_text).expanduser() for path_text in manifest_segments}

    return {
        "warnings": warnings,
        "sources": sources,
        "processed_segments": processed_segments,
        "current_paths": current_paths,
        "manifest_segments": manifest_segments,
        "fallback_paths": fallback_paths,
    }


def _pending_sources(base: pathlib.Path, state: dict[str, Any] | None = None) -> tuple[int, int]:
    state = state or _read_freshness_state(base)
    sources = state["sources"]
    processed_segments = state["processed_segments"]
    candidates: dict[str, int] = {}
    for path_text, offset in sources.items():
        path = pathlib.Path(str(path_text)).expanduser()
        candidates[str(path)] = _safe_offset(offset, _file_size(path))
    for path in state["current_paths"]:
        candidates.setdefault(str(path), 0)
    for path in state["fallback_paths"]:
        candidates.setdefault(str(path), 0)

    manifest_segments = state["manifest_segments"]
    for path_text, segment in manifest_segments.items():
        segment_id = str(segment.get("id") or "")
        if segment_id and segment_id not in processed_segments:
            candidates.setdefault(path_text, 0)

    pending_rows = 0
    pending_files = 0
    for path_text, offset in candidates.items():
        path = pathlib.Path(path_text).expanduser()
        if not path.is_file():
            continue
        rows = _count_jsonl_rows_after(path, offset)
        if rows <= 0:
            continue
        pending_rows += rows
        pending_files += 1
    return pending_rows, pending_files


def _run_metadata(db_path: pathlib.Path) -> dict[str, Any]:
    if not db_path.is_file():
        return {}
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute("select key, value from run_metadata").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if con is not None:
            con.close()
    metadata: dict[str, Any] = {}
    for key, value in rows:
        try:
            metadata[str(key)] = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            metadata[str(key)] = value
    return metadata


def _pending_normalized_rows(base: pathlib.Path, db_path: pathlib.Path) -> int:
    normalized = base / "normalized" / "prompt-usage.normalized.jsonl"
    size = _file_size(normalized)
    if size <= 0:
        return 0
    metadata = _run_metadata(db_path)
    offset = _safe_offset(metadata.get("applied_normalized_turns_size"), size)
    if size <= offset:
        return 0
    return _count_jsonl_rows_after(normalized, offset)


def _turn_start_ready_for_recovery(payload: dict[str, Any], now: datetime) -> bool:
    captured_at_ns = payload.get("captured_at_ns")
    if isinstance(captured_at_ns, (int, float)):
        age_seconds = (now.timestamp() * 1_000_000_000 - float(captured_at_ns)) / 1_000_000_000
        return age_seconds >= TURN_START_RECOVERY_AGE_SECONDS
    captured_at = payload.get("captured_at")
    if isinstance(captured_at, str) and captured_at:
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (now - parsed.astimezone(timezone.utc)).total_seconds() >= TURN_START_RECOVERY_AGE_SECONDS
    return True


def _terminal_turn_ids_for_transcript(transcript_path: str | pathlib.Path, cache: dict[str, set[str]]) -> set[str]:
    path = pathlib.Path(transcript_path).expanduser()
    cache_key = str(path.resolve(strict=False))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    terminal_turn_ids: set[str] = set()
    stream, error = transcript_parser.transcript_event_stream(path, 0)
    if error is not None or stream is None:
        cache[cache_key] = terminal_turn_ids
        return terminal_turn_ids
    try:
        for item in stream:
            event_item = item["item"]
            if not isinstance(event_item, dict) or event_item.get("type") != "event_msg":
                continue
            event = event_item.get("payload") if isinstance(event_item.get("payload"), dict) else {}
            turn_id = str(event.get("turn_id") or "")
            if turn_id and event.get("type") in TERMINAL_TURN_EVENT_TYPES:
                terminal_turn_ids.add(turn_id)
    except OSError:
        terminal_turn_ids = set()
    cache[cache_key] = terminal_turn_ids
    return terminal_turn_ids


def _turn_start_has_terminal_event(payload: dict[str, Any], terminal_cache: dict[str, set[str]]) -> bool:
    turn_id = str(payload.get("turn_id") or "")
    transcript_path = payload.get("transcript_path")
    if not turn_id or not transcript_path:
        return False
    return turn_id in _terminal_turn_ids_for_transcript(str(transcript_path), terminal_cache)


def _pending_recovery_files(base: pathlib.Path) -> int:
    state_dir = base / "state"
    count = 0
    now = datetime.now(timezone.utc)
    terminal_cache: dict[str, set[str]] = {}
    try:
        candidates = sorted(state_dir.glob("*.json"))
    except OSError:
        return 0
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        record_type = payload.get("record_type")
        if record_type == "turn_stop_missing_start":
            count += 1
        elif record_type == "turn_start" and _turn_start_ready_for_recovery(payload, now) and _turn_start_has_terminal_event(payload, terminal_cache):
            count += 1
    return count


def _latest_raw_mtime(base: pathlib.Path, state: dict[str, Any] | None = None) -> float | None:
    state = state or _read_freshness_state(base)
    candidates = list(state["current_paths"])
    candidates.extend(pathlib.Path(str(path_text)).expanduser() for path_text in state["sources"])
    candidates.extend(pathlib.Path(path_text).expanduser() for path_text in state["manifest_segments"])
    candidates.extend(state["fallback_paths"])
    mtimes = [value for value in (_mtime_unix(path) for path in candidates) if value is not None]
    return max(mtimes) if mtimes else None


def freshness_payload(token_usage_root: pathlib.Path | str, db_path: pathlib.Path | str) -> dict[str, Any]:
    base = pathlib.Path(token_usage_root).expanduser()
    db = pathlib.Path(db_path).expanduser()
    state = _read_freshness_state(base)
    db_mtime = _mtime_unix(db)
    latest_raw = _latest_raw_mtime(base, state)
    pending_rows, pending_files = _pending_sources(base, state)
    pending_normalized_rows = _pending_normalized_rows(base, db)
    pending_recovery_files = _pending_recovery_files(base)
    pending_analysis_rows = pending_rows + pending_normalized_rows
    has_db = db.is_file()
    needs_analyze = has_db and (pending_analysis_rows > 0 or pending_recovery_files > 0)
    data_health = "degraded" if state["warnings"] else "ok"
    if not has_db:
        status = "missing_db"
    elif needs_analyze:
        status = "needs_analyze"
    elif data_health == "degraded":
        status = "degraded"
    else:
        status = "current"
    return {
        "status": status,
        "needs_analyze": needs_analyze,
        "data_health": data_health,
        "warnings": state["warnings"],
        "pending_raw_rows": pending_rows if has_db else 0,
        "pending_raw_files": pending_files if has_db else 0,
        "pending_normalized_rows": pending_normalized_rows if has_db else 0,
        "pending_analysis_rows": pending_analysis_rows if has_db else 0,
        "pending_recovery_files": pending_recovery_files if has_db else 0,
        "analytics_db_mtime_unix": db_mtime,
        "analytics_db_mtime_iso": _iso_time(db_mtime),
        "latest_raw_mtime_unix": latest_raw,
        "latest_raw_mtime_iso": _iso_time(latest_raw),
    }
