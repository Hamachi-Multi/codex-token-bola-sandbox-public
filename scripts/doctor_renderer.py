"""Human-readable rendering for doctor reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IssuePresentation:
    title: str
    action: str | None = None


ISSUE_PRESENTATIONS: dict[str, IssuePresentation] = {
    "runtime_config_missing": IssuePresentation("Runtime configuration is missing", "bola install-hook"),
    "codex_dir_invalid": IssuePresentation("Codex directory is invalid"),
    "codex_cli_invalid": IssuePresentation("Codex CLI is unavailable or invalid"),
    "runtime_status_invalid": IssuePresentation("Runtime status could not be inspected"),
    "current_segment_state_invalid": IssuePresentation("Raw segment state is invalid"),
    "hooks_config_invalid": IssuePresentation("Codex hook configuration is invalid"),
    "hook_registration_missing": IssuePresentation("Required Codex hooks are missing", "bola install-hook"),
    "stale_hook_registration": IssuePresentation("Stale BOLA hook registrations remain", "bola install-hook"),
    "normalize_pending_publish_recovery_required": IssuePresentation("Normalized output recovery is required", "bola pipeline"),
    "pending_recovery_state": IssuePresentation("Completed turns are waiting for recovery", "bola pipeline --recover"),
    "recent_hook_errors": IssuePresentation("Recent hook writes failed"),
    "stale_analytics_temp_files": IssuePresentation("Stale analytics temporary files remain", "bola pipeline"),
    "retention_pruned_store_invalid": IssuePresentation("Retention state is invalid"),
    "cleanup_retention_job_invalid": IssuePresentation("Cleanup recovery state is invalid"),
    "retention_checkpoint_invalid": IssuePresentation("Retention checkpoint state is invalid"),
    "service_lock_state_invalid": IssuePresentation("Service lock state is invalid"),
    "path_transition_invalid": IssuePresentation("Path transition state is invalid"),
    "retention_pruned_store_migration_required": IssuePresentation("Retention state migration is required"),
    "retention_pruned_state_recovery_ready": IssuePresentation("Retention state is ready to finish recovery"),
    "retention_pruned_state_resolution_required": IssuePresentation("Retention state requires manual resolution"),
    "retention_pruned_state_pending": IssuePresentation("Retention state update is still in progress"),
    "retention_pruned_state_orphaned": IssuePresentation("Retention state has no active owner"),
    "stale_retention_checkpoints": IssuePresentation("Stale retention checkpoints remain"),
    "quarantine_state_invalid": IssuePresentation("Quarantine state is invalid"),
    "unacknowledged_quarantine": IssuePresentation("Quarantined records need review", "bola quarantine list"),
}


def known_issue_codes() -> frozenset[str]:
    return frozenset(ISSUE_PRESENTATIONS)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _format_bytes(value: object) -> str:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "unknown size"
    units = ("B", "KiB", "MiB", "GiB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def _format_collection(value: object) -> str | None:
    if isinstance(value, dict):
        items = [
            f"{str(key).removeprefix('error:')} ({item})"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        ]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item) for item in value]
    else:
        return None
    visible = items[:5]
    suffix = f", +{len(items) - len(visible)} more" if len(items) > len(visible) else ""
    return ", ".join(visible) + suffix


def _format_age(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    seconds = max(0, int(value))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 60 * 60:
        return f"{seconds // 60}m ago"
    if seconds < 24 * 60 * 60:
        return f"{seconds // (60 * 60)}h ago"
    return f"{seconds // (24 * 60 * 60)}d ago"


def _issue_details(issue: dict[str, object], report: dict[str, object]) -> list[str]:
    code = str(issue.get("code") or "unknown_issue")
    details: list[str] = []
    count = issue.get("count")
    if count is not None:
        details.append(f"Count: {count}")
    occurrences = issue.get("occurrences")
    if occurrences is not None:
        details.append(f"Occurrences: {occurrences}")
    byte_count = issue.get("bytes")
    if byte_count is not None:
        details.append(f"Size: {_format_bytes(byte_count)}")
    collection_labels = {
        "events": "Events",
        "errors": "Errors",
        "by_kind": "Kinds",
        "jobs": "Jobs",
        "files": "Files",
    }
    for key, label in collection_labels.items():
        formatted = _format_collection(issue.get(key))
        if formatted:
            details.append(f"{label}: {formatted}")
    for key, label in (("reason", "Reason"), ("error", "Error"), ("path", "Path")):
        value = issue.get(key)
        if value not in (None, ""):
            details.append(f"{label}: {value}")
    if code == "recent_hook_errors":
        recovery = _mapping(_mapping(report.get("runtime")).get("recovery"))
        last_error = _mapping(recovery.get("last_error"))
        if last_error.get("code"):
            age = _format_age(last_error.get("age_seconds"))
            age_text = f", {age}" if age else ""
            code_text = str(last_error["code"]).removeprefix("error:")
            details.append(f"Last occurrence: {code_text}{age_text}")
    if not details:
        extra = {key: value for key, value in issue.items() if key not in {"code", "severity"}}
        formatted = _format_collection(extra)
        if formatted:
            details.append(formatted)
    return details


def _check_lines(report: dict[str, object]) -> list[str]:
    codex_dir = _mapping(report.get("codex_dir"))
    codex_cli = _mapping(report.get("codex_cli"))
    output_dir = _mapping(report.get("output_dir"))
    analytics_db = _mapping(report.get("analytics_db"))
    hooks = _mapping(_mapping(_mapping(report.get("runtime")).get("hooks_json")).get("events"))

    lines = [
        f"[{'OK' if codex_dir.get('valid') else 'FAIL'}] Codex directory: {codex_dir.get('path') or 'unknown'}",
        f"[{'OK' if codex_cli.get('valid') else 'FAIL'}] Codex CLI: {codex_cli.get('version') or codex_cli.get('reason') or 'unknown'}",
    ]
    missing_hooks = sorted(str(name) for name, value in hooks.items() if isinstance(value, dict) and not value.get("registered"))
    if not hooks:
        lines.append("[FAIL] Codex hooks: unavailable")
    elif missing_hooks:
        lines.append(f"[WARN] Codex hooks: missing {', '.join(missing_hooks)}")
    else:
        lines.append(f"[OK] Codex hooks: {', '.join(sorted(str(name) for name in hooks))}")
    lines.append(f"[{'OK' if output_dir.get('exists') else 'INFO'}] Output directory: {output_dir.get('path') or 'unknown'}")
    analytics_status = "OK" if analytics_db.get("exists") else "INFO"
    analytics_note = f" ({_format_bytes(analytics_db.get('bytes'))})" if analytics_db.get("exists") else " (not built)"
    lines.append(f"[{analytics_status}] Analytics database: {analytics_db.get('path') or 'unknown'}{analytics_note}")
    return lines


def render_doctor_report(report: dict[str, object]) -> str:
    health = _mapping(report.get("health"))
    status = str(health.get("status") or "failed").upper()
    issues = [issue for issue in health.get("issues", []) if isinstance(issue, dict)] if isinstance(health.get("issues"), list) else []
    lines = [f"BOLA Doctor: {status}", "", "Checks", *_check_lines(report), "", "Issues"]
    if not issues:
        lines.append("None")
    for issue in issues:
        code = str(issue.get("code") or "unknown_issue")
        severity = {"degraded": "WARN", "failed": "FAIL"}.get(str(issue.get("severity") or "failed"), "FAIL")
        presentation = ISSUE_PRESENTATIONS.get(code, IssuePresentation(code.replace("_", " ").capitalize()))
        code_suffix = "" if code in ISSUE_PRESENTATIONS else f" ({code})"
        lines.append(f"[{severity}] {presentation.title}{code_suffix}")
        for detail in _issue_details(issue, report):
            lines.append(f"  {detail}")
        if presentation.action:
            lines.append(f"  Run: {presentation.action}")
    lines.extend(("", "Full report: bola doctor --json"))
    return "\n".join(lines)
