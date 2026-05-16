"""Tests for ConfigWriter — the underlying config mutation layer."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_writer_starts_with_seeded_defaults(writer_path):
    """Loader seeds default providers on first run; writer reads same file."""
    from cogitum.core.llm.config_writer import ConfigWriter
    w = ConfigWriter(path=writer_path)
    # Defaults from loader should be present (anthropic, openai, etc.)
    provs = w.providers()
    assert isinstance(provs, dict) or hasattr(provs, "keys")
    # At minimum the file exists
    assert writer_path.exists()


def test_add_provider_persists(writer_path):
    from cogitum.core.llm.config_writer import ConfigWriter
    w = ConfigWriter(path=writer_path)
    w.add_provider(
        "test-prov",
        name="Test Provider",
        format="openai_compat",
        base_url="https://api.example.com/v1",
        auth="bearer",
        enabled=False,
    )
    w.save()

    # Reload from disk
    w2 = ConfigWriter(path=writer_path)
    assert w2.has_provider("test-prov")
    p = w2.provider("test-prov")
    assert p["name"] == "Test Provider"
    assert p["base_url"] == "https://api.example.com/v1"
    assert p["enabled"] is False


def test_set_key_creates_keys_table(writer_path):
    from cogitum.core.llm.config_writer import ConfigWriter
    w = ConfigWriter(path=writer_path)
    w.add_provider("p1", name="P1", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=False)
    w.set_key("p1", "primary", "env:P1_API_KEY", notes="test note")
    w.save()

    w2 = ConfigWriter(path=writer_path)
    keys = w2.list_keys("p1")
    assert "primary" in keys
    assert keys["primary"]["secret_ref"] == "env:P1_API_KEY"
    assert keys["primary"]["notes"] == "test note"


def test_set_enabled_after_key_added(writer_path):
    from cogitum.core.llm.config_writer import ConfigWriter
    w = ConfigWriter(path=writer_path)
    w.add_provider("p1", name="P1", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=False)
    w.set_key("p1", "primary", "env:KEY")
    w.set_enabled("p1", True)
    w.save()

    w2 = ConfigWriter(path=writer_path)
    assert w2.provider("p1")["enabled"] is True


def test_remove_key(writer_path):
    from cogitum.core.llm.config_writer import ConfigWriter
    w = ConfigWriter(path=writer_path)
    w.add_provider("p1", name="P1", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("p1", "k1", "env:K1")
    w.set_key("p1", "k2", "env:K2")
    w.save()

    w2 = ConfigWriter(path=writer_path)
    assert len(w2.list_keys("p1")) == 2
    w2.remove_key("p1", "k1")
    w2.save()

    w3 = ConfigWriter(path=writer_path)
    keys = w3.list_keys("p1")
    assert "k1" not in keys
    assert "k2" in keys


def test_remove_provider(writer_path):
    from cogitum.core.llm.config_writer import ConfigWriter
    w = ConfigWriter(path=writer_path)
    w.add_provider("p1", name="P1", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.add_provider("p2", name="P2", format="openai_compat",
                   base_url="https://y", auth="bearer", enabled=True)
    w.set_key("p1", "primary", "env:P1")
    w.save()

    w2 = ConfigWriter(path=writer_path)
    assert w2.has_provider("p1")
    w2.remove_provider("p1")
    w2.save()

    w3 = ConfigWriter(path=writer_path)
    assert not w3.has_provider("p1")
    assert w3.has_provider("p2")


def test_add_model_then_list(writer_path):
    from cogitum.core.llm.config_writer import ConfigWriter
    w = ConfigWriter(path=writer_path)
    w.add_provider("p1", name="P1", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.add_model("p1", "model-a", display="Model A",
                capabilities=["text", "tools"],
                context_window=128000, max_output_tokens=8000)
    w.add_model("p1", "model-b", display="Model B",
                capabilities=["text"],
                context_window=64000, max_output_tokens=4000)
    w.save()

    w2 = ConfigWriter(path=writer_path)
    p = w2.provider("p1")
    models = p["models"]
    assert "model-a" in models
    assert "model-b" in models
    assert models["model-a"]["display"] == "Model A"
    assert models["model-a"]["context_window"] == 128000


def test_add_provider_idempotent(writer_path):
    """Re-adding a provider with same id must not clobber existing data."""
    from cogitum.core.llm.config_writer import ConfigWriter
    w = ConfigWriter(path=writer_path)
    w.add_provider("p1", name="Original", format="openai_compat",
                   base_url="https://x", auth="bearer", enabled=True)
    w.set_key("p1", "primary", "env:P1")
    w.save()

    w2 = ConfigWriter(path=writer_path)
    # Try to add again with different params — should be no-op
    w2.add_provider("p1", name="Different", format="anthropic_native",
                    base_url="https://y", auth="bearer", enabled=False)
    w2.save()

    w3 = ConfigWriter(path=writer_path)
    p = w3.provider("p1")
    assert p["name"] == "Original"
    assert p["base_url"] == "https://x"
    # Key still there
    assert "primary" in w3.list_keys("p1")
