# cogitum (npm)

[![npm version](https://img.shields.io/npm/v/cogitum.svg)](https://www.npmjs.com/package/cogitum)
[![github](https://img.shields.io/badge/github-StarryCod%2Fcogitum-yellow)](https://github.com/StarryCod/cogitum)

Cross-platform npm launcher for [Cogitum](https://github.com/StarryCod/cogitum) — a sovereign agentic CLI: Textual TUI, multi-provider LLM mesh, persistent sessions, skills, MCP, and a Telegram gateway. Imperial Fists colourway.

## Install

```bash
npm install -g cogitum
```

Fast — no clone, no pip, no postinstall. Registers two commands (`cog`, `cogitum`) and exits.

## First run

```bash
cog
```

The first launch bootstraps the Python backend:

1. Clones the repo into the platform data directory
   - Linux: `$XDG_DATA_HOME/cogitum` (default `~/.local/share/cogitum`)
   - macOS: `~/Library/Application Support/cogitum`
   - Windows: `%LOCALAPPDATA%\cogitum`
2. Creates a Python venv inside it
3. `pip install -e .[all]`
4. Writes a marker so subsequent launches skip the install path

After bootstrap, `cog` exec's the venv's `python -m cogitum.cli` with zero overhead beyond Node's process boot.

## Commands

```bash
cog                    # launch the TUI
cog setup              # provider wizard (API keys, OAuth, TG gateway)
cog update             # pull latest + reinstall (Textual progress UI)
cog --repair           # wipe and recreate the venv (after OS upgrades)
cog --where            # print the install directory
cog --version-wrapper  # npm-wrapper version + install metadata
cog --auto-update-on   # enable auto-pull on every launch when origin is ahead
cog --auto-update-off  # disable auto-update
```

Anything else forwards to the Python CLI:

```bash
cog auth login anthropic
cog mcp list
cog secret set OPENAI_API_KEY
cog tg start
```

`cog update` is the canonical updater — it's the Textual-driven flow inside `cogitum.cli` (live progress, cancel, dep refresh). Wrapper-only flags use `--` to keep them out of subcommand namespace.

## Update behaviour

- **Quiet probe.** Each launch runs a backgrounded `git ls-remote` (3s timeout, 12h cache) against `origin/master`. On the next launch, if origin is ahead of your local sha, you see a one-line banner above the TUI.
- **Manual pull.** `cog update` runs the full Textual flow with progress bar.
- **Auto-update opt-in.** `cog --auto-update-on` (or `COGITUM_AUTO_UPDATE=1`, or `touch <install dir>/.auto-update`) pulls automatically before launch when newer commits are available. Off by default — past versions did it implicitly and broke users on slow connections / with local edits.
- **Disable probe.** `COGITUM_NO_UPDATE_CHECK=1` (or `NO_UPDATE_CHECK=1`) suppresses both the probe and the banner.

## Wrapper-bump refresh

When the npm wrapper itself is upgraded (`npm update -g cogitum`) the launcher detects the version bump on the next `cog` invocation and quietly re-runs `pip install -e .[all]` against the existing clone. This catches new optional extras introduced in `pyproject.toml` between wrapper versions without forcing a full reclone.

## Requirements

- **Node.js 16+**
- **Python 3.11+** (auto-detected on first run; tries `python3.14`, `python3.13`, `python3.12`, `python3.11`, `python3`, `python`, then `py -3` on Windows)
- **git** (for the clone on first run; needed by `cog update` going forward)

## Why a wrapper?

Cogitum is a Python project. The npm wrapper exists because:

- `npm install -g` is a familiar one-liner across Linux, macOS, Windows
- It avoids the discovery problem of "is `python3` on PATH? `python`? `py`?"
- One explicit update command (`cog update`) instead of `cd ~/cogitum && git pull && pip install -e .`
- The launcher is ~30 lines — most logic lives in `lib/installer.js` (which you can audit before running)

If you'd prefer a pure Python install:

```bash
git clone https://github.com/StarryCod/cogitum
cd cogitum
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[all]
cog
```

## Config & data

The wrapper never touches your config. Cogitum reads/writes:

- `~/.config/cogitum/` — `providers.toml`, `settings.toml`, `mcp.toml`, `secrets.env`, OAuth tokens
- `~/.local/share/cogitum/` — install dir + sessions DB + cogit checkpoints (paths above for macOS/Windows)
- `~/.cache/cogitum/` — small caches (model lists, update probe)

`cog --where` prints the install dir. `COGITUM_HOME=/path cog` overrides it.

## Notes

- **Don't `sudo npm install -g`.** If you hit `EACCES`, [fix npm permissions](https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-packages-globally) instead. `sudo` makes the install dir root-owned which then breaks `cog update` running as your normal user.
- **Force re-bootstrap.** `cog --repair` wipes the venv and re-runs the install path. To re-clone too, delete `<install dir>` (see `cog --where`) and run `cog`.
- **Colour.** Honours `NO_COLOR` ([no-color.org](https://no-color.org)). Auto-disables when stdout isn't a TTY.

## License

MIT — see [LICENSE](https://github.com/StarryCod/cogitum/blob/master/LICENSE).

---

**For the Emperor!**
