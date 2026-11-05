# Contributing to Range Chart Analyzer

Thanks for helping make stratigraphic data extraction more trustworthy.
This is a single-maintainer research tool, so the bar is **reproducibility**,
not ceremony.

## Before you start

- Python >= 3.10. The core runs on the standard library; install the full
  optional set with `pip install -r requirements.txt -c constraints.txt`.
- No build step for the frontend: `index.html` loads `js/*.js` directly.
- Read `docs/ARCHITECTURE.md` first. Two facts there surprise most people:
  1. `js/` is a **line-by-line mirror** of parts of `rca_core/`, pinned by
     `tests/js/tests_diff_frontend_parity.js`. **Changing Python logic
     usually requires the same change in JS.** A PR that touches only one
     side will be asked to update the other.
  2. `js/table.js` is also inlined by the **PySide6 desktop** history dialog
     (`gui_fluent_history_detail.py`). It is not browser-only.

## The gates a PR must pass

```bash
python -m pytest tests/ -q
node tests/js/tests_diff_frontend_parity.js   # must print 0 diverged
for f in tests/js/tests_*.js; do node "$f"; done
python scripts/release_check.py --check
```

CI runs those on 3.10 / 3.12 plus a Qt job, a browser-performance job, and
an optional-dependencies job. `build.yml` runs on version tags / manual
dispatch to build and smoke-test the Windows executables.

If you change a normalizer, a scorer, a prompt, or a table config,
regenerate the differential fixtures:

```bash
python tests/gen_frontend_parity_fixtures.py
python -m pytest tests/test_frontend_parity_fixtures_2026_09_20.py
```

Commit the regenerated fixture **in the same PR** as the behaviour change.

## Conventions that exist for a reason

| Rule | Why |
|---|---|
| Do not add a new `err.*` key to `server.py` without updating `js/minimax.js` + all three locales in `js/i18n.js` | `tests/test_error_key_contract.py` gates it; an unknown key silently produces a blank error banner |
| Do not add a module-level import between `rca_core` modules without checking `tests/test_rca_core_acyclic.py` | Two real cycles exist and are grandfathered; a third one is rejected |
| Do not rename a symbol in `js/table.js` that the desktop dialog calls | `tests/test_frontend_assets.py` gates it; the dialog silently falls back to a TreeView instead of erroring |
| Prompt changes must bump `PROMPT_VERSION` | The result cache key includes it; without a bump, old replies are served under the new prompt |
| New mode? Add one `ModeSpec` in `rca_core/modes.py` | The mode list used to live in nine places and silently disagreed |
| New capability? Add a row to `rca_core/capability_matrix.py` | "Wired into only one of the five endpoints" has caused two real data bugs |

## Reporting a bug

Use the bug report template. Please include the version
(`python -c "import rca_core; print(rca_core.__version__)"`), how you
installed it (source / exe), your OS, and the log if you have one. Do not
paste an API key — `rca_core/redact.py` scrubs error bodies, but a pasted
screenshot is not scrubbed.

For security issues, see `SECURITY.md` and report privately.

## Scope

This is a local, single-user research tool. Proposals that require a server
fleet, accounts, or telemetry are out of scope by design — see the
"rejected alternatives" table in `docs/ARCHITECTURE.md`.
