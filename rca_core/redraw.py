"""Recreate an extracted range chart as a normalized image.

Borrowed from the GitHub survey (2026-09-07, thu-digitizer's
``recreated.png`` / FigDataX's validation overlay): plot the extracted
data with deterministic code and compare it against the original figure
side by side. A redraw that structurally matches the source is strong
evidence the extraction is sound; a mismatch points the operator at the
bad rows. This automates exactly the manual cross-check an evaluator
performs.

Scope (v2): range_chart species ranges as horizontal bars (base -> top
bed index / ordinal position), ordered top-youngest like a real range
chart. matplotlib is an OPTIONAL dependency - without it the module
returns ``None`` and callers degrade silently (the project's standard
optional-dependency contract).

REVIEW-2026-09-20 (finding: fabricated positions): a validation redraw is
only worth looking at when every plotted coordinate came from the
extraction. v1 silently invented one: when a bed label was unparseable the
row fell back to its *ordinal* (``i * 2`` / ``i * 2 - 1``), so an
unreadable row still drew a tidy one-unit bar at a made-up position, and
``min()/max()`` then smoothed an inverted FAD/LAD pair into a plausible
interval. Both behaviours hid exactly the defects the overlay exists to
surface. v2 therefore

* resolves each endpoint from ``range_base_idx`` / ``range_top_idx`` first,
  then ``bed_parser.parse_bed_int`` on the bed labels - and from NOTHING
  else;
* draws a row it cannot locate as an explicit gray "unresolved" placeholder
  and reports the count in the figure caption (:func:`resolve_species_range_rows`
  exposes the same numbers to callers without matplotlib);
* keeps an inverted pair (``range_base > range_top``) inverted and marks it
  with a red outline instead of swapping the endpoints away.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from .bed_parser import parse_bed_int

__all__ = ["redraw_range_chart", "resolve_species_range_rows"]

# Column colours of the overlay - also used by the legend proxies below.
_COLOR_RANGE = "#2f6fab"
_COLOR_INVERTED_FILL = "#f7c8c2"
_COLOR_INVERTED_EDGE = "#c62828"
_COLOR_UNRESOLVED_FILL = "#bdbdbd"
_COLOR_UNRESOLVED_EDGE = "#7a7a7d"

# Which row key each endpoint came from, for the caption / caller diagnostics.
SOURCE_INDEX = "idx"      # range_base_idx / range_top_idx (extractor int index)
SOURCE_BED = "bed"        # bed_parser.parse_bed_int on the bed label
SOURCE_NONE = ""          # nothing in the row locates this endpoint


def _as_number(value: Any) -> Optional[float]:
    """Strict numeric read of an extracted bed *index* field.

    Accepts ints, finite floats and digit-as-string payloads (JSON round
    trips turn ``3`` into ``"3"``). Booleans are rejected - ``True`` is not
    bed 1 - and so are ages, empty strings and anything non-finite.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n if math.isfinite(n) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            n = float(text)
        except ValueError:
            return None
        return n if math.isfinite(n) else None
    return None


def _locate_endpoint(row: dict[str, Any], which: str) -> tuple[Optional[float], str]:
    """Resolve one endpoint (``'base'`` / ``'top'``) to a plottable number.

    Priority (REVIEW-2026-09-20): the extractor's integer index field
    ``range_{which}_idx`` first, then ``parse_bed_int`` over the label
    strings (``range_{which}``, then the exact-label ``range_{which}_bed``).
    Returns ``(None, SOURCE_NONE)`` when neither reads as a bed - the caller
    then draws an unresolved placeholder. There is deliberately **no**
    ordinal fallback: a position that was not extracted must not be plotted
    as if it had been.
    """
    idx = _as_number(row.get(f"range_{which}_idx"))
    if idx is not None:
        return idx, SOURCE_INDEX
    for key in (f"range_{which}", f"range_{which}_bed"):
        raw = row.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            bed = parse_bed_int(raw)
        except Exception:  # pragma: no cover - parse_bed_int never raises
            bed = None
        if bed is not None:
            return float(bed), SOURCE_BED
    return None, SOURCE_NONE


def _label(value: Any, fallback: str) -> str:
    """Read a species label without ever raising (an OCR payload can hold any
    type, and this module is documented never to propagate)."""
    try:
        text = str(value or "").strip()
    except Exception:
        return fallback
    return text or fallback


def resolve_species_range_rows(
    rows: Any,
    *,
    max_rows: int = 60,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Resolve ``species_ranges`` rows into honest plot positions.

    Returns ``(resolved, summary)``. Each resolved entry is
    ``{"row": i, "species", "base", "top", "base_source", "top_source",
    "inverted", "unresolved"}`` where ``base`` / ``top`` are ``None`` when
    the extraction does not locate that endpoint. ``inverted`` follows the
    project-wide convention (the exporter's ``range_base_le_range_top``
    constraint, quality.py's FAD/LAD check): a bed number must not decrease
    from the base (old) to the top (young) limit.

    ``summary`` carries the counts the figure caption reports:
    ``rows_total``, ``rows_plotted``, ``located``, ``unresolved``,
    ``partially_located``, ``inverted``, ``truncated``.
    """
    source_rows = rows if isinstance(rows, list) else []
    kept = source_rows[:max_rows] if max_rows and max_rows > 0 else list(source_rows)
    resolved: list[dict[str, Any]] = []
    for i, r in enumerate(kept, start=1):
        if not isinstance(r, dict):
            # An unreadable row is a validation finding, not a blank: it is
            # plotted as unresolved rather than dropped or invented.
            resolved.append({
                "row": i,
                "species": f"row {i} (unreadable)",
                "base": None, "top": None,
                "base_source": SOURCE_NONE, "top_source": SOURCE_NONE,
                "inverted": False, "unresolved": True,
            })
            continue
        base, base_src = _locate_endpoint(r, "base")
        top, top_src = _locate_endpoint(r, "top")
        species = _label(r.get("species"), f"row {i}")
        inverted = (
            base is not None and top is not None and base > top
        )
        resolved.append({
            "row": i,
            "species": species,
            "base": base, "top": top,
            "base_source": base_src, "top_source": top_src,
            "inverted": inverted,
            "unresolved": base is None or top is None,
        })

    located = sum(1 for e in resolved if not e["unresolved"])
    summary = {
        "rows_total": len(source_rows),
        "rows_plotted": len(resolved),
        "located": located,
        "unresolved": sum(1 for e in resolved if e["unresolved"]),
        "partially_located": sum(
            1 for e in resolved
            if (e["base"] is None) != (e["top"] is None)
        ),
        "inverted": sum(1 for e in resolved if e["inverted"]),
        "truncated": max(0, len(source_rows) - len(resolved)),
    }
    return resolved, summary


def _caption(summary: dict[str, int]) -> str:
    """Second title line: what the drawing is *not* able to claim."""
    parts = [
        f"{summary['located']}/{summary['rows_plotted']} rows located from the "
        "extracted data",
        f"unresolved bed position: {summary['unresolved']}",
        f"inverted base>top: {summary['inverted']}",
    ]
    if summary.get("truncated"):
        parts.append(f"not drawn (max_rows): {summary['truncated']}")
    return " | ".join(parts)


def redraw_range_chart(
    data: dict[str, Any],
    *,
    title: str = "Recreated species ranges (from extracted data)",
    max_rows: int = 60,
) -> Optional[bytes]:
    """Render extracted ``species_ranges`` as a normalized PNG.

    Returns PNG bytes, or ``None`` when matplotlib is unavailable or the
    payload has no rows to draw. Never raises to the caller.

    Rows whose endpoints cannot be located are drawn as gray hatched
    placeholders (with the known endpoint ticked when only one side is
    missing); rows whose base is numerically above their top keep the
    inverted direction and get a red outline. The caption reports both
    counts, so the image can never imply a cleaner extraction than the one
    that was actually made.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless-safe; must precede pyplot import
        import io as _io

        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except Exception:
        return None

    rows = data.get("species_ranges") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return None

    resolved, summary = resolve_species_range_rows(rows, max_rows=max_rows)
    if not resolved:
        return None

    labels = [e["species"] for e in resolved]
    values = [
        v for e in resolved for v in (e["base"], e["top"]) if v is not None
    ]
    if values:
        lo, hi = min(values), max(values)
        pad = max(0.5, (hi - lo) * 0.05)
        x_lo, x_hi = lo - pad, hi + pad
    else:  # nothing in the payload is locatable: the placeholders still need an axis
        x_lo, x_hi = 0.0, 1.0

    try:
        fig, ax = plt.subplots(figsize=(8, max(2.0, 0.38 * len(labels) + 1.2)))
        y = range(len(labels))

        normal_rows = [(i, e) for i, e in enumerate(resolved)
                       if not e["unresolved"] and not e["inverted"]]
        spans = [(i, e) for i, e in normal_rows if e["top"] != e["base"]]
        points = [(i, e) for i, e in normal_rows if e["top"] == e["base"]]
        if spans:
            ax.barh(
                [i for i, _e in spans],
                width=[e["top"] - e["base"] for _i, e in spans],
                left=[e["base"] for _i, e in spans],
                height=0.55, color=_COLOR_RANGE, alpha=0.85,
                label="extracted range",
            )
        if points:
            # A range confined to one bed has zero width: draw it as a tick
            # so a real extraction is never invisible on its own chart.
            ax.plot(
                [e["base"] for _i, e in points], [i for i, _e in points],
                linestyle="none", marker="|", markersize=14,
                color=_COLOR_RANGE, markeredgewidth=2.2,
            )

        # Inverted pairs: drawn from base toward top, i.e. RIGHT-TO-LEFT
        # (negative width), so the direction the extraction actually claims
        # stays visible, plus a red outline as the anomaly marker.
        inverted_rows = [(i, e) for i, e in enumerate(resolved) if e["inverted"]]
        for i, e in inverted_rows:
            ax.barh(
                i, width=e["top"] - e["base"], left=e["base"], height=0.55,
                facecolor=_COLOR_INVERTED_FILL, edgecolor=_COLOR_INVERTED_EDGE,
                linewidth=1.6,
            )
            ax.annotate(
                "base>top",
                xy=(e["base"], i), xytext=(4, 6), textcoords="offset points",
                fontsize=6, color=_COLOR_INVERTED_EDGE,
            )

        # Unresolved rows: an honest "we do not know where" band.
        unresolved_rows = [
            (i, e) for i, e in enumerate(resolved)
            if e["unresolved"] and not e["inverted"]
        ]
        for i, e in unresolved_rows:
            ax.barh(
                i, width=x_hi - x_lo, left=x_lo, height=0.55,
                facecolor=_COLOR_UNRESOLVED_FILL, edgecolor=_COLOR_UNRESOLVED_EDGE,
                alpha=0.25, hatch="///", linewidth=0.6,
            )
            # The band means "we do not know where", not "the range is this
            # wide" - say so on the row itself, not only in the legend.
            ax.annotate(
                "bed position not extracted",
                xy=(x_lo, i), xytext=(3, 0), textcoords="offset points",
                va="center", fontsize=6, color=_COLOR_UNRESOLVED_EDGE,
            )
            # Tick the endpoint that IS known, so partial evidence is not lost.
            known = e["base"] if e["base"] is not None else e["top"]
            if known is not None:
                ax.plot([known], [i], marker="|", markersize=12,
                        color=_COLOR_UNRESOLVED_EDGE, markeredgewidth=1.6)

        ax.set_yticks(list(y))
        ax.set_yticklabels(labels, fontsize=8, style="italic")
        ax.invert_yaxis()  # stratigraphic convention: oldest at the bottom
        ax.set_xlim(x_lo, x_hi)
        ax.set_xlabel("stratigraphic position (bed index, as extracted)")
        ax.set_title(f"{title}\n{_caption(summary)}", fontsize=9)
        ax.grid(axis="x", alpha=0.25)

        handles = [Patch(facecolor=_COLOR_RANGE, alpha=0.85, label="extracted range")]
        if summary["inverted"]:
            handles.append(Patch(
                facecolor=_COLOR_INVERTED_FILL, edgecolor=_COLOR_INVERTED_EDGE,
                linewidth=1.6,
                label=f"inverted base>top ({summary['inverted']})",
            ))
        if summary["unresolved"]:
            handles.append(Patch(
                facecolor=_COLOR_UNRESOLVED_FILL, edgecolor=_COLOR_UNRESOLVED_EDGE,
                alpha=0.25, hatch="///",
                label=f"unresolved bed position ({summary['unresolved']})",
            ))
        ax.legend(handles=handles, loc="lower right", fontsize=7, framealpha=0.9)

        fig.tight_layout()
        buf = _io.BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return None
