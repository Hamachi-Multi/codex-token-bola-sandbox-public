from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, os, pathlib, stat, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, os, pathlib, stat, tempfile, unittest


class RetentionPrunedStoreTests(unittest.TestCase):
    def test_writable_store_uses_owner_only_permissions_under_common_umask(self) -> None:
        store = load_module("retention_pruned_store_permissions_test", ROOT / "scripts" / "retention_pruned_store.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            previous_umask = os.umask(0o022)
            try:
                store.stage_rows(base, [{"session_id": "session", "turn_id": "turn", "captured_at_unix": 1.0}])
            finally:
                os.umask(previous_umask)
            state = base / "state"
            database = store.database_path(base)

            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            with store._connection(database, writable=True) as con:
                store.ensure_schema(con)
                con.commit()
                for sidecar in (database.with_name(f"{database.name}-wal"), database.with_name(f"{database.name}-shm")):
                    self.assertTrue(sidecar.exists())
                    self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)

    def test_writable_store_tightens_existing_permissions(self) -> None:
        store = load_module("retention_pruned_store_permission_upgrade_test", ROOT / "scripts" / "retention_pruned_store.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            store.stage_rows(base, [{"session_id": "session", "turn_id": "turn", "captured_at_unix": 1.0}])
            state = base / "state"
            database = store.database_path(base)
            state.chmod(0o755)
            database.chmod(0o644)

            store.commit_stage(base, "missing-job")

            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

    def test_read_only_inspection_does_not_change_permissions(self) -> None:
        store = load_module("retention_pruned_store_read_permission_test", ROOT / "scripts" / "retention_pruned_store.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            store.stage_rows(base, [{"session_id": "session", "turn_id": "turn", "captured_at_unix": 1.0}])
            state = base / "state"
            database = store.database_path(base)
            state.chmod(0o755)
            database.chmod(0o644)

            summary = store.inspect_summary(base)

            self.assertTrue(summary["valid"])
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o644)

    def test_writable_store_rejects_symlinked_state_directory(self) -> None:
        store = load_module("retention_pruned_store_state_symlink_test", ROOT / "scripts" / "retention_pruned_store.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            base = root / "output"
            base.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (base / "state").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(store.RetentionPrunedStoreError):
                store.stage_rows(base, [{"session_id": "session", "turn_id": "turn", "captured_at_unix": 1.0}])

            self.assertEqual(list(outside.iterdir()), [])

    def test_writable_store_rejects_symlinked_database(self) -> None:
        store = load_module("retention_pruned_store_database_symlink_test", ROOT / "scripts" / "retention_pruned_store.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            base = root / "output"
            state = base / "state"
            state.mkdir(parents=True)
            outside = root / "outside.sqlite"
            outside.write_text("do-not-touch", encoding="utf-8")
            store.database_path(base).symlink_to(outside)

            with self.assertRaises(store.RetentionPrunedStoreError):
                store.stage_rows(base, [{"session_id": "session", "turn_id": "turn", "captured_at_unix": 1.0}])

            self.assertEqual(outside.read_text(encoding="utf-8"), "do-not-touch")

    def test_inspect_summary_is_read_only_and_reports_pending_jobs(self) -> None:
        store = load_module("retention_pruned_store_inspect_test", ROOT / "scripts" / "retention_pruned_store.py")
        row = {"session_id": "session", "turn_id": "turn", "captured_at_unix": 1.0}
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            job_id = "retention:inspect"
            store.stage_rows(base, [row], pruned_at_unix=5.0, job_id=job_id)
            before = {
                str(path.relative_to(base)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in base.rglob("*")
                if path.is_file()
            }

            summary = store.inspect_summary(base)

            after = {
                str(path.relative_to(base)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in base.rglob("*")
                if path.is_file()
            }

        self.assertEqual(before, after)
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["pending_rows"], 1)
        self.assertEqual(summary["committed_rows"], 0)
        self.assertEqual(summary["pending_job_ids"], [job_id])
        self.assertEqual(summary["oldest_pending_pruned_at_unix"], 5.0)

    def test_inspect_summary_reads_committed_rows_from_active_wal(self) -> None:
        store = load_module("retention_pruned_store_active_wal_test", ROOT / "scripts" / "retention_pruned_store.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            database = store.database_path(base)
            with store._connection(database, writable=True) as con:
                store.ensure_schema(con)
                con.execute(
                    """
                    insert into pruned_turns(
                      session_id, turn_id, start_ts, stop_ts, captured_at_unix,
                      pruned_at_unix, last_required_at_unix, state, job_id
                    ) values (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    ("session", "turn", 1.0, 2.0, 1.0, 5.0, 5.0, "retention:active-wal"),
                )
                con.commit()

                summary = store.inspect_summary(base)

        self.assertTrue(summary["valid"])
        self.assertEqual(summary["schema_version"], store.SCHEMA_VERSION)
        self.assertEqual(summary["pending_rows"], 1)
        self.assertEqual(summary["pending_job_ids"], ["retention:active-wal"])

    def test_legacy_json_is_imported_and_archived(self) -> None:
        store = load_module("retention_pruned_store_legacy_test", ROOT / "scripts" / "retention_pruned_store.py")
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            state = base / "state"
            state.mkdir()
            legacy = state / "retention-pruned-turns.json"
            legacy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated_at_unix": 10.0,
                        "pruned_turns": [
                            {"session_id": "parent", "turn_id": "turn", "captured_at_unix": 5.0}
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = store.migrate_legacy(base)
            rows = store.rows_for_sessions(base, ["parent"])
            archives = list((base / "reports" / "migrations" / "retention-pruned-turns").iterdir())
            legacy_exists = legacy.exists()

        self.assertEqual(result, {"imported_rows": 1, "legacy_files": 1})
        self.assertEqual([(row["session_id"], row["turn_id"], row["state"]) for row in rows], [("parent", "turn", "committed")])
        self.assertFalse(legacy_exists)
        self.assertEqual(len(archives), 1)

    def test_compaction_keeps_required_and_pending_rows(self) -> None:
        store = load_module("retention_pruned_store_compaction_test", ROOT / "scripts" / "retention_pruned_store.py")
        row = lambda session, turn: {
            "session_id": session,
            "turn_id": turn,
            "captured_at_unix": 1.0,
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = pathlib.Path(tmp_dir)
            committed_job = store.stage_rows(base, [row("required", "turn"), row("expired", "turn")], pruned_at_unix=1.0)
            store.commit_stage(base, committed_job)
            pending_job = store.stage_rows(base, [row("pending", "turn")], pruned_at_unix=1.0)
            now = 40 * 24 * 60 * 60

            result = store.mark_required_and_compact(
                base,
                [("required", "turn")],
                now_unix=now,
                grace_seconds=30 * 24 * 60 * 60,
            )
            rows = store.snapshot_rows(base)

        self.assertEqual(result, {"required_rows": 1, "deleted_rows": 1})
        self.assertEqual(set(rows), {("required", "turn"), ("pending", "turn")})
        self.assertEqual(rows[("pending", "turn")]["state"], "pending")
        self.assertIsNotNone(pending_job)
