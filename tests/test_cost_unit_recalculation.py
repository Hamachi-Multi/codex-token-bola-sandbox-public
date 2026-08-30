from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, mock, pathlib, sqlite3, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, mock, pathlib, sqlite3, tempfile, unittest


class CostUnitRecalculationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = load_module("cost_unit_recalculation_schema_test", ROOT / "scripts" / "build_analytics_schema.py")
        self.recalculation = load_module("cost_unit_recalculation_test", ROOT / "scripts" / "cost_unit_recalculation.py")
        self.cost_rates = self.recalculation.cost_rates

    def create_database(self, path: pathlib.Path) -> None:
        con = sqlite3.connect(path)
        self.schema.setup_db(con)
        con.execute(
            "insert into run_metadata values (?, ?)",
            ("analytics_schema_version", json.dumps(self.schema.ANALYTICS_SCHEMA_VERSION)),
        )
        con.commit()
        con.close()

    def test_recalculates_turns_rollups_and_cost_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            db_path = root / "analytics.sqlite"
            self.create_database(db_path)
            catalog, _revision = self.cost_rates.load_catalog(root / "missing-cost-rates.json")
            con = sqlite3.connect(db_path)
            con.executemany(
                """
                insert into turns (
                  session_id, turn_id, captured_at_unix, started_at_unix, model,
                  analytics_eligible, estimated, non_cached_input_tokens,
                  cached_input_tokens, output_tokens, weighted_credits,
                  cost_pico_usd, cost_rate_status
                ) values (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("parent", "p1", 1_780_272_000, 1_780_272_000, "gpt-5.5", 1, 2_000_000, 1_000_000, 100_000, 1.0, 1, "configured"),
                    ("child", "c1", 1_780_272_000, 1_780_272_000, "gpt-5.5", 1, 1_000_000, 0, 0, 1.0, 1, "configured"),
                    ("child", "c2", 1_780_272_000, 1_780_272_000, "unpriced-model", 1, 1_000, 0, 0, 1.0, 1, "configured"),
                    ("priced-child", "c3", 1_780_272_000, 1_780_272_000, "gpt-5.5", 1, 400_000, 0, 0, 1.0, 1, "configured"),
                    ("unavailable", "u1", 1_780_272_000, 1_780_272_000, "gpt-5.5", 0, 100, 0, 0, 1.0, 1, "configured"),
                ],
            )
            con.executemany(
                """
                insert into task_rollups (
                  parent_session_id, parent_turn_id, child_session_id, confidence,
                  own_total_tokens, child_total_tokens, total_tokens,
                  own_weighted_credits, child_weighted_credits, total_weighted_credits
                ) values (?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                """,
                [
                    ("parent", "p1", "child", "spawn_call_turn_context", 1.0, 1.0, 2.0),
                    ("pruned-parent", "missing", "priced-child", "parent_pruned_by_retention", 1.0, 1.0, 2.0),
                ],
            )
            con.commit()
            con.close()

            result = self.recalculation.recalculate_cost_units(
                db_path=db_path,
                catalog=catalog,
                expected_catalog_digest=catalog.digest,
            )

            con = sqlite3.connect(db_path)
            turns = {
                (session_id, turn_id): (credits, pico, status, effective_from)
                for session_id, turn_id, credits, pico, status, effective_from in con.execute(
                    """
                    select session_id, turn_id, weighted_credits, cost_pico_usd,
                           cost_rate_status, cost_rate_effective_from
                    from turns
                    """
                )
            }
            rollups = {
                child: (own, child_cost, total)
                for child, own, child_cost, total in con.execute(
                    """
                    select child_session_id, own_weighted_credits,
                           child_weighted_credits, total_weighted_credits
                    from task_rollups
                    """
                )
            }
            metadata = {
                key: json.loads(value)
                for key, value in con.execute(
                    "select key, value from run_metadata where key in ('cost_rate_catalog_digest','cost_rate_applied_at_unix','unpriced_turn_rows')"
                )
            }
            con.close()

        self.assertEqual(turns[("parent", "p1")], (13_500_000.0, 13_500_000_000_000, "configured", None))
        self.assertEqual(turns[("child", "c1")], (5_000_000.0, 5_000_000_000_000, "configured", None))
        self.assertEqual(turns[("child", "c2")], (None, None, "unconfigured", None))
        self.assertEqual(turns[("unavailable", "u1")], (None, None, "unavailable", None))
        self.assertEqual(rollups["child"], (13_500_000.0, None, None))
        self.assertEqual(rollups["priced-child"], (0.0, 2_000_000.0, 2_000_000.0))
        self.assertEqual(metadata["cost_rate_catalog_digest"], catalog.digest)
        self.assertGreater(metadata["cost_rate_applied_at_unix"], 0)
        self.assertEqual(metadata["unpriced_turn_rows"], 1)
        self.assertEqual(result["recalculated_turns"], 5)
        self.assertEqual(result["priced_turns"], 3)
        self.assertEqual(result["unpriced_turns"], 1)
        self.assertEqual(result["unavailable_turns"], 1)
        self.assertEqual(result["recalculated_task_rollups"], 2)

    def test_stale_catalog_digest_does_not_modify_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            db_path = root / "analytics.sqlite"
            self.create_database(db_path)
            catalog, _revision = self.cost_rates.load_catalog(root / "missing-cost-rates.json")
            con = sqlite3.connect(db_path)
            con.execute(
                """
                insert into turns (
                  session_id, turn_id, captured_at_unix, model, analytics_eligible,
                  estimated, non_cached_input_tokens, cached_input_tokens,
                  output_tokens, weighted_credits, cost_pico_usd, cost_rate_status
                ) values ('s','t',1,'gpt-5.5',1,0,100,0,0,7,7,'configured')
                """
            )
            con.commit()
            con.close()

            with self.assertRaises(self.recalculation.CostUnitRecalculationError) as raised:
                self.recalculation.recalculate_cost_units(
                    db_path=db_path,
                    catalog=catalog,
                    expected_catalog_digest="stale",
                )

            con = sqlite3.connect(db_path)
            stored = con.execute("select weighted_credits, cost_pico_usd from turns").fetchone()
            con.close()

        self.assertEqual(raised.exception.error, "cost_rate_catalog_changed")
        self.assertEqual(stored, (7.0, 7))

    def test_unsupported_schema_requires_analyze_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            db_path = root / "analytics.sqlite"
            self.create_database(db_path)
            catalog, _revision = self.cost_rates.load_catalog(root / "missing-cost-rates.json")
            con = sqlite3.connect(db_path)
            con.execute("update run_metadata set value=? where key='analytics_schema_version'", (json.dumps(1),))
            con.execute(
                """
                insert into turns (
                  session_id, turn_id, captured_at_unix, model, analytics_eligible,
                  estimated, non_cached_input_tokens, cached_input_tokens,
                  output_tokens, weighted_credits, cost_pico_usd, cost_rate_status
                ) values ('s','t',1,'gpt-5.5',1,0,100,0,0,7,7,'configured')
                """
            )
            con.commit()
            con.close()

            with self.assertRaises(self.recalculation.CostUnitRecalculationError) as raised:
                self.recalculation.recalculate_cost_units(
                    db_path=db_path,
                    catalog=catalog,
                    expected_catalog_digest=catalog.digest,
                )

            con = sqlite3.connect(db_path)
            stored = con.execute("select weighted_credits, cost_pico_usd from turns").fetchone()
            con.close()

        self.assertEqual(raised.exception.error, "cost_recalculation_requires_analyze")
        self.assertEqual(stored, (7.0, 7))

    def test_mid_recalculation_failure_rolls_back_turns_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            db_path = root / "analytics.sqlite"
            self.create_database(db_path)
            catalog, _revision = self.cost_rates.load_catalog(root / "missing-cost-rates.json")
            con = sqlite3.connect(db_path)
            con.executemany(
                """
                insert into turns (
                  session_id, turn_id, captured_at_unix, model, analytics_eligible,
                  estimated, non_cached_input_tokens, cached_input_tokens,
                  output_tokens, weighted_credits, cost_pico_usd, cost_rate_status
                ) values (?, ?, 1, 'gpt-5.5', 1, 0, 100, 0, 0, 7, 7, 'configured')
                """,
                [("s", "t1"), ("s", "t2")],
            )
            con.commit()
            con.close()
            original = self.cost_rates.priced_usage
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("injected failure")
                return original(*args, **kwargs)

            with mock.patch.object(self.recalculation.cost_rates, "priced_usage", side_effect=fail_second):
                with self.assertRaises(RuntimeError):
                    self.recalculation.recalculate_cost_units(
                        db_path=db_path,
                        catalog=catalog,
                        expected_catalog_digest=catalog.digest,
                    )

            con = sqlite3.connect(db_path)
            turns = con.execute("select weighted_credits, cost_pico_usd from turns order by turn_id").fetchall()
            digest = con.execute("select value from run_metadata where key='cost_rate_catalog_digest'").fetchone()
            con.close()

        self.assertEqual(turns, [(7.0, 7), (7.0, 7)])
        self.assertIsNone(digest)


if __name__ == "__main__":
    unittest.main()
