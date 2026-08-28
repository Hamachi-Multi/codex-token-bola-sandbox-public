"""Tools and subagent browser checks for the Codex Token Bola dashboard."""

from __future__ import annotations

from playwright_dashboard_helpers import (
    assert_true,
    fetch_json,
    scroll_bottom_state,
    wait_for_python_condition,
    wait_for_view_content,
)


def check_subagent_focus_refresh(page) -> None:
    pending_routes = []

    def hold_subagents(route) -> None:
        pending_routes.append(route)

    def wait_for_held_subagents() -> None:
        wait_for_python_condition(
            page,
            lambda: bool(pending_routes),
            description="a held subagents refresh request",
        )

    page.route("**/api/subagents?*", hold_subagents)
    try:
        page.locator("#refresh").click()
        wait_for_held_subagents()
        focused_row = page.locator("#subagent-rollups tr[data-confidence] .row-select-button").first
        old_button = focused_row.element_handle()
        focused_row.focus()
        pending_routes.pop(0).continue_()
        page.wait_for_function(
            """
            oldButton => {
              const active = document.activeElement;
              return !oldButton.isConnected
                && active?.isConnected
                && Boolean(active.closest('#subagent-rollups tr[data-confidence]'));
            }
            """,
            arg=old_button,
            timeout=10_000,
        )

        page.locator("#refresh").click()
        wait_for_held_subagents()
        old_button = page.locator("#subagent-rollups tr[data-confidence] .row-select-button").first.element_handle()
        page.locator("#days").focus()
        pending_routes.pop(0).continue_()
        page.wait_for_function(
            """
            oldButton => !oldButton.isConnected && document.activeElement === document.querySelector('#days')
            """,
            arg=old_button,
            timeout=10_000,
        )
    finally:
        for route in pending_routes:
            route.continue_()
        page.unroute("**/api/subagents?*", hold_subagents)


def check_tools_and_subagents(page, base_url: str) -> None:
    page.locator('button[data-view-target="overview"]').click()
    wait_for_view_content(
        page,
        view="overview",
        container="#projects",
        row="tr[data-session-id]",
        empty=".empty",
    )
    if page.locator("#projects tr[data-session-id]").count() > 0:
        page.locator('#projects [data-list-sort="raw"]').click()
        page.wait_for_selector('#projects th[aria-sort="descending"] [data-list-sort="raw"]', timeout=10_000)
        overview_sort_focus_restored = page.locator('#projects [data-list-sort="raw"]').evaluate(
            "button => document.activeElement === button"
        )
        assert_true(overview_sort_focus_restored, "overview sort should restore focus to the replacement header button")
        sessions = fetch_json(f"{base_url}/api/sessions?days=7&sessions_page=1&per_page=25&session_sort=raw&session_sort_dir=desc")
        first_session = page.locator("#projects tr[data-session-id]").first.get_attribute("data-session-id")
        assert_true(
            first_session == sessions["rows"][0]["session_id"],
            f"overview raw desc sort did not match API: {first_session!r} != {sessions['rows'][0]['session_id']!r}",
        )

    page.locator('button[data-view-target="subagents"]').click()
    wait_for_view_content(
        page,
        view="subagents",
        container="#subagent-rollups",
        row="tr[data-confidence]",
        empty=".empty",
        require_focus=True,
    )
    if page.locator("#subagent-rollups tr[data-confidence]").count() > 0:
        subagent_focus_index = page.locator("#subagent-rollups").evaluate(
            "() => Array.from(document.querySelectorAll('#subagent-rollups tr[data-confidence] .row-select-button')).indexOf(document.activeElement)"
        )
        assert_true(subagent_focus_index >= 0, f"subagents view should focus an attribution row on entry: {subagent_focus_index}")
        check_subagent_focus_refresh(page)
        page.locator('#subagent-rollups [data-list-sort="child_raw"]').click()
        page.wait_for_selector('#subagent-rollups th[aria-sort="descending"] [data-list-sort="child_raw"]', timeout=10_000)
        subagents = fetch_json(f"{base_url}/api/subagents?days=7&subagent_sort=child_raw&subagent_sort_dir=desc")
        first_confidence = page.locator("#subagent-rollups tr[data-confidence]").first.get_attribute("data-confidence")
        assert_true(
            first_confidence == subagents["rows"][0]["confidence"],
            f"subagents token desc sort did not match API: {first_confidence!r} != {subagents['rows'][0]['confidence']!r}",
        )
    subagent_head_heights = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('[data-view="subagents"] .panel > .panel-head'))
          .map((el) => Math.round(el.getBoundingClientRect().height * 1000) / 1000)
        """
    )
    assert_true(len(set(subagent_head_heights)) == 1, f"subagent panel headers are misaligned: {subagent_head_heights}")
    if page.locator("#subagent-rollups tr[data-confidence]").count() > 0:
        first_subagent = page.locator("#subagent-rollups tr[data-confidence]").first
        confidence = first_subagent.get_attribute("data-confidence") or ""
        subagent_detail = fetch_json(f"{base_url}/api/subagent?confidence={confidence}")
        subagent_detail_has_rows = bool((subagent_detail.get("sessions") or []) or (subagent_detail.get("rows") or []))
        first_subagent.click()
        page.wait_for_selector("#subagent-mix table, #subagent-mix .tool-detail-summary", timeout=10_000)
        if subagent_detail_has_rows:
            assert_true(page.locator("#subagent-mix table").count() > 0, "non-empty attribution detail should render tables")
            subagent_header_position = page.locator("#subagent-mix th").first.evaluate(
                "(el) => getComputedStyle(el).position"
            )
            assert_true(
                subagent_header_position != "sticky",
                f"attribution detail header should scroll normally: {subagent_header_position}",
            )
            attribution_bottom = scroll_bottom_state(page, "#subagent-mix")
            assert_true(
                attribution_bottom["remaining"] == 0,
                f"attribution detail did not reach scroll bottom: {attribution_bottom}",
            )
            assert_true(
                not attribution_bottom["canScrollDown"],
                f"attribution detail still reports scroll down at bottom: {attribution_bottom}",
            )
            assert_true(
                attribution_bottom["lastVisibleBorderBottom"] == "0px",
                f"attribution detail bottom row keeps double border: {attribution_bottom}",
            )

    page.locator('button[data-view-target="tools"]').click()
    wait_for_view_content(
        page,
        view="tools",
        container="#tool-output",
        row="tr[data-tool]",
        empty=".empty",
        require_focus=True,
    )
    if page.locator("#tool-output table").count() > 0:
        tool_focus_index = page.locator("#tool-output").evaluate(
            "() => Array.from(document.querySelectorAll('#tool-output tr[data-tool] .row-select-button')).indexOf(document.activeElement)"
        )
        assert_true(tool_focus_index >= 0, f"tools view should focus a tool row on entry: {tool_focus_index}")
        page.locator('#tool-output [data-list-sort="calls"]').click()
        page.wait_for_selector('#tool-output th[aria-sort="descending"] [data-list-sort="calls"]', timeout=10_000)
        tools = fetch_json(f"{base_url}/api/tools?days=7&tools_page=1&per_page=25&tool_sort=calls&tool_sort_dir=desc")
        first_tool = page.locator("#tool-output tr[data-tool]").first.get_attribute("data-tool")
        assert_true(
            first_tool == tools["rows"][0]["tool_name"],
            f"tools calls desc sort did not match API: {first_tool!r} != {tools['rows'][0]['tool_name']!r}",
        )
        tool_output_fit = page.locator("#tool-output").evaluate(
            """(el) => {
              const table = el.querySelector('table');
              const share = Array.from(el.querySelectorAll('th')).find((node) => (node.textContent || '').trim() === 'Share');
              const elRect = el.getBoundingClientRect();
              const shareRect = share ? share.getBoundingClientRect() : null;
              return {
                clientWidth: el.clientWidth,
                scrollWidth: el.scrollWidth,
                overflowX: getComputedStyle(el).overflowX,
                tableLayout: table ? getComputedStyle(table).tableLayout : '',
                shareRight: shareRect ? Math.round(shareRect.right) : null,
                panelRight: Math.round(elRect.right),
                columnWidths: Array.from(el.querySelectorAll('thead th')).map((node) => Math.round(node.getBoundingClientRect().width)),
              };
            }"""
        )
        assert_true(
            tool_output_fit["scrollWidth"] <= tool_output_fit["clientWidth"] + 1,
            f"tool output should not require horizontal scroll: {tool_output_fit}",
        )
        assert_true(tool_output_fit["overflowX"] == "hidden", f"tool output horizontal overflow is not hidden: {tool_output_fit}")
        assert_true(tool_output_fit["tableLayout"] == "fixed", f"tool output table should use fixed layout: {tool_output_fit}")
        assert_true(
            tool_output_fit["shareRight"] is not None and tool_output_fit["shareRight"] <= tool_output_fit["panelRight"] + 1,
            f"tool output share column is clipped: {tool_output_fit}",
        )
        assert_true(
            len(tool_output_fit["columnWidths"]) == 4
            and len(set(tool_output_fit["columnWidths"][1:])) == 1
            and tool_output_fit["columnWidths"][1] >= 70,
            f"tool output numeric columns are too compressed: {tool_output_fit}",
        )
    tool_head_heights = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('[data-view="tools"] .panel > .panel-head'))
          .map((el) => Math.round(el.getBoundingClientRect().height * 1000) / 1000)
        """
    )
    assert_true(len(set(tool_head_heights)) == 1, f"tool panel headers are misaligned: {tool_head_heights}")
    if page.locator("#tool-output tr[data-tool]").count() > 0:
        page.locator("#tool-output tr[data-tool]").first.click()
        page.wait_for_selector("#tool-detail table", timeout=10_000)
        tool_header_position = page.locator("#tool-detail th").first.evaluate("(el) => getComputedStyle(el).position")
        assert_true(tool_header_position != "sticky", f"tool detail header should scroll normally: {tool_header_position}")
        session_distribution_fit = page.locator("#tool-detail .tool-session-distribution").evaluate(
            """(el) => {
              const table = el.querySelector('table');
              const widths = Array.from(el.querySelectorAll('thead th')).map((node) => Math.round(node.getBoundingClientRect().width));
              return {
                clientWidth: el.clientWidth,
                scrollWidth: el.scrollWidth,
                tableLayout: table ? getComputedStyle(table).tableLayout : '',
                columnWidths: widths,
              };
            }"""
        )
        assert_true(
            session_distribution_fit["scrollWidth"] <= session_distribution_fit["clientWidth"] + 1,
            f"tool session distribution should not require horizontal scroll: {session_distribution_fit}",
        )
        assert_true(
            len(session_distribution_fit["columnWidths"]) == 5
            and len(set(session_distribution_fit["columnWidths"][1:])) == 1
            and session_distribution_fit["columnWidths"][1] >= 88,
            f"tool session distribution numeric columns are not evenly sized: {session_distribution_fit}",
        )
