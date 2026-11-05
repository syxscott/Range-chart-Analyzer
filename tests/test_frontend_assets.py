"""js/table.js is used by TWO frontends, and nothing used to assert it.

WHY THIS EXISTS
===============
``gui_fluent_history_detail.py`` inlines js/table.js + js/aggregate.js +
js/i18n.js into a QWebEngineView and calls ``rcaRenderResults`` to draw the
history detail page. That is a good decision -- it removes a whole second
table renderer -- but it is undiscoverable: no architecture document named
it, no test asserted it, and the dialog has a silent fallback to a TreeView
(``gui_fluent_history_detail.py:286``). Deleting or renaming a function in
js/table.js could therefore leave the browser tests green while the desktop
history page quietly stops rendering.

These tests run in the main CI job (no Qt needed): they only read files.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Assets the desktop detail view inlines. Keep in sync with
#: gui_fluent_history_detail.py:69-71.
SHARED_ASSETS = ("js/table.js", "js/aggregate.js", "js/i18n.js")

#: Symbols the Python-side HTML calls after inlining the scripts.
REQUIRED_SYMBOLS = ("rcaRenderResults", "rcaTableConfigs")


def test_shared_frontend_assets_exist_and_are_nonempty() -> None:
    for rel in SHARED_ASSETS:
        path = ROOT / rel
        assert path.is_file(), f"missing shared frontend asset: {rel}"
        assert path.stat().st_size > 1000, f"{rel} looks truncated"


def test_desktop_detail_view_still_points_at_the_shared_renderer() -> None:
    src = (ROOT / "gui_fluent_history_detail.py").read_text(encoding="utf-8")
    for var, rel in (
        ("_JS_TABLE", "table.js"),
        ("_JS_AGGREGATE", "aggregate.js"),
        ("_JS_I18N", "i18n.js"),
    ):
        pattern = rf'{var}\s*=\s*_PROJECT_ROOT\s*/\s*"js"\s*/\s*"{rel}"'
        assert re.search(pattern, src), (
            f"{var} no longer resolves js/{rel}; the desktop detail view "
            "changed its asset contract - update SHARED_ASSETS here and in "
            "docs/ARCHITECTURE.md."
        )
    assert "rcaRenderResults" in src, (
        "the desktop HTML stopped calling rcaRenderResults; the JS/Py "
        "rendering contract needs to be re-documented"
    )


def test_table_js_exports_the_symbols_the_desktop_calls() -> None:
    src = (ROOT / "js" / "table.js").read_text(encoding="utf-8")
    for symbol in REQUIRED_SYMBOLS:
        assert re.search(rf"\b(?:function|const|var|let)\s+{symbol}\b", src), (
            f"js/table.js no longer defines {symbol}, which the desktop "
            "history detail page calls after inlining this file"
        )


def test_index_html_script_tags_resolve() -> None:
    """Browser-side equivalent: a renamed script must not become a silent 404."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
    assert srcs, "no script tags found in index.html - parser went stale"
    missing = []
    for src in srcs:
        if src.startswith(("http://", "https://", "//")):
            continue
        # Strip a cache-busting query string if one is ever added.
        rel = src.split("?", 1)[0].lstrip("/")
        if not (ROOT / rel).is_file():
            missing.append(src)
    assert not missing, "index.html references missing scripts: " + ", ".join(missing)
