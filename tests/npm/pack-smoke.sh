#!/usr/bin/env bash
# Build the npm tarball and run it the two ways users will: `npm install -g` and `npx`.
# Everything happens in a temporary directory with its own HOME, npm prefix and npm cache, outside
# this repository; the real Python bootstrap runs (it needs network access to PyPI).
# Only read-only installer commands are executed (--version, --help, check, status, logs).
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf -- "${WORK}"' EXIT
export HOME="${WORK}/home" npm_config_cache="${WORK}/npm-cache" npm_config_update_notifier=false
unset XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME
mkdir -p "${HOME}" "${WORK}/elsewhere"

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

step "npm pack"
TARBALL="$(cd "${REPO}" && npm pack --pack-destination "${WORK}" --silent)"
TARBALL="${WORK}/${TARBALL}"
VERSION="$(cd "${REPO}" && node -p 'require("./package.json").version')"
tar -tzf "${TARBALL}" | sed 's/^/  /' | head -60
tar -tzf "${TARBALL}" | grep -Eq '(^|/)(tests|\.venv|__pycache__|\.git)/' && fail "tarball contains development files"

step "global install from the tarball (isolated prefix)"
npm install --global --prefix "${WORK}/prefix" "${TARBALL}" --no-audit --no-fund --loglevel=error
BIN="${WORK}/prefix/bin/frappe-setup-cli"
[[ -x "${BIN}" ]] || fail "frappe-setup-cli was not linked into the prefix bin directory"
cd "${WORK}/elsewhere"

step "global: --version (bootstraps the Python runtime in \$HOME, outside the package)"
OUT="$("${BIN}" --version)"
echo "${OUT}"
[[ "${OUT}" == *"frappe-setup ${VERSION}"* ]] || fail "unexpected version output"
[[ -x "${HOME}/.local/share/frappe-setup/npm-runtime/venv/bin/frappe-setup" ]] || fail "runtime venv not created in HOME"
[[ ! -e "${WORK}/prefix/lib/node_modules/frappe-setup-cli/.venv" ]] || fail "a venv was created inside the package"
[[ -z "$(ls -A "${WORK}/elsewhere")" ]] || fail "files were written to the caller's directory"

step "global: help lists all commands"
HELP="$("${BIN}" --help)"
for cmd in check install status doctor resume logs; do [[ "${HELP}" == *"${cmd}"* ]] || fail "help misses ${cmd}"; done

step "global: check --offline --json (read-only, JSON stays clean on stdout)"
"${BIN}" check --offline --json -b version-15 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  version-15 requirements:", d["version-15"]["requirements"])'

step "global: status and logs --path"
"${BIN}" status
"${BIN}" logs --path

step "global: invalid option exits non-zero"
if "${BIN}" install --non-interactive --bench-name '../escape' >/dev/null 2>&1; then fail "invalid bench name was accepted"; fi

step "npx from the tarball (same runtime venv is reused)"
# Equivalent of `npx frappe-setup-cli` once published; --package points npx at the local tarball.
OUT="$(npx --yes --package "${TARBALL}" frappe-setup-cli --version)"
echo "${OUT}"
[[ "${OUT}" == *"frappe-setup ${VERSION}"* ]] || fail "npx run printed unexpected output"
OUT="$(npx --yes --package "${TARBALL}" frappe-setup-cli status 2>&1)"
echo "${OUT}"
[[ "${OUT}" == *"npx frappe-setup-cli install"* ]] || fail "messages should name the npx command"

printf '\nPASS: npm tarball %s works via global install and npx\n' "$(basename "${TARBALL}")"
