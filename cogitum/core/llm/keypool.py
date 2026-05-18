"""
KeyPool — multi-key state machine with health tracking, quota windows,
cooldowns and async lease semantics.

Design goals:
  * Many keys per provider, all considered equivalent at load time.
  * Equal-burn routing by default — keys are picked to keep their
    rolling RPM/TPM usage as level as possible.
  * 429 / network errors put a key on exponential cooldown but never
    discard it; auth errors disable the key for the session.
  * Lease lifecycle is explicit: `async with pool.lease(...) as lease:`
    so consumers can record outcomes (`ok`, `rate_limited`, `auth_error`,
    `error`) before the lease auto-closes.
  * Lock-free fast path; a single asyncio.Lock guards key picking only.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .credentials import CredentialError, CredentialResolver, default_resolver

if TYPE_CHECKING:
    from .provider import KeyConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class KeyStatus(str, Enum):
    ACTIVE = "active"
    RATE_LIMITED = "rate_limited"
    AUTH_ERROR = "auth_error"            # 401/403 — disabled for the session
    QUOTA_EXHAUSTED = "quota_exhausted"  # daily cap reached
    DISABLED = "disabled"                # user-disabled in config


# ---------------------------------------------------------------------------
# Cooldown ladder for 429 / transient
# ---------------------------------------------------------------------------

_COOLDOWN_LADDER_S: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 60.0, 120.0)


def _next_cooldown(streak: int) -> float:
    idx = min(streak, len(_COOLDOWN_LADDER_S) - 1)
    base = _COOLDOWN_LADDER_S[idx]
    # +/- 20% jitter so multiple keys don't recover synchronously
    return base * random.uniform(0.8, 1.2)


# ---------------------------------------------------------------------------
# Sliding window counter
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _Window:
    """Rolling counter over a fixed time window."""
    span_s: float
    events: deque[tuple[float, int]] = field(default_factory=deque)
    total: int = 0

    def add(self, amount: int, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self.events.append((now, amount))
        self.total += amount
        self._evict(now)

    def value(self, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        self._evict(now)
        return self.total

    def _evict(self, now: float) -> None:
        cutoff = now - self.span_s
        ev = self.events
        while ev and ev[0][0] < cutoff:
            _, amt = ev.popleft()
            self.total -= amt


# ---------------------------------------------------------------------------
# Key state
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class KeyState:
    """Live state for a single API key inside a pool."""
    config: "KeyConfig"
    secret: str                                  # resolved value
    status: KeyStatus = KeyStatus.ACTIVE
    inflight: int = 0                            # active leases
    last_used: float = 0.0
    error_streak: int = 0                        # consecutive transient failures
    cooldown_until: float = 0.0                  # monotonic timestamp
    rpm: _Window = field(default_factory=lambda: _Window(60.0))
    tpm: _Window = field(default_factory=lambda: _Window(60.0))
    rpd: _Window = field(default_factory=lambda: _Window(86400.0))
    total_requests: int = 0
    total_tokens: int = 0
    last_error: str = ""

    # ----- predicates -----

    def is_available(self, now: float) -> bool:
        if not self.config.enabled or self.status in (
            KeyStatus.AUTH_ERROR,
            KeyStatus.DISABLED,
            KeyStatus.QUOTA_EXHAUSTED,
        ):
            return False
        if self.status == KeyStatus.RATE_LIMITED and now < self.cooldown_until:
            return False
        return self._under_local_limits(now)

    def _under_local_limits(self, now: float) -> bool:
        cfg = self.config
        if cfg.rpm_limit is not None and self.rpm.value(now) >= cfg.rpm_limit:
            return False
        if cfg.tpm_limit is not None and self.tpm.value(now) >= cfg.tpm_limit:
            return False
        if cfg.rpd_limit is not None and self.rpd.value(now) >= cfg.rpd_limit:
            return False
        return True

    # ----- routing score (lower = preferred) -----

    def score(self, now: float) -> float:
        """Equal-burn metric: weighted RPM share + small bias for least-recent."""
        weight = max(self.config.weight, 1e-3)
        rpm_load = self.rpm.value(now) / weight
        # Tiebreaker: prefer keys not used recently.
        recency = max(0.0, 5.0 - (now - self.last_used))
        return rpm_load + recency * 0.01 + self.inflight * 0.5


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------

class LeaseOutcome(str, Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    AUTH_ERROR = "auth_error"
    QUOTA_EXHAUSTED = "quota_exhausted"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class KeyLease:
    """A short-lived checkout of one key from the pool.

    Use as `async with pool.lease() as lease:` and call
    `lease.record(...)` before the block ends.
    """
    pool: "KeyPool"
    state: KeyState
    started_at: float
    closed: bool = False
    outcome: LeaseOutcome = LeaseOutcome.ERROR
    tokens_used: int = 0
    error_msg: str = ""
    # Explicit cooldown override in seconds. Set when the provider's
    # 429 response carried a Retry-After header — that value is more
    # accurate than our exponential ladder. Pool reads this on release
    # and applies it directly (with a +2s safety pad). Zero / None
    # means "use the ladder".
    cooldown_hint: float = 0.0

    @property
    def secret(self) -> str:
        return self.state.secret

    @property
    def key_id(self) -> str:
        return self.state.config.id

    def record(
        self,
        outcome: LeaseOutcome,
        *,
        tokens: int = 0,
        error: str = "",
        cooldown_hint: float = 0.0,
    ) -> None:
        self.outcome = outcome
        self.tokens_used = tokens
        self.error_msg = error
        if cooldown_hint > 0:
            self.cooldown_hint = cooldown_hint

    def release(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.pool._release(self)

    async def __aenter__(self) -> "KeyLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc is not None and self.outcome == LeaseOutcome.ERROR and not self.error_msg:
            self.error_msg = f"{exc_type.__name__}: {exc}"
        self.release()


class NoKeyAvailable(RuntimeError):
    """All keys in the pool are unavailable (rate-limited, errored, disabled)."""


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

class KeyPool:
    """Pool of `KeyState`s for one provider."""

    states: list[KeyState]
    _lock: asyncio.Lock

    def __init__(
        self,
        states: list[KeyState],
        *,
        provider_id: str = "",
    ) -> None:
        self.states = states
        self.provider_id = provider_id
        self._lock = asyncio.Lock()

    # ---- construction ----------------------------------------------------

    @classmethod
    def from_configs(
        cls,
        keys: list["KeyConfig"],
        *,
        provider_id: str = "",
        resolver: CredentialResolver | None = None,
    ) -> "KeyPool":
        resolver = resolver or default_resolver()
        states: list[KeyState] = []
        for cfg in keys:
            if not cfg.enabled:
                continue
            try:
                secret = resolver.resolve(cfg.secret_ref)
            except CredentialError as e:
                logger.warning(
                    "key %s/%s: skipping (credential resolve failed: %s)",
                    provider_id, cfg.id, e,
                )
                continue
            states.append(KeyState(config=cfg, secret=secret))
        return cls(states, provider_id=provider_id)

    # ---- introspection ---------------------------------------------------

    @property
    def size(self) -> int:
        return len(self.states)

    @property
    def active_count(self) -> int:
        now = time.monotonic()
        return sum(1 for s in self.states if s.is_available(now))

    def snapshot(self) -> list[dict[str, object]]:
        """Plain-dict snapshot for the UI / debug commands."""
        now = time.monotonic()
        out = []
        for s in self.states:
            out.append({
                "id": s.config.id,
                "status": s.status.value,
                "inflight": s.inflight,
                "rpm": s.rpm.value(now),
                "tpm": s.tpm.value(now),
                "rpd": s.rpd.value(now),
                "error_streak": s.error_streak,
                "cooldown_in_s": max(0.0, s.cooldown_until - now),
                "total_requests": s.total_requests,
                "total_tokens": s.total_tokens,
                "last_error": s.last_error,
            })
        return out

    # ---- pick & lease ----------------------------------------------------

    async def lease(self) -> KeyLease:
        """Pick the best available key and return a lease."""
        async with self._lock:
            now = time.monotonic()
            self._auto_recover(now)
            candidates = [s for s in self.states if s.is_available(now)]
            if not candidates:
                # Double-check: force recover any keys whose cooldown has passed
                for s in self.states:
                    if s.status == KeyStatus.RATE_LIMITED and now >= s.cooldown_until:
                        s.status = KeyStatus.ACTIVE
                candidates = [s for s in self.states if s.is_available(now)]
            if not candidates:
                cooldown = self._earliest_cooldown(now)
                raise NoKeyAvailable(
                    f"all {len(self.states)} keys unavailable in pool"
                    f" {self.provider_id!r}"
                    + (f" (next recovery in {cooldown:.1f}s)" if cooldown else "")
                )
            # Lowest score wins. Random tie-break.
            candidates.sort(key=lambda s: (s.score(now), random.random()))
            pick = candidates[0]
            pick.inflight += 1
            pick.last_used = now
            pick.rpm.add(1, now)
            pick.rpd.add(1, now)
            return KeyLease(pool=self, state=pick, started_at=now)

    # ---- internals -------------------------------------------------------

    def _release(self, lease: KeyLease) -> None:
        s = lease.state
        s.inflight = max(0, s.inflight - 1)
        s.total_requests += 1
        s.total_tokens += lease.tokens_used
        if lease.tokens_used:
            s.tpm.add(lease.tokens_used)

        if lease.outcome == LeaseOutcome.OK:
            s.error_streak = 0
            s.status = KeyStatus.ACTIVE
            s.cooldown_until = 0.0
            s.last_error = ""
            return

        if lease.outcome == LeaseOutcome.RATE_LIMITED:
            s.error_streak += 1
            s.status = KeyStatus.RATE_LIMITED
            # Prefer an explicit Retry-After hint from the provider —
            # it's more accurate than our exponential ladder. Pad +2s
            # so we don't race the clock and re-hit the same 429
            # immediately when the cooldown expires. Cap at the ladder
            # max so a hostile provider can't park our key for hours.
            if lease.cooldown_hint > 0:
                cooldown = min(lease.cooldown_hint + 2.0, _COOLDOWN_LADDER_S[-1])
            elif len(self.states) == 1:
                # Single-key pool with no hint: use minimal cooldown
                # so the agent-level retry can do the backoff. Don't
                # punish the only key for minutes.
                cooldown = min(_next_cooldown(s.error_streak - 1), 5.0)
            else:
                cooldown = _next_cooldown(s.error_streak - 1)
            s.cooldown_until = time.monotonic() + cooldown
            s.last_error = lease.error_msg or "rate limited"
            logger.info(
                "key %s/%s rate-limited (streak=%d, cooldown=%.1fs%s)",
                self.provider_id, s.config.id, s.error_streak,
                cooldown,
                " from Retry-After" if lease.cooldown_hint > 0 else "",
            )
            return

        if lease.outcome == LeaseOutcome.AUTH_ERROR:
            s.status = KeyStatus.AUTH_ERROR
            s.last_error = lease.error_msg or "auth error"
            logger.warning(
                "key %s/%s auth error — disabled for session",
                self.provider_id, s.config.id,
            )
            return

        if lease.outcome == LeaseOutcome.QUOTA_EXHAUSTED:
            s.status = KeyStatus.QUOTA_EXHAUSTED
            s.last_error = lease.error_msg or "quota exhausted"
            return

        # generic ERROR / CANCELLED
        if lease.outcome == LeaseOutcome.CANCELLED:
            # User-initiated cancel should NOT penalize the key
            s.last_error = ""
            return

        s.error_streak += 1
        s.last_error = lease.error_msg or "error"
        # Soft cooldown after 3 consecutive errors (network issues, timeouts)
        # Cap streak influence at 8 to prevent absurd cooldowns
        if s.error_streak >= 3:
            effective_streak = min(s.error_streak - 3, len(_COOLDOWN_LADDER_S) - 1)
            cooldown = _next_cooldown(effective_streak)
            # Single-key pool: cap at 10s for generic errors
            if len(self.states) == 1:
                cooldown = min(cooldown, 10.0)
            s.status = KeyStatus.RATE_LIMITED
            s.cooldown_until = time.monotonic() + cooldown

    def _auto_recover(self, now: float) -> None:
        for s in self.states:
            if s.status == KeyStatus.RATE_LIMITED and now >= s.cooldown_until:
                s.status = KeyStatus.ACTIVE

    def _earliest_cooldown(self, now: float) -> float:
        candidates = [
            s.cooldown_until - now
            for s in self.states
            if s.status == KeyStatus.RATE_LIMITED and s.cooldown_until > now
        ]
        if candidates:
            return max(min(candidates), 0.5)  # at least 0.5s to avoid busy-loop
        # If all rate-limited keys have expired cooldowns, recover them now
        for s in self.states:
            if s.status == KeyStatus.RATE_LIMITED and s.cooldown_until <= now:
                s.status = KeyStatus.ACTIVE
        return 0.0


__all__ = [
    "KeyPool",
    "KeyState",
    "KeyStatus",
    "KeyLease",
    "LeaseOutcome",
    "NoKeyAvailable",
]
