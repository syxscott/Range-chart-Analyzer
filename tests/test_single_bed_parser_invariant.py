"""One bed parser, pinned structurally AND by value.

AUDIT-2026-10-01. rca_core/bed_parser.py exists because of a measured
data-integrity failure its own docstring records: "a predicted Bed 23c and
ground truth Bed 23d would both score as integer 23 -> false positive accuracy".
The module's docstring also asserts the fix -- every other call site routes
through it -- and that assertion had nothing checking it.

The history says the assertion is worth checking. eval_metrics had a second
parser ("a third, weaker parser" in quality.py's words), exporter had one, and
_eval_metrics._bed_num added a flat 0.001 for every subscript, which put 23a,
23c and 23z on one point -- the same disease again, in the module that decides
the reported accuracy.

What exists today: tests/test_bed_parser.py::test_exporter_and_eval_metrics_now_
agree_on_subscript compares the exporter's parse against the shared one for FIVE
hardcoded strings. That does not cover eval_metrics (the module that scores),
does not cover the shapes most likely to diverge, and does not stop a new
private parser from appearing.

So this pins:

  1. the STRUCTURE -- the complete set of bed-parsing definitions across
     rca_core and js, as a known and explained list, read by scanning the
     sources. A fifth parser fails the test instead of quietly re-creating the
     bug bed_parser.py was written to end.
  2. the VALUE -- exporter._parse_bed, eval_metrics._parse_bed and the shared
     parse_bed must agree, over a label matrix read from the modules themselves
     rather than from a table, so the test cannot drift with the product.
  3. the SPACING -- eval_metrics._bed_num reimplements the subscript offset
     rather than calling bed_parser.bed_position. A reimplementation is exactly
     what drifts, so the two are compared letter by letter.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

from rca_core.bed_parser import bed_position, parse_bed  # noqa: E402
from rca_core.eval_metrics import _bed_num, _parse_bed as em_parse_bed  # noqa: E402
from rca_core.exporter import _parse_bed as exporter_parse_bed  # noqa: E402

# Every bed-parsing definition, and why it is allowed to exist.
#   rca_core/quality.py::_parse_bed_n + _BED_RE -- the ONE documented
#     exception. It answers a different question (the bed INDEX, for the
#     before/after comparison) and the INVERSION decision routes through
#     _subbed_inverted -> parse_bed, so a dropped subscript cannot change a
#     verdict. quality.py's own comment says so where it is defined.
#   js/quality.js::_parseBedN + _BARE_BED_RE -- its mirror, for the same
#     reason; the subscript-aware sibling (rcaParseBed / rcaSubbedInverted) is
#     what the inversion check asks.
ALLOWED_PRIVATE_PARSERS = {
    "rca_core/quality.py": {
        "_parse_bed_n": "bed INDEX for the before/after comparison; the "
                        "inversion decision goes through _subbed_inverted",
        "_BED_RE": "the same exception's regex",
    },
    "js/quality.js": {
        "_parseBedN": "mirror of quality._parse_bed_n, same reason",
        "_BARE_BED_RE": "its regex",
        # The browser's subscript-aware parser, added 2026-10-01 as a faithful
        # port of rca_core/bed_parser.py. It IS a second implementation, so the
        # list is not enough on its own: TestValue below runs the same label
        # matrix through rcaParseBed and through the Python parser and requires
        # them to agree value for value.
        "_BED_FULL_RE": "internals of rcaParseBed (the browser's port)",
        "_BED_SUB_RE": "internals of rcaParseBed",
        "_BED_UNIT_RE": "internals of rcaParseBed (the unit rejection)",
        "_subIsBedLabel": "internals of rcaParseBed",
        "_bedStrayResidue": "internals of rcaParseBed",
        "_BED_DIGITS": "the \\p{Nd} class Python's \\d matches; "
                        "internals of rcaParseBed",
    },
    "rca_core/exporter.py": {
        "_parse_bed": "module-level alias kept for tests that monkey-patch "
                      "here; delegates to bed_parser.parse_bed",
        "_wpd_bed_value": "WPD bed LEVEL for the exchange bundle; its own "
                          "docstring says it goes 'via the SHARED bed parser'",
    },
    "rca_core/eval_metrics.py": {
        # The module that decides the reported accuracy, so its bed handling
        # had already been repaired once: a "third, weaker parser" in quality.py
        # and a flat 0.001 subscript offset in _bed_num. What it has today is
        # wrappers, not a parser, and each is named so the inventory is complete.
        "_parse_bed": "thin wrapper over bed_parser.parse_bed; keeps the dict "
                      "form so the scorer cannot drop a subscript",
        "_bed_sub": "reads bed_sub off an ALREADY parsed dict; parses nothing",
        "_bed_num": "comparable up-section position; REIMPLEMENTS "
                    "bed_parser.bed_position, and TestValue compares the two "
                    "letter by letter rather than trusting the reimplementation",
        "_score_bed_pair": "the exact / within_tolerance / wrong verdict over "
                           "two already-parsed beds; not a parser",
        "_is_bed_shaped": "a predicate telling a bed LABEL from a bare number; "
                          "operates on the already-parsed dict",
        "_BED_KEYWORD_PREFIX": 'the constant "bed" for that keyword check',
    },
    "js/minimax.js": {
        # Mirror of normalize_columnar_result's local fi(): an INDEX coercion
        # (int + a `lossy` flag) for range_top_idx / bed_idx fields, which are
        # already indices. It never sees a "Bed 23c" label, so it cannot drop a
        # subscript. Same category as the two above, and it has a Python twin.
        "rcaBedIndex": "columnar INDEX coercion with a lossy flag, not a label "
                       "parser; mirrors normalize_columnar_result's fi()",
    },
    "js/table.js": {
        # A SECOND copy of the same integer coercion, in the editor. Found by
        # this guard, not by review: the browser now holds two independent
        # implementations of "bed label -> integer index". Neither can produce
        # the 23c/23d false positive (they drop the subscript by design and the
        # verdict goes through the subscript parser), but two copies of one
        # coercion is how they drift apart, so it is registered rather than
        # left to be discovered again.
        "rcaParseBedN": "the editor's copy of quality._parseBedN; an INDEX "
                        "coercion, and the editor's inversion check additionally "
                        "asks rcaSubbedInverted for the subscript",
        "rcaBedPairInverted": "the editor's bed-pair verdict; not a parser, but "
                              "it reads one, so it is named here",
    },
    "js/viz.js": {
        # Display-side only: "bed index of a boundary string, or null" for the
        # canvas axis, trusting only the FIRST number of a label. It feeds
        # drawing, never a number that is exported or scored, so it is
        # deliberately looser than the metric parser and is named here rather
        # than routed through it.
        "rcaVizBedValue": "display-side axis label helper; never exported or scored",
    },
}

# A definition that parses a bed looks like a name containing "bed" (or
# "BED_RE") that is a function/assignment and is not the shared module.
_PARSER_NAME = re.compile(r"bed", re.I)


def _module_level_defs(path: Path):
    """Top-level function names and module-level assigned names that look like a
    bed parser, with AST so a match inside a docstring or a comment does not
    count."""
    rel = path.relative_to(REPO).as_posix()
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        tree = ast.parse(text)
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
        return rel, {n for n in names if _PARSER_NAME.search(n)}
    # JS: function declarations and top-level consts, comments stripped first.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    names = set(re.findall(r"^function ([A-Za-z_$][\w$]*)", text, re.M))
    names |= set(re.findall(r"^const ([A-Za-z_$][\w$]*) =", text, re.M))
    return rel, {n for n in names if _PARSER_NAME.search(n)}


# The names that are NOT parsers but do contain "bed" -- a bed parser's callers,
# the shared module itself, and so on.
NOT_PARSERS = {
    "rca_core/bed_parser.py": {"_row_key", "bed_position"},  # it IS the parser
    "rca_core/quality.py": {"_subbed_inverted"},             # asks the shared one
    "js/quality.js": {"rcaParseBed", "rcaSubbedInverted",
                      "rcaEditRowKey", "rcaEditAlignRows"},
    # Tick GENERATION from a numeric range, not a label parser: it takes
    # (lo, hi, maxN) numbers and rounds them.
    "js/viz.js": {"rcaVizBedTicks"},
    # Row-key CONSTANTS, not parsers: SOURCE_BED is the string "bed", a tag for
    # which field an endpoint's position came from.
    "rca_core/redraw.py": {"SOURCE_BED", "SOURCE_INDEX", "SOURCE_NONE"},
}


class TestStructure:
    def test_no_new_bed_parser_appeared(self):
        """A private bed parser is how the false-positive-accuracy bug came back
        twice already, so the set of them is a fact to assert, not a comment."""
        found = {}
        for path in sorted(list((REPO / "rca_core").glob("*.py"))
                           + list((REPO / "js").glob("*.js"))):
            if path.name == "bed_parser.py":
                continue
            rel, names = _module_level_defs(path)
            names -= NOT_PARSERS.get(rel, set())
            allowed = set(ALLOWED_PRIVATE_PARSERS.get(rel, {}))
            unexpected = names - allowed
            assert not unexpected, (
                f"{rel} defines {sorted(unexpected)}, which look like bed "
                "parsers and are not on the explained list. Route it through "
                "rca_core.bed_parser (or js/quality.js's rcaParseBed for the "
                "browser) -- a private parser is what made a predicted Bed 23c "
                "and a true Bed 23d score as an exact match, and it has already "
                "happened twice here.")
            found[rel] = sorted(names)

    def test_every_explained_exception_is_still_needed(self):
        """An exception that no longer exists should be deleted, not left as a
        comment that describes a parser nobody wrote."""
        for rel, entries in ALLOWED_PRIVATE_PARSERS.items():
            _, names = _module_level_defs(REPO / rel)
            for name in entries:
                assert name in names, (
                    f"{rel}::{name} is on the explained-exception list but is "
                    f"gone; drop the entry and its reason from ALLOWED_PRIVATE_PARSERS")

    def test_the_js_mirror_still_has_no_subscript_parser(self):
        """The browser's subscript parser is what rcaSubbedInverted asks. If a
        future change removes it, the sub-bed rule silently stops working."""
        _, names = _module_level_defs(REPO / "js" / "quality.js")
        assert "rcaParseBed" in names, "js/quality.js lost rcaParseBed"
        assert "rcaSubbedInverted" in names, "js/quality.js lost rcaSubbedInverted"


# Labels chosen to include every shape the three parsers could disagree on:
# bare, Bed-prefixed, subscripted, upper-case subscript, the metre/unit traps
# and the range/decimal traps, plus the non-ASCII digits whose handling differs
# between the languages.
LABELS = [
    "9", "09", "9a", "9A", "9b", "9z", "9Z", "9m", "9M", "9 ab", "9abc",
    "23", "23a", "23c", "23m", "0", "0a", "007", "1e2",
    "bed 9", "Bed 9", "BED 9", "bed9", "bed  9", "bed 9a", "Bed 23c",
    "bed 9 ab", "bed 9 m", "bed 12.5", "bed 23 to 25",
    "253 Ma", "253Ma", "0.5 Ma", "23 m", "23-25", "23 to 25", "12 ka",
    "5 my", "100 yr", "7 ft", "27a", "27 a", "  9a  ", "9a ",
    "", " ", "abc", "-9", "-9a", "9.5", "9,5", "9 9", "a9", "9a9",
    "Madison 3", "Talung Fm bed 4", "bed", "Bed", "9bed", "9 9",
    "100", "100a", "100z", "999", "9/", "9.", ".9", "+9", "9e", "9E5",
    "９", "٩", "9３", "Bed ９",
]


class TestValue:
    @pytest.mark.parametrize("label", LABELS)
    def test_all_three_parsers_agree(self, label):
        a = exporter_parse_bed(label)
        b = em_parse_bed(label)
        c = parse_bed(label)
        assert a == c, f"exporter vs shared on {label!r}: {a!r} vs {c!r}"
        assert b == c, f"eval_metrics vs shared on {label!r}: {b!r} vs {c!r}"

    @pytest.mark.parametrize("label", LABELS)
    def test_the_scorer_position_matches_the_shared_position(self, label):
        """eval_metrics._bed_num reimplements bed_parser.bed_position instead
        of calling it. A reimplementation is what drifts, so it is compared
        rather than trusted.

        Scoped to labels the shared parser accepts as a BED: _bed_num answers a
        wider question ("bed index, else age"), so "253 Ma" is 253.0 for it and
        None for bed_position by design, and comparing those two would be
        asserting that two different contracts agree.
        """
        if bed_position(label) is None:
            pytest.skip(f"{label!r} is not a bed label; bed_position declines it")
        row = {"v": label}
        mine = _bed_num(row, "v")
        theirs = bed_position(label)
        assert mine == pytest.approx(theirs, abs=1e-12), (
            f"{label!r}: eval_metrics._bed_num says {mine!r}, "
            f"bed_parser.bed_position says {theirs!r}")

    def test_the_sub_bed_spacing_is_uniform_across_every_letter(self):
        """The bug the constant was introduced for: a FLAT offset put 23a, 23c
        and 23z on one point. Checked over the whole alphabet, not two letters.
        """
        # "m" is the metre, not a subscript, and the shared parser refuses it --
        # asserted below rather than skipped, because that refusal is part of
        # the contract this file is protecting.
        prev = None
        for ch in "abcdefghijklmnopqrstuvwxyz":
            if ch == "m":
                continue
            pos = bed_position(f"23{ch}")
            assert pos is not None, f"23{ch} should be a sub-bed"
            assert 23.0 < pos < 24.0, f"23{ch} = {pos} escaped its own bed"
            if prev is not None:
                assert pos > prev, f"23{ch} did not sort after its predecessor"
            prev = pos
        assert bed_position("23m") is None, (
            "23m is a thickness, not a sub-bed; if the parser started accepting "
            "it, '23 m' would quietly become bed 23m in the accuracy metric")
        assert bed_position("23") == 23.0, "the bare bed must sit at its own index"
        assert bed_position("23") < bed_position("23a"), "bare bed below its sub-bed"
        assert bed_position("23z") < bed_position("24"), "last sub-bed below the next"


# The browser's own port of the parser. It is a second implementation by
# necessity -- the browser cannot import Python -- so its agreement with
# rca_core/bed_parser is asserted over the same matrix rather than assumed. The
# paths are passed in as ARGUMENTS: a hardcoded absolute path works on the
# machine that wrote it and is ENOENT on CI, which is how the export guard
# failed its first 3.12 run.
_JS_DRIVER = r"""
const vm = require('vm');
const fs = require('fs');
const ctx = vm.createContext({ console, Math, JSON, String, Number });
ctx.window = ctx; ctx.globalThis = ctx;
vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), ctx,
                { filename: 'js/quality.js' });
if (typeof ctx.rcaParseBed !== 'function') {
  console.error('ENV BROKEN: rcaParseBed is not exposed'); process.exit(3);
}
// Environment self-check against a case both implementations must answer.
const probe = ctx.rcaParseBed('Bed 23c');
if (!probe || probe.bed_num !== 23 || probe.bed_sub !== 'c') {
  // Double quotes outside: a nested single quote inside a single-quoted JS
  // string is a syntax error, and the whole driver then fails to parse.
  console.error("ENV BROKEN: rcaParseBed('Bed 23c') = " + JSON.stringify(probe));
  process.exit(3);
}
const labels = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify(labels.map((l) => {
  const p = ctx.rcaParseBed(l);
  return p ? [p.bed_num, p.bed_sub] : null;
})));
"""


@pytest.fixture(scope="module")
def js_parse_bed():
    import json
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "bed.js"
        d.write_text(_JS_DRIVER, encoding="utf-8")
        s = Path(td) / "labels.json"
        s.write_text(json.dumps(LABELS, ensure_ascii=True), encoding="utf-8")
        r = subprocess.run(
            ["node", str(d), str(s), str(REPO / "js" / "quality.js")],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"js/quality.js did not evaluate: {r.stderr[:400]}"
        return json.loads(r.stdout)


class TestTheBrowserParserIsAVerifiedPort:
    def test_agrees_with_the_python_parser_on_every_label(self, js_parse_bed):
        import json
        bad = []
        for label, js in zip(LABELS, js_parse_bed):
            p = parse_bed(label)
            mine = [p["bed_num"], p["bed_sub"]] if p else None
            if mine != js:
                bad.append((label, mine, js))
        assert not bad, "\n".join(
            f"{label!r}: python={mine!r} js={js!r}" for label, mine, js in bad)
