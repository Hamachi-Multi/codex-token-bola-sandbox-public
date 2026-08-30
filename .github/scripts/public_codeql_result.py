#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CODEQL_WORKFLOW = "codeql.yml"
PUBLIC_MAIN_BRANCH = "main"
DEFAULT_MAX_ATTEMPTS = 60
DEFAULT_POLL_INTERVAL_SECONDS = 30


class InputError(Exception):
    pass


class GitHubClient:
    def __init__(self, *, token: str | None = None, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubClient":
        return cls(token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))

    def request_json(self, path: str) -> Any:
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
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise InputError(f"GitHub API request failed: {path}: HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise InputError(f"GitHub API request failed: {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise InputError(f"GitHub API returned invalid JSON: {path}: {exc}") from exc


def validate_repo(value: str) -> str:
    if not REPO_RE.fullmatch(value):
        raise InputError(f"repo must be owner/repo: {value}")
    return value


def validate_sha(value: str) -> str:
    if not HEX_SHA_RE.fullmatch(value):
        raise InputError("expected SHA must be a 40 character lowercase hex SHA")
    return value


def validate_run_id(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InputError("CodeQL workflow run ID must be a positive integer")
    return value


def workflow_runs_path(*, repo: str, expected_sha: str) -> str:
    query = urllib.parse.urlencode(
        {
            "branch": PUBLIC_MAIN_BRANCH,
            "event": "push",
            "head_sha": expected_sha,
            "per_page": 100,
        }
    )
    workflow = urllib.parse.quote(CODEQL_WORKFLOW, safe="")
    return f"/repos/{repo}/actions/workflows/{workflow}/runs?{query}"


def matching_public_main_run(payload: Any, *, expected_sha: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise InputError("CodeQL workflow runs response is missing workflow_runs list")

    matches = [
        run
        for run in payload["workflow_runs"]
        if isinstance(run, dict)
        and run.get("head_branch") == PUBLIC_MAIN_BRANCH
        and run.get("head_sha") == expected_sha
        and run.get("event") == "push"
    ]
    if not matches:
        return None
    run_ids = {validate_run_id(run.get("id")) for run in matches}
    if len(run_ids) != 1:
        raise InputError("multiple CodeQL workflow runs match public main and expected SHA")
    return matches[0]


def terminal_conclusion(run: dict[str, Any]) -> str | None:
    status = run.get("status")
    if not isinstance(status, str) or not status:
        raise InputError("CodeQL workflow run is missing status")
    if status != "completed":
        return None
    conclusion = run.get("conclusion")
    if not isinstance(conclusion, str) or not conclusion:
        raise InputError("completed CodeQL workflow run is missing conclusion")
    return conclusion


def resolve_codeql_result(
    client: Any,
    *,
    repo: str,
    expected_sha: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    repo = validate_repo(repo)
    expected_sha = validate_sha(expected_sha)
    if max_attempts < 1:
        raise InputError("max attempts must be positive")
    if poll_interval_seconds < 0:
        raise InputError("poll interval seconds must not be negative")

    matched_run_id: int | None = None
    path = workflow_runs_path(repo=repo, expected_sha=expected_sha)
    for attempt in range(max_attempts):
        run = matching_public_main_run(client.request_json(path), expected_sha=expected_sha)
        if run is not None:
            matched_run_id = validate_run_id(run.get("id"))
            conclusion = terminal_conclusion(run)
            if conclusion is not None:
                return {
                    "status": "completed",
                    "conclusion": conclusion,
                    "workflow_run_id": matched_run_id,
                }
        if attempt + 1 < max_attempts:
            sleep(poll_interval_seconds)

    result: dict[str, Any] = {"status": "timed_out", "conclusion": "missing"}
    if matched_run_id is not None:
        result["workflow_run_id"] = matched_run_id
    return result


def write_github_output(path: pathlib.Path, result: dict[str, Any]) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"conclusion={result['conclusion']}\n")
    except OSError as exc:
        raise InputError(f"cannot write github output file: {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve the exact public main CodeQL workflow conclusion.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--github-output", required=True, type=pathlib.Path)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = resolve_codeql_result(
            client or GitHubClient.from_env(),
            repo=args.repo,
            expected_sha=args.expected_sha,
            sleep=sleep,
        )
        write_github_output(args.github_output, result)
    except InputError as exc:
        print(json.dumps({"status": "error", "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
