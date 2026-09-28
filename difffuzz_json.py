"""Differential fuzzer: rca_core/json_utils.safe_json_loads  vs  js mirror.

WHY THIS EXISTS
---------------
`safe_json_loads` is the LAST gate before anything a model said becomes
scientific data, and it is the most failure-prone function in the app: a
six-level fallback chain (fences -> control-char strip -> strict parse ->
balanced-object scoring -> bracket extraction -> prose-embedded rescue). The
committed gate covers it with 20 fixtures, all hand-written.

The generator here leans on the shape that matters for a STRING PARSER:
take valid payloads and TRUNCATE them at every offset. That walks the whole
fallback chain systematically for the cost of one seed, and it is exactly the
case a model produces when it hits max_tokens — the single most common real
failure mode in this app.

CONTROL CHARACTERS ARE A DELIBERATE PART OF THE POOL. Level 2 of both chains
deletes the non-whitespace control range, and a hand-written fixture set will
never place one in the middle of a caption. Escaping vs deleting them is the
difference between a dropped byte and a character that later crashes
openpyxl with IllegalCharacterError.

NOT IN CI, DELIBERATELY
-----------------------
A root `tests_*.py` would have to be added to the explicit list in
.github/workflows/ci.yml. Pinned cases live in
tests/fixtures/frontend_parity_2026_09_20.json instead.

USAGE
-----
    python difffuzz_json.py
    python difffuzz_json.py 3000 99 --list
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

from rca_core.json_utils import safe_json_loads  # noqa: E402

# json-utils.js reads nothing from other scripts, but config.js defines things
# it may consult; the parity harness loads both, so mirror that order.
JS_DRIVER = r"""
const fs = require('fs'); const vm = require('vm'); const path = require('path');
const REPO = process.env.RCA_REPO;
const sb = { console }; sb.window = sb; sb.globalThis = sb;
vm.createContext(sb);
for (const f of ['js/config.js', 'js/json-utils.js']) {
  vm.runInContext(fs.readFileSync(path.join(REPO, f), 'utf8'), sb, { filename: f });
}
if (typeof sb.safeJsonLoads !== 'function') {
  process.stderr.write('safeJsonLoads missing from js/json-utils.js\n');
  process.exit(3);
}
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
// AUDIT-2026-09-27: JSON.stringify turns Infinity/NaN into null, which made
// 1e400 look like "JS produced null" when the engine had in fact produced
// Infinity -- the same value Python produced. Both engines DO agree on the
// overflow; the transport was eating it. Tag the non-finite numbers so the
// comparison sees what the engine actually returned.
const seen = (v) => {
  if (Array.isArray(v)) return v.map(seen);
  if (v && typeof v === 'object') {
    const o = {}; for (const k of Object.keys(v)) o[k] = seen(v[k]); return o;
  }
  if (typeof v === 'number' && !isFinite(v)) return v > 0 ? '__INF__' : (v < 0 ? '__-INF__' : '__NAN__');
  return v;
};
process.stdout.write(JSON.stringify(cases.map((t) => {
  try { return { ok: true, v: seen(sb.safeJsonLoads(t)) }; }
  catch (e) { return { ok: false, e: String((e && e.message) || e) }; }
})));
"""

PAYLOADS = [
    '{"sections":[{"name":"A"}],"confidence":0.9}',
    '{"species_ranges":[{"species":"A","section":"S","range_base":"7","range_top":"9"}]}',
    '[{"species":"A","section":"S"}]',
    '{"data":{"sections":[{"name":"A"}]},"confidence":0.2}',
    '{"zonations":[{"name":"N"}],"correlations":[]}',
    '{"sections":[{"name":"\\u6c49"}],"note":"caf\\u00e9"}',
    '{"a":{"b":{"c":{"d":[1,2,3]}}}}',
    '{"x":1e400,"y":-0,"z":0.1,"w":12345678901234567890}',
    '{"t":"tab\\there","n":"nl\\nhere","q":"q\\"uote"}',
    '{"s":"' + "long" * 200 + '"}',
]
SCHEMA_BLOCK = ('```json\n{"properties": {"sections": {"type": "array"}},'
                ' "required": ["sections"], "format": "rca-v1"}\n```')
CTRL = ["\x00", "\x01", "\x07", "\x08", "\x0b", "\x0c", "\x1f", "\x7f",
        "\t", "\n", "\r"]


def wrap(kind, body, rng):
    if kind == "plain":
        return body
    if kind == "fence":
        return "```json\n" + body + "\n```"
    if kind == "fence_nolang":
        return "```\n" + body + "\n```"
    if kind == "fence_unclosed":
        return "```json\n" + body
    if kind == "prose":
        return "Sure! Here is the result:\n" + body + "\nHope that helps."
    if kind == "schema_then_payload":
        return SCHEMA_BLOCK + "\nResult:\n```json\n" + body + "\n```"
    if kind == "payload_then_schema":
        return "Result:\n```json\n" + body + "\n```\n" + SCHEMA_BLOCK
    if kind == "two_fences":
        return SCHEMA_BLOCK + "\n```json\n" + body + "\n```"
    if kind == "bom":
        return "\ufeff" + body
    if kind == "trailing_comma":
        return body[:-1] + ",}" if body.endswith("}") else body
    if kind == "nul_tail":
        return body + "".join(rng.choice(CTRL) for _ in range(rng.randint(1, 3)))
    if kind == "ctrl_inside":
        c = rng.choice([x for x in CTRL if x not in ("\t", "\n", "\r")])
        i = rng.randint(1, max(1, len(body) - 1))
        return body[:i] + c + body[i:]
    if kind == "spacey":
        return body.replace(",", " , ").replace(":", " : ")
    return body


KINDS = ["plain", "fence", "fence_nolang", "fence_unclosed", "prose",
         "schema_then_payload", "payload_then_schema", "two_fences", "bom",
         "trailing_comma", "nul_tail", "ctrl_inside", "spacey"]


def gen_cases(n, seed):
    rng = random.Random(seed)
    out = []
    i = 0
    while len(out) < n:
        body = rng.choice(PAYLOADS)
        kind = rng.choice(KINDS)
        s = wrap(kind, body, rng)
        out.append({"i": i, "text": s})
        i += 1
        # Systematic truncation: every prefix of this one exercises the whole
        # fallback chain, which is what a max_tokens cutoff actually looks like.
        if rng.random() < 0.22 and len(out) + len(s) <= n:
            for k in range(len(s)):
                if len(out) >= n:
                    break
                out.append({"i": i, "text": s[:k]})
                i += 1
    return out[:n]


def norm(v):
    if isinstance(v, dict):
        return {k: norm(v[k]) for k in sorted(v, key=str)}
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            # Match the driver's sentinel so both sides land on one token.
            return "__NAN__" if f != f else ("__INF__" if f > 0 else "__-INF__")
        return round(f, 9)
    raise TypeError("difffuzz: unhandled type %r -- report it, do not coerce"
                    % (type(v).__name__, v))


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    show_all = "--list" in argv
    n = int(args[0]) if args else 2000
    seed = int(args[1]) if len(args) > 1 else 20260927

    cases = gen_cases(n, seed)
    with tempfile.TemporaryDirectory() as d:
        cp = os.path.join(d, "c.json")
        dp = os.path.join(d, "d.js")
        with open(cp, "w", encoding="utf-8") as f:
            json.dump([c["text"] for c in cases], f, ensure_ascii=False)
        with open(dp, "w", encoding="utf-8") as f:
            f.write(JS_DRIVER)
        proc = subprocess.run(["node", dp, cp], capture_output=True, text=True,
                              encoding="utf-8", env=dict(os.environ, RCA_REPO=REPO))
    if proc.returncode != 0:
        print("node driver failed (rc=%d)\n%s" % (proc.returncode, proc.stderr[:1500]))
        return 2
    js_out = json.loads(proc.stdout)

    agree = agree_refuse = mismatch = 0
    buckets = {"wording": 0, "accepted-vs-refused": 0, "value": 0,
               "refusal-class": 0}
    examples = []

    def refusal_key(msg):
        """The refusal's REASON, not its rendering.

        Python says "no JSON object found in '`'" and JS says "no JSON object
        found in: `", which is the same refusal written two ways. Comparing
        the whole string turned 815 cases into "mismatches" and buried the
        handful where one engine ACCEPTED input the other refused -- the only
        kind that is a real contract difference here.
        """
        head = str(msg).strip()
        for sep in (" in '", " in:", " in ", ":"):
            if sep in head:
                head = head.split(sep, 1)[0]
        return head.strip().lower()
    for c, jr in zip(cases, js_out):
        try:
            pr = norm(safe_json_loads(c["text"]))
            py_ok = True
        except Exception as exc:
            pr, py_ok = "%s: %s" % (type(exc).__name__, exc), False
        if py_ok and jr.get("ok"):
            try:
                jn = norm(jr["v"])
            except TypeError as exc:
                mismatch += 1
                if show_all or len(examples) < 3:
                    examples.append((c, [str(exc)], pr, jr["v"]))
                continue
            if pr == jn:
                agree += 1
            else:
                mismatch += 1
                buckets["value"] += 1
                if show_all or len(examples) < 3:
                    examples.append((c, ["value differs"], pr, jr["v"]))
        elif (not py_ok) and (not jr.get("ok")):
            tail = pr.split(": ", 1)[-1].strip()
            if refusal_key(tail) == refusal_key(jr.get("e")):
                agree_refuse += 1
            else:
                mismatch += 1
                buckets["refusal-class"] += 1
                if show_all or len(examples) < 3:
                    examples.append((c, ["refusal CLASS differs"], pr, jr.get("e")))
        else:
            mismatch += 1
            buckets["accepted-vs-refused"] += 1
            if show_all or len(examples) < 3:
                examples.append((c, ["one engine accepted, the other refused "
                                    "(py_ok=%s js_ok=%s)" % (py_ok, jr.get("ok"))],
                                 pr, jr.get("v") if jr.get("ok") else jr.get("e")))

    print("cases           : %d  (seed %d)" % (n, seed))
    print("agreed on value : %d" % agree)
    print("agreed on refusal: %d" % agree_refuse)
    print("MISMATCHED      : %d" % mismatch)
    print("  by class      : %s" % buckets)
    for c, d_, pv, jv in examples:
        print("=" * 70)
        print("  #%d text=%r" % (c["i"], c["text"][:160]))
        for line in d_[:4]:
            print("   ", line)
        print("     py:", json.dumps(pv, ensure_ascii=False, default=str)[:220])
        print("     js:", json.dumps(jv, ensure_ascii=False, default=str)[:220])
    return 1 if mismatch else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
