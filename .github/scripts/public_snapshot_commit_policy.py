#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any


ALLOWED_TYPES = ("feat", "fix")
SUBJECT_RE = re.compile(r"^(?P<type>[a-z][a-z0-9-]*)(?:\((?P<scope>[^()\r\n]+)\))?(?P<breaking>!)?: (?P<description>\S.*)$")
BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
FORBIDDEN_MESSAGE_REGEXES = (
    ("operator_home_path", re.compile(r"(^|[^A-Za-z0-9_])(/home/|/Users/|/mnt/c/Users/)[^\s\"']+")),
    ("windows_operator_home_path", re.compile(r"(?i)(^|[^A-Za-z0-9_])[A-Z]:\\Users\\[^\s\"']+")),
    ("private_sha_reference", re.compile(r"(?i)\b(private|internal)\s+(sha|commit)\s*[:=]?\s*[0-9a-f]{7,40}\b")),
    ("private_release_record_path", re.compile(r"(^|[\s\"'`])release/records/[^\s\"'`]+")),
    ("private_release_manifest_path", re.compile(r"(^|[\s\"'`])release/export-manifest\.json\b")),
    ("private_handoff_path", re.compile(r"(^|[\s\"'`])handoffs/[^\s\"'`]+")),
    ("private_review_path", re.compile(r"(^|[\s\"'`])reviews/[^\s\"'`]+")),
    ("private_codex_path", re.compile(r"(^|[\s\"'`])\.codex/[^\s\"'`]+")),
    ("private_transcript_reference", re.compile(r"(?i)\b(prompt text|transcript text|transcript raw|internal verification log)\b")),
    ("approver_reference", re.compile(r"(?i)\bapprover\s*[:=]")),
    ("internal_issue_reference", re.compile(r"(?i)\binternal\s+(issue|ticket)\s*[:#]")),
)


class InputError(Exception):
    pass


def parse_subject(subject: str) -> dict[str, Any] | None:
    match = SUBJECT_RE.match(subject)
    if not match:
        return None
    return {
        "subject": subject,
        "type": match.group("type"),
        "scope": match.group("scope"),
        "breaking": bool(match.group("breaking")),
    }


def first_line(message: str) -> str:
    return message.splitlines()[0].strip() if message.splitlines() else ""


def validate_snapshot_commit_message(message: str, allowed_types: tuple[str, ...] = ALLOWED_TYPES) -> dict[str, Any]:
    subject = first_line(message)
    parsed = parse_subject(subject)
    errors: list[str] = []

    if parsed is None:
        parsed = {"subject": subject, "type": None, "scope": None, "breaking": False}
        errors.append("snapshot subject must be a Conventional Commit subject")
    else:
        if parsed["type"] not in allowed_types:
            errors.append(f"snapshot subject type must be one of: {', '.join(allowed_types)}")
        parsed["breaking"] = bool(parsed["breaking"] or BREAKING_FOOTER_RE.search(message))

    for regex_id, pattern in FORBIDDEN_MESSAGE_REGEXES:
        if pattern.search(message):
            errors.append(f"blocked commit message content matches {regex_id}")

    return {"ok": not errors, "errors": errors, **parsed}


def read_message(args: argparse.Namespace) -> str:
    if args.subject is not None:
        return args.subject
    try:
        return args.message_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read commit message file: {args.message_file}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate public product snapshot commit message policy.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--subject")
    source.add_argument("--message-file", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_snapshot_commit_message(read_message(args))
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
