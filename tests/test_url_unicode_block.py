"""F1: hostnames containing Cf-category Unicode must be blocked."""
from __future__ import annotations

import pytest

from cogitum.core.builtin_tools import _is_url_safe


def test_zwsp_in_hostname_blocked():
    """example\\u200b.com must be rejected — invisible char."""
    safe, reason = _is_url_safe("http://example\u200b.com/")
    assert safe is False
    assert "Cf" in reason or "format" in reason or "zero-width" in reason


def test_rlo_in_hostname_blocked():
    """RLO (U+202E) in hostname is rejected."""
    safe, reason = _is_url_safe("http://exa\u202emple.com/")
    assert safe is False


def test_zwj_in_hostname_blocked():
    safe, _ = _is_url_safe("http://example\u200d.com/")
    assert safe is False


def test_lrm_in_hostname_blocked():
    """Left-to-right mark (U+200E) — Cf category."""
    safe, _ = _is_url_safe("http://x\u200ey.com/")
    assert safe is False


def test_normal_ascii_hostname_passes_unicode_check():
    """Normal hostname only fails if private/etc — not on the unicode gate."""
    # example.com resolves to a public IP — we expect either pass or
    # a DNS error, but NOT the "Cf" rejection.
    safe, reason = _is_url_safe("http://example.com/")
    if not safe:
        assert "Cf" not in reason and "format" not in reason
