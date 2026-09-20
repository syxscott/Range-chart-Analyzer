"""Failing tests that capture each finding in the prompt agent's scope.

These tests:
- HIGH: genus-bias instruction removed
- MEDIUM: P-2 FIX bug tag removed from prompt text
- MEDIUM: degradation clause refers to per-row confidence + per-row notes fields
- MEDIUM: Japanese + Russian stratigraphic-suffix mappings present
- LOW: structured range endpoints (FAD/LAD, observed vs projected, bed ordering)
- LOW: reworked / in-situ occurrence flag
- LOW: author / year explicitly requested
- HIGH: Python and JS prompts share identical text + PROMPT_VERSION 'v3'

Run:  python tests/test_prompt_fixes.py
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rca_core.prompt import (
    PROMPT_VERSION,
    POS_FIELDS,
    RANGE_CHART_SYSTEM_PROMPT,
    ABUNDANCE_DIAGRAM_SYSTEM_PROMPT,
    CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT,
    SCATTER_PLOT_SYSTEM_PROMPT,
    COLUMNAR_SECTION_SYSTEM_PROMPT,
    CHART_LANG_HINT,
)

_pass = 0
_fail = 0


def check(name, cond, evidence=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        msg = f"FAIL {name}"
        if evidence:
            msg += f" -- {evidence}"
        print(msg)


# ---------------------------------------------------------------------------
# JS-side prompt extraction helpers
# ---------------------------------------------------------------------------

_JS_PROMPT_PATH = os.path.join(HERE, "..", "js", "prompt.js")


def _read_js_source() -> str:
    with open(_JS_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _extract_js_prompt_const(src: str, const_name: str) -> str:
    """Extract the joined string from a const declaration like:
       const FOO = ['a', 'b'].join('\\n');
    Returns the joined Python-style string the constant evaluates to.
    """
    # Find the start marker (handles any spacing before/after =)
    start_marker = "const " + const_name + " = ["
    start = src.find(start_marker)
    if start < 0:
        return ""
    # Find the closing ].join('\n'); — match the literal end of the array literal.
    end_marker = "].join('\\n');"
    end = src.find(end_marker, start)
    if end < 0:
        end_marker2 = '].join("\\n");'
        end = src.find(end_marker2, start)
        if end < 0:
            return ""
    raw = src[start + len(start_marker):end]
    # Split into lines and reverse JS single-quote escaping.
    lines = raw.split("\n")
    out = []
    for line in lines:
        s = line.strip()
        if s.endswith(","):
            s = s[:-1]
        if s.startswith("'") and s.endswith("'") and len(s) >= 2:
            inner = s[1:-1]
            # JS escape reversal for our specific strings
            inner = inner.replace("\\'", "'")
            out.append(inner)
        elif s == "":
            out.append("")
    joined = "\n".join(out)
    # JS template literals have a leading newline because the opening `[` is
    # followed by a literal '\n' before the first quoted string. The Python
    # side uses `"\n".join([...])` so there is no leading newline. The JS side
    # may also have a trailing newline from the final blank array slot.
    # Strip both so we can do a byte-identical comparison.
    if joined.startswith("\n"):
        joined = joined[1:]
    if joined.endswith("\n"):
        joined = joined[:-1]
    return joined


def _js_prompt_versions() -> dict:
    """Parse the JS PROMPT_VERSION object literal into a {mode: version} dict.

    The literal is an object, e.g. ``{range_chart: 'v4', ...}`` — the old
    string-form regex (``const PROMPT_VERSION = 'v3'``) predates the per-mode
    dict and returned "" for the object form.
    """
    src = _read_js_source()
    m = re.search(r"const\s+PROMPT_VERSION\s*=\s*\{([^}]+)\}", src, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for k, v in re.findall(r"(\w+)\s*:\s*'([^']*)'", m.group(1)):
        out[k] = v
    return out


_EVAL_JS = (
    "var fs=require('fs'), vm=require('vm');"
    "var src=fs.readFileSync(process.argv[1],'utf8');"
    "var ctx=vm.createContext({});"
    "vm.runInContext(src,ctx,{filename:'prompt.js'});"
    "var out=@@NAMES@@.map(function(n){"
    "var v=vm.runInContext(n,ctx);"
    "if(typeof v!=='string')throw new Error(n+' is not a string');"
    "return v;});"
    "process.stdout.write(out.join(String.fromCharCode(31)));"
)

_JS_TEXT_CACHE: dict[tuple, tuple] = {}


def _eval_js_prompt_consts(const_names):
    """Return the EVALUATED text of the given js/prompt.js constants.

    BORROW-2026-09-20: the coverage-contract and 0-999 axis clauses are
    generated at runtime inside js/prompt.js (exactly like the Python side
    generates them from rca_core.reason_codes), so the raw-source scraper
    ``_extract_js_prompt_const`` can not see them — only a real JS evaluator
    can. Mirrors how tests_frontend.js loads prompt.js through vm.runInContext.
    Raises AssertionError when node is unavailable or the eval fails, so a
    missing JS toolchain can never hide prompt drift.
    """
    key = tuple(const_names)
    if key in _JS_TEXT_CACHE:
        return _JS_TEXT_CACHE[key]
    node = shutil.which(os.environ.get("NODE_EXE", "node"))
    assert node, "node executable not found — can not evaluate js/prompt.js"
    names_js = "[" + ",".join("'" + n + "'" for n in const_names) + "]"
    script = _EVAL_JS.replace("@@NAMES@@", names_js)
    proc = subprocess.run([node, "-e", script, _JS_PROMPT_PATH],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, (
        f"node failed to evaluate js/prompt.js: {proc.stderr[:2000]}")
    texts = proc.stdout.split("\x1f")
    assert len(texts) == len(const_names), (
        f"node returned {len(texts)} texts for {len(const_names)} constants")
    _JS_TEXT_CACHE[key] = texts
    return texts


def _contract_block(prompt_text: str) -> str:
    """The generated (A)+(B) clause block of a contract-carrying prompt.

    Everything from the ANSWER EVERY REQUESTED CELL bullet through the end of
    the AXIS CALIBRATION bullet. Both sides build it from their own constants
    (Python: rca_core/reason_codes.py; JS: the slug array in js/prompt.js),
    so this block MUST be byte-identical across the two implementations —
    that is the mirroring contract of BORROW-2026-09-20.
    """
    start_marker = "- ANSWER EVERY REQUESTED CELL"
    end_marker = "it is simply not converted."
    start = prompt_text.index(start_marker)
    end = prompt_text.index(end_marker) + len(end_marker)
    return prompt_text[start:end]


def _diff_report(py_text: str, js_text: str, max_lines: int = 40) -> str:
    lines = difflib.unified_diff(py_text.split("\n"), js_text.split("\n"),
                                 fromfile="python", tofile="js/prompt.js",
                                 lineterm="", n=0)
    return "\n".join(list(lines)[:max_lines])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_genus_bias_instruction():
    """HIGH: prompt must NOT say 'prefer a plausible known radiolarian genus'.

    Transcription fidelity: the model should transcribe what is on the chart,
    leave the field empty (or '[unclear]') when unreadable, and lower
    confidence — never invent plausible-looking taxon names.
    """
    bad_phrase = "prefer a plausible known radiolarian genus"
    check(
        "py-no-genus-bias",
        bad_phrase not in RANGE_CHART_SYSTEM_PROMPT.lower(),
        evidence="genus-bias phrase leaked into the prompt",
    )
    js = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    check(
        "js-no-genus-bias",
        bad_phrase not in js.lower(),
        evidence="genus-bias phrase leaked into JS prompt",
    )


def test_no_p2_fix_tag():
    """MEDIUM: 'P-2 FIX:' internal bug-tag must NOT appear in the shipped prompt.

    Keep the underlying open-nomenclature sentence, strip the 'P-2 FIX:' prefix.
    """
    check(
        "py-no-p2-fix",
        "P-2 FIX" not in RANGE_CHART_SYSTEM_PROMPT,
        evidence="'P-2 FIX' internal tag shipped in prompt",
    )
    js = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    check(
        "js-no-p2-fix",
        "P-2 FIX" not in js,
        evidence="'P-2 FIX' internal tag shipped in JS prompt",
    )
    # The underlying open-nomenclature sentence must still be present.
    check(
        "py-keeps-open-nomen-sentence",
        "Preserve open nomenclature qualifiers" in RANGE_CHART_SYSTEM_PROMPT,
    )
    js_prompt = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    check(
        "js-keeps-open-nomen-sentence",
        "Preserve open nomenclature qualifiers" in js_prompt,
    )


def test_degradation_clause_references_per_row_fields():
    """MEDIUM: degradation clause must mention per-row confidence + notes.

    The range_chart schema only carries top-level confidence today, so the
    degradation clause telling the model to 'lower the confidence field' is
    unfulfillable. The schema must include per-row confidence + per-row notes
    (or an equivalent), and the prompt must instruct the model to use them.
    """
    # Per-row confidence + notes fields must be declared in the schema example.
    check(
        "py-schema-row-confidence",
        '"confidence"' in RANGE_CHART_SYSTEM_PROMPT and 'per-row' in RANGE_CHART_SYSTEM_PROMPT.lower()
        or 'row-level' in RANGE_CHART_SYSTEM_PROMPT.lower()
        or '"row_confidence"' in RANGE_CHART_SYSTEM_PROMPT
        or '"confidence"' in RANGE_CHART_SYSTEM_PROMPT.split('"species_ranges":')[1].split('],')[0],
        evidence="schema must declare per-row confidence",
    )
    # Prompt must mention per-row notes / note field explicitly.
    check(
        "py-schema-row-notes",
        '"note"' in RANGE_CHART_SYSTEM_PROMPT or '"notes"' in RANGE_CHART_SYSTEM_PROMPT,
        evidence="schema must declare per-row note(s) field",
    )
    # The degradation clause must not say 'append a note inside the field'
    # — that pushes epistemic noise into taxon names and breaks dedup.
    check(
        "py-no-note-in-field",
        "append a brief note to the relevant field" not in RANGE_CHART_SYSTEM_PROMPT,
        evidence="degradation clause still asks for inline notes",
    )
    # The degradation clause should instruct the model to use the per-row
    # confidence + notes fields instead.
    degradation = RANGE_CHART_SYSTEM_PROMPT.split("DEGRADE GRACEFULLY")[1] if "DEGRADE GRACEFULLY" in RANGE_CHART_SYSTEM_PROMPT else ""
    check(
        "py-degradation-mentions-row-confidence",
        "confidence" in degradation and "note" in degradation.lower(),
        evidence="degradation clause does not point to per-row confidence + notes",
    )

    js_prompt = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    check(
        "js-schema-row-notes",
        '"note"' in js_prompt or '"notes"' in js_prompt,
        evidence="JS schema missing per-row note(s)",
    )
    check(
        "js-no-note-in-field",
        "append a brief note to the relevant field" not in js_prompt,
        evidence="JS degradation clause still asks for inline notes",
    )


def test_japanese_and_russian_mappings_present():
    """MEDIUM: Japanese + Russian stratigraphic-suffix mappings must be present.

    Japanese: 系/統/階/群/組/段/帯
    Russian:  system / series / age / group / suite / svita / zona (approximate)
    """
    # Japanese — 系 is the standard Japanese term for System.
    for jp in ["系 =", "統 =", "階 =", "群 =", "組 =", "段 =", "帯 ="]:
        check(
            f"py-jp-mapping:{jp.strip()}",
            jp in RANGE_CHART_SYSTEM_PROMPT,
            evidence=f"missing Japanese mapping for {jp}",
        )

    # Russian — at least the high-value suffixes
    for ru in ["svita", "zona", "group", "suite"]:
        check(
            f"py-ru-mapping:{ru}",
            ru in RANGE_CHART_SYSTEM_PROMPT.lower(),
            evidence=f"missing Russian mapping for {ru}",
        )

    js_prompt = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    for jp in ["系 =", "統 =", "階 =", "群 =", "組 =", "段 =", "帯 ="]:
        check(
            f"js-jp-mapping:{jp.strip()}",
            jp in js_prompt,
            evidence=f"missing Japanese mapping in JS prompt for {jp}",
        )


def test_structured_range_endpoints():
    """LOW: range endpoints must include structured fields for FAD/LAD, bed idx, observed vs projected.

    schema additions expected:
        range_top_bed, range_base_bed, range_top_idx, range_base_idx,
        endpoint_kind (observed | projected | truncated)
    """
    for field in ["range_top_bed", "range_base_bed", "range_top_idx", "range_base_idx", "endpoint_kind"]:
        check(
            f"py-endpoint:{field}",
            field in RANGE_CHART_SYSTEM_PROMPT,
            evidence=f"missing structured field {field}",
        )
    js_prompt = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    for field in ["range_top_bed", "range_base_bed", "range_top_idx", "range_base_idx", "endpoint_kind"]:
        check(
            f"js-endpoint:{field}",
            field in js_prompt,
            evidence=f"missing structured field {field} in JS prompt",
        )


def test_reworked_flag_present():
    """LOW: schema must include a reworked / in-situ occurrence flag."""
    check(
        "py-reworked-flag",
        "reworked" in RANGE_CHART_SYSTEM_PROMPT.lower(),
        evidence="reworked flag missing from prompt",
    )
    js_prompt = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    check(
        "js-reworked-flag",
        "reworked" in js_prompt.lower(),
        evidence="reworked flag missing from JS prompt",
    )


def test_author_year_requested():
    """LOW: prompt must request author/year explicitly.

    The schema declares `author_year`; the prompt must ask for it.
    """
    check(
        "py-author-year",
        "author" in RANGE_CHART_SYSTEM_PROMPT.lower() and "year" in RANGE_CHART_SYSTEM_PROMPT.lower(),
        evidence="author/year not explicitly requested",
    )
    js_prompt = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    check(
        "js-author-year",
        "author" in js_prompt.lower() and "year" in js_prompt.lower(),
        evidence="author/year not explicitly requested in JS prompt",
    )


def test_byte_identical_python_vs_js():
    """HIGH: the Python and JS range-chart prompts MUST be byte-identical.

    After all fixes, the joined string on both sides must match exactly.
    """
    js_prompt = _extract_js_prompt_const(_read_js_source(), "RANGE_CHART_SYSTEM_PROMPT")
    check(
        "py-vs-js-range-prompt-byte-identical",
        RANGE_CHART_SYSTEM_PROMPT == js_prompt,
        evidence=(
            "drift detected: "
            f"py len={len(RANGE_CHART_SYSTEM_PROMPT)} js len={len(js_prompt)}"
        ),
    )


def test_prompt_version_both_sides():
    """HIGH: PROMPT_VERSION must be a per-mode dict and match on both sides.

    Updated 2026-08-06: the original test asserted ``PROMPT_VERSION == "v3"``
    (a string), which stopped matching when PROMPT_VERSION became a per-mode
    dict — the stale assertion failed only when this file is run directly
    (``python tests/test_prompt_fixes.py``), not under pytest (check-style
    tests don't assert). The canonical mode keys must resolve to the same
    version on the Python and JS sides; the legacy "abundance" alias must
    remain consistent with the canonical "abundance_diagram" key.
    """
    check("py-prompt-version-dict",
          isinstance(PROMPT_VERSION, dict) and len(PROMPT_VERSION) >= 5,
          evidence=f"py={PROMPT_VERSION!r}")
    for mode in ("range_chart", "columnar_section", "abundance_diagram",
                 "phylogenetic_tree", "chemical_stratigraphy",
                 "scatter_plot"):
        check(f"py-prompt-version-{mode}", bool(PROMPT_VERSION.get(mode)),
              evidence=f"mode={mode} py={PROMPT_VERSION!r}")
    check("py-abundance-alias-consistent",
          PROMPT_VERSION.get("abundance") == PROMPT_VERSION.get("abundance_diagram"),
          evidence=f"py={PROMPT_VERSION!r}")
    js_versions = _js_prompt_versions()
    for mode in ("range_chart", "columnar_section", "abundance_diagram",
                 "phylogenetic_tree", "abundance", "chemical_stratigraphy",
                 "scatter_plot"):
        check(f"js-prompt-version-{mode}",
              js_versions.get(mode) == PROMPT_VERSION.get(mode),
              evidence=f"mode={mode} js={js_versions.get(mode)!r} py={PROMPT_VERSION.get(mode)!r}")


def test_contract_modes_python_vs_js():
    """HIGH: the four BORROW-2026-09-20 contract modes must mirror Python.

    Added when the extraction contract (three-state ``response_kind``, the
    closed ``reason_codes`` list, the ``*_pos_0_999`` fields and the root
    ``axis_calibration`` block) landed in rca_core/prompt.py. Two layers:

    * CHEMICAL_STRATIGRAPHY / SCATTER_PLOT — hard asserts on the per-mode
      PROMPT_VERSION AND on the full evaluated prompt text being byte
      identical. These two modes had no prompt-parity coverage at all before
      (the loops above are check()-style and print-only under pytest), so
      drift here was silent; from now on it fails the run.
    * RANGE_CHART / ABUNDANCE_DIAGRAM — hard assert ONLY on the generated
      (A)+(B) clause block (the new contract text, which must be identical
      by definition), plus a print-only byte-identical report for the rest
      of the prompt so the legacy drift stays visible without breaking.

    The comparison runs js/prompt.js through ``node`` + vm (same loading as
    tests_frontend.js) because the clause block is generated at runtime on
    both sides and the raw-source scraper above can not see it.
    """
    # 1) Versions of the two newly covered modes: real assert.
    js_versions = _js_prompt_versions()
    for mode in ("chemical_stratigraphy", "scatter_plot"):
        assert js_versions.get(mode) == PROMPT_VERSION.get(mode), (
            f"PROMPT_VERSION drift for {mode}: "
            f"js={js_versions.get(mode)!r} py={PROMPT_VERSION.get(mode)!r}")

    # 2) Prompt text. (js const, python text, byte-identical hard?)
    specs = [
        ("RANGE_CHART_SYSTEM_PROMPT", "range_chart",
         RANGE_CHART_SYSTEM_PROMPT, False),
        ("ABUNDANCE_DIAGRAM_SYSTEM_PROMPT", "abundance_diagram",
         ABUNDANCE_DIAGRAM_SYSTEM_PROMPT, False),
        ("CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT", "chemical_stratigraphy",
         CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT, True),
        ("SCATTER_PLOT_SYSTEM_PROMPT", "scatter_plot",
         SCATTER_PLOT_SYSTEM_PROMPT, True),
    ]
    js_texts = _eval_js_prompt_consts([s[0] for s in specs])
    for (const_name, mode, py_text, hard), js_text in zip(specs, js_texts):
        # The generated contract block must be byte-identical in EVERY
        # contract mode (minimum mirroring requirement) — hard assert.
        block = _contract_block(py_text)
        if block not in js_text:
            js_block = (_contract_block(js_text)
                        if "ANSWER EVERY REQUESTED CELL" in js_text else "")
            print(_diff_report(block, js_block))
        assert block in js_text, (
            f"{const_name}: the generated (A)+(B) contract clause block "
            f"drifted from rca_core/prompt.py (see diff above)")
        # The *pos_0_999 field names the contract asks for must be present.
        for field, _what in POS_FIELDS[mode]:
            assert f'"{field}"' in js_text, (
                f"{const_name}: schema field {field!r} missing in JS prompt")
        if hard:
            if py_text != js_text:
                print(_diff_report(py_text, js_text))
            assert py_text == js_text, (
                f"{const_name} drift: py len={len(py_text)} "
                f"js len={len(js_text)}")
        else:
            # Legacy modes: report evaluated drift without breaking.
            check(f"py-vs-js-{const_name}-evaluated-byte-identical",
                  py_text == js_text,
                  evidence=f"py len={len(py_text)} js len={len(js_text)}")


if __name__ == "__main__":
    test_no_genus_bias_instruction()
    test_no_p2_fix_tag()
    test_degradation_clause_references_per_row_fields()
    test_japanese_and_russian_mappings_present()
    test_structured_range_endpoints()
    test_reworked_flag_present()
    test_author_year_requested()
    test_byte_identical_python_vs_js()
    test_prompt_version_both_sides()
    test_contract_modes_python_vs_js()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(1 if _fail else 0)