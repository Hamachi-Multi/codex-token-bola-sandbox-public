from __future__ import annotations

import sqlite3
import unittest

from tests.support import ROOT, _turn_normalized, load_module


class UnavailableTurnAnalyticsTests(unittest.TestCase):
    def test_resolved_token_data_outranks_unavailable_lifecycle_variant(self) -> None:
        build = load_module("build_resolution_rank_test", ROOT / "scripts" / "build_analytics.py")
        normalize = load_module("normalize_resolution_rank_test", ROOT / "scripts" / "normalize.py")
        resolved = {"turn_status": "aborted", "token_resolution_status": "resolved", "estimated": True}
        unavailable = {"turn_status": "completed", "token_resolution_status": "unavailable", "estimated": False}

        self.assertGreater(build.row_turn_rank(resolved), build.row_turn_rank(unavailable))
        self.assertGreater(normalize.rank(resolved), normalize.rank(unavailable))

    def test_unavailable_turn_is_visible_but_excluded_from_analytics(self) -> None:
        build = load_module("build_unavailable_turn_test", ROOT / "scripts" / "build_analytics.py")
        queries = load_module("dashboard_unavailable_turn_test", ROOT / "scripts" / "dashboard_queries.py")
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            build.setup_db(con)
            resolved = _turn_normalized("s-resolved", "t-resolved", total=100) | {
                "started_at": "2026-01-01T00:00:00+00:00",
                "token_resolution_status": "resolved",
            }
            unavailable = _turn_normalized("s-unavailable", "t-unavailable", total=900) | {
                "started_at": "2026-01-01T00:00:01+00:00",
                "token_resolution_status": "unavailable",
                "token_resolution_reason": "no_token_count_before_task_complete",
            }
            build.upsert_turn_row(con, resolved, {})
            build.upsert_turn_row(con, unavailable, {})
            con.commit()

            stored = dict(
                con.execute(
                    """
                    select token_resolution_status, token_resolution_reason, analytics_eligible,
                           total_tokens, weighted_credits, model_call_count
                    from turns
                    where session_id='s-unavailable' and turn_id='t-unavailable'
                    """
                ).fetchone()
            )
            dashboard = queries.DashboardQueries(
                con,
                {"days": ["0"], "page": ["1"], "per_page": ["25"]},
            )
            summary = dashboard.summary_payload()
            turns = dashboard.turns_payload()
            detail = queries.DashboardQueries(
                con,
                {
                    "days": ["0"],
                    "session_id": ["s-unavailable"],
                    "turn_id": ["t-unavailable"],
                },
            ).turn_payload()
        finally:
            con.close()

        self.assertEqual(stored["token_resolution_status"], "unavailable")
        self.assertEqual(stored["token_resolution_reason"], "no_token_count_before_task_complete")
        self.assertEqual(stored["analytics_eligible"], 0)
        self.assertEqual(stored["total_tokens"], 0)
        self.assertIsNone(stored["weighted_credits"])
        self.assertEqual(stored["model_call_count"], 0)
        self.assertEqual(summary["turns"], 1)
        self.assertEqual(summary["total_tokens"], 100)
        self.assertEqual(summary["unavailable_turns"], 1)
        self.assertEqual(turns["total"], 2)
        unavailable_row = next(row for row in turns["rows"] if row["turn_id"] == "t-unavailable")
        self.assertEqual(unavailable_row["token_resolution_status"], "unavailable")
        self.assertEqual(unavailable_row["token_data_available"], 0)
        self.assertEqual(detail["turn"]["token_resolution_status"], "unavailable")
        self.assertEqual(detail["turn"]["token_data_available"], 0)
        self.assertEqual(detail["model_call_total"], 0)
        self.assertEqual(detail["tool_call_total"], 0)


if __name__ == "__main__":
    unittest.main()
