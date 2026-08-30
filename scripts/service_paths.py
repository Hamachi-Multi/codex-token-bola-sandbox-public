"""Shared configuration and filesystem paths for Codex Token Bola."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Mapping

try:
    from platformdirs import user_config_path, user_data_path
except ModuleNotFoundError:  # Source checkouts may run before project dependencies are installed.
    def user_config_path(appname: str | None = None, appauthor: bool = False) -> pathlib.Path:
        base = pathlib.Path.home() / "Library" / "Application Support" if sys.platform == "darwin" else pathlib.Path.home() / ".config"
        return base / appname if appname else base

    def user_data_path(appname: str | None = None, appauthor: bool = False) -> pathlib.Path:
        base = pathlib.Path.home() / "Library" / "Application Support" if sys.platform == "darwin" else pathlib.Path.home() / ".local" / "share"
        return base / appname if appname else base


SERVICE_DIR_NAME = "bola"
LEGACY_CONFIG_DIR_NAME = "codex-token-bola"
OUTPUT_DIR_ENV = "BOLA_OUTPUT_DIR"
LEGACY_ENV_PREFIX = "CODEX_TOKEN_USAGE_"
CONFIG_SCHEMA_VERSION = 1
PATH_TRANSITION_SCHEMA_VERSION = 1
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
MANAGED_DIRECTORY_NAMES = ("analytics", "bad", "normalized", "raw", "reports", "state", "tmp")
MANAGED_ROOT_FILE_NAMES = ("hook-probe-events.jsonl", "prompt-usage-errors.jsonl")


class ConfigurationError(RuntimeError):
    """Raised when persistent runtime configuration is invalid."""


class LegacyNameUnsupported(ConfigurationError):
    def __init__(self, *, kind: str, names: list[str], mappings: dict[str, str], path: pathlib.Path | None = None):
        self.kind = kind
        self.names = names
        self.mappings = mappings
        self.path = path
        if kind == "environment":
            message = f"legacy environment variable names are unsupported: {', '.join(names)}"
        else:
            message = f"legacy config is unsupported: {path}"
        super().__init__(message)

    def payload(self) -> dict[str, object]:
        return {
            "error": "legacy_name_unsupported" if self.kind == "environment" else "legacy_config_unsupported",
            "kind": self.kind,
            "names": self.names,
            "mappings": self.mappings,
            "path": str(self.path) if self.path is not None else None,
            "message": str(self),
        }


class PathLockBusy(RuntimeError):
    """Raised when another hook or path operation owns the path lock."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        super().__init__(f"Codex Token Bola path lock is already held: {path}")


class PathTransitionPhase(str, Enum):
    PREPARING = "preparing"
    PENDING = "pending"
    APPLYING = "applying"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class PathTransition(Mapping[str, object]):
    transition_id: str
    source_output_dir: pathlib.Path
    active_output_dir: pathlib.Path
    created_at_ns: int
    phase: PathTransitionPhase
    rollback: bool = False
    transferred_state_files: tuple[str, ...] = ()
    created_state_files: tuple[str, ...] = ()
    previous_transition: PathTransition | None = None

    def __getitem__(self, key: str) -> object:
        return self.to_payload()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_payload())

    def __len__(self) -> int:
        return len(self.to_payload())

    def __post_init__(self) -> None:
        if not isinstance(self.transition_id, str) or not self.transition_id:
            raise ConfigurationError("path transition id must not be empty")
        if not isinstance(self.phase, PathTransitionPhase):
            raise ConfigurationError(f"unsupported path transition phase: {self.phase}")
        source = _expanded_absolute(self.source_output_dir)
        active = _expanded_absolute(self.active_output_dir)
        if source == active:
            raise ConfigurationError("path transition source and active output directories must differ")
        if isinstance(self.created_at_ns, bool) or self.created_at_ns <= 0:
            raise ConfigurationError("path transition created_at_ns must be a positive integer")
        for field, names in (
            ("transferred_state_files", self.transferred_state_files),
            ("created_state_files", self.created_state_files),
        ):
            if any(not name or pathlib.Path(name).name != name for name in names):
                raise ConfigurationError(f"path transition {field} must contain file names")
        if self.previous_transition is not None and self.previous_transition.previous_transition is not None:
            raise ConfigurationError("path transition previous_transition may be nested only once")
        if self.previous_transition is None and self.rollback:
            raise ConfigurationError("path transition rollback requires previous_transition")
        if self.previous_transition is not None:
            if not self.rollback:
                raise ConfigurationError("path transition previous_transition requires rollback")
            if source != self.previous_transition.active_output_dir or active != self.previous_transition.source_output_dir:
                raise ConfigurationError("path transition rollback paths do not reverse previous_transition")
        object.__setattr__(self, "source_output_dir", source)
        object.__setattr__(self, "active_output_dir", active)

    @classmethod
    def prepare_set(
        cls,
        source: pathlib.Path,
        active: pathlib.Path,
        *,
        transition_id: str,
        created_at_ns: int,
        rollback: bool = False,
        transferred_state_files: tuple[str, ...] = (),
        created_state_files: tuple[str, ...] = (),
        previous_transition: PathTransition | None = None,
    ) -> PathTransition:
        if previous_transition is not None and previous_transition.phase is not PathTransitionPhase.PENDING:
            raise ConfigurationError("only a pending path transition may be preserved for rollback")
        return cls(
            transition_id=transition_id,
            source_output_dir=source,
            active_output_dir=active,
            created_at_ns=created_at_ns,
            phase=PathTransitionPhase.PREPARING,
            rollback=rollback,
            transferred_state_files=transferred_state_files,
            created_state_files=created_state_files,
            previous_transition=previous_transition,
        )

    def mark_pending(self) -> PathTransition:
        return self._transition(PathTransitionPhase.PENDING, {PathTransitionPhase.PREPARING})

    def begin_migration(self) -> PathTransition:
        return self._transition(
            PathTransitionPhase.APPLYING,
            {PathTransitionPhase.PENDING, PathTransitionPhase.RECOVERY_REQUIRED},
        )

    def mark_recovery_required(self) -> PathTransition:
        return self._transition(PathTransitionPhase.RECOVERY_REQUIRED, {PathTransitionPhase.APPLYING})

    def _transition(self, phase: PathTransitionPhase, allowed: set[PathTransitionPhase]) -> PathTransition:
        if self.phase not in allowed:
            raise ConfigurationError(f"path transition cannot move from {self.phase.value} to {phase.value}")
        return PathTransition(
            transition_id=self.transition_id,
            source_output_dir=self.source_output_dir,
            active_output_dir=self.active_output_dir,
            created_at_ns=self.created_at_ns,
            phase=phase,
            rollback=self.rollback,
            transferred_state_files=self.transferred_state_files,
            created_state_files=self.created_state_files,
            previous_transition=self.previous_transition,
        )

    def to_payload(self, *, include_schema: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "transition_id": self.transition_id,
            "source_output_dir": str(self.source_output_dir),
            "active_output_dir": str(self.active_output_dir),
            "created_at_ns": self.created_at_ns,
            "phase": self.phase.value,
        }
        if self.rollback:
            payload["rollback"] = True
        if self.transferred_state_files:
            payload["transferred_state_files"] = list(self.transferred_state_files)
        if self.created_state_files:
            payload["created_state_files"] = list(self.created_state_files)
        if self.previous_transition is not None:
            payload["previous_transition"] = self.previous_transition.to_payload(include_schema=True)
        if include_schema:
            payload["schema_version"] = PATH_TRANSITION_SCHEMA_VERSION
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object], *, allow_previous: bool = True) -> PathTransition:
        if payload.get("schema_version", PATH_TRANSITION_SCHEMA_VERSION) != PATH_TRANSITION_SCHEMA_VERSION:
            raise ConfigurationError("unsupported path transition schema")
        required = {"transition_id", "source_output_dir", "active_output_dir", "created_at_ns", "phase"}
        missing = sorted(required - set(payload))
        if missing:
            raise ConfigurationError(f"path transition is missing fields: {', '.join(missing)}")
        try:
            phase = PathTransitionPhase(str(payload["phase"]))
        except ValueError as exc:
            raise ConfigurationError(f"unsupported path transition phase: {payload.get('phase')}") from exc
        created_at_ns = payload["created_at_ns"]
        if isinstance(created_at_ns, bool) or not isinstance(created_at_ns, int):
            raise ConfigurationError("path transition created_at_ns must be a positive integer")

        def file_names(field: str) -> tuple[str, ...]:
            value = payload.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ConfigurationError(f"path transition {field} must be a list of file names")
            return tuple(value)

        previous_payload = payload.get("previous_transition")
        previous: PathTransition | None = None
        if previous_payload is not None:
            if not allow_previous or not isinstance(previous_payload, Mapping):
                raise ConfigurationError("invalid path transition previous_transition")
            previous = cls.from_payload(previous_payload, allow_previous=False)
            if previous.phase is not PathTransitionPhase.PENDING:
                raise ConfigurationError("path transition previous_transition must be pending")
        rollback = payload.get("rollback", False)
        if not isinstance(rollback, bool):
            raise ConfigurationError("path transition rollback must be a boolean")
        return cls(
            transition_id=payload["transition_id"],
            source_output_dir=pathlib.Path(str(payload["source_output_dir"])),
            active_output_dir=pathlib.Path(str(payload["active_output_dir"])),
            created_at_ns=created_at_ns,
            phase=phase,
            rollback=rollback,
            transferred_state_files=file_names("transferred_state_files"),
            created_state_files=file_names("created_state_files"),
            previous_transition=previous,
        )


@dataclass(frozen=True)
class RuntimePaths:
    project_root: pathlib.Path
    runtime_config_path: pathlib.Path
    codex_dir: pathlib.Path
    output_dir: pathlib.Path

    def as_dict(self) -> dict[str, str]:
        return {
            "project_root": str(self.project_root),
            "runtime_config_path": str(self.runtime_config_path),
            "codex_dir": str(self.codex_dir),
            "output_dir": str(self.output_dir),
        }


@dataclass(frozen=True)
class OutputLayout:
    """Canonical paths for files owned by one BOLA output directory."""

    root: pathlib.Path

    @property
    def analytics_dir(self) -> pathlib.Path:
        return self.root / "analytics"

    @property
    def analytics_db(self) -> pathlib.Path:
        return self.analytics_dir / "bola.sqlite"

    @property
    def bad_dir(self) -> pathlib.Path:
        return self.root / "bad"

    @property
    def normalized_dir(self) -> pathlib.Path:
        return self.root / "normalized"

    @property
    def normalized_log(self) -> pathlib.Path:
        return self.normalized_dir / "prompt-usage.normalized.jsonl"

    @property
    def normalize_state(self) -> pathlib.Path:
        return self.normalized_dir / "normalize-state.json"

    @property
    def raw_dir(self) -> pathlib.Path:
        return self.root / "raw"

    @property
    def reports_dir(self) -> pathlib.Path:
        return self.root / "reports"

    @property
    def state_dir(self) -> pathlib.Path:
        return self.root / "state"

    @property
    def tmp_dir(self) -> pathlib.Path:
        return self.root / "tmp"

    @property
    def error_log(self) -> pathlib.Path:
        return self.root / "prompt-usage-errors.jsonl"


def _expanded_absolute(value: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(value).expanduser().resolve(strict=False)


def runtime_config_path(env: Mapping[str, str] | None = None) -> pathlib.Path:
    source = os.environ if env is None else env
    config_home = source.get("XDG_CONFIG_HOME")
    base = _expanded_absolute(config_home) if config_home else pathlib.Path(user_config_path(appname=None, appauthor=False))
    return base / SERVICE_DIR_NAME / "runtime.conf"


def legacy_config_path(env: Mapping[str, str] | None = None) -> pathlib.Path:
    return runtime_config_path(env).parent.parent / LEGACY_CONFIG_DIR_NAME / "config.json"


def default_output_dir(env: Mapping[str, str] | None = None) -> pathlib.Path:
    source = os.environ if env is None else env
    data_home = source.get("XDG_DATA_HOME")
    if data_home:
        return _expanded_absolute(data_home) / SERVICE_DIR_NAME
    return _expanded_absolute(user_data_path(appname=SERVICE_DIR_NAME, appauthor=False))


def legacy_environment_mappings(env: Mapping[str, str]) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for name in sorted(env):
        if not name.startswith(LEGACY_ENV_PREFIX):
            continue
        suffix = name[len(LEGACY_ENV_PREFIX) :]
        mappings[name] = OUTPUT_DIR_ENV if suffix == "DATA_ROOT" else f"BOLA_{suffix}"
    return mappings


def reject_legacy_names(env: Mapping[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    mappings = legacy_environment_mappings(source)
    if mappings:
        raise LegacyNameUnsupported(kind="environment", names=list(mappings), mappings=mappings)
    current = runtime_config_path(source)
    legacy = legacy_config_path(source)
    if not current.exists() and legacy.is_file():
        raise LegacyNameUnsupported(
            kind="config",
            names=[str(legacy)],
            mappings={str(legacy): str(current)},
            path=legacy,
        )


def path_lock_path(env: Mapping[str, str] | None = None) -> pathlib.Path:
    return runtime_config_path(env).with_name("paths.lock")


def path_transition_path(env: Mapping[str, str] | None = None) -> pathlib.Path:
    return runtime_config_path(env).with_name("path-transition.json")


@contextlib.contextmanager
def acquire_path_lock(*, blocking: bool = True, env: Mapping[str, str] | None = None) -> Iterator[pathlib.Path]:
    path = path_lock_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise PathLockBusy(path) from exc
        yield path
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def load_path_transition(path: str | pathlib.Path | None = None) -> PathTransition | None:
    target = _expanded_absolute(path) if path is not None else path_transition_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"invalid path transition at {target}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != PATH_TRANSITION_SCHEMA_VERSION:
        raise ConfigurationError(f"unsupported path transition schema at {target}")
    try:
        return PathTransition.from_payload(payload)
    except ConfigurationError as exc:
        raise ConfigurationError(f"invalid path transition at {target}: {exc}") from exc


def read_path_transition(path: str | pathlib.Path | None = None) -> dict[str, object] | None:
    transition = load_path_transition(path)
    return transition.to_payload() if transition is not None else None


def write_path_transition(payload: PathTransition | Mapping[str, object], path: str | pathlib.Path | None = None) -> pathlib.Path:
    target = _expanded_absolute(path) if path is not None else path_transition_path()
    transition = payload if isinstance(payload, PathTransition) else PathTransition.from_payload(payload)
    value = transition.to_payload()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def clear_path_transition(path: str | pathlib.Path | None = None) -> None:
    target = _expanded_absolute(path) if path is not None else path_transition_path()
    target.unlink(missing_ok=True)


def _parse_runtime_config(text: str, target: pathlib.Path) -> dict[str, object]:
    values: dict[str, str] = {}
    allowed = {"schema_version", "codex_dir", "output_dir"}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"invalid BOLA runtime config line {line_number} at {target}: expected key=value")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or key not in allowed:
            label = key or "<empty>"
            raise ConfigurationError(f"unsupported BOLA runtime config key at {target}:{line_number}: {label}")
        if key in values:
            raise ConfigurationError(f"duplicate BOLA runtime config key at {target}:{line_number}: {key}")
        if not value:
            raise ConfigurationError(f"BOLA runtime config value must not be empty at {target}:{line_number}: {key}")
        values[key] = value

    missing = sorted(allowed - set(values))
    if missing:
        raise ConfigurationError(f"missing BOLA runtime config keys at {target}: {', '.join(missing)}")
    try:
        schema_version = int(values["schema_version"])
    except ValueError as exc:
        raise ConfigurationError(
            f"invalid BOLA runtime config schema at {target}: {values['schema_version']!r}"
        ) from exc
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigurationError(
            f"unsupported BOLA runtime config schema at {target}: "
            f"expected {CONFIG_SCHEMA_VERSION}, got {schema_version}"
        )

    payload: dict[str, object] = {"schema_version": schema_version}
    for key in ("codex_dir", "output_dir"):
        value = pathlib.Path(values[key]).expanduser()
        if not value.is_absolute():
            raise ConfigurationError(f"BOLA runtime config path must be absolute at {target}: {key}={values[key]}")
        payload[key] = str(value.resolve(strict=False))
    return payload


def read_config(path: str | pathlib.Path | None = None) -> dict[str, object]:
    target = _expanded_absolute(path) if path is not None else runtime_config_path()
    if path is None:
        reject_legacy_names()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"invalid BOLA runtime config at {target}: {exc}") from exc
    return _parse_runtime_config(text, target)


def require_runtime_config(path: str | pathlib.Path | None = None) -> dict[str, object]:
    target = _expanded_absolute(path) if path is not None else runtime_config_path()
    configured = read_config(target)
    if not configured:
        raise ConfigurationError(
            f"BOLA runtime config is missing at {target}; run bola install-hook first"
        )
    return configured


def write_config(config: Mapping[str, object], path: str | pathlib.Path | None = None) -> pathlib.Path:
    target = _expanded_absolute(path) if path is not None else runtime_config_path()
    payload: dict[str, str] = {"schema_version": str(CONFIG_SCHEMA_VERSION)}
    defaults: dict[str, object] = {
        "codex_dir": _expanded_absolute("~/.codex"),
        "output_dir": default_output_dir(),
    }
    for key, default in defaults.items():
        value = config.get(key, default)
        if value is None or not str(value).strip():
            raise ConfigurationError(f"BOLA runtime config requires {key}: {target}")
        payload[key] = str(_expanded_absolute(str(value)))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(f"{key}={value}" for key, value in payload.items()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def update_config(
    *,
    updates: Mapping[str, str | pathlib.Path] | None = None,
    unset: tuple[str, ...] = (),
    path: str | pathlib.Path | None = None,
) -> dict[str, object]:
    target = _expanded_absolute(path) if path is not None else runtime_config_path()
    current = read_config(target)
    if not current:
        current = {
            "codex_dir": _expanded_absolute("~/.codex"),
            "output_dir": default_output_dir(),
        }
    for key in unset:
        if key not in {"codex_dir", "output_dir"}:
            raise ConfigurationError(f"unsupported Codex Token Bola config field: {key}")
        current.pop(key, None)
    for key, value in (updates or {}).items():
        if key not in {"codex_dir", "output_dir"}:
            raise ConfigurationError(f"unsupported Codex Token Bola config field: {key}")
        current[key] = str(_expanded_absolute(value))
    write_config(current, target)
    return read_config(target)


def resolve_runtime_paths(
    *,
    codex_dir: str | pathlib.Path | None = None,
    output_dir: str | pathlib.Path | None = None,
    env: Mapping[str, str] | None = None,
    project_root: str | pathlib.Path | None = None,
    config: Mapping[str, object] | None = None,
) -> RuntimePaths:
    source = os.environ if env is None else env
    reject_legacy_names(source)
    persistent = dict(config) if config is not None else read_config(runtime_config_path(source))
    resolved_project = _expanded_absolute(project_root or PROJECT_ROOT)
    codex_value = codex_dir if codex_dir is not None else source.get("CODEX_HOME") or persistent.get("codex_dir") or "~/.codex"
    data_value = output_dir if output_dir is not None else source.get(OUTPUT_DIR_ENV) or persistent.get("output_dir") or default_output_dir(source)
    return RuntimePaths(
        project_root=resolved_project,
        runtime_config_path=runtime_config_path(source),
        codex_dir=_expanded_absolute(str(codex_value)),
        output_dir=_expanded_absolute(str(data_value)),
    )


def codex_dir_path(codex_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    return resolve_runtime_paths(codex_dir=codex_dir).codex_dir


def output_dir_path(output_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    return resolve_runtime_paths(output_dir=output_dir).output_dir


def output_layout(output_dir: str | pathlib.Path | None = None) -> OutputLayout:
    """Resolve the canonical output layout without creating directories."""

    return OutputLayout(root=output_dir_path(output_dir))


def ensure_output_tmp_dir(output_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    """Create the service-owned temporary directory for a writer."""

    target = output_layout(output_dir).tmp_dir
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink() or not target.is_dir():
        raise ConfigurationError(f"BOLA temporary path must be a directory: {target}")
    target.chmod(0o700)
    return target


def service_root(
    _codex_dir: str | pathlib.Path | None = None,
    *,
    output_dir: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """Return the app-managed output directory."""

    return output_dir_path(output_dir)


def managed_paths(root: str | pathlib.Path) -> list[pathlib.Path]:
    base = _expanded_absolute(root)
    paths = [base / name for name in MANAGED_DIRECTORY_NAMES]
    paths.extend(base / name for name in MANAGED_ROOT_FILE_NAMES)
    paths.extend(sorted(base.glob("prompt-usage*.jsonl")))
    unique: dict[pathlib.Path, None] = {}
    for path in paths:
        unique[path] = None
    return list(unique)


def has_managed_data(root: str | pathlib.Path) -> bool:
    return any(path.exists() or path.is_symlink() for path in managed_paths(root))
