"""Generate HTML report from gold-standard test results.

Usage:
    python scripts/gold_report.py results.json output.html
    python scripts/gold_report.py results.json output.html --title "VLM Accuracy Report"

Input (results.json):
{
  "timestamp": "2026-07-27T00:00:00Z",
  "cases": [
    {
      "id": "rc_synth_001",
      "type": "range_chart",
      "metrics": {
        "species_precision_recall": {"precision": 0.95, "recall": 0.9, "f1": 0.925},
        "range_top_accuracy": {"acc_exact": 0.8, "acc_tolerance": 0.9},
        "biozone_accuracy": {"precision": 1.0, "recall": 1.0, "f1": 1.0}
      },
      "passed": true
    }
  ],
  "summary": {
    "total_cases": 8,
    "passed": 6,
    "failed": 2,
    "overall_f1": 0.91
  }
}

BORROW-2026-09-20: ``metrics`` may additionally carry the additive blocks
emitted by ``rca_core.eval_metrics.tiered_eval_report`` — ``boundary_tiers``
(four-tier proportions + weighted score), ``error_typology`` (per-label error
counts), ``tracks`` (descriptive vs reasoning, deliberately not blended) and
``refusals`` (refusal / uncertainty rate beside precision). They render as
extra lines per case; a results file without them renders exactly as before.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def main(results_path: Path, output_path: Path, title: str = "VLM Accuracy Report"):
    results = json.loads(results_path.read_text(encoding="utf-8"))
    html = generate_html(results, title)
    output_path.write_text(html, encoding="utf-8")
    print(f"Report written to: {output_path}")


def _fmt(value: Any, digits: int = 2) -> str:
    """Render one metric number: FIX-2026-09-22 — None means "not measured".

    ``rca_core.eval_metrics`` reports an undefined ratio (zero denominator)
    as None instead of a fake 0.0, so the renderer must show "n/a" there —
    mirroring the deliberate refusal=n/a path below — rather than a
    flattering or penalizing 0.00 for something that was never measured.
    """
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def _borrow_metric_lines(metrics: dict) -> list[str]:
    """BORROW-2026-09-20: one HTML line per new eval_metrics layer.

    Reads the additive blocks ``rca_core.eval_metrics.tiered_eval_report``
    emits (tiered boundaries, error typology, split tracks, refusal rate) and
    returns nothing at all for a legacy results file, so old and new reports
    share this renderer. Undefined ratios (None) render as "n/a".
    """
    lines: list[str] = []
    tiers = (metrics.get("boundary_tiers") or {}).get("row") or {}
    if tiers:
        rates = tiers.get("rates") or {}
        rendered = " ".join(
            f"{tier}={_fmt(rates[tier])}" for tier in
            ("strict", "adjacent", "coarse", "wrong") if tier in rates
        )
        lines.append(f"tiers {rendered} weighted={_fmt(tiers.get('weighted_score', 0))}")
    typology = metrics.get("error_typology") or {}
    counts = typology.get("counts") or {}
    if counts:
        errors = {k: v for k, v in counts.items() if v and k != "correct"}
        rendered = " ".join(f"{k}={v}" for k, v in sorted(errors.items())) or "none"
        lines.append(f"errors {rendered}")
        # FIX-2026-09-22 (audit item 1): duplicated ground-truth keys are
        # listed by eval_metrics; surface them instead of hiding first-wins.
        duplicates = typology.get("duplicate_ground_truth") or []
        for dup in duplicates:
            lines.append(
                f"duplicate gt rows section={dup.get('section') or '—'}"
                f" species={dup.get('species') or '—'} rows={dup.get('rows')}"
                " (first row kept)"
            )
    tracks = metrics.get("tracks") or {}
    if tracks.get("descriptive") or tracks.get("reasoning"):
        lines.append(
            f"descriptive={_fmt((tracks['descriptive'] or {}).get('score'))}"
            f" reasoning={_fmt((tracks['reasoning'] or {}).get('score'))}"
        )
    refusals = metrics.get("refusals") or {}
    if refusals:
        if refusals.get("refusal_field_present"):
            # FIX-2026-09-22 (audit item 5, A-6): when EVERY row was declined
            # the headline species trio is a label-only optic; say so loudly.
            suffix = " [all rows refused: P/R/F1 label-only]" if refusals.get("all_rows_refused") else ""
            lines.append(
                f"refusal={_fmt(refusals.get('refusal_rate', 0))}"
                f" (not_drawn={_fmt(refusals.get('not_drawn_rate', 0))}"
                f" uncertain={_fmt(refusals.get('uncertain_rate', 0))})"
                f" P_answered={_fmt(refusals.get('precision_on_answered', 0))}"
                f" R_answered={_fmt(refusals.get('recall_on_answered', 0))}"
                + suffix
            )
        else:
            # BORROW-2026-09-20: be explicit that the refusal rate is UNKNOWN
            # here rather than silently 0 — the run predates response_kind.
            lines.append("refusal=n/a (no response_kind; unanswered cells count as omission)")
    return lines


def generate_html(results: dict[str, Any], title: str = "VLM Accuracy Report") -> str:
    cases = results.get("cases", [])
    summary = results.get("summary", {})
    # FIX-2026-09-22 (audit item 5): datetime.utcnow() is deprecated and naive;
    # stamp a timezone-aware UTC time and keep the trailing-Z wire format.
    timestamp = results.get(
        "timestamp", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    # Aggregate metrics by type
    by_type: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        ct = case.get("type", "unknown")
        by_type.setdefault(ct, []).append(case)

    # Per-type summary cards
    type_cards = ""
    for ct, ct_cases in by_type.items():
        total = len(ct_cases)
        passed = sum(1 for c in ct_cases if c.get("passed"))
        avg_f1 = 0.0
        f1_vals = []
        for c in ct_cases:
            m = c.get("metrics", {})
            spr = m.get("species_precision_recall", {})
            f1 = spr.get("f1", 0.0)
            if f1:
                f1_vals.append(f1)
        if f1_vals:
            avg_f1 = round(sum(f1_vals) / len(f1_vals), 3)
        pct = round(passed / total * 100) if total else 0
        color = "#2ecc71" if pct >= 80 else "#f39c12" if pct >= 60 else "#e74c3c"
        type_cards += f"""
        <div class="type-card">
            <h3>{ct.replace('_', ' ').title()}</h3>
            <div class="big-num" style="color:{color}">{pct}%</div>
            <div class="sub">{passed}/{total} passed</div>
            <div class="sub">Avg F1: {avg_f1}</div>
        </div>"""

    # Per-case tables
    case_rows = ""
    for case in cases:
        cid = case.get("id", "")
        ct = case.get("type", "")
        passed = case.get("passed", False)
        status_cls = "pass" if passed else "fail"
        status_txt = "PASS" if passed else "FAIL"
        m = case.get("metrics", {})

        spr = m.get("species_precision_recall", {})
        rta = m.get("range_top_accuracy", {})
        bza = m.get("biozone_accuracy", {})
        abe = m.get("abundance_sum_error", {})
        phd = m.get("phylogenetic_topology_distance", {})

        # Build metrics cell
        metrics_parts = []
        if spr:
            metrics_parts.append(f"P={spr.get('precision',0):.2f} R={spr.get('recall',0):.2f} F1={spr.get('f1',0):.2f}")
        if rta:
            metrics_parts.append(f"top_exact={rta.get('acc_exact',0):.2f} tol={rta.get('acc_tolerance',0):.2f}")
        if bza:
            metrics_parts.append(f"BZ F1={bza.get('f1',0):.2f}")
        if abe:
            metrics_parts.append(f"sum_err={abe.get('max_error',0):.1f}%")
        if phd is not None:
            metrics_parts.append(f"RF_dist={phd}")
        # BORROW-2026-09-20: the new eval_metrics layers are optional siblings,
        # so a results file produced before them still renders unchanged.
        metrics_parts.extend(_borrow_metric_lines(m))

        metrics_cell = "<br>".join(metrics_parts) if metrics_parts else "—"

        case_rows += f"""
        <tr class="{status_cls}">
            <td>{cid}</td>
            <td>{ct}</td>
            <td class="status {status_cls}">{status_txt}</td>
            <td class="metrics">{metrics_cell}</td>
        </tr>"""

    overall_f1 = summary.get("overall_f1", 0.0)
    total = summary.get("total_cases", len(cases))
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    overall_pct = round(passed / total * 100) if total else 0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; background: #f8f9fa; }}
    h1 {{ color: #2c3e50; }}
    .header {{ display: flex; justify-content: space-between; align-items: center; }}
    .timestamp {{ color: #7f8c8d; font-size: 0.9rem; }}
    .summary-bar {{ background: white; border-radius: 8px; padding: 1.5rem; margin: 1rem 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
    .summary-bar h2 {{ margin: 0 0 0.5rem 0; }}
    .big-num {{ font-size: 3rem; font-weight: 700; line-height: 1; }}
    .sub {{ color: #7f8c8d; font-size: 0.9rem; }}
    .type-cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 1rem 0; }}
    .type-card {{ background: white; border-radius: 8px; padding: 1rem 1.5rem; min-width: 150px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
    .type-card h3 {{ margin: 0 0 0.5rem 0; font-size: 0.9rem; color: #7f8c8d; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
    th {{ background: #2c3e50; color: white; padding: 0.75rem; text-align: left; font-size: 0.85rem; }}
    td {{ padding: 0.6rem 0.75rem; border-bottom: 1px solid #ecf0f1; font-size: 0.9rem; }}
    tr:last-child td {{ border-bottom: none; }}
    .status.pass {{ color: #27ae60; font-weight: 700; }}
    .status.fail {{ color: #e74c3c; font-weight: 700; }}
    tr.fail {{ background: #fdf2f2; }}
    tr.pass {{ background: #f2fdf2; }}
    .metrics {{ font-size: 0.8rem; color: #555; font-family: monospace; }}
    .footer {{ margin-top: 2rem; color: #7f8c8d; font-size: 0.8rem; text-align: center; }}
</style>
</head>
<body>
<div class="header">
    <h1>{title}</h1>
    <div class="timestamp">Generated: {timestamp}</div>
</div>

<div class="summary-bar">
    <h2>Overall: {overall_pct}% ({passed}/{total} cases passed)</h2>
    <div>Weighted F1: <strong>{overall_f1}</strong></div>
    <div style="margin-top:0.5rem">
        <span style="color:#27ae60">● {passed} passed</span>
        &nbsp;&nbsp;
        <span style="color:#e74c3c">● {failed} failed</span>
    </div>
</div>

<div class="type-cards">
    {type_cards}
</div>

<table>
<thead>
<tr>
    <th>Case ID</th>
    <th>Type</th>
    <th>Status</th>
    <th>Metrics</th>
</tr>
</thead>
<tbody>
{case_rows}
</tbody>
</table>

<div class="footer">
    Generated by Range Chart Analyzer Gold-Standard Framework &middot; {timestamp}
</div>
</body>
</html>"""


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate HTML report from gold-standard results")
    parser.add_argument("results", type=Path, help="Path to results JSON file")
    parser.add_argument("output", type=Path, help="Path to output HTML file")
    parser.add_argument("--title", default="VLM Accuracy Report", help="Report title")
    args = parser.parse_args()

    sys.exit(main(args.results, args.output, args.title))
