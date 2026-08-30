"""SQLite schema helpers for analytics builds."""

from __future__ import annotations

import sqlite3


ANALYTICS_SCHEMA_VERSION = 3


def setup_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        drop table if exists turns;
        drop table if exists model_call_summaries;
        drop table if exists tool_call_summaries;
        drop table if exists tool_call_samples;
        drop table if exists task_rollups;
        drop table if exists source_context_threads;
        drop table if exists source_context_edges;
        drop table if exists run_metadata;

        create table turns (
          session_id text not null,
          turn_id text not null,
          captured_at text,
          captured_at_unix real,
          started_at text,
          started_at_unix real,
          stopped_at text,
          cwd text,
          project text,
          thread_name text,
          model text,
          model_from_context integer not null default 0,
          reasoning_effort text,
          turn_status text,
          token_resolution_status text not null default 'resolved' check(token_resolution_status in ('resolved','pending','unavailable')),
          token_resolution_reason text,
          analytics_eligible integer not null default 1 check(analytics_eligible in (0,1)),
          estimated integer not null,
          schema_version integer,
          source_priority integer,
          prompt_preview text,
          prompt_sha256 text,
          prompt_chars integer,
          prompt_lines integer,
          code_block_chars integer,
          assistant_chars integer,
          input_tokens integer,
          cached_input_tokens integer,
          non_cached_input_tokens integer,
          output_tokens integer,
          reasoning_output_tokens integer,
          total_tokens integer,
          cached_ratio real,
          model_call_count integer,
          weighted_credits real,
          cost_pico_usd integer,
          cost_rate_status text not null default 'unconfigured' check(cost_rate_status in ('configured','unconfigured','unavailable')),
          cost_rate_effective_from text,
          category text,
          workflow text,
          transcript_path text,
          primary key (session_id, turn_id)
        );

        create table model_call_summaries (
          session_id text not null,
          turn_id text not null,
          calls integer,
          input_tokens integer,
          cached_input_tokens integer,
          non_cached_input_tokens integer,
          output_tokens integer,
          reasoning_output_tokens integer,
          total_tokens integer,
          weighted_credits real,
          max_total_tokens integer,
          max_output_tokens integer,
          first_call_index integer,
          last_call_index integer,
          primary key (session_id, turn_id)
        );

        create table tool_call_summaries (
          session_id text not null,
          turn_id text not null,
          tool_name text not null,
          tool_namespace text,
          calls integer,
          output_chars integer,
          output_reported_tokens integer,
          output_tokens integer,
          failed_calls integer,
          total_duration_ms integer,
          max_duration_ms integer,
          max_output_tokens integer,
          primary key (session_id, turn_id, tool_name, tool_namespace)
        );

        create table tool_call_samples (
          session_id text not null,
          turn_id text not null,
          call_id text not null,
          tool_name text not null,
          tool_namespace text,
          sample_reason text not null,
          sample_rank integer,
          started_at text,
          completed_at text,
          duration_ms integer,
          output_chars integer,
          output_reported_tokens integer,
          output_tokens integer,
          status text,
          exit_code integer,
          primary key (session_id, turn_id, call_id, sample_reason)
        );

        create table task_rollups (
          parent_session_id text,
          parent_turn_id text,
          child_session_id text,
          child_agent_role text,
          child_agent_nickname text,
          child_started_at text,
          child_started_unix real,
          confidence text,
          own_total_tokens integer,
          child_total_tokens integer,
          total_tokens integer,
          own_weighted_credits real,
          child_weighted_credits real,
          total_weighted_credits real,
          primary key (parent_session_id, parent_turn_id, child_session_id)
        );

        create table source_context_threads (
          session_id text primary key,
          rollout_path text,
          created_at_ms integer,
          thread_name text,
          model text,
          reasoning_effort text,
          agent_role text,
          agent_nickname text
        );

        create table source_context_edges (
          child_session_id text primary key,
          parent_session_id text not null,
          status text
        );

        create table run_metadata (
          key text primary key,
          value text
        );

        create index idx_model_call_summaries_turn on model_call_summaries(session_id, turn_id);
        create index idx_tool_call_summaries_turn on tool_call_summaries(session_id, turn_id);
        create index idx_tool_call_samples_tool on tool_call_samples(tool_name, output_tokens desc);
        create index idx_task_rollups_parent on task_rollups(parent_session_id, parent_turn_id);
        """
    )
    ensure_indexes(con)


def ensure_indexes(con: sqlite3.Connection) -> None:
    existing_turn_columns = {str(row[1]) for row in con.execute("pragma table_info(turns)")}
    if "thread_name" not in existing_turn_columns:
        con.execute("alter table turns add column thread_name text")
    if "schema_version" not in existing_turn_columns:
        con.execute("alter table turns add column schema_version integer")
    if "source_priority" not in existing_turn_columns:
        con.execute("alter table turns add column source_priority integer")
    if "model_from_context" not in existing_turn_columns:
        con.execute("alter table turns add column model_from_context integer not null default 0")
    if "started_at_unix" not in existing_turn_columns:
        con.execute("alter table turns add column started_at_unix real")
        con.execute(
            "update turns set started_at_unix = coalesce(cast(strftime('%s', started_at) as real), captured_at_unix)"
        )
        con.executescript(
            """
            drop index if exists idx_turns_latest_order;
            drop index if exists idx_turns_weighted_order;
            drop index if exists idx_turns_weighted_order_asc;
            drop index if exists idx_turns_project_latest_order;
            """
        )
    if "token_resolution_status" not in existing_turn_columns:
        con.execute("alter table turns add column token_resolution_status text not null default 'resolved'")
    if "token_resolution_reason" not in existing_turn_columns:
        con.execute("alter table turns add column token_resolution_reason text")
    if "analytics_eligible" not in existing_turn_columns:
        con.execute("alter table turns add column analytics_eligible integer not null default 1")
    if "cost_pico_usd" not in existing_turn_columns:
        con.execute("alter table turns add column cost_pico_usd integer")
    if "cost_rate_status" not in existing_turn_columns:
        con.execute("alter table turns add column cost_rate_status text not null default 'unconfigured'")
    if "cost_rate_effective_from" not in existing_turn_columns:
        con.execute("alter table turns add column cost_rate_effective_from text")
    con.executescript(
        """
        create table if not exists source_context_threads (
          session_id text primary key,
          rollout_path text,
          created_at_ms integer,
          thread_name text,
          model text,
          reasoning_effort text,
          agent_role text,
          agent_nickname text
        );
        create table if not exists source_context_edges (
          child_session_id text primary key,
          parent_session_id text not null,
          status text
        );
        create index if not exists idx_turns_captured_at_unix on turns(captured_at_unix);
        create index if not exists idx_turns_started_at_unix on turns(started_at_unix);
        create index if not exists idx_turns_latest_order on turns(started_at_unix desc, session_id desc, turn_id desc);
        create index if not exists idx_turns_weighted_order on turns(weighted_credits desc, started_at_unix desc, session_id desc, turn_id desc);
        create index if not exists idx_turns_weighted_order_asc on turns(weighted_credits asc, started_at_unix desc, session_id desc, turn_id desc);
        create index if not exists idx_turns_project on turns(project);
        create index if not exists idx_turns_project_captured_at_unix on turns(project, captured_at_unix);
        create index if not exists idx_turns_project_started_at_unix on turns(project, started_at_unix);
        create index if not exists idx_turns_project_latest_order on turns(project, started_at_unix desc, session_id desc, turn_id desc);
        create index if not exists idx_turns_thread_name on turns(thread_name);
        create index if not exists idx_turns_category on turns(category);
        create index if not exists idx_turns_analytics_eligible on turns(analytics_eligible, started_at_unix desc);
        """
    )
