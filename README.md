<div align="center">

# ⚔️ COGITUM

**Sovereign agentic CLI — forge, delegate, persist.**  
*Imperial Fists colorway. Built for engineers who think in terminals.*

[![Python](https://img.shields.io/badge/python-3.11%2B-FFD43B?logo=python&logoColor=black)](https://python.org)
[![Textual](https://img.shields.io/badge/TUI-Textual-8A2BE2)](https://textual.textualize.io)
[![License](https://img.shields.io/badge/license-MIT-A8732D)](LICENSE)
[![Status](https://img.shields.io/badge/status-Preview-F5C24A)](https://github.com/OpenReason/cogitum)

</div>

---

<p align="center">
  <img src="assets/main.png" alt="Cogitum Main Interface" width="90%">
</p>

> **Cogitum** is a terminal-native AI agent with a multi-provider LLM mesh, 15+ built-in tools, persistent sessions, memory, skills, checkpoints, and a Telegram gateway — all wrapped in a keyboard-driven TUI that stays out of your way.

---

## ✨ What makes it different

| Feature | Why it matters |
|---------|----------------|
| 🕸️ **Multi-Provider Mesh** | Load-balance across OpenAI, Anthropic, OpenRouter, Cerebras, Groq, CanopyWave, and any OpenAI-compatible endpoint. Automatic failover, key pooling, rate-limit tracking. |
| 🧰 **15+ Built-in Tools** | Terminal, browser (Playwright), web search, file read/write/edit, git-style search, memory, skills, checkpoints, delegation, MCP servers. |
| 🛡️ **Approval Layer** | Dangerous operations (`rm`, `git push`, package installs) pause for your approval. Per-tool risk levels. |
| 💾 **Persistent Sessions** | Every conversation is auto-saved as JSONL. Resume anytime with `/resume`. Search across sessions. |
| 🧠 **Memory & Skills** | Cross-session memory + **82+ built-in skills** (coding, research, MLOps, red-teaming, smart-home…) injected into every system prompt. |
| 📦 **Cogit Checkpoints** | One-command project snapshots before destructive edits. Restore, diff, garbage-collect. |
| 🎯 **Self-Learning Skills** | The agent observes your workflow and **writes its own skills** via the `skills` tool — refining its expertise for your specific projects over time. |
| 🤖 **Delegation Modes** | Spawn parallel worker agents or expert review boards (`security`, `scale`, `ux`, `frontend`…). |
| 📡 **Telegram Gateway** | Run the same agent as a personal Telegram bot with streaming, inline keyboards, and media support. |
| 🔌 **MCP Integration** | Connect external MCP servers (stdio / HTTP) — tools are auto-discovered and registered dynamically. |

---

## 📸 Interface Tour

### Main Chat
<p align="center">
  <img src="assets/main.png" alt="Main Chat" width="90%">
</p>

- **Feed** — streaming markdown bubbles, tool cards, thinking blocks, queued messages.
- **Inspector** — live token count, context window usage, model info.
- **Queue Bar** — pending messages while the agent works. Press `↑` on empty composer to edit the last queued item.
- **Composer** — slash commands (`/setup`, `/models`, `/resume`, `/clear`, `/quit`), history with `↑/↓`, paste folding for long content.

### Model Picker — `Ctrl+P` or `/models`
<p align="center">
  <img src="assets/model_wizard.png" alt="Model Picker" width="90%">
</p>

- Live search across all resolved models (ID, alias, provider, capabilities).
- Fuzzy scoring fallback via `rapidfuzz`.
- Toggle filters: `text`, `vision`, `reasoning`, `tools`, `caching`, `free`, context size.
- Detail pane with cost, key health, and capabilities.

### Session Picker — `/resume`
<p align="center">
  <img src="assets/resume_wizard.png" alt="Session Picker" width="90%">
</p>

- Search across saved sessions.
- Preview pane shows last 8 messages with role badges.
- `↑/↓` navigate, `Enter` resume, `Delete` remove.

### Setup Wizard — `Ctrl+S` or `/setup`
<p align="center">
  <img src="assets/wizard_setup.png" alt="Setup Wizard" width="90%">
</p>

- Add providers from presets or custom config.
- OAuth / PKCE for Claude Pro/Max and ChatGPT Plus/Pro.
- Four secret backends: `env`, `vault` (AES-256-GCM), `keyring`, `plain`.
- Live connectivity testing before you save.

---

## 🚀 Quick Start

### Linux / macOS — One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/StarryCod/cogitum/master/scripts/install.sh | bash
```

Or via **npm** (Node.js wrapper, installs Python backend automatically):

```bash
npm install -g cogitum
```

### Windows — Manual install (PowerShell)

```powershell
# 1. Ensure Python 3.11+ is installed
python --version

# 2. Clone and install
git clone https://github.com/StarryCod/cogitum.git $env:LOCALAPPDATA\cogitum
cd $env:LOCALAPPDATA\cogitum
python -m venv .venv
.venv\Scripts\pip install -e ".[all]"

# 3. Add to PATH
# Add $env:LOCALAPPDATA\cogitum\.venv\Scripts to your PATH

# 4. Run
cog setup   # first-time wizard
cog         # launch TUI
```

### From source

```bash
git clone https://github.com/StarryCod/cogitum.git
cd cogitum
pip install -e ".[all]"
cog setup
cog
```

> **Requirements:** Python 3.11+, Git. Optional: Node.js 16+ for npm install.

### Key Bindings (TUI)

| Key | Action |
|-----|--------|
| `Enter` | Send message |
| `Shift+Enter` / `Ctrl+Enter` | New line in composer |
| `↑ / ↓` | Browse message history (when cursor at edge) |
| `/` | Open slash-command autocomplete |
| `Esc` | Cancel running agent |
| `Ctrl+P` | Open model picker |
| `Ctrl+S` | Open setup wizard |
| `Ctrl+C` | Copy selection (or "Use Ctrl+Q to quit" hint) |
| `Ctrl+Q` | Quit |

### Slash Commands

| Command | Description |
|---------|-------------|
| `/setup` | Provider & auth wizard |
| `/models` | Browse and switch models |
| `/model <id>` | Direct model switch |
| `/new` | Fresh session |
| `/resume` | Resume past session |
| `/title <name>` | Rename session |
| `/tools` | List available tools |
| `/mcp` | MCP server status |
| `/mcp reload` | Hot-reload MCP config |
| `/godmode <on/off/list>` | Toggle aggressive system prompts |
| `/clear` | Clear feed |
| `/quit` | Exit |

---

## 🧰 Tool Arsenal

### Filesystem
- `read_file` — paginated file reading with line numbers. Blocks sensitive paths.
- `write_file` — auto-cogit before overwrite.
- `edit_file` — exact find-and-replace with context preview.
- `append_file` — safe append.
- `search_files` — ripgrep fallback with timeout caps.
- `list_dir` — sorted directory listing.

### Shell
- `terminal` — three modes: **normal**, **timeout**, **background** (PID management, stdin/stdout streaming).
- Dangerous commands auto-save a checkpoint before execution.

### Web
- `fetch_url` — SSRF-protected HTTP fetch with HTML stripping.
- `web_search` — DuckDuckGo, no API key required.
- `browser` — Persistent Playwright session: `open`, `click`, `type`, `extract`, `screenshot`, `scroll`, `act` (JS eval).

### Memory & Knowledge
- `memory` — Persistent key-value notes (`user.md` + `memory.md`) injected into every system prompt.
- `skills` — Markdown skill library with YAML frontmatter, categories, fuzzy search.
- `session_search` — Search and read past conversation sessions.

---

## 🎓 Skill Library — 82+ Built-in Skills

Cogitum ships with **82+ pre-written skills** organized into categories. They are markdown files with YAML frontmatter, automatically injected into the system prompt so the agent knows how to handle specialized tasks:

| Category | Example Skills |
|----------|----------------|
| **github** | PR workflow, issue triage, release management, code review |
| **mlops** | Model deployment, training pipelines, monitoring, A/B testing |
| **data-science** | EDA, feature engineering, visualization, statistical testing |
| **creative** | Writing, storytelling, brainstorming, content strategy |
| **productivity** | Meeting notes, todo management, calendar automation |
| **red-teaming** | Adversarial testing, prompt injection checks, security audits |
| **research** | Literature review, hypothesis testing, citation management |
| **smart-home** | Device automation, scene scripting, energy optimization |
| **autonomous-ai-agents** | Agent design patterns, tool chaining, self-reflection |
| **custom** | Skills the agent **wrote itself** while working on your projects |

**Self-Learning:** The agent can create, update, and delete skills via the `skills` tool. Over time it builds a **personalized knowledge base** tailored to your codebase, workflow, and preferences. Skills persist across sessions in `~/.config/cogitum/skills/`.

---

### Advanced
- `cogit` — Content-addressable project checkpoints. Save, list, restore, diff, cleanup.
- `delegate_task` — Parallel sub-agents: **workers** (up to 10 tasks) or **experts** (security, scale, ux, frontend, optimization review boards).
- `send_media` — Telegram-only: send photos/documents from agent results.
- `mcp_*` — Dynamically registered tools from connected MCP servers.

---

## 🕸️ Multi-Provider Mesh

Cogitum does not lock you into one API. It builds a **Mesh** from `~/.config/cogitum/providers.toml`:

```toml
[providers.canopywave]
name = "CanopyWave"
format = "openai_compat"
base_url = "https://api.canopywave.ai/v1"
auth = "bearer"
enabled = true

[providers.canopywave.keys.primary]
secret_ref = "env:CANOPYWAVE_API_KEY"
weight = 1.0
rpm_limit = 60
tpm_limit = 100000

[providers.canopywave.models.kimi-k2-6]
display = "Kimi K2.6"
aliases = ["kimi", "k2.6"]
capabilities = ["text", "vision", "tools", "caching"]
context_window = 256000
max_output_tokens = 32000
```

**Features:**
- **Key Pooling** — Multiple keys per provider with weighted equal-burn routing, RPM/TPM/RPD tracking, and auto-cooldown on rate limits.
- **Auto-Failover** — If a provider or key fails, the mesh transparently tries the next fallback model/provider.
- **Auto-Refresh** — Background model discovery on startup.
- **OAuth Support** — Claude Pro/Max and ChatGPT Plus/Pro via browser PKCE flow.

---

## 🔌 MCP (Model Context Protocol)

Connect external tool servers via `~/.config/cogitum/mcp.toml`:

```toml
[servers.fetch]
command = "uvx"
args = ["mcp-server-fetch"]

[servers.time]
command = "uvx"
args = ["mcp-server-time", "--local-timezone", "Europe/Moscow"]
```

- **Auto-discovery** — Tools are registered as `mcp_server_tool` dynamically.
- **Security** — Per-tool risk assignment, secret redaction in errors, minimal subprocess env.
- **Hot-reload** — File watcher auto-reconnects when `mcp.toml` changes.
- **Sampling bridge** — MCP servers can request LLM completions through Cogitum.

---

## 💾 Persistence

| Layer | Storage | What survives |
|-------|---------|---------------|
| **Sessions** | `~/.config/cogitum/sessions/*.jsonl` | Full conversation history, model per session |
| **Memory** | `~/.config/cogitum/memory/*.md` | User identity, agent notes |
| **Skills** | `~/.config/cogitum/skills/**/*.md` | Reusable procedural knowledge |
| **Checkpoints** | `~/.config/cogitum/cogit/` | Project snapshots (content-addressed) |
| **Config** | `~/.config/cogitum/providers.toml` | Provider mesh, keys, models |
| **Secrets** | `~/.config/cogitum/secrets.env` | Plain env secrets |
| **Vault** | `~/.config/cogitum/vault.enc` | AES-256-GCM encrypted secrets |
| **Auth** | `~/.config/cogitum/auth.json` | OAuth tokens |

---

## 📡 Telegram Gateway

Run Cogitum as a personal Telegram bot:

```bash
cog tg setup   # Configure token & user ID
cog tg start   # Start daemon
cog tg status  # Check health
```

- **Streaming** — Live message editing with thinking/status/response rails.
- **Commands** — `/new`, `/resume`, `/models`, `/model`, `/reload`, `/stop`, `/help`.
- **Media** — Auto-detects screenshots in tool results and sends them as photos.
- **Session sync** — One session per chat, persisted to disk.
- **Admin whitelist** — Single-user access control.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────┐
│                  TUI (Textual)              │
│   Feed │ Composer │ Inspector │ StatusBar   │
└────────────────────┬────────────────────────┘
                     │ AgentEvents
┌────────────────────▼────────────────────────┐
│                 Agent Loop                  │
│   stream → parse → tools → inject → retry   │
└────────────────────┬────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   ┌────────┐  ┌────────┐  ┌──────────┐
   │  Mesh  │  │ Registry│  │ Sessions │
   │(LLM)   │  │(Tools) │  │(Store)   │
   └────────┘  └────────┘  └──────────┘
        │            │
   ┌────┴────┐   ┌──┴──────┐
   │Providers│   │ Built-in │
   │  + MCP  │   │  + MCP   │
   └─────────┘   └──────────┘
```

---

## 🛡️ Security

- **Approval gates** for medium/danger tools. Configurable per-tool and per-MCP-tool.
- **Path blocking** — `read_file` refuses `/proc`, `/sys`, `/dev`, `.ssh`, `.aws`, etc.
- **SSRF protection** — `fetch_url` blocks localhost, private IPs, cloud metadata endpoints.
- **Secret redaction** — API keys, tokens, and bearer headers are stripped from error messages before they reach the LLM.
- **Vault encryption** — AES-256-GCM with Argon2id KDF for at-rest secrets.
- **OAuth storage** — Tokens stored at `0600` permissions.

---

## 📋 CLI Reference

```bash
cog                              # Launch TUI
cog setup [--tty]               # Provider wizard
cog models                      # List resolved models
cog auth login <provider>       # OAuth login
cog auth logout <provider>      # Remove OAuth
cog auth list                   # Show OAuth status
cog vault init                  # Create encrypted vault
cog vault set <key>             # Store secret in vault
cog vault get <key>             # Retrieve secret
cog secret set <name> [value]   # Store env secret
cog secret list                 # List secrets (masked)
cog providers path              # Show providers.toml path
cog providers edit              # Edit in $EDITOR
cog tg setup/start/stop/status  # Telegram gateway
cog mcp ...                     # MCP server management
```

---

## 🎨 Design

Cogitum uses a warm **Imperial Fists** palette — no blue, no generic AI chrome:

| Token | Color | Usage |
|-------|-------|-------|
| `GOLD_HI` | `#F5C24A` | Selections, headers, YOU badge |
| `GOLD` | `#C7A23E` | Agent header, accents |
| `BRONZE` | `#A8732D` | Borders, tool cards, AI badge |
| `COPPER` | `#8C6B4F` | Card borders, secondary text |
| `TXT` | `#E6E1CF` | Primary text (parchment) |
| `BG` | `#0E0E11` | Background |
| `BG_SOFT` | `#1A1816` | Cards, inputs |
| `RUST` | `#B85C4F` | Errors, danger |

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

**Built with** [Textual](https://textual.textualize.io) · [Rich](https://rich.readthedocs.io) · [httpx](https://www.python-httpx.org)

</div>
