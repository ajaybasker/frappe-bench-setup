# Release checklist

- [ ] `config/version-matrix.yaml`: re-check docs.frappe.io requirements and upstream
      `pyproject.toml` / `package.json` / CI workflow versions for every branch; update "Last reviewed".
- [ ] `config/app-catalog.yaml`: every URL answers `git ls-remote`.
- [ ] `.venv/bin/ruff check src tests` and `.venv/bin/ruff format --check src tests` clean.
- [ ] `.venv/bin/mypy` clean.
- [ ] `.venv/bin/pytest` green.
- [ ] `FRAPPE_SETUP_NETWORK_TESTS=1 .venv/bin/pytest -m network` green.
- [ ] `tests/integration/run_e2e.sh version-15` PASS.
- [ ] `tests/integration/run_e2e.sh version-16` PASS.
- [ ] Optional: `tests/integration/run_e2e.sh version-15 --erpnext`.
- [ ] Record each passing run in the `tested:` section of the matrix and the README test table
      (platform, date, versions, result). Never record runs that did not pass.
- [ ] Interactive smoke test on a real Ubuntu 24.04 VM: `./install.sh`, accept defaults, open the site.
- [ ] Replace `ORG` in README clone URL; bump `version` in `pyproject.toml` and `src/frappe_setup/__init__.py`.
- [ ] Tag the release.
