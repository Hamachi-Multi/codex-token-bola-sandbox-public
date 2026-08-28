"""Source-context snapshots used by incremental analytics builds."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable


CONTEXT_SNAPSHOT_VERSION = 1
THREAD_COLUMNS = (
    "session_id",
    "rollout_path",
    "created_at_ms",
    "thread_name",
    "model",
    "reasoning_effort",
    "agent_role",
    "agent_nickname",
)
EDGE_COLUMNS = ("child_session_id", "parent_session_id", "status")


def _text(value: Any) -> str:
    return str(value or "")


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def thread_projection(threads: dict[str, dict[str, Any]]) -> dict[str, tuple[Any, ...]]:
    projection: dict[str, tuple[Any, ...]] = {}
    for session_id, thread in threads.items():
        key = _text(session_id)
        if not key:
            continue
        projection[key] = (
            key,
            _text(thread.get("rollout_path")),
            _integer(thread.get("created_at_ms")),
            _text(thread.get("thread_name")),
            _text(thread.get("model")),
            _text(thread.get("reasoning_effort")),
            _text(thread.get("agent_role")),
            _text(thread.get("agent_nickname")),
        )
    return projection


def edge_projection(edges: Iterable[tuple[str, str, str]]) -> dict[str, tuple[str, str, str]]:
    projection: dict[str, tuple[str, str, str]] = {}
    for parent, child, status in edges:
        parent_id = _text(parent)
        child_id = _text(child)
        if not parent_id or not child_id:
            continue
        projection[child_id] = (child_id, parent_id, _text(status))
    return projection


def read_thread_snapshot(con: sqlite3.Connection) -> dict[str, tuple[Any, ...]]:
    return {
        str(row[0]): tuple(row)
        for row in con.execute(f"select {','.join(THREAD_COLUMNS)} from source_context_threads")
    }


def read_edge_snapshot(con: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    return {
        str(row[0]): (str(row[0]), str(row[1]), str(row[2] or ""))
        for row in con.execute(f"select {','.join(EDGE_COLUMNS)} from source_context_edges")
    }


def changed_keys(previous: dict[str, tuple[Any, ...]], current: dict[str, tuple[Any, ...]]) -> set[str]:
    return {key for key in previous.keys() | current.keys() if previous.get(key) != current.get(key)}


def edge_change_sessions(
    previous: dict[str, tuple[str, str, str]],
    current: dict[str, tuple[str, str, str]],
) -> set[str]:
    sessions: set[str] = set()
    for child in changed_keys(previous, current):
        for row in (previous.get(child), current.get(child)):
            if row is not None:
                sessions.update((str(row[0]), str(row[1])))
    return sessions


def edge_rows(projection: dict[str, tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return [(parent, child, status) for child, parent, status in projection.values()]


def expand_sessions(sessions: set[str], edges: Iterable[tuple[str, str, str]]) -> set[str]:
    expanded = set(sessions)
    adjacent: dict[str, set[str]] = {}
    for parent, child, _status in edges:
        adjacent.setdefault(parent, set()).add(child)
        adjacent.setdefault(child, set()).add(parent)
    pending = list(expanded)
    while pending:
        session_id = pending.pop()
        for related in adjacent.get(session_id, set()):
            if related in expanded:
                continue
            expanded.add(related)
            pending.append(related)
    return expanded


def apply_snapshot_changes(
    con: sqlite3.Connection,
    previous_threads: dict[str, tuple[Any, ...]],
    current_threads: dict[str, tuple[Any, ...]],
    previous_edges: dict[str, tuple[str, str, str]],
    current_edges: dict[str, tuple[str, str, str]],
) -> None:
    changed_threads = changed_keys(previous_threads, current_threads)
    removed_threads = sorted(changed_threads - current_threads.keys())
    if removed_threads:
        con.executemany("delete from source_context_threads where session_id=?", [(key,) for key in removed_threads])
    current_thread_rows = [current_threads[key] for key in sorted(changed_threads & current_threads.keys())]
    if current_thread_rows:
        con.executemany(
            f"insert or replace into source_context_threads ({','.join(THREAD_COLUMNS)}) values (?,?,?,?,?,?,?,?)",
            current_thread_rows,
        )

    changed_edges = changed_keys(previous_edges, current_edges)
    removed_edges = sorted(changed_edges - current_edges.keys())
    if removed_edges:
        con.executemany("delete from source_context_edges where child_session_id=?", [(key,) for key in removed_edges])
    current_edge_rows = [current_edges[key] for key in sorted(changed_edges & current_edges.keys())]
    if current_edge_rows:
        con.executemany(
            f"insert or replace into source_context_edges ({','.join(EDGE_COLUMNS)}) values (?,?,?)",
            current_edge_rows,
        )


def replace_snapshot(
    con: sqlite3.Connection,
    threads: dict[str, tuple[Any, ...]],
    edges: dict[str, tuple[str, str, str]],
) -> None:
    con.execute("delete from source_context_threads")
    con.execute("delete from source_context_edges")
    if threads:
        con.executemany(
            f"insert into source_context_threads ({','.join(THREAD_COLUMNS)}) values (?,?,?,?,?,?,?,?)",
            [threads[key] for key in sorted(threads)],
        )
    if edges:
        con.executemany(
            f"insert into source_context_edges ({','.join(EDGE_COLUMNS)}) values (?,?,?)",
            [edges[key] for key in sorted(edges)],
        )
