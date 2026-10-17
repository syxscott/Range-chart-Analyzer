"""Regressions for the 2026-10-17 review of rca_core/matrix.py and modes.py.

Four defects, all of the same species: a value the MODEL produced reached code
that assumed a narrower type than the model actually emits, and the resulting
exception was swallowed by a caller several frames up. The user-visible symptom
was never a traceback -- it was a paid label call plus a geometry pass silently
thrown away, leaving one line in the log.

  P2-4  `legend_codes` called `.get` on every legend entry, while the two guards
        around it (`legend_is_plausible`, `assemble_occurrence_matrix`) filter
        non-dicts first. A mixed list -- the shape models actually return --
        passed plausibility and then exploded one line later.

  P2-1  `rca_core.modes.__all__` exported `CLASSIFY_PROMPT_VERSION`, which has
        never existed (the real name is `CLASSIFICATION_PROMPT_VERSION`).
        `from rca_core.modes import *` raised AttributeError. ruff's F822 sees
        this statically, but only a runtime getattr asserts the real contract,
        so this file walks `__all__` for every mode module.

  P2-3  `render_label_image` returns the INPUT BYTES unchanged on three of its
        four paths (over-scale, resize failure, and the PIL-missing fallback).
        Only the resize path re-encodes as PNG. So a 4000x3000 JPEG scan -- over
        LABEL_SCALE_MAX_EDGE (2400), under the 4000 default max edge, therefore
        passed through untouched -- was declared `image/png` at the call site.
        Strict media-type validation (Anthropic) rejects that with 400, and the
        two-stage path degrades to single_pass for a reason no log line states.
        `guess_media_type` reads the magic number so the caller never has to
        guess; extractor.py:5471 still hardcodes the string and MUST call it.

  P2-4b `_strat_band`'s nested conditional was flagged as a possible precedence
        hazard. This file pins its actual behaviour on every non-dict element
        type so a future refactor cannot silently start calling `.get` on an int.

Everything runs offline: the model is stubbed and the images are synthetic.
"""
from __future__ import annotations

import base64
import importlib

import pytest

from rca_core import matrix as matrix_mod
from rca_core.matrix import guess_media_type, legend_codes, legend_is_plausible

# ---------------------------------------------------------------------------
# P2-4: legend_codes must survive a mixed legend list
# ---------------------------------------------------------------------------

_MIXED_LEGEND = [
    "A",                        # bare string -- a model that answered with codes
    {"code": "B", "class": "extinct"},
    123,                        # a number where an object was expected
    None,
    ["C"],                      # a nested list
    {"class": "no code at all"},
]


def test_legend_codes_does_not_raise_on_mixed_list():
    """The reported crash, verbatim: `AttributeError: 'str' object has no
    attribute 'get'`. Before the fix this raised."""
    # Must not raise -- that IS the assertion.
    codes = legend_codes(_MIXED_LEGEND)
    assert codes == ["B"]


def test_legend_codes_tolerates_every_non_dict_element_type():
    """Each hostile element type on its own, so a failure names the type that
    broke rather than just 'the mixed list'."""
    for bad in ("A", 123, None, 1.5, True, ["A"], {"a"}, object()):
        codes = legend_codes([bad, {"code": "B"}])
        assert codes == ["B"], f"failed on {bad!r}"


def test_legend_codes_still_reads_the_dict_entries():
    """The fix must not become 'give up on the whole list'. A mixed legend is
    still worth reading: the dict entries carry the only usable codes."""
    assert legend_codes([
        {"code": "X", "class": "range"},
        "junk",
        {"code": "Y", "class": "chance"},
    ]) == ["X", "Y"]


def test_legend_codes_keeps_first_character_only():
    """Pre-existing contract, pinned so the isinstance filter did not change it:
    a code is ONE character, and duplicates collapse."""
    assert legend_codes([
        {"code": "triangle"}, {"code": "circle"}, {"code": "tri"},
    ]) == ["t", "c"]


def test_mixed_legend_reaches_the_prompt_instead_of_exploding():
    """The end-to-end consequence. `extract_matrix_two_stage` calls
    `legend_codes` at line 748 to build the closed-vocabulary cell prompt; an
    exception there propagates to extractor.py:5436's `except`, which rewrites
    the run to single_pass and logs only 'grid_read failed'. So the contract
    that matters is: a plausible-but-mixed legend must not abort the path."""
    legend = [{"code": "A", "class": "present"}, "A", None, 7]
    assert legend_is_plausible(legend) is None, "plausibility must still pass"
    # ...and the code extraction that follows it must not raise.
    assert legend_codes(legend) == ["A"]


def test_legend_codes_on_empty_and_none():
    """A missing legend is a documented case (one run returned three classes
    with null codes); it must yield the empty list, not raise."""
    assert legend_codes([]) == []
    assert legend_codes(None) == []


# ---------------------------------------------------------------------------
# P2-3: guess_media_type must read the bytes, not trust the caller
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# A minimal valid 1x1 PNG (real header, real IHDR/IEND chunk structure).
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)
# A minimal JPEG: SOI + APP0/JFIF header + EOI. Enough for magic-number sniffing.
_JPEG = bytes([
    0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00,
    0xFF, 0xD9,
])


def test_guess_media_type_png():
    assert guess_media_type(_b64(_PNG_1X1)) == "image/png"


def test_guess_media_type_jpeg():
    assert guess_media_type(_b64(_JPEG)) == "image/jpeg"


def test_guess_media_type_strips_data_url_prefix():
    """`_gray_array` accepts a data-URL prefix (matrix.py:193), so
    `guess_media_type` must too -- otherwise the same image is detected in one
    helper and not the other."""
    assert guess_media_type("data:image/jpeg;base64," + _b64(_JPEG)) == "image/jpeg"
    assert guess_media_type("data:image/png;base64," + _b64(_PNG_1X1)) == "image/png"


def test_guess_media_type_defaults_to_png_on_garbage():
    """Unrecognisable bytes fall back to image/png: the status quo, and the
    conservative choice (a wrong 'image/jpeg' on a PNG would break the call just
    as surely as the current bug)."""
    assert guess_media_type(_b64(b"not an image at all")) == "image/png"
    assert guess_media_type("") == "image/png"
    assert guess_media_type("!!!not base64!!!") == "image/png"


def test_guess_media_type_detects_real_jpeg_bytes():
    """Not a hand-written magic number: encode an actual JPEG with PIL and check
    the sniffer agrees. This is the case that matters -- the label image handed
    to the model IS a real JPEG scan in production."""
    pil = pytest.importorskip("PIL")
    import io

    buf = io.BytesIO()
    pil.Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="JPEG")
    assert guess_media_type(_b64(buf.getvalue())) == "image/jpeg"

    buf = io.BytesIO()
    pil.Image.new("RGB", (8, 8), (30, 30, 200)).save(buf, format="PNG")
    assert guess_media_type(_b64(buf.getvalue())) == "image/png"


def test_guess_media_type_is_consistent_with_render_label_image():
    """The bug in one assertion. `render_label_image` returns the input
    unchanged when the figure is over LABEL_SCALE_MAX_EDGE, so whatever the
    caller passed in comes back out with its original format -- and the call
    site declared it PNG regardless. After the fix the declared type must match
    the bytes for BOTH branches."""
    pil = pytest.importorskip("PIL")
    import io

    from rca_core.matrix import LABEL_SCALE_MAX_EDGE

    # A JPEG wide enough to take the pass-through branch (>= 2400px).
    wide_jpeg = io.BytesIO()
    pil.Image.new("RGB", (LABEL_SCALE_MAX_EDGE + 10, 100),
                  (10, 10, 10)).save(wide_jpeg, format="JPEG")
    b64 = _b64(wide_jpeg.getvalue())

    out = matrix_mod.render_label_image(b64)
    assert out == b64, "over-scale must still short-circuit (perf contract)"
    # ...and the type we would declare for it is JPEG, not PNG.
    assert guess_media_type(out) == "image/jpeg"


# ---------------------------------------------------------------------------
# P2-1: every name in __all__ must actually exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", [
    "rca_core.modes",
    "rca_core.capabilities",
    "rca_core.names",
    "rca_core.grid",
])
def test_every_name_in_dunder_all_resolves(module_name):
    """The runtime contract behind ruff's F822.

    `from <mod> import *` walks `__all__` and raises AttributeError on the first
    name the module does not define. A static check can tell you a name is
    undefined; only this tells you the module is actually importable that way,
    which is how other modules consume it.

    (``rca_core.matrix`` is deliberately absent: it declares no `__all__`, so
    star-import from it already exposes exactly what is public.)
    """
    mod = importlib.import_module(module_name)
    missing = [name for name in mod.__all__ if not hasattr(mod, name)]
    assert not missing, (
        f"{module_name}.__all__ exports names that do not exist: {missing}"
    )


def test_modes_star_import_actually_works():
    """Execute the star-import for real, in a namespace of its own, so the
    failure mode is the one a caller would hit."""
    mod = importlib.import_module("rca_core.modes")
    namespace: dict = {}
    exec("from rca_core.modes import *", namespace)  # noqa: S102
    for name in mod.__all__:
        assert name in namespace, f"star import did not bind {name}"


def test_modes_classification_prompt_version_is_the_exported_name():
    """Pin the specific fix: the exported name resolves, and it is the one
    PROMPT_VERSIONS is keyed by -- an alias would keep the name alive while
    leaving the version bump writing to a different string."""
    mod = importlib.import_module("rca_core.modes")
    assert "CLASSIFICATION_PROMPT_VERSION" in mod.__all__
    assert not hasattr(mod, "CLASSIFY_PROMPT_VERSION"), (
        "the dead name must be gone, not merely shadowed"
    )
    assert mod.PROMPT_VERSIONS["chart_classify"] == mod.CLASSIFICATION_PROMPT_VERSION


# ---------------------------------------------------------------------------
# P2-4b: _strat_band's nested conditional, pinned on every element type
# ---------------------------------------------------------------------------


def test_strat_band_columns_tolerates_every_element_type():
    """The flagged line reads:

        v = _text(item if isinstance(item, str)
                  else (item or {}).get("label")
                  if isinstance(item, dict) else "")

    which parses as `item if isinstance(item, str)
                        else ((item or {}).get("label") if isinstance(item, dict) else "")`.

    The `isinstance(item, dict)` guard sits on the OUTER else, so an int or a
    None reaches the empty-string arm and never reaches `.get`. Verified: no
    crash for any element type, and non-dicts are dropped rather than guessed at.
    This test exists to keep that true -- it is NOT a fix, the line is unchanged.
    """
    from rca_core.matrix import _strat_band

    cols = [{"label": "A1"}, {"label": "A2"}]
    for item in ["A1", {"label": "A2"}, 123, None, 1.5, True, ["x"],
                 {"other": "A1"}, {"label": "ZZ"}, object()]:
        band = _strat_band({"label": "b", "columns": [item]}, cols)
        assert band["label"] == "b"
        assert isinstance(band["values"], list)
        assert set(band["values"]) <= {"A1", "A2"}


def test_strat_band_reads_the_recognised_shapes():
    """The two shapes it is SUPPOSED to read, so the tolerance above is not
    mistaken for 'it reads everything'."""
    from rca_core.matrix import _strat_band

    cols = [{"label": "A1"}, {"label": "A2"}, {"label": "A3"}]
    band = _strat_band(
        {"label": "conodont", "columns": ["A1", {"label": "A2"}, 123, "A3", "A3"]},
        cols,
    )
    assert band == {"label": "conodont", "values": ["A1", "A2", "A3"]}
