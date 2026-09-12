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


print("=== Test 1: no API key set -> clear error, not a silent failure ===")
try:
    v.count_new_homes_built("/us/fl/lee", months_back=18, api_key=None)
    print("FAIL: should have raised")
except RuntimeError as e:
    print("OK:", e)

print("\n=== Test 2: month->year math (18 months -> 2 calendar years back) ===")
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response({"count": 1847})
    result = v.count_new_homes_built("/us/fl/lee", months_back=18, api_key="FAKEKEY")
    print(result)
    called_params = mock_get.call_args.kwargs["params"]
    print("query params sent:", called_params)
    assert result["new_homes_built"] == 1847
    assert result["year_range"][1] - result["year_range"][0] == 2, "18 months should round up to 2 years"
    print("OK")

print("\n=== Test 3: 6 months -> 1 year back (minimum) ===")
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response({"count": 42})
    result = v.count_new_homes_built("/us/fl/lee", months_back=6, api_key="FAKEKEY")
    print(result)
    assert result["year_range"][1] - result["year_range"][0] == 1
    print("OK")

print("\n=== Test 4: 30 months -> 3 years back ===")
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response({"count": 99})
    result = v.count_new_homes_built("/us/fl/lee", months_back=30, api_key="FAKEKEY")
    print(result)
    assert result["year_range"][1] - result["year_range"][0] == 3
    print("OK")

print("\n=== Test 5: alternate response shapes the fallback should handle ===")
for shape_name, body in [
    ("total", {"total": 55}),
    ("meta.count", {"meta": {"count": 77}}),
    ("meta.total", {"meta": {"total": 88}}),
    ("parcels.count", {"parcels": {"count": 99}}),
]:
    with patch("vacant_land_search.requests.get") as mock_get:
        mock_get.return_value = fake_response(body)
        result = v.count_new_homes_built("/us/fl/lee", months_back=12, api_key="FAKEKEY")
        print(shape_name, "->", result["new_homes_built"])

print("\n=== Test 6: unrecognized response shape -> clear error showing raw payload ===")
with patch("vacant_land_search.requests.get") as mock_get:
    mock_get.return_value = fake_response({"something_unexpected": 123})
    try:
        v.count_new_homes_built("/us/fl/lee", months_back=12, api_key="FAKEKEY")
        print("FAIL: should have raised")
    except RuntimeError as e:
        print("OK:", str(e)[:200])

print("\n=== Test 7: invalid months_back ===")
try:
    v.count_new_homes_built("/us/fl/lee", months_back=0, api_key="FAKEKEY")
    print("FAIL: should have raised")
except ValueError as e:
    print("OK:", e)
