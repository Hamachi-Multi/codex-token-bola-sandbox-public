#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import private_export_guard


def read_path_file(path: pathlib.Path | None) -> list[str]:
    if path is None:
        return []
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        raise private_export_guard.InputError(f"cannot read path list: {path}: {exc}") from exc


def include_scope_matches(manifest: dict[str, Any], relative_path: str) -> bool:
    return any(private_export_guard.matches(pattern, relative_path) for pattern in manifest["include"])


def normalize_candidate_path(relative_path: str, label: str) -> str:
    if not isinstance(relative_path, str) or not relative_path:
        raise private_export_guard.InputError(f"{label} path must be a non-empty relative path")
    if "\\" in relative_path:
        raise private_export_guard.InputError(f"{label} path must use forward slashes: {relative_path}")

    candidate = pathlib.PurePosixPath(relative_path)
    if candidate.is_absolute():
        raise private_export_guard.InputError(f"{label} path must be a relative path: {relative_path}")
    if ".." in candidate.parts:
        raise private_export_guard.InputError(f"{label} path escapes public worktree: {relative_path}")

    normalized = candidate.as_posix()
    if normalized in ("", ".") or normalized != relative_path:
        raise private_export_guard.InputError(f"{label} path must be a normalized relative path: {relative_path}")
    return normalized


def denied_path_errors(manifest: dict[str, Any], relative_path: str, label: str) -> list[str]:
    errors: list[str] = []
    for rule_key in ("never_public_path_globs", "public_only_ops_paths"):
        for pattern in manifest[rule_key]:
            if private_export_guard.matches(pattern, relative_path):
                errors.append(f"blocked {label} path {relative_path} matches {rule_key}:{pattern}")
    return errors


def changed_path_errors(manifest: dict[str, Any], relative_path: str, label: str) -> list[str]:
    errors = denied_path_errors(manifest, relative_path, label)
    if not include_scope_matches(manifest, relative_path):
        errors.append(f"{label} path {relative_path} is outside manifest include/delete scope")
    return errors


def validate_candidate_surface(
    manifest_path: pathlib.Path | str,
    public_worktree: pathlib.Path | str,
    *,
    changed_paths: list[str] | None = None,
    staged_paths: list[str] | None = None,
) -> dict[str, Any]:
    manifest_path = pathlib.Path(manifest_path)
    public_worktree = pathlib.Path(public_worktree)
    manifest = private_export_guard.load_manifest(manifest_path)
    errors: list[str] = []

    if not public_worktree.is_dir():
        raise private_export_guard.InputError(f"public worktree is not a directory: {public_worktree}")

    regexes = private_export_guard.compile_forbidden_regexes(manifest)
    for relative in private_export_guard.relative_files(public_worktree):
        for pattern in manifest["never_public_path_globs"]:
            if private_export_guard.matches(pattern, relative):
                errors.append(f"blocked public worktree path {relative} matches never_public_path_globs:{pattern}")

    for label, paths in (("changed", changed_paths or []), ("staged", staged_paths or [])):
        for relative in paths:
            relative = normalize_candidate_path(relative, label)
            errors.extend(changed_path_errors(manifest, relative, label))
            if (public_worktree / relative).is_file():
                errors.extend(private_export_guard.content_rule_errors(public_worktree, relative, regexes))

    return {"ok": not errors, "errors": errors}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a public release candidate worktree surface.")
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--public-worktree", required=True, type=pathlib.Path)
    parser.add_argument("--changed-paths-file", type=pathlib.Path)
    parser.add_argument("--staged-paths-file", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_candidate_surface(
            args.manifest,
            args.public_worktree,
            changed_paths=read_path_file(args.changed_paths_file),
            staged_paths=read_path_file(args.staged_paths_file),
        )
    except private_export_guard.InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
