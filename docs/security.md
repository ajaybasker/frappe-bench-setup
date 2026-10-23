# Security

## Secrets

| Secret | How it is supplied | Where it goes | Never |
|--------|-------------------|---------------|-------|
| Administrator password | wizard (hidden input, confirmed), `--admin-password-file` (mode 600), or `FRAPPE_SETUP_ADMIN_PASSWORD` | answered to Frappe's own `getpass` prompt over a PTY | argv, state file, logs, config file |
| DB superuser password | wizard (hidden), `--db-root-password-file` (mode 600), or `FRAPPE_SETUP_DB_ROOT_PASSWORD` | PTY (site creation); a private temporary MariaDB option file (connectivity check, deleted immediately); `sudo mariadb` stdin (only when enabling password auth) | argv, state file, logs |

- There are deliberately **no** `--admin-password` / `--db-root-password` flags.
- Secret files that are group/world readable are rejected.
- YAML config files that contain password keys are rejected.
- Every log line, displayed command, error report, `status` output and `doctor --share` report
  passes through the redactor (registered values + patterns for password options, `key=value`
  / JSON password fields, SQL `IDENTIFIED BY` / `PASSWORD('…')`, URL credentials, GitHub/GitLab tokens).
- Logs and state files are created with mode 600 in directories with mode 700.
- Environment variables are never written to logs.

Note: Frappe itself stores the generated *site* database password in
`sites/<site>/site_config.json`, as it always does. That file belongs to the bench.

## Privileged operations

- sudo is used only for: `apt-get update/install`, adding the MariaDB APT repository (key +
  sources file), starting the MariaDB service, and — if root has *no* password at all — adding
  password authentication to `root@localhost` (keeping `unix_socket`).
- The exact commands are displayed and must be confirmed (or pre-authorized with `--yes`).
- The MariaDB signing key is verified against the fingerprint in the matrix
  (`177F 4010 FE56 CA33 3630 0305 F165 6F24 C74C D1D8`) before it is installed.
- An existing MariaDB repository configuration is never modified.
- A root account that already has a password is never changed.
- Running as root is refused (except `--allow-root` for disposable containers).

## What is never done

- No `shell=True`, no `eval`, no string-built shell commands; all subprocesses use argv lists.
- Git URLs are validated (https/ssh only, no credentials, no `ext::`, no `file://`, no leading
  `-`) and always passed after `--`.
- No `bench new-site --force` (it drops an existing database) and no `bench get-app --overwrite`.
- No deletion of existing benches, sites, databases, apps or user files. Cleanup applies only to a
  partial bench recorded as created by this tool, after explicit consent; the default is to
  rename it (`<name>.incomplete-<timestamp>`).
- No firewall, AppArmor/SELinux, or database authentication is disabled.
- System Python and system Node.js are never replaced, downgraded, or shadowed globally
  (no `uv python install --default`, no global npm installs, no edits to `~/.bashrc`).
- No production configuration (`bench setup production`, nginx, supervisor) is touched.

## Development-only settings

The tool targets **development** environments:

- `developer_mode` is enabled on the site by default (`--no-developer-mode` to disable).
- The site is made the bench default site (`--no-set-default` to disable).
- Bench backups (cron) are off by default (`--backups` to enable).

Do not expose a development bench to untrusted networks.

## Downloads

| What | Source | Integrity |
|------|--------|-----------|
| Node.js | https://nodejs.org/dist | SHA-256 from `SHASUMS256.txt` (TLS) |
| MariaDB repo key | https://mariadb.org/mariadb_release_signing_key.pgp | GPG fingerprint check |
| uv | PyPI (pip, TLS) | pip/TLS |
| Python | `uv python install` (python-build-standalone) | uv's checksums |
| Bench, Frappe, apps | PyPI / GitHub via Bench | Bench/git/TLS |

Archive extraction rejects absolute paths, `..`, links escaping the target and device files.

## Reporting a vulnerability

Please open a private security advisory on the repository instead of a public issue.
