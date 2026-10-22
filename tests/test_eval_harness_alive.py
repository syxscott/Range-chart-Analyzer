"""AUDIT-2026-10-22: the accuracy-evaluation harness must stay alive.

``tests/eval/run_eval.py`` scores saved extractions against the
hand-transcribed ground truth in ``tests/eval/ground_truth.py``. It is the
only accuracy measurement this project has, and it is not in CI because its
artefacts (``_figtest/<tag>_result.json``) are gitignored -- the figures are
from copyrighted publications and committing the extractions is a data
decision that has not been taken.

That gap is recorded; what must NOT happen is the harness rotting to the
point where the decision cannot be executed: a scorer that crashes on missing
artefacts, a ground-truth module that fails to import, a summary that stops
being parseable. This module pins exactly that -- run the harness offline
with NO artefacts and require the three properties a maintainer needs:

  * exit 0 (missing truth is "not transcribed", not a failure -- the
    ground-truth docstring's own rule: "absence of truth is not a pass");
  * every untranscribed figure is named as NOT TRANSCRIBED, so silence
    cannot masquerade as a clean run;
  * the known-error canaries still trip, because that is the check that
    the scorer catches a transcription mistake rather than forgiving it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "tests" / "eval" / "run_eval.py"


def test_eval_harness_runs_and_stays_honest(tmp_path):
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, "-I", str(EVAL)],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, env=env,
    )
    out = r.stdout or ""
    # 1. the harness itself did not crash
    assert r.returncode == 0, (
        "tests/eval/run_eval.py exits %d without artefacts -- the harness "
        "rotted, which blocks the maintainer's ability to ever measure "
        "accuracy again. stderr: %s" % (r.returncode, (r.stderr or "")[:400]))
    # 2. untranscribed figures are NAMED, not silently passed
    assert "NOT TRANSCRIBED" in out, (
        "the harness no longer distinguishes 'not transcribed' from 'clean' "
        "-- that distinction is the whole point of the ground-truth file")
    # 3. the scorer still catches the planted transcription errors
    assert "all known transcription errors are caught" in out, (
        "the known-error canaries stopped tripping; the scorer's "
        "catch-a-mistake property is gone. output: %s" % out[:400])
    # 4. a per-figure recall table is printed (the scoreable subset)
    assert "RECALL" in out
