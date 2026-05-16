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

    Network calls run in parallel (fast). The TOML write phase runs
    sequentially against a single shared ConfigWriter to avoid race
    conditions where parallel writers would clobber each other's edits.
    """
    writer = ConfigWriter()
    providers = writer.providers()
    if not providers:
        return {}

    results: dict[str, dict[str, Any]] = {}
    discoverable: list[tuple[str, str, str]] = []  # (pid, base_url, secret_ref)

    for pid, raw in providers.items():
        if not raw.get("enabled", True):
            results[pid] = {"status": "skipped", "count": 0,
                           "message": "disabled"}
            continue

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

        if secret_ref.startswith("oauth:"):
            results[pid] = {"status": "skipped", "count": 0,
                           "message": "oauth subscription"}
            continue

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

        discoverable.append((pid, base_url, secret_ref))

    # Phase 1: parallel network fetch — gather raw model lists
    fetch_tasks = [
        _fetch_one(pid, base_url, secret_ref, timeout)
        for pid, base_url, secret_ref in discoverable
    ]
    fetched = await asyncio.gather(*fetch_tasks, return_exceptions=False)

    # Phase 2: serialised TOML edits against the shared writer
    any_changed = False
    for pid, status, models, message in fetched:
        if status != "ok":
            results[pid] = {"status": status, "count": 0, "message": message}
            continue

        raw = writer.provider(pid)
        if not raw:
            results[pid] = {"status": "error", "count": 0,
                           "message": "provider vanished mid-flight"}
            continue

        existing = raw.get("models") or {}
        live_ids = {m.get("model_id") for m in models if m.get("model_id")}
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

        # Prune phantom models that the live API does not actually serve.
        pruned = 0
        for mid in list(existing.keys()):
            if mid not in live_ids:
                try:
                    writer.remove_model(pid, mid)
                    pruned += 1
                except Exception:  # noqa: BLE001
                    pass

        if added or pruned:
            any_changed = True

        msg_parts = [f"added {added} new"]
        if pruned:
            msg_parts.append(f"pruned {pruned} stale")
        msg_parts.append(f"({len(live_ids)} total)")
        results[pid] = {
            "status": "ok",
            "count": added,
            "message": " · ".join(msg_parts),
        }

    if any_changed:
        try:
            writer.save()
        except Exception as e:  # noqa: BLE001
            logger.warning("refresh_all: writer.save() failed: %s", e)

    return results


async def _fetch_one(
    pid: str,
    base_url: str,
    secret_ref: str,
    timeout: float,
) -> tuple[str, str, list[dict[str, Any]], str]:
    """Returns (pid, status, models, message). Pure I/O, no writes."""
    try:
        api_key = resolve_secret_ref(secret_ref)
    except Exception as e:  # noqa: BLE001
        return pid, "error", [], f"resolve failed: {e}"

    if not api_key:
        return pid, "skipped", [], "key empty (env var missing?)"

    try:
        models = await discover_models(base_url, api_key, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return pid, "error", [], f"discovery error: {e}"

    if not models:
        return pid, "error", [], "endpoint returned 0 models"

    return pid, "ok", models, ""
