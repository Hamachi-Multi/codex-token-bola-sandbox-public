"""SQLite payload builders for the Codex Token Bola dashboard API."""

from __future__ import annotations

import math
import pathlib
import sqlite3
import sys
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cost_rates
from dashboard_query_helpers import (
    SUBAGENT_CONFIDENCE_ORDER,
    TURN_SORT_COLUMNS,
    ApiError,
    complete_subagent_rows,
    empty_payload,
    empty_session_detail_payload,
    empty_subagent_payload,
    empty_summary,
    empty_turns_payload,
    int_query,
    rows_to_dicts,
)

__all__ = [
    "ApiError",
    "DashboardQueries",
    "empty_payload",
    "empty_session_detail_payload",
    "empty_subagent_payload",
    "empty_summary",
    "empty_turns_payload",
]

SESSION_SORT_COLUMNS = {
    "session": "lower(coalesce(nullif(project,''), 'Unknown'))",
    "credits": "credits",
    "raw": "raw",
    "turns": "turns",
}
TOOL_SORT_COLUMNS = {
    "tool_name": "lower(tool_name)",
    "calls": "calls",
    "output_tokens": "output_tokens",
    "share": "output_tokens",
}
SUBAGENT_SORT_COLUMNS = {
    "confidence": "confidence",
    "rows": "rows",
    "child_credits": "child_credits",
    "child_raw": "child_raw",
}


class DashboardQueries:
    def __init__(self, con: sqlite3.Connection, query) -> None:
        self.con = con
        self.query = query
        self.turn_columns = {str(row[1]) for row in con.execute("pragma table_info(turns)")}

    def analytics_eligible_predicate(self, alias: str | None = None) -> str:
        if "analytics_eligible" not in self.turn_columns:
            return "1=1"
        prefix = f"{alias}." if alias else ""
        return f"{prefix}analytics_eligible=1"

    def resolution_list_columns(self) -> str:
        if "token_resolution_status" not in self.turn_columns:
            return "'resolved' token_resolution_status, null token_resolution_reason, 1 token_data_available"
        reason = "token_resolution_reason" if "token_resolution_reason" in self.turn_columns else "null"
        available = "analytics_eligible" if "analytics_eligible" in self.turn_columns else "case when token_resolution_status='resolved' then 1 else 0 end"
        return f"token_resolution_status, {reason} token_resolution_reason, {available} token_data_available"

    def cost_sum_expression(self, *, alias: str = "", zero_when_empty: bool = False) -> str:
        prefix = f"{alias}." if alias else ""
        weighted = f"{prefix}weighted_credits"
        aggregate = f"sum({weighted})"
        if "cost_pico_usd" in self.turn_columns:
            pico = f"{prefix}cost_pico_usd"
            aggregate = (
                f"case when count({pico})=count({weighted}) "
                f"then sum({pico})/{cost_rates.PICO_USD_PER_COST_UNIT}.0 else sum({weighted}) end"
            )
        if zero_when_empty:
            aggregate = f"coalesce({aggregate}, 0)"
        return f"case when count({weighted})=count(*) then {aggregate} end"

    def filters(self, alias=None):
        prefix = f"{alias}." if alias else ""
        clauses = ["1=1"]
        args = []
        days = int_query(self.query, "days", 7, 0, 3650)
        if days > 0:
            clauses.append(f"{prefix}started_at_unix >= strftime('%s','now') - ?")
            args.append(days * 86400)
        session_id = (self.query.get("session_id") or [""])[0].strip()
        if session_id:
            clauses.append(f"{prefix}session_id = ?")
            args.append(session_id)
        project = (self.query.get("project") or [""])[0].strip()
        if project:
            clauses.append(f"{prefix}project = ?")
            args.append(project)
        focus_session_id = (self.query.get("focus_session_id") or [""])[0].strip()
        focus_turn_id = (self.query.get("focus_turn_id") or [""])[0].strip()
        if focus_session_id and focus_turn_id:
            clauses.append(f"{prefix}session_id = ?")
            args.append(focus_session_id)
            clauses.append(f"{prefix}turn_id = ?")
            args.append(focus_turn_id)
        return " and ".join(clauses), args

    def selected_turns_cte(self):
        where, args = self.filters()
        sql = f"with selected_turns as (select * from turns where {where} and {self.analytics_eligible_predicate()} order by weighted_credits desc, started_at_unix desc, session_id desc, turn_id desc"
        sql += ")"
        return sql, args

    def create_selected_turns_temp(self) -> None:
        where, args = self.filters()
        self.con.execute("drop table if exists temp.selected_turns")
        sql = f"create temp table selected_turns as select * from turns where {where} and {self.analytics_eligible_predicate()} order by weighted_credits desc, started_at_unix desc, session_id desc, turn_id desc"
        self.con.execute(sql, args)
        self.con.execute("create index idx_selected_turns_turn on selected_turns(session_id, turn_id)")

    def selected_rollups_cte(self) -> tuple[str, list[Any]]:
        selected_where, selected_args = self.filters()
        rollup_where, rollup_args = self.filters("t")
        rollup_where = rollup_where.replace("t.started_at_unix", "coalesce(t.started_at_unix, r.child_started_unix)")
        rollup_where = rollup_where.replace("t.session_id", "coalesce(t.session_id, r.parent_session_id)")
        rollup_where = rollup_where.replace("t.project", "coalesce(t.project, ct.project)")
        return (
            f"""
            with selected_turns as (
              select *
              from turns
              where {selected_where} and {self.analytics_eligible_predicate()}
              order by weighted_credits desc, started_at_unix desc, session_id desc, turn_id desc
            ),
            child_turns as (
              select session_id, coalesce(max(project), '') project, coalesce(max(cwd), '') cwd
              from turns
              where {self.analytics_eligible_predicate()}
              group by session_id
            ),
            selected_rollups as (
              select r.*, coalesce(st.project, t.project, ct.project, '') project,
                     coalesce(st.session_id, t.session_id, r.parent_session_id) session_id,
                     coalesce(st.thread_name, t.thread_name, '') thread_name,
                     coalesce(st.cwd, t.cwd, ct.cwd, '') cwd,
                     coalesce(st.prompt_preview, t.prompt_preview, '') prompt_preview
              from task_rollups r
              left join selected_turns st on st.session_id = r.parent_session_id and st.turn_id = r.parent_turn_id
              left join turns t on t.session_id = r.parent_session_id and t.turn_id = r.parent_turn_id and {self.analytics_eligible_predicate('t')}
              left join child_turns ct on ct.session_id = r.child_session_id
              where st.session_id is not null or (t.session_id is null and {rollup_where})
              order by r.child_weighted_credits desc
            )
            """,
            [*selected_args, *rollup_args],
        )

    def turn_order_clause(self) -> str:
        sort_key = (self.query.get("sort") or ["date"])[0]
        sort_dir = (self.query.get("sort_dir") or ["desc"])[0]
        expression = self.session_label_sort_expression() if sort_key == "session" else TURN_SORT_COLUMNS.get(sort_key, TURN_SORT_COLUMNS["date"])
        direction = "asc" if str(sort_dir).lower() == "asc" else "desc"
        if sort_key == "credits":
            return f"weighted_credits is null asc, weighted_credits {direction}, started_at_unix desc, session_id desc, turn_id desc"
        if sort_key in {"date", "time"}:
            return f"{expression} {direction}, session_id {direction}, turn_id {direction}"
        return f"{expression} {direction}, started_at_unix desc, session_id desc, turn_id desc"

    def session_label_mode(self) -> str:
        value = str((self.query.get("session_label_mode") or ["project"])[0] or "")
        return value if value in {"project", "thread"} else "project"

    def session_label_sort_expression(self) -> str:
        if self.session_label_mode() == "thread":
            return "lower(coalesce(nullif(thread_name,''), 'Unnamed'))"
        return "lower(coalesce(nullif(project,''), 'Unknown'))"

    def rollup_order_clause(self, sort_param: str, dir_param: str, columns: dict[str, str], default_key: str, tie_breaker: str) -> str:
        sort_key = (self.query.get(sort_param) or [default_key])[0]
        sort_dir = (self.query.get(dir_param) or ["desc"])[0]
        expression = columns.get(sort_key, columns[default_key])
        direction = "asc" if str(sort_dir).lower() == "asc" else "desc"
        return f"{expression} {direction}, {tie_breaker}"

    def session_order_clause(self) -> str:
        columns = {**SESSION_SORT_COLUMNS, "session": self.session_label_sort_expression()}
        return self.rollup_order_clause(
            "session_sort",
            "session_sort_dir",
            columns,
            "credits",
            "latest_started_at_unix desc, session_id desc",
        )

    def tool_order_clause(self) -> str:
        return self.rollup_order_clause(
            "tool_sort",
            "tool_sort_dir",
            TOOL_SORT_COLUMNS,
            "output_tokens",
            "tool_name asc",
        )

    def subagent_rows_ordered(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sort_key = (self.query.get("subagent_sort") or ["child_credits"])[0]
        sort_dir = (self.query.get("subagent_sort_dir") or ["desc"])[0]
        key = sort_key if sort_key in SUBAGENT_SORT_COLUMNS else "child_credits"
        reverse = str(sort_dir).lower() != "asc"
        completed = complete_subagent_rows(rows)
        if key == "confidence":
            return sorted(completed, key=lambda row: str(row.get("confidence") or ""), reverse=reverse)
        if reverse:
            return sorted(completed, key=lambda row: (-float(row.get(key) or 0), str(row.get("confidence") or "")))
        return sorted(completed, key=lambda row: (float(row.get(key) or 0), str(row.get("confidence") or "")))

    def turns_payload(self):
        where, args = self.filters()
        order_clause = self.turn_order_clause()
        page = int_query(self.query, "page", 1, 1, 100000)
        per_page = int_query(self.query, "per_page", 25, 1, 100)
        offset = (page - 1) * per_page
        focus_session_id = (self.query.get("focus_session_id") or [""])[0]
        focus_turn_id = (self.query.get("focus_turn_id") or [""])[0]
        focused = bool(focus_session_id and focus_turn_id)
        cost_rate_status = "cost_rate_status" if "cost_rate_status" in self.turn_columns else "'configured' cost_rate_status"
        cost_rate_effective_from = "cost_rate_effective_from" if "cost_rate_effective_from" in self.turn_columns else "null cost_rate_effective_from"
        select_cols = f"""
            session_id, turn_id, captured_at, started_at, prompt_preview, cwd, project, thread_name, turn_status,
            {self.resolution_list_columns()},
            weighted_credits credits, {cost_rate_status}, {cost_rate_effective_from},
            total_tokens raw, model_call_count calls
        """
        if focused:
            focus_where = f"{where} and session_id=? and turn_id=?"
            focus_args = [*args, focus_session_id, focus_turn_id]
            total = self.con.execute(f"select count(*) from turns where {focus_where}", focus_args).fetchone()[0]
            rows = rows_to_dicts(
                self.con.execute(
                    f"""
                    select {select_cols}
                    from turns
                    where {focus_where}
                    order by {order_clause}
                    limit 1
                    """,
                    focus_args,
                )
            )
            page = 1
        else:
            total = self.con.execute(f"select count(*) from turns where {where}", args).fetchone()[0]
            page_count = max(1, math.ceil(total / per_page))
            page = min(page, page_count)
            offset = (page - 1) * per_page
            rows = rows_to_dicts(
                self.con.execute(
                    f"""
                    select {select_cols}
                    from turns
                    where {where}
                    order by {order_clause}
                    limit ? offset ?
                    """,
                    [*args, per_page, offset],
                )
            )
        return {"rows": rows, "total": total, "page": page, "per_page": per_page, "focused": focused}

    def first_column_page(self, key: str, total: int) -> tuple[int, int, int]:
        per_page = int_query(self.query, "per_page", 25, 1, 100)
        requested_page = int_query(self.query, key, 1, 1, 100000)
        page_count = max(1, math.ceil(total / per_page))
        page = min(requested_page, page_count)
        return page, per_page, (page - 1) * per_page

    def session_options_payload(self):
        limit = int_query(self.query, "limit", 50, 1, 200)
        search = str((self.query.get("q") or [""])[0] or "").strip().lower()
        turn_clauses = ["1=1"]
        turn_args: list[Any] = []
        if "days" in self.query:
            days = int_query(self.query, "days", 0, 0, 3650)
            if days > 0:
                turn_clauses.append("started_at_unix >= strftime('%s','now') - ?")
                turn_args.append(days * 86400)
        turn_where = " and ".join(turn_clauses)
        where = ""
        args: list[Any] = []
        if search:
            where = """
                where lower(
                    coalesce(session_id,'') || ' ' ||
                    coalesce(thread_name,'') || ' ' ||
                    coalesce(project,'') || ' ' ||
                    coalesce(cwd,'')
                ) like ?
            """
            args.append(f"%{search}%")
        rows = rows_to_dicts(
            self.con.execute(
                """
                with scoped_turns as (
                  select *
                  from turns
                  where {turn_where} and {eligible}
                ),
                session_rows as (
                  select s.session_id,
                         coalesce(max(nullif(thread_name,'')), '') thread_name,
                         coalesce(
                           (select t2.project from scoped_turns t2
                            where t2.session_id = s.session_id
                            order by t2.started_at_unix desc, t2.turn_id desc
                            limit 1),
                           ''
                         ) project,
                         coalesce(
                           (select t2.cwd from scoped_turns t2
                            where t2.session_id = s.session_id
                            order by t2.started_at_unix desc, t2.turn_id desc
                            limit 1),
                           ''
                         ) cwd,
                         count(*) turns,
                         max(started_at_unix) latest_started_at_unix,
                         {cost_sum} credits
                  from scoped_turns s
                  group by s.session_id
                )
                select *
                from session_rows
                {where}
                order by latest_started_at_unix desc, session_id desc
                limit ?
                """.format(
                    turn_where=turn_where,
                    eligible=self.analytics_eligible_predicate(),
                    where=where,
                    cost_sum=self.cost_sum_expression(),
                ),
                [*turn_args, *args, limit + 1],
            )
        )
        return {"rows": rows[:limit], "limit": limit, "has_more": len(rows) > limit}

    def dashboard_lite_payload(self):
        return {
            "summary": self.summary_payload(),
            "projects": {"rows": []},
            "sessions": {"rows": [], "total": 0, "page": int_query(self.query, "sessions_page", 1, 1, 100000), "per_page": int_query(self.query, "per_page", 25, 1, 100)},
            "turns": self.turns_payload(),
            "tools": {"rows": [], "total": 0, "page": int_query(self.query, "tools_page", 1, 1, 100000), "per_page": int_query(self.query, "per_page", 25, 1, 100), "output_tokens_total": 0},
            "subagents": {"rows": complete_subagent_rows([])},
        }

    def dashboard_payload(self):
        if (self.query.get("lite") or [""])[0] == "1":
            return self.dashboard_lite_payload()
        self.create_selected_turns_temp()
        turns = self.turns_payload()
        summary = dict(
            self.con.execute(
                f"""
                select count(*) turns,
                       coalesce(sum(total_tokens), 0) total_tokens,
                       coalesce(sum(input_tokens), 0) input_tokens,
                       coalesce(sum(cached_input_tokens), 0) cached_input_tokens,
                       coalesce(sum(non_cached_input_tokens), 0) non_cached_input_tokens,
                       coalesce(sum(output_tokens), 0) output_tokens,
                       coalesce(sum(reasoning_output_tokens), 0) reasoning_output_tokens,
                       coalesce(sum(model_call_count), 0) model_calls,
                       coalesce((
                         select sum(c.calls)
                         from tool_call_summaries c
                         join selected_turns st on st.session_id = c.session_id and st.turn_id = c.turn_id
                       ), 0) tool_calls,
                       {self.cost_sum_expression(zero_when_empty=True)} weighted_credits,
                       sum(case when weighted_credits is null then 1 else 0 end) unpriced_turns,
                       case when sum(input_tokens) > 0 then cast(sum(cached_input_tokens) as real) / sum(input_tokens) else 0 end cached_ratio
                from selected_turns
                """
            ).fetchone()
        )
        summary["unavailable_turns"] = self.unavailable_turn_count()
        summary["cost_complete"] = int(summary.get("unpriced_turns") or 0) == 0
        projects = rows_to_dicts(
            self.con.execute(
                f"select coalesce(project,'') project, count(*) turns, sum(total_tokens) raw, {self.cost_sum_expression()} credits from selected_turns group by coalesce(project,'') order by credits is null, credits desc limit 20"
            )
        )
        session_total = int(
            self.con.execute(
                """
                select count(*)
                from (
                  select 1
                  from selected_turns
                  group by session_id
                )
                """
            ).fetchone()[0]
            or 0
        )
        session_page, session_per_page, session_offset = self.first_column_page("sessions_page", session_total)
        session_order = self.session_order_clause()
        sessions = rows_to_dicts(
            self.con.execute(
                f"""
                select *
                from (
                  select s.session_id,
                         coalesce(max(nullif(thread_name,'')), '') thread_name,
                         coalesce(
                           (select st.project from selected_turns st
                            where st.session_id = s.session_id
                            order by st.started_at_unix desc, st.turn_id desc
                            limit 1),
                           ''
                         ) project,
                         coalesce(
                           (select st.cwd from selected_turns st
                            where st.session_id = s.session_id
                            order by st.started_at_unix desc, st.turn_id desc
                            limit 1),
                           ''
                         ) cwd,
                         count(*) turns,
                         sum(total_tokens) raw,
                         {self.cost_sum_expression()} credits,
                         max(started_at_unix) latest_started_at_unix
                  from selected_turns s
                  group by s.session_id
                )
                order by {session_order}
                limit ? offset ?
                """,
                (session_per_page, session_offset),
            )
        )
        tool_total = int(
            self.con.execute(
                """
                select count(*)
                from (
                  select 1
                  from tool_call_summaries c
                  join selected_turns t on t.session_id = c.session_id and t.turn_id = c.turn_id
                  group by c.tool_name
                )
                """
            ).fetchone()[0]
            or 0
        )
        tool_output_total = int(
            self.con.execute(
                """
                select coalesce(sum(c.output_tokens), 0)
                from tool_call_summaries c
                join selected_turns t on t.session_id = c.session_id and t.turn_id = c.turn_id
                """
            ).fetchone()[0]
            or 0
        )
        tool_page, tool_per_page, tool_offset = self.first_column_page("tools_page", tool_total)
        tool_order = self.tool_order_clause()
        tools = rows_to_dicts(
            self.con.execute(
                f"""
                select c.tool_name,
                       coalesce(sum(c.calls), 0) calls,
                       coalesce(sum(c.output_chars), 0) output_chars,
                       coalesce(sum(coalesce(c.output_reported_tokens,0)), 0) reported_tokens,
                       coalesce(sum(c.output_tokens), 0) output_tokens
                from tool_call_summaries c
                join selected_turns t on t.session_id = c.session_id and t.turn_id = c.turn_id
                group by c.tool_name
                order by {tool_order}
                limit ? offset ?
                """,
                (tool_per_page, tool_offset),
            )
        )
        subagents = rows_to_dicts(
            self.con.execute(
                """
                select r.confidence,
                       count(*) rows,
                       sum(r.child_total_tokens) child_raw,
                       case when count(r.child_weighted_credits)=count(*) then sum(r.child_weighted_credits) end child_credits
                from task_rollups r
                join selected_turns t on t.session_id = r.parent_session_id and t.turn_id = r.parent_turn_id
                group by r.confidence
                order by child_credits desc
                """
            )
        )
        return {
            "summary": summary,
            "projects": {"rows": projects},
            "sessions": {"rows": sessions, "total": session_total, "page": session_page, "per_page": session_per_page},
            "turns": turns,
            "tools": {
                "rows": tools,
                "total": tool_total,
                "page": tool_page,
                "per_page": tool_per_page,
                "output_tokens_total": tool_output_total,
            },
            "subagents": {"rows": self.subagent_rows_ordered(subagents)},
        }

    def session_detail_payload(self):
        self.create_selected_turns_temp()
        selected_session_id = (self.query.get("selected_session_id") or [""])[0]
        if not selected_session_id:
            first = self.con.execute(
                """
                select session_id
                from selected_turns
                group by session_id
                order by sum(weighted_credits) is null, sum(weighted_credits) desc, max(started_at_unix) desc, session_id desc
                limit 1
                """
            ).fetchone()
            selected_session_id = first["session_id"] if first else ""
        self.con.execute("drop table if exists temp.session_turns")
        self.con.execute(
            "create temp table session_turns as select * from selected_turns where session_id = ?",
            (selected_session_id,),
        )
        self.con.execute("create index idx_session_turns_turn on session_turns(session_id, turn_id)")
        session_turn_columns = {str(row[1]) for row in self.con.execute("pragma table_info(session_turns)")}
        workflow_expr = "coalesce(workflow,'')" if "workflow" in session_turn_columns else "''"
        category_expr = "coalesce(category,'')" if "category" in session_turn_columns else "''"
        summary = dict(
            self.con.execute(
                f"""
                select ? session_id,
                       coalesce(max(nullif(thread_name,'')), '') thread_name,
                       coalesce(
                         (select st.project from session_turns st
                          order by st.started_at_unix desc, st.turn_id desc
                          limit 1),
                         ''
                       ) project,
                       coalesce(
                         (select st.cwd from session_turns st
                          order by st.started_at_unix desc, st.turn_id desc
                          limit 1),
                         ''
                       ) cwd,
                       count(*) turns,
                       coalesce(sum(total_tokens), 0) raw,
                       {self.cost_sum_expression(zero_when_empty=True)} credits,
                       coalesce(sum(model_call_count), 0) model_calls,
                       coalesce(sum(non_cached_input_tokens), 0) non_cached_input_tokens,
                       case when sum(input_tokens) > 0 then cast(sum(cached_input_tokens) as real) / sum(input_tokens) else 0 end cached_ratio
                from session_turns
                """,
                (selected_session_id,),
            ).fetchone()
        )
        workflows = rows_to_dicts(
            self.con.execute(
                f"""
                select {workflow_expr} workflow,
                       {category_expr} category,
                       count(*) turns,
                       coalesce(sum(total_tokens), 0) raw,
                       {self.cost_sum_expression(zero_when_empty=True)} credits
                from session_turns
                group by {workflow_expr}, {category_expr}
                order by credits desc
                limit 12
                """
            )
        )
        tools = rows_to_dicts(
            self.con.execute(
                """
                select c.tool_name,
                       coalesce(sum(c.calls), 0) calls,
                       coalesce(sum(c.output_tokens), 0) output_tokens
                from tool_call_summaries c
                join session_turns t on t.session_id = c.session_id and t.turn_id = c.turn_id
                group by c.tool_name
                order by output_tokens desc
                limit 8
                """
            )
        )
        turns = rows_to_dicts(
            self.con.execute(
                """
                select session_id, turn_id, prompt_preview, turn_status,
                       weighted_credits credits, total_tokens raw
                from session_turns
                order by weighted_credits desc
                limit 8
                """
            )
        )
        subagents = rows_to_dicts(
            self.con.execute(
                """
                select r.confidence,
                       count(*) rows,
                       sum(r.child_total_tokens) child_raw,
                       case when count(r.child_weighted_credits)=count(*) then sum(r.child_weighted_credits) end child_credits
                from task_rollups r
                join session_turns t on t.session_id = r.parent_session_id and t.turn_id = r.parent_turn_id
                group by r.confidence
                order by child_credits desc
                """
            )
        )
        return {
            "summary": summary,
            "workflows": workflows,
            "tools": tools,
            "turns": turns,
            "subagents": complete_subagent_rows(subagents),
        }

    def summary_payload(self):
        cte, selected_args = self.selected_turns_cte()
        row = self.con.execute(
            f"""
            {cte}
            select count(*) turns,
                   coalesce(sum(total_tokens), 0) total_tokens,
                   coalesce(sum(input_tokens), 0) input_tokens,
                   coalesce(sum(cached_input_tokens), 0) cached_input_tokens,
                   coalesce(sum(non_cached_input_tokens), 0) non_cached_input_tokens,
                   coalesce(sum(output_tokens), 0) output_tokens,
                   coalesce(sum(reasoning_output_tokens), 0) reasoning_output_tokens,
                   coalesce(sum(model_call_count), 0) model_calls,
                   coalesce((
                     select sum(c.calls)
                     from tool_call_summaries c
                     join selected_turns st on st.session_id = c.session_id and st.turn_id = c.turn_id
                   ), 0) tool_calls,
                   {self.cost_sum_expression(zero_when_empty=True)} weighted_credits,
                   sum(case when weighted_credits is null then 1 else 0 end) unpriced_turns,
                   case when sum(input_tokens) > 0 then cast(sum(cached_input_tokens) as real) / sum(input_tokens) else 0 end cached_ratio
            from selected_turns
            """,
            selected_args,
        ).fetchone()
        result = dict(row)
        result["unavailable_turns"] = self.unavailable_turn_count()
        result["cost_complete"] = int(result.get("unpriced_turns") or 0) == 0
        return result

    def unavailable_turn_count(self) -> int:
        if "token_resolution_status" not in self.turn_columns:
            return 0
        where, args = self.filters()
        return int(
            self.con.execute(
                f"select count(*) from turns where {where} and token_resolution_status='unavailable'",
                args,
            ).fetchone()[0]
            or 0
        )

    def projects_payload(self):
        cte, selected_args = self.selected_turns_cte()
        rows = rows_to_dicts(
            self.con.execute(
                f"{cte} select coalesce(project,'') project, count(*) turns, sum(total_tokens) raw, {self.cost_sum_expression()} credits from selected_turns group by coalesce(project,'') order by credits is null, credits desc limit 20",
                selected_args,
            )
        )
        return {"rows": rows}

    def sessions_payload(self):
        self.create_selected_turns_temp()
        total = int(
            self.con.execute(
                """
                select count(*)
                from (
                  select 1
                  from selected_turns
                  group by session_id
                )
                """
            ).fetchone()[0]
            or 0
        )
        page, per_page, offset = self.first_column_page("sessions_page", total)
        session_order = self.session_order_clause()
        rows = rows_to_dicts(
            self.con.execute(
                f"""
                select *
                from (
                  select s.session_id,
                         coalesce(max(nullif(thread_name,'')), '') thread_name,
                         coalesce(
                           (select st.project from selected_turns st
                            where st.session_id = s.session_id
                            order by st.started_at_unix desc, st.turn_id desc
                            limit 1),
                           ''
                         ) project,
                         coalesce(
                           (select st.cwd from selected_turns st
                            where st.session_id = s.session_id
                            order by st.started_at_unix desc, st.turn_id desc
                            limit 1),
                           ''
                         ) cwd,
                         count(*) turns,
                         sum(total_tokens) raw,
                         {self.cost_sum_expression()} credits,
                         max(started_at_unix) latest_started_at_unix
                  from selected_turns s
                  group by s.session_id
                )
                order by {session_order}
                limit ? offset ?
                """,
                (per_page, offset),
            )
        )
        return {"rows": rows, "total": total, "page": page, "per_page": per_page}

    def project_options_payload(self):
        rows = rows_to_dicts(
            self.con.execute(
                """
                select coalesce(project,'') project,
                       count(*) turns,
                       {cost_sum} credits
                from turns
                where {eligible}
                group by coalesce(project,'')
                order by credits desc
                """.format(
                    eligible=self.analytics_eligible_predicate(),
                    cost_sum=self.cost_sum_expression(),
                )
            )
        )
        return {"rows": rows}

    def categories_payload(self):
        cte, selected_args = self.selected_turns_cte()
        rows = rows_to_dicts(
            self.con.execute(
                f"{cte} select category, workflow, count(*) turns, sum(total_tokens) raw, {self.cost_sum_expression()} credits from selected_turns group by category, workflow order by credits is null, credits desc limit 24",
                selected_args,
            )
        )
        return {"rows": rows}

    def tools_payload(self):
        self.create_selected_turns_temp()
        self.con.execute("drop table if exists temp.selected_tool_rollups")
        self.con.execute(
            """
            create temp table selected_tool_rollups as
            select c.tool_name,
                   coalesce(sum(c.calls),0) calls,
                   coalesce(sum(c.output_chars),0) output_chars,
                   coalesce(sum(coalesce(c.output_reported_tokens,0)),0) reported_tokens,
                   coalesce(sum(c.output_tokens),0) output_tokens
            from tool_call_summaries c
            join selected_turns t on t.session_id = c.session_id and t.turn_id = c.turn_id
            group by c.tool_name
            """
        )
        self.con.execute("create index idx_selected_tool_rollups_output on selected_tool_rollups(output_tokens desc, tool_name)")
        total = int(self.con.execute("select count(*) from selected_tool_rollups").fetchone()[0] or 0)
        output_tokens_total = int(self.con.execute("select coalesce(sum(output_tokens), 0) from selected_tool_rollups").fetchone()[0] or 0)
        page, per_page, offset = self.first_column_page("tools_page", total)
        tool_order = self.tool_order_clause()
        rows = rows_to_dicts(
            self.con.execute(
                f"""
                select tool_name,
                       calls,
                       output_chars,
                       reported_tokens,
                       output_tokens
                from selected_tool_rollups
                order by {tool_order}
                limit ? offset ?
                """,
                (per_page, offset),
            )
        )
        return {"rows": rows, "total": total, "page": page, "per_page": per_page, "output_tokens_total": output_tokens_total}

    def tool_payload(self):
        tool_name = (self.query.get("tool_name") or [""])[0]
        if not tool_name:
            raise ApiError("tool_name_required", 400)
        self.create_selected_turns_temp()
        self.con.execute("drop table if exists temp.selected_tool_detail_summaries")
        self.con.execute(
            """
            create temp table selected_tool_detail_summaries as
            select c.tool_name,
                   c.session_id,
                   c.turn_id,
                   coalesce(t.thread_name,'') thread_name,
                   coalesce(t.project,'') project,
                   coalesce(t.cwd,'') cwd,
                   t.started_at_unix,
                   coalesce(c.calls,0) calls,
                   coalesce(c.output_chars,0) output_chars,
                   coalesce(c.output_reported_tokens,0) reported_tokens,
                   coalesce(c.output_tokens,0) output_tokens,
                   coalesce(c.total_duration_ms,0) total_duration_ms
            from tool_call_summaries c
            join selected_turns t on t.session_id = c.session_id and t.turn_id = c.turn_id
            where c.tool_name = ?
            """,
            (tool_name,),
        )
        self.con.execute("create index idx_selected_tool_detail_summaries_session on selected_tool_detail_summaries(session_id)")
        self.con.execute("drop table if exists temp.selected_tool_detail_sessions")
        self.con.execute(
            """
            create temp table selected_tool_detail_sessions as
            select d.session_id,
                   coalesce(max(nullif(d.thread_name,'')), '') thread_name,
                   coalesce(max(nullif(d.project,'')), '') project,
                   coalesce(
                     (select latest.cwd from selected_tool_detail_summaries latest
                      where latest.session_id = d.session_id
                      order by latest.started_at_unix desc, latest.turn_id desc
                      limit 1),
                     ''
                   ) cwd,
                   coalesce(sum(d.calls),0) calls,
                   coalesce(sum(d.output_chars),0) output_chars,
                   coalesce(sum(d.reported_tokens),0) reported_tokens,
                   coalesce(sum(d.output_tokens),0) output_tokens
            from selected_tool_detail_summaries d
            group by d.session_id
            """
        )
        self.con.execute("create index idx_selected_tool_detail_sessions_output on selected_tool_detail_sessions(output_tokens desc, session_id desc)")
        self.con.execute("drop table if exists temp.selected_tool_detail_samples")
        self.con.execute(
            """
            create temp table selected_tool_detail_samples as
            select c.session_id,
                   c.turn_id,
                   coalesce(t.thread_name,'') thread_name,
                   coalesce(t.project,'') project,
                   coalesce(t.cwd,'') cwd,
                   t.prompt_preview,
                   c.output_chars,
                   coalesce(c.output_reported_tokens,0) output_reported_tokens,
                   c.output_tokens,
                   c.status,
                   c.duration_ms
            from tool_call_samples c
            join selected_turns t on t.session_id = c.session_id and t.turn_id = c.turn_id
            where c.tool_name = ?
            """,
            (tool_name,),
        )
        self.con.execute("create index idx_selected_tool_detail_samples_output on selected_tool_detail_samples(output_tokens desc)")
        summary = self.con.execute(
            """
            select tool_name,
                   coalesce(sum(calls),0) calls,
                   coalesce(sum(output_chars),0) output_chars,
                   coalesce(sum(reported_tokens),0) reported_tokens,
                   coalesce(sum(output_tokens),0) output_tokens,
                   coalesce(cast(sum(output_chars) as real) / nullif(sum(calls),0), 0) avg_output_chars,
                   coalesce(cast(sum(output_tokens) as real) / nullif(sum(calls),0), 0) avg_output_tokens,
                   coalesce(cast(sum(total_duration_ms) as real) / nullif(sum(calls),0), 0) avg_duration_ms
            from selected_tool_detail_summaries
            group by tool_name
            """
        ).fetchone()
        if summary is None:
            raise ApiError("tool_not_found", 404)
        sessions = rows_to_dicts(
            self.con.execute(
                """
                select session_id,
                       thread_name,
                       project,
                       cwd,
                       calls,
                       output_chars,
                       reported_tokens,
                       output_tokens
                from selected_tool_detail_sessions
                order by output_tokens desc, session_id desc
                limit 12
                """,
            )
        )
        calls = rows_to_dicts(
            self.con.execute(
                """
                select session_id, turn_id, thread_name, project, cwd, prompt_preview, output_chars,
                       output_reported_tokens,
                       output_tokens,
                       status, duration_ms
                from selected_tool_detail_samples
                order by output_tokens desc
                limit 10
                """,
            )
        )
        return {"summary": dict(summary), "sessions": sessions, "calls": calls}

    def subagents_payload(self):
        cte, rollup_args = self.selected_rollups_cte()
        rows = rows_to_dicts(
            self.con.execute(
                f"""
                {cte}
                select confidence,
                       count(*) rows,
                       sum(child_total_tokens) child_raw,
                       case when count(child_weighted_credits)=count(*) then sum(child_weighted_credits) end child_credits
                from selected_rollups
                group by confidence
                order by child_credits desc
                """,
                rollup_args,
            )
        )
        return {"rows": self.subagent_rows_ordered(rows)}

    def subagent_payload(self):
        confidence = (self.query.get("confidence") or [""])[0]
        if confidence not in SUBAGENT_CONFIDENCE_ORDER:
            raise ApiError("confidence_required", 400)
        cte, selected_args = self.selected_rollups_cte()
        summary = dict(
            self.con.execute(
                f"""
                {cte}
                select ? confidence,
                       count(*) rows,
                       coalesce(sum(child_total_tokens),0) child_raw,
                       case when count(child_weighted_credits)=count(*) then coalesce(sum(child_weighted_credits),0) end child_credits
                from selected_rollups
                where confidence = ?
                """,
                [*selected_args, confidence, confidence],
            ).fetchone()
        )
        sessions = rows_to_dicts(
            self.con.execute(
                f"""
                {cte}
                select session_id,
                       coalesce(max(nullif(thread_name,'')), '') thread_name,
                       coalesce(max(nullif(project,'')), '') project,
                       coalesce(max(cwd), '') cwd,
                       count(*) rows,
                       coalesce(sum(child_total_tokens),0) child_raw,
                       case when count(child_weighted_credits)=count(*) then coalesce(sum(child_weighted_credits),0) end child_credits
                from selected_rollups
                where confidence = ?
                group by session_id
                order by child_credits desc, session_id desc
                limit 12
                """,
                [*selected_args, confidence],
            )
        )
        rows = rows_to_dicts(
            self.con.execute(
                f"""
                {cte}
                select session_id, thread_name, project, cwd, parent_session_id, parent_turn_id, child_session_id, child_agent_role,
                       child_agent_nickname, child_started_at,
                       prompt_preview, child_total_tokens child_raw,
                       child_weighted_credits child_credits
                from selected_rollups
                where confidence = ?
                order by child_weighted_credits desc
                limit 20
                """,
                [*selected_args, confidence],
            )
        )
        return {"summary": summary, "sessions": sessions, "rows": rows}

    def turn_payload(self):
        session_id = (self.query.get("session_id") or [""])[0]
        turn_id = (self.query.get("turn_id") or [""])[0]
        turn_where, turn_args = self.filters()
        resolution_columns = self.resolution_list_columns()
        cost_rate_status = "cost_rate_status" if "cost_rate_status" in self.turn_columns else "'configured' cost_rate_status"
        cost_rate_effective_from = "cost_rate_effective_from" if "cost_rate_effective_from" in self.turn_columns else "null cost_rate_effective_from"
        turn = self.con.execute(
            f"""
            select session_id, turn_id, captured_at, started_at, stopped_at, cwd, project, thread_name, model, reasoning_effort,
                   turn_status, {resolution_columns},
                   estimated, prompt_preview, prompt_chars, prompt_lines, code_block_chars,
                   assistant_chars, input_tokens, cached_input_tokens, non_cached_input_tokens, output_tokens,
                   reasoning_output_tokens, total_tokens, cached_ratio, model_call_count,
                   weighted_credits, {cost_rate_status}, {cost_rate_effective_from}, category, workflow
            from turns
            where session_id=? and turn_id=? and {turn_where}
            """,
            [session_id, turn_id, *turn_args],
        ).fetchone()
        if turn is None:
            raise ApiError("turn_not_found", 404)
        model_summary_row = self.con.execute(
            """
            select calls, input_tokens, cached_input_tokens, non_cached_input_tokens, output_tokens,
                   reasoning_output_tokens, total_tokens, weighted_credits, max_total_tokens,
                   max_output_tokens, first_call_index, last_call_index
            from model_call_summaries
            where session_id=? and turn_id=?
            """,
            (session_id, turn_id),
        ).fetchone()
        model_summary = dict(model_summary_row) if model_summary_row is not None else {
            "calls": int(turn["model_call_count"] or 0),
            "input_tokens": int(turn["input_tokens"] or 0),
            "cached_input_tokens": int(turn["cached_input_tokens"] or 0),
            "non_cached_input_tokens": int(turn["non_cached_input_tokens"] or 0),
            "output_tokens": int(turn["output_tokens"] or 0),
            "reasoning_output_tokens": int(turn["reasoning_output_tokens"] or 0),
            "total_tokens": int(turn["total_tokens"] or 0),
            "weighted_credits": None if turn["weighted_credits"] is None else float(turn["weighted_credits"]),
            "max_total_tokens": 0,
            "max_output_tokens": 0,
            "first_call_index": None,
            "last_call_index": None,
        }
        tool_summaries = rows_to_dicts(
            self.con.execute(
                """
                select tool_name, tool_namespace, calls, output_chars, output_reported_tokens,
                       output_tokens, failed_calls, total_duration_ms, max_duration_ms,
                       max_output_tokens
                from tool_call_summaries
                where session_id=? and turn_id=?
                order by output_tokens desc, calls desc, tool_name
                """,
                (session_id, turn_id),
            )
        )
        tool_call_total = self.con.execute(
            "select coalesce(sum(calls),0) from tool_call_summaries where session_id=? and turn_id=?",
            (session_id, turn_id),
        ).fetchone()[0]
        rollups = rows_to_dicts(
            self.con.execute(
                """
                select child_session_id, child_agent_role, child_agent_nickname, child_started_at,
                       confidence, own_total_tokens, child_total_tokens, total_tokens,
                       own_weighted_credits, child_weighted_credits, total_weighted_credits
                from task_rollups
                where parent_session_id=? and parent_turn_id=?
                order by child_weighted_credits desc
                """,
                (session_id, turn_id),
            )
        )
        return {
            "turn": dict(turn),
            "model_call_summary": model_summary,
            "tool_call_summary": {"rows": tool_summaries},
            "model_call_total": int(model_summary.get("calls") or 0),
            "tool_call_total": tool_call_total,
            "subagents": rollups,
        }

    def payload(self, path: str) -> dict[str, Any]:
        routes = {
            "/api/dashboard": self.dashboard_payload,
            "/api/session-detail": self.session_detail_payload,
            "/api/summary": self.summary_payload,
            "/api/projects": self.projects_payload,
            "/api/sessions": self.sessions_payload,
            "/api/project-options": self.project_options_payload,
            "/api/session-options": self.session_options_payload,
            "/api/categories": self.categories_payload,
            "/api/turns": self.turns_payload,
            "/api/tools": self.tools_payload,
            "/api/tool": self.tool_payload,
            "/api/subagents": self.subagents_payload,
            "/api/subagent": self.subagent_payload,
            "/api/turn": self.turn_payload,
        }
        handler = routes.get(path)
        if handler is None:
            raise ApiError("not_found", 404)
        return handler()
