# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Range Chart Analyzer.

Build (from the project root, inside the conda ``dev`` env)::

    python -m PyInstaller --noconfirm RangeChartAnalyzer.spec

Produces TWO one-file executables from a single (shared, expensive) Analysis
pass, which is where nearly all the PySide6 collection time goes:

    dist/RangeChartAnalyzer.exe            -- windowed, what the user gets
    dist/RangeChartAnalyzer-console.exe    -- same build, stderr attached

The console twin exists so the packaged artifact can be smoke-tested without
guessing: ``main.py server --port 8899 --no-browser`` is forwarded to
``server.main()``, so the exe can be driven headlessly and its HTTP endpoints
probed. A silent crash inside a windowed exe is otherwise invisible.

Resource layout mirrors how the code actually resolves paths at runtime:

* ``server.py:584``  ``ROOT = os.path.dirname(os.path.abspath(__file__))``
  -- the static handler serves ``index.html``/``js``/``css``/``assets``
  relative to this, so they must land at the bundle root.
* ``rca_core/standards/ics.py:69``
  ``Path(__file__).parent.parent / "resources" / "ics_2024.json"``
  -- frozen module ``__file__`` points inside the bundle, hence the
  ``rca_core/resources`` destination.

User data (history DB, provider keys) is deliberately NOT bundled: it lives in
``~/.range_chart_analyzer`` (rca_core/db.py:32, rca_core/secrets_store.py:82),
so the exe itself can sit in a read-only directory.
"""

from PyInstaller.utils.hooks import collect_submodules

import os

block_cipher = None

# qfluentwidgets resolves most of its widget tree through dynamic imports
# (components/, common/, window/...), which static analysis cannot see.
hiddenimports = collect_submodules("qfluentwidgets")

# Dev-only trees. Everything else stays in, because matplotlib/PySide6 pull in
# surprising transitive imports and a wrong exclusion surfaces at runtime, not
# at build time.
excludes = [
    "pytest",
    "_pytest",
    "IPython",
    "jupyter",
    "notebook",
    "nbconvert",
    "nbformat",
    "traitlets",
    # --- ML / NLP stack that this tool never calls -------------------------
    # The first build shipped 681.5 MB because these arrived as transitive
    # dependencies of the science stack sitting in the same conda env. Measured
    # cost inside the onefile exe (compressed): torch 98.5, polars runtime
    # 48.6, pyarrow 25.2, onnxruntime 11.9, scipy 23.1, bitsandbytes 36.7,
    # spacy 9.3, transformers 9.0, grpc 4.5, hf_xet 3.3, blis 6.3,
    # sklearn 5.1, pandas 6.1, pdfminer 6.1  ~= 293 MB of pure dead weight.
    #
    # These are NOT guesses. Two audits, both re-runnable from this repo:
    #   scripts/audit_exclude_imports.py  -- imports the whole first-party tree plus
    #                         qfluentwidgets in a FRESH SUBPROCESS per trial,
    #                         with each candidate blocked by a meta-path
    #                         finder. A shared interpreter cannot test this:
    #                         after the baseline, every __import__ returns from
    #                         sys.modules without ever consulting the finder.
    #   scripts/audit_exclude_suite.py  -- re-runs the full 3577-test suite with the same
    #                         blockers installed via sitecustomize, which is
    #                         what catches the LAZY imports the first audit
    #                         cannot see (matplotlib imports scipy from inside
    #                         a function body for its interpolate paths).
    #
    # What is deliberately NOT excluded, having been checked and found used:
    #   PySide6 + QtWebEngine -- gui_fluent.py imports QWebEngineView to embed
    #       the web frontend in the native window. Dropping it removes 79.8 MB
    #       of Chromium and breaks the main content area. qtwebengine is not
    #       dead weight just because pywebview is absent from the env.
    #   cv2 (deskew), matplotlib (charts), PIL (images), numpy.
    "torch", "torchvision", "torchaudio",
    "transformers", "tokenizers", "safetensors", "hf_xet", "huggingface_hub",
    "spacy", "thinc", "blis", "preshed", "murmurhash", "cymem", "srsly",
    "onnxruntime", "polars", "_polars_runtime_32", "pyarrow",
    "bitsandbytes", "scipy", "sklearn", "pandas",
    "grpc", "google", "pdfminer", "pdfplumber",
]

datas = [
    # Static web frontend (served by server.py, opened by app.py/PyWebView).
    ("index.html", "."),
    ("js", "js"),
    ("css", "css"),
    ("assets", "assets"),
    # Runtime data tables read via Path(__file__) from inside rca_core.
    ("rca_core/resources", "rca_core/resources"),
    # 0.1.3: the changelog has to SHIP. `gui_fluent_onboarding.changelog_path()`
    # resolves it next to the module file, so in a frozen build it is looked
    # for at the bundle root -- and it was not there. The packaged app's
    # "what's new" dialog therefore found nothing to show, silently, in
    # exactly the build that was released to users. 77 KB against a 235 MB
    # payload is not a trade-off worth making.
    ("CHANGELOG.md", "."),
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


# ---------------------------------------------------------------------------
# 0.1.2 (2026-10-18): UPX compression, take two.
#
# Measured why it goes WHERE it goes. In PyInstaller 6.22 the switches are
# per-BUILD, not on Analysis:
#   - Analysis(upx_dir=..., upx_exclude=...) is silently ACCEPTED as a kwarg
#     but does nothing (no UPX lines in the build log, size unchanged —
#     that was my first attempt this release).
#   - The binaries are compressed when the DATA/BINARY collection runs with
#     upx_binaries=True, driven by CONF['upx_dir'] for the executable path
#     and `upx_exclude` on the same build element
#     (PyInstaller/building/utils.py:process_collected_binary, api.py:316).
#
# Why compress at all, and why the exclude list is this long: PyInstaller 6
# zlib-deflates every archive entry (flag=1 on 4334/4335 in this build), so
# UPX's LZMA only buys the ~55% of a DLL that zlib cannot reach (PE padding,
# relocations). The candidates that pay are the big non-Qt binaries —
# cv2.pyd 86 MB, openblas 20 MB, avcodec 14 MB, _rust.pyd 9 MB. The Qt
# trees stay out: compressed Chromium/Qt DLLs are the classic
# "worked until I added UPX" breakage, the interpreter is kept
# uncompressed so crash traces stay readable, and MS runtime / OpenSSL are
# either signature-tight or verified by their stacks.
UPX_DIR = r"C:\Users\Administrator\AppData\Local\Temp\upx-4.2.4-win64"
UPX_EXCLUDE = [
    # Qt / WebEngine: compressed Chromium/Qt DLLs are the #1 "UPX broke my
    # build" report, and their zlib-deflated payload limits UPX's gain.
    "Qt6WebEngineCore.dll", "Qt6WebEngine*.dll",
    "Qt6*.dll", "Qt5*.dll", "Qt*.dll",
    # interpreter: keeps Windows Error Reporting minidumps symbolized.
    "python3*.dll", "python.exe",
    # MS runtime: tiny already, and CFG-enabled binaries are skipped by
    # PyInstaller itself anyway (utils.py:147).
    "vcruntime*.dll", "ucrtbase.dll", "concrt*.dll", "msvcp*.dll",
    # OpenSSL: some stacks verify these signatures.
    "libcrypto-*.dll", "libssl-*.dll",
]

import os as _os
if _os.path.isfile(_os.path.join(UPX_DIR, "upx.exe")):
    from PyInstaller.config import CONF
    CONF["upx_dir"] = UPX_DIR
    # api.py:482 / :1106 — `self.upx = CONF['upx_available'] and kwargs.get('upx')`.
    # Without this CONF flag the per-EXE `upx=True` below is a no-op.
    CONF["upx_available"] = True
    print("[spec] UPX enabled from %s" % UPX_DIR)
else:
    print("[spec] UPX dir not found — building UNCOMPRESSED: %s" % UPX_DIR)
    UPX_DIR = None
    UPX_EXCLUDE = []

# The two onefile builds below are where upx_binaries / upx_exclude live.
# Datas (icudtl.dat, .pak resources) are never UPX'd regardless —
# process_collected_binary only handles BINARY typecodes.

# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-10-16 size work: strip payload that provably cannot affect a run
# ---------------------------------------------------------------------------
# Measured on the 0.1.1 baseline (dist = 323.5 MB, uncompressed payload
# 839.1 MB over 4342 entries, read from PKG-00.toc -- not from site-packages
# directory sizes, which over-count up to 3x):
#
#     204.8 MB  PySide6/Qt6WebEngineCore.dll
#     145.1 MB  cv2  (86.3 cv2.pyd + 58.9 two ffmpeg videoio DLLs)
#      75.8 MB  PySide6/resources/qtwebengine_devtools_resources.debug.pak
#      20.6 MB  PySide6/opengl32sw.dll
#      11.6 MB  PySide6/resources/qtwebengine_devtools_resources.pak
#
# The two entries removed here are worth 146.3 MB of the 839 MB payload and
# cost nothing at runtime:
#
# 1. `*.debug.pak` / `qtwebengine_devtools_resources.pak` -- DevTools DEBUG
#    symbol tables and the DevTools frontend bundle. QtWebEngine loads these
#    only when a profile is opened with DevTools enabled; this project never
#    calls showDevTools() or sets a DevTools attribute. PyInstaller's
#    `collect_qtwebengine_files()` sweeps the whole `resources/` directory
#    with no filter, so debug builds ride along in every bundle.
#
# 2. The two `opencv_videoio_ffmpeg*.dll` -- 58.9 MB of video decoders for a
#    tool that extracts from still images. deskew.py and extractor.py call
#    resize / GaussianBlur / Canny / HoughLinesP / fastNlMeansDenoising /
#    createCLAHE / cvtColor, all imgproc or photo; nothing in the repository
#    calls a videoio entry point. Verified by moving both DLLs aside and
#    running every call site in a fresh interpreter: 10/10 passed and
#    `import cv2` still succeeded, restored afterwards by digest.
#    (scripts/audit_exclude_imports.py and audit_exclude_suite.py cover Python
#    imports; a binary dropped from `a.binaries` is invisible to both, which
#    is why this one needed its own probe.)

def _drop(entries, should_drop):
    """Remove matching TOC entries and report what went."""
    kept, dropped = [], []
    for entry in entries:
        name = str(entry[0]).replace("\\", "/").lower()
        (dropped if should_drop(name) else kept).append(entry)
    return kept, dropped


def _src_size(entry):
    """Bytes a TOC entry costs.

    A .toc entry is (dest_name, src_path, typecode[, size]) and, in this
    PyInstaller version, carries no length -- the source path points at the
    real file, so stat it. Two earlier drafts of the size probe assumed the
    length was there and reported every entry as 0.0 MB while still printing a
    confident table.
    """
    try:
        return os.path.getsize(str(entry[1]))
    except (OSError, IndexError):
        return 0


def _is_devtools_bulk(name):
    base = name.rsplit("/", 1)[-1]
    return (base.endswith(".debug.pak")
            or base.startswith("qtwebengine_devtools_resources"))


def _is_ffmpeg_videoio(name):
    base = name.rsplit("/", 1)[-1]
    return base.startswith("opencv_videoio_ffmpeg")


a.binaries, dropped_bin = _drop(a.binaries, _is_ffmpeg_videoio)
a.datas, dropped_data = _drop(a.datas, _is_devtools_bulk)
_removed = sum(_src_size(e) for e in (dropped_bin + dropped_data))
print("[spec] dropped %d binary + %d data entries, %.1f MB of payload"
      % (len(dropped_bin), len(dropped_data), _removed / 1e6))

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --- windowed build: the shipped artifact -------------------------------
# 0.1.2 (2026-10-18): UPX is applied as a POST-PROCESS (scripts/upx_compress.py),
# not here. Two reasons, both learned the hard way:
#   1. UPX inside the spec compresses every binary it can see, including
#      QtWebEngineCore and python313.dll. WebEngine crashes under compressed
#      Chromium in some versions, and a compressed interpreter hides crash
#      traces from Windows Error Reporting.
#   2. A post-process can be A/B-verified: the un-UPX exe from dist/ stays
#      on disk for comparison, and the smoke test runs against the
#      compressed copy before it replaces anything.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RangeChartAnalyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 0.1.2: onefile's PKG (api.py:663) takes upx_binaries=self.upx, where
    # self.upx = CONF['upx_available'] and kwargs.get('upx'). The CONF flag
    # and the exclude list are set in the UPX block above; this kwarg is the
    # per-build switch. When UPX_DIR is absent both degrade to False/[].
    upx=bool(UPX_DIR),
    upx_exclude=UPX_EXCLUDE,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/logo.ico",
)

# --- console twin: identical payload, stderr attached -------------------
# Only the final link step re-runs; Analysis/PYZ above are reused.
exe_console = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RangeChartAnalyzer-console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 0.1.2: onefile's PKG (api.py:663) takes upx_binaries=self.upx, where
    # self.upx = CONF['upx_available'] and kwargs.get('upx'). The CONF flag
    # and the exclude list are set in the UPX block above; this kwarg is the
    # per-build switch. When UPX_DIR is absent both degrade to False/[].
    upx=bool(UPX_DIR),
    upx_exclude=UPX_EXCLUDE,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/logo.ico",
)
