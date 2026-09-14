"""
Regression tests for check_nearby_development() (OpenStreetMap Overpass) and
for the "silent omission" bug found during the 2026-09-14 regression
checkpoint and fixed in build_evidence_report() during this session.

This file used to be a stale, ad-hoc manual script: it hand-copied its own
duplicate of the Overpass query instead of calling the application's real
check_nearby_development() function, printed raw output with no assertions,
and made live network calls every time it ran -- so it never actually
verified retry/backoff behavior, never verified failure handling, and could
not be trusted to catch a real regression (a flaky Overpass response would
just print something different, not fail). Replaced with real assertions
against the actual app code, using mocks for the deterministic cases.

THE BUG THIS FILE NOW GUARDS AGAINST:
build_evidence_report() previously OMITTED the "Nearby development /
surrounding houses" and "Physical usable area (screening only)" categories
entirely from its findings list whenever the underlying check (Overpass /
flood-wetland) failed to return data -- instead of recording them as
UNKNOWN like every other category in the report. That shrank the
screenable-findings denominator the decision engine divides by, which meant
a transient network hiccup on either check could shift a parcel's computed
deal-potential tier, even though nothing about the parcel itself changed.
Reproduced live at a real coordinate during the regression checkpoint: 17
findings one run (Nearby development missing, Overpass failed), 18 findings
the next (present, Overpass succeeded) -- same parcel, same request.
"""
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v


def fake_overpass_response(total, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.text = f"error body (status {status})"
    resp.json.return_value = {"elements": [{"tags": {"total": str(total)}}]}
    return resp


# ---------------------------------------------------------------------
print("=== Test 1: check_nearby_development succeeds on the first try ===")
with patch("vacant_land_search.requests.post") as mock_post:
    mock_post.return_value = fake_overpass_response(42)
    result = v.check_nearby_development(30.2900, -97.7400)
    assert result == {"nearby_building_count": 42, "likely_utilities_nearby": True}
    assert mock_post.call_count == 1
    # A real, identifying User-Agent must be sent -- Overpass's shared
    # public server rejects requests with no User-Agent at all (406).
    _, kwargs = mock_post.call_args
    assert "User-Agent" in kwargs.get("headers", {}), "Overpass call is missing its User-Agent header"
print("OK -- single successful call, correct shape returned")
print("ALL PASS")

print("\n=== Test 2: a near-zero count is reported honestly (not silently rounded up) ===")
with patch("vacant_land_search.requests.post") as mock_post:
    mock_post.return_value = fake_overpass_response(0)
    result = v.check_nearby_development(32.2500, -101.4800)
    assert result == {"nearby_building_count": 0, "likely_utilities_nearby": False}
print("OK -- a genuinely rural/undeveloped area is reported as 0, not conflated with a failed lookup")
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 3: transient failures are retried, and a later success is used ===")
with patch("vacant_land_search.requests.post") as mock_post, patch("vacant_land_search.time.sleep") as mock_sleep:
    mock_post.side_effect = [
        fake_overpass_response(0, status=500),
        fake_overpass_response(0, status=503),
        fake_overpass_response(17, status=200),
    ]
    result = v.check_nearby_development(30.0, -97.0)
    assert result["nearby_building_count"] == 17
    assert mock_post.call_count == 3, "should have retried twice before the third call succeeded"
    assert mock_sleep.call_count == 2, "should sleep with backoff between retries, not hammer the server"
print("OK -- recovered from 2 transient failures and used the successful 3rd response")
print("ALL PASS")

print("\n=== Test 4: exhausting all 4 attempts raises (does not silently return a fake zero) ===")
with patch("vacant_land_search.requests.post") as mock_post, patch("vacant_land_search.time.sleep") as mock_sleep:
    mock_post.return_value = fake_overpass_response(0, status=500)
    try:
        v.check_nearby_development(30.0, -97.0)
        print("FAIL: expected an exception after exhausting all retries")
        assert False
    except RuntimeError as e:
        assert "500" in str(e)
    assert mock_post.call_count == 4, f"expected exactly 4 attempts, got {mock_post.call_count}"
print("OK -- raises a real error after 4 failed attempts instead of pretending to have an answer")
print("ALL PASS")

# ---------------------------------------------------------------------
# The actual regression fix: build_evidence_report must record these two
# categories as UNKNOWN (never omit them) regardless of whether the
# underlying check succeeded -- so the total finding count, and therefore
# the decision engine's denominator, is stable across identical requests.
# ---------------------------------------------------------------------
NEARBY_CATEGORY = "Nearby development / surrounding houses"
PHYSICAL_CATEGORY = "Physical usable area (screening only)"

MINIMAL_TAX_FLAGS = {"flagged": False}
MINIMAL_ZONING = {}
MINIMAL_MAILING = {}


def findings_by_category(buildability):
    report = v.build_evidence_report(buildability, MINIMAL_ZONING, MINIMAL_TAX_FLAGS, MINIMAL_MAILING)
    return {f["category"]: f for f in report["findings"]}, report


print("\n=== Test 5: 'Nearby development / surrounding houses' is never silently omitted ===")
by_cat_missing, report_missing = findings_by_category({})
by_cat_present, report_present = findings_by_category({
    "nearby_development": {"nearby_building_count": 12, "likely_utilities_nearby": True},
})
assert NEARBY_CATEGORY in by_cat_missing, "category must be present (as UNKNOWN) even when Overpass failed"
assert by_cat_missing[NEARBY_CATEGORY]["status"] == v.STATUS_UNKNOWN
assert by_cat_missing[NEARBY_CATEGORY]["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE
assert NEARBY_CATEGORY in by_cat_present
assert by_cat_present[NEARBY_CATEGORY]["status"] == v.STATUS_CAUTION
# The exact bug: the finding COUNT must not depend on whether this one
# flaky source happened to succeed -- only its status should differ.
assert len(report_missing["findings"]) == len(report_present["findings"]), (
    f"finding count drifted with data availability: {len(report_missing['findings'])} vs "
    f"{len(report_present['findings'])} -- this is the exact bug found in the regression checkpoint"
)
print(f"OK -- category present either way; finding count stable at {len(report_missing['findings'])} both times")
print("ALL PASS")

print("\n=== Test 6: 'Physical usable area (screening only)' is never silently omitted ===")
by_cat_missing2, report_missing2 = findings_by_category({})
by_cat_present2, report_present2 = findings_by_category({
    "flood_and_wetland": {"flood_or_wetland_area_acres": 0.0, "total_area_acres": 5.0, "buildability_score": 100},
})
assert PHYSICAL_CATEGORY in by_cat_missing2, "category must be present (as UNKNOWN) even when the flood/wetland check failed"
assert by_cat_missing2[PHYSICAL_CATEGORY]["status"] == v.STATUS_UNKNOWN
assert by_cat_missing2[PHYSICAL_CATEGORY]["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE
assert PHYSICAL_CATEGORY in by_cat_present2
assert by_cat_present2[PHYSICAL_CATEGORY]["status"] == v.STATUS_CLEAR
assert len(report_missing2["findings"]) == len(report_present2["findings"]), (
    f"finding count drifted with data availability: {len(report_missing2['findings'])} vs "
    f"{len(report_present2['findings'])} -- this is the exact bug found in the regression checkpoint"
)
print(f"OK -- category present either way; finding count stable at {len(report_missing2['findings'])} both times")
print("ALL PASS")

print("\n=== Test 7: an UNKNOWN status here can never itself force a hard-stop 'Avoid' tier ===")
# _HARD_STOP_CATEGORIES only fires on a CONFIRMED status=CONCERN (see
# build_decision_summary) -- this fix adds an UNKNOWN finding, which must
# fall into "unknowns", never "concerns". Verified structurally, not just
# by re-deriving the same logic being tested.
assert by_cat_missing[NEARBY_CATEGORY]["status"] != v.STATUS_CONCERN
assert by_cat_missing2[PHYSICAL_CATEGORY]["status"] != v.STATUS_CONCERN
decision = v.build_decision_summary(report_missing2, {}, MINIMAL_TAX_FLAGS)
assert decision.get("deal_potential") != "Avoid", (
    "a total data outage on these optional checks must never, by itself, force an Avoid tier -- "
    f"got: {decision.get('deal_potential')}"
)
print(f"OK -- all-data-missing case resolved to '{decision.get('deal_potential')}', not a false Avoid")
print("ALL PASS")

print("\n=== ALL check_nearby_development / silent-omission REGRESSION TESTS PASSED ===")

# ---------------------------------------------------------------------
# Best-effort LIVE smoke check against the real public Overpass API.
# Not asserted and never fails this suite -- Overpass is a shared public
# service this toolkit doesn't control, and a transient outage there is
# not a bug in this application. This exists purely so a genuine schema
# change on Overpass's end (not just an outage) is at least visible
# in this file's output the next time someone runs it.
# ---------------------------------------------------------------------
print("\n=== Live smoke check (best-effort, not asserted, network access required) ===")
try:
    live_developed = v.check_nearby_development(30.2900, -97.7400)  # Austin, TX residential area
    print("Developed neighborhood (Austin, TX):", live_developed)
    time.sleep(1)  # be polite to the shared public Overpass instance
    live_rural = v.check_nearby_development(32.2500, -101.4800)  # remote West Texas farmland
    print("Remote farmland (West Texas):", live_rural)
    if live_developed["nearby_building_count"] <= live_rural["nearby_building_count"]:
        print("NOTE: developed area did not show a higher building count than rural farmland -- "
              "worth a manual look, but not treated as a hard failure (real map data can surprise you).")
    else:
        print("OK -- developed area shows meaningfully more nearby buildings than rural farmland, as expected.")
except Exception as e:
    print(f"SKIPPED -- live Overpass call failed ({type(e).__name__}: {e}); this is a live-network "
          f"dependency issue, not something this test suite can control.")
