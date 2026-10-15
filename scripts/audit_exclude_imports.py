"""Would excluding these packages break the app AT IMPORT TIME?

The spec file already warns that a wrong exclusion "surfaces at runtime, not at
build time". A full onefile build here takes ~10 minutes, so discovering a
broken exclusion after the fact is expensive. This runs the same import the exe
performs, with the candidates blocked by a meta-path finder -- seconds, not
minutes.

Each trial runs in a FRESH SUBPROCESS, and that is not tidiness. The first
version reused one interpreter: after the baseline imported everything, every
later `__import__` hit `sys.modules` and returned without ever consulting the
meta-path finder. It reported "all 27 candidates blocked: OK in 0.0s" -- a
0.0s verdict from code that never ran. A shared interpreter cannot test an
exclusion.

What this does NOT prove: that no lazy import (inside a function body) touches a
blocked name later. To cover that the probe also calls a few entry points
rather than merely importing them.

Read-only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANDIDATES = [
    "torch", "torchvision", "torchaudio",
    "transformers", "tokenizers", "safetensors", "hf_xet", "huggingface_hub",
    "spacy", "thinc", "blis", "preshed", "murmurhash", "cymem", "srsly",
    "onnxruntime", "polars", "_polars_runtime_32", "pyarrow",
    "bitsandbytes", "scipy", "sklearn", "pandas",
    "grpc", "google", "pdfminer", "pdfplumber",
]

MODULES = (
    "gui", "server", "app", "gui_fluent",
    "rca_core", "rca_core.extractor", "rca_core.exporter",
    "rca_core.aggregate", "rca_core.quality", "rca_core.prompt",
    "rca_core.chart_mode", "rca_core.modes", "rca_core.llm",
    "rca_core.capabilities", "rca_core.history",
    "rca_core.standards.darwin_core", "rca_core.standards.pbdb",
    "rca_core.standards.ics",
    "qfluentwidgets",
)

# Runs in the child. Prints one JSON line so the parent never has to parse prose.
CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
blocked = set(json.loads(sys.argv[2]))

class Blocker:
    def find_module(self, fullname, path=None):
        return self.find_spec(fullname, path)
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ImportError("BLOCKED BY PROBE: %s" % fullname)
        return None

sys.meta_path.insert(0, Blocker())
try:
    for m in json.loads(sys.argv[3]):
        __import__(m)
    from rca_core import exporter, aggregate, chart_mode
    exporter.get_configs_for_result({"columns": [], "legend": [],
                                     "occurrences": [], "confidence": 0.5})
    aggregate.merge_results([])
    chart_mode.auto_detect_chart_mode_ex("range chart")
    print("PROBE_OK")
except Exception as exc:
    print("PROBE_FAIL %s: %s" % (type(exc).__name__, exc))
'''


def run(blocked):
    r = subprocess.run(
        [sys.executable, "-c", CHILD, ROOT, json.dumps(sorted(blocked)),
         json.dumps(list(MODULES))],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    # Match on LINES, not on the whole blob: importing qfluentwidgets prints an
    # emoji banner ("QFluentWidgets Pro is now released...") before our marker,
    # so a startswith on the joined stdout reads as a failure that never happened.
    lines = [l.strip() for l in (r.stdout or "").splitlines()]
    if "PROBE_OK" in lines:
        return True, ""
    fail = next((l for l in lines if l.startswith("PROBE_FAIL")), "")
    return False, (fail.replace("PROBE_FAIL ", "")
                   or (r.stderr or "").strip()[-200:] or "rc=%d, no marker"
                   % r.returncode)


def main() -> int:
    ok, err = run([])
    print("baseline (nothing blocked)      : %s" % ("OK" if ok else "FAIL " + err))
    if not ok:
        print("  baseline itself fails; the probe measures nothing.")
        return 1

    ok, err = run(CANDIDATES)
    print("all %d candidates blocked        : %s"
          % (len(CANDIDATES), "OK" if ok else "FAIL " + err))
    if ok:
        print("  -> every candidate is droppable. Still verify the built exe.")
        return 0

    needed = []
    for c in CANDIDATES:
        good, e = run([c])
        if not good:
            needed.append(c)
            print("    NEEDED  %-22s %s" % (c, e[:96]))

    safe = [c for c in CANDIDATES if c not in needed]
    ok, err = run(safe)
    print()
    print("safe set (%d) verified           : %s"
          % (len(safe), "OK" if ok else "FAIL " + err))
    print()
    print("EXCLUDE = %s" % json.dumps(safe))
    if needed:
        print("KEEP    = %s" % json.dumps(needed))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
