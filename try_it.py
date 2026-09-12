"""
try_it.py -- a hands-on demo of the full vacant land toolkit.

Run this, answer a couple questions, and it will exercise everything
in the toolkit that doesn't need a paid subscription:
  1. Search a real area for genuinely vacant land (using your free
     Realie API key -- costs 1 token per 100 parcels checked).
  2. Show you the vacant parcels it found.
  3. Pick the first one and run the full, free buildability report on
     it (flood zones, septic suitability, hazards, road access, slope,
     contamination, wildfire, broadband -- all real government data,
     no extra API cost).
  4. Show tax/assessment info and current owner info for that parcel.
  5. Build ready-to-click listing-site search links for that address.
  6. Run all 8 city/county-specific tax-delinquency checks (King County
     WA, Pittsburgh PA, Philadelphia PA, Norfolk VA, Sonoma County CA,
     Richmond VA, New York City, Cache County UT) against known real
     records, to show that feature working end to end (these only
     cover those 8 specific places -- see the toolkit's notes on why no
     nationwide version exists).

NOT covered here (needs a paid subscription, not a website):
  - Regrid/ATTOM search (search_vacant_land_parcels)
  - Contractor/permit market activity (add_market_activity_metrics)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vacant_land_search as v


def ask(prompt: str, default: str) -> str:
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def main():
    print("=" * 70)
    print("VACANT LAND TOOLKIT -- TRY IT OUT")
    print("=" * 70)

    api_key = os.environ.get("REALIE_API_KEY")
    if not api_key:
        print("\nREALIE_API_KEY isn't set in this terminal session.")
        api_key = input("Paste your Realie API key here (or press Enter to cancel): ").strip()
        if not api_key:
            print("No key provided -- can't search Realie without one. Exiting.")
            return

    print("\nWhere do you want to search? (Leave blank to use the example: Cape Coral, FL)")
    state = ask("Two-letter state code", "FL")
    county = ask("County (optional, press Enter to skip)", "Lee")
    city = ask("City (optional, press Enter to skip)", "Cape Coral")
    county = county or None
    city = city or None

    print(f"\nSearching {city or county or state}, {state} for vacant land...")
    print("(This uses 1 of your free monthly Realie tokens -- checks up to 100 parcels.)")

    try:
        results = v.search_vacant_land_realie(
            state, county=county, city=city, api_key=api_key, max_pages=1
        )
    except Exception as e:
        print(f"\nSomething went wrong: {e}")
        return

    print(f"\nFound {len(results)} genuinely vacant parcels (no houses/structures on them).")
    if not results:
        print("Try a different area -- some areas may be mostly built out already.")
        return

    print("\nFirst 5 results:")
    for r in results[:5]:
        flag = " [TAX FLAG]" if r["tax_flags"]["flagged"] else ""
        acres = f"{r['acres']:.2f}" if r["acres"] is not None else "?"
        print(f"  APN {r['apn']:<28} {acres:>6} acres   ({r['lat']:.4f}, {r['lon']:.4f}){flag}")

    pick = results[0]
    print(f"\n{'=' * 70}")
    print(f"Running the full buildability report on the first result (APN {pick['apn']})...")
    print("This is 100% free government data -- no extra API cost.")
    print("=" * 70)

    # The buildability report needs the parcel's boundary, but Realie's
    # search doesn't return one -- it returns a center point. So this
    # builds a small placeholder square around that point just to show
    # the report running. For a real deal, you'd get the actual parcel
    # boundary from Realie's parcel-detail endpoint (or your GIS source)
    # instead of a placeholder box.
    lat, lon = pick["lat"], pick["lon"]
    d = 0.0005  # roughly a small lot-sized box around the point
    placeholder_boundary = [
        (lon - d, lat - d), (lon + d, lat - d),
        (lon + d, lat + d), (lon - d, lat + d),
        (lon - d, lat - d),
    ]

    try:
        report = v.get_comprehensive_buildability_report(placeholder_boundary)
    except Exception as e:
        print(f"Buildability report failed: {e}")
        return

    print("\n--- CONCERNS (plain English summary) ---")
    if report["concerns"]:
        for c in report["concerns"]:
            print(f"  - {c}")
    else:
        print("  No concerns flagged.")

    print("\n--- FULL REPORT ---")
    for section, data in report.items():
        if section == "concerns":
            continue
        print(f"\n[{section}]")
        print(f"  {data}")

    print(f"\n{'=' * 70}")
    print("Tax/assessment info for this parcel:")
    print(v.get_tax_assessment_info_realie(pick["raw"]))
    print("\nCurrent owner info for this parcel:")
    print(v.get_current_owner_info(pick["raw"].get("salesHistory") or []))

    print(f"\n{'=' * 70}")
    print("Listing-site search links for this parcel's address:")
    loc = pick["raw"].get("propertyLocation") or {}
    street = loc.get("addressLine1") or loc.get("street") or ""
    addr_city = loc.get("city") or city or ""
    addr_state = loc.get("state") or state
    addr_zip = loc.get("zipCode") or ""
    if street:
        for site, link in v.build_listing_search_links(street, addr_city, addr_state, addr_zip).items():
            print(f"  {site}: {link}")
    else:
        print("  (No address on this record to build links from.)")

    print(f"\n{'=' * 70}")
    print("City/county-specific tax delinquency checks -- these only work")
    print("for 8 places (no nationwide source exists -- most of the")
    print("country has no free structured source for this at all). Each")
    print("one below is run against a known real record from that place,")
    print("chosen so this demo shows the feature working regardless of")
    print("where you searched above.")
    print("=" * 70)

    delinquency_checks = [
        ("King County, WA", "account 000080001506",
         lambda: v.check_king_county_delinquent_tax("000080001506")),
        ("Pittsburgh, PA", "PIN 0014L00244000000",
         lambda: v.check_pittsburgh_tax_delinquency("0014L00244000000")),
        ("Philadelphia, PA", "OPA 41040500",
         lambda: v.check_philadelphia_tax_delinquency("41040500")),
        ("Norfolk, VA", "biitem 00000218",
         lambda: v.check_norfolk_tax_delinquency("00000218")),
        ("Sonoma County, CA", "assessment number 001011005000",
         lambda: v.check_sonoma_county_tax_delinquency("001011005000")),
        ("Richmond, VA", "property code W0000104004",
         lambda: v.check_richmond_tax_delinquency("W0000104004")),
        ("New York City", "BBL 1-16-3 (Manhattan)",
         lambda: v.check_nyc_tax_lien_sale_list("1", "16", "3")),
        ("Cache County, UT", "tax ID 02-216-0025",
         lambda: v.check_cache_county_tax_delinquency("02-216-0025")),
    ]
    for place, identifier, check in delinquency_checks:
        try:
            print(f"\n{place} ({identifier}):")
            print(f"  {check()}")
        except Exception as e:
            print(f"  Check failed: {e}")

    print(f"\n{'=' * 70}")
    print("DEMO COMPLETE. Everything above ran against real live data,")
    print("except the buildability report's parcel boundary (a placeholder")
    print("box around the search result's center point -- see the comment")
    print("above where it's built).")


if __name__ == "__main__":
    main()
