#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
from typing import Any


SAFE_CANDIDATE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CHECK_NAME_MAP = {
    "compile-test": "public-ci / compile-test",
    "asset-static-sanity": "public-ci / asset-static-sanity",
    "public-sensitive-guard": "public-ci / public-sensitive-guard",
    "candidate-snapshot-guard": "public-ci / candidate-snapshot-guard",
    "codeql": "codeql",
}


class InputError(Exception):
    pass


class GitHubClient:
    def __init__(self, *, token: str | None = None, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubClient":
        return cls(token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))

    def request_json(self, path: str) -> dict[str, Any]:
        url = f"{self.api_url}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise InputError(f"GitHub API request failed: {path}: HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise InputError(f"GitHub API request failed: {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise InputError(f"GitHub API returned invalid JSON: {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise InputError(f"GitHub API response must be a JSON object: {path}")
        return payload


def validate_candidate(candidate: str) -> str:
    if not candidate or not SAFE_CANDIDATE_RE.match(candidate):
        raise InputError(f"candidate must be a safe relative name: {candidate}")
    return candidate


def validate_attempt(attempt: int) -> int:
    if attempt < 1:
        raise InputError(f"attempt must be positive: {attempt}")
    return attempt


def validate_repo(repo: str) -> str:
    if not REPO_RE.match(repo):
        raise InputError(f"repo must be owner/repo: {repo}")
    return repo


def validate_sha(value: str, label: str) -> str:
    if not HEX_SHA_RE.match(value):
        raise InputError(f"{label} must be a 40 character lowercase hex SHA")
    return value


def public_candidate_branch(candidate: str, attempt: int) -> str:
    return f"release-candidate/{validate_candidate(candidate)}-attempt-{validate_attempt(attempt):03d}"


def ref_sha(payload: dict[str, Any], label: str) -> str:
    object_payload = payload.get("object")
    if not isinstance(object_payload, dict):
        raise InputError(f"{label} ref response is missing object")
    sha = object_payload.get("sha")
    if not isinstance(sha, str):
        raise InputError(f"{label} ref response is missing object.sha")
    return validate_sha(sha, f"{label} SHA")


def extract_required_check_conclusions(payload: dict[str, Any]) -> dict[str, str]:
    runs = payload.get("check_runs")
    if not isinstance(runs, list):
        raise InputError("check-runs response is missing check_runs list")

    checks: dict[str, str] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = run.get("name")
        if not isinstance(name, str):
            continue
        check_name = CHECK_NAME_MAP.get(name)
        if not check_name:
            continue
        conclusion = run.get("conclusion")
        status = run.get("status")
        value = conclusion if isinstance(conclusion, str) and conclusion else status
        if not isinstance(value, str) or not value:
            value = "missing"
        if checks.get(check_name) == "success":
            continue
        checks[check_name] = value
    return checks


def build_public_state(
    client: GitHubClient,
    *,
    repo: str,
    candidate: str,
    attempt: int,
    expected_public_candidate_sha: str,
) -> dict[str, Any]:
    repo = validate_repo(repo)
    expected_public_candidate_sha = validate_sha(expected_public_candidate_sha, "expected public candidate SHA")
    branch = public_candidate_branch(candidate, attempt)
    candidate_ref = client.request_json(f"/repos/{repo}/git/ref/heads/{branch}")
    main_ref = client.request_json(f"/repos/{repo}/git/ref/heads/main")
    candidate_sha = ref_sha(candidate_ref, "public candidate")
    main_sha = ref_sha(main_ref, "public main")
    if candidate_sha != expected_public_candidate_sha:
        raise InputError("public candidate head SHA does not match expected SHA")

    check_runs = client.request_json(f"/repos/{repo}/commits/{candidate_sha}/check-runs?per_page=100")
    return {
        "public_candidate_branch": branch,
        "public_candidate_head_sha": candidate_sha,
        "public_main_sha": main_sha,
        "checks": extract_required_check_conclusions(check_runs),
    }


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot write public state: {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read observed public release candidate state from GitHub.")
    parser.add_argument("--repo", required=True, help="Public repository in owner/repo form.")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--expected-public-candidate-sha", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        state = build_public_state(
            GitHubClient.from_env(),
            repo=args.repo,
            candidate=args.candidate,
            attempt=args.attempt,
            expected_public_candidate_sha=args.expected_public_candidate_sha,
        )
        write_json(args.output, state)
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
