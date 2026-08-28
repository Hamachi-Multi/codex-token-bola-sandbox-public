# Dashboard API Contract

This document records the stable Codex Token Bola dashboard payload fields that
the browser UI and tests may rely on. Generated service data remains local
private data; the dashboard API is not a public export surface.

## Request Security Contract

The dashboard server accepts only `localhost` or IPv4 loopback bind addresses.
It refuses IPv6 and network-facing addresses and does not provide a
network-exposure override or CORS access.

Every `GET` and `POST` request must contain exactly one `Host` header matching
the host and port configured when the server started. Every `POST` request must
also satisfy all of these checks before its body is parsed or any mutation is
started:

- exactly one `Origin` matching `http://<configured-host>:<configured-port>`
- `Sec-Fetch-Site: same-origin` when that header is present
- exactly one `Content-Type` whose media type is `application/json`; parameters
  such as `charset=UTF-8` are allowed
- exactly one valid `Content-Length`, with a body no larger than 64 KiB
- a valid JSON object body; bodyless actions send `{}`

Requests that fail these checks use these error responses:

- invalid `Host`: HTTP 403 with `request_host_forbidden`
- missing or invalid `Origin`: HTTP 403 with `request_origin_forbidden`
- non-`same-origin` `Sec-Fetch-Site`: HTTP 403 with
  `cross_site_request_forbidden`
- non-JSON content type: HTTP 415 with `application_json_required`
- missing content length: HTTP 411 with `content_length_required`
- invalid content length: HTTP 400 with `invalid_content_length`
- body larger than 64 KiB: HTTP 413 with `request_body_too_large`
- malformed JSON or a non-object JSON value: HTTP 400 with `invalid_json`

Remote use requires transport such as SSH local port forwarding while the
server remains bound to loopback.

## `POST /api/rebuild`

The request body requires a canonical UUID string in `operation_id`. The
browser creates it with `crypto.randomUUID()` before starting the request. The
same ID owns progress and cancellation for the lifetime of that Analyze job.
Missing or malformed IDs return HTTP 400 with `invalid_operation_id`.

Analyze may complete after quarantining malformed input. This is a completed
degraded operation, not an HTTP failure. The response uses HTTP 200 and includes:

- `ok: true`
- `status: "degraded"`
- `data_health: "degraded"`
- `quarantine.unacknowledged_events`
- valid-row analytics metadata from the completed build

The CLI pipeline exits with status `1` for this response class. The dashboard
server recognizes it only when the final JSON explicitly reports
`status: "degraded"`; other nonzero exits retain the existing failure response
and HTTP status. A clean Analyze response uses `status: "healthy"` and
`data_health: "ok"`.

## Analyze progress and cancellation

`GET /api/rebuild/progress` and Analyze busy responses include the active
`operation_id`. The browser ignores progress belonging to another ID. A busy
Analyze may be observed, but an observer does not own its cancellation.

`POST /api/rebuild/cancel` requires the same `operation_id` used to start the
active Analyze. No active Analyze returns HTTP 409 with
`analysis_not_running`; a different ID returns HTTP 409 with
`operation_id_mismatch`. Cancellation requested before the worker process is
attached remains pending and is delivered after attachment.

When server shutdown has started, new Analyze and Cleanup mutations return
HTTP 503 with `server_shutting_down`.

## `/api/dashboard`

Stable top-level fields:

- `summary`: aggregate token, model-call, tool-call, and cost-unit totals.
- `turns`: paginated turn rows for the selected dashboard scope.
- `sessions`, `projects`, `tools`, `subagents`: dashboard list payloads.
- `cleanup`: cleanup summary data when requested by the dashboard route.

## `/api/turn`

Stable fields:

- `turn`: selected turn record.
- `model_call_summary`: model-call aggregate for the turn.
- `tool_call_summary`: summarized tool rows for the turn.
- `model_call_total`: model-call count.
- `tool_call_total`: tool-call count.
- `subagents`: child task rollups for the turn.

This endpoint does not expose raw model-call or raw tool-call detail arrays.
Browser code should use the summary fields above.

## `/api/log-cleanup`

The request query requires `timezone`, an IANA name such as `Asia/Seoul`.
`cutoff_date` is an optional `YYYY-MM-DD` and means the final local calendar
date included in deletion. When omitted, the server selects the date immediately
before the seven local dates kept by the default retention policy. The server
converts the following local midnight to the exclusive `cutoff_unix` used by
retention scanning.

Stable fields:

- `summary`: total service bytes and cleanup totals.
- `retention`: cleanup preview metadata for the selected cutoff.
- `rows`: cleanup groups for the Retention Impact table.

Each cleanup row exposes display-oriented fields:

- `label`
- `group_id`
- `capabilities`
- `bytes`
- `compactable_bytes`
- `deletable_bytes`
- `status`
- `retention_effect`
- `display`
- `delete_all_display`

`group_id` is the stable row identity. `label` is display text only. The current
cleanup row groups are defined in `scripts/dashboard_cleanup_contract.py`.

`display` is the cutoff-mode presentation payload. `delete_all_display` is the
all-logs presentation payload. Browser code should not infer file
or row semantics from `label` or comma-joined paths when structured row fields
are available.

`retention.selected` includes `cutoff_date`, `timezone`, and `cutoff_unix`.
The first two fields describe the user's inclusive local-date selection;
`cutoff_unix` is the following local midnight expressed as an exclusive Unix
boundary.

`scanned_rows`, `deletable_rows`, and display `affected_rows` are JSONL row
counts only. File-only cleanup groups expose file impact through
`affected_files`, `scope_unit`, and `action_file_counts` instead of mixing file
counts into row fields.

Cleanup row display payloads must use one of these `action` values:

- `-`: no file or row mutation for this mode.
- `Delete`: the listed files or rows are deleted.
- `Rewrite`: at least one source file is rewritten to retain newer rows.
- `Rebuild`: derived output is deleted and rebuilt from retained sources.

Each display payload also exposes `action_file_counts` with stable `Delete`,
`Rewrite`, and `Rebuild` integer keys. Browser code should render Retention
Impact action counts from this field instead of deriving counts from paths,
labels, row action text, or detail items.

## `POST /api/log-cleanup/retention`

This endpoint applies the selected retention cutoff and rebuilds derived
analysis state when needed. The request contract is:

- `cutoff_date`: required `YYYY-MM-DD`.
- `timezone`: required IANA time-zone name.
- `preview_signature`: required signature from `/api/log-cleanup`
  `retention.selected.preview_signature`.

The server must re-read the current cleanup preview before deleting data and
must pass the same `preview_signature` to the `retention-prune` command. The
prune command requires `--preview-signature` and must reject missing or stale
signatures before cleanup preflight, recovery, derived output reset, or physical
deletion. A retention delete should run when the preview has either JSONL rows
to delete or old pending turn-start state files to remove.

Successful mutation responses include:

- `ok`: `true`.
- `cutoff_date`: applied cutoff date.
- `timezone`: applied IANA time zone.
- `retention`: prune command result metadata.
- `cleanup`: refreshed cleanup payload for the same cutoff.

If the rebuild quarantines malformed retained input, the mutation still returns
HTTP 200 after rebuilding valid rows and adds `status: "degraded"`,
`data_health: "degraded"`, and `quarantine`. Other nonzero prune results remain
failures.

No-op responses are allowed only when the selected preview has no deletable
JSONL rows and no pending turn-start state files. They include `noop: true`,
`retention.deleted_rows: 0`, and the unchanged `cleanup` preview.

Failure responses:

- Missing `cutoff_date`: HTTP 400 with `cutoff_date_required`.
- Malformed `cutoff_date`: HTTP 400 with `cutoff_date_invalid`.
- Missing `timezone`: HTTP 400 with `cutoff_timezone_required`.
- Unknown `timezone`: HTTP 400 with `cutoff_timezone_invalid`.
- Missing `preview_signature`: HTTP 400 with
  `cleanup_preview_signature_required`.
- Stale `preview_signature`, including stale signatures reported by
  `retention-prune`: HTTP 409 with `cleanup_preview_stale`.
- Retention preview manifest failure: HTTP 409 with `cleanup_preview_failed`.
- Service busy: HTTP 409 with `analysis_or_cleanup_running`.
- Prune failure: HTTP 500 with `retention_prune_failed`. This response may
  include `partial_mutation`, `recovery_required`, `derived_rebuild_required`,
  `physical_delete_pending`, `pending_files`, `stage`, and `deleted_rows`.

## `POST /api/log-cleanup/all`

This endpoint deletes service-owned generated log and analysis data for the
all-logs cleanup flow. The request contract is:

- `confirm_all_logs`: required `true`.

Successful responses include:

- `ok`: `true`.
- `deleted_bytes`: bytes deleted from service-owned targets.
- `deleted`: per-target deletion results.
- `cleanup`: refreshed cleanup payload after deletion.

Failure responses:

- Missing confirmation: HTTP 400 with `delete_all_confirmation_required`.
- Service busy: HTTP 409 with `analysis_or_cleanup_running`.
- Partial delete failure: HTTP 500 with `cleanup_delete_failed`. This response
  may include `delete_failed`, `partial_mutation`, `failed`, `deleted`,
  `deleted_bytes`, and a refreshed `cleanup` payload.

This endpoint does not accept per-row or per-file target selectors. Browser code
must use the confirmation contract above and render row-level file detail from
`/api/log-cleanup/detail` only.

## `GET /api/log-cleanup/progress`

This endpoint exposes the current cleanup progress snapshot. It is used while
retention cleanup or all-logs cleanup is running.

Stable fields:

- `status`: progress state such as `idle`, `running`, `completed`, `failed`, or
  `unknown`.
- `running`: whether the snapshot itself is currently running.
- `cleanup_running`: whether a cleanup handler currently owns the cleanup
  progress slot.
- `phase`: cleanup phase, including `cleanup-prepare`, `cleanup-delete`,
  `cleanup-rebuild`, or `cleanup-refresh`.
- `phase_index`, `phase_count`: zero-based phase position and total phases.
- `checkpoint`: short machine-readable progress checkpoint.
- `phase_progress`, `overall_progress`: numeric progress values.
- `processed`, `total`: optional item counters.
- `updated_at_unix`: snapshot write time.

When no cleanup snapshot is active, the endpoint returns an idle progress
payload with `cleanup_running: false`. Completed or failed snapshots are removed
when the owning cleanup request closes.

## `/api/log-cleanup/detail`

This endpoint is row-specific and is selected by stable `group_id`, not by the
display `label`. The request contract is:

- `group_id`: required stable cleanup row id.
- `preview_signature`: required signature from `/api/log-cleanup`
  `retention.selected.preview_signature`.
- `cutoff_date`: optional `YYYY-MM-DD`; when omitted, the server uses the
  default retention cutoff.
- `timezone`: required IANA time-zone name used to interpret `cutoff_date` and
  the default retention date.

It returns:

- `row`: a row-specific detail payload that overlays the parent row from
  `/api/log-cleanup`.

It must not expose the full global `retention.selected.files` list. The full
preview and cutoff metadata belong to `/api/log-cleanup`; detail views should
render only the requested row's structured `display.items`, `display.targets`,
`delete_all_display.items`, or `delete_all_display.targets`.

Failure responses:

- Missing `group_id`: HTTP 400 with `cleanup_group_id_required`.
- Missing `preview_signature`: HTTP 400 with
  `cleanup_preview_signature_required`.
- Stale `preview_signature`: HTTP 409 with `cleanup_preview_stale`.
- Unknown `group_id`: HTTP 404 with `cleanup_row_not_found`.
- Retention preview manifest failure: HTTP 409 with `cleanup_preview_failed`.
