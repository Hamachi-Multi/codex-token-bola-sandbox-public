"""Desktop browser checks for the Codex Token Bola dashboard."""

from __future__ import annotations

from playwright_dashboard_cleanup import check_cleanup_desktop
from playwright_dashboard_helpers import assert_true, open_dashboard
from playwright_dashboard_toolbar import check_toolbar
from playwright_dashboard_tools import check_tools_and_subagents
from playwright_dashboard_turns import check_turns_and_selected_turn


def prepare_desktop(page, base_url: str) -> None:
    open_dashboard(page, base_url)
    initial_focus_is_row_action = page.evaluate(
        "() => Boolean(document.activeElement?.closest?.('.row-select-button'))"
    )
    assert_true(not initial_focus_is_row_action, "initial dashboard load should not force focus into the first data row")


def check_desktop_toolbar(page, base_url: str) -> None:
    prepare_desktop(page, base_url)
    check_toolbar(page)


def check_desktop_turns(page, base_url: str) -> None:
    prepare_desktop(page, base_url)
    check_turns_and_selected_turn(page, base_url)


def check_desktop_cleanup(page, base_url: str) -> None:
    prepare_desktop(page, base_url)
    check_cleanup_desktop(page, base_url)


def check_desktop_tools(page, base_url: str) -> None:
    prepare_desktop(page, base_url)
    check_tools_and_subagents(page, base_url)
