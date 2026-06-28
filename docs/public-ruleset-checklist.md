# Public Ruleset Checklist

Use this checklist when applying `release/public-bootstrap` to the sandbox or production public repository

## Required Rulesets

- `public-branch-catch-all`: block create, update, and delete for unexpected `refs/heads/*` branch patterns
- `public-main-snapshot-promotion`: allow product snapshot updates to `refs/heads/main` only from the promotion GitHub App
- `public-main-ops-update`: allow public-only operations updates only through public ops PRs, required checks, and the public ops actor
- `public-release-candidate-branches`: allow `release-candidate/*` create, update, and delete only from the snapshot GitHub App
- `public-ops-branches`: allow `public-ops/*` branch create and update only from the public ops actor
- `public-release-tags`: allow `refs/tags/v*` creation only from the release-tag GitHub App

## Security Controls

- Enable secret scanning push protection before the first production candidate push
- Do not grant bypass for snapshot, promotion, or release-tag GitHub Apps
- Add optional never-public path deny ruleset only if the public repo type and plan support repository push rulesets
- Keep tag update and deletion blocked in production except through approved orphan tag recovery

## Baseline Tag

- Create initial baseline tag `v0.1.0` on the public repo bootstrap commit before tag ruleset lock-down
- If tag ruleset is already enabled, create the baseline tag through the release-tag GitHub App path
