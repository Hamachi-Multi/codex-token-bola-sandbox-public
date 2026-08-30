"""Settings browser checks for effective-dated Cost Units rates."""

from __future__ import annotations

from playwright_dashboard_helpers import assert_true, open_dashboard


def check_cost_rate_settings(page, base_url: str) -> None:
    def initial_cost_rates_without_recalculation(route) -> None:
        response = route.fetch()
        body = response.json()
        body["rebuild_required"] = False
        route.fulfill(response=response, json=body)

    page.route("**/api/cost-rates", initial_cost_rates_without_recalculation, times=1)
    open_dashboard(page, base_url)
    shared_scrollbar_state = page.evaluate(
        """
        () => ({
          html: document.documentElement.classList.contains('scrollbar-hidden'),
          body: document.body.classList.contains('scrollbar-hidden'),
          session: document.querySelector('#session-options').classList.contains('scrollbar-hidden'),
        })
        """
    )
    assert_true(all(shared_scrollbar_state.values()), f"shared scrollbar class should cover root and session scrolling: {shared_scrollbar_state}")
    page.locator("#session-picker-button").click()
    page.wait_for_selector("#session-options", state="visible")
    session_scroll = page.locator("#session-options").evaluate(
        """
        (options) => {
          const additions = Array.from({length: 24}, (_, index) => {
            const row = document.createElement('div');
            row.className = 'session-option test-scroll-row';
            row.textContent = `Scroll test ${index}`;
            return row;
          });
          additions.forEach(row => options.appendChild(row));
          options.scrollTop = 48;
          const result = {
            scrollTop: options.scrollTop,
            scrollbarWidth: getComputedStyle(options).scrollbarWidth,
          };
          additions.forEach(row => row.remove());
          options.scrollTop = 0;
          return result;
        }
        """
    )
    assert_true(
        session_scroll["scrollTop"] > 0 and session_scroll["scrollbarWidth"] == "none",
        f"hidden Session scrollbar should preserve scrolling: {session_scroll}",
    )
    page.locator("#session-picker-button").click()
    page.locator('button[data-view-target="settings"]').click()
    page.wait_for_selector("#cost-rate-list .cost-rate-model", state="attached")
    assert_true(page.locator(".appbar .toolbar").is_visible(), "Settings should preserve the shared appbar toolbar")
    assert_true(
        page.locator('[data-settings-detail="cost-rates"] #cost-rate-status').count() == 1,
        "Cost Units status should belong to the selected-setting detail",
    )
    assert_true(
        page.locator("#cost-rate-list .cost-rate-rebuild").count() == 0,
        "Cost Units should not stack a separate recalculation banner above the model list",
    )
    desktop_layout = page.locator("[data-view=\"settings\"]").evaluate(
        """
        (view) => ({
          layoutWidth: view.querySelector('.settings-layout').getBoundingClientRect().width,
          layoutLeft: view.querySelector('.settings-layout').getBoundingClientRect().left,
          mainContentLeft: document.querySelector('main').getBoundingClientRect().left
            + parseFloat(getComputedStyle(document.querySelector('main')).paddingLeft),
          mainContentWidth: document.querySelector('main').getBoundingClientRect().width
            - parseFloat(getComputedStyle(document.querySelector('main')).paddingLeft)
            - parseFloat(getComputedStyle(document.querySelector('main')).paddingRight),
          columns: getComputedStyle(view.querySelector('.settings-master-detail')).gridTemplateColumns,
          listBox: view.querySelector('.settings-list-panel').getBoundingClientRect().toJSON(),
          detailBox: view.querySelector('.settings-detail-panel').getBoundingClientRect().toJSON(),
          listHeadBox: view.querySelector('.settings-list-panel > .panel-head').getBoundingClientRect().toJSON(),
          detailHeadBox: view.querySelector('.settings-detail-panel > .panel-head').getBoundingClientRect().toJSON(),
          panels: view.querySelectorAll('.settings-master-detail > .panel').length,
          staticItems: view.querySelectorAll('#settings-list > .settings-list-item').length,
          modelItems: view.querySelectorAll('#cost-rate-list .cost-rate-summary').length,
          leftModelItems: view.querySelectorAll('#settings-list #cost-rate-list .cost-rate-summary').length,
          selectedKey: view.querySelector('#settings-list').dataset.selectedSetting,
          visibleDetail: view.querySelector('[data-settings-detail]:not([hidden])')?.dataset.settingsDetail || '',
          listItemAlignment: [...view.querySelectorAll('.settings-list-item')].map(item => {
            const itemBox = item.getBoundingClientRect();
            const nameBox = item.querySelector('.settings-list-name').getBoundingClientRect();
            return Math.abs((nameBox.top - itemBox.top) - (itemBox.bottom - nameBox.bottom));
          }),
        })
        """
    )
    assert_true(
        abs(desktop_layout["layoutLeft"] - desktop_layout["mainContentLeft"]) < 1
        and abs(desktop_layout["layoutWidth"] - desktop_layout["mainContentWidth"]) < 1,
        f"Settings should use the same content bounds as other pages: {desktop_layout}",
    )
    assert_true(" " in desktop_layout["columns"], f"Settings should use one desktop row with two columns: {desktop_layout}")
    assert_true(
        desktop_layout["listBox"]["x"] < desktop_layout["detailBox"]["x"]
        and abs(desktop_layout["listBox"]["y"] - desktop_layout["detailBox"]["y"]) < 1
        and abs(desktop_layout["listBox"]["bottom"] - desktop_layout["detailBox"]["bottom"]) < 1,
        f"the complete settings list and selected detail should share one row: {desktop_layout}",
    )
    detail_to_list_ratio = desktop_layout["detailBox"]["width"] / desktop_layout["listBox"]["width"]
    assert_true(
        1.4 <= detail_to_list_ratio <= 1.6,
        f"Settings should match the detail-heavy list/detail proportion: {desktop_layout}",
    )
    assert_true(
        abs(desktop_layout["listHeadBox"]["height"] - desktop_layout["detailHeadBox"]["height"]) < 1,
        f"Settings and Selected Setting headers should use the same height: {desktop_layout}",
    )
    assert_true(
        desktop_layout["panels"] == 2
        and desktop_layout["staticItems"] == 2
        and desktop_layout["modelItems"] >= 1
        and desktop_layout["leftModelItems"] == 0,
        f"the left panel should contain only top-level settings while models stay in the right detail: {desktop_layout}",
    )
    assert_true(
        desktop_layout["selectedKey"] == "general" and desktop_layout["visibleDetail"] == "general",
        f"Settings should open with one matching selected detail: {desktop_layout}",
    )
    assert_true(
        all(offset <= 1.5 for offset in desktop_layout["listItemAlignment"]),
        f"Settings list labels should be vertically centered: {desktop_layout}",
    )
    assert_true(
        page.locator('[data-settings-detail="general"] .settings-general-row').count() == 3
        and page.locator("#theme-system").is_visible()
        and page.locator("#turn-page-size").is_visible()
        and page.locator("#session-label-mode").is_visible(),
        "General should expose Theme, Rows per page, and Session label in one detail area",
    )
    settings_scroll = page.locator(".settings-layout").evaluate(
        """
        (layout) => ({
          clientHeight: layout.clientHeight,
          scrollHeight: layout.scrollHeight,
          scrollbarWidth: getComputedStyle(layout).scrollbarWidth,
        })
        """
    )
    assert_true(
        settings_scroll["scrollHeight"] <= settings_scroll["clientHeight"] + 1
        and settings_scroll["scrollbarWidth"] == "none",
        f"one-row Settings should fit the desktop workspace without a visible scrollbar: {settings_scroll}",
    )

    page.locator('[data-settings-select="cost-rates"]').click()
    page.wait_for_selector("#cost-rate-list .cost-rate-model", state="visible")
    cost_workspace = page.locator('[data-settings-detail="cost-rates"]').evaluate(
        """
        (detail) => ({
          visible: !detail.hidden,
          nestedPanels: detail.querySelectorAll('.cost-rate-model-panel, .cost-rate-detail-panel').length,
          modelListParent: detail.querySelector('#cost-rate-list')?.parentElement === detail,
          modelRows: detail.querySelectorAll('#cost-rate-list .cost-rate-model').length,
          expandedRows: detail.querySelectorAll('#cost-rate-list .cost-rate-summary[aria-expanded="true"]').length,
        })
        """
    )
    assert_true(
        cost_workspace["visible"]
        and cost_workspace["modelRows"] >= 1
        and cost_workspace["nestedPanels"] == 0
        and cost_workspace["modelListParent"]
        and cost_workspace["expandedRows"] == 0,
        f"Cost Units should be one continuous inline model-settings area: {cost_workspace}",
    )
    assert_true(
        page.locator("#settings-cost-rates-title").count() == 0,
        "the selected-setting header should provide the Cost Units title without duplicating it inside the detail",
    )
    initial_cost_rate_status = (page.locator("#cost-rate-status").text_content() or "").strip()
    assert_true(page.locator("#cost-rate-reset-all").is_disabled(), "Reset should be disabled without custom prices")
    assert_true(initial_cost_rate_status == "", "Recalculation availability should not add a separate status message")
    assert_true(page.locator("#cost-rate-recalculate").is_disabled(), "Recalculate should start disabled when no rebuild is needed")
    initial_cost_rate_actions = page.locator(".cost-rate-heading-actions").evaluate(
        """
        (actions) => {
          const box = (selector) => actions.querySelector(selector).getBoundingClientRect().toJSON();
          return {
            recalculate: box('#cost-rate-recalculate'),
            reset: box('#cost-rate-reset-all'),
            addModel: box('#cost-rate-add-model'),
          };
        }
        """
    )
    assert_true(
        initial_cost_rate_actions["recalculate"]["width"] > 0,
        f"Recalculate should retain a stable layout slot: {initial_cost_rate_actions}",
    )
    animated_buttons = page.evaluate(
        """
        () => [...document.querySelectorAll('button')]
          .map(button => ({
            id: button.id,
            classes: button.className,
            duration: getComputedStyle(button).transitionDuration,
          }))
          .filter(button => button.duration !== '0s')
        """
    )
    assert_true(
        not animated_buttons,
        f"dashboard button states should change without decorative animation: {animated_buttons}",
    )
    assert_true(page.locator("#cost-rate-recalculate").is_visible(), "Recalculate should always keep its header slot")

    dated_model = page.locator('#cost-rate-list [data-cost-rate-model="gpt-5.6"]')
    assert_true(dated_model.count() == 1, "built-in dated rates should appear in Cost Units settings")
    dated_model.locator("[data-cost-rate-select]").click()
    dated_detail = dated_model.locator(".cost-rate-detail")
    default_rate_row = dated_detail.locator(".cost-rate-history-row").filter(has_text="Default")
    dated_rate_row = dated_detail.locator(".cost-rate-history-row").filter(has_text="2026-08-21")
    assert_true(
        default_rate_row.locator("[data-cost-rate-delete]").count() == 0,
        "default rates should not expose Delete",
    )
    assert_true(
        dated_rate_row.locator("[data-cost-rate-delete]").count() == 1,
        "dated built-in rates should expose Delete",
    )

    detected = page.locator('#cost-rate-list [data-cost-rate-model="gpt-5.1"]')
    assert_true(detected.count() == 1, "detected fixture model should appear in Cost Units settings")
    cost_header_height = page.locator(".settings-cost-detail-head").evaluate("header => header.getBoundingClientRect().height")
    detected_summary = detected.locator("[data-cost-rate-select]")
    detected_summary.focus()
    detected_summary.press("Enter")
    page.wait_for_function(
        "() => document.querySelector('[data-cost-rate-model=\"gpt-5.1\"] [data-cost-rate-select]').getAttribute('aria-expanded') === 'true'"
    )
    assert_true(
        detected_summary.evaluate("button => document.activeElement === button"),
        "expanding a cost model should preserve keyboard focus on its replaced summary button",
    )
    assert_true(
        page.locator("#settings-list").get_attribute("data-selected-setting") == "cost-rates"
        and page.locator('[data-settings-detail="cost-rates"]').is_visible(),
        "selecting a model should expand its settings inline",
    )
    expanded_cost_header_height = page.locator(".settings-cost-detail-head").evaluate("header => header.getBoundingClientRect().height")
    assert_true(
        abs(expanded_cost_header_height - cost_header_height) < 1,
        f"expanding a model should not resize the Cost Units header: before={cost_header_height}, after={expanded_cost_header_height}",
    )
    model_status = detected.locator(".cost-rate-model-status").text_content() or ""
    assert_true(model_status == "Configured", "detected model should report only its configured pricing state")

    selected_detail = detected.locator(".cost-rate-detail")
    assert_true(selected_detail.is_visible(), "selected model settings should expand directly below its row")
    assert_true(
        selected_detail.locator('.cost-rate-history-head [role="columnheader"]').count() == 6
        and selected_detail.locator('.cost-rate-history-row [role="cell"]').count() >= 6,
        "price history should expose complete table header and cell semantics",
    )
    default_row = selected_detail.locator(".cost-rate-history-row").filter(has_text="Default")
    assert_true(default_row.count() == 1, "built-in model should expose one undated default rate")
    built_in_default_input = (default_row.locator('[data-label="Input"]').text_content() or "").strip()
    assert_true(
        (default_row.locator("span").first.text_content() or "").strip() == "Default",
        "the base rate should be labeled Default instead of showing a start date",
    )
    default_row.locator("[data-cost-rate-edit]").click()
    default_editor = selected_detail.locator("[data-cost-rate-editor]")
    assert_true(default_editor.is_visible(), "default rate should remain editable")
    assert_true(
        default_editor.locator('input[name="effective_from"][type="date"]').count() == 0,
        "editing a default rate should not expose an effective-date control",
    )
    default_editor.locator("[data-cost-rate-cancel]").click()

    selected_detail.locator("[data-cost-rate-add-period]").click()
    editor = selected_detail.locator("[data-cost-rate-editor]")
    assert_true(editor.is_visible(), "price-change editor should open inline")
    assert_true(
        editor.locator('input[name="effective_from"][type="date"]').count() == 1,
        "a dated price change should retain its effective-date control",
    )
    page.wait_for_function(
        "() => document.activeElement === document.querySelector('[data-cost-rate-editor] input:not([type=\"hidden\"])')"
    )
    editor.locator('[name="input_price"]').fill("2")
    editor.locator('[name="cached_input_price"]').fill("0.2")
    editor.locator('[name="output_price"]').fill("12")
    page.wait_for_function(
        "() => document.querySelector('[data-cost-rate-editor] [data-cost-rate-ratio]')?.textContent.includes('Input 1× · Cached 0.1× · Output 6×')"
    )
    assert_true(
        "Input 1× · Cached 0.1× · Output 6×" in (editor.locator("[data-cost-rate-ratio]").text_content() or ""),
        "relative ratio preview should update from entered prices",
    )
    editor.locator("[data-cost-rate-cancel]").click()
    assert_true(selected_detail.locator("[data-cost-rate-editor]").count() == 0, "cancel should close the price editor")

    default_row.locator("[data-cost-rate-edit]").click()
    override_editor = selected_detail.locator("[data-cost-rate-editor]")
    override_editor.locator('[name="input_price"]').fill("4.25")
    override_editor.locator('[name="cached_input_price"]').fill("0.425")
    override_editor.locator('[name="output_price"]').fill("25.5")
    page.evaluate(
        """
        () => {
          const nativeFetch = window.fetch.bind(window);
          window.__costRateNativeFetch = nativeFetch;
          window.fetch = (resource, options = {}) => {
            const url = typeof resource === 'string' ? resource : resource.url;
            if (url.endsWith('/api/cost-rates') && String(options.method || 'GET').toUpperCase() === 'POST') {
              return new Promise((resolve, reject) => {
                window.setTimeout(() => nativeFetch(resource, options).then(resolve, reject), 260);
              });
            }
            return nativeFetch(resource, options);
          };
        }
        """
    )
    override_editor.locator('button[type="submit"]').click()
    page.wait_for_timeout(50)
    early_save_state = page.evaluate(
        """
        () => ({
          addModelDisabled: document.querySelector('#cost-rate-add-model').disabled,
          status: document.querySelector('#cost-rate-status').textContent.trim(),
        })
        """
    )
    assert_true(
        not early_save_state["addModelDisabled"] and early_save_state["status"] == initial_cost_rate_status,
        f"fast cost-rate saves should not flash a transient busy state: {early_save_state}",
    )
    page.wait_for_timeout(120)
    delayed_save_state = page.evaluate(
        """
        () => ({
          addModelDisabled: document.querySelector('#cost-rate-add-model').disabled,
          status: document.querySelector('#cost-rate-status').textContent.trim(),
        })
        """
    )
    assert_true(
        delayed_save_state["addModelDisabled"] and delayed_save_state["status"] == "Saving cost rate",
        f"slow cost-rate saves should expose a stable busy state: {delayed_save_state}",
    )
    page.wait_for_function("() => !document.querySelector('#cost-rate-recalculate').disabled")
    page.evaluate("() => { window.fetch = window.__costRateNativeFetch; delete window.__costRateNativeFetch; }")
    assert_true(
        (page.locator("#cost-rate-status").text_content() or "").strip() == "",
        "saving a price should enable Recalculate without adding a separate status message",
    )
    updated_cost_rate_actions = page.locator(".cost-rate-heading-actions").evaluate(
        """
        (actions) => {
          const box = (selector) => actions.querySelector(selector).getBoundingClientRect().toJSON();
          return {
            recalculate: box('#cost-rate-recalculate'),
            reset: box('#cost-rate-reset-all'),
            addModel: box('#cost-rate-add-model'),
          };
        }
        """
    )
    assert_true(
        abs(updated_cost_rate_actions["recalculate"]["x"] - initial_cost_rate_actions["recalculate"]["x"]) < 1
        and abs(updated_cost_rate_actions["reset"]["x"] - initial_cost_rate_actions["reset"]["x"]) < 1
        and abs(updated_cost_rate_actions["addModel"]["x"] - initial_cost_rate_actions["addModel"]["x"]) < 1,
        f"Cost Units header actions should not jump when recalculation becomes available: before={initial_cost_rate_actions}, after={updated_cost_rate_actions}",
    )
    reset_all = page.locator("#cost-rate-reset-all")
    assert_true(reset_all.is_enabled(), "Reset should enable when custom prices exist")
    reset_all.click()
    reset_dialog = page.locator("#cost-rate-reset-dialog")
    assert_true(reset_dialog.get_attribute("aria-hidden") == "false", "Reset should require confirmation")
    page.wait_for_function("() => document.activeElement === document.querySelector('#cost-rate-reset-cancel')")
    page.locator("#cost-rate-reset-cancel").click()
    assert_true(reset_dialog.get_attribute("aria-hidden") == "true", "Cancel should preserve custom prices")
    reset_all.click()
    with page.expect_response(
        lambda response: response.url.endswith("/api/cost-rates") and response.request.method == "POST"
    ) as reset_response:
        page.locator("#cost-rate-reset-confirm").click()
    assert_true(reset_response.value.ok, "Reset should complete through the cost-rates API")
    page.wait_for_function(
        "() => document.querySelector('#cost-rate-reset-dialog').getAttribute('aria-hidden') === 'true' && document.querySelector('#cost-rate-reset-all').disabled"
    )
    assert_true(
        (page.locator("#cost-rate-status").text_content() or "").strip() == initial_cost_rate_status,
        "reset should restore the initial built-in catalog state",
    )
    restored_default_row = selected_detail.locator(".cost-rate-history-row").filter(has_text="Default")
    assert_true(
        (restored_default_row.locator('[data-label="Input"]').text_content() or "").strip() == built_in_default_input,
        "reset should restore the bundled default price",
    )

    assert_true(page.locator("#cost-rate-recalculate").is_visible(), "Recalculate should remain visible after reset")

    add_model = page.locator("#cost-rate-add-model")
    list_box_before = page.locator("#cost-rate-list").evaluate(
        "list => ({offsetTop: list.offsetTop, offsetHeight: list.offsetHeight})"
    )
    add_model.click()
    new_editor = page.locator("#cost-rate-model-popover [data-cost-rate-editor]")
    assert_true(new_editor.is_visible(), "Add model should open a model and price editor")
    list_box_after = page.locator("#cost-rate-list").evaluate(
        "list => ({offsetTop: list.offsetTop, offsetHeight: list.offsetHeight})"
    )
    assert_true(
        list_box_before == list_box_after,
        f"Add model popover should not move or resize the model list: before={list_box_before}, after={list_box_after}",
    )
    assert_true(
        page.locator("#cost-rate-model-dialog").evaluate(
            "dialog => getComputedStyle(dialog).position === 'fixed' && getComputedStyle(dialog).backgroundColor !== 'rgba(0, 0, 0, 0)'"
        ),
        "Add model editor should use the shared modal backdrop",
    )
    assert_true(
        page.locator("#cost-rate-model-popover").get_attribute("aria-modal") == "true",
        "Add model editor should expose modal semantics",
    )
    assert_true(add_model.get_attribute("aria-expanded") == "true", "Add model should expose its expanded state")
    assert_true((add_model.text_content() or "").strip() == "+", "Add model should use a compact plus icon")
    assert_true(add_model.get_attribute("aria-label") == "Add model", "plus icon should retain the Add model name")
    page.wait_for_function(
        "() => document.activeElement === document.querySelector('#cost-rate-model-popover [name=\"model_id\"]')"
    )
    new_editor.locator('button[type="submit"]').focus()
    page.keyboard.press("Tab")
    assert_true(
        page.evaluate("() => document.activeElement === document.querySelector('#cost-rate-model-popover-close')"),
        "Add model should trap keyboard focus inside the modal",
    )
    page.keyboard.press("Escape")
    assert_true(new_editor.count() == 0, "Escape should close the Add model editor")
    assert_true(add_model.get_attribute("aria-expanded") == "false", "closed Add model should reset its expanded state")
    assert_true((add_model.text_content() or "").strip() == "+", "Add model should keep its plus icon while closed")

    add_model.click()
    new_editor = page.locator("#cost-rate-model-popover [data-cost-rate-editor]")
    new_editor.locator("[data-cost-rate-cancel]").click()
    assert_true(add_model.get_attribute("aria-expanded") == "false", "editor Cancel should reset the Add model toggle")

    def active_service_status(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"running":true,"operation":"analysis","status":"running","progress_available":true,"phase":"build","checkpoint":"turns","overall_progress":42,"operation_id":"11111111-1111-4111-8111-111111111111"}',
        )

    cost_rate_status_before_activity = (page.locator("#cost-rate-status").text_content() or "").strip()
    page.route("**/api/service-status", active_service_status)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.wait_for_function("() => !document.querySelector('#service-activity').hidden")
    assert_true(
        "Analyze · 42%" in (page.locator("#service-activity").text_content() or ""),
        "appbar should expose active analysis progress",
    )
    assert_true(page.locator("#cost-rate-add-model").is_disabled(), "active service work should lock cost-rate mutations")
    if page.locator("#cost-rate-recalculate").is_visible():
        assert_true(page.locator("#cost-rate-recalculate").is_disabled(), "active service work should lock recalculation")
    assert_true(
        (page.locator("#cost-rate-status").text_content() or "").strip() == cost_rate_status_before_activity,
        "Cost Units should not duplicate the shared analysis status",
    )
    page.unroute("**/api/service-status", active_service_status)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.wait_for_function("() => document.querySelector('#service-activity').hidden")
    assert_true(page.locator("#cost-rate-add-model").is_enabled(), "cost-rate mutations should unlock when service work finishes")

    page.set_viewport_size({"width": 1024, "height": 768})
    page.locator('[data-settings-select="general"]').click()
    compact_settings = page.evaluate(
        """
        () => {
          const list = document.querySelector('.settings-list-panel').getBoundingClientRect();
          const detail = document.querySelector('.settings-detail-panel').getBoundingClientRect();
          const copy = document.querySelector('.settings-general-row .settings-detail-copy').getBoundingClientRect();
          return {listWidth: list.width, detailWidth: detail.width, copyWidth: copy.width};
        }
        """
    )
    assert_true(
        1.4 <= compact_settings["detailWidth"] / compact_settings["listWidth"] <= 1.6
        and compact_settings["copyWidth"] >= 180,
        f"compact Settings should preserve the detail-heavy proportion and readable content: {compact_settings}",
    )
    page.locator('button[data-view-target="turns"]').click()
    page.wait_for_selector('#turn-list tr[data-turn]')
    compact_turns = page.evaluate(
        """
        () => ({
          headerDisplay: getComputedStyle(document.querySelector('#turn-list thead')).display,
          metaDisplay: getComputedStyle(document.querySelector('#turn-list .row-meta')).display,
        })
        """
    )
    assert_true(
        compact_turns["headerDisplay"] == "none" and compact_turns["metaDisplay"] == "block",
        f"compact Turns should switch from clipped columns to readable rows: {compact_turns}",
    )
    page.locator('button[data-view-target="settings"]').click()
    page.locator('[data-settings-select="cost-rates"]').click()

    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function(
        "() => document.querySelector('.page-nav-frame').dataset.canScrollLeft === 'true'"
    )
    settings_tab_box = page.locator('button[data-view-target="settings"]').bounding_box()
    assert_true(
        settings_tab_box is not None and settings_tab_box["x"] >= 0 and settings_tab_box["x"] + settings_tab_box["width"] <= 390,
        f"active Settings tab should remain visible on mobile: {settings_tab_box}",
    )
    assert_true(
        page.locator(".page-nav-frame").get_attribute("data-can-scroll-left") == "true",
        "mobile navigation should expose a left overflow cue when earlier tabs are hidden",
    )
    page.locator('button[data-view-target="overview"]').focus()
    page.wait_for_function(
        "() => document.querySelector('.page-nav-frame').dataset.canScrollRight === 'true'"
    )
    overview_tab_box = page.locator('button[data-view-target="overview"]').bounding_box()
    assert_true(
        overview_tab_box is not None and overview_tab_box["x"] >= 0 and overview_tab_box["x"] + overview_tab_box["width"] <= 390,
        f"focused Overview tab should be revealed on mobile: {overview_tab_box}",
    )
    page.locator('button[data-view-target="settings"]').click()
    mobile_layout = page.locator("[data-view=\"settings\"]").evaluate(
        """
        (view) => ({
          pageOverflow: document.documentElement.scrollWidth > window.innerWidth,
          viewOverflow: view.scrollWidth > view.clientWidth,
          columns: getComputedStyle(view.querySelector('.settings-master-detail')).gridTemplateColumns,
          listBottom: view.querySelector('.settings-list-panel').getBoundingClientRect().bottom,
          detailTop: view.querySelector('.settings-detail-panel').getBoundingClientRect().top,
          listScroll: (() => {
            const list = view.querySelector('.settings-list');
            return {
              clientHeight: list.clientHeight,
              scrollHeight: list.scrollHeight,
              scrollbarWidth: getComputedStyle(list).scrollbarWidth,
            };
          })(),
        })
        """
    )
    assert_true(not mobile_layout["pageOverflow"], f"Cost Units settings should not overflow the mobile page: {mobile_layout}")
    assert_true(not mobile_layout["viewOverflow"], f"Cost Units settings should fit the mobile settings view: {mobile_layout}")
    assert_true(" " not in mobile_layout["columns"], f"Settings should stack its primary columns on mobile: {mobile_layout}")
    assert_true(
        mobile_layout["detailTop"] >= mobile_layout["listBottom"],
        f"selected setting details should follow the complete settings list on mobile: {mobile_layout}",
    )
    assert_true(
        mobile_layout["listScroll"]["scrollbarWidth"] == "none",
        f"the complete mobile settings list should keep the shared hidden-scrollbar treatment: {mobile_layout}",
    )
    mobile_scroll = page.evaluate(
        """
        () => {
          window.scrollTo(0, 120);
          return {
            scrollY: window.scrollY,
            htmlScrollbarWidth: getComputedStyle(document.documentElement).scrollbarWidth,
            bodyScrollbarWidth: getComputedStyle(document.body).scrollbarWidth,
          };
        }
        """
    )
    assert_true(
        mobile_scroll["scrollY"] > 0
        and mobile_scroll["htmlScrollbarWidth"] == "none"
        and mobile_scroll["bodyScrollbarWidth"] == "none",
        f"hidden mobile scrollbar should preserve page scrolling: {mobile_scroll}",
    )
