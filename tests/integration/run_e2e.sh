#!/usr/bin/env bash
# End-to-end test in a disposable Ubuntu 24.04 container (never touches the host system).
#
#   tests/integration/run_e2e.sh <frappe-branch> [extra install args...]
#   e.g. tests/integration/run_e2e.sh version-15
#        tests/integration/run_e2e.sh version-16 --erpnext
#
# Steps inside the container (as a non-root sudo user):
#   1. copy the repository (simulates `git clone`)
#   2. ./install.sh install --non-interactive --yes ...   (fresh machine: installs everything)
#   3. checks: site answers /api/method/ping (done by the installer), status, doctor
#   4. reruns the same install to prove idempotency (no site/bench recreated)
#   5. scans every log/state file for the secrets
# Artifacts (logs, state, reports) are copied to tests/integration/.artifacts/<name>/.
set -euo pipefail

BRANCH="${1:?usage: run_e2e.sh <frappe-branch> [install args...]}"
shift || true
EXTRA_ARGS=("$@")
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="frappe-setup-e2e:ubuntu-24.04"
NAME="frappe-setup-e2e-${BRANCH}-$(date +%s)"
ART="${REPO}/tests/integration/.artifacts/${NAME}"
mkdir -p "${ART}"

docker build -q --build-arg "APT_MIRROR=${E2E_APT_MIRROR:-}" -t "${IMAGE}" "${REPO}/tests/integration" >/dev/null

cleanup() {
    docker cp "${NAME}:/home/dev/.local/state/frappe-setup" "${ART}/state" >/dev/null 2>&1 || true
    docker cp "${NAME}:/home/dev/report.json" "${ART}/doctor-report.json" >/dev/null 2>&1 || true
    if [[ "${KEEP_CONTAINER:-0}" != "1" ]]; then docker rm -f "${NAME}" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

docker run -d --name "${NAME}" -v "${REPO}:/src:ro" "${IMAGE}" sleep infinity >/dev/null

# Secrets are generated per run, written to 0600 files, and never passed as arguments.
ADMIN_PW="Adm-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
DB_PW="Db-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
docker exec -i "${NAME}" bash -c 'umask 077; cat > ~/admin.pw' <<<"${ADMIN_PW}"
docker exec -i "${NAME}" bash -c 'umask 077; cat > ~/db.pw' <<<"${DB_PW}"

docker exec "${NAME}" bash -c 'mkdir -p ~/frappe-setup-cli && cd /src && tar --exclude=./.venv --exclude=./tests/integration/.artifacts -cf - . | tar -xf - -C ~/frappe-setup-cli'

run_install() {
    docker exec "${NAME}" bash -c "cd ~/frappe-setup-cli && ./install.sh install --non-interactive --yes \
        --frappe-branch ${BRANCH} --site dev.localhost \
        --admin-password-file ~/admin.pw --db-root-password-file ~/db.pw ${EXTRA_ARGS[*]:-}"
}

echo "== [1/4] fresh install (${BRANCH} ${EXTRA_ARGS[*]:-})"
start=$(date +%s)
run_install 2>&1 | tee "${ART}/install-1.txt"
echo "first install took $(( $(date +%s) - start ))s" | tee -a "${ART}/summary.txt"

echo "== [2/4] status / doctor"
docker exec "${NAME}" bash -c 'cd ~/frappe-setup-cli && .venv/bin/frappe-setup status && .venv/bin/frappe-setup doctor --share ~/report.json' 2>&1 | tee "${ART}/status-doctor.txt"

echo "== [3/4] rerun (must be idempotent)"
start=$(date +%s)
run_install 2>&1 | tee "${ART}/install-2.txt"
echo "second install took $(( $(date +%s) - start ))s" | tee -a "${ART}/summary.txt"
grep -q "already exists" "${ART}/install-2.txt" || { echo "FAIL: rerun did not reuse the site"; exit 1; }

echo "== [4/4] secret scan"
if docker exec "${NAME}" bash -c "grep -rlF -e '${ADMIN_PW}' -e '${DB_PW}' ~/.local/state/frappe-setup ~/frappe-setup-cli ~/report.json 2>/dev/null"; then
    echo "FAIL: a secret was found in the files listed above"; exit 1
fi
if grep -rlF -e "${ADMIN_PW}" -e "${DB_PW}" "${ART}"; then echo "FAIL: secret in console output"; exit 1; fi
echo "no secrets found in logs, state, reports or console output" | tee -a "${ART}/summary.txt"

docker exec "${NAME}" bash -c 'du -sh ~/frappe-projects/frappe-bench ~/.local/share/frappe-setup ~/.local/share/uv 2>/dev/null; df -h / | tail -1' 2>&1 | tee -a "${ART}/summary.txt"
echo "PASS: ${BRANCH} ${EXTRA_ARGS[*]:-}" | tee -a "${ART}/summary.txt"
