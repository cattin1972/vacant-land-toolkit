import sys
sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v

print("=== Slope: steep (Pikes Peak area) vs flat (rural Kansas) ===")
print("steep:", v.get_slope_estimate(38.8409, -105.0423))
print("flat: ", v.get_slope_estimate(38.5, -99.5))

print("\n=== Contamination: Love Canal (known Superfund site) vs rural Kansas (clean) ===")
print("Love Canal:", v.check_contamination_sites(43.08, -78.949, radius_km=2.0))
print("clean:     ", v.check_contamination_sites(38.5, -99.5, radius_km=2.0))

print("\n=== Fine-grained wildfire: Sierra Nevada forest (high) vs rural Kansas cropland (low) ===")
print("forest:", v.check_fine_grained_wildfire_risk(39.30, -120.80))
print("crop:  ", v.check_fine_grained_wildfire_risk(38.5, -99.5))

print("\n=== Broadband: downtown Denver (served) vs remote Alaska (unserved) ===")
print("Denver:", v.check_broadband_availability(39.7392, -104.9903))
print("Alaska:", v.check_broadband_availability(65.0, -152.0))

print("\n=== Full comprehensive report still works end-to-end with all new checks wired in ===")
cape_coral_lot = [
    (-81.9560, 26.5700), (-81.9550, 26.5700),
    (-81.9550, 26.5710), (-81.9560, 26.5710), (-81.9560, 26.5700),
]
import json, time
t0 = time.time()
report = v.get_comprehensive_buildability_report(cape_coral_lot)
print(f"(took {time.time()-t0:.1f}s)")
print(json.dumps(report, indent=2, default=str))
