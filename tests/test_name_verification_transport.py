"""AUDIT-2026-10-01 [item 27] -> CAPABILITY-2026-11: the desktop fork closed.

This file ORIGINALLY documented a gap: GBIF name verification shipped on ONE
transport (the browser) and no product Python code called ``rca_core.names``.
It said, verbatim: "WHEN THE DESKTOP IS WIRED UP, INVERT THIS FILE. The four
'gap' tests are written to fail the moment a product caller appears, so the
wiring cannot land silently and leave the documentation claiming a fork that
no longer exists."

The wiring has now landed (v0.3.0 + A-group work, 2026-11):
``gui_fluent.py::_run_name_capabilities`` calls ``verify_names`` +
``name_issues`` on a QThread worker and ``check_name_grounding`` for the
second vision pass. It is an EXPLICIT, DEFAULT-OFF opt-in (extra GBIF requests
and one extra model call cost money), so the browser remains the only path
that verifies on EVERY extraction. Results are folded into the result dict as
``name_candidates`` / ``name_issues`` / ``grounding`` and shown in the Fluent
result panel.

WHAT IS STILL OPEN (do not let the inversion hide it):
  * ``rca_core/report.py`` / ``exporter.py`` / ``quality.py`` / ``server.py``
    still neither emit nor attest name findings. The evidence-chain REPORT
    does not yet reproduce the check; only the exported result JSON carries
    the Fluent findings. That is a separate owner decision and is asserted
    as a known residual gap below rather than silently forgotten.
  * ``server.py`` still performs no GBIF verification; the browser does it
    client-side. The capability matrix records that waiver.

``test_browser_constants_mirror_python`` is the real cross-engine invariant
and outlives the gap.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from rca_core import names as names_mod

REPO = Path(__file__).resolve().parent.parent

#: Directories whose ``.py`` files are tooling, vendored code or fixtures --
#: never "the product", but also never the place a name check would live.
_NOT_PRODUCT_DIRS = frozenset({
    "node_modules", ".git", "outputs", "docs", "references", "proxy",
    "__pycache__", ".pytest_cache", ".ruff_cache",
})


def _is_test_path(rel: Path) -> bool:
    return any(part == "tests" or part.startswith("test")
               for part in rel.parts) or rel.name.startswith("tests_")


def _product_python_files() -> list[Path]:
    out: list[Path] = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if any(part in _NOT_PRODUCT_DIRS for part in rel.parts):
            continue
        if _is_test_path(rel):
            continue
        out.append(path)
    return out


def _imports_names_module() -> list[str]:
    """AST scan: which PRODUCT files import rca_core.names / .names ?"""
    hits: list[str] = []
    for path in _product_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name == "rca_core.names" or a.name == "names"
                       for a in node.names):
                    hits.append("%s:%d" % (path.relative_to(REPO), node.lineno))
            elif isinstance(node, ast.ImportFrom):
                # `from .names import x`  -> level=1, module="names"
                # `from . import names`    -> level=1, module=None, names=[names]
                # `from ..names import x`  -> level=2
                # The second form has NO module part, so testing only
                # `node.module` misses it -- and that is the form a sibling
                # module would use.  Both halves are checked.
                mod = node.module or ""
                base = mod.rsplit(".", 1)[-1]
                alias_is_names = any(a.name == "names" for a in node.names)
                if mod in ("rca_core.names", "names") or (
                        node.level and (base == "names" or alias_is_names)):
                    hits.append("%s:%d" % (path.relative_to(REPO), node.lineno))
            elif isinstance(node, ast.Call):
                # importlib.import_module("rca_core.names") / __import__(...)
                fn = node.func
                fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if fname in ("import_module", "__import__") and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value in ("rca_core.names", "names"):
                            hits.append("%s:%d"
                                        % (path.relative_to(REPO), node.lineno))
    return hits


# ---------------------------------------------------------------------------
# The inverted contract: the desktop IS wired, and ONLY the desktop is wired
# ---------------------------------------------------------------------------

def test_fluent_is_now_the_only_python_caller_for_name_verification():
    """Inverted from ``...has_no_caller...``. The wiring must not land silently.

    ``gui_fluent.py`` must import rca_core.names, and NO other product module
    may do so — server.py / rca_core/* still leave GBIF to the browser. If a
    second Python caller appears, this test names it so the capability matrix
    and the transport docs can be updated in the same commit.
    """
    hits = _imports_names_module()
    files = {h.split(":", 1)[0].replace("\\", "/") for h in hits}
    assert "gui_fluent.py" in files, (
        "the Fluent desktop wiring disappeared; name verification is again "
        f"unreachable from Python. hits={hits}")
    unexpected = files - {"gui_fluent.py"}
    assert not unexpected, (
        "a NEW Python product caller for rca_core.names appeared — update the "
        f"capability matrix and this file: {sorted(unexpected)}")
    # The import must be a REAL call, not a dead import: the worker payload
    # invokes verify_names + name_issues off the GUI thread.
    src = (REPO / "gui_fluent.py").read_text(encoding="utf-8")
    assert "verify_names(" in src and "name_issues(" in src
    assert "_run_name_capabilities" in src


def test_name_findings_are_in_the_fluent_result_but_not_yet_in_the_report():
    """Inverted from ``...never_reach_the_evidence_chain``.

    Fluent now folds the findings into ``self.result`` (so the exported JSON
    carries them), but the evidence-chain REPORT / exporters / scorer still
    do not attest them. Keep that residual gap visible instead of declaring
    the audit chain complete.
    """
    fluent = (REPO / "gui_fluent.py").read_text(encoding="utf-8")
    assert "name_issues" in fluent and "names.fuzzy" in fluent
    assert 'data["name_issues"]' in fluent
    assert 'data["grounding"]' in fluent
    for rel in ("rca_core/report.py", "rca_core/exporter.py",
                "rca_core/quality.py", "server.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for needle in ("name_issues", "names.fuzzy", "names.unmatched",
                       "names.ambiguous"):
            assert needle not in text, (
                f"{rel} now references {needle!r} -- the evidence-chain gap "
                "is being closed; update this file and the audit docs")


def test_browser_still_calls_gbif_and_fluent_has_a_python_transport():
    """Inverted from ``test_the_only_live_gbif_round_is_the_browsers``."""
    app = (REPO / "js" / "app.js").read_text(encoding="utf-8")
    assert "api.gbif.org" in app and "fetch(GBIF_MATCH_URL" in app, (
        "the browser stopped calling GBIF -- re-read this file")
    # Fluent's Python transport is opt-in (default OFF); the browser stays
    # the only transport that verifies on EVERY extraction.
    fluent = (REPO / "gui_fluent.py").read_text(encoding="utf-8")
    assert "verify_names(" in fluent
    assert "cfg.get(\"verify_names\", False)" in fluent
    for rel in ("rca_core/quality.py", "rca_core/report.py", "server.py"):
        assert "api.gbif.org" not in (REPO / rel).read_text(encoding="utf-8")


def test_names_fuzzy_keys_are_translated_and_reachable_on_python():
    """Inverted from ``...translated_but_unreachable_on_python``."""
    from rca_core.i18n import TRANSLATIONS
    for lang in ("zh", "en", "ja"):
        for key in ("names.fuzzy", "names.unmatched", "names.ambiguous"):
            text = TRANSLATIONS[lang].get(key)
            assert text and text != key, "%s/%s untranslated" % (lang, key)
    assert names_mod.name_issues is not None
    fluent = (REPO / "gui_fluent.py").read_text(encoding="utf-8")
    assert "name_issues(" in fluent, (
        "the translated keys are again unreachable from a Python product path")


# ---------------------------------------------------------------------------
# A real cross-engine invariant, kept regardless of the gap above
# ---------------------------------------------------------------------------

def test_browser_constants_mirror_python():
    """js/app.js claims its timeout/budget MIRROR this module. Hold it to it."""
    app = (REPO / "js" / "app.js").read_text(encoding="utf-8")

    def _js_const(name: str) -> int:
        match = re.search(r"const\s+%s\s*=\s*(\d+)\s*;" % name, app)
        assert match, "js/app.js no longer defines %s" % name
        return int(match.group(1))

    py_timeout = inspect.signature(
        names_mod.verify_name_gbif).parameters["timeout"].default
    assert _js_const("NAME_VERIFY_TIMEOUT_MS") == int(py_timeout * 1000)
    assert _js_const("NAME_VERIFY_BUDGET_MS") == int(
        names_mod.DEFAULT_BATCH_BUDGET_SECONDS * 1000)
