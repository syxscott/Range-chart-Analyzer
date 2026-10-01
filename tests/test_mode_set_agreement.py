"""The mode sets, in the five places that enumerate them, must agree.

AUDIT-2026-10-01. This is the "two artifacts must corroborate" pattern applied
to a SET rather than to a value, which is why it needs no contract guessing:
either a mode is listed or it is not.

Why it matters, concretely. rca_core/capabilities.py hands back an
unknown-mode STUB for anything it does not know -- maturity "assistant",
export_supported False. The UI consults that flag to decide whether to offer
the table tab, and the maturity string is what the user is shown. So a mode the
browser can classify but the registry does not declare gets its working table
export disabled and is labelled exploratory; and a mode the registry declares
that the classifier can no longer return can never be selected in the first
place.

The five, each read from the artifact itself:
  * rca_core/capabilities.py   CAPABILITIES
  * rca_core/aggregate.py      SCHEMA_BY_MODE  (merge consensus, so narrower)
  * rca_core/prompt.py         PROMPT_VERSION
  * js/minimax.js              KNOWN_CHART_TYPES

Two differences are CORRECT and are named here so the test does not have to
rediscover them:
  * ``chart_classify`` appears only in PROMPT_VERSION. It is a TASK -- "which
    chart type is this figure" -- not an extraction mode: it has no registry
    entry, no merge schema, and is never a chart type the classifier returns.
  * aggregate.SCHEMA_BY_MODE lists only the five modes that are MERGED across
    runs. The assistant modes (chemical_stratigraphy, paleomap, scatter_plot)
    have no merge consensus schema, and paleomap/chemical/scatter results are
    shape-classified by exporter._TABLELESS_MODES instead.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import rca_core.aggregate as AGG  # noqa: E402
import rca_core.capabilities as CAP  # noqa: E402
import rca_core.prompt as PR  # noqa: E402

REGISTRY = set(CAP.CAPABILITIES)
SCHEMAS = set(getattr(AGG, "SCHEMA_BY_MODE", {}))
# PROMPT_VERSION carries legacy aliases on purpose; the suite already pins
# "abundance" == "abundance_diagram", so an alias key is not drift.
PROMPT_VERSIONS = set(getattr(PR, "PROMPT_VERSION", {})) - {"abundance"}

# The task, not a mode: it has no registry entry, no merge schema, and is never
# a chart type the classifier returns.
TASK_ONLY = {"chart_classify"}

# Modes with no merge consensus schema. exporter._TABLELESS_MODES classifies
# their RESULTS by shape instead, so their absence from SCHEMA_BY_MODE is
# deliberate rather than a gap.
NO_MERGE_SCHEMA = {"chemical_stratigraphy", "paleomap", "scatter_plot"}


def _known_chart_types() -> set:
    """The browser's list.

    Read out of the source because KNOWN_CHART_TYPES is a LOCAL const inside a
    function, so it is not on the vm context. That is safe here and only here:
    it is a PLAIN literal array with nothing computed inside it, unlike the
    prompt constants -- array literals holding function calls -- which is why
    those needed an evaluator. The assertion below is the environment
    self-check: if the extraction ever stops matching, the set will not contain
    the two modes every side certainly shares.
    """
    src = (REPO / "js" / "minimax.js").read_text(encoding="utf-8")
    m = re.search(r"const KNOWN\s*=\s*\[([^\]]*)\]", src)
    assert m, "could not find the KNOWN chart-type array in js/minimax.js"
    found = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert {"range_chart", "paleomap"} <= found, (
        f"the extraction produced {sorted(found)}, which cannot be the chart-type "
        "list -- the pattern matched something else")
    return found


KNOWN = _known_chart_types()


class TestTheSetsAgree:
    def test_the_registry_covers_every_mode_the_product_can_produce(self):
        produced = SCHEMAS | KNOWN | (PROMPT_VERSIONS - TASK_ONLY)
        missing = sorted(produced - REGISTRY)
        assert not missing, (
            f"modes the product can produce but capabilities.py does not declare: "
            f"{missing}. Each would get the unknown-mode stub -- export disabled "
            "and maturity shown as 'assistant'.")

    def test_the_browser_can_classify_every_declared_mode(self):
        missing = sorted(REGISTRY - KNOWN)
        assert not missing, (
            f"capabilities.py declares {missing} but the browser's chart-type list "
            "does not, so those modes can never be selected")

    def test_prompt_versions_cover_every_declared_mode(self):
        # chart_classify is the one prompt with no mode of its own.
        missing = sorted(REGISTRY - PROMPT_VERSIONS)
        assert not missing, (
            f"declared modes with no PROMPT_VERSION: {missing}. Without one the "
            "cache key cannot include a version, so an edited prompt would keep "
            "answering from the old cache")

    def test_only_the_named_exceptions_differ(self):
        """Every difference from the registry must be one of the two documented
        reasons. A NEW difference is drift."""
        assert SCHEMAS - REGISTRY == set(), "a merge schema for an undeclared mode"
        assert SCHEMAS | REGISTRY == REGISTRY | (NO_MERGE_SCHEMA), (
            "the set of modes without a merge schema changed; if one gained a "
            "schema, update the exception list rather than the test")
        assert PROMPT_VERSIONS - REGISTRY == TASK_ONLY, (
            "a prompt appeared for something that is not a declared mode; if it "
            "is a real mode, give it a registry entry, and if it is a task, name "
            "it in TASK_ONLY")

    def test_the_registry_shape_is_what_the_ui_expects(self):
        for mode, entry in CAP.CAPABILITIES.items():
            assert entry.get("maturity") in {"stable", "candidate", "assistant"}, mode
            assert isinstance(entry.get("export_supported"), bool), mode
            assert entry.get("notes"), (
                f"{mode} has no notes -- the maturity label is shown to the "
                "user with nothing behind it")
        unknown = CAP.maturity_for("no_such_mode")
        assert unknown["maturity"] == "assistant"
        assert unknown["export_supported"] is False
        assert CAP.export_supported_for("no_such_mode") is False
