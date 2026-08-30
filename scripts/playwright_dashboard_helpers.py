"""Shared helpers for browser-level dashboard checks."""

from __future__ import annotations

import json
import time
import urllib.request

DEFAULT_UI_TIMEOUT_MS = 10_000

def compact_date(value: str) -> str:
    if not value:
        return "-"
    marker = value.find("T")
    if marker >= 0:
        return value[:marker]
    return value


def compact_time(value: str) -> str:
    if not value:
        return "-"
    marker = value.find("T")
    if marker >= 0 and len(value) >= marker + 9:
        return value[marker + 1 : marker + 9]
    return value


def compact_session_id(value: str) -> str:
    text = (value or "").replace("-", "")
    return text[-4:]


def session_path_label(value: str) -> str:
    text = (value or "").replace("\\", "/").rstrip("/")
    part = text.split("/")[-1] if text else ""
    return f"{part}/" if part else ""


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=45) as response:
        return json.load(response)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def set_dashboard_select(page, selector: str, value: str) -> None:
    page.locator(selector).evaluate(
        """
        (element, value) => {
          element.value = value;
          element.dispatchEvent(new Event('change', {bubbles: true}));
        }
        """,
        value,
    )


def open_dashboard(page, base_url: str, *, path: str = "") -> None:
    page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
    page.wait_for_function(
        """
        () => {
          const summary = document.querySelector('#summary');
          return Boolean(summary && !summary.querySelector('.loading, .loading-line'));
        }
        """,
        timeout=DEFAULT_UI_TIMEOUT_MS,
    )


def wait_for_view_content(
    page,
    *,
    view: str,
    container: str,
    row: str,
    empty: str,
    require_focus: bool = False,
) -> None:
    page.wait_for_function(
        """
        ({view, container, row, empty, requireFocus}) => {
          const section = document.querySelector(`[data-view="${view}"]`);
          const panel = document.querySelector(container);
          if (!section?.classList.contains('active') || !panel) return false;
          const rows = Array.from(panel.querySelectorAll(row));
          if (!rows.length && !panel.querySelector(empty)) return false;
          if (!requireFocus || !rows.length) return true;
          const buttons = rows
            .map(item => item.querySelector('.row-select-button'))
            .filter(Boolean);
          return buttons.includes(document.activeElement) && document.activeElement.isConnected;
        }
        """,
        arg={
            "view": view,
            "container": container,
            "row": row,
            "empty": empty,
            "requireFocus": require_focus,
        },
        timeout=DEFAULT_UI_TIMEOUT_MS,
    )


def wait_for_python_condition(page, predicate, *, description: str, timeout_ms: int = DEFAULT_UI_TIMEOUT_MS) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(25)
    raise AssertionError(f"timed out waiting for {description}")


def parse_number(value: str | None) -> float:
    text = str(value or "").replace(",", "").strip()
    return float(text or "0")


def scroll_bottom_state(page, selector: str) -> dict:
    return page.evaluate(
        """
        (selector) => {
          const el = document.querySelector(selector);
          el.scrollTop = el.scrollHeight;
          el.dispatchEvent(new Event('scroll'));
          const er = el.getBoundingClientRect();
          const visibleRows = Array.from(el.querySelectorAll('tbody tr')).filter((row) => {
            const rect = row.getBoundingClientRect();
            return rect.bottom > er.top && rect.top < er.bottom;
          });
          const row = visibleRows[visibleRows.length - 1] || el.querySelector('tbody tr:last-child');
          const rr = row ? row.getBoundingClientRect() : null;
          const td = el.querySelector('td');
          const style = getComputedStyle(el);
          return {
            remaining: el.scrollHeight - el.clientHeight - el.scrollTop,
            paddingBottom: style.paddingBottom,
            scrollPaddingBottom: style.scrollPaddingBottom,
            canScrollDown: el.classList.contains('can-scroll-down'),
            lastVisibleBottomDelta: rr ? Math.round((er.bottom - rr.bottom) * 1000) / 1000 : null,
            lastVisibleBorderBottom: row ? getComputedStyle(row.querySelector('td')).borderBottomWidth : null,
            rowHeight: rr ? Math.round(rr.height * 1000) / 1000 : null,
            lineHeight: td ? getComputedStyle(td).lineHeight : null,
          };
        }
        """,
        selector,
    )
