"""``rca_core/modes_impl/`` must be a LEAF subpackage (Phase 3 / P1-4).

WHY THIS EXISTS
===============
``tests/test_rca_core_acyclic.py`` builds its import graph with
``CORE.glob("*.py")`` -- top-level ``rca_core/*.py`` ONLY. A new subpackage
is invisible to it. That is a real hole: a design where
``modes_impl/_shared.py`` does ``from ..extractor import ...`` while
``extractor.py`` re-exports from ``modes_impl`` creates a MODULE-LEVEL cycle
``extractor -> modes_impl.<mode> -> _shared -> extractor`` that the existing
zero-exemption guard ``test_import_time_graph_is_strictly_acyclic`` cannot
see.

This file closes the hole. It is AST-only (it never imports ``rca_core``),
so it still runs when the cycle would make importing the package fail.

The rule, in one line: **no file under modes_impl/ may import extractor,
at module level or anywhere else; every module-level relative import must
stay inside modes_impl/.** Absolute imports of ``rca_core.*`` are allowed
only for the two explicitly listed, proven leaf modules, because those are
already imported by ``extractor.py`` itself and contain no extractor import.

Do NOT relax ``tests/test_rca_core_acyclic.py`` to accommodate a design that
fails here -- fix the design.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "rca_core"
PKG = CORE / "modes_impl"

#: The only absolute ``rca_core.*`` imports allowed from modes_impl/. Both
#: are stdlib-only leaf modules that extractor.py already imported before
#: this split; neither imports extractor (asserted below).
ALLOWED_ABSOLUTE = frozenset({"rca_core.reason_codes", "rca_core.row_flags"})
FORBIDDEN_TARGET = "rca_core.extractor"


def _files() -> list[Path]:
    return sorted(PKG.glob("*.py"))


def _imports(path: Path, *, module_level_only: bool):
    """Yield (node, resolved_target) for Import / ImportFrom nodes.

    With ``module_level_only=True`` the bodies of functions and lambdas are
    skipped (they do not execute at import time). Class bodies, ``if`` blocks
    and ``try`` blocks DO execute at import time and are kept.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # Relative imports resolve against the package the file actually lives
    # in -- rca_core.modes_impl for the subpackage, rca_core for the root.
    package = ("rca_core.modes_impl" if path.parent.name == "modes_impl"
               else "rca_core")

    def walk(node: ast.AST, in_func: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                yield from walk(child, True)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                if not (module_level_only and in_func):
                    yield child
            else:
                yield from walk(child, in_func)

    for node in walk(tree, False):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # level=1 -> the file's own package; level=2 -> its parent.
                parts = package.split(".")
                up = node.level - 1
                base = ".".join(parts[:len(parts) - up]) if up < len(parts) else ""
                tgt = f"{base}.{node.module}" if node.module else base
                yield node, tgt
            else:
                yield node, node.module or ""


def _module_level_imports(path: Path):
    return list(_imports(path, module_level_only=True))


def _assert_no_extractor_import(paths: list[Path], *, module_level_only: bool) -> None:
    bad = []
    for path in paths:
        for node, target in _imports(path, module_level_only=module_level_only):
            if target == FORBIDDEN_TARGET or target.startswith(
                    FORBIDDEN_TARGET + "."):
                bad.append(f"{path.name}:{node.lineno} -> {target}")
            # ``from rca_core import extractor`` / ``import rca_core.extractor``
            if isinstance(node, ast.ImportFrom) and node.module == "rca_core":
                if any(a.name == "extractor" for a in node.names):
                    bad.append(f"{path.name}:{node.lineno} -> from rca_core import extractor")
    assert bad == [], (
        "modes_impl/ must not import extractor (that would recreate the "
        "module-level cycle the v0.3.0 zero-exemption guard forbids):\n  "
        + "\n  ".join(bad)
    )


def test_no_module_level_import_of_extractor() -> None:
    """(a) No module-level ImportFrom/Import resolves to rca_core.extractor."""
    files = _files()
    assert len(files) >= 10, f"modes_impl scan found too few files: {files}"
    _assert_no_extractor_import(files, module_level_only=True)


def test_module_level_relative_imports_stay_inside_modes_impl() -> None:
    """(b) Every module-level relative import targets the modes_impl package."""
    bad = []
    for path in _files():
        for node, target in _module_level_imports(path):
            if isinstance(node, ast.ImportFrom) and node.level:
                if not (target == "rca_core.modes_impl"
                        or target.startswith("rca_core.modes_impl.")):
                    bad.append(f"{path.name}:{node.lineno} -> {target}")
            elif isinstance(node, ast.ImportFrom) and not node.level:
                if target.startswith("rca_core.") and target not in ALLOWED_ABSOLUTE:
                    bad.append(f"{path.name}:{node.lineno} -> absolute {target}")
            elif isinstance(node, ast.Import):
                if any(a.name.startswith("rca_core.") for a in node.names):
                    for a in node.names:
                        if a.name.startswith("rca_core.") and a.name not in ALLOWED_ABSOLUTE:
                            bad.append(f"{path.name}:{node.lineno} -> import {a.name}")
    assert bad == [], (
        "a module-level import escaped modes_impl/ (or used an undeclared "
        "rca_core module):\n  " + "\n  ".join(bad)
    )


def test_no_absolute_import_of_rca_core_extractor() -> None:
    """(c) No ``import rca_core.extractor`` / ``from rca_core import extractor``."""
    _assert_no_extractor_import(_files(), module_level_only=False)


def test_no_extractor_import_at_any_scope() -> None:
    """Stronger than (a): a function-local ``from ..extractor import ...``
    would not deadlock at import time but would still weld the modules
    together. Forbid it too."""
    _assert_no_extractor_import(_files(), module_level_only=False)


def test_declared_leaf_modules_are_true_leaves() -> None:
    """The two allowed absolute imports must not lead back to extractor."""
    for dotted in sorted(ALLOWED_ABSOLUTE):
        path = CORE / (dotted.split(".")[-1] + ".py")
        assert path.exists(), f"allow-listed leaf module missing: {path}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad += [a.name for a in node.names
                        if a.name == FORBIDDEN_TARGET
                        or a.name.startswith(FORBIDDEN_TARGET + ".")]
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("rca_core"):
                    bad.append(node.module)
                # A relative ``from .extractor import`` inside a leaf would
                # also resolve back to extractor; level is checked below.
                if node.level and node.module == "extractor":
                    bad.append("relative .extractor")
        assert bad == [], f"{dotted} is not a leaf: {bad}"


def test_combined_module_level_graph_is_acyclic() -> None:
    """Close the scan hole itself: build the module-level import graph over
    BOTH rca_core/*.py and rca_core/modes_impl/*.py and demand zero cycles.

    ``tests/test_rca_core_acyclic.py`` only sees the first set of nodes, so
    this is the assertion that actually catches
    ``extractor <-> modes_impl._shared``.
    """
    nodes: dict[str, set[str]] = {}
    for path in sorted(CORE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        nodes[path.stem] = set()
    for path in _files():
        if path.name == "__init__.py":
            continue
        nodes[f"modes_impl.{path.stem}"] = set()

    def add_edges(path: Path, key: str) -> None:
        for _node, target in _module_level_imports(path):
            if target.startswith("rca_core.modes_impl."):
                nodes[key].add(f"modes_impl.{target.rsplit('.', 1)[-1]}")
            elif target == "rca_core.modes_impl":
                pass  # importing the package itself is not an edge to a node
            elif isinstance(target, str) and target.startswith("rca_core."):
                stem = target.rsplit(".", 1)[-1]
                if stem in nodes:
                    nodes[key].add(stem)

    for path in sorted(CORE.glob("*.py")):
        if path.name != "__init__.py":
            add_edges(path, path.stem)
    for path in _files():
        if path.name != "__init__.py":
            add_edges(path, f"modes_impl.{path.stem}")

    found: set[frozenset[str]] = set()

    def walk(start: str, node: str, path: list[str], seen: frozenset[str]) -> None:
        for nxt in sorted(nodes.get(node, ())):
            if nxt not in nodes:
                continue
            if nxt == start:
                found.add(frozenset(path))
            elif nxt not in seen:
                walk(start, nxt, path + [nxt], seen | {nxt})

    for start in sorted(nodes):
        walk(start, start, [start], frozenset({start}))
    assert not found, (
        "module-level import cycle spanning rca_core + modes_impl/: "
        + str(sorted(map(sorted, found)))
    )
