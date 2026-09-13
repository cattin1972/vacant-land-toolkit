"""
Prototype website for the vacant land toolkit.

Flask backend wrapping vacant_land_search.py -- this is a local, runnable
demo, not a production deployment (no auth, no anti-scraping protection
yet -- see anti-scraping-terms-of-service-draft.md for what's needed
before this goes public).

Run: python app.py, then open http://127.0.0.1:5000 in a browser.
"""
import concurrent.futures
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vacant_land_search as v

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# NOTE: this app deliberately holds NO server-side, cross-request
# parcel cache (fixed 2026-09-12 -- there used to be a shared
# in-memory `_PARCEL_CACHE` dict here). That was a real correctness
# risk for a real multi-subscriber product: one shared dict, keyed only
# by APN, touched by every visitor's requests. APNs are only unique
# WITHIN a county, not nationally, so two different customers searching
# two different states could -- in principle -- collide on the same
# key and serve each other stale or wrong data on a detail click. Even
# short of an actual collision, a shared cache is simply the wrong
# foundation for per-subscriber data. Every parcel's own raw data is
# now sent to the browser at search time and handed back by the
# browser itself on every follow-up request (report, skip trace) --
# each visitor's browser is the only place their own search results
# live, so there is no shared state between customers at all, by
# construction, not by luck.

# The 8 places with a real tax-delinquency check -- see
# vacant_land_search.py for why only these 8 exist (no nationwide
# source). Each place needs a DIFFERENT kind of ID (biitem, PIN, OPA
# number, etc.) that isn't derivable from a Realie search result, so
# this is offered as its own lookup tool rather than auto-matched to
# search results.
DELINQUENCY_CHECKS = {
    "king_county": {
        "label": "King County, WA",
        "id_label": "Account number",
        "example": "000080001506",
        "fn": v.check_king_county_delinquent_tax,
    },
    "pittsburgh": {
        "label": "Pittsburgh, PA",
        "id_label": "Parcel PIN",
        "example": "0014L00244000000",
        "fn": v.check_pittsburgh_tax_delinquency,
    },
    "philadelphia": {
        "label": "Philadelphia, PA",
        "id_label": "OPA number",
        "example": "41040500",
        "fn": v.check_philadelphia_tax_delinquency,
    },
    "norfolk": {
        "label": "Norfolk, VA",
        "id_label": "Biitem",
        "example": "00000218",
        "fn": v.check_norfolk_tax_delinquency,
    },
    "sonoma": {
        "label": "Sonoma County, CA",
        "id_label": "Assessment number",
        "example": "001011005000",
        "fn": v.check_sonoma_county_tax_delinquency,
    },
    "richmond": {
        "label": "Richmond, VA",
        "id_label": "Property code",
        "example": "W0000104004",
        "fn": v.check_richmond_tax_delinquency,
    },
    "cache_county": {
        "label": "Cache County, UT",
        "id_label": "Tax ID",
        "example": "02-216-0025",
        "fn": v.check_cache_county_tax_delinquency,
    },
}


def _placeholder_boundary(lat: float, lon: float) -> list[tuple[float, float]]:
    """Same placeholder-box approach as try_it.py -- Realie's search
    only gives a center point, not a real parcel boundary."""
    d = 0.0005
    return [
        (lon - d, lat - d), (lon + d, lat - d),
        (lon + d, lat + d), (lon - d, lat + d),
        (lon - d, lat - d),
    ]


@app.route("/")
def index():
    return render_template("index.html", delinquency_checks=DELINQUENCY_CHECKS)


@app.route("/print")
def print_view():
    return render_template("print.html")


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(force=True)
    state = (data.get("state") or "").strip().upper()
    county = (data.get("county") or "").strip() or None
    city = (data.get("city") or "").strip() or None
    min_acres = data.get("min_acres") or None
    max_acres = data.get("max_acres") or None
    api_key = (data.get("api_key") or os.environ.get("REALIE_API_KEY") or "").strip()

    if not state:
        return jsonify({"error": "State is required."}), 400
    if not api_key:
        return jsonify({"error": "No Realie API key set (env var REALIE_API_KEY, or paste one in)."}), 400

    try:
        results = v.search_vacant_land_realie(
            state, county=county, city=city,
            min_acres=float(min_acres) if min_acres else None,
            max_acres=float(max_acres) if max_acres else None,
            api_key=api_key, max_pages=1,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    out = []
    for r in results:
        tax_info = v.get_tax_assessment_info_realie(r["raw"])
        owner = v.get_current_owner_info(r["raw"].get("salesHistory") or [])
        mailing = v.get_owner_mailing_address(r["raw"])
        loc = r["raw"].get("propertyLocation") or {}
        result_city = loc.get("city") or city or ""
        result_state = loc.get("state") or state

        # Instant priority: free, itemized ranking signal computed purely
        # from data Realie's own search response already includes -- see
        # build_instant_priority's own docstring for why this exists as a
        # separate, much cheaper first pass ahead of the decision engine's
        # slower per-parcel evidence screening (built for /api/parcel/
        # <apn>/report and /api/parcel/score/bulk).
        priority = v.build_instant_priority(r["tax_flags"], owner, mailing, result_city, result_state)

        out.append({
            "apn": r["apn"],
            "lat": r["lat"],
            "lon": r["lon"],
            "acres": r["acres"],
            "tax_flags": r["tax_flags"],
            "land_value": tax_info.get("land_value") or tax_info.get("assessed_value"),
            # The PROPERTY's own site address -- for reference/map/listing-
            # link purposes only. Never use this as a mail-to address (see
            # get_owner_mailing_address's docstring): a vacant lot is often
            # not a deliverable mailing address at all.
            "address": loc.get("addressLine1") or loc.get("street") or "",
            "city": result_city,
            "state": result_state,
            "zip_code": loc.get("zipCode") or "",
            "owner_name": owner.get("owner_name"),
            "owner_type": owner.get("owner_type"),
            "ownership_years": owner.get("ownership_years"),
            "price_paid": owner.get("price_paid"),
            # The OWNER's actual mailing address -- this, not the property
            # address above, is what a direct-mail piece should go to.
            "mail_street": mailing.get("mail_street"),
            "mail_city": mailing.get("mail_city"),
            "mail_state": mailing.get("mail_state"),
            "mail_zip": mailing.get("mail_zip"),
            "instant_score": priority["instant_score"],
            "instant_reasons": priority["reasons"],
            "instant_negative_reasons": priority["negative_reasons"],
            # Skip tracing isn't wired in yet (waiting on a provider API
            # key) -- these two columns are placeholders so the export/
            # print feature already has the right shape and doesn't need
            # rework once skip tracing lands.
            "owner_phone": None,
            "owner_email": None,
            # The full raw Realie record -- the browser holds onto this
            # and sends it back on every follow-up request (report,
            # skip trace) instead of the server remembering it. See the
            # module-level note above for why.
            "raw": r["raw"],
        })

    # Sort so the parcels most worth a customer's time come first. This is
    # about ORDER, not filtering -- every parcel Realie returned is still
    # here, nothing is dropped. Instant score is the only signal available
    # at this point (the decision engine's deeper screening hasn't run
    # yet); once a customer deep-screens a subset, the frontend re-sorts
    # by the combined priority (see PRIORITY_RANK in index.html).
    out.sort(key=lambda x: x["instant_score"], reverse=True)

    return jsonify({"count": len(out), "results": out})


@app.route("/api/parcel/<path:apn>/report", methods=["POST"])
def api_parcel_report(apn):
    # The browser sends back the SAME raw record it already got from
    # its own earlier /api/search call -- no server-side lookup at all,
    # so there is nothing here that could ever be shared between two
    # different visitors' sessions (see the module-level note above).
    data = request.get_json(force=True) or {}
    raw = data.get("raw")
    if not raw:
        return jsonify({"error": "Missing parcel data -- run a search first."}), 400

    lat, lon = raw.get("latitude"), raw.get("longitude")
    try:
        buildability = v.get_comprehensive_buildability_report(_placeholder_boundary(lat, lon))
    except Exception as e:
        buildability = {"error": str(e)}

    loc = raw.get("propertyLocation") or {}
    listing_links = {}
    street = loc.get("addressLine1") or loc.get("street")
    if street:
        listing_links = v.build_listing_search_links(
            street, loc.get("city") or "", loc.get("state") or "", loc.get("zipCode") or ""
        )

    try:
        zoning = v.check_zoning_district(lat, lon)
    except Exception as e:
        zoning = {"error": str(e)}

    tax_flags = v.get_tax_flags(raw)
    owner_mailing = v.get_owner_mailing_address(raw)

    # The trust/evidence report is the PRIMARY thing the UI now renders --
    # see the framework's own module-level docstring in vacant_land_search.py
    # for why. It's built from whatever buildability/zoning data actually
    # came back; a failed sub-call (network error, etc.) is treated as
    # "no usable data for this category," which the framework already
    # knows how to represent honestly, not as a reason to skip the whole
    # report.
    try:
        evidence = v.build_evidence_report(
            buildability if "error" not in buildability else {},
            zoning if "error" not in zoning else {},
            tax_flags,
            owner_mailing,
        )
    except Exception as e:
        evidence = {"error": str(e)}

    # The decision engine ("does this parcel deserve the next 10 minutes?")
    # sits on top of the evidence report above -- zero new network calls,
    # same "failed sub-call is handled, not fatal" treatment.
    try:
        decision = v.build_decision_summary(
            evidence if "error" not in evidence else {},
            buildability if "error" not in buildability else {},
            tax_flags,
        )
    except Exception as e:
        decision = {"error": str(e)}

    return jsonify({
        "apn": apn,
        "decision": decision,
        "evidence": evidence,
        "buildability": buildability,
        "tax_assessment": v.get_tax_assessment_info_realie(raw),
        "current_owner": v.get_current_owner_info(raw.get("salesHistory") or []),
        "tax_flags": tax_flags,
        "listing_links": listing_links,
        "zoning": zoning,
    })


@app.route("/api/parcel/score/bulk", methods=["POST"])
def api_score_bulk():
    """
    Scores a batch of search results with JUST the deal-potential tier
    (not the full report) so the results list can show a color/tier per
    row without forcing a user to open every parcel one at a time. This
    runs the exact same government-API calls as a full parcel report, per
    parcel -- there is no cheaper way to get this signal -- which is why
    it is an explicit, user-triggered action with its own time/cost
    framing in the UI, the same pattern already used for skip-trace-all,
    rather than something /api/search runs automatically for every result.
    Capped at 20 parcels per call and run 4-at-a-time so a large result
    set doesn't hammer the free government APIs this whole toolkit runs on.
    """
    data = request.get_json(force=True) or {}
    parcels = data.get("parcels") or []
    MAX_PARCELS = 20
    to_score = parcels[:MAX_PARCELS]

    def score_one(p):
        apn = p.get("apn")
        raw = p.get("raw") or {}
        lat, lon = raw.get("latitude"), raw.get("longitude")
        try:
            buildability = v.get_comprehensive_buildability_report(_placeholder_boundary(lat, lon))
        except Exception as e:
            buildability = {"error": str(e)}
        try:
            zoning = v.check_zoning_district(lat, lon)
        except Exception as e:
            zoning = {"error": str(e)}
        tax_flags = v.get_tax_flags(raw)
        owner_mailing = v.get_owner_mailing_address(raw)
        try:
            evidence = v.build_evidence_report(
                buildability if "error" not in buildability else {},
                zoning if "error" not in zoning else {},
                tax_flags, owner_mailing,
            )
            decision = v.build_decision_summary(
                evidence if "error" not in evidence else {},
                buildability if "error" not in buildability else {},
                tax_flags,
            )
        except Exception as e:
            decision = {"error": str(e)}
        return {
            "apn": apn,
            "deal_potential": decision.get("deal_potential"),
            "color": decision.get("color"),
            "headline": decision.get("headline"),
            "recommended_action": decision.get("recommended_action"),
            # Trimmed to the top couple of each -- enough for the Priority
            # Picks panel to show WHY a parcel ranked where it did without
            # bloating a 20-parcel bulk response; "Show full evidence
            # report" on the parcel's own page still has everything.
            "top_positive_factors": (decision.get("top_positive_factors") or [])[:2],
            "deal_killer_risks": (decision.get("deal_killer_risks") or [])[:2],
            "top_risks": (decision.get("top_risks") or [])[:2],
        }

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(score_one, p): p for p in to_score}
        for future in concurrent.futures.as_completed(futures):
            p = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"apn": p.get("apn"), "error": str(e)})

    return jsonify({
        "count": len(results),
        "results": results,
        "skipped": max(0, len(parcels) - len(to_score)),
    })


@app.route("/api/delinquency/<place_key>")
def api_delinquency(place_key):
    place = DELINQUENCY_CHECKS.get(place_key)
    if not place:
        return jsonify({"error": "Unknown place."}), 404
    parcel_id = request.args.get("id", "").strip()
    if not parcel_id:
        return jsonify({"error": "Missing id parameter."}), 400
    try:
        result = place["fn"](parcel_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result or {"found": False})


@app.route("/api/skiptrace/<path:apn>", methods=["POST"])
def api_skiptrace_one(apn):
    """
    Skip traces the owner of ONE parcel. Takes the CALLER's own
    Tracerfy API key AND the parcel's own raw data in the request body
    -- no server-side lookup (see the module-level note above: no
    shared state between visitors' sessions). Neither is ever stored;
    the key is never billed to this site's own account (see the
    BYO-key section note in vacant_land_search.py for why: both
    Tracerfy's and BatchData's own Terms of Service prohibit a resale/
    markup model on one shared account).
    """
    data = request.get_json(force=True) or {}
    raw = data.get("raw")
    if not raw:
        return jsonify({"error": "Missing parcel data -- run a search first."}), 400

    api_key = (data.get("tracerfy_api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "Tracerfy API key is required -- this is billed to YOUR OWN Tracerfy account."}), 400

    mailing = v.get_owner_mailing_address(raw)
    if not mailing.get("mail_street"):
        return jsonify({"error": "No owner mailing address on file for this parcel -- nothing to skip trace."}), 400

    try:
        result = v.skip_trace_owner(
            mailing["mail_street"], mailing.get("mail_city"), mailing.get("mail_state"),
            zip_code=mailing.get("mail_zip"), api_key=api_key,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result)


@app.route("/api/skiptrace/bulk", methods=["POST"])
def api_skiptrace_bulk():
    """
    Skip traces MANY parcels at once (a "Skip Trace All Results"
    button). Same BYO-key model as the single endpoint above -- every
    lookup bills the caller's own Tracerfy account. `parcels` in the
    request body is a list of {"apn": ..., "raw": ...} -- the browser's
    own copy of each parcel's data from its earlier /api/search call,
    no server-side lookup (see the module-level note above). `max_lookups`
    (default 15, same default as skip_trace_owners_bulk) hard-caps real
    spend per click.
    """
    data = request.get_json(force=True) or {}
    api_key = (data.get("tracerfy_api_key") or "").strip()
    parcels = data.get("parcels") or []
    max_lookups = int(data.get("max_lookups") or 15)

    if not api_key:
        return jsonify({"error": "Tracerfy API key is required -- this is billed to YOUR OWN Tracerfy account."}), 400
    if not parcels:
        return jsonify({"error": "No parcels selected."}), 400

    owners = []
    for parcel in parcels:
        apn, raw = parcel.get("apn"), parcel.get("raw")
        if not raw:
            continue
        mailing = v.get_owner_mailing_address(raw)
        if not mailing.get("mail_street"):
            continue
        owners.append({
            "apn": apn,
            "street": mailing["mail_street"],
            "city": mailing.get("mail_city"),
            "state": mailing.get("mail_state"),
            "zip": mailing.get("mail_zip"),
        })

    if not owners:
        return jsonify({"error": "None of the selected parcels have an owner mailing address on file."}), 400

    try:
        results = v.skip_trace_owners_bulk(owners, api_key=api_key, max_lookups=max_lookups)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"count": len(results), "results": results})


if __name__ == "__main__":
    # Render (and most hosts) assign the port via $PORT and expect the
    # app to bind 0.0.0.0, not localhost -- this still defaults to the
    # old local-dev behavior (port 5000, debug on) when PORT isn't set.
    # In production, Render should run this via gunicorn instead of
    # this __main__ block at all (see the start command in the repo's
    # deployment notes) -- gunicorn imports the `app` object directly
    # and never executes this block.
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
