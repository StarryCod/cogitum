#!/usr/bin/env node
/**
 * `cog` — primary Cogitum launcher.
 *
 * Tiny wrapper. Defers all real work to lib/installer.js so the
 * launcher itself stays trivially auditable. See the docstring at the
 * top of lib/installer.js for the full lifecycle.
 *
 * Behaviour:
 *   cog                    → launch the TUI (after first-run bootstrap)
 *   cog setup              → run the provider wizard
 *   cog update             → pull latest + reinstall (Textual progress UI)
 *   cog --repair           → wipe venv, recreate from scratch
 *   cog --where            → print the install directory
 *   cog --version-wrapper  → print npm-wrapper version + install metadata
 *   cog --auto-update-on   → enable auto-pull on every launch when newer
 *   cog --auto-update-off  → disable auto-pull
 *   cog <anything else>    → forwarded to `python -m cogitum.cli <anything>`
 *
 * Note: `cog update` (no dashes) is the canonical update command — it's
 * a Python subcommand inside cogitum.cli that runs the full Textual
 * update flow with progress + cancel. The wrapper just forwards to it.
 *
 * Env vars honoured:
 *   COGITUM_HOME=/path           → override install directory
 *   COGITUM_AUTO_UPDATE=1        → temporarily enable auto-update
 *   COGITUM_NO_UPDATE_CHECK=1    → suppress the update probe + banner
 *   NO_UPDATE_CHECK=1            → same, NO_COLOR-style alias
 *   NO_COLOR=1                   → drop ANSI colour
 */
'use strict';

const { launch } = require('../lib/installer.js');
process.exit(launch(process.argv.slice(2)));
