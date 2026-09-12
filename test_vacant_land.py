import sys
sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")

import vacant_land_search as v

print("=== Test 1: current owner info (previous owners should be ignored) ===")
fake_deeds = [
    {"ownerName": "John Q Smith", "saleDate": "2010-06-15", "salePrice": 50000},
    {"ownerName": "Riverbend Holdings LLC", "saleDate": "2018-01-01", "salePrice": 120000},
    {"ownerName": "Maria Garcia", "saleDate": "2022-03-10", "salePrice": 175000},
]
print(v.get_current_owner_info(fake_deeds, as_of=v.date(2026, 9, 11)))
print("(expect: Maria Garcia, individual, 175000, ~4.5 years)")

print("\nsingle-record case:", v.get_current_owner_info(
    [{"ownerName": "Bandera Ranch Holdings LLC", "saleDate": "2015-01-01", "salePrice": 90000}],
    as_of=v.date(2026, 9, 11),
))

print("\nno usable date case:", v.get_current_owner_info([{"ownerName": "No Date LLC"}]))

print("\n=== Test 2: owner classification edge cases ===")
for name in ["Smith Family Trust", "Bob Jones", "ACME Inc.", "Jane O'Malley", "123 Main Street Co", "Doe, Jane & Doe, John"]:
    print(f"{name!r} -> {v._classify_owner(name)}")

print("\n=== Test 3: listing search links ===")
links = v.build_listing_search_links("123 Rural Rd", "Bandera", "TX", "78003")
for k, val in links.items():
    print(k, "->", val)

print("\n=== Test 4: haversine sanity check (should be small, e.g. a few miles) ===")
# Austin, TX downtown vs. approx 10 miles north
d = v._haversine_miles(30.2672, -97.7431, 30.4200, -97.7431)
print("distance (deg lat offset ~0.15):", round(d, 2), "miles")

print("\n=== Test 5: FEMA + USFWS live endpoint check (real network call) ===")
# A small rectangle near a known Texas floodplain-adjacent area (Guadalupe River, Kerrville TX)
test_polygon = [
    (-99.1500, 29.8700),
    (-99.1480, 29.8700),
    (-99.1480, 29.8720),
    (-99.1500, 29.8720),
    (-99.1500, 29.8700),
]
try:
    score = v.calculate_buildability_score(test_polygon)
    print("SUCCESS:", score)
except Exception as e:
    print("FAILED:", type(e).__name__, str(e))

print("\n=== Test 6: Census geocoder live endpoint check ===")
try:
    zc = v._get_zip_code(30.2672, -97.7431)  # Austin, TX
    print("zip code result:", zc)
except Exception as e:
    print("FAILED:", type(e).__name__, str(e))
