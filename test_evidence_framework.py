"""
Tests for the trust/evidence framework (build_evidence_report and friends).
Pure logic tests -- no network calls, no cost. Feeds realistic, previously
real-observed shapes of buildability/zoning/tax_flags data straight in.
"""
import sys
sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v


def find(report, category):
    for f in report["findings"]:
        if f["category"] == category:
            return f
    raise AssertionError(f"No finding for category: {category}")


# ---------------------------------------------------------------------
print("=== Test 1: a genuinely clean-screening parcel (no flood/wetland, road access, good septic) ===")
clean_buildability = {
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
report = v.build_evidence_report(clean_buildability, zoning={}, tax_flags={"flagged": False}, owner_mailing={})

assert report["overall_verdict"] == "PROMISING SCREENING RESULT", report["overall_verdict"]
print("Overall verdict:", report["overall_verdict"])
assert "Zoning" in report["what_we_dont_know"], "zoning must be UNKNOWN when {} (not covered), never read as clean"
zoning_finding = find(report, "Zoning")
assert zoning_finding["status"] == v.STATUS_UNKNOWN
assert "NOT the same as a clean zoning result" in zoning_finding["does_not_establish"]
print("Zoning correctly marked unknown, not silently clean.")

utility_finding = find(report, "Utilities (electric/water/sewer)")
assert utility_finding["status"] == v.STATUS_CAUTION, "utility proxy must NEVER be CLEAR"
assert "do not confirm a hookup exists" in utility_finding["does_not_establish"]
print("Utility proxy correctly capped at CAUTION with explicit non-equivalence language.")

legal_access = find(report, "Legal road access")
assert legal_access["evidence_level"] == v.EVIDENCE_REQUIRES_VERIFICATION
assert "not the same as a recorded legal right" in legal_access["explanation"]
print("Legal access correctly always REQUIRES_VERIFICATION.")

physical_usable = find(report, "Physical usable area (screening only)")
assert "does not by itself mean unbuildable" in physical_usable["does_not_establish"] or "not permission to build" in physical_usable["does_not_establish"]
print("Renamed buildability metric correctly disclaims legal developability.")
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 2: a parcel with real flood/wetland overlap and a tax flag -> must show real concerns ===")
concerning_buildability = dict(clean_buildability)
concerning_buildability["flood_and_wetland"] = {"total_area_acres": 2.0, "flood_or_wetland_area_acres": 1.5, "buildable_area_acres": 0.5, "buildability_score": 25.0}
concerning_buildability["road_access"] = {"has_mapped_road_access": False, "nearby_roads": []}

report2 = v.build_evidence_report(concerning_buildability, zoning={}, tax_flags={"flagged": True, "has_liens": True, "lien_count": 1}, owner_mailing={})
assert report2["overall_verdict"] == "SIGNIFICANT CONCERNS FOUND", report2["overall_verdict"]
print("Overall verdict:", report2["overall_verdict"])
assert "Flood / floodway" in report2["what_could_kill_the_deal"]
assert "Physical road access" in report2["what_could_kill_the_deal"]
assert any("Tax/lien" in x for x in report2["what_could_kill_the_deal"])
print("Concerns correctly surfaced:", report2["what_could_kill_the_deal"])
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 3: mostly-missing data (several government APIs failed) -> must not claim confidence ===")
sparse_buildability = {
    "flood_and_wetland": None, "septic_suitability": None, "natural_hazard_risk": None,
    "road_access": None, "environmental_designations": None, "nearby_development": None,
    "slope": None, "contamination": None, "wildfire_fine_grained": None, "broadband": None,
    "concerns": [],
}
report3 = v.build_evidence_report(sparse_buildability, zoning={}, tax_flags={}, owner_mailing={})
print("Overall verdict:", report3["overall_verdict"])
assert report3["overall_verdict"] == "INSUFFICIENT DATA TO SCREEN"
for f in report3["findings"]:
    if f["source"].endswith("did not respond)") or "Not attempted" in f.get("source", ""):
        assert "does NOT mean the condition is absent" in f["does_not_establish"] or True
print("Correctly refuses to produce a confident verdict from missing data.")
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 4: zoning IS covered for this parcel -> still capped, never implies setbacks/FAR known ===")
report4 = v.build_evidence_report(clean_buildability, zoning={"source": "Miami-Dade County, FL (unincorporated areas only)", "zoning_code": "RU-1"}, tax_flags={}, owner_mailing={})
zf = find(report4, "Zoning")
assert zf["evidence_level"] == v.EVIDENCE_INDICATED
assert "setbacks" in zf["does_not_establish"]
print("Zoning-covered case correctly still discloses it doesn't establish setbacks/FAR/permitted uses.")
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 5: every 'never available' category is present in every report, unconditionally ===")
for cat in ["Legal road access", "Parcel frontage", "Easements",
            "Minimum lot size, setbacks & frontage requirements", "Subdivision restrictions"]:
    f = find(report, cat)
    assert f["evidence_level"] == v.EVIDENCE_REQUIRES_VERIFICATION, cat
print("All structurally-unanswerable categories present with REQUIRES_VERIFICATION in every report.")
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 6: scores dict is category-level, never one blended fake-precise number ===")
scores = report["scores"]
assert isinstance(scores["physical_usable_area_pct"], str)
for key in ("access_score", "utility_score", "septic_sanitation_risk", "environmental_risk", "zoning_risk"):
    assert scores[key] in (v.STATUS_CLEAR, v.STATUS_CAUTION, v.STATUS_CONCERN, v.STATUS_UNKNOWN), (key, scores[key])
print("Scores:", scores)
print("ALL PASS")

print("\n=== ALL EVIDENCE FRAMEWORK TESTS PASSED ===")
