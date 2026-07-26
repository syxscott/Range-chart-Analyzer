"""Regression tests for P2-4: HistoryStore.update_result must persist
PROV-O (W3C provenance ontology) shaped activity records so downstream
consumers (CSL-Editor ingestion, repository archival) can ingest edits
without bespoke mapping.

REVIEW-2026-07-25 P2-4.
"""
from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.db import Database
from rca_core.history import HistoryStore, HistoryRecord


def _gc_temp():
    gc.collect()
    time.sleep(0.05)


def _new_store(tmp, name="t.db"):
    db = Database(path=os.path.join(tmp, name))
    store = HistoryStore(db=db)
    rec = HistoryRecord(
        timestamp=1.0, source_file="x.png",
        provider_id="p", provider_name="P", model="m",
        mode="range_chart", runs=1,
        result={"confidence": 0.5}, raw="{}",
    )
    rid = store.add(rec)
    return db, store, rid


class TestProvOSerialization:
    def test_edit_chain_contains_prov_o_activity(self):
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                store.update_result(rid, {"confidence": 0.9}, editor="user")
                row = db.execute(
                    "SELECT edit_provenance FROM history WHERE id = ?",
                    (rid,)
                ).fetchone()
                chain = json.loads(row[0])
                assert len(chain) == 1
                entry = chain[0]
                assert "prov_o_activity" in entry
                act = entry["prov_o_activity"]
                assert act["type"] == "prov:Activity"
                assert act["prov:wasAssociatedWith"]["type"] == "prov:Agent"
                assert "gui" not in act["prov:wasAssociatedWith"]["id"]  # this was 'user'
                assert act["prov:wasAssociatedWith"]["id"] == "rca:agent:user"
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_each_edit_emits_distinct_activity_id(self):
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                store.update_result(rid, {}, editor="user")
                store.update_result(rid, {}, editor="gui_fluent")
                store.update_result(rid, {}, editor="js/app.js")
                row = db.execute(
                    "SELECT edit_provenance FROM history WHERE id = ?",
                    (rid,)
                ).fetchone()
                chain = json.loads(row[0])
                act_ids = [c["prov_o_activity"]["id"] for c in chain]
                assert len(set(act_ids)) == 3, (
                    f"each edit must get a unique activity id; got {act_ids}"
                )
                # Agent IDs are unique per editor
                agents = [c["prov_o_activity"]["prov:wasAssociatedWith"]["id"]
                          for c in chain]
                assert agents == [
                    "rca:agent:user",
                    "rca:agent:gui_fluent",
                    "rca:agent:js/app.js",
                ]
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_activity_carries_used_and_generated_entities(self):
        """PROV-O ``prov:used`` and ``prov:generated`` link the activity
        to the history record entity and the resulting edit artifact."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                store.update_result(rid, {"x": 1}, editor="user")
                row = db.execute(
                    "SELECT edit_provenance FROM history WHERE id = ?",
                    (rid,)
                ).fetchone()
                chain = json.loads(row[0])
                act = chain[0]["prov_o_activity"]
                assert act["prov:used"]["type"] == "prov:Entity"
                assert str(rid) in act["prov:used"]["id"]
                assert act["prov:generated"]["type"] == "prov:Entity"
                assert "edit:1" in act["prov:generated"]["id"]
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass


class TestToProvJsonLd:
    """Tests for HistoryStore.to_prov_jsonld()."""

    def test_returns_context(self):
        """Output always includes the @context key."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                doc = store.to_prov_jsonld(rid)
                assert "@context" in doc
                ctx = doc["@context"]
                # W3C PROV-O context must be present
                assert "prov" in ctx
                assert ctx["prov"] == "https://www.w3.org/ns/prov#"
                # RCA custom namespace must be present
                assert "rca" in ctx
                assert "xsd" in ctx
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_missing_record_returns_only_context(self):
        """Requesting a non-existent record returns minimal document."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, _ = _new_store(tmp)
            try:
                doc = store.to_prov_jsonld(99999)
                assert "@context" in doc
                assert len(doc) == 1  # only @context
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_image_entity_present(self):
        """The original image entity (rca:image:{{sha256}}) is declared."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                doc = store.to_prov_jsonld(rid)
                entities = doc.get("prov:entity", [])
                img_entities = [e for e in entities if e["@id"].startswith("rca:image:")]
                assert len(img_entities) >= 1, f"expected image entity, got {entities}"
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_extraction_activity_present(self):
        """The extraction activity (rca:history:{id}:extract) is declared."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                doc = store.to_prov_jsonld(rid)
                activities = doc.get("prov:activity", [])
                extract_acts = [a for a in activities if ":extract" in a["@id"]]
                assert len(extract_acts) == 1, f"expected 1 extract activity, got {activities}"
                act = extract_acts[0]
                assert "prov:used" in act
                assert "prov:wasAssociatedWith" in act
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_edit_activities_present_after_edits(self):
        """After edits, rca:history:{id}:edit:N activities are declared."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                store.update_result(rid, {"a": 1}, editor="user")
                store.update_result(rid, {"a": 2}, editor="gui_fluent")
                doc = store.to_prov_jsonld(rid)
                activities = doc.get("prov:activity", [])
                edit_activities = [a for a in activities if ":edit:" in a["@id"]]
                assert len(edit_activities) == 2, f"expected 2 edit activities, got {len(edit_activities)}"
                ids = sorted(a["@id"] for a in edit_activities)
                assert "rca:history:1:edit:1" in ids
                assert "rca:history:1:edit:2" in ids
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_agent_declarations_present(self):
        """User, gui_fluent, and js/app.js agents are declared."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                doc = store.to_prov_jsonld(rid)
                agents = doc.get("prov:agent", [])
                agent_ids = {a["@id"] for a in agents}
                assert "rca:agent:user" in agent_ids
                assert "rca:agent:gui_fluent" in agent_ids
                assert "rca:agent:js/app.js" in agent_ids
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_prov_links_exist(self):
        """prov:wasGeneratedBy, prov:used, prov:wasAssociatedWith links present."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                store.update_result(rid, {"k": "v"}, editor="user")
                doc = store.to_prov_jsonld(rid)
                # Check at least one activity has prov:used
                activities = doc.get("prov:activity", [])
                used_links = [a for a in activities if "prov:used" in a]
                assert len(used_links) >= 1, "expected at least one prov:used link"
                # Check at least one activity has prov:wasAssociatedWith
                assoc_links = [a for a in activities if "prov:wasAssociatedWith" in a]
                assert len(assoc_links) >= 1, "expected at least one prov:wasAssociatedWith link"
                # Check at least one entity has prov:wasGeneratedBy
                entities = doc.get("prov:entity", [])
                gen_links = [e for e in entities if "prov:wasGeneratedBy" in e]
                assert len(gen_links) >= 1, "expected at least one prov:wasGeneratedBy link"
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_endedAtTime_on_edit_activity(self):
        """Edit activity in the chain carries prov:endedAtTime."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db, store, rid = _new_store(tmp)
            try:
                store.update_result(rid, {"x": 1}, editor="user")
                doc = store.to_prov_jsonld(rid)
                activities = doc.get("prov:activity", [])
                edit_acts = [a for a in activities if ":edit:" in a["@id"]]
                assert len(edit_acts) == 1
                act = edit_acts[0]
                assert "prov:startedAtTime" in act, f"expected startedAtTime in {act}"
                assert "prov:endedAtTime" in act, f"expected endedAtTime in {act}"
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_merge_activity_when_runs_gt_1(self):
        """When runs > 1, a merge activity is included."""
        tmp = tempfile.mkdtemp(prefix="rca_p24_")
        try:
            db = Database(path=os.path.join(tmp, "t.db"))
            store = HistoryStore(db=db)
            rec = HistoryRecord(
                timestamp=1.0, source_file="x.png",
                provider_id="p", provider_name="P", model="m",
                mode="range_chart", runs=3,  # multi-run
                result={"confidence": 0.5}, raw="{}",
            )
            rid = store.add(rec)
            try:
                doc = store.to_prov_jsonld(rid)
                activities = doc.get("prov:activity", [])
                merge_acts = [a for a in activities if ":merge" in a["@id"]]
                assert len(merge_acts) == 1, f"expected 1 merge activity, got {activities}"
            finally:
                db.close()
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass