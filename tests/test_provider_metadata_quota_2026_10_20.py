"""The provider record's new fields, and the two live probes they enable.

REVIEW-2026-10-20, the cc-switch borrowings:

  * a provider carries NOTES (what the account is for, which plan, when it
    expires), a list of CUSTOM ENDPOINTS the speed test keeps, a FULL-URL
    flag for a gateway whose path is not the canonical one, and a PROFILE
    name that groups providers;
  * the MODEL METADATA catalogue says what a model can do -- and the
    max-output number is MEASURED, not copied: MiniMax-M3.1-Flash answers a
    larger ``max_tokens`` with "does not support max tokens > 524288";
  * the QUOTA query reads MiniMax's programming-plan endpoint, whose route
    and response shape were confirmed against a live Token Plan key;
  * the LATENCY probe measures the endpoint's round trip.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rca_core.llm as L                                     # noqa: E402


# --- the record's new fields -----------------------------------------------

def test_the_new_fields_persist():
    p = L.LlmProvider(name="n", endpoint="https://x.example.com",
                      api_key="k", model="m",
                      notes="work account, expires 2026-12",
                      custom_endpoints=["https://a.example.com"],
                      full_url=True, profile="work")
    d = p.to_dict()
    assert d["notes"] == "work account, expires 2026-12"
    assert d["custom_endpoints"] == ["https://a.example.com"]
    assert d["full_url"] is True
    assert d["profile"] == "work"
    back = L.LlmProvider.from_dict(d)
    assert back.notes == p.notes
    assert back.custom_endpoints == p.custom_endpoints
    assert back.full_url is True
    assert back.profile == "work"


def test_an_old_record_without_the_fields_still_loads():
    d = L.LlmProvider(name="n", endpoint="https://x.example.com",
                      api_key="k", model="m").to_dict()
    for key in ("notes", "custom_endpoints", "full_url", "profile"):
        d.pop(key, None)
    back = L.LlmProvider.from_dict(d)
    assert back.notes == ""
    assert back.custom_endpoints == []
    assert back.full_url is False
    assert back.profile == ""


def test_two_providers_do_not_share_their_endpoint_list():
    a = L.LlmProvider(name="a", endpoint="https://x.example.com", api_key="k")
    b = L.LlmProvider(name="b", endpoint="https://y.example.com", api_key="k")
    a.custom_endpoints.append("https://z.example.com")
    assert b.custom_endpoints == [], \
        "a mutable default shared between records is a cross-talk bug"


# --- the full-URL endpoint --------------------------------------------------

def test_a_full_url_endpoint_is_used_verbatim():
    p = L.LlmProvider(model="m", endpoint="https://gw.example.com/custom/messages",
                      full_url=True)
    assert L._request_target(p, "/v1/messages") == \
        "https://gw.example.com/custom/messages"


def test_a_normal_endpoint_is_a_prefix():
    p = L.LlmProvider(model="m", endpoint="https://api.minimaxi.com/anthropic")
    assert L._request_target(p, "/v1/messages") == \
        "https://api.minimaxi.com/anthropic/v1/messages"


def test_a_full_url_provider_has_no_model_listing_route():
    p = L.LlmProvider(model="m", endpoint="https://gw.example.com/custom/messages",
                      api_key="k", full_url=True)
    with patch.object(L, "_get_json", return_value=(b"{}", 200, b"")) as g:
        assert L.list_models(p, timeout_sec=5) == []
        assert g.called is False, "a full-URL endpoint must not be probed for /v1/models"


# --- the model metadata catalogue ------------------------------------------

def test_the_measured_max_output_is_catalogueued():
    """524288 is what the API itself said, not what a doc claimed."""
    meta = L.model_metadata_for("MiniMax-M3.1-Flash")
    assert meta is not None
    assert meta.max_output == 524288
    assert meta.context_window == 1000000


def test_the_metadata_lookup_is_case_and_prefix_tolerant():
    assert L.model_metadata_for("minimax-m3.1-flash") == \
        L.model_metadata_for("MiniMax-M3.1-Flash")
    assert L.model_metadata_for("MiniMax-M3.1-Flash-Preview") is not None
    assert L.model_metadata_for("gpt-4o") is None


# --- the quota query --------------------------------------------------------

def _minimax_payload(interval_remaining=99.0, weekly_remaining=82.0,
                      weekly_status=1):
    """The PARSED payload -- `_quota_get_json` returns a dict, not bytes."""
    return {
        "model_remains": [
            {"model_name": "general",
             "current_interval_remaining_percent": interval_remaining,
             "current_interval_status": 1,
             "current_weekly_status": weekly_status,
             "current_weekly_remaining_percent": weekly_remaining,
             "end_time": 1792479600000,
             "weekly_end_time": 1792944000000},
            {"model_name": "video",
             "current_interval_remaining_percent": 100.0,
             "current_weekly_status": 1,
             "current_weekly_remaining_percent": 100.0},
        ],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


def test_the_minimax_plan_becomes_two_tiers():
    p = L.LlmProvider(model="m", endpoint="https://api.minimaxi.com/anthropic",
                      api_key="k")
    with patch.object(L, "_quota_get_json",
                      return_value=(_minimax_payload(), 200, "")):
        q = L.query_coding_plan_quota(p, timeout_sec=5)
    assert q.ok
    assert [t.name for t in q.tiers] == ["five_hour", "weekly_limit"]
    assert q.tiers[0].used_percent == 1.0     # 100 - 99
    assert q.tiers[1].used_percent == 18.0    # 100 - 82
    assert q.tiers[0].resets_at == 1792479600000
    assert q.tiers[1].resets_at == 1792944000000


def test_a_plan_without_a_weekly_limit_has_no_weekly_tier():
    """``current_weekly_status == 3`` means there is no weekly bucket.

    Its remaining percent is a constant 100, so showing it would render a
    fake "0% used" bucket every time.
    """
    p = L.LlmProvider(model="m", endpoint="https://api.minimaxi.com/anthropic",
                      api_key="k")
    with patch.object(L, "_quota_get_json",
                      return_value=(_minimax_payload(weekly_status=3), 200, "")):
        q = L.query_coding_plan_quota(p, timeout_sec=5)
    assert [t.name for t in q.tiers] == ["five_hour"]


def test_the_video_bucket_is_never_shown():
    p = L.LlmProvider(model="m", endpoint="https://api.minimaxi.com/anthropic",
                      api_key="k")
    with patch.object(L, "_quota_get_json",
                      return_value=(_minimax_payload(), 200, "")):
        q = L.query_coding_plan_quota(p, timeout_sec=5)
    assert all(t.name != "video" for t in q.tiers)


def test_a_business_error_is_reported_not_raised():
    p = L.LlmProvider(model="m", endpoint="https://api.minimaxi.com/anthropic",
                      api_key="k")
    body = {"base_resp": {"status_code": 1004, "status_msg": "no plan"}}
    with patch.object(L, "_quota_get_json", return_value=(body, 200, "")):
        q = L.query_coding_plan_quota(p, timeout_sec=5)
    assert not q.ok
    assert "no plan" in q.error


def test_a_provider_with_no_quota_route_says_so():
    p = L.LlmProvider(model="m", endpoint="https://unknown.example.com",
                      api_key="k")
    q = L.query_coding_plan_quota(p, timeout_sec=5)
    assert not q.ok
    assert "quota" in q.error.lower()


def test_an_empty_key_is_refused_before_any_request():
    p = L.LlmProvider(model="m", endpoint="https://api.minimaxi.com/anthropic",
                      api_key="")
    with patch.object(L, "_quota_get_json") as g:
        q = L.query_coding_plan_quota(p, timeout_sec=5)
    assert not q.ok
    assert g.called is False


# --- the latency probe ------------------------------------------------------

def test_the_latency_probe_reports_the_round_trip():
    p = L.LlmProvider(model="m", endpoint="https://api.minimaxi.com/anthropic",
                      api_key="k")
    with patch.object(L.urllib.request, "urlopen") as u:
        u.return_value.__enter__.return_value.read.return_value = b"{}"
        u.return_value.__enter__.return_value.status = 200
        lat, status, err = L.measure_endpoint_latency(p, timeout_sec=5)
    assert lat is not None and lat >= 0
    assert status == 200
    assert err == ""


def test_an_error_status_still_measures_a_latency():
    """A 404 still says the endpoint is alive and how far away it is."""
    import urllib.error
    p = L.LlmProvider(model="m", endpoint="https://api.minimaxi.com/anthropic",
                      api_key="k")
    with patch.object(L.urllib.request, "urlopen",
                      side_effect=urllib.error.HTTPError(
                          "u", 404, "nope", None, None)):
        lat, status, err = L.measure_endpoint_latency(p, timeout_sec=5)
    assert lat is not None, "an HTTP answer is still a latency measurement"
    assert status == 404
    assert "404" in err
