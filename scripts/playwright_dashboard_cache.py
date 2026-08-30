"""Browser checks for dashboard query caching and atomic page transitions."""

from __future__ import annotations

from playwright_dashboard_helpers import assert_true, open_dashboard, set_dashboard_select


def check_query_cache_runtime(page, base_url: str) -> None:
    open_dashboard(page, base_url)
    result = page.evaluate(
        """
        async () => {
          const module = await import('/assets/dashboard/query-cache.js');
          let clock = 100;
          let fetches = 0;
          let release;
          const cache = module.createQueryCache({
            fetcher: () => {
              fetches += 1;
              return new Promise(resolve => { release = resolve; });
            },
            maxEntries: 2,
            maxBytes: 1024,
            ttlMs: 10,
            now: () => clock,
          });
          const first = cache.load('/api/example?b=2&a=1');
          const second = cache.load('/api/example?a=1&b=2');
          await Promise.resolve();
          const deduped = fetches === 1 && cache.stats().inFlight === 1;
          release({value: 1});
          await Promise.all([first, second]);
          const cached = (await cache.load('/api/example?a=1&b=2')).value === 1 && fetches === 1;
          clock = 111;
          const expiredLoad = cache.load('/api/example?a=1&b=2');
          await Promise.resolve();
          const expired = fetches === 2;
          release({value: 2});
          await expiredLoad;

          const lru = module.createQueryCache({maxEntries: 2, maxBytes: 1024, ttlMs: 100, now: () => 0});
          lru.prime('/a', {value: 'a'});
          lru.prime('/b', {value: 'b'});
          lru.peek('/a');
          lru.prime('/c', {value: 'c'});
          const lruEvicted = lru.peek('/a').hit && !lru.peek('/b').hit && lru.peek('/c').hit;

          const bounded = module.createQueryCache({maxEntries: 10, maxBytes: 40, ttlMs: 100, now: () => 0});
          const oversizedBypassed = !bounded.prime('/huge', {value: 'x'.repeat(100)}) && bounded.stats().entries === 0;

          const generated = module.createQueryCache({maxEntries: 10, maxBytes: 1024, ttlMs: 100, now: () => 0});
          generated.setGeneration('one');
          generated.prime('/stable', {value: 1});
          const sameGenerationKept = !generated.setGeneration('one') && generated.peek('/stable').hit;
          const generationChanged = generated.setGeneration('two') && !generated.peek('/stable').hit;

          let releaseStale;
          const invalidated = module.createQueryCache({
            fetcher: () => new Promise(resolve => { releaseStale = resolve; }),
            maxEntries: 10,
            maxBytes: 1024,
            ttlMs: 100,
            now: () => 0,
          });
          const staleLoad = invalidated.load('/stale');
          await Promise.resolve();
          invalidated.clear();
          releaseStale({value: 'stale'});
          await staleLoad;
          const staleResultBypassed = invalidated.stats().entries === 0;

          return {
            canonical: module.canonicalQueryKey('/api/example?b=2&a=1') === '/api/example?a=1&b=2',
            deduped,
            cached,
            expired,
            lruEvicted,
            oversizedBypassed,
            sameGenerationKept,
            generationChanged,
            staleResultBypassed,
          };
        }
        """
    )
    assert_true(all(result.values()), f"query cache contract failed: {result}")


def check_atomic_turn_pagination(page, base_url: str) -> None:
    open_dashboard(page, base_url)
    page.locator('button[data-view-target="turns"]').click()
    page.evaluate(
        """
        () => {
          window.__queryRequests = [];
          window.__holdNextPage = true;
          window.__originalFetch = window.fetch;
          window.fetch = (input, init) => {
            const url = new URL(typeof input === 'string' ? input : input.url, location.origin);
            window.__queryRequests.push(url.pathname + url.search);
            if (url.pathname === '/api/turns' && url.searchParams.get('page') === '2' && window.__holdNextPage) {
              return new Promise((resolve, reject) => {
                window.__releaseNextPage = () => window.__originalFetch(input, init).then(resolve, reject);
              });
            }
            return window.__originalFetch(input, init);
          };
        }
        """
    )
    set_dashboard_select(page, "#turn-page-size", "10")
    page.wait_for_function("() => document.querySelectorAll('#turn-list tr[data-turn]').length === 10", timeout=10_000)
    total = int((page.locator("#turn-pager .page-status").inner_text().split("/")[-1]).replace(",", "").strip())
    if total <= 10:
        page.evaluate("() => { window.fetch = window.__originalFetch; }")
        return

    page.wait_for_function("() => typeof window.__releaseNextPage === 'function'", timeout=10_000)
    page.evaluate("() => { window.__queryRequests = []; }")
    old_first = page.locator("#turn-list tr[data-turn]").first.get_attribute("data-turn")
    old_detail = page.locator("#detail").inner_text()
    page.locator("#next-page").click()
    pending = page.evaluate(
        """
        () => ({
          first: document.querySelector('#turn-list tr[data-turn]')?.dataset.turn || '',
          detail: document.querySelector('#detail')?.innerText || '',
          busy: document.querySelector('#turn-pager')?.getAttribute('aria-busy'),
          summaryLoading: Boolean(document.querySelector('#summary .loading, #summary .loading-line')),
        })
        """
    )
    assert_true(pending["first"] == old_first, f"uncached transition replaced turn rows early: {pending}")
    assert_true(pending["detail"] == old_detail, f"uncached transition replaced detail early: {pending}")
    assert_true(pending["busy"] == "true" and not pending["summaryLoading"], f"pager busy state was not isolated: {pending}")

    page.evaluate("() => { window.__holdNextPage = false; window.__releaseNextPage(); }")
    expected_end = min(20, total)
    page.wait_for_function(
        "expected => document.querySelector('#turn-pager .page-status')?.textContent.includes(expected)",
        arg=f"11-{expected_end}",
        timeout=10_000,
    )
    new_first = page.locator("#turn-list tr[data-turn]").first.get_attribute("data-turn")
    assert_true(new_first != old_first, "completed page transition did not replace turn rows")

    before_back = page.evaluate("() => window.__queryRequests.filter(path => path.startsWith('/api/turns?') && new URL(path, location.origin).searchParams.get('page') === '1').length")
    page.locator("#prev-page").click()
    page.wait_for_function("() => document.querySelector('#turn-pager .page-status')?.textContent.startsWith('1-10')", timeout=10_000)
    after_back = page.evaluate("() => window.__queryRequests.filter(path => path.startsWith('/api/turns?') && new URL(path, location.origin).searchParams.get('page') === '1').length")
    dashboard_requests = page.evaluate("() => window.__queryRequests.filter(path => path.startsWith('/api/dashboard?')).length")
    assert_true(after_back == before_back, "cached back navigation fetched page 1 again")
    assert_true(dashboard_requests == 0, f"turn pagination refetched dashboard payload: {dashboard_requests}")
    page.evaluate("() => { window.fetch = window.__originalFetch; delete window.__originalFetch; }")


def check_stale_dashboard_cannot_overwrite_sort(page, base_url: str) -> None:
    open_dashboard(page, base_url)
    page.locator('button[data-view-target="turns"]').click()
    page.evaluate(
        """
        async () => {
          const cache = await import('/assets/dashboard/query-cache.js');
          cache.clearAnalyticsQueryCache();
          window.__originalFetch = window.fetch;
          window.__holdDashboard = true;
          window.fetch = (input, init) => {
            const url = new URL(typeof input === 'string' ? input : input.url, location.origin);
            if (url.pathname === '/api/dashboard' && window.__holdDashboard) {
              return new Promise((resolve, reject) => {
                window.__releaseDashboard = () => window.__originalFetch(input, init).then(resolve, reject);
              });
            }
            return window.__originalFetch(input, init);
          };
        }
        """
    )
    page.locator("#refresh").click()
    page.wait_for_function("() => typeof window.__releaseDashboard === 'function'", timeout=10_000)
    page.locator('#turn-list [data-turn-sort="credits"]').click()
    page.wait_for_function(
        "() => document.querySelector('#turn-list th[aria-sort=\"descending\"] [data-turn-sort=\"credits\"]') !== null",
        timeout=10_000,
    )
    expected_ids = page.locator("#turn-list tr[data-turn]").evaluate_all("rows => rows.map(row => row.dataset.turn)")
    page.evaluate("() => { window.__holdDashboard = false; window.__releaseDashboard(); }")
    page.wait_for_timeout(300)
    actual_ids = page.locator("#turn-list tr[data-turn]").evaluate_all("rows => rows.map(row => row.dataset.turn)")
    cached_ids = page.evaluate(
        """
        async () => {
          const cache = await import('/assets/dashboard/query-cache.js');
          const hit = cache.peekCachedJSON('/api/turns?days=7&session_label_mode=project&page=1&per_page=25&sort=credits&sort_dir=desc');
          return hit.hit ? (hit.data.rows || []).map(row => row.turn_id) : [];
        }
        """
    )
    assert_true(actual_ids == expected_ids, "stale dashboard response replaced the latest turn sort")
    assert_true(cached_ids == expected_ids, "stale dashboard response polluted the latest sort cache key")
    page.evaluate("() => { window.fetch = window.__originalFetch; delete window.__originalFetch; }")


def check_stale_failure_and_detail_failure_are_local(page, base_url: str) -> None:
    open_dashboard(page, base_url)
    page.locator('button[data-view-target="turns"]').click()
    set_dashboard_select(page, "#turn-page-size", "10")
    page.wait_for_function("() => document.querySelectorAll('#turn-list tr[data-turn]').length === 10", timeout=10_000)
    page.evaluate(
        """
        async () => {
          const cache = await import('/assets/dashboard/query-cache.js');
          cache.clearAnalyticsQueryCache();
          window.__originalFetch = window.fetch;
          window.__failDetail = false;
          window.__failPage = false;
          window.__failCurrentSort = false;
          window.__delayNextDashboardFailure = false;
          window.__trackDashboardSuccess = false;
          window.__dashboardSuccessSeen = false;
          window.fetch = (input, init) => {
            const url = new URL(typeof input === 'string' ? input : input.url, location.origin);
            if (url.pathname === '/api/dashboard' && window.__delayNextDashboardFailure) {
              window.__delayNextDashboardFailure = false;
              return new Promise(resolve => {
                window.__releaseStaleDashboardFailure = () => resolve(new Response(
                  JSON.stringify({error: 'stale dashboard failed'}),
                  {status: 500, headers: {'Content-Type': 'application/json'}}
                ));
              });
            }
            if (url.pathname === '/api/dashboard' && window.__trackDashboardSuccess) {
              return window.__originalFetch(input, init).then(response => {
                window.__dashboardSuccessSeen = true;
                return response;
              });
            }
            if (url.pathname === '/api/turns' && url.searchParams.get('sort') === 'credits') {
              return new Promise(resolve => {
                window.__releaseStaleSort = () => resolve(new Response(
                  JSON.stringify({error: 'stale sort failed'}),
                  {status: 500, headers: {'Content-Type': 'application/json'}}
                ));
              });
            }
            if (url.pathname === '/api/turns' && url.searchParams.get('page') === '2' && window.__failPage) {
              return Promise.resolve(new Response(
                JSON.stringify({error: 'page failed'}),
                {status: 500, headers: {'Content-Type': 'application/json'}}
              ));
            }
            if (url.pathname === '/api/turns' && url.searchParams.get('sort') === 'raw' && window.__failCurrentSort) {
              return Promise.resolve(new Response(
                JSON.stringify({error: 'sort failed'}),
                {status: 500, headers: {'Content-Type': 'application/json'}}
              ));
            }
            if (url.pathname === '/api/turn' && window.__failDetail) {
              return Promise.resolve(new Response(
                JSON.stringify({error: 'detail failed'}),
                {status: 500, headers: {'Content-Type': 'application/json'}}
              ));
            }
            return window.__originalFetch(input, init);
          };
        }
        """
    )

    page.evaluate("() => { window.__delayNextDashboardFailure = true; document.querySelector('#refresh').click(); }")
    page.wait_for_function("() => typeof window.__releaseStaleDashboardFailure === 'function'", timeout=10_000)
    page.evaluate("() => { window.__trackDashboardSuccess = true; document.querySelector('#refresh').click(); }")
    page.wait_for_function("() => window.__dashboardSuccessSeen === true", timeout=10_000)
    successful_summary = page.locator("#summary").inner_text()
    page.evaluate("() => window.__releaseStaleDashboardFailure()")
    page.wait_for_timeout(200)
    assert_true(page.locator("#query-status").is_hidden(), "stale dashboard failure surfaced after a newer refresh succeeded")
    assert_true(page.locator("#summary").inner_text() == successful_summary, "stale dashboard failure replaced the latest summary")

    page.locator('#turn-list [data-turn-sort="credits"]').click()
    page.wait_for_function("() => typeof window.__releaseStaleSort === 'function'", timeout=10_000)
    page.locator('#turn-list [data-turn-sort="prompt"]').click()
    page.wait_for_function(
        "() => document.querySelector('#turn-list th[aria-sort=\"ascending\"] [data-turn-sort=\"prompt\"]') !== null",
        timeout=10_000,
    )
    latest_ids = page.locator("#turn-list tr[data-turn]").evaluate_all("rows => rows.map(row => row.dataset.turn)")
    page.evaluate("() => window.__releaseStaleSort()")
    page.wait_for_timeout(200)
    assert_true(
        page.locator("#turn-list tr[data-turn]").evaluate_all("rows => rows.map(row => row.dataset.turn)") == latest_ids,
        "stale list failure replaced the latest successful rows",
    )
    assert_true(page.locator("#query-status").is_hidden(), "stale list failure surfaced a user-visible error")

    page.evaluate(
        """
        async () => {
          const cache = await import('/assets/dashboard/query-cache.js');
          cache.clearAnalyticsQueryCache();
          window.__failCurrentSort = true;
        }
        """
    )
    page.locator('#turn-list [data-turn-sort="raw"]').click()
    page.wait_for_function("() => !document.querySelector('#query-status').hidden", timeout=10_000)
    rolled_back_sort = page.evaluate("async () => (await import('/assets/dashboard/core.js')).state.turnSortKey")
    assert_true(rolled_back_sort == "prompt", f"failed sort did not restore state: {rolled_back_sort}")
    assert_true(
        page.locator('#turn-list th[aria-sort="ascending"] [data-turn-sort="prompt"]').count() == 1,
        "failed sort changed the visible sort header",
    )

    page.evaluate(
        """
        async () => {
          const cache = await import('/assets/dashboard/query-cache.js');
          cache.clearAnalyticsQueryCache();
          window.__failCurrentSort = false;
          window.__failPage = true;
        }
        """
    )
    previous_ids = page.locator("#turn-list tr[data-turn]").evaluate_all("rows => rows.map(row => row.dataset.turn)")
    previous_detail = page.locator("#detail").inner_text()
    previous_projects = page.locator("#projects").inner_text()
    page.locator("#next-page").click()
    page.wait_for_function("() => !document.querySelector('#query-status').hidden", timeout=10_000)
    assert_true(
        page.locator("#turn-list tr[data-turn]").evaluate_all("rows => rows.map(row => row.dataset.turn)") == previous_ids,
        "current page failure replaced the existing turn rows",
    )
    assert_true(page.locator("#detail").inner_text() == previous_detail, "current page failure replaced the existing detail")
    assert_true(page.locator("#projects").inner_text() == previous_projects, "current page failure replaced an unrelated panel")
    assert_true(page.locator("#next-page").is_enabled(), "current page failure left the pager disabled")

    page.evaluate(
        """
        async () => {
          const cache = await import('/assets/dashboard/query-cache.js');
          cache.clearAnalyticsQueryCache();
          window.__failPage = false;
          window.__failDetail = true;
        }
        """
    )
    page.locator("#next-page").click()
    page.wait_for_function("() => document.querySelector('#turn-pager .page-status')?.textContent.startsWith('11-')", timeout=10_000)
    assert_true(page.locator("#turn-list tr[data-turn]").count() > 0, "detail failure blocked the successful list page")
    assert_true(page.locator("#turn-list tr.selected").count() == 0, "detail failure left a mismatched selected row")
    assert_true(page.locator("#detail-status").inner_text() == "error", "detail failure was not isolated to the detail panel")
    assert_true("detail failed" in page.locator("#detail").inner_text(), "detail failure message was not rendered locally")
    assert_true("detail failed" not in page.locator("#projects").inner_text(), "detail failure replaced an unrelated panel")

    page.evaluate("() => { window.__failDetail = false; }")
    page.locator("#turn-list tr[data-turn]").first.click()
    page.wait_for_function(
        "() => document.querySelector('#turn-list tr.selected') !== null && document.querySelector('#detail-status')?.textContent !== 'error'",
        timeout=10_000,
    )
    page.evaluate("() => { window.fetch = window.__originalFetch; delete window.__originalFetch; }")
