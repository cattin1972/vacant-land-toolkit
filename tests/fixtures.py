"""
Representative parcel-analysis fixtures.

Every shape here matches what the real functions this toolkit calls
actually return (or fail to return) -- confirmed by reading
build_evidence_report's own field reads in vacant_land_search.py, not
guessed. Four families, used across tests/test_dangerous_false_conclusions.py,
tests/test_conflicting_data.py, and tests/test_backend_contracts.py:

  - COMPLETE_CLEAN_*   -- every optional data source succeeded, nothing concerning
  - ALL_MISSING_*       -- every optional data source failed outright (None):
                           the "total API outage" scenario
  - CONFLICTING_*       -- two real signals genuinely disagree
  - single-category variants for the specific dangerous-false-conclusion
    scenarios this project's testing requirements name explicitly
"""

REALIE_RAW_VACANT_LOT = {
    "parcelId": "TEST-APN-0001",
    "realieParcelId": "TEST-APN-0001",
    "latitude": 26.6406,
    "longitude": -81.9873,
    "propertyLocation": {"addressLine1": "123 Test Rd", "city": "Cape Coral", "state": "FL", "zipCode": "33990"},
    "propertyClassification": {"propertyUseCode": 8001},
    "buildingInformation": {"buildings": [{"buildingArea": 0}]},
    "landInformation": {"calculatedAcres": 5.0},
    "salesHistory": [{"saleDate": "2020-01-01", "salePrice": 15000, "grantee": "Jane Individual Doe"}],
    "valuationInformation": {"totalAssessedValue": 42000, "totalLandValue": 42000},
    "taxInformation": {"taxAmount": 900, "taxYear": 2025},
    "propertyIdentification": {
        "currentOwner": {
            "ownerStreet": "1 Elsewhere Ave", "ownerCity": "Austin",
            "ownerState": "TX", "ownerZipCode": "78701",
        }
    },
}

REALIE_RAW_LARGE_VACANT_LOT = {
    **REALIE_RAW_VACANT_LOT,
    "parcelId": "TEST-APN-LARGE",
    "landInformation": {"calculatedAcres": 40.0},
}

MINIMAL_TAX_FLAGS_CLEAN = {"flagged": False, "lien_count": 0, "foreclosure_count": 0}
MINIMAL_TAX_FLAGS_FLAGGED = {"flagged": True, "lien_count": 1, "foreclosure_count": 0}
# Conflicting: flagged True but the counts that supposedly justified it are
# both zero -- a real internal-consistency scenario get_tax_flags() must
# never produce this way (see test_conflicting_data.py), used here as an
# input to prove any DOWNSTREAM consumer of tax_flags doesn't quietly trust
# the "flagged" boolean over the actual counts either.
CONFLICTING_TAX_FLAGS = {"flagged": True, "lien_count": 0, "foreclosure_count": 0}

MINIMAL_MAILING = {
    "mail_street": "1 Elsewhere Ave", "mail_city": "Austin", "mail_state": "TX", "mail_zip": "78701",
}
MAILING_MISSING = {}

COMPLETE_CLEAN_BUILDABILITY = {
    "road_access": {"has_mapped_road_access": True, "nearby_roads": ["Test Rd"]},
    "nearby_development": {"nearby_building_count": 12, "likely_utilities_nearby": True},
    "septic_suitability": {"rating": "Not limited", "soil_name": "Test Soil", "coverage_pct": 95},
    "flood_and_wetland": {"flood_or_wetland_area_acres": 0.0, "total_area_acres": 5.0, "buildability_score": 100.0},
    "slope": {"steep": False, "approx_slope_percent": 2.1},
    "environmental_designations": {"in_coastal_barrier_resources_system": False, "in_critical_habitat": False},
    "contamination": {"superfund_sites_nearby": [], "brownfield_sites_nearby": []},
    "wildfire_fine_grained": {"tier": "Low", "raw_value": 1},
}

# Every optional external check failed outright (the API/network calls
# themselves errored) -- must NEVER be treated as "nothing concerning found."
ALL_MISSING_BUILDABILITY = {
    "road_access": None,
    "nearby_development": None,
    "septic_suitability": None,
    "flood_and_wetland": None,
    "slope": None,
    "environmental_designations": None,
    "contamination": None,
    "wildfire_fine_grained": None,
}

CLEAN_ZONING_COVERED = {"zoning_code": "AG-2", "source": "Miami-Dade County zoning GIS"}
ZONING_NOT_COVERED = {}
ZONING_LOOKUP_FAILED = {"lookup_failed": True}

# Road adjacency IS known (a mapped road touches the boundary) but legal
# access is, by design, never checkable by this toolkit -- see the
# dangerous-false-conclusion test that uses this.
ROAD_ADJACENT_BUILDABILITY = {
    **COMPLETE_CLEAN_BUILDABILITY,
    "road_access": {"has_mapped_road_access": True, "nearby_roads": ["Test Rd", "Second St"]},
}

# Utility PROXIMITY is known (lots of nearby buildings) but service
# AVAILABILITY at this specific parcel is, by design, never checkable.
UTILITY_PROXIMITY_HIGH_BUILDABILITY = {
    **COMPLETE_CLEAN_BUILDABILITY,
    "nearby_development": {"nearby_building_count": 40, "likely_utilities_nearby": True},
}

# A genuinely large, physically spacious parcel, but zoning is one of the
# many U.S. locations this toolkit has no zoning coverage for at all.
LARGE_PARCEL_ZONING_UNKNOWN_ACRES = 40.0
