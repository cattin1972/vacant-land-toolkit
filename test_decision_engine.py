"""
Tests for the decision engine (build_decision_summary) that sits on top of
the evidence framework. Pure logic tests -- no network calls, no cost.
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

# ---------------------------------------------------------------------
print("=== Test 1: clean parcel, zoning covered -> Strong / GREEN ===")
evidence = v.build_evidence_report(CLEAN_BUILDABILITY, zoning={"source": "Miami-Dade County, FL", "zoning_code": "RU-1"}, tax_flags={"flagged": False}, owner_mailing={})
decision = v.build_decision_summary(evidence, CLEAN_BUILDABILITY, {"flagged": False})
print(decision["deal_potential"], decision["color"], "|", decision["headline"])
assert decision["deal_potential"] == "Strong", decision
assert decision["color"] == "GREEN"
assert any("12 buildings" in p for p in decision["top_positive_factors"])
assert not decision["deal_killer_risks"]
print("Recommended action:", decision["recommended_action"])
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 2: clean parcel, zoning NOT covered -> still Strong (zoning is grouped with the other")
print("    always-REQUIRES_VERIFICATION categories like legal access), but must be visibly called out ===")
evidence2 = v.build_evidence_report(CLEAN_BUILDABILITY, zoning={}, tax_flags={"flagged": False}, owner_mailing={})
decision2 = v.build_decision_summary(evidence2, CLEAN_BUILDABILITY, {"flagged": False})
print(decision2["deal_potential"], decision2["color"], "|", decision2["headline"])
print("Recommended action:", decision2["recommended_action"])
assert decision2["deal_potential"] == "Strong", decision2
assert any("Zoning" in r for r in decision2["top_risks"]), decision2["top_risks"]
assert "zoning" in decision2["recommended_action"].lower()
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 3: landlocked parcel -> Avoid / RED via hard-stop, exact recommended action ===")
landlocked = dict(CLEAN_BUILDABILITY)
landlocked["road_access"] = {"has_mapped_road_access": False, "nearby_roads": []}
evidence3 = v.build_evidence_report(landlocked, zoning={"source": "x", "zoning_code": "RU-1"}, tax_flags={"flagged": False}, owner_mailing={})
decision3 = v.build_decision_summary(evidence3, landlocked, {"flagged": False})
print(decision3["deal_potential"], decision3["color"], "|", decision3["recommended_action"])
assert decision3["deal_potential"] == "Avoid"
assert decision3["color"] == "RED"
assert decision3["recommended_action"] == "Do not spend time pursuing this parcel until legal access is verified."
assert any("Physical road access" in d for d in decision3["deal_killer_risks"])
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 4: mostly flood/wetland (severe usable-area loss) -> Avoid / RED via hard-stop ===")
flooded = dict(CLEAN_BUILDABILITY)
flooded["flood_and_wetland"] = {"total_area_acres": 2.0, "flood_or_wetland_area_acres": 1.8, "buildable_area_acres": 0.2, "buildability_score": 10.0}
evidence4 = v.build_evidence_report(flooded, zoning={"source": "x", "zoning_code": "RU-1"}, tax_flags={"flagged": False}, owner_mailing={})
decision4 = v.build_decision_summary(evidence4, flooded, {"flagged": False})
print(decision4["deal_potential"], decision4["color"], "|", decision4["recommended_action"])
assert decision4["deal_potential"] == "Avoid"
assert "flood/wetland impact" in decision4["recommended_action"]
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 5: tax flagged but otherwise clean -> Weak / YELLOW (not a hard stop, but a real concern), motivated-seller note present ===")
evidence5 = v.build_evidence_report(CLEAN_BUILDABILITY, zoning={"source": "x", "zoning_code": "RU-1"}, tax_flags={"flagged": True, "has_liens": True, "lien_count": 2}, owner_mailing={})
decision5 = v.build_decision_summary(evidence5, CLEAN_BUILDABILITY, {"flagged": True, "has_liens": True, "lien_count": 2})
print(decision5["deal_potential"], decision5["color"])
print("Deal-killer risks:", decision5["deal_killer_risks"])
assert any("motivated seller" in d for d in decision5["deal_killer_risks"])
assert decision5["deal_potential"] in ("Strong", "Moderate"), "a tax flag alone (not a hard-stop category) should not force Avoid/Weak by itself"
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 6: sparse/missing data -> Insufficient information / GRAY ===")
sparse = {
    "flood_and_wetland": None, "septic_suitability": None, "natural_hazard_risk": None,
    "road_access": None, "environmental_designations": None, "nearby_development": None,
    "slope": None, "contamination": None, "wildfire_fine_grained": None, "broadband": None,
    "concerns": [],
}
evidence6 = v.build_evidence_report(sparse, zoning={}, tax_flags={}, owner_mailing={})
decision6 = v.build_decision_summary(evidence6, sparse, {})
print(decision6["deal_potential"], decision6["color"], "|", decision6["headline"])
assert decision6["deal_potential"] == "Insufficient information"
assert decision6["color"] == "GRAY"
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 7: three independent (non-hard-stop) concerns stack up -> Avoid via concern-count, not a single category ===")
stacked = dict(CLEAN_BUILDABILITY)
stacked["septic_suitability"] = {"rating": "Very limited", "soil_name": "Wet clay", "coverage_pct": 80}
stacked["slope"] = {"relief_meters": 40, "approx_slope_percent": 35, "steep": True}
stacked["environmental_designations"] = {"in_coastal_barrier_resources_system": True, "cbrs_units": ["FL-12"], "in_critical_habitat": False, "critical_habitat_species": []}
evidence7 = v.build_evidence_report(stacked, zoning={"source": "x", "zoning_code": "RU-1"}, tax_flags={"flagged": False}, owner_mailing={})
decision7 = v.build_decision_summary(evidence7, stacked, {"flagged": False})
print(decision7["deal_potential"], decision7["color"], "|", decision7["recommended_action"])
assert decision7["deal_potential"] == "Avoid"
print("ALL PASS")

print("\n=== ALL DECISION ENGINE TESTS PASSED ===")
