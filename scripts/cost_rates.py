"""Model-specific default and effective-dated Cost Units rates."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

import service_paths


SCHEMA_VERSION = 1
COST_UNITS_PER_USD = 1_000_000
PICO_USD_PER_COST_UNIT = 1_000_000
PRICE_SCALE = Decimal(PICO_USD_PER_COST_UNIT)
MAX_PRICE = Decimal("100000")
SQLITE_INTEGER_MAX = (1 << 63) - 1
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class CostRateError(RuntimeError):
    def __init__(self, error: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.error = error
        self.field = field

    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = {"error": self.error, "message": str(self)}
        if self.field:
            value["field"] = self.field
        return value


class CostRateRevisionConflict(CostRateError):
    def __init__(self, revision: str) -> None:
        super().__init__("cost_rates_revision_conflict", "Cost rates changed in another request")
        self.revision = revision

    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "revision": self.revision}


@dataclass(frozen=True)
class CostRate:
    model_id: str
    effective_from: str | None
    input_price: Decimal
    cached_input_price: Decimal
    output_price: Decimal
    source: str = "custom"
    source_url: str | None = None

    @property
    def effective_unix(self) -> float:
        if self.effective_from is None:
            return float("-inf")
        parsed = date.fromisoformat(self.effective_from)
        return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).timestamp()

    @property
    def is_default(self) -> bool:
        return self.effective_from is None

    def key(self) -> tuple[str, str | None]:
        return self.model_id, self.effective_from

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_id": self.model_id,
            "effective_from": self.effective_from,
            "is_default": self.is_default,
            "input_price": decimal_text(self.input_price),
            "cached_input_price": decimal_text(self.cached_input_price),
            "output_price": decimal_text(self.output_price),
            "source": self.source,
            "relative_ratio": {
                "input": "1",
                "cached_input": decimal_text(self.cached_input_price / self.input_price),
                "output": decimal_text(self.output_price / self.input_price),
            },
        }
        if self.source_url:
            payload["source_url"] = self.source_url
        return payload

    def storage_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_id": self.model_id,
            "effective_from": self.effective_from,
            "input_price": decimal_text(self.input_price),
            "cached_input_price": decimal_text(self.cached_input_price),
            "output_price": decimal_text(self.output_price),
        }
        if self.is_default:
            payload["is_default"] = True
        return payload


def _builtin(
    model_id: str,
    effective_from: str | None,
    input_price: str,
    cached_input_price: str,
    output_price: str,
    source_url: str,
) -> CostRate:
    return CostRate(
        model_id=model_id,
        effective_from=effective_from,
        input_price=Decimal(input_price),
        cached_input_price=Decimal(cached_input_price),
        output_price=Decimal(output_price),
        source="built-in",
        source_url=source_url,
    )


GPT_51_URL = "https://developers.openai.com/api/docs/models/gpt-5.1"
GPT_54_URL = "https://developers.openai.com/api/docs/models/gpt-5.4"
GPT_55_URL = "https://developers.openai.com/api/docs/models/gpt-5.5"
GPT_56_URL = "https://openai.com/index/gpt-5-6/"
GPT_56_REPRICE_URL = "https://openai.com/index/advancing-the-price-performance-frontier-with-gpt-5-6/"
GPT_56_SOL_URL = "https://developers.openai.com/api/docs/models/gpt-5.6-sol"


BUILTIN_RATES: tuple[CostRate, ...] = (
    _builtin("gpt-5.1", None, "1.25", "0.125", "10", GPT_51_URL),
    _builtin("gpt-5.1-codex", None, "1.25", "0.125", "10", GPT_51_URL),
    _builtin("gpt-5.1-codex-max", None, "1.25", "0.125", "10", GPT_51_URL),
    _builtin("gpt-5.1-codex-mini", None, "0.25", "0.025", "2", GPT_51_URL),
    _builtin("gpt-5.4", None, "2.5", "0.25", "15", GPT_54_URL),
    _builtin("gpt-5.4-mini", None, "0.75", "0.075", "4.5", GPT_54_URL),
    _builtin("gpt-5.4-nano", None, "0.2", "0.02", "1.25", GPT_54_URL),
    _builtin("gpt-5.5", None, "5", "0.5", "30", GPT_55_URL),
    _builtin("gpt-5.6", None, "5", "0.5", "30", GPT_56_URL),
    _builtin("gpt-5.6-sol", None, "5", "0.5", "30", GPT_56_URL),
    _builtin("gpt-5.6-terra", None, "2.5", "0.25", "15", GPT_56_URL),
    _builtin("gpt-5.6-luna", None, "1", "0.1", "6", GPT_56_URL),
    _builtin("gpt-5.6-terra", "2026-07-30", "2", "0.2", "12", GPT_56_REPRICE_URL),
    _builtin("gpt-5.6-luna", "2026-07-30", "0.2", "0.02", "1.2", GPT_56_REPRICE_URL),
    _builtin("gpt-5.6", "2026-08-21", "4", "0.4", "20", GPT_56_SOL_URL),
    _builtin("gpt-5.6-sol", "2026-08-21", "4", "0.4", "20", GPT_56_SOL_URL),
)


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def config_path(env: Mapping[str, str] | None = None) -> pathlib.Path:
    return service_paths.runtime_config_path(env).with_name("cost-rates.json")


def lock_path(env: Mapping[str, str] | None = None) -> pathlib.Path:
    return config_path(env).with_name("cost-rates.lock")


def _thread_lock(path: pathlib.Path) -> threading.Lock:
    key = str(path.expanduser().resolve(strict=False))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


@contextlib.contextmanager
def acquire_lock(path: pathlib.Path | None = None) -> Iterable[None]:
    target = pathlib.Path(path or lock_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    with _thread_lock(target):
        fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def parse_model_id(value: Any) -> str:
    model_id = str(value or "").strip()
    if not MODEL_ID_PATTERN.fullmatch(model_id):
        raise CostRateError("cost_rate_invalid", "Model ID is invalid", field="model_id")
    if model_id == "unknown":
        raise CostRateError("cost_rate_invalid", "The unknown model cannot have a cost rate", field="model_id")
    return model_id


def parse_effective_from(value: Any, *, is_default: bool = False) -> str | None:
    if is_default:
        if value not in (None, ""):
            raise CostRateError("cost_rate_invalid", "A default rate cannot have an effective date", field="effective_from")
        return None
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise CostRateError("cost_rate_invalid", "Effective date must use YYYY-MM-DD", field="effective_from") from exc
    if parsed.isoformat() != text:
        raise CostRateError("cost_rate_invalid", "Effective date must use YYYY-MM-DD", field="effective_from")
    return text


def parse_price(value: Any, field: str, *, positive: bool) -> Decimal:
    if isinstance(value, bool):
        raise CostRateError("cost_rate_invalid", "Price must be a decimal number", field=field)
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CostRateError("cost_rate_invalid", "Price must be a decimal number", field=field) from exc
    if not price.is_finite() or price > MAX_PRICE or (price <= 0 if positive else price < 0):
        condition = "greater than zero" if positive else "zero or greater"
        raise CostRateError("cost_rate_invalid", f"Price must be {condition}", field=field)
    try:
        within_precision = price == price.quantize(Decimal("0.000001"))
    except InvalidOperation:
        within_precision = False
    if not within_precision:
        raise CostRateError("cost_rate_invalid", "Price supports at most 6 decimal places", field=field)
    return price


def parse_rate(payload: Mapping[str, Any], *, source: str = "custom") -> CostRate:
    is_default = payload.get("is_default", False)
    if not isinstance(is_default, bool):
        raise CostRateError("cost_rate_invalid", "Default rate flag must be true or false", field="is_default")
    return CostRate(
        model_id=parse_model_id(payload.get("model_id")),
        effective_from=parse_effective_from(payload.get("effective_from"), is_default=is_default),
        input_price=parse_price(payload.get("input_price"), "input_price", positive=True),
        cached_input_price=parse_price(payload.get("cached_input_price"), "cached_input_price", positive=False),
        output_price=parse_price(payload.get("output_price"), "output_price", positive=False),
        source=source,
    )


def _canonical_payload(
    rates: Iterable[CostRate],
    deleted_builtin_rates: Iterable[tuple[str, str | None]] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "rates": [rate.storage_payload() for rate in sorted(rates, key=lambda item: (item.model_id, item.effective_unix))],
    }
    deleted = sorted(
        ({"model_id": model_id, "effective_from": effective_from} for model_id, effective_from in deleted_builtin_rates),
        key=lambda item: (item["model_id"], item["effective_from"] or ""),
    )
    if deleted:
        payload["deleted_builtin_rates"] = deleted
    return payload


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_custom_rate_config(
    path: pathlib.Path | None = None,
) -> tuple[list[CostRate], set[tuple[str, str | None]], str]:
    target = pathlib.Path(path or config_path()).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = _canonical_payload([])
        return [], set(), _digest(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CostRateError("cost_rates_config_invalid", f"Invalid cost rates config at {target}: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != SCHEMA_VERSION
        or not isinstance(raw.get("rates"), list)
        or not isinstance(raw.get("deleted_builtin_rates", []), list)
    ):
        raise CostRateError("cost_rates_config_invalid", f"Unsupported cost rates config at {target}")
    rates: list[CostRate] = []
    seen: set[tuple[str, str | None]] = set()
    for item in raw["rates"]:
        if not isinstance(item, dict):
            raise CostRateError("cost_rates_config_invalid", f"Invalid cost rate entry at {target}")
        rate = parse_rate(item)
        if rate.key() in seen:
            raise CostRateError("cost_rates_config_invalid", f"Duplicate cost rate at {target}: {rate.key()}")
        seen.add(rate.key())
        rates.append(rate)
    deleted_builtin_rates: set[tuple[str, str | None]] = set()
    for item in raw.get("deleted_builtin_rates", []):
        if not isinstance(item, dict):
            raise CostRateError("cost_rates_config_invalid", f"Invalid deleted built-in rate at {target}")
        key = (
            parse_model_id(item.get("model_id")),
            parse_effective_from(item.get("effective_from")),
        )
        if key in deleted_builtin_rates:
            raise CostRateError("cost_rates_config_invalid", f"Duplicate deleted built-in rate at {target}: {key}")
        if key in seen:
            raise CostRateError("cost_rates_config_invalid", f"Cost rate is both configured and deleted at {target}: {key}")
        deleted_builtin_rates.add(key)
    payload = _canonical_payload(rates, deleted_builtin_rates)
    return rates, deleted_builtin_rates, _digest(payload)


def read_custom_rates(path: pathlib.Path | None = None) -> tuple[list[CostRate], str]:
    rates, _deleted_builtin_rates, revision = read_custom_rate_config(path)
    return rates, revision


def write_custom_rates(
    rates: Iterable[CostRate],
    path: pathlib.Path | None = None,
    *,
    deleted_builtin_rates: Iterable[tuple[str, str | None]] = (),
) -> str:
    target = pathlib.Path(path or config_path()).expanduser()
    payload = _canonical_payload(rates, deleted_builtin_rates)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return _digest(payload)


class CostRateCatalog:
    def __init__(self, rates: Iterable[CostRate]) -> None:
        self.rates = tuple(sorted(rates, key=lambda item: (item.model_id, item.effective_unix)))
        by_model: dict[str, list[CostRate]] = {}
        for rate in self.rates:
            by_model.setdefault(rate.model_id, []).append(rate)
        self.by_model = {key: tuple(value) for key, value in by_model.items()}
        self.digest = _digest({"schema_version": SCHEMA_VERSION, "rates": [rate.storage_payload() for rate in self.rates]})

    def resolve(self, model_id: Any, timestamp: Any) -> CostRate | None:
        model = str(model_id or "").strip()
        try:
            instant = float(timestamp)
        except (TypeError, ValueError):
            return None
        candidates = self.by_model.get(model, ())
        selected: CostRate | None = None
        for rate in candidates:
            if rate.effective_unix > instant:
                break
            selected = rate
        return selected

    def cost_pico_usd(self, rate: CostRate, *, non_cached_input: int, cached_input: int, output: int) -> int:
        value = (
            Decimal(max(0, int(non_cached_input))) * rate.input_price
            + Decimal(max(0, int(cached_input))) * rate.cached_input_price
            + Decimal(max(0, int(output))) * rate.output_price
        ) * PRICE_SCALE
        if value > SQLITE_INTEGER_MAX:
            raise CostRateError("cost_rate_out_of_range", "Calculated cost exceeds the supported range")
        return int(value)


def priced_usage(
    catalog: CostRateCatalog,
    model: Any,
    started_at_unix: Any,
    *,
    non_cached_input: int,
    cached_input: int,
    output: int,
) -> tuple[int | None, float | None, CostRate | None]:
    """Price one stored usage snapshot with the supplied rate catalog."""
    rate = catalog.resolve(model, started_at_unix)
    if rate is None:
        return None, None, None
    pico_usd = catalog.cost_pico_usd(
        rate,
        non_cached_input=non_cached_input,
        cached_input=cached_input,
        output=output,
    )
    return pico_usd, pico_usd / PICO_USD_PER_COST_UNIT, rate


def load_catalog_state(
    path: pathlib.Path | None = None,
) -> tuple[CostRateCatalog, list[CostRate], set[tuple[str, str | None]], str]:
    custom, deleted_builtin_rates, revision = read_custom_rate_config(path)
    merged = {rate.key(): rate for rate in BUILTIN_RATES if rate.key() not in deleted_builtin_rates}
    merged.update({rate.key(): rate for rate in custom})
    return CostRateCatalog(merged.values()), custom, deleted_builtin_rates, revision


def load_catalog(path: pathlib.Path | None = None) -> tuple[CostRateCatalog, str]:
    catalog, _custom, _deleted_builtin_rates, revision = load_catalog_state(path)
    return catalog, revision


def update_custom_rates(
    *,
    action: str,
    expected_revision: str,
    rate_payload: Mapping[str, Any],
    path: pathlib.Path | None = None,
) -> tuple[CostRateCatalog, str]:
    target = pathlib.Path(path or config_path()).expanduser()
    lock_target = target.with_name("cost-rates.lock")
    with acquire_lock(lock_target):
        custom, deleted_builtin_rates, revision = read_custom_rate_config(target)
        if expected_revision != revision:
            raise CostRateRevisionConflict(revision)
        requested = parse_rate(rate_payload)
        key = requested.key()
        custom_by_key = {rate.key(): rate for rate in custom}
        builtin_by_key = {rate.key(): rate for rate in BUILTIN_RATES}
        if action == "upsert":
            custom_by_key[key] = requested
            deleted_builtin_rates.discard(key)
        elif action == "reset":
            if key not in builtin_by_key:
                raise CostRateError("cost_rate_not_built_in", "Only built-in rates can be reset")
            custom_by_key.pop(key, None)
            deleted_builtin_rates.discard(key)
        elif action == "delete":
            if requested.is_default:
                raise CostRateError("cost_rate_delete_forbidden", "Default rates cannot be deleted")
            if key not in custom_by_key and key not in builtin_by_key:
                raise CostRateError("cost_rate_not_found", "Cost rate was not found")
            custom_by_key.pop(key, None)
            if key in builtin_by_key:
                deleted_builtin_rates.add(key)
        else:
            raise CostRateError("cost_rate_action_invalid", "Unsupported cost rate action")
        new_revision = write_custom_rates(
            custom_by_key.values(),
            target,
            deleted_builtin_rates=deleted_builtin_rates,
        )
        merged = {key: rate for key, rate in builtin_by_key.items() if key not in deleted_builtin_rates}
        merged.update(custom_by_key)
        return CostRateCatalog(merged.values()), new_revision


def reset_all_custom_rates(
    *,
    expected_revision: str,
    path: pathlib.Path | None = None,
) -> tuple[CostRateCatalog, str]:
    target = pathlib.Path(path or config_path()).expanduser()
    lock_target = target.with_name("cost-rates.lock")
    with acquire_lock(lock_target):
        _custom, _deleted_builtin_rates, revision = read_custom_rate_config(target)
        if expected_revision != revision:
            raise CostRateRevisionConflict(revision)
        new_revision = write_custom_rates([], target)
        return CostRateCatalog(BUILTIN_RATES), new_revision
