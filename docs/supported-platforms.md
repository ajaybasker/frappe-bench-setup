# Supported platforms

## Status

| Platform | Architecture | Status | Meaning |
|----------|--------------|--------|---------|
| Ubuntu 24.04 LTS | x86_64 | **supported** | Tested end-to-end (see below) |
| Ubuntu 24.04 LTS | aarch64 | untested | Refused unless `--allow-untested-platform` |
| Debian 12 / 13 | x86_64, aarch64 | untested | Refused unless `--allow-untested-platform` |
| everything else | — | unsupported | Always refused (use an Ubuntu 24.04 VM or container) |

Status comes from the `platforms:` list in [config/version-matrix.yaml](../config/version-matrix.yaml).
A platform moves to `supported` only after `tests/integration/run_e2e.sh` passes on it.

## Verified end-to-end runs

All runs: disposable `ubuntu:24.04` Docker container (Ubuntu 24.04.5), non-root user with sudo, no
systemd, starting from only `python3`, `git`, `sudo`, `ca-certificates`. Recorded in the matrix
`tested:` section.

| Date | Frappe | Apps | Python | Node.js | MariaDB | Result |
|------|--------|------|--------|---------|---------|--------|
| 2026-10-23 | version-16 (16.53.0) | frappe | 3.14.9 (uv) | 24.22.1 (managed) | 11.8.9 (MariaDB repo) | PASS — fresh install 377 s, ping = pong, rerun idempotent 15 s, no secrets leaked |
| 2026-10-23 | version-15 (15.124.0) | frappe | 3.12.3 (Ubuntu) | 20.20.2 (managed) | 10.11.14 (Ubuntu) | PASS via `resume` after a first-run bug (fixed); ping = pong, rerun 20 s, doctor clean, no secrets leaked |

Measured footprint (Frappe only): bench ≈ 1.2–1.3 GB, managed Node/Yarn/uv tools ≈ 230–270 MB,
uv Python ≈ 70–180 MB. The matrix minimum of 4 GB free disk includes headroom for packages and builds.

## Not verified

- A real (non-container) Ubuntu 24.04 machine with systemd — the systemd code path for starting
  MariaDB is implemented but was not exercised (containers have no systemd).
- The interactive wizard on a fresh machine (the wizard itself was exercised on a provisioned
  machine through a real PTY up to the final confirmation).
- `develop` branch end-to-end.
- PostgreSQL.
- wkhtmltopdf installation (`--wkhtmltopdf`).
- Debian, aarch64.

## Notes for Ubuntu 24.04

- Ubuntu's MariaDB is 10.11: fine for v15 (Frappe prints an informational "untested above 10.8" warning);
  v16/develop document 11.8, so on a machine without MariaDB the official 11.8 repository is used.
- Ubuntu's Python 3.12 satisfies v15; v16/develop need 3.14, installed with uv in user space.
- Ubuntu's `nodejs` package (18.x) satisfies v15's `>=18` only; the tool installs Node 20 (v15) or
  24 (v16+) in user space when no compatible Node is found.
- Some networks block individual files from Ubuntu mirrors over plain HTTP; see troubleshooting.
