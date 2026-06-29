"""Shared helpers for browser-level dashboard checks."""

from __future__ import annotations

import json
import urllib.request

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
