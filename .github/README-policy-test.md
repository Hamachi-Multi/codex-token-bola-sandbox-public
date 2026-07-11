# Codex Token Bola

Codex Token Bola captures Codex turn-level token usage, normalizes raw hook logs,
builds a SQLite analytics database, and serves a dashboard for optimization work.

## Data Model

- `turns`: one user prompt / Codex turn, with token totals, cache ratio, prompt metadata, category, and workflow.
- `model_call_summaries`: per-turn model call counts, token totals, maxima, and weighted cost units.
- `tool_call_summaries` and `tool_call_samples`: tool output size, failure counts, samples, and timing. `issued_by_model_call_index` and `consumed_by_model_call_index` describe the step interval where tool output moved into the next model input.
- `task_rollups`: parent turn to subagent usage attribution.

## Capture Defaults

The hook preserves a bounded copy of the user's submitted prompt for new turn logs while
keeping tool output previews off by default:

- user prompt preview text: enabled, first 800 characters by default
- instruction excerpt text: enabled, first 600 non-code-block characters by default
- tool output preview text in analytics DB: disabled
- log and analytics files are written with owner-only mode where Codex Token Bola writes the file

Codex Token Bola does not provide secret detection, masking, or scrub/export
features. Treat generated service artifacts as local private data.

Disable user prompt text capture when working with sensitive prompts:

```bash
CODEX_TOKEN_USAGE_STORE_TEXT=0
```

Limit stored user prompt text or tool output previews when needed:

```bash
CODEX_TOKEN_USAGE_PROMPT_PREVIEW_CHARS=800
CODEX_TOKEN_USAGE_INSTRUCTION_EXCERPT_CHARS=600
CODEX_TOKEN_USAGE_TOOL_OUTPUT_PREVIEW_CHARS=500
```

Tune hook path bounds when needed:

```bash
CODEX_TOKEN_USAGE_HOOK_TAIL_SCAN_BYTES=1048576
CODEX_TOKEN_USAGE_HOOK_FORWARD_SCAN_BYTES=16777216
CODEX_TOKEN_USAGE_HOOK_APPEND_LOCK_TIMEOUT_MS=500
```

## CLI

Install the hook into a Codex home:

```bash
mkdir -p ~/.codex/hooks
cp hooks/token-usage.py ~/.codex/hooks/token-usage.py
```

Run diagnostics:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py doctor
```

Run the full offline pipeline:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py pipeline
```

Run the default incremental analysis path:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py pipeline --incremental
```

The default incremental path does not recover pending hook states. Run recovery
explicitly when you want to scan saved pending states before analysis:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py pipeline --incremental --recover
```

## Existing User Path Migration

Codex Token Bola uses `~/.codex/codex-token-bola` as its service directory.
Older installs may have service data under `~/.codex/token-usage`.

Preview the one-shot migration:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py migrate-path
```

Move the legacy service directory:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py migrate-path --apply
```

The runtime does not dual-read or dual-write the legacy directory. If both
directories exist, migration stops and you must back up or remove one path
before trying again.

## Raw Log Rotation and Retention

Analyze closes the current raw segment by pointer handoff before normalize/build.
New hook writes go to the next current segment selected by
`state/current-raw-segments.json`:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py pipeline --incremental
```

Prune dashboard-visible data older than a cutoff and rebuild derived outputs:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py retention-prune --cutoff 2026-05-20T00:00:00+00:00 --preview-signature <signature-from-log-cleanup-preview>
```

Retention source pruning only mutates service-owned raw files under
`~/.codex/codex-token-bola`; the command also removes and rebuilds derived normalized
state and analytics outputs. `--preview-signature` must match the current
`/api/log-cleanup` retention preview, and `--output` must stay under
`~/.codex/codex-token-bola/analytics/`. It does not delete Codex CLI transcripts or
internal CLI logs.

Manually rotate current raw segments with the same pointer handoff used by
Analyze:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py compact
```

Build analytics only:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py build
```

Use custom project roots when your repositories are not under `~/src`:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py build --project-root ~/work
```

Serve the dashboard:

```bash
python3 ~/.codex/codex-token-bola/scripts/codex_token_usage.py serve --host 127.0.0.1 --port 8766
```

Install browser verification dependencies:

```bash
python3 -m pip install "$HOME/.codex/codex-token-bola[ui]"
python3 -m playwright install chromium
```

## Dashboard Semantics

The top-right analysis scope selects the highest-cost turns within the active
time range and optional session filter. Expensive turns are paginated in fixed
25-row pages.
Weighted cost units are non-cached-input-equivalent tokens:

```text
non_cached_input_tokens * 1.0
+ cached_input_tokens * 0.1
+ output_tokens * 6.0
```

The default weights mirror GPT-5.5 token price ratios while keeping the result
in token-sized units instead of dollars or per-million-token pricing units.

Dashboard route payloads are documented in
`docs/dashboard-api-contract.md`. Treat fields not listed there as internal
implementation details.

Tool timing uses step intervals:

```text
2 -> 3
```

This means model step 2 requested the tool call, the tool ran, and the output
was available to step 3 as input context.

Subagent attribution confidence values:

- `spawn_call_turn_context`: direct parent transcript `spawn_agent` turn context was found.
- `child_task_time_overlap`: child start time overlaps a parent turn range.
- `spawn_edge_nearest_parent_turn`: fallback to nearest earlier parent turn.

## Verification

```bash
make compile && make test
make ui-check
```

For a running local dashboard, restart the server first, then run `make ui-check-live` against the live instance.
