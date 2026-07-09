#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any


RECORD_RELATIVE_ROOT = "release/records"
SAFE_CANDIDATE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SNAPSHOT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DEFAULT_CHECKS = {
    "private_compile_test": "pending",
    "private_ui_check": "pending",
    "private_export_guard": "pending",
    "public_release_candidate_diff_review": "pending",
    "public_release_candidate_checks": "pending",
    "public_main_promotion": "pending",
    "public_ci": "pending",
    "semantic_release": "pending",
}
ALLOWED_TRANSITIONS = {
    "pending": {"approved", "failed"},
    "approved": {"candidate_prepared", "failed"},
    "candidate_prepared": {"candidate_pushed", "failed"},
    "candidate_pushed": {"promoted", "failed"},
    "promoted": {"published", "failed"},
}


class InputError(Exception):
    pass


def validate_candidate(candidate: str) -> str:
    if not candidate or not SAFE_CANDIDATE_RE.match(candidate):
        raise InputError(f"candidate must be a safe relative name: {candidate}")
    return candidate


def validate_attempt(attempt: int) -> int:
    if attempt < 1:
        raise InputError(f"attempt must be positive: {attempt}")
    return attempt


def validate_sha(value: str, label: str) -> str:
    if not HEX_SHA_RE.match(value):
        raise InputError(f"{label} must be a 40 character lowercase hex SHA")
    return value


def validate_snapshot_manifest_digest(value: str) -> str:
    if not SNAPSHOT_DIGEST_RE.match(value):
        raise InputError("snapshot manifest digest must be sha256:<64 lowercase hex chars>")
    return value


def attempt_record_relative_path(candidate: str, attempt: int) -> str:
    candidate = validate_candidate(candidate)
    attempt = validate_attempt(attempt)
    return f"{RECORD_RELATIVE_ROOT}/{candidate}/attempt-{attempt:03d}.json"


def attempt_record_path(records_root: pathlib.Path | str, candidate: str, attempt: int) -> pathlib.Path:
    validate_candidate(candidate)
    validate_attempt(attempt)
    return pathlib.Path(records_root) / candidate / f"attempt-{attempt:03d}.json"


def validate_public_candidate_branch(branch: str, candidate: str, attempt: int) -> str:
    expected = f"release-candidate/{validate_candidate(candidate)}-attempt-{validate_attempt(attempt):03d}"
    if branch != expected:
        raise InputError(f"public candidate branch must match candidate and attempt: expected {expected}")
    return branch


def summary_index_path(records_root: pathlib.Path | str, candidate: str) -> pathlib.Path:
    candidate = validate_candidate(candidate)
    return pathlib.Path(records_root) / f"{candidate}.json"


def atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise InputError(f"cannot read release record: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid release record json: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError(f"release record must be a JSON object: {path}")
    return payload


def merge_checks(checks: dict[str, str] | None) -> dict[str, str]:
    merged = dict(DEFAULT_CHECKS)
    if checks:
        for key, value in checks.items():
            if key not in DEFAULT_CHECKS:
                raise InputError(f"unknown release record check: {key}")
            merged[key] = value
    return merged


def validate_transition(current_status: str, next_status: str) -> None:
    if next_status not in ALLOWED_TRANSITIONS.get(current_status, set()):
        raise InputError(f"invalid release record transition: {current_status} -> {next_status}")


def write_summary_index(records_root: pathlib.Path | str, record: dict[str, Any]) -> dict[str, Any]:
    candidate = validate_candidate(str(record["candidate"]))
    attempt = validate_attempt(int(record["attempt"]))
    summary = {
        "candidate": candidate,
        "latest_attempt": attempt,
        "latest_status": record["status"],
        "latest_record": attempt_record_relative_path(candidate, attempt),
        "public_main_sha": record.get("public_main_sha") or "",
        "version": record.get("version") or "",
        "tag": record.get("tag") or "",
        "updated_at": record["updated_at"],
    }
    atomic_write_json(summary_index_path(records_root, candidate), summary)
    return summary


def write_candidate_prepared(
    records_root: pathlib.Path | str,
    *,
    candidate: str,
    attempt: int,
    release_ref: str,
    private_main_sha: str,
    private_release_sha: str,
    public_candidate_branch: str,
    public_candidate_base_sha: str,
    snapshot_manifest_digest: str,
    approver: str,
    created_at: str,
    updated_at: str,
    checks: dict[str, str] | None = None,
    previous_attempt: str | None = None,
) -> dict[str, Any]:
    records_root = pathlib.Path(records_root)
    candidate = validate_candidate(candidate)
    attempt = validate_attempt(attempt)
    path = attempt_record_path(records_root, candidate, attempt)
    if path.exists():
        raise InputError(f"release attempt record already exists: {path}")

    record = {
        "candidate": candidate,
        "attempt": attempt,
        "status": "candidate_prepared",
        "release_ref": release_ref,
        "private_main_sha": validate_sha(private_main_sha, "private main SHA"),
        "private_release_sha": validate_sha(private_release_sha, "private release SHA"),
        "public_candidate_branch": validate_public_candidate_branch(public_candidate_branch, candidate, attempt),
        "public_candidate_base_sha": validate_sha(public_candidate_base_sha, "public candidate base SHA"),
        "public_candidate_sha": None,
        "snapshot_manifest_digest": validate_snapshot_manifest_digest(snapshot_manifest_digest),
        "public_main_sha": "",
        "version": "",
        "tag": "",
        "github_release_url": "",
        "approver": approver,
        "previous_attempt": previous_attempt,
        "checks": merge_checks(checks),
        "incident": None,
        "failure_stage": "",
        "failure_reason": "",
        "created_at": created_at,
        "updated_at": updated_at,
    }
    atomic_write_json(path, record)
    write_summary_index(records_root, record)
    return {"ok": True, "status": record["status"], "record": attempt_record_relative_path(candidate, attempt)}


def mark_candidate_pushed(
    records_root: pathlib.Path | str,
    *,
    candidate: str,
    attempt: int,
    public_candidate_sha: str,
    updated_at: str,
) -> dict[str, Any]:
    records_root = pathlib.Path(records_root)
    path = attempt_record_path(records_root, candidate, attempt)
    record = read_json(path)
    validate_transition(str(record.get("status") or ""), "candidate_pushed")
    record["status"] = "candidate_pushed"
    record["public_candidate_sha"] = validate_sha(public_candidate_sha, "public candidate SHA")
    record["updated_at"] = updated_at
    atomic_write_json(path, record)
    write_summary_index(records_root, record)
    return {"ok": True, "status": record["status"], "record": attempt_record_relative_path(candidate, attempt)}


def mark_promoted(
    records_root: pathlib.Path | str,
    *,
    candidate: str,
    attempt: int,
    public_main_sha: str,
    updated_at: str,
) -> dict[str, Any]:
    records_root = pathlib.Path(records_root)
    path = attempt_record_path(records_root, candidate, attempt)
    record = read_json(path)
    validate_transition(str(record.get("status") or ""), "promoted")
    public_main_sha = validate_sha(public_main_sha, "public main SHA")
    if record.get("public_candidate_sha") != public_main_sha:
        raise InputError("public main SHA must match release record public_candidate_sha")

    checks = merge_checks(record.get("checks"))
    checks["public_main_promotion"] = "passed"
    record["status"] = "promoted"
    record["public_main_sha"] = public_main_sha
    record["checks"] = checks
    record["updated_at"] = updated_at
    atomic_write_json(path, record)
    write_summary_index(records_root, record)
    return {"ok": True, "status": record["status"], "record": attempt_record_relative_path(candidate, attempt)}


def mark_published(
    records_root: pathlib.Path | str,
    *,
    candidate: str,
    attempt: int,
    version: str,
    tag: str,
    github_release_url: str,
    updated_at: str,
) -> dict[str, Any]:
    records_root = pathlib.Path(records_root)
    path = attempt_record_path(records_root, candidate, attempt)
    record = read_json(path)
    validate_transition(str(record.get("status") or ""), "published")
    if not version:
        raise InputError("version must be non-empty")
    if not tag:
        raise InputError("tag must be non-empty")
    if not github_release_url:
        raise InputError("github release URL must be non-empty")

    checks = merge_checks(record.get("checks"))
    checks["semantic_release"] = "passed"
    record["status"] = "published"
    record["version"] = version
    record["tag"] = tag
    record["github_release_url"] = github_release_url
    record["checks"] = checks
    record["updated_at"] = updated_at
    atomic_write_json(path, record)
    write_summary_index(records_root, record)
    return {"ok": True, "status": record["status"], "record": attempt_record_relative_path(candidate, attempt)}


def mark_failed(
    records_root: pathlib.Path | str,
    *,
    candidate: str,
    attempt: int,
    failure_stage: str,
    failure_reason: str,
    updated_at: str,
) -> dict[str, Any]:
    records_root = pathlib.Path(records_root)
    path = attempt_record_path(records_root, candidate, attempt)
    record = read_json(path)
    validate_transition(str(record.get("status") or ""), "failed")
    if not failure_stage:
        raise InputError("failure stage must be non-empty")
    if not failure_reason:
        raise InputError("failure reason must be non-empty")

    record["status"] = "failed"
    record["failure_stage"] = failure_stage
    record["failure_reason"] = failure_reason
    record["updated_at"] = updated_at
    atomic_write_json(path, record)
    write_summary_index(records_root, record)
    return {"ok": True, "status": record["status"], "record": attempt_record_relative_path(candidate, attempt)}


def normalize_staged_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise InputError("staged path must be a non-empty relative path")
    if "\\" in path:
        raise InputError(f"staged path must use forward slashes: {path}")
    candidate = pathlib.PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != path:
        raise InputError(f"staged path must be a normalized relative path: {path}")
    return path


def validate_staged_record_paths(paths: list[str]) -> dict[str, Any]:
    errors: list[str] = []
    for path in paths:
        path = normalize_staged_path(path)
        if not path.startswith(f"{RECORD_RELATIVE_ROOT}/"):
            errors.append(f"staged path outside release records: {path}")
    return {"ok": not errors, "errors": errors}


def read_path_file(path: pathlib.Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        raise InputError(f"cannot read staged paths file: {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage private release attempt records.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepared = subparsers.add_parser("candidate-prepared")
    prepared.add_argument("--records-root", required=True, type=pathlib.Path)
    prepared.add_argument("--candidate", required=True)
    prepared.add_argument("--attempt", required=True, type=int)
    prepared.add_argument("--release-ref", required=True)
    prepared.add_argument("--private-main-sha", required=True)
    prepared.add_argument("--private-release-sha", required=True)
    prepared.add_argument("--public-candidate-branch", required=True)
    prepared.add_argument("--public-candidate-base-sha", required=True)
    prepared.add_argument("--snapshot-manifest-digest", required=True)
    prepared.add_argument("--approver", required=True)
    prepared.add_argument("--created-at", required=True)
    prepared.add_argument("--updated-at", required=True)

    pushed = subparsers.add_parser("candidate-pushed")
    pushed.add_argument("--records-root", required=True, type=pathlib.Path)
    pushed.add_argument("--candidate", required=True)
    pushed.add_argument("--attempt", required=True, type=int)
    pushed.add_argument("--public-candidate-sha", required=True)
    pushed.add_argument("--updated-at", required=True)

    promoted = subparsers.add_parser("promoted")
    promoted.add_argument("--records-root", required=True, type=pathlib.Path)
    promoted.add_argument("--candidate", required=True)
    promoted.add_argument("--attempt", required=True, type=int)
    promoted.add_argument("--public-main-sha", required=True)
    promoted.add_argument("--updated-at", required=True)

    published = subparsers.add_parser("published")
    published.add_argument("--records-root", required=True, type=pathlib.Path)
    published.add_argument("--candidate", required=True)
    published.add_argument("--attempt", required=True, type=int)
    published.add_argument("--version", required=True)
    published.add_argument("--tag", required=True)
    published.add_argument("--github-release-url", required=True)
    published.add_argument("--updated-at", required=True)

    failed = subparsers.add_parser("failed")
    failed.add_argument("--records-root", required=True, type=pathlib.Path)
    failed.add_argument("--candidate", required=True)
    failed.add_argument("--attempt", required=True, type=int)
    failed.add_argument("--failure-stage", required=True)
    failed.add_argument("--failure-reason", required=True)
    failed.add_argument("--updated-at", required=True)

    guard = subparsers.add_parser("guard-staged")
    guard.add_argument("--paths-file", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "candidate-prepared":
            result = write_candidate_prepared(
                args.records_root,
                candidate=args.candidate,
                attempt=args.attempt,
                release_ref=args.release_ref,
                private_main_sha=args.private_main_sha,
                private_release_sha=args.private_release_sha,
                public_candidate_branch=args.public_candidate_branch,
                public_candidate_base_sha=args.public_candidate_base_sha,
                snapshot_manifest_digest=args.snapshot_manifest_digest,
                approver=args.approver,
                created_at=args.created_at,
                updated_at=args.updated_at,
            )
        elif args.command == "candidate-pushed":
            result = mark_candidate_pushed(
                args.records_root,
                candidate=args.candidate,
                attempt=args.attempt,
                public_candidate_sha=args.public_candidate_sha,
                updated_at=args.updated_at,
            )
        elif args.command == "promoted":
            result = mark_promoted(
                args.records_root,
                candidate=args.candidate,
                attempt=args.attempt,
                public_main_sha=args.public_main_sha,
                updated_at=args.updated_at,
            )
        elif args.command == "published":
            result = mark_published(
                args.records_root,
                candidate=args.candidate,
                attempt=args.attempt,
                version=args.version,
                tag=args.tag,
                github_release_url=args.github_release_url,
                updated_at=args.updated_at,
            )
        elif args.command == "failed":
            result = mark_failed(
                args.records_root,
                candidate=args.candidate,
                attempt=args.attempt,
                failure_stage=args.failure_stage,
                failure_reason=args.failure_reason,
                updated_at=args.updated_at,
            )
        elif args.command == "guard-staged":
            result = validate_staged_record_paths(read_path_file(args.paths_file))
        else:
            raise InputError(f"unknown release records command: {args.command}")
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
