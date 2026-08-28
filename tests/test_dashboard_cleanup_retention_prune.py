from __future__ import annotations

try:
    from tests.support import (
        Any,
        DashboardFixtureMixin,
        ROOT,
        _raw_segment,
        _turn_raw,
        argparse,
        assert_retention_derived_outputs_unchanged,
        datetime,
        gzip,
        io,
        json,
        load_module,
        mock,
        pathlib,
        seed_retention_derived_outputs,
        sqlite3,
        subprocess,
        sys,
        tempfile,
        timezone,
        unittest,
    )
except ModuleNotFoundError:
    from support import (
        Any,
        DashboardFixtureMixin,
        ROOT,
        _raw_segment,
        _turn_raw,
        argparse,
        assert_retention_derived_outputs_unchanged,
        datetime,
        gzip,
        io,
        json,
        load_module,
        mock,
        pathlib,
        seed_retention_derived_outputs,
        sqlite3,
        subprocess,
        sys,
        tempfile,
        timezone,
        unittest,
    )


class DashboardCleanupRetentionPruneTests(DashboardFixtureMixin, unittest.TestCase):
    RETENTION_CUTOFF = "2026-05-20T00:00:00+00:00"
    LEGACY_CLEANUP_ROW_FIELDS = {
        "retention_role",
        "retention_impact",
        "delete_all_impact",
        "retention_summary",
        "delete_all_summary",
    }

    def assert_cleanup_row_uses_display_contract(self, row: dict[str, Any]) -> None:
        self.assertTrue(self.LEGACY_CLEANUP_ROW_FIELDS.isdisjoint(row))
        self.assertIn("display", row)
        self.assertIn("delete_all_display", row)
        for display in (row["display"], row["delete_all_display"]):
            if "action" in display:
                self.assertEqual(set(display.get("action_file_counts", {})), {"Delete", "Rewrite", "Rebuild"})

    def retention_prune_args(
        self,
        cli: Any,
        codex_dir: pathlib.Path,
        output: pathlib.Path,
        *,
        cutoff: str | None = None,
        preview_signature: str | None = None,
    ) -> argparse.Namespace:
        cutoff_text = cutoff or self.RETENTION_CUTOFF
        signature = preview_signature
        if signature is None:
            signature = cli.dashboard_cleanup.retention_preview_signature(codex_dir / "bola", cli.parse_cutoff(cutoff_text))
        return argparse.Namespace(
            codex_dir=str(codex_dir),
            output_dir=str(codex_dir / "bola"),
            output=str(output),
            cutoff=cutoff_text,
            preview_signature=signature,
        )

    def test_retention_apply_rejects_untracked_plan_before_mutation(self) -> None:
        cleanup = load_module("cleanup_retention_untracked_rejection_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            raw_prompt = raw_dir / "prompt-usage.raw.jsonl"
            raw_prompt.write_text(
                json.dumps(_turn_raw("s-old", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"})
                + "\n"
                + json.dumps(_turn_raw("s-new", "new", total=200) | {"captured_at": "2026-05-20T00:00:00+00:00"})
                + "\n",
                encoding="utf-8",
            )
            cutoff_unix = datetime(2026, 5, 10, tzinfo=timezone.utc).timestamp()
            plan = {
                "base": str(base),
                "cutoff": cutoff_unix,
                "segments": {"plans": [], "deleted_rows": 0, "scanned_rows": 0, "deleted_bytes": 0},
                "untracked": [{"path": str(raw_prompt), "deleted_rows": 1}],
                "pending_turn_state": {"targets": [], "deleted_files": 0, "deleted_bytes": 0},
                "pruned_turns": [],
            }
            before = raw_prompt.read_bytes()

            with self.assertRaisesRegex(cleanup._retention.raw_segments.ManifestError, "untracked retention plans are not supported"):
                cleanup.apply_delete_logs_older_than_plan(plan)
            job = cleanup.read_cleanup_retention_job(base)
            after = raw_prompt.read_bytes()

        self.assertEqual(after, before)
        self.assertIsNone(job)

    def test_pending_turn_state_plan_preserves_state_after_drift(self) -> None:
        cleanup = load_module("cleanup_pending_turn_state_drift_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            target = state_dir / ("a" * 32 + ".json")
            target.write_text(
                json.dumps({"record_type": "turn_start", "captured_at": "2026-05-01T00:00:00+00:00", "session_id": "s-old", "turn_id": "t-old"}) + "\n",
                encoding="utf-8",
            )
            cutoff_unix = datetime.fromisoformat("2026-05-20T00:00:00+00:00").timestamp()
            plan = cleanup._retention.plan_pending_turn_state_for_retention(base, cutoff_unix)
            target.write_text(
                json.dumps(
                    {
                        "record_type": "turn_start",
                        "captured_at": "2026-05-01T00:00:00+00:00",
                        "session_id": "s-replaced",
                        "turn_id": "t-replaced",
                        "extra": "changed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = cleanup._retention.apply_pending_turn_state_plan(plan)

            self.assertEqual(result["deleted_files"], 0)
            self.assertEqual(result["protected_files"], 1)
            self.assertTrue(target.exists())

    def test_reset_derived_outputs_rejects_normalized_symlink_escape(self) -> None:
        cleanup = load_module("cleanup_reset_derived_symlink_escape_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            external = pathlib.Path(tmp) / "external"
            analytics = base / "analytics"
            external.mkdir(parents=True)
            analytics.mkdir(parents=True)
            external_file = external / "prompt-usage.normalized.jsonl"
            external_file.write_text("external data\n", encoding="utf-8")
            (base / "normalized").symlink_to(external, target_is_directory=True)

            with self.assertRaises(ValueError):
                cleanup.reset_derived_outputs(base, analytics / "bola.sqlite")

            self.assertTrue(external_file.exists())

    def test_retention_prune_rejects_untracked_plan_before_reset(self) -> None:
        cli = load_module("cli_retention_untracked_drift_before_reset_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            raw_dir = base / "raw"
            state_dir = base / "state"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            raw_file = raw_dir / "prompt-usage.raw.jsonl"
            raw_file.write_text(
                json.dumps(_turn_raw("s-old", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"})
                + "\n"
                + json.dumps(_turn_raw("s-new", "new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"})
                + "\n",
                encoding="utf-8",
            )
            (state_dir / "raw-segments-manifest.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "segments": []}) + "\n", encoding="utf-8"
            )
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "current": {}}) + "\n", encoding="utf-8"
            )
            derived = seed_retention_derived_outputs(base)
            cutoff_unix = cli.parse_cutoff(self.RETENTION_CUTOFF)
            plan = {
                "base": str(base),
                "cutoff": cutoff_unix,
                "segments": {"plans": [], "deleted_rows": 0, "scanned_rows": 0, "deleted_bytes": 0},
                "untracked": [{"path": str(raw_file), "deleted_rows": 1}],
                "pending_turn_state": {"targets": [], "deleted_files": 0, "deleted_bytes": 0},
                "pruned_turns": [],
            }
            raw_file.write_text(json.dumps(_turn_raw("s-other", "other", total=300) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n", encoding="utf-8")

            with (
                mock.patch.object(cli.dashboard_cleanup, "plan_delete_logs_older_than", return_value=plan),
                mock.patch.object(cli.dashboard_cleanup, "reset_derived_outputs", side_effect=AssertionError("reset must not run after source drift")),
            ):
                with self.assertRaises(cli.raw_segments.ManifestError):
                    cli.retention_prune(self.retention_prune_args(cli, codex_dir, derived["db"]))

            assert_retention_derived_outputs_unchanged(self, derived)

    def test_retention_plan_validation_ignores_protected_pending_state_drift(self) -> None:
        cli = load_module("cli_retention_pending_state_protection_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "raw-segments-manifest.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "segments": []}) + "\n", encoding="utf-8"
            )
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "current": {}}) + "\n", encoding="utf-8"
            )
            target = state_dir / ("b" * 32 + ".json")
            target.write_text(
                json.dumps({"record_type": "turn_start", "captured_at": "2026-05-01T00:00:00+00:00", "session_id": "s-old", "turn_id": "t-old"}) + "\n",
                encoding="utf-8",
            )
            plan = cli.dashboard_cleanup.plan_delete_logs_older_than(base, cli.parse_cutoff(self.RETENTION_CUTOFF))
            target.write_text(
                json.dumps({"record_type": "turn_start", "captured_at": "2026-05-01T00:00:00+00:00", "session_id": "s-replaced", "turn_id": "t-replaced"})
                + "\n",
                encoding="utf-8",
            )

            result = cli.dashboard_cleanup.validate_delete_logs_older_than_plan(plan)

            self.assertEqual(result, {"ok": True})
            self.assertTrue(target.exists())

    def test_segment_retention_plan_does_not_store_retained_payload_and_apply_rewrites(self) -> None:
        raw_segments = load_module("raw_segment_retention_stream_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            segment_path = archive / "prompt-usage.raw.jsonl.20260501000000.20260520000000.1.jsonl.gz"
            old_payload = json.dumps(_turn_raw("s-old", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            new_payload = json.dumps(_turn_raw("s-new", "new", total=200) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n"
            payload = (old_payload + new_payload).encode("utf-8")
            with gzip.open(segment_path, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(
                segment_path,
                payload=payload,
                min_time=datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp(),
                max_time=datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp(),
                rows=2,
            )
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})

            real_open = raw_segments._retention.open_segment_payload
            open_calls = 0

            def counted_open(path: pathlib.Path):
                nonlocal open_calls
                open_calls += 1
                return real_open(path)

            with mock.patch.object(raw_segments._retention, "open_segment_payload", side_effect=counted_open):
                plan = raw_segments.plan_segments_older_than(base, datetime(2026, 5, 10, tzinfo=timezone.utc).timestamp())
                result = raw_segments.apply_segment_plans(base, plan)
            retained_segments = raw_segments.read_manifest(base)["segments"]
            retained_path = pathlib.Path(retained_segments[0]["path"])
            with gzip.open(retained_path, "rt", encoding="utf-8") as handle:
                remaining = [json.loads(line)["turn_id"] for line in handle]

        self.assertNotIn("retained_payload", plan["plans"][0])
        self.assertEqual(open_calls, 1)
        self.assertEqual(result["rewritten_files"], 1)
        self.assertEqual(remaining, ["new"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "ru_maxrss uses KiB thresholds on Linux")
    def test_segment_retention_streaming_keeps_rss_bounded(self) -> None:
        raw_segments = load_module("raw_segment_retention_rss_fixture_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            segment_path = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.rss.jsonl.gz"
            line = (
                json.dumps(
                    {
                        "record_type": "turn_usage_raw",
                        "captured_at": "2026-05-01T00:00:00+00:00",
                        "padding": "x" * 900,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            rows = 45_000
            payload = line * rows
            with gzip.open(segment_path, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(
                segment_path,
                payload=payload,
                min_time=datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp(),
                max_time=datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp(),
                rows=rows,
            )
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})
            script = """
import pathlib
import resource
import sys
sys.path.insert(0, sys.argv[1])
import raw_segments
base = pathlib.Path(sys.argv[2])
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
plan = raw_segments.plan_segments_older_than(base, float(sys.argv[3]))
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
raw_segments.discard_segment_plan_artifacts(plan)
print(after - before)
"""
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(ROOT / "scripts"),
                    str(base),
                    str(datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp()),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        rss_growth_kib = int(result.stdout.strip())
        self.assertLess(rss_growth_kib, 30 * 1024)

    def test_retention_plan_batches_pruned_turn_state_without_collecting_rows(self) -> None:
        cleanup = load_module("cleanup_retention_pruned_batch_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = cleanup._retention.raw_segments.ensure_current_segment(
                base,
                kind="prompt_usage",
                source_name="prompt-usage.raw.jsonl",
            )
            source = pathlib.Path(current["path"])
            source.write_text(
                "".join(
                    json.dumps(_turn_raw("session", f"turn-{index}", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n" for index in range(1201)
                ),
                encoding="utf-8",
            )
            original_stage = cleanup._retention.stage_pruned_turn_state
            batch_sizes: list[int] = []
            job_ids: list[str | None] = []

            def stage(base_arg, cutoff_arg, turns, *, job_id=None):
                batch = list(turns)
                batch_sizes.append(len(batch))
                job_ids.append(job_id)
                return original_stage(base_arg, cutoff_arg, batch, job_id=job_id)

            with mock.patch.object(cleanup._retention, "stage_pruned_turn_state", side_effect=stage):
                plan = cleanup.plan_delete_logs_older_than(
                    base,
                    datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp(),
                    operation_job_id="retention:batch-test",
                )

            cleanup.discard_delete_logs_older_than_plan(plan)

        self.assertEqual(batch_sizes, [500, 500, 201])
        self.assertEqual(job_ids, ["retention:batch-test"] * 3)
        self.assertEqual(plan["operation_job_id"], "retention:batch-test")
        self.assertEqual(plan["pruned_turn_count"], 1201)
        self.assertEqual(plan["pruned_turns"], [])

    def test_retention_checkpoint_uses_disk_backups_and_restores_current_file(self) -> None:
        cli = load_module("retention_disk_checkpoint_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            source = current_dir / "prompt-usage.raw.jsonl.current.1.jsonl"
            payload = b"x" * (2 * 1024 * 1024)
            source.write_bytes(payload)

            checkpoint = cli.raw_segment_state_checkpoint(base, "retention:checkpoint-test")
            metadata = json.loads((pathlib.Path(checkpoint["checkpoint_dir"]) / "checkpoint.json").read_text(encoding="utf-8"))
            backup = next(iter(checkpoint["current_backups"].values()))
            source.unlink()
            cli.restore_raw_segment_state_checkpoint(base, checkpoint)

            restored = source.read_bytes()
            checkpoint_exists = pathlib.Path(checkpoint["checkpoint_dir"]).exists()

        self.assertEqual(restored, payload)
        self.assertFalse(checkpoint_exists)
        self.assertNotIn("current_file_bytes", checkpoint)
        self.assertIsInstance(backup, pathlib.Path)
        self.assertEqual(metadata["operation_job_id"], "retention:checkpoint-test")
        self.assertEqual(metadata["output_dir"], str(base.resolve()))

    def test_retention_checkpoint_sweep_fails_closed_on_symlink(self) -> None:
        cli = load_module("retention_checkpoint_symlink_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            root = cli.retention_checkpoints.root_path(base)
            root.mkdir(parents=True)
            target = base / "outside"
            target.mkdir()
            (root / "checkpoint-linked").symlink_to(target, target_is_directory=True)

            summary = cli.retention_checkpoints.inspect(base)
            with self.assertRaises(cli.retention_checkpoints.RetentionCheckpointError):
                cli.retention_checkpoints.sweep(base)

        self.assertFalse(summary["valid"])

    def test_recovery_discards_pre_reset_pending_state_by_operation_id(self) -> None:
        cleanup = load_module("retention_pre_reset_recovery_test", ROOT / "scripts" / "dashboard_cleanup.py")
        store = load_module("retention_pre_reset_store_test", ROOT / "scripts" / "retention_pruned_store.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            operation_job_id = "retention:interrupted-plan"
            store.stage_rows(
                base,
                [{"session_id": "session", "turn_id": "turn", "captured_at_unix": 1.0}],
                job_id=operation_job_id,
            )
            cleanup.write_cleanup_retention_job(
                base,
                {
                    "phase": "planning",
                    "operation_job_id": operation_job_id,
                    "cutoff_unix": 1.0,
                    "derived_rebuild_required": False,
                    "recovery_required": True,
                    "physical_delete_pending": False,
                },
            )

            result = cleanup._recovery.recover_retention_cleanup(base)
            summary = store.inspect_summary(base)

        self.assertEqual(result["recovered_phase"], "planning")
        self.assertIsNone(result["job"])
        self.assertEqual(summary["pending_rows"], 0)

    def test_retention_prune_resets_derived_outputs_and_rebuilds_dashboard_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            raw_dir = base / "raw"
            raw_segments = load_module("raw_segments_retention_prune_pipeline_test", ROOT / "scripts" / "raw_segments.py")
            normalized_dir = base / "normalized"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text(
                json.dumps(_turn_raw("s-old", "t-old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"})
                + "\n"
                + json.dumps(_turn_raw("s-new", "t-new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"})
                + "\n",
                encoding="utf-8",
            )
            (normalized_dir / "prompt-usage.normalized.jsonl").write_text("stale\n", encoding="utf-8")
            (normalized_dir / "normalize-state.json").write_text('{"stale":true}\n', encoding="utf-8")
            db_path = analytics_dir / "bola.sqlite"

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "bola.py"),
                    "pipeline",
                    "--codex-dir",
                    str(codex_dir),
                    "--output-dir",
                    str(base),
                    "--output",
                    str(db_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn('"turn_rows":2', result.stdout)

            cutoff_text = self.RETENTION_CUTOFF
            cleanup = load_module("cleanup_retention_prune_pipeline_signature_test", ROOT / "scripts" / "dashboard_cleanup.py")
            preview_signature = cleanup.retention_preview_signature(base, datetime.fromisoformat(cutoff_text).timestamp())
            prune = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "bola.py"),
                    "retention-prune",
                    "--codex-dir",
                    str(codex_dir),
                    "--output-dir",
                    str(base),
                    "--output",
                    str(db_path),
                    "--cutoff",
                    cutoff_text,
                    "--preview-signature",
                    preview_signature,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            parsed = json.loads(prune.stdout.splitlines()[-1])
            self.assertEqual(parsed["deleted_rows"], 1)
            con = sqlite3.connect(db_path)
            try:
                turns = con.execute("select turn_id from turns order by turn_id").fetchall()
            finally:
                con.close()
            self.assertEqual(turns, [("t-new",)])
            self.assertNotIn("stale", (normalized_dir / "prompt-usage.normalized.jsonl").read_text(encoding="utf-8"))

    def test_retention_prune_requires_preview_signature_before_preflight(self) -> None:
        cli = load_module("cli_retention_missing_preview_guard_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            db_path = codex_dir / "bola" / "analytics" / "bola.sqlite"
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    cli.dashboard_cleanup, "retention_preview_signature", side_effect=AssertionError("signature must not run without a supplied preview")
                ),
                mock.patch.object(
                    cli.dashboard_cleanup, "preflight_delete_logs_older_than", side_effect=AssertionError("preflight must not run without a supplied preview")
                ),
                mock.patch("sys.stdout", stdout),
            ):
                code = cli.retention_prune(
                    argparse.Namespace(
                        codex_dir=str(codex_dir),
                        output_dir=str(codex_dir / "bola"),
                        output=str(db_path),
                        cutoff=self.RETENTION_CUTOFF,
                        preview_signature="",
                    )
                )
            payload = json.loads(stdout.getvalue().splitlines()[-1])

        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "cleanup_preview_signature_required")

    def test_retention_prune_rejects_stale_preview_signature_before_preflight(self) -> None:
        cli = load_module("cli_retention_stale_preview_guard_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            db_path = base / "analytics" / "bola.sqlite"
            (base / "analytics").mkdir(parents=True)
            args = argparse.Namespace(
                codex_dir=str(codex_dir),
                output_dir=str(base),
                output=str(db_path),
                cutoff="2026-05-20T00:00:00+00:00",
                preview_signature="old",
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    cli.dashboard_cleanup, "preflight_delete_logs_older_than", side_effect=AssertionError("preflight must not run for stale preview")
                ),
                mock.patch.object(cli.dashboard_cleanup, "retention_preview_signature", return_value="fresh"),
                mock.patch.object(cli.dashboard_cleanup, "reset_derived_outputs", side_effect=AssertionError("reset must not run for stale preview")),
                mock.patch("sys.stdout", stdout),
            ):
                code = cli.retention_prune(args)
            payload = json.loads(stdout.getvalue().splitlines()[-1])

        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "cleanup_preview_stale")

    def test_retention_prune_records_recovery_job_when_derived_rebuild_fails(self) -> None:
        cli = load_module("cli_retention_rebuild_job_marker_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            raw_dir = base / "raw"
            raw_segments = load_module("raw_segments_retention_rebuild_job_test", ROOT / "scripts" / "raw_segments.py")
            raw_dir.mkdir(parents=True)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            pathlib.Path(current["path"]).write_text(
                json.dumps(_turn_raw("s-old", "t-old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )

            with (
                mock.patch.object(
                    cli,
                    "run_script_json",
                    side_effect=[
                        (0, {"normalized_turns_size": 1}, "", ""),
                        (3, {"error": "build failed"}, "", "build failed"),
                    ],
                ),
                mock.patch("sys.stdout", new=io.StringIO()),
                mock.patch("sys.stderr", new=io.StringIO()),
            ):
                code = cli.retention_prune(self.retention_prune_args(cli, codex_dir, base / "analytics" / "bola.sqlite"))
            job = cli.dashboard_cleanup.read_cleanup_retention_job(base)

        self.assertEqual(code, 3)
        self.assertEqual(job["phase"], "failed")
        self.assertEqual(job["failed_stage"], "build")
        self.assertTrue(job["derived_rebuild_required"])
        self.assertEqual(job["deleted_rows"], 1)

    def test_retention_prune_rejects_output_outside_service_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            raw_dir = base / "raw"
            state_dir = base / "state"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            raw_file = raw_dir / "prompt-usage.raw.jsonl"
            raw_file.write_text(json.dumps(_turn_raw("s-old", "t-old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n", encoding="utf-8")
            manifest = state_dir / "raw-segments-manifest.json"
            manifest.write_text(json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "segments": []}) + "\n", encoding="utf-8")
            before_raw = raw_file.read_bytes()
            before_manifest = manifest.read_bytes()
            external_db = pathlib.Path(tmp) / "external.sqlite"

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "bola.py"),
                    "retention-prune",
                    "--codex-dir",
                    str(codex_dir),
                    "--output-dir",
                    str(codex_dir / "bola"),
                    "--output",
                    str(external_db),
                    "--cutoff",
                    "2026-05-20T00:00:00+00:00",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("retention output must be under", result.stderr)
            self.assertEqual(raw_file.read_bytes(), before_raw)
            self.assertEqual(manifest.read_bytes(), before_manifest)
            self.assertFalse((state_dir / "retention-pruned-turns.json").exists())

    def test_retention_prune_rejects_analytics_directory_output_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            raw_dir = base / "raw"
            analytics_dir = base / "analytics"
            raw_dir.mkdir(parents=True)
            analytics_dir.mkdir(parents=True)
            raw_file = raw_dir / "prompt-usage.raw.jsonl"
            raw_file.write_text(json.dumps(_turn_raw("s-old", "t-old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n", encoding="utf-8")
            before_raw = raw_file.read_bytes()

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "bola.py"),
                    "retention-prune",
                    "--codex-dir",
                    str(codex_dir),
                    "--output-dir",
                    str(codex_dir / "bola"),
                    "--output",
                    str(analytics_dir),
                    "--cutoff",
                    "2026-05-20T00:00:00+00:00",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("retention output must be a database file", result.stderr)
            self.assertEqual(raw_file.read_bytes(), before_raw)

    def test_retention_prune_keyboard_interrupt_restores_checkpoint_and_marks_progress(self) -> None:
        cli = load_module("cli_retention_keyboard_interrupt_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            checkpoint = {"state": "before"}
            with (
                mock.patch.object(cli.dashboard_cleanup, "retention_preview_signature", return_value="fresh"),
                mock.patch.object(cli.dashboard_cleanup, "preflight_delete_logs_older_than", return_value={}),
                mock.patch.object(cli, "raw_segment_state_checkpoint", return_value=checkpoint),
                mock.patch.object(cli.dashboard_cleanup, "plan_delete_logs_older_than", side_effect=KeyboardInterrupt()),
                mock.patch.object(cli, "restore_raw_segment_state_checkpoint") as restore_checkpoint,
                mock.patch.object(cli.progress_control, "write_progress") as write_progress,
            ):
                result = cli.retention_prune(
                    self.retention_prune_args(cli, codex_dir, codex_dir / "bola" / "analytics" / "bola.sqlite", preview_signature="fresh")
                )

        self.assertEqual(result, 130)
        restore_checkpoint.assert_called_once_with(codex_dir / "bola", checkpoint)
        self.assertTrue(
            any(call.kwargs.get("status") == "failed" and call.kwargs.get("checkpoint") == "restore-checkpoint" for call in write_progress.mock_calls)
        )

    def test_retention_prune_does_not_mutate_sources_when_reset_fails(self) -> None:
        cli = load_module("cli_retention_reset_failure_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            raw_dir = base / "raw"
            state_dir = base / "state"
            current_dir = raw_dir / "current"
            raw_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            current_dir.mkdir(parents=True)
            raw_file = raw_dir / "prompt-usage.raw.jsonl"
            raw_file.write_text(json.dumps(_turn_raw("s-old", "t-old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n", encoding="utf-8")
            manifest = state_dir / "raw-segments-manifest.json"
            pointer = state_dir / "current-raw-segments.json"
            manifest.write_text(json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "segments": []}) + "\n", encoding="utf-8")
            pointer.write_text(json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "current": {}}) + "\n", encoding="utf-8")
            before_raw = raw_file.read_bytes()
            before_manifest = manifest.read_bytes()
            before_pointer = pointer.read_bytes()
            before_current_files = sorted(path.name for path in current_dir.iterdir())

            with mock.patch.object(cli.dashboard_cleanup, "reset_derived_outputs", side_effect=OSError("reset failed")):
                with self.assertRaises(OSError):
                    cli.retention_prune(self.retention_prune_args(cli, codex_dir, base / "analytics" / "bola.sqlite"))
            recovery_job = cli.dashboard_cleanup.read_cleanup_retention_job(base)
            self.assertEqual(raw_file.read_bytes(), before_raw)
            self.assertEqual(manifest.read_bytes(), before_manifest)
            self.assertEqual(pointer.read_bytes(), before_pointer)
            self.assertEqual(sorted(path.name for path in current_dir.iterdir()), before_current_files)
            self.assertFalse((state_dir / "raw-segment-rotation-pending.json").exists())
            self.assertEqual(recovery_job["phase"], "failed")
            self.assertEqual(recovery_job["failed_stage"], "reset")
            self.assertTrue(recovery_job["derived_rebuild_required"])

    def test_pending_derived_rebuild_blocks_another_retention_prune(self) -> None:
        cleanup = load_module("cleanup_pending_derived_rebuild_guard_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            cleanup.write_cleanup_retention_job(
                base,
                {
                    "phase": "derived_reset_pending",
                    "operation_job_id": "retention:pending-rebuild",
                    "cutoff_unix": 1.0,
                    "deleted_rows": 0,
                    "derived_rebuild_required": True,
                    "recovery_required": True,
                    "physical_delete_pending": False,
                },
            )

            with self.assertRaises(cleanup._retention.raw_segments.ManifestError) as raised:
                cleanup.preflight_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

        self.assertIn("derived rebuild must complete", str(raised.exception))

    def test_successful_pipeline_rebuild_clears_derived_recovery_job(self) -> None:
        cleanup = load_module("cleanup_complete_derived_rebuild_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            cleanup.write_cleanup_retention_job(
                base,
                {
                    "phase": "failed",
                    "operation_job_id": "retention:failed-build",
                    "cutoff_unix": 1.0,
                    "deleted_rows": 0,
                    "failed_stage": "build",
                    "derived_rebuild_required": True,
                    "recovery_required": True,
                    "physical_delete_pending": False,
                },
            )

            result = cleanup.complete_retention_derived_rebuild(base)

            self.assertTrue(result["updated"])
            self.assertIsNone(result["job"])
            self.assertIsNone(cleanup.read_cleanup_retention_job(base))

    def test_successful_pipeline_rebuild_preserves_physical_delete_recovery(self) -> None:
        cleanup = load_module("cleanup_complete_derived_with_physical_pending_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            cleanup.write_cleanup_retention_job(
                base,
                {
                    "phase": "physical_delete_pending",
                    "operation_job_id": "retention:physical-pending",
                    "cutoff_unix": 1.0,
                    "deleted_rows": 0,
                    "derived_rebuild_required": True,
                    "recovery_required": True,
                    "physical_delete_pending": True,
                    "pending_files": 1,
                },
            )

            result = cleanup.complete_retention_derived_rebuild(base)
            job = cleanup.read_cleanup_retention_job(base)

            self.assertTrue(result["updated"])
            self.assertFalse(job["derived_rebuild_required"])
            self.assertTrue(job["physical_delete_pending"])
            self.assertEqual(job["phase"], "physical_delete_pending")

    def test_retention_prune_restores_current_files_when_reset_fails_after_empty_rotation(self) -> None:
        cli = load_module("cli_retention_reset_current_files_test", ROOT / "scripts" / "bola.py")
        raw_segments = cli.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            prompt = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            before_pointer = raw_segments.strict_read_current_pointer(base)
            before_files = {
                pathlib.Path(prompt["path"]): pathlib.Path(prompt["path"]).read_bytes(),
            }

            with mock.patch.object(cli.dashboard_cleanup, "reset_derived_outputs", side_effect=OSError("reset failed")):
                with self.assertRaises(OSError):
                    cli.retention_prune(self.retention_prune_args(cli, codex_dir, base / "analytics" / "bola.sqlite"))

            after_pointer = raw_segments.strict_read_current_pointer(base)

            self.assertEqual(after_pointer, before_pointer)
            for path, content in before_files.items():
                self.assertEqual(path.read_bytes(), content)

    def test_retention_prune_preserves_forward_current_append_when_reset_fails(self) -> None:
        cli = load_module("cli_retention_reset_forward_append_test", ROOT / "scripts" / "bola.py")
        raw_segments = cli.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            real_plan = cli.dashboard_cleanup.plan_delete_logs_older_than
            appended = json.dumps(_turn_raw("live", "append", total=111) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n"

            def plan_then_append(root: pathlib.Path, cutoff: float, **kwargs: object) -> dict[str, object]:
                plan = real_plan(root, cutoff, **kwargs)
                pointer = raw_segments.strict_read_current_pointer(base)
                current_prompt = pathlib.Path(pointer["current"]["prompt_usage"]["path"])
                current_prompt.write_text(appended, encoding="utf-8")
                return plan

            with (
                mock.patch.object(cli.dashboard_cleanup, "plan_delete_logs_older_than", side_effect=plan_then_append),
                mock.patch.object(cli.dashboard_cleanup, "reset_derived_outputs", side_effect=OSError("reset failed")),
            ):
                with self.assertRaises(OSError):
                    cli.retention_prune(self.retention_prune_args(cli, codex_dir, base / "analytics" / "bola.sqlite"))

            pointer = raw_segments.strict_read_current_pointer(base)
            current_prompt = pathlib.Path(pointer["current"]["prompt_usage"]["path"])

            self.assertIn('"turn_id": "append"', current_prompt.read_text(encoding="utf-8"))

    def test_retention_prune_preserves_derived_outputs_when_delete_preflight_fails(self) -> None:
        cli = load_module("cli_retention_delete_preflight_failure_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            derived = seed_retention_derived_outputs(base)
            db_path = derived["db"]

            with (
                mock.patch.object(cli.dashboard_cleanup, "retention_preview_signature", return_value="fresh"),
                mock.patch.object(cli.dashboard_cleanup, "preflight_delete_logs_older_than", side_effect=cli.raw_segments.ManifestError("bad manifest")),
            ):
                with self.assertRaises(cli.raw_segments.ManifestError):
                    cli.retention_prune(self.retention_prune_args(cli, codex_dir, db_path, preview_signature="fresh"))
            assert_retention_derived_outputs_unchanged(self, derived)

    def test_retention_prune_preserves_derived_outputs_when_delete_plan_fails(self) -> None:
        cli = load_module("cli_retention_delete_plan_failure_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            derived = seed_retention_derived_outputs(base)
            db_path = derived["db"]

            with (
                mock.patch.object(cli.dashboard_cleanup, "retention_preview_signature", return_value="fresh"),
                mock.patch.object(cli.dashboard_cleanup, "preflight_delete_logs_older_than", return_value={"ok": True}),
                mock.patch.object(cli.dashboard_cleanup, "plan_delete_logs_older_than", side_effect=cli.raw_segments.ManifestError("rotation failed")),
            ):
                with self.assertRaises(cli.raw_segments.ManifestError):
                    cli.retention_prune(self.retention_prune_args(cli, codex_dir, db_path, preview_signature="fresh"))
            assert_retention_derived_outputs_unchanged(self, derived)

    def test_retention_prune_preserves_derived_outputs_when_current_pointer_invalid(self) -> None:
        cli = load_module("cli_retention_current_pointer_preflight_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            state_dir = base / "state"
            raw_dir = base / "raw"
            state_dir.mkdir(parents=True)
            raw_dir.mkdir(parents=True)
            derived = seed_retention_derived_outputs(base)
            db_path = derived["db"]
            (state_dir / "raw-segments-manifest.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "segments": []}) + "\n", encoding="utf-8"
            )
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "base": str(base.resolve()),
                        "updated_at_unix": 1.0,
                        "current": {
                            "prompt_usage": {
                                "id": "prompt-usage.raw.jsonl.current.bad",
                                "kind": "prompt_usage",
                                "source_name": "prompt-usage.raw.jsonl",
                                "path": str(raw_dir / "prompt-usage.raw.jsonl"),
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(cli.raw_segments.ManifestError):
                cli.retention_prune(self.retention_prune_args(cli, codex_dir, db_path, preview_signature="fresh"))
            assert_retention_derived_outputs_unchanged(self, derived)

    def test_retention_prune_preserves_derived_outputs_when_manifest_segment_malformed(self) -> None:
        cli = load_module("cli_retention_malformed_manifest_segment_test", ROOT / "scripts" / "bola.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            state_dir = base / "state"
            state_dir.mkdir(parents=True)
            derived = seed_retention_derived_outputs(base)
            db_path = derived["db"]
            (state_dir / "raw-segments-manifest.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "segments": [42]}) + "\n", encoding="utf-8"
            )
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "current": {}}) + "\n", encoding="utf-8"
            )

            with self.assertRaises(cli.raw_segments.ManifestError):
                cli.retention_prune(self.retention_prune_args(cli, codex_dir, db_path, preview_signature="fresh"))
            assert_retention_derived_outputs_unchanged(self, derived)

    def test_retention_prune_preserves_derived_outputs_when_cutoff_metadata_missing(self) -> None:
        cli = load_module("cli_retention_missing_cutoff_metadata_test", ROOT / "scripts" / "bola.py")
        raw_segments = load_module("raw_segments_missing_cutoff_metadata_test", ROOT / "scripts" / "raw_segments.py")
        cases = [
            ("missing", "max_time_unix", None),
            ("invalid", "min_time_unix", "2026-05-23T00:00:00Z"),
            ("invalid", "max_time_unix", True),
            ("inverted", "min_time_unix", 1779580800.0),
        ]
        for mode, field, replacement in cases:
            with self.subTest(mode=mode, field=field):
                with tempfile.TemporaryDirectory() as tmp:
                    codex_dir = pathlib.Path(tmp) / ".codex"
                    base = codex_dir / "bola"
                    state_dir = base / "state"
                    archive = base / "raw" / "archive"
                    state_dir.mkdir(parents=True)
                    archive.mkdir(parents=True)
                    derived = seed_retention_derived_outputs(base)
                    db_path = derived["db"]
                    segment_path = archive / "prompt-usage.raw.jsonl.20260523000000.20260523000000.1.jsonl.gz"
                    payload = (json.dumps(_turn_raw("s1", "new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n").encode("utf-8")
                    with gzip.open(segment_path, "wb") as handle:
                        handle.write(payload)
                    segment = _raw_segment(
                        segment_path, payload=payload, min_time=1779494400.0, max_time=1779494400.0, rows=1, days=[[1779494400, 1, len(payload)]]
                    )
                    if mode == "missing":
                        segment.pop(field)
                    else:
                        segment[field] = replacement
                    raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})
                    (state_dir / "current-raw-segments.json").write_text(
                        json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "current": {}}) + "\n", encoding="utf-8"
                    )

                    with mock.patch.object(cli.dashboard_cleanup, "retention_preview_signature", return_value="fresh"):
                        with self.assertRaises(cli.raw_segments.ManifestError):
                            cli.retention_prune(self.retention_prune_args(cli, codex_dir, db_path, preview_signature="fresh"))

                    assert_retention_derived_outputs_unchanged(self, derived)

    def test_retention_prune_preserves_derived_outputs_when_segment_metadata_stale(self) -> None:
        cli = load_module("cli_retention_stale_segment_metadata_test", ROOT / "scripts" / "bola.py")
        raw_segments = load_module("raw_segments_stale_segment_metadata_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            codex_dir = pathlib.Path(tmp) / ".codex"
            base = codex_dir / "bola"
            state_dir = base / "state"
            archive = base / "raw" / "archive"
            state_dir.mkdir(parents=True)
            archive.mkdir(parents=True)
            derived = seed_retention_derived_outputs(base)
            db_path = derived["db"]
            segment_path = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(segment_path, "wb") as handle:
                handle.write(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        _raw_segment(segment_path, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1, days=[[1777593600, 1, len(payload)]])
                    ]
                },
            )
            (state_dir / "current-raw-segments.json").write_text(
                json.dumps({"schema_version": 1, "base": str(base.resolve()), "updated_at_unix": 1.0, "current": {}}) + "\n", encoding="utf-8"
            )

            with mock.patch.object(cli.dashboard_cleanup, "retention_preview_signature", return_value="fresh"):
                code = cli.retention_prune(self.retention_prune_args(cli, codex_dir, db_path, preview_signature="fresh"))
            self.assertEqual(code, 2)
            assert_retention_derived_outputs_unchanged(self, derived)

    def test_cleanup_payload_exposes_action_file_counts_contract(self) -> None:
        cleanup = load_module("cleanup_action_file_counts_payload_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_action_file_counts_payload_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            seed_retention_derived_outputs(base)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            old_payload = (json.dumps(_turn_raw("s-old", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(old_payload)

            mixed_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260523000000.2.jsonl.gz"
            mixed_old = json.dumps(_turn_raw("s-mixed", "old", total=110) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            mixed_new = json.dumps(_turn_raw("s-mixed", "new", total=210) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n"
            mixed_payload = (mixed_old + mixed_new).encode("utf-8")
            with gzip.open(mixed_segment, "wb") as handle:
                handle.write(mixed_payload)

            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        _raw_segment(old_segment, payload=old_payload, min_time=1777593600.0, max_time=1777593600.0, rows=1),
                        _raw_segment(
                            mixed_segment,
                            payload=mixed_payload,
                            min_time=1777593600.0,
                            max_time=1779494400.0,
                            rows=2,
                            days=[
                                [1777593600, 1, len(mixed_old.encode("utf-8"))],
                                [1779494400, 1, len(mixed_new.encode("utf-8"))],
                            ],
                        ),
                    ]
                },
            )

            payload = cleanup.cleanup_payload(
                base, base / "analytics" / "bola.sqlite", retention_cutoff_unix=datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp()
            )

        rows = {row["group_id"]: row for row in payload["rows"]}
        raw_display = rows["archived_raw_logs"]["display"]
        normalized_display = rows["normalized_outputs"]["display"]
        analytics_display = rows["analytics_database"]["display"]
        raw_delete_all = rows["archived_raw_logs"]["delete_all_display"]

        self.assertEqual(raw_display["action_file_counts"], {"Delete": 1, "Rewrite": 1, "Rebuild": 0})
        self.assertEqual(raw_delete_all["action_file_counts"], {"Delete": 2, "Rewrite": 0, "Rebuild": 0})
        self.assertEqual(normalized_display["action_file_counts"], {"Delete": 0, "Rewrite": 0, "Rebuild": 2})
        self.assertEqual(analytics_display["action_file_counts"], {"Delete": 0, "Rewrite": 0, "Rebuild": 1})

    def test_retention_prune_preflight_does_not_create_archive_directory(self) -> None:
        cleanup = load_module("cleanup_retention_preflight_no_archive_mkdir_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_preflight_no_archive_mkdir_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "token-usage"
            current_dir = base / "raw" / "current"
            archive_dir = base / "raw" / "archive"
            current_dir.mkdir(parents=True)
            segment_path = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            payload = (
                json.dumps(_turn_raw("s-old", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"})
                + "\n"
                + json.dumps(_turn_raw("s-new", "new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"})
                + "\n"
            ).encode("utf-8")
            segment_path.write_bytes(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        _raw_segment(
                            segment_path,
                            payload=payload,
                            min_time=1777593600.0,
                            max_time=1779494400.0,
                            rows=2,
                            days=[[1777593600, 1, len(payload) // 2], [1779494400, 1, len(payload) - (len(payload) // 2)]],
                        )
                    ]
                },
            )

            cleanup.preflight_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            self.assertFalse(archive_dir.exists())

    def test_retention_prune_deletes_whole_old_segment_and_updates_manifest(self) -> None:
        cleanup = load_module("cleanup_whole_segment_prune_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_whole_prune_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})

            result = cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            old_exists = old_segment.exists()
            manifest_segments = raw_segments.read_manifest(base)["segments"]

        self.assertEqual(result["deleted_rows"], 1)
        self.assertFalse(old_exists)
        self.assertEqual(manifest_segments, [])

    def test_retention_prune_rewrites_mixed_segment_and_keeps_new_rows(self) -> None:
        cleanup = load_module("cleanup_mixed_segment_prune_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_mixed_prune_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            mixed_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260523000000.1.jsonl.gz"
            old_payload = json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            new_payload = json.dumps(_turn_raw("s2", "new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n"
            payload = (old_payload + new_payload).encode("utf-8")
            with gzip.open(mixed_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(
                mixed_segment,
                payload=payload,
                min_time=1777593600.0,
                max_time=1779494400.0,
                rows=2,
                days=[[1777593600, 1, len(old_payload.encode("utf-8"))], [1779494400, 1, len(new_payload.encode("utf-8"))]],
            )
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})

            result = cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            manifest = raw_segments.read_manifest(base)
            retained_path = pathlib.Path(manifest["segments"][0]["path"])
            with gzip.open(retained_path, "rt", encoding="utf-8") as handle:
                retained_rows = [json.loads(line) for line in handle]

        self.assertEqual(result["deleted_rows"], 1)
        self.assertEqual(len(manifest["segments"]), 1)
        self.assertEqual([row["turn_id"] for row in retained_rows], ["new"])

    def test_retention_prune_preserves_untracked_sources_when_segment_apply_fails(self) -> None:
        cleanup = load_module("cleanup_untracked_segment_boundary_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_untracked_segment_boundary_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raw_dir = base / "raw"
            archive = raw_dir / "archive"
            raw_dir.mkdir(parents=True)
            archive.mkdir(parents=True)
            untracked_raw = raw_dir / "prompt-usage.raw.jsonl"
            old_line = json.dumps(_turn_raw("untracked", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            new_line = json.dumps(_turn_raw("untracked", "new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n"
            untracked_raw.write_text(old_line + new_line, encoding="utf-8")
            before_untracked = untracked_raw.read_bytes()
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("segment", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {"segments": [_raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)]},
            )
            plan = cleanup.plan_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            with mock.patch.object(
                cleanup._retention.raw_segments, "apply_segment_plans", side_effect=cleanup._retention.raw_segments.ManifestError("segment apply failed")
            ):
                with self.assertRaises(cleanup._retention.raw_segments.ManifestError):
                    cleanup.apply_delete_logs_older_than_plan(plan)

            after_untracked = untracked_raw.read_bytes()

        self.assertEqual(after_untracked, before_untracked)

    def test_retention_prune_does_not_commit_pruned_state_when_segment_apply_fails(self) -> None:
        cleanup = load_module("cleanup_segment_failure_pruned_state_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_segment_failure_pruned_state_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raw_dir = base / "raw"
            archive = raw_dir / "archive"
            raw_dir.mkdir(parents=True)
            archive.mkdir(parents=True)
            untracked_raw = raw_dir / "prompt-usage.raw.jsonl"
            untracked_raw.write_text(
                json.dumps(_turn_raw("untracked", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("segment", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {"segments": [_raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)]},
            )
            plan = cleanup.plan_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            with mock.patch.object(
                cleanup._retention.raw_segments, "apply_segment_plans", side_effect=cleanup._retention.raw_segments.ManifestError("segment apply failed")
            ):
                with self.assertRaises(cleanup._retention.raw_segments.ManifestError):
                    cleanup.apply_delete_logs_older_than_plan(plan)

            retention_db = base / "state" / "retention-pruned-turns.sqlite"
            with sqlite3.connect(retention_db) as con:
                pending_rows = con.execute("select count(*) from pruned_turns where state='pending'").fetchone()[0]

        self.assertEqual(pending_rows, 0)

    def test_retention_prune_records_cleanup_job_when_physical_delete_is_pending(self) -> None:
        cleanup = load_module("cleanup_physical_pending_job_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = cleanup._retention.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("segment", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})
            plan = cleanup.plan_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            with mock.patch.object(
                cleanup._retention.raw_segments._state,
                "sweep_segment_sources",
                return_value={"deleted_files": 0, "pending_source_segments": [segment], "errors": [{"path": str(old_segment), "error": "busy"}]},
            ):
                result = cleanup.apply_delete_logs_older_than_plan(plan)
            job = cleanup.read_cleanup_retention_job(base)
            with sqlite3.connect(base / "state" / "retention-pruned-turns.sqlite") as con:
                pruned_states = con.execute("select state from pruned_turns").fetchall()
            payload_with_job = cleanup.cleanup_payload(
                base, base / "analytics" / "bola.sqlite", retention_cutoff_unix=datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp()
            )

            cleanup._recovery.recover_retention_cleanup(base)
            job_after_recovery = cleanup.read_cleanup_retention_job(base)
            rebuild_result = cleanup.complete_retention_derived_rebuild(base)
            job_after_rebuild = cleanup.read_cleanup_retention_job(base)

        self.assertTrue(result["physical_delete_pending"])
        self.assertEqual(result["pending_files"], 1)
        self.assertEqual(job["phase"], "physical_delete_pending")
        self.assertTrue(job["physical_delete_pending"])
        self.assertEqual(job["pending_files"], 1)
        self.assertEqual(pruned_states, [("committed",)])
        self.assertEqual(payload_with_job["retention"]["job"]["phase"], "physical_delete_pending")
        self.assertEqual(job_after_recovery["phase"], "derived_rebuild_required")
        self.assertTrue(rebuild_result["updated"])
        self.assertIsNone(job_after_rebuild)

    def test_retention_prune_keeps_pending_pruned_state_when_commit_fails_after_mutation(self) -> None:
        cleanup = load_module("cleanup_commit_failure_pruned_state_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raw_dir = base / "raw"
            raw_dir.mkdir(parents=True)
            current = cleanup._retention.raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            raw_prompt = pathlib.Path(current["path"])
            raw_prompt.write_text(
                json.dumps(_turn_raw("prompt", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )
            plan = cleanup.plan_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            with mock.patch.object(cleanup._retention, "commit_pruned_turn_state", side_effect=OSError("state commit failed")):
                with self.assertRaises(OSError):
                    cleanup.apply_delete_logs_older_than_plan(plan)

            retention_db = base / "state" / "retention-pruned-turns.sqlite"
            with sqlite3.connect(retention_db) as con:
                pending = con.execute("select session_id, turn_id, state from pruned_turns order by session_id, turn_id").fetchall()
            failed_job = cleanup.read_cleanup_retention_job(base)
            recovery = cleanup._recovery.recover_retention_cleanup(base)
            with sqlite3.connect(retention_db) as con:
                recovered = con.execute("select session_id, turn_id, state from pruned_turns order by session_id, turn_id").fetchall()

        self.assertEqual(pending, [("prompt", "old", "pending")])
        self.assertTrue(failed_job["pruned_state_commit_ready"])
        self.assertTrue(recovery["job"]["pruned_state_commit_recovered"])
        self.assertEqual(recovered, [("prompt", "old", "committed")])

    def test_segment_apply_state_distinguishes_retry_commit_and_drift(self) -> None:
        raw_segments = load_module("raw_segments_apply_state_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            previous_manifest = raw_segments.empty_manifest(base) | {"segments": [{"id": "old"}]}
            next_manifest = raw_segments.empty_manifest(base) | {"segments": []}
            plan = {
                "previous_manifest": previous_manifest,
                "next_manifest": next_manifest,
                "source_segments": [{"id": "old"}],
                "retained_segments": [],
            }
            raw_segments.write_manifest(base, previous_manifest)
            not_started = raw_segments.inspect_segment_apply_state(base, plan)

            raw_segments.write_apply_marker(
                base,
                {
                    "phase": "manifest_pending",
                    "previous_manifest": previous_manifest,
                    "next_manifest": next_manifest,
                    "source_segments": plan["source_segments"],
                    "retained_segments": [],
                },
            )
            recovery_pending = raw_segments.inspect_segment_apply_state(base, plan)

            raw_segments.clear_apply_marker(base)
            raw_segments.write_manifest(base, next_manifest)
            committed = raw_segments.inspect_segment_apply_state(base, plan)

            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [{"id": "drift"}]})
            unknown = raw_segments.inspect_segment_apply_state(base, plan)

        self.assertIs(not_started, raw_segments.SegmentApplyState.NOT_STARTED)
        self.assertIs(recovery_pending, raw_segments.SegmentApplyState.RECOVERY_PENDING)
        self.assertIs(committed, raw_segments.SegmentApplyState.LOGICAL_DELETE_COMMITTED)
        self.assertIs(unknown, raw_segments.SegmentApplyState.UNKNOWN)

    def test_retention_prune_recovers_attribution_after_manifest_write_failure(self) -> None:
        cleanup = load_module("cleanup_manifest_failure_attribution_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("session", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            cleanup._retention.raw_segments.write_manifest(base, cleanup._retention.raw_segments.empty_manifest(base) | {"segments": [segment]})
            plan = cleanup.plan_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            with mock.patch.object(cleanup._retention.raw_segments._retention, "write_manifest", side_effect=OSError("manifest write failed")):
                with self.assertRaises(OSError):
                    cleanup.apply_delete_logs_older_than_plan(plan)

            failed_job = cleanup.read_cleanup_retention_job(base)
            marker = cleanup._retention.raw_segments.read_apply_marker(base)
            with sqlite3.connect(base / "state" / "retention-pruned-turns.sqlite") as con:
                pending = con.execute("select state from pruned_turns").fetchall()

            recovery = cleanup._recovery.recover_retention_cleanup(base)
            with sqlite3.connect(base / "state" / "retention-pruned-turns.sqlite") as con:
                recovered = con.execute("select state from pruned_turns").fetchall()

        self.assertEqual(marker["phase"], "manifest_pending")
        self.assertTrue(failed_job["pruned_state_commit_ready"])
        self.assertEqual(pending, [("pending",)])
        self.assertTrue(recovery["job"]["pruned_state_commit_recovered"])
        self.assertEqual(recovered, [("committed",)])

    def test_retention_prune_recovers_attribution_after_post_sweep_failure(self) -> None:
        cleanup = load_module("cleanup_post_sweep_attribution_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("session", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            cleanup._retention.raw_segments.write_manifest(base, cleanup._retention.raw_segments.empty_manifest(base) | {"segments": [segment]})
            plan = cleanup.plan_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            real_sweep = cleanup._retention.raw_segments._retention.sweep_apply_marker

            def sweep_then_fail(base_arg: pathlib.Path) -> dict[str, Any]:
                real_sweep(base_arg)
                raise OSError("post-sweep failure")

            with mock.patch.object(cleanup._retention.raw_segments._retention, "sweep_apply_marker", side_effect=sweep_then_fail):
                with self.assertRaises(OSError):
                    cleanup.apply_delete_logs_older_than_plan(plan)

            failed_job = cleanup.read_cleanup_retention_job(base)
            self.assertIsNone(cleanup._retention.raw_segments.read_apply_marker(base))
            self.assertEqual(cleanup._retention.raw_segments.strict_read_manifest(base)["segments"], [])
            with sqlite3.connect(base / "state" / "retention-pruned-turns.sqlite") as con:
                pending = con.execute("select state from pruned_turns").fetchall()

            recovery = cleanup._recovery.recover_retention_cleanup(base)
            with sqlite3.connect(base / "state" / "retention-pruned-turns.sqlite") as con:
                recovered = con.execute("select state from pruned_turns").fetchall()

        self.assertTrue(failed_job["pruned_state_commit_ready"])
        self.assertEqual(pending, [("pending",)])
        self.assertTrue(recovery["job"]["pruned_state_commit_recovered"])
        self.assertEqual(recovered, [("committed",)])

    def test_retention_prune_preserves_unresolved_attribution_on_manifest_drift(self) -> None:
        cleanup = load_module("cleanup_manifest_drift_attribution_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("session", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            cleanup._retention.raw_segments.write_manifest(base, cleanup._retention.raw_segments.empty_manifest(base) | {"segments": [segment]})
            plan = cleanup.plan_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            def drift_then_fail(base_arg: pathlib.Path, _plan: dict[str, Any]) -> dict[str, Any]:
                drift = cleanup._retention.raw_segments.empty_manifest(base_arg) | {"segments": [segment, dict(segment, id="unexpected")]}
                cleanup._retention.raw_segments.write_manifest(base_arg, drift)
                raise OSError("manifest drift")

            with mock.patch.object(cleanup._retention.raw_segments, "apply_segment_plans", side_effect=drift_then_fail):
                with self.assertRaises(OSError):
                    cleanup.apply_delete_logs_older_than_plan(plan)

            failed_job = cleanup.read_cleanup_retention_job(base)
            with sqlite3.connect(base / "state" / "retention-pruned-turns.sqlite") as con:
                pending = con.execute("select state from pruned_turns").fetchall()

        self.assertEqual(failed_job["phase"], "failed")
        self.assertNotIn("pruned_state_commit_ready", failed_job)
        self.assertEqual(pending, [("pending",)])

    def test_retention_prune_preserves_undated_and_corrupt_segments_with_null_bounds(self) -> None:
        cleanup = load_module("cleanup_unverifiable_segment_prune_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_unverifiable_segment_prune_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            undated_path = archive / "prompt-usage.raw.jsonl.undated.undated.1.jsonl.gz"
            corrupt_path = archive / "prompt-usage.raw.jsonl.undated.undated.2.jsonl.gz"
            undated_row = _turn_raw("s1", "undated", total=100)
            undated_row.pop("captured_at", None)
            undated_payload = (json.dumps(undated_row) + "\n").encode("utf-8")
            corrupt_payload = b"{not-json}\n"
            with gzip.open(undated_path, "wb") as handle:
                handle.write(undated_payload)
            with gzip.open(corrupt_path, "wb") as handle:
                handle.write(corrupt_payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {
                    "segments": [
                        _raw_segment(undated_path, payload=undated_payload, min_time=None, max_time=None, rows=1, undated=1),
                        _raw_segment(corrupt_path, payload=corrupt_payload, min_time=None, max_time=None, rows=1, corrupt=1),
                    ]
                },
            )

            result = cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            manifest = raw_segments.read_manifest(base)

        self.assertEqual(result["deleted_rows"], 0)
        self.assertEqual(len(manifest["segments"]), 2)

    def test_retention_segment_prune_does_not_mutate_when_pruned_state_write_fails(self) -> None:
        cleanup = load_module("cleanup_segment_state_first_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_segment_state_first_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {"segments": [_raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)]},
            )
            before_manifest = raw_segments.read_manifest(base)

            with mock.patch.object(cleanup._retention, "stage_pruned_turn_state", side_effect=OSError("state write failed")):
                with self.assertRaises(OSError):
                    cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            old_exists = old_segment.exists()
            manifest_after = raw_segments.read_manifest(base)

        self.assertTrue(old_exists)
        self.assertEqual(manifest_after, before_manifest)

    def test_retention_segment_whole_delete_rejects_corrupt_manifest_rows(self) -> None:
        cleanup = load_module("cleanup_segment_corrupt_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_segment_corrupt_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n{broken-json").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {"segments": [_raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)]},
            )

            with self.assertRaises(cleanup._retention.raw_segments.ManifestError):
                cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            old_exists = old_segment.exists()

        self.assertTrue(old_exists)

    def test_retention_segment_whole_delete_rejects_checksum_mismatch(self) -> None:
        cleanup = load_module("cleanup_segment_checksum_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_segment_checksum_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {"segments": [_raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1, sha256="bad")]},
            )

            with self.assertRaises(cleanup._retention.raw_segments.ManifestError):
                cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            old_exists = old_segment.exists()

        self.assertTrue(old_exists)

    def test_retention_segment_rejects_duplicate_manifest_identity_before_mutation(self) -> None:
        cleanup = load_module("cleanup_segment_duplicate_identity_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_duplicate_identity_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            first_path = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            second_path = archive / "prompt-usage.raw.jsonl.20260502000000.20260502000000.2.jsonl.gz"
            first_payload = (json.dumps(_turn_raw("s1", "old-1", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            second_payload = (json.dumps(_turn_raw("s2", "old-2", total=100) | {"captured_at": "2026-05-02T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(first_path, "wb") as handle:
                handle.write(first_payload)
            with gzip.open(second_path, "wb") as handle:
                handle.write(second_payload)
            first = _raw_segment(first_path, payload=first_payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            second = _raw_segment(second_path, payload=second_payload, min_time=1777680000.0, max_time=1777680000.0, rows=1)
            second["id"] = first["id"]
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [first, second]})

            with self.assertRaises(cleanup._retention.raw_segments.ManifestError):
                cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            first_exists = first_path.exists()
            second_exists = second_path.exists()

        self.assertTrue(first_exists)
        self.assertTrue(second_exists)

    def test_retention_segment_prune_excludes_resolved_equivalent_manifest_archive_path(self) -> None:
        cleanup = load_module("cleanup_segment_resolved_path_exclusion_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_resolved_path_exclusion_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            equivalent_path = archive / ".." / "archive" / old_segment.name
            segment = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            segment["path"] = str(equivalent_path)
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})

            result = cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            old_exists = old_segment.exists()

        self.assertEqual(result["deleted_rows"], 1)
        self.assertFalse(old_exists)

    def test_retention_prune_deletes_current_jsonl_segment_and_updates_manifest(self) -> None:
        cleanup = load_module("cleanup_current_jsonl_segment_prune_test", ROOT / "scripts" / "dashboard_cleanup.py")
        raw_segments = load_module("raw_segments_current_jsonl_segment_prune_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current_dir = base / "raw" / "current"
            current_dir.mkdir(parents=True)
            old_segment = current_dir / "prompt-usage.raw.jsonl.current.1777593600000000000.jsonl"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            old_segment.write_bytes(payload)
            raw_segments.write_manifest(
                base,
                raw_segments.empty_manifest(base)
                | {"segments": [_raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)]},
            )

            result = cleanup.delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            old_exists = old_segment.exists()
            manifest_segments = raw_segments.read_manifest(base)["segments"]

        self.assertEqual(result["deleted_rows"], 1)
        self.assertFalse(old_exists)
        self.assertEqual(manifest_segments, [])

    def test_retention_prune_rotates_current_segments_before_planning(self) -> None:
        cleanup = load_module("cleanup_current_rotate_before_plan_test", ROOT / "scripts" / "dashboard_cleanup.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            calls: list[str] = []

            def fake_freeze(base_arg: pathlib.Path, cutoff: float, signature: str) -> dict[str, object]:
                calls.append("rotate")
                return {}

            def fake_plan(
                base_arg: pathlib.Path,
                cutoff: float,
                *,
                pruned_turn_sink=None,
            ) -> dict[str, object]:
                calls.append("plan")
                return {"plans": [], "deleted_turns": [], "deleted_rows": 0, "scanned_rows": 0}

            with (
                mock.patch.object(cleanup._preview, "freeze_retention_snapshot", side_effect=fake_freeze),
                mock.patch.object(cleanup._retention.raw_segments, "plan_segments_older_than", side_effect=fake_plan),
            ):
                cleanup.plan_delete_logs_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

        self.assertLess(calls.index("rotate"), calls.index("plan"))

    def test_retention_snapshot_routes_append_after_pointer_swap(self) -> None:
        import threading
        import time

        cleanup = load_module("cleanup_atomic_retention_snapshot_test", ROOT / "scripts" / "dashboard_cleanup.py")
        hook = load_module("hook_atomic_retention_snapshot_test", ROOT / "scripts" / "hook.py")
        raw_segments = cleanup._retention.raw_segments
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            current = raw_segments.ensure_current_segment(base, kind="prompt_usage", source_name="prompt-usage.raw.jsonl")
            old_path = pathlib.Path(current["path"])
            old_path.write_text('{"record_type":"turn_start","turn_id":"before-swap"}\n', encoding="utf-8")
            expected = cleanup._preview.retention_preview_signature(base, 1.0)
            append_started = threading.Event()
            append_finished = threading.Event()
            append_result: list[bool] = []
            real_begin = raw_segments.begin_rotate_all_current_segments_unlocked

            def append() -> None:
                append_started.set()
                append_result.append(
                    hook.append_current_segment_jsonl(
                        {"record_type": "turn_stop", "turn_id": "after-swap"},
                        kind="prompt_usage",
                        source_name="prompt-usage.raw.jsonl",
                        base_dir=base,
                    )
                )
                append_finished.set()

            def begin_then_release(base_arg: pathlib.Path) -> dict[str, object]:
                thread = threading.Thread(target=append)
                thread.start()
                self.assertTrue(append_started.wait(1.0))
                time.sleep(0.05)
                self.assertFalse(append_finished.is_set())
                result = real_begin(base_arg)
                return result

            with mock.patch.object(raw_segments, "begin_rotate_all_current_segments_unlocked", side_effect=begin_then_release):
                cleanup._preview.freeze_retention_snapshot(base, 1.0, expected)

            self.assertTrue(append_finished.wait(2.0))
            self.assertEqual(append_result, [True])
            pointer = raw_segments.strict_read_current_pointer(base)
            new_path = pathlib.Path(pointer["current"]["prompt_usage"]["path"])
            self.assertNotEqual(new_path, old_path)
            self.assertNotIn("after-swap", old_path.read_text(encoding="utf-8"))
            self.assertIn("after-swap", new_path.read_text(encoding="utf-8"))

    def test_retention_segment_apply_does_not_unlink_sources_when_manifest_write_fails(self) -> None:
        raw_segments = load_module("raw_segments_segment_apply_manifest_fail_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})

            plan = raw_segments.plan_segments_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            with mock.patch.object(raw_segments._retention, "write_manifest", side_effect=OSError("manifest write failed")):
                with self.assertRaises(OSError):
                    raw_segments.apply_segment_plans(base, plan)
            old_exists = old_segment.exists()
            manifest_id = raw_segments.strict_read_manifest(base)["segments"][0]["id"]

        self.assertTrue(old_exists)
        self.assertEqual(manifest_id, segment["id"])

    def test_retention_segment_apply_retries_manifest_before_unlinking_marker_sources(self) -> None:
        raw_segments = load_module("raw_segments_segment_apply_retry_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            retained_segment = archive / "prompt-usage.raw.jsonl.20260520000000.20260520000000.retained.jsonl.gz"
            old_payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            retained_payload = (json.dumps(_turn_raw("s2", "new", total=200) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(old_payload)
            with gzip.open(retained_segment, "wb") as handle:
                handle.write(retained_payload)
            old = _raw_segment(old_segment, segment_id="prompt-usage.raw.jsonl.old", payload=old_payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            retained = _raw_segment(
                retained_segment,
                segment_id="prompt-usage.raw.jsonl.retained",
                payload=retained_payload,
                min_time=1779235200.0,
                max_time=1779235200.0,
                rows=1,
                days=[[1779235200, 1, len(retained_payload)]],
            )
            previous_manifest = raw_segments.empty_manifest(base) | {"segments": [old]}
            next_manifest = raw_segments.empty_manifest(base) | {"segments": [retained]}
            raw_segments.write_manifest(base, previous_manifest)
            raw_segments.write_apply_marker(
                base,
                {
                    "phase": "manifest_pending",
                    "previous_manifest": previous_manifest,
                    "source_segments": [old],
                    "retained_segments": [retained],
                    "next_manifest": next_manifest,
                },
            )

            raw_segments.reconcile_apply_marker(base)
            old_exists = old_segment.exists()
            manifest_segments = raw_segments.strict_read_manifest(base)["segments"]

        self.assertFalse(old_exists)
        self.assertEqual(manifest_segments, [retained])

    def test_retention_segment_apply_keeps_unlink_pending_when_source_unlink_fails(self) -> None:
        raw_segments = load_module("raw_segments_segment_apply_pending_unlink_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(old_segment, payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})
            plan = raw_segments.plan_segments_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())

            with mock.patch.object(
                raw_segments._state,
                "sweep_segment_sources",
                return_value={"deleted_files": 0, "pending_source_segments": [segment], "errors": [{"path": str(old_segment), "error": "busy"}]},
            ):
                result = raw_segments.apply_segment_plans(base, plan)
            marker = raw_segments.read_apply_marker(base)
            manifest_segments = raw_segments.strict_read_manifest(base)["segments"]
            still_exists_after_failure = old_segment.exists()

            retry = raw_segments.sweep_apply_marker(base)
            marker_after_retry = raw_segments.read_apply_marker(base)

        self.assertTrue(result["physical_delete_pending"])
        self.assertEqual(result["pending_files"], 1)
        self.assertEqual(marker["phase"], "unlink_pending")
        self.assertEqual(marker["unlink_pending_segments"], [segment])
        self.assertEqual(manifest_segments, [])
        self.assertTrue(still_exists_after_failure)
        self.assertEqual(retry["pending_files"], 0)
        self.assertIsNone(marker_after_retry)

    def test_retention_segment_apply_rejects_manifest_applied_extra_source(self) -> None:
        raw_segments = load_module("raw_segments_segment_apply_extra_source_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            extra_segment = archive / "prompt-usage.raw.jsonl.20260502000000.20260502000000.2.jsonl.gz"
            old_payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            extra_payload = (json.dumps(_turn_raw("s2", "extra", total=100) | {"captured_at": "2026-05-02T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(old_payload)
            with gzip.open(extra_segment, "wb") as handle:
                handle.write(extra_payload)
            old = _raw_segment(old_segment, segment_id="prompt-usage.raw.jsonl.old", payload=old_payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            extra = _raw_segment(
                extra_segment, segment_id="prompt-usage.raw.jsonl.extra", payload=extra_payload, min_time=1777680000.0, max_time=1777680000.0, rows=1
            )
            previous_manifest = raw_segments.empty_manifest(base) | {"segments": [old]}
            next_manifest = raw_segments.empty_manifest(base) | {"segments": []}
            raw_segments.write_manifest(base, next_manifest)
            raw_segments.write_apply_marker(
                base,
                {
                    "phase": "unlink_pending",
                    "previous_manifest": previous_manifest,
                    "source_segments": [old, extra],
                    "retained_segments": [],
                    "next_manifest": next_manifest,
                },
            )

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.reconcile_apply_marker(base)
            old_exists = old_segment.exists()
            extra_exists = extra_segment.exists()
            marker_exists = raw_segments.segment_apply_marker_path(base).exists()

        self.assertTrue(old_exists)
        self.assertTrue(extra_exists)
        self.assertTrue(marker_exists)

    def test_retention_segment_apply_rejects_manifest_pending_missing_source(self) -> None:
        raw_segments = load_module("raw_segments_segment_apply_missing_source_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(payload)
            old = _raw_segment(old_segment, segment_id="prompt-usage.raw.jsonl.old", payload=payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            previous_manifest = raw_segments.empty_manifest(base) | {"segments": [old]}
            next_manifest = raw_segments.empty_manifest(base) | {"segments": []}
            raw_segments.write_manifest(base, previous_manifest)
            raw_segments.write_apply_marker(
                base,
                {
                    "phase": "manifest_pending",
                    "previous_manifest": previous_manifest,
                    "source_segments": [old],
                    "retained_segments": [],
                    "next_manifest": next_manifest,
                },
            )
            old_segment.unlink()

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.reconcile_apply_marker(base)
            manifest_segments = raw_segments.strict_read_manifest(base)["segments"]
            marker_exists = raw_segments.segment_apply_marker_path(base).exists()

        self.assertEqual(manifest_segments, [old])
        self.assertTrue(marker_exists)

    def test_retention_segment_apply_rejects_manifest_drift_before_write(self) -> None:
        raw_segments = load_module("raw_segments_segment_apply_manifest_drift_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            other_segment = archive / "prompt-usage.raw.jsonl.20260523000000.20260523000000.2.jsonl.gz"
            old_payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            other_payload = (json.dumps(_turn_raw("s2", "other", total=100) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(old_payload)
            with gzip.open(other_segment, "wb") as handle:
                handle.write(other_payload)
            old = _raw_segment(old_segment, segment_id="prompt-usage.raw.jsonl.old", payload=old_payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            other = _raw_segment(
                other_segment, segment_id="prompt-usage.raw.jsonl.other", payload=other_payload, min_time=1779494400.0, max_time=1779494400.0, rows=1
            )
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [old]})
            plan = raw_segments.plan_segments_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            drifted_manifest = raw_segments.empty_manifest(base) | {"segments": [old, other]}
            raw_segments.write_manifest(base, drifted_manifest)

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.apply_segment_plans(base, plan)
            manifest_segments = raw_segments.strict_read_manifest(base)["segments"]
            old_exists = old_segment.exists()
            other_exists = other_segment.exists()

        self.assertEqual(manifest_segments, [old, other])
        self.assertTrue(old_exists)
        self.assertTrue(other_exists)

    def test_retention_segment_apply_does_not_publish_retained_when_marker_write_fails(self) -> None:
        raw_segments = load_module("raw_segments_segment_apply_marker_fail_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            mixed_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260523000000.1.jsonl.gz"
            old_payload = json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            new_payload = json.dumps(_turn_raw("s2", "new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n"
            payload = (old_payload + new_payload).encode("utf-8")
            with gzip.open(mixed_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(
                mixed_segment,
                payload=payload,
                min_time=1777593600.0,
                max_time=1779494400.0,
                rows=2,
                days=[[1777593600, 1, len(old_payload.encode("utf-8"))], [1779494400, 1, len(new_payload.encode("utf-8"))]],
            )
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})
            plan = raw_segments.plan_segments_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            retained_path = pathlib.Path(plan["retained_segments"][0]["path"])

            with mock.patch.object(raw_segments._retention, "write_apply_marker", side_effect=OSError("marker write failed")):
                with self.assertRaises(OSError):
                    raw_segments.apply_segment_plans(base, plan)
            retained_exists = retained_path.exists()
            compatibility_paths = sorted(archive.glob("prompt-usage.raw.jsonl.*.gz"))

        self.assertFalse(retained_exists)
        self.assertEqual(compatibility_paths, [mixed_segment])

    def test_retention_segment_apply_rejects_stale_retained_metadata_in_next_manifest(self) -> None:
        raw_segments = load_module("raw_segments_segment_apply_stale_retained_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            old_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260501000000.1.jsonl.gz"
            retained_segment = archive / "prompt-usage.raw.jsonl.20260520000000.20260520000000.retained.jsonl.gz"
            old_payload = (json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n").encode("utf-8")
            retained_payload = (json.dumps(_turn_raw("s2", "new", total=200) | {"captured_at": "2026-05-20T00:00:00+00:00"}) + "\n").encode("utf-8")
            with gzip.open(old_segment, "wb") as handle:
                handle.write(old_payload)
            with gzip.open(retained_segment, "wb") as handle:
                handle.write(retained_payload)
            old = _raw_segment(old_segment, segment_id="prompt-usage.raw.jsonl.old", payload=old_payload, min_time=1777593600.0, max_time=1777593600.0, rows=1)
            retained = _raw_segment(
                retained_segment,
                segment_id="prompt-usage.raw.jsonl.retained",
                payload=retained_payload,
                min_time=1779235200.0,
                max_time=1779235200.0,
                rows=1,
                days=[[1779235200, 1, len(retained_payload)]],
            )
            stale_retained = dict(retained)
            stale_retained["rows"] = 999
            previous_manifest = raw_segments.empty_manifest(base) | {"segments": [old]}
            next_manifest = raw_segments.empty_manifest(base) | {"segments": [stale_retained]}
            raw_segments.write_manifest(base, previous_manifest)
            raw_segments.write_apply_marker(
                base,
                {
                    "phase": "manifest_pending",
                    "previous_manifest": previous_manifest,
                    "source_segments": [old],
                    "retained_segments": [retained],
                    "next_manifest": next_manifest,
                },
            )

            with self.assertRaises(raw_segments.ManifestError):
                raw_segments.reconcile_apply_marker(base)
            manifest_segments = raw_segments.strict_read_manifest(base)["segments"]
            old_exists = old_segment.exists()

        self.assertEqual(manifest_segments, [old])
        self.assertTrue(old_exists)

    def test_retention_segment_mixed_rewrite_reports_deleted_bytes(self) -> None:
        raw_segments = load_module("raw_segments_mixed_deleted_bytes_test", ROOT / "scripts" / "raw_segments.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            archive = base / "raw" / "archive"
            archive.mkdir(parents=True)
            mixed_segment = archive / "prompt-usage.raw.jsonl.20260501000000.20260523000000.1.jsonl.gz"
            old_payload = json.dumps(_turn_raw("s1", "old", total=100) | {"captured_at": "2026-05-01T00:00:00+00:00"}) + "\n"
            new_payload = json.dumps(_turn_raw("s2", "new", total=200) | {"captured_at": "2026-05-23T00:00:00+00:00"}) + "\n"
            payload = (old_payload + new_payload).encode("utf-8")
            with gzip.open(mixed_segment, "wb") as handle:
                handle.write(payload)
            segment = _raw_segment(
                mixed_segment,
                payload=payload,
                min_time=1777593600.0,
                max_time=1779494400.0,
                rows=2,
                days=[[1777593600, 1, len(old_payload.encode("utf-8"))], [1779494400, 1, len(new_payload.encode("utf-8"))]],
            )
            raw_segments.write_manifest(base, raw_segments.empty_manifest(base) | {"segments": [segment]})

            plan = raw_segments.plan_segments_older_than(base, datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
            result = raw_segments.apply_segment_plans(base, plan)

        self.assertEqual(plan["deleted_rows"], 1)
        self.assertGreater(plan["deleted_bytes"], 0)
        self.assertGreater(result["deleted_bytes"], 0)
