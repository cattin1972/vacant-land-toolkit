import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v


def fake_response(json_body):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body
    return resp


print("=== Test 1: get_tax_flags ===")
clean = {"liens": [], "foreclosures": []}
has_lien = {"liens": [{"amount": 500}], "foreclosures": []}
has_foreclosure = {"liens": [], "foreclosures": [{"date": "2023-01-01"}]}
both = {"liens": [{"a": 1}, {"a": 2}], "foreclosures": [{"b": 1}]}
missing = {}

print("clean:         ", v.get_tax_flags(clean))
print("has_lien:      ", v.get_tax_flags(has_lien))
print("has_foreclosure:", v.get_tax_flags(has_foreclosure))
print("both:          ", v.get_tax_flags(both))
print("missing fields:", v.get_tax_flags(missing))

assert v.get_tax_flags(clean)["flagged"] is False
assert v.get_tax_flags(has_lien)["flagged"] is True
assert v.get_tax_flags(has_foreclosure)["flagged"] is True
assert v.get_tax_flags(both)["lien_count"] == 2 and v.get_tax_flags(both)["foreclosure_count"] == 1
assert v.get_tax_flags(missing)["flagged"] is False
print("ALL PASS")

print("\n=== Test 2: search filter — exclude_tax_flagged=False (default) keeps everything, tags each result ===")
page = {
    "results": [
        {
            "parcelId": "CLEAN1", "latitude": 26.5, "longitude": -81.9,
            "propertyClassification": {"propertyUseCode": 8001},
            "buildingInformation": {"buildings": []},
            "landInformation": {"calculatedAcres": 5.0},
            "liens": [], "foreclosures": [],
        },
        {
            "parcelId": "FLAGGED1", "latitude": 26.6, "longitude": -81.8,
            "propertyClassification": {"propertyUseCode": 8001},
            "buildingInformation": {"buildings": []},
            "landInformation": {"calculatedAcres": 6.0},
            "liens": [{"amount": 1200}], "foreclosures": [],
        },
    ],
    "cursor": None,
}

with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(page)
    results = v.search_vacant_land_realie("FL", city="Cape Coral", api_key="FAKEKEY")
    apns = sorted(r["apn"] for r in results)
    print("apns (default, no exclusion):", apns)
    assert apns == ["CLEAN1", "FLAGGED1"], "default should keep both"
    flagged_result = next(r for r in results if r["apn"] == "FLAGGED1")
    print("FLAGGED1 tax_flags:", flagged_result["tax_flags"])
    assert flagged_result["tax_flags"]["flagged"] is True
    print("ALL PASS")

print("\n=== Test 3: search filter — exclude_tax_flagged=True removes flagged parcels ===")
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(page)
    results = v.search_vacant_land_realie("FL", city="Cape Coral", exclude_tax_flagged=True, api_key="FAKEKEY")
    apns = sorted(r["apn"] for r in results)
    print("apns (excluding flagged):", apns)
    assert apns == ["CLEAN1"], f"FAIL: expected only CLEAN1, got {apns}"
    print("ALL PASS")

print("\n=== ALL TAX FLAG TESTS PASSED ===")
