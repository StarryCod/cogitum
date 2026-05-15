"""
cogitum.core.builtin_tools
~~~~~~~~~~~~~~~~~~~~~~~~~~
Built-in tools registered into the global REGISTRY.
Import this module once at startup to activate them.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

from cogitum.core.tools import tool

# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

@tool(tags=["fs", "read"])
def read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a text file and return its contents with line numbers.

    path: Absolute or relative path to the file.
    offset: First line to return (1-indexed).
    limit: Maximum number of lines to return.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"ERROR: file not found: {path}"
    lines = p.read_text(errors="replace").splitlines()
    total = len(lines)
    chunk = lines[offset - 1 : offset - 1 + limit]
    numbered = "\n".join(f"{offset + i}|{line}" for i, line in enumerate(chunk))
    return f"[{total} lines total, showing {offset}–{offset + len(chunk) - 1}]\n{numbered}"


@tool(tags=["fs", "write"])
def write_file(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed.

    path: Absolute or relative path to the file.
    content: Full text content to write.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"OK: wrote {len(content)} bytes to {path}"


@tool(tags=["fs", "write"])
def append_file(path: str, content: str) -> str:
    """Append content to a file (creates it if missing).

    path: Absolute or relative path to the file.
    content: Text to append.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(content)
    return f"OK: appended {len(content)} bytes to {path}"


@tool(tags=["fs", "search"])
def search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None) -> str:
    """Search file contents with ripgrep (regex).

    pattern: Regex pattern to search for.
    path: Directory or file to search in.
    file_glob: Optional glob to filter files, e.g. '*.py'.
    """
    cmd = ["rg", "--line-number", "--color=never", "-m", "5", pattern, path]
    if file_glob:
        cmd += ["-g", file_glob]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = result.stdout.strip()
        return out if out else "(no matches)"
    except FileNotFoundError:
        # fallback: grep
        cmd2 = ["grep", "-rn", "--include", file_glob or "*", pattern, path]
        result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=15)
        return result2.stdout.strip() or "(no matches)"
    except subprocess.TimeoutExpired:
        return "ERROR: search timed out"


@tool(tags=["fs"])
def list_dir(path: str = ".") -> str:
    """List files and directories at a path.

    path: Directory to list.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"ERROR: path not found: {path}"
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
    lines = []
    for e in entries:
        kind = "F" if e.is_file() else "D"
        size = f"{e.stat().st_size:>10}" if e.is_file() else "          "
        lines.append(f"[{kind}] {size}  {e.name}")
    return "\n".join(lines) or "(empty)"


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

@tool(tags=["shell"])
async def terminal(command: str, workdir: Optional[str] = None, timeout: int = 60) -> str:
    """Run a shell command and return combined stdout+stderr.

    command: Shell command to execute.
    workdir: Working directory (defaults to cwd).
    timeout: Max seconds to wait (default 60).
    """
    cwd = workdir or os.getcwd()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"ERROR: command timed out after {timeout}s"
        output = stdout.decode(errors="replace").strip()
        rc = proc.returncode
        if rc != 0:
            return f"[exit {rc}]\n{output}"
        return output or "(no output)"
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Web / fetch
# ---------------------------------------------------------------------------

@tool(tags=["web"])
async def fetch_url(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return its text content (HTML stripped).

    url: URL to fetch.
    max_chars: Maximum characters to return.
    """
    try:
        import httpx
        from html.parser import HTMLParser

        class _Stripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts: list[str] = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "head"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style", "head"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    stripped = data.strip()
                    if stripped:
                        self.parts.append(stripped)

        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers={"User-Agent": "Cogitum/1.0"})
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                parser = _Stripper()
                parser.feed(resp.text)
                text = "\n".join(parser.parts)
            else:
                text = resp.text
        return text[:max_chars]
    except Exception as e:
        return f"ERROR: {e}"
