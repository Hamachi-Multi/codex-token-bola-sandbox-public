"""Mobile browser checks for the Codex Token Bola dashboard."""

from __future__ import annotations

from playwright_dashboard_helpers import assert_true


def check_mobile(page, base_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.locator('button[data-view-target="turns"]').click()
    page.wait_for_selector("#turn-list tr[data-turn]", timeout=10_000)

    mobile_state = page.evaluate(
        """
        () => {
          const doc = document.documentElement;
          const first = document.querySelector('#turn-list tr[data-turn]');
          const prompt = first ? first.querySelector('td:nth-child(3)') : null;
          const dateTimeCell = first ? first.querySelector('td:nth-child(1)') : null;
          const sessionCell = first ? first.querySelector('td:nth-child(2)') : null;
          const themeToggle = document.querySelector('.theme-toggle');
          const themeText = document.querySelector('[data-theme-mode="light"] .theme-toggle-text');
          return {
            scrollWidth: doc.scrollWidth,
            clientWidth: doc.clientWidth,
            themeInsetRight: themeToggle ? Math.round(window.innerWidth - themeToggle.getBoundingClientRect().right) : null,
            themeInsetBottom: themeToggle ? Math.round(window.innerHeight - themeToggle.getBoundingClientRect().bottom) : null,
            themeTextDisplay: themeText ? getComputedStyle(themeText).display : null,
            promptDisplay: prompt ? getComputedStyle(prompt).display : null,
            dateTimeDisplay: dateTimeCell ? getComputedStyle(dateTimeCell).display : null,
            sessionDisplay: sessionCell ? getComputedStyle(sessionCell).display : null,
          };
        }
        """
    )
    assert_true(
        mobile_state["scrollWidth"] <= mobile_state["clientWidth"] + 1,
        f"mobile page overflows horizontally: {mobile_state}",
    )
    assert_true(12 <= mobile_state["themeInsetRight"] <= 16, f"mobile theme toggle should sit near the page right edge: {mobile_state}")
    assert_true(12 <= mobile_state["themeInsetBottom"] <= 16, f"mobile theme toggle should sit near the page bottom edge: {mobile_state}")
    assert_true(mobile_state["themeTextDisplay"] == "none", f"mobile theme toggle should hide text labels: {mobile_state}")
    assert_true(mobile_state["promptDisplay"] != "none", f"mobile prompt cell hidden: {mobile_state}")
    assert_true(mobile_state["dateTimeDisplay"] == "none", f"mobile date-time column should collapse into row meta: {mobile_state}")
    assert_true(mobile_state["sessionDisplay"] == "none", f"mobile session column should collapse into row meta: {mobile_state}")

    page.locator('button[data-view-target="cleanup"]').click()
    page.wait_for_selector("#cleanup-files table", timeout=10_000)
    cleanup_mobile_state = page.evaluate(
        """
        () => {
          const doc = document.documentElement;
          const summary = document.querySelector('.cleanup-selection-summary');
          const form = document.querySelector('.cleanup-retention-form');
          const allOption = document.querySelector('.cleanup-all-option');
          const actions = document.querySelector('.cleanup-action-row');
          const presets = document.querySelector('.cleanup-retention-presets');
          const firstRow = document.querySelector('#cleanup-files tbody tr');
          return {
            scrollWidth: doc.scrollWidth,
            clientWidth: doc.clientWidth,
            summaryColumns: summary ? getComputedStyle(summary).gridTemplateColumns : '',
            formDisplay: form ? getComputedStyle(form).display : '',
            allOptionDisplay: allOption ? getComputedStyle(allOption).display : '',
            actionColumns: actions ? getComputedStyle(actions).gridTemplateColumns : '',
            presetOverflowX: presets ? getComputedStyle(presets).overflowX : '',
            firstRowWidth: firstRow ? Math.round(firstRow.getBoundingClientRect().width * 1000) / 1000 : 0,
          };
        }
        """
    )
    assert_true(
        cleanup_mobile_state["scrollWidth"] <= cleanup_mobile_state["clientWidth"] + 1,
        f"mobile cleanup overflows horizontally: {cleanup_mobile_state}",
    )
    assert_true(len(cleanup_mobile_state["summaryColumns"].split(" ")) == 1, f"mobile cleanup summary should be one column: {cleanup_mobile_state}")
    assert_true(cleanup_mobile_state["formDisplay"] == "grid", f"mobile cleanup controls should stack as grid: {cleanup_mobile_state}")
    assert_true(cleanup_mobile_state["allOptionDisplay"] == "", f"mobile cleanup should not expose a separate all-data control: {cleanup_mobile_state}")
    assert_true(" " in cleanup_mobile_state["actionColumns"], f"mobile cleanup actions should use two columns: {cleanup_mobile_state}")
    assert_true(cleanup_mobile_state["presetOverflowX"] in ("auto", "scroll"), f"mobile cleanup presets should be horizontally scrollable: {cleanup_mobile_state}")
