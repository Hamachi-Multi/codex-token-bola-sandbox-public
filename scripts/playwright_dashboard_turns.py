"""Turns and selected-turn browser checks for the Codex Token Bola dashboard."""

from __future__ import annotations

import re

from playwright_dashboard_helpers import (
    assert_true,
    compact_date,
    compact_session_id,
    compact_time,
    fetch_json,
    parse_number,
    scroll_bottom_state,
    session_path_label,
)


def check_turns_and_selected_turn(page, base_url: str) -> None:
    page.locator('button[data-view-target="turns"]').click()
    page.wait_for_selector("#turn-list tr[data-turn]", state="attached", timeout=10_000)
    turn_view_focus_index = page.locator("#turn-list").evaluate(
        "() => Array.from(document.querySelectorAll('#turn-list tr[data-turn]')).indexOf(document.activeElement)"
    )
    assert_true(turn_view_focus_index >= 0, f"turns view should focus a turn row on entry: {turn_view_focus_index}")

    headers = page.locator("#turn-list th").all_text_contents()
    assert_true(
        headers == ["Date", "Session", "Prompt", "Cost Units", "Total Tokens"],
        f"unexpected Turns headers: {headers}",
    )
    time_sort_button_count = page.locator('#turn-list [data-turn-sort="clock"]').count()
    assert_true(time_sort_button_count == 0, "Time column should not expose sorting")
    header_font_families = page.locator("#turn-list th .sort-button").evaluate_all(
        "(els) => Array.from(new Set(els.map((el) => getComputedStyle(el).fontFamily)))"
    )
    assert_true(
        len(header_font_families) == 1,
        f"turn header fonts are inconsistent: {header_font_families}",
    )
    date_header_padding = page.locator("#turn-list th").first.locator(".sort-button").evaluate(
        "(el) => getComputedStyle(el).paddingLeft"
    )
    assert_true(
        date_header_padding == "18px",
        f"Date header padding does not align with status stripe offset: {date_header_padding}",
    )
    status_legend = page.locator(".status-legend").text_content() or ""
    assert_true(
        all(label in status_legend for label in ["Completed", "Incomplete", "Aborted"]),
        f"turn status legend is incomplete: {status_legend!r}",
    )
    turn_header_line = page.locator("#turn-list th").first.evaluate(
        """
        (el) => ({
          borderBottom: getComputedStyle(el).borderBottomWidth,
          boxShadow: getComputedStyle(el).boxShadow,
        })
        """
    )
    assert_true(turn_header_line["borderBottom"] == "0px", f"turn header keeps a double border: {turn_header_line}")
    assert_true("inset" in turn_header_line["boxShadow"], f"turn header lost its bottom rule: {turn_header_line}")
    date_cell_fit = page.locator("#turn-list").evaluate(
        """
        () => {
          const cells = Array.from(document.querySelectorAll('#turn-list .datetime-cell'));
          return cells.map((cell) => ({
            text: cell.textContent || '',
            scrollWidth: cell.scrollWidth,
            clientWidth: cell.clientWidth,
          }));
        }
        """
    )
    clipped_date_cells = [cell for cell in date_cell_fit if cell["scrollWidth"] > cell["clientWidth"] + 1]
    assert_true(not clipped_date_cells, f"turn date cells should not clip desktop timestamps: {date_cell_fit}")

    dashboard = fetch_json(f"{base_url}/api/dashboard?days=7&limit=100&page=1&per_page=5")
    rows = dashboard["turns"]["rows"]
    assert_true(rows, "dashboard returned no turn rows")
    session_options = fetch_json(f"{base_url}/api/session-options")["rows"]
    assert_true(page.locator("#session-picker-button").is_visible(), "session filter is not visible")
    assert_true(page.locator("#project").count() == 0, "project filter should not remain in the header")
    if session_options:
        selected_session = session_options[0]["session_id"]
        page.locator("#session-picker-button").click()
        page.locator("#session-search").fill(selected_session[-4:])
        page.locator(f'#session-options [data-session-id="{selected_session}"]').click()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#turn-list tr[data-turn]", timeout=10_000)
        assert_true(compact_session_id(selected_session) in page.locator("#session-picker-button").inner_text(), "selected session label did not update")
        session_dashboard = fetch_json(f"{base_url}/api/dashboard?days=7&limit=100&session_id={selected_session}")
        selected_rows = page.locator("#turn-list tr[data-turn]").evaluate_all(
            "(rows) => rows.map((row) => row.dataset.session)"
        )
        assert_true(
            all(value == selected_session for value in selected_rows),
            f"session filter rendered rows from another session: {selected_rows}",
        )
        assert_true(
            0 < page.locator("#turn-list tr[data-turn]").count() <= session_dashboard["turns"]["total"],
            "session filter row count did not match API total bounds",
        )
        page.locator("#session-picker-button").click()
        page.locator('#session-options [data-session-id=""]').click()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#turn-list tr[data-turn]", timeout=10_000)
    page.locator("#turn-page-size").select_option("10")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#turn-list tr[data-turn]", timeout=10_000)
    rendered_turn_rows = page.locator("#turn-list tr[data-turn]").count()
    assert_true(
        rendered_turn_rows == min(10, dashboard["turns"]["total"]),
        f"page rows did not apply to turns list: {rendered_turn_rows}",
    )
    if rendered_turn_rows > 1:
        page.locator("#turn-list tr[data-turn]").first.focus()
        page.keyboard.press("ArrowDown")
        turn_focus_state = page.locator("#turn-list").evaluate(
            "() => { const rows = Array.from(document.querySelectorAll('#turn-list tr[data-turn]')); return {focused: rows.indexOf(document.activeElement), selected: rows.findIndex((row) => row.classList.contains('selected'))}; }"
        )
        assert_true(turn_focus_state == {"focused": 1, "selected": 1}, f"ArrowDown should move focused and selected turn row to next row: {turn_focus_state}")
        selected_turn_background_before_hover = page.locator("#turn-list tr.selected td").first.evaluate("(el) => getComputedStyle(el).backgroundColor")
        page.locator("#turn-list tr.selected").hover()
        selected_turn_background_after_hover = page.locator("#turn-list tr.selected td").first.evaluate("(el) => getComputedStyle(el).backgroundColor")
        assert_true(
            selected_turn_background_after_hover == selected_turn_background_before_hover,
            f"selected turn row hover should not change active background: before={selected_turn_background_before_hover}, after={selected_turn_background_after_hover}",
        )
        turn_focus_style = page.locator("#turn-list").evaluate(
            "() => { const active = document.activeElement; const firstCell = active ? active.querySelector('td') : null; return {rowOutline: active ? getComputedStyle(active).outlineStyle : '', cellOutline: firstCell ? getComputedStyle(firstCell).outlineStyle : '', background: firstCell ? getComputedStyle(firstCell).backgroundColor : ''}; }"
        )
        assert_true(turn_focus_style["rowOutline"] == "none" and turn_focus_style["cellOutline"] == "none", f"focused turn row should match hover without outline: {turn_focus_style}")
        page.keyboard.press("ArrowUp")
        turn_focus_state = page.locator("#turn-list").evaluate(
            "() => { const rows = Array.from(document.querySelectorAll('#turn-list tr[data-turn]')); return {focused: rows.indexOf(document.activeElement), selected: rows.findIndex((row) => row.classList.contains('selected'))}; }"
        )
        assert_true(turn_focus_state == {"focused": 0, "selected": 0}, f"ArrowUp should move focused and selected turn row to previous row: {turn_focus_state}")
        page.wait_for_function("() => (document.querySelector('#detail')?.textContent || '').includes('Call Summary')")
    detail_status = page.locator("#detail-status").text_content() or ""
    if detail_status and "Model Calls" in detail_status:
        assert_true(page.locator("#detail-list").count() == 0, "selected turn should not render per-call detail list")
        detail_text = page.locator("#detail").text_content() or ""
        assert_true("Call Summary" in detail_text, "selected turn missing call summary")
        assert_true("Tool Calls" in detail_text, "selected turn missing tool calls")
        detail_layout = page.locator("#detail").evaluate(
            """
            (el) => {
              const prompt = el.querySelector('.selected-turn-identity .method-name');
              const sections = Array.from(el.querySelectorAll('.selected-turn-section'));
              const contextGrid = el.querySelector('.selected-turn-context-grid');
              const contextCells = contextGrid ? Array.from(contextGrid.children) : [];
              const sectionFor = (title) => {
                const section = sections.find((item) => (item.querySelector('.selected-turn-section-title') || {}).textContent === title);
                return section || null;
              };
              const metricCountFor = (title) => {
                const section = sectionFor(title);
                return section ? section.querySelectorAll('.selected-turn-metric').length : 0;
              };
              const metricGridFor = (title) => {
                const section = sectionFor(title);
                return section ? section.querySelector('.selected-turn-metric-grid') : null;
              };
              const columnCountFor = (title) => {
                const grid = metricGridFor(title);
                return grid ? getComputedStyle(grid).gridTemplateColumns.split(/\\s+/).filter(Boolean).length : 0;
              };
              const secondBorderWidthFor = (title) => {
                const grid = metricGridFor(title);
                const second = grid ? grid.querySelectorAll('.selected-turn-metric')[1] : null;
                return second ? getComputedStyle(second).borderRightWidth : '';
              };
              const toolSection = sectionFor('Tool Calls');
              const firstTool = toolSection ? toolSection.querySelector('.selected-turn-tool-name') : null;
              const firstToolStats = toolSection ? toolSection.querySelector('.selected-turn-tool-stats') : null;
              const secondToolStat = firstToolStats ? firstToolStats.querySelectorAll('.selected-turn-tool-stat')[1] : null;
              const toolToggle = toolSection ? toolSection.querySelector('[data-toggle-tools]') : null;
              return {
                hasDetail: !!el.querySelector('.selected-turn-detail'),
                hasHeader: !!el.querySelector('.selected-turn-header'),
                hasContext: sections.some((section) => (section.querySelector('.selected-turn-section-title') || {}).textContent === 'Turn Context'),
                hasSummary: sections.some((section) => (section.querySelector('.selected-turn-section-title') || {}).textContent === 'Turn Summary'),
                hasIdentity: !!el.querySelector('.selected-turn-identity'),
                hasHiddenPrompt: !!el.querySelector('.selected-turn-identity.has-hidden-prompt'),
                promptOverflows: prompt ? prompt.scrollHeight > prompt.clientHeight + 1 : false,
                promptLineClamp: prompt ? getComputedStyle(prompt).webkitLineClamp : '',
                contextMetrics: metricCountFor('Turn Context'),
                summaryMetrics: metricCountFor('Turn Summary'),
                tokenMetrics: metricCountFor('Token Summary'),
                callMetrics: metricCountFor('Call Summary'),
                summaryColumns: columnCountFor('Turn Summary'),
                tokenColumns: columnCountFor('Token Summary'),
                callColumns: columnCountFor('Call Summary'),
                callSecondBorderWidth: secondBorderWidthFor('Call Summary'),
                contextColumnCount: contextGrid ? getComputedStyle(contextGrid).gridTemplateColumns.split(/\\s+/).filter(Boolean).length : 0,
                contextColumns: contextGrid ? getComputedStyle(contextGrid).gridTemplateColumns.split(/\\s+/).filter(Boolean) : [],
                contextBorders: contextCells.map((cell) => getComputedStyle(cell).borderRightWidth),
                contextAlignments: contextCells.map((cell) => getComputedStyle(cell).alignContent),
                contextJustifyItems: contextCells.map((cell) => getComputedStyle(cell).justifyItems),
                contextTextAlignments: contextCells.map((cell) => getComputedStyle(cell).textAlign),
                statusText: (el.querySelector('.selected-turn-context-status .status') || {}).textContent || '',
                statusMinWidth: el.querySelector('.selected-turn-context-status .status') ? getComputedStyle(el.querySelector('.selected-turn-context-status .status')).minWidth : '',
                statusMinHeight: el.querySelector('.selected-turn-context-status .status') ? getComputedStyle(el.querySelector('.selected-turn-context-status .status')).minHeight : '',
                selectedTables: el.querySelectorAll('.selected-turn-section table').length,
                hasContextStatus: !!el.querySelector('.selected-turn-context-status .status'),
                hasToolList: !!el.querySelector('.selected-turn-tool-list'),
                toolRows: el.querySelectorAll('.selected-turn-tool-row').length,
                firstToolClamp: firstTool ? getComputedStyle(firstTool).webkitLineClamp : '',
                firstToolWhiteSpace: firstTool ? getComputedStyle(firstTool).whiteSpace : '',
                firstToolStatColumns: firstToolStats ? getComputedStyle(firstToolStats).gridTemplateColumns.split(/\\s+/).filter(Boolean).length : 0,
                secondToolStatBorderWidth: secondToolStat ? getComputedStyle(secondToolStat).borderRightWidth : '',
                hasToolToggle: !!toolToggle,
                sections: Array.from(el.querySelectorAll('.selected-turn-section-title')).map((node) => node.textContent || ''),
              };
            }
            """
        )
        assert_true(detail_layout["hasDetail"], f"selected turn detail wrapper missing: {detail_layout}")
        assert_true(detail_layout["hasHeader"], f"selected turn header missing: {detail_layout}")
        assert_true(detail_layout["hasContext"], f"selected turn context summary missing: {detail_layout}")
        assert_true(detail_layout["hasSummary"], f"selected turn summary missing: {detail_layout}")
        assert_true(detail_layout["hasIdentity"], f"selected turn identity row missing: {detail_layout}")
        if detail_layout["promptOverflows"]:
            assert_true(detail_layout["hasHiddenPrompt"], f"selected turn hidden prompt state is missing: {detail_layout}")
            assert_true(detail_layout["promptLineClamp"] == "2", f"selected turn prompt should use two-line ellipsis: {detail_layout}")
        assert_true(detail_layout["selectedTables"] == 0, f"selected turn should use metric blocks instead of tables: {detail_layout}")
        assert_true(detail_layout["hasContextStatus"] and detail_layout["contextMetrics"] == 2, f"selected turn context metrics changed: {detail_layout}")
        assert_true(detail_layout["contextColumnCount"] == 3, f"selected turn context should keep three aligned cells: {detail_layout}")
        assert_true(len(set(detail_layout["contextColumns"])) == 1, f"selected turn context cells should have equal spacing: {detail_layout}")
        assert_true(all(width == "0px" for width in detail_layout["contextBorders"]), f"selected turn context should not show vertical dividers: {detail_layout}")
        assert_true(all(value == "center" for value in detail_layout["contextAlignments"]), f"selected turn context cells should align consistently: {detail_layout}")
        assert_true(all(value == "center" for value in detail_layout["contextJustifyItems"]), f"selected turn context content should be centered: {detail_layout}")
        assert_true(all(value == "center" for value in detail_layout["contextTextAlignments"]), f"selected turn context text should be centered: {detail_layout}")
        assert_true(detail_layout["statusText"].strip(), f"selected turn status pill should keep visible text: {detail_layout}")
        assert_true(detail_layout["statusMinWidth"] == "86px" and detail_layout["statusMinHeight"] == "26px", f"selected turn status pill size changed: {detail_layout}")
        assert_true(detail_layout["summaryMetrics"] == 4, f"selected turn summary metric count changed: {detail_layout}")
        assert_true(detail_layout["tokenMetrics"] == 4, f"selected turn token metric count changed: {detail_layout}")
        assert_true(detail_layout["callMetrics"] == 4, f"selected turn call metric count changed: {detail_layout}")
        assert_true(detail_layout["summaryColumns"] == 4, f"turn summary should render as one row of four metrics: {detail_layout}")
        assert_true(detail_layout["tokenColumns"] == 4, f"token summary should render as one row of four metrics: {detail_layout}")
        assert_true(detail_layout["callColumns"] == 4, f"call summary should render as one row of four metrics: {detail_layout}")
        assert_true(detail_layout["callSecondBorderWidth"] != "0px", f"call summary second divider is missing: {detail_layout}")
        expected_sections = ["Turn Context", "Turn Summary", "Token Summary", "Call Summary", "Tool Calls"]
        assert_true(detail_layout["hasToolList"], f"selected turn tool summary should render an empty list shell when no tools exist: {detail_layout}")
        if " / 0 Tool Calls" not in detail_status:
            assert_true(detail_layout["hasToolList"] and detail_layout["toolRows"] >= 1, f"selected turn tool summary should render a readable list: {detail_layout}")
            assert_true(detail_layout["toolRows"] == page.locator("#detail .selected-turn-tool-row").count(), f"selected turn tool rows should be queryable: {detail_layout}")
            assert_true(detail_layout["firstToolClamp"] == "2" and detail_layout["firstToolWhiteSpace"] == "normal", f"selected turn tool names should allow two-line reading: {detail_layout}")
            assert_true(detail_layout["firstToolStatColumns"] == 4, f"selected turn tool stats should render as one row of four metrics: {detail_layout}")
            assert_true(detail_layout["secondToolStatBorderWidth"] != "0px", f"selected turn tool stats second divider is missing: {detail_layout}")
            assert_true(not detail_layout["hasToolToggle"] or detail_layout["toolRows"] == 16, f"collapsed selected turn tool list should show top 16 rows before expansion: {detail_layout}")
            tool_focus_state = page.locator("#detail").evaluate(
                """
                () => ({
                  focusableRows: Array.from(document.querySelectorAll('#detail .selected-turn-tool-row')).filter(row => row.tabIndex >= 0).length,
                  toggleFocusable: !document.querySelector('#detail [data-toggle-tools]') || document.querySelector('#detail [data-toggle-tools]').tabIndex >= 0,
                })
                """
            )
            assert_true(
                tool_focus_state["focusableRows"] == 0 and tool_focus_state["toggleFocusable"],
                f"selected turn tool rows should be static and only the expand control should be focusable: {tool_focus_state}",
            )
        else:
            assert_true(detail_layout["toolRows"] == 0 and not detail_layout["hasToolToggle"], f"empty selected turn tool summary should not render rows or controls: {detail_layout}")
        assert_true(detail_layout["sections"] == expected_sections, f"selected turn sections are not in the expected order: {detail_layout}")
    page.locator("#turn-page-size").select_option("25")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#turn-list tr[data-turn]", timeout=10_000)
    expected_datetime = f"{compact_date(rows[0].get('captured_at') or '')} {compact_time(rows[0].get('captured_at') or '')}"
    rendered_datetime = page.locator("#turn-list tr[data-turn] .datetime-cell").first.text_content()
    assert_true(
        rendered_datetime == expected_datetime,
        f"first rendered datetime {rendered_datetime!r} != API datetime {expected_datetime!r}",
    )
    detail_session = (page.locator("#detail .selected-turn-identity .method-desc").first.text_content() or "").strip()
    expected_session_id = compact_session_id(rows[0].get("session_id") or "")
    expected_thread_name = (rows[0].get("thread_name") or "").strip()
    expected_path = session_path_label(rows[0].get("cwd") or "")
    assert_true(
        not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", detail_session),
        f"selected turn session still shows a full UUID: {detail_session!r}",
    )
    if expected_thread_name:
        assert_true(
            f"{expected_thread_name} · {expected_session_id}" in detail_session,
            f"selected turn named session label is not compact: {detail_session!r}",
        )
    else:
        assert_true(
            f"{expected_path} · {expected_session_id}" in detail_session,
            f"selected turn fallback session label is not compact id plus path: {detail_session!r}",
        )
    assert_true(" / " in detail_session, f"selected turn identity metadata is missing session/date context: {detail_session!r}")

    captured = [row.get("captured_at") or "" for row in rows]
    assert_true(captured == sorted(captured, reverse=True), f"turn rows are not latest-first: {captured}")

    page.locator('#turn-list [data-turn-sort="credits"]').click()
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('#turn-list th[aria-sort="descending"] [data-turn-sort="credits"]', timeout=10_000)
    credits_desc = fetch_json(f"{base_url}/api/dashboard?days=7&limit=100&page=1&per_page=5&sort=credits&sort_dir=desc")[
        "turns"
    ]["rows"]
    status_line_width = page.locator("#turn-list tr[data-turn] td:first-child").first.evaluate(
        "el => getComputedStyle(el, '::before').width"
    )
    assert_true(status_line_width in {"3px", "4px"}, f"status line is not visible: {status_line_width}")

    rendered_cost_desc = page.locator("#turn-list tr[data-turn] td:nth-child(4) .compact-number").first.get_attribute("title")
    assert_true(
        abs(parse_number(rendered_cost_desc) - float(credits_desc[0].get("credits") or 0)) < 0.01,
        f"cost desc sort did not match API: {rendered_cost_desc!r}",
    )

    page.locator('#turn-list [data-turn-sort="credits"]').click()
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('#turn-list th[aria-sort="ascending"] [data-turn-sort="credits"]', timeout=10_000)
    credits_asc = fetch_json(f"{base_url}/api/dashboard?days=7&limit=100&page=1&per_page=5&sort=credits&sort_dir=asc")[
        "turns"
    ]["rows"]
    rendered_cost_asc = page.locator("#turn-list tr[data-turn] td:nth-child(4) .compact-number").first.get_attribute("title")
    assert_true(
        abs(parse_number(rendered_cost_asc) - float(credits_asc[0].get("credits") or 0)) < 0.01,
        f"cost asc sort did not match API: {rendered_cost_asc!r}",
    )

    turn_list_state = page.evaluate(
        """
        () => {
          const el = document.querySelector('#turn-list');
          el.scrollTop = Math.min(120, el.scrollHeight);
          el.dispatchEvent(new Event('scroll'));
          const shadow = el.querySelector('.table-header-shadow');
          const header = el.querySelector('th');
          const headerRect = header.getBoundingClientRect();
          const shadowRect = shadow.getBoundingClientRect();
          const style = getComputedStyle(el);
          return {
            canScroll: el.scrollHeight - el.clientHeight > 1,
            canScrollUp: el.classList.contains('can-scroll-up'),
            paddingBottom: style.paddingBottom,
            scrollPaddingBottom: style.scrollPaddingBottom,
            shadowOpacity: shadow ? getComputedStyle(shadow).opacity : null,
            shadowHeaderGap: Math.round((shadowRect.top - headerRect.bottom) * 100) / 100,
          };
        }
        """
    )
    page.wait_for_timeout(250)
    turn_list_state["shadowOpacity"] = page.evaluate(
        """
        () => {
          const shadow = document.querySelector('#turn-list .table-header-shadow');
          return shadow ? getComputedStyle(shadow).opacity : null;
        }
        """
    )
    if turn_list_state["canScroll"]:
        assert_true(turn_list_state["paddingBottom"] == "1px", f"turn-list should only keep a 1px scroll edge guard: {turn_list_state}")
        assert_true(turn_list_state["scrollPaddingBottom"] == "8px", f"turn-list scroll padding missing: {turn_list_state}")
        assert_true(turn_list_state["canScrollUp"], f"turn-list did not enter can-scroll-up: {turn_list_state}")
        assert_true(abs(turn_list_state["shadowHeaderGap"]) <= 1, f"header shadow is offset from header edge: {turn_list_state}")
        assert_true(float(turn_list_state["shadowOpacity"]) > 0, f"header shadow did not appear: {turn_list_state}")
        turn_bottom = scroll_bottom_state(page, "#turn-list")
        assert_true(turn_bottom["remaining"] == 0, f"turn-list did not reach scroll bottom: {turn_bottom}")
        assert_true(not turn_bottom["canScrollDown"], f"turn-list still reports scroll down at bottom: {turn_bottom}")
        assert_true(turn_bottom["lineHeight"] == "20px", f"turn-list row line-height is fractional: {turn_bottom}")
        assert_true(
            turn_bottom["lastVisibleBottomDelta"] >= 0.5,
            f"turn-list bottom row clips at scroll end: {turn_bottom}",
        )
        assert_true(turn_bottom["lastVisibleBorderBottom"] == "0px", f"turn-list bottom row keeps double border: {turn_bottom}")
    else:
        assert_true(turn_list_state["paddingBottom"] == "0px", f"non-scrollable turn-list should not add scroll guard: {turn_list_state}")
        assert_true(turn_list_state["scrollPaddingBottom"] == "auto", f"non-scrollable turn-list should not add scroll padding: {turn_list_state}")
