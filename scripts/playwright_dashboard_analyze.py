"""Analyze browser checks for the Codex Token Bola dashboard."""

from __future__ import annotations

from playwright_dashboard_helpers import assert_true, open_dashboard


def check_analyze_cancel_failure_is_local(page, base_url: str) -> None:
    open_dashboard(page, base_url)
    page.evaluate(
        """
        () => {
          window.__analyzeOriginalFetch = window.fetch;
          window.__analyzeCancelRequests = 0;
          window.__analyzeProgressRequests = 0;
          window.__analyzeRebuildPending = false;
          window.fetch = (input, init) => {
            const url = new URL(typeof input === 'string' ? input : input.url, location.origin);
            if (url.pathname === '/api/rebuild/cancel') {
              window.__analyzeCancelRequests += 1;
              return Promise.resolve(new Response(
                JSON.stringify({error: 'mock cancel failed'}),
                {status: 500, headers: {'Content-Type': 'application/json'}}
              ));
            }
            if (url.pathname === '/api/rebuild/progress') {
              window.__analyzeProgressRequests += 1;
              return window.__analyzeOriginalFetch(input, init);
            }
            if (url.pathname === '/api/rebuild' && String((init || {}).method || 'GET').toUpperCase() === 'POST') {
              window.__analyzeRebuildPending = true;
              return new Promise(resolve => {
                window.__resolveAnalyzeRebuild = () => resolve(new Response(
                  JSON.stringify({
                    new_turn_rows: 0,
                    post_analysis_compact: {},
                    quarantine: {unacknowledged_events: 0},
                    data_health: 'ok',
                  }),
                  {status: 200, headers: {'Content-Type': 'application/json'}}
                ));
              });
            }
            return window.__analyzeOriginalFetch(input, init);
          };
        }
        """
    )
    try:
        page.locator("#rebuild").click()
        page.wait_for_function("() => window.__analyzeRebuildPending === true", timeout=5_000)
        page.wait_for_function(
            "() => document.querySelector('#rebuild .analyze-button-label').textContent === 'Cancel'",
            timeout=5_000,
        )
        page.locator("#rebuild").click()
        page.wait_for_function("() => window.__analyzeCancelRequests === 1", timeout=5_000)
        page.wait_for_function(
            "() => document.querySelector('#query-status').textContent.includes('Cancel failed')",
            timeout=5_000,
        )
        failure_state = page.evaluate(
            """
            () => ({
              label: document.querySelector('#rebuild .analyze-button-label').textContent,
              analyzeState: document.querySelector('#rebuild').dataset.analyzeState,
              disabled: document.querySelector('#rebuild').disabled,
              panelErrors: document.querySelectorAll('main .error').length,
              queryStatus: document.querySelector('#query-status').textContent,
            })
            """
        )
        assert_true(
            failure_state["label"] == "Cancel"
            and failure_state["analyzeState"] == "running"
            and not failure_state["disabled"]
            and failure_state["panelErrors"] == 0
            and "mock cancel failed" in failure_state["queryStatus"],
            f"cancel failure should remain local while analysis continues: {failure_state}",
        )
        page.wait_for_function("() => window.__analyzeProgressRequests > 0", timeout=5_000)
        page.locator("#rebuild").click()
        page.wait_for_function("() => window.__analyzeCancelRequests === 2", timeout=5_000)
        page.evaluate("() => window.__resolveAnalyzeRebuild()")
        page.wait_for_function(
            "() => document.querySelector('#rebuild .analyze-button-label').textContent === 'Analyze'",
            timeout=10_000,
        )
    finally:
        page.evaluate(
            """
            () => {
              if (window.__analyzeOriginalFetch) window.fetch = window.__analyzeOriginalFetch;
              delete window.__analyzeOriginalFetch;
              delete window.__resolveAnalyzeRebuild;
            }
            """
        )
