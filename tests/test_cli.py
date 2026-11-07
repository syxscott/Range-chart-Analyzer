"""The headless CLI must be a thin adapter, not a second pipeline.

Tests here never call a real LLM: extract / resolve_auto_mode / ProviderStore
are stubbed, and what is asserted is the CLI's OWN behaviour -- batching,
stable ordering, output shape, failure isolation, and that the same core
functions the GUIs use are the ones called.
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core import cli  # noqa: E402


class _Result:
    def __init__(self, ok=True, data=None, error_key=None):
        self.ok = ok
        self.data = data if data is not None else {"species_ranges": []}
        self.error_key = error_key
        self.image_sha256 = "0" * 64
        self.raw = ""


def _stub_core(monkeypatch, calls, *, fail_on=None, resolved="range_chart"):
    monkeypatch.setattr(cli, "_provider", lambda: SimpleNamespace(name="stub"))

    def fake_resolve(**kw):
        calls.append(("resolve", kw["filename"]))
        return resolved, None

    def fake_extract(**kw):
        calls.append(("extract", kw["filename"], kw["mode"]))
        if fail_on and kw["filename"] == fail_on:
            return _Result(ok=False, data=None, error_key="err.extract")
        return _Result()

    # Import sites are inside run_cli; patch the source modules instead.
    import rca_core.extractor as extractor_mod
    monkeypatch.setattr(extractor_mod, "resolve_auto_mode", fake_resolve)
    monkeypatch.setattr(extractor_mod, "extract", fake_extract)
    return extractor_mod


def _images(tmp_path, names=("a.png", "b.png")):
    d = tmp_path / "figs"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"\x89PNG\r\n\x1a\n" + n.encode())
    return d


def test_batch_writes_one_json_per_image_with_provenance(tmp_path, monkeypatch):
    calls = []
    _stub_core(monkeypatch, calls)
    figs = _images(tmp_path)
    out = tmp_path / "out"
    rc = cli.run_cli(["--image", str(figs), "--out", str(out), "--quiet"])
    assert rc == 0
    files = sorted(p.name for p in out.glob("*.json"))
    assert files == ["a.json", "b.json"]
    payload = json.loads((out / "a.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["mode"] == "range_chart"
    assert len(payload["image_sha256"]) == 64
    assert payload["report"]["provenance"]["source"] == "cli"
    assert payload["report"]["degradations"] == [] or isinstance(
        payload["report"]["degradations"], list)
    # JSON carries the column semantics inline: one self-describing file per
    # figure, with no duplicate <stem>.dictionary.json sidecar.
    assert "confidence" in payload["data_dictionary"]
    assert "not a calibrated probability" in payload["confidence_note"]
    assert [c[0] for c in calls] == ["resolve", "extract", "resolve", "extract"]


def test_explicit_mode_skips_classification(tmp_path, monkeypatch):
    calls = []
    _stub_core(monkeypatch, calls)
    fig = _images(tmp_path, ("one.png",)) / "one.png"
    rc = cli.run_cli(["--image", str(fig), "--out", str(tmp_path / "o"),
                      "--mode", "columnar_section", "--quiet"])
    assert rc == 0
    assert [c[0] for c in calls] == ["extract"]
    assert calls[0][2] == "columnar_section"


def test_one_failed_figure_does_not_abort_the_batch(tmp_path, monkeypatch):
    calls = []
    _stub_core(monkeypatch, calls, fail_on="a.png")
    figs = _images(tmp_path)
    out = tmp_path / "out"
    rc = cli.run_cli(["--image", str(figs), "--out", str(out), "--quiet"])
    assert rc == 1
    assert (out / "a.json").is_file() and (out / "b.json").is_file()
    assert json.loads((out / "a.json").read_text())["ok"] is False


def test_missing_image_returns_usage_error(tmp_path):
    rc = cli.run_cli(["--image", str(tmp_path / "nope.png"),
                      "--out", str(tmp_path / "o"), "--quiet"])
    assert rc == 2


def test_repeatable_image_order_and_dedup(tmp_path, monkeypatch):
    calls = []
    _stub_core(monkeypatch, calls)
    figs = _images(tmp_path, ("b.png", "a.png"))
    rc = cli.run_cli(["--image", str(figs / "b.png"),
                      "--image", str(figs / "a.png"),
                      "--image", str(figs / "b.png"),
                      "--out", str(tmp_path / "o"), "--quiet"])
    assert rc == 0
    assert [c[1] for c in calls if c[0] == "extract"] == ["b.png", "a.png"]


def test_cli_is_reachable_from_the_launcher(tmp_path):
    import subprocess
    help_result = subprocess.run(
        [sys.executable, "main.py", "extract", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert help_result.returncode == 0, help_result.stderr
    assert "--image" in help_result.stdout and "--out" in help_result.stdout
