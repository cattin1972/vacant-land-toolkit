"""
Tests for build_instant_priority() -- the free, zero-network-call ranking
signal computed for every search result the moment a search returns.
"""
import sys
sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v

# ---------------------------------------------------------------------
print("=== Test 1: strong motivated-seller profile -- all 4 signals present ===")
p = v.build_instant_priority(
    {"flagged": True, "lien_count": 2, "foreclosure_count": 0},
    {"owner_type": "individual", "ownership_years": 22},
    {"mail_city": "Brooklyn", "mail_state": "NY"},
    "Cape Coral", "FL",
)
print(p)
assert p["instant_score"] == 25 + 15 + 15 + 10, p["instant_score"]
assert len(p["reasons"]) == 4
assert not p["negative_reasons"]
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 2: unremarkable parcel -- no signals, all negatives explained ===")
p2 = v.build_instant_priority(
    {"flagged": False},
    {"owner_type": "corporate", "ownership_years": 1.0},
    {"mail_city": "Cape Coral", "mail_state": "FL"},
    "Cape Coral", "FL",
)
print(p2)
assert p2["instant_score"] == 0
assert not p2["reasons"]
assert len(p2["negative_reasons"]) == 5  # no tax flag, not absentee, recent purchase, corporate owner, + the summary line
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 3: missing data (no sale history, no mailing address) -> no crash, no false claims ===")
p3 = v.build_instant_priority({}, {}, {}, "Cape Coral", "FL")
print(p3)
assert p3["instant_score"] == 0
assert "No distress, absentee-ownership, or long-tenure signals" in p3["negative_reasons"][-1]
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 4: same-state, different-city absentee gets a smaller bump than out-of-state ===")
in_state = v.build_instant_priority({}, {}, {"mail_city": "Miami", "mail_state": "FL"}, "Cape Coral", "FL")
out_state = v.build_instant_priority({}, {}, {"mail_city": "Brooklyn", "mail_state": "NY"}, "Cape Coral", "FL")
print("in-state:", in_state["instant_score"], "| out-of-state:", out_state["instant_score"])
assert 0 < in_state["instant_score"] < out_state["instant_score"]
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 5: foreclosure-only (no liens) still scores and is itemized separately from liens ===")
p5 = v.build_instant_priority({"flagged": True, "lien_count": 0, "foreclosure_count": 1}, {}, {}, "x", "FL")
print(p5)
assert p5["instant_score"] == 20
assert "Foreclosure" in p5["reasons"][0]
print("ALL PASS")

print("\n=== ALL INSTANT PRIORITY TESTS PASSED ===")
