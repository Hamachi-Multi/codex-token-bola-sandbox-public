"""SQLite schema helpers for analytics builds."""

from __future__ import annotations

import sqlite3


def setup_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        drop table if exists turns;
        drop table if exists model_call_summaries;
        drop table if exists tool_call_summaries;
        drop table if exists tool_call_samples;
        drop table if exists task_rollups;
        drop table if exists run_metadata;

        create table turns (
          session_id text not null,
          turn_id text not null,
          captured_at text,
          captured_at_unix real,
          started_at text,
          stopped_at text,
          cwd text,
          project text,
          thread_name text,
          model text,
          reasoning_effort text,
          turn_status text,
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
          uncached_input_equivalent real,
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
          output_preview text,
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
    con.executescript(
        """
        create index if not exists idx_turns_captured_at_unix on turns(captured_at_unix);
        create index if not exists idx_turns_latest_order on turns(captured_at_unix desc, session_id desc, turn_id desc);
        create index if not exists idx_turns_weighted_order on turns(weighted_credits desc, captured_at_unix desc, session_id desc, turn_id desc);
        create index if not exists idx_turns_weighted_order_asc on turns(weighted_credits asc, captured_at_unix desc, session_id desc, turn_id desc);
        create index if not exists idx_turns_project on turns(project);
        create index if not exists idx_turns_project_captured_at_unix on turns(project, captured_at_unix);
        create index if not exists idx_turns_project_latest_order on turns(project, captured_at_unix desc, session_id desc, turn_id desc);
        create index if not exists idx_turns_thread_name on turns(thread_name);
        create index if not exists idx_turns_category on turns(category);
        """
    )
