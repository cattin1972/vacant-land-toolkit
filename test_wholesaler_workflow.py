"""
Tests for the CONTACT OWNER / OFFER-NEGOTIATE layer: build_buyer_fit_tags
and build_owner_outreach_brief. Pure logic tests -- no network calls.
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
    "nearby_development": {"nearby_building_count": 20, "likely_utilities_nearby": True},
    "slope": {"relief_meters": 0.5, "approx_slope_percent": 1.5, "steep": False},
    "contamination": {"superfund_sites_nearby": [], "brownfield_sites_nearby": []},
    "wildfire_fine_grained": {"raw_value": 100.0, "tier": "Low (rough estimate)"},
    "broadband": {"total_locations": 50, "served_locations": 48},
    "concerns": [],
}

# ---------------------------------------------------------------------
print("=== Test 1: small clean parcel with lots of nearby development -> fits owner-builder + infill tags ===")
evidence = v.build_evidence_report(CLEAN_BUILDABILITY, zoning={"source": "x", "zoning_code": "RU-1"}, tax_flags={"flagged": False}, owner_mailing={})
tags = v.build_buyer_fit_tags(1.5, CLEAN_BUILDABILITY, evidence)
tag_names = [t["tag"] for t in tags]
print(tag_names)
assert "Owner-builder / small residential lot buyers" in tag_names
assert "Infill / near-town buyers" in tag_names
assert all(t["reason"] for t in tags), "every tag must carry a concrete reason"
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 2: large rural parcel -> land banking tag, not owner-builder ===")
rural = dict(CLEAN_BUILDABILITY)
rural["nearby_development"] = {"nearby_building_count": 1, "likely_utilities_nearby": False}
evidence2 = v.build_evidence_report(rural, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
tags2 = v.build_buyer_fit_tags(25.0, rural, evidence2)
tag_names2 = [t["tag"] for t in tags2]
print(tag_names2)
assert "Land banking / hold-for-appreciation buyers" in tag_names2
assert "Owner-builder / small residential lot buyers" not in tag_names2
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 3: no acreage/evidence data -> graceful 'not enough data' tag, no crash ===")
tags3 = v.build_buyer_fit_tags(None, {}, {})
print(tags3)
assert len(tags3) == 1 and "Not enough data" in tags3[0]["tag"]
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 4: outreach brief -- landlocked parcel triggers the road-access question ===")
landlocked = dict(CLEAN_BUILDABILITY)
landlocked["road_access"] = {"has_mapped_road_access": False, "nearby_roads": []}
evidence4 = v.build_evidence_report(landlocked, zoning={}, tax_flags={"flagged": True, "has_liens": True, "lien_count": 3}, owner_mailing={"mail_city": "Brooklyn", "mail_state": "NY"})
decision4 = v.build_decision_summary(evidence4, landlocked, {"flagged": True, "has_liens": True, "lien_count": 3})
priority4 = v.build_instant_priority({"flagged": True, "lien_count": 3}, {"owner_type": "individual", "ownership_years": 15}, {"mail_city": "Brooklyn", "mail_state": "NY"}, "Cape Coral", "FL")
brief = v.build_owner_outreach_brief(
    {"owner_name": "JANE DOE", "owner_type": "individual", "ownership_years": 15},
    {"mail_city": "Brooklyn", "mail_state": "NY"},
    {"flagged": True, "has_liens": True, "lien_count": 3},
    priority4, decision4, evidence4, 5.0, "Cape Coral", "FL",
)
print(brief)
assert any("get to the property" in q.lower() for q in brief["questions_to_ask"])
assert any("lien" in q.lower() or "tax" in q.lower() for q in brief["questions_to_ask"])
assert "out of state" in brief["absentee_note"]
assert brief["disclaimer"] == v.OUTREACH_DISCLAIMER
assert "not verified" in brief["disclaimer"].lower()
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 5: outreach brief never fabricates data it doesn't have ===")
brief2 = v.build_owner_outreach_brief({}, {}, {}, {}, {}, {}, None, None, None)
print(brief2)
assert brief2["owner_summary"] == "Unknown owner name"
assert brief2["absentee_note"] is None
assert "sole owner" in brief2["questions_to_ask"][-1].lower() or len(brief2["questions_to_ask"]) >= 1
print("ALL PASS")

print("\n=== ALL WHOLESALER WORKFLOW TESTS PASSED ===")
