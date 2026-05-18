#!/usr/bin/env node
/**
 * `cog` — primary Cogitum launcher.
 *
 * Tiny wrapper. Defers all real work to lib/installer.js so the
 * launcher itself stays trivially auditable. See the docstring at the
 * top of lib/installer.js for the full lifecycle.
 *
 * Behaviour:
 *   cog                  → launch the TUI (after first-run bootstrap)
 *   cog setup            → run the provider wizard
 *   cog --update         → pull latest + reinstall deps
 *   cog --repair         → wipe venv, recreate from scratch
 *   cog --where          → print the install directory
 *   cog --version-wrapper → print npm-wrapper version + install metadata
 *   cog <anything else>  → forwarded to `python -m cogitum.cli <anything>`
 */
'use strict';

const { launch } = require('../lib/installer.js');
process.exit(launch(process.argv.slice(2)));
