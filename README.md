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
BOLA_STORE_TEXT=0
```

Limit stored user prompt text or tool output previews when needed:

```bash
BOLA_PROMPT_PREVIEW_CHARS=800
BOLA_INSTRUCTION_EXCERPT_CHARS=600
BOLA_TOOL_OUTPUT_PREVIEW_CHARS=500
```

Tune hook path bounds when needed:

```bash
BOLA_HOOK_TAIL_SCAN_BYTES=1048576
BOLA_HOOK_FORWARD_SCAN_BYTES=16777216
BOLA_HOOK_APPEND_LOCK_TIMEOUT_MS=500
```

## CLI

From the root of a cloned checkout, create and activate a repository-local
virtual environment, then install the package into it:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Keep this virtual environment active for the remaining commands in this
section. The `.venv` directory is local to the checkout and is not committed.

Confirm the installed package version:

```bash
bola --version
```

Release builds derive this version from the Git tag. An exact `vX.Y.Z` tag
produces package version `X.Y.Z`; ordinary commits after a tag produce a
development version.

```bash
bola install-hook --codex-dir "${CODEX_HOME:-$HOME/.codex}"
```

`install-hook` requires an existing, initialized, writable Codex directory and a
working `codex --version` command. It does not create a missing Codex directory. A
custom directory must contain Codex-created configuration, authentication,
state, history, or session data. Initialize a new custom directory by running Codex with
the same environment before installing the hook:

```bash
CODEX_HOME=~/private/codex-dir codex
bola install-hook --codex-dir ~/private/codex-dir
```

An `--output-dir` does not need to exist during installation. Codex Token Bola
creates it on the first hook or pipeline write.

The hook is not copied into `~/.codex`. `install-hook` records the exact Python
interpreter used to run `install-hook` and invokes `codex_token_bola.hook` as a
module. Rerun `install-hook` after replacing `.venv`, moving the checkout, or
otherwise moving that Python environment.

Verify the configured paths and Codex event registration:

```bash
bola doctor --codex-dir "${CODEX_HOME:-$HOME/.codex}"
```

`doctor` reports a top-level `health` status in addition to the Codex directory,
CLI, hook, recovery, and analytics checks. Exit status `0` means healthy, `1`
means degraded by unresolved runtime signals, and `2` means failed or recovery
is required.

Malformed raw rows and recovery state are quarantined instead of being counted
as a clean success. Normalization and analysis still apply valid rows, then
exit with status `1` and a JSON `status` of `degraded`. Inspect unresolved
quarantine events without exposing their stored payloads:

```bash
bola quarantine list
```

The list command exits `1` while unresolved events remain and `0` after all
current events are acknowledged.

Acknowledgement records that the exclusion was reviewed; it does not delete or
repair the evidence under `bad/`:

```bash
bola quarantine acknowledge --event-id <EVENT_ID>
bola quarantine acknowledge --all
```

Acknowledged repeats of the same source/content/error signature are reported as
informational occurrences. A changed source, payload, or error signature creates
a new unresolved event and degrades `doctor` again.

Run the full offline pipeline:

```bash
bola pipeline
```

Run the default incremental analysis path:

```bash
bola pipeline --incremental
```

The default incremental path does not recover pending hook states. Run recovery
explicitly when you want to scan saved pending states before analysis:

```bash
bola pipeline --incremental --recover
```

## Runtime Paths

`codex_dir` is read-only input for Codex state and hook registration. It
defaults to `~/.codex`. The output directory owns every file generated by
Codex Token Bola, including raw logs, normalized data, analytics, state,
temporary files, and reports. By default it uses the operating system's user
data directory: `${XDG_DATA_HOME:-~/.local/share}/bola` on WSL2 Ubuntu and
native Linux.

Path precedence is CLI option, environment variable, persistent config, then
the default. The supported environment variables are `CODEX_HOME` and
`BOLA_OUTPUT_DIR`. Persistent configuration is stored at
`${XDG_CONFIG_HOME:-~/.config}/bola/config.json` on WSL2 Ubuntu and native
Linux, only after an explicit configuration or install command writes it.

Names from the pre-BOLA runtime are intentionally not accepted. Legacy
`CODEX_TOKEN_USAGE_*` variables fail with exit code `2` and report their
`BOLA_*` replacements. A legacy `codex-token-bola/config.json` also fails
closed; BOLA does not move it or neighboring files automatically.

Show effective paths:

```bash
bola paths show
```

Store custom paths. An output-directory change immediately hands off in-flight
hook recovery state, then new hook and dashboard work uses the new directory:

```bash
bola paths set --codex-dir ~/.codex --output-dir ~/private/codex-token-data
```

Changing `--codex-dir` validates the initialized Codex directory and CLI,
registers the hook in the new directory, and removes only this application's
registration from the previous directory. It never moves Codex sessions or
configuration data.

Commands that perform work also accept `--codex-dir` and `--output-dir` for
one invocation. `install-hook` persists path options supplied to that command.

## Data Migration

Changing the output directory does not move completed historical files. The
application keeps running from the new output directory while a pending
migration is shown by `paths show`. Preview merging the previous output into
the active output directory:

```bash
bola paths migrate --output-dir
```

Apply the reviewed migration:

```bash
bola paths migrate --output-dir --apply
```

The migration imports old raw evidence as verified closed segments, preserves
diagnostic evidence under `reports/migrations/`, and rebuilds normalized data
and analytics from the combined raw inputs. Only after verification does it
remove the previous service-owned files. Source code, `.git`, unrelated files,
and Codex CLI data are never moved or removed. Data from older application
names is ignored and is never migrated or deleted automatically.

Before scanning source logs, migration completes any physical deletion already
committed by retention. If a source file still cannot be removed, migration
returns exit code 2 with `source_physical_delete_pending`, preserves the pending
transition, and imports nothing. Fix the reported filesystem or permission
error and repeat the same `paths migrate` command.

Migration also merges retention-pruned parent-turn attribution by
`session_id` and `turn_id` before rebuilding analytics. Identical rows are
deduplicated. Conflicting rows return exit code 2 with
`retention_pruned_turn_conflict` before any import begins, preserving both
directories for inspection.

Only one output transition can be pending. Repeating the active path is a
no-op, returning to the immediately previous path reverses the pending
migration, and selecting a third path is rejected until migration completes.

## Raw Log Rotation and Retention

Analyze closes the current raw segment by pointer handoff before normalize/build.
New hook writes go to the next current segment selected by
`state/current-raw-segments.json`:

```bash
bola pipeline --incremental
```

Preview dashboard-visible data older than a cutoff without writing service data:

```bash
bola retention-preview --cutoff 2026-05-20T00:00:00+00:00
```

Review the returned counts, then pass its `preview_signature` to prune and
rebuild derived outputs:

```bash
bola retention-prune --cutoff 2026-05-20T00:00:00+00:00 --preview-signature <signature-from-retention-preview>
```

Dashboard retention uses the browser's IANA time zone, such as `Asia/Seoul`,
and calendar-day boundaries instead of rolling 24-hour windows. `Keep Recent 1
Day` keeps the browser's current local date and deletes through the previous
local date. A directly selected `Delete Through` date is also included in the
deletion range. Internally, the server converts the following local midnight
to an exclusive UTC Unix cutoff; stored log timestamps and raw segment indexes
remain UTC.

Date-based retention never deletes pending turn state because an old start can
still belong to a running Codex turn. `bola doctor` reports stale recovery
state and `bola reconcile` recovers turns with terminal transcript evidence.
The explicit `All Logs` cleanup remains the only dashboard action that removes
pending turn state.

Python's `zoneinfo` reads the operating-system time-zone database when one is
available. The project also declares `tzdata==2026.3` for minimal environments
that do not provide an IANA database. WSL2 Ubuntu is the verified runtime
target. Native Linux is expected to be compatible but is not yet verified.
macOS and native Windows are outside the supported runtime scope.

`retention-preview` never creates or refreshes the retention index. A preview
can become stale as hooks append data, so `retention-prune` always revalidates
the explicit signature and fails closed instead of previewing automatically.

Retention source pruning only mutates service-owned raw files under the active
output directory; the command also removes and rebuilds derived normalized
state and analytics outputs. `retention-prune --cutoff` remains a low-level
exclusive instant. Its `--preview-signature` must match the current
`/api/log-cleanup` retention preview, and `--output` must stay under
`<output-dir>/analytics/`. It does not delete Codex CLI transcripts or internal
CLI logs.

Manually rotate current raw segments with the same pointer handoff used by
Analyze:

```bash
bola compact
```

Build analytics only:

```bash
bola build
```

Use custom project roots when your repositories are not under `~/src`:

```bash
bola build --project-root ~/work
```

Serve the dashboard:

```bash
bola serve --host 127.0.0.1 --port 8766
```

Only one dashboard server may own an output directory. A second server for the
same output directory exits with status `2`; servers using different output
directories may run concurrently. During shutdown, the server stops accepting
new operations and terminates every Dashboard-owned command group before it
exits. Linux parent-death supervision also cleans up those groups if the server
is killed abruptly.

The dashboard accepts only `localhost` or IPv4 loopback bind addresses and
rejects IPv6 or network-facing addresses before bind. Every request must target
the configured local host and port, and mutation requests must be same-origin
JSON requests. To use a dashboard running on another machine, keep it bound to
`127.0.0.1` there and open an SSH tunnel:

```bash
ssh -N -L 8766:127.0.0.1:8766 user@remote-host
```

Then open `http://127.0.0.1:8766` locally. This forwards the local port through
SSH without exposing the dashboard as a network service.

Install browser verification dependencies:

```bash
python -m pip install '.[ui]'
python -m playwright install chromium
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

Dashboard responsive layout and component sizing guidelines are documented in
`docs/dashboard-responsive-layout.md`.

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

With the repository virtual environment active, build the distribution wheel
with isolated build dependencies:

```bash
python -m pip install '.[dev]'
python -m build --wheel
```

For a running local dashboard, restart the server first, then run `make ui-check-live` against the live instance.

UI checks run in isolated browser contexts. To reproduce one scenario or stress it repeatedly:

```bash
python scripts/playwright_dashboard_check.py --scenario desktop-tools-subagents
python scripts/playwright_dashboard_check.py --scenario desktop-tools-subagents --repeat 10
```
