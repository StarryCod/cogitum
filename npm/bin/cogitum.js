#!/usr/bin/env node
/**
 * `cogitum` — long-form alias of `cog`.
 *
 * Identical entry point. Some users prefer the unabbreviated command
 * (e.g. when scripting in CI where `cog` may collide with another
 * tool of the same name).
 */
'use strict';

const { launch } = require('../lib/installer.js');
process.exit(launch(process.argv.slice(2)));
