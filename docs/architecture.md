# Architecture

## Overview

```
install.sh ──► .venv/bin/frappe-setup (Typer CLI, cli.py)
                    │
     ┌──────────────┼───────────────────────────────────────────────┐
     │ wizard       │ prompts.py   (collects + validates config)    │
     │ orchestrator │ installer.py (phases, state, verification)    │
     └──────────────┼───────────────────────────────────────────────┘
                    ▼
  preflight ─► resolve ─► dependencies ─► database ─► bench ─► apps ─► site ─► site_apps ─► configure ─► verify
  (system.py,  (remote.py, (dependencies.py) (database.py) (bench_   (app_     (site_setup.py)                (verification.py)
   preflight.py) app_setup.py)                            setup.py) setup.py)
                    │
     every external program runs through commands.Runner (argv only, redacted logs, process groups, PTY)
```

## Modules

| Module | Responsibility | Side effects |
|--------|----------------|--------------|
| `cli.py` | Commands, flag parsing, exit codes, final report | none directly |
| `prompts.py` | Wizard steps, presets from flags, input validation loop | none |
| `installer.py` | Phase orchestration, state bookkeeping, recovery decisions | via the modules below |
| `system.py` | Read-only detection: OS, CPU, RAM, tools, interpreters, Node runtimes, services, ports, benches | none (read-only commands) |
| `compatibility.py` | Version matrix model, version parsing, constraint intersection | none |
| `preflight.py` | Pure evaluation of facts against a branch → `CheckItem`s (ok/warn/fail/missing, fixable) | none |
| `remote.py` | `git ls-remote`, blob-less shallow snapshots, parsing pyproject/package.json/hooks.py | temp dir in cache (removed) |
| `dependencies.py` | Plan → authorize → install → verify; `ToolEnv` (PATH for the bench) | apt (sudo, authorized), user-space installs |
| `database.py` | Superuser access check, optional enabling of password auth for MariaDB root | DB (authorized) |
| `bench_setup.py` | `bench init` with verified options, bench structure validation | creates the bench |
| `app_setup.py` | App branch selection, framework-constraint check, topological order, `bench get-app` | adds apps |
| `site_setup.py` | `bench new-site` via PTY, `install-app`, `migrate`, `set-config`, `use` | creates the site |
| `verification.py` | Temporary `bench start` + `/api/method/ping` | starts/stops processes |
| `recovery.py` | Per-bench JSON state, cleanup eligibility, quarantine | state files |
| `commands.py` | The only subprocess entry point | — |
| `redaction.py`, `logging_config.py` | Secret masking, per-run log files | log files |
| `diagnosis.py` | Output patterns → cause/remedy/retry-safe | none |
| `doctor.py` | Read-only diagnostics and shareable report | report file (on request) |

## Key design decisions

**Single subprocess gateway.** `Runner.run` (streamed, bounded tail), `Runner.capture` (exact
output for data such as `git show`), `Runner.run_with_prompts` (PTY) and `Runner.run_on_terminal`
(only `sudo -v`). Children run in their own session/process group so a timeout or Ctrl-C
terminates the whole tree (e.g. yarn workers spawned by `bench init`).

**Secrets through a PTY.** Frappe's `new-site` reads the DB root and Administrator passwords
with `getpass()` when the flags are omitted (verified in `frappe/installer.py` and
`frappe/database/*/setup_db.py` for v15 and v16). The tool answers those prompts on a
pseudo-terminal, so passwords never appear in `ps`, shell history, state or logs. A prompt that
repeats (rejected value) aborts instead of looping.

**Remote metadata without raw.githubusercontent.com or the GitHub API.** A
`git clone --depth 1 --filter=blob:none --no-checkout` followed by `git show HEAD:<file>` fetches
only the needed blobs (~400 KB, ~3 s), needs nothing but git, and has no rate limit.

**Preflight is pure.** `system.gather_facts()` runs read-only probes once; `preflight.evaluate()`
is a pure function of facts + matrix + upstream metadata, which makes it exhaustively testable.

**Fixable vs blocking.** Every check says whether the dependency engine may fix it. Blocking
items (unsupported OS, root, low RAM/disk, an existing-but-too-old MariaDB) stop the run before
any change.

**User-space runtimes.** Python via `uv python install`, Node.js from nodejs.org (SHA-256
verified against `SHASUMS256.txt`), Yarn into a private npm prefix, Bench via `uv tool install`.
The resulting PATH is recorded (`ToolEnv`) and written to an env script per bench.

**State is advisory, the system is authoritative.** Each phase re-inspects reality (does the
bench exist and validate? does the site exist? which apps are installed?) before acting. The state
file only decides what is *allowed* (cleanup of resources this tool created) and what to report.

## Phase contract

| Phase | Idempotency rule |
|-------|------------------|
| preflight | read-only |
| resolve | read-only (network) |
| dependencies | installs only missing/incompatible items; re-checks afterwards |
| database | skipped when the site already exists |
| bench | reuse a valid bench; refuse foreign/incompatible ones; quarantine/delete only our own partial bench with consent |
| apps | skip apps already present with the same remote and branch; refuse mismatches |
| site | reuse an existing site; never `--force` |
| site_apps | install only apps missing from `list-apps`; `migrate` only for pre-existing sites |
| configure | `set-config` and `use` are idempotent; env script rewritten atomically |
| verify | read-only apart from a temporary `bench start` |
