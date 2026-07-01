#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any


REQUIRED_FILES = [
    ".github/workflows/release.yml",
    ".github/workflows/codeql.yml",
    ".github/dependabot.yml",
    "package.json",
    "package-lock.json",
    ".releaserc.json",
    "scripts/public_main_release_guard.py",
    "scripts/public_snapshot_commit_policy.py",
    "docs/public-ruleset-checklist.md",
    "docs/public-ops-path-policy.md",
]
REQUIRED_RELEASE_JOBS = {
    "compile_test": "compile-test",
    "asset_static_sanity": "asset-static-sanity",
    "public_sensitive_guard": "public-sensitive-guard",
    "candidate_snapshot_guard": "candidate-snapshot-guard",
    "product_snapshot_guard": "product-snapshot-guard",
    "semantic_release": "semantic-release",
}
REQUIRED_SEMANTIC_RELEASE_DEPS = [
    "semantic-release",
    "@semantic-release/commit-analyzer",
    "@semantic-release/release-notes-generator",
    "@semantic-release/github",
    "conventional-changelog-conventionalcommits",
]
FORBIDDEN_SEMANTIC_RELEASE_PLUGINS = [
    "@semantic-release/git",
    "@semantic-release/changelog",
]


class InputError(Exception):
    pass


def read_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid {label} json: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError(f"{label} must be a JSON object: {path}")
    return payload


def flatten_plugins(plugins: object) -> list[str]:
    if not isinstance(plugins, list):
        return []
    flattened: list[str] = []
    for plugin in plugins:
        if isinstance(plugin, str):
            flattened.append(plugin)
        elif isinstance(plugin, list) and plugin and isinstance(plugin[0], str):
            flattened.append(plugin[0])
    return flattened


def validate_inventory(root: pathlib.Path) -> list[str]:
    return [f"missing public bootstrap file: {relative}" for relative in REQUIRED_FILES if not (root / relative).is_file()]


def validate_release_workflow(root: pathlib.Path) -> list[str]:
    workflow_path = root / ".github" / "workflows" / "release.yml"
    if not workflow_path.is_file():
        return []
    text = workflow_path.read_text(encoding="utf-8")
    errors: list[str] = []
    if "name: public-ci" not in text:
        errors.append("public release workflow name must be public-ci")
    if "release-candidate/*" not in text:
        errors.append("public release workflow must trigger on release-candidate/*")
    if "permissions:\n  contents: read\n  id-token: none" not in text:
        errors.append("public release workflow must default to contents read and id-token none")
    for job_id, display_name in REQUIRED_RELEASE_JOBS.items():
        if f"  {job_id}:" not in text:
            errors.append(f"public release workflow missing required job id: {job_id}")
        if f"name: {display_name}" not in text:
            errors.append(f"public release workflow missing required job display name: {display_name}")
    if "github.ref == 'refs/heads/main'" not in text:
        errors.append("semantic-release must be gated to public main")
    if "scripts/public_main_release_guard.py" not in text:
        errors.append("public release workflow must call public main release guard")
    if "product snapshot actor, subject, and codeql polling run here" in text:
        errors.append("public release workflow must not keep product snapshot guard placeholder")
    if (
        "release-tag App token mint is not implemented" in text
        or "semantic-release runs here with release-tag App GH_TOKEN" in text
    ):
        errors.append("public release workflow must not keep semantic-release publish placeholder")
    if "semantic_release: ${{ steps.guard.outputs.semantic_release }}" not in text:
        errors.append("public release workflow must expose semantic_release guard output")
    if "needs.product_snapshot_guard.outputs.semantic_release == 'true'" not in text:
        errors.append("semantic-release must be gated by semantic_release guard output")
    release_token_fragments = (
        "id: release-tag-token",
        "uses: actions/create-github-app-token@v3",
        "app-id: ${{ secrets.RELEASE_TAG_APP_ID }}",
        "private-key: ${{ secrets.RELEASE_TAG_PRIVATE_KEY }}",
        "owner: ${{ github.repository_owner }}",
        "repositories: ${{ github.event.repository.name }}",
        "permission-contents: write",
    )
    if not all(fragment in text for fragment in release_token_fragments):
        errors.append("public release workflow must mint release-tag GitHub App token")
    if "GH_TOKEN: ${{ steps.release-tag-token.outputs.token }}" not in text:
        errors.append("public release workflow must pass release-tag token as semantic-release GH_TOKEN")
    if 'git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/${{ github.repository }}.git"' not in text:
        errors.append("public release workflow must configure git remote with release-tag token")
    if "npx semantic-release" not in text:
        errors.append("public release workflow must run npx semantic-release")
    for variable in ("vars.PROMOTION_APP_ACTOR", "vars.SNAPSHOT_AUTHOR_EMAIL", "vars.PUBLIC_OPS_ACTOR"):
        if variable not in text:
            errors.append(f"public release workflow missing guard variable: {variable}")
    if "@semantic-release/git" in text or "@semantic-release/changelog" in text:
        errors.append("public release workflow must not run semantic-release git or changelog plugins")
    return errors


def validate_codeql_workflow(root: pathlib.Path) -> list[str]:
    codeql_path = root / ".github" / "workflows" / "codeql.yml"
    if not codeql_path.is_file():
        return []
    text = codeql_path.read_text(encoding="utf-8")
    errors: list[str] = []
    for fragment in (
        "name: codeql",
        "security-events: write",
        "    name: codeql",
        "github/codeql-action/init@v3",
        "languages: python",
        "github/codeql-action/analyze@v3",
    ):
        if fragment not in text:
            errors.append(f"codeql workflow missing fragment: {fragment}")
    return errors


def validate_semantic_release(root: pathlib.Path) -> list[str]:
    errors: list[str] = []
    package_path = root / "package.json"
    release_config_path = root / ".releaserc.json"
    lock_path = root / "package-lock.json"
    if package_path.is_file():
        package = read_json(package_path, "package.json")
        dev_dependencies = package.get("devDependencies")
        if not isinstance(dev_dependencies, dict):
            errors.append("package.json devDependencies must be a JSON object")
            dev_dependencies = {}
        for dependency in REQUIRED_SEMANTIC_RELEASE_DEPS:
            if dependency not in dev_dependencies:
                errors.append(f"package.json missing semantic-release dependency: {dependency}")
    if lock_path.is_file():
        lock = read_json(lock_path, "package-lock.json")
        packages = lock.get("packages")
        if not isinstance(packages, dict) or "" not in packages:
            errors.append("package-lock.json must include root package metadata")
    if release_config_path.is_file():
        config = read_json(release_config_path, ".releaserc.json")
        if config.get("branches") != ["main"]:
            errors.append("semantic-release branches must be ['main']")
        if config.get("tagFormat") != "v${version}":
            errors.append("semantic-release tagFormat must be v${version}")
        plugins = flatten_plugins(config.get("plugins"))
        for dependency in REQUIRED_SEMANTIC_RELEASE_DEPS[1:4]:
            if dependency not in plugins:
                errors.append(f"semantic-release config missing plugin: {dependency}")
        for forbidden in FORBIDDEN_SEMANTIC_RELEASE_PLUGINS:
            if forbidden in plugins:
                errors.append(f"semantic-release baseline must not include plugin: {forbidden}")
    return errors


def validate_docs(root: pathlib.Path) -> list[str]:
    required_fragments = {
        "docs/public-ruleset-checklist.md": [
            "public-branch-catch-all",
            "public-main-snapshot-promotion",
            "public-release-candidate-branches",
            "public-release-tags",
            "secret scanning push protection",
        ],
        "docs/public-ops-path-policy.md": [
            "public-ops-path-policy",
            ".github/**",
            "package.json",
            "package-lock.json",
            ".releaserc.json",
            "chore(public-ops):",
        ],
    }
    errors: list[str] = []
    for relative, fragments in required_fragments.items():
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for fragment in fragments:
            if fragment not in text:
                errors.append(f"{relative} missing required fragment: {fragment}")
    return errors


def validate_public_bootstrap(bootstrap_root: pathlib.Path | str) -> dict[str, Any]:
    root = pathlib.Path(bootstrap_root)
    if not root.is_dir():
        raise InputError(f"bootstrap root is not a directory: {root}")
    errors = [
        *validate_inventory(root),
        *validate_release_workflow(root),
        *validate_codeql_workflow(root),
        *validate_semantic_release(root),
        *validate_docs(root),
    ]
    return {"ok": not errors, "errors": errors}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate public repo bootstrap artifacts.")
    parser.add_argument("--bootstrap-root", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_public_bootstrap(args.bootstrap_root)
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
