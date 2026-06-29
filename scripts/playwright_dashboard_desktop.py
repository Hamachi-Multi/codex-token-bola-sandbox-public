"""Desktop browser checks for the Codex Token Bola dashboard."""

from __future__ import annotations

from playwright_dashboard_cleanup import check_cleanup_desktop
from playwright_dashboard_toolbar import check_toolbar
from playwright_dashboard_tools import check_tools_and_subagents
from playwright_dashboard_turns import check_turns_and_selected_turn


def check_desktop(page, base_url: str) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    check_toolbar(page)
    check_turns_and_selected_turn(page, base_url)
    check_cleanup_desktop(page, base_url)
    check_tools_and_subagents(page, base_url)
