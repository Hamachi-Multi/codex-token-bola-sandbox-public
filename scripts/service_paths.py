"""Shared filesystem paths for Codex Token Bola service data."""

from __future__ import annotations

import os
import pathlib


SERVICE_DIR_NAME = "codex-token-bola"
LEGACY_DIR_NAME = "token-usage"


class PathMigrationRequired(RuntimeError):
    def __init__(self, codex_home: pathlib.Path):
        self.codex_home = codex_home
        self.legacy = legacy_root(codex_home)
        self.destination = service_root(codex_home)
        super().__init__(migration_required_message(codex_home))


class PathMigrationConflict(RuntimeError):
    def __init__(self, codex_home: pathlib.Path):
        self.codex_home = codex_home
        self.legacy = legacy_root(codex_home)
        self.destination = service_root(codex_home)
        super().__init__(
            "both legacy and Codex Token Bola service directories exist; "
            f"move or remove one path before migration: {self.legacy} -> {self.destination}"
        )


class PathMigrationPlan:
    def __init__(
        self,
        codex_home: pathlib.Path,
        legacy: pathlib.Path,
        destination: pathlib.Path,
        *,
        legacy_exists: bool,
        destination_exists: bool,
        action: str,
        message: str,
    ) -> None:
        self.codex_home = codex_home
        self.legacy = legacy
        self.destination = destination
        self.legacy_exists = legacy_exists
        self.destination_exists = destination_exists
        self.action = action
        self.message = message

    def as_dict(self) -> dict[str, object]:
        return {
            "codex_home": str(self.codex_home),
            "legacy": str(self.legacy),
            "destination": str(self.destination),
            "legacy_exists": self.legacy_exists,
            "destination_exists": self.destination_exists,
            "action": self.action,
            "message": self.message,
        }


def codex_home_path(codex_home: str | pathlib.Path | None = None) -> pathlib.Path:
    raw = codex_home if codex_home is not None else os.environ.get("CODEX_HOME", "~/.codex")
    return pathlib.Path(raw).expanduser()


def service_root(codex_home: str | pathlib.Path | None = None) -> pathlib.Path:
    return codex_home_path(codex_home) / SERVICE_DIR_NAME


def legacy_root(codex_home: str | pathlib.Path | None = None) -> pathlib.Path:
    return codex_home_path(codex_home) / LEGACY_DIR_NAME


def migration_required_message(codex_home: str | pathlib.Path | None = None) -> str:
    home = codex_home_path(codex_home)
    return (
        f"Codex Token Bola data must be migrated from {legacy_root(home)} to {service_root(home)}. "
        "Run `python3 scripts/codex_token_usage.py migrate-path --apply` before using this command."
    )


def assert_migrated(codex_home: str | pathlib.Path | None = None) -> None:
    home = codex_home_path(codex_home)
    if legacy_root(home).exists() and not service_root(home).exists():
        raise PathMigrationRequired(home)


def migration_plan(codex_home: str | pathlib.Path | None = None) -> PathMigrationPlan:
    home = codex_home_path(codex_home)
    legacy = legacy_root(home)
    destination = service_root(home)
    legacy_exists = legacy.exists()
    destination_exists = destination.exists()
    if legacy_exists and destination_exists:
        return PathMigrationPlan(
            home,
            legacy,
            destination,
            legacy_exists=True,
            destination_exists=True,
            action="conflict",
            message="both legacy and destination directories exist; automatic merge is not supported",
        )
    if legacy_exists:
        return PathMigrationPlan(
            home,
            legacy,
            destination,
            legacy_exists=True,
            destination_exists=False,
            action="move",
            message="legacy service directory will be moved to Codex Token Bola path",
        )
    return PathMigrationPlan(
        home,
        legacy,
        destination,
        legacy_exists=False,
        destination_exists=destination_exists,
        action="noop",
        message="no legacy service directory found",
    )
