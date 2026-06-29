"""Cleanup browser checks for the Codex Token Bola dashboard."""

from __future__ import annotations

import json
import urllib.parse

from dashboard_cleanup_contract import (
    CLEANUP_ALLOWED_ACTIONS,
    CLEANUP_REQUIRED_DISPLAY_FIELDS,
    CLEANUP_RETIRED_LABELS,
)
from playwright_dashboard_helpers import assert_true, fetch_json

LEGACY_CLEANUP_ROW_FIELDS = {
    "retention_role",
    "retention_impact",
    "delete_all_impact",
    "retention_summary",
    "delete_all_summary",
}


def assert_cleanup_row_contract(row: dict, *, context: str) -> None:
    label = str(row.get("label") or "")
    assert_true(label not in CLEANUP_RETIRED_LABELS, f"{context} exposes retired cleanup row {label!r}: {row}")
    leaked = sorted(LEGACY_CLEANUP_ROW_FIELDS.intersection(row))
    assert_true(not leaked, f"{context} exposes legacy cleanup fields {leaked}: {row}")
    for key in ("display", "delete_all_display"):
        display = row.get(key)
        assert_true(isinstance(display, dict), f"{context} row {label!r} missing {key}: {row}")
        missing = sorted(CLEANUP_REQUIRED_DISPLAY_FIELDS.difference(display))
        assert_true(not missing, f"{context} row {label!r} {key} missing fields {missing}: {display}")
        action = str(display.get("action") or "")
        assert_true(action in CLEANUP_ALLOWED_ACTIONS, f"{context} row {label!r} {key} has invalid action {action!r}: {display}")
    assert_true(isinstance(row.get("group_id"), str) and row.get("group_id"), f"{context} row {label!r} missing stable group_id: {row}")
    assert_true(isinstance(row.get("capabilities"), list), f"{context} row {label!r} missing capabilities list: {row}")


def assert_cleanup_payload_contract(payload: dict, *, context: str) -> None:
    rows = payload.get("rows") or []
    assert_true(isinstance(rows, list) and rows, f"{context} cleanup payload should include rows: {payload}")
    labels = {str(row.get("label") or "") for row in rows}
    assert_true(CLEANUP_RETIRED_LABELS.isdisjoint(labels), f"{context} cleanup payload exposes retired rows: {labels}")
    for row in rows:
        assert_cleanup_row_contract(row, context=context)


def assert_cleanup_detail_payload_contract(payload: dict, *, group_id: str, context: str) -> None:
    assert_true(isinstance(payload, dict), f"{context} detail response should be an object: {payload}")
    assert_true("retention" not in payload, f"{context} detail response should not expose global retention preview: {payload}")
    row = payload.get("row")
    assert_true(isinstance(row, dict), f"{context} detail response should include row patch: {payload}")
    assert_true(str(row.get("group_id") or "") == group_id, f"{context} detail row should match requested group_id {group_id!r}: {row}")
    for key in ("display", "delete_all_display"):
        display = row.get(key)
        assert_true(isinstance(display, dict), f"{context} detail row missing {key}: {row}")
        assert_true(isinstance(display.get("targets"), list), f"{context} detail {key} should expose target paths list: {display}")
        assert_true(isinstance(display.get("targets_truncated"), int), f"{context} detail {key} should expose targets_truncated: {display}")
        if "items" in display:
            assert_true(isinstance(display["items"], list), f"{context} detail {key}.items should be a list when present: {display}")


def cleanup_detail_display_has_files(display: dict) -> bool:
    items = display.get("items")
    targets = display.get("targets")
    return (isinstance(items, list) and len(items) > 0) or (isinstance(targets, list) and len(targets) > 0)


def check_cleanup_short_desktop(page, base_url: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/#cleanup", wait_until="networkidle")
    page.wait_for_selector("#cleanup-files tr[data-cleanup-file]", timeout=10_000)
    compact_state = page.evaluate(
        """
        () => {
          const files = document.querySelector('#cleanup-files');
          const scroller = files.querySelector('.table-scroll');
          const rows = Array.from(document.querySelectorAll('#cleanup-files tbody tr'));
          const firstRow = rows[0].getBoundingClientRect();
          const lastRow = rows[rows.length - 1].getBoundingClientRect();
          const rowHeights = rows.map((row) => row.getBoundingClientRect().height);
          const layout = document.querySelector('.cleanup-layout').getBoundingClientRect();
          const view = document.querySelector('.view.active').getBoundingClientRect();
          const workbench = document.querySelector('.cleanup-workbench-panel').getBoundingClientRect();
          const form = document.querySelector('.cleanup-retention-form').getBoundingClientRect();
          const appbar = document.querySelector('.appbar').getBoundingClientRect();
          const preset = document.querySelector('[data-cleanup-retention-preset="1"]');
          const lastPreset = document.querySelector('[data-cleanup-retention-preset="90"]').getBoundingClientRect();
          const refresh = document.querySelector('#cleanup-refresh').getBoundingClientRect();
          return {
            appbarHeight: Math.round(appbar.height * 1000) / 1000,
            rowHeight: Math.round(firstRow.height * 1000) / 1000,
            rowHeightDelta: Math.round((Math.max(...rowHeights) - Math.min(...rowHeights)) * 1000) / 1000,
            lastRowBottom: Math.round(lastRow.bottom * 1000) / 1000,
            viewportHeight: window.innerHeight,
            layoutBottomDelta: Math.round(Math.abs(layout.bottom - view.bottom) * 1000) / 1000,
            workbenchHeight: Math.round(workbench.height * 1000) / 1000,
            formBottomDelta: Math.round((workbench.bottom - form.bottom) * 1000) / 1000,
            canScroll: files.scrollHeight - files.clientHeight > 1,
            tableCanScrollX: scroller.scrollWidth - scroller.clientWidth > 1,
            tableOverflowX: getComputedStyle(scroller).overflowX,
            overflowY: getComputedStyle(files).overflowY,
            presetWhiteSpace: getComputedStyle(preset).whiteSpace,
            presetHeight: Math.round(preset.getBoundingClientRect().height * 1000) / 1000,
            presetActionGap: Math.round((refresh.left - lastPreset.right) * 1000) / 1000,
            summaryValueSize: getComputedStyle(document.querySelector('#cleanup-selected-bytes')).fontSize,
          };
        }
        """
    )
    assert_true(compact_state["appbarHeight"] <= 74, f"short desktop toolbar should remain on one row: {compact_state}")
    assert_true(compact_state["presetWhiteSpace"] == "nowrap", f"short desktop preset labels should not wrap: {compact_state}")
    assert_true(compact_state["presetHeight"] <= 34, f"short desktop preset buttons should stay compact: {compact_state}")
    assert_true(compact_state["presetActionGap"] >= 8, f"short desktop cleanup presets should not overlap actions: {compact_state}")
    assert_true(float(compact_state["summaryValueSize"].replace("px", "")) >= 22, f"short desktop cleanup summary value is too small: {compact_state}")
    assert_true(compact_state["rowHeight"] <= 42, f"short desktop should keep managed-file rows compact: {compact_state}")
    assert_true(compact_state["rowHeightDelta"] <= 1, f"short desktop managed-file rows should have equal heights: {compact_state}")
    assert_true(compact_state["layoutBottomDelta"] <= 1, f"short desktop cleanup page should fill the available content height: {compact_state}")
    assert_true(compact_state["workbenchHeight"] >= 180, f"short desktop log retention should absorb remaining height: {compact_state}")
    assert_true(0 <= compact_state["formBottomDelta"] <= 2, f"short desktop log retention controls should stay inside the panel: {compact_state}")
    assert_true(compact_state["overflowY"] == "hidden", f"short desktop managed files should clip table overflow at the panel edge: {compact_state}")
    assert_true(compact_state["tableOverflowX"] == "auto", f"short desktop managed files should keep horizontal table scrolling available: {compact_state}")
    assert_true(not compact_state["canScroll"], f"short desktop managed files should not report internal scroll: {compact_state}")
    assert_true(
        compact_state["lastRowBottom"] <= compact_state["viewportHeight"],
        f"short desktop should keep every managed file row visible: {compact_state}",
    )
    page.set_viewport_size({"width": 900, "height": 650})
    page.goto(f"{base_url}/#cleanup", wait_until="networkidle")
    page.wait_for_selector("#cleanup-files tr[data-cleanup-file]", timeout=10_000)
    minimum_desktop = page.evaluate(
        """
        () => {
          const files = document.querySelector('#cleanup-files');
          const scroller = files.querySelector('.table-scroll');
          const rows = Array.from(document.querySelectorAll('#cleanup-files tbody tr'));
          const lastRow = rows[rows.length - 1].getBoundingClientRect();
          const view = document.querySelector('.view.active').getBoundingClientRect();
          const layout = document.querySelector('.cleanup-layout').getBoundingClientRect();
          return {
            viewportHeight: window.innerHeight,
            lastRowBottom: Math.round(lastRow.bottom * 1000) / 1000,
            layoutBottomDelta: Math.round(Math.abs(layout.bottom - view.bottom) * 1000) / 1000,
            filesCanScrollY: files.scrollHeight - files.clientHeight > 1,
            tableCanScrollX: scroller.scrollWidth - scroller.clientWidth > 1,
            tableOverflowX: getComputedStyle(scroller).overflowX,
          };
        }
        """
    )
    assert_true(minimum_desktop["layoutBottomDelta"] <= 1, f"900x650 cleanup layout should fill the active view: {minimum_desktop}")
    assert_true(not minimum_desktop["filesCanScrollY"], f"900x650 cleanup files should not need vertical scrolling: {minimum_desktop}")
    assert_true(minimum_desktop["tableOverflowX"] == "auto", f"900x650 cleanup table should expose horizontal scrolling: {minimum_desktop}")
    assert_true(minimum_desktop["tableCanScrollX"], f"900x650 cleanup table should scroll horizontally instead of clipping columns: {minimum_desktop}")
    assert_true(
        minimum_desktop["lastRowBottom"] <= minimum_desktop["viewportHeight"],
        f"900x650 cleanup should keep every managed file row vertically visible: {minimum_desktop}",
    )

def check_cleanup_desktop(page, base_url: str) -> None:
    page.locator('button[data-view-target="cleanup"]').click()
    page.wait_for_selector("#cleanup-files tr[data-cleanup-file]", timeout=10_000)
    cleanup, delete_button = check_cleanup_table_contract(page, base_url)
    check_cleanup_selection_state(page)
    check_cleanup_all_preset(page, cleanup)
    check_cleanup_retention_preset(page, base_url)
    check_cleanup_preview_race(page, base_url)
    check_cleanup_layout(page)
    check_cleanup_detail_modal(page, base_url)
    check_cleanup_retired_rows_and_controls(page)
    check_cleanup_refresh_stability(page, delete_button)


def cleanup_row_summaries(page) -> dict:
    return page.evaluate(
        """
        () => ({
          rows: Array.from(document.querySelectorAll('#cleanup-files tbody tr')).map(row => ({
            groupId: row.dataset.cleanupFile || '',
            totalSize: row.querySelector('.cleanup-size-cell')?.textContent || '',
            affectedSize: row.querySelector('.cleanup-affected-size-cell')?.textContent || '',
            affectedFiles: row.querySelector('.cleanup-affected-files-summary')?.getAttribute('aria-label') || '',
            visibleAffectedFiles: row.querySelector('.cleanup-affected-files-cell')?.textContent || '',
          })),
        })
        """
    )


def expected_cleanup_rows(page, rows: list[dict], *, all_mode: bool) -> dict:
    return page.evaluate(
        """
        ({ rows, allMode }) => Object.fromEntries(rows.map(row => {
          const selectedDisplay = allMode ? (row.delete_all_display || {}) : (row.display || {});
          return [row.group_id, {
            totalSize: formatBytes(row.bytes || 0),
            affectedSize: formatBytes(selectedDisplay.delete_size || 0),
          }];
        }))
        """,
        {"rows": rows, "allMode": all_mode},
    )


def expected_cleanup_totals(page, rows: list[dict]) -> dict:
    return page.evaluate(
        """
        ({ rows }) => {
          const totals = rows.reduce((acc, row) => {
            const display = row.delete_all_display || {};
            acc.affectedFiles += Number(display.affected_files || 0);
            if (String(row.label || '') === 'Raw Current Segments') {
              acc.rawSegmentRows += Number(display.affected_rows || 0);
            }
            return acc;
          }, {rawSegmentRows: 0, affectedFiles: 0});
          const label = (count, noun) => `${compactNumber(count)} ${count === 1 ? noun : `${noun}s`}`;
          return {
            rawSegmentRows: label(totals.rawSegmentRows, 'row'),
            affectedFiles: label(totals.affectedFiles, 'file'),
          };
        }
        """,
        {"rows": rows},
    )


def assert_cleanup_rows_match(summary: dict, expected_by_group: dict, *, groups: tuple[str, ...] | None = None, context: str) -> None:
    rows = summary.get("rows") or []
    selected_groups = groups or tuple(row.get("groupId") for row in rows)
    for group_id in selected_groups:
        row = next((item for item in rows if item["groupId"] == group_id), None)
        expected = expected_by_group.get(group_id)
        assert_true(row is not None and expected is not None, f"{context} missing row {group_id}: {summary}")
        assert_true(row["affectedSize"] == expected["affectedSize"], f"{context} affected size mismatch for {group_id}: {row} vs {expected}")
        assert_true(row["totalSize"] == expected["totalSize"], f"{context} total size mismatch for {group_id}: {row} vs {expected}")


def check_cleanup_table_contract(page, base_url: str) -> tuple[dict, object]:
    cleanup_focus_index = page.locator("#cleanup-files").evaluate(
        "() => Array.from(document.querySelectorAll('#cleanup-files tr[data-cleanup-file]')).indexOf(document.activeElement)"
    )
    assert_true(cleanup_focus_index >= 0, f"cleanup view should focus a cleanup row on entry: {cleanup_focus_index}")
    cleanup_text = page.locator('[data-view="cleanup"]').inner_text()
    assert_true("Cleanup" in cleanup_text, "cleanup title missing")
    assert_true("Log Cleanup" not in cleanup_text, "old cleanup title should not render")
    assert_true(all(label in cleanup_text for label in ("Total Size", "Affected Size", "Affected Files", "Segment Rows")), "cleanup impact columns missing")
    assert_true("Total Files" not in cleanup_text, "cleanup impact table should not expose a standalone Total Files column")
    assert_true("Actions" not in cleanup_text, "cleanup impact table should not expose the old actions column")
    assert_true("Affected Rows" not in cleanup_text, "cleanup should not expose mixed-semantics affected rows")
    assert_true("Delete Logs" in cleanup_text, "retention cleanup action label missing")
    cleanup = fetch_json(f"{base_url}/api/log-cleanup")
    assert_cleanup_payload_contract(cleanup, context="initial cleanup")
    cleanup_headers = [
        text for text in page.locator("#cleanup-files th").all_text_contents()
        if text.strip()
    ]
    assert_true(
        cleanup_headers == ["File Group", "Total Size", "Affected Size", "Affected Files"],
        f"unexpected cleanup headers: {cleanup_headers}",
    )
    cleanup_table = page.locator("#cleanup-files table")
    cleanup_table_layout = cleanup_table.evaluate("(el) => getComputedStyle(el).tableLayout")
    assert_true(cleanup_table_layout == "fixed", f"cleanup table should keep fixed columns: {cleanup_table_layout}")
    cleanup_col_widths = page.locator("#cleanup-files col").evaluate_all("(cols) => cols.map(col => getComputedStyle(col).width)")
    assert_true(len(cleanup_col_widths) == 4, f"cleanup table should expose four fixed columns: {cleanup_col_widths}")
    cleanup_column_layout = page.locator("#cleanup-files tr[data-cleanup-file]").first.evaluate(
        """
        (row) => {
          const affectedSize = row.querySelector('.cleanup-affected-size-cell');
          const affected = row.querySelector('.cleanup-affected-files-cell');
          const affectedSizeRect = affectedSize.getBoundingClientRect();
          const affectedRect = affected.getBoundingClientRect();
          return {
            hasUnexpectedTotalFiles: !!row.querySelector('.cleanup-total-files-cell'),
            hasUnexpectedSpacer: !!row.querySelector('.cleanup-total-files-spacer, .cleanup-column-spacer, .cleanup-affected-files-spacer'),
            affectedSizeWidth: Math.round(affectedSizeRect.width),
            affectedFilesWidth: Math.round(affectedRect.width),
            affectedSizeToAffectedFilesGap: Math.round(affectedRect.left - affectedSizeRect.right),
          };
        }
        """
    )
    assert_true(
        not cleanup_column_layout["hasUnexpectedTotalFiles"]
        and not cleanup_column_layout["hasUnexpectedSpacer"]
        and cleanup_column_layout["affectedFilesWidth"] > cleanup_column_layout["affectedSizeWidth"]
        and cleanup_column_layout["affectedSizeToAffectedFilesGap"] == 0,
        f"cleanup should render Affected Files directly after Affected Size without Total Files spacer: {cleanup_column_layout}",
    )
    cleanup_row_accessibility = page.locator("#cleanup-files tr[data-cleanup-file]").first.evaluate(
        """
        (row) => ({
          hasButtonRole: row.getAttribute('role') === 'button',
          hasDialogPopup: row.getAttribute('aria-haspopup') === 'dialog',
          label: row.getAttribute('aria-label') || '',
        })
        """
    )
    assert_true(
        not cleanup_row_accessibility["hasButtonRole"]
        and cleanup_row_accessibility["hasDialogPopup"]
        and cleanup_row_accessibility["label"].endswith(". Press Enter or Space to open file detail."),
        f"cleanup impact rows should expose detail-dialog keyboard semantics without button role: {cleanup_row_accessibility}",
    )
    expected_action_counts = {
        str(row.get("group_id") or ""): ((row.get("display") or {}).get("action_file_counts") or {})
        for row in cleanup.get("rows", [])
    }
    cleanup_action_counts = page.locator("#cleanup-files tr[data-cleanup-file]").evaluate_all(
        """
        (rows) => rows.map(row => ({
          groupId: row.getAttribute('data-cleanup-file') || '',
          labels: Array.from(row.querySelectorAll('.cleanup-affected-files-part')).map(item => item.getAttribute('title') || item.getAttribute('aria-label') || ''),
          values: Array.from(row.querySelectorAll('.cleanup-affected-files-value')).map(item => item.textContent.trim()),
        }))
        """
    )
    for rendered in cleanup_action_counts:
        group_id = rendered["groupId"]
        expected = expected_action_counts.get(group_id, {})
        expected_values = [
            str(sum(int(expected.get(key, 0) or 0) for key in ("Delete", "Rebuild", "Rewrite"))),
            str(int(expected.get("Delete", 0) or 0)),
            str(int(expected.get("Rebuild", 0) or 0)),
            str(int(expected.get("Rewrite", 0) or 0)),
        ]
        assert_true(
            rendered["labels"] == ["Total Files", "Delete Files", "Rebuild Files", "Rewrite Files"]
            and rendered["values"] == expected_values,
            f"cleanup affected-file action counts should render API action_file_counts for {group_id}: {rendered} vs {expected}",
        )
    cleanup_numeric_alignment = page.locator("#cleanup-files tr[data-cleanup-file]").first.evaluate(
        """
        (row) => {
          const defs = [
            ['Total Size', 'th:nth-child(2)', '.cleanup-size-cell'],
            ['Affected Size', 'th:nth-child(3)', '.cleanup-affected-size-cell'],
          ];
          return defs.map(([label, headerSelector, cellSelector]) => {
            const header = document.querySelector(`#cleanup-files ${headerSelector}`);
            const cell = row.querySelector(cellSelector);
            const headerRect = header.getBoundingClientRect();
            const cellRect = cell.getBoundingClientRect();
            const headerStyle = getComputedStyle(header);
            const cellStyle = getComputedStyle(cell);
            const range = document.createRange();
            range.selectNodeContents(header);
            const headerTextRect = range.getBoundingClientRect();
            const cellTextRange = document.createRange();
            cellTextRange.selectNodeContents(cell);
            const cellTextRect = cellTextRange.getBoundingClientRect();
            return {
              label,
              headerJustify: headerStyle.justifyContent,
              headerTextAlign: headerStyle.textAlign,
              cellJustify: cellStyle.justifyContent,
              cellTextAlign: cellStyle.textAlign,
              headerRightDelta: Math.abs((headerRect.right - (parseFloat(headerStyle.paddingRight) || 0)) - headerTextRect.right),
              valueRightDelta: Math.abs((cellRect.right - (parseFloat(cellStyle.paddingRight) || 0)) - cellTextRect.right),
            };
          });
        }
        """
    )
    assert_true(
        all(
            item["headerJustify"] == "flex-end"
            and item["headerTextAlign"] == "right"
            and item["cellJustify"] == "flex-end"
            and item["cellTextAlign"] == "right"
            and item["headerRightDelta"] <= 1
            and item["valueRightDelta"] <= 1
            for item in cleanup_numeric_alignment
        ),
        f"cleanup size columns should be right-aligned: {cleanup_numeric_alignment}",
    )
    affected_files_alignment = page.locator("#cleanup-files .cleanup-affected-files-cell").first.evaluate(
        """
        (cell) => {
          const summary = cell.querySelector('.cleanup-affected-files-summary');
          const cellRect = cell.getBoundingClientRect();
          const summaryRect = summary.getBoundingClientRect();
          const style = getComputedStyle(cell);
          return {
            cellJustify: style.justifyContent,
            cellAlign: style.textAlign,
            paddingLeft: parseFloat(style.paddingLeft) || 0,
            summaryAlign: getComputedStyle(summary).textAlign,
            summaryLabel: summary.getAttribute('aria-label') || '',
            visibleText: summary.textContent.trim().replace(/\\s+/g, ' '),
            partCount: summary.querySelectorAll('.cleanup-affected-files-part').length,
            iconCount: summary.querySelectorAll('.cleanup-affected-files-icon svg').length,
            centerDelta: Math.abs((cellRect.left + cellRect.width / 2) - (summaryRect.left + summaryRect.width / 2)),
          };
        }
        """
    )
    assert_true(
        affected_files_alignment["cellJustify"] == "center"
        and affected_files_alignment["cellAlign"] == "center"
        and affected_files_alignment["paddingLeft"] < 144
        and affected_files_alignment["summaryAlign"] == "center"
        and all(label in affected_files_alignment["summaryLabel"] for label in ("Total", "Delete", "Rebuild", "Rewrite"))
        and not any(label in affected_files_alignment["visibleText"] for label in ("Total", "Delete", "Rebuild", "Rewrite"))
        and affected_files_alignment["partCount"] == 4
        and affected_files_alignment["iconCount"] == 4
        and affected_files_alignment["centerDelta"] <= 1,
        f"cleanup affected-files values should be centered in its own column: {affected_files_alignment}",
    )
    affected_files_header_alignment = page.locator("#cleanup-files th.cleanup-affected-files-header").evaluate(
        """
        (header) => {
          const headerRect = header.getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(header);
          const textRect = range.getBoundingClientRect();
          const style = getComputedStyle(header);
          return {
            justifyContent: style.justifyContent,
            textAlign: style.textAlign,
            centerDelta: Math.abs((headerRect.left + headerRect.width / 2) - (textRect.left + textRect.width / 2)),
          };
        }
        """
    )
    assert_true(
        affected_files_header_alignment["justifyContent"] == "center"
        and affected_files_header_alignment["textAlign"] == "center"
        and affected_files_header_alignment["centerDelta"] <= 1,
        f"cleanup affected-files header should be centered with the values: {affected_files_header_alignment}",
    )
    affected_file_summaries = page.locator("#cleanup-files .cleanup-affected-files-summary").evaluate_all(
        """
        (summaries) => summaries.map(summary => {
          const label = summary.getAttribute('aria-label') || '';
          const parseCompact = (value) => {
            const text = String(value || '').replace(/,/g, '');
            const match = text.match(/^([\\d.]+)([KMBT])?$/);
            if (!match) return 0;
            const multipliers = {K: 1_000, M: 1_000_000, B: 1_000_000_000, T: 1_000_000_000_000};
            return (Number(match[1]) || 0) * (multipliers[match[2]] || 1);
          };
          const values = Object.fromEntries(Array.from(label.matchAll(/(Total|Delete|Rebuild|Rewrite)(?: Files)?\\s+([\\d,.KMBT]+)/g)).map(match => [match[1], parseCompact(match[2])]));
          return {label, values};
        })
        """
    )
    assert_true(
        all(item["values"].get("Total") == item["values"].get("Delete", 0) + item["values"].get("Rebuild", 0) + item["values"].get("Rewrite", 0) for item in affected_file_summaries),
        f"cleanup affected-files total should match action breakdown: {affected_file_summaries}",
    )
    raw_row = next((row for row in cleanup["rows"] if row.get("group_id") == "raw_current_segments"), None)
    if raw_row:
        raw_summary = page.locator('#cleanup-files tr[data-cleanup-file="raw_current_segments"]').evaluate(
            """
            (row) => ({
              affectedLabel: row.querySelector('.cleanup-affected-files-summary')?.getAttribute('aria-label') || '',
            })
            """
        )
        raw_affected_files = str(raw_row.get("display", {}).get("affected_files") or "")
        assert_true(
            f"Total Files {raw_affected_files}" in raw_summary["affectedLabel"],
            f"raw current affected-files total should use selected affected files: {raw_summary} vs {raw_affected_files}",
        )
    delete_button = page.locator("#cleanup-delete")
    delete_style = delete_button.evaluate(
        "(el) => ({color: getComputedStyle(el).color, background: getComputedStyle(el).backgroundColor})"
    )
    assert_true(delete_style["background"] != "rgba(0, 0, 0, 0)", f"delete action has no danger background: {delete_style}")
    cleanup_row_cursor = page.locator("#cleanup-files tbody tr").first.evaluate("(el) => getComputedStyle(el).cursor")
    assert_true(cleanup_row_cursor == "default", f"cleanup impact rows should use the default cursor: {cleanup_row_cursor}")
    assert_true(cleanup["summary"]["service_bytes"] >= cleanup["summary"]["active_raw_bytes"], f"invalid cleanup summary: {cleanup}")
    assert_true("deletable_bytes" in cleanup["summary"], f"cleanup summary missing deletable bytes: {cleanup}")
    assert_true("retention" in cleanup and "selected" in cleanup["retention"], f"cleanup retention preview missing: {cleanup}")
    return cleanup, delete_button


def check_cleanup_selection_state(page) -> None:
    cleanup_selection_state = page.locator("#cleanup-files").evaluate(
        """
        () => {
          const rows = Array.from(document.querySelectorAll('#cleanup-files tbody tr'));
          const active = document.activeElement;
          const activeCell = active && active.matches('#cleanup-files tbody tr') ? active.querySelector('td') : null;
          return {
            selectedRows: document.querySelectorAll('#cleanup-files tbody tr.selected').length,
            ariaSelectedRows: document.querySelectorAll('#cleanup-files tbody tr[aria-selected="true"]').length,
            activeIndex: rows.indexOf(active),
            activeStripe: activeCell ? getComputedStyle(activeCell).boxShadow : '',
            rowIds: rows.map(row => row.dataset.cleanupFile || ''),
            selectedBackgrounds: Array.from(document.querySelectorAll('#cleanup-files tbody tr.selected td')).map(cell => getComputedStyle(cell).backgroundColor),
            inactiveBackgrounds: Array.from(document.querySelectorAll('#cleanup-files tbody tr:not(.selected) td')).map(cell => getComputedStyle(cell).backgroundColor),
          };
        }
        """
    )
    assert_true(cleanup_selection_state["selectedRows"] == 1, f"cleanup impact list should keep exactly one selected class: {cleanup_selection_state}")
    assert_true(cleanup_selection_state["ariaSelectedRows"] == 1, f"cleanup impact list should expose exactly one selected row: {cleanup_selection_state}")
    assert_true(cleanup_selection_state["activeIndex"] >= 0, f"cleanup impact list should still focus a row on entry: {cleanup_selection_state}")
    assert_true(cleanup_selection_state["activeStripe"] == "none", f"cleanup impact focus should not draw a left color stripe: {cleanup_selection_state}")
    row_ids = cleanup_selection_state["rowIds"]
    assert_true(all(row_ids), f"cleanup impact rows must render non-empty API group_id values: {cleanup_selection_state}")
    assert_true(len(row_ids) == len(set(row_ids)), f"cleanup impact row group_id values must be unique: {cleanup_selection_state}")
    assert_true(
        all(value == "rgb(238, 243, 239)" for value in cleanup_selection_state["selectedBackgrounds"]),
        f"cleanup impact selected row should keep the selected background: {cleanup_selection_state}",
    )
    assert_true(
        all(value == "rgba(0, 0, 0, 0)" for value in cleanup_selection_state["inactiveBackgrounds"]),
        f"cleanup impact inactive rows should not look selected: {cleanup_selection_state}",
    )
    selected_cleanup_row = page.locator("#cleanup-files tbody tr.selected").first
    assert_true(selected_cleanup_row.count() == 1, f"cleanup impact list should have exactly one selected row: {cleanup_selection_state}")
    selected_cleanup_bg = selected_cleanup_row.locator("td").first.evaluate("(el) => getComputedStyle(el).backgroundColor")
    selected_cleanup_row.hover()
    selected_cleanup_hover_bg = selected_cleanup_row.locator("td").first.evaluate("(el) => getComputedStyle(el).backgroundColor")
    assert_true(
        selected_cleanup_hover_bg == selected_cleanup_bg,
        f"selected cleanup impact row hover should keep selected background: before={selected_cleanup_bg}, after={selected_cleanup_hover_bg}",
    )
    selected_theme_state = page.evaluate(
        """
        async () => {
          document.querySelector('[data-theme-mode="dark"]').click();
          while (document.documentElement.dataset.theme !== 'dark') {
            await new Promise(resolve => requestAnimationFrame(resolve));
          }
          return {
            theme: document.documentElement.dataset.theme || '',
            transitioningClass: document.documentElement.classList.contains('theme-transitioning'),
            selectedCellBg: getComputedStyle(document.querySelector('#cleanup-files tbody tr.selected td')).backgroundColor,
            selectedCellColor: getComputedStyle(document.querySelector('#cleanup-files tbody tr.selected td')).color,
            activePresetBg: getComputedStyle(document.querySelector('[data-cleanup-retention-preset][aria-pressed="true"]')).backgroundColor,
            activePresetColor: getComputedStyle(document.querySelector('[data-cleanup-retention-preset][aria-pressed="true"]')).color,
            inactivePresetBg: getComputedStyle(document.querySelector('[data-cleanup-retention-preset][aria-pressed="false"]')).backgroundColor,
            activeNavBg: getComputedStyle(document.querySelector('.nav-btn.active')).backgroundColor,
            activeNavColor: getComputedStyle(document.querySelector('.nav-btn.active')).color,
          };
        }
        """
    )
    assert_true(
        selected_theme_state == {
            "theme": "dark",
            "transitioningClass": False,
            "selectedCellBg": "rgba(255, 255, 255, 0.07)",
            "selectedCellColor": "rgb(231, 231, 231)",
            "activePresetBg": "rgba(255, 255, 255, 0.08)",
            "activePresetColor": "rgb(230, 230, 230)",
            "inactivePresetBg": "rgb(24, 24, 24)",
            "activeNavBg": "rgb(33, 33, 33)",
            "activeNavColor": "rgb(231, 231, 231)",
        },
        f"selected and active cleanup controls should apply dark DOM colors immediately: {selected_theme_state}",
    )
    page.locator('[data-theme-mode="light"]').click()
    page.wait_for_timeout(180)


def check_cleanup_all_preset(page, cleanup: dict) -> None:
    assert_true(page.locator("#cleanup-delete-all").count() == 0, "cleanup should expose all-delete through the ALL preset, not a separate button")
    page.locator('[data-cleanup-retention-preset="all"]').click()
    page.wait_for_function(
        "() => document.querySelector('[data-cleanup-retention-preset=\"all\"]').getAttribute('aria-pressed') === 'true'",
        timeout=5_000,
    )
    page.wait_for_function(
        "() => document.querySelector('#cleanup-selected-count').textContent === 'all logs'",
        timeout=5_000,
    )
    all_cleanup_summary = cleanup_row_summaries(page)
    all_summary = page.evaluate(
        """
        () => ({
          label: document.querySelector('#cleanup-selected-label').textContent,
          value: document.querySelector('#cleanup-selected-bytes').textContent,
          files: document.querySelector('#cleanup-retention-files').textContent,
          cutoff: document.querySelector('#cleanup-selected-count').textContent,
          dateDisabled: document.querySelector('#cleanup-retention-date').disabled,
          dateValue: document.querySelector('#cleanup-retention-date').value,
        })
        """
    )
    all_expected = expected_cleanup_rows(page, cleanup["rows"], all_mode=True)
    all_expected_totals = expected_cleanup_totals(page, cleanup["rows"])
    assert_true(all_summary["label"] == "Segment Rows", f"ALL cleanup should summarize segment rows: {all_summary}")
    assert_true(all_summary["value"] == all_expected_totals["rawSegmentRows"], f"ALL cleanup top rows should match raw segment rows: {all_summary} vs {all_expected_totals}")
    assert_true(all_summary["files"] == all_expected_totals["affectedFiles"], f"ALL cleanup top files should match impact-table totals: {all_summary} vs {all_expected_totals}")
    assert_true("rows" in all_summary["value"], f"ALL cleanup summary should report rows, not bytes: {all_summary}")
    assert_true(all_summary["cutoff"] == "all logs", f"ALL cleanup should still identify full-log scope: {all_summary}")
    assert_true(all_summary["dateDisabled"] is False, f"ALL cleanup should keep date picker usable: {all_summary}")
    assert_true(all_summary["dateValue"] == "", f"ALL cleanup should show no selected cutoff date until the user picks one: {all_summary}")
    assert_true(not any(unit in all_summary["value"] for unit in ["B", "KB", "MB", "GB"]), f"ALL cleanup top summary should not switch to size units: {all_summary}")
    assert_cleanup_rows_match(all_cleanup_summary, all_expected, groups=("state_files",), context="ALL cleanup")
    derived_delete_rows = [
        row for row in all_cleanup_summary["rows"]
        if row["groupId"] in {"normalized_outputs", "analytics_database"}
        and any(label in row["affectedFiles"] for label in ("Rebuild", "Rewrite"))
    ]
    assert_true(
        all(row["affectedSize"] != "0 B" for row in derived_delete_rows),
        f"ALL cleanup should show affected size for derived files that are actually changed: {derived_delete_rows}",
    )


def check_cleanup_retention_preset(page, base_url: str) -> None:
    page.locator("#cleanup-retention-date").fill("2027-01-01")
    page.locator("#cleanup-retention-date").dispatch_event("change")
    page.wait_for_function(
        """
        () => document.querySelector('[data-cleanup-retention-preset="all"]').getAttribute('aria-pressed') === 'false'
          && document.querySelector('#cleanup-retention-custom-state').hidden === false
          && document.querySelector('#cleanup-selected-count').textContent !== 'all logs'
        """,
        timeout=10_000,
    )
    page.locator('[data-cleanup-retention-preset="7"]').click()
    page.wait_for_function(
        "() => document.querySelector('[data-cleanup-retention-preset=\"7\"]').getAttribute('aria-pressed') === 'true'",
        timeout=5_000,
    )
    page.wait_for_function(
        "() => document.querySelector('#cleanup-selected-count').textContent !== 'all logs'",
        timeout=5_000,
    )
    page.wait_for_function(
        """
        () => document.querySelector('#cleanup-retention-date').value !== '2027-01-01'
          && document.querySelector('#cleanup-retention-custom-state').hidden === true
          && (document.querySelector('#cleanup-selected-count').textContent || '').startsWith(document.querySelector('#cleanup-retention-date').value)
        """,
        timeout=10_000,
    )
    retention_cleanup_summary = cleanup_row_summaries(page)
    retention_cutoff_date = page.locator("#cleanup-retention-date").input_value()
    retention_cleanup = fetch_json(f"{base_url}/api/log-cleanup?cutoff_date={retention_cutoff_date}")
    assert_cleanup_payload_contract(retention_cleanup, context="retention cleanup")
    retention_expected = expected_cleanup_rows(page, retention_cleanup["rows"], all_mode=False)
    retention_derived_delete_rows = [
        row for row in retention_cleanup_summary["rows"]
        if row["groupId"] in {"normalized_outputs", "analytics_database"}
        and any(label in row["affectedFiles"] for label in ("Rebuild", "Rewrite"))
    ]
    assert_true(
        all(row["affectedSize"] != "0 B" for row in retention_derived_delete_rows),
        f"retention cleanup should show affected size for derived files reset during rebuild: {retention_derived_delete_rows}",
    )
    assert_cleanup_rows_match(retention_cleanup_summary, retention_expected, context="retention cleanup")


def check_cleanup_preview_race(page, base_url: str) -> None:
    pending_routes = []

    def hold_cleanup_preview(route) -> None:
        pending_routes.append(route)

    def wait_for_pending_routes(count: int) -> None:
        for _ in range(100):
            if len(pending_routes) >= count:
                return
            page.wait_for_timeout(50)
        assert_true(False, f"expected {count} held cleanup preview requests, got {len(pending_routes)}")

    page.route("**/api/log-cleanup?*", hold_cleanup_preview)
    try:
        delete_button = page.locator("#cleanup-delete")
        assert_true(not delete_button.is_disabled(), "delete action should start enabled before preview race check")
        page.locator('[data-cleanup-retention-preset="14"]').click()
        wait_for_pending_routes(1)
        assert_true(delete_button.is_disabled(), "delete action should disable while cleanup preview is loading")
        page.locator('[data-cleanup-retention-preset="30"]').click()
        wait_for_pending_routes(2)
        assert_true(delete_button.is_disabled(), "delete action should remain disabled while replacement cleanup preview is loading")

        stale_payload = fetch_json(f"{base_url}/api/log-cleanup?cutoff_date=2000-01-01")
        pending_routes[0].fulfill(status=200, content_type="application/json", body=json.dumps(stale_payload))
        page.wait_for_timeout(100)
        assert_true(delete_button.is_disabled(), "stale cleanup preview response should not re-enable delete action")

        current_cutoff = page.locator("#cleanup-retention-date").input_value()
        current_payload = fetch_json(f"{base_url}/api/log-cleanup?cutoff_date={urllib.parse.quote(current_cutoff)}")
        pending_routes[1].fulfill(status=200, content_type="application/json", body=json.dumps(current_payload))
        page.wait_for_function("() => !document.querySelector('#cleanup-delete').disabled", timeout=10_000)
        selected_count = page.locator("#cleanup-selected-count").text_content() or ""
        assert_true(
            selected_count.startswith(current_cutoff) or selected_count == "cutoff unavailable",
            f"latest cleanup preview should commit after stale response is ignored: cutoff={current_cutoff!r}, selected={selected_count!r}",
        )
    finally:
        page.unroute("**/api/log-cleanup?*", hold_cleanup_preview)
    page.locator('[data-cleanup-retention-preset="7"]').click()
    page.wait_for_function(
        "() => document.querySelector('[data-cleanup-retention-preset=\"7\"]').getAttribute('aria-pressed') === 'true' && !document.querySelector('#cleanup-delete').disabled",
        timeout=10_000,
    )


def check_cleanup_layout(page) -> None:
    cleanup_fit = page.evaluate(
        """
        () => {
          const files = document.querySelector('#cleanup-files').getBoundingClientRect();
          const filesNode = document.querySelector('#cleanup-files');
          const scroller = filesNode.querySelector('.table-scroll');
          const table = document.querySelector('#cleanup-files table').getBoundingClientRect();
          const layout = document.querySelector('.cleanup-layout').getBoundingClientRect();
          const view = document.querySelector('.view.active').getBoundingClientRect();
          const workbench = document.querySelector('.cleanup-workbench-panel').getBoundingClientRect();
          const body = document.querySelector('.cleanup-action-body').getBoundingClientRect();
          const summary = document.querySelector('.cleanup-selection-summary').getBoundingClientRect();
          const form = document.querySelector('.cleanup-retention-form').getBoundingClientRect();
          const row = document.querySelector('#cleanup-files tbody tr').getBoundingClientRect();
          const rowList = Array.from(document.querySelectorAll('#cleanup-files tbody tr'));
          const lastRow = rowList[rowList.length - 1].getBoundingClientRect();
          const style = getComputedStyle(document.querySelector('#cleanup-files'));
          return {
            emptySpace: Math.round((files.height - table.height) * 1000) / 1000,
            workbenchHeight: Math.round(workbench.height * 1000) / 1000,
            rowHeight: Math.round(row.height * 1000) / 1000,
            lastRowBottom: Math.round(lastRow.bottom * 1000) / 1000,
            viewportHeight: window.innerHeight,
            layoutBottomDelta: Math.round(Math.abs(layout.bottom - view.bottom) * 1000) / 1000,
            summaryHeight: Math.round(summary.height * 1000) / 1000,
            formHeight: Math.round(form.height * 1000) / 1000,
            formBottomDelta: Math.round((workbench.bottom - form.bottom) * 1000) / 1000,
            actionBodyDisplay: getComputedStyle(document.querySelector('.cleanup-action-body')).display,
            statCount: document.querySelectorAll('.cleanup-retention-stat').length,
            summaryColumns: getComputedStyle(document.querySelector('.cleanup-selection-summary')).gridTemplateColumns.split(' ').length,
            overflowY: style.overflowY,
            tableOverflowX: getComputedStyle(scroller).overflowX,
            paddingBottom: style.paddingBottom,
            canScroll: filesNode.scrollHeight - filesNode.clientHeight > 1,
            tableCanScrollX: scroller.scrollWidth - scroller.clientWidth > 1,
            hasScrollFade: document.querySelector('#cleanup-files').classList.contains('scroll-fade-target'),
          };
        }
        """
    )
    assert_true(cleanup_fit["emptySpace"] <= 2, f"cleanup files should fit table content tightly: {cleanup_fit}")
    assert_true(cleanup_fit["layoutBottomDelta"] <= 1, f"cleanup page should fill the same content height as other pages: {cleanup_fit}")
    assert_true(cleanup_fit["workbenchHeight"] >= 250, f"log retention panel should absorb remaining height dynamically: {cleanup_fit}")
    assert_true(cleanup_fit["actionBodyDisplay"] == "flex", f"log retention body should distribute height with flex: {cleanup_fit}")
    assert_true(cleanup_fit["summaryColumns"] == 2 and cleanup_fit["statCount"] == 2, f"log retention summary should use a split information layout: {cleanup_fit}")
    assert_true(cleanup_fit["summaryHeight"] >= 90, f"log retention summary should keep enough readable space: {cleanup_fit}")
    assert_true(0 <= cleanup_fit["formBottomDelta"] <= 2, f"log retention controls should sit at the panel bottom: {cleanup_fit}")
    assert_true(cleanup_fit["rowHeight"] >= 37, f"managed file rows should remain readable after state rows are split: {cleanup_fit}")
    assert_true(cleanup_fit["overflowY"] == "hidden", f"cleanup files should clip table overflow at the panel edge: {cleanup_fit}")
    assert_true(cleanup_fit["tableOverflowX"] == "auto", f"cleanup table should own horizontal scrolling: {cleanup_fit}")
    assert_true(cleanup_fit["paddingBottom"] == "0px", f"cleanup files should not keep scroll padding: {cleanup_fit}")
    assert_true(not cleanup_fit["canScroll"], f"cleanup files should show all rows without scrolling: {cleanup_fit}")
    assert_true(not cleanup_fit["hasScrollFade"], f"cleanup files should not use scroll fade state: {cleanup_fit}")
    assert_true(
        cleanup_fit["lastRowBottom"] <= cleanup_fit["viewportHeight"],
        f"cleanup files should keep all rows visible in the viewport: {cleanup_fit}",
    )
    assert_true(page.locator("#cleanup-selected-bytes").is_visible(), "cleanup selected bytes summary is not visible")
    assert_true(page.locator("#cleanup-selected-count").is_visible(), "cleanup selected count summary is not visible")
    assert_true(page.locator("#cleanup-delete").is_visible(), "cleanup delete action is not visible")
    assert_true(page.locator(".cleanup-detail-panel").count() == 0, "cleanup file detail should not be a fixed side panel")
    assert_true(page.locator("#cleanup-detail-modal").count() == 1, "cleanup detail modal should be mounted")


def check_cleanup_detail_modal(page, base_url: str) -> None:
    cleanup_payload = fetch_json(f"{base_url}/api/log-cleanup")
    preview_signature = str(((cleanup_payload.get("retention") or {}).get("selected") or {}).get("preview_signature") or "")
    assert_true(preview_signature, f"cleanup detail check requires preview signature: {cleanup_payload}")
    detail_by_group = {}
    populated_group_id = ""
    empty_group_id = ""
    for row in cleanup_payload.get("rows", []):
        group_id = str(row.get("group_id") or "")
        if not group_id:
            continue
        detail_query = urllib.parse.urlencode({"group_id": group_id, "preview_signature": preview_signature})
        detail = fetch_json(f"{base_url}/api/log-cleanup/detail?{detail_query}")
        assert_cleanup_detail_payload_contract(detail, group_id=group_id, context=f"cleanup detail API {group_id}")
        detail_by_group[group_id] = detail
        display = (detail.get("row") or {}).get("display") or {}
        if cleanup_detail_display_has_files(display):
            populated_group_id = populated_group_id or group_id
        else:
            empty_group_id = empty_group_id or group_id
    assert_true(populated_group_id, f"cleanup detail modal check requires a non-empty detail row: {cleanup_payload}")
    cleanup_detail_group_id = populated_group_id
    cleanup_detail = detail_by_group[cleanup_detail_group_id]
    page.locator(f'#cleanup-files tr[data-cleanup-file="{cleanup_detail_group_id}"]').dblclick()
    page.wait_for_selector("#cleanup-detail-modal.open", timeout=5_000)
    loading_footer_focusables = page.locator("#cleanup-detail-modal").evaluate(
        """
        (modal) => {
          const footer = modal.querySelector('.cleanup-detail-loading-footer');
          if (!footer) return 0;
          return footer.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])').length;
        }
        """
    )
    assert_true(loading_footer_focusables == 0, f"cleanup detail loading footer should not expose hidden focus targets: {loading_footer_focusables}")
    page.wait_for_selector("#cleanup-detail-modal-body .cleanup-affected-file-pager", timeout=10_000)
    cleanup_row = next((row for row in cleanup_payload.get("rows", []) if str(row.get("group_id") or "") == cleanup_detail_group_id), None)
    assert_true(isinstance(cleanup_row, dict), f"cleanup detail check requires parent row {cleanup_detail_group_id!r}: {cleanup_payload}")
    detail_row = cleanup_detail["row"]
    assert_true(str(detail_row.get("label") or "") == str(cleanup_row.get("label") or ""), f"cleanup detail row should identify the same file group: {detail_row} vs {cleanup_row}")
    detail_text = page.locator("#cleanup-detail-modal-body").text_content() or ""
    assert_true("Affected Files" in detail_text, f"cleanup file detail did not render file ledger: {detail_text!r}")
    assert_true("Deletion Targets" not in detail_text and "Rebuild Inputs" not in detail_text, f"cleanup file detail should not switch header labels: {detail_text!r}")
    assert_true("Selected File Group" not in detail_text, f"cleanup file detail header should stay compact: {detail_text!r}")
    assert_true(page.locator(".cleanup-file-status").count() == 0, "cleanup file detail should not render an unclear status badge")
    cleanup_detail_modal = page.locator("#cleanup-detail-modal").evaluate(
        "(el) => { const dialog = el.querySelector('.turn-modal-dialog').getBoundingClientRect(); return {open: el.classList.contains('open'), ariaHidden: el.getAttribute('aria-hidden'), title: el.querySelector('#cleanup-detail-modal-title').textContent || '', hasSubtitle: !!el.querySelector('#cleanup-detail-modal-subtitle'), width: Math.round(dialog.width), height: Math.round(dialog.height)}; }"
    )
    assert_true(cleanup_detail_modal["open"] and cleanup_detail_modal["ariaHidden"] == "false", f"cleanup modal should open on double-click: {cleanup_detail_modal}")
    assert_true(
        cleanup_detail_modal["title"] == "File Detail"
        and not cleanup_detail_modal["hasSubtitle"],
        f"cleanup modal title should not render a separate subtitle: {cleanup_detail_modal}",
    )
    assert_true(cleanup_detail_modal["width"] == 1040 and cleanup_detail_modal["height"] == 720, f"cleanup modal should use fixed desktop dimensions: {cleanup_detail_modal}")
    cleanup_detail_meta = page.locator("#cleanup-detail-modal-body .cleanup-detail-meta").evaluate(
        "(el) => ({label: (el.querySelector('span:first-child')?.textContent || '').trim(), value: (el.querySelector('.cleanup-detail-meta-value')?.textContent || '').trim()})"
    )
    cleanup_detail_description = page.locator("#cleanup-detail-modal-body .cleanup-detail-description").evaluate(
        "(el) => { const name = el.querySelector('.cleanup-detail-description-name'); const copy = el.querySelector('.cleanup-detail-description-copy'); return {text: (el.textContent || '').trim(), name: (name?.textContent || '').trim(), copy: (copy?.textContent || '').trim(), nameColor: name ? getComputedStyle(name).color : '', copyColor: copy ? getComputedStyle(copy).color : ''}; }"
    )
    cleanup_detail_style = page.locator("#cleanup-detail-modal-body .cleanup-detail").evaluate(
        "(el) => { const hero = el.querySelector('.cleanup-detail-hero'); const summary = el.querySelector('.cleanup-detail-summary'); const ledger = el.querySelector('.cleanup-affected-file-ledger'); const header = el.querySelector('.cleanup-affected-file-header'); const list = el.querySelector('.cleanup-affected-file-list'); const columns = el.querySelector('.cleanup-affected-file-columns'); const pager = el.querySelector('.cleanup-affected-file-pager'); const firstRow = el.querySelector('.cleanup-affected-file-row'); const rows = Array.from(el.querySelectorAll('.cleanup-affected-file-row')); const lastRow = rows.length ? rows[rows.length - 1] : null; const rowMain = firstRow ? firstRow.querySelector('.cleanup-affected-file-row-main') : null; const fileName = firstRow ? firstRow.querySelector('.cleanup-affected-file-name') : null; const kind = firstRow ? firstRow.querySelector('.cleanup-affected-file-kind') : null; const path = firstRow ? firstRow.querySelector('.cleanup-affected-file-path') : el.querySelector('.cleanup-affected-file-path'); const number = firstRow ? firstRow.querySelector('.cleanup-affected-file-number') : el.querySelector('.cleanup-affected-file-number'); const fileRect = fileName ? fileName.getBoundingClientRect() : null; const numberRect = number ? number.getBoundingClientRect() : null; const pathRect = path ? path.getBoundingClientRect() : null; const fileText = fileName ? fileName.textContent || '' : ''; const pathText = path ? path.textContent || '' : ''; const pathStyle = path ? getComputedStyle(path) : null; const actions = Array.from(el.querySelectorAll('.cleanup-affected-file-action')); const actionLefts = actions.map(node => Math.round(node.getBoundingClientRect().left)); const actionWidths = actions.map(node => Math.round(node.getBoundingClientRect().width)); const actionCenters = actions.map(node => { const rect = node.getBoundingClientRect(); return Math.round(rect.top + rect.height / 2); }); const rowCenters = rows.map(node => { const rect = node.getBoundingClientRect(); return Math.round(rect.top + rect.height / 2); }); const numberLefts = Array.from(el.querySelectorAll('.cleanup-affected-file-number')).map(node => Math.round(node.getBoundingClientRect().left)); const numberCenters = Array.from(el.querySelectorAll('.cleanup-affected-file-number')).map(node => { const rect = node.getBoundingClientRect(); return Math.round(rect.top + rect.height / 2); }); const summaryIconCenters = Array.from(el.querySelectorAll('.cleanup-detail-summary-icon')).map(node => { const rect = node.getBoundingClientRect(); return Math.round(rect.left + rect.width / 2); }); const summaryValueCenters = Array.from(el.querySelectorAll('.cleanup-detail-summary dd')).map(node => { const rect = node.getBoundingClientRect(); return Math.round(rect.left + rect.width / 2); }); return {hasToolSummary: !!el.querySelector('.tool-detail-grid'), hasToolName: !!el.querySelector('.tool-name-cell'), hasSectionTitle: !!el.querySelector('.tool-detail-section-title'), hasHero: !!hero, hasHeroGroup: !!el.querySelector('.cleanup-detail-group'), heroColumns: hero ? getComputedStyle(hero).gridTemplateColumns.split(' ').length : 0, hasSummary: !!summary, summaryItems: el.querySelectorAll('.cleanup-detail-summary div').length, summaryLabels: Array.from(el.querySelectorAll('.cleanup-detail-summary dt')).map(node => node.getAttribute('aria-label') || ''), summaryValues: Array.from(el.querySelectorAll('.cleanup-detail-summary dd')).map(node => node.textContent || ''), summaryIconCount: el.querySelectorAll('.cleanup-detail-summary-icon svg').length, summaryIconCenters, summaryValueCenters, hasContext: !!el.querySelector('.cleanup-detail-context'), hasLedger: !!ledger, hasHeader: !!header, hasColumns: !!columns, columnLabels: columns ? columns.textContent || '' : '', rowDisplay: firstRow ? getComputedStyle(firstRow).display : '', rowMainDisplay: rowMain ? getComputedStyle(rowMain).display : '', rowMainColumns: rowMain ? getComputedStyle(rowMain).gridTemplateColumns : '', rowMainGap: rowMain ? getComputedStyle(rowMain).columnGap : '', affectedFileSummary: (el.querySelector('.cleanup-affected-file-title') || {}).textContent || '', hasAffectedFileList: !!list, hasLedgerHead: !!el.querySelector('.cleanup-affected-file-list-head'), hasPager: !!pager, pagerHidden: pager ? pager.hidden : false, pagerText: pager ? pager.textContent || '' : '', pagerButtons: pager ? pager.querySelectorAll('button').length : 0, affectedFileItems: rows.length, affectedFileItemHeights: rows.map(node => Math.round(node.getBoundingClientRect().height)), affectedFileLastBorderBottom: lastRow ? getComputedStyle(lastRow).borderBottomWidth : '', actionLefts, actionWidths, actionCenters, actionLabels: actions.map(node => node.getAttribute('aria-label') || ''), actionTexts: actions.map(node => (node.textContent || '').trim()), actionIconCount: el.querySelectorAll('.cleanup-affected-file-action-icon svg').length, rowCenters, numberLefts, numberCenters, fileNameTexts: Array.from(el.querySelectorAll('.cleanup-affected-file-name')).map(node => node.textContent || ''), pathTexts: Array.from(el.querySelectorAll('.cleanup-affected-file-path')).map(node => node.textContent || ''), firstFileName: fileText, firstPath: pathText, pathWhiteSpace: pathStyle ? pathStyle.whiteSpace : '', pathLineClamp: pathStyle ? pathStyle.webkitLineClamp : '', pathWrap: pathStyle ? pathStyle.overflowWrap : '', numberAlign: number ? getComputedStyle(number).textAlign : '', fileNumberGap: fileRect && numberRect ? Math.round(numberRect.left - fileRect.right) : null, numberPathGap: numberRect && pathRect ? Math.round(pathRect.left - numberRect.right) : null, hasFileNameLine: !!el.querySelector('.cleanup-affected-file-name'), hasOldFileNameLine: !!el.querySelector('.cleanup-affected-file'), hasKind: !!kind, factItems: el.querySelectorAll('.cleanup-affected-file-fact').length, hasTable: !!el.querySelector('table'), hasSummaryFacts: !!el.querySelector('.cleanup-file-fact'), hasImpact: !!el.querySelector('.cleanup-file-impact'), hasSourceCards: !!el.querySelector('.cleanup-file-source-item'), hasProgress: !!el.querySelector('.cleanup-affected-file-progress')}; }"
    )
    assert_true(not cleanup_detail_style["hasToolSummary"] and not cleanup_detail_style["hasToolName"], f"cleanup file detail should not reuse Tool Detail summary structure: {cleanup_detail_style}")
    assert_true(not cleanup_detail_style["hasSectionTitle"] and cleanup_detail_style["hasHero"] and cleanup_detail_style["hasSummary"], f"cleanup file detail should use cleanup-specific hero summary: {cleanup_detail_style}")
    assert_true(cleanup_detail_style["heroColumns"] == 2 and cleanup_detail_style["summaryItems"] == 4 and not cleanup_detail_style["hasContext"], f"cleanup detail hero should summarize without extra context rows: {cleanup_detail_style}")
    assert_true(not cleanup_detail_style["hasHeroGroup"], f"cleanup detail hero should not repeat the group title: {cleanup_detail_style}")
    assert_true(cleanup_detail_style["summaryLabels"] == ["Total Files", "Delete Files", "Rebuild Files", "Rewrite Files"], f"cleanup detail summary icons should expose file-specific hover labels: {cleanup_detail_style}")
    assert_true(cleanup_detail_style["summaryIconCount"] == 4, f"cleanup detail summary labels should render as icons: {cleanup_detail_style}")
    assert_true(
        cleanup_detail_description["name"]
        and cleanup_detail_description["copy"]
        and " - " not in cleanup_detail_description["text"]
        and f"{cleanup_detail_description['name']} {cleanup_detail_description['copy']}" == cleanup_detail_description["text"]
        and cleanup_detail_description["nameColor"] != cleanup_detail_description["copyColor"],
        f"cleanup detail description should visually separate the group name from the explanation: {cleanup_detail_description}",
    )
    assert_true(
        cleanup_detail_style["summaryIconCenters"] == cleanup_detail_style["summaryValueCenters"],
        f"cleanup detail summary icons should align to value centers: {cleanup_detail_style}",
    )
    assert_true(cleanup_detail_meta["label"] == "Delete before", f"cleanup detail cutoff should sit in the identity meta line: {cleanup_detail_meta!r}")
    delete_before_value = cleanup_detail_meta["value"]
    assert_true(
        delete_before_value == "all logs"
        or delete_before_value == "cutoff unavailable"
        or (len(delete_before_value) == 10 and delete_before_value.count("-") == 2),
        f"cleanup detail Delete Before should show date only: {cleanup_detail_style}",
    )
    assert_true(cleanup_detail_style["hasLedger"] and cleanup_detail_style["hasHeader"], f"cleanup detail should render a deletion target ledger: {cleanup_detail_style}")
    assert_true(cleanup_detail_style["hasPager"] and not cleanup_detail_style["pagerHidden"], f"cleanup detail should always show footer pagination: {cleanup_detail_style}")
    assert_true(" · " not in cleanup_detail_style["affectedFileSummary"] and "row" not in cleanup_detail_style["affectedFileSummary"], f"cleanup affected file header should not synthesize row scope: {cleanup_detail_style}")
    assert_true(
        cleanup_detail_style["affectedFileSummary"].startswith("Affected Files"),
        f"cleanup affected file header should use a stable file ledger label: {cleanup_detail_style}",
    )
    if cleanup_detail_group_id == "archived_raw_logs":
        empty_text = page.locator("#cleanup-detail-modal-body .cleanup-affected-file-empty").text_content() or ""
        assert_true(cleanup_detail_style["summaryValues"][0] == "0", f"archived raw logs should keep zero file summary: {cleanup_detail_style}")
        assert_true(cleanup_detail_style["affectedFileItems"] == 0, f"archived raw logs should not synthesize the archive directory as a target row: {cleanup_detail_style}")
        assert_true(cleanup_detail_style["affectedFileSummary"] == "Affected Files", f"archived raw logs should keep a compact ledger header: {cleanup_detail_style}")
        assert_true(empty_text == "No affected files.", f"archived raw logs should render a compact empty ledger: {empty_text!r}")
    if cleanup_detail_style["affectedFileItems"] == 0:
        assert_true(
            cleanup_detail_style["affectedFileSummary"] == "Affected Files"
            and not cleanup_detail_style["hasColumns"] and "0-0 / 0" in cleanup_detail_style["pagerText"],
            f"empty cleanup detail should keep a compact empty ledger: {cleanup_detail_style}",
        )
    else:
        assert_true(
            len(cleanup_detail_style["actionLabels"]) == cleanup_detail_style["affectedFileItems"]
            and cleanup_detail_style["actionIconCount"] == cleanup_detail_style["affectedFileItems"]
            and all(label in CLEANUP_ALLOWED_ACTIONS for label in cleanup_detail_style["actionLabels"])
            and all(text == "" for text in cleanup_detail_style["actionTexts"]),
            f"cleanup affected-file rows should show icon-only actions with accessible labels: {cleanup_detail_style}",
        )
        assert_true(cleanup_detail_style["affectedFileItems"] <= 25, f"cleanup affected-file modal should paginate long affected-file lists: {cleanup_detail_style}")
        assert_true(" / " in cleanup_detail_style["pagerText"] and cleanup_detail_style["pagerButtons"] == 2, f"cleanup affected-file pager should summarize the active file slice: {cleanup_detail_style}")
        assert_true(not cleanup_detail_style["hasColumns"] and cleanup_detail_style["rowDisplay"] == "flex" and cleanup_detail_style["rowMainDisplay"] == "grid", f"cleanup affected-file rows should follow the compact fixed-column layout: {cleanup_detail_style}")
        assert_true("72px" in cleanup_detail_style["rowMainColumns"], f"cleanup affected-file action column should stay fixed: {cleanup_detail_style}")
        assert_true(cleanup_detail_style["rowMainGap"] == "28px", f"cleanup affected-file rows should keep enough gap before actions: {cleanup_detail_style}")
        assert_true(cleanup_detail_style["affectedFileLastBorderBottom"] == "0px", f"cleanup affected-file list should not draw a double bottom border: {cleanup_detail_style}")
        assert_true(max(cleanup_detail_style["actionLefts"] or [0]) - min(cleanup_detail_style["actionLefts"] or [0]) <= 1, f"cleanup affected-file actions should align to a fixed column: {cleanup_detail_style}")
        assert_true(
            cleanup_detail_style["actionCenters"]
            and cleanup_detail_style["rowCenters"]
            and max(abs(action_center - row_center) for action_center, row_center in zip(cleanup_detail_style["actionCenters"], cleanup_detail_style["rowCenters"])) <= 1,
            f"cleanup affected-file actions should be vertically centered in each row: {cleanup_detail_style}",
        )
        assert_true(all(width == 72 for width in cleanup_detail_style["actionWidths"]), f"cleanup affected-file actions should keep a fixed width: {cleanup_detail_style}")
        assert_true(cleanup_detail_style["hasAffectedFileList"] and not cleanup_detail_style["hasLedgerHead"] and cleanup_detail_style["affectedFileItems"] >= 1, f"cleanup file detail should use a readable target ledger layout: {cleanup_detail_style}")
        assert_true(
            min(cleanup_detail_style["affectedFileItemHeights"] or [85]) == 85
            and max(cleanup_detail_style["affectedFileItemHeights"] or [85]) == 85,
            f"cleanup affected-file rows should keep a fixed two-line path height regardless of path length: {cleanup_detail_style}",
        )
        assert_true(cleanup_detail_style["pathWhiteSpace"] == "normal" and cleanup_detail_style["pathLineClamp"] == "2", f"cleanup affected-file ledger should clamp paths to two lines: {cleanup_detail_style}")
        assert_true(cleanup_detail_style["hasFileNameLine"] and cleanup_detail_style["hasKind"] and not cleanup_detail_style["hasOldFileNameLine"], f"cleanup affected-file rows should separate file names from directories: {cleanup_detail_style}")
        assert_true(len(cleanup_detail_style["pathTexts"]) >= 1 and cleanup_detail_style["pathTexts"][0].startswith("/") and cleanup_detail_style["firstFileName"] not in cleanup_detail_style["firstPath"] and "/" not in cleanup_detail_style["firstFileName"], f"cleanup affected-file rows should expose directory path without duplicating the filename: {cleanup_detail_style}")
    assert_true(cleanup_detail_style["factItems"] == 0 and not cleanup_detail_style["hasProgress"], f"cleanup affected-file rows should avoid secondary fact clutter: {cleanup_detail_style}")
    assert_true(not cleanup_detail_style["hasTable"], f"cleanup file detail target list should not reuse table layout: {cleanup_detail_style}")
    assert_true(not cleanup_detail_style["hasSourceCards"], f"cleanup file detail should not use bespoke source cards: {cleanup_detail_style}")
    assert_true(not cleanup_detail_style["hasSummaryFacts"], f"cleanup file detail should not repeat table summary facts: {cleanup_detail_style}")
    assert_true(not cleanup_detail_style["hasImpact"], f"cleanup file detail should not repeat the table deletion impact: {cleanup_detail_style}")
    page.locator("#cleanup-detail-modal-close").click()
    page.wait_for_selector("#cleanup-detail-modal.open", state="detached", timeout=5_000)
    keyboard_row = page.locator(f'#cleanup-files tr[data-cleanup-file="{cleanup_detail_group_id}"]').first
    keyboard_row.focus()
    page.keyboard.press("Space")
    page.wait_for_selector("#cleanup-detail-modal.open", timeout=5_000)
    page.wait_for_selector("#cleanup-detail-modal-body .cleanup-affected-file-pager", timeout=10_000)
    page.locator("#cleanup-detail-modal-close").click()
    page.wait_for_selector("#cleanup-detail-modal.open", state="detached", timeout=5_000)
    if empty_group_id and empty_group_id != cleanup_detail_group_id:
        page.locator(f'#cleanup-files tr[data-cleanup-file="{empty_group_id}"]').dblclick()
        page.wait_for_selector("#cleanup-detail-modal.open", timeout=5_000)
        page.wait_for_selector("#cleanup-detail-modal-body .cleanup-affected-file-pager", timeout=10_000)
        empty_detail_style = page.locator("#cleanup-detail-modal-body .cleanup-detail").evaluate(
            "(el) => { const pager = el.querySelector('.cleanup-affected-file-pager'); return {summaryValues: Array.from(el.querySelectorAll('.cleanup-detail-summary dd')).map(node => node.textContent || ''), affectedFileSummary: (el.querySelector('.cleanup-affected-file-title') || {}).textContent || '', affectedFileItems: el.querySelectorAll('.cleanup-affected-file-row').length, hasColumns: !!el.querySelector('.cleanup-affected-file-columns'), pagerText: pager ? pager.textContent || '' : '', emptyText: (el.querySelector('.cleanup-affected-file-empty') || {}).textContent || ''}; }"
        )
        assert_true(
            empty_detail_style["summaryValues"][0] == "0"
            and empty_detail_style["affectedFileItems"] == 0
            and empty_detail_style["affectedFileSummary"] == "Affected Files"
            and not empty_detail_style["hasColumns"]
            and "0-0 / 0" in empty_detail_style["pagerText"]
            and empty_detail_style["emptyText"] == "No affected files.",
            f"empty cleanup detail should keep a compact empty ledger: {empty_detail_style}",
        )
        page.locator("#cleanup-detail-modal-close").click()
        page.wait_for_selector("#cleanup-detail-modal.open", state="detached", timeout=5_000)


def check_cleanup_retired_rows_and_controls(page) -> None:
    assert_true(
        page.locator('#cleanup-files tr[data-cleanup-file="normalized_model_calls"]').count() == 0,
        "cleanup should not expose retired normalized model-call log rows",
    )
    assert_true(
        page.locator('#cleanup-files tr[data-cleanup-file="raw_model_calls"]').count() == 0,
        "cleanup should not expose retired raw model-call log rows",
    )
    assert_true(page.locator('#cleanup-files tr[data-cleanup-file="reports"]').count() == 0, "cleanup should not expose unused reports row")
    assert_true(page.locator('[data-cleanup-retention-preset="7"]').get_attribute("aria-pressed") == "true", "7 day preset should be active by default")
    assert_true(page.locator('[data-cleanup-retention-preset="1"]').count() == 1, "1 day retention preset should be available")
    assert_true(page.locator('[data-cleanup-retention-preset="all"]').count() == 1, "ALL retention preset should be available")
    assert_true(page.locator("#cleanup-retention-date").input_value(), "retention date should default to a concrete date")
    retention_rows = page.locator("#cleanup-selected-bytes").text_content() or ""
    assert_true("row" in retention_rows, f"retention summary should report raw segment rows: {retention_rows!r}")
    assert_true(page.locator('[data-cleanup-retention-preset="custom"]').count() == 0, "cleanup should not render a Custom preset button")
    assert_true(page.locator("#cleanup-retention-custom").is_visible(), "cleanup date field should be visible by default")
    cleanup_control_display = page.locator(".cleanup-control-row").evaluate("(el) => getComputedStyle(el).display")
    assert_true(cleanup_control_display == "flex", f"cleanup controls should share one row: {cleanup_control_display}")
    cleanup_control_style = page.locator(".cleanup-retention-form").evaluate(
        "(el) => ({height: Math.round(el.getBoundingClientRect().height), borderBottom: getComputedStyle(el).borderBottomWidth})"
    )
    assert_true(cleanup_control_style["height"] >= 62, f"cleanup control row is too short: {cleanup_control_style}")
    assert_true(cleanup_control_style["borderBottom"] == "0px", f"cleanup control row should not create a second bottom border: {cleanup_control_style}")
    cleanup_empty_status = page.locator("#cleanup-action-status").evaluate(
        "(el) => ({height: Math.round(el.getBoundingClientRect().height), padding: getComputedStyle(el).padding})"
    )
    assert_true(cleanup_empty_status["height"] == 0, f"empty cleanup action status should not reserve space: {cleanup_empty_status}")
    assert_true(cleanup_empty_status["padding"] == "0px", f"empty cleanup action status should not keep padding: {cleanup_empty_status}")
    cleanup_inline_alignment = page.evaluate(
        """
        () => {
          const label = document.querySelector('.cleanup-retention-label').getBoundingClientRect();
          const firstPreset = document.querySelector('[data-cleanup-retention-preset="7"]').getBoundingClientRect();
          const refresh = document.querySelector('#cleanup-refresh').getBoundingClientRect();
          return Math.max(label.top, firstPreset.top, refresh.top) - Math.min(label.top, firstPreset.top, refresh.top);
        }
        """
    )
    assert_true(cleanup_inline_alignment <= 6, f"cleanup retention controls should stay on one row: {cleanup_inline_alignment}")
    cleanup_control_alignment = page.evaluate(
        """
        () => {
          const input = document.querySelector('#cleanup-retention-date').getBoundingClientRect();
          const refresh = document.querySelector('#cleanup-refresh').getBoundingClientRect();
          return Math.abs((input.top + input.height / 2) - (refresh.top + refresh.height / 2));
        }
        """
    )
    assert_true(cleanup_control_alignment <= 2, f"cleanup date and actions should be vertically centered: {cleanup_control_alignment}")


def check_cleanup_refresh_stability(page, delete_button) -> None:
    affected_size_before = page.locator("#cleanup-files .cleanup-affected-size-cell").all_text_contents()
    cleanup_widths_before = page.locator("#cleanup-files th").evaluate_all(
        "(cells) => cells.map(cell => Math.round(cell.getBoundingClientRect().width))"
    )
    page.locator("#cleanup-files tbody tr").first.evaluate("(el) => { el.dataset.renderProbe = 'stable'; }")
    page.locator("#cleanup-retention-date").fill("2027-01-01")
    page.locator("#cleanup-retention-date").dispatch_event("change")
    page.wait_for_function(
        """
        before => Array.from(document.querySelectorAll('#cleanup-files .cleanup-affected-size-cell'))
          .map(cell => cell.textContent || '')
          .join('|') !== before
        """,
        arg="|".join(affected_size_before),
        timeout=10_000,
    )
    retention_cutoff = page.locator("#cleanup-selected-count").text_content() or ""
    assert_true("20" in retention_cutoff or "cutoff unavailable" in retention_cutoff, f"retention cutoff did not render: {retention_cutoff!r}")
    assert_true(
        page.locator("#cleanup-files tbody tr").first.get_attribute("data-render-probe") == "stable",
        "retention changes should update existing managed-file rows instead of rebuilding the table",
    )
    affected_size_after = page.locator("#cleanup-files .cleanup-affected-size-cell").all_text_contents()
    assert_true(affected_size_after != affected_size_before, f"managed files affected sizes did not update: before={affected_size_before}, after={affected_size_after}")
    cleanup_widths_after = page.locator("#cleanup-files th").evaluate_all(
        "(cells) => cells.map(cell => Math.round(cell.getBoundingClientRect().width))"
    )
    assert_true(
        cleanup_widths_after == cleanup_widths_before,
        f"managed file column widths changed after value refresh: before={cleanup_widths_before}, after={cleanup_widths_after}",
    )
    assert_true(not delete_button.is_disabled(), "delete action should enable when selected cutoff has old rows")
