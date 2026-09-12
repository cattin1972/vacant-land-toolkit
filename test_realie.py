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
print("=== Test 1: _is_genuinely_vacant -- the safety-critical hard filter ===")

truly_vacant = {
    # Matches real Realie API shape (confirmed live 2026-09-12): even a
    # genuinely vacant parcel gets ONE placeholder entry in buildings,
    # all zeros -- there is no such thing as an empty buildings list in
    # real data. This is why the filter checks total building AREA, not
    # array length.
    "propertyClassification": {"propertyUseCode": 8001},
    "buildingInformation": {"buildings": [{"buildingArea": 0, "actualYearBuilt": None}]},
}
has_a_house = {
    "propertyClassification": {"propertyUseCode": 1001},  # residential w/ structure
    "buildingInformation": {"buildings": [{"buildingArea": 1850, "actualYearBuilt": 1998}]},
}
mismatch_code_says_vacant_but_has_building = {
    # a stale/wrong assessor code -- the building-area check must catch this
    "propertyClassification": {"propertyUseCode": 8001},
    "buildingInformation": {"buildings": [{"buildingArea": 1200, "actualYearBuilt": 2005}]},
}
mismatch_no_buildings_but_code_not_vacant = {
    # real edge case seen live: residential-coded parcel with no
    # structure built yet -- still excluded, since the use code says
    # non-vacant (both signals must agree)
    "propertyClassification": {"propertyUseCode": 1001},
    "buildingInformation": {"buildings": [{"buildingArea": 0, "actualYearBuilt": None}]},
}
under_construction = {
    "propertyClassification": {"propertyUseCode": 8014},
    "buildingInformation": {"buildings": [{"buildingArea": 0, "actualYearBuilt": None}]},
}
missing_fields = {}

print("truly vacant (code+no buildings):", v._is_genuinely_vacant(truly_vacant), "(expect True)")
print("has a house:", v._is_genuinely_vacant(has_a_house), "(expect False)")
print("stale code but has building:", v._is_genuinely_vacant(mismatch_code_says_vacant_but_has_building), "(expect False -- must catch this)")
print("no buildings but non-vacant code:", v._is_genuinely_vacant(mismatch_no_buildings_but_code_not_vacant), "(expect False)")
print("under construction (8014, excluded on purpose):", v._is_genuinely_vacant(under_construction), "(expect False)")
print("missing fields entirely:", v._is_genuinely_vacant(missing_fields), "(expect False, not a crash)")

assert v._is_genuinely_vacant(truly_vacant) is True
assert v._is_genuinely_vacant(has_a_house) is False
assert v._is_genuinely_vacant(mismatch_code_says_vacant_but_has_building) is False
assert v._is_genuinely_vacant(mismatch_no_buildings_but_code_not_vacant) is False
assert v._is_genuinely_vacant(under_construction) is False
assert v._is_genuinely_vacant(missing_fields) is False
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 2: _extract_acres fallback order ===")
print(v._extract_acres({"landInformation": {"calculatedAcres": 5.5, "acres": 6.0, "deedAcres": 6.2}}), "(expect 5.5, prefers calculatedAcres)")
print(v._extract_acres({"landInformation": {"acres": 6.0, "deedAcres": 6.2}}), "(expect 6.0)")
print(v._extract_acres({"landInformation": {"deedAcres": 6.2}}), "(expect 6.2)")
print(v._extract_acres({"landInformation": {}}), "(expect None)")
print(v._extract_acres({}), "(expect None, not a crash)")

# ---------------------------------------------------------------------
print("\n=== Test 3: search_vacant_land_realie -- pagination + hard filter + acreage range ===")

page1 = {
    # Real shape confirmed live: top-level "properties" array + a
    # "metadata" object carrying "nextCursor" (not a top-level "cursor").
    "properties": [
        {  # should PASS: vacant, no building, 10 acres, in range
            "parcelId": "A1", "latitude": 26.57, "longitude": -81.95,
            "propertyClassification": {"propertyUseCode": 8001},
            "buildingInformation": {"buildings": [{"buildingArea": 0}]},
            "landInformation": {"calculatedAcres": 10.0},
        },
        {  # should FAIL: has a house
            "parcelId": "A2", "latitude": 26.58, "longitude": -81.96,
            "propertyClassification": {"propertyUseCode": 1001},
            "buildingInformation": {"buildings": [{"buildingArea": 1400, "actualYearBuilt": 2001}]},
            "landInformation": {"calculatedAcres": 8.0},
        },
        {  # should FAIL: vacant but out of acreage range (too small)
            "parcelId": "A3", "latitude": 26.59, "longitude": -81.97,
            "propertyClassification": {"propertyUseCode": 8001},
            "buildingInformation": {"buildings": [{"buildingArea": 0}]},
            "landInformation": {"calculatedAcres": 0.5},
        },
    ],
    "metadata": {"nextCursor": "page2token"},
}
page2 = {
    "properties": [
        {  # should PASS: vacant, no building, 20 acres, in range
            "parcelId": "A4", "latitude": 26.60, "longitude": -81.98,
            "propertyClassification": {"propertyUseCode": 8008},
            "buildingInformation": {"buildings": [{"buildingArea": 0}]},
            "landInformation": {"calculatedAcres": 20.0},
        },
    ],
    "metadata": {"nextCursor": None},
}

with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.side_effect = [fake_response(page1), fake_response(page2)]
    results = v.search_vacant_land_realie("FL", county="Lee", city="Cape Coral", min_acres=5, max_acres=40, api_key="FAKEKEY")
    apns = sorted(r["apn"] for r in results)
    print("APNs returned:", apns, "(expect ['A1', 'A4'] -- A2 has a house, A3 too small)")
    assert apns == ["A1", "A4"], f"FAIL: got {apns}"
    print("call count:", mock_get.call_count, "(expect 2, one per page)")
    assert mock_get.call_count == 2
    print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 4: get_tax_assessment_info_realie ===")
fake_record = {
    "valuationInformation": {
        "totalAssessedValue": 87500, "totalMarketValue": 95000,
        "totalLandValue": 87500, "totalBuildingValue": 0, "taxableValue": 82000,
    },
    "taxInformation": {"taxAmount": 1142.30, "taxYear": 2025},
}
print(v.get_tax_assessment_info_realie(fake_record))

# ---------------------------------------------------------------------
print("\n=== Test 5: get_current_owner_info works with Realie-shaped salesHistory (generic function, no changes needed) ===")
realie_sales_history_as_deed_records = [
    {"grantee": "John Smith", "saleDate": "2020-05-01", "salePrice": 45000},
    {"grantee": "Land Ventures LLC", "saleDate": "2023-11-15", "salePrice": 62000},
]
result = v.get_current_owner_info(realie_sales_history_as_deed_records, as_of=v.date(2026, 9, 11))
print(result)
assert result["owner_name"] == "Land Ventures LLC"
assert result["owner_type"] == "corporate"
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 6: count_new_homes_built_realie ===")
page1 = {
    "properties": [
        {"buildingInformation": {"buildings": [{"buildingArea": 1800, "actualYearBuilt": 2026}]}},   # in range
        {"buildingInformation": {"buildings": [{"buildingArea": 1500, "actualYearBuilt": 2010}]}},   # too old
        {"buildingInformation": {"buildings": [{"buildingArea": 0}]}},                                # vacant, no year
    ],
    "metadata": {"nextCursor": None},
}
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(page1)
    result = v.count_new_homes_built_realie("FL", months_back=18, city="Cape Coral", api_key="FAKEKEY")
    print(result)
    assert result["new_homes_built"] == 1
    print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 7: no API key -> clear error ===")
try:
    v.search_vacant_land_realie("FL", api_key=None)
    print("FAIL: should have raised")
except RuntimeError as e:
    print("OK:", e)

print("\n=== ALL TESTS PASSED ===")
