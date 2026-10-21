"""The Python-side dead weight, and the evidence for it.

The Qt family was removed by a dependency rule (``qt_payload.py``): a PE
stays if something in the bundle imports it. This is the same question
one level up -- for the PYTHON packages and the data trees -- and it
carries the same trap: "nothing imports it" has to mean "nothing
OUTSIDE its own subpackage imports it", or the rule drops something
that is genuinely reachable.

Every entry below was checked against the import graph of a real build
(PyInstaller's own ``xref-*.html``) AND against the first-party source:

  * no first-party module imports any of these packages -- ``grep`` over
    the whole tree finds only ``references/``, which is not shipped;
  * the only importers are the packages that are themselves dropped.

That is why ``bs4`` is here even though ``lxml.html.soupparser`` imports
it: ``lxml.html`` is dropped too, and ``lxml`` itself stays because
``openpyxl`` uses ``lxml.etree``.

What is deliberately NOT here:

  * ``lxml`` itself -- ``openpyxl.xml`` imports ``lxml.etree``. Dropping
    it would not break the export, it would make it slower, which is a
    different kind of regression and not one to trade for 2.6 MB.
  * ``matplotlib``'s fonts -- DejaVu is matplotlib's default face.
  * ``PIL._imagingft`` -- Pillow's font rendering.
  * ``libcrypto-1_1-x64.dll`` -- it looks like pollution (it was picked
    up from ``C:\\Program Files\\HONOR\\PCManager`` because the Windows
    DLL search path found it there), but ``sqlite3.dll`` statically
    imports it, so removing it would stop SQLite from loading at all.
"""
from __future__ import annotations

import re

# Python packages to keep out of the bundle. The value is the evidence.
DEAD_PY_PACKAGES = {
    "requests": "not in the import graph at all",
    "urllib3": "not in the import graph at all",
    "idna": "not in the import graph at all",
    "bs4": "only imported by lxml.html.soupparser, which is dropped too",
    "soupsieve": "only imported by bs4",
    "html5lib": "only imported by bs4.builder._html5lib and lxml.html.html5parser",
    "chardet": "only imported by bs4.dammit",
    # charset_normalizer is NOT here: numpy.f2py.crackfortran imports it,
    # numpy is a hard dependency, and it is worth 0.01 MB. The rule was
    # caught by the test that checks these claims against a real build's
    # import graph, which is the only reason it is not in the bundle.
}

# Subpackages of a package that stays.
DEAD_PY_SUBPACKAGES = (
    "lxml.html",
    "lxml.html.soupparser",
    "lxml.html.html5parser",
)

# Data trees dropped whole.
DEAD_DATA_PREFIXES = (
    "matplotlib/mpl-data/sample_data/",   # example data, never rendered
)

# Binaries dropped whole. The Pillow AVIF codec is the big one: Pillow
# registers its format plugins in a try/except loop, so a missing
# optional codec costs the AVIF format and nothing else.
DEAD_BINARY_PREFIXES = (
    "pil/_avif",
)


def _norm(dest: str) -> str:
    return dest.replace("\\", "/")


def excludes() -> list[str]:
    """The module names to hand to PyInstaller's ``excludes``."""
    return sorted(DEAD_PY_PACKAGES) + sorted(DEAD_PY_SUBPACKAGES)


def analyse(binaries, datas):
    """Which collected binaries and data files may go.

    ``binaries``/``datas`` are lists of (dest, src, typecode) tuples.
    """
    drop_bin, drop_data = [], []
    for dest, src, tc in binaries:
        low = _norm(dest).lower()
        if any(low.startswith(p) for p in DEAD_BINARY_PREFIXES):
            drop_bin.append((dest, src, tc))
    for dest, src, tc in datas:
        low = _norm(dest).lower()
        if any(low.startswith(p) for p in DEAD_DATA_PREFIXES):
            drop_data.append((dest, src, tc))
    return drop_bin, drop_data


def read_xref_importers(xref_path, packages=None):
    """From a PyInstaller xref: who imports each of ``packages``?

    Returns {package: sorted(importer names)}. Used to CHECK the rules
    above against a real build instead of trusting this file.
    """
    import html
    import pathlib

    path = pathlib.Path(xref_path)
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r'<a name="([^"]+)"></a>', raw)
    graph = {}
    for i in range(1, len(parts) - 1, 2):
        graph[html.unescape(parts[i])] = [
            html.unescape(m) for m in re.findall(r'href="#([^"]+)"', parts[i + 1])]
    wanted = set(packages) if packages else set(DEAD_PY_PACKAGES)
    out = {p: set() for p in wanted}
    for mod, imps in graph.items():
        for i in imps:
            if i in out:
                out[i].add(mod)
    return {k: sorted(v) for k, v in out.items()}
