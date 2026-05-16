"""Auto-discovery of models for all configured providers.

Hits each provider's /v1/models endpoint in parallel, refreshes the
config with discovered model metadata. Used at startup of TUI and TG
gateway so the model picker always shows current models.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config_writer import ConfigWriter
from .discovery import discover_models, resolve_secret_ref

logger = logging.getLogger(__name__)


async def refresh_all_providers(
    *,
    timeout: float = 8.0,
    only_empty: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch /v1/models for every provider that has a usable key.

    Args:
        timeout: per-provider HTTP timeout in seconds
        only_empty: if True, skip providers that already have models

    Returns:
        Map of pid -> {status: 'ok'|'skipped'|'error', count: int, message: str}
    """
    writer = ConfigWriter()
    providers = writer.providers()
    if not providers:
        return {}

    results: dict[str, dict[str, Any]] = {}
    tasks = []
    for pid, raw in providers.items():
        if not raw.get("enabled", True):
            results[pid] = {"status": "skipped", "count": 0,
                           "message": "disabled"}
            continue

        # Find first usable key
        keys = raw.get("keys") or {}
        if not keys:
            results[pid] = {"status": "skipped", "count": 0,
                           "message": "no keys"}
            continue

        secret_ref = next(iter(keys.values())).get("secret_ref", "")
        if not secret_ref:
            results[pid] = {"status": "skipped", "count": 0,
                           "message": "no secret_ref"}
            continue

        # Skip OAuth providers (subscription tokens can't list models)
        if secret_ref.startswith("oauth:"):
            results[pid] = {"status": "skipped", "count": 0,
                           "message": "oauth subscription"}
            continue

        # Skip anthropic-native (no /v1/models, hardcoded list is fine)
        if raw.get("format") == "anthropic_native":
            results[pid] = {"status": "skipped", "count": 0,
                           "message": "anthropic_native (no /v1/models)"}
            continue

        existing = raw.get("models") or {}
        if only_empty and existing:
            results[pid] = {"status": "skipped", "count": len(existing),
                           "message": f"already has {len(existing)} models"}
            continue

        base_url = raw.get("base_url", "")
        if not base_url:
            results[pid] = {"status": "skipped", "count": 0,
                           "message": "no base_url"}
            continue

        tasks.append(_refresh_one(pid, base_url, secret_ref, timeout, results))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Persist any model additions
    if any(r.get("status") == "ok" for r in results.values()):
        try:
            writer.save()
        except Exception as e:  # noqa: BLE001
            logger.warning("refresh_all: writer.save() failed: %s", e)

    return results


async def _refresh_one(
    pid: str,
    base_url: str,
    secret_ref: str,
    timeout: float,
    results: dict[str, dict[str, Any]],
) -> None:
    try:
        api_key = resolve_secret_ref(secret_ref)
    except Exception as e:  # noqa: BLE001
        results[pid] = {"status": "error", "count": 0,
                       "message": f"resolve failed: {e}"}
        return
    if not api_key:
        results[pid] = {"status": "skipped", "count": 0,
                       "message": "key empty (env var missing?)"}
        return

    try:
        models = await discover_models(base_url, api_key, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        results[pid] = {"status": "error", "count": 0,
                       "message": f"discovery error: {e}"}
        return

    if not models:
        results[pid] = {"status": "error", "count": 0,
                       "message": "endpoint returned 0 models"}
        return

    # Merge into config — only add NEW model ids, don't clobber existing
    writer = ConfigWriter()
    raw = writer.provider(pid)
    if not raw:
        results[pid] = {"status": "error", "count": 0,
                       "message": "provider vanished mid-flight"}
        return

    existing = raw.get("models") or {}
    added = 0
    for m in models:
        mid = m.get("model_id")
        if not mid or mid in existing:
            continue
        writer.add_model(
            pid, mid,
            display=m.get("display", mid),
            capabilities=m.get("capabilities", ["text", "tools"]),
            context_window=m.get("context_window", 128000),
            max_output_tokens=m.get("max_output_tokens", 16000),
        )
        added += 1

    writer.save()
    total = len(existing) + added
    results[pid] = {
        "status": "ok",
        "count": added,
        "message": f"added {added} new ({total} total)",
    }
