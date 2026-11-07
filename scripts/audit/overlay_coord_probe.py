#!/usr/bin/env python3
"""P1-9 / G2 evidence probe: what does ``pos_0_999`` actually map to?

WHY THIS EXISTS
===============
docs/OWNER_DECISIONS.md G2 needs an owner decision between a "true" overlay
(endpoints mapped back to PIXELS in the original figure) and a "half-true"
one (annotations drawn in the normalised 0..999 coordinate system).  The
decision was blocked on one factual question nobody had measured:

    is ``pos_0_999`` a pixel coordinate, or a normalised axis position --
    and does ``axis_calibration`` carry enough information to invert it
    back into original-image pixels?

This script answers it from the REAL payload fixtures committed to the repo,
not from the prompt docs.  It is deliberately read-only and stdlib-only so
it can be rerun whenever the schema changes.  A regression in the answer
(e.g. somebody adds pixel anchors) is exactly what should force G2 to be
revisited, so tests/test_overlay_coord_probe.py calls ``probe()`` and pins
the conclusion.

Usage::

    python scripts/audit/overlay_coord_probe.py           # human-readable
    python scripts/audit/overlay_coord_probe.py --json    # machine-readable

Exit code is 0 when the probe ran, regardless of the conclusion; the
conclusion is data, not a pass/fail verdict.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "real_payloads"
POS_SUFFIX = "_pos_0_999"
POS_KEY = "pos_0_999"
SCALE_MAX = 999.0

#: Any of these appearing under axis_calibration would be a PIXEL anchor.
#: rca_core/geometry.py::Anchor expects {"pixel", "data_value"} pairs; the
#: prompt-side axis_calibration uses {"at_0", "at_999", "unit"} instead.
PIXEL_ANCHOR_HINTS = (
    "pixel", "px", "x_pixel", "y_pixel", "pixel_at_0", "pixel_at_999",
    "data_value", "anchors", "tick_pixels", "image_width", "image_height",
)


def _iter_pos_fields(node: Any, path: str = ""):
    """Yield (path, key, value) for every pos_0_999-shaped field."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key == POS_KEY or key.endswith(POS_SUFFIX):
                yield here, key, value
            yield from _iter_pos_fields(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _iter_pos_fields(value, f"{path}[{i}]")


def _pos_number(value: Any) -> float | None:
    """A pos field is either a number or {"pos": number, ...}."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        raw = value.get("pos")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw)
    return None


def _declared_value(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("value")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _calibration_endpoints(cal: Any, axis: str) -> tuple[float, float] | None:
    """Return (value_at_0, value_at_999) for one axis, or None."""
    if not isinstance(cal, dict):
        return None
    spec = cal.get(axis)
    if not isinstance(spec, dict):
        return None
    lo, hi = spec.get("at_0"), spec.get("at_999")
    if isinstance(lo, bool) or isinstance(hi, bool):
        return None
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return None
    return float(lo), float(hi)


def _find_pixel_anchors(cal: Any) -> list[str]:
    hits: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else key
                if key.lower() in PIXEL_ANCHOR_HINTS:
                    hits.append(here)
                walk(value, here)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(cal, "axis_calibration")
    return hits


def probe(fixture_dir: pathlib.Path | None = None) -> dict[str, Any]:
    """Measure the coord schema over every committed real payload."""
    directory = fixture_dir or FIXTURE_DIR
    files = sorted(directory.glob("*.json"))
    report: dict[str, Any] = {
        "fixture_dir": str(directory),
        "files": len(files),
        "files_with_calibration": 0,
        "files_with_pos": 0,
        "files_with_both": 0,
        "calibration_shapes": [],
        "pixel_anchor_hits": [],
        "linear_checks": [],
        "max_abs_residual": None,
        "conclusion": None,
    }
    residuals: list[float] = []

    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        payload = doc.get("data", doc)
        if not isinstance(payload, dict):
            continue
        cal = payload.get("axis_calibration")
        pos_fields = list(_iter_pos_fields(payload))
        has_cal = isinstance(cal, dict) and bool(cal)
        has_pos = bool(pos_fields)
        report["files_with_calibration"] += int(has_cal)
        report["files_with_pos"] += int(has_pos)
        report["files_with_both"] += int(has_cal and has_pos)
        if has_cal:
            report["calibration_shapes"].append(
                {"file": path.name, "axes": sorted(cal),
                 "raw": json.dumps(cal, ensure_ascii=False, sort_keys=True)[:400]}
            )
            report["pixel_anchor_hits"].extend(
                f"{path.name}:{hit}" for hit in _find_pixel_anchors(cal)
            )

        for field_path, _key, value in pos_fields:
            pos = _pos_number(value)
            if pos is None or not has_cal:
                continue
            axis = "vertical"
            if isinstance(value, dict) and isinstance(value.get("axis"), str):
                axis = value["axis"]
            ends = _calibration_endpoints(cal, axis)
            declared = _declared_value(value)
            if ends is None:
                continue
            lo, hi = ends
            predicted = lo + (pos / SCALE_MAX) * (hi - lo)
            residual = None if declared is None else abs(declared - predicted)
            if residual is not None:
                residuals.append(residual)
            report["linear_checks"].append({
                "file": path.name,
                "field": field_path,
                "axis": axis,
                "pos": pos,
                "predicted_value": round(predicted, 6),
                "declared_value": declared,
                "abs_residual": None if residual is None else round(residual, 6),
            })

    report["linear_checks_checked"] = len(report["linear_checks"])
    report["linear_checks_with_declared_value"] = len(residuals)
    report["max_abs_residual"] = max(residuals) if residuals else None

    # The decision this probe exists to inform.
    has_pixel_anchors = bool(report["pixel_anchor_hits"])
    exact_linear = bool(residuals) and max(residuals) < 0.01
    report["pos_maps_to_data_value_linearly"] = exact_linear
    report["pos_maps_to_pixel"] = has_pixel_anchors
    if has_pixel_anchors:
        report["conclusion"] = (
            "axis_calibration now carries pixel anchors; a TRUE overlay is "
            "possible. Revisit G2."
        )
    elif exact_linear:
        report["conclusion"] = (
            "pos_0_999 is a NORMALISED axis position, not a pixel. "
            "axis_calibration stores only the DATA values at 0 and 999 "
            "(at_0 / at_999 / unit), with no pixel-side anchors, so "
            "pos -> data-value is exact linear arithmetic but "
            "pos -> ORIGINAL-IMAGE PIXEL is underdetermined. A true overlay "
            "needs a new pixel-anchor capture step (rca_core/geometry.py "
            "already implements the fit); the half-true overlay in the "
            "normalised coordinate system needs no new data."
        )
    else:
        report["conclusion"] = (
            "INCONCLUSIVE: not enough payloads carry both axis_calibration "
            "and declared pos values to verify the linear mapping, and no "
            "pixel anchors were found. Do not decide G2 from this run."
        )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--fixture-dir", type=pathlib.Path, default=None)
    args = ap.parse_args(argv)
    rep = probe(args.fixture_dir)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print("=== P1-9 / G2 coordinate probe ===")
    print(f"fixtures scanned            : {rep['files']}")
    print(f"with axis_calibration       : {rep['files_with_calibration']}")
    print(f"with pos_0_999 fields       : {rep['files_with_pos']}")
    print(f"with both                   : {rep['files_with_both']}")
    print(f"linear checks (declared)    : {rep['linear_checks_with_declared_value']}")
    print(f"max |declared - predicted|  : {rep['max_abs_residual']}")
    print(f"pixel anchors in calibration: {rep['pixel_anchor_hits'] or 'NONE'}")
    print("calibration shapes:")
    for shape in rep["calibration_shapes"]:
        print(f"  {shape['file']}: {shape['raw']}")
    print(f"pos -> data value linear    : {rep['pos_maps_to_data_value_linearly']}")
    print(f"pos -> pixel possible       : {rep['pos_maps_to_pixel']}")
    print(f"CONCLUSION: {rep['conclusion']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
