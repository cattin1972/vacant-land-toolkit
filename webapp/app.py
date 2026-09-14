"""
Prototype website for the vacant land toolkit.

Flask backend wrapping vacant_land_search.py -- this is a local, runnable
demo, not a production deployment (no auth, no anti-scraping protection
yet -- see anti-scraping-terms-of-service-draft.md for what's needed
before this goes public).

Run: python app.py, then open http://127.0.0.1:5000 in a browser.
"""
import concurrent.futures
import hmac
import os
import sys
import threading
import time
from collections import defaultdict, deque
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vacant_land_search as v

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# --------------------------------------------------------------------------
# Lightweight, dependency-free per-IP rate limiting (added 2026-09-14,
# security audit). Applied only to the endpoints that require NO API key
# at all and each trigger real outbound calls to free government
# services (a parcel report alone fans out to ~12-14 of them) -- found
# as a real risk two ways: an attacker hammering these could exhaust
# this single-process server's thread pool (denial of service against
# this app itself), or could burn through the SHARED rate-limit
# tolerance those free government services extend to this server's own
# IP, potentially getting it throttled or blocked entirely -- breaking
# the feature for every legitimate customer, not just the attacker.
#
# In-memory, per-process -- resets on restart, and if this ever runs
# under more than one gunicorn worker, each worker tracks its own
# counts independently rather than sharing one global count. A real,
# honest limitation for a single small deployment, not a substitute for
# a real edge/WAF-level limiter if this needs to scale past one
# process later -- documented so it isn't mistaken for more than it is.
_rate_limit_lock = threading.Lock()
_rate_limit_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def rate_limited(max_requests: int, window_seconds: int):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            # X-Forwarded-For is set by Render's own proxy in front of
            # this app; request.remote_addr alone would just be the
            # proxy's own IP for every visitor. Take the first (client)
            # entry, since a caller could otherwise spoof additional
            # entries after their own real one.
            forwarded = request.headers.get("X-Forwarded-For", "")
            client_ip = (forwarded.split(",")[0].strip() if forwarded else None) or request.remote_addr or "unknown"
            bucket_key = (fn.__name__, client_ip)
            now = time.monotonic()
            with _rate_limit_lock:
                hits = _rate_limit_hits[bucket_key]
                while hits and now - hits[0] > window_seconds:
                    hits.popleft()
                if len(hits) >= max_requests:
                    retry_after = max(1, int(window_seconds - (now - hits[0])))
                    return jsonify({"error": f"Too many requests -- try again in about {retry_after}s."}), 429
                hits.append(now)
            return fn(*args, **kwargs)
        return wrapped
    return decorator

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


def _check_nyc_tax_lien_by_bbl_string(bbl_string: str) -> dict:
    """
    Adapter so NYC's check (borough, block, lot as 3 separate params --
    the only one of the 8 places shaped this way) fits the same single-
    text-field UI every other place here uses, instead of needing its
    own special-cased form. Expects "borough-block-lot", e.g. "1-16-3"
    (matches the BBL format check_nyc_tax_lien_sale_list itself already
    returns for display). check_nyc_tax_lien_sale_list() was built and
    tested earlier in this project but never actually wired in here --
    found as an open item during the 2026-09-14 data-source audit.
    """
    parts = [p.strip() for p in bbl_string.split("-")]
    if len(parts) != 3 or not all(parts):
        raise ValueError("Enter NYC's BBL as borough-block-lot, e.g. 1-16-3 (see the example below the field).")
    borough, block, lot = parts
    return v.check_nyc_tax_lien_sale_list(borough, block, lot)


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
    "nyc": {
        "label": "New York City",
        "id_label": "BBL (borough-block-lot)",
        "example": "1-16-3",
        "fn": _check_nyc_tax_lien_by_bbl_string,
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


@app.route("/api/health/sources")
def api_health_sources():
    """
    Operator-facing diagnostic, not a customer-facing feature (not
    linked from the UI) -- a quick "is any external data source having
    a bad day right now" check without needing to dig through Render's
    log viewer. Per-process, in-memory, resets on every restart/deploy
    (see log_source_event's own docstring) -- a debugging aid, not a
    durable observability system.

    SECURE BY DEFAULT (fixed 2026-09-14, security audit): this used to
    be open to anyone who found the URL, with no secret required. Now
    disabled entirely (a plain 404, not a 401/403 -- never confirm the
    endpoint even exists to an unauthenticated caller) unless
    HEALTH_CHECK_SECRET is explicitly set in the environment, and even
    then only responds to a request carrying that exact secret. Costs
    nothing to leave off; an operator who wants this has to opt in.
    """
    expected_secret = os.environ.get("HEALTH_CHECK_SECRET")
    if not expected_secret:
        return jsonify({"error": "Not found."}), 404
    provided = request.args.get("secret") or request.headers.get("X-Health-Secret") or ""
    if not hmac.compare_digest(provided, expected_secret):
        return jsonify({"error": "Not found."}), 404
    return jsonify(v.get_source_health_summary())


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(force=True)
    state = (data.get("state") or "").strip().upper()
    county = (data.get("county") or "").strip() or None
    city = (data.get("city") or "").strip() or None
    min_acres = data.get("min_acres") or None
    max_acres = data.get("max_acres") or None
    # Deliberately NOT falling back to a server-side REALIE_API_KEY env
    # var (removed 2026-09-14, per explicit instruction: "I do not want
    # my own Realie tokens being spent in that way"). That fallback was
    # a real, flagged financial-exposure risk -- every visitor who
    # didn't paste in their own key would silently draw down the
    # operator's own free-tier allowance (or a paid plan the operator
    # personally pays for), with no per-visitor isolation or spend
    # visibility. Every search now requires the CALLER's own key, full
    # stop -- same bring-your-own-key discipline already used for
    # skip tracing, and for the same reason.
    api_key = (data.get("api_key") or "").strip()

    if not state:
        return jsonify({"error": "State is required."}), 400
    if not api_key:
        return jsonify({"error": "A Realie API key is required -- paste your own key in above. Get a free one at https://www.realie.ai/."}), 400

    try:
        results = v.search_vacant_land_realie(
            state, county=county, city=city,
            min_acres=float(min_acres) if min_acres else None,
            max_acres=float(max_acres) if max_acres else None,
            api_key=api_key, max_pages=1,
        )
        # Deliberately NOT including state/county/city here (fixed
        # 2026-09-14, security audit): which market a customer is
        # searching is THEIR OWN competitively sensitive business
        # activity, not diagnostic noise -- an earlier version of this
        # line put it in the detail field, which /api/health/sources
        # then exposed to anyone who found that URL. A result COUNT
        # with no geography still shows "Realie searches are failing
        # right now" without exposing what any specific customer is
        # doing.
        v.log_source_event("realie_search", "ok", detail=f"{len(results)} results")
    except Exception as e:
        v.log_source_event("realie_search", "error", detail=type(e).__name__)
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
@rate_limited(max_requests=30, window_seconds=60)
def api_parcel_report(apn):
    # The browser sends back the SAME raw record it already got from
    # its own earlier /api/search call -- no server-side lookup at all,
    # so there is nothing here that could ever be shared between two
    # different visitors' sessions (see the module-level note above).
    data = request.get_json(force=True) or {}
    raw = data.get("raw")
    if not raw:
        return jsonify({"error": "Missing parcel data -- run a search first."}), 400
    acres = data.get("acres")

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
    current_owner = v.get_current_owner_info(raw.get("salesHistory") or [])

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

    # CONTACT OWNER / OFFER-NEGOTIATE layer -- pure synthesis of everything
    # above, zero new network calls. See both functions' own docstrings
    # for why neither one suggests an offer price or gives contract advice.
    property_city = loc.get("city")
    property_state = loc.get("state")
    priority = v.build_instant_priority(tax_flags, current_owner, owner_mailing, property_city, property_state)
    try:
        buyer_fit = v.build_buyer_fit_tags(
            float(acres) if acres is not None else None,
            buildability if "error" not in buildability else {},
            evidence if "error" not in evidence else {},
        )
    except Exception as e:
        buyer_fit = [{"tag": "Buyer-fit unavailable", "reason": str(e)}]
    try:
        outreach_brief = v.build_owner_outreach_brief(
            current_owner, owner_mailing, tax_flags, priority, decision,
            evidence if "error" not in evidence else {},
            float(acres) if acres is not None else None,
            property_city, property_state,
        )
    except Exception as e:
        outreach_brief = {"error": str(e)}

    return jsonify({
        "apn": apn,
        "decision": decision,
        "buyer_fit": buyer_fit,
        "outreach_brief": outreach_brief,
        "evidence": evidence,
        "buildability": buildability,
        "tax_assessment": v.get_tax_assessment_info_realie(raw),
        "current_owner": current_owner,
        "tax_flags": tax_flags,
        "listing_links": listing_links,
        "zoning": zoning,
    })


@app.route("/api/parcel/score/bulk", methods=["POST"])
@rate_limited(max_requests=6, window_seconds=300)
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
            # Echoed back unchanged -- the frontend matches results back
            # to rows by this composite key, not bare apn, since APNs
            # are only unique within a county and a batch spanning
            # multiple counties could otherwise misattribute a result
            # to the wrong parcel (see rowKey() in index.html).
            "key": p.get("key"),
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
                results.append({"apn": p.get("apn"), "key": p.get("key"), "error": str(e)})

    return jsonify({
        "count": len(results),
        "results": results,
        "skipped": max(0, len(parcels) - len(to_score)),
    })


@app.route("/api/delinquency/<place_key>")
@rate_limited(max_requests=30, window_seconds=60)
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
        # Outcome only -- never the address/owner name this call just
        # looked up (that's real PII from a real person, not something
        # that belongs in a log file even without a name attached to it).
        v.log_source_event("tracerfy", "ok" if result.get("hit") else "no_coverage")
    except Exception as e:
        v.log_source_event("tracerfy", "error", detail=type(e).__name__)
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
            # Echoed back unchanged in each result -- the frontend
            # matches results back to rows by this composite key, not
            # bare apn, since two different parcels in one batch could
            # share an APN across counties (see rowKey() in index.html).
            "key": parcel.get("key"),
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
    # Secure by default (fixed 2026-09-14, security audit): this used to
    # default to "true" when FLASK_DEBUG was unset. Flask's debug mode
    # includes the interactive Werkzeug debugger, which lets anyone who
    # can trigger a traceback run arbitrary Python code from their
    # browser -- a severe vulnerability if this ever ran exposed to the
    # internet with debug on. Unreachable in production today (gunicorn
    # imports `app` directly and never executes this block at all), but
    # a secure-by-default fallback shouldn't rely on that alone -- an
    # insecure default is still an insecure default even when today's
    # deployment happens not to exercise it. Explicitly opt IN with
    # FLASK_DEBUG=true for local development instead.
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
