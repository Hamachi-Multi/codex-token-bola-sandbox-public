#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import promotion_eligibility
import public_repo_bootstrap
import release_records


REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_FIELDS = [
    "sandbox_public_repo",
    "public_main_branch",
    "candidate",
    "attempt",
    "public_candidate_branch",
    "public_candidate_base_sha",
    "expected_public_candidate_sha",
    "required_public_checks",
    "protected_environments",
    "github_apps",
    "environment_secrets",
    "public_repo_variables",
    "run_mode",
]
REQUIRED_PROTECTED_ENVIRONMENTS = {
    "release_export": "production-release-export",
    "main_promotion": "production-main-promotion",
    "release_publish": "public-release-publish",
}
REQUIRED_GITHUB_APPS = ["snapshot", "promotion", "release_tag"]
REQUIRED_ENVIRONMENT_SECRETS = {
    "private": {
        "production-release-export": ["SNAPSHOT_APP_ID", "SNAPSHOT_PRIVATE_KEY"],
        "production-main-promotion": ["PROMOTION_APP_ID", "PROMOTION_PRIVATE_KEY"],
        "production-release-record-update": ["RELEASE_RECORD_APP_ID", "RELEASE_RECORD_PRIVATE_KEY"],
    },
    "public": {
        "public-release-publish": ["RELEASE_TAG_APP_ID", "RELEASE_TAG_PRIVATE_KEY"],
        "public-orphan-tag-recovery": ["ORPHAN_TAG_RECOVERY_APP_ID", "ORPHAN_TAG_RECOVERY_PRIVATE_KEY"],
    },
}
REQUIRED_PUBLIC_REPO_VARIABLES = ["PROMOTION_APP_ACTOR", "SNAPSHOT_AUTHOR_EMAIL", "PUBLIC_OPS_ACTOR"]
NEXT_INPUTS = [
    "replace example sandbox_public_repo with the live sandbox repo before live run",
    "install snapshot, promotion, and release-tag GitHub Apps on the sandbox public repo",
    "configure protected environment approvals for release export, main promotion, and release publish",
    "capture live public main base SHA before pushing the sandbox candidate branch",
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


def validate_required_fields(config: dict[str, Any]) -> list[str]:
    return [f"missing config field: {field}" for field in REQUIRED_FIELDS if field not in config]


def validate_repo(config: dict[str, Any]) -> list[str]:
    repo = config.get("sandbox_public_repo")
    if not isinstance(repo, str) or not REPO_NAME_RE.match(repo):
        return ["sandbox_public_repo must use owner/repo format"]
    return []


def validate_candidate_contract(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    candidate = config.get("candidate")
    attempt = config.get("attempt")
    branch = config.get("public_candidate_branch")
    if not isinstance(candidate, str):
        errors.append("candidate must be a string")
        candidate = ""
    if type(attempt) is not int:
        errors.append("attempt must be an integer")
        attempt = 0
    if not isinstance(branch, str):
        errors.append("public_candidate_branch must be a string")
        branch = ""

    if isinstance(candidate, str) and type(attempt) is int and isinstance(branch, str):
        try:
            release_records.validate_public_candidate_branch(branch, candidate, attempt)
        except release_records.InputError as exc:
            errors.append(str(exc))
    return errors


def validate_sha_fields(config: dict[str, Any]) -> list[str]:
    labels = {
        "public_candidate_base_sha": "public candidate base SHA",
        "expected_public_candidate_sha": "expected public candidate SHA",
    }
    errors: list[str] = []
    for field, label in labels.items():
        value = config.get(field)
        if not isinstance(value, str):
            errors.append(f"{label} must be a string")
            continue
        try:
            release_records.validate_sha(value, label)
        except release_records.InputError as exc:
            errors.append(str(exc))
    return errors


def validate_public_checks(config: dict[str, Any]) -> list[str]:
    checks = config.get("required_public_checks")
    if checks != promotion_eligibility.REQUIRED_PUBLIC_CHECKS:
        return ["required_public_checks must match promotion eligibility required checks"]
    return []


def validate_protected_environments(config: dict[str, Any]) -> list[str]:
    environments = config.get("protected_environments")
    if not isinstance(environments, dict):
        return ["protected_environments must be a JSON object"]
    errors: list[str] = []
    for key, expected in REQUIRED_PROTECTED_ENVIRONMENTS.items():
        if environments.get(key) != expected:
            errors.append(f"protected_environments.{key} must be {expected}")
    return errors


def validate_github_apps(config: dict[str, Any]) -> list[str]:
    apps = config.get("github_apps")
    if not isinstance(apps, dict):
        return ["github_apps must be a JSON object"]
    errors: list[str] = []
    for app_name in REQUIRED_GITHUB_APPS:
        app = apps.get(app_name)
        if not isinstance(app, dict):
            errors.append(f"github_apps.{app_name} must be a JSON object")
            continue
        for field in ("app_slug", "actor"):
            if not isinstance(app.get(field), str) or not app.get(field):
                errors.append(f"github_apps.{app_name}.{field} must be a non-empty string")
    return errors


def validate_environment_secrets(config: dict[str, Any]) -> list[str]:
    environment_secrets = config.get("environment_secrets")
    if not isinstance(environment_secrets, dict):
        return ["environment_secrets must be a JSON object"]
    errors: list[str] = []
    for repo_kind, environments in REQUIRED_ENVIRONMENT_SECRETS.items():
        configured_environments = environment_secrets.get(repo_kind)
        if not isinstance(configured_environments, dict):
            errors.append(f"environment_secrets.{repo_kind} must be a JSON object")
            continue
        for environment, expected_secrets in environments.items():
            configured_secrets = configured_environments.get(environment)
            if configured_secrets != expected_secrets:
                errors.append(f"environment_secrets.{repo_kind}.{environment} must be: {', '.join(expected_secrets)}")
    return errors


def validate_public_repo_variables(config: dict[str, Any]) -> list[str]:
    variables = config.get("public_repo_variables")
    if variables != REQUIRED_PUBLIC_REPO_VARIABLES:
        return [f"public_repo_variables must be: {', '.join(REQUIRED_PUBLIC_REPO_VARIABLES)}"]
    return []


def validate_public_bootstrap(repo_root: pathlib.Path) -> list[str]:
    try:
        result = public_repo_bootstrap.validate_public_bootstrap(repo_root / "release" / "public-bootstrap")
    except public_repo_bootstrap.InputError as exc:
        return [f"public bootstrap: {exc}"]
    return [f"public bootstrap: {error}" for error in result["errors"]]


def validate_sandbox_dry_run_readiness(repo_root: pathlib.Path | str, config_path: pathlib.Path | str) -> dict[str, Any]:
    repo_root = pathlib.Path(repo_root)
    config = read_json(pathlib.Path(config_path), "sandbox dry-run config")
    errors = validate_required_fields(config)
    warnings: list[str] = []

    if not repo_root.is_dir():
        errors.append(f"repo root is not a directory: {repo_root}")
    if config.get("public_main_branch") != "main":
        errors.append("public_main_branch must be main")
    if config.get("run_mode") != "readiness_only":
        errors.append("run_mode must be readiness_only")
    if isinstance(config.get("sandbox_public_repo"), str) and config["sandbox_public_repo"].startswith("example/"):
        warnings.append("sandbox_public_repo is an example value")

    errors.extend(validate_repo(config))
    errors.extend(validate_candidate_contract(config))
    errors.extend(validate_sha_fields(config))
    errors.extend(validate_public_checks(config))
    errors.extend(validate_protected_environments(config))
    errors.extend(validate_github_apps(config))
    errors.extend(validate_environment_secrets(config))
    errors.extend(validate_public_repo_variables(config))
    if repo_root.is_dir():
        errors.extend(validate_public_bootstrap(repo_root))

    return {"ok": not errors, "errors": errors, "warnings": warnings, "next_inputs": list(NEXT_INPUTS)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate sandbox public release dry-run readiness inputs.")
    parser.add_argument("--repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--config", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_sandbox_dry_run_readiness(args.repo_root, args.config)
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)], "warnings": [], "next_inputs": []}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
