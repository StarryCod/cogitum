#!/usr/bin/env node
const { spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const INSTALL_DIR = path.join(os.homedir(), '.local', 'share', 'cogitum');
const VENV_DIR = path.join(INSTALL_DIR, '.venv');
const REPO = 'https://github.com/StarryCod/cogitum.git';

function log(...args) {
  console.log('\x1b[32m[cogitum]\x1b[0m', ...args);
}

function warn(...args) {
  console.warn('\x1b[33m[cogitum]\x1b[0m', ...args);
}

function error(...args) {
  console.error('\x1b[31m[cogitum]\x1b[0m', ...args);
}

function run(cmd, args, opts = {}) {
  const result = spawnSync(cmd, args, {
    stdio: opts.silent ? 'pipe' : 'inherit',
    ...opts,
  });
  return result;
}

function findPython() {
  for (const py of ['python3.11', 'python3.12', 'python3.13', 'python3']) {
    const result = run(py, ['--version'], { silent: true });
    if (result.status === 0) {
      const ver = result.stdout.toString().trim() || result.stderr.toString().trim();
      const match = ver.match(/(\d+)\.(\d+)/);
      if (match) {
        const major = parseInt(match[1], 10);
        const minor = parseInt(match[2], 10);
        if (major > 3 || (major === 3 && minor >= 11)) {
          return py;
        }
      }
    }
  }
  return null;
}

function main() {
  log('Installing Cogitum ...');

  const python = findPython();
  if (!python) {
    error('Python 3.11+ is required but not found.');
    error('Install it first: sudo apt install python3.11 python3.11-venv');
    process.exit(1);
  }
  log('Using:', python);

  if (!fs.existsSync(INSTALL_DIR)) {
    log('Cloning repository ...');
    const r = run('git', ['clone', '--depth', '1', REPO, INSTALL_DIR], { silent: true });
    if (r.status !== 0) {
      error('git clone failed. Is git installed?');
      process.exit(1);
    }
  } else {
    log('Updating existing install ...');
    run('git', ['-C', INSTALL_DIR, 'pull', '--ff-only'], { silent: true });
  }

  if (!fs.existsSync(VENV_DIR)) {
    log('Creating virtual environment ...');
    run(python, ['-m', 'venv', VENV_DIR], { silent: true });
  }

  log('Installing Python dependencies ...');
  const pip = path.join(VENV_DIR, 'bin', 'pip');
  run(python, [pip, 'install', '--upgrade', 'pip'], { silent: true });
  const r = run(python, [pip, 'install', '-e', `${INSTALL_DIR}[all]`], { silent: true });
  if (r.status !== 0) {
    error('pip install failed.');
    process.exit(1);
  }

  log('Cogitum installed! Run: npx cogit');
}

main();
