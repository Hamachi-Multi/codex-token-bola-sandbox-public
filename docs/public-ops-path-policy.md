# Public Ops Path Policy

`public-ops-path-policy` is a GitHub Actions required check for public operations PRs

The check runs from `.github/workflows/public-ops-policy.yml` on `pull_request` events targeting `main`

Only `public-ops/*` head branches are eligible. The workflow checks out the pull request base SHA and evaluates pull request metadata through the GitHub API without executing pull request code

## Allowed Paths

Public ops PRs may change only:

- `.github/**`
- `docs/public-ops-path-policy.md`
- `docs/public-ruleset-checklist.md`
- `package.json`
- `package-lock.json`
- `.releaserc.json`

## Subject Policy

Public ops PR titles, head commit subjects, and configured squash subject preview must use a non-release subject such as:

```text
chore(public-ops): update public release workflow
```

The policy must fail `feat`, `fix`, `perf`, breaking markers, and `BREAKING CHANGE:` footers

The pull request title and every commit subject must use `chore(public-ops): ...`

Checking both inputs covers the repository squash title modes without trusting a release-type preview

The workflow validates both current and previous filenames so a rename cannot remove a product path through an allowed destination

## Main Commit Recheck

After merge to public `main`, `product_snapshot_guard` must recheck the actual public main commit subject on `github.sha`

Public ops commits must not run semantic-release

## Trust Boundary

The public operations branch ruleset must limit `public-ops/*` updates to the trusted public ops actor

This repository does not use a separate GitHub App, webhook service, or check server for this policy
