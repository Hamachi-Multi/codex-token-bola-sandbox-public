#!/usr/bin/env python3
"""Shared process lock for token usage analysis and raw compaction."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import time
from typing import Iterator

import service_paths


LOCK_HELD_ENV = "CODEX_TOKEN_USAGE_LOCK_HELD"
LOCK_PATH_ENV = "CODEX_TOKEN_USAGE_LOCK_PATH"
LOCK_FD_ENV = "CODEX_TOKEN_USAGE_LOCK_FD"
LOCK_ENV_KEYS = (LOCK_HELD_ENV, LOCK_PATH_ENV, LOCK_FD_ENV)


class ServiceLockBusy(RuntimeError):
    def __init__(self, path: pathlib.Path):
        super().__init__(f"token usage service lock is already held: {path}")
        self.path = path


class ServiceLock:
    def __init__(self, path: pathlib.Path, fd: int | None):
        self.path = path
        self.fd = fd


def default_lock_path(codex_home: str | pathlib.Path | None = None) -> pathlib.Path:
    return service_paths.service_root(codex_home) / "state" / "service.lock"


def scrub_lock_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base or {})
    for key in LOCK_ENV_KEYS:
        env.pop(key, None)
    return env


def child_lock_env(
    base: dict[str, str] | None = None,
    lock_path: pathlib.Path | str | None = None,
    lock_fd: int | None = None,
) -> dict[str, str]:
    env = scrub_lock_env(base)
    env[LOCK_HELD_ENV] = "1"
    if lock_path:
        env[LOCK_PATH_ENV] = str(pathlib.Path(lock_path).expanduser())
    if lock_fd is not None:
        env[LOCK_FD_ENV] = str(lock_fd)
    return env


def lock_pass_fds(env: dict[str, str] | None = None) -> tuple[int, ...]:
    source = env if env is not None else os.environ
    try:
        fd = int(source.get(LOCK_FD_ENV, ""))
    except (TypeError, ValueError):
        return ()
    if fd < 0:
        return ()
    try:
        os.fstat(fd)
    except OSError:
        return ()
    return (fd,)


def valid_inherited_lock(lock_path: pathlib.Path | str | None = None, codex_home: str | pathlib.Path | None = None) -> tuple[pathlib.Path, int] | None:
    if os.environ.get(LOCK_HELD_ENV) != "1":
        return None
    try:
        fd = int(os.environ.get(LOCK_FD_ENV, ""))
    except (TypeError, ValueError):
        return None
    if fd < 0:
        return None
    inherited_path = pathlib.Path(os.environ.get(LOCK_PATH_ENV) or lock_path or default_lock_path(codex_home)).expanduser()
    expected_path = pathlib.Path(lock_path).expanduser() if lock_path else default_lock_path(codex_home)
    try:
        fd_stat = os.fstat(fd)
        path_stat = inherited_path.stat()
        expected_stat = expected_path.stat()
    except OSError:
        return None
    if fd_stat.st_ino != path_stat.st_ino or fd_stat.st_dev != path_stat.st_dev:
        return None
    if fd_stat.st_ino != expected_stat.st_ino or fd_stat.st_dev != expected_stat.st_dev:
        return None
    return inherited_path, fd


@contextlib.contextmanager
def acquire_service_lock(
    lock_path: pathlib.Path | str | None = None,
    reason: str = "token-usage",
    codex_home: str | pathlib.Path | None = None,
) -> Iterator[ServiceLock]:
    inherited = valid_inherited_lock(lock_path=lock_path, codex_home=codex_home)
    if inherited is not None:
        inherited_path, fd = inherited
        yield ServiceLock(inherited_path, fd)
        return

    path = pathlib.Path(lock_path).expanduser() if lock_path else default_lock_path(codex_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ServiceLockBusy(path) from exc
        locked = True
        os.set_inheritable(fd, True)
        payload = {"pid": os.getpid(), "reason": reason, "acquired_at_unix": time.time()}
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        yield ServiceLock(path, fd)
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
