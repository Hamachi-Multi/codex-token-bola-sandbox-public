"""Dashboard freshness metadata derived from raw log offsets."""

from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
import sqlite3
import sys
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import transcript_parser
import cost_rates
import turn_lifecycle

NORMALIZE_LOGIC_VERSION = 5
RECOVERY_RECORD_TYPES = {"turn_start", "turn_stop_missing_start"}
TURN_START_RECOVERY_AGE_SECONDS = 60
RECOVERY_TRANSCRIPT_CACHE_LIMIT = 128
_RECOVERY_TRANSCRIPT_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_RECOVERY_TRANSCRIPT_CACHE_LOCK = threading.RLock()


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


def _source_warning(warnings: list[dict[str, str]] | None, code: str, path: pathlib.Path) -> None:
    if warnings is None:
        return
    warning = {"code": code, "path": str(path)}
    if warning not in warnings:
        warnings.append(warning)


def _file_size(
    path: pathlib.Path,
    warnings: list[dict[str, str]] | None = None,
    *,
    missing_is_warning: bool = True,
) -> int | None:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        if missing_is_warning:
            _source_warning(warnings, "freshness_source_stat_error", path)
        return None
    except OSError:
        _source_warning(warnings, "freshness_source_stat_error", path)
        return None


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


def _count_jsonl_rows_after(
    path: pathlib.Path,
    offset: int,
    warnings: list[dict[str, str]] | None = None,
) -> int | None:
    if path.suffix == ".gz":
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            _source_warning(warnings, "freshness_source_read_error", path)
            return None
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return sum(1 for line in handle if line.strip())
    except OSError:
        _source_warning(warnings, "freshness_source_read_error", path)
        return None


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
    warnings = state["warnings"]
    sources = state["sources"]
    processed_segments = state["processed_segments"]
    candidates: dict[str, int] = {}
    for path_text, offset in sources.items():
        path = pathlib.Path(str(path_text)).expanduser()
        size = _file_size(path, warnings)
        if size is not None:
            candidates[str(path)] = _safe_offset(offset, size)
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
        rows = _count_jsonl_rows_after(path, offset, warnings)
        if rows is None or rows <= 0:
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


def _pending_normalized_rows(base: pathlib.Path, db_path: pathlib.Path, warnings: list[dict[str, str]] | None = None) -> int:
    normalized = base / "normalized" / "prompt-usage.normalized.jsonl"
    size = _file_size(normalized, warnings, missing_is_warning=False)
    if size is None or size <= 0:
        return 0
    metadata = _run_metadata(db_path)
    offset = _safe_offset(metadata.get("applied_normalized_turns_size"), size)
    if size <= offset:
        return 0
    rows = _count_jsonl_rows_after(normalized, offset, warnings)
    return rows if rows is not None else 0


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


def _complete_jsonl_state(path: pathlib.Path, size: int) -> tuple[int, str]:
    if size <= 0:
        return 0, hashlib.blake2b(b"", digest_size=16).hexdigest()
    try:
        with path.open("rb") as handle:
            digest = hashlib.blake2b(digest_size=16)
            sample_positions = sorted({0, max(0, size // 2 - 2048), max(0, size - 4096)})
            for position in sample_positions:
                handle.seek(position)
                digest.update(position.to_bytes(8, "big"))
                digest.update(handle.read(min(4096, size - position)))
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return size, digest.hexdigest()
            cursor = size
            while cursor > 0:
                chunk_start = max(0, cursor - 64 * 1024)
                handle.seek(chunk_start)
                chunk = handle.read(cursor - chunk_start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    return chunk_start + newline + 1, digest.hexdigest()
                cursor = chunk_start
    except OSError:
        return 0, ""
    return 0, digest.hexdigest()


def _scan_terminal_turn_ids(path: pathlib.Path, start: int, end: int) -> tuple[dict[str, int], bool]:
    if end <= start:
        return {}, True
    stream, error = transcript_parser.transcript_event_stream(path, start, end)
    if error is not None or stream is None:
        return {}, False
    terminal_turn_offsets: dict[str, int] = {}
    try:
        for item in stream:
            terminal = turn_lifecycle.terminal_turn_event(item)
            if terminal is not None:
                turn_id = terminal["turn_id"]
                terminal_turn_offsets[turn_id] = max(
                    terminal_turn_offsets.get(turn_id, -1),
                    terminal["event_offset"],
                )
    except OSError:
        return {}, False
    return terminal_turn_offsets, True


def _pending_start_offset(payload: dict[str, Any], complete_size: int) -> int:
    value = payload.get("start_file_size")
    if not isinstance(value, int) or value < 0 or value > complete_size:
        return 0
    return value


def _terminal_turn_ids_for_pending_states(path: pathlib.Path, states: list[dict[str, Any]]) -> set[str]:
    try:
        stat_result = path.stat()
    except OSError:
        return set()
    complete_size, content_signature = _complete_jsonl_state(path, stat_result.st_size)
    minimum_start = min((_pending_start_offset(state, complete_size) for state in states), default=complete_size)
    cache_key = str(path.resolve(strict=False))
    identity = (int(stat_result.st_dev), int(stat_result.st_ino))

    with _RECOVERY_TRANSCRIPT_CACHE_LOCK:
        cached = _RECOVERY_TRANSCRIPT_CACHE.get(cache_key)
        same_size_rewrite = (
            cached is not None
            and int(cached.get("observed_size") or 0) == int(stat_result.st_size)
            and int(cached.get("scanned_to") or 0) == complete_size
            and (
                int(cached.get("mtime_ns") or 0) != int(stat_result.st_mtime_ns)
                or int(cached.get("ctime_ns") or 0) != int(stat_result.st_ctime_ns)
                or cached.get("content_signature") != content_signature
            )
        )
        if (
            cached is None
            or cached.get("identity") != identity
            or int(cached.get("scanned_to") or 0) > complete_size
            or same_size_rewrite
        ):
            cached = {
                "identity": identity,
                "scanned_from": minimum_start,
                "scanned_to": minimum_start,
                "terminal_turn_offsets": {},
            }

        scanned_from = int(cached.get("scanned_from") or 0)
        scanned_to = int(cached.get("scanned_to") or 0)
        terminal_turn_offsets = dict(cached.get("terminal_turn_offsets") or {})

        if minimum_start < scanned_from:
            found, ok = _scan_terminal_turn_ids(path, minimum_start, scanned_from)
            if not ok:
                return set()
            for turn_id, offset in found.items():
                terminal_turn_offsets[turn_id] = max(terminal_turn_offsets.get(turn_id, -1), offset)
            scanned_from = minimum_start
        if complete_size > scanned_to:
            found, ok = _scan_terminal_turn_ids(path, scanned_to, complete_size)
            if not ok:
                return set()
            for turn_id, offset in found.items():
                terminal_turn_offsets[turn_id] = max(terminal_turn_offsets.get(turn_id, -1), offset)
            scanned_to = complete_size

        cached.update(
            {
                "identity": identity,
                "scanned_from": scanned_from,
                "scanned_to": scanned_to,
                "observed_size": int(stat_result.st_size),
                "mtime_ns": int(stat_result.st_mtime_ns),
                "ctime_ns": int(stat_result.st_ctime_ns),
                "content_signature": content_signature,
                "terminal_turn_offsets": terminal_turn_offsets,
            }
        )
        _RECOVERY_TRANSCRIPT_CACHE[cache_key] = cached
        _RECOVERY_TRANSCRIPT_CACHE.move_to_end(cache_key)
        while len(_RECOVERY_TRANSCRIPT_CACHE) > RECOVERY_TRANSCRIPT_CACHE_LIMIT:
            _RECOVERY_TRANSCRIPT_CACHE.popitem(last=False)
        return {
            turn_id
            for state in states
            if (turn_id := str(state.get("turn_id") or ""))
            and terminal_turn_offsets.get(turn_id, -1) >= _pending_start_offset(state, complete_size)
        }


def _pending_recovery_files(base: pathlib.Path) -> int:
    state_dir = base / "state"
    count = 0
    now = datetime.now(timezone.utc)
    pending_by_transcript: dict[str, list[dict[str, Any]]] = {}
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
        elif record_type == "turn_start" and _turn_start_ready_for_recovery(payload, now):
            transcript_path = str(payload.get("transcript_path") or "")
            turn_id = str(payload.get("turn_id") or "")
            if transcript_path and turn_id:
                pending_by_transcript.setdefault(transcript_path, []).append(payload)
    for transcript_path, states in pending_by_transcript.items():
        terminal_turn_ids = _terminal_turn_ids_for_pending_states(pathlib.Path(transcript_path).expanduser(), states)
        count += sum(1 for state in states if str(state.get("turn_id") or "") in terminal_turn_ids)
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
    pending_normalized_rows = _pending_normalized_rows(base, db, state["warnings"])
    pending_recovery_files = _pending_recovery_files(base)
    pending_analysis_rows = pending_rows + pending_normalized_rows
    has_db = db.is_file()
    metadata = _run_metadata(db)
    cost_rate_catalog_changed = False
    try:
        catalog, _revision = cost_rates.load_catalog()
        applied_catalog_digest = metadata.get("cost_rate_catalog_digest")
        cost_rate_catalog_changed = has_db and bool(applied_catalog_digest) and applied_catalog_digest != catalog.digest
    except cost_rates.CostRateError as exc:
        state["warnings"].append({"code": exc.error, "path": str(cost_rates.config_path())})
    if cost_rate_catalog_changed:
        state["warnings"].append({"code": "cost_rate_catalog_changed", "path": str(cost_rates.config_path())})
    needs_analyze = has_db and (pending_analysis_rows > 0 or pending_recovery_files > 0 or cost_rate_catalog_changed)
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
        "cost_rate_catalog_changed": cost_rate_catalog_changed,
        "analytics_db_mtime_unix": db_mtime,
        "analytics_db_mtime_iso": _iso_time(db_mtime),
        "latest_raw_mtime_unix": latest_raw,
        "latest_raw_mtime_iso": _iso_time(latest_raw),
    }
