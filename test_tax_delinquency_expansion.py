import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v


def fake_response(json_body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = __import__("requests").exceptions.HTTPError(response=resp)
    resp.json.return_value = json_body
    resp.url = "http://fake"
    return resp


# ---------------------------------------------------------------------
print("=== Test 1: check_norfolk_tax_delinquency -- sums across installments/years ===")
rows = [
    {"biitem": "X1", "bwtaxyear": "2023", "owner_name": "A OWNER", "address": "1 MAIN ST", "total": "100.00"},
    {"biitem": "X1", "bwtaxyear": "2024", "owner_name": "A OWNER", "address": "1 MAIN ST", "total": "50.50"},
]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(rows)
    result = v.check_norfolk_tax_delinquency("X1")
    print(result)
    assert result["amount_owed"] == 150.5
    assert result["years_owed"] == ["2023", "2024"]
    assert result["delinquent"] is True
print("ALL PASS")

with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response([])
    assert v.check_norfolk_tax_delinquency("NOPE") == {}
print("not-found -> {} : PASS")

# ---------------------------------------------------------------------
print("\n=== Test 2: check_sonoma_county_tax_delinquency -- picks the row with the REAL default, not the latest year ===")
rows = [
    {"taxyear": "2022", "defaultamt": "30049", "location_address": "611 N CLOVERDALE BLVD",
     "mailaddress1": "8955 MCCOY AVE", "mailaddress2": "SACRAMENTO CA", "default_date": "06/30/2025", "existsbankruptcy": "No"},
    {"taxyear": "2023", "defaultamt": "0", "location_address": "611 N CLOVERDALE BLVD",
     "mailaddress1": "8955 MCCOY AVE", "mailaddress2": "SACRAMENTO CA", "default_date": "06/30/2025", "existsbankruptcy": "No"},
    {"taxyear": "2024", "defaultamt": "0", "location_address": "611 N CLOVERDALE BLVD",
     "mailaddress1": "8955 MCCOY AVE", "mailaddress2": "SACRAMENTO CA", "default_date": "06/30/2025", "existsbankruptcy": "No"},
]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(rows)
    result = v.check_sonoma_county_tax_delinquency("001011005000")
    print(result)
    # Regression check: an earlier version of this function picked the
    # LATEST taxyear's row (2024, defaultamt=0) and would have reported
    # $0 owed on an actual $30,049 default. Must not regress to that.
    assert result["amount_owed"] == 30049.0, f"FAIL: got {result['amount_owed']} (bug would show 0.0)"
    assert result["as_of_tax_year"] == "2022"
    assert result["delinquent"] is True
print("ALL PASS -- correctly found the real default, not the misleadingly-zero latest year")

# ---------------------------------------------------------------------
print("\n=== Test 3: check_richmond_tax_delinquency -- sums per-year bills, handles '$' and ',' ===")
rows = [
    {"property_code": "W1", "bill_year": "2024", "current_owner_name_1": "TEST LLC", "total_due": "$1,200.50"},
    {"property_code": "W1", "bill_year": "2025", "current_owner_name_1": "TEST LLC", "total_due": "$361.90"},
]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(rows)
    result = v.check_richmond_tax_delinquency("W1")
    print(result)
    assert result["amount_owed"] == 1562.4
    assert result["years_owed"] == ["2024", "2025"]
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 4: check_nyc_tax_lien_sale_list -- binary flag, no dollar amount ===")
rows = [
    {"cycle": "90 Day Notice", "water_debt_only": "NO"},
    {"cycle": "Final Notice", "water_debt_only": "NO"},
]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(rows)
    result = v.check_nyc_tax_lien_sale_list("1", "16", "3")
    print(result)
    assert result["on_lien_sale_list"] is True
    assert result["cycles"] == ["90 Day Notice", "Final Notice"]
    assert result["water_debt_only"] is False

with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response([])
    result = v.check_nyc_tax_lien_sale_list("9", "1", "1")
    assert result == {"bbl": "9-1-1", "on_lien_sale_list": False, "cycles": [], "water_debt_only": False}
print("ALL PASS")

# ---------------------------------------------------------------------
print("\n=== Test 5: check_cache_county_tax_delinquency -- handles fully-qualified ArcGIS field names ===")
fake_feature = {
    "features": [{
        "attributes": {
            "giscache.sde.parcel_current.owner_name": "JSC PROPERTIES LLC",
            "giscache.sde.parcel_current.address_complete": "1007 S 275 W, LOGAN",
            "InGeoCounty.dbo.gis_parcel_delinquent_taxes.parcel_total_due": 1517.47,
            "InGeoCounty.dbo.gis_parcel_delinquent_taxes.curr_tax_year_due": 0.0,
            "InGeoCounty.dbo.gis_parcel_delinquent_taxes.backtax_total_due": 1517.47,
            "InGeoCounty.dbo.gis_parcel_delinquent_taxes.unpaid_yr_count": 1,
        }
    }]
}
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(fake_feature)
    result = v.check_cache_county_tax_delinquency("02-216-0025")
    print(result)
    assert result["owner"] == "JSC PROPERTIES LLC"
    assert result["amount_owed"] == 1517.47
    assert result["delinquent"] is True
print("ALL PASS")

with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response({"features": []})
    assert v.check_cache_county_tax_delinquency("NOPE") == {}
print("not-found -> {} : PASS")

# ---------------------------------------------------------------------
print("\n=== Test 6: _get_socrata_json retries on 5xx, doesn't retry on 4xx ===")
call_count = {"n": 0}

def flaky_then_ok(url, params=None, timeout=None):
    call_count["n"] += 1
    if call_count["n"] < 3:
        return fake_response({}, status=503)
    return fake_response([{"biitem": "OK", "total": "1.00"}])

with patch("vacant_land_search.requests.get", side_effect=flaky_then_ok), patch("vacant_land_search.time.sleep"):
    result = v.check_norfolk_tax_delinquency("OK")
    print(f"Succeeded after {call_count['n']} attempts:", result)
    assert call_count["n"] == 3
    assert result["amount_owed"] == 1.0
print("ALL PASS -- retried through transient 503s")

call_count["n"] = 0

def bad_request(url, params=None, timeout=None):
    call_count["n"] += 1
    return fake_response({}, status=404)

with patch("vacant_land_search.requests.get", side_effect=bad_request), patch("vacant_land_search.time.sleep"):
    try:
        v.check_norfolk_tax_delinquency("BAD")
        print("FAIL: should have raised on 404")
    except Exception as e:
        print(f"OK, raised immediately on 404 without retrying: {e}")
        assert call_count["n"] == 1, f"FAIL: retried a 4xx {call_count['n']} times"
print("ALL PASS -- did not waste retries on a non-transient error")

print("\n=== Test 7: malformed-but-present amount fields never become a false 'not delinquent' ===")
# A real delinquency ROW exists (rows is non-empty) but its dollar
# amount is garbage -- the honest result is UNKNOWN, never a confident
# "delinquent: False", and never a silent $0.
bad_rows = [{"biitem": "X2", "bwtaxyear": "2024", "owner_name": "B OWNER", "address": "2 MAIN ST", "total": "N/A"}]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(bad_rows)
    result = v.check_norfolk_tax_delinquency("X2")
    print(result)
    assert result["delinquent"] is None, "must be UNKNOWN, not a confident False"
    assert result["amount_owed"] is None
    assert "data_quality_issue" in result
print("Norfolk: PASS")

bad_sonoma_rows = [{"assessment_number": "S1", "defaultamt": "garbage", "taxyear": "2024"}]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(bad_sonoma_rows)
    result = v.check_sonoma_county_tax_delinquency("S1")
    print(result)
    assert result["delinquent"] is None
    assert result["amount_owed"] is None
print("Sonoma: PASS")

bad_richmond_rows = [{"property_code": "R1", "bill_year": "2024", "current_owner_name_1": "C OWNER", "total_due": "???"}]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(bad_richmond_rows)
    result = v.check_richmond_tax_delinquency("R1")
    print(result)
    assert result["delinquent"] is None
    assert result["amount_owed"] is None
print("Richmond: PASS")

bad_king_rows = [{"billed_amount": "not-a-number", "paid_amount": "0"}]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(bad_king_rows)
    result = v.check_king_county_delinquent_tax("K1")
    print(result)
    assert result["delinquent"] is None
    assert result["amount_owed"] is None
print("King County: PASS")

# And the normal, clean-data path must still work exactly as before --
# genuinely absent fields default to 0 without raising.
clean_rows = [{"biitem": "X3", "bwtaxyear": "2024", "owner_name": "D OWNER", "address": "3 MAIN ST", "total": None}]
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response(clean_rows)
    result = v.check_norfolk_tax_delinquency("X3")
    assert result["delinquent"] is False
    assert result["amount_owed"] == 0.0
print("Genuinely-absent field still defaults to 0, unaffected by this fix: PASS")

print("\n=== ALL TESTS PASSED ===")
