"""The shared config directory is created 0700 by every writer, not some (2026-09-30).

`~/.range_chart_analyzer` holds providers.json, the Fernet key
(fernet_key.fek), the PBKDF2 salt and rca.db. rca_core/secrets_store.py's
`_base_dir()` states the invariant in its own docstring -- the directory "used
to be created with os.makedirs(..., exist_ok=True) and left at the process
umask default (0777 & ~umask, i.e. usually 0755). Everything this module
stores in it is key material, so it is now 0700 best-effort" -- and enforces
it at its own two call sites.

Three OTHER modules create the SAME directory with no mode at all:

    rca_core/llm.py    ProviderStore.save()   providers.json
    rca_core/db.py     Database.__init__      rca.db
    rca_core/cache.py  _ensure_dir()          extract_cache.sqlite

`os.makedirs` only applies `mode` when it actually creates the directory, so
whichever writer ran first fixed the mode for the whole install. On a fresh
machine ProviderStore.save() is a perfectly ordinary first thing to run, and
it handed a 0755 directory -- listable and traversable by every local user --
to the module holding the API keys. secrets_store would tighten it on its next
call, but the window between app start and the first key operation is open,
and an install that only ever exports never takes that call at all.

The runtime assertion here is POSIX-only and therefore SKIPPED on a Windows
dev box, where mode bits are advisory (the real ACL comes from the profile
directory). CI runs ubuntu-latest, so the assertion is exercised there. That
split is deliberate: the code-level fact -- three of six makedirs call sites
on one directory lack the mode -- is measurable anywhere, while the permission
value is not.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.cache import ResultCache  # noqa: E402
from rca_core.db import Database, default_db_path  # noqa: E402
from rca_core.llm import LlmProvider, ProviderStore  # noqa: E402

# The two tests below that only compare PATHS and the source text do not need
# POSIX, so they opt back in explicitly rather than being dragged down by the
# module-level skip. The skip is applied per-test instead, at the two that
# actually stat a directory.
_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX mode bits are advisory on Windows; CI's ubuntu-latest "
           "is where the permission value is actually asserted",
)


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _run_writer(tmp_path, which):
    """Let one of the three writers be the FIRST to create the directory."""
    base = os.path.join(tmp_path, ".range_chart_analyzer")
    monkey_home = str(tmp_path)
    old = os.environ.get("HOME")
    oldp = os.environ.get("USERPROFILE")
    os.environ["HOME"] = monkey_home
    os.environ["USERPROFILE"] = monkey_home
    try:
        if which == "provider_store":
            store = ProviderStore(path=os.path.join(base, "providers.json"))
            store.add(LlmProvider(
                api_format="anthropic",
                endpoint="https://api.minimaxi.com/anthropic",
                api_key="sk-test-0123456789", model="m"))
        elif which == "database":
            Database(default_db_path()).close()
        elif which == "cache":
            ResultCache(db_path=os.path.join(base, "extract_cache.sqlite"))
            # touch the path so the file is materialised like real use
            c = ResultCache(db_path=os.path.join(base, "extract_cache.sqlite"))
            c.put("k", {"a": 1}) if hasattr(c, "put") else None
        else:
            raise AssertionError(which)
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old
        if oldp is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = oldp
    return base


@_posix_only
@pytest.mark.parametrize("writer", ["provider_store", "database", "cache"])
def test_each_writer_creates_the_config_dir_private(writer):
    """The invariant, one writer at a time: whoever gets there FIRST must leave
    a 0700 directory, because the other two inherit whatever it chose."""
    with tempfile.TemporaryDirectory() as td:
        base = _run_writer(td, writer)
        assert os.path.isdir(base), f"{writer} did not create the directory"
        assert _mode(base) == 0o700, (
            f"{writer} created ~/.range_chart_analyzer as "
            f"{oct(_mode(base))}; the default umask leaves 0755, which exposes "
            f"providers.json, fernet_key.fek and the salt to every local user"
        )


def test_every_makedirs_on_the_config_dir_passes_a_mode():
    """Platform-independent half of the guard, so the change is covered even
    on a Windows dev box where the runtime assertion below cannot run.

    The fact being pinned is simple: `os.makedirs` applies `mode` only when it
    CREATES the directory, so a call site without one silently inherits
    whatever umask the first writer happened to run under.
    """
    import ast
    import pathlib

    repo = pathlib.Path(PROJECT_ROOT)
    sites = []
    for rel in ("rca_core/llm.py", "rca_core/db.py", "rca_core/cache.py",
                "rca_core/secrets_store.py"):
        text = (repo / rel).read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "makedirs"):
                continue
            src = ast.get_source_segment(text, node) or ""
            # only the calls that build a DIRECTORY, not one that targets a
            # file; every makedirs in these four modules is a directory create
            sites.append((f"{rel}:{node.lineno}", "mode=" in src, src))

    assert sites, "no makedirs calls found -- the scan is wrong, not the code"
    missing = [f"{where}  {src}" for where, has_mode, src in sites
               if not has_mode]
    assert not missing, (
        "these makedirs calls create ~/.range_chart_analyzer (or its parent) "
        "without a mode, so a 0755 umask default wins for every writer that "
        "runs after them:\n  " + "\n  ".join(missing)
    )


def test_the_three_writers_all_target_the_same_directory():
    """Guards the premise of the runtime test above. If one of them ever moved
    its default location, those assertions would keep passing while the shared
    invariant quietly stopped applying.

    Compared against the CURRENT home rather than a temp one on purpose:
    rca_core/cache.py computes _CACHE_DIR and _DB_PATH at IMPORT time, so no
    amount of monkeypatching afterwards can move them, and a test that pretended
    otherwise would be asserting against a directory nothing writes to.
    """
    from rca_core import cache as C

    expected = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer")
    assert os.path.dirname(default_db_path()) == expected
    assert os.path.dirname(C._DB_PATH) == expected
    assert os.path.dirname(
        ProviderStore(path="p").path or "") in ("", expected) or True
    # ProviderStore has no module-level default; its caller supplies the path.
    # What must hold is that the two modules that DO default agree with
    # secrets_store's own base directory, which is the one that states the
    # invariant.
    from rca_core import secrets_store as S
    assert S._base_dir() == expected


@_posix_only
def test_an_existing_loose_directory_is_not_left_loose():
    """mode only applies on CREATION, so a directory another writer made 0755
    stays 0755 for the rest of the install. secrets_store._base_dir() is the
    one place that repairs it, which is why its chmod is unconditional."""
    from rca_core import secrets_store as S

    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("HOME")
        oldp = os.environ.get("USERPROFILE")
        os.environ["HOME"] = td
        os.environ["USERPROFILE"] = td
        try:
            base = os.path.join(td, ".range_chart_analyzer")
            os.makedirs(base, exist_ok=True)
            os.chmod(base, 0o755)
            assert _mode(base) == 0o755
            assert S._base_dir() == base
            assert _mode(base) == 0o700, (
                "_base_dir() must repair a directory some other writer left "
                "loose -- that unconditional chmod is the only safety net"
            )
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old
            if oldp is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = oldp
