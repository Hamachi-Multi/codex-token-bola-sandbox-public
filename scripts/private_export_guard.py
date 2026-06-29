#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import pathlib
import re
import sys
from typing import Any


PATH_RULE_KEYS = ("exclude", "never_public_path_globs", "public_only_ops_paths")


class InputError(Exception):
    pass


def load_manifest(manifest_path: pathlib.Path) -> dict[str, Any]:
    try:
        with manifest_path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read manifest: {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid manifest json: {manifest_path}: {exc}") from exc

    required_keys = {"include", "exclude", "required", "never_public_path_globs", "public_only_ops_paths", "forbidden_regexes"}
    missing = sorted(required_keys - set(manifest))
    if missing:
        raise InputError(f"manifest missing required keys: {', '.join(missing)}")

    for key in ("include", *PATH_RULE_KEYS, "required", "forbidden_regexes"):
        if not isinstance(manifest[key], list):
            raise InputError(f"manifest key must be a list: {key}")

    return manifest


def relative_files(root: pathlib.Path) -> list[str]:
    files: list[str] = []
    for path in root.rglob("*"):
        if path.is_file():
            files.append(path.relative_to(root).as_posix())
    return sorted(files)


def matches(pattern: str, relative_path: str) -> bool:
    return fnmatch.fnmatchcase(relative_path, pattern)


def path_rule_errors(manifest: dict[str, Any], relative_path: str) -> list[str]:
    errors: list[str] = []
    for rule_key in PATH_RULE_KEYS:
        for pattern in manifest[rule_key]:
            if matches(pattern, relative_path):
                errors.append(f"blocked export path {relative_path} matches {rule_key}:{pattern}")
    return errors


def compile_forbidden_regexes(manifest: dict[str, Any]) -> list[tuple[str, re.Pattern[str]]]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for entry in manifest["forbidden_regexes"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("pattern"), str):
            raise InputError("forbidden_regexes entries must include string id and pattern")
        try:
            compiled.append((entry["id"], re.compile(entry["pattern"])))
        except re.error as exc:
            raise InputError(f"invalid forbidden regex {entry['id']}: {exc}") from exc
    return compiled


def content_rule_errors(export_dir: pathlib.Path, relative_path: str, regexes: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    path = export_dir / relative_path
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [f"cannot read export file {relative_path}: {exc}"]

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []

    errors: list[str] = []
    for regex_id, pattern in regexes:
        if pattern.search(text):
            errors.append(f"forbidden content in {relative_path} matches {regex_id}")
    return errors


def has_exported_child_file(exported_files: list[str], relative_dir: str) -> bool:
    prefix = relative_dir.rstrip("/") + "/"
    return any(relative.startswith(prefix) for relative in exported_files)


def validate_export(repo_root: pathlib.Path | str, manifest_path: pathlib.Path | str, export_dir: pathlib.Path | str) -> dict[str, Any]:
    repo_root = pathlib.Path(repo_root)
    manifest_path = pathlib.Path(manifest_path)
    export_dir = pathlib.Path(export_dir)
    manifest = load_manifest(manifest_path)
    errors: list[str] = []

    if not repo_root.is_dir():
        raise InputError(f"repo root is not a directory: {repo_root}")
    if not export_dir.is_dir():
        raise InputError(f"export dir is not a directory: {export_dir}")

    exported_files = relative_files(export_dir)
    for relative in manifest["required"]:
        source = repo_root / relative
        if not source.exists():
            errors.append(f"missing required repo path: {relative}")
            continue
        if source.is_dir():
            if not has_exported_child_file(exported_files, relative):
                errors.append(f"required export directory has no exported files: {relative}")
        elif not (export_dir / relative).is_file():
            errors.append(f"missing required export path: {relative}")

    regexes = compile_forbidden_regexes(manifest)
    for relative in exported_files:
        errors.extend(path_rule_errors(manifest, relative))
        errors.extend(content_rule_errors(export_dir, relative, regexes))

    return {"ok": not errors, "errors": errors}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a private release export staging directory.")
    parser.add_argument("--repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--export-dir", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_export(args.repo_root, args.manifest, args.export_dir)
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
