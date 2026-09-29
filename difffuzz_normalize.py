"""Differential fuzzer: the five rca_core normalizers  vs  their js/ mirrors.

WHY THIS EXISTS
---------------
The normalizers are the widest mirror surface in the repo and the one that
meets the least trustworthy input: they turn RAW LLM JSON into the scientific
structures everything downstream trusts. The committed parity gate covers
them with 62 fixtures, all hand-written, all structurally valid. The defects
already found here by reading (a bare `str(v)` leaking a Python repr into a
coordinate field, `norm_coords` dropping real coordinate strings, a dict
coordinate crashing the paleomap path) were all cases where the input was
MALFORMED -- and no fixture was malformed, so none of them could have been
caught there.

So the generator below is built to produce what a model actually emits:
right keys with wrong scalar types, lists where dicts belong, nulls in place
of rows, numbers as strings, booleans where counts belong, and empty
containers. That is the space the hand-written fixtures skip entirely.

Both engines mutate their argument IN PLACE, so each gets its own deep copy;
passing one payload to both would have the first engine's rewrite leak into
the second's input and manufacture a thousand phantom divergences.

KNOWN AND EXPECTED
------------------
`rc_dict_shaped_sections` — a list replaced by a DICT whose keys are
ARRAY-INDEX-like ("0", "1"): ECMAScript enumerates integer-like keys first,
JSON.parse already lost the order, so the row SEQUENCE differs. Values match.
Already in EXPECTED_DIVERGENCES; the generator uses non-integer dict keys so
this stays out of the way unless you deliberately aim at it.

NOT IN CI, DELIBERATELY
-----------------------
A root `tests_*.py` would have to be added to the explicit list in
.github/workflows/ci.yml. Pinned cases live in
tests/fixtures/frontend_parity_2026_09_20.json instead.

USAGE
-----
    python difffuzz_normalize.py
    python difffuzz_normalize.py 2000 4242 --list
    python difffuzz_normalize.py 500 7 range_chart
"""
import copy
import json
import os
import random
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from rca_core.extractor import (  # noqa: E402
    normalize_abundance_result,
    normalize_columnar_result,
    normalize_result,
    normalize_zonation_chart_result,
    _normalize_phylogenetic_tree_into,
)

# Same load list and same context shape as tests_diff_frontend_parity.js, in
# the same order. quality.js reads the ICS table and the contract mirror at
# CALL time, so a shorter list puts the two engines on different code paths.
JS_DRIVER = r"""
const fs = require('fs'); const vm = require('vm'); const path = require('path');
const REPO = process.env.RCA_REPO;
const SCRIPTS = ['js/config.js', 'js/json-utils.js', 'js/ics_table.js',
                 'js/reason-codes.js', 'js/quality.js', 'js/aggregate.js',
                 'js/ics_table.js', 'js/table.js', 'js/export.js',
                 'js/prompt.js', 'js/minimax.js'];
const store = new Map();
const ctx = {
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  TextEncoder, TextDecoder, URL,
  fetch: async () => { throw new Error('network disabled'); },
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  document: {
    documentElement: { style: {}, dataset: {},
      classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
      setAttribute() {}, removeAttribute() {}, getAttribute: () => null },
    addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true,
    querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
    createElement: () => ({ style: {}, appendChild() {} }),
    body: { appendChild() {}, removeChild() {},
            classList: { add() {}, remove() {} } },
  },
  alert() {}, prompt: () => '', confirm: () => false,
  location: { href: 'http://localhost/', origin: 'http://localhost' },
  navigator: { language: 'en' },
};
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);
for (const rel of SCRIPTS) {
  vm.runInContext(fs.readFileSync(path.join(REPO, rel), 'utf8'), ctx, { filename: rel });
}
vm.runInContext(`
  globalThis.__exp = {
    normalizeResult: typeof rcaNormalizeResult !== 'undefined' ? rcaNormalizeResult : null,
    normalizeColumnar: typeof rcaNormalizeColumnarResult !== 'undefined' ? rcaNormalizeColumnarResult : null,
    normalizeAbundance: typeof rcaNormalizeAbundanceResult !== 'undefined' ? rcaNormalizeAbundanceResult : null,
    normalizeZonation: typeof rcaNormalizeZonationChartResult !== 'undefined' ? rcaNormalizeZonationChartResult : null,
    normalizePhylo: typeof rcaNormalizePhylogeneticTreeResult !== 'undefined' ? rcaNormalizePhylogeneticTreeResult : null,
  };
`, ctx);
const F = ctx.__exp;
for (const k of Object.keys(F)) {
  if (typeof F[k] !== 'function') {
    process.stderr.write('normalizer missing: ' + k + ' (SCRIPTS list is stale)\n');
    process.exit(3);
  }
}
const RUN = {
  range_chart: F.normalizeResult, columnar_section: F.normalizeColumnar,
  abundance_diagram: F.normalizeAbundance, zonation_chart: F.normalizeZonation,
  phylogenetic_tree: F.normalizePhylo,
};
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(cases.map((c) => {
  try { return { ok: true, v: RUN[c.mode](c.payload) }; }
  catch (e) { return { ok: false, e: String((e && e.message) || e) }; }
})));
"""

# --- what a model actually emits -------------------------------------------
SCALARS = [
    "Bed 7", "bed 7", "  Bed 7  ", "", None, 0, 1, 12.5, -3, True, False,
    "26٠ Ma", "２６０ Ma", "sp.", "cf. sp.", "aff. sp.", "ø", "a,b", "a-b",
    "9", "10", "NaN", "[]", "{}", "null", "Wuchiapingian", "晚二叠世",
]
BAD_SCALARS = [{"lat": 1}, {"lat": 1, "lon": 2}, {"a": 1}, [1, 2], [[]], {},
               "48.1N 12.0E", (1, 2)]
LISTS = [[], ["a"], ["a", "a"], [1, 2], [None], ["a", None, "b"],
         {"a": 1}, "not-a-list", 42, True]


def sc(rng):
    return rng.choice(SCALARS + BAD_SCALARS) if rng.random() < 0.12 else rng.choice(SCALARS)


def maybe(rng, v, p=0.7):
    return v if rng.random() < p else None


def range_payload(rng):
    p = {}
    if rng.random() < 0.9:
        p["sections"] = ([{"name": rng.choice(["S1", "s1", " S1 ", ""]),
                           "columns": rng.choice(LISTS),
                           "age_range": sc(rng)}]
                         if rng.random() < 0.9 else rng.choice(LISTS))
    if rng.random() < 0.9:
        p["species_ranges"] = ([{
            "species": sc(rng), "section": sc(rng),
            "range_base": sc(rng), "range_top": sc(rng),
            "biozone": sc(rng), "confidence": rng.choice(SCALARS),
            "note": sc(rng), "author": sc(rng),
            "range_base_idx": rng.choice([1, 2, "3", None, {}]),
            "range_top_idx": rng.choice([5, 6, "7", None, []]),
            "response_kind": rng.choice(["extracted", "not_drawn", "bogus", None]),
            "reason_codes": rng.choice([["crosses_top"], "crosses_top", [], None]),
        } for _ in range(rng.randint(0, 4))]
            if rng.random() < 0.92 else rng.choice(LISTS))
    for k in ("biozones", "other_fossils", "fossil_sites", "paleomap"):
        if rng.random() < 0.3:
            p[k] = ([{"name": sc(rng), "note": sc(rng),
                      "response_kind": rng.choice(["extracted", "legend_only", None])}
                     for _ in range(rng.randint(0, 3))]
                    if rng.random() < 0.85 else rng.choice(LISTS))
    p["confidence"] = rng.choice(SCALARS)
    if rng.random() < 0.2:
        p["unknown_future_key"] = [sc(rng) for _ in range(rng.randint(0, 2))]
    return p


def columnar_payload(rng):
    p = {"confidence": rng.choice(SCALARS)}
    p["sections"] = ([{
        "id": sc(rng), "group": sc(rng),
        "coordinates_text": sc(rng), "thickness_m": sc(rng),
        "lithology": sc(rng),
        "coordinates": rng.choice([None, {"lat": 1}, {"lat": 1.5, "lon": 2.5},
                                   [1, 2], "48.1N 12.0E", "1,2", 42, []]),
    } for _ in range(rng.randint(0, 4))] if rng.random() < 0.9 else rng.choice(LISTS))
    for k in ("fossil_legend", "lithology_legend", "cross_beds"):
        if rng.random() < 0.35:
            p[k] = [{"name": sc(rng), "note": sc(rng)} for _ in range(rng.randint(0, 3))]
    return p


def abundance_payload(rng):
    p = {"confidence": rng.choice(SCALARS)}
    p["abundances"] = ([{
        "site": sc(rng), "taxon": sc(rng), "level": sc(rng),
        "depth": sc(rng), "abundance": sc(rng), "abundance_unit": sc(rng),
    } for _ in range(rng.randint(0, 4))] if rng.random() < 0.9 else rng.choice(LISTS))
    for k in ("sites", "zones"):
        if rng.random() < 0.35:
            p[k] = [{"name": sc(rng), "note": sc(rng)} for _ in range(rng.randint(0, 3))]
    return p


def zonation_payload(rng):
    p = {"confidence": rng.choice(SCALARS)}
    p["zones"] = ([{
        "zonation": sc(rng), "name": sc(rng), "rank": sc(rng),
        "age_span": sc(rng), "base_age": sc(rng), "top_age": sc(rng),
        "stage": sc(rng), "defined_by": sc(rng), "note": sc(rng),
    } for _ in range(rng.randint(0, 4))] if rng.random() < 0.9 else rng.choice(LISTS))
    for k in ("zonations", "correlations"):
        if rng.random() < 0.35:
            if k == "correlations":
                p[k] = [{"from_zonation": sc(rng), "from_zone": sc(rng),
                         "to_zonation": sc(rng), "to_zone": sc(rng),
                         "note": sc(rng)} for _ in range(rng.randint(0, 3))]
            else:
                p[k] = [{"name": sc(rng), "note": sc(rng)} for _ in range(rng.randint(0, 3))]
    return p


def phylo_payload(rng):
    # AUDIT-2026-09-30: this used to be 400/400 vacuous. The tool reported
    # "MISMATCHED=0 ... engines agree" for the phylogenetic_tree mode while
    # actually comparing NOTHING: agreed=0, agreed_refusal=400, py_raised=400,
    # js_raised=400. Two independent reasons, both in the GENERATOR, not in the
    # code under test:
    #
    #   * node ids were sc(rng) -- an arbitrary scalar, so the chosen root_ids
    #     (["n1"], "n1", ...) almost never named a real node, and
    #   * the parent key was spelled "parent_id", which
    #     _normalize_phylogenetic_tree_into does not read. It reads "parent",
    #     so every node came in parentless and any non-root raised
    #     "Non-root node must have a parent".
    #
    # Either one alone would have made the whole mode raise before reaching a
    # single line of normalisation. "Both engines refused" was being counted
    # as agreement, which is the same blind spot as a group that exists but
    # covers nothing.
    p = {"confidence": rng.choice(SCALARS)}
    if rng.random() < 0.10:
        # Keep a MINORITY of deliberately unusable payloads, so the
        # "both engines refuse the same input" path stays exercised -- as a
        # minority, which is the point.
        p["nodes"] = ([{"id": sc(rng), "parent": sc(rng)}]
                      if rng.random() < 0.5 else [])
        p["root_ids"] = rng.choice([[], None, 42, "nope", "n1"])
        p["metadata"] = rng.choice([{}, None, "str", [1]])
        p["legend"] = rng.choice([{}, None, "str", [1]])
        return p

    # A well-formed tree: ids are generated first so root_ids and parents can
    # only reference nodes that exist, and the first node is the root.
    n = rng.randint(1, 5)
    ids = ["n%d" % i for i in range(1, n + 1)]
    nodes = []
    for i, nid in enumerate(ids):
        nodes.append({
            "id": nid,
            "parent": None if i == 0 else rng.choice(ids[:i]),
            "name": rng.choice([nid, "taxon " + nid, "", None, sc(rng)]),
            "is_leaf": rng.choice([True, False, None, "yes"]),
            # branch_length / node_age_ma / support go through float coercion;
            # support is range-checked, so keep it in [0, 100] here and let
            # the degenerate branch above be the one that exercises the guard.
            "branch_length": rng.choice([0.0, 0.5, 1.5, "1.5", "", None]),
            "node_age_ma": rng.choice([300.0, 250.5, "250", "", None]),
            "support": rng.choice([100, 95.5, 0, None, ""]),
        })
    p["nodes"] = nodes
    p["root_ids"] = ([ids[0]] if rng.random() < 0.8
                     else [rng.choice(ids) for _ in range(rng.randint(1, n))])
    p["metadata"] = rng.choice([{}, {"title": "x"}, None, "str", [1]])
    p["legend"] = rng.choice([{}, {"a": 1}, None, "str", [1]])
    return p


MODES = {
    "range_chart": (range_payload, normalize_result),
    "columnar_section": (columnar_payload, normalize_columnar_result),
    "abundance_diagram": (abundance_payload, normalize_abundance_result),
    "zonation_chart": (zonation_payload, normalize_zonation_chart_result),
    "phylogenetic_tree": (phylo_payload, _normalize_phylogenetic_tree_into),
}


def gen_cases(mode, n, seed):
    factory, _ = MODES[mode]
    rng = random.Random(seed)
    return [{"i": i, "mode": mode, "payload": factory(rng)} for i in range(n)]


def norm(v):
    """Engine-neutral shape. UNKNOWN TYPES ARE AN ERROR, not a coercion.

    An earlier version ended in `return str(v)`, which silently stringified
    any type it did not recognise -- including a Python tuple -- and so turned
    "one engine produced a different container" into an innocent-looking
    string comparison. It cost a full round of chasing a divergence that
    eight isolated variants could not reproduce. Anything JSON cannot carry
    has to be surfaced, because it is exactly the thing worth looking at.
    """
    if isinstance(v, dict):
        return {k: norm(v[k]) for k in sorted(v, key=str)}
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 9)
    raise TypeError("difffuzz: unhandled type %r (%s) -- report it, do not coerce"
                    % (type(v).__name__, v))


#: Field paths whose divergence is an ALREADY-ADJUDICATED open decision, not
#: a fresh finding. Kept in lockstep with EXPECTED_DIVERGENCES in
#: tests_diff_frontend_parity.js, which registers the same two as
#: rc_root_confidence_nan and rc_row_confidence_nan.
#:
#: Both are the same defect in two costumes: Python clamps a confidence it
#: cannot parse to 1.0 -- FULL CERTAINTY -- while the JS mirror clamps to 0.0
#: at the root and None per row. "Unmeasurable" reading as "maximally
#: certain" is precisely the blind spot this whole review wave is about, and
#: it is why the direction is a product decision rather than a patch: fixing
#: it here would silently invalidate 200+ recorded parity fixtures.
EXPECTED_FIELD_DIVERGENCES = (
    ".confidence",                  # root-level confidence
    ".species_ranges[N].confidence",  # per-row confidence
)

_ROW_CONFIDENCE_SUFFIX = ".confidence"


def is_expected_divergence(path):
    """True when `path` is a parked, deliberately-unaligned field.

    `path` is one entry of a ``diff_paths`` result, i.e. the field PATH with
    the observed values already appended ("`confidence: py=1.0 js=0.0`"), so
    the path is taken as everything before the first ": ".

    Compares by shape rather than by regex, so it does not need `re` imported
    above this point, and so an index in the path (species_ranges[7]) cannot
    hide a divergence that the same field at another index would report.
    """
    head = path.split(": ", 1)[0].strip()
    if head in EXPECTED_FIELD_DIVERGENCES:
        return True
    prefix, sep, _tail = head.rpartition(_ROW_CONFIDENCE_SUFFIX)
    if not sep or not prefix.startswith(".species_ranges["):
        return False
    return prefix.endswith("]") and prefix[16:-1].isdigit()


def diff_paths(a, b, path="", out=None, limit=10):
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if type(a) is not type(b) and not (isinstance(a, (int, float))
                                       and isinstance(b, (int, float))):
        out.append("%s: py=%r js=%r" % (path, a, b))
        return out
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append("%s.%s: missing in py (js=%r)" % (path, k, b[k]))
            elif k not in b:
                out.append("%s.%s: missing in js (py=%r)" % (path, k, a[k]))
            else:
                diff_paths(a[k], b[k], "%s.%s" % (path, k), out, limit)
            if len(out) >= limit:
                break
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append("%s: len py=%d js=%d" % (path, len(a), len(b)))
        for i in range(min(len(a), len(b))):
            diff_paths(a[i], b[i], "%s[%d]" % (path, i), out, limit)
            if len(out) >= limit:
                break
    elif a != b:
        out.append("%s: py=%r js=%r" % (path, a, b))
    return out


def run_mode(mode, n, seed, show_all):
    _, py_fn = MODES[mode]
    cases = gen_cases(mode, n, seed)
    with tempfile.TemporaryDirectory() as d:
        cp = os.path.join(d, "c.json")
        dp = os.path.join(d, "d.js")
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False)
        with open(dp, "w", encoding="utf-8") as f:
            f.write(JS_DRIVER)
        proc = subprocess.run(["node", dp, cp], capture_output=True, text=True,
                              encoding="utf-8", env=dict(os.environ, RCA_REPO=REPO))
    if proc.returncode != 0:
        print("[%s] node driver failed (rc=%d)\n%s"
              % (mode, proc.returncode, proc.stderr[:1500]))
        return 2
    js_out = json.loads(proc.stdout)

    mismatch = py_raised = js_raised = agree = agree_refuse = 0
    expected = 0
    examples = []
    for c, jr in zip(cases, js_out):
        try:
            pr = norm(py_fn(copy.deepcopy(c["payload"])))   # mutates in place
            py_ok = True
        except Exception as exc:
            pr, py_ok = "%s: %s" % (type(exc).__name__, exc), False
        if not py_ok:
            py_raised += 1
        if not jr.get("ok"):
            js_raised += 1
        if py_ok and jr.get("ok"):
            d_ = diff_paths(pr, norm(jr["v"]))
            if d_:
                # AUDIT-2026-09-29: a permanently-red tool is a tool nobody
                # runs twice. The two confidence-clamping paths are already
                # adjudicated as open product decisions (EXPECTED_DIVERGENCES
                # in tests_diff_frontend_parity.js, as rc_root_confidence_nan
                # and rc_row_confidence_nan), so they are counted separately
                # instead of as fresh findings. exit 0 now means "nothing NEW
                # diverged" rather than "the two known ones happened again".
                new_d = [p for p in d_ if not is_expected_divergence(p)]
                expected += len(d_) - len(new_d)
                if new_d:
                    mismatch += 1
                    if show_all or len(examples) < 3:
                        examples.append((c, new_d, pr, jr["v"]))
                else:
                    agree += 1
            else:
                agree += 1
        elif (not py_ok) and (not jr.get("ok")):
            # Both refused. Python prefixes its exception class, JS does not,
            # so compare the message tail before calling it agreement --
            # a TypeError where the mirror raises a domain error is a REAL
            # difference in failure mode, not two spellings of one refusal.
            tail = pr.split(": ", 1)[-1].strip()
            if tail == str(jr.get("e")).strip():
                agree_refuse += 1
            else:
                mismatch += 1
                if show_all or len(examples) < 3:
                    examples.append((c, ["refusal text differs"], pr, jr.get("e")))
        else:
            mismatch += 1
            if show_all or len(examples) < 3:
                examples.append((c, ["one engine raised: py_ok=%s js_ok=%s"
                                    % (py_ok, jr.get("ok"))], pr,
                                 jr.get("e") if not jr.get("ok") else jr.get("v")))
    print("[%s] cases=%d seed=%d agreed=%d agreed_refusal=%d py_raised=%d "
          "js_raised=%d MISMATCHED=%d expected_parked=%d"
          % (mode, n, seed, agree, agree_refuse, py_raised, js_raised,
             mismatch, expected))
    for c, d_, pr, jv in examples:
        print("=" * 72)
        print("  #%d  payload=%s" % (c["i"], json.dumps(c["payload"], ensure_ascii=False)[:400]))
        for line in d_[:6]:
            print("   ", line)
        print("     py:", json.dumps(pr, ensure_ascii=False, default=str)[:260])
        print("     js:", json.dumps(jv, ensure_ascii=False, default=str)[:260])
    return 1 if mismatch else 0


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    show_all = "--list" in argv
    n = int(args[0]) if args else 400
    seed = int(args[1]) if len(args) > 1 else 20260927
    want = args[2] if len(args) > 2 else "all"
    modes = sorted(MODES) if want == "all" else [want]
    if any(m not in MODES for m in modes):
        print("known modes: %s" % ", ".join(sorted(MODES)))
        return 2
    rc = 0
    for m in modes:
        rc |= run_mode(m, n, seed, show_all)
    print()
    print("overall: %s" % ("DIVERGENT" if rc else "engines agree"))
    return rc


def _rca_crashproof_stdout():
    """Dev tool: payloads are printed verbatim and can hold any character.
    On a GBK console that is a fatal UnicodeEncodeError, which reads like
    "the fuzzer found something" when it found nothing. Make stdout lossy."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


_rca_crashproof_stdout()

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
