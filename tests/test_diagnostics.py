"""``python main.py doctor`` must be safe to run on a real research machine.

The three properties that matter:
  * it is READ-ONLY (never creates a data dir, key, or database),
  * it reports the optional-dependency degradations that are otherwise silent,
  * it reports the real encryption level instead of a swallowed RuntimeWarning.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core import diagnostics  # noqa: E402


def test_doctor_output_names_the_dependencies_and_the_encryption_level():
    buf = io.StringIO()
    diagnostics.run_doctor(stream=buf)
    text = buf.getvalue()
    assert "matplotlib" in text
    assert "numpy" in text
    assert "encryption_status" in text
    assert "数据文件" in text
    assert "体检" in text


def test_doctor_never_creates_the_data_directory(tmp_path, monkeypatch):
    """On a machine with no prior install, running doctor must leave no trace."""
    target = tmp_path / "home" / ".range_chart_analyzer"
    monkeypatch.setattr(diagnostics, "_data_dir", lambda: target)
    buf = io.StringIO()
    diagnostics.run_doctor(stream=buf)
    assert not target.exists(), "doctor created the user's data directory"
    assert "尚未创建" in buf.getvalue()


def test_doctor_detects_a_corrupt_history_database(tmp_path, monkeypatch):
    """A corrupt rca.db must be REPORTED, not silently shown as an empty list."""
    data = tmp_path / ".range_chart_analyzer"
    data.mkdir()
    (data / "rca.db").write_bytes(b"this is not a sqlite database")
    monkeypatch.setattr(diagnostics, "_data_dir", lambda: data)
    buf = io.StringIO()
    rc = diagnostics.run_doctor(stream=buf)
    text = buf.getvalue()
    assert "rca.db" in text
    assert rc == 1, "a corrupt critical data file must make doctor fail"
    assert "备份" in text, "doctor must tell the user to back up before deleting"


def test_doctor_flags_an_empty_fernet_key_file(tmp_path, monkeypatch):
    data = tmp_path / ".range_chart_analyzer"
    data.mkdir()
    (data / "fernet_key.fek").write_bytes(b"")
    monkeypatch.setattr(diagnostics, "_data_dir", lambda: data)
    buf = io.StringIO()
    diagnostics.run_doctor(stream=buf)
    assert "空文件" in buf.getvalue()


def test_doctor_is_reachable_from_the_launcher():
    """The README and CONTRIBUTING.md both promise this exact command.

    Exit code is 0 on a fully provisioned machine and 1 when doctor has
    something to report -- CI deliberately does NOT install matplotlib or
    the GUI toolkits, so rc==1 there is CORRECT behaviour, not a failure.
    Assert the command runs and produces the report, not that the host is
    perfectly provisioned.
    """
    import subprocess
    result = subprocess.run(
        [sys.executable, "main.py", "doctor"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode in (0, 1), result.stderr
    assert "体检" in result.stdout
    assert "matplotlib" in result.stdout
    assert "encryption_status" in result.stdout


def test_release_gate_survives_post_release_commits(tmp_path):
    """A tag is a snapshot; new commits after a release must not fail the gate.

    The first version of scripts/release_check.py required tag == HEAD, which
    would turn the gate red the moment anyone committed docs or a fix after
    publishing. The real invariants are: tag is an ancestor of HEAD, and the
    tag's own bytes contain the PBDB fixes.
    """
    import subprocess
    src = (ROOT / "scripts" / "release_check.py").read_text(encoding="utf-8")
    assert "merge-base" in src and "--is-ancestor" in src
    assert 'if head != tag_commit:' not in src, "the tag==HEAD rule came back"
