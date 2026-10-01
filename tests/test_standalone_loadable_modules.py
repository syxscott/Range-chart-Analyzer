"""Files this repository loads as DATA, not as a package member.

AUDIT-2026-10-02.

Four places in the tree take a module's TEXT and execute it somewhere that is
not a package -- ``importlib.util.spec_from_file_location`` with a bare name,
or a write into a throwaway tree:

  * tests/test_update_ics_2026_09_20.py and _2026_09_22.py load
    ``scripts/update_ics.py``; the second ALSO copies
    ``rca_core/standards/ics.py`` into a synthetic tree with no parent
    package. That is a PRODUCTION gate, not a test fixture:
    ``scripts/update_ics.py::validate_loads_in_ics_module`` validates a
    candidate ICS payload the same way, so a file that cannot be executed
    without a package blocks ``--write`` for entirely the wrong reason.
  * tests/test_audit_2026_09_27.py, tests/test_review_2026_09_10.py and
    tests/test_update_ics_2026_09_22.py load ``server.py`` by file path.
  * tests/test_eval_audit_2026_09_22.py, tests/test_eval_tiers_2026_09_20.py,
    tests/test_frontend_parity_fixtures_2026_09_20.py and
    tests/test_parity_fixture_expressibility.py load
    ``tests/gen_frontend_parity_fixtures.py`` and a gold-report script.

The consequence is concrete and it cost a day once already: a
``from ..age_patterns import ...`` added to ``standards/ics.py`` broke TEN
tests with ``ImportError: attempted relative import with no known parent
package``, in a suite (``test_update_ics_*``) whose name points nowhere near
the file that was edited, and whose failure text names neither the file nor
the contract. So the rule is asserted here rather than left in a comment:

    a module loaded this way must not use a RELATIVE import.

Absolute ``import rca_core.x`` is fine and is not what this forbids -- the
repository root is on sys.path for the tests and for the real server, so only
the leading dots break.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: repo-relative path -> why it is loaded as data
STANDALONE_LOADABLE: dict[str, str] = {
    "server.py": "loaded by file path from three test modules",
    "rca_core/standards/ics.py": (
        "scripts/update_ics.py::validate_loads_in_ics_module copies THIS "
        "FILE's text into a throwaway tree with no parent package; the same "
        "copy is what --write uses to decide whether a candidate ICS payload "
        "may be promoted"
    ),
    "scripts/update_ics.py": "the promotion script itself, loaded by file path",
    "tests/gen_frontend_parity_fixtures.py": (
        "the differential-fixture generator, re-executed by two test modules "
        "so the committed fixture is proven to match Python"
    ),
}


def _relative_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0:
            out.append("." * node.level + (node.module or ""))
        elif isinstance(node, ast.ImportFrom) and node.level == 1:
            out.append("." + (node.module or ""))
    return out


def test_every_standalone_loadable_module_exists():
    for rel in STANDALONE_LOADABLE:
        assert (ROOT / rel).is_file(), rel
        assert STANDALONE_LOADABLE[rel].strip(), "a reason is required: " + rel


def test_no_standalone_loadable_module_uses_a_relative_import():
    """The assertion that would have caught the ten-test breakage here."""
    for rel in STANDALONE_LOADABLE:
        offenders = _relative_imports(ROOT / rel)
        assert not offenders, (
            "%s is loaded as source text with no parent package, so a "
            "relative import raises ImportError there; inline the value or "
            "move it to a module that is imported normally. Offenders: %r"
            % (rel, offenders)
        )


def _spec_from_file_location_targets() -> set[str]:
    """Every .py path a test hands to spec_from_file_location.

    Built by walking the test tree's AST, so a NEW dynamic-load site shows up
    here rather than only in a traceback. The path is reconstructed from the
    string constants of the second argument, which is enough for every current
    call because they all spell their target as a chain of literals.
    """
    found: set[str] = set()
    for path in sorted((ROOT / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test module is CI's job
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fname != "spec_from_file_location" or len(node.args) < 2:
                continue
            parts = [n.value for n in ast.walk(node.args[1])
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if not parts or not parts[-1].endswith(".py"):
                continue
            # Only the trailing components matter: the literals are the path
            # TAIL after ROOT, sometimes with a prefix that is a variable.
            tail = [p for p in parts if p.endswith(".py") or "/" in p or "\\" in p]
            if not tail:
                continue
            rel = tail[-1].replace("\\", "/")
            found.add(rel.split("/")[-1] if rel.count("/") == 0 else rel)
    return found


def test_every_dynamically_loaded_module_is_declared():
    """If a new module starts being loaded as data, it must be declared here.

    Otherwise it silently inherits the no-relative-import constraint without
    anyone knowing it has one -- and the first person to add a relative import
    gets failures in an unrelated suite with an error that names neither the
    file nor the contract.
    """
    declared = set(STANDALONE_LOADABLE)
    # The comparison is by file name: several targets are addressed through a
    # variable path, so the literal tail is the only stable part.
    declared_names = {Path(d).name for d in declared}
    undeclared = {
        name for name in _spec_from_file_location_targets()
        if Path(name).name not in declared_names
    }
    assert not undeclared, (
        "these modules are loaded via spec_from_file_location but are not in "
        "STANDALONE_LOADABLE, so their no-relative-import contract is "
        "unenforced: %s" % sorted(undeclared)
    )
