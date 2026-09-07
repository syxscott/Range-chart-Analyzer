"""Recreate an extracted range chart as a normalized image.

Borrowed from the GitHub survey (2026-09-07, thu-digitizer's
``recreated.png`` / FigDataX's validation overlay): plot the extracted
data with deterministic code and compare it against the original figure
side by side. A redraw that structurally matches the source is strong
evidence the extraction is sound; a mismatch points the operator at the
bad rows. This automates exactly the manual cross-check an evaluator
performs.

Scope (v1): range_chart species ranges as horizontal bars (base -> top
bed index / ordinal position), ordered top-youngest like a real range
chart. matplotlib is an OPTIONAL dependency - without it the module
returns ``None`` and callers degrade silently (the project's standard
optional-dependency contract).
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["redraw_range_chart"]


def _to_num(value: Any, ordinal: int) -> float:
    """Bed labels are usually numbers-as-strings ('12'); fall back to the
    row's ordinal position so mixed labels still produce a drawing."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return float(ordinal)


def redraw_range_chart(
    data: dict[str, Any],
    *,
    title: str = "Recreated species ranges (from extracted data)",
    max_rows: int = 60,
) -> Optional[bytes]:
    """Render extracted ``species_ranges`` as a normalized PNG.

    Returns PNG bytes, or ``None`` when matplotlib is unavailable or the
    payload has no rows to draw. Never raises to the caller.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless-safe; must precede pyplot import
        import io as _io

        import matplotlib.pyplot as plt
    except Exception:
        return None

    rows = data.get("species_ranges") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    rows = rows[:max_rows]

    labels: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    for i, r in enumerate(rows, start=1):
        if not isinstance(r, dict):
            continue
        species = str(r.get("species") or f"row {i}")
        base = _to_num(r.get("range_base"), i * 2)
        top = _to_num(r.get("range_top"), i * 2 - 1)
        labels.append(species)
        starts.append(min(base, top))
        ends.append(max(base, top))
    if not labels:
        return None

    try:
        fig, ax = plt.subplots(figsize=(8, max(2.0, 0.38 * len(labels) + 1.0)))
        y = range(len(labels))
        ax.barh(y, width=[e - s for s, e in zip(starts, ends)],
                left=starts, height=0.55, color="#2f6fab", alpha=0.85)
        ax.set_yticks(list(y))
        ax.set_yticklabels(labels, fontsize=8, style="italic")
        ax.invert_yaxis()  # stratigraphic convention: oldest at the bottom
        ax.set_xlabel("stratigraphic position (bed / level, as extracted)")
        ax.set_title(title, fontsize=10)
        ax.grid(axis="x", alpha=0.25)
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
