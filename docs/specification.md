# Frappe Setup CLI — Specification

Status: implemented (see "Verification status" at the end for what was actually tested).

## 1. Problem

Installing a Frappe development environment requires a branch-specific combination of
Python, Node.js, Yarn, MariaDB (or PostgreSQL), Redis, system libraries, and the Bench CLI,
followed by several Bench commands that each fail in their own way. Requirements differ
between framework branches (for example v15 runs on Python 3.10–3.14 and Node 18+, while
v16 needs Python 3.14 and Node 24), and the failure messages rarely point at the cause.

## 2. Goals

1. One documented entry point (`./install.sh`) that bootstraps the tool and starts a guided wizard.
2. Detect the machine and every prerequisite **before** changing anything.
3. Resolve the requested Frappe branch and every requested app branch against the real
   upstream repositories before any long-running step.
4. Install only what is missing or incompatible, only with explicit authorization, and
   prefer user-space, isolated installs over system-wide changes.
5. Create a bench and a site, install the selected apps in dependency order, and verify the
   result (including an HTTP ping of the running site when authorized).
6. Fail with an actionable report; make every phase idempotent and the run resumable.

## 3. Non-goals

- Production deployment (nginx, supervisor, TLS, `bench setup production`).
- Operating systems other than Ubuntu 24.04 / 26.04 x86-64 are **not claimed** as supported (the code is
  structured so that other Debian-family releases can be added once tested).
- Managing an existing production bench.

## 4. Requirements

### 4.1 Functional

| ID | Requirement |
|----|-------------|
| F1 | `frappe-setup` (no arguments) launches the interactive wizard. |
| F2 | `check` reports OS, architecture, CPU features, RAM, disk, Python, pip, Node, npm, Yarn, MariaDB/PostgreSQL, Redis, build tools, existing benches, user permissions and ports; it does not modify the machine. |
| F3 | The wizard collects bench name, parent directory, Frappe branch, site name, database engine and credentials, Administrator password, and applications, validating each value. |
| F4 | Frappe branches come from `config/version-matrix.yaml`; each is resolved with `git ls-remote` against the official repository. An unavailable branch is an error, never silently substituted. |
| F5 | For the resolved branch the tool reads `requires-python` (pyproject.toml), `engines.node` (package.json) and `__version__` directly from upstream and cross-checks them against the matrix. |
| F6 | Apps: none, ERPNext, selected official apps (from a catalog of real repositories), or custom Git URLs. Every app branch is verified with `git ls-remote`; app name, `required_apps` and `[tool.bench.frappe-dependencies]` are read from upstream before installation; the install order is a topological sort of declared dependencies. |
| F7 | Dependency installers detect, version-check, install (only if missing/incompatible), and verify: system packages (apt), Python interpreter (existing or uv-managed), Node.js (existing or checksum-verified official tarball in user space), Yarn (user-space prefix), Bench CLI (`uv tool install frappe-bench`), MariaDB server (Ubuntu archive or the official MariaDB repository when the branch requires a newer series), Redis, and DB superuser password authentication. |
| F8 | Bench: `bench init` with the selected branch and Python, then structural validation (apps/frappe, env, sites, Procfile, frappe importable from the env at the expected version, node_modules present, assets built). |
| F9 | Site: DB connectivity check, `bench new-site` (never `--force`), apps installed in order, site verified with `list-apps`; optionally the bench is started and `/api/method/ping` must answer `pong`. |
| F10 | `status`, `doctor`, `resume`, `logs` commands as documented in the README. |
| F11 | Non-interactive mode: every value from flags or a YAML config file; secrets only from files or environment variables, never from flags. |

### 4.2 Safety and security

| ID | Requirement |
|----|-------------|
| S1 | No secret is ever placed on a command line, written to state files, or written to logs. Secrets reach Bench through a PTY answering Frappe's own `getpass` prompts. |
| S2 | Every log line and every displayed command passes through a redactor (known secret values + patterns for passwords, tokens, and URL credentials). |
| S3 | `sudo` is used only after the exact commands have been shown and authorized (interactive confirmation or `--yes`). |
| S4 | No `shell=True`; all subprocesses use argument vectors; URLs are passed after `--`; user input is validated before use. |
| S5 | Never delete or overwrite an existing bench, site, database, app or user file. `bench new-site --force` and `bench get-app --overwrite` are never used. |
| S6 | Cleanup only applies to resources recorded as created by this tool, and only after explicit authorization; the default cleanup action is to rename (quarantine), not delete. |
| S7 | System Python and system Node.js are never replaced or downgraded. |
| S8 | Running as root is refused unless `--allow-root` is given (e.g. disposable containers). |
| S9 | Firewall, AppArmor/SELinux and DB authentication are never disabled. Enabling password authentication for the DB superuser keeps `unix_socket` authentication in place. |

### 4.3 Reliability

| ID | Requirement |
|----|-------------|
| R1 | Each phase records its status in a per-bench state file; reruns skip completed, still-valid phases. |
| R2 | Ctrl-C terminates the running child process group, marks the phase `interrupted`, and prints the `resume` command. |
| R3 | Network-only operations (ls-remote, metadata probes, downloads) are retried with backoff; long builds are not retried automatically. |
| R4 | Every failure reports: phase, redacted command, exit code, log file, likely cause (when evidenced by output), remedy, and whether retrying is safe. |

## 5. Acceptance criteria

1. `./install.sh` on a clean Ubuntu 24.04 x86-64 machine (non-root user with sudo) bootstraps the CLI and starts the wizard.
2. Invalid names, paths, URLs and branches are rejected with a reason.
3. `check` lists missing prerequisites and makes no changes.
4. Unsupported OS/arch/branch combinations stop before any change, with remediation.
5. No privileged command runs without authorization.
6. A Frappe-only bench and site are created and the site answers `/api/method/ping` (tested in a disposable container).
7. ERPNext or other valid apps can be installed in dependency order.
8. Final output gives the exact commands to start the bench and the URL.
9. Failures never delete unrelated data.
10. An interrupted run can be resumed or rerun without duplicating sites or overwriting configuration.
11. The automated unit test suite passes.
12. Documentation distinguishes tested from untested behaviour.

## 6. Architecture

See [architecture.md](architecture.md).

## 7. Compatibility strategy

- `config/version-matrix.yaml` records, per branch, the **documented** requirements (official
  docs table + upstream `pyproject.toml` / `package.json`), the versions upstream CI tests
  with, the versions this tool installs when something is missing, and — separately — the
  combinations **tested by this project**.
- At run time the upstream files of the resolved branch are read via a shallow, blob-less,
  no-checkout git clone (`git show HEAD:<file>`). This works where raw.githubusercontent.com is
  blocked and is not subject to GitHub API rate limits. If upstream disagrees with the matrix,
  the stricter constraint wins and a warning is shown.
- Install targets: an existing compatible interpreter/runtime is always preferred.
  Otherwise Python comes from `uv python install` (user space) and Node.js from the official
  nodejs.org tarball, verified against `SHASUMS256.txt`, unpacked under
  `~/.local/share/frappe-setup/`. A shell snippet that puts these on `PATH` is generated per bench.

## 8. Assumptions and open questions

- A1. Ubuntu 24.04 ships MariaDB 10.11. v15 documents ≥10.6.6 (Frappe prints an "untested
  above 10.8" warning but works). v16 documents 11.8; the tool therefore offers the official
  MariaDB 11.8 repository when MariaDB is not yet installed, and refuses (with an explicit
  `--allow-db-version-mismatch` override) when an older server is already installed, because
  upgrading a user's existing database server is out of scope.
- A2. PostgreSQL support is limited to Frappe-only sites against an already-running server;
  it is marked experimental and was not tested end-to-end.
- A3. wkhtmltopdf (PDF generation for v15/v16) is optional; Ubuntu's package is built without
  patched Qt. v17/develop uses Chromium managed by Bench.
- A4. RAM/disk thresholds are based on measured container installs (see supported-platforms.md).
