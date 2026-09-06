"""Series-label alias tests for rock-unit terms (Sprint B 2026-09-05).

English "Upper Permian" / "Lower Jurassic" and Chinese 上X统/下X统 labels
are ubiquitous on published range charts. Before Sprint B they silently
degraded to period-level bounds; these tests pin them to series precision.
"""

from rca_core.standards.ics import ics_age_range_bounds, ics_resolve_age_bound


def test_upper_permian_resolves_to_lopingian():
    name, ma = ics_resolve_age_bound("Upper Permian")
    assert name == "Lopingian"
    older, younger = ics_age_range_bounds("Upper Permian")
    assert older == 259.51
    assert abs(younger - 251.902) < 1e-9


def test_lower_jurassic_resolves_to_hettangian_base():
    older, younger = ics_age_range_bounds("Lower Jurassic")
    assert older == 201.4
    assert abs(younger - 174.7) < 1e-9


def test_upper_cambrian_matches_furongian():
    assert ics_age_range_bounds("upper Cambrian") == ics_age_range_bounds("Furongian")


def test_lower_carboniferous_is_mississippian():
    name, _ = ics_resolve_age_bound("lower carboniferous")
    assert name == "Mississippian"


def test_cn_tong_forms_resolve_like_shi_forms():
    for tong, shi in [
        ("上二叠统", "晚二叠世"),
        ("下三叠统", "早三叠世"),
        ("上白垩统", "晚白垩世"),
        ("下寒武统", "早寒武世"),
    ]:
        assert ics_age_range_bounds(tong) == ics_age_range_bounds(shi), tong


def test_series_still_wins_over_period_fallback():
    # "Upper Permian" must not degrade to the Permian period base (298.9).
    older, _ = ics_age_range_bounds("Upper Permian")
    assert older < 298.9
