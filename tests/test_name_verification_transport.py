"""AUDIT-2026-10-01 [item 27]: GBIF name verification ships on ONE transport only.

This file documents a gap rather than a contract.  It exists because the gap is
invisible from the code: ``rca_core/names.py`` is 709 lines with 8 public
functions, 7 test files exercise it, it was hardened specifically for the
Python transport (a private opener to dodge GBIF's vhost 404, a 15 s
per-request timeout, a 60 s wall-clock batch budget, local rejection of
malformed names), and its three ``msg_key``s are translated in all three
languages in both catalogs -- yet NO product code calls it.

Measured, not assumed (``git grep`` over every non-test ``.py``; the AST-level
scan below repeats it without shelling out to git):

* the ONLY live GBIF round in the product is the browser's --
  ``js/app.js`` builds ``https://api.gbif.org/v1/species/match?verbose=true``
  and ``fetch``es it after every extraction (fail-silent, capped at 20 names);
* the desktop GUI (``gui_fluent.py``, PySide6) and the local backend
  (``server.py``) perform NO name verification at all;
* ``js/app.js`` says in a comment that its three constants MIRROR this module.
  That claim is true today, and ``test_browser_constants_mirror_python`` below
  is what keeps it true.

Consequences for research use, which is why this is filed rather than left as
an observation:

1. ``README.md`` lists 学名模糊验证(GBIF) under 科研可信度.  On the transport
   most researchers use (the desktop app) that capability does not exist.
2. The browser's findings are held in ``state._nameIssues`` -- assigned,
   rendered, cleared.  Never serialised.  ``exporter.py`` / ``report.py`` /
   ``quality.py`` / ``server.py`` contain no name-issue key at all, so the
   audit report can neither reproduce nor attest the check.
3. Even where it does run, it is a LIVE call at view time, so the same input
   re-run later meets a different GBIF state.  A quality signal that is not in
   the evidence chain cannot be part of a reproducible result -- the exact
   distinction ``reason_codes.py`` exists to protect ("a silent omission has
   to stay detectable as one").

WHEN THE DESKTOP IS WIRED UP, INVERT THIS FILE.  The four "gap" tests are
written to fail the moment a product caller appears, so the wiring cannot land
silently and leave the documentation claiming a fork that no longer exists.
``test_browser_constants_mirror_python`` is the exception: it is a real
cross-engine invariant and should outlive the gap.
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
# The gap, stated so it cannot be mistaken for coverage
# ---------------------------------------------------------------------------

def test_python_product_has_no_caller_for_name_verification():
    hits = _imports_names_module()
    assert hits == [], (
        "a product module now imports rca_core.names -- the desktop/backend "
        "fork this file documents is being closed.  Update this file and the "
        "README, and decide whether the findings must also enter the evidence "
        "chain (they currently are not serialised anywhere).\n  "
        + "\n  ".join(hits))


def test_name_issues_never_reach_the_evidence_chain():
    """The report cannot reproduce or attest a check it does not record."""
    for rel in ("rca_core/report.py", "rca_core/exporter.py",
                "rca_core/quality.py", "server.py", "gui_fluent.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for needle in ("name_issues", "names.fuzzy", "names.unmatched",
                       "names.ambiguous"):
            assert needle not in text, (
                "%s now references %r -- name findings reached the audit "
                "chain; update this file" % (rel, needle))


def test_the_only_live_gbif_round_is_the_browsers():
    app = (REPO / "js" / "app.js").read_text(encoding="utf-8")
    assert "api.gbif.org" in app and "fetch(GBIF_MATCH_URL" in app, (
        "the browser stopped calling GBIF -- one more transport may now own "
        "this, or the feature may be gone entirely.  Re-read this file.")
    for rel in ("rca_core/quality.py", "rca_core/report.py", "server.py"):
        assert "api.gbif.org" not in (REPO / rel).read_text(encoding="utf-8")


def test_names_fuzzy_keys_are_translated_but_unreachable_on_python():
    """9 catalog entries (3 keys x 3 languages) that no Python path emits."""
    from rca_core.i18n import TRANSLATIONS
    for lang in ("zh", "en", "ja"):
        for key in ("names.fuzzy", "names.unmatched", "names.ambiguous"):
            text = TRANSLATIONS[lang].get(key)
            assert text and text != key, "%s/%s untranslated" % (lang, key)
    # They resolve -- for a caller that does not exist.
    assert names_mod.name_issues is not None


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
