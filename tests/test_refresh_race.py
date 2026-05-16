"""Tests for refresh.py — parallel network, sequential writes, prune phantoms."""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def stub_secrets(monkeypatch):
    def fake_resolve(ref: str) -> str:
        if not ref:
            return ""
        return "fake-key-" + ref.split(":")[-1]
    # patch on the live module after conftest reset
    from cogitum.core.llm import refresh as _r
    monkeypatch.setattr(_r, "resolve_secret_ref", fake_resolve)


def _seed(writer, pid, base_url, models):
    writer.add_provider(
        pid, name=pid, format="openai_compat",
        base_url=base_url, auth="bearer",
    )
    writer.set_key(pid, "primary", f"plain:tok-{pid}")
    for m in models:
        writer.add_model(pid, m, display=m, capabilities=["text", "tools"])


@pytest.mark.asyncio
async def test_writes_persist_across_parallel_providers(tmp_path, stub_secrets):
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm import refresh as R

    cfg_path = tmp_path / "providers.toml"
    w = ConfigWriter(path=cfg_path)
    _seed(w, "alpha", "https://alpha.test/v1", ["alpha-old"])
    _seed(w, "beta", "https://beta.test/v1", ["beta-old"])
    _seed(w, "gamma", "https://gamma.test/v1", ["gamma-old"])
    w.save()

    async def fake_discover(base_url, api_key, *, timeout):
        host = base_url.split("//")[1].split(".")[0]
        return [
            {"model_id": f"{host}-new1", "display": f"{host} new 1"},
            {"model_id": f"{host}-new2", "display": f"{host} new 2"},
        ]

    def factory(*a, **k):
        return ConfigWriter(path=cfg_path)

    with patch.object(R, "discover_models", side_effect=fake_discover):
        with patch.object(R, "ConfigWriter", side_effect=factory):
            res = await R.refresh_all_providers(timeout=2.0)

    for pid in ("alpha", "beta", "gamma"):
        assert res[pid]["status"] == "ok", f"{pid}: {res.get(pid)}"

    final = ConfigWriter(path=cfg_path)
    for pid, expected in [
        ("alpha", {"alpha-new1", "alpha-new2"}),
        ("beta", {"beta-new1", "beta-new2"}),
        ("gamma", {"gamma-new1", "gamma-new2"}),
    ]:
        models = set((final.provider(pid).get("models") or {}).keys())
        assert expected.issubset(models), \
            f"{pid} lost: have {models}, expected ⊇ {expected}"


@pytest.mark.asyncio
async def test_prunes_stale_preset_models(tmp_path, stub_secrets):
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm import refresh as R

    cfg_path = tmp_path / "providers.toml"
    w = ConfigWriter(path=cfg_path)
    _seed(w, "cerebras", "https://api.cerebras.ai/v1", ["llama-3.3-70b"])
    w.save()

    async def fake_discover(base_url, api_key, *, timeout):
        return [
            {"model_id": "llama3.1-8b", "display": "Llama 3.1 8B"},
            {"model_id": "qwen-3-235b-a22b-instruct-2507",
             "display": "Qwen 3 235B"},
        ]

    def factory(*a, **k):
        return ConfigWriter(path=cfg_path)

    with patch.object(R, "discover_models", side_effect=fake_discover):
        with patch.object(R, "ConfigWriter", side_effect=factory):
            res = await R.refresh_all_providers(timeout=2.0)

    assert res["cerebras"]["status"] == "ok"
    assert "pruned 1 stale" in res["cerebras"]["message"]

    final = ConfigWriter(path=cfg_path)
    models = set((final.provider("cerebras").get("models") or {}).keys())
    assert models == {"llama3.1-8b", "qwen-3-235b-a22b-instruct-2507"}


@pytest.mark.asyncio
async def test_skips_oauth_and_disabled(tmp_path, stub_secrets):
    from cogitum.core.llm.config_writer import ConfigWriter
    from cogitum.core.llm import refresh as R

    cfg_path = tmp_path / "providers.toml"
    w = ConfigWriter(path=cfg_path)
    w.add_provider("openai-codex", name="codex", format="openai_compat",
                   base_url="https://x/v1", auth="bearer")
    w.set_key("openai-codex", "subscription", "oauth:openai-codex")
    w.add_provider("off", name="off", format="openai_compat",
                   base_url="https://y/v1", auth="bearer")
    w.set_key("off", "primary", "plain:x")
    w.set_enabled("off", False)
    w.save()

    def factory(*a, **k):
        return ConfigWriter(path=cfg_path)

    with patch.object(R, "ConfigWriter", side_effect=factory):
        res = await R.refresh_all_providers(timeout=2.0)

    assert res["openai-codex"]["status"] == "skipped"
    assert "oauth" in res["openai-codex"]["message"].lower()
    assert res["off"]["status"] == "skipped"
    assert "disabled" in res["off"]["message"].lower()
