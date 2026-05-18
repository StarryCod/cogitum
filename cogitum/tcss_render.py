"""
cogitum.tcss_render
~~~~~~~~~~~~~~~~~~~

Theme-aware CSS renderer for Cogitum's main TCSS.

The .tcss file shipped at ``cogitum/cogitum.tcss.template`` contains
``$TOKEN`` placeholders (no braces) which are substituted with hex
values from the active theme's token map. The rendered string is
fed to Textual via ``App.CSS`` (class-level), which is computed at
module import time when the app class is defined.

Why string substitution and not Python format strings: the template
is a normal CSS file and CSS uses ``{`` ``}`` extensively for blocks.
``string.Template`` (``$NAME``) avoids escaping every single brace.

Why CSS class attr instead of writing a generated tcss to disk:
Textual's CSS_PATH expects a path relative to the App class's module,
which makes per-instance overrides awkward. Setting App.CSS to a
rendered string is simpler and avoids a stale-file problem when
the user swaps themes (no risk of an out-of-date generated file
sitting in the package dir).
"""
from __future__ import annotations

import logging
from pathlib import Path
from string import Template

from .themes import TOKEN_NAMES, get_active_theme

log = logging.getLogger(__name__)


_TEMPLATE_PATH = Path(__file__).parent / "cogitum.tcss.template"


def render_tcss() -> str:
    """Substitute theme tokens into the TCSS template and return the result.

    Falls back to a minimal stub on any read/render error so the app
    still launches with a usable (if ugly) palette. Logs the actual
    failure at WARNING so a missing template gets noticed.
    """
    try:
        raw = _TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("tcss_render: failed to read template: %s", e)
        return _MINIMAL_STUB

    theme = get_active_theme()
    # Defensive: ensure every TOKEN_NAMES key is present. Missing
    # keys would otherwise leave the literal $TOKEN in the rendered
    # CSS and Textual would error on the unparseable colour.
    missing = [k for k in TOKEN_NAMES if k not in theme]
    if missing:
        log.warning("tcss_render: theme is missing tokens %s; using stub", missing)
        return _MINIMAL_STUB

    try:
        return Template(raw).substitute(theme)
    except (KeyError, ValueError) as e:
        log.warning("tcss_render: substitution failed: %s", e)
        return _MINIMAL_STUB


# Minimal CSS Cogitum can boot from when the template is unreadable
# or substitution blows up. Keeps the app launchable for diagnosis;
# users will see something stark but functional.
_MINIMAL_STUB = """\
Screen { background: #0E0E11; color: #E6E1CF; }
#main { layout: horizontal; height: 1fr; }
#feed-pane { width: 2fr; padding: 1 2; }
#inspector-pane { width: 1fr; padding: 1 2; }
.feed-entry { height: auto; margin: 1 0; }
"""


__all__ = ["render_tcss"]
