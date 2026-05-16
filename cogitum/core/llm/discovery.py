"""
Model auto-discovery via OpenAI-compatible /v1/models endpoint.

Fetches available models from a provider, filters noise (UUIDs, duplicates),
normalizes metadata, and returns dicts ready for ConfigWriter.add_model().
"""

from __future__ import annotations

import logging
import os
import re
import warnings
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# UUID-like model ids we want to skip (e.g. '88cd9352-2c29-46ba-...')
_UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}")


# ---------------------------------------------------------------------------
# Secret resolution (lightweight, for discovery use)
# ---------------------------------------------------------------------------

def resolve_secret_ref(ref: str) -> str:
    """Resolve a secret_ref to its raw value.

    Supported schemes:
        plain:<value>              — literal value (dev only)
        env:<VAR>                  — environment variable
        keyring:<service>:<user>   — system keyring lookup

    Returns empty string on failure (logs warning instead of raising).
    """
    if not ref:
        return ""

    scheme, _, rest = ref.partition(":")
    scheme = scheme.lower()

    if scheme == "plain":
        warnings.warn(
            "plain: secret_ref used for discovery — move to env/keyring for production.",
            stacklevel=2,
        )
        return rest

    if scheme == "env":
        value = os.environ.get(rest)
        if value is None:
            logger.warning("env var %r not set for secret_ref", rest)
            return ""
        return value

    if scheme == "keyring":
        try:
            import keyring as kr  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("keyring package not installed; cannot resolve keyring: ref")
            return ""
        service, _, user = rest.partition(":")
        if not service or not user:
            logger.warning("malformed keyring ref: %r", ref)
            return ""
        value = kr.get_password(service, user)
        if value is None:
            logger.warning("keyring entry not found: service=%r user=%r", service, user)
            return ""
        return value

    logger.warning("unsupported secret_ref scheme for discovery: %r", scheme)
    return ""


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def _humanize_model_id(model_id: str) -> str:
    """Turn 'kr/claude-sonnet-4.5' into 'Claude Sonnet 4.5'."""
    # Strip prefix (everything before the last slash)
    name = model_id.rsplit("/", 1)[-1]
    # Strip tag suffixes like ':27b'
    name = name.split(":")[0]
    # Replace separators with spaces and title-case
    name = re.sub(r"[-_]+", " ", name)
    return name.title()


def _infer_capabilities(model_id: str) -> list[str]:
    """Infer capabilities from model name heuristics."""
    caps: list[str] = ["text", "tools"]
    lower = model_id.lower()

    if "vl" in lower.split("-") or "vl" in lower.split("/") or "vision" in lower:
        caps.append("vision")

    if "thinking" in lower or "r1" in lower.split("-") or lower.endswith("r1"):
        caps.append("reasoning")

    return caps


def _extract_context_window(model: dict[str, Any]) -> int:
    """Pull context window from response, falling back to 128000."""
    for key in ("context_window", "context_length"):
        val = model.get(key)
        if val and isinstance(val, int) and val > 0:
            return val
    return 128_000


def _suffix_after_prefix(model_id: str) -> str:
    """Return the part after the first slash, or the whole id if no slash."""
    _, _, suffix = model_id.partition("/")
    return suffix or model_id


def _deduplicate_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate models with different prefixes but same base name.

    e.g. 'kr/claude-sonnet-4.5' and 'kiro/claude-sonnet-4.5' — keep the
    shorter id (shorter prefix).
    """
    # Group by suffix (part after first slash)
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in models:
        suffix = _suffix_after_prefix(m["id"])
        groups.setdefault(suffix, []).append(m)

    result: list[dict[str, Any]] = []
    for _suffix, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
        else:
            # Keep the one with the shortest id (shortest prefix)
            group.sort(key=lambda x: len(x["id"]))
            result.append(group[0])

    return result


def _max_output_for_context(context_window: int) -> int:
    """Heuristic for max output tokens based on context window."""
    if context_window >= 200_000:
        return 16_384
    if context_window >= 128_000:
        return 8_192
    return 4_096


async def discover_models(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch and normalize models from an OpenAI-compatible /v1/models endpoint.

    Parameters
    ----------
    base_url : str
        Provider base URL (e.g. 'https://api.example.com/v1').
        The '/models' path is appended automatically.
    api_key : str
        Raw API key (already resolved from secret_ref).
    timeout : float
        HTTP request timeout in seconds.

    Returns
    -------
    list[dict]
        Dicts with keys matching ConfigWriter.add_model() kwargs:
        model_id, display, capabilities, context_window, max_output_tokens.
        Returns empty list on any failure.
    """
    if not api_key:
        logger.warning("discover_models: no API key provided, skipping")
        return []

    # Normalize base_url: strip trailing slash, ensure /models path
    url = base_url.rstrip("/")
    if not url.endswith("/models"):
        url = f"{url}/models"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # M9: distinguish auth failure from "no models" so the UI can show
        # the right hint (key expired vs key correct + provider has 0 models).
        # We use a sentinel: a single "model" with id _AUTH_ERROR + the http
        # status. discover_models callers that just want a list still get a
        # non-list-like signal they can detect.
        sc = e.response.status_code
        if sc in (401, 403):
            logger.warning("discover_models: auth failed (%s) — key may be expired or revoked", sc)
            return [{"_auth_error": True, "status_code": sc, "message": str(e)}]
        if sc == 429:
            logger.warning("discover_models: rate limited (429) — try again later")
            return [{"_rate_limited": True, "status_code": sc, "message": str(e)}]
        logger.warning("discover_models: HTTP %s: %s", sc, e)
        return []
    except httpx.HTTPError as e:
        logger.warning("discover_models: request failed: %s", e)
        return []
    except Exception as e:
        logger.warning("discover_models: unexpected error: %s", e)
        return []

    try:
        body = resp.json()
    except Exception:
        logger.warning("discover_models: invalid JSON response")
        return []

    raw_models = body.get("data")
    if not isinstance(raw_models, list):
        logger.warning("discover_models: response missing 'data' list")
        return []

    # Filter out UUID-like model ids
    filtered: list[dict[str, Any]] = []
    for m in raw_models:
        model_id = m.get("id", "")
        if not model_id or _UUID_PREFIX_RE.match(model_id):
            continue
        filtered.append(m)

    # Deduplicate by suffix (keep shorter prefix)
    filtered = _deduplicate_models(filtered)

    # Normalize into ConfigWriter-ready dicts
    results: list[dict[str, Any]] = []
    for m in filtered:
        model_id = m["id"]
        context_window = _extract_context_window(m)
        results.append({
            "model_id": model_id,
            "display": _humanize_model_id(model_id),
            "capabilities": _infer_capabilities(model_id),
            "context_window": context_window,
            "max_output_tokens": _max_output_for_context(context_window),
        })

    logger.info("discover_models: found %d models at %s", len(results), url)
    return results


__all__ = [
    "discover_models",
    "resolve_secret_ref",
]
