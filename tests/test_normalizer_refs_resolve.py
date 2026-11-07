"""``normalizer_ref``: resolvable, but STILL NOT WIRED (Phase 3 / P1-4).

WHY THIS EXISTS
===============
``rca_core/modes.py`` declares, for each chart mode, an import-path string
``normalizer_ref``. Historically those strings pointed at
``rca_core.extractor`` and NOTHING in the repo ever parsed them: the dispatch
point resolves normalizers through the hard-wired ``_MODE_DISPATCH`` table
and direct function calls (see docs/ARCH-REVIEW-2026-11-02.md §7, and
``tests/test_occurrence_matrix.py::test_d5_every_registry_reference_resolves``
which only checks the string is importable).

After the P1-4 split the bodies live in ``rca_core/modes_impl/<name>.py`` and
the refs point there. Two properties must now hold:

1. Every non-empty ``normalizer_ref`` resolves to a CALLABLE that is the very
   same object as the name re-exported by ``rca_core.extractor`` -- not a copy.
   This is what keeps all existing import paths and monkeypatch targets alive.
2. The scheduler STILL does not consume the field. ``docs/OWNER_DECISIONS.md``
   G8 says wiring it up is a behaviour change that needs a human decision, so
   this test deliberately FAILS the moment somebody wires it: that is the
   signal to come back here, get G8 adjudicated, and rewrite the guard.

Test 2 is a RATCHET, in the same spirit as
``tests/test_rca_core_acyclic.py``: it does not forbid the change, it makes
the change impossible to do silently.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import rca_core
from rca_core import extractor, modes

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "rca_core"


def _declared_refs() -> list[tuple[str, str]]:
    return [(name, spec.normalizer_ref)
            for name, spec in modes.MODES.items() if spec.normalizer_ref]


def test_every_normalizer_ref_resolves_to_the_extractor_object() -> None:
    refs = _declared_refs()
    assert len(refs) == 10, f"expected 10 mode refs, got {len(refs)}"

    for mode_name, ref in refs:
        mod_path, sep, fn_name = ref.partition(":")
        assert sep and fn_name, f"{mode_name}: malformed ref {ref!r}"
        assert mod_path.startswith("rca_core.modes_impl."), (
            f"{mode_name}: ref {ref!r} does not point at modes_impl"
        )
        module = importlib.import_module(mod_path)
        resolved = getattr(module, fn_name, None)
        assert resolved is not None, f"{mode_name}: {ref!r} has no attribute"
        assert callable(resolved), f"{mode_name}: {ref!r} is not callable"

        # Same object, not a copy: the re-export must be an alias.
        assert getattr(extractor, fn_name, None) is resolved, (
            f"{mode_name}: extractor.{fn_name} is not the object resolved "
            f"from {ref!r} -- an import path or monkeypatch target broke"
        )
        # rca_core/__init__.py re-exports the same names too.
        if hasattr(rca_core, fn_name):
            assert getattr(rca_core, fn_name) is resolved, (
                f"{mode_name}: rca_core.{fn_name} diverged"
            )


def test_the_phylogenetic_alias_is_still_one_object() -> None:
    """``normalize_phylogenetic_tree_result`` was a module-level ALIAS, not a
    def. Moving it must not turn it into a second function object."""
    assert (extractor.normalize_phylogenetic_tree_result
            is extractor._normalize_phylogenetic_tree_into)
    assert (extractor.normalize_phylogenetic_tree_result
            is importlib.import_module(
                "rca_core.modes_impl.phylogenetic_tree"
            ).normalize_phylogenetic_tree_result)


def test_normalizer_ref_is_declared_but_not_wired() -> None:
    """G8: lock the fact that the scheduler does NOT use this field.

    If you are here because this failed, you (or someone) just wired the
    dispatcher to ``ModeSpec.normalizer_ref``. That is a behaviour change
    requiring the G8 decision in docs/OWNER_DECISIONS.md -- do not simply
    delete this assertion. Get the decision, then rewrite this test to assert
    the NEW dispatch contract instead.
    """
    # 1. No runtime consumer anywhere in rca_core outside the declaration.
    consumers = []
    for path in sorted(CORE.rglob("*.py")):
        if path.name == "modes.py":      # the declaration itself
            continue
        if "normalizer_ref" in path.read_text(encoding="utf-8"):
            consumers.append(str(path.relative_to(ROOT)))
    assert consumers == [], (
        "something in rca_core now reads normalizer_ref: "
        + ", ".join(consumers)
        + ". Wiring it is G8 (docs/OWNER_DECISIONS.md) -- update this test "
          "with the adjudicated dispatch contract instead of deleting it."
    )

    # 2. The dispatch point in particular must stay free of it.
    extractor_src = (CORE / "extractor.py").read_text(encoding="utf-8")
    assert "normalizer_ref" not in extractor_src, (
        "extractor.py now mentions normalizer_ref -- the hard-wired "
        "_MODE_DISPATCH dispatch was replaced. This is G8; see above."
    )
    tree = ast.parse(extractor_src, filename="extractor.py")
    attrs = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "normalizer_ref"]
    assert attrs == [], f"normalizer_ref attribute access at lines {attrs}"

    # 3. The hard-wired table is still the dispatch mechanism.
    assert isinstance(extractor._MODE_DISPATCH, dict)
    assert "range_chart" in extractor._MODE_DISPATCH
    assert extractor._MODE_DISPATCH["range_chart"] is extractor.extract_range_chart
