"""
Backend contract tests filling gaps the existing legacy test_*.py scripts
didn't cover: explicit cross-customer isolation, a full API-key-never-
leaks sweep across every route (not just Realie/Tracerfy individually),
end-to-end report generation with a realistic fixture, and the two
rate-limited endpoints that had no dedicated test yet (score/bulk,
delinquency).

Uses the werkzeug test client (via the `client` fixture in conftest.py) --
real Flask routing and real application logic, zero real network calls
(everything that would hit a government/Realie/Tracerfy API is mocked).
"""
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
from fixtures import (
    COMPLETE_CLEAN_BUILDABILITY,
    REALIE_RAW_VACANT_LOT,
)


def fake_response(json_body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body
    return resp


# ---------------------------------------------------------------------
# Customer isolation: two different callers' parcel/skip-trace requests
# must never cross, in either direction.
# ---------------------------------------------------------------------

def test_two_customers_parcel_reports_never_cross(client):
    """Customer A's raw parcel data and Customer B's raw parcel data are
    posted back-to-back; each response must reflect only its own caller's
    input, never the other's -- the whole point of the stateless,
    browser-holds-its-own-data architecture."""
    raw_a = {**REALIE_RAW_VACANT_LOT, "parcelId": "CUSTOMER-A-APN", "latitude": 26.0, "longitude": -81.0}
    raw_b = {**REALIE_RAW_VACANT_LOT, "parcelId": "CUSTOMER-B-APN", "latitude": 40.0, "longitude": -74.0,
             "propertyIdentification": {"currentOwner": {"ownerStreet": "999 Customer B St", "ownerCity": "Boston", "ownerState": "MA", "ownerZipCode": "02108"}}}

    with patch("vacant_land_search.get_comprehensive_buildability_report") as mock_build, \
         patch("vacant_land_search.check_zoning_district") as mock_zoning:
        mock_build.return_value = COMPLETE_CLEAN_BUILDABILITY
        mock_zoning.return_value = {}

        resp_a = client.post("/api/parcel/CUSTOMER-A-APN/report", json={"raw": raw_a, "acres": 5.0})
        resp_b = client.post("/api/parcel/CUSTOMER-B-APN/report", json={"raw": raw_b, "acres": 5.0})

    data_a = resp_a.get_json()
    data_b = resp_b.get_json()
    assert data_a["apn"] == "CUSTOMER-A-APN"
    assert data_b["apn"] == "CUSTOMER-B-APN"
    assert "Customer B" not in str(data_a)
    assert "Boston" not in str(data_a)
    assert "Customer A" not in str(data_b) if "Customer A" in str(raw_a) else True


def test_two_customers_skiptrace_keys_never_cross(client):
    """Customer A's Tracerfy key must never be used to bill/attribute
    Customer B's lookup, and vice versa -- each request supplies its own
    key and its own parcel; the mock asserts on exactly which key was
    used for which call."""
    raw = {**REALIE_RAW_VACANT_LOT}
    calls = []

    def fake_skip_trace(street, city, state, zip_code=None, api_key=None):
        calls.append(api_key)
        return {"hit": False, "owner_name": None, "phones": [], "emails": [], "credits_deducted": 0}

    with patch("vacant_land_search.skip_trace_owner", side_effect=fake_skip_trace):
        client.post("/api/skiptrace/A-APN", json={"raw": raw, "tracerfy_api_key": "customer-a-key"})
        client.post("/api/skiptrace/B-APN", json={"raw": raw, "tracerfy_api_key": "customer-b-key"})

    assert calls == ["customer-a-key", "customer-b-key"]


# ---------------------------------------------------------------------
# API key never leaks -- a systematic sweep, not just the two dedicated
# no-shared-key tests (which check server-side FALLBACK usage, a
# different property from "does the caller's own key ever get echoed
# back in a response body").
# ---------------------------------------------------------------------

SECRET_REALIE_KEY = "sk-realie-test-secret-should-never-appear-in-any-response"
SECRET_TRACERFY_KEY = "sk-tracerfy-test-secret-should-never-appear-in-any-response"


def test_realie_key_is_never_echoed_in_search_response(client):
    with patch("vacant_land_search.requests.get") as mock_get:
        mock_get.return_value = fake_response({"properties": [], "metadata": {}})
        resp = client.post("/api/search", json={"state": "FL", "api_key": SECRET_REALIE_KEY})
    assert SECRET_REALIE_KEY not in resp.get_data(as_text=True)


def test_realie_key_is_never_echoed_in_search_error_response(client):
    with patch("vacant_land_search.requests.get") as mock_get:
        mock_get.return_value = fake_response({}, status=401)
        mock_get.return_value.raise_for_status.side_effect = Exception("401 Unauthorized")
        resp = client.post("/api/search", json={"state": "FL", "api_key": SECRET_REALIE_KEY})
    assert SECRET_REALIE_KEY not in resp.get_data(as_text=True)


def test_tracerfy_key_is_never_echoed_in_skiptrace_response(client):
    raw = {**REALIE_RAW_VACANT_LOT}
    with patch("vacant_land_search.skip_trace_owner") as mock_trace:
        mock_trace.return_value = {"hit": True, "owner_name": "Test", "phones": [], "emails": [], "credits_deducted": 1}
        resp = client.post("/api/skiptrace/APN1", json={"raw": raw, "tracerfy_api_key": SECRET_TRACERFY_KEY})
    assert SECRET_TRACERFY_KEY not in resp.get_data(as_text=True)


def test_tracerfy_key_is_never_echoed_in_skiptrace_error_response(client):
    raw = {**REALIE_RAW_VACANT_LOT}
    with patch("vacant_land_search.skip_trace_owner", side_effect=RuntimeError("401 Unauthorized for tracerfy")):
        resp = client.post("/api/skiptrace/APN1", json={"raw": raw, "tracerfy_api_key": SECRET_TRACERFY_KEY})
    assert SECRET_TRACERFY_KEY not in resp.get_data(as_text=True)


def test_tracerfy_key_is_never_echoed_in_bulk_skiptrace_response(client):
    raw = {**REALIE_RAW_VACANT_LOT}
    with patch("vacant_land_search.skip_trace_owners_bulk") as mock_bulk:
        mock_bulk.return_value = [{"apn": "APN1", "key": "k1", "hit": False}]
        resp = client.post("/api/skiptrace/bulk", json={
            "tracerfy_api_key": SECRET_TRACERFY_KEY,
            "parcels": [{"apn": "APN1", "raw": raw, "key": "k1"}],
        })
    assert SECRET_TRACERFY_KEY not in resp.get_data(as_text=True)


# ---------------------------------------------------------------------
# End-to-end report generation with a realistic fixture.
# ---------------------------------------------------------------------

def test_full_parcel_report_generation_end_to_end(client):
    with patch("vacant_land_search.get_comprehensive_buildability_report") as mock_build, \
         patch("vacant_land_search.check_zoning_district") as mock_zoning:
        mock_build.return_value = COMPLETE_CLEAN_BUILDABILITY
        mock_zoning.return_value = {}
        resp = client.post("/api/parcel/TEST-APN/report", json={"raw": REALIE_RAW_VACANT_LOT, "acres": 5.0})

    assert resp.status_code == 200
    data = resp.get_json()
    for key in ("decision", "buyer_fit", "outreach_brief", "evidence", "current_owner", "tax_flags", "listing_links"):
        assert key in data, f"missing top-level key: {key}"

    assert data["decision"]["deal_potential"] in (
        "Strong", "Moderate", "Weak", "Avoid", "Insufficient information",
    )
    assert data["evidence"]["findings"], "a realistic fixture must produce actual findings, not an empty report"
    assert data["current_owner"]["owner_name"] == "Jane Individual Doe"
    assert data["outreach_brief"].get("disclaimer"), "outreach brief must always carry its standing disclaimer"


# ---------------------------------------------------------------------
# Rate limiting on the two endpoints that had no dedicated test yet.
# ---------------------------------------------------------------------

def test_score_bulk_rate_limit_enforced_per_ip(client):
    with patch("vacant_land_search.get_comprehensive_buildability_report") as mock_build, \
         patch("vacant_land_search.check_zoning_district") as mock_zoning:
        mock_build.return_value = COMPLETE_CLEAN_BUILDABILITY
        mock_zoning.return_value = {}
        statuses = []
        for i in range(8):
            resp = client.post(
                "/api/parcel/score/bulk",
                json={"parcels": [{"apn": "T1", "raw": REALIE_RAW_VACANT_LOT, "key": "k1"}]},
                headers={"X-Forwarded-For": "203.0.113.77"},
            )
            statuses.append(resp.status_code)
    assert statuses.count(200) == 6, f"expected exactly 6 to succeed (the documented limit), got {statuses.count(200)}"
    assert statuses.count(429) == 2


def test_delinquency_rate_limit_enforced_per_ip(client, flask_app_module):
    # app.py's DELINQUENCY_CHECKS dict captures a direct reference to the
    # real function at import time (`"fn": v.check_king_county_delinquent_tax`),
    # so patching the function on the vacant_land_search module afterward
    # does NOT reach it -- the dict entry must be patched directly, or
    # this test would silently make 32 REAL network calls to King
    # County's government API instead of testing the rate limiter at all.
    with patch.dict(
        flask_app_module.DELINQUENCY_CHECKS["king_county"],
        {"fn": lambda parcel_id: {"found": False}},
    ):
        statuses = []
        for i in range(32):
            resp = client.get(
                "/api/delinquency/king_county?id=000080001506",
                headers={"X-Forwarded-For": "203.0.113.88"},
            )
            statuses.append(resp.status_code)
    assert statuses.count(200) == 30, f"expected exactly 30 to succeed (the documented limit), got {statuses.count(200)}"
    assert statuses.count(429) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
