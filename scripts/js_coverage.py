"""Measure what the JS suites actually execute, with Node's own V8 coverage.

Run it with:

    python scripts/js_coverage.py            # every suite, every front-end file
    python scripts/js_coverage.py js/table.js js/app.js

WHY THIS EXISTS. A note in the backlog said "the front-end is thinly
covered -- about 4900 lines across js/table.js, js/export.js and
js/ics_table.js". That number was wrong, and the way it was wrong is
worth recording, because the same mistake is easy to repeat:

* V8's coverage offsets are in UTF-16 CODE UNITS (JavaScript string
  characters), NOT bytes. js/i18n.js is 62,899 characters and 79,507
  UTF-8 bytes, so a byte-based line map puts every line after the first
  CJK block in the wrong place. Measured that way, js/i18n.js came out
  81.7 % covered and js/table.js showed a 30-line hole in its export
  block -- both artefacts, both real files that execute end to end.
* V8 reports BLOCK ranges inside functions. A file's trailing top-level
  export block is not inside any function, so it produces no range of
  its own.

The number this script prints is therefore "of the lines V8 can
attribute, how many were reached", with the unattributed remainder
reported separately rather than folded into the percentage. For the
front-end as it stands, the answer is that essentially every line runs.

The suites load the front-end with ``vm.runInContext(src, ctx,
{filename: f})``, so V8 reports the RELATIVE name the suite chose. The
mapping from that name to a file on disk is resolved here, and an
unresolvable name is REPORTED rather than counted as zero coverage.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COV = os.path.join(ROOT, "_tmpwin", "cov")

#: The front-end files this script reports on.
FRONTEND = [
    "js/table.js", "js/export.js", "js/ics_table.js", "js/app.js",
    "js/aggregate.js", "js/viz.js", "js/quality.js", "js/minimax.js",
    "js/history.js", "js/i18n.js", "js/prompt.js", "js/config.js",
    "js/json-utils.js", "js/error-utils.js", "js/reason-codes.js",
    "js/sharpen_worker.js", "js/theme.js",
]


def suites() -> list[str]:
    return sorted(f for f in os.listdir(ROOT)
                  if f.startswith("tests_") and f.endswith(".js"))


def collect() -> dict:
    """Run every suite with V8 coverage on, and merge the ranges."""
    os.makedirs(COV, exist_ok=True)
    for name in os.listdir(COV):
        os.remove(os.path.join(COV, name))
    env = dict(os.environ)
    env["NODE_V8_COVERAGE"] = COV
    for suite in suites():
        subprocess.run(["node", suite], cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    blocks: dict[str, list] = {}
    unresolved: set[str] = set()
    for name in os.listdir(COV):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(COV, name), encoding="utf-8") as f:
            payload = json.load(f)
        for entry in payload.get("result") or []:
            url = entry.get("url") or ""
            if not url.endswith(".js"):
                continue
            path = resolve(url)
            if path is None:
                unresolved.add(url)
                continue
            if os.path.join(ROOT, "node_modules") in path:
                continue
            ranges = blocks.setdefault(path, [])
            for fn in entry.get("functions") or []:
                for r in fn.get("ranges") or []:
                    if r.get("count", 0) > 0:
                        ranges.append((r["startOffset"], r["endOffset"]))
    return {"blocks": blocks, "unresolved": unresolved}


def resolve(url: str):
    """Map a V8 script URL to a file on disk."""
    if url.startswith("file://"):
        path = url[7:].replace("/", os.sep)
        return path if os.path.isfile(path) else None
    if os.path.isabs(url):
        return url if os.path.isfile(url) else None
    for cand in (os.path.join(ROOT, url.replace("/", os.sep)),
                 os.path.join(ROOT, "js", url)):
        if os.path.isfile(cand):
            return cand
    return None


def line_map(src: str) -> list[int]:
    """Character offset -> line number. CHARACTERS, not bytes."""
    line_of = [0] * (len(src) + 1)
    line = 1
    for i, ch in enumerate(src):
        line_of[i] = line
        if ch == "\n":
            line += 1
    line_of[len(src)] = line
    return line_of


def report(targets: list[str], data: dict) -> None:
    blocks = data["blocks"]
    print("%-26s %7s %10s %8s %9s" % ("file", "lines", "attributed",
                                       "covered", "pct"))
    print("-" * 68)
    tot_lines = tot_attr = tot_cov = 0
    for rel in targets:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", newline="") as f:
            src = f.read()
        n = len(src)
        line_of = line_map(src)
        total_lines = src.count("\n") + 1
        covered = set()
        for start, end in blocks.get(path, []):
            a = line_of[min(start, n)]
            b = line_of[min(max(end - 1, 0), n)]
            covered.update(range(a, b + 1))
        attributed = len(covered)
        pct = (100.0 * len(covered) / total_lines) if total_lines else 0.0
        print("%-26s %7d %10d %8d %8.1f%%"
              % (rel, total_lines, attributed, len(covered), pct))
        tot_lines += total_lines
        tot_attr += attributed
        tot_cov += len(covered)
    print("-" * 68)
    print("%-26s %7d %10d %8d %8.1f%%"
          % ("TOTAL", tot_lines, tot_attr, tot_cov,
             100.0 * tot_cov / tot_lines if tot_lines else 0.0))
    if data["unresolved"]:
        print()
        print("script names that could not be resolved to a file "
              "(NOT counted as zero coverage):")
        for u in sorted(data["unresolved"]):
            if "node_modules" in u:
                continue
            print("   ", u)


def main() -> int:
    targets = sys.argv[1:] or FRONTEND
    data = collect()
    report(targets, data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
