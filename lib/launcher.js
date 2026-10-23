'use strict';
/**
 * npm launcher for Frappe Setup CLI.
 *
 * This file contains no installation logic. It checks that the machine can run the bundled
 * Python installer, then runs `bash install.sh <args>` (the same bootstrap used from a git
 * checkout) with the terminal attached, and exits with the installer's exit code.
 *
 * Differences from running ./install.sh in a checkout (set through environment variables):
 *   FRAPPE_SETUP_VENV_DIR      ~/.local/share/frappe-setup/npm-runtime/venv  (package dirs may be
 *                              read-only, e.g. global installs, or pruned, e.g. the npx cache)
 *   FRAPPE_SETUP_INSTALL_MODE  copy  (regular pip install from a staged copy, not editable)
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const PACKAGE_ROOT = path.resolve(__dirname, '..');
const MIN_NODE_MAJOR = 18;
const MIN_PYTHON = [3, 10];
const PYTHON_CANDIDATES = ['python3', 'python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3.10'];
const MAX_ARGS = 200;
const MAX_ARG_LENGTH = 4096;

/** Files the bundled installer needs at run time (relative to the package root). */
const REQUIRED_FILES = [
  'install.sh',
  'pyproject.toml',
  'README.md',
  'src/frappe_setup/__init__.py',
  'src/frappe_setup/cli.py',
  'src/frappe_setup/installer.py',
  'config/version-matrix.yaml',
  'config/app-catalog.yaml',
];

// Signals that are forwarded to the installer when they are sent to this process only.
// SIGINT is deliberately not forwarded: Ctrl+C in a terminal already reaches the installer,
// which shares this process group; a second SIGINT would interrupt its own cleanup.
const FORWARDED_SIGNALS = ['SIGTERM', 'SIGHUP', 'SIGQUIT'];

class LauncherError extends Error {
  constructor(message, { hint = '', exitCode = 1 } = {}) {
    super(message);
    this.hint = hint;
    this.exitCode = exitCode;
  }
}

function readPackageJson(root = PACKAGE_ROOT) {
  return JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
}

/** Locate an executable on PATH without spawning a shell. */
function findExecutable(name, envPath = process.env.PATH || '') {
  for (const dir of envPath.split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, name);
    try {
      const st = fs.statSync(candidate);
      if (st.isFile()) {
        fs.accessSync(candidate, fs.constants.X_OK);
        return candidate;
      }
    } catch {
      // not here
    }
  }
  return null;
}

/** First Python >= 3.10 on PATH, as { path, version } or null. */
function findPython(env = process.env) {
  for (const name of PYTHON_CANDIDATES) {
    const exe = findExecutable(name, env.PATH);
    if (!exe) continue;
    const res = spawnSync(exe, ['-c', 'import sys; print("%d.%d.%d" % sys.version_info[:3])'], {
      encoding: 'utf8',
      env,
      timeout: 15000,
    });
    if (res.status !== 0 || !res.stdout) continue;
    const version = res.stdout.trim();
    const [major, minor] = version.split('.').map(Number);
    if (major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])) {
      return { path: exe, version };
    }
  }
  return null;
}

function missingPackageFiles(root = PACKAGE_ROOT) {
  return REQUIRED_FILES.filter((rel) => !fs.existsSync(path.join(root, rel)));
}

/** Validate forwarded arguments. They are passed as an argv array, never through a shell. */
function validateArgs(args) {
  if (!Array.isArray(args)) throw new LauncherError('Internal error: arguments must be an array', { exitCode: 2 });
  if (args.length > MAX_ARGS) throw new LauncherError(`Too many arguments (${args.length})`, { exitCode: 2 });
  for (const arg of args) {
    if (typeof arg !== 'string') throw new LauncherError('Invalid argument type', { exitCode: 2 });
    if (arg.includes('\0')) throw new LauncherError('Arguments must not contain NUL bytes', { exitCode: 2 });
    if (arg.length > MAX_ARG_LENGTH) throw new LauncherError('An argument is too long', { exitCode: 2 });
  }
  return args;
}

/** Where the installer's private virtualenv lives (user-owned, outside the package). */
function runtimeVenvDir(env = process.env) {
  const xdg = env.XDG_DATA_HOME && path.isAbsolute(env.XDG_DATA_HOME) ? env.XDG_DATA_HOME : null;
  const base = xdg || path.join(env.HOME || os.homedir(), '.local', 'share');
  return path.join(base, 'frappe-setup', 'npm-runtime', 'venv');
}

/**
 * Check what the launcher itself needs. Returns { errors, warnings, python, bash }.
 * Frappe's own prerequisites (MariaDB, Redis, Node for the bench, ...) are checked and
 * installed by the Python installer, with your permission.
 */
function preflight({ platform = process.platform, nodeVersion = process.versions.node, env = process.env, root = PACKAGE_ROOT } = {}) {
  const errors = [];
  const warnings = [];
  if (platform !== 'linux') {
    errors.push({
      message: `Frappe Setup CLI runs on Linux only (detected ${platform}).`,
      hint: 'Use Ubuntu 24.04 x86_64, for example in a virtual machine or container.',
    });
  }
  const nodeMajor = Number(String(nodeVersion).split('.')[0]);
  if (!(nodeMajor >= MIN_NODE_MAJOR)) {
    errors.push({ message: `Node.js ${MIN_NODE_MAJOR}+ is required to run this launcher (found ${nodeVersion}).`, hint: 'Upgrade Node.js.' });
  }
  const missing = missingPackageFiles(root);
  if (missing.length) {
    errors.push({
      message: `The package is incomplete; missing: ${missing.join(', ')}.`,
      hint: 'Reinstall it (npm install -g frappe-setup-cli) or clear the npx cache (npx clear-npx-cache).',
    });
  }
  const bash = findExecutable('bash', env.PATH);
  if (!bash) {
    errors.push({ message: 'bash was not found on PATH.', hint: 'On Ubuntu: sudo apt install bash' });
  }
  const python = platform === 'linux' ? findPython(env) : null;
  if (platform === 'linux' && !python) {
    errors.push({
      message: `Python ${MIN_PYTHON.join('.')}+ was not found on PATH (needed to run the installer).`,
      hint: 'On Ubuntu 24.04 Python 3.12 is preinstalled; otherwise: sudo apt install python3 python3-venv',
    });
  }
  if (!findExecutable('git', env.PATH)) {
    warnings.push('git is not installed; the installer will offer to install it (it needs sudo for that).');
  }
  const isRoot = typeof process.getuid === 'function' && process.getuid() === 0;
  if (!isRoot && !findExecutable('sudo', env.PATH)) {
    warnings.push('sudo is not available; missing system packages cannot be installed automatically.');
  }
  return { errors, warnings, python, bash };
}

function formatProblems(result, write) {
  for (const w of result.warnings) write(`! ${w}\n`);
  for (const e of result.errors) {
    write(`x ${e.message}\n`);
    if (e.hint) write(`  -> ${e.hint}\n`);
  }
}

/**
 * Run the bundled installer. Resolves with the exit code the launcher should exit with.
 * `spawnImpl` and `proc` are injectable for tests.
 */
function runInstaller(args, { root = PACKAGE_ROOT, env = process.env, bash, cwd = process.cwd(), spawnImpl = spawn, proc = process } = {}) {
  validateArgs(args);
  const childEnv = {
    ...env,
    FRAPPE_SETUP_VENV_DIR: env.FRAPPE_SETUP_VENV_DIR || runtimeVenvDir(env),
    FRAPPE_SETUP_INSTALL_MODE: 'copy',
    FRAPPE_SETUP_LAUNCHER: 'npm',
    // npx sets npm_command=exec; messages then say `npx frappe-setup-cli resume`.
    FRAPPE_SETUP_COMMAND: env.npm_command === 'exec' ? 'npx frappe-setup-cli' : 'frappe-setup-cli',
  };
  return new Promise((resolve) => {
    // The installer resolves its own files relative to install.sh; the working directory is
    // the caller's, so relative paths given on the command line (--config, --parent-dir) work.
    const child = spawnImpl(bash || 'bash', [path.join(root, 'install.sh'), ...args], {
      cwd,
      env: childEnv,
      stdio: 'inherit',
    });
    const handlers = {};
    const cleanup = () => {
      for (const [sig, fn] of Object.entries(handlers)) proc.removeListener(sig, fn);
    };
    // Keep the launcher alive on Ctrl+C; the installer receives the terminal's SIGINT itself
    // and decides how to stop (it prints how to resume).
    handlers.SIGINT = () => {};
    for (const sig of FORWARDED_SIGNALS) {
      handlers[sig] = () => {
        try {
          child.kill(sig);
        } catch {
          // child already gone
        }
      };
    }
    for (const [sig, fn] of Object.entries(handlers)) proc.on(sig, fn);

    child.on('error', (err) => {
      cleanup();
      proc.stderr.write(`x Could not start the installer: ${err.message}\n`);
      resolve(1);
    });
    child.on('exit', (code, signal) => {
      cleanup();
      if (signal) {
        const num = os.constants.signals[signal] || 0;
        resolve(128 + num);
      } else {
        resolve(code === null ? 1 : code);
      }
    });
  });
}

async function main(argv = process.argv.slice(2), { env = process.env, proc = process } = {}) {
  const write = (s) => proc.stderr.write(s);
  let pkg;
  try {
    pkg = readPackageJson();
  } catch (err) {
    write(`x Cannot read package.json: ${err.message}\n`);
    return 1;
  }
  try {
    validateArgs(argv);
  } catch (err) {
    write(`x ${err.message}\n`);
    return err.exitCode || 2;
  }
  const result = preflight({ env });
  const pythonText = result.python ? `Python ${result.python.version}` : 'Python not found';
  // Status goes to stderr so machine-readable output (e.g. `check --json`) stays clean on stdout.
  write(`frappe-setup-cli ${pkg.version} (npm launcher) - ${process.platform}/${process.arch}, ${pythonText}\n`);
  if (result.errors.length || result.warnings.length) formatProblems(result, write);
  if (result.errors.length) {
    write('Nothing was changed on this machine.\n');
    return 1;
  }
  return runInstaller(argv, { env, bash: result.bash, proc });
}

module.exports = {
  PACKAGE_ROOT,
  REQUIRED_FILES,
  LauncherError,
  findExecutable,
  findPython,
  missingPackageFiles,
  validateArgs,
  runtimeVenvDir,
  preflight,
  runInstaller,
  main,
};
