"""UsageSummary.by_day attributes every request to the day it HAPPENED.

AUDIT-2026-10-02 [item 10.1]

The three tests that used to cover this all recomputed the implementation's
own formula to produce their expected value::

    majority  = (ts // 86400) * 86400 + off + 43200
    expected  = (majority // 86400) * 86400 - off        # tests_bugfixes.py:100
    majority  = (utc_ts // 86400) * 86400 + off + 43200  # ..._07_31_core.py:111
    key       = (majority // 86400) * 86400 - off        # ..._07_31_core.py:152
    majority  = day + off + 43200                        # test_audit_2026_09_27.py:603

so none of them could fail on a change to the formula: they assert that the
code equals itself. Worse, the east-offset test's docstring states the
invariant that the value it accepts violates --

    "Rows at local 00:00-08:00 (UTC+8) must land on the SAME local day,
     not the previous one"                                  # ..._07_31_core.py:74

and the row it plants (UTC 18:00 = Beijing 08-01 02:00) is accepted into a
key that resolves to 07-31.

Measured over every hour of one UTC day, planting ONE row per hour and
resolving the bucket the way the only consumer does
(``time.strftime("%m-%d", time.localtime(d["day"]))``,
gui_fluent_pages.py:790), the old rule filed this many of the 24 hours under
a date the call was not made on:

    UTC+13  11/24      UTC+9   9/24      UTC+8   8/24 (local 00:00-08:00)
    UTC+5.5  5/24      UTC+1   1/24      UTC+0   0/24
    UTC-5    5/24      UTC-8    8/24

The AUDIT-2026-09-27 P2 note that ships with the code says the offset was
"counted twice ... every bar west of UTC was labelled a day early" and that
"the author's own UTC+8 zone landed on the right date anyway". The new rule
inverted which hemisphere is wrong rather than fixing either: it is now wrong
at the *east* end of the UTC day instead of the west end. Both were invisible
because the tests restated the formula.

The rule is "file each request under the local calendar day it was made on".
A per-day cost figure gets reconciled against an invoice, and a call made at
00:03 belongs to today. Nothing else in the module is affected: the
aggregation identities below are unaffected and are asserted here too, because
a by_day fix that lost a row would still satisfy the day test.

Measured on the same module and found clean, so nobody re-derives it:

* ``estimate_tokens`` is monotone under appending characters. A first probe
  here reported the opposite; it was comparing siblings (``base + "abcd"``
  against ``base + "x"``) rather than a chain, which is not a monotonicity
  violation -- ``latin // 4`` and ``other // 4`` are both non-decreasing in
  their counters and each character is classified independently. The
  ``max(1, other) // 4`` in that function is dead arithmetic (``max(1, x)
  // 4 == x // 4`` for every x >= 0, verified over 0..4999) but harmless.
* Every breakdown sums back to ``total_requests`` and to
  ``total_input_tokens + total_output_tokens`` -- by_day, by_provider and
  by_model alike, with no row lost or double counted.
* ``success_count <= rated_requests <= total_requests`` holds for a mix of
  2xx / 3xx / 1xx / 5xx / NULL, and both rates stay in [0, 1].
* ``parse_anthropic_usage`` returns None for a usage block carrying only
  cache tokens (the ``inp == 0 and out == 0`` guard). No provider emits
  ``input_tokens == 0`` next to a non-zero cache count, so this is
  unreachable rather than wrong.
* ``usage_from_text`` returns all-zero for any ``side`` other than exactly
  ``"in"``/``"out"`` -- but it has no call site in the repository.
* ``total_cost_usd`` is a real column, is written and read back into
  ``UsageRecord``, and is never aggregated into ``UsageSummary`` nor
  populated by any caller. A missing feature, not a defect.
"""

from __future__ import annotations

import time as _time

import pytest

from rca_core.db import Database
from rca_core.usage import UsageRecord, UsageStore

# 2026-07-31 00:00 UTC -- a real day, mid-range for every era we care about.
DAY0 = 1785456000

# Spread over both hemispheres, both DST-sign conventions, whole and half
# hours, and the extremes: UTC+14 (Kiritimati) .. UTC-11 (Pacific/Midway).
OFFSETS = [
    50400,   # UTC+14
    43200,   # UTC+12
    39600,   # UTC+11
    36000,   # UTC+10
    32400,   # UTC+9  (Asia/Tokyo, Asia/Seoul)
    28800,   # UTC+8  (Asia/Shanghai) -- the author's own zone
    19800,   # UTC+5.5 (Asia/Kolkata, Asia/Kathmandu) -- half hour
    3600,    # UTC+1
    0,       # UTC+0  (CI runs here; it is the one offset that was always right)
    -18000,  # UTC-5  (America/New_York)
    -28800,  # UTC-8  (America/Los_Angeles)
    -39600,  # UTC-11 (Pacific/Midway)
]


def _local_date(epoch: float, off: int) -> tuple[int, int, int]:
    """Calendar date the clock shows at ``epoch`` in a zone with offset ``off``.

    Deliberately ``gmtime(epoch + off)`` and NOT ``localtime`` -- the point of
    these tests is to have an oracle that shares no code with the code under
    test, and ``localtime`` is exactly what the code under test (and the
    existing tests) monkeypatch.
    """
    return _time.gmtime(epoch + off)[:3]


@pytest.fixture
def fixed_zone(monkeypatch):
    """Install a zone with a constant offset and yield the installer.

    ``usage.summary`` does ``import time as _time`` then reads
    ``_time.localtime(ts).tm_gmtoff``; patching the attribute on the module
    object is therefore enough to move the whole process into another zone,
    and it is undone automatically by monkeypatch.
    """

    def _install(off: int):
        class _Fake:
            tm_gmtoff = off

            def __call__(self, ts):
                return self

        monkeypatch.setattr(_time, "localtime", _Fake())

    return _install


def _store(tmp_path, name="u.db"):
    return UsageStore(db=Database(path=str(tmp_path / name)))


# ---------------------------------------------------------------------------
# The property: the bar a request is drawn under is the day it was made on.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("off", OFFSETS)
def test_a_request_is_charted_on_the_day_it_was_made(fixed_zone, tmp_path, off):
    """One request in each hour of a UTC day; the buckets must be exactly the
    local calendar dates those hours fall on -- no more, no fewer, and one
    bucket per local date (a UTC day is not a local day, so it is expected to
    split at some non-zero offset)."""
    fixed_zone(off)
    store = _store(tmp_path)
    for hour in range(24):
        store.record(UsageRecord(
            timestamp=DAY0 + hour * 3600, provider_id="p", provider_name="p",
            model="m", input_tokens=100, output_tokens=50,
        ))

    s = store.summary()
    got: dict[tuple[int, int, int], int] = {}
    for d in s.by_day:
        date = _local_date(d["day"], off)
        assert date not in got, (
            f"UTC{off / 3600:+g}h: two buckets resolve to the same local date "
            f"{date} -- a local day was split"
        )
        got[date] = d["count"]
    want: dict[tuple[int, int, int], int] = {}
    for hour in range(24):
        date = _local_date(DAY0 + hour * 3600, off)
        want[date] = want.get(date, 0) + 1
    assert got == want, (
        f"UTC{off / 3600:+g}h: buckets {got} != the local dates the calls were "
        f"made on {want}"
    )
    assert sum(got.values()) == 24


@pytest.mark.parametrize("off", OFFSETS)
def test_a_single_call_is_charted_on_its_own_calendar_day(fixed_zone, tmp_path, off):
    """The everyday shape, one request at a time: what day its bar is drawn
    under is the day the user made the call. At UTC+8 that is the difference
    between 00:30 and 09:30 on consecutive local dates."""
    fixed_zone(off)
    for hour in (0, 6, 9, 12, 18, 23):
        store = _store(tmp_path, f"u{hour}.db")
        store.record(UsageRecord(
            timestamp=DAY0 + hour * 3600, provider_id="p", provider_name="p",
            model="m", input_tokens=100, output_tokens=50,
        ))
        s = store.summary()
        assert len(s.by_day) == 1, (off, hour, s.by_day)
        got = _local_date(s.by_day[0]["day"], off)
        want = _local_date(DAY0 + hour * 3600, off)
        assert got == want, (
            f"UTC{off / 3600:+g}h, request at UTC hour {hour:02d}: bar drawn "
            f"under {got}, call was made on {want}"
        )


@pytest.mark.parametrize("off", OFFSETS)
def test_late_utc_rows_escape_the_utc_day_they_sit_in(fixed_zone, tmp_path, off):
    """A UTC day is not a local day: east of UTC its last hours are tomorrow.

    This is the specific shape the majority-of-the-UTC-day rule cannot
    represent, and it is the everyday one -- an evening's work in Shanghai is
    the following calendar day's early hours in UTC.
    """
    fixed_zone(off)
    store = _store(tmp_path)
    for day in range(3):
        for hour in (0, 12, 18, 23):
            store.record(UsageRecord(
                timestamp=DAY0 + day * 86400 + hour * 3600,
                provider_id="p", provider_name="p", model="m",
                input_tokens=10, output_tokens=5,
            ))
    s = store.summary()
    by_date: dict[tuple[int, int, int], int] = {}
    for d in s.by_day:
        date = _local_date(d["day"], off)
        by_date[date] = by_date.get(date, 0) + d["count"]
    # Every planted hour, bucketed by its own local date.
    want: dict[tuple[int, int, int], int] = {}
    for day in range(3):
        for hour in (0, 12, 18, 23):
            date = _local_date(DAY0 + day * 86400 + hour * 3600, off)
            want[date] = want.get(date, 0) + 1
    assert by_date == want, (off, by_date, want)


# ---------------------------------------------------------------------------
# The invariant the consumer actually depends on, kept from the old tests.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("off", OFFSETS)
def test_the_key_is_local_midnight(fixed_zone, tmp_path, off):
    """gui_fluent_pages.py:790 re-localises the key, so key + offset must be a
    UTC midnight -- that is what "00:00 local" means as an instant."""
    fixed_zone(off)
    store = _store(tmp_path)
    for day in range(2):
        store.record(UsageRecord(
            timestamp=DAY0 + day * 86400 + 5 * 3600,
            provider_id="p", provider_name="p", model="m",
            input_tokens=1, output_tokens=1,
        ))
    for d in store.summary().by_day:
        assert (d["day"] + off) % 86400 == 0, (off, d["day"])


# ---------------------------------------------------------------------------
# Aggregation identities -- a by_day fix that dropped or double-counted a row
# would still pass the day tests above.
# ---------------------------------------------------------------------------

def test_every_request_appears_exactly_once_in_every_breakdown(tmp_path):
    store = _store(tmp_path)
    codes = [200, 204, 299, 500, 404, None, None, 301, 199, 300]
    for i, code in enumerate(codes):
        store.record(UsageRecord(
            timestamp=DAY0 + i * 3600, provider_id="p", provider_name="p",
            model="m", input_tokens=10, output_tokens=5,
            cache_read_tokens=7, cache_creation_tokens=3, status_code=code,
        ))
    s = store.summary()
    assert s.total_requests == len(codes)
    assert sum(d["count"] for d in s.by_day) == s.total_requests
    assert sum(d["count"] for d in s.by_provider) == s.total_requests
    assert sum(d["count"] for d in s.by_model) == s.total_requests
    want_tokens = sum(10 + 5 for _ in codes)
    assert sum(d["tokens"] for d in s.by_day) == want_tokens
    assert sum(d["tokens"] for d in s.by_provider) == want_tokens
    assert sum(d["tokens"] for d in s.by_model) == want_tokens
    assert s.total_input_tokens + s.total_output_tokens == want_tokens


def test_day_keys_are_distinct_and_ascending(tmp_path):
    store = _store(tmp_path)
    for day in range(5):
        for hour in range(0, 24, 3):
            store.record(UsageRecord(
                timestamp=DAY0 + day * 86400 + hour * 3600,
                provider_id="p", provider_name="p", model="m",
                input_tokens=1, output_tokens=1,
            ))
    keys = [d["day"] for d in store.summary().by_day]
    assert len(keys) == len(set(keys))
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# The rate ladder: REVIEW-2026-09-20 finding 11 moved the success-rate
# denominator off total_requests onto rated_requests.
# ---------------------------------------------------------------------------

def test_success_count_is_between_zero_and_rated_which_is_between_zero_and_total(tmp_path):
    store = _store(tmp_path)
    codes = [200, 204, 299, 500, 404, None, None, 301, 199, 300]
    for i, code in enumerate(codes):
        store.record(UsageRecord(
            timestamp=DAY0 + i * 60, provider_id="p", provider_name="p",
            model="m", input_tokens=10, output_tokens=5, status_code=code,
        ))
    s = store.summary()
    # 2xx only: 200/204/299.  301 and 300 are not successes, 199 is not a
    # success, and NULL is unrated rather than failed.
    assert s.success_count == 3
    assert s.rated_requests == 8          # 10 rows minus the 2 NULLs
    assert s.total_requests == 10
    assert 0.0 <= s.success_rate <= 1.0
    assert s.success_count <= s.rated_requests <= s.total_requests
    assert s.success_rate == pytest.approx(3 / 8)


def test_rates_stay_in_range_for_a_window_of_nothing(tmp_path):
    s = _store(tmp_path).summary()
    assert s.total_requests == 0
    assert s.rated_requests == 0
    assert s.success_rate == 0.0
    assert s.cache_hit_rate == 0.0
    assert s.by_day == [] and s.by_provider == [] and s.by_model == []


def test_cache_hit_rate_is_a_fraction(tmp_path):
    store = _store(tmp_path)
    for i in range(4):
        store.record(UsageRecord(
            timestamp=DAY0 + i * 3600, provider_id="p", provider_name="p",
            model="m", input_tokens=10, output_tokens=0,
            cache_read_tokens=30, cache_creation_tokens=0,
        ))
    s = store.summary()
    # cache_read / (input + cache_creation + cache_read)
    assert s.cache_hit_rate == pytest.approx(120 / 160)
    assert 0.0 <= s.cache_hit_rate <= 1.0
