"""F43: install_token_scrub_filter must NOT run at module import time.

Importing cogitum.gateway.telegram should leave the root logger
untouched. The filter is installed lazily by ``CogitumBot.start()`` and
``run_bot()`` — operators get the protection when the bot actually
runs, but pytest / dictConfig / external configurators are no longer
silently mutated by a side-effect import.
"""
from __future__ import annotations

import importlib
import logging
import sys


def _strip_cogitum_modules():
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)


def _root_filter_count():
    rl = logging.getLogger()
    return len(list(rl.filters))


def test_import_does_not_install_token_scrub_filter():
    """Mere import must not add a filter to the root logger."""
    _strip_cogitum_modules()
    # Snapshot the root filter list BEFORE importing the gateway.
    before = _root_filter_count()
    import cogitum.gateway.telegram  # noqa: F401
    after = _root_filter_count()
    assert after == before, (
        f"telegram import installed {after - before} root logger filter(s) — "
        "it should be lazy (called from start()/run_bot()), not module-level"
    )


def test_install_function_still_works_when_called_directly():
    """The lazy install path itself is unchanged — start() still wires it."""
    _strip_cogitum_modules()
    import cogitum.gateway.telegram as tg

    rl = logging.getLogger()
    before = len(list(rl.filters))
    tg.install_token_scrub_filter()
    after = len(list(rl.filters))
    assert after >= before  # filter is installed (or already-present)
    assert getattr(rl, "_token_scrub_installed", False) is True

    # Idempotent: second call shouldn't pile on duplicate filters.
    second = len(list(rl.filters))
    tg.install_token_scrub_filter()
    assert len(list(rl.filters)) == second
