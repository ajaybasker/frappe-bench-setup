# Frappe Setup CLI

**One command to get a working, verified Frappe Framework development environment.**

```bash
git clone https://github.com/ORG/frappe-setup-cli.git
cd frappe-setup-cli
./install.sh
```

That's it. `./install.sh` prepares the installer, then a guided wizard does the rest: it checks your
machine, installs what's missing (it asks before using `sudo`), creates the bench and site, installs the
apps you picked, and checks that the site actually answers in a browser. At the end it prints the exact
commands to start working:

```bash
source ~/.local/share/frappe-setup/env/frappe-bench-<id>.sh   # path is printed by the installer
cd ~/frappe-projects/frappe-bench
bench start
# open http://localhost:8000  →  login: Administrator / the password you chose
```

> Replace `ORG` with the GitHub organisation that hosts this repository.

---

## Contents

1. [What it does](#what-it-does)
2. [Supported environments](#supported-environments)
3. [Prerequisites](#prerequisites)
4. [The wizard, step by step](#the-wizard-step-by-step)
5. [Choosing a Frappe version](#choosing-a-frappe-version)
6. [Bench and site configuration](#bench-and-site-configuration)
7. [ERPNext, official and custom apps](#erpnext-official-and-custom-apps)
8. [After installation: everyday Bench commands](#after-installation-everyday-bench-commands)
9. [All frappe-setup commands](#all-frappe-setup-commands)
10. [Non-interactive / advanced usage](#non-interactive--advanced-usage)
11. [Logs, resume and recovery](#logs-resume-and-recovery)
12. [Troubleshooting](#troubleshooting)
13. [Security](#security)
14. [Development and testing](#development-and-testing)
15. [Known limitations](#known-limitations)
16. [Contributing](#contributing)

---

## What it does

| Step | What happens | Changes your system? |
|------|--------------|----------------------|
| System check | OS, CPU, RAM, disk, Python, pip, Node.js, npm, Yarn, MariaDB/PostgreSQL, Redis, build tools, existing benches, sudo, ports | No |
| Version resolution | Verifies the Frappe branch and every app branch against the real GitHub repositories and reads their actual requirements (`requires-python`, `engines.node`, app dependencies) | No |
| Dependencies | Installs only what is **missing or incompatible**, shows every `sudo` command first | Yes, after you confirm |
| Database | Checks the superuser login; on a fresh MariaDB it can enable password login for `root` (socket login keeps working) | Only if you confirm |
| Bench | `bench init` with the chosen branch and a compatible Python, then validates the result | Creates the bench folder |
| Apps | `bench get-app` in dependency order (e.g. ERPNext before HRMS) | Adds apps to the bench |
| Site | `bench new-site`, installs the apps, enables developer mode, makes it the default site | Creates the site and its database |
| Verification | Starts the bench briefly and checks `/api/method/ping` answers `pong` | No lasting change |

Design rules: nothing is overwritten or deleted, system Python and system Node.js are never replaced,
passwords never appear on the command line or in logs, and every step can be safely rerun.

## Supported environments

| Platform | Status |
|----------|--------|
| Ubuntu 24.04 LTS, x86_64 | **Supported** (see [verified results](#test-results)) |
| Ubuntu 24.04 aarch64, Debian 12/13 | In the matrix but **untested** — refused unless `--allow-untested-platform` |
| Anything else (other distros, macOS, WSL1, Ubuntu ≤ 22.04) | Not supported — use an Ubuntu 24.04 VM/container |

| Frappe branch | Python | Node.js | MariaDB | Redis | Source |
|---------------|--------|---------|---------|-------|--------|
| `version-15` | ≥3.10, <3.15 | ≥18 | ≥10.6.6 | ≥6 | docs.frappe.io + upstream `pyproject.toml`/`package.json` |
| `version-16` | 3.14 | ≥24 | 11.8 | ≥6 | same |
| `develop` (v17 pre-release) | 3.14 | ≥24 | 11.8 | ≥6 | same |

These are read live from the branch at install time; [config/version-matrix.yaml](config/version-matrix.yaml)
keeps the documented values, the versions upstream CI uses, what this tool installs, and — separately —
what this project has actually tested.

Resources: at least 2 GB RAM (4 GB RAM+swap recommended), 4 GB free disk for Frappe alone, about
2 GB more per large app such as ERPNext.

## Prerequisites

- Ubuntu 24.04 with a **regular user that can use `sudo`** (do not run as root).
- `python3` (preinstalled on Ubuntu) and an internet connection.
- `git` to clone this repository.

Everything else (build tools, MariaDB, Redis, a compatible Python, Node.js, Yarn, Bench CLI) is detected and
installed for you if needed.

## The wizard, step by step

```
╔══════════════════════════════════════════════╗
║               FRAPPE SETUP CLI               ║
║  Guided Development Environment Installer    ║
╚══════════════════════════════════════════════╝

Step 1/10: System compatibility       what's installed, which Frappe versions are feasible here
Step 2/10: Installation directory     default ~/frappe-projects
Step 3/10: Bench configuration        default frappe-bench; detects existing benches
Step 4/10: Frappe version             version-15 / version-16 / develop
Step 5/10: Site configuration         site name, database, passwords (hidden input), developer mode
Step 6/10: Applications               none / ERPNext / official apps / custom Git URLs
Step 7/10: Review                     summary; nothing has been changed yet
Step 8/10: Dependency installation    shows the plan and every sudo command, asks first
Step 9/10: Bench and site provisioning
Step 10/10: Environment verification  starts the bench, pings the site, prints how to start
```

Press **Enter** to accept a default. **Ctrl-C** at any point stops safely; continue later with
`./install.sh resume`.

**Beginner example** — accept every default:

```
Parent directory for the bench [~/frappe-projects]:  ⏎
Bench name [frappe-bench]:                           ⏎
Frappe version [1]:                                  ⏎   (version-15)
Site name [dev.localhost]:                           ⏎
Database engine [1]:                                 ⏎   (MariaDB)
Database superuser [root]:                           ⏎
root password (existing password, or a new one to set if MariaDB is fresh): ********
Administrator password for the new site: ********
Enable developer mode on the site? [Y/n]:            ⏎
Applications [1]:                                    2   (Frappe + ERPNext)
Start the installation? [Y/n]:                       ⏎
```

## Choosing a Frappe version

- **version-15** — mature stable release; works with Ubuntu 24.04's own Python 3.12, Node 18+ and MariaDB 10.11.
- **version-16** — current stable release; needs Python 3.14, Node 24 and MariaDB 11.8. The installer gets
  Python 3.14 via `uv` and Node 24 from nodejs.org **in your home directory**. On a machine without
  MariaDB it uses the official MariaDB 11.8 repository (key fingerprint verified). If an **older MariaDB
  is already installed**, it stops and explains your options instead of upgrading your database server.
- **develop** — unstable pre-release; may break at any time.

The installer verifies the branch exists upstream and never silently switches to another one.

## Bench and site configuration

| Setting | Default | Notes |
|---------|---------|-------|
| Parent directory | `~/frappe-projects` | `~` expanded; system folders (`/etc`, `/usr`, `/var`, …) refused |
| Bench name | `frappe-bench` | letters, digits, `-`, `_`; bench path = parent/name |
| Site name | `dev.localhost` | `*.localhost` works in browsers without editing `/etc/hosts` |
| Database | MariaDB on `127.0.0.1:3306`, user `root` | PostgreSQL is experimental (existing server only) |
| Developer mode | on | `--no-developer-mode` to disable |
| Default site | yes | so `http://localhost:8000` serves it |

Existing things are handled safely:

- **Existing bench** at the target path → reused after compatibility checks (branch and Python must match), never re-initialised.
- **Existing site** with the same name → reused, never recreated or dropped; only missing apps are installed.
- **A folder that is not a bench** → refused; pick another name.
- **A half-created bench from an interrupted run** → you choose: rename it aside (default), delete it, or stop.

## ERPNext, official and custom apps

In the wizard pick one of: Frappe only · Frappe + ERPNext · Frappe + selected official apps · Frappe + custom Git URLs.

Official apps offered (repository URLs only; branches are resolved live):
ERPNext, Frappe HR (`hrms`), Payments, CRM, Helpdesk, Telephony, Learning (`lms`), Education, Wiki,
Builder, Insights, Drive, Print Designer — see [config/app-catalog.yaml](config/app-catalog.yaml).

Before anything long-running, each app is checked:

- the branch exists (default: same name as the Frappe branch, e.g. `version-15`; if the app has no such
  branch you are asked to pick one — nothing is guessed);
- the app's declared Frappe range (`[tool.bench.frappe-dependencies]`) matches your Frappe version;
- its `required_apps` are selected too (e.g. HRMS → ERPNext, Helpdesk → Telephony, LMS → Payments);
- install order is sorted so dependencies come first.

Custom apps: give an `https://` or SSH URL and a branch/tag. Private repos work with an SSH key or a git
credential helper; URLs with embedded tokens are refused (they'd end up in `.git/config` and logs).

## After installation: everyday Bench commands

Always start a new terminal with the generated environment script so the right Node.js/Yarn/Bench are used:

```bash
source ~/.local/share/frappe-setup/env/frappe-bench-<id>.sh   # path shown at the end of install / in `status`
cd ~/frappe-projects/frappe-bench
```

(You may add that `source` line to `~/.bashrc`; the installer does not edit your shell files.)

**Run and develop**

| Command | What it does |
|---------|--------------|
| `bench start` | Start web server, workers, Redis, socket.io and the asset watcher (Ctrl-C stops) |
| `bench --site dev.localhost console` | Python console inside the site |
| `bench --site dev.localhost mariadb` | SQL shell on the site database |
| `bench build` / `bench build --app erpnext` | Rebuild frontend assets |
| `bench --site dev.localhost clear-cache` | Clear the site cache |
| `bench --site dev.localhost migrate` | Apply schema changes / patches after pulling code |
| `bench new-app my_app` | Scaffold a new custom app in `apps/` |
| `bench version` | Show versions of all apps in the bench |

**Sites**

| Command | What it does |
|---------|--------------|
| `bench new-site site2.localhost` | Create another site (prompts for DB root and Administrator passwords) |
| `bench use site2.localhost` | Make a site the default |
| `bench --site dev.localhost list-apps` | Apps installed on a site |
| `bench --site dev.localhost set-config developer_mode 1` | Set a site config value |
| `bench --site dev.localhost backup --with-files` | Backup to `sites/dev.localhost/private/backups` |
| `bench --site dev.localhost restore <file.sql.gz>` | Restore a backup |
| `bench drop-site site2.localhost` | Remove a site (moves it to `archived_sites`, drops the DB — careful) |

**Apps**

| Command | What it does |
|---------|--------------|
| `bench get-app --branch version-15 https://github.com/frappe/hrms` | Download an app into the bench |
| `bench --site dev.localhost install-app hrms` | Install it on a site (keep `bench start` running in another terminal: apps such as ERPNext need the bench's Redis) |
| `bench --site dev.localhost uninstall-app hrms` | Remove it from a site (deletes its data) |
| `bench update` | Pull all apps, migrate all sites, rebuild — do this deliberately, after a backup |

Or simply rerun `./install.sh` with more apps selected: existing bench and site are reused and only the
new apps are added.

## All frappe-setup commands

Run them via `./install.sh <command>` or directly as `.venv/bin/frappe-setup <command>`.

| Command | Purpose | Modifies the system? |
|---------|---------|----------------------|
| `./install.sh` | Interactive wizard (same as `install`) | after confirmation |
| `./install.sh check` | Compatibility report for all branches (`-b version-16` for one, `--json`, `--offline`) | no |
| `./install.sh install` | Wizard; or fully scripted with `--non-interactive` | after confirmation / `--yes` |
| `./install.sh status` | Known benches/sites, phase status of past runs, start command (`--json`) | no |
| `./install.sh doctor` | Diagnose services, bench, site, PATH, last failure; `--share report.json` writes a redacted report | no |
| `./install.sh resume` | Continue the last unfinished installation (asks for passwords again) | after confirmation |
| `./install.sh logs` | Show the latest log (`--list`, `--show FILE`, `-n 200`, `--path`) — secrets redacted | no |

## Non-interactive / advanced usage

All wizard answers are available as flags; passwords only via files (mode 600) or environment variables.

```bash
umask 077
printf '%s' 'Admin-password-here' > ~/.frappe-admin.pw
printf '%s' 'MariaDB-root-password' > ~/.frappe-db.pw

./install.sh install --non-interactive --yes \
  --frappe-branch version-15 \
  --parent-dir ~/frappe-projects --bench-name erp-bench \
  --site erp.localhost \
  --app erpnext --app hrms \
  --app-url git@github.com:my-org/my_custom_app.git@develop \
  --admin-password-file ~/.frappe-admin.pw \
  --db-root-password-file ~/.frappe-db.pw
```

Useful flags: `--db-host/--db-port/--db-root-user`, `--db-engine postgres`, `--no-developer-mode`,
`--no-set-default`, `--no-verify-http`, `--wkhtmltopdf` (PDF printing), `--backups`, `--reuse-existing-bench`,
`--cleanup quarantine|delete|abort`, `--verbose`. Environment variables `FRAPPE_SETUP_ADMIN_PASSWORD` /
`FRAPPE_SETUP_DB_ROOT_PASSWORD` work instead of the files. Safety overrides (use knowingly):
`--allow-untested-platform`, `--allow-db-version-mismatch`, `--allow-root` (disposable containers only).

A YAML file can hold the non-secret settings — see [config/example-install.yaml](config/example-install.yaml):

```bash
./install.sh install --config my-setup.yaml --admin-password-file ~/.frappe-admin.pw
```

`--yes` pre-authorizes the displayed sudo commands. Without cached sudo credentials a non-interactive run
stops and tells you to run `sudo -v` first.

## Logs, resume and recovery

- Logs: `~/.local/state/frappe-setup/logs/` (one file per run, mode 600, secrets masked). Every error shows its log path.
- Run state: `~/.local/state/frappe-setup/runs/` (configuration and phase status — never passwords).
- Managed runtimes and env scripts: `~/.local/share/frappe-setup/`; Python/Bench via uv in `~/.local/share/uv`, `~/.local/bin/bench`.

Every failure reports the **phase**, the **command** (redacted), the **exit code**, the **log file**,
the **likely cause** (when the output shows it), a **fix**, and whether **retrying is safe**.

Interrupted or failed? Fix the cause and run `./install.sh resume`. Each step re-checks the real state
first, so finished work (packages, bench, site, apps) is reused, not repeated or duplicated.

## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md). The quickest checks:

```bash
./install.sh doctor          # what is wrong and how to fix it
./install.sh logs -n 150     # the end of the last log
```

## Security

Summary (details in [docs/security.md](docs/security.md)): no password flags; secrets reach Frappe through
its own hidden prompts; logs/state/reports are redacted; all commands run without a shell; `sudo` commands
are shown and confirmed; Git URLs are validated; existing benches, sites, databases and files are never
deleted or overwritten; nothing disables firewalls or database authentication. Developer mode is a
development-only setting — don't expose this bench to untrusted networks.

## Development and testing

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                         # unit tests (mocked system commands, safe on any machine)
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy
FRAPPE_SETUP_NETWORK_TESTS=1 .venv/bin/pytest -m network      # read-only checks against GitHub
tests/integration/run_e2e.sh version-15                      # full install in a disposable Docker container
tests/integration/run_e2e.sh version-16 --erpnext
```

The end-to-end script builds a clean Ubuntu 24.04 image, runs `./install.sh` as a non-root sudo user,
checks `status`/`doctor`, reruns the install to prove idempotency, and scans all logs/state/output for the
passwords. It never touches the host. Project layout: [docs/architecture.md](docs/architecture.md).

### Test results

<!-- TEST-RESULTS:START -->
**Unit tests (executed):** 304 passed, 1 skipped (the opt-in network test). ruff and mypy clean.

**End-to-end (executed, 2026-10-23, clean Ubuntu 24.04.5 x86_64 Docker container, non-root sudo user):**

| Frappe | Apps | Runtimes used | Result |
|--------|------|---------------|--------|
| version-16 (16.53.0) | frappe | Python 3.14.9 (uv), Node 24.22.1, MariaDB 11.8.9 (official repo) | **PASS** — fresh install 6 min 17 s, site answered `/api/method/ping`, rerun reused everything (15 s), no passwords in logs/state/output |
| version-15 (15.124.0) | frappe | Python 3.12.3 (Ubuntu), Node 20.20.2, MariaDB 10.11.14 (Ubuntu) | **PASS via `resume`** — the first run exposed a Yarn-detection bug (fixed + regression test); `resume` then completed, ping OK, rerun 20 s, `doctor` clean, no passwords leaked |

**Also executed:** the interactive wizard through a real terminal (PTY) on a provisioned Ubuntu 24.04
machine, up to the final confirmation (declined → nothing changed); `check` against live upstream branches.

**Not yet verified:** a physical/VM Ubuntu with systemd, the `develop` branch, PostgreSQL, `--wkhtmltopdf`,
Debian/aarch64. See [docs/supported-platforms.md](docs/supported-platforms.md).
<!-- TEST-RESULTS:END -->

## Known limitations

- Only Ubuntu 24.04 x86_64 is tested. Other platforms are refused by default.
- Development setups only — no production setup (nginx, supervisor, TLS).
- PostgreSQL: experimental, Frappe-only, requires an existing server; not tested end-to-end.
- An already-installed MariaDB older than the branch requires (e.g. 10.11 for v16) is not upgraded.
- wkhtmltopdf from Ubuntu is built without patched Qt (some PDF features differ).
- Default Bench ports (8000, 9000, 11000, 13000) are shared with other benches on the machine; only one can run at a time unless you change ports.
- The managed Node.js/Yarn are only on PATH after `source`-ing the generated env script.

## Contributing

1. Open an issue describing the problem or platform you want to support.
2. Keep changes covered by unit tests (`pytest`), and run `ruff` and `mypy`.
3. For a new platform or Frappe branch: update `config/version-matrix.yaml` with sources, run
   `tests/integration/run_e2e.sh` on that platform, and add the result to the `tested` section only after it passes.
4. Never add flags that take secrets as arguments, and never add `shell=True`.

License: MIT (see [LICENSE](LICENSE)).
