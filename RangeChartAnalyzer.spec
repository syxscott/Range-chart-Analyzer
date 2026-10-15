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

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --- windowed build: the shipped artifact -------------------------------
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
    upx=False,
    upx_exclude=[],
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/logo.ico",
)
