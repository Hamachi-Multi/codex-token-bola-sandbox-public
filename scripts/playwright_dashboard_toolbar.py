"""Toolbar browser checks for the Codex Token Bola dashboard."""

from __future__ import annotations

from playwright_dashboard_helpers import assert_true


def check_theme_transition_stability(page) -> None:
    page.add_init_script(
        """
        window.__themeReloadTransitions = [];
        document.addEventListener('transitionrun', event => {
          window.__themeReloadTransitions.push({
            property: event.propertyName,
            target: event.target && (event.target.id || event.target.className || event.target.tagName),
          });
        }, true);
        """
    )
    page.evaluate(
        """
        () => {
          localStorage.setItem('codex-token-usage-dashboard-settings', JSON.stringify({themeMode: 'dark'}));
        }
        """
    )
    page.reload(wait_until="load")
    page.wait_for_timeout(220)
    reload_state = page.evaluate(
        """
        () => ({
          theme: document.documentElement.dataset.theme || '',
          bodyBg: getComputedStyle(document.body).backgroundColor,
          transitions: window.__themeReloadTransitions || [],
        })
        """
    )
    assert_true(
        reload_state["theme"] == "dark"
        and reload_state["bodyBg"] == "rgb(15, 15, 15)"
        and reload_state["transitions"] == [],
        f"saved dark theme reload should not replay element transitions: {reload_state}",
    )
    fallback_state = page.evaluate(
        """
        async () => {
          const originalStartViewTransition = document.startViewTransition;
          document.startViewTransition = undefined;
          document.querySelector('[data-theme-mode="light"]').click();
          while (document.documentElement.dataset.theme !== 'light') {
            await new Promise(resolve => requestAnimationFrame(resolve));
          }
          document.querySelector('button[data-view-target="overview"]').click();
          document.querySelector('[data-theme-mode="dark"]').click();
          while (document.documentElement.dataset.theme !== 'dark') {
            await new Promise(resolve => requestAnimationFrame(resolve));
          }
          await new Promise(resolve => requestAnimationFrame(resolve));
          await new Promise(resolve => requestAnimationFrame(resolve));
          const activeNav = document.querySelector('.nav-btn.active');
          const preset = document.querySelector('[data-cleanup-retention-preset][aria-pressed="true"]');
          const state = {
            theme: document.documentElement.dataset.theme || '',
            commitClass: document.documentElement.classList.contains('theme-commit'),
            activeNavTransition: getComputedStyle(activeNav).transitionDuration,
            activeNavBg: getComputedStyle(activeNav).backgroundColor,
            activeNavColor: getComputedStyle(activeNav).color,
            presetTransition: preset ? getComputedStyle(preset).transitionDuration : '',
          };
          document.startViewTransition = originalStartViewTransition;
          return state;
        }
        """
    )
    assert_true(
        fallback_state["theme"] == "dark"
        and fallback_state["commitClass"] is True
        and fallback_state["activeNavTransition"] == "0s"
        and fallback_state["activeNavBg"] == "rgb(33, 33, 33)"
        and fallback_state["activeNavColor"] == "rgb(231, 231, 231)"
        and fallback_state["presetTransition"] in {"", "0s"},
        f"fallback theme switch should keep element transitions disabled through the theme window: {fallback_state}",
    )
    page.wait_for_timeout(180)
    rapid_toggle_state = page.evaluate(
        """
        async () => {
          const dark = document.querySelector('[data-theme-mode="dark"]');
          const light = document.querySelector('[data-theme-mode="light"]');
          dark.click();
          while (document.documentElement.dataset.theme !== 'dark') {
            await new Promise(resolve => requestAnimationFrame(resolve));
          }
          light.click();
          while (document.documentElement.dataset.theme !== 'light') {
            await new Promise(resolve => requestAnimationFrame(resolve));
          }
          return {
            theme: document.documentElement.dataset.theme || '',
            hasViewTransition: typeof document.startViewTransition === 'function',
            transitioningClass: document.documentElement.classList.contains('theme-transitioning'),
            bodyBg: getComputedStyle(document.body).backgroundColor,
            headingColor: getComputedStyle(document.querySelector('h1')).color,
          };
        }
        """
    )
    assert_true(
        rapid_toggle_state == {
            "theme": "light",
            "hasViewTransition": True,
            "transitioningClass": False,
            "bodyBg": "rgb(246, 246, 244)",
            "headingColor": "rgb(32, 33, 31)",
        },
        f"theme toggles should update DOM colors immediately without theme-transitioning state: {rapid_toggle_state}",
    )
    dark_text_state = page.evaluate(
        """
        async () => {
          document.querySelector('[data-theme-mode="dark"]').click();
          while (document.documentElement.dataset.theme !== 'dark') {
            await new Promise(resolve => requestAnimationFrame(resolve));
          }
          await new Promise(resolve => requestAnimationFrame(resolve));
          return {
            theme: document.documentElement.dataset.theme || '',
            transitioningClass: document.documentElement.classList.contains('theme-transitioning'),
            headingColor: getComputedStyle(document.querySelector('h1')).color,
            panelTextColor: getComputedStyle(document.querySelector('.panel')).color,
            bodyBg: getComputedStyle(document.body).backgroundColor,
            panelBg: getComputedStyle(document.querySelector('.panel')).backgroundColor,
            expectedText: getComputedStyle(document.documentElement).getPropertyValue('--text').trim(),
          };
        }
        """
    )
    assert_true(
        dark_text_state["theme"] == "dark"
        and dark_text_state["transitioningClass"] is False
        and dark_text_state["headingColor"] == "rgb(231, 231, 231)"
        and dark_text_state["panelTextColor"] == "rgb(231, 231, 231)"
        and dark_text_state["bodyBg"] == "rgb(15, 15, 15)"
        and dark_text_state["panelBg"] == "rgb(24, 24, 24)"
        and dark_text_state["expectedText"] == "#e7e7e7",
        f"dark mode DOM colors should apply immediately while the view snapshot animates: {dark_text_state}",
    )
    page.locator('[data-theme-mode="light"]').click()
    page.wait_for_timeout(180)


def check_theme_text_contrast_across_views(page) -> None:
    views = ("overview", "turns", "tools", "subagents", "cleanup")
    for view in views:
        page.locator('[data-theme-mode="light"]').click()
        page.wait_for_timeout(180)
        page.locator(f'button[data-view-target="{view}"]').click()
        if view == "cleanup":
            page.wait_for_selector("#cleanup-files tr[data-cleanup-file]", timeout=10_000)
        page.wait_for_timeout(100)
        frames = page.evaluate(
            """
            async () => {
              function parseRgb(value) {
                const match = value.match(/rgba?\\(([^)]+)\\)/);
                if (!match) return null;
                const parts = match[1].split(',').map(part => Number(part.trim()));
                return {r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1};
              }
              function luminance(color) {
                const values = [color.r, color.g, color.b].map(value => {
                  value /= 255;
                  return value <= 0.03928 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
                });
                return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2];
              }
              function contrast(fg, bg) {
                const first = luminance(fg);
                const second = luminance(bg);
                return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
              }
              function backgroundFor(el) {
                let current = el;
                while (current) {
                  const bg = parseRgb(getComputedStyle(current).backgroundColor);
                  if (bg && bg.a > 0.5) return bg;
                  current = current.parentElement;
                }
                return parseRgb(getComputedStyle(document.body).backgroundColor);
              }
              document.querySelector('[data-theme-mode="dark"]').click();
              while (document.documentElement.dataset.theme !== 'dark') {
                await new Promise(resolve => requestAnimationFrame(resolve));
              }
              const frames = [];
              for (let i = 0; i < 4; i += 1) {
                await new Promise(resolve => requestAnimationFrame(resolve));
                const visible = [...document.querySelectorAll('body *')].filter(el => {
                  const rect = el.getBoundingClientRect();
                  const hasOwnText = [...el.childNodes].some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
                  return rect.width > 1
                    && rect.height > 1
                    && rect.bottom >= 0
                    && rect.right >= 0
                    && rect.top <= innerHeight
                    && rect.left <= innerWidth
                    && hasOwnText;
                }).map(el => {
                  const style = getComputedStyle(el);
                  const fg = parseRgb(style.color);
                  const bg = backgroundFor(el);
                  return {
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    className: typeof el.className === 'string' ? el.className : '',
                    ariaSelected: el.getAttribute('aria-selected') || '',
                    ariaPressed: el.getAttribute('aria-pressed') || '',
                    text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 48),
                    color: style.color,
                    background: `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`,
                    contrast: Math.round(contrast(fg, bg) * 100) / 100,
                  };
                });
                frames.push({
                  frame: i,
                  totalVisible: visible.length,
                  lowContrast: visible.filter(item => item.contrast < 3).slice(0, 20),
                });
              }
              return frames;
            }
            """
        )
        low_contrast = [item for frame in frames for item in frame["lowContrast"]]
        assert_true(not low_contrast, f"{view} theme switch should not pass through unreadable text contrast: {frames}")
        page.wait_for_timeout(180)
    page.locator('[data-theme-mode="light"]').click()
    page.wait_for_timeout(180)
    page.locator('button[data-view-target="overview"]').click()


def check_theme_toggle_inactive_icon_contrast(page) -> None:
    contrast_state = page.evaluate(
        """
        async () => {
          function parseRgb(value) {
            const match = value.match(/rgba?\\(([^)]+)\\)/);
            if (!match) return null;
            const parts = match[1].split(',').map(part => Number(part.trim()));
            return {r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1};
          }
          function composite(fg, bg, opacity) {
            const alpha = Math.min(1, Math.max(0, Number(opacity) * Number(fg.a ?? 1)));
            return {
              r: fg.r * alpha + bg.r * (1 - alpha),
              g: fg.g * alpha + bg.g * (1 - alpha),
              b: fg.b * alpha + bg.b * (1 - alpha),
              a: 1,
            };
          }
          function luminance(color) {
            const values = [color.r, color.g, color.b].map(value => {
              value /= 255;
              return value <= 0.03928 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
            });
            return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2];
          }
          function contrast(fg, bg) {
            const first = luminance(fg);
            const second = luminance(bg);
            return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
          }
          function backgroundFor(el) {
            let current = el;
            while (current) {
              const bg = parseRgb(getComputedStyle(current).backgroundColor);
              if (bg && bg.a > 0.5) return bg;
              current = current.parentElement;
            }
            return parseRgb(getComputedStyle(document.body).backgroundColor);
          }
          async function sample(theme, inactiveSelector) {
            document.querySelector(`[data-theme-mode="${theme}"]`).click();
            while (document.documentElement.dataset.theme !== theme) {
              await new Promise(resolve => requestAnimationFrame(resolve));
            }
            const button = document.querySelector(inactiveSelector);
            const style = getComputedStyle(button);
            const fg = parseRgb(style.color);
            const bg = backgroundFor(button);
            const effective = composite(fg, bg, style.opacity);
            return {
              theme,
              selector: inactiveSelector,
              color: style.color,
              opacity: style.opacity,
              background: `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`,
              contrast: Math.round(contrast(effective, bg) * 100) / 100,
            };
          }
          return [
            await sample('light', '[data-theme-mode="dark"]'),
            await sample('dark', '[data-theme-mode="light"]'),
          ];
        }
        """
    )
    low_contrast = [item for item in contrast_state if item["contrast"] < 3]
    assert_true(not low_contrast, f"inactive theme toggle icons should keep 3:1 contrast: {contrast_state}")
    page.locator('[data-theme-mode="light"]').click()
    page.wait_for_function("() => document.documentElement.dataset.theme === 'light'", timeout=2_000)


def check_toolbar(page) -> None:
    toolbar_dashboard_requests: list[str] = []
    page.on(
        "request",
        lambda request: toolbar_dashboard_requests.append(request.url)
        if "/api/dashboard?" in request.url
        else None,
    )
    toolbar_dashboard_requests.clear()
    toolbar_height = page.locator(".toolbar").bounding_box()["height"]
    analyze_button_state = page.evaluate(
        """
        () => {
          const button = document.querySelector('#rebuild');
          const idleWidth = Math.round(button.getBoundingClientRect().width * 1000) / 1000;
          setAnalyzeButtonState('running', 'normalizing raw logs · 0s', 0, 12);
          const runningWidth = Math.round(button.getBoundingClientRect().width * 1000) / 1000;
          const runningLabel = button.querySelector('.analyze-button-label').textContent;
          const runningAria = button.getAttribute('aria-label');
          setAnalyzeButtonState('idle', 'Analyze');
          return {idleWidth, runningWidth, runningLabel, runningAria};
        }
        """
    )
    assert_true(analyze_button_state["runningLabel"] == "Cancel", f"analyze button should show Cancel while running: {analyze_button_state}")
    assert_true(
        analyze_button_state["idleWidth"] == analyze_button_state["runningWidth"],
        f"analyze button size changed when showing Cancel: {analyze_button_state}",
    )
    assert_true("Cancel analysis" in analyze_button_state["runningAria"], f"analyze cancel action is not exposed to assistive tech: {analyze_button_state}")
    page.set_viewport_size({"width": 1280, "height": 720})
    compact_appbar_state = page.evaluate(
        """
        () => {
          const appbar = document.querySelector('.appbar').getBoundingClientRect();
          const toolbar = document.querySelector('.toolbar').getBoundingClientRect();
          const rebuild = document.querySelector('#rebuild').getBoundingClientRect();
          const title = document.querySelector('h1').getBoundingClientRect();
          return {
            viewportWidth: window.innerWidth,
            appbarLeft: Math.round(appbar.left),
            appbarRight: Math.round(appbar.right),
            toolbarRight: Math.round(toolbar.right),
            rebuildRight: Math.round(rebuild.right),
            titleLeft: Math.round(title.left),
            bodyScrollWidth: document.documentElement.scrollWidth,
            bodyClientWidth: document.documentElement.clientWidth,
          };
        }
        """
    )
    assert_true(
        compact_appbar_state["appbarLeft"] >= 0
        and compact_appbar_state["titleLeft"] >= 0
        and compact_appbar_state["appbarRight"] <= compact_appbar_state["viewportWidth"]
        and compact_appbar_state["toolbarRight"] <= compact_appbar_state["viewportWidth"]
        and compact_appbar_state["rebuildRight"] <= compact_appbar_state["viewportWidth"]
        and compact_appbar_state["bodyScrollWidth"] == compact_appbar_state["bodyClientWidth"],
        f"compact desktop appbar should fit inside the viewport: {compact_appbar_state}",
    )
    page.set_viewport_size({"width": 1440, "height": 900})
    theme_initial_state = page.evaluate(
        """
        () => ({
          selectCount: document.querySelectorAll('#theme-mode').length,
          toggleTop: Math.round(document.querySelector('.theme-toggle').getBoundingClientRect().top),
          titleBottom: Math.round(document.querySelector('h1').getBoundingClientRect().bottom),
          appbarBottom: Math.round(document.querySelector('.appbar').getBoundingClientRect().bottom),
          appbarHeight: Math.round(document.querySelector('.appbar').getBoundingClientRect().height),
          subrowCount: document.querySelectorAll('.appbar-subrow').length,
          firstContentTop: Math.round(document.querySelector('.metric-strip').getBoundingClientRect().top),
          switcherRight: Math.round(document.querySelector('.theme-switcher').getBoundingClientRect().right),
          labelCount: document.querySelectorAll('.theme-toggle-label').length,
          lightText: document.querySelector('[data-theme-mode="light"] .theme-toggle-text').textContent,
          darkText: document.querySelector('[data-theme-mode="dark"] .theme-toggle-text').textContent,
          lightTextDisplay: getComputedStyle(document.querySelector('[data-theme-mode="light"] .theme-toggle-text')).display,
          toggleBackground: getComputedStyle(document.querySelector('.theme-toggle')).backgroundColor,
          toggleBorderTopWidth: getComputedStyle(document.querySelector('.theme-toggle')).borderTopWidth,
          activeButtonBackground: getComputedStyle(document.querySelector('[data-theme-mode="light"]')).backgroundColor,
          activeButtonShadow: getComputedStyle(document.querySelector('[data-theme-mode="light"]')).boxShadow,
          activeButtonOpacity: getComputedStyle(document.querySelector('[data-theme-mode="light"]')).opacity,
          toggleBottom: Math.round(document.querySelector('.theme-toggle').getBoundingClientRect().bottom),
          toggleLeft: Math.round(document.querySelector('.theme-toggle').getBoundingClientRect().left),
          navLeft: Math.round(document.querySelector('.page-nav').getBoundingClientRect().left),
          toggleRight: Math.round(document.querySelector('.theme-toggle').getBoundingClientRect().right),
          viewportWidth: window.innerWidth,
          viewportHeight: window.innerHeight,
          pageWidth: document.documentElement.clientWidth,
          lightPressed: document.querySelector('[data-theme-mode="light"]').getAttribute('aria-pressed'),
          darkPressed: document.querySelector('[data-theme-mode="dark"]').getAttribute('aria-pressed'),
        })
        """
    )
    assert_true(theme_initial_state["selectCount"] == 0, f"theme mode should not use a select control: {theme_initial_state}")
    assert_true(70 <= theme_initial_state["appbarHeight"] <= 74, f"theme toggle should not collapse or stretch the header: {theme_initial_state}")
    assert_true(theme_initial_state["subrowCount"] == 0, f"theme toggle should not reserve a header subrow: {theme_initial_state}")
    assert_true(theme_initial_state["toggleTop"] > theme_initial_state["appbarBottom"], f"bottom theme toggle should sit outside the header: {theme_initial_state}")
    assert_true(theme_initial_state["toggleBottom"] > theme_initial_state["firstContentTop"], f"theme toggle should live near the bottom viewport edge: {theme_initial_state}")
    assert_true(theme_initial_state["labelCount"] == 0, f"theme control should not show a separate Theme label: {theme_initial_state}")
    assert_true(theme_initial_state["lightText"] == "Light", f"light theme button should keep accessible text content: {theme_initial_state}")
    assert_true(theme_initial_state["darkText"] == "Dark", f"dark theme button should keep accessible text content: {theme_initial_state}")
    assert_true(theme_initial_state["lightTextDisplay"] == "none", f"desktop theme toggle should render icon-only controls: {theme_initial_state}")
    assert_true(theme_initial_state["toggleBackground"] == "rgba(0, 0, 0, 0)", f"theme toggle frame should blend into the page background: {theme_initial_state}")
    assert_true(theme_initial_state["toggleBorderTopWidth"] == "0px", f"theme toggle frame should not draw a separate border: {theme_initial_state}")
    assert_true(theme_initial_state["activeButtonBackground"] == "rgba(0, 0, 0, 0)", f"active theme button should not draw a separate background: {theme_initial_state}")
    assert_true(theme_initial_state["activeButtonShadow"] == "none", f"active theme button should not draw a selected inset: {theme_initial_state}")
    assert_true(theme_initial_state["activeButtonOpacity"] == "0.96", f"active theme button should use opacity instead of a filled state: {theme_initial_state}")
    assert_true(theme_initial_state["toggleLeft"] > theme_initial_state["navLeft"], f"theme toggle should stay in the trailing appbar area: {theme_initial_state}")
    assert_true(theme_initial_state["switcherRight"] == theme_initial_state["toggleRight"], f"theme switcher should align as one trailing group: {theme_initial_state}")
    assert_true(20 <= theme_initial_state["viewportWidth"] - theme_initial_state["toggleRight"] <= 24, f"theme toggle should sit slightly away from the viewport right edge: {theme_initial_state}")
    assert_true(18 <= theme_initial_state["viewportHeight"] - theme_initial_state["toggleBottom"] <= 22, f"theme toggle should sit near the bottom viewport edge: {theme_initial_state}")
    assert_true(theme_initial_state["lightPressed"] == "true" and theme_initial_state["darkPressed"] == "false", f"light mode should be active by default: {theme_initial_state}")
    page.locator('[data-theme-mode="dark"]').click()
    page.wait_for_function(
        "() => document.documentElement.dataset.theme === 'dark' && JSON.parse(localStorage.getItem('codex-token-usage-dashboard-settings') || '{}').themeMode === 'dark'",
        timeout=2_000,
    )
    theme_dark_state = page.evaluate(
        """
        () => ({
          theme: document.documentElement.dataset.theme || '',
          lightPressed: document.querySelector('[data-theme-mode="light"]').getAttribute('aria-pressed'),
          darkPressed: document.querySelector('[data-theme-mode="dark"]').getAttribute('aria-pressed'),
          stored: JSON.parse(localStorage.getItem('codex-token-usage-dashboard-settings') || '{}').themeMode || '',
        })
        """
    )
    assert_true(theme_dark_state == {"theme": "dark", "lightPressed": "false", "darkPressed": "true", "stored": "dark"}, f"dark icon should activate dark mode: {theme_dark_state}")
    page.locator('[data-theme-mode="light"]').click()
    page.wait_for_function(
        "() => document.documentElement.dataset.theme === 'light' && JSON.parse(localStorage.getItem('codex-token-usage-dashboard-settings') || '{}').themeMode === 'light'",
        timeout=2_000,
    )
    theme_light_state = page.evaluate(
        """
        () => ({
          theme: document.documentElement.dataset.theme || '',
          lightPressed: document.querySelector('[data-theme-mode="light"]').getAttribute('aria-pressed'),
          darkPressed: document.querySelector('[data-theme-mode="dark"]').getAttribute('aria-pressed'),
          stored: JSON.parse(localStorage.getItem('codex-token-usage-dashboard-settings') || '{}').themeMode || '',
        })
        """
    )
    assert_true(theme_light_state == {"theme": "light", "lightPressed": "true", "darkPressed": "false", "stored": "light"}, f"light icon should reactivate light mode: {theme_light_state}")
    check_theme_transition_stability(page)
    check_theme_text_contrast_across_views(page)
    check_theme_toggle_inactive_icon_contrast(page)
    toolbar_dashboard_requests.clear()
    page.locator("#days").select_option("custom")
    page.wait_for_selector("#custom-days-popover:not([hidden])", timeout=5_000)
    page.locator("#custom-days-input").focus()
    custom_days_open_state = page.evaluate(
        """
        () => ({
          toolbarHeight: document.querySelector('.toolbar').getBoundingClientRect().height,
          selectValue: document.querySelector('#days').value,
          popoverHidden: document.querySelector('#custom-days-popover').hidden,
          inputValue: document.querySelector('#custom-days-input').value,
          unit: document.querySelector('#custom-days-popover .toolbar-custom-unit').textContent,
          controlWidth: Math.round(document.querySelector('#custom-days-popover').closest('.custom-filter-control').getBoundingClientRect().width * 1000) / 1000,
          popoverWidth: Math.round(document.querySelector('#custom-days-popover').getBoundingClientRect().width * 1000) / 1000,
          valueBottom: Math.round(document.querySelector('#custom-days-popover .toolbar-custom-value').getBoundingClientRect().bottom * 1000) / 1000,
          applyTop: Math.round(document.querySelector('#custom-days-apply').getBoundingClientRect().top * 1000) / 1000,
          inputFont: getComputedStyle(document.querySelector('#custom-days-input')).fontSize,
          applyWidth: Math.round(document.querySelector('#custom-days-apply').getBoundingClientRect().width * 1000) / 1000,
          focusOutline: getComputedStyle(document.querySelector('#custom-days-popover .toolbar-custom-value')).outlineStyle,
          focusShadow: getComputedStyle(document.querySelector('#custom-days-popover .toolbar-custom-value')).boxShadow,
        })
        """
    )
    assert_true(custom_days_open_state["toolbarHeight"] == toolbar_height, f"custom days popover should not move toolbar: {custom_days_open_state}")
    assert_true(custom_days_open_state["selectValue"] == "custom", f"custom days should show Custom while editing: {custom_days_open_state}")
    assert_true(not custom_days_open_state["popoverHidden"], f"custom days popover should open below select: {custom_days_open_state}")
    assert_true(custom_days_open_state["popoverWidth"] == custom_days_open_state["controlWidth"], f"custom days popover should match control width: {custom_days_open_state}")
    assert_true(custom_days_open_state["applyTop"] > custom_days_open_state["valueBottom"], f"custom days apply should sit below the input row: {custom_days_open_state}")
    assert_true(custom_days_open_state["applyWidth"] < custom_days_open_state["popoverWidth"], f"custom days apply should fit inside panel padding: {custom_days_open_state}")
    assert_true(custom_days_open_state["inputFont"] == "13px", f"custom days input should use compact readable numeric type: {custom_days_open_state}")
    assert_true(custom_days_open_state["focusOutline"] == "none", f"custom days input group should not use a heavy outline: {custom_days_open_state}")
    assert_true("inset" in custom_days_open_state["focusShadow"], f"custom days input group should use a subtle inset focus ring: {custom_days_open_state}")
    assert_true(custom_days_open_state["inputValue"] == "7", f"custom days editor should start from stored value: {custom_days_open_state}")
    assert_true(custom_days_open_state["unit"] == "Days", f"custom days editor should show the unit: {custom_days_open_state}")
    assert_true(not toolbar_dashboard_requests, f"opening custom days should not load before apply: {toolbar_dashboard_requests}")
    page.locator("#custom-days-input").fill("14")
    page.locator("#custom-days-input").press("Enter")
    page.wait_for_function(
        "requests => requests.some((url) => url.includes('days=14'))",
        arg=toolbar_dashboard_requests,
        timeout=10_000,
    )
    custom_days_state = page.evaluate(
        """
        () => ({
          toolbarHeight: document.querySelector('.toolbar').getBoundingClientRect().height,
          daysValue: document.querySelector('#days').value,
          customDays: document.querySelector('#custom-days').value,
          label: document.querySelector('#days').options[document.querySelector('#days').selectedIndex].textContent,
          customOptions: Array.from(document.querySelectorAll('#days option')).filter((option) => option.value.includes('custom') && !option.hidden).length,
        })
        """
    )
    assert_true(custom_days_state["customOptions"] == 1, f"time range should expose one Custom option: {custom_days_state}")
    assert_true(custom_days_state["daysValue"] == "custom", f"custom days should stay selected after prompt apply: {custom_days_state}")
    assert_true(custom_days_state["customDays"] == "14", f"custom days should store prompt value: {custom_days_state}")
    assert_true(custom_days_state["label"] == "~ 14 Days", f"custom days label should show applied value: {custom_days_state}")
    assert_true(custom_days_state["toolbarHeight"] == toolbar_height, f"custom prompt should not change toolbar row height: {custom_days_state}")
    page.locator("#days").dispatch_event("pointerdown")
    page.locator("#days").select_option("custom")
    page.wait_for_selector("#custom-days-popover:not([hidden])", timeout=5_000)
    page.locator("#custom-days-input").fill("21")
    page.locator("#custom-days-apply").click()
    page.wait_for_function(
        "requests => requests.some((url) => url.includes('days=21'))",
        arg=toolbar_dashboard_requests,
        timeout=10_000,
    )
    custom_days_reselect_state = page.evaluate(
        """
        () => ({
          daysValue: document.querySelector('#days').value,
          customDays: document.querySelector('#custom-days').value,
          label: document.querySelector('#days').options[document.querySelector('#days').selectedIndex].textContent,
        })
        """
    )
    assert_true(custom_days_reselect_state["daysValue"] == "custom", f"reselecting custom days should keep custom selected: {custom_days_reselect_state}")
    assert_true(custom_days_reselect_state["customDays"] == "21", f"reselecting custom days should update value: {custom_days_reselect_state}")
    assert_true(custom_days_reselect_state["label"] == "~ 21 Days", f"reselecting custom days should update label: {custom_days_reselect_state}")
    page.locator("#days").dispatch_event("pointerdown")
    page.locator("#days").select_option("custom")
    page.wait_for_selector("#custom-days-popover:not([hidden])", timeout=5_000)
    page.locator("#custom-days-input").fill("")
    empty_custom_request_count = len(toolbar_dashboard_requests)
    with page.expect_response(lambda response: "/api/dashboard?" in response.url and "days=7" in response.url, timeout=10_000):
        page.locator("#custom-days-apply").click()
    empty_custom_days_state = page.evaluate(
        """
        () => ({
          daysValue: document.querySelector('#days').value,
          customDays: document.querySelector('#custom-days').value,
          label: document.querySelector('#days option[value="custom"]').textContent,
          selectedLabel: document.querySelector('#days').options[document.querySelector('#days').selectedIndex].textContent,
          popoverHidden: document.querySelector('#custom-days-popover').hidden,
          storedDays: JSON.parse(localStorage.getItem('codex-token-usage-dashboard-settings') || '{}').days,
          storedCustomDays: JSON.parse(localStorage.getItem('codex-token-usage-dashboard-settings') || '{}').customDays,
        })
        """
    )
    assert_true(empty_custom_days_state["daysValue"] == "7", f"empty custom days should return to the 7 Days preset: {empty_custom_days_state}")
    assert_true(empty_custom_days_state["customDays"] == "7", f"empty custom days should clear the previous custom value: {empty_custom_days_state}")
    assert_true(empty_custom_days_state["label"] == "Custom", f"empty custom days should reset the Custom option label: {empty_custom_days_state}")
    assert_true(empty_custom_days_state["selectedLabel"] == "7 Days", f"empty custom days should immediately show 7 Days as the selected label: {empty_custom_days_state}")
    assert_true(empty_custom_days_state["popoverHidden"], f"empty custom days should close the editor: {empty_custom_days_state}")
    assert_true(empty_custom_days_state["storedDays"] == "7", f"empty custom days should persist the default preset: {empty_custom_days_state}")
    assert_true(empty_custom_days_state["storedCustomDays"] == "7", f"empty custom days should persist the cleared value: {empty_custom_days_state}")
    assert_true(len(toolbar_dashboard_requests) > empty_custom_request_count, f"empty custom days should reload with the default preset: {toolbar_dashboard_requests}")

    removed_scope_state = page.evaluate(
        """
        () => ({
          rowsControl: !!document.querySelector('#rows'),
          percentPopover: !!document.querySelector('#custom-percent-popover'),
          storedRows: JSON.parse(localStorage.getItem('codex-token-usage-dashboard-settings') || '{}').rows,
          storedCustomPercent: JSON.parse(localStorage.getItem('codex-token-usage-dashboard-settings') || '{}').customPercent,
        })
        """
    )
    assert_true(
        removed_scope_state == {
            "rowsControl": False,
            "percentPopover": False,
            "storedRows": None,
            "storedCustomPercent": None,
        },
        f"analysis percent scope controls should be removed from the toolbar: {removed_scope_state}",
    )
    assert_true(not any("limit_percent=" in url for url in toolbar_dashboard_requests), f"dashboard requests should not send removed limit_percent: {toolbar_dashboard_requests}")
