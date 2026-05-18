# cogitum (npm)

[![npm version](https://img.shields.io/npm/v/cogitum.svg)](https://www.npmjs.com/package/cogitum)

Cross-platform npm wrapper around [Cogitum](https://github.com/StarryCod/cogitum) — a sovereign agentic CLI built in Python with a Textual TUI, multi-provider LLM mesh, persistent sessions, skills, MCP integration, and a Telegram gateway.

## Install

```bash
npm install -g cogitum
```

This is *fast* — installation does **not** clone the repo or run pip. It registers two commands (`cog`, `cogitum`) and exits.

## First run

```bash
cog
```

The first launch bootstraps the Python backend:

1. Clones the repo into the platform-appropriate data directory:
   - Linux: `$XDG_DATA_HOME/cogitum` (default `~/.local/share/cogitum`)
   - macOS: `~/Library/Application Support/cogitum`
   - Windows: `%LOCALAPPDATA%\cogitum`
2. Creates a Python virtual environment inside it
3. Installs `cogitum` and all extras into the venv
4. Writes a marker file so subsequent launches skip this step entirely

Subsequent `cog ...` invocations exec the venv's `python -m cogitum.cli` directly, with zero startup overhead beyond Node's process boot.

## Wrapper commands

```bash
cog                    # launch the TUI
cog setup              # run the provider wizard (first-time auth setup)
cog --update           # pull latest from origin/master, reinstall deps
cog --repair           # wipe and recreate the venv (use after OS upgrades)
cog --where            # print the install directory
cog --version-wrapper  # print npm-wrapper version + install metadata
```

Anything else is forwarded to the underlying Python CLI:

```bash
cog auth login anthropic
cog mcp list
cog secret set OPENAI_API_KEY
```

## Requirements

- **Node.js 16+** (for the wrapper itself)
- **Python 3.11+** (for the Cogitum backend; auto-detected on first run)
- **git** (for cloning the repo on first run)

## Why a wrapper?

Cogitum is a Python project. The npm wrapper exists because:

- `npm install -g` is a familiar one-liner across Linux, macOS, Windows
- It avoids the discovery problem of "is `python3` on PATH? `python`? `py`?"
- Updates can be triggered with one explicit command (`cog --update`) rather than git pull + pip install
- The wrapper's launcher is 30 lines — most of the logic lives in the Python project itself

If you'd prefer a pure Python install, skip the wrapper:

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/StarryCod/cogitum/master/scripts/install.sh | bash

# Windows (PowerShell)
iwr https://raw.githubusercontent.com/StarryCod/cogitum/master/scripts/install.ps1 | iex
```

## Notes

- **Do not use `sudo npm install -g`.** If you hit `EACCES` errors, [fix npm permissions](https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-packages-globally). `sudo` makes the install dir owned by root, which then breaks `cog --update` when running as your normal user.
- **Marker file controls bootstrap.** If you want to force re-bootstrap, delete `~/.local/share/cogitum/.installed` (path varies by OS — see `cog --where`) and run `cog` again.
- **Install directory override.** Set `COGITUM_HOME=/some/path` to install elsewhere.
- **Color output.** Honours `NO_COLOR` per [no-color.org](https://no-color.org). Auto-disables when stdout is not a TTY.

## License

MIT — see [LICENSE](https://github.com/StarryCod/cogitum/blob/master/LICENSE).

---

**For the Emperor!**
