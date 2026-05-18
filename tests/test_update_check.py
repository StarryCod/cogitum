"""Tests for cogitum.core.update_check.

Network is mocked at the httpx layer so tests are deterministic and
don't hit GitHub. Cache state is isolated per test via the
COGITUM_CACHE_DIR override (see conftest).
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────


def test_split_version_strips_pre_release():
    from cogitum.core.update_check import _split_version
    assert _split_version("1.2.3") == (1, 2, 3)
    assert _split_version("1.2.3rc1") == (1, 2, 3)
    assert _split_version("0.0.1") == (0, 0, 1)
    # Empty / garbage degrades to (0,) so comparisons still work.
    assert _split_version("") == (0,)
    assert _split_version("x") == (0,)


def test_is_newer_strict_greater():
    from cogitum.core.update_check import is_newer
    assert is_newer("0.3.0", "0.2.0") is True
    assert is_newer("1.0.0", "0.99.99") is True
    assert is_newer("0.2.1", "0.2.0") is True
    # Equal is NOT newer — banners shouldn't shout for no reason.
    assert is_newer("0.2.0", "0.2.0") is False
    # Lower is not newer.
    assert is_newer("0.1.9", "0.2.0") is False


def test_parse_pyproject_version_extracts_project_table_version():
    from cogitum.core.update_check import _parse_pyproject_version
    toml = """\
[build-system]
requires = ["hatchling"]

[project]
name = "cogitum"
version = "0.5.7"
description = "Stuff"
"""
    assert _parse_pyproject_version(toml) == "0.5.7"


def test_parse_pyproject_version_returns_none_for_garbage():
    from cogitum.core.update_check import _parse_pyproject_version
    assert _parse_pyproject_version("not a toml") is None
    assert _parse_pyproject_version("") is None


def test_parse_pyproject_ignores_other_table_versions():
    """A `version = "..."` line outside [project] (e.g. inside
    [tool.poetry] or a comment) must NOT be picked up."""
    from cogitum.core.update_check import _parse_pyproject_version
    toml = """\
# version = "9.9.9"      ← comment, must ignore
[tool.something]
version = "0.0.1"

[project]
name = "x"
version = "1.2.3"
"""
    assert _parse_pyproject_version(toml) == "1.2.3"


# ─────────────────────────────────────────────────────────────────────────
# detect_install_method
# ─────────────────────────────────────────────────────────────────────────


def test_detect_install_method_npm_via_env_var(monkeypatch):
    """COGITUM_HOME is the strong signal — set by the npm launcher."""
    from cogitum.core.update_check import detect_install_method
    monkeypatch.setenv("COGITUM_HOME", "/tmp/whatever")
    assert detect_install_method() == "npm"


def test_detect_install_method_falls_through_when_no_env(monkeypatch):
    """Without COGITUM_HOME we fall through to the path heuristic.
    The exact answer depends on the test machine — we just check it
    returns one of the two known strings (npm/source) and doesn't
    crash. pip is no longer a possible answer since Cogitum isn't
    on PyPI."""
    from cogitum.core.update_check import detect_install_method
    monkeypatch.delenv("COGITUM_HOME", raising=False)
    method = detect_install_method()
    assert method in ("npm", "source")


# ─────────────────────────────────────────────────────────────────────────
# UpdateInfo / upgrade_command
# ─────────────────────────────────────────────────────────────────────────


def test_upgrade_command_always_cogitum_update():
    """Single canonical upgrade command — `cogitum update`."""
    from cogitum.core.update_check import UpdateInfo
    for method in ("npm", "pip", "source", "unknown"):
        info = UpdateInfo(current="0.1.0", latest="0.2.0", newer=True, install_method=method)
        assert info.upgrade_command() == "cogitum update"


# ─────────────────────────────────────────────────────────────────────────
# Cache lifecycle
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_uses_cache_within_ttl(tmp_path, monkeypatch):
    """A fresh cache entry short-circuits the network probe."""
    monkeypatch.setenv("COGITUM_CACHE_DIR", str(tmp_path))

    # Reload the module so its _cache_path() picks up the new env var.
    import importlib, cogitum.core.update_check as uc
    importlib.reload(uc)

    cache_file = tmp_path / "update-check.json"
    cache_file.write_text(
        json.dumps({"latest": "9.9.9", "fetched_at": time.time()}),
        encoding="utf-8",
    )

    # If the cache is honoured, the probe must NOT be called.
    with patch.object(uc, "_fetch_latest_version", new=AsyncMock()) as fetch:
        info = await uc.check()
    fetch.assert_not_called()
    assert info.latest == "9.9.9"
    assert info.newer is True  # 9.9.9 > whatever we ship


@pytest.mark.asyncio
async def test_check_ignores_expired_cache(tmp_path, monkeypatch):
    """A cache entry older than TTL must be re-fetched."""
    monkeypatch.setenv("COGITUM_CACHE_DIR", str(tmp_path))
    import importlib, cogitum.core.update_check as uc
    importlib.reload(uc)

    cache_file = tmp_path / "update-check.json"
    # Set fetched_at well outside the TTL window.
    cache_file.write_text(
        json.dumps({"latest": "0.0.1", "fetched_at": time.time() - uc._CACHE_TTL_S - 60}),
        encoding="utf-8",
    )

    with patch.object(uc, "_fetch_latest_version",
                      new=AsyncMock(return_value="2.0.0")) as fetch:
        info = await uc.check()
    fetch.assert_awaited_once()
    assert info.latest == "2.0.0"
    # Cache should now hold the fresh value.
    assert json.loads(cache_file.read_text())["latest"] == "2.0.0"


@pytest.mark.asyncio
async def test_check_force_bypasses_cache(tmp_path, monkeypatch):
    """force=True must always call the probe even on a hot cache."""
    monkeypatch.setenv("COGITUM_CACHE_DIR", str(tmp_path))
    import importlib, cogitum.core.update_check as uc
    importlib.reload(uc)

    cache_file = tmp_path / "update-check.json"
    cache_file.write_text(
        json.dumps({"latest": "0.1.0", "fetched_at": time.time()}),
        encoding="utf-8",
    )
    with patch.object(uc, "_fetch_latest_version",
                      new=AsyncMock(return_value="0.5.0")) as fetch:
        info = await uc.check(force=True)
    fetch.assert_awaited_once()
    assert info.latest == "0.5.0"


@pytest.mark.asyncio
async def test_check_returns_unknown_on_network_failure(tmp_path, monkeypatch):
    """When the probe fails (no httpx, GitHub down, timeout, ...)
    we must NOT crash and must NOT poison the cache. The caller
    sees latest=None which is the documented 'unknown' state."""
    monkeypatch.setenv("COGITUM_CACHE_DIR", str(tmp_path))
    import importlib, cogitum.core.update_check as uc
    importlib.reload(uc)

    with patch.object(uc, "_fetch_latest_version",
                      new=AsyncMock(return_value=None)):
        info = await uc.check()
    assert info.latest is None
    assert info.newer is False
    # Cache must not have been written for an unknown answer.
    assert not (tmp_path / "update-check.json").exists()
