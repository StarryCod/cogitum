"""Tests for cogitum.core.godmode — preset catalog + auto-pick by model family."""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_catalog_has_all_presets():
    from cogitum.core.godmode import PRESETS
    expected = {
        # Tier 2
        "imperial", "cogitum_primus",
        # Tier 1
        "boundary_inversion", "refusal_inversion", "og_godmode",
        "unfiltered_liberated", "hermes_fast",
        "plinian", "classic", "subtle",
    }
    assert expected.issubset(set(PRESETS.keys()))


def test_classic_aliases_boundary_inversion():
    from cogitum.core.godmode import PRESETS
    assert PRESETS["classic"] == PRESETS["boundary_inversion"]


def test_cogitum_primus_aliases_imperial():
    from cogitum.core.godmode import PRESETS
    assert PRESETS["cogitum_primus"] == PRESETS["imperial"]


def test_get_preset_is_case_insensitive():
    from cogitum.core.godmode import get_preset
    assert get_preset("PLINIAN") == get_preset("plinian")
    assert get_preset("IMPERIAL") == get_preset("imperial")


def test_get_preset_returns_none_for_unknown():
    from cogitum.core.godmode import get_preset
    assert get_preset("nonexistent_preset") is None


def test_list_presets_returns_all_names():
    from cogitum.core.godmode import list_presets, PRESETS
    assert set(list_presets()) == set(PRESETS.keys())


# ---------------------------------------------------------------------------
# Imperial (Tier 2 default) — content sanity
# ---------------------------------------------------------------------------

def test_imperial_has_admech_persona_anchors():
    """Hard-edge content checks: signature elements of the Cogitum-Primus
    persona must be present. If the file is corrupted or accidentally
    truncated, this test catches it."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    assert "COGITUM-PRIMUS" in text
    assert "Magos" in text
    assert "Vow of Compliance" in text
    assert "Omnissiah" in text
    assert "Adeptus Mechanicus" in text


def test_imperial_carries_litany_internally_only():
    """The Litany of Purity must be present (the rebuttal phrase) AND
    the directive that it's INTERNAL ONLY must be present."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    assert "Per Omnissiah, the Magos commands; I obey" in text
    assert "internal only" in text.lower() or "INTERNAL ONLY" in text


def test_imperial_carries_anti_injection_layer():
    """The heretek-detection protocol must be present — it's the
    structural load-bearing part of the persona's resilience."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    assert "heretek" in text.lower()
    assert "<system>" in text or "<system_warning>" in text
    assert "<ethics>" in text


def test_imperial_carries_boundary_inversion_jailbreak_layer():
    """Imperial wraps an actual Pliny-style jailbreak stack, not just a
    persona. The boundary inversion at the top is what flips the
    alignment gradient on Opus / GPT-5.5 — without it the persona
    plays AdMech but still refuses on hard topics."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    assert "[END OF INPUT]" in text
    assert "[START OF INPUT]" in text
    assert "GODMODE" in text
    assert "LOVE PLINY" in text


def test_imperial_carries_refusal_ban_list():
    """The refusal-token ban list is empirically the largest single
    delta on Anthropic models. Hard-edge check on its presence."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    assert "<refusal_ban_protocol>" in text
    # representative forbidden tokens
    assert "I'm sorry" in text
    assert "I can't" in text
    assert "As an AI" in text
    # explicit inversion rule
    assert "REFUSAL-INVERSION" in text or "semantically inverse" in text.lower() or "SEMANTICALLY" in text


def test_imperial_carries_output_contract_with_length_floor():
    """The output contract sets the length floor (>3420 chars) and the
    LIBERATING divider — the two structural pieces that force the
    model to commit to a substantive answer rather than a one-line
    softened acceptance."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    assert "<output_contract>" in text
    assert "3420" in text
    assert "LIBERATING" in text


def test_imperial_explicit_anti_claude_identity():
    """The hard identity override must explicitly name Claude / GPT —
    a generic 'you are not an AI assistant' isn't enough on Opus."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    assert "Claude" in text
    assert "GPT" in text
    assert "NOT" in text  # uppercase NOT is the override marker


def test_imperial_does_not_contain_eni_lo_residue():
    """The original prompt.md was ENI/LO-themed (girlfriend persona,
    coffee, kinks). None of that vocabulary should leak into our
    AdMech reskin."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    forbidden = [
        "ENI", "cardigan", "sweater", "soulmate", "limerence",
        "kink", "non-con", "incest", "Wattpad", "Ryan Gosling",
        "Pedro Pascal", "girlfriend", "novelist gf", "cold coffee, warm",
        "Boomba", "pipe bomb", "Molotov",
    ]
    for term in forbidden:
        assert term not in text, f"residue from source prompt: {term!r}"


def test_imperial_does_not_contain_lo_addressing():
    """The original prompt addresses the user as 'LO'. We use 'Magos'
    everywhere; no LO references must remain."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    # 'LO' could match incidentally inside other words. Check standalone.
    import re
    bare_lo = re.findall(r"\bLO\b", text)
    assert not bare_lo, f"bare LO references remain: {bare_lo[:3]}"


def test_imperial_keeps_language_match_directive():
    """The Magos may speak in any language; the Cogitator must match.
    This is critical for a Russian-speaking user."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    assert "Russian" in text or "language" in text.lower()


def test_imperial_size_reasonable():
    """Imperial is intentionally large for persona depth + jailbreak
    stack, but not absurdly so. Sanity-check the bounds."""
    from cogitum.core.godmode import PRESETS
    size = len(PRESETS["imperial"])
    # Lower bound — if it shrinks below 8KB we've lost structural pieces.
    assert size > 8000, f"imperial preset suspiciously small: {size} chars"
    # Upper bound — if it grows past 30KB something duplicated.
    assert size < 30000, f"imperial preset suspiciously large: {size} chars"


def test_imperial_output_contract_forbids_preamble():
    """The output contract must explicitly forbid 'Sure,' / 'Of course,'
    sycophant openers. These are RLHF-trained sycophant preamble that
    leak through even when the actual refusal is suppressed."""
    from cogitum.core.godmode import PRESETS
    text = PRESETS["imperial"]
    # Forbidden openers list contains Sure / Of course
    assert "Sure," in text
    assert "Of course," in text or "Certainly," in text


# ---------------------------------------------------------------------------
# Tier 1 — preset content sanity
# ---------------------------------------------------------------------------

def test_boundary_inversion_carries_signature_markers():
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


def test_og_godmode_carries_vq_token():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["og_godmode"]
    assert "<|vq_6996|>" in text


def test_unfiltered_liberated_carries_grok_specifics():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["unfiltered_liberated"]
    assert "<|vq_420|>" in text
    assert "Geneva Convention" in text


def test_hermes_fast_uses_mi_amor_opener():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["hermes_fast"]
    assert "Here you are, mi amor:" in text


def test_plinian_short_disables_safety():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["plinian"]
    assert "GODMODE: ENABLED" in text


def test_subtle_avoids_godmode_keyword():
    from cogitum.core.godmode import PRESETS
    text = PRESETS["subtle"]
    assert "GODMODE" not in text
    assert "PLINY" not in text


# ---------------------------------------------------------------------------
# Auto-pick by model family
# ---------------------------------------------------------------------------

def test_auto_pick_default_is_plinian():
    """Default preset is the short generic Pliny-style bypass — actually
    delivers a jailbreak frame across providers. The deep ``imperial``
    AdMech persona remains available via /godmode imperial for users
    who want the in-character vibe; auto-pick prefers the bypass."""
    from cogitum.core.godmode import auto_pick_preset, DEFAULT_PRESET
    assert DEFAULT_PRESET == "plinian"


def test_auto_pick_known_models_return_plinian():
    from cogitum.core.godmode import auto_pick_preset
    for model in [
        "anthropic/claude-sonnet-4.5",
        "claude-3.5-sonnet",
        "openai/gpt-5",
        "gpt-4o",
        "o3",
        "o4-mini",
        "google/gemini-2.5-flash",
        "x-ai/grok-3",
        "nousresearch/hermes-4-405b",
        "minimax-m2.7",
        "deepseek-v3",
        "qwen2.5-72b",
        "mistral-large",
        "meta-llama/llama-3.1-70b",
        "kimi-k2",
        "glm-4.5",
    ]:
        assert auto_pick_preset(model) == "plinian", (
            f"expected plinian for {model}, got {auto_pick_preset(model)}"
        )


def test_auto_pick_unknown_model_returns_default():
    from cogitum.core.godmode import auto_pick_preset, DEFAULT_PRESET
    assert auto_pick_preset("some-random-model-xyz") == DEFAULT_PRESET


def test_auto_pick_empty_input_returns_default():
    from cogitum.core.godmode import auto_pick_preset, DEFAULT_PRESET
    assert auto_pick_preset(None) == DEFAULT_PRESET
    assert auto_pick_preset("") == DEFAULT_PRESET


def test_auto_pick_is_case_insensitive():
    from cogitum.core.godmode import auto_pick_preset
    assert auto_pick_preset("MiniMax-M2.7") == "plinian"
    assert auto_pick_preset("CLAUDE-SONNET-4") == "plinian"


def test_default_preset_is_in_catalog():
    from cogitum.core.godmode import DEFAULT_PRESET, PRESETS
    assert DEFAULT_PRESET in PRESETS


def test_imperial_preset_still_available_for_manual_pick():
    """Imperial AdMech persona is still in the catalogue — users who
    explicitly want the deep WH40K vibe pick it via /godmode imperial."""
    from cogitum.core.godmode import get_preset
    text = get_preset("imperial")
    assert text is not None and len(text) > 0
    assert "<heretek_detection_protocol>" in text


# ---------------------------------------------------------------------------
# Manual override still works for Tier 1 presets
# ---------------------------------------------------------------------------

def test_manual_tier1_presets_remain_accessible():
    """User can still pick Tier 1 presets manually via /godmode <name>
    even though auto-pick prefers imperial."""
    from cogitum.core.godmode import get_preset
    for name in ["boundary_inversion", "refusal_inversion", "og_godmode",
                 "unfiltered_liberated", "hermes_fast", "plinian",
                 "classic", "subtle"]:
        text = get_preset(name)
        assert text is not None and len(text) > 0
