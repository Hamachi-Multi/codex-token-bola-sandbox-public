"""Durable indexed retention attribution state.

The store is intentionally separate from the derived analytics database. Raw
retention can reset analytics outputs, while parent-turn attribution must
survive that reset and output-directory migrations.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterable, Iterator
from typing import Any


SCHEMA_VERSION = 1
GRACE_SECONDS = 30 * 24 * 60 * 60
DB_RELATIVE_PATH = pathlib.Path("state") / "retention-pruned-turns.sqlite"
LEGACY_RELATIVE_PATHS = (
    pathlib.Path("state") / "retention-pruned-turns.json",
    pathlib.Path("state") / "retention-pruned-turns.pending.json",
)


class RetentionPrunedStoreError(RuntimeError):
    pass


def database_path(base: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / DB_RELATIVE_PATH


def inspect_summary(base: pathlib.Path | str) -> dict[str, Any]:
    """Inspect the durable store without creating, migrating, or updating it."""
    path = database_path(base)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "valid": True,
        "schema_version": None,
        "committed_rows": 0,
        "pending_rows": 0,
        "pending_job_ids": [],
        "pending_job_count": 0,
        "pending_job_ids_truncated": False,
        "oldest_pending_pruned_at_unix": None,
    }
    if not path.exists():
        return result
    try:
        wal_path = path.with_name(f"{path.name}-wal")
        connection_uri = f"file:{path}?mode=ro" if wal_path.exists() else f"file:{path}?mode=ro&immutable=1"
        con = sqlite3.connect(connection_uri, uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("pragma query_only=on")
            integrity = con.execute("pragma quick_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise RetentionPrunedStoreError("retention pruned store quick_check failed")
            metadata = con.execute("select value from store_metadata where key='schema_version'").fetchone()
            if metadata is None or str(metadata[0]) != str(SCHEMA_VERSION):
                raise RetentionPrunedStoreError("unsupported retention pruned store schema")
            columns = {str(row[1]) for row in con.execute("pragma table_info(pruned_turns)")}
            required = {
                "session_id", "turn_id", "start_ts", "stop_ts", "captured_at_unix",
                "pruned_at_unix", "last_required_at_unix", "state", "job_id",
            }
            if not required.issubset(columns):
                raise RetentionPrunedStoreError("retention pruned store columns are incomplete")
            counts = {
                str(row[0]): int(row[1])
                for row in con.execute("select state, count(*) from pruned_turns group by state")
            }
            jobs = [str(row[0]) for row in con.execute("select distinct job_id from pruned_turns where state='pending' order by job_id limit 21")]
            job_count = int(con.execute("select count(distinct job_id) from pruned_turns where state='pending'").fetchone()[0])
            oldest = con.execute("select min(pruned_at_unix) from pruned_turns where state='pending'").fetchone()
            result.update(
                schema_version=SCHEMA_VERSION,
                committed_rows=counts.get("committed", 0),
                pending_rows=counts.get("pending", 0),
                pending_job_ids=jobs[:20],
                pending_job_count=job_count,
                pending_job_ids_truncated=len(jobs) > 20,
                oldest_pending_pruned_at_unix=float(oldest[0]) if oldest and oldest[0] is not None else None,
            )
        finally:
            con.close()
    except (sqlite3.Error, OSError, RetentionPrunedStoreError) as exc:
        result.update(valid=False, error=str(exc))
    return result


def _open_flags(*flags: int) -> int:
    result = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for flag in flags:
        result |= flag
    return result


def _prepare_private_state_directory(path: pathlib.Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(path, _open_flags(os.O_RDONLY, getattr(os, "O_DIRECTORY", 0)))
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise RetentionPrunedStoreError(f"retention state path is not a directory: {path}")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    except (OSError, RetentionPrunedStoreError) as exc:
        if isinstance(exc, RetentionPrunedStoreError):
            raise
        raise RetentionPrunedStoreError(f"cannot secure retention state directory {path}: {exc}") from exc


def _secure_private_file(path: pathlib.Path, *, required: bool) -> None:
    try:
        descriptor = os.open(path, _open_flags(os.O_RDONLY))
    except FileNotFoundError:
        if required:
            raise RetentionPrunedStoreError(f"retention store file missing: {path}")
        return
    except OSError as exc:
        raise RetentionPrunedStoreError(f"cannot open retention store file safely {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RetentionPrunedStoreError(f"retention store path is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        raise RetentionPrunedStoreError(f"cannot secure retention store file {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _prepare_private_database(path: pathlib.Path) -> None:
    _prepare_private_state_directory(path.parent)
    try:
        descriptor = os.open(path, _open_flags(os.O_RDWR, os.O_CREAT), 0o600)
    except OSError as exc:
        raise RetentionPrunedStoreError(f"cannot create retention store safely {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RetentionPrunedStoreError(f"retention store path is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        raise RetentionPrunedStoreError(f"cannot secure retention store file {path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _secure_sqlite_files(path: pathlib.Path) -> None:
    _secure_private_file(path, required=True)
    _secure_private_file(path.with_name(f"{path.name}-wal"), required=False)
    _secure_private_file(path.with_name(f"{path.name}-shm"), required=False)


@contextlib.contextmanager
def _connection(path: pathlib.Path, *, writable: bool) -> Iterator[sqlite3.Connection]:
    if writable:
        _prepare_private_database(path)
        con = sqlite3.connect(path, timeout=30)
        try:
            con.execute("pragma journal_mode=wal")
            con.execute("pragma synchronous=full")
            _secure_sqlite_files(path)
        except Exception:
            con.close()
            raise
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        try:
            if writable:
                _secure_sqlite_files(path)
        finally:
            con.close()
        if writable:
            _secure_sqlite_files(path)


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        create table if not exists store_metadata (
          key text primary key,
          value text not null
        );
        create table if not exists pruned_turns (
          session_id text not null,
          turn_id text not null,
          start_ts real not null,
          stop_ts real not null,
          captured_at_unix real not null,
          pruned_at_unix real not null,
          last_required_at_unix real not null,
          state text not null check (state in ('pending', 'committed')),
          job_id text not null,
          primary key (session_id, turn_id)
        );
        create index if not exists pruned_turns_session_start
          on pruned_turns(session_id, start_ts);
        create index if not exists pruned_turns_state_pruned
          on pruned_turns(state, pruned_at_unix);
        create index if not exists pruned_turns_last_required
          on pruned_turns(last_required_at_unix);
        create index if not exists pruned_turns_job
          on pruned_turns(job_id, state);
        """
    )
    con.execute(
        "insert or replace into store_metadata(key, value) values ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def normalize_row(item: dict[str, Any], *, pruned_at_unix: float) -> tuple[Any, ...] | None:
    session_id = str(item.get("session_id") or "")
    turn_id = str(item.get("turn_id") or "")
    if not session_id or not turn_id:
        return None
    captured = _parse_time(item.get("captured_at")) or _safe_float(item.get("captured_at_unix"))
    start = _parse_time(item.get("started_at")) or _safe_float(item.get("start_ts")) or captured
    stop = _parse_time(item.get("stopped_at")) or _safe_float(item.get("stop_ts")) or captured
    if stop < start:
        stop = start
    pruned_at = max(0.0, _safe_float(item.get("pruned_at_unix"), pruned_at_unix))
    last_required = max(pruned_at, _safe_float(item.get("last_required_at_unix"), pruned_at))
    return session_id, turn_id, start, stop, captured, pruned_at, last_required


def _legacy_payload(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RetentionPrunedStoreError(f"invalid retention pruned turn state at {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version", 1) != 1:
        raise RetentionPrunedStoreError(f"unsupported retention pruned turn state at {path}")
    if not isinstance(payload.get("pruned_turns"), list):
        raise RetentionPrunedStoreError(f"invalid retention pruned turn rows at {path}")
    return payload


def _archive_legacy(base: pathlib.Path, paths: list[pathlib.Path]) -> None:
    if not paths:
        return
    archive = base / "reports" / "migrations" / "retention-pruned-turns"
    archive.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if not path.exists():
            continue
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()[:16]
        target = archive / f"{path.name}.{digest}.json"
        if target.exists():
            path.unlink()
        else:
            path.replace(target)
        target.chmod(0o600)


def migrate_legacy(base: pathlib.Path | str) -> dict[str, int]:
    root = pathlib.Path(base).expanduser()
    legacy = [root / relative for relative in LEGACY_RELATIVE_PATHS]
    payloads = [(path, _legacy_payload(path)) for path in legacy]
    payloads = [(path, payload) for path, payload in payloads if payload is not None]
    if not payloads:
        return {"imported_rows": 0, "legacy_files": 0}
    path = database_path(root)
    imported = 0
    with _connection(path, writable=True) as con:
        ensure_schema(con)
        with con:
            for legacy_path, payload in payloads:
                assert payload is not None
                pruned_at = _safe_float(payload.get("updated_at_unix"), time.time())
                state = "pending" if legacy_path.name.endswith(".pending.json") else "committed"
                legacy_hasher = hashlib.sha256()
                with legacy_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        legacy_hasher.update(chunk)
                job_id = f"legacy:{legacy_hasher.hexdigest()}"
                for item in payload.get("pruned_turns") or []:
                    if not isinstance(item, dict):
                        continue
                    row = normalize_row(item, pruned_at_unix=pruned_at)
                    if row is None:
                        continue
                    con.execute(
                        """
                        insert into pruned_turns(
                          session_id, turn_id, start_ts, stop_ts, captured_at_unix,
                          pruned_at_unix, last_required_at_unix, state, job_id
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        on conflict(session_id, turn_id) do update set
                          start_ts=excluded.start_ts,
                          stop_ts=excluded.stop_ts,
                          captured_at_unix=excluded.captured_at_unix,
                          pruned_at_unix=min(pruned_turns.pruned_at_unix, excluded.pruned_at_unix),
                          last_required_at_unix=max(pruned_turns.last_required_at_unix, excluded.last_required_at_unix),
                          state=case when pruned_turns.state='committed' then 'committed' else excluded.state end,
                          job_id=case when pruned_turns.state='committed' then pruned_turns.job_id else excluded.job_id end
                        """,
                        (*row, state, job_id),
                    )
                    imported += 1
    _archive_legacy(root, [path for path, _payload in payloads])
    return {"imported_rows": imported, "legacy_files": len(payloads)}


def stage_rows(
    base: pathlib.Path | str,
    turns: Iterable[dict[str, Any]],
    *,
    pruned_at_unix: float | None = None,
    job_id: str | None = None,
) -> str | None:
    root = pathlib.Path(base).expanduser()
    migrate_legacy(root)
    now = time.time() if pruned_at_unix is None else float(pruned_at_unix)
    identifier = job_id or uuid.uuid4().hex
    rows = (normalize_row(item, pruned_at_unix=now) for item in turns)
    path = database_path(root)
    inserted = 0
    with _connection(path, writable=True) as con:
        ensure_schema(con)
        with con:
            for row in rows:
                if row is None:
                    continue
                existing = con.execute(
                    "select start_ts, stop_ts, captured_at_unix from pruned_turns where session_id=? and turn_id=?",
                    (row[0], row[1]),
                ).fetchone()
                if existing is not None and tuple(float(existing[key]) for key in ("start_ts", "stop_ts", "captured_at_unix")) != tuple(float(value) for value in row[2:5]):
                    raise RetentionPrunedStoreError(f"conflicting retention pruned turn row: {row[0]}/{row[1]}")
                con.execute(
                    """
                    insert into pruned_turns(
                      session_id, turn_id, start_ts, stop_ts, captured_at_unix,
                      pruned_at_unix, last_required_at_unix, state, job_id
                    ) values (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    on conflict(session_id, turn_id) do update set
                      pruned_at_unix=min(pruned_turns.pruned_at_unix, excluded.pruned_at_unix),
                      last_required_at_unix=max(pruned_turns.last_required_at_unix, excluded.last_required_at_unix),
                      state=case when pruned_turns.state='committed' then 'committed' else 'pending' end,
                      job_id=case when pruned_turns.state='committed' then pruned_turns.job_id else excluded.job_id end
                    """,
                    (*row, identifier),
                )
                inserted += 1
    return identifier if inserted else None


def commit_stage(base: pathlib.Path | str, job_id: str | None) -> None:
    if not job_id:
        return
    path = database_path(base)
    if not path.exists():
        raise RetentionPrunedStoreError(f"retention pruned turn store missing: {path}")
    with _connection(path, writable=True) as con:
        ensure_schema(con)
        with con:
            con.execute("update pruned_turns set state='committed' where job_id=?", (job_id,))


def discard_stage(base: pathlib.Path | str, job_id: str | None) -> None:
    if not job_id:
        return
    path = database_path(base)
    if not path.exists():
        return
    with _connection(path, writable=True) as con:
        ensure_schema(con)
        with con:
            con.execute("delete from pruned_turns where job_id=? and state='pending'", (job_id,))


def rows_for_sessions(base: pathlib.Path | str, session_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    root = pathlib.Path(base).expanduser()
    path = database_path(root)
    if not path.exists():
        # A build is a mutating operation and may perform the one-time import.
        migrate_legacy(root)
    if not path.exists():
        return []
    with _connection(path, writable=False) as con:
        ids = sorted({str(value) for value in (session_ids or []) if str(value)})
        if not ids:
            rows = con.execute(
                "select * from pruned_turns order by session_id, start_ts, turn_id"
            ).fetchall()
        else:
            rows = []
            for start in range(0, len(ids), 400):
                chunk = ids[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    con.execute(
                        f"select * from pruned_turns where session_id in ({placeholders}) order by session_id, start_ts, turn_id",
                        chunk,
                    ).fetchall()
                )
        return [dict(row) for row in rows]


def snapshot_rows(base: pathlib.Path | str) -> dict[tuple[str, str], dict[str, Any]]:
    """Read SQLite and legacy rows without mutating either source."""
    root = pathlib.Path(base).expanduser()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    path = database_path(root)
    if path.exists():
        with _connection(path, writable=False) as con:
            for row in con.execute("select * from pruned_turns"):
                item = dict(row)
                result[(str(item["session_id"]), str(item["turn_id"]))] = item
    for relative in LEGACY_RELATIVE_PATHS:
        legacy_path = root / relative
        payload = _legacy_payload(legacy_path)
        if payload is None:
            continue
        pruned_at = _safe_float(payload.get("updated_at_unix"), legacy_path.stat().st_mtime)
        state = "pending" if legacy_path.name.endswith(".pending.json") else "committed"
        for value in payload.get("pruned_turns") or []:
            if not isinstance(value, dict):
                continue
            row = normalize_row(value, pruned_at_unix=pruned_at)
            if row is None:
                continue
            item = {
                "session_id": row[0],
                "turn_id": row[1],
                "start_ts": row[2],
                "stop_ts": row[3],
                "captured_at_unix": row[4],
                "pruned_at_unix": row[5],
                "last_required_at_unix": row[6],
                "state": state,
                "job_id": f"legacy:{legacy_path.name}",
            }
            key = (str(row[0]), str(row[1]))
            previous = result.get(key)
            if previous is not None and any(float(previous[field]) != float(item[field]) for field in ("start_ts", "stop_ts", "captured_at_unix")):
                raise RetentionPrunedStoreError(f"conflicting retention pruned turn row: {key[0]}/{key[1]}")
            if previous is None or previous.get("state") != "committed":
                result[key] = item
    return result


def mark_required_and_compact(
    base: pathlib.Path | str,
    required_keys: Iterable[tuple[str, str]],
    *,
    now_unix: float | None = None,
    grace_seconds: float = GRACE_SECONDS,
) -> dict[str, int]:
    path = database_path(base)
    if not path.exists():
        return {"required_rows": 0, "deleted_rows": 0}
    now = time.time() if now_unix is None else float(now_unix)
    required = sorted({(str(session), str(turn)) for session, turn in required_keys if session and turn})
    with _connection(path, writable=True) as con:
        ensure_schema(con)
        with con:
            con.executemany(
                "update pruned_turns set last_required_at_unix=? where session_id=? and turn_id=?",
                [(now, session, turn) for session, turn in required],
            )
            cursor = con.execute(
                "delete from pruned_turns where state='committed' and last_required_at_unix < ?",
                (now - max(0.0, float(grace_seconds)),),
            )
        return {"required_rows": len(required), "deleted_rows": max(0, int(cursor.rowcount))}


def export_rows(base: pathlib.Path | str) -> dict[tuple[str, str], dict[str, Any]]:
    migrate_legacy(base)
    return snapshot_rows(base)


def merge_stores(source: pathlib.Path | str, destination: pathlib.Path | str) -> dict[str, int]:
    source_rows = export_rows(source)
    destination_rows = export_rows(destination)
    conflicts = [
        key
        for key in source_rows.keys() & destination_rows.keys()
        if any(float(source_rows[key][field]) != float(destination_rows[key][field]) for field in ("start_ts", "stop_ts", "captured_at_unix"))
    ]
    if conflicts:
        session_id, turn_id = sorted(conflicts)[0]
        raise RetentionPrunedStoreError(f"conflicting retention pruned turn row: {session_id}/{turn_id}")
    if source_rows:
        root = pathlib.Path(destination).expanduser()
        path = database_path(root)
        with _connection(path, writable=True) as con:
            ensure_schema(con)
            with con:
                for row in source_rows.values():
                    con.execute(
                        """
                        insert into pruned_turns values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        on conflict(session_id, turn_id) do update set
                          pruned_at_unix=min(pruned_turns.pruned_at_unix, excluded.pruned_at_unix),
                          last_required_at_unix=max(pruned_turns.last_required_at_unix, excluded.last_required_at_unix),
                          state=case when pruned_turns.state='committed' or excluded.state='committed' then 'committed' else 'pending' end
                        """,
                        tuple(row[field] for field in (
                            "session_id", "turn_id", "start_ts", "stop_ts", "captured_at_unix",
                            "pruned_at_unix", "last_required_at_unix", "state", "job_id",
                        )),
                    )
    return {
        "source_rows": len(source_rows),
        "destination_rows": len(destination_rows),
        "merged_rows": len(set(source_rows) | set(destination_rows)),
        "deduplicated_rows": len(source_rows) + len(destination_rows) - len(set(source_rows) | set(destination_rows)),
    }
