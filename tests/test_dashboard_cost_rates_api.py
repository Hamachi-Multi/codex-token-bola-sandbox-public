from __future__ import annotations

try:
    from tests.support import ROOT, json, load_module, pathlib, sqlite3, tempfile, unittest
except ModuleNotFoundError:
    from support import ROOT, json, load_module, pathlib, sqlite3, tempfile, unittest


class DashboardCostRatesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = load_module("dashboard_cost_rates_recalculate_api_test", ROOT / "scripts" / "dashboard_cost_rates_api.py")
        self.schema = load_module("dashboard_cost_rates_recalculate_schema_test", ROOT / "scripts" / "build_analytics_schema.py")

    def create_database(self, path: pathlib.Path) -> None:
        con = sqlite3.connect(path)
        self.schema.setup_db(con)
        con.execute(
            "insert into run_metadata values (?, ?)",
            ("analytics_schema_version", json.dumps(self.schema.ANALYTICS_SCHEMA_VERSION)),
        )
        con.execute(
            """
            insert into turns (
              session_id, turn_id, captured_at_unix, started_at_unix, model,
              analytics_eligible, estimated, non_cached_input_tokens,
              cached_input_tokens, output_tokens, weighted_credits,
              cost_pico_usd, cost_rate_status
            ) values ('s','t',1780272000,1780272000,'gpt-5.5',1,0,100,0,0,7,7,'configured')
            """
        )
        con.commit()
        con.close()

    def handler(self, *, root: pathlib.Path, db_path: pathlib.Path, body: dict[str, object]):
        handler = self.api.DashboardCostRatesApiMixin()
        manager = self.api.dashboard_operation_state.DashboardOperationManager()
        sent: list[tuple[dict[str, object], int]] = []
        handler.read_json_body = lambda: body
        handler.dashboard_operation_manager = lambda: manager
        handler.dashboard_output_dir = lambda: root
        handler.dashboard_db_path = lambda: db_path
        handler.dashboard_cost_rates_config_path = lambda: root / "cost-rates.json"
        handler.send_json = lambda payload, status=200: sent.append((payload, status))
        return handler, manager, sent

    def test_recalculate_endpoint_updates_costs_without_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            db_path = root / "analytics.sqlite"
            self.create_database(db_path)
            catalog, _revision = self.api.cost_rates.load_catalog(root / "cost-rates.json")
            handler, manager, sent = self.handler(
                root=root,
                db_path=db_path,
                body={"expected_catalog_digest": catalog.digest},
            )

            handler.handle_cost_rates_recalculate()

            con = sqlite3.connect(db_path)
            stored = con.execute("select weighted_credits, cost_pico_usd from turns").fetchone()
            con.close()

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], 200)
        self.assertTrue(sent[0][0]["ok"])
        self.assertEqual(sent[0][0]["catalog_digest"], catalog.digest)
        self.assertEqual(stored, (500.0, 500_000_000))
        self.assertFalse(manager.has_active_operation())

    def test_recalculate_endpoint_rejects_missing_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            handler, manager, sent = self.handler(root=root, db_path=root / "missing.sqlite", body={})

            handler.handle_cost_rates_recalculate()

        self.assertEqual(sent, [({
            "error": "cost_rate_catalog_digest_required",
            "message": "The current cost rate catalog digest is required",
        }, 400)])
        self.assertFalse(manager.has_active_operation())

    def test_recalculate_endpoint_reports_active_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            handler, manager, sent = self.handler(root=root, db_path=root / "missing.sqlite", body={"expected_catalog_digest": "digest"})
            lease = manager.begin("cleanup", root)
            try:
                handler.handle_cost_rates_recalculate()
            finally:
                lease.close()

        self.assertEqual(sent[0][1], 409)
        self.assertEqual(sent[0][0]["error"], "analysis_or_cleanup_running")
        self.assertEqual(sent[0][0]["operation"], "cleanup")

    def test_reset_all_restores_built_in_rates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            config_path = root / "cost-rates.json"
            _catalog, revision = self.api.cost_rates.load_catalog(config_path)
            _catalog, updated_revision = self.api.cost_rates.update_custom_rates(
                action="upsert",
                expected_revision=revision,
                rate_payload={
                    "model_id": "gpt-5.5",
                    "effective_from": None,
                    "is_default": True,
                    "input_price": "4",
                    "cached_input_price": "0.4",
                    "output_price": "24",
                },
                path=config_path,
            )
            handler, _manager, sent = self.handler(
                root=root,
                db_path=root / "missing.sqlite",
                body={
                    "action": "reset_all",
                    "expected_revision": updated_revision,
                    "confirm_reset_all": True,
                },
            )

            handler.handle_cost_rates_post()
            custom, _revision = self.api.cost_rates.read_custom_rates(config_path)

        self.assertEqual(sent[0][1], 200)
        self.assertTrue(sent[0][0]["ok"])
        self.assertEqual(sent[0][0]["custom_rate_count"], 0)
        self.assertEqual(custom, [])

    def test_reset_all_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)
            handler, _manager, sent = self.handler(
                root=root,
                db_path=root / "missing.sqlite",
                body={"action": "reset_all", "expected_revision": "revision"},
            )

            handler.handle_cost_rates_post()

        self.assertEqual(sent, [({
            "error": "cost_rates_reset_confirmation_required",
            "message": "Resetting all custom cost rates requires confirmation",
        }, 400)])


if __name__ == "__main__":
    unittest.main()
