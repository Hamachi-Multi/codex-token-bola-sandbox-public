#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from typing import Any


ALLOWED_PRODUCT_PATHS = (
    "codex_token_bola/**",
    "scripts/**",
    "tests/**",
    "docs/assets/dashboard/overview.png",
    "docs/assets/dashboard/turns.png",
    "docs/assets/dashboard/tools.png",
    "docs/assets/dashboard/subagents.png",
    "docs/assets/dashboard/cleanup.png",
    "docs/assets/dashboard/settings.png",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "LICENSES/**",
    "LICENSE",
    "Makefile",
    "pyproject.toml",
    ".gitignore",
)
NEVER_PUBLIC_PATHS = (
    "analytics/**",
    "bad/**",
    "normalized/**",
    "raw/**",
    "reports/**",
    "state/**",
    "tmp/**",
    "release/**",
    "handoffs/**",
    "reviews/tmp/**",
    "reviews/**",
    "AGENTS.md",
    ".agents/**",
    ".codex/**",
    "*.jsonl",
    "*.ndjson",
    "*.pyc",
    "*.bak.*",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite*",
    "*.db*",
    "__pycache__/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    ".venv/**",
    "build/**",
    "dist/**",
    "*.egg-info/**",
    "node_modules/**",
    "*.log",
    ".env*",
    "setup.cfg",
    "docs/production-public-transition-checklist.md",
    "docs/production-public-readiness-gate.md",
    "docs/release-pipeline.md",
    "docs/release-pipeline-bootstrap-plan.md",
    "docs/release-pipeline-implementation-plan.md",
    "docs/sandbox-integration-dry-run.md",
)
PUBLIC_ONLY_OPS_PATHS = (
    ".github/**",
    "docs/public-ops-path-policy.md",
    "docs/public-ruleset-checklist.md",
    "package.json",
    "package-lock.json",
    ".releaserc.json",
    "scripts/public_main_release_guard.py",
    "scripts/public_snapshot_commit_policy.py",
)
RETIRED_PRODUCT_PATHS = (
    "assets/**",
    "docs/architecture-overview.md",
    "docs/dashboard-api-contract.md",
    "docs/dashboard-responsive-layout.md",
    "hooks/**",
)
FORBIDDEN_REGEXES = (
    ("operator_home_path", r"(^|[^A-Za-z0-9_])(/home/|/Users/|/mnt/c/Users/)[^\s\"']+"),
    ("windows_operator_home_path", r"(?i)(^|[^A-Za-z0-9_])[A-Z]:\\Users\\[^\s\"']+"),
    ("credential_literal", r"(?i)(api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[\"'][^\"']+[\"']"),
    ("github_token_literal", r"(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9_]+)"),
    ("openai_token_literal", r"sk-[A-Za-z0-9_-]{20,}"),
    ("private_key_material", r"-----BEGIN ([A-Z ]+ )?PRIVATE KEY-----"),
    ("private_transcript_artifact", r"rollout-[0-9T:-]+-[0-9a-f-]+\.jsonl|transcript raw"),
)
CONTENT_SCAN_EXCLUSIONS = frozenset(
    {
        ".github/scripts/public_candidate_snapshot_guard.py",
        ".github/scripts/public_snapshot_commit_policy.py",
    }
)


class InputError(Exception):
    pass


def matches(pattern: str, relative_path: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if relative_path == prefix or relative_path.startswith(f"{prefix}/"):
            return True
    return fnmatch.fnmatchcase(relative_path, pattern)


def matching_rule(patterns: tuple[str, ...], relative_path: str) -> str | None:
    return next((pattern for pattern in patterns if matches(pattern, relative_path)), None)


def normalize_relative_path(value: str) -> str:
    if not value or "\\" in value:
        raise InputError(f"candidate path must use a non-empty POSIX relative path: {value}")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise InputError(f"candidate path must be normalized and relative: {value}")
    return value


def run_git(repo_root: pathlib.Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=not binary,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if binary else result.stderr
        stdout = result.stdout.decode("utf-8", errors="replace") if binary else result.stdout
        detail = str(stderr).strip() or str(stdout).strip()
        raise InputError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def resolve_commit(repo_root: pathlib.Path, ref: str) -> str:
    output = run_git(repo_root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    return str(output).strip()


def changed_paths(repo_root: pathlib.Path, base_sha: str, head_sha: str) -> list[str]:
    output = run_git(repo_root, "diff", "--name-only", "--no-renames", "-z", base_sha, head_sha, binary=True)
    try:
        values = bytes(output).decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise InputError("candidate paths must be UTF-8") from exc
    return sorted(normalize_relative_path(value) for value in values if value)


def load_snapshot_policy(repo_root: pathlib.Path) -> Any:
    path = repo_root / ".github" / "scripts" / "public_snapshot_commit_policy.py"
    if path.is_symlink() or not path.is_file():
        raise InputError(f"snapshot commit policy must be a regular file: {path}")
    spec = importlib.util.spec_from_file_location("public_candidate_snapshot_commit_policy", path)
    if spec is None or spec.loader is None:
        raise InputError(f"cannot load snapshot commit policy: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate_tree(repo_root: pathlib.Path) -> tuple[list[str], list[str], list[str]]:
    paths: list[str] = []
    files: list[str] = []
    errors: list[str] = []
    for current, directories, filenames in os.walk(repo_root, topdown=True, followlinks=False):
        current_path = pathlib.Path(current)
        if current_path == repo_root:
            directories[:] = [name for name in directories if name != ".git"]
        directories.sort()
        filenames.sort()
        for name in [*directories, *filenames]:
            path = current_path / name
            relative = path.relative_to(repo_root).as_posix()
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise InputError(f"cannot inspect candidate path {relative}: {exc}") from exc
            if stat.S_ISLNK(mode):
                errors.append(f"candidate path must not be a symlink: {relative}")
                continue
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                errors.append(f"candidate path must be a regular file or directory: {relative}")
                continue
            paths.append(relative)
            if stat.S_ISREG(mode):
                files.append(relative)
    return paths, files, errors


def content_errors(repo_root: pathlib.Path, files: list[str]) -> list[str]:
    regexes = [(regex_id, re.compile(pattern)) for regex_id, pattern in FORBIDDEN_REGEXES]
    errors: list[str] = []
    for relative in files:
        if relative in CONTENT_SCAN_EXCLUSIONS:
            continue
        try:
            data = (repo_root / relative).read_bytes()
        except OSError as exc:
            errors.append(f"cannot read candidate file {relative}: {exc}")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for regex_id, pattern in regexes:
            if pattern.search(text):
                errors.append(f"forbidden content in {relative} matches {regex_id}")
    return errors


def validate_candidate(repo_root: pathlib.Path | str, *, base_ref: str, head_ref: str) -> dict[str, Any]:
    root = pathlib.Path(repo_root).resolve(strict=True)
    if not root.is_dir():
        raise InputError(f"repo root is not a directory: {root}")
    base_sha = resolve_commit(root, base_ref)
    head_sha = resolve_commit(root, head_ref)
    parents = str(run_git(root, "show", "-s", "--format=%P", head_sha)).strip().split()
    changed = changed_paths(root, base_sha, head_sha)
    errors: list[str] = []
    if parents != [base_sha]:
        errors.append("candidate head must have exactly the specified base commit as parent")
    if not changed:
        errors.append("candidate commit must change at least one product path")
    for relative in changed:
        never_public = matching_rule(NEVER_PUBLIC_PATHS, relative)
        public_ops = matching_rule(PUBLIC_ONLY_OPS_PATHS, relative)
        retired = matching_rule(RETIRED_PRODUCT_PATHS, relative)
        if never_public:
            errors.append(f"changed path {relative} matches never-public rule {never_public}")
        if public_ops:
            errors.append(f"changed path {relative} matches public-only ops rule {public_ops}")
        if matching_rule(ALLOWED_PRODUCT_PATHS, relative) is None and retired is None:
            errors.append(f"changed path is outside allowed product or retired paths: {relative}")
        if retired and ((root / relative).exists() or (root / relative).is_symlink()):
            errors.append(f"retired product path must be deleted from candidate: {relative}")
    tree_paths, tree_files, tree_errors = candidate_tree(root)
    errors.extend(tree_errors)
    for relative in tree_paths:
        never_public = matching_rule(NEVER_PUBLIC_PATHS, relative)
        retired = matching_rule(RETIRED_PRODUCT_PATHS, relative)
        if never_public:
            errors.append(f"candidate tree path {relative} matches never-public rule {never_public}")
        if retired:
            errors.append(f"candidate tree path {relative} matches retired product rule {retired}")
    errors.extend(content_errors(root, tree_files))
    message = str(run_git(root, "show", "-s", "--format=%B", head_sha))
    policy = load_snapshot_policy(root).validate_snapshot_commit_message(message)
    errors.extend(str(error) for error in policy["errors"])
    return {
        "ok": not errors,
        "errors": errors,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_paths": changed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a public product snapshot candidate commit and tree.")
    parser.add_argument("--repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_candidate(args.repo_root, base_ref=args.base_ref, head_ref=args.head_ref)
    except (InputError, OSError) as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
