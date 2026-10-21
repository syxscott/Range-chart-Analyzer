"""The packaged build must ship the files the app reads at runtime.

0.1.3: ``CHANGELOG.md`` was not in the spec's ``datas``. In a onefile build
``gui_fluent_onboarding.changelog_path()`` resolves the changelog next to the
module file, i.e. at the bundle root -- where it was not. The packaged app's
"what's new" dialog therefore had nothing to show, silently, in exactly the
artifact that gets released to users.

The CI matrix never builds a onefile exe, so no upstream check would have
caught this: the missing file only exists between the spec and the shipped
binary. So the spec is parsed instead.
"""
from __future__ import annotations

import ast
import pathlib

SPEC = pathlib.Path(__file__).resolve().parents[1] / "RangeChartAnalyzer.spec"

# (source, destination) -> what breaks without it
REQUIRED: dict[tuple[str, str], str] = {
    ("index.html", "."): "server.py serves the front-end from the bundle root",
    ("js", "js"): "the front-end's modules",
    ("css", "css"): "the front-end's styles",
    ("assets", "assets"): "logo and images referenced by the GUI",
    ("rca_core/resources", "rca_core/resources"): (
        "rca_core/standards/ics.py reads it via Path(__file__)"
    ),
    ("CHANGELOG.md", "."): (
        "the packaged 'what's new' dialog reads it at the bundle root"
    ),
}


def _spec_datas() -> set[tuple[str, str]]:
    """The ``datas`` list in the spec, as (source, destination) pairs."""
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "datas" for t in node.targets
        ):
            continue
        pairs: set[tuple[str, str]] = set()
        for element in node.value.elts:
            if (
                isinstance(element, ast.Tuple)
                and len(element.elts) == 2
                and all(isinstance(e, ast.Constant) for e in element.elts)
            ):
                pairs.add((element.elts[0].value, element.elts[1].value))
        return pairs
    raise AssertionError("RangeChartAnalyzer.spec has no `datas` assignment")


def test_required_payload_is_bundled():
    bundled = _spec_datas()
    missing = {
        pair: why
        for pair, why in REQUIRED.items()
        if pair not in bundled
    }
    assert not missing, (
        "the onefile exe would be missing runtime payload: "
        + "; ".join(f"{src} -> {dst} ({why})" for (src, dst), why in missing.items())
    )


def test_every_bundled_source_exists_on_disk():
    """A datas entry pointing at a missing file is a build-time error."""
    root = SPEC.parent
    for src, _dst in _spec_datas():
        assert (root / src).exists(), f"spec bundles {src}, which does not exist"
