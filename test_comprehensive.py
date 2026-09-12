import sys, json, time
sys.path.insert(0, r"C:\Users\catti\Documents\vacant-land-toolkit")
import vacant_land_search as v

# A small lot near Cape Coral, FL (the user's own example area) -- coastal,
# canal-laced, plausible real vacant-land-investing target.
cape_coral_lot = [
    (-81.9560, 26.5700),
    (-81.9550, 26.5700),
    (-81.9550, 26.5710),
    (-81.9560, 26.5710),
    (-81.9560, 26.5700),
]

print("=== Running full comprehensive buildability report (Cape Coral, FL area) ===")
t0 = time.time()
report = v.get_comprehensive_buildability_report(cape_coral_lot)
print(f"(took {time.time()-t0:.1f}s)\n")
print(json.dumps(report, indent=2, default=str))
