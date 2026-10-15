"""Score the ten round-2 results against the transcribed ground truth.

Usage:
    python tests/eval/run_eval.py                 # offline: score saved results
    python tests/eval/run_eval.py --with-ocr      # also feed the OCR pass in

Offline by default: it reads `_figtest/<tag>_result.json`, which is what a past
run produced. It does NOT call the model, so it is safe to run in CI and it
always scores the SAME artefacts -- which is the property the two ad-hoc
scorers did not have.

Figures with no entry in ``ground_truth.GROUND_TRUTH`` are reported as
"not transcribed", never as "clean". That distinction is the whole point: an
absent ground truth must not read as a passing result.
"""
import argparse
import io
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

from ground_truth import (  # noqa: E402
    GROUND_TRUTH,
    KNOWN_ASSEMBLED_LABELS,
    KNOWN_TRANSCRIPTION_ERRORS,
)
from scorer import score  # noqa: E402

ART = REPO / "_figtest"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-ocr", action="store_true",
                    help="feed round2_ocr.json in as weak supporting evidence")
    ap.add_argument("--prefix", default="",
                    help="artifact prefix, e.g. 'r3_' to score round 3 "
                         "results instead of round 2. Round 2's files are the "
                         "BEFORE baseline for every fix in this cycle, so the "
                         "runner must never overwrite them -- and scoring the "
                         "two side by side is the whole point.")
    ap.add_argument("--out", default="eval_report.json",
                    help="report filename under _figtest/")
    args = ap.parse_args()

    ocr_all = {}
    ocr_path = ART / "round2_ocr.json"
    if args.with_ocr and ocr_path.is_file():
        ocr_all = json.load(io.open(ocr_path, encoding="utf-8"))

    report = {}
    print("%-14s %7s %7s %7s %8s  %s"
          % ("FIGURE", "TRUTH", "PRODUCED", "UNSUP", "RECALL", "MODE"))
    print("-" * 74)
    for tag in sorted({m["tag"] for m in
                       json.load(io.open(ART / "round2_manifest.json",
                                         encoding="utf-8"))}):
        rp = ART / ("%s%s_result.json" % (args.prefix, tag))
        if not rp.is_file():
            continue
        truth = GROUND_TRUTH.get(tag)
        if truth is None:
            report[tag] = {"status": "not_transcribed"}
            print("%-14s %7s %7s %7s %8s  %s"
                  % (tag, "-", "-", "-", "-",
                     "NOT TRANSCRIBED - absence of truth is not a pass"))
            continue
        data = json.load(io.open(rp, encoding="utf-8"))
        ocr = [l["text"] for l in
               (ocr_all.get(tag, {}).get("merged") or [])]
        out = score(data, truth, ocr)
        out["status"] = "scored"
        report[tag] = out
        print("%-14s %7d %7d %7d %8s  %s"
              % (tag, out["n_truth"], out["n_produced"], out["n_unsupported"],
                 out["recall"], truth.get("expect_mode", "?")))

    print("\n--- known errors: does the scorer still catch them? ---")
    ok = True
    for tag, produced, printed, how in KNOWN_TRANSCRIPTION_ERRORS:
        truth = GROUND_TRUTH.get(tag)
        if not truth:
            continue
        out = score({"species_ranges": [{"species": produced}],
                     "abundances": [{"taxon": produced}]},
                    {**truth, "names": [printed]})
        caught = out["unsupported"] == [produced]
        ok = ok and caught
        print("  [%s] %-14s %s" % ("OK  " if caught else "MISS", tag, produced))
        if not caught:
            print("        scorer treated it as supported -- evidence %r"
                  % out["supported"])
    print("  -> %s" % ("all known transcription errors are caught"
                        if ok else "AT LEAST ONE IS NO LONGER CAUGHT"))

    print("\n--- assembled labels (printed as two separate cells) ---")
    for tag, assembled, note in KNOWN_ASSEMBLED_LABELS:
        truth = GROUND_TRUTH.get(tag)
        mark = "in truth set" if truth else "figure not transcribed yet"
        print("  %-14s %-46s [%s]" % (tag, assembled, mark))

    json.dump(report, io.open(ART / args.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nwrote _figtest/%s" % args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
