# Public Ops Path Policy

Public operations use two private `workflow_dispatch` decisions and a public candidate branch

The private Stage workflow creates one exact commit on `public-ops/*`. GitHub Actions runs `public-ops-candidate-guard`, the normal compile and asset checks, the sensitive-path guard, and CodeQL on that commit

The private Apply workflow accepts only the recorded candidate SHA after all candidate checks succeed. It fast-forwards public `main` to that exact SHA and verifies the resulting main checks before closing the durable ops record

## Allowed Paths

The trusted private manifest permits only:

- `.github/dependabot.yml`
- `.github/release-dependency-audit-allowlist.json`
- `.github/scripts/public_candidate_snapshot_guard.py`
- `.github/scripts/public_main_release_guard.py`
- `.github/scripts/public_snapshot_commit_policy.py`
- `.github/scripts/release_dependency_audit.py`
- `.github/workflows/codeql.yml`
- `.github/workflows/release.yml`
- `docs/public-ops-path-policy.md`
- `docs/public-ruleset-checklist.md`
- `package.json`
- `package-lock.json`
- `.releaserc.json`

The release dependency audit exception is fail-closed and expires on 2026-09-22. It permits only the listed GHSA identifiers at exact nested `npm` package paths while `@semantic-release/npm` remains disabled in `.releaserc.json`. New advisories, changed paths, severity changes, unused exceptions, enabled npm publishing, and expired exceptions fail `release-dependency-audit`

The candidate guard rejects every other changed path. Before any public write token is minted, the private Stage scans every managed source file and the copied candidate for credential literals, token shapes, private keys, operator paths, and transcript artifacts. It also verifies that each managed file exactly matches `release/public-bootstrap`

The two public policy files that contain the forbidden regular expressions themselves are exempt only while their private manifest SHA-256 values match exactly. Any edit invalidates the exemption until its reviewed digest is updated

The retired `.github/scripts/public_ops_path_policy.py`, `.github/workflows/public-ops-policy.yml`, `scripts/public_main_release_guard.py`, and `scripts/public_snapshot_commit_policy.py` paths are accepted only as migration deletions and must be absent from the candidate tree

## Subject Policy

The candidate must contain one commit on the recorded public base and its subject must use:

```text
chore(public-ops): update public release workflow
```

Public ops commits never run semantic-release

The private Stage validates the subject as a single line and applies the same sensitive-content rules before creating the commit

## Trust Boundary

The Ops Stage GitHub App may create, update, and delete only `public-ops/*` branches

The Ops Apply GitHub App may update only public `main`

The candidate SHA, public base SHA, private source SHA, policy digest, tree digest, and state transitions are retained under the private `release/ops-records/**` namespace

The current record keeps the latest valid apply and verification observations with the workflow run ID, run attempt, original actor, triggering actor, outcome, and observation time. Earlier observations remain available in the release-record branch Git history. Runs that do not change or observe public state are not added to the durable record
