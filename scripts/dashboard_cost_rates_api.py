"""Cost-rate configuration endpoints for the local dashboard."""

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
import cost_unit_recalculation
import dashboard_operation_state
import service_lock


def _analytics_metadata(db_path: pathlib.Path) -> dict[str, Any]:
    if not db_path.is_file():
        return {}
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute("select key, value from run_metadata").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if con is not None:
            con.close()
    result: dict[str, Any] = {}
    for key, value in rows:
        try:
            result[str(key)] = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            result[str(key)] = value
    return result


def _detected_models(db_path: pathlib.Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            """
            select coalesce(nullif(trim(model),''),'unknown') model_id,
                   count(*) turns,
                   sum(case when weighted_credits is null then 1 else 0 end) unpriced_turns,
                   min(coalesce(started_at_unix,captured_at_unix)) first_used_unix,
                   max(coalesce(started_at_unix,captured_at_unix)) last_used_unix
            from turns
            group by coalesce(nullif(trim(model),''),'unknown')
            order by count(*) desc, model_id
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if con is not None:
            con.close()
    return [
        {
            "model_id": str(model_id),
            "turns": int(turns or 0),
            "unpriced_turns": int(unpriced_turns or 0),
            "first_used_unix": first_used,
            "last_used_unix": last_used,
        }
        for model_id, turns, unpriced_turns, first_used, last_used in rows
    ]


def cost_rates_payload(*, config_path: pathlib.Path, db_path: pathlib.Path) -> dict[str, Any]:
    catalog, custom, deleted_builtin_rates, revision = cost_rates.load_catalog_state(config_path)
    custom_keys = {rate.key() for rate in custom}
    builtin_keys = {rate.key() for rate in cost_rates.BUILTIN_RATES}
    now = time.time()
    detected = _detected_models(db_path)
    detected_by_model = {row["model_id"]: row for row in detected}
    model_ids = sorted(set(catalog.by_model) | set(detected_by_model))
    models: list[dict[str, Any]] = []
    for model_id in model_ids:
        history = []
        for rate in reversed(catalog.by_model.get(model_id, ())):
            item = rate.public_payload()
            item["override"] = rate.key() in custom_keys and rate.key() in builtin_keys
            item["custom_only"] = rate.key() in custom_keys and rate.key() not in builtin_keys
            item["deletable"] = not rate.is_default
            history.append(item)
        current = catalog.resolve(model_id, now)
        detected_row = detected_by_model.get(model_id, {})
        first_rate = next(iter(catalog.by_model.get(model_id, ())), None)
        first_used = detected_row.get("first_used_unix")
        coverage_required = bool(
            detected_row
            and (first_rate is None or first_used is None or float(first_used) < first_rate.effective_unix)
        )
        if model_id == "unknown":
            status = "unavailable"
        elif current is None or coverage_required:
            status = "setup_required"
        else:
            status = "configured"
        models.append(
            {
                "model_id": model_id,
                "status": status,
                "detected": bool(detected_row),
                "turns": int(detected_row.get("turns") or 0),
                "unpriced_turns": int(detected_row.get("unpriced_turns") or 0),
                "coverage_required": coverage_required,
                "first_used_unix": first_used,
                "last_used_unix": detected_row.get("last_used_unix"),
                "current": current.public_payload() if current is not None else None,
                "history": history,
            }
        )
    models.sort(key=lambda row: (not row["detected"], -row["turns"], row["model_id"]))
    metadata = _analytics_metadata(db_path)
    applied_digest = str(metadata.get("cost_rate_catalog_digest") or "")
    return {
        "schema_version": cost_rates.SCHEMA_VERSION,
        "revision": revision,
        "custom_rate_count": len(custom),
        "custom_change_count": len(custom) + len(deleted_builtin_rates),
        "catalog_digest": catalog.digest,
        "analytics_catalog_digest": applied_digest or None,
        "rebuild_required": bool(db_path.is_file() and applied_digest != catalog.digest),
        "unit": "USD per 1M tokens",
        "cost_units_per_usd": cost_rates.COST_UNITS_PER_USD,
        "models": models,
    }


class DashboardCostRatesApiMixin:
    def dashboard_cost_rates_config_path(self) -> pathlib.Path:
        return cost_rates.config_path()

    def handle_cost_rates_get(self) -> None:
        try:
            self.send_json(
                cost_rates_payload(
                    config_path=self.dashboard_cost_rates_config_path(),
                    db_path=self.dashboard_db_path(),
                )
            )
        except cost_rates.CostRateError as exc:
            self.send_json(exc.payload(), 409)

    def handle_cost_rates_post(self) -> None:
        body = self.read_json_body()
        action = str(body.get("action") or "")
        expected_revision = str(body.get("expected_revision") or "")
        rate_payload = body.get("rate")
        if action == "reset_all" and body.get("confirm_reset_all") is not True:
            self.send_json(
                {
                    "error": "cost_rates_reset_confirmation_required",
                    "message": "Resetting all custom cost rates requires confirmation",
                },
                400,
            )
            return
        if action != "reset_all" and not isinstance(rate_payload, dict):
            self.send_json({"error": "cost_rate_invalid", "message": "Cost rate must be an object"}, 400)
            return
        try:
            with service_lock.acquire_service_lock(reason="cost-rates", output_dir=self.dashboard_output_dir()):
                if action == "reset_all":
                    cost_rates.reset_all_custom_rates(
                        expected_revision=expected_revision,
                        path=self.dashboard_cost_rates_config_path(),
                    )
                else:
                    cost_rates.update_custom_rates(
                        action=action,
                        expected_revision=expected_revision,
                        rate_payload=rate_payload,
                        path=self.dashboard_cost_rates_config_path(),
                    )
        except service_lock.ServiceLockBusy as exc:
            self.send_json(dashboard_operation_state.service_busy_payload(lock_path=exc.path), 409)
            return
        except cost_rates.CostRateRevisionConflict as exc:
            self.send_json(exc.payload(), 409)
            return
        except cost_rates.CostRateError as exc:
            status = 404 if exc.error == "cost_rate_not_found" else 400
            self.send_json(exc.payload(), status)
            return
        self.send_json(
            {
                "ok": True,
                **cost_rates_payload(
                    config_path=self.dashboard_cost_rates_config_path(),
                    db_path=self.dashboard_db_path(),
                ),
            }
        )

    def handle_cost_rates_recalculate(self) -> None:
        body = self.read_json_body()
        expected_catalog_digest = body.get("expected_catalog_digest")
        if not isinstance(expected_catalog_digest, str) or not expected_catalog_digest:
            self.send_json(
                {
                    "error": "cost_rate_catalog_digest_required",
                    "message": "The current cost rate catalog digest is required",
                },
                400,
            )
            return

        manager = self.dashboard_operation_manager()
        try:
            lease = manager.begin("cost_recalculation", self.dashboard_output_dir())
        except dashboard_operation_state.ServerShuttingDown:
            self.send_json({"error": "server_shutting_down"}, 503)
            return
        except dashboard_operation_state.OperationBusy:
            self.send_json(manager.busy_payload(), 409)
            return

        try:
            try:
                with service_lock.acquire_service_lock(
                    reason="cost-recalculation",
                    output_dir=self.dashboard_output_dir(),
                ):
                    catalog, _revision = cost_rates.load_catalog(self.dashboard_cost_rates_config_path())
                    result = cost_unit_recalculation.recalculate_cost_units(
                        db_path=self.dashboard_db_path(),
                        catalog=catalog,
                        expected_catalog_digest=expected_catalog_digest,
                    )
            except service_lock.ServiceLockBusy as exc:
                self.send_json(dashboard_operation_state.service_busy_payload(lock_path=exc.path), 409)
                return
            except cost_unit_recalculation.CostUnitRecalculationError as exc:
                self.send_json(exc.payload(), 409)
                return
            except cost_rates.CostRateError as exc:
                self.send_json(exc.payload(), 409)
                return
            self.send_json({"ok": True, **result})
        finally:
            lease.close()
