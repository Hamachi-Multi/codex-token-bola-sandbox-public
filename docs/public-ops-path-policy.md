# Public Ops Path Policy

`public-ops-path-policy` is an external required check for public operations PRs

## Allowed Paths

Public ops PRs may change only:

- `.github/**`
- `package.json`
- `package-lock.json`
- `.releaserc.json`

## Subject Policy

Public ops PR titles, head commit subjects, and configured squash subject preview must use a non-release subject such as:

```text
chore(public-ops): update public release workflow
```

The policy must fail `feat`, `fix`, `perf`, breaking markers, and `BREAKING CHANGE:` footers

## Main Commit Recheck

After merge to public `main`, `product_snapshot_guard` must recheck the actual public main commit subject on `github.sha`

Public ops commits must not run semantic-release
