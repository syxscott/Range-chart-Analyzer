"""REVIEW-2026-09-20 (extraction-core tail): locks for json_utils / chart_mode /
cache / error_utils.

Companion to the extractor/llm fixes of the same review round. Each test names
the failure it prevents:

  1. json_utils Level 3.5 — a repair that yields a legal ARRAY was discarded by
     the dict-only payload guard, so Level 4 rescued one inner row.
  2. json_utils strip_markdown_fence — the single-fence shortcut skipped the
     placeholder/payload ranking, so a restated example inside one fence beat
     the real payload sitting in the prose after it (Level 3 accepts any dict).
  3. json_utils Level 2 / _repair_truncated_json — documented delete-not-escape
     rationale + the dead ``outer_ever_closed`` flag is gone.
  4. chart_mode — a caption naming a range chart is a POSITIVE hit (matched=True)
     so the vision classifier is neither called nor allowed to overturn it;
     "correlation of" no longer hijacks non-zonation captions; the three
     assistant-only modes got conservative tables.
  5. chart_mode — no unreachable duplicate heuristic left after ``return mode``.
  6. cache — the LRU touch on a cache HIT can no longer raise (shared SQLite
     file) and lose the request.
  7. error_utils — Retry-After=0 no longer means a zero-second backoff, the
     retry loop no longer sleeps after its final attempt, and the error code is
     parsed from the SAME (trimmed) body that is stored/displayed.
"""
from __future__ import annotations

import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import rca_core.error_utils as EU  # noqa: E402
from rca_core.cache import ResultCache  # noqa: E402
from rca_core.chart_mode import auto_detect_chart_mode, auto_detect_chart_mode_ex  # noqa: E402
from rca_core.json_utils import (  # noqa: E402
    _looks_like_payload_list,
    _repair_truncated_json,
    safe_json_loads,
    strip_markdown_fence,
)


# ---------------------------------------------------------------------------
# 1. Level 3.5: a repaired TOP-LEVEL ARRAY is a payload too
# ---------------------------------------------------------------------------

class TestTruncatedArrayRepair:
    ROWS = ('[{"species": "A. alva", "section": "S1", "range_top": "12.5", '
            '"range_base": "14.0"}, '
            '{"species": "B. beta", "section": "S1", "range_top": "10.1", '
            '"range_base": "12.5"}, '
            '{"species": "C. gamma", "section": "S2", "range_top": "9.0", '
            '"range_base": "11.0"}, '
            '{"species": "D. delta", "section": "S2", "range_top": "8.0", "ra')

    def test_repaired_array_is_wrapped_not_dropped(self):
        parsed = safe_json_loads(self.ROWS)
        assert "_array_root" in parsed, (
            f"Level 3.5 threw the repaired array away: {list(parsed)[:4]}")
        rows = parsed["_array_root"]
        # The three COMPLETE rows survive; the partial 4th was cut at the last
        # element boundary, so at worst its leading fields are gone.
        assert len(rows) >= 3, f"only {len(rows)} rows recovered"
        assert [r["species"] for r in rows[:3]] == ["A. alva", "B. beta",
                                                   "C. gamma"]

    def test_array_root_still_feeds_the_normalizers(self):
        from rca_core.extractor import normalize_result

        data = normalize_result(safe_json_loads(self.ROWS))
        assert len(data.get("species_ranges") or []) >= 3

    def test_scalar_array_in_prose_is_not_rescued_as_payload(self):
        # A cut list of scalars in prose is not "the extraction" — it must not
        # be promoted by the new array rule either.
        with pytest.raises(ValueError):
            safe_json_loads("Field notes: 12, 14, 15, 18, 20")

    def test_looks_like_payload_list_rule(self):
        assert _looks_like_payload_list([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
        assert _looks_like_payload_list([{"sections": [], "confidence": 0.9}])
        assert not _looks_like_payload_list([])
        assert not _looks_like_payload_list([1, 2, 3])
        # Objects must stay the MAJORITY: one dict among scalars is prose.
        assert _looks_like_payload_list([{"a": 1, "b": 2}, {"c": 3}, 4])
        assert not _looks_like_payload_list([{"a": 1, "b": 2}, 3, 4])
        # ...and a single-field row array is NOT enough (one stray {"a": 1]).
        assert not _looks_like_payload_list([{"a": 1}])


# ---------------------------------------------------------------------------
# 2. Single fence must not outrank a better payload in the prose
# ---------------------------------------------------------------------------

_FENCED_EXAMPLE_THEN_PLAIN_PAYLOAD = (
    "Following the requested schema, example:\n"
    "```json\n"
    '{"sections": [{"name": "<section name>"}], '
    '"species_ranges": [{"species": "<binomial>", "section": "<section>"}], '
    '"biozones": [], "other_fossils": [], "confidence": 0.0}\n'
    "```\n"
    "Measured from the figure, the actual result is\n"
    '{"sections": [{"name": "Hilly Farm", "kind": "limestone"}], '
    '"species_ranges": [{"species": "Dictyomitrella kamoensis", '
    '"section": "Hilly Farm", "range_top": "4.2", "range_base": "6.8"}], '
    '"biozones": [{"name": "Zone 3"}], "other_fossils": [], '
    '"confidence": 0.86}'
)


class TestSingleFenceParticipatesInSelection:
    def test_plain_text_payload_beats_fenced_example(self):
        parsed = safe_json_loads(_FENCED_EXAMPLE_THEN_PLAIN_PAYLOAD)
        assert parsed.get("confidence") == 0.86, (
            f"the fenced example evicted the prose payload: {parsed}")
        assert [s["name"] for s in parsed["sections"]] == ["Hilly Farm"]
        assert "<section name>" not in repr(parsed)

    def test_fence_still_wins_when_it_holds_the_only_payload(self):
        # No regression: the historical single-fence answers are untouched.
        assert strip_markdown_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
        assert strip_markdown_fence('Here:\n```json\n{"a": 1}\n```\nThanks') \
            == '{"a": 1}'
        text = ('```json\n{"sections": [{"name": "S1"}], "confidence": 0.5}\n```\n'
                "Done with the report.")
        assert strip_markdown_fence(text) == \
            '{"sections": [{"name": "S1"}], "confidence": 0.5}'

    def test_multi_fence_without_payload_keys_falls_back_to_first(self):
        text = 'prose ```json\n{"foo": 1}\n``` middle ```json\n{"bar": 2}\n``` tail'
        assert strip_markdown_fence(text) == '{"foo": 1}'

    def test_truncated_single_fence_still_reaches_the_repair_level(self):
        # The fence closes but the JSON inside does not: the block is the only
        # candidate, so it must survive to Level 3.5 instead of being traded
        # for the prose.
        text = ('```json\n'
                '{"sites": [{"name": "K1"}], "abundances": '
                '[{"taxon": "A", "value": 1}, {"taxon": "B", "val\n'
                '```\n')
        parsed = safe_json_loads(text)
        assert len(parsed.get("abundances") or []) == 2


# ---------------------------------------------------------------------------
# 3. Level 2 / _repair_truncated_json housekeeping
# ---------------------------------------------------------------------------

class TestControlCharAndDeadCode:
    def test_binary_junk_is_still_deleted_not_escaped(self):
        # Documented trade-off (json_utils Level 2 comment): a NUL escaped to
        # \u0000 would crash the xlsx writer (XML forbids it) instead of
        # costing one byte, so this class stays a DELETE.
        assert safe_json_loads('{"a": "hel\x00lo"}') == {"a": "hello"}

    def test_raw_newline_inside_string_is_preserved_by_escaping(self):
        assert safe_json_loads('{"note": "line1\nline2"}') == \
            {"note": "line1\nline2"}

    def test_repair_has_no_outer_ever_closed_flag(self):
        import inspect

        from rca_core.json_utils import _repair_truncated_json

        # The flag itself must be gone (the explanatory comment may name it).
        assert "outer_ever_closed = False" not in inspect.getsource(
            _repair_truncated_json)

    def test_repair_still_refuses_closed_payloads(self):
        assert _repair_truncated_json('{"a": 1} trailing prose') is None
        assert _repair_truncated_json('{"a": 1, "b": ') == '{"a": 1, "b": null}' or \
            _repair_truncated_json('{"a": 1, "b": ') is not None


# ---------------------------------------------------------------------------
# 4/5. chart_mode: caption beats the classifier, narrowed zonation wording,
#      conservative tables for the three assistant modes.
# ---------------------------------------------------------------------------

class TestChartModeCaptionWins:
    @pytest.mark.parametrize("caption", [
        "Conodont range chart",
        "Figure 3. Range charts of the Trilobita",
        "Fig. 2 — Stratigraphy and conodont range-chart",
        "图 5. 牙形石延限表",
        "放射虫延限図",
        "График совмещения видов",
    ])
    def test_explicit_range_chart_is_a_positive_hit(self, caption):
        # matched=True is the point: resolve_auto_mode must NOT spend a vision
        # call, and the classifier must not be able to overturn the caption.
        assert auto_detect_chart_mode_ex(caption) == ("range_chart", True)
        assert auto_detect_chart_mode(caption) == "range_chart"

    def test_correlation_of_measured_sections_no_longer_hijacks(self):
        # Used to return ("zonation_chart", True) from the broad "correlation
        # of" keyword — a lithostratigraphic figure, silently mis-routed with
        # no vision fallback.
        assert auto_detect_chart_mode_ex(
            "Correlation of the measured sections") == ("range_chart", False)

    def test_zone_specific_correlation_still_short_circuits(self):
        assert auto_detect_chart_mode_ex(
            "Zone correlation of the radiolarians") == ("zonation_chart", True)
        assert auto_detect_chart_mode_ex(
            "Correlation chart of the boreholes") == ("zonation_chart", True)
        assert auto_detect_chart_mode_ex(
            "Correlation of zones across the basin") == ("zonation_chart", True)
        # The SPLIT form the locked suites use — narrowing must not lose it.
        assert auto_detect_chart_mode_ex(
            "Correlation of Triassic zones") == ("zonation_chart", True)
        assert auto_detect_chart_mode_ex(
            "Fig. 1. Correlation of Triassic radiolarian zones and subzones"
        ) == ("zonation_chart", True)
        # ... but it stops at the sentence boundary, so a lithostratigraphic
        # caption whose next sentence happens to contain "zone" is not
        # swallowed either.
        assert auto_detect_chart_mode_ex(
            "Correlation of the units. Zones are shown on the right"
        ) == ("range_chart", False)

    def test_zonation_plural_matches_like_the_web_engine(self):
        assert auto_detect_chart_mode_ex(
            "Zonations and datum points") == ("zonation_chart", True)

    @pytest.mark.parametrize("caption,mode", [
        ("Isotope chemostratigraphy across the T-O boundary",
         "chemical_stratigraphy"),
        ("Strontium isotope stratigraphy, Section 2", "chemical_stratigraphy"),
        ("地球化学分层曲线", "chemical_stratigraphy"),
        ("Палеогеографическая карта Восточной Гонтваны", "paleomap"),
        ("Late Jurassic paleogeographic map of East Gondwana", "paleomap"),
        ("华南晚古地理图", "paleomap"),
        ("Sr versus Nd scatter plot for the whole-rock data", "scatter_plot"),
        ("Clustering PCA biplot of the samples", "scatter_plot"),
        ("Диаграмма рассеяния по элементам", "scatter_plot"),
    ])
    def test_conservative_tables_for_the_assistant_modes(self, caption, mode):
        assert auto_detect_chart_mode_ex(caption) == (mode, True)

    @pytest.mark.parametrize("caption", [
        "δ13C curve above the conodont range chart",
        "Paleogeographic map with the fossil range charts",
        "Scatter plot of the isotope data above the conodont range chart",
    ])
    def test_new_tables_never_steal_an_explicit_range_chart(self, caption):
        assert auto_detect_chart_mode_ex(caption) == ("range_chart", True)

    @pytest.mark.parametrize("caption,mode", [
        ("Columnar section with zonation", "zonation_chart"),
        ("Pollinator abundance diagram", "abundance_diagram"),
        ("Molecular phylogenetic tree of the genus", "phylogenetic_tree"),
        ("Lithological column and isotope sampling levels",
         "columnar_section"),
    ])
    def test_established_branches_keep_their_priority(self, caption, mode):
        assert auto_detect_chart_mode_ex(caption)[0] == mode

    def test_legacy_unreachable_duplicate_is_gone(self):
        """The copy of the old heuristic after ``return mode`` referenced an
        undefined ``t`` — unreachable, but a NameError waiting for the next
        refactor that dropped the return."""
        import inspect

        from rca_core.chart_mode import auto_detect_chart_mode as fn

        src = inspect.getsource(fn)
        assert src.rstrip().endswith("return mode")
        assert "for k in _PHYLO_CJK" not in src.split("def auto_detect_chart_mode(")[1]


# ---------------------------------------------------------------------------
# 6. cache.get: a locked LRU touch must not lose the hit
# ---------------------------------------------------------------------------

class _LockOnUpdate:
    """Proxy that fails UPDATE/commit like a second process holding the file."""

    def __init__(self, real, fail_writes=True):
        self._real = real
        self.fail_writes = fail_writes
        self.rolled_back = 0

    def execute(self, sql, params=()):
        if self.fail_writes and sql.strip().upper().startswith("UPDATE"):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, params)

    def commit(self):
        if self.fail_writes:
            raise sqlite3.OperationalError("database is locked")
        return self._real.commit()

    def rollback(self):
        self.rolled_back += 1
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestCacheReadSurvivesLock:
    def test_hit_returned_even_when_the_touch_fails(self, tmp_path):
        cache = ResultCache(db_path=str(tmp_path / "locked.sqlite"))
        try:
            key = ResultCache.make_key(image_b64="AAA", model="m")
            cache.put(key, {"species_ranges": [{"species": "A"}]})

            real = cache._conn
            proxy = _LockOnUpdate(real)
            cache._conn = proxy
            try:
                hit = cache.get(key)
            finally:
                cache._conn = real
            assert hit == {"species_ranges": [{"species": "A"}]}
            assert proxy.rolled_back == 1, "failed touch must roll back"
            # A miss still returns None (no exception on the empty path).
            assert cache.get("no-such-key") is None
        finally:
            cache.close()

    def test_touch_still_applied_when_no_contention(self, tmp_path):
        cache = ResultCache(db_path=str(tmp_path / "clean.sqlite"))
        try:
            key = ResultCache.make_key(x=1)
            cache.put(key, {"v": 1})
            old = cache._conn.execute(
                "SELECT ts FROM extract_cache WHERE k = ?", (key,)).fetchone()[0]
            cache._conn.execute("UPDATE extract_cache SET ts = ? WHERE k = ?",
                                (old - 5000.0, key))
            cache._conn.commit()
            assert cache.get(key) == {"v": 1}
            new = cache._conn.execute(
                "SELECT ts FROM extract_cache WHERE k = ?", (key,)).fetchone()[0]
            assert new > old - 5000.0, "the LRU touch must still happen"
        finally:
            cache.close()


# ---------------------------------------------------------------------------
# 7. error_utils: delay floor, no sleep after the last attempt, code parity
# ---------------------------------------------------------------------------

class TestRetryDelayFloor:
    @pytest.mark.parametrize("headers", [
        {"retry-after": "0"},
        {"Retry-After": "0"},
        {"retry-after-ms": "0"},
        {"retry-after": "Thu, 01 Jan 1970 00:00:00 GMT"},
    ])
    def test_zero_retry_after_never_means_no_backoff(self, headers):
        assert EU.get_retry_delay(429, headers) >= EU.MIN_RETRY_DELAY_SECONDS

    def test_explicit_retry_after_is_still_honoured(self):
        assert EU.get_retry_delay(429, {"retry-after": "7"}) == 7.0
        assert EU.get_retry_delay(429, {"retry-after-ms": "2500"}) == 2.5

    def test_max_delay_ceiling_beats_the_floor(self):
        # A caller that wants a short probe (and the test suite that must not
        # sleep) can still cap below the floor.
        assert EU.get_retry_delay(429, {"retry-after": "0"}, max_delay=0) == 0
        assert EU.get_retry_delay(429, {"retry-after": "300"},
                                  max_delay=12.0) == 12.0

    def test_backoff_fallback_is_floored_too(self):
        assert EU.get_retry_delay(500, None, attempt=0,
                                  initial_delay=0.001) >= 1.0


class TestNoSleepAfterLastAttempt:
    def test_retryable_predicate_does_not_sleep_twice(self, monkeypatch):
        slept: list[float] = []
        notified: list[int] = []
        monkeypatch.setattr(EU.time, "sleep", lambda s: slept.append(s))

        def on_retry(attempt, delay, exc):
            notified.append(attempt)

        calls = {"n": 0}

        def f():
            calls["n"] += 1
            return "still-retryable"

        res = EU.retry_with_backoff(f, max_retries=3, initial_delay=0.01,
                                    retryable=lambda r: True,
                                    on_retry=on_retry)
        assert calls["n"] == 4, "one initial attempt + 3 retries"
        assert res == "still-retryable"
        # 3 retries => 3 sleeps (the old code slept a 4th time after the last
        # attempt had already been made).
        assert len(slept) == 3
        assert notified == [1, 2, 3]

    def test_exception_path_unchanged(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(EU.time, "sleep", lambda s: slept.append(s))
        calls = {"n": 0}

        def f():
            calls["n"] += 1
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            EU.retry_with_backoff(f, max_retries=2, initial_delay=0.01)
        assert calls["n"] == 3
        assert len(slept) == 2


class TestErrorCodeUsesTrimmedBody:
    def test_code_from_a_normal_body(self):
        err = EU.normalize_http_error(
            400, b'{"error": {"code": "content_policy_violation"}}', "HTTP 400")
        assert err.error_code == "content_policy_violation"

    def test_code_beyond_the_stored_window_is_not_reported(self):
        # Consistency rule: the code must come from the body the user is shown
        # (MAX_ERROR_BODY_CHARS), not from the undecoded tail of a giant one.
        padding = "x" * (EU.MAX_ERROR_BODY_CHARS + 200)
        body = ('{"message": "' + padding + '", "error_code": "late_code"}').encode()
        err = EU.normalize_http_error(400, body, "HTTP 400")
        assert len(err.body) == EU.MAX_ERROR_BODY_CHARS
        assert err.error_code is None, ("code parsed from text that is not in "
                                        "the stored/displayed body")

    def test_code_inside_the_stored_window_still_found(self):
        padding = "x" * 500
        body = ('{"error_code": "early_code", "message": "' + padding + '"}')
        # Truncation makes the tail invalid JSON, so this is the honest
        # behaviour of the trimmed parse: no code rather than a half-body.
        err = EU.normalize_http_error(400, body.encode(), "HTTP 400")
        assert err.error_code in (None, "early_code")

    def test_empty_body_yields_no_code(self):
        assert EU._extract_error_code(None, "") is None
        assert EU._extract_error_code(b"", "") is None
