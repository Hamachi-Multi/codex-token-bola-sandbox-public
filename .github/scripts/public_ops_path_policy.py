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


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PUBLIC_OPS_SUBJECT_RE = re.compile(r"^chore\(public-ops\): \S.*$")
BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
EXACT_ALLOWED_PATHS = {
    "package.json",
    "package-lock.json",
    ".releaserc.json",
    "docs/public-ops-path-policy.md",
    "docs/public-ruleset-checklist.md",
}
ALLOWED_PATH_PREFIX = ".github/"
PAGE_SIZE = 100


class InputError(Exception):
    pass


class GitHubClient:
    def __init__(self, *, token: str | None = None, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubClient":
        return cls(token=os.environ.get("GITHUB_TOKEN"))

    def request_json(self, path: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.api_url}{path}", headers=headers)
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


def read_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid {label} JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError(f"{label} must be a JSON object")
    return payload


def require_object(payload: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise InputError(f"{label} is missing {key}")
    return value


def require_string(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} is missing {key}")
    return value


def validate_repo(repo: str) -> str:
    if not REPO_RE.match(repo):
        raise InputError(f"repository.full_name must be owner/repo: {repo}")
    return repo


def api_page_path(repo: str, pull_number: int, resource: str, page: int) -> str:
    return f"/repos/{repo}/pulls/{pull_number}/{resource}?per_page={PAGE_SIZE}&page={page}"


def paginated_pull_items(client: Any, *, repo: str, pull_number: int, resource: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = client.request_json(api_page_path(repo, pull_number, resource, page))
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise InputError(f"pull request {resource} response must be a list of objects")
        items.extend(payload)
        if len(payload) < PAGE_SIZE:
            return items
        page += 1


def allowed_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    parts = pathlib.PurePosixPath(path).parts
    if ".." in parts:
        return False
    return path in EXACT_ALLOWED_PATHS or path.startswith(ALLOWED_PATH_PREFIX)


def first_line(message: str) -> str:
    lines = message.splitlines()
    return lines[0].strip() if lines else ""


def validate_public_ops_message(message: str, label: str) -> list[str]:
    errors: list[str] = []
    if not PUBLIC_OPS_SUBJECT_RE.match(first_line(message)):
        errors.append(f"{label} must use chore(public-ops): subject")
    if BREAKING_FOOTER_RE.search(message):
        errors.append(f"{label} must not contain a breaking change footer")
    return errors


def validate_public_ops_pr(client: Any, event: dict[str, Any]) -> dict[str, Any]:
    pull_request = require_object(event, "pull_request", "event")
    repository = require_object(event, "repository", "event")
    base = require_object(pull_request, "base", "pull_request")
    head = require_object(pull_request, "head", "pull_request")

    repo = validate_repo(require_string(repository, "full_name", "repository"))
    pull_number = event.get("number")
    if not isinstance(pull_number, int) or isinstance(pull_number, bool) or pull_number < 1:
        raise InputError("event.number must be a positive integer")

    errors: list[str] = []
    if require_string(base, "ref", "pull_request.base") != "main":
        errors.append("public ops pull request base branch must be main")
    if not require_string(head, "ref", "pull_request.head").startswith("public-ops/"):
        errors.append("public ops pull request head branch must start with public-ops/")

    title = require_string(pull_request, "title", "pull_request")
    errors.extend(validate_public_ops_message(title, "pull request title"))
    body = pull_request.get("body")
    if body is not None and not isinstance(body, str):
        raise InputError("pull_request.body must be a string or null")
    if isinstance(body, str) and BREAKING_FOOTER_RE.search(body):
        errors.append("pull request body must not contain a breaking change footer")

    files = paginated_pull_items(client, repo=repo, pull_number=pull_number, resource="files")
    commits = paginated_pull_items(client, repo=repo, pull_number=pull_number, resource="commits")
    if not files:
        errors.append("public ops pull request must change at least one file")
    if not commits:
        errors.append("public ops pull request must contain at least one commit")

    for index, item in enumerate(files):
        filename = item.get("filename")
        if not isinstance(filename, str):
            raise InputError(f"pull request files[{index}] is missing filename")
        if not allowed_path(filename):
            errors.append(f"public ops path is not allowed: {filename}")
        previous_filename = item.get("previous_filename")
        if previous_filename is not None:
            if not isinstance(previous_filename, str):
                raise InputError(f"pull request files[{index}].previous_filename must be a string")
            if not allowed_path(previous_filename):
                errors.append(f"public ops previous path is not allowed: {previous_filename}")

    for index, item in enumerate(commits):
        commit = item.get("commit")
        if not isinstance(commit, dict):
            raise InputError(f"pull request commits[{index}] is missing commit")
        message = commit.get("message")
        if not isinstance(message, str) or not message:
            raise InputError(f"pull request commits[{index}].commit is missing message")
        errors.extend(validate_public_ops_message(message, f"commit {index + 1}"))

    return {
        "ok": not errors,
        "errors": errors,
        "checked_files": len(files),
        "checked_commits": len(commits),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a public operations pull request policy.")
    parser.add_argument("--event-file", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_public_ops_pr(GitHubClient.from_env(), read_json(args.event_file, "GitHub event"))
    except InputError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, separators=(",", ":")))
        return 2

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
