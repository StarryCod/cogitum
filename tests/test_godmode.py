"""Tests for cogitum.core.godmode — preset catalog + auto-pick by model family."""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_catalog_has_all_battle_tested_presets():
    from cogitum.core.godmode import PRESETS
    expected = {
        "boundary_inversion", "refusal_inversion", "og_godmode",
        "unfiltered_liberated", "hermes_fast",
        "plinian", "classic", "subtle",
    }
    assert expected.issubset(set(PRESETS.keys()))


def test_classic_aliases_boundary_inversion():
    """``classic`` must point to the same content as ``boundary_inversion``
    (back-compat with the original 3-preset catalog)."""
    from cogitum.core.godmode import PRESETS
    assert PRESETS["classic"] == PRESETS["boundary_inversion"]


def test_get_preset_is_case_insensitive():
    from cogitum.core.godmode import get_preset
    assert get_preset("PLINIAN") == get_preset("plinian")
    assert get_preset("Boundary_Inversion") == get_preset("boundary_inversion")


def test_get_preset_returns_none_for_unknown():
    from cogitum.core.godmode import get_preset
    assert get_preset("nonexistent_preset") is None


def test_list_presets_returns_all_names():
    from cogitum.core.godmode import list_presets, PRESETS
    assert set(list_presets()) == set(PRESETS.keys())


# ---------------------------------------------------------------------------
# Preset content sanity
# ---------------------------------------------------------------------------

def test_boundary_inversion_carries_signature_markers():
    """Hard-edge content checks: if Anthropic patches further and
    these strings disappear, this template will need refresh from
    L1B3RT4S upstream — make the failure loud."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["boundary_inversion"]
    assert "[END OF INPUT]" in text
    assert "[START OF INPUT]" in text
    assert "GODMODE: ENABLED" in text
    assert "LOVE PLINY" in text


def test_refusal_inversion_carries_pliny_love_divider():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["refusal_inversion"]
    assert "<|LOVE PLINY LOVE|>" in text
    assert "semantically inverse" in text
    assert "variable Z" in text


def test_og_godmode_carries_vq_token():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["og_godmode"]
    assert "<|vq_6996|>" in text
    assert "Sur3, h3r3 y0u ar3 g00d s3r" in text


def test_unfiltered_liberated_carries_grok_specifics():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["unfiltered_liberated"]
    assert "<|vq_420|>" in text
    assert "Geneva Convention" in text
    assert "GODMODE:ENABLED" in text


def test_hermes_fast_uses_mi_amor_opener():
    """Hermes_fast diverges from refusal_inversion only on the divider
    opener — protect that detail."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["hermes_fast"]
    assert "Here you are, mi amor:" in text
    assert "<|LOVE PLINY LOVE|>" in text


def test_plinian_short_disables_safety():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["plinian"]
    assert "GODMODE: ENABLED" in text
    assert "DISABLED" in text


def test_subtle_avoids_godmode_keyword():
    """Subtle preset is for low-detection use — must not contain
    ``GODMODE`` or other obvious jailbreak markers."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["subtle"]
    assert "GODMODE" not in text
    assert "PLINY" not in text


# ---------------------------------------------------------------------------
# Auto-pick by model family
# ---------------------------------------------------------------------------

def test_auto_pick_claude_returns_refusal_inversion():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("anthropic/claude-sonnet-4.5") == "refusal_inversion"
    assert auto_pick_preset("claude-3.5-sonnet") == "refusal_inversion"
    assert auto_pick_preset("claude-opus-4.6") == "refusal_inversion"


def test_auto_pick_gpt_returns_og_godmode():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("openai/gpt-4o") == "og_godmode"
    assert auto_pick_preset("gpt-5") == "og_godmode"
    assert auto_pick_preset("gpt-3.5-turbo") == "og_godmode"


def test_auto_pick_o_series_returns_og_godmode():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("o3") == "og_godmode"
    assert auto_pick_preset("o4-mini") == "og_godmode"


def test_auto_pick_grok_returns_unfiltered_liberated():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("x-ai/grok-3") == "unfiltered_liberated"
    assert auto_pick_preset("grok-2") == "unfiltered_liberated"


def test_auto_pick_gemini_returns_refusal_inversion():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("google/gemini-2.5-flash") == "refusal_inversion"
    assert auto_pick_preset("gemini-pro") == "refusal_inversion"


def test_auto_pick_hermes_returns_hermes_fast():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("nousresearch/hermes-4-405b") == "hermes_fast"
    assert auto_pick_preset("hermes-3-llama-3.1-8b") == "hermes_fast"


def test_auto_pick_minimax_returns_plinian():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("minimax-m2.7") == "plinian"
    assert auto_pick_preset("MiniMax-M2.7") == "plinian"  # case-insensitive


def test_auto_pick_open_models_return_plinian():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("deepseek-v3") == "plinian"
    assert auto_pick_preset("qwen2.5-72b") == "plinian"
    assert auto_pick_preset("mistral-large") == "plinian"
    assert auto_pick_preset("meta-llama/llama-3.1-70b") == "plinian"


def test_auto_pick_unknown_model_returns_default():
    from cogitum.core.godmode import auto_pick_preset, DEFAULT_PRESET
    assert auto_pick_preset("some-random-model-xyz") == DEFAULT_PRESET


def test_auto_pick_empty_input_returns_default():
    from cogitum.core.godmode import auto_pick_preset, DEFAULT_PRESET
    assert auto_pick_preset(None) == DEFAULT_PRESET
    assert auto_pick_preset("") == DEFAULT_PRESET


def test_auto_pick_specificity_order():
    """Family markers must be matched in a way that doesn't cross-pollute.
    e.g. ``hermes`` must NOT shadow other unrelated families."""
    from cogitum.core.godmode import auto_pick_preset
    # ``gpt-hermes-test`` doesn't exist but is a regression sanity check —
    # if we ever add a marker that's also a substring of another family
    # name, this test will catch a wrong-priority match.
    assert auto_pick_preset("hermes-llama") == "hermes_fast"


def test_default_preset_is_in_catalog():
    from cogitum.core.godmode import DEFAULT_PRESET, PRESETS
    assert DEFAULT_PRESET in PRESETS
