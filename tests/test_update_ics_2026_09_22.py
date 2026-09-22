"""FIX-2026-09-22 audit items 2/3/4/5 for scripts/update_ics.py.

Regressions pinned here (all offline - the ICS fixtures under tests/ and an
injected fetch callable; the network layer is never touched):

  * item 2: --offline-fixture + --write-canonical is REFUSED (toy data must
    not silently become the canonical authority); --force promotes it only
    behind a loud warning.
  * item 3: promote durability - numbered .bak rotation (no clobbered fixed
    backup), ATOMIC writes (temp + fsync + os.replace, never a half file),
    the payload must load through rca_core/standards/ics.py BEFORE the
    canonical file is touched, and the ics.py fallback is LOUD.
  * item 4: version provenance - refusing to stamp post-baseline stages with
    the baseline's ICS_VERSION instead of failing the promote.
  * item 5a: the working Macrostrat endpoint leads; the 404-ing documented
    alias stays as fallback.
"""

from __future__ import annotations

import importlib.util
import json
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("update_ics_2026_09_22",
                                              ROOT / "scripts" / "update_ics.py")
update_ics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_ics)

FIXTURE_JSON = ROOT / "tests" / "fixtures" / "ics_macrostrat_mini.json"
FIXTURE_TTL = ROOT / "tests" / "fixtures" / "ics_chart_mini.ttl"
BASELINE = ROOT / "rca_core" / "resources" / "ics_2024.json"
ICS_MODULE = ROOT / "rca_core" / "standards" / "ics.py"


def _fixture_fetch(url: str) -> str:
    """Serve the mini Macrostrat payload offline, any endpoint order."""
    if "timescale_id=2" in url or "timescale_id=3" in url:
        return json.dumps({"success": {"data": []}})
    return FIXTURE_JSON.read_text(encoding="utf-8")


def _copy_baseline(tmp_path: Path) -> Path:
    canon = tmp_path / "ics_2024.json"
    canon.write_bytes(BASELINE.read_bytes())
    return canon


def _promote(tmp_path: Path, canon: Path, *, lines: list, **kwargs):
    """Fetch-injected macrostrat promotion (no fixture provenance, so the
    run exercises exactly one gate at a time)."""
    kwargs.setdefault("ics_version", None)
    kwargs.setdefault("retrieved_at", "2026-09-22T00:00:00Z")
    return update_ics.run(out=tmp_path / "ics_current.json", canonical=canon,
                          fetch=_fixture_fetch, write_canonical=True,
                          reporter=lines.append, **kwargs)


# ---------------------------------------------------------------------------
# item 2: --offline-fixture + --write-canonical
# ---------------------------------------------------------------------------

class TestFixturePromotionGate:
    def test_run_level_gate_refuses_and_changes_nothing(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        before = canon.read_bytes()
        lines: list = []
        summary = update_ics.run(out=tmp_path / "ics_current.json",
                                 canonical=canon, fixture=FIXTURE_JSON,
                                 retrieved_at="2026-09-22T00:00:00Z",
                                 write_canonical=True, reporter=lines.append)
        assert not summary["promoted"]
        assert summary["promote_reason"] == "offline-fixture provenance"
        assert canon.read_bytes() == before
        assert not list(tmp_path.glob("ics_2024.json.bak*"))
        assert any("REFUSING" in ln and "offline-fixture" in ln for ln in lines)

    def test_force_promotes_but_prints_a_loud_warning(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        lines: list = []
        summary = update_ics.run(out=tmp_path / "ics_current.json",
                                 canonical=canon, fixture=FIXTURE_JSON,
                                 retrieved_at="2026-09-22T00:00:00Z",
                                 write_canonical=True, force=True,
                                 reporter=lines.append)
        assert summary["promoted"]
        assert any("LOUD WARNING" in ln and "OFFLINE-FIXTURE" in ln
                   for ln in lines)

    def test_cli_refuses_the_combination_with_its_own_exit_code(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        before = canon.read_bytes()
        code = update_ics.main([
            "--offline-fixture", str(FIXTURE_JSON),
            "--canonical", str(canon),
            "--out", str(tmp_path / "ics_current.json"),
            "--write-canonical",
            "--retrieved-at", "2026-09-22T00:00:00Z"])
        assert code == 4
        assert canon.read_bytes() == before


# ---------------------------------------------------------------------------
# item 4: ics_version provenance
# ---------------------------------------------------------------------------

class TestVersionProvenance:
    def test_macrostrat_channel_falls_back_to_the_baseline_stamp(self, tmp_path):
        # The Macrostrat channel always answers "" for ics_version; the
        # fallback "ICS v2024/12" comes from ICS_VERSION in ics.py and
        # describes the BASELINE, not the refresh.
        canon = _copy_baseline(tmp_path)
        lines: list = []
        summary = _promote(tmp_path, canon, lines=lines)
        assert summary["version"] == "ICS v2024/12"
        assert summary["version_source"] == "baseline-fallback"
        # the fixture ladder carries stages the 2024/12 baseline never had
        assert "Meghalayan" in summary["new_beyond_baseline"] or \
               summary["new_beyond_baseline"], "contradiction must be detectable"

    def test_promote_refused_when_version_contradicts_the_stage_set(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        before = canon.read_bytes()
        lines: list = []
        summary = _promote(tmp_path, canon, lines=lines)
        assert not summary["promoted"]
        assert summary["promote_reason"] == "ics_version provenance"
        assert any("REFUSING" in ln for ln in lines)
        assert any("--ics-version" in ln for ln in lines), \
            "the refusal must tell the operator how to resolve it"
        assert canon.read_bytes() == before
        assert not list(tmp_path.glob("ics_2024.json.bak*"))

    def test_explicit_ics_version_resolves_the_contradiction(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        lines: list = []
        summary = _promote(tmp_path, canon, lines=lines,
                           ics_version="ICS v2026/06")
        assert summary["promoted"], lines
        assert summary["version_source"] == "explicit"
        promoted = json.loads(canon.read_text(encoding="utf-8"))
        new_row = promoted["Meghalayan"]
        assert new_row["ics_version"] == "ICS v2026/06", \
            "new data must carry the version it was actually established at"

    def test_channel_carried_version_is_derived_and_trusted(self, tmp_path):
        # The ICS chart RDF carries its own stamp (owl:versionIRI) - that IS
        # a derived version, so a promote with post-baseline stages needs no
        # --ics-version. Absent baseline keeps the payload validation-free
        # (TTL-vs-2024 ladder conflicts are pinned in the 09-20 file).
        canon = tmp_path / "ics_2024.json"  # does not exist yet
        lines: list = []
        summary = update_ics.run(out=tmp_path / "ics_current.json",
                                 canonical=canon,
                                 fetch=lambda url: FIXTURE_TTL.read_text(encoding="utf-8"),
                                 retrieved_at="2026-09-22T00:00:00Z",
                                 write_canonical=True, reporter=lines.append)
        assert not summary["errors"], lines
        assert summary["version"] == "2026-06"
        assert summary["version_source"] == "channel"
        assert summary["new_beyond_baseline"], "post-baseline stages exist"
        assert summary["promoted"], lines

    def test_gitignore_hides_promote_byproducts(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "*.bak" in ignore
        assert "*.json.tmp" in ignore


# ---------------------------------------------------------------------------
# item 3a/3b: backup rotation + atomic write
# ---------------------------------------------------------------------------

class TestPromoteDurability:
    def _promote_thrice(self, tmp_path, canon, lines):
        snapshots = [canon.read_bytes()]
        for stamp in ("2026-09-22T00:00:00Z", "2026-09-22T01:00:00Z",
                      "2026-09-22T02:00:00Z"):
            _promote(tmp_path, canon, lines=lines,
                     ics_version=f"ICS v2026/0{len(snapshots)}",
                     retrieved_at=stamp)
            snapshots.append(canon.read_bytes())
        return snapshots

    def test_two_promotes_keep_the_true_baseline_recoverable(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        original = canon.read_bytes()
        lines: list = []
        self._promote_thrice(tmp_path, canon, lines)
        # newest backup at .bak, older generations numbered upward - the
        # fixed-name clobber (.bak overwritten every promote) is gone:
        assert canon.with_name("ics_2024.json.bak").exists()
        assert canon.with_name("ics_2024.json.bak.1").exists()
        deepest = canon.with_name("ics_2024.json.bak.2")
        assert deepest.exists(), "after three promotes two older generations survive"
        assert deepest.read_bytes() == original, \
            "the hand-rebuilt baseline must still be recoverable from the FS"

    def test_rotation_is_capped(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        lines: list = []
        for i in range(8):
            _promote(tmp_path, canon, lines=lines,
                     ics_version=f"ICS v2026/{i}",
                     retrieved_at=f"2026-09-22T0{i}:00:00Z")
        backups = sorted(tmp_path.glob("ics_2024.json.bak*"))
        assert len(backups) == update_ics.BACKUP_KEEP
        names = {b.name for b in backups}
        assert names == {"ics_2024.json.bak"} | {
            f"ics_2024.json.bak.{k}" for k in range(1, update_ics.BACKUP_KEEP)}

    def test_write_json_is_atomic_and_leaves_no_temp_file(self, tmp_path, monkeypatch):
        target = tmp_path / "table.json"
        calls: list = []
        real_replace = update_ics.os.replace

        def spy_replace(src, dst):
            calls.append((Path(src), Path(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(update_ics.os, "replace", spy_replace)
        update_ics.write_json(target, {"A": {"top_ma": 1.0}})
        # UI-REVIEW-2026-09-22: tmp name now carries pid+uuid (unique per
        # run); assert the sibling-staging + os.replace CONTRACT.
        assert len(calls) == 1
        staged, dst = calls[0]
        assert dst == target
        assert staged.parent == target.parent
        assert staged.name.startswith(target.name)
        assert staged.name.endswith(".tmp"), \
            "write_json must stage in a sibling temp file and os.replace it"
        assert json.loads(target.read_text(encoding="utf-8")) == \
            {"A": {"top_ma": 1.0}}

    def test_failed_atomic_write_keeps_the_old_file_intact(self, tmp_path, monkeypatch):
        target = tmp_path / "table.json"
        update_ics.write_json(target, {"A": {"top_ma": 1.0}})
        before = target.read_bytes()

        def boom(src, dst):
            raise OSError("crash mid-promote")

        monkeypatch.setattr(update_ics.os, "replace", boom)
        with pytest.raises(OSError):
            update_ics.write_json(target, {"B": {"top_ma": 2.0}})
        # the never-reached rename means: old bytes intact, no truncation,
        # and the staged temp file cleaned up.
        assert target.read_bytes() == before
        assert not list(tmp_path.glob("*.tmp"))

    def test_payload_that_would_degrade_ics_py_blocks_the_promote(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        before = canon.read_bytes()
        bad = json.loads(before)
        # a half-table shaped like a real corruption: one row is a string,
        # which makes standards/ics.py's M7(b) fallback zero the WHOLE table
        bad["Holocene"] = "truncated-write-artifact"
        lines: list = []
        result = update_ics.promote_to_canonical(
            canon, bad, errors=[], diff=update_ics.diff_tables({}, bad),
            force=False, reporter=lines.append,
            provenance="macrostrat:defs", version="ICS v2026/06",
            version_source="explicit")
        assert not result["promoted"]
        assert result["reason"] == "payload fails ics.py load"
        assert any("DEGRADED" in ln for ln in lines), lines
        assert canon.read_bytes() == before
        assert not list(tmp_path.glob("ics_2024.json.bak*")), \
            "a payload that cannot load must not even rotate the backups"

    def test_non_finite_age_bounds_are_blocked(self, tmp_path):
        canon = _copy_baseline(tmp_path)
        bad = {"Only": {"rank": "Stage", "abbrev": "On", "top_ma": float("nan"),
                        "base_ma": 10.0, "period": "Cambrian",
                        "period_top_ma": 0.0, "period_base_ma": 538.8,
                        "era": "Paleozoic", "source": "s", "license": "l",
                        "retrieved_at": "t", "ics_version": "v"}}
        lines: list = []
        result = update_ics.promote_to_canonical(
            canon, bad, errors=[], diff=update_ics.diff_tables({}, bad),
            force=False, reporter=lines.append,
            provenance="macrostrat:defs", version="ICS v2026/06",
            version_source="explicit")
        assert not result["promoted"]
        assert any("strict JSON" in ln or "non-finite" in ln for ln in lines)

    def test_good_payload_loads_through_the_ics_module(self):
        table = json.loads(BASELINE.read_text(encoding="utf-8"))
        assert update_ics.validate_loads_in_ics_module(table) == []


# ---------------------------------------------------------------------------
# item 3d: standards/ics.py must degrade LOUDLY
# ---------------------------------------------------------------------------

def _load_ics_copy(tmp_path: Path, payload: str, module_name: str):
    """Execute a copy of rca_core/standards/ics.py against *payload*, in a
    throwaway tree, exactly as the consumers import it at startup."""
    src = ICS_MODULE.read_text(encoding="utf-8")
    root = tmp_path / module_name
    (root / "standards").mkdir(parents=True)
    (root / "resources").mkdir(parents=True)
    module_path = root / "standards" / "ics.py"
    module_path.write_text(src, encoding="utf-8")
    (root / "resources" / "ics_2024.json").write_text(payload, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"ics_copy_{module_name}",
                                                  module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestLoudIcsFallback:
    def test_truncated_file_warns_and_flags_degraded(self, tmp_path):
        raw = BASELINE.read_text(encoding="utf-8")
        half = raw[: len(raw) // 2]  # a crash-mid-write looks exactly like this
        with pytest.warns(RuntimeWarning, match="ICS 2024 table unavailable"):
            module = _load_ics_copy(tmp_path, half, "truncated")
        assert module.ICS_2024 == {}
        assert module.ICS_TABLE_DEGRADED is True

    def test_parseable_but_wrong_shape_is_caught_too(self, tmp_path):
        # A JSON ARRAY parses fine but poisons every dict consumer - the
        # fallback must not treat "valid JSON" as "usable table".
        with pytest.warns(RuntimeWarning, match="non-empty object"):
            module = _load_ics_copy(tmp_path, "[1, 2, 3]", "listshape")
        assert module.ICS_2024 == {}
        assert module.ICS_TABLE_DEGRADED is True

    def test_row_that_is_not_an_object_degrades_loudly(self, tmp_path):
        table = json.loads(BASELINE.read_text(encoding="utf-8"))
        table["Holocene"] = "artifact"
        with pytest.warns(RuntimeWarning, match="row\\(s\\) are not objects"):
            module = _load_ics_copy(tmp_path, json.dumps(table), "badrow")
        assert module.ICS_2024 == {}

    def test_healthy_load_is_silent(self, tmp_path):
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning -> test failure
            module = _load_ics_copy(
                tmp_path, BASELINE.read_text(encoding="utf-8"), "healthy")
        assert module.ICS_TABLE_DEGRADED is False
        assert len(module.ICS_2024) == 98


# ---------------------------------------------------------------------------
# item 5a: endpoint order (kept here too; the 09-20 file pins it as well)
# ---------------------------------------------------------------------------

class TestEndpointOrder:
    def test_working_endpoint_is_first_and_404_alias_is_fallback(self):
        urls = [url for _label, url in update_ics.MACROSTRAT_AGE_URLS]
        assert "intervals?format=json&all=1&timescale_id=1" in urls[0]
        assert "/defs/ages?" in urls[1]

    def test_offline_channel_never_touches_the_404_first(self):
        tried: list = []

        def fetch(url):
            tried.append(url)
            return FIXTURE_JSON.read_text(encoding="utf-8")

        update_ics.acquire("macrostrat", fetch=fetch)
        assert "timescale_id=1" in tried[0]
