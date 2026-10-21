"""The Python-side exclusion rules must be checkable, not trusted.

The rules in scripts/payload_policy.py are claims about an import
graph. A claim that cannot be checked is a comment. The interesting
test here therefore uses a REAL build's cross-reference -- PyInstaller's
own ``xref-*.html`` -- and asks whether each package really is
unreachable from outside the set being dropped.

That file only exists after a build, so the test skips when it is not
there (CI does not build a onefile exe).
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    # Literal path chain, not an f-string: the contract test in
    # tests/test_standalone_loadable_modules.py reconstructs the target of a
    # spec_from_file_location call from its string constants, and an
    # f-string hides the file name from it.
    path = {
        "payload_policy": REPO / "scripts" / "payload_policy.py",
        "qt_payload": REPO / "scripts" / "qt_payload.py",
    }[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


policy = _load("payload_policy")

XREF = REPO / "build" / "RangeChartAnalyzer" / "xref-RangeChartAnalyzer.html"


def test_the_exclude_list_is_the_union_of_the_rules():
    ex = policy.excludes()
    assert "requests" in ex
    assert "bs4" in ex
    assert "lxml.html" in ex
    # lxml itself must stay: openpyxl uses lxml.etree
    assert "lxml" not in ex
    assert "lxml.etree" not in ex


def test_analyse_drops_the_avif_codec_and_the_sample_data():
    binaries = [
        ("PIL/_avif.cp310-win_amd64.pyd", "x", "b"),
        ("PIL/_imaging.cp310-win_amd64.pyd", "x", "b"),
        ("matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf", "x", "b"),
    ]
    datas = [
        ("matplotlib/mpl-data/sample_data/misc/4.png", "x", "d"),
        ("matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf", "x", "d"),
    ]
    drop_bin, drop_data = policy.analyse(binaries, datas)
    dropped_bin = {d.lower() for d, _s, _t in drop_bin}
    dropped_data = {d.lower() for d, _s, _t in drop_data}
    assert "pil/_avif.cp310-win_amd64.pyd" in dropped_bin
    assert "pil/_imaging.cp310-win_amd64.pyd" not in dropped_bin
    assert "matplotlib/mpl-data/fonts/ttf/dejavusans.ttf" not in dropped_bin
    assert "matplotlib/mpl-data/sample_data/misc/4.png" in dropped_data
    assert "matplotlib/mpl-data/fonts/ttf/dejavusans.ttf" not in dropped_data


@pytest.mark.skipif(not XREF.exists(),
                    reason="needs a real build's xref (CI does not build onefile)")
def test_every_excluded_package_is_unreachable_in_a_real_build():
    """The rules must match what the import graph actually says.

    A package may be imported by ITSELF and by the other packages being
    dropped, and by the standard library -- nothing else. A first-party
    importer would mean the rule is wrong.
    """
    importers = policy.read_xref_importers(XREF)
    allowed = set(policy.DEAD_PY_PACKAGES) | set(policy.DEAD_PY_SUBPACKAGES)
    # The standard library is imported by everything, including the
    # packages being dropped; it says nothing about reachability.
    stdlib = {
        "__future__", "abc", "base64", "bisect", "bz2", "codecs", "collections",
        "collections.abc", "contextlib", "copy", "csv", "datetime", "decimal",
        "difflib", "email", "enum", "errno", "fnmatch", "functools", "gc",
        "genericpath", "gettext", "glob", "gzip", "hashlib", "heapq", "html",
        "http", "importlib", "inspect", "io", "itertools", "json", "keyword",
        "linecache", "locale", "logging", "lzma", "math", "mimetypes", "ntpath",
        "numbers", "operator", "os", "pathlib", "pickle", "pkgutil", "platform",
        "posixpath", "pprint", "queue", "random", "re", "reprlib", "secrets",
        "select", "shlex", "shutil", "signal", "site", "socket", "sre_compile",
        "sre_constants", "sre_parse", "ssl", "stat", "string", "struct",
        "subprocess", "sys", "sysconfig", "tarfile", "tempfile", "textwrap",
        "threading", "time", "timeit", "token", "tokenize", "traceback",
        "types", "typing", "unicodedata", "urllib", "uuid", "warnings", "weakref",
        "xml", "zipfile", "zlib",
    }
    for pkg, users in importers.items():
        if pkg not in policy.DEAD_PY_PACKAGES:
            continue
        outside = [u for u in users
                   if not (u == pkg or u.startswith(pkg + ".")
                           or u.split(".")[0] in allowed
                           or any(u == sp or u.startswith(sp + ".")
                                  for sp in policy.DEAD_PY_SUBPACKAGES)
                           or u.split(".")[0] in stdlib)]
        assert not outside, (
            f"{pkg} is imported by {outside} -- the rule to drop it is wrong"
        )


@pytest.mark.skipif(not XREF.exists(),
                    reason="needs a real build's xref (CI does not build onefile)")
def test_lxml_stays_because_openpyxl_uses_it():
    importers = policy.read_xref_importers(XREF, packages=["lxml.etree"])
    users = importers.get("lxml.etree", [])
    assert any(u.startswith("openpyxl") for u in users), (
        "lxml.etree is expected to be reached from openpyxl -- if that "
        "changed, the 'keep lxml' rule needs re-examining"
    )
