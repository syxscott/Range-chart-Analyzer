"""Backup must include the Fernet master key and never leak into a repo."""
from __future__ import annotations

import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core.backup import create_backup, run_backup  # noqa: E402


def _fake_home(tmp_path: pathlib.Path):
    home = tmp_path / "home"
    data = home / ".range_chart_analyzer"
    data.mkdir(parents=True)
    (data / "providers.json").write_text('{"providers": []}', encoding="utf-8")
    (data / "fernet_key.fek").write_bytes(b"K" * 44)
    (data / "rca.db").write_bytes(b"SQLite format 3\x00")
    (home / ".range_chart_analyzer.json").write_text("{}", encoding="utf-8")
    return home


def test_backup_contains_all_critical_files(tmp_path):
    home = _fake_home(tmp_path)
    dest = tmp_path / "b.zip"
    manifest = create_backup(dest, home=home)
    assert dest.is_file()
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
        assert ".range_chart_analyzer/fernet_key.fek" in names
        assert ".range_chart_analyzer/providers.json" in names
        assert ".range_chart_analyzer/rca.db" in names
        assert ".range_chart_analyzer.json" in names
        assert "BACKUP_MANIFEST.json" in names
        assert json.loads(z.read("BACKUP_MANIFEST.json"))[
            "contains_fernet_key"] is True
    assert manifest["contains_fernet_key"] is True
    assert manifest["missing_critical"] == []


def test_manifest_warns_when_the_key_is_missing(tmp_path):
    home = _fake_home(tmp_path)
    (home / ".range_chart_analyzer" / "fernet_key.fek").unlink()
    manifest = create_backup(tmp_path / "b.zip", home=home)
    assert "fernet_key.fek" in manifest["missing_critical"]
    assert "password" in manifest["warning"]


def test_backup_is_read_only_with_respect_to_the_data_dir(tmp_path):
    home = _fake_home(tmp_path)
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    create_backup(tmp_path / "b.zip", home=home)
    after = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert before == after


def test_cli_entry_point(tmp_path, capsys):
    home = _fake_home(tmp_path)
    rc = run_backup(["--out", str(tmp_path / "b.zip"), "--home", str(home)])
    assert rc == 0
    assert "wrote" in capsys.readouterr().out
