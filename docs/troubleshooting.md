# Troubleshooting

Start with:

```bash
./install.sh doctor           # read-only diagnosis with fixes
./install.sh logs -n 200      # end of the most recent install/resume log (redacted)
./install.sh status           # which phase failed, and the start command
```

Every error panel shows **Phase**, **Command**, **Exit code**, **Likely cause**, **Fix**,
**Safe to retry** and **Log file**. After fixing the cause, run `./install.sh resume`.

## Common problems

| Symptom (in the error or log) | Cause | Fix |
|-------------------------------|-------|-----|
| `Python's venv module is not available` (install.sh) | Ubuntu without `python3-venv` | Answer `y`, or `sudo apt install python3.12-venv` |
| `Operating system … not supported` | Not Ubuntu 24.04 x86_64 | Use an Ubuntu 24.04 VM/container (or `--allow-untested-platform` for Debian/arm64 at your own risk) |
| `Bench should not be run as root` | Running as root | Create a regular sudo user and run as that user |
| `MariaDB 10.11 is older than the Frappe v16 requirement` | v16 needs MariaDB 11.8; an older server is installed | Use version-15, upgrade MariaDB yourself, use a fresh VM, or `--allow-db-version-mismatch` |
| `The database rejected the credentials for 'root'` | Wrong password, or root uses socket login only | Re-enter; on a fresh MariaDB let the installer enable password login; or `sudo mariadb-secure-installation` |
| `Cannot connect to mariadb at 127.0.0.1:3306` | Service not running | `sudo systemctl start mariadb`; `systemctl status mariadb` |
| `Could not get lock /var/lib/dpkg/lock` | Another apt / Software Updater is running | Wait for it, then resume |
| `Failed to fetch http://…`, `Unable to fetch some archives`, or apt hangs on one package | Mirror outage, or the network blocks some plain-HTTP downloads (seen in testing: one `.deb` blocked over HTTP on every mirror, fine over HTTPS) | Retry; if it persists set `URIs: https://mirrors.edge.kernel.org/ubuntu/` in `/etc/apt/sources.list.d/ubuntu.sources`, `sudo apt-get update`, then resume |
| `dpkg was interrupted` | An earlier apt run was killed | `sudo dpkg --configure -a`, then resume |
| `sudo needs a password but the installer is running non-interactively` | `--non-interactive` without cached sudo | Run `sudo -v` first, or run interactively |
| `Could not resolve host`, `ETIMEDOUT`, `early EOF` | Network / proxy | Fix connectivity (`https_proxy` if needed); resume is safe |
| `JavaScript heap out of memory`, `Killed` during `bench init` | Not enough RAM for the asset build | Add swap (`sudo fallocate -l 4G /swapfile …`), close apps, resume |
| `No space left on device` | Disk full | Free space or choose another parent directory; resume |
| `has no 'version-16' branch; choose one explicitly` | App does not publish that branch | Pick a branch: `--app crm@main` |
| `declares frappe >=15…,<16, but version-16 is frappe 16.x` | App branch doesn't match the framework | Use the app branch matching your Frappe branch |
| `requires the app 'telephony', which is not selected` | Missing `required_apps` | Add the required app (it is installed first automatically) |
| `apps/<name> exists with a different remote` | An app with that name is already in the bench | frappe-setup won't overwrite it; rename/remove it yourself or use that repository |
| `An incomplete bench from an earlier run is in the way` | Interrupted `bench init` | Interactive: choose *rename*; non-interactive: `--cleanup quarantine` |
| `<path> already exists and is not a bench` | Target folder holds other files | Choose another bench name or parent directory |
| `redis.exceptions.ConnectionError: Error 111 connecting to 127.0.0.1:11000` during a manual `bench … install-app` | The bench's own Redis only runs under `bench start` | Keep `bench start` running in another terminal while installing apps manually (frappe-setup starts the bench's Redis itself during installation) |
| `HTTP check did not pass: port 8000 is already in use` | Another bench / process uses the port | Stop it (`ss -ltnp | grep 8000`) or change `webserver_port` |
| `node: not found` / wrong Node when running `bench` later | New terminal without the env script | `source ~/.local/share/frappe-setup/env/<bench>-<id>.sh` (path in `status`) |
| `Frappe prints an 'untested above 10.8' warning` (v15) | Frappe's own warning for MariaDB 10.11/11.x | Informational; the documented minimum (10.6.6) is met |

## npm launcher (`npx frappe-setup-cli`)

| Symptom | Cause | Fix |
|---------|-------|-----|
| `x Frappe Setup CLI runs on Linux only` | macOS / Windows | Use an Ubuntu 24.04 VM or container (WSL2 is Linux but untested) |
| `x Python 3.10+ was not found on PATH` | No usable `python3` | `sudo apt install python3 python3-venv` |
| `x Node.js 18+ is required` | Old Node.js running npx | Upgrade Node.js (nodejs.org or nvm) — the bench's own Node.js is handled separately |
| `x The package is incomplete` | Corrupted npm cache / partial install | `npx clear-npx-cache` or `npm install -g frappe-setup-cli` again |
| `npm error code EACCES` on `npm install -g` | Global prefix owned by root | `npm install -g --prefix ~/.local frappe-setup-cli` (and make sure `~/.local/bin` is on PATH), or use `npx` |
| `Could not install the CLI dependencies` | No access to PyPI while preparing `~/.local/share/frappe-setup/npm-runtime/venv` | Check network/proxy, then rerun (safe) |
| Strange Python errors after an upgrade | Stale runtime environment | `rm -rf ~/.local/share/frappe-setup/npm-runtime` and rerun (your benches are not affected) |

## Starting over safely

frappe-setup never deletes your data. To discard a test installation yourself:

```bash
cd ~/frappe-projects/frappe-bench
bench drop-site dev.localhost        # drops the site database, archives the site folder
cd .. && rm -rf frappe-bench         # only if you are sure
rm ~/.local/state/frappe-setup/runs/<id>.json   # forget the run state (see `status --json`)
```

## Sharing a report

```bash
./install.sh doctor --share ~/frappe-setup-report.json
```

The report contains versions, check results and phase status — no passwords, tokens, environment
variables, and your home directory is replaced by `~`. Review it before sharing.
