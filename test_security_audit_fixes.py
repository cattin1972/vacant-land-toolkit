"""
Regression tests for fixes made during the 2026-09-14 security audit.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v

sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit\webapp")


def fake_response(json_body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body
    return resp


# ---------------------------------------------------------------------
print("=== Test 1: Cache County tax_id injection payloads are rejected before any network call ===")
malicious_payloads = [
    "' OR '1'='1",
    "x'; DROP TABLE parcels; --",
    "1 OR 1=1",
    "02-216-0025' OR 'a'='a",
    "<script>alert(1)</script>",
]
with patch("vacant_land_search.requests.get") as mock_get:
    for payload in malicious_payloads:
        try:
            v.check_cache_county_tax_delinquency(payload)
            print(f"FAIL: payload was not rejected: {payload!r}")
            assert False
        except ValueError:
            pass
    assert not mock_get.called, "a malicious tax_id must never reach the network call at all"
print(f"OK -- all {len(malicious_payloads)} injection payloads rejected before any request was made")
print("ALL PASS")

print("\n=== Test 2: well-formed Cache County tax_ids still work normally ===")
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response({"features": []})
    for good_id in ["02-216-0025", "ABC123", "0014L00244000000"]:
        result = v.check_cache_county_tax_delinquency(good_id)
        assert result == {}
    assert mock_get.call_count == 3, "legitimate IDs must still reach the real query"
print("ALL PASS -- legitimate lookups are unaffected by the validation")

# ---------------------------------------------------------------------
print("\n=== Test 3: /api/search's diagnostic log never includes search geography (cross-user leak fix) ===")
os.chdir(r"C:\Users\catti\Documents\vacant-land-toolkit\webapp")
import app as flask_app

client = flask_app.app.test_client()

with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response({"properties": [], "metadata": {}})
    with patch("vacant_land_search.log_source_event") as mock_log:
        resp = client.post("/api/search", json={"state": "FL", "county": "Lee", "city": "Cape Coral", "api_key": "test-key"})
        assert resp.status_code == 200
        # Every call to log_source_event must be free of this specific
        # customer's search geography -- inspect every call made.
        for call in mock_log.call_args_list:
            args, kwargs = call
            all_text = " ".join(str(a) for a in args) + " " + " ".join(f"{k}={v}" for k, v in kwargs.items())
            assert "Lee" not in all_text, f"Cape Coral/Lee County leaked into a log call: {call}"
            assert "Cape Coral" not in all_text, f"leaked into a log call: {call}"
print("OK -- no log_source_event call included this search's county/city")
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 4: /api/health/sources is closed by default (no HEALTH_CHECK_SECRET configured) ===")
os.environ.pop("HEALTH_CHECK_SECRET", None)
resp = client.get("/api/health/sources")
print(resp.status_code, resp.get_json())
assert resp.status_code == 404
print("ALL PASS")

print("\n=== Test 5: /api/health/sources rejects a wrong secret even when one IS configured ===")
os.environ["HEALTH_CHECK_SECRET"] = "the-real-secret"
resp = client.get("/api/health/sources?secret=guess")
print(resp.status_code)
assert resp.status_code == 404
print("ALL PASS")

print("\n=== Test 6: /api/health/sources works with the correct secret ===")
resp = client.get("/api/health/sources?secret=the-real-secret")
print(resp.status_code, resp.get_json())
assert resp.status_code == 200
assert "totals" in resp.get_json()
print("ALL PASS")
os.environ.pop("HEALTH_CHECK_SECRET", None)

print("\n=== Test 7: rate limiter blocks a single IP after its limit, but not other IPs ===")
raw_valid = {"latitude": 26.6406, "longitude": -81.9873, "propertyLocation": {}}
with patch("vacant_land_search.get_comprehensive_buildability_report") as mock_build, \
     patch("vacant_land_search.check_zoning_district") as mock_zoning:
    mock_build.return_value = {"concerns": []}
    mock_zoning.return_value = {}
    # Same IP, hammering /api/parcel/<apn>/report past its 30/60s limit.
    statuses = []
    for i in range(32):
        resp = client.post(
            "/api/parcel/TEST/report",
            json={"raw": raw_valid, "acres": 1.0},
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
        statuses.append(resp.status_code)
    ok_count = statuses.count(200)
    blocked_count = statuses.count(429)
    print(f"IP #1 (32 requests): {ok_count} succeeded, {blocked_count} blocked (429)")
    assert ok_count == 30, f"expected exactly 30 to succeed before the limit, got {ok_count}"
    assert blocked_count == 2, f"expected the last 2 to be blocked, got {blocked_count}"

    # A DIFFERENT IP must not be affected by the first IP's usage at all.
    resp = client.post(
        "/api/parcel/TEST/report",
        json={"raw": raw_valid, "acres": 1.0},
        headers={"X-Forwarded-For": "198.51.100.9"},
    )
    print("A different IP's very first request:", resp.status_code)
    assert resp.status_code == 200, "a different IP must not inherit another IP's rate-limit usage"
print("ALL PASS -- limit is enforced per-IP, not globally shared")

print("\n=== ALL SECURITY AUDIT REGRESSION TESTS PASSED ===")
