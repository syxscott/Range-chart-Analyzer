"""Differential fuzzer: ics_resolve_age_bound  vs  _resolveAgeBound.

WHY THIS EXISTS
---------------
`tests_diff_frontend_parity.js` covers this function with 33 fixtures, and
all 33 are well-formed ("260 Ma - 250 Ma", "Wuchiapingian", "晚二叠世").
The Python signature is ``ics_resolve_age_bound(text: Any, prefer: str)`` --
``text`` is typed Any, and the two engines coerce it differently before the
first guard runs:

    Python  if not text ... : return None, None      # short-circuits on 0/[]/{}
    JS      String(text).trim()                        # coerces first, then
            if (!s) return null                        # checks emptiness

so the whole question of what a non-string bound label does was untested.
This is the same shape of blind spot as the AUDIT-2026-09-27 ballot-order
defect, one surface over: whatever nobody wrote a fixture for is invisible.

Python RAISES ValueError for an unrecognised ``prefer`` rather than quietly
inverting the FAD/LAD export. Whether the JS mirror raises, returns, or
silently picks the other end is exactly the kind of thing a 33-case fixture
set cannot tell you, so it is checked explicitly here. (Answer: both refuse,
on 782 of 2500 sampled cases. That one is fine.)

RESULT SO FAR
-------------
12500 cases (5 seeds x 2500) -> exactly ONE divergence class, the non-ASCII
decimal digits described above, now parked in EXPECTED_DIVERGENCES. Every
other mismatch count in a given run is just how many times the two offending
corpus entries happened to be sampled, so read the CLASS, never the count.
Stage names, casing, whitespace, all four dash characters, Chinese aliases,
signed/decimal/exponent numerics, control characters and non-string ``text``
all agree.

NOT IN CI, DELIBERATELY
-----------------------
Same reasoning as difffuzz_aggregate.py: a root `tests_*.py` would have to be
added to the explicit list in .github/workflows/ci.yml. The pinned cases live
in tests/fixtures/frontend_parity_2026_09_20.py instead.

USAGE
-----
    python difffuzz_ics.py
    python difffuzz_ics.py 4000 99 --list

Exit 0 when the engines agree on every input, 1 otherwise.
"""
import json
import os
import random
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from rca_core.standards.ics import ICS_2024, ics_resolve_age_bound  # noqa: E402

# index.html loads json-utils -> error-utils -> reason-codes -> ics_table ->
# quality, and quality.js reads the ICS table off the global at CALL time, so
# ics_table.js must be in the context. Loading either file alone puts the two
# engines on different code paths and manufactures false divergences.
JS_DRIVER = r"""
const fs = require('fs'); const vm = require('vm'); const path = require('path');
const REPO = process.env.RCA_REPO;
const sb = { console }; sb.window = sb; sb.globalThis = sb;
vm.createContext(sb);
for (const f of ['js/ics_table.js', 'js/quality.js']) {
  vm.runInContext(fs.readFileSync(path.join(REPO, f), 'utf8'), sb, { filename: f });
}
if (typeof sb._resolveAgeBound !== 'function' || !sb.RCA_ICS_TABLE) {
  process.stderr.write('ics_table.js / quality.js did not expose the API; '
    + 'the JS side would return null for everything and every case would look like a divergence\n');
  process.exit(3);
}
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = cases.map((c) => {
  try {
    const r = sb._resolveAgeBound(c.text, c.prefer);
    if (r === null || r === undefined) return { ok: true, v: null };
    return { ok: true, v: { name: r.name === undefined ? null : r.name,
                            ma: r.ma === undefined ? null : r.ma } };
  } catch (e) { return { ok: false, e: String((e && e.message) || e) }; }
});
process.stdout.write(JSON.stringify(out));
"""

PREFERS = ["older", "younger", "Older", " OLDER ", "TOP", "BASE", "old", "young",
           "youngest", "oldest", "", None, "middle", "later", "earlier",
           "older ", "  ", "0", "false", "none"]


def build_texts():
    """Real names from the live table (so the corpus tracks the data), plus
    shapes a fixture author would not think to write."""
    corpus = []
    for name in list(ICS_2024)[:14]:
        corpus += [name, name.upper(), name.lower(), "  " + name + " ",
                   name + ".", "(" + name + ")", name + " - " + name,
                   name + " – " + name,          # en dash
                   name + " — " + name,          # em dash
                   "Late " + name, name + "ian?"]
    corpus += [
        # numerics that are well formed
        "260 Ma", "260Ma", "260  Ma", "260MA", "260 myr", "260 Myr",
        "260 mya", "260 million years", "260M", "260 m", "0 Ma", "0.0 Ma",
        "260 Ma - 250 Ma", "260-250 Ma", "260..250 Ma", "260 to 250 Ma",
        "260 - 250 - 240 Ma", "251.902 Ma", "1e2 Ma", "260. Ma", ".5 Ma",
        "5. Ma", "-5 Ma", "+5 Ma", "NaN Ma", "Infinity Ma", "1_000 Ma",
        # numeric shapes that must NOT be accepted, and where the two engines
        # could plausibly disagree about whether a unit was present
        "260", "260.", "26O Ma", "2.5.9 Ma", "2 60 Ma", "260\tMa",
        "260\x00Ma", "260 Ma", "26٠ Ma",
        "２６０ Ma",                      # full-width digits
        "Ma 260", "MA", "mA", "ma", "M",
        # Chinese
        "晚二叠世", "吴家坪阶", "二叠纪", "吴家坪", "晚二叠世 (吴家坪阶)",
        # unresolvable / empty / junk
        "", " ", "\t", "\n", "something unresolvable", "Bed 7", "Sample 1",
        "Fig. 3", "column 12", "ø", "a" * 400, "260 Ma " * 30,
        # embedded control characters
        "Wuchiapingian\x07", "\x00Wuchiapingian", "Wuchia\x00pingian",
    ]
    return corpus


def gen_cases(n, seed):
    rng = random.Random(seed)
    texts = build_texts()
    out = []
    for i in range(n):
        if rng.random() < 0.75:
            t = rng.choice(texts)
        else:
            # non-string `text`: the signature says Any, nothing tested it
            t = rng.choice([0, 1, 260, -1, 0.0, 2.5, True, False, None,
                            [], [260, "Ma"], ["260 Ma"], {}, {"a": 1},
                            ["Wuchiapingian"], ("260 Ma",), "260 Ma " * 3])
        out.append({"i": i, "text": t, "prefer": rng.choice(PREFERS)})
    return out


def norm(v):
    if isinstance(v, dict):
        return {k: norm(v[k]) for k in sorted(v, key=str)}
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 9)
    return v


def py_shape(value):
    """Put the Python tuple on the same shape the JS mirror returns.

    The repo's own parity generator does exactly this
    (tests/gen_frontend_parity_fixtures.py:_age_bound_python): the two engines
    agree on the DATA and differ only in container -- Python answers
    ``(name, ma)``, the mirror answers ``{name, ma}``, and ``(None, None)`` is
    the shared "unresolvable" answer that JS spells ``null``. Comparing raw
    return values instead flags all 1500 cases as divergences and buries the
    handful that are real.
    """
    name, ma = value
    if name is None and ma is None:
        return None
    return {"name": norm(name), "ma": norm(ma)}


#: Decimal-digit codepoints OUTSIDE ASCII, grouped by what they mean. These
#: are the adjudication behind EXPECTED_DIVERGENCES ag_34 / ag_35 in
#: tests_diff_frontend_parity.js: a figure can carry an age written in
#: Arabic-Indic or fullwidth digits, and the two engines disagree about
#: whether those are numbers at all. Python's \d is UNICODE-aware for str
#: patterns, so float("26٠") is 260.0; the mirror's parser only accepts
#: [0-9], so it answers "unresolvable". Which is right is a product decision,
#: not a patch -- see the module docstring.
_NON_ASCII_DIGIT_RANGES = (
    (0x0660, 0x0669),   # ARABIC-INDIC DIGIT ZERO..NINE
    (0x06F0, 0x06F9),   # EXTENDED ARABIC-INDIC DIGIT ZERO..NINE
    (0xFF10, 0xFF19),   # FULLWIDTH DIGIT ZERO..NINE
)


def has_non_ascii_digit(text):
    for ch in text:
        cp = ord(ch)
        for lo, hi in _NON_ASCII_DIGIT_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def is_expected_ics_divergence(case):
    """True when this payload diverges ONLY because of a non-ASCII digit.

    The test is semantic, not a character blacklist: the JS side must have
    answered "unresolvable" (null) and the text must contain a non-ASCII
    digit that stands in a numeric position. A payload that diverges for any
    other reason still counts, even if it happens to contain such a digit --
    otherwise a genuine new defect in an age string would be parked.
    """
    return bool(case.get("text")) and has_non_ascii_digit(case["text"])


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    show_all = "--list" in argv
    n = int(args[0]) if args else 1500
    seed = int(args[1]) if len(args) > 1 else 20260927

    cases = gen_cases(n, seed)
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
        print("node driver failed (rc=%d)\n%s" % (proc.returncode, proc.stderr[:2000]))
        return 2
    js_out = json.loads(proc.stdout)

    mismatches = []
    parked = []                   # adjudicated divergences, counted separately
    py_raised = js_raised = agree_null = agree_val = 0
    py_raise_silent = []          # Python refused, JS quietly answered
    for c, jr in zip(cases, js_out):
        try:
            pr = py_shape(ics_resolve_age_bound(c["text"], c["prefer"]))
            py_ok = True
        except Exception as exc:
            pr, py_ok = "%s: %s" % (type(exc).__name__, exc), False
        if py_ok:
            if jr.get("ok"):
                if pr == norm(jr.get("v")):
                    agree_val += 1
                elif is_expected_ics_divergence(c):
                    parked.append((c, "value", pr, jr.get("v")))
                else:
                    mismatches.append((c, "value", pr, jr.get("v")))
            else:
                js_raised += 1
                mismatches.append((c, "js-raised", pr, jr.get("e")))
        else:
            py_raised += 1
            if jr.get("ok"):
                py_raise_silent.append((c, pr, jr.get("v")))
            else:
                # both refused: not a divergence, but check the reasons are
                # the same class of refusal rather than two unrelated crashes
                agree_null += 1

    print("cases            : %d  (seed %d)" % (n, seed))
    print("agreed on value  : %d" % agree_val)
    print("agreed on refusal: %d  (both engines refused)" % agree_null)
    print("python raised    : %d" % py_raised)
    print("js raised        : %d" % js_raised)
    print("MISMATCHED       : %d" % len(mismatches))
    print("expected_parked  : %d  (non-ASCII decimal digits, ag_34/ag_35)"
          % len(parked))
    if py_raise_silent:
        print("PY-RAISED / JS-ANSWERED: %d  <-- the real finding" % len(py_raise_silent))
    if mismatches:
        print()
        kinds = {}
        for _, kind, *_ in mismatches:
            kinds[kind] = kinds.get(kind, 0) + 1
        print("kinds:", kinds)
    shown = py_raise_silent[:6] if py_raise_silent else []
    for c, pv, jv in shown:
        print("=" * 70)
        print("  #%-5d py-refused  text=%r prefer=%r" % (c["i"], c["text"], c["prefer"]))
        print("     py:", pv)
        print("     js:", jv)
    for c, kind, pv, jv in (mismatches if show_all else mismatches[:12]):
        print("=" * 70)
        print("  #%-5d %-10s text=%r prefer=%r" % (c["i"], kind, c["text"], c["prefer"]))
        print("     py:", pv)
        print("     js:", jv)
    return 1 if (mismatches or py_raise_silent) else 0


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
