from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, mock, pathlib, sqlite3, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, mock, pathlib, sqlite3, tempfile, unittest


class BuildAnalyticsContextTests(unittest.TestCase):
    def test_snapshot_diff_tracks_thread_and_edge_add_update_delete(self) -> None:
        context = load_module("build_analytics_context_diff_test", ROOT / "scripts" / "build_analytics_context.py")
        con = sqlite3.connect(":memory:")
        try:
            con.execute(
                "create table source_context_threads (session_id text primary key, rollout_path text, created_at_ms integer, thread_name text, model text, reasoning_effort text, agent_role text, agent_nickname text)"
            )
            con.execute(
                "create table source_context_edges (child_session_id text primary key, parent_session_id text not null, status text)"
            )
            previous_threads = context.thread_projection(
                {
                    "keep": {"thread_name": "old"},
                    "remove": {"thread_name": "remove"},
                }
            )
            previous_edges = context.edge_projection(
                [("parent-old", "keep", "running"), ("parent-remove", "remove", "done")]
            )
            context.replace_snapshot(con, previous_threads, previous_edges)

            current_threads = context.thread_projection(
                {
                    "keep": {"thread_name": "new"},
                    "add": {"thread_name": "add"},
                }
            )
            current_edges = context.edge_projection(
                [("parent-new", "keep", "done"), ("parent-add", "add", "running")]
            )
            context.apply_snapshot_changes(
                con,
                previous_threads,
                current_threads,
                previous_edges,
                current_edges,
            )

            self.assertEqual(context.read_thread_snapshot(con), current_threads)
            self.assertEqual(context.read_edge_snapshot(con), current_edges)
            self.assertEqual(context.changed_keys(previous_threads, current_threads), {"keep", "remove", "add"})
            self.assertEqual(
                context.edge_change_sessions(previous_edges, current_edges),
                {"keep", "remove", "add", "parent-old", "parent-new", "parent-remove", "parent-add"},
            )
        finally:
            con.close()

    def test_edge_expansion_uses_old_and_new_graph_connectivity(self) -> None:
        context = load_module("build_analytics_context_closure_test", ROOT / "scripts" / "build_analytics_context.py")
        old_edges = context.edge_projection([("old-root", "child", "done")])
        new_edges = context.edge_projection([("new-root", "child", "running"), ("child", "grandchild", "running")])

        expanded = context.expand_sessions(
            {"child"},
            {*context.edge_rows(old_edges), *context.edge_rows(new_edges)},
        )

        self.assertEqual(expanded, {"old-root", "new-root", "child", "grandchild"})

    def test_incremental_context_sync_preserves_normalized_model_and_removes_stale_rollup(self) -> None:
        build = load_module("build_analytics_context_incremental_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            build.NORMALIZED_LOG = base / "normalized.jsonl"
            build.NORMALIZED_LOG.write_text("", encoding="utf-8")
            build.ANALYTICS_DB = base / "analytics.sqlite"
            build.STATE_DB = base / "missing-state.sqlite"
            build.SESSION_INDEX = base / "session-index.jsonl"
            build.RETENTION_PRUNED_TURNS_FILE = base / "retention.json"
            con = sqlite3.connect(build.ANALYTICS_DB)
            build.setup_db(con)
            build.upsert_turn_row(
                con,
                {"session_id": "parent", "turn_id": "p1", "captured_at": "2026-01-01T00:00:00Z", "model": "normalized-model", "usage": {"total_tokens": 10}},
                {"parent": {"thread_name": "old parent", "model": "old-context"}},
            )
            build.upsert_turn_row(
                con,
                {"session_id": "child", "turn_id": "c1", "captured_at": "2026-01-01T00:00:01Z", "usage": {"total_tokens": 20}},
                {"child": {"thread_name": "old child", "model": "old-context"}},
            )
            con.execute(
                "insert into task_rollups values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("parent", "p1", "child", None, None, None, 0, "old", 10, 20, 30, 10.0, 20.0, 30.0),
            )
            old_threads = build.build_analytics_context.thread_projection(
                {
                    "parent": {"thread_name": "old parent", "model": "old-context"},
                    "child": {"thread_name": "old child", "model": "old-context"},
                }
            )
            old_edges = build.build_analytics_context.edge_projection([("parent", "child", "done")])
            build.build_analytics_context.replace_snapshot(con, old_threads, old_edges)
            build.write_metadata(
                con,
                {
                    "context_snapshot_version": build.build_analytics_context.CONTEXT_SNAPSHOT_VERSION,
                    "analytics_schema_version": build.ANALYTICS_SCHEMA_VERSION,
                    "cost_rate_catalog_digest": build.COST_RATE_CATALOG.digest,
                },
            )
            con.commit()
            con.close()

            current_threads = {
                "parent": {"thread_name": "new parent", "model": "old-context"},
                "child": {"thread_name": "new child", "model": "old-context"},
            }
            with mock.patch.object(build, "read_threads", return_value=current_threads), mock.patch.object(build, "read_edges", return_value=[]):
                result = build.incremental_build(type("Args", (), {"turns_offset": 0})())

            self.assertIsNotNone(result)
            con = sqlite3.connect(build.ANALYTICS_DB)
            try:
                rows = con.execute(
                    "select session_id, thread_name, model, model_from_context from turns order by session_id"
                ).fetchall()
                rollup_count = con.execute("select count(*) from task_rollups").fetchone()[0]
                edge_count = con.execute("select count(*) from source_context_edges").fetchone()[0]
            finally:
                con.close()

        self.assertEqual(
            rows,
            [
                ("child", "new child", "old-context", 1),
                ("parent", "new parent", "normalized-model", 0),
            ],
        )
        self.assertEqual(rollup_count, 0)
        self.assertEqual(edge_count, 0)

    def test_context_model_change_requires_full_cost_rebuild(self) -> None:
        build = load_module("build_analytics_context_model_cost_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            build.NORMALIZED_LOG = base / "normalized.jsonl"
            build.NORMALIZED_LOG.write_text("", encoding="utf-8")
            build.ANALYTICS_DB = base / "analytics.sqlite"
            build.STATE_DB = base / "missing-state.sqlite"
            build.SESSION_INDEX = base / "session-index.jsonl"
            build.RETENTION_PRUNED_TURNS_FILE = base / "retention.json"
            con = sqlite3.connect(build.ANALYTICS_DB)
            build.setup_db(con)
            old_threads = {"child": {"thread_name": "child", "model": "gpt-5.5"}}
            build.upsert_turn_row(
                con,
                {"session_id": "child", "turn_id": "c1", "captured_at": "2026-06-01T00:00:00Z", "usage": {"total_tokens": 20}},
                old_threads,
            )
            build.build_analytics_context.replace_snapshot(
                con,
                build.build_analytics_context.thread_projection(old_threads),
                {},
            )
            build.write_metadata(
                con,
                {
                    "context_snapshot_version": build.build_analytics_context.CONTEXT_SNAPSHOT_VERSION,
                    "analytics_schema_version": build.ANALYTICS_SCHEMA_VERSION,
                    "cost_rate_catalog_digest": build.COST_RATE_CATALOG.digest,
                },
            )
            con.commit()
            con.close()

            current_threads = {"child": {"thread_name": "child", "model": "gpt-5.6-sol"}}
            with mock.patch.object(build, "read_threads", return_value=current_threads), mock.patch.object(build, "read_edges", return_value=[]):
                result = build.incremental_build(type("Args", (), {"turns_offset": 0})())

        self.assertIsNone(result)

    def test_incremental_build_falls_back_once_without_snapshot_version(self) -> None:
        build = load_module("build_analytics_context_upgrade_fallback_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            build.NORMALIZED_LOG = base / "normalized.jsonl"
            build.NORMALIZED_LOG.write_text("", encoding="utf-8")
            build.ANALYTICS_DB = base / "analytics.sqlite"
            con = sqlite3.connect(build.ANALYTICS_DB)
            build.setup_db(con)
            con.commit()
            con.close()

            with mock.patch.object(build, "read_threads", side_effect=AssertionError("source context must not be read")):
                result = build.incremental_build(type("Args", (), {"turns_offset": 0})())

        self.assertIsNone(result)

    def test_noop_context_snapshot_skips_rollup_rebuild(self) -> None:
        build = load_module("build_analytics_context_noop_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            build.NORMALIZED_LOG = base / "normalized.jsonl"
            build.NORMALIZED_LOG.write_text("", encoding="utf-8")
            build.ANALYTICS_DB = base / "analytics.sqlite"
            build.STATE_DB = base / "missing-state.sqlite"
            build.SESSION_INDEX = base / "session-index.jsonl"
            build.RETENTION_PRUNED_TURNS_FILE = base / "retention.json"
            threads = {"parent": {"thread_name": "parent"}, "child": {"thread_name": "child"}}
            edges = [("parent", "child", "done")]
            con = sqlite3.connect(build.ANALYTICS_DB)
            build.setup_db(con)
            build.build_analytics_context.replace_snapshot(
                con,
                build.build_analytics_context.thread_projection(threads),
                build.build_analytics_context.edge_projection(edges),
            )
            build.write_metadata(
                con,
                {
                    "context_snapshot_version": build.build_analytics_context.CONTEXT_SNAPSHOT_VERSION,
                    "analytics_schema_version": build.ANALYTICS_SCHEMA_VERSION,
                    "cost_rate_catalog_digest": build.COST_RATE_CATALOG.digest,
                    "applied_retention_fingerprint": "same",
                },
            )
            con.commit()
            con.close()

            with mock.patch.object(build, "read_threads", return_value=threads), mock.patch.object(build, "read_edges", return_value=edges), mock.patch.object(build, "retention_input_fingerprint", return_value="same"), mock.patch.object(build, "spawn_turn_contexts", side_effect=AssertionError("no rollup rebuild expected")):
                result = build.incremental_build(type("Args", (), {"turns_offset": 0})())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["context_changed_threads"], 0)
        self.assertEqual(result["context_changed_edges"], 0)
        self.assertEqual(result["context_affected_sessions"], 0)
        self.assertFalse(result["retention_context_changed"])

    def test_retention_change_rebuilds_connected_rollups_without_full_build(self) -> None:
        build = load_module("build_analytics_retention_context_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            build.NORMALIZED_LOG = base / "normalized.jsonl"
            build.NORMALIZED_LOG.write_text("", encoding="utf-8")
            build.ANALYTICS_DB = base / "analytics.sqlite"
            build.STATE_DB = base / "missing-state.sqlite"
            build.SESSION_INDEX = base / "session-index.jsonl"
            build.RETENTION_PRUNED_TURNS_FILE = base / "retention.json"
            threads = {"parent": {"thread_name": "parent"}, "child": {"thread_name": "child"}}
            edges = [("parent", "child", "done")]
            con = sqlite3.connect(build.ANALYTICS_DB)
            build.setup_db(con)
            build.build_analytics_context.replace_snapshot(
                con,
                build.build_analytics_context.thread_projection(threads),
                build.build_analytics_context.edge_projection(edges),
            )
            build.write_metadata(
                con,
                {
                    "context_snapshot_version": build.build_analytics_context.CONTEXT_SNAPSHOT_VERSION,
                    "analytics_schema_version": build.ANALYTICS_SCHEMA_VERSION,
                    "cost_rate_catalog_digest": build.COST_RATE_CATALOG.digest,
                    "applied_retention_fingerprint": "old",
                },
            )
            con.commit()
            con.close()

            observed: list[set[str] | None] = []

            def record_rollup_call(_con, _threads, _spawn, _usage, _ranges, affected_sessions=None, **_kwargs):
                observed.append(affected_sessions)

            with mock.patch.object(build, "read_threads", return_value=threads), mock.patch.object(build, "read_edges", return_value=edges), mock.patch.object(build, "retention_input_fingerprint", return_value="new"), mock.patch.object(build, "spawn_turn_contexts", return_value={}), mock.patch.object(build, "rebuild_task_rollups", side_effect=record_rollup_call):
                result = build.incremental_build(type("Args", (), {"turns_offset": 0})())

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(observed, [{"parent", "child"}])
        self.assertTrue(result["retention_context_changed"])
        self.assertEqual(result["analysis_mode"], "incremental")

    def test_child_task_start_accepts_current_and_legacy_payloads_and_caches_results(self) -> None:
        build = load_module("build_analytics_child_start_formats_test", ROOT / "scripts" / "build_analytics.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            current = pathlib.Path(tmp_dir) / "current.jsonl"
            legacy = pathlib.Path(tmp_dir) / "legacy.jsonl"
            current.write_text(
                json.dumps({"type": "event_msg", "timestamp": "2026-01-01T00:00:09Z", "payload": {"type": "task_started", "started_at": "2026-01-01T00:00:01Z"}}) + "\n",
                encoding="utf-8",
            )
            legacy.write_text(
                json.dumps({"type": "event_msg", "timestamp": "2026-01-01T00:00:02Z", "payload": {"msg": "task_started"}}) + "\n",
                encoding="utf-8",
            )
            cache = {}
            current_ts = build.child_task_started_ts({"rollout_path": str(current)}, cache)
            legacy_ts = build.child_task_started_ts({"rollout_path": str(legacy)}, cache)
            current.unlink()

            self.assertEqual(build.child_task_started_ts({"rollout_path": str(current)}, cache), current_ts)
            self.assertEqual(current_ts, build.parse_time("2026-01-01T00:00:01Z"))
            self.assertEqual(legacy_ts, build.parse_time("2026-01-01T00:00:02Z"))


if __name__ == "__main__":
    unittest.main()
