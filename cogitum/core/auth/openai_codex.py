"""
ChatGPT (Plus/Pro) OAuth via the Codex CLI client.

Same shape as Anthropic but with a different callback port (1455) and
auth.openai.com endpoints. The access token also embeds an OpenAI account
id we have to extract from the JWT for downstream API calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from .callback_server import CallbackServer
from .pkce import generate_pkce, random_state
from .types import (
    OAuthAuthInfo,
    OAuthCredentials,
    OAuthPrompt,
    OnAuth,
    OnPrompt,
    OnProgress,
)

logger = logging.getLogger(__name__)


_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CALLBACK_HOST = "127.0.0.1"
_CALLBACK_PORT = 1455
_CALLBACK_PATH = "/auth/callback"
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}{_CALLBACK_PATH}"
_SCOPE = "openid profile email offline_access"
_JWT_CLAIM = "https://api.openai.com/auth"


def _decode_jwt(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        # urlsafe + missing padding
        pad = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
        return json.loads(decoded)
    except Exception:
        return None


def _account_id(access_token: str) -> str | None:
    payload = _decode_jwt(access_token)
    if not payload:
        return None
    auth = payload.get(_JWT_CLAIM) or {}
    aid = auth.get("chatgpt_account_id")
    return aid if isinstance(aid, str) and aid else None


def _post_form(url: str, body: dict[str, str], *, timeout: float = 30.0) -> dict[str, Any]:
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"OpenAI token endpoint returned {e.code}: {text}") from e


def _parse_manual_input(text: str) -> tuple[str | None, str | None]:
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
    if "code=" in text and "=" in text:
        qs = urllib.parse.parse_qs(text)
        return (qs.get("code") or [None])[0], (qs.get("state") or [None])[0]
    return text, None


class OpenAICodexOAuthProvider:
    id = "openai-codex"
    name = "ChatGPT Plus/Pro (Codex Subscription)"

    async def login(
        self,
        *,
        on_auth: OnAuth,
        on_prompt: OnPrompt,
        on_progress: OnProgress | None = None,
    ) -> OAuthCredentials:
        verifier, challenge = generate_pkce()
        state = random_state(16)

        url = self._authorize_url(challenge, state)
        async with CallbackServer(
            host=_CALLBACK_HOST,
            port=_CALLBACK_PORT,
            path=_CALLBACK_PATH,
            expected_state=state,
            success_message="OpenAI authentication completed. You can close this window.",
        ) as server:
            await on_auth(
                OAuthAuthInfo(
                    url=url,
                    instructions="A browser window should open. Complete login to finish.",
                )
            )

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
                    parsed_code, parsed_state = _parse_manual_input(prompt_task.result() or "")
                    if parsed_state and parsed_state != state:
                        raise RuntimeError("OAuth state mismatch")
                    code = parsed_code
            finally:
                wait_task.cancel()
                prompt_task.cancel()
                server.cancel()

            if not code:
                raise RuntimeError("OpenAI OAuth aborted (no code)")

            if on_progress:
                await on_progress("Exchanging authorization code for tokens…")
            return await asyncio.to_thread(self._exchange_code, code, verifier)

    async def refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        return await asyncio.to_thread(self._refresh, creds.refresh)

    # ---- internals ----------------------------------------------------

    @staticmethod
    def _authorize_url(challenge: str, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "scope": _SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "cogitum",
        }
        return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def _exchange_code(self, code: str, verifier: str) -> OAuthCredentials:
        data = _post_form(
            _TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": _CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": _REDIRECT_URI,
            },
        )
        return self._creds_from_response(data)

    def _refresh(self, refresh_token: str) -> OAuthCredentials:
        data = _post_form(
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
            raise RuntimeError(f"unexpected OpenAI token response: {data}") from e

        expires = time.time() + expires_in - 300
        aid = _account_id(access)
        extra: dict[str, Any] = {}
        if aid:
            extra["account_id"] = aid
        return OAuthCredentials(access=access, refresh=refresh, expires=expires, extra=extra)


openai_codex_oauth = OpenAICodexOAuthProvider()
