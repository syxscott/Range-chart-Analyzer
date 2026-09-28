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
    p = {"confidence": rng.choice(SCALARS)}
    p["nodes"] = ([{
        "id": sc(rng), "name": sc(rng), "parent_id": sc(rng),
        "branch_length": sc(rng), "legend": sc(rng),
    } for _ in range(rng.randint(0, 4))] if rng.random() < 0.9 else rng.choice(LISTS))
    p["root_ids"] = rng.choice([[], ["n1"], ["n1", "n2"], "n1", None, 42])
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
                mismatch += 1
                if show_all or len(examples) < 3:
                    examples.append((c, d_, pr, jr["v"]))
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
          "js_raised=%d MISMATCHED=%d"
          % (mode, n, seed, agree, agree_refuse, py_raised, js_raised, mismatch))
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
