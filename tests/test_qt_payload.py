"""The Qt payload decision must be verifiable, not asserted.

Two properties matter and are checked here:

  1. The decision removes the ballast -- the QML tree, the Qt3D/Charts/
     Pdf family, the ffmpeg backends.
  2. The decision is CLOSED: after the removal, every PE that survives has
     every DLL it imports still present. A drop that breaks that closure
     is a crash waiting for the first user of that feature.

CI does not install PySide6 on purpose (the GUI suites run in a separate
job), so these tests skip when the Qt installation is not there.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qt_payload", REPO / "scripts" / "qt_payload.py")
qt_payload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qt_payload)


def _pyside6_dir():
    try:
        import PySide6
    except Exception:
        return None
    return pathlib.Path(PySide6.__file__).parent


PYSIDE6 = _pyside6_dir()
pytestmark = pytest.mark.skipif(
    PYSIDE6 is None or not (PYSIDE6 / "Qt6Core.dll").exists(),
    reason="PySide6 is not installed (CI runs the Qt suites in a separate job)")


def test_pe_imports_reads_a_real_import_table():
    """The PE parser must actually parse, not silently return []."""
    imps = qt_payload.pe_imports(PYSIDE6 / "Qt6Quick.dll")
    assert imps, "no imports parsed out of a real Qt DLL"
    low = [i.lower() for i in imps]
    assert "qt6core.dll" in low
    assert "qt6qml.dll" in low


def test_pe_imports_survives_a_non_pe_file():
    assert qt_payload.pe_imports(PYSIDE6 / "does-not-exist.dll") == []
    junk = REPO / "pytest.ini"
    assert qt_payload.pe_imports(junk) == []


def test_a_binding_imports_its_own_qt_dll():
    """The binding's import table is what roots the closure.

    Note the naming: the BINDING is ``QtWebEngineWidgets.pyd`` and the
    DLL it loads is ``Qt6WebEngineWidgets.dll`` -- the "6" belongs to the
    DLL only. Getting that backwards makes the root set empty, and an
    empty root set silently drops everything.
    """
    imps = qt_payload.pe_imports(PYSIDE6 / "QtWebEngineWidgets.pyd")
    assert imps, "the binding's import table came back empty"
    low = [i.lower() for i in imps]
    assert "qt6webenginewidgets.dll" in low
    assert "qt6core.dll" in low
    assert not (PYSIDE6 / "Qt6WebEngineWidgets.pyd").exists(), (
        "there is no Qt6-prefixed binding; the 6 belongs to the DLL")


def test_the_qml_tree_and_the_dead_families_are_dropped():
    """The ballast the measurement found must actually be removed."""
    binaries = [
        ("PySide6/Qt6Core.dll", str(PYSIDE6 / "Qt6Core.dll"), "b"),
        ("PySide6/QtWebEngineWidgets.pyd",
         str(PYSIDE6 / "QtWebEngineWidgets.pyd"), "b"),
        ("PySide6/Qt6Quick3DRuntimeRender.dll",
         str(PYSIDE6 / "Qt6Quick3DRuntimeRender.dll"), "b"),
        ("PySide6/qml/QtQuick/Controls/Basic/x.dll", "x", "b"),
        ("PySide6/plugins/designer/qaxwidget.dll", "x", "b"),
        ("PySide6/plugins/imageformats/qpdf.dll", "x", "b"),
        ("PySide6/plugins/imageformats/qjpeg.dll", "x", "b"),
    ]
    datas = [("PySide6/qml/QtQuick/qmldir", "x", "d")]

    drop_bin, drop_data, report = qt_payload.analyse(binaries, datas)

    dropped = {d.lower() for d in report["dropped_binaries"]}
    assert "pyside6/qt6quick3druntimerender.dll" in dropped
    assert "pyside6/qml/qtquick/controls/basic/x.dll" in dropped
    assert "pyside6/plugins/designer/qaxwidget.dll" in dropped
    assert "pyside6/plugins/imageformats/qpdf.dll" in dropped
    # the platform plugin and the JPEG reader stay
    assert "pyside6/plugins/imageformats/qjpeg.dll" not in dropped
    assert "pyside6/qt6core.dll" not in dropped
    assert any(d.lower().startswith("pyside6/qml/") for d in report["dropped_datas"])


def test_a_dll_something_imports_is_never_dropped():
    """The closure property, on a case with a real edge."""
    binaries = [
        ("PySide6/QtQuickWidgets.pyd", str(PYSIDE6 / "QtQuickWidgets.pyd"), "b"),
        ("PySide6/Qt6Quick.dll", str(PYSIDE6 / "Qt6Quick.dll"), "b"),
        ("PySide6/Qt6Qml.dll", str(PYSIDE6 / "Qt6Qml.dll"), "b"),
        ("PySide6/Qt6Core.dll", str(PYSIDE6 / "Qt6Core.dll"), "b"),
    ]
    drop_bin, _drop_data, report = qt_payload.analyse(binaries, [])
    dropped = {d.lower() for d in report["dropped_binaries"]}
    # Qt6QuickWidgets.pyd imports Qt6Quick.dll, which imports Qt6Qml.dll,
    # which imports Qt6Core.dll -- all three must survive.
    assert "pyside6/qt6quick.dll" not in dropped
    assert "pyside6/qt6qml.dll" not in dropped
    assert "pyside6/qt6core.dll" not in dropped


def test_the_decision_leaves_the_kept_closure_closed():
    """Every surviving PE must have all of its imports still present.

    This is the property that makes the whole exercise safe: a drop set
    that is not closed cannot be shipped, and this test is what says so
    before a build rather than after a user's crash.
    """
    site = PYSIDE6
    binaries = []
    for p in sorted(site.glob("*.pyd")):
        binaries.append((f"PySide6/{p.name}", str(p), "b"))
    for p in sorted((site / "plugins").rglob("*.dll")):
        rel = p.relative_to(site).as_posix()
        binaries.append((f"PySide6/{rel}", str(p), "b"))
    for p in sorted(site.glob("*.dll")):
        binaries.append((f"PySide6/{p.name}", str(p), "b"))

    drop_bin, _drop_data, report = qt_payload.analyse(binaries, [])
    dropped = set(report["dropped_binaries"])

    surviving = {d.lower() for d, _s, _t in binaries} - dropped
    present = {d.rsplit("/", 1)[-1] for d in surviving}

    missing = {}
    for dest, src, _t in binaries:
        if dest.lower() in dropped:
            continue
        for dep in qt_payload.pe_imports(pathlib.Path(src)):
            base = dep.rsplit("\\", 1)[-1].lower()
            if base.startswith("qt6") and base not in present:
                missing.setdefault(dest, []).append(base)

    assert not missing, (
        "the drop set is not closed -- these survivors import DLLs that "
        f"would be gone: {missing}"
    )
