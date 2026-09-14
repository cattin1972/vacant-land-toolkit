"""
Mocked tests for the Tracerfy skip-tracing integration.

These use a FAKE response shaped like Tracerfy's API, for fast/free
regression coverage of the code's logic (field extraction, bulk
looping, error handling, cost caps). The underlying request/response
SHAPE these mocks are based on has separately been confirmed correct
against a real account and a real address (2026-09-12) -- see the
LIVE-TESTED section note in vacant_land_search.py for that real test's
result. These mocked tests exist so that logic changes here don't need
a real, billed API call every time to verify.
"""
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v


def fake_response(json_body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body
    return resp


# ---------------------------------------------------------------------
print("=== Test 1: skip_trace_owner -- a hit, per Tracerfy's documented response shape ===")
hit_response = {
    "address": "123 Main St", "city": "Austin", "state": "TX",
    "hit": True, "persons_count": 1, "credits_deducted": 1,
    "persons": [{
        "first_name": "Jane", "last_name": "Doe", "full_name": "Jane Doe",
        "property_owner": True,
        "phones": [{"number": "5125550100", "type": "Mobile", "dnc": False, "litigator": False, "carrier": "T-MOBILE", "rank": 1}],
        "emails": [{"email": "jane@example.com", "rank": 1}],
    }],
}
with patch("vacant_land_search.requests.post") as mock_post:
    mock_post.return_value = fake_response(hit_response)
    result = v.skip_trace_owner("123 Main St", "Austin", "TX", zip_code="78701", api_key="FAKEKEY")
    print(result)
    assert result["hit"] is True
    assert result["owner_name"] == "Jane Doe"
    assert result["phones"] == [{"number": "5125550100", "type": "Mobile", "dnc": False, "litigator": False}]
    assert result["emails"] == ["jane@example.com"]

    # Confirm the request was built as documented
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"] == {"Authorization": "Bearer FAKEKEY"}
    assert call_kwargs["json"] == {"address": "123 Main St", "city": "Austin", "state": "TX", "find_owner": True, "zip": "78701"}
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 2: skip_trace_owner -- no hit ===")
with patch("vacant_land_search.requests.post") as mock_post:
    mock_post.return_value = fake_response({"hit": False, "credits_deducted": 1})
    result = v.skip_trace_owner("999 Nowhere Rd", "Nowhere", "TX", api_key="FAKEKEY")
    print(result)
    assert result == {"hit": False, "owner_name": None, "phones": [], "emails": [], "credits_deducted": 1}
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 3: no API key -> clear error, no request made ===")
try:
    v.skip_trace_owner("123 Main St", "Austin", "TX", api_key=None)
    print("FAIL: should have raised")
except RuntimeError as e:
    print("OK:", e)

# ---------------------------------------------------------------------
print("\n=== Test 4: skip_trace_owners_bulk -- loops one at a time, merges results, caps at max_lookups ===")
owners = [
    {"apn": "A1", "street": "1 First St", "city": "Austin", "state": "TX"},
    {"apn": "A2", "street": "2 Second St", "city": "Austin", "state": "TX"},
    {"apn": "A3", "street": "3 Third St", "city": "Austin", "state": "TX"},
]
with patch("vacant_land_search.requests.post") as mock_post:
    mock_post.side_effect = [
        fake_response({"hit": True, "credits_deducted": 1, "persons": [{"full_name": "Owner One", "phones": [], "emails": []}]}),
        fake_response({"hit": False, "credits_deducted": 1}),
        fake_response({"hit": True, "credits_deducted": 1, "persons": [{"full_name": "Owner Three", "phones": [], "emails": []}]}),
    ]
    results = v.skip_trace_owners_bulk(owners, api_key="FAKEKEY", max_lookups=15)
    print(results)
    assert len(results) == 3
    assert mock_post.call_count == 3
    assert results[0]["apn"] == "A1" and results[0]["owner_name"] == "Owner One"
    assert results[1]["apn"] == "A2" and results[1]["hit"] is False
    assert results[2]["apn"] == "A3" and results[2]["owner_name"] == "Owner Three"
print("ALL PASS")

print("\n=== Test 5: skip_trace_owners_bulk -- hard cap on max_lookups (cost control) ===")
many_owners = [{"apn": f"A{i}", "street": f"{i} St", "city": "Austin", "state": "TX"} for i in range(20)]
with patch("vacant_land_search.requests.post") as mock_post:
    mock_post.return_value = fake_response({"hit": False, "credits_deducted": 1})
    results = v.skip_trace_owners_bulk(many_owners, api_key="FAKEKEY", max_lookups=5)
    print(f"Requested 20 owners, capped at 5 -- got {len(results)} results, made {mock_post.call_count} calls")
    assert len(results) == 5
    assert mock_post.call_count == 5
print("ALL PASS")

print("\n=== Test 6: skip_trace_owners_bulk -- one bad lookup doesn't abort the rest ===")
with patch("vacant_land_search.requests.post") as mock_post:
    def side_effect(*args, **kwargs):
        if kwargs["json"]["address"] == "2 Second St":
            raise __import__("requests").exceptions.HTTPError("500 Server Error")
        return fake_response({"hit": True, "credits_deducted": 1, "persons": [{"full_name": "OK Owner", "phones": [], "emails": []}]})
    mock_post.side_effect = side_effect
    results = v.skip_trace_owners_bulk(owners, api_key="FAKEKEY")
    print(results)
    assert results[0]["hit"] is True
    assert results[1]["hit"] is False and "error" in results[1]
    assert results[2]["hit"] is True
print("ALL PASS")

print("\n=== Test 7: a 200 response with no 'hit' field is an ERROR, never a silent 'no match' ===")
# Regression test for a real bug found during the 2026-09-14 external-
# data-source audit: a degraded Tracerfy response (200 OK, but missing
# the documented 'hit' field entirely -- e.g. an {"error": ...} body)
# used to read as payload.get("hit") -> None -> falsy -> a confident
# "no match found," after real money had already been billed for a
# lookup that never actually completed.
with patch("vacant_land_search.requests.post") as mock_post:
    mock_post.return_value = fake_response({"error": "internal issue", "credits_deducted": 1})
    try:
        v.skip_trace_owner("1 First St", "Austin", "TX", api_key="FAKEKEY")
        print("FAIL: should have raised on a missing 'hit' field")
    except RuntimeError as e:
        print(f"OK, raised instead of silently reporting no match: {e}")
print("ALL PASS")

# And bulk mode must turn that into a per-item error, not a silent miss.
with patch("vacant_land_search.requests.post") as mock_post:
    mock_post.return_value = fake_response({"error": "internal issue", "credits_deducted": 1})
    results = v.skip_trace_owners_bulk([{"apn": "A1", "street": "1 First St", "city": "Austin", "state": "TX"}], api_key="FAKEKEY")
    print(results)
    assert results[0]["hit"] is False
    assert "error" in results[0], "a degraded response must surface as an error, not a bare miss"
print("ALL PASS")

print("\n=== ALL TESTS PASSED (mocked only -- see file docstring) ===")
