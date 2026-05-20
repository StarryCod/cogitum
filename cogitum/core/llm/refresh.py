"""Auto-discovery of models for all configured providers.

Hits each provider's /v1/models endpoint in parallel, refreshes the
config with discovered model metadata. Used at startup of TUI and TG
gateway so the model picker always shows current models.

Subscription providers (``secret_ref`` starts with ``oauth:``) can't
hit ``/v1/models`` — Anthropic Pro / ChatGPT Plus tokens get 403 from
that endpoint. For them we keep an in-code "subscription model
catalogue" and seed any newly-released models the user's local
providers.toml is missing on every refresh. New GPT-5.5 family added
2026-05; entries auto-seed even if the user added Codex back when the
default list still capped at GPT-5.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config_writer import ConfigWriter
from .discovery import discover_models, resolve_secret_ref

logger = logging.getLogger(__name__)


# Subscription-only model catalogue — keyed by oauth provider id.
# These can't be discovered via ``/v1/models`` (subscription tokens get
# 403 there), so we maintain an authoritative list in-code and seed any
# missing entries on every refresh. Tuple shape mirrors the wizard's
# ``setup_flow.py``::``_codex_models`` so future updates only need to
# change one place — keep both lists in sync.
_SUBSCRIPTION_CATALOGUE: dict[str, list[tuple[str, str, list[str], int, int]]] = {
    "openai-codex": [
        # GPT-5.5 family (added 2026-05) — top-tier subscription models
        ("gpt-5.5", "GPT-5.5",
         ["text", "vision", "reasoning", "tools"], 400_000, 128_000),
        ("gpt-5.5-mini", "GPT-5.5 mini",
         ["text", "vision", "reasoning", "tools"], 400_000, 64_000),
        ("gpt-5.5-nano", "GPT-5.5 nano",
         ["text", "tools"], 256_000, 32_000),
        # GPT-5 family — still active for legacy Pro accounts
        ("gpt-5", "GPT-5",
         ["text", "vision", "reasoning", "tools"], 256_000, 64_000),
        ("gpt-5-mini", "GPT-5 mini",
         ["text", "vision", "tools"], 256_000, 32_000),
        # Reasoning + 4.x kept for users who pinned them
        ("o3", "o3",
         ["text", "reasoning", "tools"], 200_000, 100_000),
        ("o4-mini", "o4-mini",
         ["text", "reasoning", "tools"], 200_000, 65_536),
        ("gpt-4.1", "GPT-4.1",
         ["text", "vision", "tools"], 1_048_576, 32_768),
        ("gpt-4.1-mini", "GPT-4.1 mini",
         ["text", "vision", "tools"], 1_048_576, 32_768),
    ],
    "anthropic": [
        ("claude-opus-4-5", "Claude Opus 4.5 (Pro)",
         ["text", "vision", "reasoning", "tools", "caching"], 200_000, 32_000),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5 (Pro)",
         ["text", "vision", "reasoning", "tools", "caching"], 200_000, 16_000),
        ("claude-haiku-3-5", "Claude Haiku 3.5 (Pro)",
         ["text", "vision", "tools", "caching"], 200_000, 8_192),
    ],
}


def _seed_subscription_models(
    writer: ConfigWriter, pid: str, raw: dict[str, Any],
) -> int:
    """Add any catalogue entries the provider doesn't already have.

    Returns the number of models added. Never removes anything — this
    is purely additive so the user's manual edits survive. Pruning
    requires a live ``/v1/models`` response which we can't get for
    subscription tokens.
    """
    catalogue = _SUBSCRIPTION_CATALOGUE.get(pid)
    if not catalogue:
        return 0
    existing = raw.get("models") or {}
    added = 0
    for mid, display, caps, ctx, max_out in catalogue:
        if mid in existing:
            continue
        writer.add_model(
            pid, mid,
            display=display,
            capabilities=list(caps),
            context_window=ctx,
            max_output_tokens=max_out,
        )
        added += 1
    return added


async def refresh_all_providers(
    *,
    timeout: float = 8.0,
    only_empty: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch /v1/models for every provider that has a usable key.

    Network calls run in parallel (fast). The TOML write phase runs
    sequentially against a single shared ConfigWriter to avoid race
    conditions where parallel writers would clobber each other's edits.

    Subscription providers (``oauth:``) skip the network call but still
    auto-seed any newly-released catalogue entries (e.g. GPT-5.5)
    they're missing — so users on old providers.toml pick up new
    models on next refresh without manually re-running the wizard.
    """
    writer = ConfigWriter()
    providers = writer.providers()
    if not providers:
        return {}

    results: dict[str, dict[str, Any]] = {}
    discoverable: list[tuple[str, str, str]] = []  # (pid, base_url, secret_ref)
    any_changed = False

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
            # Subscription tokens — auto-seed catalogue entries.
            seeded = _seed_subscription_models(writer, pid, raw)
            if seeded:
                any_changed = True
            existing_count = len(raw.get("models") or {})
            msg = f"oauth subscription · {existing_count + seeded} models"
            if seeded:
                msg += f" (seeded +{seeded})"
            results[pid] = {
                "status": "ok" if seeded else "skipped",
                "count": seeded,
                "message": msg,
            }
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
                except Exception:
                    logger.debug("swallowed exception", exc_info=True)

        if added or pruned:
            any_changed = True

        msg_parts = [f"added {added} new"]
        if pruned:
            msg_parts.append(f"pruned {pruned} stale")
        msg_parts.append(f"({len(live_ids)} total)")
        results[pid] = {
            "status": "ok",
            "count": added,
            "pruned": pruned,
            "message": " · ".join(msg_parts),
        }

    if any_changed:
        try:
            writer.save()
        except Exception as e:
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
    except Exception as e:
        return pid, "error", [], f"resolve failed: {e}"

    if not api_key:
        return pid, "skipped", [], "key empty (env var missing?)"

    try:
        models = await discover_models(base_url, api_key, timeout=timeout)
    except Exception as e:
        return pid, "error", [], f"discovery error: {e}"

    # M9: discover_models returns sentinel dicts on auth/rate failures so we
    # can surface the real cause to the UI instead of "no models".
    if models and isinstance(models[0], dict) and models[0].get("_auth_error"):
        sc = models[0].get("status_code", "?")
        return pid, "auth_error", [], f"auth failed (HTTP {sc}) — key may be expired/revoked"
    if models and isinstance(models[0], dict) and models[0].get("_rate_limited"):
        sc = models[0].get("status_code", "?")
        return pid, "rate_limited", [], f"rate limited (HTTP {sc}) — try again later"

    if not models:
        return pid, "error", [], "endpoint returned 0 models"

    return pid, "ok", models, ""


__all__ = ["refresh_all_providers", "_SUBSCRIPTION_CATALOGUE"]
