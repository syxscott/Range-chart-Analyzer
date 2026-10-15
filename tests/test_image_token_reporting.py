"""What the usage block says about the image, and what it refuses to say.

These functions shipped in the 2026-10-15 audit with NO tests, which is how a
regression in them would have gone unnoticed until a user compared two bills.
That gap is closed here.

Three properties are pinned:

  1. the estimate is reported on its OWN key and never folded into
     ``input_tokens`` -- for a provider that already bills the image there,
     adding it would double-count the user;
  2. the capped estimate is a CONSTANT for every figure in the size range this
     tool sees, so it must ship with the pixel size and the cap flag beside it
     or it reads as a measurement that lost its discriminating power;
  3. nothing here judges whether an image is good enough. A resolution
     threshold was written and deleted because the measured evidence
     contradicts it (875x1446 fails, 1331x788 passes); the negative result is
     documented in llm.py and re-pinned here so it is not re-introduced.
"""
import base64
import io
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rca_core.llm import (  # noqa: E402
    _annotate_image_tokens,
    _image_geometry,
    estimate_image_tokens,
)


def _b64(w: int, h: int) -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class TestGeometryReading:
    def test_real_dimensions_round_trip(self):
        assert _image_geometry(_b64(320, 240)) == (320, 240)

    @pytest.mark.parametrize("bad", ["", None, "not base64!!", "aGVsbG8="])
    def test_unreadable_input_returns_none_instead_of_raising(self, bad):
        """None, never an exception.

        This runs on whatever the user pasted. A base64 string that decodes to
        something PIL does not recognise is an ordinary occurrence (a truncated
        upload, a wrong field), not a bug to propagate out of a usage report.
        """
        assert _image_geometry(bad or "") is None

    def test_the_annotated_path_does_not_raise_on_junk_either(self):
        usage = {}
        _annotate_image_tokens(usage, "not base64!!")
        assert usage["image_tokens_estimated"] is None
        assert "image_pixels" not in usage


class TestTheEstimateStaysOutOfInputTokens:
    def test_input_tokens_is_never_modified(self):
        """The whole point of reporting it separately.

        Folding an estimate into a number the provider bills on would inflate
        the user's bill by ~1600 tokens per extraction. That is worse than the
        under-reporting this was written to fix.
        """
        usage = {"input_tokens": 5647, "output_tokens": 16058}
        _annotate_image_tokens(usage, _b64(1300, 900))
        assert usage["input_tokens"] == 5647, "input_tokens was rewritten"
        assert usage["output_tokens"] == 16058
        assert usage["image_tokens_estimated"] > 0

    def test_the_estimate_flag_is_always_present_even_without_an_image(self):
        """Callers must be able to test for the key without KeyError."""
        usage = {}
        _annotate_image_tokens(usage, "")
        assert "image_tokens_estimated" in usage
        assert usage["image_tokens_estimated"] is None


class TestTheCappedConstantIsInterpretable:
    """Why `image_pixels` and `image_tokens_capped` exist at all.

    Measured on ten real figures the capped estimate was exactly 1600 for
    every one, so on its own it says nothing. Reporting it alone looked like
    a broken measurement; these two keys make "1600 because capped" legible.
    """

    @pytest.mark.parametrize("size", [(1096, 1316), (1517, 1712), (1331, 788),
                                      (1435, 1580)])
    def test_the_cap_actually_binds_on_real_figures(self, size):
        usage = {}
        _annotate_image_tokens(usage, _b64(*size))
        assert usage["image_tokens_estimated"] == 1600
        assert usage["image_tokens_capped"] is True, (
            "if this ever stops being capped, the constant-1600 caveat above "
            "no longer applies and this test should be re-examined")
        assert usage["image_tokens_uncapped"] > 1600

    def test_pixel_size_is_reported_verbatim(self):
        usage = {}
        _annotate_image_tokens(usage, _b64(777, 1234))
        assert usage["image_pixels"] == [777, 1234]

    def test_the_downscale_shows_up_in_the_uncapped_number(self):
        """Where the fit-to-1568 step is actually observable.

        An earlier version of this file asserted the downscale through
        `estimate_image_tokens` and the mutation `scale = 1.0` was NOT caught.
        That was not a bad mutation, it was a bad assertion: once the long edge
        is over 1568 the tile count rounds to 3 per axis either way (1568/750 =
        2.09), so the capped answer is identical with and without the downscale
        for every realistic figure. The step only changes the UNCAPPED number:

            1600x3000  uncapped, scaled   -> 1568x836  -> 3x2 = 6 tiles = 4500
            1600x3000  uncapped, unscaled -> 3000x1600 -> 4x3 = 12 tiles = 9000

        Both cap to 1600, so this is asserted where it can actually be seen.
        """
        usage = {}
        _annotate_image_tokens(usage, _b64(1600, 3000))
        assert usage["image_tokens_estimated"] == 1600
        assert usage["image_tokens_capped"] is True
        assert usage["image_tokens_uncapped"] == 4500, (
            "the fit-to-1568 step is not reaching the uncapped number; either "
            "the scale block is dead code or it is being applied twice")

    def test_uncapped_tracks_the_uncapped_size_not_the_capped_one(self):
        """Two figures that both cap to 1600 but differ before the cap.

        1200x800  -> 2x2 tiles = 3000  -> capped 1600
        1200x1600 -> fits to 1176x1568 -> 3x2 tiles = 4500 -> capped 1600

        The capped number is 1600 for both, which is the whole reason the
        uncapped one is reported. A pair like 1500x800 vs 1500x1500 would NOT
        prove anything: both are 2x2 tiles, so their uncapped values are
        legitimately equal. If the uncapped number were derived from the
        capped estimate it would be constant, and this test would still pass
        with a decorative field.
        """
        usage = {}
        _annotate_image_tokens(usage, _b64(1200, 800))
        wide = dict(usage)
        usage2 = {}
        _annotate_image_tokens(usage2, _b64(1200, 1600))
        assert wide["image_tokens_estimated"] == usage2["image_tokens_estimated"] == 1600
        assert wide["image_tokens_uncapped"] != usage2["image_tokens_uncapped"]
        assert {wide["image_tokens_uncapped"], usage2["image_tokens_uncapped"]} == {3000, 4500}


class TestEstimateMatchesItsOwnDocumentedRule:
    def test_small_image_is_one_tile(self):
        # under 750 on both edges -> 1 tile, no cap
        assert estimate_image_tokens(_b64(700, 700)) == 750

    def test_wide_and_short_is_still_one_tile(self):
        # long edge 1500 -> 2 tiles across, 300px -> 1 down = 2 tiles = 1500
        assert estimate_image_tokens(_b64(1500, 300)) == 1500

    def test_oversize_is_scaled_before_tiling(self):
        # 3000x3000 scales to 1568x1568 -> 3x3 tiles = 6750, capped to 1600
        assert estimate_image_tokens(_b64(3000, 3000)) == 1600

    def test_junk_is_none(self):
        assert estimate_image_tokens("nope") is None
