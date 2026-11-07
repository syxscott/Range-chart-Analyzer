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

2026-11-07 (Phase 3 / P1-4): THE SCAN IS NOW RECURSIVE. The old passes used
``CORE.glob("*.py")``, which sees only top-level ``rca_core/*.py``. A new
subpackage (``rca_core/modes_impl/``) was therefore invisible, so a design
with ``modes_impl/_shared.py -> extractor`` plus ``extractor -> modes_impl``
could have created a MODULE-LEVEL cycle that no guard detected. Both graph
passes now use ``CORE.rglob("*.py")``, node names are dotted paths relative
to ``rca_core/`` (e.g. ``extractor``, ``modes_impl._shared``), and relative
import targets are resolved by ``level`` instead of
``node.module.split(".")[0]``. ``KNOWN_CYCLES`` is unchanged: the recursive
scan found NO additional cycle (verified, not assumed).
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
# The 2026-11-07 recursive scan (root + subpackages) still finds exactly this
# one cycle, so no waiver was added or removed.
KNOWN_CYCLES = {
    frozenset({"llm", "protocols"}),
}


def _node_name(path: Path) -> str:
    """Dotted node name relative to CORE: ``rca_core/modes_impl/_shared.py``
    -> ``modes_impl._shared``; ``rca_core/extractor.py`` -> ``extractor``."""
    return ".".join(path.relative_to(CORE).with_suffix("").parts)


def _module_paths() -> list[tuple[str, Path]]:
    """Every .py under CORE, recursively, minus package facades and caches."""
    out: list[tuple[str, Path]] = []
    for path in sorted(CORE.rglob("*.py")):
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            # The package facades import everything by design; including them
            # would make every submodule look cyclic. They have no inbound
            # relative imports, so they cannot participate in a cycle anyway.
            continue
        out.append((_node_name(path), path))
    return out


def _import_targets(node_name: str, node: ast.AST) -> list[str]:
    """Resolve one Import/ImportFrom to rca_core-internal node names.

    Relative imports are resolved by ``level`` against the importing module's
    own package, NOT by ``node.module.split(".")[0]``::

        extractor            + from .shape import ...            -> shape
        extractor            + from .modes_impl.range_chart ...  -> modes_impl.range_chart
        modes_impl._shared   + from ..extractor import ...       -> extractor
        modes_impl.range_chart + from ._shared import ...        -> modes_impl._shared

    Absolute ``from rca_core.X import`` / ``import rca_core.X`` are stripped
    of the ``rca_core.`` prefix so they land on the same node names. Imports
    of anything outside ``rca_core`` (stdlib, third party) are not graph edges.
    ``from . import name`` / ``from .. import name`` are alias-aware.
    """
    out: list[str] = []
    if isinstance(node, ast.ImportFrom):
        pkg = node_name.split(".")[:-1]
        if node.level:
            base = pkg[:max(0, len(pkg) - (node.level - 1))]
            if node.module:
                out.append(".".join(base + node.module.split(".")))
            else:
                for alias in node.names:
                    out.append(".".join(base + [alias.name]))
        else:
            module = node.module or ""
            if module.startswith("rca_core."):
                out.append(module[len("rca_core."):])
            elif module == "rca_core":
                out.extend(alias.name for alias in node.names)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith("rca_core."):
                out.append(alias.name[len("rca_core."):])
    return [t for t in out if t]


def _import_graph() -> dict[str, set[str]]:
    """Full graph: every internal import, including function-local ones."""
    graph: dict[str, set[str]] = {}
    for name, path in _module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for target in _import_targets(name, node):
                    if target != name:
                        deps.add(target)
        graph[name] = deps
    return graph


def _edges_by_scope(path: Path) -> tuple[set[str], set[str]]:
    """Split a module's imports into (import-time, deferred), same targets.

    "Import-time" means the statement actually executes while the module is
    being imported: module body or a class body. Function bodies, lambda
    bodies and ``if TYPE_CHECKING:`` blocks do not, so they are deferred.
    A bare ``try/except ImportError`` DOES execute at import time and stays
    in the first set -- which is the conservative, correct choice for a
    deadlock guard.
    """
    name = _node_name(path)
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
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                bucket = deferred if (in_func or in_type_check) else eager
                for target in _import_targets(name, child):
                    if target != name:
                        bucket.add(target)
            else:
                visit(child, in_func=in_func, in_type_check=in_type_check)

    visit(tree, in_func=False, in_type_check=False)
    return eager, deferred


def _import_time_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for name, path in _module_paths():
        graph[name] = _edges_by_scope(path)[0]
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


def _targets_of(src: str, node_name: str) -> list[str]:
    imp = ast.parse(src).body[0]
    return _import_targets(node_name, imp)


def test_relative_import_targets_are_resolved_by_level() -> None:
    """Pin the resolution formula itself (the recursive scan's foundation)."""
    assert _targets_of("from .shape import X", "extractor") == ["shape"]
    assert _targets_of("from .modes_impl.range_chart import X", "extractor") \
        == ["modes_impl.range_chart"]
    assert _targets_of("from ..extractor import X", "modes_impl._shared") \
        == ["extractor"]
    assert _targets_of("from ._shared import X", "modes_impl.range_chart") \
        == ["modes_impl._shared"]
    assert _targets_of("from rca_core.extractor import X", "modes_impl._shared") \
        == ["extractor"]
    assert _targets_of("import rca_core.extractor", "modes_impl._shared") \
        == ["extractor"]
    assert _targets_of("from . import error_utils", "extractor") == ["error_utils"]


def test_recursive_scan_sees_the_subpackages() -> None:
    """The hole this pass closes: modes_impl/ nodes must be real graph nodes."""
    graph = _import_graph()
    assert "modes_impl._shared" in graph
    assert "modes_impl.zonation_chart" in graph
    assert "standards.ics" in graph
    assert graph["modes_impl.zonation_chart"] == {"modes_impl._shared"}


def test_rca_core_cycles_are_exactly_the_known_ones() -> None:
    graph = _import_graph()
    assert len(graph) >= 30, "rca_core recursive scan found too few modules"
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
    genuinely dangerous new import. NOT relaxed for the recursive scan.
    """
    graph = _import_time_graph()
    cycles = _cycle_components(graph)
    assert not cycles, (
        "rca_core has a MODULE-LEVEL import cycle (an import-time deadlock "
        f"risk): {sorted(map(sorted, cycles))}. Break it by moving the shared "
        "code into a leaf module both sides can import one-way."
    )


def test_modes_impl_has_no_edge_back_to_extractor() -> None:
    """Phase 3 / P1-4: zero reverse edges, on BOTH graphs.

    ``extractor -> modes_impl._shared`` is allowed and expected (one-way
    down). ``modes_impl.* -> extractor`` in either the full or the import-time
    graph would recreate the module-level cycle the recursive scan exists to
    catch.
    """
    full = _import_graph()
    eager = _import_time_graph()
    reverse: list[str] = []
    for name in sorted(n for n in full if n.startswith("modes_impl.")):
        for graph, label in ((full, "full"), (eager, "import-time")):
            for target in sorted(graph.get(name, set())):
                if target == "extractor" or target.startswith("extractor."):
                    reverse.append(f"{name} -[{label}]-> {target}")
    assert reverse == [], (
        "modes_impl/ has a reverse edge to extractor: " + ", ".join(reverse)
        + ". Fix the design; do not weaken test_import_time_graph_is_strictly_acyclic."
    )
    # And the allowed direction really is present.
    assert "modes_impl._shared" in full["extractor"]
    assert "modes_impl._shared" in eager["extractor"]


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
    clean at ARCH-REVIEW-2026-11-02; this keeps it clean -- now recursively,
    so a leak inside a subpackage is caught too.
    """
    forbidden = ("tkinter", "PySide6", "qfluentwidgets", "http.server",
                 "socketserver", "webview")
    offenders = []
    for name, path in _module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for imported in names:
                if imported in forbidden:
                    offenders.append(f"{name}:{node.lineno} imports {imported}")
    assert not offenders, "UI/web dependency leaked into rca_core:\n  " + "\n  ".join(offenders)
