#!/usr/bin/env bash
# Frappe Setup CLI bootstrap.
#
#   ./install.sh                 start the guided installer
#   ./install.sh check           read-only compatibility report
#   ./install.sh <command> ...   any frappe-setup command / option (see --help)
#
# This script only prepares a private virtualenv for the CLI. It asks before installing the
# one system package it may need (python3-venv).
#
# Environment (used by the npm launcher; defaults suit a git checkout):
#   FRAPPE_SETUP_VENV_DIR      where the virtualenv lives        (default: <repo>/.venv)
#   FRAPPE_SETUP_INSTALL_MODE  editable | copy                    (default: editable)
#       editable: `pip install -e <repo>` - the checkout is used live (development).
#       copy:     a regular install from a staged copy, so the venv never depends on the
#                 package directory (npm caches and global prefixes may be read-only or pruned).
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${FRAPPE_SETUP_VENV_DIR:-${REPO_DIR}/.venv}"
INSTALL_MODE="${FRAPPE_SETUP_INSTALL_MODE:-editable}"
STAMP="${VENV_DIR}/.frappe-setup-installed"
[[ "${VENV_DIR}" == /* ]] || { printf 'FRAPPE_SETUP_VENV_DIR must be an absolute path\n' >&2; exit 2; }
[[ "${INSTALL_MODE}" == "editable" || "${INSTALL_MODE}" == "copy" ]] \
    || { printf 'FRAPPE_SETUP_INSTALL_MODE must be editable or copy\n' >&2; exit 2; }
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

# (Re)install the CLI when the venv is missing or the installed sources changed.
if [[ "${INSTALL_MODE}" == "copy" ]]; then
    # Hash everything that ends up in the venv, so a new package version reinstalls.
    SOURCE_HASH="$(cd "${REPO_DIR}" && find pyproject.toml src config -type f \( -name '*.py' -o -name '*.yaml' -o -name '*.toml' \) \
        -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
else
    SOURCE_HASH="$(sha256sum < "${REPO_DIR}/pyproject.toml" | cut -d' ' -f1)"
fi
if [[ ! -x "${VENV_DIR}/bin/frappe-setup" || ! -f "${STAMP}" || "$(cat "${STAMP}")" != "${SOURCE_HASH}" ]]; then
    say "Preparing the installer environment in ${VENV_DIR}"
    mkdir -p "$(dirname -- "${VENV_DIR}")"
    "$PYTHON" -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip \
        || die "Could not upgrade pip (network problem?). Rerun when online."
    if [[ "${INSTALL_MODE}" == "copy" ]]; then
        STAGE="$(mktemp -d)"
        cp -R "${REPO_DIR}/pyproject.toml" "${REPO_DIR}/README.md" "${REPO_DIR}/src" "${REPO_DIR}/config" "${STAGE}/"
        [[ -f "${REPO_DIR}/LICENSE" ]] && cp "${REPO_DIR}/LICENSE" "${STAGE}/"
        if ! "${VENV_DIR}/bin/python" -m pip install --quiet --disable-pip-version-check "${STAGE}"; then
            rm -rf -- "${STAGE}"
            die "Could not install the CLI dependencies (network problem?). Rerun when online."
        fi
        rm -rf -- "${STAGE}"
    else
        # Editable install: config/version-matrix.yaml in this checkout is used directly.
        "${VENV_DIR}/bin/python" -m pip install --quiet --disable-pip-version-check -e "${REPO_DIR}" \
            || die "Could not install the CLI dependencies (network problem?). Rerun ./install.sh when online."
    fi
    echo "${SOURCE_HASH}" > "${STAMP}"
fi

# Messages name the command the user actually types (./install.sh resume, ...).
export FRAPPE_SETUP_COMMAND="${FRAPPE_SETUP_COMMAND:-./install.sh}"
exec "${VENV_DIR}/bin/frappe-setup" "$@"
