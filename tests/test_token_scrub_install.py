"""Tier-4 R2: install_token_scrub_filter() must survive logging
reconfiguration (e.g. user-level dictConfig after import).

Before R2 the filter was attached at module-import time as a side
effect. Any user code that called ``logging.config.dictConfig({...})``
afterwards wiped filters off the root logger and the bot URL leaked
back into logs. The R2 fix exposes ``install_token_scrub_filter()`` so
the gateway can re-attach the filter at start() time, and ALSO does
the install at import for the common case.
"""
from __future__ import annotations

import logging
import logging.config

from cogitum.gateway.telegram import (
    _TokenScrubFilter,
    install_token_scrub_filter,
)


def _has_scrub_filter(logger: logging.Logger) -> bool:
    return any(isinstance(f, _TokenScrubFilter) for f in logger.filters)


def test_install_filter_idempotent():
    """Calling install twice must not double-stack the filter."""
    install_token_scrub_filter()
    install_token_scrub_filter()
    root = logging.getLogger()
    matches = [f for f in root.filters if isinstance(f, _TokenScrubFilter)]
    assert len(matches) == 1


def test_filter_reattaches_after_dictconfig_wipe():
    """Simulate user code calling logging.config.dictConfig — that
    rebuilds the root logger's handler list and clears its filters.
    The gateway's start() path must restore the filter."""
    install_token_scrub_filter()
    root = logging.getLogger()
    assert _has_scrub_filter(root)

    # Wipe filters by hand (dictConfig with disable_existing_loggers=True
    # would do similar in practice, but is more invasive for a test).
    root.filters = []
    # And clear the cache attribute the install() guard checks so we're
    # forced through the real install path.
    if hasattr(root, "_token_scrub_installed"):
        delattr(root, "_token_scrub_installed")

    assert not _has_scrub_filter(root)

    install_token_scrub_filter()
    assert _has_scrub_filter(root)


def test_filter_redacts_token_in_log_message():
    """End-to-end: a record passed through the filter has the token
    scrubbed from its formatted message. We test the filter directly
    rather than via caplog because caplog captures records BEFORE
    handler emission (where filters run); using a custom handler that
    runs after the filter chain would be the only reliable caplog
    path, and the direct unit test is clearer."""
    install_token_scrub_filter()
    secret = "1234567890:SECRET_TOKEN_DO_NOT_LEAK_ABCDEF"

    record = logging.LogRecord(
        name="cogitum.gateway.telegram",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="connect failed: %s",
        args=(f"https://api.telegram.org/bot{secret}/getMe",),
        exc_info=None,
    )

    # Run the filter as the logging machinery would.
    root = logging.getLogger()
    for f in root.filters:
        if isinstance(f, _TokenScrubFilter):
            f.filter(record)

    rendered = record.getMessage()
    assert "SECRET_TOKEN_DO_NOT_LEAK" not in rendered
    assert "/bot<REDACTED>/" in rendered
