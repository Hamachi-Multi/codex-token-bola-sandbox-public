#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import private_export_guard


def included_product_path(manifest: dict[str, Any], relative_path: str) -> bool:
    if not any(private_export_guard.matches(pattern, relative_path) for pattern in manifest["include"]):
        return False
    return not private_export_guard.path_rule_errors(manifest, relative_path)


def clear_directory(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def stage_export(repo_root: pathlib.Path | str, manifest_path: pathlib.Path | str, export_dir: pathlib.Path | str) -> dict[str, Any]:
    repo_root = pathlib.Path(repo_root)
    manifest_path = pathlib.Path(manifest_path)
    export_dir = pathlib.Path(export_dir)
    manifest = private_export_guard.load_manifest(manifest_path)

    if not repo_root.is_dir():
        raise private_export_guard.InputError(f"repo root is not a directory: {repo_root}")
    if repo_root.resolve() == export_dir.resolve():
        raise private_export_guard.InputError("export dir must not be repo root")

    clear_directory(export_dir)
    staged_files: list[str] = []

    for relative in private_export_guard.relative_files(repo_root):
        if not included_product_path(manifest, relative):
            continue
        source = repo_root / relative
        if source.is_symlink():
            raise private_export_guard.InputError(f"included export path must not be a symlink: {relative}")
        target = export_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        staged_files.append(relative)

    return {"ok": True, "errors": [], "files": sorted(staged_files)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a public release export staging directory.")
    parser.add_argument("--repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--export-dir", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = stage_export(args.repo_root, args.manifest, args.export_dir)
    except private_export_guard.InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)], "files": []}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
