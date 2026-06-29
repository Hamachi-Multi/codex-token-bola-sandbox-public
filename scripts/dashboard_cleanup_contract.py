"""Shared Cleanup dashboard row and display contracts."""

from __future__ import annotations

CLEANUP_ALLOWED_ACTIONS = {"-", "Delete", "Rewrite", "Rebuild"}

CLEANUP_ROW_DEFINITIONS = (
    {
        "label": "Normalized Outputs",
        "group_id": "normalized_outputs",
        "role": "derived_rebuild",
        "retention_effect": "rebuilt after delete",
        "capabilities": ["rebuild_after_delete", "delete_all"],
    },
    {
        "label": "Analytics Database",
        "group_id": "analytics_database",
        "role": "derived_rebuild",
        "retention_effect": "rebuilt after delete",
        "capabilities": ["rebuild_after_delete", "delete_all"],
    },
    {
        "label": "Archived Raw Logs",
        "group_id": "archived_raw_logs",
        "role": "source_prune",
        "retention_effect": "rows deleted by cutoff",
        "capabilities": ["retention_delete", "delete_all"],
    },
    {
        "label": "Raw Current Segments",
        "group_id": "raw_current_segments",
        "role": "source_prune",
        "retention_effect": "rows deleted by cutoff",
        "capabilities": ["retention_delete", "delete_all"],
    },
    {
        "label": "Pending Turn State",
        "group_id": "pending_turn_state",
        "role": "pending_turn_state",
        "retention_effect": "orphan start state deleted by cutoff",
        "capabilities": ["retention_delete", "delete_all"],
    },
    {
        "label": "State Files",
        "group_id": "state_files",
        "role": "state",
        "retention_effect": "retention state updated",
        "capabilities": ["delete_all"],
    },
)

CLEANUP_ROW_GROUPS = {str(row["label"]): dict(row) for row in CLEANUP_ROW_DEFINITIONS}
CLEANUP_ROW_GROUPS_BY_ID = {str(row["group_id"]): dict(row) for row in CLEANUP_ROW_DEFINITIONS}
CLEANUP_ROW_LABELS = tuple(str(row["label"]) for row in CLEANUP_ROW_DEFINITIONS)

CLEANUP_RETIRED_LABELS = {"Raw Model Calls", "Normalized Model Calls", "Reports"}

CLEANUP_REQUIRED_DISPLAY_FIELDS = {
    "action",
    "total_size",
    "total_rows",
    "delete_size",
    "affected_rows",
    "affected_files",
    "action_file_counts",
    "detail_title",
    "detail_items_kind",
}


def cleanup_group_for_label(label: str) -> dict[str, object]:
    if label not in CLEANUP_ROW_GROUPS:
        raise KeyError(f"Unknown cleanup row label: {label}")
    return dict(CLEANUP_ROW_GROUPS[label])


def cleanup_row_definitions() -> tuple[dict[str, object], ...]:
    return tuple(dict(row) for row in CLEANUP_ROW_DEFINITIONS)
