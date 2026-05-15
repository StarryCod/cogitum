"""Static HTML for OAuth callback responses. Imperial Fists colorway."""

from __future__ import annotations


_BASE_CSS = """
  :root {
    color-scheme: dark;
    --bg: #0E0E11;
    --card: #16161B;
    --border: #2A2620;
    --text: #E6E1CF;
    --muted: #8C8678;
    --gold: #F5C24A;
    --bronze: #A8732D;
    --rust: #9B3A2A;
  }
  html, body { background: var(--bg); color: var(--text); margin: 0; height: 100%; font: 15px/1.5 system-ui, sans-serif; }
  main { min-height: 100%; display: grid; place-items: center; padding: 32px; }
  .card { width: min(420px, 100%); background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 28px; }
  .seal { width: 48px; height: 48px; border-radius: 14px; background: rgba(245,194,74,0.08); border: 1px solid rgba(245,194,74,0.18); color: var(--gold); display: grid; place-items: center; font-weight: 600; font-size: 22px; margin-bottom: 18px; }
  .seal.err { background: rgba(155,58,42,0.10); border-color: rgba(155,58,42,0.30); color: var(--rust); }
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 8px; letter-spacing: -0.01em; }
  p { color: var(--muted); margin: 0 0 6px; font-size: 14px; }
  code { font-family: ui-monospace, "JetBrains Mono", monospace; color: var(--bronze); font-size: 12px; word-break: break-all; }
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<title>{title}</title>
<style>{_BASE_CSS}</style>
</head><body><main>{body}</main></body></html>
"""


def oauth_success_html(message: str) -> str:
    return _page(
        "Cogitum — authenticated",
        f"""<section class="card">
  <div class="seal">⬡</div>
  <h1>Cogitum</h1>
  <p>{_escape(message)}</p>
</section>""",
    )


def oauth_error_html(message: str, detail: str = "") -> str:
    detail_html = f'<p><code>{_escape(detail)}</code></p>' if detail else ""
    return _page(
        "Cogitum — error",
        f"""<section class="card">
  <div class="seal err">!</div>
  <h1>Cogitum</h1>
  <p>{_escape(message)}</p>
  {detail_html}
</section>""",
    )


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
