#!/usr/bin/env python3
from __future__ import annotations

import argparse
import filecmp
import json
import pathlib
import shutil
import sys
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import private_export_guard
import release_export_staging


def write_path_file(path: pathlib.Path | None, paths: list[str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{relative}\n" for relative in paths), encoding="utf-8")


def remove_empty_parents(root: pathlib.Path, start: pathlib.Path) -> None:
    current = start
    while current != root and current.exists():
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def product_files(root: pathlib.Path, manifest: dict[str, Any]) -> set[str]:
    if not root.is_dir():
        return set()
    return {relative for relative in private_export_guard.relative_files(root) if release_export_staging.included_product_path(manifest, relative)}


def sync_candidate(
    manifest_path: pathlib.Path | str,
    export_dir: pathlib.Path | str,
    public_worktree: pathlib.Path | str,
    *,
    changed_paths_file: pathlib.Path | None = None,
    staged_paths_file: pathlib.Path | None = None,
) -> dict[str, Any]:
    manifest = private_export_guard.load_manifest(pathlib.Path(manifest_path))
    export_dir = pathlib.Path(export_dir)
    public_worktree = pathlib.Path(public_worktree)

    if not export_dir.is_dir():
        raise private_export_guard.InputError(f"export dir is not a directory: {export_dir}")
    if not public_worktree.is_dir():
        raise private_export_guard.InputError(f"public worktree is not a directory: {public_worktree}")

    export_files = product_files(export_dir, manifest)
    existing_files = product_files(public_worktree, manifest)
    changed_paths: set[str] = set()

    for relative in sorted(existing_files - export_files):
        target = public_worktree / relative
        target.unlink()
        remove_empty_parents(public_worktree, target.parent)
        changed_paths.add(relative)

    for relative in sorted(export_files):
        source = export_dir / relative
        target = public_worktree / relative
        if target.exists() and filecmp.cmp(source, target, shallow=False):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        changed_paths.add(relative)

    paths = sorted(changed_paths)
    write_path_file(changed_paths_file, paths)
    write_path_file(staged_paths_file, paths)
    return {"ok": True, "errors": [], "changed_paths": paths, "staged_paths": paths}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync a public release candidate worktree from export staging.")
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--export-dir", required=True, type=pathlib.Path)
    parser.add_argument("--public-worktree", required=True, type=pathlib.Path)
    parser.add_argument("--changed-paths-file", type=pathlib.Path)
    parser.add_argument("--staged-paths-file", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = sync_candidate(
            args.manifest,
            args.export_dir,
            args.public_worktree,
            changed_paths_file=args.changed_paths_file,
            staged_paths_file=args.staged_paths_file,
        )
    except private_export_guard.InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)], "changed_paths": [], "staged_paths": []}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
