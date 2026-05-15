"""Shared types for OAuth flows."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass(slots=True)
class OAuthCredentials:
    """Tokens for one subscription. `expires` is unix-epoch seconds."""
    access: str
    refresh: str
    expires: float
    extra: dict[str, Any] = field(default_factory=dict)

    def expired(self, *, leeway_s: float = 60.0) -> bool:
        return time.time() + leeway_s >= self.expires

    def as_dict(self) -> dict[str, Any]:
        return {
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuthCredentials":
        return cls(
            access=str(data["access"]),
            refresh=str(data["refresh"]),
            expires=float(data["expires"]),
            extra=dict(data.get("extra") or {}),
        )


@dataclass(slots=True)
class OAuthAuthInfo:
    url: str
    instructions: str = ""


@dataclass(slots=True)
class OAuthPrompt:
    message: str
    placeholder: str = ""


# Callbacks the UI implements. The TUI wizard wires these to its dialogs;
# the headless `cog setup` flow wires them to plain stdin/stdout.

OnAuth = Callable[[OAuthAuthInfo], Awaitable[None]]
OnPrompt = Callable[[OAuthPrompt], Awaitable[str]]
OnProgress = Callable[[str], Awaitable[None]]


class OAuthProvider(Protocol):
    """Subscription-style auth provider."""

    id: str
    name: str

    async def login(
        self,
        *,
        on_auth: OnAuth,
        on_prompt: OnPrompt,
        on_progress: OnProgress | None = None,
    ) -> OAuthCredentials: ...

    async def refresh(self, creds: OAuthCredentials) -> OAuthCredentials: ...
