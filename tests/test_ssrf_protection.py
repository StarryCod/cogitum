"""Tier-2 security regression tests for fetch_url / _is_url_safe.

Covers:
  * SSRF via redirect chain (302 -> internal IP)
  * SSRF via obfuscated hostname forms (decimal, octal, IPv6-mapped, …)
  * SSRF via DNS resolution to a private address
  * Decompression-bomb DoS (gzip-style oversize body)
  * Round-2 additions:
      - CGNAT range (100.64.0.0/10) blocked direct + via DNS
      - Empty Location header → ERROR within 1 hop (no DoS amp)
      - Location resolving to current URL → ERROR within 1 hop
  * Regression: legitimate public URL still works

NOTE on imports / conftest: ``tests/conftest.py`` has an autouse
fixture that pops every ``cogitum.*`` module from ``sys.modules`` between
tests. The ``_is_url_safe``/``fetch_url`` references imported at the
top of this file therefore point at the FIRST loaded copy of
``cogitum.core.builtin_tools``. Patches against
``cogitum.core.builtin_tools.socket.getaddrinfo`` still work because
we patch the ``socket`` module's ``getaddrinfo`` attribute, which is
shared across all module copies. ``test_browser_ssrf.py`` works around
the same trap with the local ``_bt()`` import-inside-test pattern.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch, MagicMock

import pytest

from cogitum.core.builtin_tools import _is_url_safe, fetch_url


# ---------------------------------------------------------------------------
# Helpers — fake httpx.AsyncClient surface so we can drive fetch_url
# without doing any real I/O.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Mimics the bits of httpx.Response that fetch_url touches."""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict | None = None,
        chunks: list[bytes] | None = None,
        encoding: str = "utf-8",
    ):
        self.status_code = status_code
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._chunks = chunks if chunks is not None else [b""]
        self.encoding = encoding

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self, chunk_size: int = 65536):
        for c in self._chunks:
            # In real httpx, oversize chunks are split — we honour the
            # requested chunk_size so the cap-check inside fetch_url
            # actually fires after each yielded slice.
            for i in range(0, len(c), chunk_size):
                yield c[i : i + chunk_size]


class _FakeClient:
    """Drop-in for httpx.AsyncClient. Replays a queue of responses."""

    def __init__(self, responses):
        # responses can be a list of _FakeResponse OR a callable(url) -> _FakeResponse
        self._responses = responses
        self.requests: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        self.requests.append(url)
        if callable(self._responses):
            resp = self._responses(url)
        else:
            resp = self._responses.pop(0)

        @asynccontextmanager
        async def _cm():
            yield resp

        return _cm()


def _patch_httpx(client: _FakeClient):
    """Patch the `httpx` module that fetch_url imports lazily."""
    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = lambda *a, **kw: client
    return patch.dict("sys.modules", {"httpx": fake_httpx})


def _patch_dns_public():
    """Pin getaddrinfo to a public IP for every host. Lets us test
    fetch_url paths without depending on real DNS in CI."""
    fake_info = [(2, 1, 6, "", ("8.8.8.8", 0))]
    return patch(
        "cogitum.core.builtin_tools.socket.getaddrinfo", return_value=fake_info
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Redirect-bypass tests (Fix #1)
# ---------------------------------------------------------------------------


def test_redirect_to_metadata_server_blocked():
    # Public URL 302's into AWS IMDS — fetch_url must reject the hop.
    client = _FakeClient([
        _FakeResponse(
            status_code=302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
        ),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/"))
    assert out.startswith("ERROR: redirect blocked")
    # Reason from _is_url_safe should mention the IMDS IP.
    assert "169.254.169.254" in out


def test_redirect_to_localhost_blocked():
    client = _FakeClient([
        _FakeResponse(
            status_code=302,
            headers={"location": "http://127.0.0.1:5000/admin"},
        ),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/"))
    assert out.startswith("ERROR: redirect blocked")
    assert "127.0.0.1" in out


def test_redirect_chain_max_hops():
    # 7 consecutive 302's, all to public hosts — must give up after 5.
    chain = [f"http://hop{i}.example.com/" for i in range(7)]
    responses = [
        _FakeResponse(status_code=302, headers={"location": chain[i + 1]})
        for i in range(6)
    ]
    # Shouldn't be reached, but provide a final 200 just in case.
    responses.append(_FakeResponse(status_code=200, chunks=[b"ok"]))
    client = _FakeClient(responses)
    # Pin DNS to a public IP so _is_url_safe lets each hop through —
    # we want to test the redirect-count limit, not the resolver.
    fake_info = [(2, 1, 6, "", ("8.8.8.8", 0))]
    with _patch_httpx(client), patch(
        "cogitum.core.builtin_tools.socket.getaddrinfo", return_value=fake_info
    ):
        out = _run(fetch_url(chain[0]))
    assert out.startswith("ERROR")
    assert "too many redirects" in out


def test_relative_redirect_resolved_against_current_url():
    # 302 with a relative Location must be joined against the current
    # URL before re-validation (otherwise "/admin" wouldn't be a URL).
    client = _FakeClient([
        _FakeResponse(status_code=302, headers={"location": "/page2"}),
        _FakeResponse(
            status_code=200,
            headers={"content-type": "text/plain"},
            chunks=[b"hello"],
        ),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/start"))
    assert out == "hello"
    # Second request URL should be the joined absolute form.
    assert client.requests[1] == "http://example.com/page2"


# ---------------------------------------------------------------------------
# Obfuscated-host tests (Fix #2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",          # decimal form of 127.0.0.1
        "http://127.1/",                # short form
        "http://0/",                    # 0.0.0.0
        "http://[::ffff:127.0.0.1]/",   # IPv6-mapped IPv4 loopback
        "http://0177.0.0.1/",           # octal form
        "http://[::1]/",                # IPv6 loopback
        "http://169.254.169.254/",      # AWS metadata
        "http://localhost/",
        "http://127.0.0.1/",
    ],
)
def test_obfuscated_local_hosts_blocked(url):
    ok, reason = _is_url_safe(url)
    assert not ok, f"expected {url} to be blocked, got ok={ok}"
    assert reason  # non-empty explanation


def test_decimal_ip_blocked():
    ok, _ = _is_url_safe("http://2130706433/")
    assert not ok


def test_short_ip_blocked():
    assert not _is_url_safe("http://127.1/")[0]
    assert not _is_url_safe("http://0/")[0]


def test_ipv6_mapped_blocked():
    assert not _is_url_safe("http://[::ffff:127.0.0.1]/")[0]


def test_octal_ip_blocked():
    assert not _is_url_safe("http://0177.0.0.1/")[0]


def test_invalid_scheme_blocked():
    assert not _is_url_safe("ftp://example.com/")[0]
    assert not _is_url_safe("file:///etc/passwd")[0]


def test_dns_resolves_to_private_blocked():
    """A public-looking domain whose A record points at 127.0.0.1
    must be rejected — this is the static half of DNS-rebind defence."""
    fake_info = [(2, 1, 6, "", ("127.0.0.1", 0))]
    with patch("cogitum.core.builtin_tools.socket.getaddrinfo", return_value=fake_info):
        ok, reason = _is_url_safe("http://attacker.example/")
    assert not ok
    assert "non-public" in reason
    assert "127.0.0.1" in reason


def test_dns_any_private_in_set_blocks():
    """If getaddrinfo returns multiple addresses and ANY of them is
    private, reject — otherwise an attacker just needs one bogus
    A-record alongside a real one."""
    fake_info = [
        (2, 1, 6, "", ("8.8.8.8", 0)),
        (2, 1, 6, "", ("10.0.0.5", 0)),
    ]
    with patch("cogitum.core.builtin_tools.socket.getaddrinfo", return_value=fake_info):
        ok, _ = _is_url_safe("http://attacker.example/")
    assert not ok


def test_dns_failure_blocks():
    import socket as _s
    with patch(
        "cogitum.core.builtin_tools.socket.getaddrinfo",
        side_effect=_s.gaierror("nodename nor servname provided"),
    ):
        ok, reason = _is_url_safe("http://nope.invalid/")
    assert not ok
    assert "DNS" in reason


# ---------------------------------------------------------------------------
# Decompression-bomb test (Fix #3)
# ---------------------------------------------------------------------------


def test_decompression_bomb_caps_at_5mb():
    """Body that decompresses to ~10 MB must be aborted before the
    full payload is buffered — the cap is 5 MB."""
    # Generator-style: yield 1 MB at a time so we can observe the
    # streaming abort without actually allocating gigabytes.
    one_mb = b"A" * (1024 * 1024)
    chunks = [one_mb] * 10  # 10 MB total — well over the 5 MB cap
    client = _FakeClient([
        _FakeResponse(
            status_code=200,
            headers={"content-type": "text/plain"},
            chunks=chunks,
        ),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/"))
    assert out.startswith("ERROR")
    assert "too large" in out or "5MB" in out


def test_body_under_cap_returned():
    """Regression: a 1 KB body comes back intact."""
    client = _FakeClient([
        _FakeResponse(
            status_code=200,
            headers={"content-type": "text/plain"},
            chunks=[b"x" * 1024],
        ),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/", max_chars=2048))
    assert out == "x" * 1024


# ---------------------------------------------------------------------------
# Regression — happy path still works
# ---------------------------------------------------------------------------


def test_legitimate_public_url_still_works():
    body = b"<html><body><p>Hello world</p></body></html>"
    client = _FakeClient([
        _FakeResponse(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            chunks=[body],
        ),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/"))
    assert "Hello world" in out
    assert "<html>" not in out  # HTML stripped


def test_plain_text_returned_verbatim():
    client = _FakeClient([
        _FakeResponse(
            status_code=200,
            headers={"content-type": "text/plain"},
            chunks=[b"line1\nline2"],
        ),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/"))
    assert out == "line1\nline2"


def test_unsafe_initial_url_short_circuits():
    """If the URL itself is unsafe, no httpx call should be made."""
    client = _FakeClient([])  # empty — would IndexError if used
    with _patch_httpx(client):
        out = _run(fetch_url("http://127.0.0.1/"))
    assert out.startswith("ERROR")
    assert client.requests == []


# ---------------------------------------------------------------------------
# Round-2: CGNAT (RFC 6598, 100.64.0.0/10) — F-3.
# ---------------------------------------------------------------------------


def test_cgnat_blocked():
    """Direct CGNAT IP literal must be rejected."""
    ok, reason = _is_url_safe("http://100.64.0.5/")
    assert not ok
    assert "CGNAT" in reason


def test_cgnat_high_block_blocked():
    """100.127.0.0 is at the upper end of 100.64.0.0/10."""
    ok, reason = _is_url_safe("http://100.127.0.1/")
    assert not ok
    assert "CGNAT" in reason


def test_cgnat_via_dns_blocked():
    """Domain whose A-record falls inside CGNAT — same outcome."""
    fake_info = [(2, 1, 6, "", ("100.64.0.5", 0))]
    with patch(
        "cogitum.core.builtin_tools.socket.getaddrinfo", return_value=fake_info
    ):
        ok, reason = _is_url_safe("http://attacker.example/")
    assert not ok
    assert "CGNAT" in reason


def test_just_above_cgnat_allowed():
    """100.128.0.0 is OUTSIDE 100.64.0.0/10 — must NOT be blocked.

    Defends against the regression where someone widens the mask too
    far (e.g. uses /8 by accident). Real public IPs in 100.128.0.0/9
    do exist on the internet.
    """
    fake_info = [(2, 1, 6, "", ("100.128.0.5", 0))]
    with patch(
        "cogitum.core.builtin_tools.socket.getaddrinfo", return_value=fake_info
    ):
        ok, _reason = _is_url_safe("http://public.example/")
    assert ok


# ---------------------------------------------------------------------------
# Round-2: empty / fragment-only / same-URL Location — F-4.
# ---------------------------------------------------------------------------


def test_redirect_with_empty_location_errors():
    """``Location:`` with empty value must error within 1 hop, not loop."""
    client = _FakeClient([
        _FakeResponse(status_code=302, headers={"location": ""}),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/"))
    assert out.startswith("ERROR")
    assert "empty Location" in out
    # Crucial: only ONE request to the upstream server. The DoS-amp bug
    # would have produced len(requests) == MAX_REDIRECTS + 1.
    assert len(client.requests) == 1


def test_redirect_to_same_url_no_loop():
    """``Location`` resolving to the current URL → ERROR, single hop."""
    client = _FakeClient([
        _FakeResponse(
            status_code=302, headers={"location": "http://example.com/"}
        ),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/"))
    assert out.startswith("ERROR")
    assert "same URL" in out
    assert len(client.requests) == 1


def test_redirect_fragment_only_no_loop():
    """``Location: #foo`` resolves to current URL + fragment — same target."""
    client = _FakeClient([
        _FakeResponse(status_code=302, headers={"location": "#anchor"}),
    ])
    with _patch_httpx(client):
        out = _run(fetch_url("http://example.com/page"))
    assert out.startswith("ERROR")
    assert "same URL" in out
    assert len(client.requests) == 1
