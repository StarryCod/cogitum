"""
Anthropic OAuth (Claude Pro / Max).

Authorize URL is on claude.ai, token endpoint on platform.claude.com,
PKCE S256, public client. State is the PKCE verifier (matches the
upstream pi-mono behaviour).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from .callback_server import CallbackServer
from .pkce import generate_pkce
from .types import (
    OAuthAuthInfo,
    OAuthCredentials,
    OnAuth,
    OnPrompt,
    OnProgress,
)

logger = logging.getLogger(__name__)


# Public client id from upstream Anthropic docs / pi-mono / claude code.
# (base64-decoded in pi for reasons known only to its author; we just inline.)
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CALLBACK_HOST = "127.0.0.1"
_CALLBACK_PORT = 53692
_CALLBACK_PATH = "/callback"
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}{_CALLBACK_PATH}"
_SCOPES = (
    "org:create_api_key user:profile user:inference user:sessions:claude_code"
    " user:mcp_servers user:file_upload"
)


def _build_authorize_url(challenge: str, state: str) -> str:
    params = {
        "code": "true",
        "client_id": _CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _REDIRECT_URI,
        "scope": _SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _post_json(url: str, body: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"Anthropic token endpoint returned {e.code}: {body_text}"
        ) from e


def _parse_manual_input(text: str) -> tuple[str | None, str | None]:
    """Accept a raw code, a `code#state` pair, or the full redirect URL."""
    text = text.strip()
    if not text:
        return None, None
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urllib.parse.urlsplit(text)
        qs = urllib.parse.parse_qs(parsed.query)
        return (
            (qs.get("code") or [None])[0],
            (qs.get("state") or [None])[0],
        )
    if "#" in text:
        code, state = text.split("#", 1)
        return code or None, state or None
    if "code=" in text:
        qs = urllib.parse.parse_qs(text)
        return (qs.get("code") or [None])[0], (qs.get("state") or [None])[0]
    return text, None


class AnthropicOAuthProvider:
    """Sync-style API but async-implemented; matches the protocol shape in
    `auth/types.py`."""

    id = "anthropic"
    name = "Anthropic (Claude Pro/Max)"

    async def login(
        self,
        *,
        on_auth: OnAuth,
        on_prompt: OnPrompt,
        on_progress: OnProgress | None = None,
    ) -> OAuthCredentials:
        verifier, challenge = generate_pkce()
        # Upstream uses verifier as state — keep parity so manual-paste URLs work.
        state = verifier
        url = _build_authorize_url(challenge, state)

        async with CallbackServer(
            host=_CALLBACK_HOST,
            port=_CALLBACK_PORT,
            path=_CALLBACK_PATH,
            expected_state=state,
            success_message="Anthropic authentication completed. You can close this window.",
        ) as server:
            await on_auth(
                OAuthAuthInfo(
                    url=url,
                    instructions=(
                        "Complete login in your browser. If the browser is on"
                        " another machine, paste the final redirect URL here."
                    ),
                )
            )

            from .types import OAuthPrompt

            # Wait for either the browser callback or a manual paste.
            wait_task = asyncio.create_task(server.wait_for_code(timeout=600))
            prompt_task = asyncio.create_task(
                on_prompt(
                    OAuthPrompt(
                        message="Paste the redirect URL or authorization code (or wait for browser):",
                        placeholder=_REDIRECT_URI,
                    )
                )
            )

            code: str | None = None
            try:
                done, _pending = await asyncio.wait(
                    {wait_task, prompt_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wait_task in done and not wait_task.cancelled():
                    res = wait_task.result()
                    if res is not None:
                        code = res.code
                if code is None and prompt_task in done and not prompt_task.cancelled():
                    text = (prompt_task.result() or "").strip()
                    parsed_code, parsed_state = _parse_manual_input(text)
                    if parsed_state and parsed_state != state:
                        raise RuntimeError("OAuth state mismatch")
                    code = parsed_code
            finally:
                wait_task.cancel()
                prompt_task.cancel()
                server.cancel()

            if not code:
                raise RuntimeError("Anthropic OAuth aborted (no code)")

            if on_progress:
                await on_progress("Exchanging authorization code for tokens…")

            return await asyncio.to_thread(
                self._exchange_code, code, state, verifier
            )

    async def refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        return await asyncio.to_thread(self._refresh, creds.refresh)

    # ---- HTTP --------------------------------------------------------

    def _exchange_code(self, code: str, state: str, verifier: str) -> OAuthCredentials:
        data = _post_json(
            _TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": _CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        return self._creds_from_response(data)

    def _refresh(self, refresh_token: str) -> OAuthCredentials:
        data = _post_json(
            _TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": _CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
        return self._creds_from_response(data)

    @staticmethod
    def _creds_from_response(data: dict[str, Any]) -> OAuthCredentials:
        try:
            access = data["access_token"]
            refresh = data["refresh_token"]
            expires_in = float(data["expires_in"])
        except (KeyError, ValueError) as e:
            raise RuntimeError(f"unexpected Anthropic token response: {data}") from e

        # 5 minutes safety margin so we refresh before the server drops us.
        expires = time.time() + expires_in - 300
        scope = data.get("scope")
        return OAuthCredentials(
            access=access,
            refresh=refresh,
            expires=expires,
            extra={"scope": scope} if scope else {},
        )


anthropic_oauth = AnthropicOAuthProvider()
