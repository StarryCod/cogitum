"""Tests for the responsive setup_flow modals.

Audit fix #3: every wizard screen had a fixed CSS width (60, 64, 70,
78, 84, 86, 88, 96) so the 84-col AddProvider modal clipped on a
classic 80x24 terminal. The fix wraps each width in min-width /
max-width so they shrink on narrow screens and never blow past the
viewport.
"""
from __future__ import annotations

from cogitum.setup_flow import (
    AddProviderModal,
    ConfirmModal,
    CustomProviderModal,
    KeyEntryModal,
    KeyManagerModal,
    ManageModelsModal,
    MaxTokensModal,
    MessageModal,
    OAuthLoginModal,
    SetupScreen,
)


def _has_max_width_pct(css: str) -> bool:
    """Helper: does the CSS define a percentage-based max-width?"""
    return "max-width: 9" in css or "max-width: 95%" in css


def test_message_modal_has_responsive_widths():
    css = MessageModal.DEFAULT_CSS
    assert "max-width" in css
    assert "min-width" in css


def test_confirm_modal_has_responsive_widths():
    css = ConfirmModal.DEFAULT_CSS
    assert "max-width" in css
    assert "min-width" in css


def test_max_tokens_modal_has_responsive_widths():
    css = MaxTokensModal.DEFAULT_CSS
    assert "max-width: 95%" in css
    assert "min-width: 40" in css


def test_key_entry_modal_has_responsive_widths():
    css = KeyEntryModal.DEFAULT_CSS
    assert "max-width: 95%" in css
    assert "min-width: 44" in css


def test_add_provider_modal_has_responsive_widths():
    """The 84-col AddProviderModal that broke the audit's 80x24 case."""
    css = AddProviderModal.DEFAULT_CSS
    assert "max-width: 95%" in css
    assert "min-width" in css
    assert "max-height: 95%" in css


def test_custom_provider_modal_has_responsive_widths():
    css = CustomProviderModal.DEFAULT_CSS
    assert "max-width: 95%" in css
    assert "min-width: 44" in css


def test_key_manager_modal_has_responsive_widths():
    css = KeyManagerModal.DEFAULT_CSS
    assert "max-width: 95%" in css
    assert "min-width: 48" in css


def test_manage_models_modal_has_responsive_widths():
    css = ManageModelsModal.DEFAULT_CSS
    assert "max-width: 95%" in css
    assert "min-width: 48" in css


def test_oauth_login_modal_has_responsive_widths():
    css = OAuthLoginModal.DEFAULT_CSS
    assert "max-width: 95%" in css
    assert "min-width: 50" in css


def test_setup_screen_rail_collapses_on_narrow():
    """On narrow terminals the 28-col rail eats the content pane —
    we collapse it to 12 cols so the wizard content stays usable."""
    css = SetupScreen.DEFAULT_CSS
    assert "App.-narrow SetupScreen #setup-rail" in css
