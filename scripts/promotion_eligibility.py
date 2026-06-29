#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any


REQUIRED_PUBLIC_CHECKS = [
    "public-ci / compile-test",
    "public-ci / asset-static-sanity",
    "public-ci / public-sensitive-guard",
    "public-ci / candidate-snapshot-guard",
    "codeql",
]


class InputError(Exception):
    pass


def read_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid {label} json: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError(f"{label} must be a JSON object: {path}")
    return payload


def validate_promotion_eligibility(
    record_path: pathlib.Path | str,
    public_state: dict[str, Any],
    *,
    expected_public_candidate_sha: str,
) -> dict[str, Any]:
    record = read_json(pathlib.Path(record_path), "release record")
    errors: list[str] = []
    record_public_candidate_sha = record.get("public_candidate_sha")

    if record.get("status") != "candidate_pushed":
        errors.append("release record status must be candidate_pushed")
    if record_public_candidate_sha != expected_public_candidate_sha:
        errors.append("release record public_candidate_sha does not match expected public candidate SHA")
    if public_state.get("public_candidate_branch") != record.get("public_candidate_branch"):
        errors.append("public candidate branch does not match release record")
    if public_state.get("public_candidate_head_sha") != record_public_candidate_sha:
        errors.append("public candidate head SHA does not match release record public_candidate_sha")
    if public_state.get("public_main_sha") != record.get("public_candidate_base_sha"):
        errors.append("public main SHA does not match recorded public candidate base SHA")

    checks = public_state.get("checks")
    if not isinstance(checks, dict):
        checks = {}
    for check_name in REQUIRED_PUBLIC_CHECKS:
        conclusion = checks.get(check_name)
        if conclusion is None:
            errors.append(f"required public check missing: {check_name}")
        elif conclusion != "success":
            errors.append(f"required public check failed: {check_name}={conclusion}")

    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True, "errors": [], "promotion_target_sha": record_public_candidate_sha}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate public main promotion eligibility.")
    parser.add_argument("--record", required=True, type=pathlib.Path)
    parser.add_argument("--public-state", required=True, type=pathlib.Path)
    parser.add_argument("--expected-public-candidate-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        public_state = read_json(args.public_state, "public state")
        result = validate_promotion_eligibility(
            args.record,
            public_state,
            expected_public_candidate_sha=args.expected_public_candidate_sha,
        )
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
