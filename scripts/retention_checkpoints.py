#!/usr/bin/env python3
"""Retention raw-state checkpoint lifecycle helpers."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

try:
    from raw_segments_common import current_pointer_path, manifest_path, pending_rotation_path, segment_apply_marker_path
except ModuleNotFoundError:
    from .raw_segments_common import current_pointer_path, manifest_path, pending_rotation_path, segment_apply_marker_path


SCHEMA_VERSION = 1
ROOT_RELATIVE_PATH = pathlib.Path("tmp") / "retention-checkpoints"
METADATA_NAME = "checkpoint.json"


class RetentionCheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetentionCheckpoint(Mapping[str, object]):
    base: pathlib.Path
    operation_job_id: str
    checkpoint_dir: pathlib.Path
    state_backups: dict[pathlib.Path, pathlib.Path | None]
    current_files: frozenset[pathlib.Path]
    current_backups: dict[pathlib.Path, pathlib.Path]

    def to_payload(self) -> dict[str, object]:
        return {
            "checkpoint_dir": self.checkpoint_dir,
            "state_backups": dict(self.state_backups),
            "current_files": set(self.current_files),
            "current_backups": dict(self.current_backups),
        }

    def __getitem__(self, key: str) -> object:
        return self.to_payload()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_payload())

    def __len__(self) -> int:
        return len(self.to_payload())


def root_path(base: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(base).expanduser() / ROOT_RELATIVE_PATH


def metadata_payload(base: pathlib.Path, operation_job_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_job_id": operation_job_id,
        "output_dir": str(base.expanduser().resolve()),
        "pid": os.getpid(),
        "created_at_unix": time.time(),
    }


def write_metadata(checkpoint_dir: pathlib.Path, base: pathlib.Path, operation_job_id: str) -> pathlib.Path:
    path = checkpoint_dir / METADATA_NAME
    payload = metadata_payload(base, operation_job_id)
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def create(base: pathlib.Path | str, operation_job_id: str | None = None) -> RetentionCheckpoint:
    base_path = pathlib.Path(base).expanduser().resolve()
    job_id = operation_job_id or f"retention:{uuid.uuid4().hex}"
    if not job_id.startswith("retention:"):
        raise RetentionCheckpointError("retention checkpoint operation_job_id must start with retention:")
    state_paths = [
        manifest_path(base_path),
        current_pointer_path(base_path),
        pending_rotation_path(base_path),
        segment_apply_marker_path(base_path),
    ]
    current_dir = base_path / "raw" / "current"
    checkpoint_root = root_path(base_path)
    checkpoint_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoint_root.chmod(0o700)
    checkpoint_dir = pathlib.Path(tempfile.mkdtemp(prefix="checkpoint-", dir=checkpoint_root))
    checkpoint_dir.chmod(0o700)
    write_metadata(checkpoint_dir, base_path, job_id)
    current_files = {path.resolve() for path in current_dir.iterdir()} if current_dir.exists() else set()
    current_backups: dict[pathlib.Path, pathlib.Path] = {}
    state_backups: dict[pathlib.Path, pathlib.Path | None] = {}
    try:
        for index, path in enumerate(sorted(current_files, key=str)):
            if not path.is_file():
                continue
            backup = checkpoint_dir / f"current-{index}"
            try:
                os.link(path, backup)
            except OSError:
                shutil.copyfile(path, backup)
            current_backups[path] = backup
        for index, path in enumerate(state_paths):
            if not path.exists():
                state_backups[path] = None
                continue
            backup = checkpoint_dir / f"state-{index}"
            shutil.copyfile(path, backup)
            state_backups[path] = backup
    except Exception:
        shutil.rmtree(checkpoint_dir, ignore_errors=True)
        raise
    return RetentionCheckpoint(
        base=base_path,
        operation_job_id=job_id,
        checkpoint_dir=checkpoint_dir,
        state_backups=state_backups,
        current_files=frozenset(current_files),
        current_backups=current_backups,
    )


def discard(checkpoint: RetentionCheckpoint | Mapping[str, object]) -> None:
    checkpoint_dir = checkpoint.get("checkpoint_dir")
    if isinstance(checkpoint_dir, pathlib.Path):
        shutil.rmtree(checkpoint_dir, ignore_errors=True)


def restore(base: pathlib.Path | str, checkpoint: RetentionCheckpoint | Mapping[str, object]) -> None:
    base_path = pathlib.Path(base).expanduser().resolve()
    if isinstance(checkpoint, RetentionCheckpoint) and checkpoint.base != base_path:
        raise RetentionCheckpointError("retention checkpoint output directory mismatch")
    current_files = checkpoint.get("current_files")
    current_dir = base_path / "raw" / "current"
    if current_dir.exists() and isinstance(current_files, (set, frozenset)):
        for path in current_dir.iterdir():
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved not in current_files and path.is_file() and path.stat().st_size > 0:
                discard(checkpoint)
                return
    state_backups = checkpoint.get("state_backups")
    if isinstance(state_backups, dict):
        for path, backup in state_backups.items():
            if not isinstance(path, pathlib.Path):
                continue
            if backup is None:
                path.unlink(missing_ok=True)
                continue
            if not isinstance(backup, pathlib.Path) or not backup.is_file():
                raise RetentionCheckpointError(f"retention checkpoint state backup missing: {backup}")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.restore.tmp")
            shutil.copyfile(backup, temporary)
            temporary.replace(path)
            path.chmod(0o600)
    current_backups = checkpoint.get("current_backups")
    if isinstance(current_backups, dict):
        for path, backup in current_backups.items():
            if not isinstance(path, pathlib.Path) or not isinstance(backup, pathlib.Path):
                continue
            if path.exists():
                continue
            if not backup.is_file():
                raise RetentionCheckpointError(f"retention checkpoint current backup missing: {backup}")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(backup, path)
            except OSError:
                shutil.copyfile(backup, path)
            path.chmod(0o600)
    if current_dir.exists() and isinstance(current_files, (set, frozenset)):
        for path in current_dir.iterdir():
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved not in current_files and path.is_file() and path.stat().st_size == 0:
                path.unlink(missing_ok=True)
    discard(checkpoint)


def _read_entry(base: pathlib.Path, entry: pathlib.Path) -> dict[str, Any]:
    try:
        entry_stat = entry.lstat()
    except OSError as exc:
        raise RetentionCheckpointError(f"cannot inspect retention checkpoint: {entry}") from exc
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise RetentionCheckpointError(f"invalid retention checkpoint entry: {entry}")
    if not entry.name.startswith("checkpoint-"):
        raise RetentionCheckpointError(f"unexpected retention checkpoint entry: {entry}")
    metadata_path = entry / METADATA_NAME
    if metadata_path.is_symlink():
        raise RetentionCheckpointError(f"retention checkpoint metadata is a symlink: {metadata_path}")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "path": str(entry),
            "operation_job_id": None,
            "created_at_unix": entry_stat.st_mtime,
            "legacy_metadata": True,
        }
    except (OSError, json.JSONDecodeError) as exc:
        raise RetentionCheckpointError(f"invalid retention checkpoint metadata: {metadata_path}") from exc
    expected_base = str(base.expanduser().resolve())
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or not str(payload.get("operation_job_id") or "").startswith("retention:")
        or payload.get("output_dir") != expected_base
        or not isinstance(payload.get("pid"), int)
        or not isinstance(payload.get("created_at_unix"), (int, float))
    ):
        raise RetentionCheckpointError(f"invalid retention checkpoint metadata: {metadata_path}")
    return {
        "path": str(entry),
        "operation_job_id": str(payload["operation_job_id"]),
        "created_at_unix": float(payload["created_at_unix"]),
        "legacy_metadata": False,
    }


def inspect(base: pathlib.Path | str) -> dict[str, Any]:
    root = root_path(base)
    result: dict[str, Any] = {
        "path": str(root),
        "exists": False,
        "valid": True,
        "count": 0,
        "bytes": 0,
        "oldest_created_at_unix": None,
        "operation_job_ids": [],
        "legacy_metadata_count": 0,
    }
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return result
    except OSError as exc:
        result.update(valid=False, error=str(exc))
        return result
    result["exists"] = True
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        result.update(valid=False, error="checkpoint root is not a directory")
        return result
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(root.iterdir(), key=lambda value: value.name)
        for child in children:
            entries.append(_read_entry(pathlib.Path(base).expanduser(), child))
    except (OSError, RetentionCheckpointError) as exc:
        result.update(valid=False, error=str(exc))
        return result
    total_bytes = 0
    for entry in entries:
        entry_path = pathlib.Path(str(entry["path"]))
        for child in entry_path.rglob("*"):
            try:
                child_stat = child.lstat()
            except OSError as exc:
                result.update(valid=False, error=str(exc))
                return result
            if stat.S_ISLNK(child_stat.st_mode):
                result.update(valid=False, error=f"checkpoint contains symlink: {child}")
                return result
            if stat.S_ISREG(child_stat.st_mode):
                total_bytes += child_stat.st_size
    created = [float(entry["created_at_unix"]) for entry in entries]
    result.update(
        count=len(entries),
        bytes=total_bytes,
        oldest_created_at_unix=min(created) if created else None,
        operation_job_ids=sorted({str(entry["operation_job_id"]) for entry in entries if entry["operation_job_id"]}),
        legacy_metadata_count=sum(bool(entry["legacy_metadata"]) for entry in entries),
    )
    return result


def sweep(base: pathlib.Path | str, *, active_operation_job_id: str | None = None) -> dict[str, int]:
    root = root_path(base)
    summary = inspect(base)
    if not summary.get("valid"):
        raise RetentionCheckpointError(str(summary.get("error") or "invalid retention checkpoint state"))
    if not summary.get("exists"):
        return {"removed": 0, "kept": 0}
    removed = 0
    kept = 0
    for child in sorted(root.iterdir(), key=lambda value: value.name):
        entry = _read_entry(pathlib.Path(base).expanduser(), child)
        if active_operation_job_id and entry.get("operation_job_id") == active_operation_job_id:
            kept += 1
            continue
        try:
            shutil.rmtree(child)
        except OSError as exc:
            raise RetentionCheckpointError(f"cannot remove retention checkpoint: {child}") from exc
        removed += 1
    try:
        root.rmdir()
    except OSError:
        pass
    return {"removed": removed, "kept": kept}
