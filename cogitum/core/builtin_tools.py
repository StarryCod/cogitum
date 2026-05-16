"""
cogitum.core.builtin_tools
~~~~~~~~~~~~~~~~~~~~~~~~~~
Built-in tools registered into the global REGISTRY.
Import this module once at startup to activate them.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from cogitum.core.tools import tool

# ---------------------------------------------------------------------------
# Security: path sandbox
# ---------------------------------------------------------------------------

# Sensitive paths that should NEVER be read/written by the LLM
_SENSITIVE_PATHS = {
    "/etc/shadow", "/etc/passwd", "/etc/sudoers",
    ".ssh/authorized_keys", ".ssh/id_rsa", ".ssh/id_ed25519",
    ".gnupg", ".aws/credentials", ".config/gcloud",
}

_SENSITIVE_PREFIXES = (
    "/proc/", "/sys/", "/dev/",
)

# Dangerous shell patterns that trigger auto-save
_DANGEROUS_COMMANDS = (
    "rm ", "rm -", "rmdir", "git reset", "git checkout --",
    "git clean", "git push -f", "git push --force",
    "drop table", "drop database", "truncate ",
    "dd if=", "mkfs", "fdisk",
)


def _is_dangerous_command(cmd: str) -> bool:
    """Check if a shell command is potentially destructive."""
    lower = cmd.lower().strip()
    return any(lower.startswith(d) or f" {d}" in lower or f"&&{d}" in lower
               for d in _DANGEROUS_COMMANDS)


def _tool_subtitle_for_approval(tool_name: str, args: dict) -> str:
    """Generate a human-readable description for approval prompt."""
    if tool_name == "terminal":
        cmd = args.get("command", "")
        mode = args.get("mode", "normal")
        if mode == "background":
            return f"[background] {cmd[:100]}"
        return cmd[:120]
    elif tool_name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        return f"Write {len(content)} chars → {path}"
    elif tool_name == "edit_file":
        return f"Edit {args.get('path', '')}"
    elif tool_name == "cogit":
        return f"{args.get('action', '')} {args.get('label', '')}"
    elif tool_name == "delegate_task":
        return f"mode={args.get('mode', '')}"
    return str(args)[:100]


# Medium-risk patterns (not destructive but worth noting)
_MEDIUM_COMMANDS = (
    "pip install", "pip uninstall", "npm install", "npm uninstall",
    "apt install", "apt remove", "pacman -S", "pacman -R",
    "systemctl", "chmod", "chown", "curl -X POST", "curl -X PUT",
    "curl -X DELETE", "git push", "git merge", "git rebase",
    "docker rm", "docker stop", "kill ", "pkill ",
)


def classify_danger(tool_name: str, arguments: dict) -> str:
    """Classify tool call danger level: 'low', 'medium', or 'danger'.

    Returns the level as a string.
    """
    # Terminal commands need deeper analysis
    if tool_name == "terminal":
        cmd = arguments.get("command", "")
        if _is_dangerous_command(cmd):
            return "danger"
        lower = cmd.lower().strip()
        if any(lower.startswith(m) or f" {m}" in lower for m in _MEDIUM_COMMANDS):
            return "medium"
        # Background mode is medium (long-running)
        if arguments.get("mode") == "background" and cmd not in ("list", "read", "kill", "write"):
            return "medium"
        return "low"

    # Write operations
    if tool_name == "write_file":
        path = arguments.get("path", "")
        # Overwriting config files is medium
        if any(x in path for x in (".env", "config", ".toml", ".yaml", ".yml")):
            return "medium"
        return "low"

    if tool_name == "edit_file":
        return "low"

    # Cogit restore is medium (changes files)
    if tool_name == "cogit" and arguments.get("action") == "restore":
        return "medium"

    # Browser actions are low
    if tool_name == "browser":
        return "low"

    # Everything else is low
    return "low"


def _auto_cogit_save(label: str, scope_path: str | None = None) -> str | None:
    """Auto-save a cogit checkpoint before dangerous operations.
    
    scope_path: if provided, checkpoint only that file/dir (much faster than whole project).
                If file is outside project_dir, skip checkpoint entirely (not our concern).
    Returns None on success, error string on failure.
    """
    try:
        from cogitum.core.cogit import CogitStore
        session_id = os.environ.get("COGITUM_SESSION_ID", "default")
        project_dir = os.environ.get("COGITUM_PROJECT_DIR", os.getcwd())
        # Determine scope
        scope = None
        if scope_path:
            try:
                from pathlib import Path as _P
                p = _P(scope_path).expanduser().resolve()
                pd = _P(project_dir).resolve()
                # If file is outside project_dir, skip checkpoint entirely
                rel = p.relative_to(pd)
                scope = str(rel)
            except (ValueError, OSError):
                # File outside project — don't checkpoint random files
                return None
        store = CogitStore(session_id=session_id, project_dir=project_dir)
        cp = store.save(label=f"auto: {label}", scope=scope)
        return None
    except Exception:
        return None  # don't block the operation on checkpoint failure


def _is_path_safe(p: Path) -> tuple[bool, str]:
    """Check if a path is safe to access. Returns (safe, reason)."""
    resolved = str(p.resolve())

    # Block /proc, /sys, /dev
    for prefix in _SENSITIVE_PREFIXES:
        if resolved.startswith(prefix):
            return False, f"access denied: {prefix} is restricted"

    # Block known sensitive files
    for sensitive in _SENSITIVE_PATHS:
        if resolved.endswith(sensitive) or f"/{sensitive}" in resolved:
            return False, f"access denied: sensitive file"

    return True, ""


def _is_url_safe(url: str) -> tuple[bool, str]:
    """Check if a URL is safe to fetch (no SSRF)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "invalid URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} not allowed (http/https only)"

    hostname = parsed.hostname or ""

    # Block localhost
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, "localhost access denied"

    # Block private/link-local IPs
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_link_local or ip.is_loopback:
            return False, f"private/internal IP {hostname} denied"
    except ValueError:
        pass  # hostname is a domain, not IP — ok

    # Block cloud metadata endpoints
    if hostname in ("169.254.169.254", "metadata.google.internal"):
        return False, "cloud metadata endpoint denied"

    return True, ""

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
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
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
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    # Auto-save checkpoint if file already exists (overwrite = destructive)
    if p.exists():
        _auto_cogit_save(f"before write_file {path}", scope_path=str(p))
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
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(content)
    return f"OK: appended {len(content)} bytes to {path}"


@tool(tags=["fs", "write"])
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Targeted find-and-replace in a file. Errors if old_string is not found or matches multiple locations.

    path: Absolute or relative path to the file.
    old_string: Exact text to find (must match exactly once in the file).
    new_string: Replacement text.
    """
    p = Path(path).expanduser()
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    if not p.exists():
        return f"ERROR: file not found: {path}"
    # Auto-save checkpoint before editing
    _auto_cogit_save(f"before edit_file {path}", scope_path=str(p))
    content = p.read_text(errors="replace")
    count = content.count(old_string)
    if count == 0:
        return "ERROR: old_string not found in file"
    if count > 1:
        return f"ERROR: old_string matches {count} locations (must be unique)"
    # Find line number of the match for context
    idx = content.index(old_string)
    line_num = content[:idx].count("\n") + 1
    new_content = content.replace(old_string, new_string, 1)
    p.write_text(new_content)
    # Show context around the replacement
    lines = new_content.splitlines()
    new_line_count = new_string.count("\n") + 1
    start = max(0, line_num - 2)
    end = min(len(lines), line_num + new_line_count + 1)
    context = "\n".join(f"{start + i + 1}|{lines[start + i]}" for i in range(end - start))
    return f"OK: replaced at line {line_num}\n{context}"


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
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    if not p.exists():
        return f"ERROR: path not found: {path}"
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
    lines = []
    for e in entries:
        kind = "F" if e.is_file() else "D"
        try:
            size = f"{e.stat().st_size:>10}" if e.is_file() else "          "
        except (OSError, PermissionError):
            size = "         ?"
        lines.append(f"[{kind}] {size}  {e.name}")
    return "\n".join(lines) or "(empty)"


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

@tool(tags=["shell"])
async def terminal(
    command: str,
    workdir: Optional[str] = None,
    mode: str = "normal",
    timeout: int = 120,
    pid: int = 0,
    stdin_data: str = "",
    last_n: int = 50,
) -> str:
    """Run shell commands in three modes: normal, timeout, background.

    PARAMETERS
      command:     Shell command to run, OR a background action verb
                   ('list' / 'read' / 'kill' / 'write' / 'close') when
                   mode='background' and you're managing an existing process.
      workdir:     Working directory (defaults to current).
      mode:        'normal' | 'timeout' | 'background'.  Default 'normal'.
      timeout:     Hard time-limit in seconds for mode='timeout' (default 120).
                   Ignored otherwise.
      pid:         PID of an existing background process for read/kill/write/close.
      stdin_data:  Text to send to a background process's stdin via 'write'.
      last_n:      Tail size when reading background output (default 50).

    MODES

      normal     Run synchronously, no timeout. Returns full stdout+stderr
                 once the command exits. Best for short interactive things
                 (ls, cat, git status). Output is capped at 50KB.

      timeout    Same as normal, but the command is killed if it exceeds
                 `timeout` seconds. On kill returns the message
                 "TIMEOUT: command killed after Ns. Last output: ...". Use
                 this when you want a hard guarantee the call won't hang.

      background Spawn the command and return its PID immediately. The agent
                 keeps working while the process runs. Then issue follow-ups:
                   command='list',  mode='background'             → all PIDs
                   command='read',  mode='background', pid=N      → tail output
                   command='write', mode='background', pid=N,
                                   stdin_data='answer'            → send to stdin (\\n appended)
                   command='close', mode='background', pid=N      → close stdin (EOF)
                   command='kill',  mode='background', pid=N      → terminate
                 Use background for servers, watchers, long builds, anything
                 that needs interactive stdin, or work you want to overlap
                 with other tool calls.
    """
    from cogitum.core.process_manager import ProcessManager

    # Auto-save checkpoint before dangerous commands
    if _is_dangerous_command(command) and command not in ("list", "read", "kill", "write", "close"):
        _auto_cogit_save(f"before terminal: {command[:50]}")

    cwd = workdir or os.getcwd()
    pm = ProcessManager.get()

    # ── Background mode: management actions ──
    if mode == "background":
        if command == "list":
            pm.cleanup_finished_older_than(seconds=300)  # housekeeping
            procs = pm.list_processes()
            if not procs:
                return "No background processes running."
            lines = ["Background processes:"]
            for bp in procs:
                cmd_short = bp.command[:60] + ("…" if len(bp.command) > 60 else "")
                lines.append(f"  PID {bp.pid} | {bp.status} | {bp.uptime:.0f}s | {cmd_short}")
            return "\n".join(lines)

        elif command == "read":
            if not pid:
                return "ERROR: pid required for 'read' action"
            return pm.read_output(pid, last_n=last_n)

        elif command == "kill":
            if not pid:
                return "ERROR: pid required for 'kill' action"
            return await pm.kill(pid)

        elif command == "write":
            if not pid:
                return "ERROR: pid required for 'write' action"
            if not stdin_data:
                return "ERROR: stdin_data required for 'write' action"
            return await pm.write_stdin(pid, stdin_data)

        elif command == "close":
            if not pid:
                return "ERROR: pid required for 'close' action"
            return await pm.close_stdin(pid)

        else:
            # Start a new background process
            bp = await pm.spawn(command, workdir=cwd)
            await asyncio.sleep(0.3)  # brief wait to catch immediate failures
            if bp.finished:
                output = "\n".join(bp.output_lines[-20:])
                return (
                    f"Process exited immediately (exit {bp.exit_code}):\n{output}"
                )
            return (
                f"OK: started background process PID {bp.pid}\n"
                f"Use terminal(command='read', mode='background', pid={bp.pid}) to check output, "
                f"'write' to send stdin, 'kill' to stop."
            )

    # ── Normal mode: no timeout ──
    if mode == "normal":
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode(errors="replace").strip()
            # Cap output at 50KB
            if len(output) > 50000:
                output = output[:50000] + "\n… (truncated, 50KB limit)"
            rc = proc.returncode
            if rc != 0:
                return f"[exit {rc}]\n{output}"
            return output or "(no output)"
        except Exception as e:
            return f"ERROR: {e}"

    # ── Timeout mode: kill if exceeds limit ──
    if mode == "timeout":
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
                # Capture any partial output before killing
                partial = b""
                try:
                    if proc.stdout:
                        partial = await asyncio.wait_for(proc.stdout.read(8192), timeout=0.5)
                except Exception:
                    pass
                proc.kill()
                await proc.wait()
                tail = partial.decode(errors="replace").strip()[-2000:]
                return (
                    f"TIMEOUT: command killed after {timeout}s.\n"
                    f"Last output:\n{tail or '(none captured)'}\n"
                    f"Hint: switch to mode='background' if the command is long-running."
                )
            output = stdout.decode(errors="replace").strip()
            if len(output) > 50000:
                output = output[:50000] + "\n… (truncated, 50KB limit)"
            rc = proc.returncode
            if rc != 0:
                return f"[exit {rc}]\n{output}"
            return output or "(no output)"
        except Exception as e:
            return f"ERROR: {e}"

    return f"ERROR: unknown mode '{mode}' (use 'normal', 'timeout', or 'background')"


# ---------------------------------------------------------------------------
# Web / fetch
# ---------------------------------------------------------------------------

@tool(tags=["web"])
async def fetch_url(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return its text content (HTML stripped).

    url: URL to fetch.
    max_chars: Maximum characters to return.
    """
    safe, reason = _is_url_safe(url)
    if not safe:
        return f"ERROR: {reason}"
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


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@tool(tags=["memory"])
def memory(action: str, target: str = "memory", content: str = "", old_text: str = "") -> str:
    """Persistent memory that survives across sessions.

    action: 'add', 'replace', or 'remove'.
    target: 'memory' (agent notes) or 'user' (user profile).
    content: The entry text (required for add/replace).
    old_text: Substring identifying the entry to replace/remove.
    """
    from cogitum.core.memory import memory_add, memory_replace, memory_remove

    if action == "add":
        if not content:
            return "ERROR: content required for add"
        return memory_add(target, content)
    elif action == "replace":
        if not old_text or not content:
            return "ERROR: old_text and content required for replace"
        return memory_replace(target, old_text, content)
    elif action == "remove":
        if not old_text:
            return "ERROR: old_text required for remove"
        return memory_remove(target, old_text)
    else:
        return f"ERROR: unknown action '{action}' (use add/replace/remove)"


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@tool(tags=["skills"])
def skills(action: str, name: str = "", content: str = "", category: str = "") -> str:
    """Agent's procedural memory — reusable knowledge for recurring tasks.

    action: 'list', 'read', 'write', or 'delete'.
    name: Skill name (required for read/write/delete).
    content: Full skill markdown (required for write).
    category: Filter by category (for list) or assign category (for write).
    """
    from cogitum.core.skills import list_skills, read_skill, write_skill, delete_skill, list_categories

    if action == "list":
        items = list_skills(category=category)
        if not items:
            if category:
                cats = list_categories()
                return f"No skills in category '{category}'. Available categories: {', '.join(cats)}"
            return "No skills yet. Use skills(action='write', name='...', content='...') to create one."
        # Group by category
        by_cat: dict[str, list] = {}
        for s in items:
            by_cat.setdefault(s.category, []).append(s)
        lines = [f"Available skills ({len(items)}):"]
        for cat in sorted(by_cat):
            lines.append(f"\n  [{cat}]")
            for s in by_cat[cat]:
                desc = s.description[:60] + "…" if len(s.description) > 60 else s.description
                lines.append(f"    • {s.name}: {desc}")
        return "\n".join(lines)
    elif action == "read":
        if not name:
            return "ERROR: name required for read"
        text = read_skill(name)
        if text is None:
            return f"ERROR: skill '{name}' not found. Use skills(action='list') to see available."
        return text
    elif action == "write":
        if not name or not content:
            return "ERROR: name and content required for write"
        return write_skill(name, content, category=category or "custom")
    elif action == "delete":
        if not name:
            return "ERROR: name required for delete"
        return delete_skill(name)
    else:
        return f"ERROR: unknown action '{action}' (use list/read/write/delete)"


# ---------------------------------------------------------------------------
# Session Search (cross-session awareness)
# ---------------------------------------------------------------------------

@tool(tags=["sessions"])
def session_search(action: str, query: str = "", session_id: str = "", limit: int = 10, offset: int = 0) -> str:
    """Search and browse past conversation sessions.

    action: 'list', 'read', or 'search'.
    query: Search query for 'search' action (matches session titles).
    session_id: Session ID for 'read' action.
    limit: Max results for list/search, or max messages for read (default 10).
    offset: Skip first N results/messages (for pagination).

    Use this to recall past conversations, find context from previous sessions,
    or check what was discussed before.
    """
    from cogitum.core.sessions import get_store
    from datetime import datetime

    store = get_store()

    if action == "list":
        sessions = store.list_sessions(limit=limit)
        if not sessions:
            return "No past sessions found."
        lines = [f"Past sessions ({len(sessions)}):"]
        for s in sessions:
            ts = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
            title = s.title or "(untitled)"
            lines.append(f"  • [{ts}] {title} ({s.count} msgs) — id:{s.id[:12]}")
        return "\n".join(lines)

    elif action == "search":
        if not query:
            return "ERROR: query required for search"
        results = store.search(query, limit=limit)
        if not results:
            return f"No sessions matching: {query}"
        lines = [f"Sessions matching '{query}':"]
        for s in results:
            ts = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  • [{ts}] {s.title} ({s.count} msgs) — id:{s.id[:12]}")
        return "\n".join(lines)

    elif action == "read":
        if not session_id:
            return "ERROR: session_id required for read"
        # Support partial ID match
        full_id = session_id
        if len(session_id) < 20:
            all_sessions = store.list_sessions(limit=200)
            matches = [s for s in all_sessions if s.id.startswith(session_id)]
            if not matches:
                return f"ERROR: no session found with id starting with '{session_id}'"
            if len(matches) > 1:
                return f"ERROR: ambiguous id '{session_id}' — matches {len(matches)} sessions"
            full_id = matches[0].id

        messages = store.load_session(full_id)
        if not messages:
            return f"Session {session_id} is empty."

        # Apply offset and limit
        subset = messages[offset:offset + limit]
        lines = [f"Session messages ({len(messages)} total, showing {offset+1}–{offset+len(subset)}):"]
        for msg in subset:
            role = msg.role.upper()
            # Extract text content
            text_parts = []
            for p in msg.parts:
                if hasattr(p, "text") and p.text:
                    text_parts.append(p.text[:200])
                elif hasattr(p, "name"):
                    text_parts.append(f"[tool_call: {p.name}]")
                elif hasattr(p, "content") and hasattr(p, "tool_call_id"):
                    preview = p.content[:100] if p.content else ""
                    text_parts.append(f"[result: {preview}]")
            content = " ".join(text_parts)[:300]
            ts = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M") if msg.timestamp else ""
            lines.append(f"  [{ts}] {role}: {content}")
        return "\n".join(lines)

    else:
        return f"ERROR: unknown action '{action}' (use list/read/search)"


# ---------------------------------------------------------------------------
# Cogit (checkpoints)
# ---------------------------------------------------------------------------

@tool(tags=["cogit"])
def cogit(action: str, label: str = "", index: int = 0, scope: str = "") -> str:
    """Smart checkpoints — save/restore project state.

    action: 'save', 'list', 'restore', 'diff', or 'cleanup'.
    label: Description for save (e.g. 'before refactor auth').
    index: Checkpoint number for restore/diff.
    scope: Directory or file to checkpoint (relative path).
           Examples: 'src/', 'cogitum/core/', 'main.py'.
           Empty = entire project (with smart filtering).

    Use scope to checkpoint only the relevant part of the project.
    This keeps checkpoints fast and small.
    """
    from cogitum.core.cogit import CogitStore
    import os

    # Get session_id and project_dir from app context
    session_id = os.environ.get("COGITUM_SESSION_ID", "default")
    project_dir = os.environ.get("COGITUM_PROJECT_DIR", os.getcwd())

    store = CogitStore(session_id=session_id, project_dir=project_dir)

    if action == "save":
        cp = store.save(label=label, scope=scope or None)
        scope_info = f" [scope: {cp.scope}]" if cp.scope != "." else ""
        return f"OK: checkpoint #{cp.index} '{cp.label}' saved ({cp.file_count} files){scope_info}"
    elif action == "list":
        checkpoints = store.list_checkpoints()
        if not checkpoints:
            return "No checkpoints yet. Use cogit(action='save', label='...') to create one."
        lines = []
        for cp in checkpoints:
            from datetime import datetime
            ts = datetime.fromtimestamp(cp.timestamp).strftime("%H:%M")
            scope_info = f" [{cp.scope}]" if cp.scope != "." else ""
            lines.append(f"  #{cp.index} [{ts}] {cp.label} ({cp.file_count} files){scope_info}")
        return f"Checkpoints ({len(checkpoints)}):\n" + "\n".join(lines)
    elif action == "restore":
        if index <= 0:
            return "ERROR: index required (positive integer)"
        return store.restore(index)
    elif action == "diff":
        if index <= 0:
            return "ERROR: index required for diff"
        return store.diff(index)
    elif action == "cleanup":
        removed = store.cleanup(keep_last=10)
        return f"OK: removed {removed} old checkpoints" if removed else "Nothing to clean up."
    else:
        return f"ERROR: unknown action '{action}' (use save/list/restore/diff/cleanup)"


# ---------------------------------------------------------------------------
# Delegate Task
# ---------------------------------------------------------------------------

@tool(tags=["delegate"])
def delegate_task(
    mode: str,
    tasks: str = "",
    content: str = "",
    experts: str = "",
    model: str = "",
) -> str:
    """Spawn parallel sub-agents for complex work.

    mode: 'workers' or 'experts'.

    Workers mode — parallel agents doing independent tasks:
      tasks: JSON array of [{id, goal, context?}]. Up to 10 parallel.

    Experts mode — review board analyzing content:
      content: Code/plan/architecture to review.
      experts: Comma-separated expert names (security,scale,optimization,ux,ui,frontend).
               Empty = all experts.

    model: Optional model override for sub-agents.
    """
    import json as _json

    # --- Depth-limited recursive delegation ---
    from .delegate import MAX_DELEGATE_DEPTH

    current_depth = int(os.environ.get("COGITUM_DELEGATE_DEPTH", "0"))
    if current_depth >= MAX_DELEGATE_DEPTH:
        return (
            f"ERROR: delegation depth limit reached ({current_depth}/{MAX_DELEGATE_DEPTH}). "
            "Sub-agents cannot delegate further. Complete the task directly."
        )

    # Increment depth for child agents
    os.environ["COGITUM_DELEGATE_DEPTH"] = str(current_depth + 1)

    try:
        if mode == "workers":
            if not tasks:
                return "ERROR: tasks required (JSON array of [{id, goal, context?}])"
            try:
                task_list = _json.loads(tasks)
            except _json.JSONDecodeError as e:
                return f"ERROR: invalid JSON in tasks: {e}"

            if not isinstance(task_list, list) or len(task_list) == 0:
                return "ERROR: tasks must be a non-empty JSON array"
            if len(task_list) > 10:
                return "ERROR: max 10 parallel workers"

            # Store for async execution by agent loop
            return f"DELEGATE_WORKERS:{_json.dumps(task_list)}"

        elif mode == "experts":
            if not content:
                return "ERROR: content required for expert review"
            expert_list = [e.strip() for e in experts.split(",") if e.strip()] if experts else []
            payload = {"content": content, "experts": expert_list, "model": model}
            return f"DELEGATE_EXPERTS:{_json.dumps(payload)}"

        else:
            return f"ERROR: unknown mode '{mode}' (use 'workers' or 'experts')"
    finally:
        # Restore depth after delegation completes (for the current process)
        os.environ["COGITUM_DELEGATE_DEPTH"] = str(current_depth)


# ---------------------------------------------------------------------------
# Web Search (DuckDuckGo — no API key needed)
# ---------------------------------------------------------------------------

@tool(tags=["web", "search"])
async def web_search(query: str, max_results: int = 8) -> str:
    """Search the web using DuckDuckGo and return results.

    query: Search query string.
    max_results: Maximum number of results to return (default 8).
    """
    import httpx
    import re as _re
    from html.parser import HTMLParser

    class _DDGParser(HTMLParser):
        """Parse DuckDuckGo HTML search results."""
        def __init__(self):
            super().__init__()
            self.results: list[dict[str, str]] = []
            self._in_result = False
            self._in_title = False
            self._in_snippet = False
            self._current: dict[str, str] = {}
            self._buf = ""

        def handle_starttag(self, tag, attrs):
            attrs_d = dict(attrs)
            cls = attrs_d.get("class", "")
            # Result link
            if tag == "a" and "result__a" in cls:
                self._in_title = True
                self._current["url"] = attrs_d.get("href", "")
                self._buf = ""
            # Snippet
            if tag == "a" and "result__snippet" in cls:
                self._in_snippet = True
                self._buf = ""

        def handle_endtag(self, tag):
            if tag == "a" and self._in_title:
                self._in_title = False
                self._current["title"] = self._buf.strip()
            if tag == "a" and self._in_snippet:
                self._in_snippet = False
                self._current["snippet"] = self._buf.strip()
                if self._current.get("title"):
                    self.results.append(self._current)
                self._current = {}

        def handle_data(self, data):
            if self._in_title or self._in_snippet:
                self._buf += data

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=headers,
            )
            resp.raise_for_status()

        parser = _DDGParser()
        parser.feed(resp.text)
        results = parser.results[:max_results]

        if not results:
            # Fallback: try lite version
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(
                    "https://lite.duckduckgo.com/lite/",
                    params={"q": query},
                    headers=headers,
                )
                resp.raise_for_status()
            # Parse lite results (simpler format)
            lines = []
            for line in resp.text.splitlines():
                stripped = line.strip()
                if 'class="result-link"' in stripped:
                    href_match = _re.search(r'href="([^"]+)"', stripped)
                    text_match = _re.search(r'>([^<]+)<', stripped)
                    if href_match and text_match:
                        lines.append({"title": text_match.group(1), "url": href_match.group(1), "snippet": ""})
            results = lines[:max_results]

        if not results:
            return f"No results found for: {query}"

        # Clean DDG redirect URLs
        from urllib.parse import urlparse, parse_qs, unquote
        def _clean_url(raw: str) -> str:
            if "duckduckgo.com/l/" in raw:
                parsed = urlparse(raw)
                qs = parse_qs(parsed.query)
                if "uddg" in qs:
                    return unquote(qs["uddg"][0])
            return raw

        out_lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            out_lines.append(f"{i}. {r['title']}")
            out_lines.append(f"   {_clean_url(r['url'])}")
            if r.get("snippet"):
                out_lines.append(f"   {r['snippet'][:150]}")
            out_lines.append("")
        return "\n".join(out_lines)

    except Exception as e:
        return f"ERROR: web search failed: {e}"


# ---------------------------------------------------------------------------
# Browser (Playwright — full page interaction)
# ---------------------------------------------------------------------------

@tool(tags=["web", "browser"])
async def browser(action: str, url: str = "", selector: str = "", text: str = "", screenshot: bool = False) -> str:
    """Control a headless browser for web interaction.

    Actions:
      open       url=…             — navigate to URL (waits for DOM)
      click      selector=…        — click element by CSS selector
      type       selector=…, text= — fill input/textarea
      text                          — extract visible body text (capped 8KB)
      extract    selector=…        — inner_text of one element (capped 8KB)
      links                         — list every <a> on the page (href + label)
      act        text=<JS>         — run arbitrary page.evaluate(JS); JSON result
      screenshot                    — save .png; returns absolute path
      scroll     text=down|up|N    — scroll one viewport or N px
      back / forward / reload       — history navigation
      title / url                   — current title / current url
      close                         — shut the browser, free resources

    All actions reuse a single page so cookies/login persist between calls.
    """
    import json as _json

    # Lazy-init browser state via module-level dict
    global _BROWSER_STATE
    if "_BROWSER_STATE" not in globals():
        _BROWSER_STATE = {"browser": None, "page": None}

    state = _BROWSER_STATE

    async def _ensure_browser():
        if state["browser"] is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                return "ERROR: playwright not installed. Run: pip install playwright && playwright install chromium"
            pw = await async_playwright().start()
            state["_pw"] = pw
            try:
                state["browser"] = await pw.chromium.launch(headless=True)
            except Exception as e:
                # Fallback: try the full chromium bundle directly if the
                # default headless-shell couldn't be located (common right
                # after `pip install playwright` without the small shell).
                import os as _os
                cache = _os.path.expanduser("~/.cache/ms-playwright")
                exe = None
                if _os.path.isdir(cache):
                    for entry in sorted(_os.listdir(cache), reverse=True):
                        if entry.startswith("chromium-"):
                            cand = _os.path.join(cache, entry, "chrome-linux64", "chrome")
                            if _os.path.exists(cand):
                                exe = cand
                                break
                if exe:
                    state["browser"] = await pw.chromium.launch(
                        headless=True, executable_path=exe,
                    )
                else:
                    await pw.stop()
                    state["_pw"] = None
                    return (
                        f"ERROR: chromium launch failed ({e}). "
                        f"Run: .venv/bin/playwright install chromium"
                    )
        if state["page"] is None:
            state["page"] = await state["browser"].new_page()
        return None

    if action == "close":
        if state.get("browser"):
            await state["browser"].close()
        if state.get("_pw"):
            await state["_pw"].stop()
        state["browser"] = None
        state["page"] = None
        state["_pw"] = None
        return "OK: browser closed"

    err = await _ensure_browser()
    if err:
        return err

    page = state["page"]

    try:
        if action == "open":
            if not url:
                return "ERROR: url required for 'open' action"
            # SSRF check
            safe, reason = _is_url_safe(url)
            if not safe:
                return f"ERROR: {reason}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            title = await page.title()
            result = f"OK: opened {url} — title: {title}"

        elif action == "click":
            if not selector:
                return "ERROR: selector required for 'click' action"
            await page.click(selector, timeout=10000)
            result = f"OK: clicked {selector}"

        elif action == "type":
            if not selector:
                return "ERROR: selector required for 'type' action"
            await page.fill(selector, text, timeout=10000)
            result = f"OK: typed into {selector}"

        elif action == "text":
            # Extract visible text from page
            content = await page.inner_text("body")
            # Truncate
            if len(content) > 8000:
                content = content[:8000] + "\n… (truncated)"
            result = content

        elif action == "extract":
            if not selector:
                return "ERROR: selector required for 'extract' action"
            content = await page.locator(selector).first.inner_text(timeout=10000)
            if len(content) > 8000:
                content = content[:8000] + "\n… (truncated)"
            result = content

        elif action == "links":
            links = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".slice(0, 200)"
                ".map(a => ({href: a.href, label: (a.innerText||'').trim().slice(0,80)}))"
            )
            if not links:
                return "(no links on page)"
            lines = [f"Links on {await page.url}:" if False else f"Links ({len(links)}):"]
            for i, l in enumerate(links, 1):
                lines.append(f"  {i:3}. {l['label'] or '(no text)'}")
                lines.append(f"        {l['href']}")
            result = "\n".join(lines[:1 + 200])

        elif action == "act":
            if not text:
                return "ERROR: text=<JS expression> required for 'act' action"
            try:
                value = await page.evaluate(text)
                # Best-effort serialization
                try:
                    payload = _json.dumps(value, default=str)[:6000]
                except Exception:
                    payload = str(value)[:6000]
                result = f"OK: act → {payload}"
            except Exception as e:
                return f"ERROR: act JS threw: {e}"

        elif action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=10000)
            result = f"OK: back → {await page.title()}"

        elif action == "forward":
            await page.go_forward(wait_until="domcontentloaded", timeout=10000)
            result = f"OK: forward → {await page.title()}"

        elif action == "reload":
            await page.reload(wait_until="domcontentloaded", timeout=15000)
            result = f"OK: reloaded → {await page.title()}"

        elif action == "title":
            result = await page.title()

        elif action == "url":
            result = page.url

        elif action == "screenshot":
            import tempfile
            path = tempfile.mktemp(suffix=".png", prefix="cogitum_browser_")
            await page.screenshot(path=path, full_page=False)
            result = f"OK: screenshot saved to {path}"

        elif action == "scroll":
            direction = text.lower() if text else "down"
            if direction == "down":
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
            elif direction == "up":
                await page.evaluate("window.scrollBy(0, -window.innerHeight)")
            else:
                await page.evaluate(f"window.scrollBy(0, {int(direction)})")
            result = f"OK: scrolled {direction}"

        else:
            return f"ERROR: unknown action '{action}' (use open/click/type/text/screenshot/scroll/close)"

        # Optional screenshot after action
        if screenshot and action != "screenshot":
            import tempfile
            path = tempfile.mktemp(suffix=".png", prefix="cogitum_browser_")
            await page.screenshot(path=path, full_page=False)
            result += f"\nScreenshot: {path}"

        return result

    except Exception as e:
        return f"ERROR: browser action '{action}' failed: {e}"


# ---------------------------------------------------------------------------
# Telegram media (available only when running via TG gateway)
# ---------------------------------------------------------------------------

# Global reference set by TG gateway before agent runs
_tg_api = None
_tg_chat_id: int | None = None


def _set_tg_context(api, chat_id: int) -> None:
    """Called by TG gateway to inject API reference for send_media tool."""
    global _tg_api, _tg_chat_id
    _tg_api = api
    _tg_chat_id = chat_id


def _clear_tg_context() -> None:
    """Called by TG gateway after agent finishes."""
    global _tg_api, _tg_chat_id
    _tg_api = None
    _tg_chat_id = None


@tool(tags=["media", "telegram"])
async def send_media(path: str, caption: str = "", media_type: str = "auto") -> str:
    """Send a file (photo, document, audio) to the user in Telegram chat.

    Use this when you want to share an image, screenshot, generated file,
    or any document with the user.

    path: Absolute path to the file to send.
    caption: Optional caption text for the media.
    media_type: 'photo', 'document', or 'auto' (detect from extension).
    """
    global _tg_api, _tg_chat_id

    if _tg_api is None or _tg_chat_id is None:
        return "ERROR: send_media is only available when running via Telegram gateway."

    from pathlib import Path as P
    file_path = P(path).expanduser().resolve()

    if not file_path.exists():
        return f"ERROR: file not found: {path}"

    # Determine media type
    ext = file_path.suffix.lower()
    if media_type == "auto":
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            media_type = "photo"
        else:
            media_type = "document"

    try:
        if media_type == "photo":
            resp = await _tg_api.send_photo(_tg_chat_id, str(file_path), caption=caption)
        else:
            resp = await _tg_api.send_document(_tg_chat_id, str(file_path), caption=caption)

        if resp.get("ok"):
            return f"Sent {media_type}: {file_path.name}" + (f" with caption: {caption}" if caption else "")
        else:
            return f"ERROR: Telegram API: {resp.get('description', 'unknown error')}"
    except Exception as e:
        return f"ERROR: send_media failed: {e}"
