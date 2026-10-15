"""Run the app with CANDIDATES unimportable, to catch LAZY imports.

The import probe (scripts/audit_exclude_imports.py) only proves the packages are not needed at
import time. It cannot see a `import scipy` buried in a function body that runs
later -- and matplotlib does exactly that kind of thing for its interpolate /
interp2d paths. A wrong exclusion therefore still shows up only at runtime.

This installs the blocker as `sitecustomize`, so it applies to pytest and to
every worker process it spawns, and then the whole suite runs as it normally
would. Anything that genuinely needs an excluded package fails loudly here,
in three minutes, instead of in front of a user.

Not part of the shipped test suite: it is a build-time audit of the PyInstaller
exclude list, which is why it lives here and not in tests/.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Exactly the list validated by scripts/audit_exclude_imports.py. Kept as data so this file
# and the spec file can be diffed against each other.
CANDIDATES = [
    "torch", "torchvision", "torchaudio",
    "transformers", "tokenizers", "safetensors", "hf_xet", "huggingface_hub",
    "spacy", "thinc", "blis", "preshed", "murmurhash", "cymem", "srsly",
    "onnxruntime", "polars", "_polars_runtime_32", "pyarrow",
    "bitsandbytes", "scipy", "sklearn", "pandas",
    "grpc", "google", "pdfminer", "pdfplumber",
]

SITECUSTOMIZE = '''\
import json, os, sys
_list = json.loads(os.environ.get("RCA_BLOCKED", "[]"))
if _list:
    _set = set(_list)

    class _B:
        def find_module(self, fullname, path=None):
            return self.find_spec(fullname, path)

        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in _set:
                raise ImportError("BLOCKED BY EXCLUSION AUDIT: %s" % fullname)
            return None

    sys.meta_path.insert(0, _B())
'''


def main() -> int:
    hook_dir = os.path.join(ROOT, "_audit_hook")
    os.makedirs(hook_dir, exist_ok=True)
    with open(os.path.join(hook_dir, "sitecustomize.py"), "w",
              encoding="utf-8") as fh:
        fh.write(SITECUSTOMIZE)

    env = dict(os.environ)
    env["RCA_BLOCKED"] = json.dumps(CANDIDATES)
    env["PYTHONPATH"] = hook_dir + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"

    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header",
         "--tb=line", "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env)
    out = (r.stdout or "") + (r.stderr or "")
    with open(os.path.join(ROOT, "_audit_excl.txt"), "w", encoding="utf-8") as fh:
        fh.write(out)

    failed = [l for l in out.splitlines() if l.startswith(("FAILED", "ERROR"))]
    print("returncode : %d" % r.returncode)
    print("FAILED/ERROR lines: %d" % len(failed))
    for l in failed[:25]:
        print("   ", l[:150])
    hits = [l for l in out.splitlines() if "BLOCKED BY EXCLUSION AUDIT" in l]
    print("lines mentioning the blocker: %d" % len(hits))
    for l in hits[:8]:
        print("   ", l.strip()[:150])
    return 0 if r.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
