"""Decide which Qt payload the onefile build can leave out.

THE PROBLEM
-----------
PyInstaller's PySide6 hook calls ``pyside6_library_info.collect_extra_binaries()``,
which sweeps the whole Qt installation into the bundle -- every Qt3D,
Charts, Pdf, Location and Graphs DLL, plus the entire ``qml/`` tree --
whether or not anything imports it. Measured on the 0.1.3 build: 137
top-level PySide6 DLLs, of which 20 bindings were actually collected and
the rest were ballast. The QML tree alone was 2510 archive entries.

THE RULE
--------
A Qt6*.dll must stay if the Windows loader could be asked to load it:

  * something imports it in its PE import table (a static edge), or
  * it is in the ``plugins/`` directory Qt loads by name at runtime.

The first set is computed from the PE import tables. The second is a
directory rule, because a plugin has no import edge -- Qt finds it by
scanning ``plugins/`` and asking the platform. That is exactly why
"nobody imports it" is not a safe answer on its own: ``opengl32sw.dll``
has no importer either, and is still loaded by name on machines whose
system OpenGL is missing.

So the rule is deliberately conservative in the one place it has to be:
``plugins/`` families are only dropped when the family is for a feature
this app does not have, and the QML tree goes as a whole with the
Quick* DLLs that only it needs.

WHAT THIS DOES NOT PROVE
------------------------
That the app runs. It proves the dependency graph is closed, nothing
else. A build that drops something Qt loads by a name nobody wrote down
still starts and still fails later, at the first user of that feature.
The smoke test after the build is the other half of the proof.
"""
from __future__ import annotations

import struct
from pathlib import Path

# --- plugin families this app has no use for ------------------------------
# Each one is dropped WHOLE (directory), because Qt scans plugins/ by
# family and a half-dropped family is worse than a missing one.
DEAD_PLUGIN_FAMILIES = (
    "assetimporters",   # Qt3D / glTF importers
    "designer",         # Qt Designer (UI authoring)
    "geoservices",      # Qt Location map providers
    "multimedia",      # ffmpeg/Windows Media playback backends
    "qmltooling",      # the QML debugging/inspection tooling
    "scenes3d",        # glTF/FBX scene import
    "webview",         # the QML WebEngine (we use the QWidget one)
)

# Single plugin files dropped even though their family stays.
DEAD_PLUGIN_FILES = (
    # Qt's PDF image-format plugin: it needs Qt6Pdf.dll, and the app has
    # no PDF reader (exports are CSV/TSV/JSON/XLSX/Newick/WPD).
    "imageformats/qpdf.dll",
)

# Data trees dropped whole. Lower-case: the comparison is case-folded.
DEAD_DATA_PREFIXES = (
    "pyside6/qml/",            # the QML module tree
    "pyside6/plugins/multimedia/",
    "pyside6/plugins/designer/",
    "pyside6/plugins/webview/",
    "pyside6/plugins/geoservices/",
    "pyside6/plugins/assetimporters/",
    "pyside6/plugins/qmltooling/",
    "pyside6/plugins/scenes3d/",
)

# Top-level DLLs dropped even though no import table points at them.
# opengl32sw.dll is Qt's software-OpenGL fallback, loaded BY NAME when the
# system OpenGL is missing or too old -- so it has no import edge and the
# closure cannot see it. The app has no QOpenGLWidget and no QQuickWidget
# (the two things that use it), and QWebEngine ships its own software
# rasteriser (SwiftShader), so it is dead weight. The machines this could
# break are the ones with no usable system OpenGL at all, which cannot
# run a Chromium view either.
DEAD_BY_NAME = (
    "opengl32sw.dll",
)


# --- PE import table --------------------------------------------------------
def pe_imports_bytes(data: bytes) -> list[str]:
    """The DLL names a PE image imports. Empty list if unreadable."""
    if data[:2] != b"MZ":
        return []
    try:
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe_off:pe_off + 4] != b"PE\x00\x00":
            return []
        coff = pe_off + 4
        n_sections = struct.unpack_from("<H", data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        opt = coff + 20
        magic = struct.unpack_from("<H", data, opt)[0]
        dd_off = opt + (112 if magic == 0x20B else 96)
        imp_rva, _ = struct.unpack_from("<II", data, dd_off + 8)
        if not imp_rva:
            return []
        sec_off = opt + opt_size
        sections = []
        for i in range(n_sections):
            s = sec_off + i * 40
            vaddr, vsize = struct.unpack_from("<II", data, s + 12)
            praw, psize = struct.unpack_from("<II", data, s + 20)
            sections.append((vaddr, vsize, praw, psize))

        def to_off(rva):
            for vaddr, vsize, praw, psize in sections:
                if vaddr <= rva < vaddr + max(vsize, psize):
                    return praw + (rva - vaddr)
            return None

        off = to_off(imp_rva)
        if off is None:
            return []
        names = []
        for _ in range(4096):
            entry = struct.unpack_from("<IIIII", data, off)
            if entry == (0, 0, 0, 0, 0):
                break
            noff = to_off(entry[3])
            if noff is None:
                break
            end = data.index(b"\x00", noff)
            names.append(data[noff:end].decode("ascii", "replace"))
            off += 20
        return names
    except Exception:
        return []


def pe_imports(path: Path) -> list[str]:
    """The DLL names a PE file imports. Empty list if unreadable."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    return pe_imports_bytes(data)


def _norm(dest: str) -> str:
    return dest.replace("\\", "/")


def _is_dead_plugin_file(dest: str) -> bool:
    low = _norm(dest).lower()
    if low.startswith("pyside6/qml/"):
        # the QML module tree: the app is a QWidget application and its
        # only QML-adjacent module (qfluentwidgets.multimedia) is dropped
        return True
    if not low.startswith("pyside6/plugins/"):
        return False
    rel = low[len("pyside6/plugins/"):]
    family = rel.split("/", 1)[0]
    if family in DEAD_PLUGIN_FAMILIES:
        return True
    return rel in DEAD_PLUGIN_FILES


def _is_dead_data(dest: str) -> bool:
    low = _norm(dest).lower()
    return any(low.startswith(p) for p in DEAD_DATA_PREFIXES)


def analyse(binaries, datas):
    """Decide what to drop.

    ``binaries``/``datas`` are lists of (dest, src, typecode) tuples, the
    same shape PyInstaller keeps in ``a.binaries`` / ``a.datas``.

    Returns (drop_binaries, drop_datas, report) where the first two are
    lists of the entries to remove and ``report`` is a dict for printing.
    """
    binaries = [(str(d), str(s), t) for d, s, t in binaries]
    datas = [(str(d), str(s), t) for d, s, t in datas]

    # ---- roots: the PySide6 bindings that were actually collected --------
    # A binding is in the bundle only if something imported it, so the
    # collected set IS the set of bindings the app can reach.
    roots = [(d, s) for d, s, _ in binaries
             if _norm(d).lower().startswith("pyside6/") and d.lower().endswith(".pyd")]

    # ---- plugins we keep: every family except the dead ones -------------
    kept_plugins = [(d, s) for d, s, _ in binaries
                    if _norm(d).lower().startswith("pyside6/plugins/")
                    and not _is_dead_plugin_file(d)]

    # ---- closure over the import tables ----------------------------------
    # The universe is the whole Qt installation, NOT just the files that
    # happened to be collected. If a dependency is missing from the
    # collected set the edge would be invisible, the closure would come
    # out too small, and the drop list would take a DLL that something
    # still needs. Being conservative here is free: a DLL that is not in
    # the bundle cannot be dropped by this rule anyway.
    universe = {}
    for d, s, _ in binaries:
        base = _norm(d).rsplit("/", 1)[-1].lower()
        universe.setdefault(base, s)
    if roots:
        site_dir = Path(roots[0][1]).parent
        for p in site_dir.glob("*.dll"):
            universe.setdefault(p.name.lower(), str(p))

    closure = set()
    queue = list(roots) + list(kept_plugins)
    seen = set()
    while queue:
        dest, src = queue.pop()
        if dest in seen:
            continue
        seen.add(dest)
        for dep in pe_imports(Path(src)):
            base = dep.rsplit("\\", 1)[-1].lower()
            if base in universe:
                closure.add(base)
                # AND expand it: PySide6's bindings do not import their own
                # Qt DLL directly (Qt6QuickWidgets.pyd imports
                # Qt6QuickWidgets.dll, which imports Qt6Quick.dll), so a
                # closure that does not recurse would drop DLLs that are
                # two edges away. It would look fine and ship a broken app.
                queue.append((base, universe[base]))
            # a DLL sitting next to the importer is resolved by name too
            sibling = Path(src).parent / dep.rsplit("\\", 1)[-1]
            if sibling.exists() and sibling.suffix.lower() == ".dll":
                queue.append((str(sibling), str(sibling)))

    # ---- decide ----------------------------------------------------------
    drop_bin, drop_data = [], []
    for dest, src, tc in binaries:
        low = _norm(dest).lower()
        base = low.rsplit("/", 1)[-1]
        if _is_dead_plugin_file(dest):
            drop_bin.append((dest, src, tc))
            continue
        if low.startswith("pyside6/") and "/" not in low[8:] and low.endswith(".dll"):
            if base in DEAD_BY_NAME:
                drop_bin.append((dest, src, tc))
                continue
            if base not in closure:
                drop_bin.append((dest, src, tc))
    for dest, src, tc in datas:
        if _is_dead_data(dest):
            drop_data.append((dest, src, tc))

    report = {
        "roots": sorted(d for d, _ in roots),
        "closure": sorted(closure),
        "dropped_binaries": [d for d, _, _ in drop_bin],
        "dropped_datas": [d for d, _, _ in drop_data],
    }
    return drop_bin, drop_data, report
