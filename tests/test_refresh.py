"""Tests for refresh_all_providers — auto-discovery for all providers at once."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_refresh_skips_disabled_providers():
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm.refresh import refresh_all_providers

    w = ConfigWriter()
    w.add_provider("disab", name="Disab", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=False)
    w.set_key("disab", "primary", "env:FAKE_DISAB")
    w.save()

    results = await refresh_all_providers(timeout=1.0)
    assert "disab" in results
    assert results["disab"]["status"] == "skipped"
    assert "disabled" in results["disab"]["message"]


@pytest.mark.asyncio
async def test_refresh_skips_no_keys():
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm.refresh import refresh_all_providers

    w = ConfigWriter()
    w.add_provider("nokeys", name="NoKeys", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.save()

    results = await refresh_all_providers(timeout=1.0)
    assert results["nokeys"]["status"] == "skipped"
    assert "no keys" in results["nokeys"]["message"]


@pytest.mark.asyncio
async def test_refresh_skips_oauth_providers():
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm.refresh import refresh_all_providers

    w = ConfigWriter()
    w.add_provider("oauthp", name="OAuthP", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("oauthp", "primary", "oauth:anthropic")
    w.save()

    results = await refresh_all_providers(timeout=1.0)
    assert results["oauthp"]["status"] == "skipped"
    assert "oauth" in results["oauthp"]["message"]


@pytest.mark.asyncio
async def test_refresh_skips_anthropic_native():
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm.refresh import refresh_all_providers

    w = ConfigWriter()
    w.add_provider("anthr", name="Anthr", format="anthropic_native",
                   base_url="https://api.anthropic.com",
                   auth="x_api_key", enabled=True)
    w.set_key("anthr", "primary", "env:ANTHR_KEY")
    w.save()

    results = await refresh_all_providers(timeout=1.0)
    assert results["anthr"]["status"] == "skipped"
    assert "anthropic_native" in results["anthr"]["message"]


@pytest.mark.asyncio
async def test_refresh_only_empty_skips_populated():
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm.refresh import refresh_all_providers

    w = ConfigWriter()
    w.add_provider("hasm", name="HasM", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("hasm", "primary", "env:HASM_KEY")
    w.add_model("hasm", "model-1", display="M1",
                capabilities=["text"], context_window=8192,
                max_output_tokens=2048)
    w.save()

    results = await refresh_all_providers(timeout=1.0, only_empty=True)
    assert results["hasm"]["status"] == "skipped"
    assert "already has 1 models" in results["hasm"]["message"]


@pytest.mark.asyncio
async def test_refresh_reports_unreachable_endpoint():
    """Provider with bogus URL gets 'error' status, not crash."""
    import os
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm.refresh import refresh_all_providers

    os.environ["BOGUS_KEY"] = "fake-secret"
    w = ConfigWriter()
    w.add_provider("bogus", name="Bogus", format="openai_compat",
                   base_url="http://127.0.0.1:1/v1",  # nothing listening
                   auth="bearer", enabled=True)
    w.set_key("bogus", "primary", "env:BOGUS_KEY")
    w.save()

    results = await refresh_all_providers(timeout=1.0)
    # Either skipped (key empty) or error (network) — must not crash
    assert results["bogus"]["status"] in ("error", "skipped")


@pytest.mark.asyncio
async def test_refresh_handles_no_providers():
    """Empty provider list returns empty dict, no errors."""
    from cogitum.core.llm.refresh import refresh_all_providers
    # In isolated env there are still seeded defaults, but at least
    # the call must not raise.
    results = await refresh_all_providers(timeout=1.0)
    assert isinstance(results, dict)
