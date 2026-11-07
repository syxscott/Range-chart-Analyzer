"""CAPABILITY-2026-11: Fluent desktop wiring for the three name capabilities.

WHY THIS FILE IS MOSTLY SOURCE-TEXT / AST
=========================================
This machine has NO PySide6, so ``import gui_fluent`` fails here. Per
docs/OWNER_DECISIONS.md ("PySide6 真机 UI" note) the authoritative UI verdict
is the CI ``qt`` job (``QT_QPA_PLATFORM=offscreen`` + real PySide6). This
module therefore has three layers, all runnable in the main CI matrix:

  1. STATIC — AST/source assertions on the real ``gui_fluent.py``: QThread
     worker, one queued signal, BOUND-METHOD slot, strong worker reference,
     no QTimer in run(), no network call in any GUI-thread slot, default OFF,
     and the "construct / retranslate / gate" three-piece rule for EVERY new
     control.
  2. KERNEL PATH — the actual rca_core functions are called OFFLINE with
     injected ``fetch`` / ``llm_caller`` doubles: GBIF fuzzy suggestions,
     grounding's unverified list, and row flags all really execute.
  3. PURE ORCHESTRATION — ``_run_name_capabilities`` / ``_apply_name_capability_payload``
     / ``_cap_t`` / ``NameCapabilityWorker.run`` are extracted from the AST and
     exec'd WITHOUT importing PySide6, so the real wiring code runs here.
     The fake signal DISPATCHES to a persistent receiver and the test
     explicitly asserts ``seen`` is non-empty — a slot connected to a
     temporary receiver is only weakly held and is silently never called.

The Qt-gated test at the end is the real-event-loop version of layer 3; it
skips locally and is the CI ``qt`` job's assertion that the slot fired.
"""
from __future__ import annotations

import ast
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GUI_FL = ROOT / "gui_fluent.py"


def _src() -> str:
    return GUI_FL.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_src())


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"method {cls.name}.{name} not found")


def _method_src(cls_name: str, method_name: str) -> str:
    tree = _tree()
    return ast.get_source_segment(_src(), _method(_class(tree, cls_name),
                                                  method_name)) or ""


def _non_comment_hits(symbol: str) -> int:
    return sum(1 for line in _src().splitlines()
               if symbol in line
               and not line.strip().startswith(("#", "//", "*", "/*")))


# ---------------------------------------------------------------------------
# Pure-code extraction: run the REAL wiring functions without PySide6
# ---------------------------------------------------------------------------
_PURE_FUNCS = ("_cap_t", "_run_name_capabilities",
               "_apply_name_capability_payload")
_PURE_CONSTS = ("NAME_CAP_TEXT", "NAME_VERIFY_TIMEOUT_SEC",
                "NAME_VERIFY_BUDGET_SEC", "NAME_GROUNDING_TIMEOUT_SEC")


def _load_pure() -> dict:
    """Compile ONLY the Qt-free top-level definitions from gui_fluent.py."""
    tree = _tree()
    body: list[ast.stmt] = [ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations", asname=None)], level=0)]
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _PURE_CONSTS
                for t in node.targets):
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in _PURE_FUNCS:
            body.append(node)
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert found == set(_PURE_FUNCS), f"missing pure functions: {found}"
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {"__builtins__": __builtins__}
    exec(compile(module, "<gui_fluent_pure>", "exec"), ns)  # noqa: S102
    return ns


class _GbifFetch:
    """Offline GBIF double: parser array + FUZZY species-match answer."""

    def __init__(self, canonical="Calocycletta costata", confidence=91):
        self.calls: list[str] = []
        self.canonical = canonical
        self.confidence = confidence

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if "/v1/parser/name" in url:
            return json.dumps([{
                "canonicalName": "Calocycletta costata",
                "genusOrAbove": "Calocycletta",
                "specificEpithet": "costata",
                "type": "SCIENTIFIC",
            }])
        if "/v1/species/match" in url:
            return json.dumps({
                "matchType": "FUZZY", "confidence": self.confidence,
                "canonicalName": self.canonical,
                "scientificName": self.canonical,
                "usageKey": 123, "rank": "SPECIES", "status": "ACCEPTED",
                "kingdom": "Animalia", "alternatives": [],
            })
        raise AssertionError(f"unexpected GBIF endpoint {url!r}")


class _GroundingCaller:
    """Offline second-vision call: one name is NOT printed in the figure."""

    def __init__(self, absent=("Callimitra rebus",)):
        self.calls: list[dict] = []
        self.absent = list(absent)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return (json.dumps({"absent": self.absent}), False, 200, "", {})


def _data() -> dict:
    return {
        "species_ranges": [
            {"species": "Calocycletta costata"},
            {"species": "Callimitra rebus"},
        ],
    }


def _qt_available() -> bool:
    try:
        import PySide6  # noqa: F401
        import qfluentwidgets  # noqa: F401
        return True
    except Exception:
        return False


# ===========================================================================
# 1. STATIC WIRING
# ===========================================================================
def test_capability_matrix_declares_all_three_landed_in_fluent():
    from rca_core import capability_matrix as cm
    for name in ("names.verify", "names.fuzzy", "grounding"):
        assert "fluent" in cm.CAPABILITIES[name]["landed"], name
        assert "fluent" not in cm.CAPABILITIES[name]["waivers"], (
            f"{name}: a landed endpoint must NOT keep a stale waiver")
        assert "fluent" not in cm.unwired(name), name
    assert cm.missing_waivers() == [], "\n".join(cm.missing_waivers())


@pytest.mark.parametrize("symbol", ["verify_names", "fuzzy",
                                    "check_name_grounding"])
def test_probes_are_real_symbols_in_the_fluent_file(symbol):
    """The matrix probe must hit a real line, not just a comment."""
    assert _non_comment_hits(symbol) > 0, (
        f"{symbol!r} only appears in comments in gui_fluent.py")


def test_worker_is_a_qthread_with_one_signal_and_no_timer():
    cls = _class(_tree(), "NameCapabilityWorker")
    base_names = {getattr(b, "id", getattr(b, "attr", "")) for b in cls.bases}
    assert "QThread" in base_names, "the name checks MUST run off the GUI thread"
    run_node = _method(cls, "run")
    run = ast.get_source_segment(_src(), run_node) or ""
    # QTimer created inside QThread.run() NEVER fires (measured, see
    # _Heartbeat). Check the AST, not the text: the explanatory comment in
    # run() legitimately names QTimer, and a text search would be a false
    # positive on the very rule this test protects.
    qtimer_refs = [n for n in ast.walk(run_node)
                   if (isinstance(n, ast.Name) and n.id == "QTimer")
                   or (isinstance(n, ast.Attribute) and n.attr == "QTimer")]
    assert not qtimer_refs, "no QTimer may be constructed on a worker thread"
    assert "_run_name_capabilities(" in run, "the real kernel path must run"
    assert "done.emit(self, payload)" in run, "one queued signal back to GUI"
    class_src = ast.get_source_segment(_src(), cls) or ""
    assert "done = Signal(object, object)" in class_src
    assert "request_cancel" in class_src
    assert "cancel_requested" in class_src


def test_gui_thread_slots_contain_no_network_or_model_calls():
    """A bare GBIF / verifier call in a slot freezes the window."""
    forbidden = ("verify_names(", "check_name_grounding(", "name_issues(")
    for method in ("_on_result", "_render_result", "load_result",
                   "_update_result_actions", "retranslate",
                   "_maybe_run_name_capabilities",
                   "_on_name_capabilities_done", "_on_name_worker_finished",
                   "_refresh_name_cap_label"):
        body = _method_src("ExtractPage", method)
        for call in forbidden:
            assert call not in body, (
                f"ExtractPage.{method} calls {call} on the GUI thread")
    # The only place these calls exist is the Qt-free worker payload.
    pure = ast.get_source_segment(_src(), next(
        n for n in _tree().body
        if isinstance(n, ast.FunctionDef)
        and n.name == "_run_name_capabilities")) or ""
    for call in forbidden:
        assert call in pure, f"{call} missing from the worker payload"


def test_signal_uses_a_bound_method_and_a_strong_worker_reference():
    src = _method_src("ExtractPage", "_maybe_run_name_capabilities")
    assert "self._name_worker = worker" in src, (
        "a temporary QThread gets GC'd mid-request (Qt6 qFatal)")
    assert "worker.done.connect(self._on_name_capabilities_done)" in src, (
        "a lambda / temporary receiver is only weakly held; the slot is "
        "silently never invoked")
    assert "worker.finished.connect(self._on_name_worker_finished)" in src
    assert "worker.start()" in src
    assert ".connect(lambda" not in src
    assert "Page()." not in src
    # closeEvent must tear the worker down like every other QThread.
    win_src = _method_src("RangeChartFluentWindow", "closeEvent")
    assert 'getattr(self.extract_page, "_name_worker", None)' in win_src
    assert "nw.request_cancel()" in win_src
    assert "_park_orphaned_worker(nw)" in win_src


def test_default_is_off_and_settings_are_persisted():
    settings = ast.get_source_segment(_src(), _class(_tree(), "SettingsPage")) or ""
    assert 'cfg.get("verify_names", False)' in settings
    assert 'cfg.get("ground_names", False)' in settings
    assert 'cfg.get("verify_names", True)' not in settings
    assert 'cfg.get("ground_names", True)' not in settings
    win = ast.get_source_segment(
        _src(), _class(_tree(), "RangeChartFluentWindow")) or ""
    assert 'self.cfg.get("verify_names", False)' in win
    assert 'self.cfg.get("ground_names", False)' in win
    save = _method_src("RangeChartFluentWindow", "save_generation")
    assert '"verify_names": self.verify_names_enabled()' in save
    assert '"ground_names": self.ground_names_enabled()' in save
    # The explicit extra-cost warning must be visible in the UI text.
    assert "额外" in settings or "extra" in settings.lower()


def test_three_piece_rule_construct_retranslate_gate():
    """Every new control: __init__ + three-language retranslate + the gate."""
    extract_init = _method_src("ExtractPage", "__init__")
    extract_retr = _method_src("ExtractPage", "retranslate")
    gate = _method_src("ExtractPage", "_update_result_actions")
    for attr in ("btn_name_check", "lbl_name_cap", "lbl_name_cap_cost"):
        assert f"self.{attr}" in extract_init, f"{attr} not constructed"
        assert f"self.{attr}" in extract_retr, f"{attr} not retranslated"
        assert f"self.{attr}" in gate, f"{attr} not gated"
    assert "self.btn_name_check.setEnabled(" in gate, (
        "zero-result state must disable the action, not leave a dead button")
    assert "gate_name_capabilities(has_result)" in gate

    settings_init = _method_src("SettingsPage", "__init__")
    settings_retr = _method_src("SettingsPage", "retranslate")
    settings_gate = _method_src("SettingsPage", "gate_name_capabilities")
    for attr in ("lbl_name_cap_title", "lbl_name_cap_cost",
                 "lbl_verify_names", "sw_verify_names",
                 "lbl_ground_names", "sw_ground_names",
                 "lbl_name_cap_state"):
        assert f"self.{attr}" in settings_init, f"{attr} not constructed"
        assert f"self.{attr}" in settings_retr, f"{attr} not retranslated"
        assert f"self.{attr}" in settings_gate, f"{attr} not gated"


def test_all_three_languages_are_present_and_complete():
    ns = _load_pure()
    table = ns["NAME_CAP_TEXT"]
    assert len(table) >= 15
    for key, translations in table.items():
        for lang in ("zh", "en", "ja"):
            assert translations.get(lang), f"{key} missing {lang}"
    # _cap_t must follow the window's language and fall back to English.
    class _Win:
        def __init__(self, lang):
            self.tr = type("T", (), {"lang": lang})()
    assert "名称" in ns["_cap_t"](_Win("zh"), "title")
    assert ns["_cap_t"](_Win("en"), "title") == "Name checks (optional)"
    assert ns["_cap_t"](_Win("ja"), "run") == "名称チェックを実行"
    assert ns["_cap_t"](_Win("xx"), "run") == table["run"]["en"]
    assert ns["_cap_t"](_Win("zh"), "no-such-key") == "no-such-key"


# ===========================================================================
# 2. REAL KERNEL PATH (offline doubles, real rca_core functions)
# ===========================================================================
def test_real_core_path_yields_fuzzy_issues_and_grounding_unverified():
    from rca_core.grounding import (check_name_grounding,
                                    collect_candidate_names,
                                    flag_unverified_rows)
    from rca_core.names import name_issues, verify_names

    data = _data()
    candidates, dropped = collect_candidate_names(data)
    assert candidates == ["Calocycletta costata", "Callimitra rebus"], candidates
    assert dropped == 0

    fetch = _GbifFetch()
    verification = verify_names(candidates, fetch=fetch, timeout=2, budget=5)
    issues = name_issues(verification)
    fuzzy = [i for i in issues if i["msg_key"] == "names.fuzzy"]
    assert fuzzy, "names.fuzzy must produce a usable canonical suggestion"
    assert fuzzy[0]["suggestion"] == "Calocycletta costata"
    assert fetch.calls, "the GBIF transport was actually exercised"

    caller = _GroundingCaller()
    gr = check_name_grounding(
        data=data, image_b64="QUJD", media_type="image/png",
        provider=object(), llm_caller=caller)
    assert gr.performed and not gr.clean
    assert list(gr.unverified) == ["Callimitra rebus"]
    assert caller.calls, "the second vision call was actually exercised"

    flagged = flag_unverified_rows(data, gr.unverified)
    assert flagged.get("species_ranges") == 1
    assert "name_not_in_figure" in str(
        data["species_ranges"][1].get("_warning"))


# ===========================================================================
# 3. REAL ORCHESTRATION (AST-extracted, no PySide6 import)
# ===========================================================================
def test_pure_orchestration_composes_all_three_capabilities_offline():
    ns = _load_pure()
    fetch = _GbifFetch()
    caller = _GroundingCaller()
    payload = ns["_run_name_capabilities"](
        data=_data(), image_b64="QUJD", provider=object(),
        verify_enabled=True, grounding_enabled=True,
        name_fetch=fetch, llm_caller=caller,
        verify_budget=5.0, grounding_timeout=5)

    assert payload["candidates"] == ["Calocycletta costata", "Callimitra rebus"]
    assert any(i.get("msg_key") == "names.fuzzy" for i in payload["issues"])
    assert payload["grounding"]["status"] == "verified"
    assert payload["grounding"]["unverified"] == ["Callimitra rebus"]
    assert payload["extra_model_calls"] == 1
    assert not payload["errors"]
    assert fetch.calls and caller.calls

    data = _data()
    summary = ns["_apply_name_capability_payload"](data, payload)
    assert summary["candidates"] == 2 and summary["fuzzy"] >= 1
    assert summary["unverified"] == 1
    assert data["name_candidates"] == payload["candidates"]
    assert data["grounding"]["status"] == "verified"
    assert data["grounding_rows_flagged"].get("species_ranges") == 1


def test_not_performed_is_reported_as_unknown_never_as_clean():
    ns = _load_pure()
    payload = ns["_run_name_capabilities"](
        data=_data(), image_b64="", provider=None,
        verify_enabled=False, grounding_enabled=True,
        name_fetch=_GbifFetch(), llm_caller=_GroundingCaller(),
        grounding_timeout=5)
    assert payload["grounding"]["status"] == "not_performed"
    assert payload["extra_model_calls"] == 0
    assert payload["warnings"], "a failed check must be VISIBLE"
    assert not payload["grounding"]["unverified"], (
        "not_performed must never be presented as a clean list")


def test_a_raising_verifier_is_contained_in_errors(monkeypatch):
    from rca_core import names as names_mod
    ns = _load_pure()

    def boom(*_a, **_kw):
        raise RuntimeError("offline injected failure")

    monkeypatch.setattr(names_mod, "verify_names", boom)
    payload = ns["_run_name_capabilities"](
        data=_data(), verify_enabled=True, grounding_enabled=False,
        name_fetch=_GbifFetch())
    assert payload["errors"], "a broken GBIF must not break the result"
    assert "verify_names" in payload["errors"][0]
    assert payload["issues"] == []


def test_cancel_stops_both_external_calls():
    ns = _load_pure()
    payload = ns["_run_name_capabilities"](
        data=_data(), verify_enabled=True, grounding_enabled=True,
        should_cancel=lambda: True, name_fetch=_GbifFetch(),
        llm_caller=_GroundingCaller())
    assert payload["verification"] == {} and payload["grounding"] is None
    assert len(payload["warnings"]) == 2
    assert payload["extra_model_calls"] == 0


class _FakeSignal:
    """Minimal stand-in for PySide6.Signal (holds STRONG slot refs)."""

    def __init__(self, *_types):
        self._slots: list = []

    def connect(self, slot):
        self._slots.append(slot)

    def disconnect(self):
        self._slots.clear()

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class _FakeQThread:
    def __init__(self, *_a, **_kw):
        pass

    def isRunning(self):
        return False

    def start(self):
        self.run()


def _load_worker_class(pure: dict) -> type:
    """Extract NameCapabilityWorker with a fake Qt base/Signal."""
    cls = _class(_tree(), "NameCapabilityWorker")
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__",
                       names=[ast.alias(name="annotations", asname=None)],
                       level=0),
        cls,
    ], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {
        "__builtins__": __builtins__,
        "QThread": _FakeQThread,
        "Signal": _FakeSignal,
        "log": logging.getLogger("test_fluent_capabilities"),
        "_run_name_capabilities": pure["_run_name_capabilities"],
        "NAME_VERIFY_TIMEOUT_SEC": pure["NAME_VERIFY_TIMEOUT_SEC"],
        "NAME_VERIFY_BUDGET_SEC": pure["NAME_VERIFY_BUDGET_SEC"],
        "NAME_GROUNDING_TIMEOUT_SEC": pure["NAME_GROUNDING_TIMEOUT_SEC"],
    }
    exec(compile(module, "<gui_fluent_worker>", "exec"), ns)  # noqa: S102
    return ns["NameCapabilityWorker"]


def test_worker_emits_and_a_persistent_slot_is_actually_invoked():
    """The Qt weak-ref trap: assert the SLOT RAN, not just that it exists."""
    pure = _load_pure()
    worker_cls = _load_worker_class(pure)
    seen: list = []

    class _PersistentReceiver:
        """A long-lived receiver, like the ExtractPage widget."""

        def on_done(self, worker, payload):
            seen.append((worker, payload))

    receiver = _PersistentReceiver()
    worker = worker_cls(
        data=_data(), image_b64="QUJD", media_type="image/png",
        provider=object(), verify_enabled=True, grounding_enabled=True,
        gen=7, name_fetch=_GbifFetch(), llm_caller=_GroundingCaller())
    # Exactly the production connection shape: BOUND METHOD of a persistent
    # object, and a strong reference held by the caller.
    worker.done.connect(receiver.on_done)
    worker.run()

    assert seen, "槽没有被调用 — the worker's done signal never reached the slot"
    assert len(seen) == 1
    emitted_worker, payload = seen[0]
    assert emitted_worker is worker
    assert worker.gen == 7
    assert payload["candidates"] == ["Calocycletta costata", "Callimitra rebus"]
    assert payload["extra_model_calls"] == 1
    assert not payload["errors"]


def test_worker_never_raises_when_the_kernel_explodes(monkeypatch):
    pure = _load_pure()
    worker_cls = _load_worker_class(pure)

    def boom(**_kw):
        raise RuntimeError("injected kernel failure")

    monkeypatch.setitem(pure, "_run_name_capabilities", boom)
    # run() resolves the global at call time, so patch the class namespace.
    worker_cls.run.__globals__["_run_name_capabilities"] = boom
    seen: list = []
    worker = worker_cls(data={}, verify_enabled=False, grounding_enabled=False)
    worker.done.connect(lambda w, p: seen.append((w, p)))
    worker.run()  # must not raise into Qt
    assert seen and "worker:" in seen[0][1]["errors"][0]


# ===========================================================================
# 4. REAL Qt event loop — authoritative on the CI `qt` job
# ===========================================================================
@pytest.mark.skipif(not _qt_available(),
                    reason="PySide6/qfluentwidgets unavailable; CI qt job runs this")
def test_qt_event_loop_delivers_the_done_signal_to_a_bound_slot():
    """With REAL PySide6, assert the queued slot actually fired."""
    import time

    from PySide6.QtCore import QCoreApplication, QObject
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])  # noqa: F841
    import gui_fluent

    class Recorder(QObject):
        def __init__(self):
            super().__init__()
            self.calls: list = []

        def on_done(self, worker, payload):  # bound method of a QObject
            self.calls.append((worker, payload))

    rec = Recorder()
    worker = gui_fluent.NameCapabilityWorker(
        data=_data(), image_b64="QUJD", media_type="image/png",
        provider=object(), verify_enabled=True, grounding_enabled=True,
        gen=1, name_fetch=_GbifFetch(), llm_caller=_GroundingCaller())
    worker.done.connect(rec.on_done)
    worker.start()
    deadline = time.monotonic() + 15
    while worker.isRunning() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        worker.wait(50)
    for _ in range(10):
        QCoreApplication.processEvents()
        worker.wait(20)

    assert rec.calls, "槽没有被调用 — real Qt slot never received done()"
    assert rec.calls[0][0] is worker
    assert rec.calls[0][1]["extra_model_calls"] == 1
