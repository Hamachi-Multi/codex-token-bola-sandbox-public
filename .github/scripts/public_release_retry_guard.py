#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
RELEASE_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
RECOVERY_KINDS = {"semantic_release_retry", "orphan_release_repair"}


class InputError(Exception):
    pass


class GitHubNotFound(Exception):
    pass


class GitHubClient:
    def __init__(self, *, token: str | None = None, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubClient":
        return cls(token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))

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
            if exc.code == 404:
                raise GitHubNotFound(f"GitHub API resource not found: {path}") from exc
            raise InputError(f"GitHub API request failed: {path}: HTTP {exc.code}: {detail}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise InputError(f"GitHub API request failed: {path}: {exc}") from exc


def validate_sha(value: str, label: str) -> str:
    if not HEX_SHA_RE.fullmatch(value):
        raise InputError(f"{label} must be a 40 character lowercase hex SHA")
    return value


def run_git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise InputError(f"git {' '.join(args)} failed: {detail}")
    return result


def read_allowed_paths(path: pathlib.Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read public ops path policy: {path}: {exc}") from exc
    if not isinstance(payload, list) or any(not isinstance(item, str) or not item for item in payload):
        raise InputError("public ops path policy must be a list of non-empty paths")
    if payload != sorted(set(payload)):
        raise InputError("public ops path policy must be sorted and unique")
    return set(payload)


def ref_sha(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("object"), dict):
        raise InputError("public main ref response is invalid")
    return validate_sha(str(payload["object"].get("sha") or ""), "public main SHA")


def tag_target_sha(client: Any, *, repo_name: str, tag: str) -> str:
    encoded = urllib.parse.quote(tag, safe="")
    payload = client.request_json(f"/repos/{repo_name}/git/ref/tags/{encoded}")
    target = ref_sha(payload)
    object_payload = payload.get("object") if isinstance(payload, dict) else None
    if not isinstance(object_payload, dict) or object_payload.get("type") != "tag":
        return target
    annotated = client.request_json(f"/repos/{repo_name}/git/tags/{target}")
    nested = annotated.get("object") if isinstance(annotated, dict) else None
    if not isinstance(nested, dict) or nested.get("type") != "commit":
        raise InputError(f"annotated tag {tag} must point directly to a commit")
    return validate_sha(str(nested.get("sha") or ""), f"tag {tag} target SHA")


def release_exists(client: Any, *, repo_name: str, tag: str) -> bool:
    encoded = urllib.parse.quote(tag, safe="")
    try:
        payload = client.request_json(f"/repos/{repo_name}/releases/tags/{encoded}")
    except GitHubNotFound:
        return False
    if not isinstance(payload, dict):
        raise InputError("GitHub Release response must be a JSON object")
    return True


def exact_successful_push(client: Any, *, repo: str, workflow: str, expected_sha: str) -> int:
    query = urllib.parse.urlencode(
        {"branch": "main", "event": "push", "head_sha": expected_sha, "per_page": 100}
    )
    workflow_name = urllib.parse.quote(workflow, safe="")
    payload = client.request_json(f"/repos/{repo}/actions/workflows/{workflow_name}/runs?{query}")
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise InputError(f"{workflow} runs response is invalid")
    matches = [
        run
        for run in payload["workflow_runs"]
        if isinstance(run, dict)
        and run.get("head_branch") == "main"
        and run.get("head_sha") == expected_sha
        and run.get("event") == "push"
    ]
    if len(matches) != 1:
        raise InputError(f"expected exactly one {workflow} main push run for release SHA")
    run = matches[0]
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise InputError(f"{workflow} main push run is not successful")
    run_id = run.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise InputError(f"{workflow} run ID is invalid")
    return run_id


def validate_retry(
    client: Any,
    *,
    repo_name: str,
    repo_root: pathlib.Path,
    policy_path: pathlib.Path,
    actor: str,
    expected_actor: str,
    product_sha: str,
    release_sha: str,
    recovery_kind: str = "semantic_release_retry",
    expected_tag: str = "",
    release_date: str = "",
) -> dict[str, Any]:
    if not REPO_RE.fullmatch(repo_name):
        raise InputError(f"repo must be owner/repo: {repo_name}")
    product_sha = validate_sha(product_sha, "product SHA")
    release_sha = validate_sha(release_sha, "release SHA")
    errors: list[str] = []
    ops_commits: list[str] = []
    if actor != expected_actor:
        errors.append("release retry actor must match promotion App actor")
    if recovery_kind not in RECOVERY_KINDS:
        errors.append(f"unsupported release recovery kind: {recovery_kind}")
    elif recovery_kind == "orphan_release_repair":
        if not RELEASE_TAG_RE.fullmatch(expected_tag):
            errors.append("orphan release repair requires a vX.Y.Z expected tag")
        if not RELEASE_DATE_RE.fullmatch(release_date):
            errors.append("orphan release repair requires a YYYY-MM-DD release date")
    elif expected_tag or release_date:
        errors.append("semantic release retry must not include orphan release metadata")
    head = run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
    if head != release_sha:
        errors.append("checked out HEAD does not match expected release SHA")
    try:
        if ref_sha(client.request_json(f"/repos/{repo_name}/git/ref/heads/main")) != release_sha:
            errors.append("public main moved from expected release SHA")
        ancestry = run_git(repo_root, "merge-base", "--is-ancestor", product_sha, release_sha, check=False)
        if ancestry.returncode != 0:
            errors.append("product SHA is not an ancestor of release SHA")
        else:
            allowed = read_allowed_paths(policy_path)
            ops_commits = run_git(
                repo_root, "rev-list", "--reverse", f"{product_sha}..{release_sha}"
            ).stdout.splitlines()
            for commit in ops_commits:
                parents = run_git(repo_root, "show", "-s", "--format=%P", commit).stdout.strip().split()
                if len(parents) != 1:
                    errors.append(f"release retry descendant must have one parent: {commit}")
                    continue
                subject = run_git(repo_root, "show", "-s", "--format=%s", commit).stdout.strip()
                if not subject.startswith("chore(public-ops): "):
                    errors.append(f"release retry descendant is not public ops: {commit}")
                    continue
                changed = run_git(
                    repo_root, "diff-tree", "--no-commit-id", "--name-only", "-r", parents[0], commit
                ).stdout.splitlines()
                unmanaged = sorted(path for path in changed if path and path not in allowed)
                if unmanaged:
                    errors.append(f"public ops commit changed unmanaged paths: {commit}: {', '.join(unmanaged)}")
        verified_runs: dict[str, dict[str, int]] = {}
        for commit in ops_commits or [release_sha]:
            verified_runs[commit] = {
                "public_ci": exact_successful_push(
                    client, repo=repo_name, workflow="release.yml", expected_sha=commit
                ),
                "codeql": exact_successful_push(
                    client, repo=repo_name, workflow="codeql.yml", expected_sha=commit
                ),
            }
        public_ci_run_id = verified_runs[release_sha]["public_ci"]
        codeql_run_id = verified_runs[release_sha]["codeql"]
        if recovery_kind == "orphan_release_repair" and RELEASE_TAG_RE.fullmatch(expected_tag):
            if tag_target_sha(client, repo_name=repo_name, tag=expected_tag) != product_sha:
                errors.append("orphan release tag does not point to product SHA")
            if release_exists(client, repo_name=repo_name, tag=expected_tag):
                errors.append("GitHub Release already exists for orphan release tag")
    except (InputError, GitHubNotFound) as exc:
        errors.append(str(exc))
        public_ci_run_id = None
        codeql_run_id = None
    return {
        "ok": not errors,
        "errors": errors,
        "product_sha": product_sha,
        "release_sha": release_sha,
        "recovery_kind": recovery_kind,
        "expected_tag": expected_tag,
        "release_date": release_date,
        "public_ci_run_id": public_ci_run_id,
        "codeql_run_id": codeql_run_id,
        "verified_public_ops_commits": ops_commits,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guard a semantic-release retry on public main.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--repo-root", default=pathlib.Path("."), type=pathlib.Path)
    parser.add_argument("--policy", required=True, type=pathlib.Path)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--expected-actor", required=True)
    parser.add_argument("--product-sha", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--recovery-kind", required=True, choices=sorted(RECOVERY_KINDS))
    parser.add_argument("--expected-tag", default="")
    parser.add_argument("--release-date", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_retry(
            GitHubClient.from_env(),
            repo_name=args.repo,
            repo_root=args.repo_root,
            policy_path=args.policy,
            actor=args.actor,
            expected_actor=args.expected_actor,
            product_sha=args.product_sha,
            release_sha=args.release_sha,
            recovery_kind=args.recovery_kind,
            expected_tag=args.expected_tag,
            release_date=args.release_date,
        )
    except InputError as exc:
        result = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
