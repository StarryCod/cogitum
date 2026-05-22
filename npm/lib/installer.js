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
 *      A best-effort, cached update probe runs in the background and
 *      prints a one-line banner if origin/master is ahead. The probe
 *      is bounded (3s timeout, 12h cache) and silent on every error
 *      path — startup never depends on network reachability.
 *
 *   4. User runs `cog --update`               → explicit-intent
 *      git fetch + reset + pip install. Auto-update is OFF by default
 *      (past versions did it implicitly and broke users on slow
 *      connections / with local edits). Users who want auto-update
 *      can opt in with `COGITUM_AUTO_UPDATE=1` or by creating
 *      `<install dir>/.auto-update`.
 *
 *   5. User runs `cog --repair`               → wipe venv, recreate.
 *      For when a .venv goes bad after an OS upgrade or partial install.
 *
 *   6. npm wrapper version bump                → if the installed
 *      marker records a wrapper version older than the running one,
 *      we re-run the dep install step (pyproject.toml extras may have
 *      changed) but don't touch the clone itself.
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
 *   ./.installed   → marker (JSON: {version, npmVersion, sha, ts}); presence
 *                    means we can skip the install path on launch
 *   ./.update-check → cache (JSON: {ts, latestSha}); rate-limits the
 *                    update probe to once per 12h
 */

'use strict';

const { spawnSync, spawn } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

// ─────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────

const REPO = 'https://github.com/StarryCod/cogitum.git';
const REPO_HTTP = 'https://github.com/StarryCod/cogitum';
const BRANCH = 'master';
const PKG_VERSION = require('../package.json').version;

const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';

// Update probe cadence — once per 12h is enough; users who want a
// fresher signal can run `cog --update` directly.
const UPDATE_CHECK_TTL_MS = 12 * 60 * 60 * 1000;
const UPDATE_CHECK_TIMEOUT_MS = 3000;

// ─────────────────────────────────────────────────────────────────────────
// ANSI colour helpers — kept tiny so the file has zero deps
// ─────────────────────────────────────────────────────────────────────────

const C = {
  gold:   '\x1b[33m',
  goldHi: '\x1b[1;33m',
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
function getUpdateCheckPath() { return path.join(getInstallDir(), '.update-check'); }
function getAutoUpdateFlagPath() { return path.join(getInstallDir(), '.auto-update'); }

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
    timeout: opts.timeout || 0,
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
        ['python3.14', []],
        ['python3.13', []],
        ['python3.12', []],
        ['python3.11', []],
        ['python',     []],
        ['py',         ['-3']],
      ]
    : [
        ['python3.14', []],
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
    npmVersion: PKG_VERSION,
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
    log(`Existing clone found — syncing with origin/${BRANCH} ...`);
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
  // depth=1 keeps clone fast (~2-3s on a decent connection) but still
  // gives ``git fetch + reset`` enough history to update against any
  // origin commit later.
  const r = run('git', ['clone', '--depth', '1', '--branch', BRANCH, REPO, dir]);
  if (r.status !== 0) {
    err('git clone failed.');
    err('Is git installed and on PATH?');
    err(`Manual fallback: git clone ${REPO_HTTP} '${dir}'`);
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
// Update probe — quiet, cached, never blocks
// ─────────────────────────────────────────────────────────────────────────

function readUpdateCheckCache() {
  try {
    return JSON.parse(fs.readFileSync(getUpdateCheckPath(), 'utf-8'));
  } catch (_) {
    return null;
  }
}

function writeUpdateCheckCache(latestSha) {
  try {
    fs.writeFileSync(getUpdateCheckPath(), JSON.stringify({
      ts: Date.now(),
      latestSha,
    }, null, 2));
  } catch (_) { /* non-fatal */ }
}

/**
 * Returns the local HEAD sha or null. Synchronous — git rev-parse is
 * a millisecond-scale read against .git/HEAD, no network.
 */
function getLocalSha() {
  const r = run('git', ['-C', getInstallDir(), 'rev-parse', 'HEAD'], {
    silent: true,
    timeout: 1500,
  });
  return r.status === 0 ? r.stdout.trim() : null;
}

/**
 * Returns latest origin sha or null on any failure (no network, no
 * git, slow DNS, etc). Bounded by UPDATE_CHECK_TIMEOUT_MS.
 */
function getRemoteSha() {
  const r = run('git', ['ls-remote', REPO, BRANCH], {
    silent: true,
    timeout: UPDATE_CHECK_TIMEOUT_MS,
  });
  if (r.status !== 0) return null;
  const m = (r.stdout || '').match(/^([0-9a-f]{40})\s/);
  return m ? m[1] : null;
}

/**
 * Spawn a detached background probe that updates the cache. We do
 * this from the main process via setImmediate after the launcher has
 * already started Python — the Python TUI runs in the foreground, the
 * probe runs in the background, and the cache is read on the NEXT
 * launch. Keeps current launch zero-latency.
 */
function maybeScheduleBackgroundUpdateCheck() {
  if (process.env.NO_UPDATE_CHECK || process.env.COGITUM_NO_UPDATE_CHECK) return;
  const cache = readUpdateCheckCache();
  if (cache && (Date.now() - cache.ts) < UPDATE_CHECK_TTL_MS) return;
  // Spawn detached node process to run a tiny update-check script.
  // We can't use setTimeout because the main process may exit
  // immediately after launch() forwards to spawnSync('python').
  // Instead: spawn ourselves with a hidden flag; the child exits
  // after writing the cache.
  try {
    const child = spawn(process.execPath, [
      __filename, '__update_check_internal__',
    ], {
      detached: true,
      stdio: 'ignore',
    });
    child.unref();
  } catch (_) { /* probe is a UX nicety, not a hard requirement */ }
}

/**
 * Read cache and print a one-line banner if origin is ahead of local.
 * Called synchronously at launch start; only touches local files.
 */
function printUpdateBannerIfAvailable() {
  if (process.env.NO_UPDATE_CHECK || process.env.COGITUM_NO_UPDATE_CHECK) return;
  const cache = readUpdateCheckCache();
  if (!cache || !cache.latestSha) return;
  const local = getLocalSha();
  if (!local || local === cache.latestSha) return;
  // Newer commit on origin. Show a single-line banner above the TUI
  // so the user can see it before the alt-screen takes over.
  console.log(
    paint('▲', C.bronze) + ' ' +
    paint('Cogitum update available', C.goldHi) + '  ' +
    paint(local.slice(0, 7) + ' → ' + cache.latestSha.slice(0, 7), C.dim) + '  ' +
    paint(`run \`cog --update\` to pull origin/${BRANCH}`, C.dim)
  );
}

/**
 * Auto-update opt-in. Triggered if either:
 *   - env: COGITUM_AUTO_UPDATE=1
 *   - file: <install dir>/.auto-update exists (touch to enable)
 * Behaviour: on launch, if cache says origin is ahead of local, run
 * the full update flow synchronously before forwarding to Python.
 * Silent on any failure path — slow networks must not block the TUI.
 */
function autoUpdateEnabled() {
  if (process.env.COGITUM_AUTO_UPDATE === '1') return true;
  try { return fs.existsSync(getAutoUpdateFlagPath()); }
  catch (_) { return false; }
}

function maybeAutoUpdate() {
  if (!autoUpdateEnabled()) return;
  const cache = readUpdateCheckCache();
  if (!cache || !cache.latestSha) return;
  const local = getLocalSha();
  if (!local || local === cache.latestSha) return;
  log('Auto-update: origin/' + BRANCH + ' is ahead, pulling ...');
  try {
    _gitUpdateInPlace();
  } catch (e) {
    warn('Auto-update failed; continuing with current install.');
  }
}

/**
 * Wrapper-side git update path (used by auto-update only).
 *
 * The user-facing `cog update` is the Python Textual flow in
 * cogitum.update_flow — that's the canonical command. We keep this
 * wrapper-side helper for the auto-update branch because:
 *   - auto-update fires BEFORE we exec the Python entry point, so we
 *     can't reach update_flow yet
 *   - it's the same fetch+reset+pip flow but without the Textual UI
 *
 * Anyone running `cog --update` (legacy form) ends up in
 * cogitum.cli's `update` subcommand which calls update_flow.run().
 */
function _gitUpdateInPlace() {
  if (!fs.existsSync(path.join(getInstallDir(), '.git'))) {
    warn('Not installed yet. Running first-time bootstrap instead of update.');
    ensureInstalled();
    return;
  }
  log('Fetching latest from origin ...');
  let r = run('git', ['-C', getInstallDir(), 'fetch', '--all', '--quiet']);
  if (r.status !== 0) throw new Error('git fetch failed');
  log(`Resetting to origin/${BRANCH} ...`);
  r = run('git', ['-C', getInstallDir(), 'reset', '--hard', `origin/${BRANCH}`, '--quiet']);
  if (r.status !== 0) throw new Error('git reset failed');
  installDeps();
  writeMarker({ updated: true });
  const local = getLocalSha();
  if (local) writeUpdateCheckCache(local);
  ok('Auto-update complete.');
}

// ─────────────────────────────────────────────────────────────────────────
// Public API: ensureInstalled / update / repair
// ─────────────────────────────────────────────────────────────────────────

function ensureInstalled() {
  const marker = readMarker();
  const venvOK = fs.existsSync(getVenvPython());

  // Fast path: marker present + venv python exists + same wrapper
  // version → nothing to do.
  if (marker && venvOK && marker.npmVersion === PKG_VERSION) {
    return;
  }

  // Wrapper version bump: the npm package was upgraded but the
  // Python clone is still on whatever sha the previous wrapper
  // pinned to. Pull origin and re-install deps so the user gets the
  // fixes the wrapper bump implies. Don't repeat the full bootstrap
  // banner — this is a quiet refresh.
  if (marker && venvOK && marker.npmVersion !== PKG_VERSION) {
    log(`npm wrapper bumped ${marker.npmVersion || '(unknown)'} → ${PKG_VERSION} — refreshing backend ...`);
    try {
      ensureClone();
      installDeps();
      writeMarker({ refreshedFrom: marker.npmVersion });
      ok('Backend refresh complete.');
    } catch (e) {
      warn('Wrapper-bump refresh failed; continuing with previous backend.');
    }
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
  // Refresh the update-check cache so the banner doesn't keep showing.
  const local = getLocalSha();
  if (local) writeUpdateCheckCache(local);
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
// Internal: background update check entry point
// Spawned via spawn(process.execPath, [__filename, '__update_check_internal__'])
// ─────────────────────────────────────────────────────────────────────────

function _runBackgroundUpdateCheck() {
  // Detached process — never write to stdout/stderr (parent already
  // exited or about to). Only touches the cache file. Soft-fails on
  // every error path.
  try {
    const sha = getRemoteSha();
    if (sha) writeUpdateCheckCache(sha);
  } catch (_) { /* swallowed — UX nicety */ }
  process.exit(0);
}

if (require.main === module && process.argv[2] === '__update_check_internal__') {
  _runBackgroundUpdateCheck();
}

// ─────────────────────────────────────────────────────────────────────────
// Launcher entry point — used by bin/cog.js and bin/cogitum.js
// ─────────────────────────────────────────────────────────────────────────

function launch(argv) {
  // Handle wrapper-level commands BEFORE handing off to the Python
  // CLI so they can't be shadowed by future subcommand additions.
  if (argv.length === 1 && argv[0] === '--repair') {
    repair();
    return 0;
  }
  if (argv.length === 1 && argv[0] === '--where') {
    console.log(getInstallDir());
    return 0;
  }
  if (argv.length === 1 && (argv[0] === '--version-wrapper' || argv[0] === '--wrapper-version')) {
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
  if (argv.length === 1 && argv[0] === '--auto-update-on') {
    fs.mkdirSync(getInstallDir(), { recursive: true });
    fs.writeFileSync(getAutoUpdateFlagPath(), `enabled at ${new Date().toISOString()}\n`);
    ok(`Auto-update enabled. Cogitum will pull origin/${BRANCH} on every launch when newer.`);
    ok('Disable with `cog --auto-update-off`.');
    return 0;
  }
  if (argv.length === 1 && argv[0] === '--auto-update-off') {
    try { fs.unlinkSync(getAutoUpdateFlagPath()); } catch (_) { /* fine */ }
    ok('Auto-update disabled.');
    return 0;
  }

  ensureInstalled();
  // Order matters: maybeAutoUpdate must run BEFORE the banner so a
  // user with auto-update on doesn't see "update available" on a
  // launch that's about to apply it.
  maybeAutoUpdate();
  printUpdateBannerIfAvailable();
  // Schedule the next background probe AFTER printing the current
  // banner. The probe writes the cache that the NEXT launch reads.
  maybeScheduleBackgroundUpdateCheck();

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
    getUpdateCheckPath,
    getAutoUpdateFlagPath,
  },
};
