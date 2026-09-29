"""Differential fuzzer: rca_core/aggregate.py  vs  js/aggregate.js.

WHY THIS EXISTS
---------------
`tests_diff_frontend_parity.js` is the CI gate for Python/JS parity, but it
only replays a FIXED set of fixtures. A fixed set only covers the shapes
someone thought to write down, so it missed AUDIT-2026-09-27 [P2]: the
recombination-ballot tie-break compared `JSON.stringify(tup)` in JS against
the tuple in Python, and no fixture had two ballot values in a prefix
relationship ("bed 1" vs "bed 1 (rp13)"). Randomised inputs find that class
without anyone having to imagine it.

It found the same class of defect in FOUR other places the moment the
generators were extended past range_chart, so the value pools below are
built to stress comparison and ordering, not just values:

  * prefix pairs          "bed 1"  vs  "bed 1 (rp13)"
  * a comma INSIDE a value "x,y"    -- 0x2C is the JSON separator
  * a hyphen vs a space   "a-b"    vs "a b"   -- 0x2D vs 0x20
  * numeric vs lexicographic  "9" vs "10" -- string order is the REVERSE

NOT IN CI, DELIBERATELY
-----------------------
A root `tests_*.py` would have to be added to the explicit list in
.github/workflows/ci.yml, and a 2000-case run adds a node subprocess per
batch to every build. The pinned regression cases live where the gate can
see them instead (tests/fixtures/frontend_parity_2026_09_20.json, generated
by tests/gen_frontend_parity_fixtures.py). This file is the tool you reach
for when you CHANGE a merge rule and want to know what else moved.

USAGE
-----
    python difffuzz_aggregate.py                  # 400 cases, all 5 schemas
    python difffuzz_aggregate.py 2000 12345       # cases, seed
    python difffuzz_aggregate.py 400 7 zonation_chart
    python difffuzz_aggregate.py 400 7 all --list # print every mismatch

Exit code 0 when the engines agree on every case, 1 otherwise.
Deterministic: the same (cases, seed, mode) always produce the same inputs.
"""
import json
import os
import random
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from rca_core.aggregate import SCHEMA_BY_MODE, merge_results  # noqa: E402

# The JS mirror is a browser script with no export, so it is evaluated in a vm
# context and reached through the sandbox global. `RCA_KEYMAP_BY_MODE` is a
# top-level `const`, which does NOT become a sandbox property, so the driver
# re-exports it explicitly instead of us duplicating the keymaps in Python --
# duplicating them would let this tool drift from the code it is checking.
JS_DRIVER = r"""
const fs = require('fs'); const vm = require('vm'); const path = require('path');
const REPO = process.env.RCA_REPO;
const sb = { console }; sb.window = sb; sb.globalThis = sb;
vm.createContext(sb);
// index.html:431-432 loads these with `defer`, which preserves order, and the
// comment there says why: rcaMergeContractField() BAILS OUT when the contract
// mirror is absent, degrading response_kind to a silent mode vote. Without
// this line every case reports a false divergence -- the Python side always
// has the module, so the two engines were never actually being compared.
for (const f of ['js/reason-codes.js', 'js/aggregate.js']) {
  vm.runInContext(fs.readFileSync(path.join(REPO, f), 'utf8'), sb, { filename: f });
}
if (typeof sb.rcaMergeResponseKinds !== 'function'
    || typeof sb.rcaMergeReasonCodes !== 'function'
    || typeof sb.rcaContractTruthy !== 'function') {
  process.stderr.write('reason-codes.js did not expose its API -- the '
    + 'contract merge would silently degrade and every case would be a false positive\n');
  process.exit(3);
}
vm.runInContext(';globalThis.__KM = RCA_KEYMAP_BY_MODE;', sb);
if (typeof sb.rcaMergeResults !== 'function' || !sb.__KM) {
  process.stderr.write('js/aggregate.js did not expose the API/keymaps\n');
  process.exit(3);
}
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = [];
for (const c of cases) {
  try {
    out.push({ ok: true, value: sb.rcaMergeResults(c.runs, c.total_runs,
                                                   sb.__KM[c.mode] || null) });
  } catch (e) { out.push({ ok: false, error: String((e && e.message) || e) }); }
}
process.stdout.write(JSON.stringify(out));
"""

# --- adversarial value pools ------------------------------------------------
# Pairs marked (P) are prefix-related; (C) contains a JSON separator;
# (N) inverts under numeric vs lexicographic ordering.
PREFIX = ["bed 1", "bed 1 (rp13)", "zone b", "zone b2", "a", "aa", "a b", "a-b",
          "c,d", "e f", "e,f", "9", "10", "100", "2", "20", "p1", "p10",
          "Bed 7", "bed 7", "Bed 7 ", "  Bed 7", "Zone A", "Sample 1 (RP13)"]
NUMS = [0.9, 0.5, 1.0, 0.80, "0.8", "high", None]


def _pick(rng, pool, p=0.85):
    return rng.choice(pool) if rng.random() < p else None


def _range_row(rng):
    r = {"species": rng.choice(["Radiolaria sp.", "radiolaria sp.",
                                 "  Radiolaria sp.  ", "Radiolaria cf. sp.",
                                 "Radiolaria", "Genus species", "Genus  species",
                                 "Sp. A", "ø species"])}
    r["section"] = _pick(rng, ["Section 1", "section 1", "Section 1 ",
                               "Section 2", ""])
    r["range_base"] = _pick(rng, PREFIX)
    r["range_top"] = _pick(rng, PREFIX)
    r["biozone"] = _pick(rng, PREFIX, 0.6)
    if rng.random() < 0.4:
        r["confidence"] = rng.choice(NUMS)
    if rng.random() < 0.3:
        r["note"] = rng.choice(["note a", "note b", ""])
    if rng.random() < 0.25:
        r["range_base_idx"] = rng.choice([1, 2, "3", None])
    if rng.random() < 0.25:
        r["range_top_idx"] = rng.choice([5, 6, "7", None])
    if rng.random() < 0.2:
        r["_extras"] = {"k": rng.choice(["v1", "v2", 1, 2])}
    if rng.random() < 0.1:
        r["_warning"] = rng.choice(["warn a", "warn b"])
    return r


def _named(rng, extra_key=None):
    item = {"name": rng.choice(PREFIX)}
    if extra_key:
        item[extra_key] = rng.choice(PREFIX)
    if rng.random() < 0.4:
        item["note"] = rng.choice(["n1", "n2", ""])
    if rng.random() < 0.3:
        item["response_kind"] = rng.choice(["extracted", "not_drawn", "legend_only"])
    return item


def _columnar_row(rng):
    return {
        "id": rng.choice(PREFIX), "group": _pick(rng, PREFIX),
        "coordinates_text": _pick(rng, PREFIX, 0.5),
        "thickness_m": _pick(rng, ["1.5", "1.50", "2", "10", "9", ""], 0.5),
        "lithology": _pick(rng, ["shale", "Shale", "siltstone"], 0.4),
    }


def _abundance_row(rng):
    return {
        "site": rng.choice(["S1", "s1", "S1 ", "S2"]),
        "taxon": rng.choice(["Pinus", "pinus", "Pinus sp.", "Quercus", "ø sp."]),
        "level": _pick(rng, ["1", "2", "10", "9", ""]),
        "depth": _pick(rng, PREFIX),
        "abundance": _pick(rng, ["12", "12.0", "7", ""]),
        "abundance_unit": _pick(rng, ["%", "count", ""], 0.4),
    }


def _phylo_row(rng):
    return {"id": rng.choice(["n1", "n10", "n1x", "2", "10", "n1 (rp13)"]),
            "name": rng.choice(["Taxus", "taxus", "Taxus sp.", "Taxus cf. sp."]),
            "parent_id": _pick(rng, ["n0", "root", ""], 0.5),
            "branch_length": _pick(rng, ["0.1", "0.10", "1", "2"], 0.4)}


def _zonation_row(rng):
    return {
        "zonation": rng.choice(["Zone A", "zone a", "Zone B", "Zone A ", ""]),
        "name": rng.choice(["Zone A", "Zone B", "zone b", "Zone B2", "A"]),
        "rank": _pick(rng, ["biozone", "Biozone", ""], 0.4),
        "age_span": _pick(rng, PREFIX, 0.4),
        "base_age": _pick(rng, ["9", "10", "100", "Ma"], 0.4),
        "top_age": _pick(rng, ["9", "10", "100", "Ma"], 0.4),
        "stage": _pick(rng, ["Hettangian", "hettangian", ""], 0.3),
        "defined_by": _pick(rng, PREFIX, 0.3),
        "note": _pick(rng, ["n1", "n2", ""], 0.3),
    }


def _correlation(rng):
    return {
        "from_zonation": rng.choice(["Zone A", "zone a", "Zone B"]),
        "from_zone": rng.choice(["Zone A", "Zone B", "A"]),
        "to_zonation": rng.choice(["Zone B", "Zone C"]),
        "to_zone": rng.choice(["Zone B", "Zone C", "B"]),
        "note": rng.choice(["corr", "corr2", ""]),
    }


# --- per-mode run builders --------------------------------------------------
# Each returns one run dict shaped like a real extraction result for that mode.
def _run_range(rng, n, pool):
    rows = [_pool_or_new(rng, pool, _range_row) for _ in range(n)]
    return {"sections": sorted({r.get("section", "") for r in rows if r.get("section")}),
            "species_ranges": rows,
            "biozones": [_named(rng) for _ in range(rng.randint(0, 2))],
            "other_fossils": [_named(rng) for _ in range(rng.randint(0, 2))],
            "confidence": rng.choice(NUMS) or 0.8}


def _run_columnar(rng, n, pool):
    return {"sections": [_pool_or_new(rng, pool, _columnar_row) for _ in range(n)],
            "fossil_legend": [_named(rng) for _ in range(rng.randint(0, 2))],
            "lithology_legend": [_named(rng) for _ in range(rng.randint(0, 2))],
            "cross_beds": [_named(rng) for _ in range(rng.randint(0, 2))],
            "confidence": rng.choice(NUMS) or 0.8}


def _run_abundance(rng, n, pool):
    return {"abundances": [_pool_or_new(rng, pool, _abundance_row) for _ in range(n)],
            "sites": [_named(rng) for _ in range(rng.randint(0, 2))],
            "zones": [_named(rng) for _ in range(rng.randint(0, 2))],
            "confidence": rng.choice(NUMS) or 0.9}


def _run_phylo(rng, n, pool):
    return {"nodes": [_pool_or_new(rng, pool, _phylo_row) for _ in range(n)],
            "root_ids": [rng.choice(["n1", "n10", "root"]) for _ in range(rng.randint(0, 2))],
            "confidence": rng.choice(NUMS) or 0.7}


def _run_zonation(rng, n, pool):
    return {"zones": [_pool_or_new(rng, pool, _zonation_row) for _ in range(n)],
            "zonations": [_named(rng) for _ in range(rng.randint(0, 2))],
            "correlations": [_correlation(rng) for _ in range(rng.randint(0, 2))],
            "confidence": rng.choice(NUMS) or 0.8}


def _pool_or_new(rng, pool, factory):
    """Mostly reuse a shared row so runs GROUP together, sometimes mutate.
    Grouping is the only way to reach the vote / chimera / ballot code."""
    if pool and rng.random() < 0.7:
        row = dict(rng.choice(pool))
        if rng.random() < 0.5:
            return factory(rng)
        return row
    return factory(rng)


MODES = {
    "range_chart": (_run_range, _range_row),
    "columnar_section": (_run_columnar, _columnar_row),
    "abundance_diagram": (_run_abundance, _abundance_row),
    "phylogenetic_tree": (_run_phylo, _phylo_row),
    "zonation_chart": (_run_zonation, _zonation_row),
}


def gen_cases(mode, n_cases, seed):
    runner, row_factory = MODES[mode]
    rng = random.Random(seed)
    cases = []
    for c in range(n_cases):
        n_runs = rng.choice([1, 2, 2, 3, 3, 4, 5])
        pool = None
        if rng.random() < 0.7:
            pool = [row_factory(rng) for _ in range(rng.randint(1, 4))]
        runs = [runner(rng, rng.randint(0, 6), pool) for _ in range(n_runs)]
        if rng.random() < 0.08:
            runs.append(None)          # a run that failed
        if rng.random() < 0.08:
            runs.append({})            # a malformed run
        total = n_runs + rng.randint(0, 2) if rng.random() < 0.3 else None
        cases.append({"id": c, "mode": mode, "runs": runs, "total_runs": total})
    return cases


def norm(v):
    """Engine-neutral comparison: sorted keys, and int/float unified because
    the two engines number literals differently for the same value."""
    if isinstance(v, dict):
        return {k: norm(v[k]) for k in sorted(v, key=str)}
    if isinstance(v, list):
        return [norm(x) for x in v]
    if v is None or isinstance(v, bool) or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 9)
    return v


def diff_paths(a, b, path="", out=None, limit=14):
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
                out.append("%s.%s: missing in py" % (path, k))
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


def run_mode(mode, n_cases, seed, show_all, verbose):
    cases = gen_cases(mode, n_cases, seed)
    tmpdir = tempfile.mkdtemp(prefix="rcadiff_")
    cases_path = os.path.join(tmpdir, "cases.json")
    driver_path = os.path.join(tmpdir, "driver.js")
    with open(cases_path, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False)
    with open(driver_path, "w", encoding="utf-8") as f:
        f.write(JS_DRIVER)

    env = dict(os.environ, RCA_REPO=REPO)
    proc = subprocess.run(["node", driver_path, cases_path],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env)
    if proc.returncode != 0:
        print("[%s] node driver failed (rc=%d)\n%s"
              % (mode, proc.returncode, proc.stderr[:2000]))
        return 2
    js_out = json.loads(proc.stdout)
    schema = SCHEMA_BY_MODE[mode]

    py_err = js_err = mismatch = 0
    shown = 0
    for c, jr in zip(cases, js_out):
        try:
            pr = merge_results(c["runs"], c["total_runs"], schema)
            py_ok = True
        except Exception as exc:
            py_ok, pr = False, "%s: %s" % (type(exc).__name__, exc)
        if not py_ok:
            py_err += 1
        if not jr.get("ok"):
            js_err += 1
        d = []
        if py_ok and jr.get("ok"):
            d = diff_paths(norm(pr), norm(jr["value"]))
        if (py_ok and jr.get("ok") and not d):
            continue
        mismatch += 1
        if show_all or shown < 3:
            shown += 1
            print("=" * 72)
            print("[%s] case %d" % (mode, c["id"]))
            if not py_ok or not jr.get("ok"):
                print("    one engine raised. py_ok=%s js_ok=%s" % (py_ok, jr.get("ok")))
                print("    py:", "" if py_ok else pr)
                print("    js:", "" if jr.get("ok") else jr.get("error"))
            for line in d[:8]:
                print("   ", line)
            print("    total_runs:", c["total_runs"])
            print("    runs:", json.dumps(c["runs"], ensure_ascii=False)[:420])
    if verbose:
        print("[%s] cases=%d seed=%d py_raised=%d js_raised=%d MISMATCHED=%d"
              % (mode, n_cases, seed, py_err, js_err, mismatch))
    return 1 if mismatch else 0


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    show_all = "--list" in argv
    verbose = "--quiet" not in argv
    n_cases = int(args[0]) if args else 400
    seed = int(args[1]) if len(args) > 1 else 20260927
    want = args[2] if len(args) > 2 else "all"
    modes = sorted(MODES) if want == "all" else [want]
    bad = [m for m in modes if m not in MODES]
    if bad:
        print("unknown mode(s): %s\nknown: %s" % (", ".join(bad), ", ".join(sorted(MODES))))
        return 2

    rc = 0
    for mode in modes:
        rc |= run_mode(mode, n_cases, seed, show_all, verbose)
    print()
    print("modes run            : %s" % ", ".join(modes))
    print("overall              : %s" % ("DIVERGENT" if rc else "engines agree"))
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
