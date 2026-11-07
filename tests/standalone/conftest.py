"""Collect the script-style ``tests_*.py`` suites in this directory as pytest items.

Background
----------
``pytest.ini`` sets ``python_files = test_*.py``.  The 17 files here are named
``tests_*.py``, so ``pytest tests/`` has never collected them; CI has gated
them with a shell loop instead::

    for f in tests/standalone/tests_*.py; do python "$f"; done

That works, but the exit code is the only verdict and a failure is not
attributed to a specific pytest item.  The hook below turns each eligible
script into ONE pytest item with a standard, re-selectable nodeid
(``tests/standalone/tests_db.py::tests_db.py``) and a ``standalone::<file>``
failure header, so a regression is pinned to one file while every other file
still runs.

Zero intrusion (hard requirement)
---------------------------------
No ``tests_*.py`` file is edited, and nothing is imported or executed at
collection time.  ``python tests/standalone/tests_db.py`` still prints
``--- 17 passed, 0 failed ---`` and still exits 0 -- we literally run that
command, unchanged, in a child interpreter.

Why a child process instead of in-process ``runpy.run_path``
-----------------------------------------------------------
The suggested ``runpy.run_path(path, run_name="__main__")`` approach was
evaluated against all 17 files and is unsafe for THIS set:

* ``tests_gui_fluent.py`` ends its ``__main__`` block with ``os._exit(rc)``
  (line ~548) to dodge a Qt/QWebEngineView teardown access violation
  (0xC0000005).  ``os._exit`` cannot be caught: in-process it would terminate
  the WHOLE pytest process with no summary at all, silently dropping every
  remaining item -- including on the clean-skip path where rc == 0.
* The ``qt`` CI job runs ``pytest tests/ -k "gui or fluent or ..."`` with
  PySide6 installed.  Once this hook exists, that expression also selects
  ``standalone::tests_gui_fluent.py``; running real Qt windows inside the
  pytest process would expose that same documented teardown crash to the
  entire gate.

A child interpreter is the honest fix.  Each suite gets a fresh interpreter
with its own ``sys.path`` / ``sys.modules`` / ``os.environ`` / cwd / argv, so
"per-file isolation" is real rather than best-effort, and every exit path --
``sys.exit(1 if _fail else 0)``, ``raise SystemExit(3)``, ``os._exit(rc)`` or
a native crash -- collapses to one integer return code::

    0        -> pass   (equivalent to SystemExit(code 0/None) -> pass)
    non-zero -> fail   (equivalent to SystemExit(code != 0) -> fail)

The script's complete stdout and stderr are attached to the failure report,
so ``--- N passed, M failed ---`` and every ``FAIL ...`` line are visible.
A timeout and a launch error are failures too, never silent skips.

Exclusions (3 of 17)
--------------------
* ``tests_core.py`` -- has real ``test_*`` functions.  CI already runs it
  twice: ``python tests/standalone/tests_core.py`` (378 passed) and
  ``python -m pytest tests/standalone/tests_core.py -q`` (46 items).
  Collecting it here would double-collect whenever it is named explicitly.
* ``tests_real_corpus.py`` -- needs ``outputs/lit2020*/`` (299 MB of
  gitignored, copyrighted figures) plus a live API key; CI explicitly forbids
  wiring it into the gate.
* ``tests_onboarding.py`` -- asserts ``before is False`` and therefore needs a
  scratch ``HOME``; CI runs it in a dedicated ``mktemp -d`` step, and the
  ``qt`` job creates an onboarding marker that would break that assertion.

The 4 Qt-gated files (``tests_gui_fluent.py``, ``tests_drag.py``,
``tests_history.py``, ``tests_modern_ui.py``) are NOT excluded: on this host
(no PySide6) each was measured to self-skip gracefully and exit 0, so they are
collected normally.  Their own skip logic remains the single source of truth --
this hook never decides "missing dependency == pass".
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
# tests/standalone/ -> repository root (two levels up).  CI's glob loop runs
# `python "$f"` from the repo root, so pin cwd to it for byte-identical
# behaviour (a few suites resolve relative asset paths).
_REPO = _HERE.parent.parent
_TIMEOUT_SECONDS = 300
_MARKER = "standalone_script"

_EXCLUDED = {
    "tests_core.py": (
        "has real test_* functions; CI runs it twice (script: 378 passed, "
        "pytest: 46 items) -- collecting here would double-collect"
    ),
    "tests_real_corpus.py": (
        "needs 299 MB of gitignored copyrighted corpus + a live API key; "
        "CI explicitly forbids wiring it into the gate"
    ),
    "tests_onboarding.py": (
        "asserts `before is False`, needs a scratch HOME; CI runs it in a "
        "dedicated mktemp -d step"
    ),
}


def _same_dir(a, b) -> bool:
    """Windows-safe (case-insensitive, slash-insensitive) directory compare."""
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
        os.path.abspath(str(b))
    )


def pytest_configure(config):
    # Register so `-m "not standalone_script"` is available to the qt job
    # (which runs `pytest tests/ -k "gui or fluent ..."` with PySide6 installed
    # and must not pull these script items into that in-process Qt run).
    config.addinivalue_line(
        "markers",
        "standalone_script: a script-style tests_*.py suite from "
        "tests/standalone/ (one item per file, executed in a child interpreter)",
    )


def pytest_collect_file(file_path, parent):
    path = Path(str(file_path))
    name = path.name

    # Scope: only tests_*.py that live directly in tests/standalone/.
    if not _same_dir(path.parent, _HERE):
        return None
    if not (name.startswith("tests_") and name.endswith(".py")):
        return None
    if name in _EXCLUDED:
        return None

    # Deliberately NO `isinitpath` guard here.  pytest's own collector only
    # kicks in for a file explicitly named on the command line (isinitpath
    # bypasses `python_files`); keeping our collector present in that case is
    # what makes `pytest "tests/standalone/tests_db.py::tests_db.py"`, --lf,
    # -k and --deselect work.  `pytest_collect_file` is not firstresult, so
    # both collectors coexist; naming a file explicitly just yields its
    # test_* functions plus our one script item (harmless redundancy).
    # tests_core.py is excluded above, so acceptance command #3 still sees
    # exactly its 46 native items.

    # Return a pytest.File collector (the pattern recommended by the hook
    # spec).  Its nodeid is the FILE path, so node-id selection
    # (`pytest "tests/standalone/tests_db.py::tests_db.py"`, --lf, -k,
    # --deselect, IDE rerun) works.  Attaching the Item directly to the
    # directory collector would produce a nodeid whose path part is a
    # directory, which pytest refuses to re-select.
    return StandaloneFile.from_parent(parent, path=path)


class StandaloneFile(pytest.File):
    """Collector for one tests_*.py; yields exactly one item."""

    def collect(self):
        item = StandaloneScriptItem.from_parent(self, name=self.path.name)
        item.add_marker(getattr(pytest.mark, _MARKER))
        yield item


class _ScriptFailure(AssertionError):
    """A rendered, human-readable standalone-suite failure (no traceback)."""


class StandaloneScriptItem(pytest.Item):
    """One ``tests_*.py`` file, executed exactly as ``python <file>``."""

    def runtest(self):
        script = Path(str(self.path))
        cmd = [sys.executable or "python", str(script)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(_REPO),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _ScriptFailure(
                self._render(f"timed out after {_TIMEOUT_SECONDS}s", "", "")
            ) from exc
        except OSError as exc:
            raise _ScriptFailure(
                self._render(f"could not launch interpreter: {exc!r}", "", "")
            ) from exc

        if proc.returncode != 0:
            raise _ScriptFailure(
                self._render(
                    f"exited with code {proc.returncode} "
                    "(0 == SystemExit(0)/None == pass; non-zero == fail)",
                    proc.stdout or "",
                    proc.stderr or "",
                )
            )

    def _render(self, verdict: str, out: str, err: str) -> str:
        name = Path(str(self.path)).name
        return (
            f"standalone::{name}: {verdict}\n"
            f"  command: {sys.executable} {self.path}\n"
            f"----- script stdout -----\n{out or '(empty)'}\n"
            f"----- script stderr -----\n{err or '(empty)'}\n"
            f"----- end standalone::{name} -----"
        )

    def repr_failure(self, excinfo, style=None):
        # Render the whole report verbatim instead of letting --tb=short
        # truncate the script's `--- N passed, M failed ---` / FAIL lines.
        if isinstance(excinfo.value, _ScriptFailure):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style=style)

    def reportinfo(self):
        path = Path(str(self.path))
        return path, 0, f"standalone::{path.name}"
