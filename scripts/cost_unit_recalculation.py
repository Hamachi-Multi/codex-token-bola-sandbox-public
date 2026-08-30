"""Transactional Cost Units recalculation over an existing analytics database."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import time
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cost_rates
from build_analytics_schema import ANALYTICS_SCHEMA_VERSION


REQUIRED_COLUMNS = {
    "turns": {
        "session_id",
        "turn_id",
        "captured_at_unix",
        "started_at_unix",
        "model",
        "analytics_eligible",
        "non_cached_input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "weighted_credits",
        "cost_pico_usd",
        "cost_rate_status",
        "cost_rate_effective_from",
    },
    "task_rollups": {
        "parent_session_id",
        "parent_turn_id",
        "child_session_id",
        "own_weighted_credits",
        "child_weighted_credits",
        "total_weighted_credits",
    },
    "run_metadata": {"key", "value"},
}


class CostUnitRecalculationError(RuntimeError):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error

    def payload(self) -> dict[str, Any]:
        return {"error": self.error, "message": str(self)}


def _metadata_value(con: sqlite3.Connection, key: str) -> Any:
    try:
        row = con.execute("select value from run_metadata where key=?", (key,)).fetchone()
    except sqlite3.Error as exc:
        raise CostUnitRecalculationError(
            "cost_recalculation_requires_analyze",
            "Analytics data is not compatible with Cost Units recalculation. Run Analyze first",
        ) from exc
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return row[0]


def _validate_schema(con: sqlite3.Connection) -> None:
    if _metadata_value(con, "analytics_schema_version") != ANALYTICS_SCHEMA_VERSION:
        raise CostUnitRecalculationError(
            "cost_recalculation_requires_analyze",
            "Analytics data is not compatible with Cost Units recalculation. Run Analyze first",
        )
    try:
        for table, required in REQUIRED_COLUMNS.items():
            columns = {str(row[1]) for row in con.execute(f"pragma table_info({table})")}
            if not required.issubset(columns):
                raise CostUnitRecalculationError(
                    "cost_recalculation_requires_analyze",
                    "Analytics data is not compatible with Cost Units recalculation. Run Analyze first",
                )
    except sqlite3.Error as exc:
        raise CostUnitRecalculationError(
            "cost_recalculation_requires_analyze",
            "Analytics data is not compatible with Cost Units recalculation. Run Analyze first",
        ) from exc


def _cost_units(pico_usd: int | None) -> float | None:
    if pico_usd is None:
        return None
    return pico_usd / cost_rates.PICO_USD_PER_COST_UNIT


def _write_metadata(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "insert or replace into run_metadata(key, value) values (?, ?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def recalculate_cost_units(
    *,
    db_path: pathlib.Path | str,
    catalog: cost_rates.CostRateCatalog,
    expected_catalog_digest: str,
) -> dict[str, Any]:
    """Reprice stored turns and dependent rollup costs without rebuilding analytics."""
    if expected_catalog_digest != catalog.digest:
        raise CostUnitRecalculationError(
            "cost_rate_catalog_changed",
            "Cost rates changed before recalculation. Reload and try again",
        )

    target = pathlib.Path(db_path).expanduser()
    if not target.is_file():
        raise CostUnitRecalculationError(
            "cost_recalculation_requires_analyze",
            "Analytics data is unavailable. Run Analyze first",
        )

    started = time.monotonic()
    con = sqlite3.connect(f"file:{target}?mode=rw", uri=True, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("pragma busy_timeout=5000")
        con.execute("begin immediate")
        _validate_schema(con)

        rows = con.execute(
            """
            select session_id, turn_id, model,
                   coalesce(started_at_unix, captured_at_unix) usage_unix,
                   analytics_eligible, non_cached_input_tokens,
                   cached_input_tokens, output_tokens
            from turns
            """
        ).fetchall()
        updates: list[tuple[Any, ...]] = []
        turn_costs: dict[tuple[str, str], int | None] = {}
        session_costs: dict[str, dict[str, Any]] = {}
        priced_turns = 0
        unpriced_turns = 0
        unavailable_turns = 0

        for row in rows:
            session_id = str(row["session_id"])
            turn_id = str(row["turn_id"])
            if int(row["analytics_eligible"] or 0) != 1:
                pico_usd = None
                credits = None
                status = "unavailable"
                effective_from = None
                unavailable_turns += 1
            else:
                pico_usd, credits, rate = cost_rates.priced_usage(
                    catalog,
                    row["model"],
                    row["usage_unix"],
                    non_cached_input=int(row["non_cached_input_tokens"] or 0),
                    cached_input=int(row["cached_input_tokens"] or 0),
                    output=int(row["output_tokens"] or 0),
                )
                status = "configured" if rate is not None else "unconfigured"
                effective_from = rate.effective_from if rate is not None else None
                priced_turns += int(rate is not None)
                unpriced_turns += int(rate is None)
                turn_costs[(session_id, turn_id)] = pico_usd
                session = session_costs.setdefault(session_id, {"pico_usd": 0, "available": True})
                if pico_usd is None:
                    session["available"] = False
                else:
                    session["pico_usd"] += pico_usd
            updates.append((credits, pico_usd, status, effective_from, session_id, turn_id))

        con.executemany(
            """
            update turns
            set weighted_credits=?, cost_pico_usd=?, cost_rate_status=?, cost_rate_effective_from=?
            where session_id=? and turn_id=?
            """,
            updates,
        )

        rollup_rows = con.execute(
            "select parent_session_id, parent_turn_id, child_session_id from task_rollups"
        ).fetchall()
        rollup_updates: list[tuple[Any, ...]] = []
        for row in rollup_rows:
            parent_key = (str(row["parent_session_id"] or ""), str(row["parent_turn_id"] or ""))
            child_session_id = str(row["child_session_id"] or "")
            own_pico = turn_costs.get(parent_key, 0)
            child = session_costs.get(child_session_id)
            child_pico = 0 if child is None else (child["pico_usd"] if child["available"] else None)
            own_credits = _cost_units(own_pico)
            child_credits = _cost_units(child_pico)
            total_credits = None if own_pico is None or child_pico is None else _cost_units(own_pico + child_pico)
            rollup_updates.append(
                (
                    own_credits,
                    child_credits,
                    total_credits,
                    row["parent_session_id"],
                    row["parent_turn_id"],
                    row["child_session_id"],
                )
            )
        con.executemany(
            """
            update task_rollups
            set own_weighted_credits=?, child_weighted_credits=?, total_weighted_credits=?
            where parent_session_id=? and parent_turn_id=? and child_session_id=?
            """,
            rollup_updates,
        )

        applied_at_unix = time.time()
        _write_metadata(con, "cost_rate_catalog_digest", catalog.digest)
        _write_metadata(con, "cost_rate_applied_at_unix", applied_at_unix)
        _write_metadata(con, "unpriced_turn_rows", unpriced_turns)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    return {
        "catalog_digest": catalog.digest,
        "recalculated_turns": len(rows),
        "priced_turns": priced_turns,
        "unpriced_turns": unpriced_turns,
        "unavailable_turns": unavailable_turns,
        "recalculated_task_rollups": len(rollup_rows),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
