"""T9 + T10: provider drag-to-reorder + LlmProvider dataclass tests."""
from __future__ import annotations

import os, sys

root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, root)

_pass = 0
_fail = 0


def check(name, cond, msg=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name, msg)


# --- T10: LlmProvider dataclass coercion ---
def test_llmprovider_coercion():
    from rca_core.llm import LlmProvider, ApiFormat
    p = LlmProvider(api_format="openai")
    check("provider-coerce-str", p.api_format == ApiFormat.OPENAI)
    p2 = LlmProvider(api_format="GEMINI")
    check("provider-coerce-upper-fallback", p2.api_format == ApiFormat.ANTHROPIC)
    p3 = LlmProvider(api_format="INVALID")
    check("provider-coerce-invalid-fallback", p3.api_format == ApiFormat.ANTHROPIC)


def test_llmprovider_roundtrip():
    """to_dict → from_dict must be lossless."""
    from rca_core.llm import LlmProvider, ApiFormat
    p = LlmProvider(id="abc", name="Test", api_format=ApiFormat.GEMINI,
                    endpoint="https://x.io/v1", api_key="sk", model="m",
                    extra_headers={"X-Custom": "y"}, extra_body={"t": 0.1},
                    is_current=True, created_at=123.0, sort_index=2)
    d = p.to_dict()
    p2 = LlmProvider.from_dict(d)
    check("roundtrip-id", p2.id == "abc")
    check("roundtrip-format", p2.api_format == ApiFormat.GEMINI)
    check("roundtrip-endpoint", p2.endpoint == "https://x.io/v1")
    check("roundtrip-extra-headers", p2.extra_headers == {"X-Custom": "y"})
    check("roundtrip-extra-body", p2.extra_body == {"t": 0.1})
    check("roundtrip-is-current", p2.is_current is True)


# --- T9: ProviderDragList drop-to-reorder (headless) ---
def test_draglist_order_changed():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    from gui_fluent_providers import ProviderDragList, ProviderCard
    from rca_core import LlmProvider, ApiFormat

    store_ids = []
    def make_card(i):
        p = LlmProvider(id=f"prov-%d" % i, name="P%d" % i,
                        api_format=ApiFormat.ANTHROPIC, endpoint="https://x.io",
                        api_key="k", model="m")
        return ProviderCard(p, (i == 0), lambda k: k)

    lst = ProviderDragList()
    cards = [make_card(i) for i in range(3)]
    lst.set_cards(cards)

    # Capture the orderChanged signal.
    emitted = []
    lst.orderChanged.connect(lambda ids: emitted.append(ids))

    # Simulate drop: card 2 (prov-2) dropped onto card 0 (prov-0).
    from PySide6.QtCore import QMimeData, Qt, QPoint
    from PySide6.QtGui import QDropEvent

    mime = QMimeData()
    mime.setText("prov-2")
    # Drop at y-coordinate of card 0's location.
    target_pos = cards[0].geometry().topLeft()
    ev = QDropEvent(target_pos, Qt.MoveAction, mime, Qt.LeftButton,
                    Qt.NoModifier, QDropEvent.Drop)
    lst.dropEvent(ev)

    if emitted:
        order = emitted[-1]
        # prov-2 should now be in position 0 (or wherever card 0 was).
        check("drag-order-changed-emits", "prov-2" in order)
        check("drag-preserves-all-3", len(order) == 3 and set(order) == {"prov-0", "prov-1", "prov-2"})
    else:
        check("drag-order-changed-emits", False, "no orderChanged emitted")


if __name__ == "__main__":
    test_llmprovider_coercion()
    test_llmprovider_roundtrip()
    test_draglist_order_changed()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(0 if _fail == 0 else 1)
