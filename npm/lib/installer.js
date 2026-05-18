/**
 * Cogitum installer — cross-platform Python backend bootstrap.
 *
 * Why this file exists:
 *   The npm package is a thin wrapper around a Python project. We let
 *   `npm install -g cogitum` register the launcher commands (cog,
 *   cogitum) instantly, then bootstrap the Python backend on first
 *   actual launch. This keeps `npm install` fast and avoids the
 *   classic mistake of running pip during postinstall (sudo prompts,
 *   EACCES, slow CI builds).
 *
 * Lifecycle:
 *
 *   1. User runs `npm install -g cogitum`     → registers cog/cogitum
 *      shims pointing at bin/cog.js. NO Python work happens here.
 *
 *   2. User runs `cog` for the first time     → the launcher detects
 *      the absence of an "installed" marker and calls
 *      `ensureInstalled()` from this module. We:
 *        a) clone https://github.com/StarryCod/cogitum into the
 *           per-OS install directory (data dir, never node_modules)
 *        b) create a venv inside that clone
 *        c) pip install -e .[all]
 *        d) write the marker file so step 2 only runs once
 *
 *   3. User runs `cog ...` going forward      → launcher exec's
 *      .venv/bin/python -m cogitum.cli with all argv passed through.
 *      No git, no pip, no network. Same speed as a native install.
 *
 *   4. User runs `cog --update`               → explicit-intent
 *      git fetch + reset + pip install. We do NOT do this implicitly
 *      because past versions did and it broke users who'd customised
 *      their clone or were on a slow connection.
 *
 *   5. User runs `cog --repair`               → wipe venv, recreate.
 *      For when a .venv goes bad after an OS upgrade or partial install.
 *
 * Cross-platform layout:
 *
 *   Linux:    install dir = $XDG_DATA_HOME/cogitum  (default ~/.local/share/cogitum)
 *   macOS:    install dir = ~/Library/Application Support/cogitum
 *   Windows:  install dir = %LOCALAPPDATA%\cogitum
 *
 * Inside the install dir we always have:
 *   ./             → the cloned repo
 *   ./.venv/       → the Python virtual environment
 *   ./.installed   → marker (JSON: {version, sha, ts}); presence means
 *                    we can skip the install path on launch
 */

'use strict';

const { spawnSync } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

// ─────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────

const REPO = 'https://github.com/StarryCod/cogitum.git';
const BRANCH = 'master';
const PKG_VERSION = require('../package.json').version;

const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';

// ─────────────────────────────────────────────────────────────────────────
// ANSI colour helpers — kept tiny so the file has zero deps
// ─────────────────────────────────────────────────────────────────────────

const C = {
  gold:   '\x1b[33m',
  bronze: '\x1b[38;5;130m',
  rust:   '\x1b[38;5;160m',
  ok:     '\x1b[38;5;108m',
  dim:    '\x1b[2m',
  reset:  '\x1b[0m',
};

// Drop colour if NO_COLOR is set or stdout isn't a TTY (per the
// no-color.org convention plus standard isTTY check).
const COLORLESS = process.env.NO_COLOR || !process.stdout.isTTY;
function paint(text, code) { return COLORLESS ? text : `${code}${text}${C.reset}`; }

const log  = (msg) => console.log(paint('⚔', C.gold)   + ' ' + msg);
const warn = (msg) => console.log(paint('⚠', C.bronze) + ' ' + msg);
const err  = (msg) => console.error(paint('✗', C.rust) + ' ' + msg);
const ok   = (msg) => console.log(paint('✓', C.ok)     + ' ' + msg);

// ─────────────────────────────────────────────────────────────────────────
// Path resolution — matches cogitum/core/platform_paths.py
// ─────────────────────────────────────────────────────────────────────────

function getInstallDir() {
  if (process.env.COGITUM_HOME) {
    return process.env.COGITUM_HOME;
  }
  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
    return path.join(local, 'cogitum');
  }
  if (IS_MAC) {
    return path.join(os.homedir(), 'Library', 'Application Support', 'cogitum');
  }
  // Linux + everything else: XDG data dir
  const xdg = process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
  return path.join(xdg, 'cogitum');
}

function getVenvDir() { return path.join(getInstallDir(), '.venv'); }

function getVenvPython() {
  return IS_WIN
    ? path.join(getVenvDir(), 'Scripts', 'python.exe')
    : path.join(getVenvDir(), 'bin', 'python');
}

function getMarkerPath() { return path.join(getInstallDir(), '.installed'); }

// ─────────────────────────────────────────────────────────────────────────
// Subprocess helpers
// ─────────────────────────────────────────────────────────────────────────

function run(cmd, args, opts = {}) {
  return spawnSync(cmd, args, {
    stdio: opts.silent ? ['pipe', 'pipe', 'pipe'] : 'inherit',
    encoding: 'utf-8',
    cwd: opts.cwd,
    env: opts.env || process.env,
    shell: opts.shell || false,
  });
}

// ─────────────────────────────────────────────────────────────────────────
// Python detection — the single most fragile part of the install
// ─────────────────────────────────────────────────────────────────────────

function findPython() {
  // Order: most-specific first (3.13/3.12/3.11), then generic. On
  // Windows the `py` launcher is the canonical entry point and is
  // installed by the official Python installer; we try it last with
  // an explicit -3 selector so it picks the highest installed major.
  const candidates = IS_WIN
    ? [
        ['python3.13', []],
        ['python3.12', []],
        ['python3.11', []],
        ['python',     []],
        ['py',         ['-3']],
      ]
    : [
        ['python3.13', []],
        ['python3.12', []],
        ['python3.11', []],
        ['python3',    []],
        ['python',     []],
      ];

  for (const [bin, prefix] of candidates) {
    const r = run(bin, [...prefix, '--version'], { silent: true });
    if (r.status !== 0) continue;
    const out = (r.stdout || '') + (r.stderr || '');
    const m = out.match(/Python (\d+)\.(\d+)/);
    if (!m) continue;
    const major = parseInt(m[1], 10);
    const minor = parseInt(m[2], 10);
    if (major > 3 || (major === 3 && minor >= 11)) {
      return { bin, prefixArgs: prefix, version: `${major}.${minor}` };
    }
  }
  return null;
}

// ─────────────────────────────────────────────────────────────────────────
// Install steps
// ─────────────────────────────────────────────────────────────────────────

function readMarker() {
  try {
    const text = fs.readFileSync(getMarkerPath(), 'utf-8');
    return JSON.parse(text);
  } catch (_) {
    return null;
  }
}

function writeMarker(extra = {}) {
  const sha = (() => {
    const r = run('git', ['-C', getInstallDir(), 'rev-parse', 'HEAD'], { silent: true });
    return r.status === 0 ? r.stdout.trim() : '';
  })();
  const data = {
    version: PKG_VERSION,
    sha,
    timestamp: new Date().toISOString(),
    ...extra,
  };
  fs.writeFileSync(getMarkerPath(), JSON.stringify(data, null, 2));
}

function clearMarker() {
  try { fs.unlinkSync(getMarkerPath()); } catch (_) { /* ignore */ }
}

function ensureClone() {
  const dir = getInstallDir();
  fs.mkdirSync(dir, { recursive: true });

  if (fs.existsSync(path.join(dir, '.git'))) {
    // Repo already cloned. Sync with origin so a stale clone (left
    // over from a previous npm-package version, or from a manual
    // git clone the user did months ago) doesn't make the new
    // wrapper run against ancient code.
    log('Existing clone found — syncing with origin/' + BRANCH + ' ...');
    let r = run('git', ['-C', dir, 'fetch', '--all', '--quiet']);
    if (r.status !== 0) {
      warn('git fetch failed; proceeding with whatever is on disk.');
      return;
    }
    r = run('git', ['-C', dir, 'reset', '--hard', `origin/${BRANCH}`, '--quiet']);
    if (r.status !== 0) {
      warn('git reset failed; proceeding with whatever is on disk.');
    }
    return;
  }

  log(`Cloning Cogitum to ${dir} ...`);
  const r = run('git', ['clone', '--depth', '1', '--branch', BRANCH, REPO, dir]);
  if (r.status !== 0) {
    err('git clone failed.');
    err('Is git installed and on PATH?');
    process.exit(1);
  }
}

function ensureVenv(python) {
  const venv = getVenvDir();
  if (fs.existsSync(getVenvPython())) {
    return; // Already exists.
  }
  log('Creating virtual environment ...');
  const r = run(python.bin, [...python.prefixArgs, '-m', 'venv', venv]);
  if (r.status !== 0) {
    err('Failed to create virtual environment.');
    err(IS_WIN
      ? 'On Windows you may need: python -m pip install --user virtualenv'
      : 'On Debian/Ubuntu: sudo apt install python3-venv');
    process.exit(1);
  }
}

function installDeps() {
  const py = getVenvPython();
  log('Upgrading pip ...');
  let r = run(py, ['-m', 'pip', 'install', '--upgrade', 'pip', '--quiet']);
  if (r.status !== 0) {
    warn('pip upgrade failed (continuing with bundled pip).');
  }

  log('Installing cogitum + extras ...');
  r = run(py, ['-m', 'pip', 'install', '-e', `${getInstallDir()}[all]`]);
  if (r.status !== 0) {
    err('pip install failed. See output above.');
    process.exit(1);
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Public API: ensureInstalled / update / repair
// ─────────────────────────────────────────────────────────────────────────

function ensureInstalled() {
  // Fast path: marker present + venv python exists → nothing to do.
  if (readMarker() && fs.existsSync(getVenvPython())) {
    return;
  }

  log('First-run bootstrap — this happens once.');
  const python = findPython();
  if (!python) {
    err('Python 3.11+ is required but was not found in PATH.');
    err(IS_WIN
      ? 'Install from https://python.org/downloads or: winget install Python.Python.3.13'
      : 'Install via your package manager, e.g.:');
    if (!IS_WIN) {
      err('  Debian/Ubuntu:  sudo apt install python3.11 python3.11-venv');
      err('  Fedora:         sudo dnf install python3.11');
      err('  Arch:           sudo pacman -S python');
      err('  macOS (brew):   brew install python@3.11');
    }
    process.exit(1);
  }
  log(`Using Python ${python.version} (${python.bin}).`);

  ensureClone();
  ensureVenv(python);
  installDeps();
  writeMarker();
  ok('Bootstrap complete. Run `cog setup` to configure providers.');
}

function update() {
  if (!fs.existsSync(path.join(getInstallDir(), '.git'))) {
    warn('Not installed yet. Running first-time bootstrap instead of update.');
    ensureInstalled();
    return;
  }

  log('Fetching latest from origin ...');
  let r = run('git', ['-C', getInstallDir(), 'fetch', '--all', '--quiet']);
  if (r.status !== 0) {
    err('git fetch failed.');
    process.exit(1);
  }

  log(`Resetting to origin/${BRANCH} ...`);
  r = run('git', ['-C', getInstallDir(), 'reset', '--hard', `origin/${BRANCH}`, '--quiet']);
  if (r.status !== 0) {
    err('git reset failed.');
    process.exit(1);
  }

  // Re-install deps in case pyproject.toml changed.
  installDeps();
  writeMarker({ updated: true });
  ok('Update complete.');
}

function repair() {
  const venv = getVenvDir();
  if (fs.existsSync(venv)) {
    log('Removing broken venv ...');
    fs.rmSync(venv, { recursive: true, force: true });
  }
  clearMarker();
  ensureInstalled();
  ok('Repair complete.');
}

// ─────────────────────────────────────────────────────────────────────────
// Launcher entry point — used by bin/cog.js and bin/cogitum.js
// ─────────────────────────────────────────────────────────────────────────

function launch(argv) {
  // Handle wrapper-level commands BEFORE handing off to the Python
  // CLI so they can't be shadowed by future subcommand additions.
  if (argv.length === 1 && argv[0] === '--update') {
    update();
    return 0;
  }
  if (argv.length === 1 && argv[0] === '--repair') {
    repair();
    return 0;
  }
  if (argv.length === 1 && argv[0] === '--where') {
    console.log(getInstallDir());
    return 0;
  }
  if (argv.length === 1 && argv[0] === '--version-wrapper') {
    console.log(`cogitum-npm ${PKG_VERSION}`);
    console.log(`install dir: ${getInstallDir()}`);
    const m = readMarker();
    if (m) {
      console.log(`installed sha: ${m.sha || '(unknown)'}`);
      console.log(`bootstrapped: ${m.timestamp}`);
    } else {
      console.log('not yet bootstrapped');
    }
    return 0;
  }

  ensureInstalled();

  const py = getVenvPython();
  const r = spawnSync(py, ['-m', 'cogitum.cli', ...argv], {
    stdio: 'inherit',
    env: process.env,
  });
  return r.status === null ? 1 : r.status;
}

module.exports = {
  ensureInstalled,
  update,
  repair,
  launch,
  // Exposed for testing / introspection.
  paths: {
    getInstallDir,
    getVenvDir,
    getVenvPython,
    getMarkerPath,
  },
};
