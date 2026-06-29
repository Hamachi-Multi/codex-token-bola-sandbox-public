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

import public_snapshot_commit_policy


PUBLIC_OPS_PREFIX = "chore(public-ops):"


class InputError(Exception):
    pass


def first_line(message: str) -> str:
    return message.splitlines()[0].strip() if message.splitlines() else ""


def is_public_ops_message(message: str) -> bool:
    return first_line(message).startswith(PUBLIC_OPS_PREFIX)


def validate_public_ops(actor: str, public_ops_actor: str) -> dict[str, Any]:
    errors: list[str] = []
    if actor != public_ops_actor:
        errors.append("public ops push actor must match expected public ops actor")
    return {"ok": not errors, "errors": errors, "release_kind": "public_ops", "semantic_release": False}


def validate_product_snapshot(
    *,
    message: str,
    actor: str,
    expected_promotion_actor: str,
    author_email: str,
    expected_snapshot_author_email: str,
    codeql_conclusion: str,
) -> dict[str, Any]:
    policy = public_snapshot_commit_policy.validate_snapshot_commit_message(message)
    errors = list(policy["errors"])
    if actor != expected_promotion_actor:
        errors.append("product snapshot push actor must match expected promotion actor")
    if author_email != expected_snapshot_author_email:
        errors.append("product snapshot author email must match expected snapshot author email")
    if codeql_conclusion != "success":
        errors.append("product snapshot codeql conclusion must be success")

    release_kind = "product_snapshot" if policy["ok"] else "invalid"
    return {
        "ok": not errors,
        "errors": errors,
        "release_kind": release_kind,
        "semantic_release": not errors and release_kind == "product_snapshot",
    }


def validate_public_main_release(
    *,
    ref: str,
    message: str,
    actor: str,
    expected_promotion_actor: str,
    author_email: str,
    expected_snapshot_author_email: str,
    codeql_conclusion: str,
    public_ops_actor: str,
) -> dict[str, Any]:
    if ref != "refs/heads/main":
        return {"ok": False, "errors": ["public main release guard only accepts refs/heads/main"], "release_kind": "invalid", "semantic_release": False}
    if is_public_ops_message(message):
        return validate_public_ops(actor, public_ops_actor)
    return validate_product_snapshot(
        message=message,
        actor=actor,
        expected_promotion_actor=expected_promotion_actor,
        author_email=author_email,
        expected_snapshot_author_email=expected_snapshot_author_email,
        codeql_conclusion=codeql_conclusion,
    )


def read_message(args: argparse.Namespace) -> str:
    if args.subject is not None:
        return args.subject
    try:
        return args.message_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read commit message file: {args.message_file}: {exc}") from exc


def write_github_output(path: pathlib.Path, result: dict[str, Any]) -> None:
    value = "true" if result["semantic_release"] else "false"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"release_kind={result['release_kind']}\n")
            handle.write(f"semantic_release={value}\n")
    except OSError as exc:
        raise InputError(f"cannot write github output file: {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate public main release guard policy.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--subject")
    source.add_argument("--message-file", type=pathlib.Path)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--expected-promotion-actor", required=True)
    parser.add_argument("--author-email", required=True)
    parser.add_argument("--expected-snapshot-author-email", required=True)
    parser.add_argument("--codeql-conclusion", required=True)
    parser.add_argument("--public-ops-actor", required=True)
    parser.add_argument("--github-output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_public_main_release(
            ref=args.ref,
            message=read_message(args),
            actor=args.actor,
            expected_promotion_actor=args.expected_promotion_actor,
            author_email=args.author_email,
            expected_snapshot_author_email=args.expected_snapshot_author_email,
            codeql_conclusion=args.codeql_conclusion,
            public_ops_actor=args.public_ops_actor,
        )
        if args.github_output is not None:
            write_github_output(args.github_output, result)
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)], "release_kind": "invalid", "semantic_release": False}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
