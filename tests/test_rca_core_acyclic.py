"""The rca_core import graph must not grow a new cycle.

WHY THIS EXISTS
===============
``rca_core/modes.py`` states its own rule in a docstring: ``prompt``,
``extractor`` and ``aggregate`` import it, so it must not import them back
-- "that is a cycle". A comment is not a guard. Nothing failed when the two
real cycles below were introduced; both rely on function-local imports to
break the import-time deadlock, so they work at runtime and stay invisible
until someone tries to reorder an import or split a module.

ARCH-REVIEW-2026-11-02 measured the graph with an AST pass (module-level AND
function-local ``from .x import``): 54 edges, 2 cycles. Grep alone saw only
6 edges because it matched column-0 imports exclusively.

This is a RATCHET, not a "zero cycles" assertion: the currently known cycles
are listed explicitly, and the test fails on ANY other cycle. Each time one
of them is removed, shrink KNOWN_CYCLES in the same commit -- that way the
guard tightens instead of being deleted. A new cycle cannot be added unless
someone deliberately edits this list.

2026-11-07: the graph is now checked in TWO layers. The ratchet above counts
every relative import, including function-local ones. That over-approximates
RISK: an import inside a function body does not execute at import time and
cannot deadlock, while a module-level one can. So a second pass counts only
import-time edges and demands that graph be strictly acyclic. The remaining
``llm <-> protocols`` cycle is then pinned to evidence -- both of its edges
must be function-local (see test_known_cycle_edges_are_function_local). That
is why extracting ``transport.py`` is a tidiness improvement, not a correctness
fix, and why it is safe to defer it instead of moving ~12 transport functions
that tests patch through ``llm._post_json`` at call time.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "rca_core"

# 2026-11-05: extractor <-> matrix was broken by moving the pure
# extraction_wall_clock_budget_sec into rca_core/budget.py (both modules
# now depend one-way on budget). KNOWN_CYCLES shrank with it -- the ratchet
# tightened instead of being deleted.
KNOWN_CYCLES = {
    frozenset({"llm", "protocols"}),
}


def _import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(CORE.glob("*.py")):
        if path.name == "__init__.py":
            # The package facade imports everything by design; including it
            # would make every module look cyclic. It has no inbound relative
            # imports, so it cannot participate in a cycle anyway.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                target = node.module.split(".")[0]
                if target != path.stem:
                    deps.add(target)
        graph[path.stem] = deps
    return graph


def _edges_by_scope(path: Path) -> tuple[set[str], set[str]]:
    """Split a module's relative imports into (import-time, deferred).

    "Import-time" means the statement actually executes while the module is
    being imported: module body or a class body. Function bodies, lambda
    bodies and ``if TYPE_CHECKING:`` blocks do not, so they are deferred.
    A bare ``try/except ImportError`` DOES execute at import time and stays
    in the first set -- which is the conservative, correct choice for a
    deadlock guard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    eager: set[str] = set()
    deferred: set[str] = set()

    def visit(node: ast.AST, *, in_func: bool, in_type_check: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda)):
                visit(child, in_func=True, in_type_check=in_type_check)
            elif isinstance(child, ast.If):
                test = child.test
                is_tc = isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
                visit(child, in_func=in_func,
                      in_type_check=in_type_check or is_tc)
            elif isinstance(child, ast.ImportFrom) and child.level and child.module:
                target = child.module.split(".")[0]
                if target != path.stem:
                    (deferred if (in_func or in_type_check) else eager).add(target)
            else:
                visit(child, in_func=in_func, in_type_check=in_type_check)

    visit(tree, in_func=False, in_type_check=False)
    return eager, deferred


def _import_time_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(CORE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        graph[path.stem] = _edges_by_scope(path)[0]
    return graph


def _cycle_components(graph: dict[str, set[str]]) -> set[frozenset[str]]:
    """Return the node sets of all simple cycles (smallest-first canonical)."""
    found: set[frozenset[str]] = set()

    def walk(start: str, node: str, path: list[str], seen: frozenset[str]) -> None:
        for nxt in sorted(graph.get(node, ())):
            if nxt not in graph:
                continue
            if nxt == start:
                found.add(frozenset(path))
            elif nxt not in seen:
                walk(start, nxt, path + [nxt], seen | {nxt})

    for start in sorted(graph):
        walk(start, start, [start], frozenset({start}))
    return found


def test_rca_core_cycles_are_exactly_the_known_ones() -> None:
    graph = _import_graph()
    assert len(graph) >= 30, "rca_core scan found too few modules - path wrong?"
    cycles = _cycle_components(graph)
    assert cycles == KNOWN_CYCLES, (
        "rca_core import graph changed.\n"
        f"  new cycles     : {sorted(map(sorted, cycles - KNOWN_CYCLES))}\n"
        f"  resolved cycles: {sorted(map(sorted, KNOWN_CYCLES - cycles))}\n"
        "If you removed a cycle, shrink KNOWN_CYCLES in this file. "
        "If you added one, break it instead - function-local imports hide "
        "the deadlock at import time but keep the modules welded together."
    )


def test_import_time_graph_is_strictly_acyclic() -> None:
    """No module-level cycle is allowed, with no waiver list at all.

    This is the property that actually prevents an import-time deadlock.
    The full-graph ratchet above also counts function-local imports, which
    are harmless at import time; this test is the one that would fail on a
    genuinely dangerous new import.
    """
    graph = _import_time_graph()
    cycles = _cycle_components(graph)
    assert not cycles, (
        "rca_core has a MODULE-LEVEL import cycle (an import-time deadlock "
        f"risk): {sorted(map(sorted, cycles))}. Break it by moving the shared "
        "code into a leaf module both sides can import one-way."
    )


def test_known_cycle_edges_are_function_local() -> None:
    """The one waived cycle may only survive because its edges are lazy.

    If somebody promotes ``from .protocols import ...`` or
    ``from .llm import ...`` to module level, the waiver becomes a lie even
    though KNOWN_CYCLES still matches -- so pin the evidence here.
    """
    eager_graph = _import_time_graph()
    for pair in KNOWN_CYCLES:
        left, right = sorted(pair)
        assert right not in eager_graph.get(left, set()), (
            f"{left} -> {right} became a module-level import; the lazy-cycle "
            "waiver no longer applies"
        )
        assert left not in eager_graph.get(right, set()), (
            f"{right} -> {left} became a module-level import; the lazy-cycle "
            "waiver no longer applies"
        )
        for name, other in ((left, right), (right, left)):
            _, deferred = _edges_by_scope(CORE / f"{name}.py")
            assert other in deferred, (
                f"{name}.py no longer even imports {other} lazily -- "
                "shrink KNOWN_CYCLES, the cycle is gone"
            )


def test_core_layer_does_not_import_ui_or_web_frameworks() -> None:
    """The core is shared by Tkinter, PySide6 and the stdlib HTTP server.

    A single ``import tkinter`` or ``import http.server`` in here would make
    the headless CI path depend on a display / platform package. Verified
    clean at ARCH-REVIEW-2026-11-02; this keeps it clean.
    """
    forbidden = ("tkinter", "PySide6", "qfluentwidgets", "http.server",
                 "socketserver", "webview")
    offenders = []
    for path in sorted(CORE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in forbidden:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert not offenders, "UI/web dependency leaked into rca_core:\n  " + "\n  ".join(offenders)
