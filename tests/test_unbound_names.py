"""No module may reference a name it never binds.

AUDIT-2026-09-27 [item 7.1]. This file exists because of one bug that cost a
whole session and would have cost another: ``gui_fluent._show_history_
thumbnail`` called ``base64.b64decode`` without importing ``base64``, so
every call raised NameError -- and the broad ``except Exception`` around it
turned that into "the thumbnail blob was corrupt", a completely plausible
looking wrong outcome. Nothing failed, no test could see it, and the
history thumbnail had never once rendered.

The second instance was the same shape: ``normalize_paleomap_result``'s
``_one_point`` called ``first_non_empty``, which was never defined anywhere
in the package. Every palaeomap row whose ``coordinates`` arrived as a dict
raised NameError, and ``extract_paleomap``'s ``except Exception`` downgraded
the WHOLE result to ``ok=False`` -- silent data loss, triggered only by the
model's choice of coordinate shape.

Both are invisible to behavioural tests by construction, which is why this
is a static test. It is a repo-wide scan, not a per-file lint, because the
class of bug is "the call site looks fine in isolation".

Scope note: ``from __future__ import annotations`` makes annotations strings,
so a missing name used only in an annotation cannot raise at runtime. Those
are still reported (they break mypy and become live NameErrors under
``typing.get_type_hints``) but as a WARN, distinct from a hard failure.
"""
import ast
import builtins
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"node_modules", ".git", "outputs", "references", "__pycache__",
             ".venv", "build", "dist", ".eggs", "site-packages"}

# Names that are always available even without an import.
IMPLICIT = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
            "__loader__", "__builtins__", "__path__", "__class__"}


def _module_bindings(tree):
    bound = set(dir(builtins)) | IMPLICIT
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


def _has_future_annotations(tree):
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == \
                "__future__" and any(a.name == "annotations"
                                     for a in node.names):
            return True
    return False


def _iter_py():
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _annotation_lines(tree):
    """Line numbers that sit inside an annotation (never evaluated under the
    future import)."""
    lines = set()
    for node in ast.walk(tree):
        ann = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ann = node.returns
        elif isinstance(node, ast.AnnAssign):
            ann = node.annotation
        if ann is None:
            continue
        for sub in ast.walk(ann):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                lines.add(sub.lineno)
    return lines


class TestNoUnboundNames(unittest.TestCase):
    def test_every_name_used_is_bound_somewhere_in_its_module(self):
        hard, soft = [], []
        checked = 0
        for path in _iter_py():
            rel = os.path.relpath(path, REPO)
            try:
                src = open(path, encoding="utf-8").read()
                tree = ast.parse(src, path)
            except SyntaxError as exc:
                self.fail("%s does not parse: %s" % (rel, exc))
            except Exception:
                continue
            checked += 1
            bound = _module_bindings(tree)
            future = _has_future_annotations(tree)
            ann_lines = _annotation_lines(tree) if future else set()
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Name)
                        and isinstance(node.ctx, ast.Load)):
                    continue
                if node.id in bound:
                    continue
                item = "%s:%d %s" % (rel, node.lineno, node.id)
                (soft if node.lineno in ann_lines else hard).append(item)
        self.assertEqual(hard, [],
                         "these names are used at runtime but never bound in "
                         "their module (a missing import is the usual cause, "
                         "and a broad `except` usually hides it): %s" % hard)
        if soft:
            # Not a failure: PEP 563 keeps annotations as strings. Printed so
            # a real miss can be told apart from an annotation-only one.
            print("\nannotation-only unbound names (safe under PEP 563): %s"
                  % soft)
        self.assertGreater(checked, 30, "scan found suspiciously few files")


if __name__ == "__main__":
    unittest.main()
