#!/usr/bin/env bash
# Frappe Setup CLI bootstrap.
#
#   ./install.sh                 start the guided installer
#   ./install.sh check           read-only compatibility report
#   ./install.sh <command> ...   any frappe-setup command / option (see --help)
#
# This script only prepares a private virtualenv (.venv) for the CLI inside this
# repository. It asks before installing the one system package it may need (python3-venv).
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${REPO_DIR}/.venv"
STAMP="${VENV_DIR}/.frappe-setup-installed"
MIN_PY_MINOR=10

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "Frappe Setup CLI supports Linux only (detected $(uname -s))."
if [[ "${EUID}" -eq 0 && " $* " != *" --allow-root "* ]]; then
    die "Do not run as root. Use a regular user with sudo rights (pass --allow-root only in disposable containers)."
fi

find_python() {
    local candidate
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, ${MIN_PY_MINOR}) else 1)" 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON="$(find_python)" || die "Python 3.${MIN_PY_MINOR}+ is required to run the installer. On Ubuntu: sudo apt install python3"

venv_works() {
    local tmp
    tmp="$(mktemp -d)"
    if "$PYTHON" -m venv "$tmp/v" >/dev/null 2>&1; then rm -rf "$tmp"; return 0; fi
    rm -rf "$tmp"
    return 1
}

if [[ ! -x "${VENV_DIR}/bin/python" ]] && ! venv_works; then
    PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    PKG="python${PYVER}-venv"
    warn "Python's venv module is not available (package ${PKG} is missing)."
    command -v apt-get >/dev/null 2>&1 || die "Install the venv module for ${PYTHON} and rerun ./install.sh"
    SUDO=(sudo)
    [[ "${EUID}" -eq 0 ]] && SUDO=()
    answer="n"
    if [[ " $* " == *" --yes "* || " $* " == *" -y "* ]]; then
        say "--yes given: installing ${PKG} with: ${SUDO[*]} apt-get install -y ${PKG}"
        answer="y"
    elif [[ -t 0 ]]; then
        read -r -p "Install ${PKG} now with: ${SUDO[*]} apt-get install -y ${PKG} ? [y/N] " answer
    fi
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        "${SUDO[@]}" apt-get -o Acquire::Retries=5 update
        "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y "${PKG}" \
            || die "Installing ${PKG} failed. If a download keeps failing, your network may block plain-HTTP
   mirrors: switch /etc/apt/sources.list.d/ubuntu.sources to an https:// mirror, run 'sudo apt-get update', retry."
    else
        die "Install it yourself (sudo apt-get install ${PKG}) and rerun ./install.sh"
    fi
fi

# (Re)install the CLI when the venv is missing or the project metadata changed.
SOURCE_HASH="$(cat "${REPO_DIR}/pyproject.toml" | sha256sum | cut -d' ' -f1)"
if [[ ! -x "${VENV_DIR}/bin/frappe-setup" || ! -f "${STAMP}" || "$(cat "${STAMP}")" != "${SOURCE_HASH}" ]]; then
    say "Preparing the installer environment in ${VENV_DIR}"
    "$PYTHON" -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip \
        || die "Could not upgrade pip (network problem?). Rerun ./install.sh when online."
    # Editable install: config/version-matrix.yaml in this checkout is used directly.
    "${VENV_DIR}/bin/python" -m pip install --quiet --disable-pip-version-check -e "${REPO_DIR}" \
        || die "Could not install the CLI dependencies (network problem?). Rerun ./install.sh when online."
    echo "${SOURCE_HASH}" > "${STAMP}"
fi

exec "${VENV_DIR}/bin/frappe-setup" "$@"
