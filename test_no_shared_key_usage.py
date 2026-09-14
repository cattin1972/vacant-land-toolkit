"""
Permanent regression guard: no request to this site may EVER spend the
operator's own Realie tokens or Tracerfy credits on another visitor's
behalf. Explicit, repeated instruction from the user (2026-09-14):
"I don't want them spending mine EVER."

This doesn't just check the current code's behavior -- it actively sets
REALIE_API_KEY / TRACERFY_API_KEY (and a couple of other plausible names
someone might set on the server one day) in the environment, and mocks
the actual outbound network calls so that if any future code change
ever reintroduces a fallback to a server-side key, this test fails
LOUDLY by detecting a real network call would have fired -- not just by
checking a response field.

Run this file any time app.py or vacant_land_search.py's key-handling
code changes.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit\webapp")

# Simulate an operator who left keys sitting in the server's own
# environment -- exactly the scenario that must NEVER be usable by
# someone else's request, under any of these plausible variable names.
for var in ("REALIE_API_KEY", "TRACERFY_API_KEY", "API_KEY", "REALIE_KEY", "TRACERFY_KEY"):
    os.environ[var] = "server-side-key-that-must-never-be-used-for-a-visitor"

os.chdir(r"C:\Users\catti\Documents\vacant-land-toolkit\webapp")
import app as flask_app

client = flask_app.app.test_client()


def assert_no_network_call(mock_get, mock_post, label):
    assert not mock_get.called, f"{label}: a GET request fired -- a server-side key was used without the caller supplying one!"
    assert not mock_post.called, f"{label}: a POST request fired -- a server-side key was used without the caller supplying one!"


# ---------------------------------------------------------------------
print("=== Test 1: /api/search with no api_key -- must reject, must make ZERO network calls ===")
with patch("vacant_land_search.requests.get") as mock_get, patch("vacant_land_search.requests.post") as mock_post:
    resp = client.post("/api/search", json={"state": "FL"})
    print(resp.status_code, resp.get_json())
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert_no_network_call(mock_get, mock_post, "/api/search")
print("ALL PASS -- REALIE_API_KEY being set in the environment did not matter at all")

# ---------------------------------------------------------------------
print("\n=== Test 2: /api/search with an EMPTY STRING api_key -- must also reject, not treat as 'use the default' ===")
with patch("vacant_land_search.requests.get") as mock_get, patch("vacant_land_search.requests.post") as mock_post:
    resp = client.post("/api/search", json={"state": "FL", "api_key": ""})
    print(resp.status_code, resp.get_json())
    assert resp.status_code == 400
    assert_no_network_call(mock_get, mock_post, "/api/search (empty string key)")
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 3: /api/skiptrace/<apn> with no tracerfy_api_key -- must reject, ZERO network calls ===")
raw = {"propertyIdentification": {"currentOwner": {
    "ownerStreet": "1 Main St", "ownerCity": "Austin", "ownerState": "TX", "ownerZipCode": "78701",
}}}
with patch("vacant_land_search.requests.get") as mock_get, patch("vacant_land_search.requests.post") as mock_post:
    resp = client.post("/api/skiptrace/TEST-APN", json={"raw": raw})
    print(resp.status_code, resp.get_json())
    assert resp.status_code == 400
    assert_no_network_call(mock_get, mock_post, "/api/skiptrace/<apn>")
print("ALL PASS -- TRACERFY_API_KEY being set in the environment did not matter at all")

# ---------------------------------------------------------------------
print("\n=== Test 4: /api/skiptrace/bulk with no tracerfy_api_key -- must reject, ZERO network calls ===")
with patch("vacant_land_search.requests.get") as mock_get, patch("vacant_land_search.requests.post") as mock_post:
    resp = client.post("/api/skiptrace/bulk", json={"parcels": [{"apn": "A1", "raw": raw}]})
    print(resp.status_code, resp.get_json())
    assert resp.status_code == 400
    assert_no_network_call(mock_get, mock_post, "/api/skiptrace/bulk")
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 5: supplying YOUR OWN key still works normally (this isn't broken, just the fallback) ===")
with patch("vacant_land_search.requests.get") as mock_get:
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"properties": [], "metadata": {}}
    mock_get.return_value = fake_resp
    resp = client.post("/api/search", json={"state": "FL", "api_key": "a-real-caller-supplied-key"})
    print(resp.status_code, resp.get_json())
    assert resp.status_code == 200
    assert mock_get.called, "supplying a real key should still actually search"
    # Confirm the key that was actually used is the CALLER's, not the
    # bogus server-side one sitting in the environment.
    used_headers = mock_get.call_args.kwargs.get("headers", {})
    assert used_headers.get("Authorization") == "a-real-caller-supplied-key", \
        f"expected the caller's own key to be used, got: {used_headers}"
print("ALL PASS -- the caller's own key is used, never the server's environment variable")

print("\n=== ALL NO-SHARED-KEY-USAGE TESTS PASSED ===")
print("REALIE_API_KEY, TRACERFY_API_KEY, API_KEY, REALIE_KEY, and TRACERFY_KEY were ALL set in the")
print("environment throughout this entire test run. Not one of them was ever used to make a real")
print("request on behalf of a caller who didn't supply their own key.")
