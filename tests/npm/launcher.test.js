'use strict';
// Tests for the npm launcher. The real installer is never run here: end-to-end runs use a fake
// package root whose install.sh only records what it received.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const launcher = require('../../lib/launcher');

const REPO = path.resolve(__dirname, '..', '..');
const PKG = JSON.parse(fs.readFileSync(path.join(REPO, 'package.json'), 'utf8'));

function tmpdir(prefix = 'fsc-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function writeExe(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
  fs.chmodSync(file, 0o755);
}

// A fake installer: prints its argv as JSON plus cwd/env, echoes one stdin line on request,
// handles signals like the real CLI (exit 130 on INT, 143 on TERM) and exits with FAKE_EXIT.
const FAKE_INSTALL_SH = `#!/usr/bin/env bash
trap 'echo "installer: got SIGINT" >&2; exit 130' INT
trap 'echo "installer: got SIGTERM" >&2; exit 143' TERM
python3 - "$@" <<'PY'
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
  "mode": os.environ.get("FRAPPE_SETUP_INSTALL_MODE"), "venv": os.environ.get("FRAPPE_SETUP_VENV_DIR"),
  "launcher": os.environ.get("FRAPPE_SETUP_LAUNCHER"), "command": os.environ.get("FRAPPE_SETUP_COMMAND")}))
PY
echo "to-stderr" >&2
if [[ -n "$FAKE_READ" ]]; then read -r line; echo "read:$line"; fi
if [[ -n "$FAKE_WAIT" ]]; then echo "ready"; while true; do sleep 0.1; done; fi
exit "\${FAKE_EXIT:-0}"
`;

function fakePackage({ omit = [] } = {}) {
  const root = tmpdir('fsc-pkg-');
  fs.cpSync(path.join(REPO, 'bin'), path.join(root, 'bin'), { recursive: true });
  fs.cpSync(path.join(REPO, 'lib'), path.join(root, 'lib'), { recursive: true });
  fs.copyFileSync(path.join(REPO, 'package.json'), path.join(root, 'package.json'));
  for (const rel of launcher.REQUIRED_FILES) {
    if (omit.includes(rel)) continue;
    const file = path.join(root, rel);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    if (rel === 'install.sh') writeExe(file, FAKE_INSTALL_SH);
    else fs.writeFileSync(file, '# stub\n');
  }
  return root;
}

function runBin(root, args, { env = {}, input, cwd } = {}) {
  const home = tmpdir('fsc-home-');
  return spawnSync(process.execPath, [path.join(root, 'bin', 'frappe-setup-cli.js'), ...args], {
    cwd: cwd || tmpdir('fsc-cwd-'),
    env: { ...process.env, HOME: home, XDG_DATA_HOME: '', ...env },
    input,
    encoding: 'utf8',
    timeout: 60000,
  });
}

function firstJson(stdout) {
  return JSON.parse(stdout.split('\n').find((l) => l.startsWith('{')));
}

// ------------------------------------------------------------------ package metadata
test('package.json metadata and executable registration', () => {
  assert.equal(PKG.name, 'frappe-setup-cli');
  assert.match(PKG.version, /^\d+\.\d+\.\d+$/);
  assert.deepEqual(PKG.bin, { 'frappe-setup-cli': 'bin/frappe-setup-cli.js' });
  assert.equal(PKG.license, 'MIT');
  assert.ok(PKG.engines && PKG.engines.node);
  assert.equal(PKG.dependencies, undefined, 'the launcher must not need runtime dependencies');
  const binFile = path.join(REPO, PKG.bin['frappe-setup-cli']);
  assert.ok(fs.readFileSync(binFile, 'utf8').startsWith('#!/usr/bin/env node\n'));
  fs.accessSync(binFile, fs.constants.X_OK);
  for (const entry of ['bin/', 'lib/', 'install.sh', 'pyproject.toml', 'config/*.yaml']) {
    assert.ok(PKG.files.includes(entry), `files allowlist misses ${entry}`);
  }
});

test('npm and Python versions match', () => {
  const pyproject = fs.readFileSync(path.join(REPO, 'pyproject.toml'), 'utf8');
  const init = fs.readFileSync(path.join(REPO, 'src', 'frappe_setup', '__init__.py'), 'utf8');
  assert.match(pyproject, new RegExp(`^version = "${PKG.version.replace(/\./g, '\\.')}"$`, 'm'));
  assert.match(init, new RegExp(`__version__ = "${PKG.version.replace(/\./g, '\\.')}"`));
});

test('the real package root contains every file the installer needs', () => {
  assert.deepEqual(launcher.missingPackageFiles(REPO), []);
  assert.equal(launcher.PACKAGE_ROOT, REPO);
  assert.deepEqual(launcher.missingPackageFiles(tmpdir()), launcher.REQUIRED_FILES);
});

// ------------------------------------------------------------------ unit helpers
test('findExecutable and findPython search PATH without a shell', () => {
  const dir = tmpdir();
  writeExe(path.join(dir, 'python3'), '#!/bin/sh\necho 3.9.18\n');
  assert.equal(launcher.findExecutable('python3', dir), path.join(dir, 'python3'));
  assert.equal(launcher.findExecutable('nope', dir), null);
  assert.equal(launcher.findPython({ PATH: dir }), null, 'Python 3.9 is too old');
  writeExe(path.join(dir, 'python3.12'), '#!/bin/sh\necho 3.12.3\n');
  assert.deepEqual(launcher.findPython({ PATH: dir }), { path: path.join(dir, 'python3.12'), version: '3.12.3' });
});

test('argument validation', () => {
  assert.deepEqual(launcher.validateArgs(['install', '--site', 'a b']), ['install', '--site', 'a b']);
  assert.throws(() => launcher.validateArgs(['a\0b']), /NUL/);
  assert.throws(() => launcher.validateArgs([1]), /type/);
  assert.throws(() => launcher.validateArgs(new Array(500).fill('x')), /Too many/);
  assert.throws(() => launcher.validateArgs(['x'.repeat(5000)]), /too long/);
});

test('runtime venv lives in the user data dir, never in the package', () => {
  assert.equal(launcher.runtimeVenvDir({ HOME: '/home/u' }), '/home/u/.local/share/frappe-setup/npm-runtime/venv');
  assert.equal(launcher.runtimeVenvDir({ HOME: '/home/u', XDG_DATA_HOME: '/data' }), '/data/frappe-setup/npm-runtime/venv');
  assert.equal(launcher.runtimeVenvDir({ HOME: '/home/u', XDG_DATA_HOME: 'relative' }), '/home/u/.local/share/frappe-setup/npm-runtime/venv');
});

test('preflight: unsupported OS, old Node, missing bash/python, missing git', () => {
  const empty = tmpdir();
  const r1 = launcher.preflight({ platform: 'darwin', env: { PATH: process.env.PATH } });
  assert.ok(r1.errors.some((e) => /Linux only/.test(e.message)));
  const r2 = launcher.preflight({ nodeVersion: '16.20.0', env: { PATH: process.env.PATH } });
  assert.ok(r2.errors.some((e) => /Node\.js 18\+/.test(e.message)));
  const r3 = launcher.preflight({ platform: 'linux', env: { PATH: empty } });
  assert.ok(r3.errors.some((e) => /bash was not found/.test(e.message)));
  assert.ok(r3.errors.some((e) => /Python 3\.10\+ was not found/.test(e.message)));
  assert.ok(r3.warnings.some((w) => /git is not installed/.test(w)));
});

// ------------------------------------------------------------------ end-to-end through bin/
test('forwards arguments verbatim, sets the caller cwd and the npm runtime environment', () => {
  const root = fakePackage();
  const cwd = tmpdir('fsc-caller-');
  const args = ['install', '--site', 'my site.localhost', '$(touch /tmp/pwned)', "a'b\"c", '--yes'];
  const res = runBin(root, args, { cwd });
  assert.equal(res.status, 0, res.stderr);
  const out = firstJson(res.stdout);
  assert.deepEqual(out.argv, args);
  assert.equal(fs.realpathSync(out.cwd), fs.realpathSync(cwd));
  assert.equal(out.mode, 'copy');
  assert.equal(out.launcher, 'npm');
  assert.match(out.venv, /\/frappe-setup\/npm-runtime\/venv$/);
  assert.ok(!out.venv.startsWith(root), 'venv must not be inside the package');
  assert.match(res.stderr, /frappe-setup-cli \d+\.\d+\.\d+ \(npm launcher\)/);
  assert.match(res.stderr, /to-stderr/);
  assert.ok(!fs.existsSync('/tmp/pwned') || fs.statSync('/tmp/pwned').mtimeMs < Date.now() - 60000);
});

test('messages name the command the user typed (npx vs global install)', () => {
  const root = fakePackage();
  assert.equal(firstJson(runBin(root, [], { env: { npm_command: '' } }).stdout).command, 'frappe-setup-cli');
  assert.equal(firstJson(runBin(root, [], { env: { npm_command: 'exec' } }).stdout).command, 'npx frappe-setup-cli');
});

test('stdin is forwarded to the installer', () => {
  const res = runBin(fakePackage(), [], { env: { FAKE_READ: '1' }, input: 'hello-from-stdin\n' });
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stdout, /read:hello-from-stdin/);
});

test('installer exit codes are propagated', () => {
  const root = fakePackage();
  for (const code of [0, 1, 2, 3, 42]) {
    assert.equal(runBin(root, [], { env: { FAKE_EXIT: String(code) } }).status, code);
  }
});

test('missing Python stops before the installer runs', () => {
  const root = fakePackage();
  const onlyBash = tmpdir('fsc-path-');
  fs.symlinkSync(launcher.findExecutable('bash'), path.join(onlyBash, 'bash'));
  const res = runBin(root, [], { env: { PATH: onlyBash } });
  assert.equal(res.status, 1);
  assert.match(res.stderr, /Python 3\.10\+ was not found/);
  assert.match(res.stderr, /Nothing was changed/);
  assert.doesNotMatch(res.stdout, /argv/, 'the installer must not have been started');
});

test('an incomplete package is reported with a fix', () => {
  const res = runBin(fakePackage({ omit: ['config/version-matrix.yaml'] }), []);
  assert.equal(res.status, 1);
  assert.match(res.stderr, /package is incomplete; missing: config\/version-matrix\.yaml/);
});

test('invalid arguments are rejected before anything runs', () => {
  // NUL cannot travel through argv, so exercise validation through main() directly.
  const writes = [];
  const proc = { stderr: { write: (s) => writes.push(s) } };
  return launcher.main(['ok', 'x'.repeat(5000)], { proc }).then((code) => {
    assert.equal(code, 2);
    assert.match(writes.join(''), /too long/);
  });
});

function spawnWaiting(root, { detached }) {
  const child = spawn(process.execPath, [path.join(root, 'bin', 'frappe-setup-cli.js')], {
    cwd: tmpdir(),
    env: { ...process.env, HOME: tmpdir('fsc-home-'), FAKE_WAIT: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached,
  });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (d) => (stdout += d));
  child.stderr.on('data', (d) => (stderr += d));
  const ready = new Promise((resolve, reject) => {
    const timer = setInterval(() => {
      if (stdout.includes('ready')) {
        clearInterval(timer);
        resolve();
      }
    }, 50);
    setTimeout(() => {
      clearInterval(timer);
      reject(new Error(`installer not ready: ${stderr}`));
    }, 20000);
  });
  const exited = new Promise((resolve) => child.on('exit', (code, signal) => resolve({ code, signal, out: () => ({ stdout, stderr }) })));
  return { child, ready, exited };
}

test('Ctrl+C (SIGINT to the process group) stops the installer and returns its code', async () => {
  const { child, ready, exited } = spawnWaiting(fakePackage(), { detached: true });
  await ready;
  process.kill(-child.pid, 'SIGINT'); // what a terminal does on Ctrl+C
  const { code, out } = await exited;
  assert.equal(code, 130);
  assert.match(out().stderr, /installer: got SIGINT/);
});

test('SIGTERM sent to the launcher only is forwarded to the installer', async () => {
  const { child, ready, exited } = spawnWaiting(fakePackage(), { detached: false });
  await ready;
  child.kill('SIGTERM');
  const { code, out } = await exited;
  assert.equal(code, 143);
  assert.match(out().stderr, /installer: got SIGTERM/);
});

// ------------------------------------------------------------------ npm package contents
test('npm pack contains the runtime files and nothing private', () => {
  const res = spawnSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], {
    cwd: REPO,
    encoding: 'utf8',
    env: { ...process.env, npm_config_cache: tmpdir('fsc-npmcache-') },
    timeout: 120000,
  });
  assert.equal(res.status, 0, res.stderr);
  const files = JSON.parse(res.stdout)[0].files.map((f) => f.path);
  for (const rel of [...launcher.REQUIRED_FILES, 'bin/frappe-setup-cli.js', 'lib/launcher.js', 'LICENSE', 'package.json']) {
    assert.ok(files.includes(rel), `missing from package: ${rel}`);
  }
  const pySources = fs.readdirSync(path.join(REPO, 'src', 'frappe_setup')).filter((f) => f.endsWith('.py'));
  for (const f of pySources) assert.ok(files.includes(`src/frappe_setup/${f}`), `missing ${f}`);
  const forbidden = /(^|\/)(\.git|\.venv|venv|node_modules|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|tests)(\/|$)|\.(pyc|log|pw|secret)$|^\.env/;
  const bad = files.filter((f) => forbidden.test(f));
  assert.deepEqual(bad, [], `unexpected files in package: ${bad.join(', ')}`);
});
