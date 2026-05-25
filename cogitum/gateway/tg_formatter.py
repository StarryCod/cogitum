"""
cogitum.gateway.tg_formatter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Convert agent output to Telegram MarkdownV2 format.
Handle message splitting, tool call formatting, etc.
"""
from __future__ import annotations

import re
from typing import Any


# Characters that must be escaped in Telegram MarkdownV2
_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2.

    Every character in _ESCAPE_CHARS gets a preceding backslash.
    This is safe for any plain text — but NOT for text that already
    contains TG formatting (bold, italic, code spans, etc.).
    """
    return re.sub(r"([" + re.escape(_ESCAPE_CHARS) + r"])", r"\\\1", text)


def safe_md(text: str) -> str:
    """Wrap text for safe MarkdownV2 — escape everything, no formatting.

    Use this when you don't need any formatting and just want safe plain text.
    """
    return escape_md(text)


def _convert_table(lines: list[str]) -> str:
    """Convert a markdown table to a readable format for Telegram.
    
    Input: list of lines forming a table (|col1|col2|...)
    Output: formatted text with aligned columns using monospace.
    """
    rows: list[list[str]] = []
    for line in lines:
        # Skip separator lines (|---|---|)
        stripped = line.strip()
        if stripped and not all(c in "|-: " for c in stripped):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            rows.append(cells)
    
    if not rows:
        return ""
    
    # Format as labeled rows (header: value pairs)
    headers = rows[0] if rows else []
    if len(rows) <= 1:
        # Just headers, no data
        return " ┃ ".join(headers)
    
    result_lines = []
    for row in rows[1:]:
        parts = []
        for i, cell in enumerate(row):
            header = headers[i] if i < len(headers) else f"col{i}"
            parts.append(f"*{escape_md(header)}:* {escape_md(cell)}")
        result_lines.append(" │ ".join(parts))
    
    return "\n".join(result_lines)


def markdown_to_tg(text: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2.

    Handles: bold, italic, code, code blocks, links, tables.
    Leaves the rest escaped.
    """
    if not text.strip():
        return ""

    result = []
    lines = text.split("\n")
    in_code_block = False
    code_block_lang = ""
    code_block_lines: list[str] = []
    table_lines: list[str] = []

    for line in lines:
        # Table detection (lines starting with |)
        if not in_code_block and line.strip().startswith("|") and "|" in line.strip()[1:]:
            table_lines.append(line)
            continue
        elif table_lines:
            # End of table — convert and flush
            result.append(_convert_table(table_lines))
            table_lines = []

        # Code block start/end
        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_block_lang = line.strip()[3:].strip()
                code_block_lines = []
            else:
                # End code block
                in_code_block = False
                code_content = "\n".join(code_block_lines)
                if code_block_lang:
                    result.append(f"```{code_block_lang}\n{code_content}\n```")
                else:
                    result.append(f"```\n{code_content}\n```")
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        # Process inline formatting
        processed = _process_inline(line)
        result.append(processed)

    # Handle unclosed table
    if table_lines:
        result.append(_convert_table(table_lines))

    # Handle unclosed code block
    if in_code_block:
        code_content = "\n".join(code_block_lines)
        result.append(f"```\n{code_content}\n```")

    return "\n".join(result)


def _process_inline(line: str) -> str:
    """Process inline markdown: bold, italic, code, links."""
    # First, extract inline code spans to protect them
    parts = []
    last_end = 0
    for m in re.finditer(r"`([^`]+)`", line):
        # Escape text before this code span
        before = line[last_end:m.start()]
        parts.append(_escape_and_format(before))
        # Code span — no escaping inside
        parts.append(f"`{m.group(1)}`")
        last_end = m.end()
    # Remaining text after last code span
    remaining = line[last_end:]
    parts.append(_escape_and_format(remaining))
    return "".join(parts)


def _escape_and_format(text: str) -> str:
    """Escape text and convert bold/italic/links."""
    if not text:
        return ""

    # Convert links [text](url) before escaping
    def replace_link(m):
        link_text = escape_md(m.group(1))
        url = m.group(2)  # URLs don't need full escaping
        # Only escape ) in URL
        url = url.replace(")", "\\)")
        return f"[{link_text}]({url})"

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_link, text)

    # Convert **bold** → *bold* (TG uses single * for bold)
    # But first we need to handle this before escaping
    bold_parts = re.split(r"\*\*(.+?)\*\*", text)
    result = []
    for i, part in enumerate(bold_parts):
        if i % 2 == 0:
            # Normal text — check for italic *text*
            italic_parts = re.split(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", part)
            for j, ipart in enumerate(italic_parts):
                if j % 2 == 0:
                    result.append(escape_md(ipart))
                else:
                    result.append(f"_{escape_md(ipart)}_")
        else:
            # Bold text
            result.append(f"*{escape_md(part)}*")

    return "".join(result)


def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a message into chunks that fit Telegram's limit.

    Tries to split on paragraph boundaries, then line boundaries.
    If a chunk boundary lands inside a fenced code block, the fence is
    re-balanced: the current chunk gets a closing ``` appended and the
    next chunk gets ```<lang> prepended (language tag preserved from
    the opening fence). Without this, a single split would emit
    chunk-A with an unterminated fence and chunk-B with naked source
    starting mid-line — Telegram renders the second one as plain text
    with stray backticks.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    # State: when a chunk leaves a fence open, the next chunk must
    # be opened with this fence header (``` plus language tag).
    pending_open_fence: str | None = None

    fence_re = re.compile(r"^```([^\n`]*)$")

    def fence_state(s: str) -> tuple[bool, str | None]:
        """Return (is_open_at_end, last_open_fence_header_or_None).

        Tracks `^```...$` toggles line-by-line. The header (``` plus
        language tag, no trailing newline) of the most recent
        unmatched opener is returned so the next chunk can re-open
        with the same language.
        """
        open_header: str | None = None
        is_open = False
        for line in s.split("\n"):
            m = fence_re.match(line.rstrip("\r"))
            if not m:
                continue
            if is_open:
                is_open = False
                open_header = None
            else:
                is_open = True
                lang = m.group(1).strip()
                open_header = f"```{lang}" if lang else "```"
        return is_open, open_header

    while remaining:
        prefix = pending_open_fence + "\n" if pending_open_fence else ""
        budget = max_len - len(prefix)
        # Reserve room for a possible closing fence (\n```), so a
        # forced split inside a fence still fits under max_len.
        budget_with_close = budget - 4

        if len(remaining) <= budget:
            chunks.append(prefix + remaining)
            break

        # Try paragraph, then line boundary, then hard split.
        split_at = remaining.rfind("\n\n", 0, budget_with_close)
        if split_at == -1 or split_at < budget_with_close // 2:
            split_at = remaining.rfind("\n", 0, budget_with_close)
        if split_at == -1 or split_at < budget_with_close // 2:
            split_at = budget_with_close

        head = remaining[:split_at]
        tail = remaining[split_at:].lstrip("\n")

        is_open, open_header = fence_state(prefix + head)
        if is_open:
            chunk = prefix + head.rstrip("\n") + "\n```"
            pending_open_fence = open_header or "```"
        else:
            chunk = prefix + head
            pending_open_fence = None

        chunks.append(chunk)
        remaining = tail

    return chunks


# ── Tool call formatting ─────────────────────────────────────────────────────

_TOOL_ICONS = {
    "terminal": "⚙️",
    "read_file": "📖",
    "write_file": "📝",
    "edit_file": "✏️",
    "append_file": "📎",
    "search_files": "🔎",
    "list_dir": "📂",
    "fetch_url": "🌐",
    "web_search": "🔍",
    "browser": "🌍",
    "memory": "🧠",
    "skills": "📚",
    "cogit": "💾",
    "delegate_task": "⚔️",
}


def format_tool_call(tool_name: str, arguments: dict[str, Any]) -> str:
    """Format a tool call as a compact status line."""
    icon = _TOOL_ICONS.get(tool_name, "🔧")
    subtitle = _tool_subtitle(tool_name, arguments)
    return f"{icon} `{tool_name}` {escape_md(subtitle)}"


def format_tool_result(tool_name: str, result: str, error: bool) -> str:
    """Format a tool result as a compact status line."""
    _TOOL_ICONS.get(tool_name, "🔧")
    status = "❌" if error else "✅"
    # Compact preview of result
    preview = result.strip().split("\n")[0][:80]
    if len(result.strip()) > 80:
        preview += "…"
    return f"{status} `{tool_name}` — {escape_md(preview)}"


def format_thinking(text: str) -> str:
    """Format thinking/reasoning as a spoiler block."""
    # Truncate long thinking
    if len(text) > 800:
        text = text[:800] + "…"
    escaped = escape_md(text.strip())
    return f"💭 ||{escaped}||"


def format_session_divider(title: str = "NEW SESSION") -> str:
    """Format a session divider."""
    return f"═══════════════════\n✦ *{escape_md(title)}*\n═══════════════════"


def _tool_subtitle(tool_name: str, args: dict) -> str:
    """Generate a compact subtitle for a tool call."""
    if tool_name == "terminal":
        cmd = args.get("command", "")
        return cmd[:60] + "…" if len(cmd) > 60 else cmd
    elif tool_name == "web_search":
        return f'"{args.get("query", "")}"'
    elif tool_name == "browser":
        action = args.get("action", "")
        url = args.get("url", "")
        if action == "open" and url:
            return f"open {url[:50]}"
        return action
    elif tool_name in ("read_file", "write_file", "edit_file", "append_file"):
        return args.get("path", "")
    elif tool_name == "search_files":
        return f'"{args.get("pattern", "")}"'
    elif tool_name == "fetch_url":
        return args.get("url", "")[:50]
    elif tool_name == "memory":
        return f'{args.get("action", "")} [{args.get("target", "memory")}]'
    elif tool_name == "skills":
        action = args.get("action", "")
        name = args.get("name", "")
        return f"{action}: {name}" if name else action
    elif tool_name == "cogit":
        action = args.get("action", "")
        label = args.get("label", "")
        return f"{action}: {label}" if label else action
    elif tool_name == "delegate_task":
        mode = args.get("mode", "workers")
        return mode
    return ""
