"""
Regression test for a real bug found during the external-data-source
audit (2026-09-14): check_contamination_sites(), check_fine_grained_
wildfire_risk(), and check_broadband_availability() were computed by
get_comprehensive_buildability_report() all along, but build_evidence_report()
never read any of them -- a real Superfund site next door had ZERO effect
on the evidence report or the Deal Potential tier, only visible via the
raw-JSON toggle. Fixed by wiring contamination + wildfire in as real
findings, with "Environmental contamination" added to the decision
engine's hard-stop categories.
"""
import sys
sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v

CLEAN_BUILDABILITY = {
    "flood_and_wetland": {"total_area_acres": 2.0, "flood_or_wetland_area_acres": 0.0, "buildable_area_acres": 2.0, "buildability_score": 100.0},
    "septic_suitability": {"rating": "Not limited", "soil_name": "Astatula", "coverage_pct": 90},
    "natural_hazard_risk": {"overall_risk_rating": "Relatively Low", "hazards": {}},
    "road_access": {"has_mapped_road_access": True, "nearby_roads": ["Main St"]},
    "environmental_designations": {"in_coastal_barrier_resources_system": False, "cbrs_units": [], "in_critical_habitat": False, "critical_habitat_species": []},
    "nearby_development": {"nearby_building_count": 12, "likely_utilities_nearby": True},
    "slope": {"relief_meters": 0.5, "approx_slope_percent": 1.5, "steep": False},
    "contamination": {"superfund_sites_nearby": [], "brownfield_sites_nearby": []},
    "wildfire_fine_grained": {"raw_value": 100.0, "tier": "Low (rough estimate)"},
    "broadband": {"total_locations": 50, "served_locations": 48},
    "concerns": [],
}


def find(report, category):
    for f in report["findings"]:
        if f["category"] == category:
            return f
    raise AssertionError(f"No finding for category: {category}")


# ---------------------------------------------------------------------
print("=== Test 1: a real Superfund site nearby now forces Avoid, not just a footnote ===")
contaminated = dict(CLEAN_BUILDABILITY)
contaminated["contamination"] = {"superfund_sites_nearby": ["LOVE CANAL"], "brownfield_sites_nearby": []}
evidence = v.build_evidence_report(contaminated, zoning={"source": "x", "zoning_code": "RU-1"}, tax_flags={"flagged": False}, owner_mailing={})
decision = v.build_decision_summary(evidence, contaminated, {"flagged": False})
f = find(evidence, "Environmental contamination (Superfund/Brownfields)")
print(f["status"], "|", f["establishes"])
assert f["status"] == v.STATUS_CONCERN
assert "LOVE CANAL" in f["establishes"]
assert "Environmental contamination (Superfund/Brownfields)" in evidence["what_could_kill_the_deal"]
print("Decision:", decision["deal_potential"], decision["color"])
assert decision["deal_potential"] == "Avoid", "a confirmed nearby Superfund site must be a hard stop"
assert any("contamination" in r.lower() or "superfund" in r.lower() for r in decision["deal_killer_risks"])
print("ALL PASS -- this was previously invisible to the decision engine entirely")

# ---------------------------------------------------------------------
print("\n=== Test 2: one EPA source down does not get reported as 'confirmed clean' ===")
partial_fail = dict(CLEAN_BUILDABILITY)
partial_fail["contamination"] = {"superfund_sites_nearby": None, "brownfield_sites_nearby": []}  # Superfund lookup failed
evidence2 = v.build_evidence_report(partial_fail, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
f2 = find(evidence2, "Environmental contamination (Superfund/Brownfields)")
print(f2["status"], "|", f2["evidence_level"], "|", f2["establishes"])
assert f2["status"] != v.STATUS_CLEAR, "a half-failed check must not read as a clean result"
assert "unknown" in f2["establishes"].lower() or "could not be checked" in f2["establishes"].lower()
print("ALL PASS -- partial source failure is disclosed, not hidden behind the other source's real answer")

# ---------------------------------------------------------------------
print("\n=== Test 3: both EPA sources down -> honest no-data finding, never a false negative ===")
both_fail = dict(CLEAN_BUILDABILITY)
both_fail["contamination"] = {"superfund_sites_nearby": None, "brownfield_sites_nearby": None}
evidence3 = v.build_evidence_report(both_fail, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
f3 = find(evidence3, "Environmental contamination (Superfund/Brownfields)")
print(f3["status"], "|", f3["evidence_level"])
assert f3["status"] == v.STATUS_UNKNOWN
assert f3["evidence_level"] == v.EVIDENCE_UNKNOWN
assert "does not mean" in f3["does_not_establish"].lower() or "absence" in f3["does_not_establish"].lower()
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 4: fine-grained wildfire risk is now a real finding, capped at INDICATED (never LIKELY) ===")
wildfire_high = dict(CLEAN_BUILDABILITY)
wildfire_high["wildfire_fine_grained"] = {"raw_value": 8000.0, "tier": "High (rough estimate)"}
evidence4 = v.build_evidence_report(wildfire_high, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
f4 = find(evidence4, "Wildfire risk (fine-grained)")
print(f4["status"], "|", f4["evidence_level"])
assert f4["status"] == v.STATUS_CONCERN
assert f4["evidence_level"] == v.EVIDENCE_INDICATED, "USFS tier cutoffs are this toolkit's own approximation, never claim LIKELY/VERIFIED"
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 5: wildfire with no usable value (water/urban) is UNKNOWN, not silently clean ===")
no_wildfire_data = dict(CLEAN_BUILDABILITY)
no_wildfire_data["wildfire_fine_grained"] = {"raw_value": None, "tier": "No data (water/urban/non-burnable)"}
evidence5 = v.build_evidence_report(no_wildfire_data, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
f5 = find(evidence5, "Wildfire risk (fine-grained)")
assert f5["status"] == v.STATUS_UNKNOWN
print("ALL PASS")

print("\n=== ALL CONTAMINATION/WILDFIRE WIRING TESTS PASSED ===")

# ---------------------------------------------------------------------
print("\n=== Test 6: septic rating=None (unexpected schema) is UNKNOWN, never 'rated None' with false confidence ===")
weird_septic = dict(CLEAN_BUILDABILITY)
weird_septic["septic_suitability"] = {"rating": None, "soil_name": "Unknown", "coverage_pct": None}
evidence6 = v.build_evidence_report(weird_septic, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
f6 = find(evidence6, "Septic / sanitation")
print(f6["status"], "|", f6["evidence_level"], "|", f6["establishes"])
assert f6["evidence_level"] == v.EVIDENCE_UNKNOWN, "an unrecognized/missing rating must never be EVIDENCE_LIKELY"
assert "'None'" not in f6["establishes"], "must never display the literal Python None to a customer"
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 7: SOURCE UNAVAILABLE vs DATA UNAVAILABLE are distinguished, not both generic 'no data' ===")
source_down = dict(CLEAN_BUILDABILITY)
source_down["septic_suitability"] = None  # the API call itself failed
evidence7a = v.build_evidence_report(source_down, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
f7a = find(evidence7a, "Septic / sanitation")
print("source failure  ->", f7a["quick_label"])
assert f7a["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE

no_coverage = dict(CLEAN_BUILDABILITY)
no_coverage["septic_suitability"] = {}  # the API responded fine, just no data for this point
evidence7b = v.build_evidence_report(no_coverage, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
f7b = find(evidence7b, "Septic / sanitation")
print("no coverage     ->", f7b["quick_label"])
assert f7b["quick_label"] == v.QUICK_LABEL_DATA_UNAVAILABLE
assert f7a["quick_label"] != f7b["quick_label"], "a failed request and a confirmed coverage gap must not look identical"
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 8: every structurally-unanswerable category is labeled VERIFY MANUALLY ===")
evidence8 = v.build_evidence_report(CLEAN_BUILDABILITY, zoning={}, tax_flags={}, owner_mailing={})
for cat in ["Legal road access", "Parcel frontage", "Easements",
            "Minimum lot size, setbacks & frontage requirements", "Subdivision restrictions"]:
    f8 = find(evidence8, cat)
    assert f8["quick_label"] == v.QUICK_LABEL_VERIFY_MANUALLY, cat
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 9: a finding with real, usable data carries no quick_label at all ===")
evidence9 = v.build_evidence_report(CLEAN_BUILDABILITY, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
f9 = find(evidence9, "Wetlands")
print(f9["status"], "|", f9["quick_label"])
assert f9["quick_label"] is None, "a real, usable CLEAR finding should not be buried under an unavailability label"
print("ALL PASS")

print("\n=== ALL QUICK-LABEL VOCABULARY TESTS PASSED ===")

# ---------------------------------------------------------------------
print("\n=== Test 10: zoning lookup FAILURE is never confused with confirmed non-coverage ===")
evidence10a = v.build_evidence_report(CLEAN_BUILDABILITY, zoning={"lookup_failed": True}, tax_flags={"flagged": False}, owner_mailing={})
z10a = find(evidence10a, "Zoning")
print("lookup failed ->", z10a["evidence_level"], "|", z10a["quick_label"], "|", z10a["establishes"])
assert z10a["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE
assert "outside the 2 counties" not in z10a["establishes"], "a failed lookup must not claim confirmed non-coverage"

evidence10b = v.build_evidence_report(CLEAN_BUILDABILITY, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
z10b = find(evidence10b, "Zoning")
print("confirmed not covered ->", z10b["evidence_level"], "|", z10b["quick_label"], "|", z10b["establishes"])
assert z10b["quick_label"] == v.QUICK_LABEL_DATA_UNAVAILABLE
assert "outside the 2 counties" in z10b["establishes"]
print("ALL PASS -- a real government-source hiccup no longer masquerades as 'you're just not covered'")

print("\n=== ALL ZONING FAILURE-HANDLING TESTS PASSED ===")
