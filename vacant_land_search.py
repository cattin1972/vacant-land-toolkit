#!/usr/bin/env python3
"""
vacant_land_search.py

Standalone land-investment analysis toolkit. What's in here:
  1. search_vacant_land_parcels() — finds vacant land parcels (via
     Regrid or ATTOM) matching an acreage range + state, saves them to
     a PostGIS database, and returns each one's ID (APN) + coordinates.
  2. get_current_owner_info() — takes a parcel's deed/sale records,
     finds the most recent sale, and reports on the current owner only:
     their name, whether it's a person or a company/trust, what they
     paid, and exactly how many years they've owned the property.
  3. get_comprehensive_buildability_report() — the full physical/
     environmental buildability picture for a lot, combining:
       - calculate_buildability_score() — FEMA flood maps + USFWS
         wetland maps, exact to the parcel boundary
       - get_septic_suitability() — USDA soil survey septic rating
       - get_natural_hazard_risk() — FEMA National Risk Index (hurricane,
         wildfire, earthquake, drought, heat/cold wave, tornado, etc.)
       - check_road_access() — is it landlocked?
       - check_environmental_designations() — coastal barrier zones +
         protected species habitat
       - check_nearby_development() — rough proxy for "are utilities
         likely already run here" (NOT a confirmed hookup answer — see
         its docstring)
       - get_slope_estimate() — flags steep/unbuildable terrain
       - check_contamination_sites() — EPA Superfund + Brownfields
       - check_fine_grained_wildfire_risk() — 30m-pixel USFS wildfire data
       - check_broadband_availability() — is internet actually available
     All free, public, no API key needed. Does NOT cover zoning/lot-size
     rules or legal/title issues — see that function's docstring.
  4. add_market_activity_metrics() — for a parcel, counts active home
     builders/contractors within 15 miles and recent new-home permit
     activity in that ZIP code, as extra signals for the parcel profile.
  5. Realie integration (search_vacant_land_realie(), get_tax_assessment
     _info_realie(), count_new_homes_built_realie()) — a FREE (up to
     ~2,500 records/month) alternative to Regrid/ATTOM for #1/#2/#3's
     paid-data needs. Every result is hard-filtered to exclude any
     parcel with so much as one structure on it (checked two
     independent ways — see _is_genuinely_vacant) since this toolkit is
     built for land wholesalers, not homes. Every result also carries a
     tax_flags section (get_tax_flags — liens/foreclosures, straight
     from Realie's own data) plus an optional exclude_tax_flagged
     filter (off by default).
  6. City/county tax delinquency (check_king_county_delinquent_tax(),
     check_pittsburgh_tax_delinquency(), check_philadelphia_tax
     _delinquency()) — real, official, free government APIs for these
     three specific places only. No nationwide equivalent exists — most
     of the country has no free structured source for this at all, and
     this toolkit deliberately does NOT scrape sites to work around
     that gap (see the no-scraping rule below).

Install:
    pip install psycopg2-binary requests shapely pyproj

Environment variables (never hardcode secrets in the script):
    PROPERTY_API_PROVIDER      "regrid" or "attom"        (default: "regrid")
    PROPERTY_API_KEY           your vendor API key/token   (required for #1)
    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD          standard libpq vars
    CONTRACTOR_REGISTRY_API_URL / _API_KEY   your contractor-data provider (for #4)
    PERMIT_TRACKER_API_URL / _API_KEY        your permit-data provider (for #4)

Run:
    python vacant_land_search.py --state TX --min-acres 5 --max-acres 40

IMPORTANT — read before use:
    Regrid and ATTOM are commercial APIs that require an active paid
    subscription and a valid API key/token; this script does not, and
    cannot, work around that. The request paths, params, and response
    field names below reflect each vendor's general documented shape,
    but exact schemas vary by subscription tier and change over time.
    Before running this against production, confirm the current field
    names (APN, acreage, land-use/property-type code, lat/lon) against
    your own account's API docs and adjust PARCEL_FIELD_MAP / the query
    params accordingly. Also respect each vendor's rate limits, terms
    of use, and any restrictions on bulk/automated querying.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator
from urllib.parse import quote_plus

import psycopg2
import psycopg2.extras
import requests
from shapely.geometry import Polygon, shape
from shapely.ops import transform as shp_transform, unary_union

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vacant_land_search")

# --------------------------------------------------------------------------
# Structured per-source event logging (2026-09-14 external-data-source
# audit). Every external call site in this file already has its own
# try/except with a hand-written log message -- useful to a human
# reading logs live, but inconsistent in format and impossible to
# aggregate ("how often has FEMA failed this week?"). This adds ONE
# consistent, greppable log line plus a small in-process counter for a
# lightweight health snapshot, without a new logging framework/service.
#
# PRIVACY: `source` and `status` are always short fixed vocabulary
# (a data-source name, "ok"/"no_coverage"/"source_unavailable"/"error").
# `detail` must NEVER contain a parcel address, an owner's name, or an
# API key -- an HTTP status code or exception class name is fine. This
# is enforced by convention at each call site, not automatically, so
# any new call site added later must follow the same rule.
#
# The counter is per-process, in-memory, and resets on every restart/
# deploy -- it is a quick "is anything having a bad day right now"
# check, not a durable observability system. If this ever runs behind
# multiple gunicorn workers, each worker has its own counter; that's an
# accepted, documented limitation, not a bug.
_SOURCE_EVENT_LOCK = threading.Lock()
_SOURCE_EVENT_COUNTS: dict[tuple[str, str], int] = {}
_SOURCE_EVENT_RECENT: list[dict] = []
_MAX_RECENT_SOURCE_EVENTS = 200


def log_source_event(source: str, status: str, detail: str | None = None) -> None:
    """
    Records one external-data-source call outcome. `status` should be
    one of: "ok", "no_coverage" (source responded, nothing usable for
    this location), "source_unavailable" (request failed/timed out),
    or "error" (an unexpected exception). See the module note above for
    the privacy rule on `detail`.
    """
    log.info("data_source_event source=%s status=%s%s", source, status, f" detail={detail}" if detail else "")
    with _SOURCE_EVENT_LOCK:
        key = (source, status)
        _SOURCE_EVENT_COUNTS[key] = _SOURCE_EVENT_COUNTS.get(key, 0) + 1
        _SOURCE_EVENT_RECENT.append({
            "source": source, "status": status, "detail": detail,
            "at": datetime.utcnow().isoformat() + "Z",
        })
        if len(_SOURCE_EVENT_RECENT) > _MAX_RECENT_SOURCE_EVENTS:
            del _SOURCE_EVENT_RECENT[0]


def get_source_health_summary() -> dict:
    """
    A small in-process snapshot for a health/diagnostics endpoint --
    NOT a durable store (see the module note above). Good for spotting
    "FEMA's endpoint has failed 8 times in the last hour," not for
    long-term trend analysis.
    """
    with _SOURCE_EVENT_LOCK:
        totals: dict[str, dict[str, int]] = {}
        for (source, status), count in _SOURCE_EVENT_COUNTS.items():
            totals.setdefault(source, {})[status] = count
        return {"totals": totals, "recent_events": list(_SOURCE_EVENT_RECENT[-50:])}

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

API_PROVIDER = os.environ.get("PROPERTY_API_PROVIDER", "regrid").lower()
API_KEY = os.environ.get("PROPERTY_API_KEY")

REGRID_SEARCH_URL = "https://app.regrid.com/api/v2/parcels/query"
ATTOM_SEARCH_URL = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/basicprofile"

REQUEST_TIMEOUT_S = 30
PAGE_SIZE = 200
MAX_PAGES = 500  # hard stop to avoid runaway pagination loops

DB_DSN = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": os.environ.get("PGPORT", "5432"),
    "dbname": os.environ.get("PGDATABASE", "parcels"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", ""),
}

# Free, public, no-API-key-required government GIS endpoints used by
# calculate_buildability_score().
FEMA_NFHL_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
USFWS_WETLANDS_URL = "https://www.fws.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0/query"
CENSUS_ZCTA_URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/4/query"

# USGS point-elevation service (used to estimate slope/steepness).
USGS_EPQS_URL = "https://epqs.nationalmap.gov/v1/json"

# EPA contamination data: Superfund (NPL, the ~1,300 most serious sites)
# and Brownfields/ACRES (a much larger set of assessed sites).
EPA_SUPERFUND_URL = "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/FAC_Superfund_Site_Boundaries_EPA_Public/FeatureServer/0/query"
EPA_BROWNFIELDS_URL = "https://geopub.epa.gov/arcgis/rest/services/EMEF/efpoints/MapServer/5/query"

# US Forest Service Wildfire Hazard Potential — 30m-pixel, finer than
# FEMA's tract-level rating. NOTE: the originally-documented URL for
# this service was dead (migrated); this is the live replacement as of
# 2026-09-11 — if it goes dead again, search "USFS Wildfire Hazard
# Potential ImageServer" to find wherever it's moved to next.
USFS_WILDFIRE_HAZARD_URL = "https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/USFS_EDW_RMRS_WildfireHazardPotentialContinuous/ImageServer/identify"

# FCC Broadband Data Collection — official data, but mirrored via Esri
# Living Atlas because fcc.gov's own API requires an account/token and
# its web host actively blocks automated requests.
FCC_BROADBAND_MIRROR_URL = "https://services8.arcgis.com/peDZJliSvYims39Q/arcgis/rest/services/FCC_Broadband_Data_Collection_December_2024_View/FeatureServer/4/query"

# FEMA's National Risk Index — one free federal dataset covering nearly
# every major U.S. natural hazard (see _NRI_HAZARD_CODES below), rated
# per census tract (a tract is roughly a few thousand people's worth of
# area — this is neighborhood-level risk, not exact-parcel level, but
# it's the best free nationwide source for "everything hazard-wise").
NRI_CENSUS_TRACT_URL = "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer/0/query"

# USDA Soil Data Access — the official government soil survey (SSURGO),
# free, no key. Used to check septic-system suitability.
SOIL_DATA_ACCESS_URL = "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"

# Census TIGER/Line roads — used to check for legal road access. Layer 8
# ("Local Roads") is the finest-detail layer; layers 0-6 are highways/
# secondary roads.
ROADS_URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/8/query"

# Census feature-class codes that count as "a real road" for access
# purposes (S1100 = highway, S1200 = secondary road, S1400 = local/rural
# road). Excludes trails (S1500), driveways, alleys, parking lots, etc.
_QUALIFYING_ROAD_MTFCC = {"S1100", "S1200", "S1400"}

# Coastal Barrier Resources System (USFWS) — zones where federal flood
# insurance and federal infrastructure funding are NOT available.
CBRS_URL = "https://gis1.wim.usgs.gov/server/rest/services/CBRSMapper/CoastalBarrierResourcesSystem/MapServer/3/query"

# USFWS Critical Habitat (final designations) — protected areas for
# threatened/endangered species that can restrict development.
CRITICAL_HABITAT_URL = "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/USFWS_Critical_Habitat/FeatureServer/0/query"

# OpenStreetMap Overpass API — free, no key, but shared/public, so it's
# rate-limited and needs a real User-Agent header or it 406s.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# There is no single nationwide contractor-registry or permit-tracker
# API — these point at whatever provider you have (a state licensing
# board, a commercial data vendor, a city/county open-data portal).
# Leave unset and those specific functions will just tell you so.
CONTRACTOR_REGISTRY_API_URL = os.environ.get("CONTRACTOR_REGISTRY_API_URL")
CONTRACTOR_REGISTRY_API_KEY = os.environ.get("CONTRACTOR_REGISTRY_API_KEY")
PERMIT_TRACKER_API_URL = os.environ.get("PERMIT_TRACKER_API_URL")
PERMIT_TRACKER_API_KEY = os.environ.get("PERMIT_TRACKER_API_KEY")


@dataclass(frozen=True)
class Parcel:
    apn: str
    state: str
    acres: float | None
    lat: float
    lon: float
    raw: dict  # full raw API record, kept for traceability/audit


def _parse_date(value: Any) -> date | None:
    """Best-effort parse of a date coming back from any of these APIs —
    formats vary a lot between providers, so try the common ones."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        log.warning("Could not parse date value: %r", value)
        return None


# --------------------------------------------------------------------------
# Database (PostGIS)
# --------------------------------------------------------------------------

def get_db_connection():
    """Open a connection to the PostGIS-enabled PostgreSQL database."""
    conn = psycopg2.connect(**DB_DSN)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    conn.commit()
    return conn


def ensure_schema(conn) -> None:
    """Create the parcels table (idempotent) with a spatial point column."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vacant_land_parcels (
                apn         TEXT NOT NULL,
                state       TEXT NOT NULL,
                acres       DOUBLE PRECISION,
                lat         DOUBLE PRECISION NOT NULL,
                lon         DOUBLE PRECISION NOT NULL,
                geom        GEOMETRY(Point, 4326),
                raw         JSONB,
                fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (apn, state)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_vacant_land_parcels_geom "
            "ON vacant_land_parcels USING GIST (geom);"
        )
    conn.commit()


def upsert_parcels(conn, parcels: Iterable[Parcel]) -> None:
    """Insert/update parcels, deduped on (apn, state)."""
    rows = [
        (p.apn, p.state, p.acres, p.lat, p.lon, p.lon, p.lat, psycopg2.extras.Json(p.raw))
        for p in parcels
    ]
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO vacant_land_parcels (apn, state, acres, lat, lon, geom, raw)
            VALUES %s
            ON CONFLICT (apn, state) DO UPDATE
                SET acres      = EXCLUDED.acres,
                    lat        = EXCLUDED.lat,
                    lon        = EXCLUDED.lon,
                    geom       = EXCLUDED.geom,
                    raw        = EXCLUDED.raw,
                    fetched_at = now();
            """,
            rows,
            template="(%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)",
        )
    conn.commit()


# --------------------------------------------------------------------------
# Property API clients
#
# Each provider function is a generator that yields raw JSON records for
# one page at a time. Pagination style, auth header, and field names are
# vendor-specific — confirm against your account's current docs.
# --------------------------------------------------------------------------

def _require_api_key() -> str:
    if not API_KEY:
        raise RuntimeError(
            "PROPERTY_API_KEY environment variable is not set. "
            "Set it to your Regrid/ATTOM API key before running."
        )
    return API_KEY


def fetch_regrid_pages(min_acres: float, max_acres: float, target_state: str) -> Iterator[list[dict]]:
    """
    Query Regrid's parcel search for vacant land in `target_state` within
    the given acreage range. Regrid classifies land use via a `usedesc` /
    `zoning` style field on the parcel record (naming varies by dataset
    vintage) — adjust the filter below to match what your account returns.
    """
    api_key = _require_api_key()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    page = 1
    while page <= MAX_PAGES:
        params = {
            "token": api_key,
            "state": target_state,
            "acres_min": min_acres,
            "acres_max": max_acres,
            # TODO: confirm the exact land-use filter param/value for your
            # Regrid dataset tier, e.g. "usecode" / "usedesc" == "Vacant".
            "land_use": "vacant",
            "page": page,
            "limit": PAGE_SIZE,
        }
        resp = session.get(REGRID_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()

        records = payload.get("parcels") or payload.get("results") or []
        if not records:
            break

        yield records

        # Adjust to however Regrid signals "more pages" in your response
        # (e.g. a `next` cursor or `total_pages` field).
        if len(records) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.2)  # be polite to the API / respect rate limits


def fetch_attom_pages(min_acres: float, max_acres: float, target_state: str) -> Iterator[list[dict]]:
    """
    Query ATTOM for vacant land parcels. ATTOM's property endpoints are
    generally geo/address-scoped rather than pure state+acreage search,
    so a state-wide query typically needs to be driven county-by-county
    or via ATTOM's Area/Boundary API to get valid `geoId` values first.
    This function assumes you've already resolved `target_state` to the
    county-level geoIds you want to sweep; adapt as needed.
    """
    api_key = _require_api_key()
    session = requests.Session()
    session.headers.update({"apikey": api_key, "Accept": "application/json"})

    page = 1
    while page <= MAX_PAGES:
        params = {
            "state": target_state,
            "minAcres": min_acres,
            "maxAcres": max_acres,
            # TODO: confirm ATTOM's current vacant-land property type code
            # for your subscription (commonly propertyType ~ "VL00").
            "propertyType": "VL00",
            "page": page,
            "pagesize": PAGE_SIZE,
        }
        resp = session.get(ATTOM_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()

        records = payload.get("property") or []
        if not records:
            break

        yield records

        status = payload.get("status", {})
        if status.get("total", 0) <= page * PAGE_SIZE:
            break
        page += 1
        time.sleep(0.2)


def _extract_parcel(record: dict, target_state: str, provider: str) -> Parcel | None:
    """
    Normalize a raw provider record into a Parcel. Field names are the
    most common ones seen in each vendor's docs; fall back gracefully
    and skip records that are missing the essentials (APN + coordinates).
    """
    if provider == "regrid":
        apn = record.get("parcelnumb") or record.get("apn")
        acres = record.get("gisacre") or record.get("acres")
        lat = record.get("lat") or (record.get("geometry") or {}).get("lat")
        lon = record.get("lon") or (record.get("geometry") or {}).get("lon")
    else:  # attom
        identifier = record.get("identifier", {}) or {}
        location = record.get("location", {}) or {}
        lot = record.get("lot", {}) or {}
        apn = identifier.get("apn") or identifier.get("Apn")
        acres = lot.get("lotSize1") or lot.get("acres")
        lat = location.get("latitude")
        lon = location.get("longitude")

    if not apn or lat is None or lon is None:
        log.warning("Skipping record missing apn/lat/lon: %s", record)
        return None

    try:
        return Parcel(
            apn=str(apn),
            state=target_state,
            acres=float(acres) if acres is not None else None,
            lat=float(lat),
            lon=float(lon),
            raw=record,
        )
    except (TypeError, ValueError):
        log.warning("Skipping record with non-numeric acres/lat/lon: %s", record)
        return None


# --------------------------------------------------------------------------
# New homes built in an area, over a chosen time frame (Regrid only)
#
# Regrid ($10-20/mo Pro or Team plan): https://app.regrid.com/api/plans
#
# HONEST LIMITS on this one, unlike everything else in this file:
#   1. Not live-tested — Regrid requires a paid account, which wasn't
#      available while writing this. Everything below follows Regrid's
#      documented query syntax exactly (verified against their current
#      docs), EXCEPT the exact shape of the count in the response,
#      which their docs don't show without being logged into an
#      account. The code below tries several likely response shapes
#      and, if none match, raises a clear error showing you the real
#      response so this can be fixed in a couple minutes once you have
#      API access — not silently return a wrong number.
#   2. County "year built" records store a YEAR ONLY, never a month or
#      day. So a request like "last 18 months" can't be exact to the
#      day — it's rounded UP to whole calendar years (2 years, in that
#      example) to stay inclusive rather than miss recent homes.
# --------------------------------------------------------------------------

def count_new_homes_built(area_path: str, months_back: int, api_key: str | None = None) -> dict:
    """
    Counts homes in a given area whose county-assessor "year built"
    falls within roughly the last `months_back` months (see the section
    note above for why this is year-precision, not day-precision).

    `area_path` uses Regrid's path format: "/us/<state>/<county>", e.g.
    "/us/fl/lee" for Lee County, FL (which contains Cape Coral). Regrid's
    docs suggest city-level paths may also work (e.g.
    "/us/fl/lee/cape-coral") but that wasn't confirmed live — try it,
    and fall back to the county-level path if it doesn't return results.

    Returns:
        {
            "area_path": "/us/fl/lee",
            "year_range": [2024, 2026],
            "new_homes_built": 1847,
        }
    """
    api_key = api_key or API_KEY
    if not api_key:
        raise RuntimeError(
            "No Regrid API key set. Get one at https://app.regrid.com/api/plans "
            "and pass it as api_key= or set PROPERTY_API_KEY."
        )
    if months_back <= 0:
        raise ValueError("months_back must be positive")

    current_year = date.today().year
    years_back = max(1, -(-months_back // 12))  # ceiling division — stays inclusive
    start_year = current_year - years_back

    params = {
        "token": api_key,
        "path": area_path,
        "fields[yearbuilt][between]": f"[{start_year},{current_year}]",
        "return_count": "true",
        "return_parcels": "false",  # we only need the count, not every record
        "limit": 1,
    }
    resp = requests.get(REGRID_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()

    # Try the response shapes that would be typical for this kind of
    # API; if none match, fail loudly with the real payload rather than
    # guess wrong silently.
    count = None
    for path_guess in (
        lambda p: p["count"],
        lambda p: p["total"],
        lambda p: p["meta"]["count"],
        lambda p: p["meta"]["total"],
        lambda p: p["parcels"]["count"],
    ):
        try:
            count = path_guess(payload)
            break
        except (KeyError, TypeError):
            continue

    if count is None:
        raise RuntimeError(
            "Couldn't find a count field in Regrid's response — the response shape "
            f"wasn't confirmed live before this was written. Raw response: {json.dumps(payload)[:1000]}"
        )

    return {
        "area_path": area_path,
        "year_range": [start_year, current_year],
        "new_homes_built": count,
    }


# --------------------------------------------------------------------------
# Tax / assessment records (Regrid only)
#
# Pulls straight from the raw parcel record already fetched by
# search_vacant_land_parcels() — no extra API call needed. Confirmed
# against Regrid's current field documentation (not live-tested against
# a real account, same honest caveat as count_new_homes_built above).
#
# Does NOT include delinquency / unpaid-tax status — no source, free or
# paid, tracks that nationally; it lives only inside each county's own
# separate tax-collector system. If that specific signal matters enough
# to you, it would need a new per-county research effort, the same kind
# of scoping decision as the contractor/permit data earlier.
# --------------------------------------------------------------------------

def get_tax_assessment_info(parcel_raw: dict) -> dict:
    """
    Pulls property tax and assessed-value fields out of a parcel's raw
    Regrid record (the `raw` dict already stored on each Parcel from
    search_vacant_land_parcels).

    Returns:
        {
            "assessed_value": ...,       # parval -- total parcel value per the assessor
            "value_type": ...,           # parvaltype -- e.g. "Assessed", "Market", "Appraised"
            "land_value": ...,           # landval -- value of just the land
            "improvement_value": ...,    # improvval -- value of any buildings (usually 0/None on vacant land)
            "agricultural_value": ...,   # agval -- if taxed under an ag-use exemption (common on raw land)
            "annual_tax_amount": ...,    # taxamt
            "tax_year": ...,             # taxyear -- year these values apply to
            "homestead_exemption": ...,  # Regrid Premium tier only -- None if not on your plan
        }

    Every field is None if that county didn't provide it — not every
    county reports every field.
    """
    return {
        "assessed_value": parcel_raw.get("parval"),
        "value_type": parcel_raw.get("parvaltype"),
        "land_value": parcel_raw.get("landval"),
        "improvement_value": parcel_raw.get("improvval"),
        "agricultural_value": parcel_raw.get("agval"),
        "annual_tax_amount": parcel_raw.get("taxamt"),
        "tax_year": parcel_raw.get("taxyear"),
        "homestead_exemption": parcel_raw.get("homestead_exemption"),
    }


# ============================================================================
# Realie integration — free-tier alternative to Regrid/ATTOM
#
# Realie (realie.ai) has a genuine free tier: 25 tokens/month, and 1
# successful request = 1 token regardless of how many of the up to 100
# parcels it returns — so up to ~2,500 free parcel records/month. Signup
# needs a card on file (a $0.50 temporary hold, not a real charge, per
# their own FAQ) but shouldn't cost anything within the free tier.
#
# HONEST LIMITS, same discipline as the Regrid section above:
#   - Query syntax verified against Realie's current live documentation
#     (docs.realie.ai), NOT live-tested against a real account/API key —
#     none was available while writing this. Get a free key at
#     https://www.realie.ai/ and hand it over for a real test pass.
#   - Realie's search only filters by state/county/city/zip/use-code —
#     NOT by acreage or year-built range. So area-wide searches pull
#     everything for the chosen area (paginated) and filter locally,
#     which costs more of the free monthly budget on a big area than a
#     small one — start with one city/county at a time, not a state.
# ============================================================================

REALIE_SEARCH_URL = "https://app.realie.ai/api/public/property/search/"
REALIE_API_KEY = os.environ.get("REALIE_API_KEY")

# Realie's propertyUseCode values for vacant/unimproved land (their own
# "8000 series"). Deliberately EXCLUDES:
#   8011 "Water Area" — not land at all
#   8014 "Under Construction" — a structure is actively being built;
#        not available raw land for a wholesaler's purposes
_VACANT_LAND_USE_CODES = {
    8000, 8001, 8002, 8003, 8004, 8005, 8006, 8007,
    8008, 8009, 8010, 8012, 8013, 8015, 8016, 8017,
}


def _require_realie_api_key(api_key: str | None) -> str:
    api_key = api_key or REALIE_API_KEY
    if not api_key:
        raise RuntimeError(
            "No Realie API key set. Get a free one at https://www.realie.ai/ "
            "and pass it as api_key= or set REALIE_API_KEY."
        )
    return api_key


def _is_genuinely_vacant(record: dict) -> bool:
    """
    The hard, explicit filter: TRUE only if this parcel has no house or
    other structure on it at all. Checked two independent ways, and
    BOTH must agree — never trust a single signal for this:
      1. Its land-use code is one of Realie's "vacant land" codes.
      2. Every entry in buildingInformation.buildings has zero building
         area (i.e. no real structure).
    A land-use code can be stale or wrong in county records, so the
    building-area check is the real backstop.

    BUG FIX (2026-09-12, confirmed against real live Realie data): this
    used to check `len(buildings) == 0`, but Realie's real API always
    returns exactly one entry in buildings — even for genuinely vacant
    8000-series parcels — as an all-zero placeholder (buildingArea 0,
    0 bedrooms, etc). That meant the old length check was never true and
    this function rejected 100% of results, always. Verified live: of
    100 real Cape Coral, FL records, 52 were use-code 8001 (vacant) and
    ALL 52 had buildingArea == 0 across every building entry; the other
    48 were use-code 1001 (residential) with mostly nonzero building
    area. No case was found (in this sample) of a vacant-code parcel
    with nonzero building area, but the two-signal design is kept as
    the backstop regardless.
    """
    use_code = record.get("propertyClassification", {}).get("propertyUseCode") or record.get("useCode")
    try:
        use_code = int(use_code) if use_code is not None else None
    except (TypeError, ValueError):
        use_code = None
    code_says_vacant = use_code in _VACANT_LAND_USE_CODES

    buildings = record.get("buildingInformation", {}).get("buildings") or []
    total_building_area = sum((b.get("buildingArea") or 0) for b in buildings)
    has_no_buildings = total_building_area == 0

    return code_says_vacant and has_no_buildings


def _extract_acres(record: dict) -> float | None:
    """Realie has three possible acreage fields; prefer the GIS-derived
    one, then fall back in order (per their own documented guidance)."""
    land = record.get("landInformation", {})
    for key in ("calculatedAcres", "acres", "deedAcres"):
        value = land.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def fetch_realie_properties(
    state: str,
    county: str | None = None,
    city: str | None = None,
    api_key: str | None = None,
    max_pages: int = 25,
) -> Iterator[list[dict]]:
    """
    Pages through every property Realie has for the given area (a
    two-letter state is required; narrow further with county and/or
    city — the smaller the area, the less of your free monthly token
    budget it costs). Yields one page (up to 100 raw records) at a time.
    """
    api_key = _require_realie_api_key(api_key)
    # Confirmed against Realie's live API docs (2026-09-12): the key goes
    # straight in the Authorization header, NOT as "Bearer <key>" — that
    # Bearer-prefixed form 401s.
    headers = {"Authorization": api_key}

    cursor = None
    for _ in range(max_pages):
        params = {"state": state, "limit": 100}
        if county:
            params["county"] = county
        if city:
            params["city"] = city
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(REALIE_SEARCH_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()

        # Confirmed shape: {"properties": [...], "metadata": {"nextCursor": ...}}
        records = payload.get("properties") or payload.get("results") or payload.get("data") or []
        if not records:
            break
        yield records

        cursor = (payload.get("metadata") or {}).get("nextCursor") or payload.get("cursor") or payload.get("nextCursor")
        if not cursor:
            break


# --------------------------------------------------------------------------
# City/county-level tax delinquency — real, official, free government
# datasets (Tier 1 of the broader tax-delinquency research).
#
# There's no nationwide equivalent of this — most of the country isn't
# covered. These three places happen to publish real, free, structured,
# official delinquent-tax data with a genuine API (not scraping). Where
# they apply, they're more authoritative than get_tax_flags() (which
# only reflects whatever Realie's own data happens to include).
# --------------------------------------------------------------------------

KING_COUNTY_DELINQUENT_TAX_URL = "https://data.kingcounty.gov/resource/dsv3-ct3e.json"
WPRDC_DATASTORE_URL = "https://data.wprdc.org/api/3/action/datastore_search"
PITTSBURGH_TAX_DELINQUENCY_RESOURCE_ID = "ed0d1550-c300-4114-865c-82dc7c23235b"
PHILADELPHIA_CARTO_SQL_URL = "https://phl.carto.com/api/v2/sql"

# Expansion (2026-09-12) -- same category as the 3 above: real, free,
# structured, official government APIs, found by systematically
# searching for more cities/counties that publish this data this way.
# Still no nationwide source exists; each of these is its own one-off.
NORFOLK_DELINQUENT_TAX_URL = "https://data.norfolk.gov/resource/7qie-z5gv.json"
SONOMA_DELINQUENT_TAX_URL = "https://data.sonomacounty.ca.gov/resource/bp8v-uax7.json"
RICHMOND_DELINQUENT_TAX_URL = "https://data.richmondgov.com/resource/83t5-hbac.json"
NYC_TAX_LIEN_SALE_LIST_URL = "https://data.cityofnewyork.us/resource/9rz4-mjek.json"
CACHE_COUNTY_DELINQUENT_TAX_URL = "https://gis.cachecounty.gov/arcgis/rest/services/Treasurer/Delinquent_Taxes_Public_Red/MapServer/1/query"


def check_king_county_delinquent_tax(account_number: str) -> dict:
    """
    Looks up King County, WA's official delinquent-tax dataset by tax
    account number. Free, official, no key.

    NOTE on units: the raw billed_amount/paid_amount fields are
    fixed-width integers with no visible decimal point. Based on the
    actual sample values (a $1.275M-land-value parcel showing a
    ~$59,424 main levy bill checks out as a realistic ~1.1% WA property
    tax rate when read as cents), this treats them as CENTS and divides
    by 100 — an inference from the data's own numbers, not confirmed
    against King County's official data dictionary. Sanity-check this
    against a known bill before trusting it for real decisions.

    Also note: this dataset is one row per billing line item (levy
    code/year), not one row per parcel — this sums every row for the
    account to get a total.

    Returns:
        {"account_number": ..., "total_billed": ..., "total_paid": ...,
         "amount_owed": ..., "delinquent": True/False}
    or {} if the account isn't in the dataset.
    """
    params = {"account_number": account_number, "$limit": 1000}
    resp = requests.get(KING_COUNTY_DELINQUENT_TAX_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return {}

    def _to_cents(v):
        # A genuinely missing value is a normal 0; a PRESENT-but-
        # unparseable value must not silently become 0 either, since
        # billed_amount and paid_amount are subtracted from each other
        # below -- a silent 0 on either side can flip "delinquent" wrong
        # in EITHER direction (understating what's billed, or
        # overstating what's paid), not just toward a false negative.
        if v is None or (isinstance(v, str) and not v.strip()):
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            raise _UnparseableAmount(f"Could not parse billed/paid amount as a number: {v!r}")

    try:
        total_billed_cents = sum(_to_cents(r.get("billed_amount")) for r in rows)
        total_paid_cents = sum(_to_cents(r.get("paid_amount")) for r in rows)
    except _UnparseableAmount as e:
        log.warning("King County tax delinquency: %s (account_number=%s)", e, account_number)
        return {
            "account_number": account_number,
            "amount_owed": None,
            "delinquent": None,
            "data_quality_issue": "A billing record exists but a billed/paid amount could not be parsed from the source data -- verify manually.",
        }
    owed_cents = total_billed_cents - total_paid_cents

    return {
        "account_number": account_number,
        "total_billed": round(total_billed_cents / 100, 2),
        "total_paid": round(total_paid_cents / 100, 2),
        "amount_owed": round(owed_cents / 100, 2),
        "delinquent": owed_cents > 0,
    }


def check_pittsburgh_tax_delinquency(pin: str) -> dict:
    """
    Looks up City of Pittsburgh's official tax delinquency dataset by
    parcel PIN. Free, official (via the Western PA Regional Data
    Center), no key.

    Returns:
        {"pin": ..., "address": ..., "current_owed": ...,
         "prior_years_owed": ..., "total_owed": ..., "delinquent": True/False}
    or {} if the PIN isn't in the dataset — here, that's a GOOD sign
    (means no delinquency on record for that parcel).
    """
    params = {
        "resource_id": PITTSBURGH_TAX_DELINQUENCY_RESOURCE_ID,
        "filters": json.dumps({"pin": pin}),
        "limit": 5,
    }
    resp = requests.get(WPRDC_DATASTORE_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    records = resp.json().get("result", {}).get("records", [])
    if not records:
        return {}

    row = records[0]
    current_owed = float(row.get("current_delq_tax") or 0) + float(row.get("current_delq_pi") or 0)
    prior_owed = float(row.get("prior_delq_tax") or 0) + float(row.get("prior_delq_pi") or 0)

    return {
        "pin": pin,
        "address": row.get("address"),
        "current_owed": round(current_owed, 2),
        "prior_years_owed": round(prior_owed, 2),
        "total_owed": round(current_owed + prior_owed, 2),
        "delinquent": (current_owed + prior_owed) > 0,
    }


def check_philadelphia_tax_delinquency(opa_number: str) -> dict:
    """
    Looks up Philadelphia's official real-estate tax delinquency
    dataset by OPA parcel number. Free, official (City of Philadelphia
    Carto SQL API), no key.

    Returns:
        {"opa_number": ..., "owner": ..., "total_due": ...,
         "num_years_owed": ..., "oldest_year_owed": ..., "delinquent": True/False}
    or {} if not found.
    """
    opa_number = str(opa_number).strip()
    if not opa_number.isdigit():
        raise ValueError(f"opa_number must be numeric, got: {opa_number!r}")

    query = f"SELECT * FROM real_estate_tax_delinquencies WHERE opa_number = '{opa_number}' LIMIT 1"
    resp = requests.get(PHILADELPHIA_CARTO_SQL_URL, params={"q": query}, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    rows = resp.json().get("rows", [])
    if not rows:
        return {}

    row = rows[0]
    total_due = row.get("total_due") or 0
    return {
        "opa_number": opa_number,
        "owner": row.get("owner"),
        "total_due": total_due,
        "num_years_owed": row.get("num_years_owed"),
        "oldest_year_owed": row.get("oldest_year_owed"),
        "delinquent": total_due > 0,
    }


def _get_socrata_json(url: str, params: dict):
    """
    GET a Socrata (SODA) open-data endpoint with a couple of quiet
    retries. These public city/county portals occasionally return a
    transient 503 under load (confirmed live on NYC's tax-lien-sale
    dataset -- same request, same params, succeeded on a plain retry a
    few seconds later) -- worth retrying rather than failing the whole
    lookup over a blip, same discipline as the ArcGIS retry logic above.
    A 4xx (bad params, not found) is a real error and is NOT retried.
    """
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
            if resp.status_code >= 500:
                last_error = requests.exceptions.HTTPError(
                    f"{resp.status_code} Server Error for url: {resp.url}", response=resp
                )
            else:
                resp.raise_for_status()
                return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
        log.warning("Retrying %s after error (attempt %d/3): %s", url, attempt + 1, last_error)
        time.sleep(1.5 * (attempt + 1))
    raise last_error


class _UnparseableAmount(ValueError):
    """A dollar-amount field was PRESENT but could not be parsed as a
    number -- deliberately a different case from the field being
    genuinely absent. See _to_float's docstring for why this
    distinction matters."""


def _to_float(value, default=0.0) -> float:
    """
    Socrata datasets return every number as a string, sometimes with a
    leading '$' and thousands commas (confirmed live in Richmond's
    dataset) -- strip that before parsing.

    A genuinely missing/blank value (None or "") is a normal, legitimate
    case and defaults to 0.0 -- no dollar amount reported for that row.
    A value that IS present but doesn't parse as a number is a
    different and more serious case: silently treating it as $0 would
    turn "we couldn't read this field" into a confident "$0 owed" /
    "not delinquent" for a row that the dataset actually returned as a
    real delinquency record. Raises _UnparseableAmount for that case
    instead so callers can surface an uncertain result, never a false
    negative.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        raise _UnparseableAmount(f"Could not parse amount field as a number: {value!r}")


def check_norfolk_tax_delinquency(biitem: str) -> dict:
    """
    Looks up Norfolk, VA's official delinquent real-estate tax dataset
    by "biitem" (their internal parcel/bill item number). Free, official
    (Socrata open-data portal), no key.

    The dataset is one row per (biitem, tax year, installment) --
    confirmed live -- so this sums "total" across every matching row to
    get the full amount owed across all delinquent years/installments.

    Returns:
        {"biitem": ..., "owner": ..., "address": ..., "amount_owed": ...,
         "years_owed": [...], "delinquent": True/False}
    or {} if not found.
    """
    params = {"biitem": biitem, "$limit": 1000}
    rows = _get_socrata_json(NORFOLK_DELINQUENT_TAX_URL, params)
    if not rows:
        return {}

    years = sorted({r.get("bwtaxyear") for r in rows if r.get("bwtaxyear")})
    try:
        total_owed = sum(_to_float(r.get("total")) for r in rows)
    except _UnparseableAmount as e:
        log.warning("Norfolk tax delinquency: %s (biitem=%s)", e, biitem)
        # A real delinquency record WAS found (rows is non-empty) -- the
        # dollar amount just couldn't be read. Reporting "delinquent:
        # False" here would be a confident, false negative; UNKNOWN is
        # the honest result.
        return {
            "biitem": biitem,
            "owner": rows[0].get("owner_name"),
            "address": rows[0].get("address"),
            "amount_owed": None,
            "years_owed": years,
            "delinquent": None,
            "data_quality_issue": "A delinquency record exists but its dollar amount could not be parsed from the source data -- verify manually.",
        }
    return {
        "biitem": biitem,
        "owner": rows[0].get("owner_name"),
        "address": rows[0].get("address"),
        "amount_owed": round(total_owed, 2),
        "years_owed": years,
        "delinquent": total_owed > 0,
    }


def check_sonoma_county_tax_delinquency(assessment_number: str) -> dict:
    """
    Looks up Sonoma County, CA's official defaulted-property-tax
    dataset by assessment number. Free, official (Socrata open-data
    portal), no key.

    HONEST LIMIT on the amount, corrected after a real live check: the
    dataset has one row per tax year for the same assessment number,
    but "defaultamt" is only populated (nonzero) on the ONE row for the
    tax year the actual default was recorded in -- every other year's
    row for that same parcel shows defaultamt=0, even years AFTER the
    default (confirmed live: a real parcel showed defaultamt=$30,049 on
    its 2022 row, and $0 on its 2023/2024/2025 rows, despite all four
    rows sharing the same default_date and none marked redeemed). That
    $30,049 also looks cumulative relative to that single year's
    ~$5,979 base tax bill -- rolling in multiple already-overdue years,
    penalties, and fees at the moment of default, not just one year.
    So: taking the MOST RECENT year's row (an earlier, wrong version of
    this function did that) would silently report $0 owed on a real
    active default. This takes the MAX defaultamt across all rows for
    the assessment number instead, which correctly finds the one row
    that actually carries the number. Not confirmed against Sonoma's
    own data dictionary -- sanity-check against a real bill before
    trusting this for a purchase decision.

    Returns:
        {"assessment_number": ..., "owner_address": ..., "property_address": ...,
         "amount_owed": ..., "as_of_tax_year": ..., "default_date": ...,
         "in_bankruptcy": True/False, "delinquent": True/False}
    or {} if not found.
    """
    params = {"assessment_number": assessment_number, "$limit": 1000}
    rows = _get_socrata_json(SONOMA_DELINQUENT_TAX_URL, params)
    if not rows:
        return {}

    # The row with the highest defaultamt is the one that actually
    # carries the real default record -- see the HONEST LIMIT above.
    # A field that's PRESENT but unparseable must not silently sort as
    # $0 (that could pick the wrong row as "the" default row) or report
    # a confident "not delinquent" -- surface it as uncertain instead.
    try:
        default_row = max(rows, key=lambda r: _to_float(r.get("defaultamt")))
        amount_owed = _to_float(default_row.get("defaultamt"))
    except _UnparseableAmount as e:
        log.warning("Sonoma County tax delinquency: %s (assessment_number=%s)", e, assessment_number)
        return {
            "assessment_number": assessment_number,
            "amount_owed": None,
            "delinquent": None,
            "data_quality_issue": "A delinquency record exists but its dollar amount could not be parsed from the source data -- verify manually.",
        }
    return {
        "assessment_number": assessment_number,
        "owner_address": " ".join(filter(None, [default_row.get("mailaddress1"), default_row.get("mailaddress2")])),
        "property_address": default_row.get("location_address"),
        "amount_owed": round(amount_owed, 2),
        "as_of_tax_year": default_row.get("taxyear"),
        "default_date": default_row.get("default_date"),
        "in_bankruptcy": (default_row.get("existsbankruptcy") or "").strip().lower() == "yes",
        "delinquent": amount_owed > 0,
    }


def check_richmond_tax_delinquency(property_code: str) -> dict:
    """
    Looks up Richmond, VA's official "Delinquent Real Estate Taxes, Six
    Months or More" dataset by property code. Free, official (Socrata
    open-data portal), no key.

    One row per delinquent bill year -- confirmed live -- so this sums
    "total_due" across every matching row (unlike Sonoma above, these
    values are NOT cumulative, they're separate per-year bills).

    Returns:
        {"property_code": ..., "owner": ..., "amount_owed": ...,
         "years_owed": [...], "delinquent": True/False}
    or {} if not found (which, per the dataset's own name, means either
    not delinquent or delinquent less than six months).
    """
    params = {"property_code": property_code, "$limit": 1000}
    rows = _get_socrata_json(RICHMOND_DELINQUENT_TAX_URL, params)
    if not rows:
        return {}

    years = sorted({r.get("bill_year") for r in rows if r.get("bill_year")})
    try:
        total_owed = sum(_to_float(r.get("total_due")) for r in rows)
    except _UnparseableAmount as e:
        log.warning("Richmond tax delinquency: %s (property_code=%s)", e, property_code)
        return {
            "property_code": property_code,
            "owner": rows[0].get("current_owner_name_1"),
            "amount_owed": None,
            "years_owed": years,
            "delinquent": None,
            "data_quality_issue": "A delinquency record exists but its dollar amount could not be parsed from the source data -- verify manually.",
        }
    return {
        "property_code": property_code,
        "owner": rows[0].get("current_owner_name_1"),
        "amount_owed": round(total_owed, 2),
        "years_owed": years,
        "delinquent": total_owed > 0,
    }


def check_nyc_tax_lien_sale_list(borough: str, block: str, lot: str) -> dict:
    """
    Looks up NYC's official Tax Lien Sale List by BBL (borough-block-lot).
    Free, official (NYC Open Data / Socrata), no key.

    HONEST LIMIT, different in kind from every other tax-delinquency
    check in this file: this dataset does NOT carry a dollar amount at
    all -- confirmed live, the schema has no owed-amount field. It's a
    binary "this property was on an upcoming tax lien sale list as of
    this notice cycle" flag, tied to NYC's periodic public lien-sale
    process, not a running balance. Treat a hit as "this property has
    had serious enough tax/water debt to reach a lien sale list at some
    point," not as a current dollar figure.

    Returns:
        {"bbl": "1-16-3", "on_lien_sale_list": True/False,
         "cycles": ["90 Day Notice", ...], "water_debt_only": True/False}
    """
    params = {"borough": str(borough), "block": str(block), "lot": str(lot), "$limit": 1000}
    rows = _get_socrata_json(NYC_TAX_LIEN_SALE_LIST_URL, params)

    bbl = f"{borough}-{block}-{lot}"
    if not rows:
        return {"bbl": bbl, "on_lien_sale_list": False, "cycles": [], "water_debt_only": False}

    cycles = sorted({r.get("cycle") for r in rows if r.get("cycle")})
    water_only = all((r.get("water_debt_only") or "").strip().upper() == "YES" for r in rows)
    return {
        "bbl": bbl,
        "on_lien_sale_list": True,
        "cycles": cycles,
        "water_debt_only": water_only,
    }


def _get_arcgis_field(attrs: dict, suffix: str):
    """
    Cache County's ArcGIS layer joins two tables, so its real attribute
    keys come back fully-qualified (e.g.
    "InGeoCounty.dbo.gis_parcel_delinquent_taxes.parcel_total_due"),
    confirmed live -- not the short names ArcGIS's own fieldAliases
    metadata implies. Match by suffix instead of hardcoding the full
    qualified name, since that prefix is an internal DB path that could
    change without notice.
    """
    suffix = suffix.lower()
    for key, value in attrs.items():
        if key.lower().endswith("." + suffix) or key.lower() == suffix:
            return value
    return None


def check_cache_county_tax_delinquency(tax_id: str) -> dict:
    """
    Looks up Cache County, UT's official delinquent-tax layer by parcel
    tax ID. Free, official (county ArcGIS REST service, same technology
    already used elsewhere in this file for FEMA/EPA/USFWS layers), no
    key.

    Returns:
        {"tax_id": ..., "owner": ..., "address": ..., "amount_owed": ...,
         "current_year_due": ..., "back_taxes_due": ...,
         "unpaid_year_count": ..., "delinquent": True/False}
    or {} if not found (not in the delinquent layer at all).
    """
    params = {
        "where": f"tax_id = '{tax_id}'",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    resp = requests.get(CACHE_COUNTY_DELINQUENT_TAX_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return {}

    attrs = features[0]["attributes"]
    amount_owed = _get_arcgis_field(attrs, "parcel_total_due") or 0
    return {
        "tax_id": tax_id,
        "owner": _get_arcgis_field(attrs, "owner_name"),
        "address": _get_arcgis_field(attrs, "address_complete"),
        "amount_owed": round(float(amount_owed), 2),
        "current_year_due": _get_arcgis_field(attrs, "curr_tax_year_due"),
        "back_taxes_due": _get_arcgis_field(attrs, "backtax_total_due"),
        "unpaid_year_count": _get_arcgis_field(attrs, "unpaid_yr_count"),
        "delinquent": float(amount_owed) > 0,
    }


# --------------------------------------------------------------------------
# Zoning district lookup (2026-09-12) -- a competitor (Buildability™) has
# zoning/setback/FAR/height data that this toolkit didn't. Researched
# thoroughly: NO free source anywhere -- not one single county, checked
# across multiple independent counties -- publishes setback, FAR, height
# limit, or lot coverage as structured/queryable data. Every free
# government zoning GIS layer gives ONLY the zoning district code/name
# and its boundary polygon; the actual numeric building-envelope rules
# live in the zoning ordinance's legal text (PDF/Municode), not in any
# database. The only source found with the real numbers (setbacks/FAR/
# height/coverage) is Zoneomics (zoneomics.com, also Regrid's own
# zoning data partner at their Premium tier) -- no public self-serve
# pricing, "contact sales" only. So this only closes PART of that
# competitive gap -- zoning district name, not the buildable envelope.
#
# Only 2 places confirmed live to have a real, free, structured zoning-
# district API (not scraping, not a PDF map). A third candidate (an
# ArcGIS service the initial research called "LA County zoning") was
# checked live and turned out to actually be scoped to just the City of
# Arcadia, CA -- a single small city within LA County, not the county
# itself (confirmed via the service's own metadata: layer name "Zoning
# Label" under a service literally named ".../Arcadia/Zoning/...", tiny
# bounding box, real LA County coordinates outside Arcadia returned
# nothing). Dropped rather than shipped as a false "LA County" claim --
# same "verify live before trusting" discipline as everywhere else in
# this file. A real countywide LA County zoning API may still exist;
# it just wasn't found in this pass.
# --------------------------------------------------------------------------

MIAMI_DADE_ZONING_URL = "https://gisweb.miamidade.gov/arcgis/rest/services/LandManagement/MD_Zoning/MapServer/1/query"
KING_COUNTY_ZONING_URL = "https://gismaps.kingcounty.gov/arcgis/rest/services/Planning/KingCo_Zoning/MapServer/1/query"


_ZONING_TIMEOUT_S = 8


def _query_zoning_point(url: str, lat: float, lon: float) -> dict | None:
    params = {
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    # Short timeout, not this file's usual REQUEST_TIMEOUT_S (30s) --
    # fixed 2026-09-12: this used to be called twice IN SEQUENCE with
    # no timeout override at all, so if both government zoning servers
    # were slow, a parcel-detail page could wait up to 60 seconds on
    # this alone (confirmed live: a real request failed at ~61s, right
    # after the buildability report itself had already been fixed and
    # bounded to ~18s -- this separate zoning call was the actual
    # remaining cause). Zoning coverage here is already a narrow bonus
    # (only 2 counties), not worth blocking the page over.
    resp = requests.get(url, params=params, timeout=_ZONING_TIMEOUT_S)
    resp.raise_for_status()
    features = resp.json().get("features", [])
    return features[0]["attributes"] if features else None


def check_zoning_district(lat: float, lon: float) -> dict:
    """
    Looks up the zoning district at (lat, lon) -- ONLY works for 2
    confirmed places (unincorporated Miami-Dade County FL and
    unincorporated King County WA) since no broader free source exists
    (see the section note above). Tries each known layer in turn; a
    point outside a given layer's coverage just returns no features, so
    this is a simple, honest way to cover both without needing a
    separate "which county is this" lookup first. Live-tested: returns
    {} for downtown Miami and Lancaster/Antelope Valley CA (both
    incorporated cities, correctly not covered), and real zoning codes
    for Kendall/Westchester FL and Vashon Island WA (both genuinely
    unincorporated).

    HONEST LIMIT, same as the section note: this returns the zoning
    DISTRICT NAME/CODE only -- never a setback distance, FAR, height
    limit, or lot-coverage percentage. No free source anywhere
    publishes those as structured data; getting real numbers requires
    a paid source (Zoneomics).

    Returns:
        {"source": "Miami-Dade County, FL (unincorporated areas only)",
         "zoning_code": ..., "zoning_description": ..., "municipality": ..., "overlay": ...}
    or {} if BOTH layers were successfully queried and neither covers this point
    or {"lookup_failed": True} if at least one layer's request itself failed
        (network error, timeout, HTTP error) rather than cleanly confirming
        no coverage -- deliberately a DIFFERENT, distinguishable result from
        plain {}. Conflating "we tried and confirmed you're outside our 2
        counties" with "our government source didn't respond" was a real
        bug found during the 2026-09-14 external-data-source audit: a
        customer in Kendall, FL could see "outside our coverage" when the
        true, more useful answer was "try again" or "verify manually."
    """
    # Run both known layers IN PARALLEL rather than one after another --
    # they're independent (a point can only ever be covered by one of
    # them), and this also means a slow/failed Miami-Dade lookup no
    # longer prevents King County from being tried, which the old
    # sequential try/fall-through structure would have done.
    any_call_failed = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        miami_future = pool.submit(_query_zoning_point, MIAMI_DADE_ZONING_URL, lat, lon)
        king_future = pool.submit(_query_zoning_point, KING_COUNTY_ZONING_URL, lat, lon)

        try:
            attrs = miami_future.result()
            log_source_event("zoning_miami_dade", "ok" if attrs else "no_coverage")
        except Exception as e:
            log.warning("Miami-Dade zoning lookup failed or timed out")
            log_source_event("zoning_miami_dade", "source_unavailable", detail=type(e).__name__)
            attrs = None
            any_call_failed = True
        if attrs:
            return {
                "source": "Miami-Dade County, FL (unincorporated areas only)",
                "zoning_code": attrs.get("ZONE"),
                "zoning_description": attrs.get("ZONE_DESC") or attrs.get("SHORT_DESC"),
                "municipality": attrs.get("MUNC"),
                "overlay": attrs.get("OVLY"),
            }

        try:
            attrs = king_future.result()
            log_source_event("zoning_king_county", "ok" if attrs else "no_coverage")
        except Exception as e:
            log.warning("King County zoning lookup failed or timed out")
            log_source_event("zoning_king_county", "source_unavailable", detail=type(e).__name__)
            attrs = None
            any_call_failed = True
        if attrs:
            return {
                "source": "King County, WA (unincorporated areas only)",
                "zoning_code": attrs.get("CURRZONE"),
                "potential_rezone": attrs.get("POTENTIAL"),
                "current_temporary_zone": attrs.get("CURRTEMP"),
            }

    if any_call_failed:
        return {"lookup_failed": True}
    return {}


def get_tax_flags(record: dict) -> dict:
    """
    Pulls tax/lien/foreclosure red flags straight out of a Realie
    record's own data (liens[] and foreclosures[]) — no extra API call
    needed, Realie already includes this as standard fields.

    HONEST LIMIT: this reflects only what Realie's own county-sourced
    data happens to include for this specific parcel — it is NOT a
    guaranteed, complete, nationwide tax-delinquency check (no free or
    paid source gives you that — see the toolkit's research notes on
    this). Treat "flagged: False" as "nothing found in Realie's data
    for this parcel," not as "confirmed clean" — always verify with the
    county before relying on this for a purchase decision.

    Returns:
        {
            "has_liens": True/False,
            "lien_count": ...,
            "has_foreclosure_history": True/False,
            "foreclosure_count": ...,
            "flagged": ...,  # has_liens OR has_foreclosure_history — the "red flag"
        }
    """
    liens = record.get("liens") or []
    foreclosures = record.get("foreclosures") or []
    has_liens = len(liens) > 0
    has_foreclosures = len(foreclosures) > 0
    return {
        "has_liens": has_liens,
        "lien_count": len(liens),
        "has_foreclosure_history": has_foreclosures,
        "foreclosure_count": len(foreclosures),
        "flagged": has_liens or has_foreclosures,
    }


def search_vacant_land_realie(
    state: str,
    county: str | None = None,
    city: str | None = None,
    min_acres: float | None = None,
    max_acres: float | None = None,
    exclude_tax_flagged: bool = False,
    api_key: str | None = None,
    max_pages: int = 25,
) -> list[dict]:
    """
    Searches Realie for genuinely vacant land (no structures — see
    _is_genuinely_vacant) in the given area, optionally narrowed to an
    acreage range. Filtering happens locally after fetching, since
    Realie's search API doesn't support acreage/use-code range filters
    server-side — start with one city or county, not a whole state.

    Every result carries a "tax_flags" section (liens/foreclosures —
    see get_tax_flags) so it's visible at a glance, regardless of the
    filter setting. Set exclude_tax_flagged=True to leave flagged
    parcels out of the results entirely instead of just marking them —
    off by default, since for this business a tax/lien flag is often a
    GOOD sign (a more motivated seller), not something to hide by
    default.

    max_pages caps how many pages (= tokens, on the free tier) this one
    call can spend — defaults to 25 (the full free monthly allowance).
    Pass a smaller number (e.g. 1) when just trying the software out.

    Returns a list of:
        {
            "apn": ..., "lat": ..., "lon": ..., "acres": ...,
            "tax_flags": {...},
            "raw": {...},
        }
    """
    results = []
    for page in fetch_realie_properties(state, county=county, city=city, api_key=api_key, max_pages=max_pages):
        for record in page:
            if not _is_genuinely_vacant(record):
                continue
            acres = _extract_acres(record)
            if min_acres is not None and (acres is None or acres < min_acres):
                continue
            if max_acres is not None and (acres is None or acres > max_acres):
                continue

            tax_flags = get_tax_flags(record)
            if exclude_tax_flagged and tax_flags["flagged"]:
                continue

            lat = record.get("latitude")
            lon = record.get("longitude")
            apn = record.get("parcelId") or record.get("realieParcelId")
            if lat is None or lon is None or not apn:
                continue

            results.append({
                "apn": apn,
                "lat": float(lat),
                "lon": float(lon),
                "acres": acres,
                "tax_flags": tax_flags,
                "raw": record,
            })
    return results


def get_tax_assessment_info_realie(record: dict) -> dict:
    """
    Same purpose as get_tax_assessment_info(), but reads Realie's nested
    field shape instead of Regrid's flat one. Pass a raw record from
    search_vacant_land_realie() (the "raw" key).
    """
    valuation = record.get("valuationInformation", {})
    tax = record.get("taxInformation", {})
    return {
        "assessed_value": valuation.get("totalAssessedValue"),
        "market_value": valuation.get("totalMarketValue"),
        "land_value": valuation.get("totalLandValue"),
        "improvement_value": valuation.get("totalBuildingValue"),
        "taxable_value": valuation.get("taxableValue"),
        "annual_tax_amount": tax.get("taxAmount"),
        "tax_year": tax.get("taxYear"),
    }


def get_owner_mailing_address(record: dict) -> dict:
    """
    Pulls the CURRENT owner's mailing address out of a raw Realie
    record -- reads propertyIdentification.currentOwner (ownerStreet,
    ownerCity, ownerState, ownerZipCode, ownerZipCodePlusFour),
    confirmed live in real Cape Coral, FL data during this project.

    CRITICAL DISTINCTION, the reason this function exists separately
    from anything using propertyLocation: for a genuinely vacant lot,
    the PROPERTY's own site address is often NOT a deliverable mailing
    address at all -- nobody lives on an empty lot, and a lot of rural
    vacant land has no assigned mail-delivery point whatsoever. Direct
    mail (or any other owner outreach) needs to go to the OWNER's own
    mailing address, which is frequently a completely different place
    (this is exactly what makes an absentee owner a wholesaling target
    in the first place) -- confirmed live: a real Cape Coral, FL vacant
    lot's owner mailing address was in Brooklyn, NY, nowhere near the
    property itself. Never use propertyLocation as a mail-to address.

    Returns:
        {"owner_name": ..., "mail_street": ..., "mail_city": ...,
         "mail_state": ..., "mail_zip": ...}
    or {} if no owner mailing address is present on this record.
    """
    ident = record.get("propertyIdentification", {}) or {}
    owner = ident.get("currentOwner", {}) or {}
    street = owner.get("ownerStreet")
    if not street:
        return {}

    zip_code = owner.get("ownerZipCode") or ""
    zip_plus4 = owner.get("ownerZipCodePlusFour")
    if zip_plus4:
        zip_code = f"{zip_code}-{zip_plus4}"

    return {
        "owner_name": owner.get("ownerName"),
        "mail_street": street,
        "mail_city": owner.get("ownerCity"),
        "mail_state": owner.get("ownerState"),
        "mail_zip": zip_code,
    }


# --------------------------------------------------------------------------
# Instant priority signal -- ranks EVERY search result the moment a search
# comes back, using only fields Realie's own search response already
# includes (tax/lien flags, sale history, owner mailing address vs. the
# property's own address). Zero extra network calls, zero extra Realie
# tokens, works instantly across a 500-parcel result set. Deliberately
# separate from, and far cheaper than, the decision engine's evidence-based
# screening (build_decision_summary), which needs a slow per-parcel
# government-data lookup -- "of 500 parcels, which ones deserve a closer
# look" has to be answerable BEFORE spending 10-20 seconds of API calls on
# every single one. This is the FIRST, free ranking pass; the decision
# engine is the SECOND, deeper pass run only on the shortlist this produces.
#
# Every point below is tied to one named, itemized, real land-investing
# heuristic -- never a bare number a customer has to take on faith:
#   - Tax lien / foreclosure flags: a concrete, county-recorded distress
#     signal straight from Realie's own data.
#   - Absentee ownership (owner's mailing address in a different city/
#     state than the property itself): a well-established motivated-
#     seller proxy -- an owner who doesn't live near the parcel is more
#     likely to have forgotten it, inherited it, or want an easy exit.
#   - Long ownership tenure: a parcel held 10-20+ years is more often
#     low-basis, forgotten, or aging into an estate/heir situation --
#     classically more negotiable than a recent purchase.
#   - Individual vs. corporate/entity ownership: an individual owner is
#     typically far easier to reach and negotiate with directly than an
#     LLC, trust, bank, or government entity.
# --------------------------------------------------------------------------

def build_instant_priority(tax_flags: dict, current_owner: dict, owner_mailing: dict,
                            property_city: str | None, property_state: str | None) -> dict:
    """
    Returns:
        {
            "instant_score": int,             # sum of the itemized points below
            "reasons": [str, ...],             # positive signals found, most valuable first
            "negative_reasons": [str, ...],    # why this parcel DIDN'T score higher
        }
    """
    score = 0
    reasons: list[str] = []
    negatives: list[str] = []

    if tax_flags and tax_flags.get("flagged"):
        lien_ct = tax_flags.get("lien_count") or 0
        fc_ct = tax_flags.get("foreclosure_count") or 0
        if lien_ct:
            score += 25
            reasons.append(f"Tax lien on file ({lien_ct}) -- a concrete, county-recorded distress signal.")
        if fc_ct:
            score += 20
            reasons.append(f"Foreclosure history on file ({fc_ct} record(s)).")
    else:
        negatives.append("No tax lien or foreclosure record found for this owner in Realie's data.")

    mail_city = (owner_mailing or {}).get("mail_city")
    mail_state = (owner_mailing or {}).get("mail_state")
    if mail_state and property_state and mail_state.upper() != property_state.upper():
        score += 15
        reasons.append(
            f"Owner's mailing address is out of state ({mail_state}, property is in {property_state}) "
            f"-- a classic absentee-owner signal."
        )
    elif mail_city and property_city and mail_city.upper() != property_city.upper():
        score += 8
        reasons.append(f"Owner's mailing address ({mail_city}) is a different city than the property ({property_city}).")
    elif mail_city and property_city:
        negatives.append("Owner's mailing address matches the property's own city/state -- no absentee-owner signal found.")

    years = (current_owner or {}).get("ownership_years")
    if isinstance(years, (int, float)):
        if years >= 20:
            score += 15
            reasons.append(f"Held {years:.0f}+ years -- long-tenure owners are more often low-basis, forgotten, or aging into an estate situation.")
        elif years >= 10:
            score += 10
            reasons.append(f"Held {years:.0f} years -- a longer-tenure owner is generally more negotiable than a recent buyer.")
        elif years < 2:
            negatives.append(f"Purchased recently ({years:.1f} years ago) -- a recent buyer is typically less motivated to sell.")

    owner_type = (current_owner or {}).get("owner_type")
    if owner_type == "individual":
        score += 10
        reasons.append("Owned by an individual, not a company or entity -- typically easier to reach and negotiate with directly.")
    elif owner_type == "corporate":
        negatives.append("Owned by a company, trust, or other entity -- often slower or harder to reach directly than an individual.")

    if not reasons:
        negatives.append("No distress, absentee-ownership, or long-tenure signals found in the data available at search time.")

    return {"instant_score": score, "reasons": reasons, "negative_reasons": negatives}


def count_new_homes_built_realie(
    state: str,
    months_back: int,
    county: str | None = None,
    city: str | None = None,
    api_key: str | None = None,
) -> dict:
    """
    Realie equivalent of count_new_homes_built(). Same year-only
    precision limitation applies (see that function's docstring) — no
    county assessor tracks a month/day for construction date, only a
    year. Pages through every property in the area and counts ones
    whose actualYearBuilt falls in the computed range — this pulls the
    whole area's data (costs more of your free monthly budget than a
    server-side filter would), so start with one city/county at a time.
    """
    if months_back <= 0:
        raise ValueError("months_back must be positive")

    current_year = date.today().year
    years_back = max(1, -(-months_back // 12))
    start_year = current_year - years_back

    count = 0
    for page in fetch_realie_properties(state, county=county, city=city, api_key=api_key):
        for record in page:
            buildings = record.get("buildingInformation", {}).get("buildings") or []
            for b in buildings:
                year = b.get("actualYearBuilt")
                if year is not None and start_year <= int(year) <= current_year:
                    count += 1
                    break  # count the parcel once, even with multiple buildings

    return {
        "area": {"state": state, "county": county, "city": city},
        "year_range": [start_year, current_year],
        "new_homes_built": count,
    }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def search_vacant_land_parcels(
    min_acres: float,
    max_acres: float,
    target_state: str,
    provider: str | None = None,
    persist: bool = True,
) -> list[dict[str, Any]]:
    """
    Search for vacant land parcels matching the given acreage range and
    state via a licensed property API, dedupe by APN, optionally persist
    them to PostGIS, and return a list of:
        {"apn": ..., "lat": ..., "lon": ...}
    """
    if min_acres < 0 or max_acres <= 0 or min_acres > max_acres:
        raise ValueError("Invalid acreage range: require 0 <= min_acres <= max_acres")
    if not target_state or len(target_state) not in (2, len(target_state)):
        target_state = target_state.strip()
    if not target_state:
        raise ValueError("target_state is required (e.g. 'TX' or 'Texas')")

    provider = (provider or API_PROVIDER).lower()
    if provider not in ("regrid", "attom"):
        raise ValueError(f"Unsupported provider: {provider!r} (expected 'regrid' or 'attom')")

    fetch_pages = fetch_regrid_pages if provider == "regrid" else fetch_attom_pages

    seen_apns: set[str] = set()
    parcels: list[Parcel] = []

    log.info(
        "Searching %s for vacant land in %s (%.2f-%.2f acres)",
        provider, target_state, min_acres, max_acres,
    )

    for page_records in fetch_pages(min_acres, max_acres, target_state):
        for record in page_records:
            parcel = _extract_parcel(record, target_state, provider)
            if parcel is None or parcel.apn in seen_apns:
                continue
            seen_apns.add(parcel.apn)
            parcels.append(parcel)

    log.info("Found %d unique vacant land parcels", len(parcels))

    if persist and parcels:
        conn = get_db_connection()
        try:
            ensure_schema(conn)
            upsert_parcels(conn, parcels)
            log.info("Persisted %d parcels to PostGIS", len(parcels))
        finally:
            conn.close()

    return [{"apn": p.apn, "lat": p.lat, "lon": p.lon} for p in parcels]


# --------------------------------------------------------------------------
# Current owner info
#
# Takes the raw list of deed/sale records for a parcel (as returned by
# your property API's deed-history or sale-history field), finds the
# most recent sale, and reports on that owner only — who owned it before
# doesn't matter here.
# --------------------------------------------------------------------------

# Words in an owner name that mean "this is a company/trust/estate, not
# a person." Checked as whole words against the uppercased name.
_CORPORATE_NAME_KEYWORDS = (
    "LLC", "L.L.C", "INC", "INCORPORATED", "CORP", "CORPORATION", "TRUST",
    "TRUSTEE", "LP", "L.P", "LLP", "PARTNERSHIP", "CO", "COMPANY", "LTD",
    "LIMITED", "ESTATE", "FOUNDATION", "ASSOCIATES", "HOLDINGS", "GROUP",
    "ENTERPRISES", "PROPERTIES", "BANK", "ASSOCIATION", "CHURCH",
    "MINISTRIES", "N.A",
)


def _classify_owner(name: str | None) -> str:
    """Individual person, or a company/trust/estate/etc.?"""
    if not name:
        return "unknown"
    upper = f" {name.upper()} "
    for kw in _CORPORATE_NAME_KEYWORDS:
        if f" {kw} " in upper or f" {kw}." in upper or f" {kw}," in upper:
            return "corporate"
    return "individual"


def _extract_deed_fields(record: dict) -> tuple[str | None, float | None, date | None]:
    """
    Pull the owner name, price paid, and sale date out of one raw deed
    record. Field names vary between providers (and even between
    endpoints of the same provider), so this checks several common
    locations. Adjust here first if your provider's records don't match.
    """
    owner_block = record.get("owner") or record.get("buyer") or {}
    owner_name = (
        record.get("ownerName")
        or record.get("owner_name")
        or record.get("grantee")
        or owner_block.get("fullName")
        or owner_block.get("name")
        or owner_block.get("owner1")
    )
    if isinstance(owner_name, dict):
        owner_name = owner_name.get("fullName") or owner_name.get("name")

    amount_block = record.get("amount") or record.get("sale") or {}
    price_paid = (
        record.get("salePrice")
        or record.get("sale_price")
        or record.get("saleAmt")
        or amount_block.get("saleAmt")
        or amount_block.get("amount")
    )
    try:
        price_paid = float(price_paid) if price_paid is not None else None
    except (TypeError, ValueError):
        price_paid = None

    sale_date_raw = (
        record.get("saleDate")
        or record.get("sale_date")
        or record.get("saleTransDate")
        or record.get("recordingDate")
        or amount_block.get("saleTransDate")
    )
    sale_date = _parse_date(sale_date_raw)

    return owner_name, price_paid, sale_date


def get_current_owner_info(deed_records: list[dict], as_of: date | None = None) -> dict:
    """
    Given a parcel's raw deed/sale records (in any order), find the most
    recent sale and report on that owner only:
      - owner_name:       the current owner's legal name
      - owner_type:       "individual", "corporate", or "unknown"
      - price_paid:       what they paid at that most recent sale
      - ownership_years:  exact years from that sale date to today
                           (or to `as_of`, if given)

    Returns {} if none of the records have a usable sale date.
    """
    as_of = as_of or date.today()

    latest_owner_name = None
    latest_price_paid = None
    latest_sale_date = None

    for record in deed_records:
        owner_name, price_paid, sale_date = _extract_deed_fields(record)
        if sale_date is None:
            continue
        if latest_sale_date is None or sale_date > latest_sale_date:
            latest_sale_date = sale_date
            latest_owner_name = owner_name
            latest_price_paid = price_paid

    if latest_sale_date is None:
        return {}

    return {
        "owner_name": latest_owner_name,
        "owner_type": _classify_owner(latest_owner_name),
        "price_paid": latest_price_paid,
        "ownership_years": round((as_of - latest_sale_date).days / 365.25, 2),
    }


# --------------------------------------------------------------------------
# Buildability score: FEMA flood zones + USFWS wetlands
#
# Both FEMA's National Flood Hazard Layer and the USFWS National
# Wetlands Inventory are free public government GIS services — no
# account or API key needed.
# --------------------------------------------------------------------------

def _project_to_local_equal_area(geom, lat0: float, lon0: float):
    """
    Reproject a shapely geometry from WGS84 lon/lat (degrees) into a
    local Lambert Azimuthal Equal-Area projection centered on the
    parcel, in meters. Area math on raw degrees is meaningless — this
    makes the acreage/percentage numbers actually correct.
    """
    from pyproj import Transformer

    proj = f"+proj=laea +lat_0={lat0} +lon_0={lon0} +units=m +datum=WGS84"
    transformer = Transformer.from_crs("EPSG:4326", proj, always_xy=True)
    return shp_transform(transformer.transform, geom)


def _fetch_esri_intersecting_features(base_url: str, polygon_wgs84: Polygon, where: str = "1=1") -> list:
    """
    Query a standard Esri ArcGIS REST 'query' endpoint (FEMA NFHL and
    the USFWS Wetlands Mapper both expose this interface) for features
    that intersect the given parcel polygon. Returns shapely geometries
    in WGS84 lon/lat. `where` lets the caller filter which features come
    back server-side (see the FEMA SFHA_TF note below).
    """
    rings = [list(polygon_wgs84.exterior.coords)]
    geometry = {"rings": rings, "spatialReference": {"wkid": 4326}}

    params = {
        "f": "geojson",
        "where": where,
        "geometry": json.dumps(geometry),
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outSR": 4326,
        "outFields": "*",
        "returnGeometry": "true",
    }
    # These government GIS servers occasionally drop the connection
    # mid-handshake — harmless, but worth a couple of quiet retries
    # rather than failing the whole buildability check over a blip.
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(base_url, params=params, timeout=REQUEST_TIMEOUT_S)
            resp.raise_for_status()
            payload = resp.json()
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            log.warning("Retrying %s after connection issue (attempt %d/3): %s", base_url, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    else:
        raise last_error

    geoms = []
    for feature in payload.get("features", []):
        try:
            geoms.append(shape(feature["geometry"]))
        except Exception:
            log.warning("Could not parse a feature geometry from %s", base_url)
    return geoms


def calculate_buildability_score(polygon_coords: list[tuple[float, float]]) -> dict:
    """
    Given a vacant lot's boundary as a list of (longitude, latitude)
    points — in that order; that's the standard mapping convention, the
    reverse of how people usually say coordinates out loud — checks it
    against FEMA flood-zone maps and USFWS wetland maps and returns what
    percentage of the lot is clear to build on:

        {
            "total_area_acres": ...,
            "flood_or_wetland_area_acres": ...,
            "buildable_area_acres": ...,
            "buildability_score": ...,   # 0-100, % of the lot that's clear
        }
    """
    if len(polygon_coords) < 3:
        raise ValueError("polygon_coords needs at least 3 (longitude, latitude) points")

    parcel = Polygon(polygon_coords)
    if not parcel.is_valid:
        parcel = parcel.buffer(0)  # fixes common self-intersecting input

    lon0, lat0 = parcel.centroid.x, parcel.centroid.y

    # FEMA's flood layer maps the ENTIRE area into zones, including
    # "Zone X" (SFHA_TF = 'F'), which means *minimal/no* flood hazard.
    # Only SFHA_TF = 'T' zones are actual Special Flood Hazard Areas —
    # filtering here (server-side) is what makes this accurate instead
    # of treating every parcel as 100% flood zone.
    flood_geoms = _fetch_esri_intersecting_features(FEMA_NFHL_URL, parcel, where="SFHA_TF = 'T'")
    wetland_geoms = _fetch_esri_intersecting_features(USFWS_WETLANDS_URL, parcel)

    parcel_m = _project_to_local_equal_area(parcel, lat0, lon0)
    total_area_m2 = parcel_m.area
    if total_area_m2 == 0:
        raise ValueError("Parcel polygon has zero area — check the input coordinates")

    exclusion_geoms = flood_geoms + wetland_geoms
    if exclusion_geoms:
        exclusion_geoms_m = [_project_to_local_equal_area(g, lat0, lon0) for g in exclusion_geoms]
        # Union first so overlapping flood + wetland zones aren't double
        # counted, then clip to the parcel boundary (these layers often
        # extend well beyond the lot itself).
        excluded_area_m2 = unary_union(exclusion_geoms_m).intersection(parcel_m).area
    else:
        excluded_area_m2 = 0.0

    buildable_area_m2 = max(total_area_m2 - excluded_area_m2, 0.0)
    buildability_score = round((buildable_area_m2 / total_area_m2) * 100, 2)

    sqm_to_acres = 0.000247105
    return {
        "total_area_acres": round(total_area_m2 * sqm_to_acres, 4),
        "flood_or_wetland_area_acres": round(excluded_area_m2 * sqm_to_acres, 4),
        "buildable_area_acres": round(buildable_area_m2 * sqm_to_acres, 4),
        "buildability_score": buildability_score,
    }


# --------------------------------------------------------------------------
# Septic / soil suitability
#
# Most rural vacant land has no city sewer — it needs a septic system,
# and a lot of land that looks buildable on paper fails here because of
# the soil itself (too much clay, high water table, etc.). This checks
# the official USDA soil survey (SSURGO), free and no key needed.
# --------------------------------------------------------------------------

def get_septic_suitability(lat: float, lon: float) -> dict:
    """
    Looks up the dominant soil type at (lat, lon) in USDA's official
    soil survey and returns its rating for supporting a standard septic
    tank absorption field:

        {
            "rating": "Not limited" | "Somewhat limited" | "Very limited" | "Not rated",
            "soil_name": ...,        # the dominant soil type's name
            "coverage_pct": ...,     # how much of this spot that soil type covers
        }

    "Not rated" shows up for urban/paved/open-water areas — that's a
    real, valid answer from the government data, not an error. Returns
    {} only if no soil survey data exists at all for this point.
    """
    query = f"""
        SELECT co.mukey, co.cokey, co.compname, co.comppct_r, ci.interphrc AS rating
        FROM component co
        INNER JOIN cointerp ci ON co.cokey = ci.cokey
        WHERE co.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('point({lon} {lat})')
        )
        AND ci.mrulename = 'ENG - Septic Tank Absorption Fields'
        AND ci.ruledepth = 0
        ORDER BY co.comppct_r DESC
    """
    resp = requests.post(
        SOIL_DATA_ACCESS_URL,
        json={"query": query, "format": "JSON+COLUMNNAME"},
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        log.warning("Soil Data Access returned a non-JSON response (likely a malformed-query error)")
        return {}

    table = payload.get("Table")
    if not table or len(table) < 2:
        return {}

    columns = table[0]
    dominant_row = dict(zip(columns, table[1]))  # highest comppct_r, already sorted

    coverage_pct = dominant_row.get("comppct_r")
    try:
        coverage_pct = int(coverage_pct) if coverage_pct is not None else None
    except (TypeError, ValueError):
        pass

    return {
        "rating": dominant_row.get("rating"),
        "soil_name": dominant_row.get("compname"),
        "coverage_pct": coverage_pct,
    }


# --------------------------------------------------------------------------
# Natural hazard risk (FEMA National Risk Index)
#
# One free federal dataset scoring nearly every major U.S. natural
# hazard for the area a parcel sits in.
# --------------------------------------------------------------------------

# FEMA's hazard type codes -> plain-English names.
_NRI_HAZARD_CODES = {
    "AVLN": "Avalanche",
    "CFLD": "Coastal Flooding",
    "CWAV": "Cold Wave",
    "DRGT": "Drought",
    "ERQK": "Earthquake",
    "HAIL": "Hail",
    "HWAV": "Heat Wave",
    "HRCN": "Hurricane",
    "ISTM": "Ice Storm",
    "LNDS": "Landslide",
    "LTNG": "Lightning",
    "IFLD": "Riverine Flooding",
    "SWND": "Strong Wind",
    "TRND": "Tornado",
    "TSUN": "Tsunami",
    "VLCN": "Volcanic Activity",
    "WFIR": "Wildfire",
    "WNTW": "Winter Weather",
}


def get_natural_hazard_risk(lat: float, lon: float) -> dict:
    """
    Looks up FEMA's National Risk Index rating for the area around
    (lat, lon) — covers flooding (coastal + riverine), hurricane,
    tornado, wildfire, earthquake, drought, heat wave, cold wave,
    winter weather, hail, strong wind, lightning, landslide, tsunami,
    volcanic activity, and avalanche, all in one place.

    This is neighborhood-level (census tract) data, not exact-parcel
    level — think of it as "what's this area generally like," not
    "does this specific 2-acre lot flood." Use it alongside the
    parcel-exact flood/wetland check, not instead of it.

    Returns:
        {
            "overall_risk_rating": "Relatively Moderate",
            "hazards": {"Hurricane": "Very High", "Wildfire": "Relatively Low", ...},
        }
    or {} if no tract data covers this point (e.g. outside the U.S.).
    """
    out_fields = ["RISK_RATNG"] + [f"{code}_RISKR" for code in _NRI_HAZARD_CODES]
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(out_fields),
        "returnGeometry": "false",
        "f": "json",
    }
    resp = requests.get(NRI_CENSUS_TRACT_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    features = resp.json().get("features") or []
    if not features:
        return {}

    attrs = features[0]["attributes"]
    hazards = {name: attrs.get(f"{code}_RISKR") for code, name in _NRI_HAZARD_CODES.items()}
    return {
        "overall_risk_rating": attrs.get("RISK_RATNG"),
        "hazards": hazards,
    }


# --------------------------------------------------------------------------
# Road access ("is it landlocked?")
#
# Land with no legal road access is a classic beginner trap and can be
# nearly worthless. This is a screening check, not a legal guarantee —
# see the caveats in the docstring below.
# --------------------------------------------------------------------------

def check_road_access(polygon_coords: list[tuple[float, float]], buffer_meters: float = 10.0) -> dict:
    """
    Checks whether a vacant lot's boundary actually touches a mapped
    public road, using the U.S. Census Bureau's free TIGER/Line road
    data (no key needed).

    IMPORTANT — this is a screening tool, not a legal determination:
      - Census road data is self-reported by local governments and has
        real, known gaps — some legitimate private/rural roads are
        missing entirely (which could wrongly suggest "landlocked"),
        and a few hiking trails are mis-tagged as roads (which could
        wrongly suggest access exists).
      - This checks whether a road's mapped centerline geometrically
        touches the parcel — it does NOT confirm a legal recorded
        easement or right-of-way. Always confirm real access with a
        title company before relying on this for a purchase decision.

    Returns:
        {
            "has_mapped_road_access": True/False,
            "nearby_roads": ["N Lamar Blvd", "Congress Ave", ...],
        }
    """
    parcel = Polygon(polygon_coords)
    if not parcel.is_valid:
        parcel = parcel.buffer(0)

    lon0, lat0 = parcel.centroid.x, parcel.centroid.y
    parcel_m = _project_to_local_equal_area(parcel, lat0, lon0)

    # Query a small bounding box around the parcel (roughly +/-50m) —
    # cheap, generous margin; the real touch/distance check happens
    # afterward in projected meters, not on this box.
    minx, miny, maxx, maxy = parcel.bounds
    margin_deg = 0.0005
    params = {
        "geometry": f"{minx - margin_deg},{miny - margin_deg},{maxx + margin_deg},{maxy + margin_deg}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "NAME,MTFCC",
        "returnGeometry": "true",
        "f": "geojson",
    }
    resp = requests.get(ROADS_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    features = resp.json().get("features", [])

    touching_roads = set()
    for feature in features:
        props = feature.get("properties", {})
        if props.get("MTFCC") not in _QUALIFYING_ROAD_MTFCC:
            continue
        try:
            road_geom_m = _project_to_local_equal_area(shape(feature["geometry"]), lat0, lon0)
        except Exception:
            continue
        if road_geom_m.distance(parcel_m) <= buffer_meters and props.get("NAME"):
            touching_roads.add(props["NAME"])

    return {
        "has_mapped_road_access": len(touching_roads) > 0,
        "nearby_roads": sorted(touching_roads),
    }


# --------------------------------------------------------------------------
# Federal environmental designations: coastal barrier zones + protected
# species habitat
#
# Two more free federal checks that can heavily restrict, or effectively
# kill, development — especially near the coast.
# --------------------------------------------------------------------------

def _fetch_esri_raw_features(base_url: str, polygon_wgs84: Polygon, where: str = "1=1") -> list[dict]:
    """
    Same idea as _fetch_esri_intersecting_features, but keeps each
    feature's attributes (not just its geometry) — for checks where
    WHAT is nearby matters (which species, which CBRS unit), not just
    how much area it covers.
    """
    rings = [list(polygon_wgs84.exterior.coords)]
    geometry = {"rings": rings, "spatialReference": {"wkid": 4326}}
    params = {
        "f": "geojson",
        "where": where,
        "geometry": json.dumps(geometry),
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outSR": 4326,
        "outFields": "*",
        "returnGeometry": "true",
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(base_url, params=params, timeout=REQUEST_TIMEOUT_S)
            resp.raise_for_status()
            return resp.json().get("features", [])
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            log.warning("Retrying %s after connection issue (attempt %d/3): %s", base_url, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    raise last_error


def check_environmental_designations(polygon_coords: list[tuple[float, float]]) -> dict:
    """
    Checks a lot's boundary against two federal designations that can
    heavily restrict, or effectively kill, development — especially
    near the coast:
      - Coastal Barrier Resources System (CBRS): federally designated
        zones where federal flood insurance and federal infrastructure
        funding are NOT available — a major red flag for building.
      - Critical Habitat: areas protected for a threatened/endangered
        species, which can restrict what's allowed to be built.

    Returns:
        {
            "in_coastal_barrier_resources_system": True/False,
            "cbrs_units": [{"unit": "P20", "name": "Cayo Costa", "designation": "System Unit"}, ...],
            "in_critical_habitat": True/False,
            "critical_habitat_species": ["Piping Plover", ...],
        }
    """
    parcel = Polygon(polygon_coords)
    if not parcel.is_valid:
        parcel = parcel.buffer(0)

    cbrs_features = _fetch_esri_raw_features(CBRS_URL, parcel)
    habitat_features = _fetch_esri_raw_features(CRITICAL_HABITAT_URL, parcel)

    cbrs_units = [
        {
            "unit": f.get("properties", {}).get("Unit"),
            "name": f.get("properties", {}).get("Name"),
            "designation": f.get("properties", {}).get("Unit_Type"),
        }
        for f in cbrs_features
    ]
    species = sorted({
        f.get("properties", {}).get("comname")
        for f in habitat_features
        if f.get("properties", {}).get("comname")
    })

    return {
        "in_coastal_barrier_resources_system": len(cbrs_units) > 0,
        "cbrs_units": cbrs_units,
        "in_critical_habitat": len(species) > 0,
        "critical_habitat_species": species,
    }


# --------------------------------------------------------------------------
# Nearby development (a PROXY for "are utilities likely hooked up
# already" — not a real answer)
#
# There is no free — or paid — nationwide database that can say "is
# electric/water hookup available at this exact spot right now." That's
# decided independently by thousands of local utility companies and
# water districts, and almost none of them publish it as data. The only
# honest way to actually know is to contact the specific local utility.
# This is a best-effort SIGNAL, not a confirmation: if houses already
# stand nearby, utilities are very likely already run to that area.
# --------------------------------------------------------------------------

def check_nearby_development(lat: float, lon: float, radius_meters: float = 400.0) -> dict:
    """
    Counts existing buildings within `radius_meters` of (lat, lon) using
    free public map data (OpenStreetMap). This is a PROXY, not a
    confirmed utility answer — see the section note above. A near-zero
    count is a real warning sign; a high count is a reasonable "probably
    yes," not a guarantee. Always confirm hookup availability directly
    with the local utility company before relying on this to buy land.

    Returns:
        {
            "nearby_building_count": 151,
            "likely_utilities_nearby": True,  # nearby_building_count >= 3
        }
    """
    query = (
        "[out:json][timeout:25];"
        f'(way["building"](around:{radius_meters},{lat},{lon});'
        f'node["building"](around:{radius_meters},{lat},{lon});'
        ");"
        "out count;"
    )
    # Overpass's shared public server rejects requests with no real
    # User-Agent (406) and is prone to transient hiccups under load —
    # worth a few retries rather than failing the whole report.
    headers = {"User-Agent": "vacant-land-toolkit/1.0 (buildability research tool)"}

    resp = None
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, headers=headers, timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 200:
                break
            last_error = RuntimeError(f"Overpass API returned HTTP {resp.status_code}")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
        log.warning("Retrying Overpass API (attempt %d/4): %s", attempt + 1, last_error)
        time.sleep(3 * (attempt + 1))
    else:
        raise last_error

    total = int(resp.json()["elements"][0]["tags"]["total"])
    return {
        "nearby_building_count": total,
        "likely_utilities_nearby": total >= 3,
    }


# --------------------------------------------------------------------------
# Slope / steepness
# --------------------------------------------------------------------------

def get_slope_estimate(lat: float, lon: float, grid_spacing_m: float = 30.0) -> dict:
    """
    Estimates ground slope at (lat, lon). USGS's free elevation service
    only returns a single point's elevation, not slope directly, so
    this samples 5 nearby points (~30m apart) and computes the rise
    over that span. Steep land can be unbuildable or much more
    expensive to build on (excavation, retaining walls, septic
    complications).

    Returns:
        {
            "relief_meters": ...,          # elevation range across the sample
            "approx_slope_percent": ...,   # rough estimate, not survey-grade
            "steep": ...,                  # True if approx_slope_percent >= 15
        }
    or {} if the elevation service didn't return usable data.
    """
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))
    dlat = grid_spacing_m / meters_per_deg_lat
    dlon = grid_spacing_m / meters_per_deg_lon if meters_per_deg_lon else 0

    sample_points = [
        (lat, lon),
        (lat + dlat, lon), (lat - dlat, lon),
        (lat, lon + dlon), (lat, lon - dlon),
    ]

    # Fetch all 5 sample points IN PARALLEL, not one at a time, AND with
    # a much shorter timeout than this file's usual REQUEST_TIMEOUT_S
    # (30s). Fixed 2026-09-12: a real user hit a 110-second parcel-
    # detail page load, traced to this function alone taking 54-ish
    # seconds. Parallelizing the 5 requests helped some but NOT fully --
    # live-measured, USGS's elevation service (EPQS) was itself just
    # slow that day (each individual request taking ~30s), so firing 5
    # requests at once doesn't help when the remote server is the
    # bottleneck, not client-side sequential waiting. Slope is one
    # nice-to-have section out of 10 in the full report (explicitly
    # documented elsewhere as "a rough estimate, not survey-grade") --
    # not worth holding up the whole page for. Give up fast and return
    # {} (an honest, already-documented "no data" result) rather than
    # let one slow government server block everything else.
    _SLOPE_TIMEOUT_S = 8

    def _fetch_one(point):
        plat, plon = point
        params = {"x": plon, "y": plat, "units": "Meters", "wkid": 4326, "includeDate": "false"}
        resp = requests.get(USGS_EPQS_URL, params=params, timeout=_SLOPE_TIMEOUT_S)
        resp.raise_for_status()
        return float(resp.json().get("value"))

    elevations = []
    with ThreadPoolExecutor(max_workers=len(sample_points)) as pool:
        futures = [pool.submit(_fetch_one, p) for p in sample_points]
        for future in futures:
            try:
                elevations.append(future.result(timeout=_SLOPE_TIMEOUT_S + 2))
            except Exception:
                continue

    if len(elevations) < 2:
        return {}

    relief = max(elevations) - min(elevations)
    slope_percent = round((relief / (grid_spacing_m * 2)) * 100, 1)

    return {
        "relief_meters": round(relief, 1),
        "approx_slope_percent": slope_percent,
        "steep": slope_percent >= 15,
    }


# --------------------------------------------------------------------------
# Contamination sites (EPA Superfund + Brownfields)
# --------------------------------------------------------------------------

def _extract_site_name(attrs: dict) -> str:
    # Case-insensitive lookup — different EPA services use different
    # capitalization for what's otherwise the same kind of field
    # (confirmed live: Superfund uses "SITE_NAME", Brownfields/ACRES
    # uses "primary_name").
    lower_attrs = {k.lower(): v for k, v in attrs.items()}
    for key in ("site_name", "primary_name", "name", "fac_name"):
        if lower_attrs.get(key):
            return lower_attrs[key]
    return "Unnamed site"


def check_contamination_sites(lat: float, lon: float, radius_km: float = 2.0) -> dict:
    """
    Checks EPA's Superfund (NPL — the ~1,300 most serious contaminated
    sites nationwide) and Brownfields/ACRES (a much larger set of
    assessed sites) databases for anything within radius_km of
    (lat, lon). Both free, official EPA data, no key needed.

    The two sources are queried and error-handled INDEPENDENTLY —
    Superfund and Brownfields are two unrelated EPA services, and if one
    of them is briefly down there is no reason to also discard the
    other's already-successfully-fetched, real answer. Each list below
    is `None` (source unavailable, unknown) if THAT source's query
    failed, or a real list (possibly empty, meaning confirmed none
    found) if it succeeded — never conflate the two.

    Returns:
        {
            "superfund_sites_nearby": ["LOVE CANAL", ...] or None,
            "brownfield_sites_nearby": [...] or None,
        }
    """
    def _query(url):
        params = {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "distance": radius_km,
            "units": "esriSRUnit_Kilometer",
            "outFields": "*",
            "f": "json",
        }
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json().get("features", [])

    try:
        superfund_sites = [_extract_site_name(f.get("attributes", {})) for f in _query(EPA_SUPERFUND_URL)]
    except Exception:
        log.warning("EPA Superfund lookup failed for (%s, %s)", lat, lon)
        superfund_sites = None

    try:
        brownfield_sites = [_extract_site_name(f.get("attributes", {})) for f in _query(EPA_BROWNFIELDS_URL)]
    except Exception:
        log.warning("EPA Brownfields lookup failed for (%s, %s)", lat, lon)
        brownfield_sites = None

    return {
        "superfund_sites_nearby": superfund_sites,
        "brownfield_sites_nearby": brownfield_sites,
    }


# --------------------------------------------------------------------------
# Finer-grained wildfire risk (US Forest Service)
# --------------------------------------------------------------------------

def check_fine_grained_wildfire_risk(lat: float, lon: float) -> dict:
    """
    Wildfire risk at 30-meter pixel resolution from the US Forest
    Service — much finer than FEMA's National Risk Index (which rates
    a whole census tract at once). Free, no key.

    Returns a raw continuous hazard index plus a rough tier. IMPORTANT:
    the "tier" cutoffs below are MY OWN rough approximation from a
    handful of sample points, not an official USFS classification —
    this live endpoint doesn't expose USFS's official category
    breakpoints. Treat "raw_value" as the more trustworthy relative
    signal (higher = more hazardous) and "tier" as a rough guide only.

    Returns {} / "No data" for water, urban, or other non-burnable areas.
    """
    params = {
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "returnGeometry": "false",
        "f": "json",
    }
    resp = requests.get(USFS_WILDFIRE_HAZARD_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    raw_value = resp.json().get("value")

    if raw_value in (None, "NoData"):
        return {"raw_value": None, "tier": "No data (water/urban/non-burnable)"}

    try:
        raw_value = float(raw_value)
    except (TypeError, ValueError):
        return {"raw_value": None, "tier": "Unknown"}

    # Rough approximation only — see docstring warning above.
    if raw_value < 1000:
        tier = "Low (rough estimate)"
    elif raw_value < 5000:
        tier = "Moderate (rough estimate)"
    else:
        tier = "High (rough estimate)"

    return {"raw_value": raw_value, "tier": tier}


# --------------------------------------------------------------------------
# Broadband availability (FCC)
# --------------------------------------------------------------------------

def check_broadband_availability(lat: float, lon: float) -> dict:
    """
    Checks whether broadband internet is actually available at (lat,
    lon), using official FCC data (mirrored via Esri Living Atlas,
    since fcc.gov's own API requires an account/token and its site
    blocks automated requests). Counts "serviceable locations" in the
    surrounding census block by service status and technology.

    Returns:
        {
            "total_locations": ...,
            "unserved_locations": ...,
            "underserved_locations": ...,
            "served_locations": ...,
            "served_via_fiber": ...,
            "served_via_cable": ...,
        }
    or {} if this point isn't covered by the mirrored dataset.
    """
    params = {
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "GEOID,CountyName,StateAbbr,TotalBSLs,UnservedBSLs,UnderservedBSLs,ServedBSLs,ServedBSLsFiber,ServedBSLsCable",
        "returnGeometry": "false",
        "f": "json",
    }
    resp = requests.get(FCC_BROADBAND_MIRROR_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return {}

    attrs = features[0].get("attributes", {})
    return {
        "total_locations": attrs.get("TotalBSLs"),
        "unserved_locations": attrs.get("UnservedBSLs"),
        "underserved_locations": attrs.get("UnderservedBSLs"),
        "served_locations": attrs.get("ServedBSLs"),
        "served_via_fiber": attrs.get("ServedBSLsFiber"),
        "served_via_cable": attrs.get("ServedBSLsCable"),
    }


# --------------------------------------------------------------------------
# The full picture: one combined buildability report
# --------------------------------------------------------------------------

def get_comprehensive_buildability_report(polygon_coords: list[tuple[float, float]]) -> dict:
    """
    Runs every automated buildability check this toolkit has and
    combines them into one report for a single vacant lot:
      - flood zones + wetlands, exact to the parcel boundary
      - septic/soil suitability
      - FEMA's National Risk Index (hurricane, wildfire, earthquake,
        drought, heat wave, cold wave, tornado, hail, strong wind,
        lightning, landslide, tsunami, volcanic activity, avalanche,
        winter weather — neighborhood-level, not parcel-exact)
      - road access (is it landlocked?)
      - coastal barrier zones + protected species habitat
      - nearby development, as a rough proxy for whether utilities are
        likely already run to the area (NOT a confirmed hookup answer —
        see check_nearby_development's docstring)

    This covers every PHYSICAL/ENVIRONMENTAL buildability question that
    can be answered for free with public data. It does NOT cover, and
    can't fully automate:
      - Zoning, minimum lot size, or setback rules — set by each county
        individually, not published as one free nationwide dataset
      - A confirmed, guaranteed utility hookup answer — that only comes
        from the local utility company itself; nearby_development is a
        best-effort signal, not a substitute for that
      - Legal/title issues — easements, HOA covenants, mineral rights —
        always get a real title search before buying, no software
        (including this one) replaces that

    If any individual check fails (a government server is down, etc.)
    that section is set to None rather than crashing the whole report,
    so one bad connection doesn't lose everything else.

    PERFORMANCE NOTE (fixed 2026-09-12): this runs all 10 checks below
    IN PARALLEL (a thread pool), not one after another. A real user
    reported this page taking 110 SECONDS to load -- confirmed live --
    because the original version called 10 independent, unrelated
    government/OSM services strictly in sequence, and a couple of them
    (Overpass, the ESRI-backed checks) have their own multi-attempt
    retry loops with real sleep delays built in for handling transient
    server hiccups (see check_nearby_development and
    _fetch_esri_intersecting_features) -- when several checks each hit
    their retry ceiling, those delays stack up into minutes. None of
    these 10 checks depend on each other's results, so there's no
    reason to wait for one before starting the next. Running them
    concurrently means total wait time is roughly the SLOWEST single
    check, not the sum of all 10.

    Returns one dict with a section per check, plus a "concerns" list
    in plain English summarizing anything that stood out.
    """
    parcel = Polygon(polygon_coords)
    if not parcel.is_valid:
        parcel = parcel.buffer(0)
    lon0, lat0 = parcel.centroid.x, parcel.centroid.y

    def _flood_and_wetland():
        return calculate_buildability_score(polygon_coords)

    def _road_access():
        return check_road_access(polygon_coords)

    def _environmental_designations():
        return check_environmental_designations(polygon_coords)

    checks = {
        "flood_and_wetland": _flood_and_wetland,
        "septic_suitability": lambda: get_septic_suitability(lat0, lon0),
        "natural_hazard_risk": lambda: get_natural_hazard_risk(lat0, lon0),
        "road_access": _road_access,
        "environmental_designations": _environmental_designations,
        "nearby_development": lambda: check_nearby_development(lat0, lon0),
        "slope": lambda: get_slope_estimate(lat0, lon0),
        "contamination": lambda: check_contamination_sites(lat0, lon0),
        "wildfire_fine_grained": lambda: check_fine_grained_wildfire_risk(lat0, lon0),
        "broadband": lambda: check_broadband_availability(lat0, lon0),
    }

    # ONE OVERALL DEADLINE for the whole batch, not a per-check timeout
    # (fixed AGAIN 2026-09-12, same day -- the first version of this
    # fix had a real bug: calling future.result(timeout=15) inside a
    # for-loop, once per check, does NOT bound the total wait to 15s --
    # each iteration gets its OWN fresh 15s allowance regardless of how
    # long earlier iterations in the loop already took. Confirmed live:
    # a real request took 30.96s and got a 500 -- two checks each
    # genuinely taking ~15s in turn added up past gunicorn's default
    # 30-second worker timeout, which KILLED THE WORKER MID-REQUEST,
    # instantly wiping every visitor's cached search results with zero
    # idle time needed -- this is what was actually causing the
    # "Parcel not found" errors, not the free-tier idle spin-down.
    # concurrent.futures.wait(..., timeout=X) below waits for ALL
    # futures against ONE shared deadline, so total time is properly
    # capped at _TOTAL_DEADLINE_S regardless of how many checks there
    # are or how they're distributed. Deliberately NOT using
    # `with ThreadPoolExecutor(...) as pool:` -- that form blocks on
    # exit until every submitted task finishes, which would silently
    # defeat this deadline (a slow check would still hold up the whole
    # function even after we stop waiting on it). Python threads can't
    # be force-killed, so an abandoned slow call just keeps running
    # harmlessly in the background until it finishes on its own; it
    # simply won't be part of THIS report.
    _TOTAL_DEADLINE_S = 18

    report: dict = {}
    pool = ThreadPoolExecutor(max_workers=len(checks))
    try:
        futures = {key: pool.submit(fn) for key, fn in checks.items()}
        done, not_done = wait(futures.values(), timeout=_TOTAL_DEADLINE_S)
        for key, future in futures.items():
            if future in not_done:
                log.warning("%s check did not finish within %ss", key, _TOTAL_DEADLINE_S)
                log_source_event(key, "source_unavailable", detail=f"timeout>{_TOTAL_DEADLINE_S}s")
                report[key] = None
                continue
            try:
                report[key] = future.result()
                result_status = "ok" if report[key] else "no_coverage"
                log_source_event(key, result_status)
            except Exception as e:
                log.exception("%s check failed", key)
                log_source_event(key, "error", detail=type(e).__name__)
                report[key] = None
    finally:
        pool.shutdown(wait=False)

    concerns: list[str] = []

    if report["flood_and_wetland"] is not None:
        score = report["flood_and_wetland"]["buildability_score"]
        if score < 100:
            concerns.append(f"{100 - score:.1f}% of the lot is in a mapped flood zone or wetland.")

    if report["septic_suitability"] is not None:
        rating = report["septic_suitability"].get("rating")
        if rating in ("Somewhat limited", "Very limited"):
            concerns.append(f"Soil septic suitability: {rating}.")

    if report["natural_hazard_risk"] is not None:
        for hazard, rating in report["natural_hazard_risk"].get("hazards", {}).items():
            if rating in ("Relatively High", "Very High"):
                concerns.append(f"{hazard} risk rated '{rating}' for this area.")

    if report["road_access"] is not None:
        if not report["road_access"]["has_mapped_road_access"]:
            concerns.append("No mapped public road was found touching this parcel — may be landlocked.")

    if report["environmental_designations"] is not None:
        env = report["environmental_designations"]
        if env["in_coastal_barrier_resources_system"]:
            concerns.append("Parcel is in a Coastal Barrier Resources System zone — no federal flood insurance/funding there.")
        if env["in_critical_habitat"]:
            concerns.append(f"Parcel overlaps critical habitat for: {', '.join(env['critical_habitat_species'])}.")

    if report["nearby_development"] is not None:
        if not report["nearby_development"]["likely_utilities_nearby"]:
            concerns.append(
                "Very little development nearby — utilities may not be run to this area yet. "
                "Confirm with the local utility company."
            )

    if report["slope"] is not None and report["slope"].get("steep"):
        concerns.append(f"Steep terrain — approx {report['slope']['approx_slope_percent']}% slope.")

    if report["contamination"] is not None:
        if report["contamination"]["superfund_sites_nearby"]:
            concerns.append(f"Superfund site(s) nearby: {', '.join(report['contamination']['superfund_sites_nearby'])}.")
        if report["contamination"]["brownfield_sites_nearby"]:
            concerns.append(f"Brownfield site(s) nearby: {', '.join(report['contamination']['brownfield_sites_nearby'])}.")

    if report["wildfire_fine_grained"] is not None and "High" in (report["wildfire_fine_grained"].get("tier") or ""):
        concerns.append("Fine-grained wildfire risk estimate: High.")

    if report["broadband"] is not None:
        total = report["broadband"].get("total_locations")
        served = report["broadband"].get("served_locations")
        if total and served is not None and served == 0:
            concerns.append("No broadband service found in this area per FCC data.")

    report["concerns"] = concerns
    return report


# --------------------------------------------------------------------------
# Local market activity: active contractors + recent building permits
#
# There's no single nationwide source for either of these — contractor
# licensing is state-by-state, and permit data comes from either a
# city/county open-data portal or a commercial aggregator. Point
# CONTRACTOR_REGISTRY_API_URL / PERMIT_TRACKER_API_URL (env vars, set
# near the top of this file) at whichever one you have access to; the
# field names in _parse_contractor_record / _parse_permit_record may
# need small tweaks to match that provider's exact response shape.
# --------------------------------------------------------------------------

def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance between two lat/lon points, in miles."""
    earth_radius_mi = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_mi * math.asin(math.sqrt(a))


def _get_zip_code(lat: float, lon: float) -> str | None:
    """Reverse-geocode coordinates to a ZIP code using the free, no-key
    U.S. Census Bureau TIGERweb geography service."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ZCTA5",
        "returnGeometry": "false",
        "f": "json",
    }
    resp = requests.get(CENSUS_ZCTA_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    features = resp.json().get("features") or []
    if features:
        return features[0].get("attributes", {}).get("ZCTA5")
    return None


def _parse_contractor_record(record: dict) -> dict | None:
    """Normalize one raw contractor/builder registry record."""
    lat = record.get("lat") or record.get("latitude")
    lon = record.get("lon") or record.get("longitude")
    if lat is None or lon is None:
        return None
    status = str(record.get("status") or record.get("licenseStatus") or "").strip().lower()
    trade = str(record.get("trade") or record.get("classification") or "").lower()
    is_home_builder_or_gc = (
        not trade or any(kw in trade for kw in ("builder", "general contractor", "residential", " gc"))
    )
    return {
        "name": record.get("businessName") or record.get("name"),
        "active": status in ("active", "current", "valid"),
        "lat": float(lat),
        "lon": float(lon),
        "is_home_builder_or_gc": is_home_builder_or_gc,
    }


def count_active_contractors(lat: float, lon: float, radius_miles: float = 15.0) -> int:
    """Rule 1: active home builders / general contractors registered
    within `radius_miles` of (lat, lon)."""
    if not CONTRACTOR_REGISTRY_API_URL:
        raise RuntimeError(
            "CONTRACTOR_REGISTRY_API_URL is not set — point it at your "
            "state licensing board or contractor-data provider's API."
        )
    params = {"lat": lat, "lon": lon, "radius_miles": radius_miles}
    if CONTRACTOR_REGISTRY_API_KEY:
        params["api_key"] = CONTRACTOR_REGISTRY_API_KEY

    resp = requests.get(CONTRACTOR_REGISTRY_API_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    raw_records = payload.get("results") or payload.get("records") or []

    count = 0
    for raw in raw_records:
        rec = _parse_contractor_record(raw)
        if not rec or not rec["active"] or not rec["is_home_builder_or_gc"]:
            continue
        if _haversine_miles(lat, lon, rec["lat"], rec["lon"]) <= radius_miles:
            count += 1
    return count


def _parse_permit_record(record: dict) -> dict | None:
    """Normalize one raw building-permit record."""
    permit_type = str(record.get("permitType") or record.get("type") or "").lower()
    work_desc = str(record.get("workDescription") or record.get("description") or "").lower()
    issue_date = _parse_date(record.get("issueDate") or record.get("issued_date") or record.get("filedDate"))
    if issue_date is None:
        return None
    combined = f"{permit_type} {work_desc}"
    is_new_residential = "new" in combined and any(
        kw in combined for kw in ("residential", "single family", "dwelling", "sfr")
    )
    return {"issue_date": issue_date, "is_new_residential": is_new_residential}


def get_new_construction_permit_volume(zip_code: str, months: int = 12) -> int:
    """Rule 2: number of new-residential-construction permits filed in
    `zip_code` over the last `months` months."""
    if not PERMIT_TRACKER_API_URL:
        raise RuntimeError(
            "PERMIT_TRACKER_API_URL is not set — point it at your city/"
            "county open-data portal or permit-data provider's API."
        )
    params = {"zip": zip_code, "months": months}
    if PERMIT_TRACKER_API_KEY:
        params["api_key"] = PERMIT_TRACKER_API_KEY

    resp = requests.get(PERMIT_TRACKER_API_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()
    raw_records = payload.get("results") or payload.get("records") or []

    cutoff = date.today() - timedelta(days=30 * months)
    count = 0
    for raw in raw_records:
        rec = _parse_permit_record(raw)
        if rec and rec["is_new_residential"] and rec["issue_date"] >= cutoff:
            count += 1
    return count


def add_market_activity_metrics(parcel: dict) -> dict:
    """
    Takes a parcel dict (needs "lat" and "lon", same shape returned by
    search_vacant_land_parcels) and adds two extra numbers to its
    profile:
      - active_contractors_15mi: active home builders/GCs within 15 miles
      - new_construction_permits_12mo: new-residential permits filed in
        the parcel's ZIP code over the last 12 months
    Returns a new dict — the original parcel fields are kept as-is.
    """
    lat, lon = parcel["lat"], parcel["lon"]
    profile = dict(parcel)

    try:
        profile["active_contractors_15mi"] = count_active_contractors(lat, lon, radius_miles=15.0)
    except Exception:
        log.exception("Could not fetch contractor registry data")
        profile["active_contractors_15mi"] = None

    try:
        zip_code = _get_zip_code(lat, lon)
        profile["zip_code"] = zip_code
        profile["new_construction_permits_12mo"] = (
            get_new_construction_permit_volume(zip_code, months=12) if zip_code else None
        )
    except Exception:
        log.exception("Could not fetch permit tracker data")
        profile["new_construction_permits_12mo"] = None

    return profile


# --------------------------------------------------------------------------
# Listing-site search links
#
# Builds search-result-page URLs (not guaranteed direct listing pages —
# these sites don't offer a stable public "look up by address" link) for
# a parcel address on a few popular land/real-estate sites, so you can
# quickly pull up what's publicly shown there for a given lot.
# --------------------------------------------------------------------------

def build_listing_search_links(street: str, city: str, state: str, zip_code: str) -> dict[str, str]:
    """
    Given a parcel's verified address parts, return a dict of
    ready-to-click search-result-page URLs on Zillow, Land.com,
    Realtor.com, and Homes.com for that address. Each piece of the
    address is percent/web-encoded so addresses with spaces, apostrophes,
    or other special characters still produce a valid, working link.
    """
    full_address = f"{street}, {city}, {state} {zip_code}".strip()
    encoded_full = quote_plus(full_address)
    encoded_street = quote_plus(street)
    encoded_city = quote_plus(city)
    encoded_state = quote_plus(state)

    return {
        "zillow": f"https://www.zillow.com/homes/{encoded_full}_rb/",
        "land_com": f"https://www.land.com/search/{encoded_state}/?query={encoded_full}",
        "realtor_com": f"https://www.realtor.com/realestateandhomes-search/{encoded_city}_{encoded_state}/address-{encoded_street}",
        "homes_com": f"https://www.homes.com/search/?q={encoded_full}",
    }


# ============================================================================
# Skip tracing (Tracerfy) -- BRING-YOUR-OWN-KEY, real money per lookup
#
# Every function here takes the CALLER's own Tracerfy API key -- this
# toolkit's site never runs its own Tracerfy account or bills anyone.
# Confirmed live (2026-09-12) that this is the only viable model:
# BOTH Tracerfy's and BatchData's Terms of Service explicitly prohibit
# running one account and reselling/marking up access to it for your
# own paying customers ("internal business use" only, no sublicensing,
# no third-party resale without a separate negotiated reseller deal).
# Each end user of the eventual website needs their OWN Tracerfy
# account and key.
#
# LIVE-TESTED (2026-09-12): confirmed correct against a real account.
# Unlike the 3 previous times this project's documentation-only guesses
# turned out subtly wrong (Realie's auth header, a Sonoma County tax
# field, a misattributed "LA County zoning" source), this one worked
# exactly as documented on the first real try -- the "Authorization:
# Bearer <key>" header, the request body shape, and the response shape
# (hit/persons[].phones[]/persons[].emails[]) all matched. Live test:
# 318 60th St, Brooklyn, NY 11220 (a real vacant-land owner's mailing
# address already in this project's test data) correctly returned
# "Juan Zuniga" (matching the county record's "ZUNIGA JUAN", just
# reordered), 3 real phone numbers with a correct DNC flag on one of
# them, and 4 real email addresses.
#
# PRICING CORRECTION from that same live test: the real cost was
# 5 credits ($0.10), not the 1-credit/$0.02 "normal" tier this was
# originally written to assume -- the account has no visible control
# over which tier gets used per lookup, so budget for ~$0.10/lookup as
# the realistic default, not $0.02. The $0.02-$0.30 range across
# normal/advanced/enhanced tiers (1/2/15 credits) is still accurate as
# the full possible range per Tracerfy's docs, just not the typical
# case observed live.
# ============================================================================

TRACERFY_LOOKUP_URL = "https://tracerfy.com/v1/api/trace/lookup/"


def _require_tracerfy_api_key(api_key: str | None) -> str:
    if not api_key:
        raise RuntimeError(
            "No Tracerfy API key set. Sign up at https://www.tracerfy.com/ "
            "and pass it as api_key= -- this is YOUR OWN key, billed to "
            "YOUR OWN Tracerfy account (see the section note above on why)."
        )
    return api_key


def skip_trace_owner(
    street: str,
    city: str,
    state: str,
    zip_code: str | None = None,
    api_key: str | None = None,
) -> dict:
    """
    Looks up phone number(s) and email(s) for the owner/resident
    associated with an address, via Tracerfy's skip-tracing API.

    LIVE-TESTED (2026-09-12) -- see the section note above. Confirmed
    correct against a real account: the "Authorization: Bearer <key>"
    header, the request body shape, and the full response shape all
    matched Tracerfy's documentation exactly.

    Pricing note: Tracerfy's per-lookup cost is NOT a flat rate --
    their docs describe "normal" (1 credit), "advanced" (2 credits),
    and "enhanced" (15 credits) trace tiers at $0.02/credit. The one
    real lookup tested cost 5 credits ($0.10), not the 1-credit/$0.02
    "normal" tier this was originally assumed to default to -- budget
    ~$0.10/lookup as the realistic typical cost, with $0.02-$0.30 as
    the full possible range depending on tier (not confirmed how/
    whether the tier is caller-selectable vs. automatic).

    Returns:
        {
            "hit": True/False,
            "owner_name": "Jane Doe" or None,
            "phones": [{"number": ..., "type": ..., "dnc": ..., "litigator": ...}, ...],
            "emails": ["jane@example.com", ...],
            "credits_deducted": ...,
        }
    """
    api_key = _require_tracerfy_api_key(api_key)
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {"address": street, "city": city, "state": state, "find_owner": True}
    if zip_code:
        body["zip"] = zip_code

    resp = requests.post(TRACERFY_LOOKUP_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    payload = resp.json()

    # A 200 response with no "hit" key at all is not the documented
    # shape -- e.g. a degraded/partial Tracerfy response with an
    # {"error": ...} body instead. Silently reading that as "hit": None
    # -> falsy -> "no match found" would tell a customer an owner is
    # unreachable AFTER real money was billed for a lookup that never
    # actually completed. Raise instead so this surfaces as an error,
    # not a confident negative (see skip_trace_owners_bulk, which
    # already catches and reports exceptions per-lookup without
    # aborting the rest of a bulk run).
    if "hit" not in payload:
        raise RuntimeError(f"Unexpected Tracerfy response shape (no 'hit' field): {payload!r}"[:500])

    if not payload.get("hit"):
        return {"hit": False, "owner_name": None, "phones": [], "emails": [], "credits_deducted": payload.get("credits_deducted")}

    persons = payload.get("persons") or []
    phones = []
    emails = []
    for person in persons:
        for ph in (person.get("phones") or []):
            phones.append({
                "number": ph.get("number"),
                "type": ph.get("type"),
                "dnc": ph.get("dnc"),
                "litigator": ph.get("litigator"),
            })
        for em in (person.get("emails") or []):
            if em.get("email"):
                emails.append(em["email"])

    return {
        "hit": True,
        "owner_name": persons[0].get("full_name") if persons else None,
        "phones": phones,
        "emails": emails,
        "credits_deducted": payload.get("credits_deducted"),
    }


def skip_trace_owners_bulk(
    owners: list[dict],
    api_key: str | None = None,
    max_lookups: int = 15,
) -> list[dict]:
    """
    Skip traces a whole list of owners in one call -- for a "Skip Trace
    All Results" button on the website. Deliberately loops
    skip_trace_owner() one at a time rather than using Tracerfy's own
    documented batch/array endpoints: their docs describe a synchronous
    "instant" multi-address array mode AND a separate async CSV/queue-
    based batch mode, but the exact request/response shape for either
    wasn't confirmed clearly enough to trust without a live account.
    The single-lookup endpoint this loops IS now live-confirmed correct
    (see the section note above), but the batch endpoints themselves
    remain unverified -- looping is slower and chattier, but far less
    likely to silently do the wrong thing. Revisit if per-lookup
    round-trip overhead ever actually matters.

    `max_lookups` hard-caps how many real, billed lookups one call can
    trigger (default 15) -- same "don't let a bug or a big list rack up
    an unexpected bill" discipline as search_vacant_land_realie's
    max_pages. This is REAL MONEY per lookup billed to the caller's own
    Tracerfy account -- the website should show the caller an estimated
    cost and get explicit confirmation before calling this on a big
    list, not fire it silently.

    `owners` is a list of {"street": ..., "city": ..., "state": ...,
    "zip": ... (optional), plus any extra keys the caller wants kept}.

    Returns each input owner dict merged with its skip-trace result
    (or {"hit": False, "error": "..."} if that one lookup failed --
    one bad address doesn't abort the rest of the list).
    """
    api_key = _require_tracerfy_api_key(api_key)
    results = []
    for owner in owners[:max_lookups]:
        try:
            trace = skip_trace_owner(
                owner.get("street"), owner.get("city"), owner.get("state"),
                zip_code=owner.get("zip"), api_key=api_key,
            )
        except Exception as e:
            trace = {"hit": False, "error": str(e)}
        results.append({**owner, **trace})
    return results


# ============================================================================
# Trust & Evidence framework (2026-09-13)
#
# This is a SYNTHESIS LAYER, not a replacement. It consumes the existing
# outputs of get_comprehensive_buildability_report(), check_zoning_district(),
# get_tax_flags(), get_owner_mailing_address(), and search_vacant_land_realie()
# raw records -- it makes ZERO new external calls of its own, and it changes
# NOTHING about how those existing functions work. Every function that
# existed before this section keeps its exact same signature and behavior.
#
# WHY THIS EXISTS: a user explicitly pointed out that a vacant-land investor
# "cannot afford to be confidently wrong," and that every prior version of
# this toolkit's output (raw dicts, a bare buildability percentage) let a
# customer read more certainty into a number than the underlying data
# actually supports. This section's whole job is to never let that happen:
# every conclusion gets an explicit evidence level, an explicit statement of
# what it does NOT establish, and a recommended verification step -- visible
# to the customer, not just documented in code comments the way the rest of
# this file's honest-limits discipline usually works.
#
# THE SIX EVIDENCE LEVELS (exactly as specified, not renamed or reduced):
#   VERIFIED             -- confirmed by a source authoritative for this
#                            EXACT fact, on the real parcel geometry. Given
#                            this toolkit's current placeholder-boundary
#                            limitation (see below), almost nothing in a
#                            buildability report can honestly reach this
#                            level yet -- that's a true statement about the
#                            product today, not a bug in this framework.
#   LIKELY               -- a real, authoritative source was checked, but
#                            some gap remains (approximate geometry, a
#                            screening-grade rather than confirmatory
#                            methodology, a known dataset blind spot).
#   INDICATED            -- a real signal exists, but it is a PROXY or
#                            INFERENCE, not a direct measurement of the
#                            actual fact in question (nearby buildings as a
#                            utility signal is the canonical example).
#   UNKNOWN              -- no data source was available or returned a
#                            usable answer for this specific point. This is
#                            NEVER allowed to be presented as "checked, no
#                            issue" -- see _NO_DATA_SOURCE_FINDING below.
#   CONFLICTING          -- two or more sources disagree (reserved for
#                            future use -- this toolkit does not yet
#                            cross-validate any fact against a second
#                            source, so this level is defined but not
#                            currently produced by anything below; wiring a
#                            second source in for any category would be the
#                            natural way to start actually using it).
#   REQUIRES_VERIFICATION -- structurally cannot be answered by ANY data
#                            source this toolkit has, free or paid (legal
#                            easements, minimum lot size, setbacks,
#                            subdivision restrictions). Distinct from
#                            UNKNOWN: UNKNOWN means "we tried and got
#                            nothing back," REQUIRES_VERIFICATION means
#                            "this was never a question free public data
#                            could answer in the first place."
# ============================================================================

EVIDENCE_VERIFIED = "VERIFIED"
EVIDENCE_LIKELY = "LIKELY"
EVIDENCE_INDICATED = "INDICATED"
EVIDENCE_UNKNOWN = "UNKNOWN"
EVIDENCE_CONFLICTING = "CONFLICTING"
EVIDENCE_REQUIRES_VERIFICATION = "REQUIRES_VERIFICATION"

# Status is a SEPARATE axis from evidence level -- evidence level says how
# much to trust the finding; status says how the finding should make a
# customer feel about the parcel. A CAUTION status can sit on top of LIKELY
# evidence (real data, but the fact itself doesn't fully clear the parcel);
# an UNKNOWN status always pairs with UNKNOWN or REQUIRES_VERIFICATION
# evidence, never with VERIFIED/LIKELY/INDICATED.
STATUS_CLEAR = "CLEAR"
STATUS_CAUTION = "CAUTION"
STATUS_CONCERN = "CONCERN"
STATUS_UNKNOWN = "UNKNOWN"

# Categories that can never rise above an indirect proxy signal no matter
# how good the result is (a nearby-building count never confirms a real
# utility hookup) -- excluded from the pass/fail screening gate in both
# _finalize_evidence_report's overall_verdict AND build_decision_summary's
# deal_potential tier, for the identical reason in both places: scoring
# them like a normal risk would make the best possible tier unreachable
# for every parcel, which is its own kind of dishonesty. Shared at module
# level so the two derivations can't drift out of sync with each other.
_PERMANENTLY_INDICATIVE_CATEGORIES = {
    "Utilities (electric/water/sewer)",
    "Nearby development / surrounding houses",
}

# Concern-level findings in these categories are, on their own, severe
# enough to override every other signal and force a hard "Avoid" in the
# decision engine below -- a landlocked parcel or one that's mostly
# flood/wetland is a different kind of problem than "several open
# questions," and shouldn't just add one more point to a tally.
_HARD_STOP_CATEGORIES = {
    "Physical road access",
    "Environmental restrictions / protected areas",
    "Physical usable area (screening only)",
    "Environmental contamination (Superfund/Brownfields)",
}


def _screenable_findings(findings: list[dict]) -> list[dict]:
    """The subset of findings this toolkit can actually screen on: not the
    categories that are REQUIRES_VERIFICATION in every report by design
    (easements, setbacks, etc. -- always unknown, so including them would
    make every parcel look identically 'insufficient data'), and not the
    permanently-proxy-only categories above."""
    return [
        f for f in findings
        if f["evidence_level"] != EVIDENCE_REQUIRES_VERIFICATION
        and f["category"] not in _PERMANENTLY_INDICATIVE_CATEGORIES
    ]


# Plain-English "quick labels" shown alongside every finding, on top of
# the more technical STATUS/EVIDENCE_LEVEL badges. These are the exact
# four terms explicitly asked for during the external-data-source audit
# (2026-09-14): a failed API call must never silently read as "no," and
# a customer should be able to tell at a glance WHY a category has no
# real answer, not just that it doesn't:
#   SOURCE UNAVAILABLE -- the data source did not respond at all (a
#                          network error, a timeout, an HTTP failure).
#                          Retrying later may get a real answer.
#   DATA UNAVAILABLE   -- the source DID respond, but had nothing usable
#                          for this exact location (a genuine coverage
#                          gap, not a technical failure). Retrying won't
#                          help; a different source might.
#   VERIFY MANUALLY    -- this category is structurally unanswerable by
#                          any data source this toolkit uses, for any
#                          parcel, ever (recorded easements, setbacks,
#                          etc.) -- always requires a human/professional
#                          step, not a retry.
# A finding with real, usable data gets no quick_label at all -- the
# STATUS badge (CLEAR/CAUTION/CONCERN) already says enough, and stamping
# a label on every single finding would bury the ones that actually need
# a customer's attention.
QUICK_LABEL_SOURCE_UNAVAILABLE = "SOURCE UNAVAILABLE"
QUICK_LABEL_DATA_UNAVAILABLE = "DATA UNAVAILABLE"
QUICK_LABEL_VERIFY_MANUALLY = "VERIFY MANUALLY"


def make_finding(
    category: str,
    status: str,
    evidence_level: str,
    source: str,
    establishes: str,
    does_not_establish: str,
    explanation: str,
    limitation: str,
    next_step: str,
    source_date: str | None = None,
    quick_label: str | None = None,
) -> dict:
    """
    The one standardized shape every finding in this framework is built
    from -- category, evidence_level, and status are always machine-
    readable constants from the lists above; everything else is the plain-
    English text a customer actually reads. `source_date` is the
    underlying DATA's own vintage where one exists (a tax year, an
    assessment date) -- NOT "when this API was called," which is always
    "just now" and tells a customer nothing useful about data freshness.

    `quick_label` defaults to VERIFY MANUALLY for structurally-
    unanswerable categories -- callers reporting a real API/coverage gap
    should go through `_no_data_finding` instead, which sets the correct
    SOURCE UNAVAILABLE vs. DATA UNAVAILABLE label explicitly rather than
    guessing from evidence_level alone.
    """
    if quick_label is None and evidence_level == EVIDENCE_REQUIRES_VERIFICATION:
        quick_label = QUICK_LABEL_VERIFY_MANUALLY
    return {
        "category": category,
        "status": status,
        "evidence_level": evidence_level,
        "source": source,
        "source_date": source_date,
        "establishes": establishes,
        "does_not_establish": does_not_establish,
        "explanation": explanation,
        "limitation": limitation,
        "next_step": next_step,
        "quick_label": quick_label,
    }


def _no_data_finding(category: str, source_attempted: str, next_step: str, reason: str = "source_unavailable") -> dict:
    """
    The mandatory shape for "we have nothing." Fixes the exact failure
    mode named explicitly: "no data found" must never read as "doesn't
    exist." Every no-data finding says, in the same words, that absence of
    data is not absence of the underlying fact.

    `reason` distinguishes the two genuinely different ways a category
    ends up with nothing, since they call for different next steps:
      "source_unavailable" (default) -- the API call itself failed
        (network error, timeout, HTTP error). Worth retrying.
      "no_coverage" -- the source responded successfully but had no
        usable data for this exact point (e.g. USDA's soil survey
        simply doesn't rate this location). Retrying won't help; this
        location is just outside that source's coverage.
    """
    if reason == "no_coverage":
        quick_label = QUICK_LABEL_DATA_UNAVAILABLE
        explanation = f"{source_attempted} responded but had no usable data for this exact location."
        limitation = "A genuine coverage gap in this data source for this location, not a failed request."
    else:
        quick_label = QUICK_LABEL_SOURCE_UNAVAILABLE
        explanation = f"{source_attempted} did not respond, or the request failed, for this location."
        limitation = "No response from the data source attempted for this category -- worth retrying."
    return make_finding(
        category=category,
        status=STATUS_UNKNOWN,
        evidence_level=EVIDENCE_UNKNOWN,
        source=source_attempted,
        establishes="Nothing -- no usable data was returned.",
        does_not_establish=(
            "This does NOT mean the condition is absent. Absence of data is "
            "not evidence of absence of the underlying fact."
        ),
        explanation=explanation,
        limitation=limitation,
        next_step=next_step,
        quick_label=quick_label,
    )


def _never_available_finding(category: str, why: str, next_step: str) -> dict:
    """
    For the categories that NO free or paid data source in this toolkit
    can ever answer (legal easements, minimum lot size, setbacks,
    subdivision restrictions). Always REQUIRES_VERIFICATION, always the
    same honest framing, never silently omitted from a report.
    """
    return make_finding(
        category=category,
        status=STATUS_UNKNOWN,
        evidence_level=EVIDENCE_REQUIRES_VERIFICATION,
        source="No data source -- not attempted",
        establishes="Nothing. This was never checked against any dataset.",
        does_not_establish="Anything about whether this is or isn't a problem for this parcel.",
        explanation=why,
        limitation="This category cannot currently be answered by any free or paid source this toolkit uses.",
        next_step=next_step,
        quick_label=QUICK_LABEL_VERIFY_MANUALLY,
    )


def build_evidence_report(buildability: dict, zoning: dict, tax_flags: dict, owner_mailing: dict) -> dict:
    """
    Takes the ALREADY-COMPUTED outputs of get_comprehensive_buildability_report(),
    check_zoning_district(), get_tax_flags(), and get_owner_mailing_address()
    and reinterprets them into the evidence/trust framework above. Makes no
    new network calls -- pure synthesis of data this toolkit already fetched.

    IMPORTANT, STANDING LIMITATION that shapes every finding below: every
    buildability check that needs a parcel BOUNDARY (not just a point) is
    currently run against a synthetic placeholder box centered on the
    parcel's coordinate, not its real shape (see get_comprehensive_
    buildability_report's docstring). That means NOTHING geometry-based
    below can honestly claim VERIFIED status -- it is capped at LIKELY at
    best, regardless of how authoritative the underlying government
    dataset is, because the SHAPE being checked against that dataset is
    not confirmed to be the real parcel. This cap is applied explicitly,
    category by category, not left implicit.

    Returns:
        {
            "findings": [ ... one dict per category, in a fixed order ... ],
            "what_we_know": [ ... category names ... ],
            "what_we_think": [ ... category names ... ],
            "what_we_dont_know": [ ... category names ... ],
            "what_could_kill_the_deal": [ ... category names ... ],
            "what_to_verify_next": [ ... deduplicated next-step strings, concerns first ... ],
            "scores": { ... see _build_scores ... },
            "overall_verdict": "PROMISING SCREENING RESULT" | "MIXED SIGNALS" |
                                "SIGNIFICANT CONCERNS FOUND" | "INSUFFICIENT DATA TO SCREEN",
            "overall_verdict_explanation": "...",
        }
    """
    findings: list[dict] = []

    # --- Physical road access (is there a mapped road there at all) -------
    road = buildability.get("road_access")
    if road is None:
        findings.append(_no_data_finding(
            "Physical road access",
            "Census TIGER/Line roads (via a government server that did not respond)",
            "Check county GIS or drive the frontage in person.",
        ))
    else:
        has_road = road.get("has_mapped_road_access")
        roads = road.get("nearby_roads") or []
        if has_road:
            findings.append(make_finding(
                category="Physical road access",
                status=STATUS_CLEAR,
                evidence_level=EVIDENCE_LIKELY,
                source="U.S. Census Bureau TIGER/Line roads",
                establishes=f"A mapped road ({', '.join(roads[:3])}) appears to touch or lie near the analyzed boundary.",
                does_not_establish="Legal access, a recorded easement, or that the road is publicly maintained. See the separate Legal Road Access finding below.",
                explanation="Census road data is self-reported by local governments and checked against an approximate placeholder boundary, not the parcel's confirmed real shape.",
                limitation="TIGER data has known gaps: some real private/rural roads are missing, and a small number of trails are mis-tagged as roads.",
                next_step="Confirm the physical road surface exists by viewing satellite imagery or visiting in person.",
            ))
        else:
            findings.append(make_finding(
                category="Physical road access",
                status=STATUS_CONCERN,
                evidence_level=EVIDENCE_INDICATED,
                source="U.S. Census Bureau TIGER/Line roads",
                establishes="No mapped road was found touching the analyzed boundary in this dataset.",
                does_not_establish="That the parcel is definitely landlocked -- TIGER is known to omit real private and rural roads.",
                explanation="Absence in this specific federal dataset is a real warning sign, not a confirmed conclusion, on a placeholder boundary.",
                limitation="A parcel can have genuine physical road access that simply isn't in this dataset.",
                next_step="Verify road access with satellite imagery, a plat map, or an in-person visit before treating this as disqualifying.",
            ))

    # --- Legal road access -- structurally never answerable here ----------
    findings.append(_never_available_finding(
        "Legal road access",
        "A road touching a parcel on a map is not the same as a recorded legal right to use it. No free or paid source in this toolkit checks recorded easements, deeded rights-of-way, or county-maintained-road status.",
        "Verify recorded access/easement and legal road-maintenance status with the county recorder or a title company before relying on this parcel having legal access.",
    ))

    # --- Parcel frontage ----------------------------------------------------
    findings.append(_never_available_finding(
        "Parcel frontage",
        "No data source used by this toolkit reports measured road-frontage footage for a parcel.",
        "Obtain a plat map or survey to confirm actual frontage footage.",
    ))

    # --- Easements ------------------------------------------------------
    findings.append(_never_available_finding(
        "Easements",
        "Recorded easements (utility, access, drainage, or otherwise) are not published in any free nationwide dataset and are not checked by this toolkit.",
        "A title search is the only reliable way to identify recorded easements.",
    ))

    # --- Utilities (electric / water / sewer) -- the canonical proxy -------
    nearby = buildability.get("nearby_development")
    if nearby is None:
        findings.append(_no_data_finding(
            "Utilities (electric/water/sewer)",
            "OpenStreetMap Overpass (server did not respond)",
            "Contact the local utility companies directly.",
        ))
    else:
        count = nearby.get("nearby_building_count")
        findings.append(make_finding(
            category="Utilities (electric/water/sewer)",
            status=STATUS_CAUTION,
            evidence_level=EVIDENCE_INDICATED,
            source="OpenStreetMap building density (proxy)",
            establishes=f"{count} buildings were found nearby, used only as a rough proxy for whether utility infrastructure likely exists in the area.",
            does_not_establish=(
                "Whether electric, water, or sewer service is actually available AT THIS PARCEL. "
                "Nearby buildings do not confirm a hookup exists, that the parcel is on the same "
                "grid, or what it would cost to connect. No dataset, free or paid, answers this "
                "question directly anywhere in the United States."
            ),
            explanation="This is a screening signal only, not a utility-company confirmation.",
            limitation="A high nearby-building count is a reasonable 'probably' at best; a low count is a real warning sign but not proof of no service.",
            next_step="Call the specific local electric, water, and sewer/septic authorities to confirm hookup availability and cost before relying on this.",
        ))

    # --- Septic suitability -------------------------------------------------
    septic = buildability.get("septic_suitability")
    if septic is None:
        # get_septic_suitability() itself either raised, or the request
        # timed out at the top-level dispatcher -- the API/request
        # itself failed. Worth retrying.
        findings.append(_no_data_finding(
            "Septic / sanitation",
            "USDA Soil Data Access",
            "Commission a percolation test.",
            reason="source_unavailable",
        ))
    elif not septic:
        # get_septic_suitability() returned {} -- it ran successfully
        # but genuinely has no soil-survey coverage for this exact
        # point (or an unparseable response it couldn't recover from).
        # Retrying the same request won't help; this is a real
        # coverage gap in the source itself, not a failed call.
        findings.append(_no_data_finding(
            "Septic / sanitation",
            "USDA Soil Data Access",
            "Commission a percolation test.",
            reason="no_coverage",
        ))
    else:
        rating = septic.get("rating")
        status_map = {
            "Not limited": STATUS_CLEAR, "Somewhat limited": STATUS_CAUTION,
            "Very limited": STATUS_CONCERN, "Not rated": STATUS_UNKNOWN,
        }
        # A rating that's missing/unrecognized (None, or anything other
        # than the 4 known SSURGO values -- an unexpected schema change,
        # for instance) must be treated exactly like "Not rated": real
        # bug found during the 2026-09-14 audit where a None rating
        # fell through to EVIDENCE_LIKELY and displayed "rated 'None'"
        # -- confidently uncertain, when it should have been UNKNOWN.
        status = status_map.get(rating, STATUS_UNKNOWN)
        evidence = EVIDENCE_LIKELY if rating in status_map and rating != "Not rated" else EVIDENCE_UNKNOWN
        rating_text = rating if rating in status_map else "unrecognized/missing"
        findings.append(make_finding(
            category="Septic / sanitation",
            status=status,
            evidence_level=evidence,
            source="USDA Soil Data Access (SSURGO soil survey)",
            establishes=f"The dominant soil type at this point is rated '{rating_text}' for septic absorption fields.",
            does_not_establish=(
                "Whether a septic permit would actually be approved. This is a soil-type "
                "screening rating, not a real percolation test, and does not account for lot "
                "slope, water table depth, or lot size requirements. 'Not limited' does not mean "
                "automatically approved, and 'Very limited' does not mean automatically impossible."
            ),
            explanation=f"Soil type: {septic.get('soil_name', 'unknown')}, covering {septic.get('coverage_pct', '?')}% of the sampled point.",
            limitation="A single dominant soil type at one point, not a site-specific test.",
            next_step="Commission a real percolation/soil test from a licensed professional before assuming septic feasibility either way.",
        ))

    # --- Wetlands + Flood/floodway -------------------------------------
    flood_wetland = buildability.get("flood_and_wetland")
    if flood_wetland is None:
        findings.append(_no_data_finding("Wetlands", "USFWS National Wetlands Inventory", "Order a formal wetland delineation."))
        findings.append(_no_data_finding("Flood / floodway", "FEMA National Flood Hazard Layer", "Order a FEMA elevation certificate or flood determination."))
    else:
        excluded_acres = flood_wetland.get("flood_or_wetland_area_acres", 0)
        total_acres = flood_wetland.get("total_area_acres", 0)
        pct_clear = flood_wetland.get("buildability_score", 100)
        # HONEST LIMIT surfaced directly: flood and wetland are checked as
        # ONE combined excluded area by the underlying calculation, not
        # separately. This framework does not currently split them back
        # apart -- disclosed here rather than implied as two independent
        # confirmations.
        combined_note = (
            "This toolkit currently checks flood zones and wetlands together as one combined "
            "excluded area and cannot yet separately confirm which portion, if any, is wetland "
            "versus flood zone."
        )
        if excluded_acres > 0:
            findings.append(make_finding(
                category="Wetlands",
                status=STATUS_CAUTION,
                evidence_level=EVIDENCE_LIKELY,
                source="USFWS National Wetlands Inventory + FEMA NFHL (combined)",
                establishes=f"Some portion of the analyzed boundary ({excluded_acres:.2f} of {total_acres:.2f} acres) overlaps a mapped flood zone or wetland.",
                does_not_establish="A confirmed on-site wetland delineation, or which portion specifically is wetland versus flood zone.",
                explanation=combined_note,
                limitation="Checked against a placeholder boundary, not the parcel's confirmed real shape; national wetland maps can lag a real site visit.",
                next_step="Order a formal wetland delineation and a FEMA elevation certificate before relying on this.",
            ))
            findings.append(make_finding(
                category="Flood / floodway",
                status=STATUS_CONCERN,
                evidence_level=EVIDENCE_LIKELY,
                source="FEMA National Flood Hazard Layer (SFHA only)",
                establishes=f"Mapped flood/wetland overlap detected on the analyzed boundary.",
                does_not_establish="The specific flood zone subtype (AE, VE, etc.), floodway status, or current insurance requirements -- those carry very different implications and are not distinguished here.",
                explanation=combined_note,
                limitation="Checked against a placeholder boundary, not the parcel's confirmed real shape; FEMA maps can be years out of date in some counties.",
                next_step="Obtain the parcel's exact FEMA flood zone designation and, if in a mapped zone, an elevation certificate.",
            ))
        else:
            findings.append(make_finding(
                category="Wetlands",
                status=STATUS_CLEAR,
                evidence_level=EVIDENCE_LIKELY,
                source="USFWS National Wetlands Inventory",
                establishes="No wetlands were found overlapping the analyzed boundary in this federal dataset.",
                does_not_establish="That wetlands are definitively absent. This is one federal map, not an on-site delineation, and the boundary checked is an approximation of the real parcel.",
                explanation="Absence in this specific dataset is a positive signal, not a guarantee.",
                limitation="National wetland inventories can be out of date relative to real on-the-ground conditions.",
                next_step="A formal wetland delineation is the only way to be certain, and is advisable before any development-dependent purchase.",
            ))
            findings.append(make_finding(
                category="Flood / floodway",
                status=STATUS_CLEAR,
                evidence_level=EVIDENCE_LIKELY,
                source="FEMA National Flood Hazard Layer (SFHA only)",
                establishes=f"No mapped Special Flood Hazard Area overlap was found ({pct_clear:.1f}% of the analyzed boundary clear).",
                does_not_establish="Zero flood risk, floodway status, or that FEMA's map is current for this specific parcel.",
                explanation="FEMA maps are periodically updated and can be outdated in some counties relative to real conditions on the ground.",
                limitation="Checked against a placeholder boundary, not the parcel's confirmed real shape.",
                next_step="Confirm the parcel's official FEMA flood zone designation directly before relying on this for a purchase decision.",
            ))

    # --- Slope / terrain --------------------------------------------------
    slope = buildability.get("slope")
    if not slope:
        findings.append(_no_data_finding("Slope / terrain", "USGS elevation service", "Walk the site or commission a topographic survey."))
    else:
        steep = slope.get("steep")
        findings.append(make_finding(
            category="Slope / terrain",
            status=STATUS_CONCERN if steep else STATUS_CLEAR,
            evidence_level=EVIDENCE_LIKELY,
            source="USGS elevation service (5-point sample)",
            establishes=f"Approximately {slope.get('approx_slope_percent')}% slope estimated from a 5-point elevation sample.",
            does_not_establish="A survey-grade slope measurement, or that the terrain is uniform across the whole parcel -- a steep section between sample points would not be caught.",
            explanation="A rough estimate from 5 points ~30 meters apart, not a topographic survey.",
            limitation="Localized steep areas between the 5 sample points would not be detected.",
            next_step="Walk the site or commission a topographic survey before assuming grading costs.",
        ))

    # --- Zoning -----------------------------------------------------------
    if zoning and zoning.get("lookup_failed"):
        # A real bug found during the 2026-09-14 audit: this used to be
        # indistinguishable from "confirmed outside our 2-county
        # coverage" below, so a government server hiccup for a parcel
        # that genuinely IS in Miami-Dade or King County would tell the
        # customer they were outside coverage entirely -- wrong
        # explanation, wrong next step (retry vs. "not covered").
        findings.append(_no_data_finding(
            "Zoning",
            "County zoning GIS (Miami-Dade County, FL / King County, WA)",
            "Try again in a moment, or contact the county planning/zoning department directly.",
            reason="source_unavailable",
        ))
    elif not zoning:
        findings.append(make_finding(
            category="Zoning",
            status=STATUS_UNKNOWN,
            evidence_level=EVIDENCE_REQUIRES_VERIFICATION,
            source="County zoning GIS (only 2 counties nationwide currently covered by this toolkit)",
            establishes="Nothing -- this parcel is outside the 2 counties this toolkit currently has zoning data for.",
            does_not_establish=(
                "That zoning is favorable, unrestricted, or even residential. An unchecked "
                "zoning finding is NOT the same as a clean zoning result -- it means zoning was "
                "never checked at all for this location."
            ),
            explanation="Free, structured, nationwide zoning data does not exist; only 2 counties are covered today.",
            limitation="No zoning information is available for the vast majority of U.S. parcels in this toolkit today.",
            next_step="Contact the county planning/zoning department directly to confirm the zoning district and permitted uses.",
            quick_label=QUICK_LABEL_DATA_UNAVAILABLE,
        ))
    else:
        findings.append(make_finding(
            category="Zoning",
            status=STATUS_CAUTION,
            evidence_level=EVIDENCE_INDICATED,
            source=zoning.get("source", "County zoning GIS"),
            establishes=f"Zoning district code: {zoning.get('zoning_code', 'unknown')}.",
            does_not_establish="The actual permitted uses, minimum lot size, setbacks, height limits, or FAR for that zoning code -- only the district name/code itself is known.",
            explanation="District code only; the underlying zoning ordinance text (what that code actually allows) is not looked up.",
            limitation="A code like this means little without looking up what it actually permits in the local zoning ordinance.",
            next_step="Look up the actual zoning ordinance for this code with the county, including permitted uses, setbacks, and any overlay restrictions.",
        ))

    # --- Minimum lot size / setbacks / frontage requirements ---------------
    findings.append(_never_available_finding(
        "Minimum lot size, setbacks & frontage requirements",
        "These numeric zoning requirements are not published in any free nationwide dataset and are not checked even in the 2 counties this toolkit has base zoning coverage for.",
        "Obtain these figures directly from the county zoning ordinance or a paid zoning-data provider before assuming a structure of any particular size is permitted.",
    ))

    # --- Parcel dimensions --------------------------------------------------
    findings.append(make_finding(
        category="Parcel dimensions",
        status=STATUS_UNKNOWN,
        evidence_level=EVIDENCE_REQUIRES_VERIFICATION,
        source="Not available -- only a placeholder boundary exists internally",
        establishes="Nothing about the parcel's real shape or exact boundary lines.",
        does_not_establish="Anything -- the acreage figure shown elsewhere in this report comes from the county assessor and is separate from, and more reliable than, this boundary question.",
        explanation="This toolkit does not currently have access to the parcel's real recorded boundary, only its approximate center point.",
        limitation="All geometry-based findings in this report (flood, wetland, road access, environmental designations) are checked against a synthetic placeholder shape, not the real parcel.",
        next_step="Obtain a plat map or survey for the parcel's real boundary before relying on any shape-based finding above.",
    ))

    # --- Subdivision restrictions -------------------------------------------
    findings.append(_never_available_finding(
        "Subdivision restrictions",
        "Deed restrictions, HOA covenants, and subdivision plat restrictions are not published in any free nationwide dataset.",
        "A title search will reveal recorded subdivision restrictions, if any.",
    ))

    # --- Environmental restrictions / protected areas -----------------------
    env = buildability.get("environmental_designations")
    if env is None:
        findings.append(_no_data_finding("Environmental restrictions / protected areas", "USFWS Coastal Barrier Resources System + Critical Habitat", "Consult a wetlands/environmental attorney or consultant."))
    else:
        in_cbrs = env.get("in_coastal_barrier_resources_system")
        in_habitat = env.get("in_critical_habitat")
        if in_cbrs or in_habitat:
            parts = []
            if in_cbrs:
                parts.append("within a Coastal Barrier Resources System zone (no federal flood insurance or infrastructure funding available there)")
            if in_habitat:
                species = ", ".join(env.get("critical_habitat_species") or [])
                parts.append(f"overlapping designated critical habitat for: {species}")
            findings.append(make_finding(
                category="Environmental restrictions / protected areas",
                status=STATUS_CONCERN,
                evidence_level=EVIDENCE_LIKELY,
                source="USFWS Coastal Barrier Resources System + Critical Habitat (federal only)",
                establishes="This parcel is " + " and ".join(parts) + ".",
                does_not_establish="Any state or local environmental designations, which are not checked at all.",
                explanation="Federal designations only. These can restrict or add significant cost to development.",
                limitation="Checked against a placeholder boundary, and federal-only -- state/local protected-area rules are not covered.",
                next_step="Consult an environmental attorney or consultant before proceeding.",
            ))
        else:
            findings.append(make_finding(
                category="Environmental restrictions / protected areas",
                status=STATUS_CLEAR,
                evidence_level=EVIDENCE_LIKELY,
                source="USFWS Coastal Barrier Resources System + Critical Habitat (federal only)",
                establishes="No federal coastal-barrier or critical-habitat designation was found overlapping the analyzed boundary.",
                does_not_establish="That the parcel is free of ALL environmental restrictions -- only these 2 specific federal designations were checked; state and local designations are not covered at all.",
                explanation="A clean result on 2 specific federal datasets, not a comprehensive environmental clearance.",
                limitation="State and local environmental designations are not checked by this toolkit.",
                next_step="Check with the state environmental agency for any state-level protected-area designations.",
            ))

    # --- Environmental contamination (EPA Superfund + Brownfields) ---------
    # Computed by get_comprehensive_buildability_report all along, but
    # was NEVER read here before this fix -- a real Superfund site next
    # door had zero effect on the evidence report or the deal-potential
    # tier, only visible via the raw-JSON toggle. Superfund/Brownfields
    # proximity is a serious, decision-relevant environmental liability
    # for a land investor, not a footnote.
    contamination = buildability.get("contamination")
    if contamination is None:
        findings.append(_no_data_finding(
            "Environmental contamination (Superfund/Brownfields)",
            "EPA Superfund (NPL) + Brownfields/ACRES",
            "Check the EPA's public Superfund/Brownfields site locators directly before relying on this being clear.",
        ))
    else:
        superfund_sites = contamination.get("superfund_sites_nearby")
        brownfield_sites = contamination.get("brownfield_sites_nearby")
        # The two EPA sources are independently fault-tolerant (see
        # check_contamination_sites) -- a source that failed reports
        # None here (unknown), never confused with a source that
        # succeeded and confirmed zero sites nearby ([]).
        if superfund_sites is None and brownfield_sites is None:
            findings.append(_no_data_finding(
                "Environmental contamination (Superfund/Brownfields)",
                "EPA Superfund (NPL) + Brownfields/ACRES (both sources did not respond)",
                "Check the EPA's public Superfund/Brownfields site locators directly before relying on this being clear.",
            ))
        elif superfund_sites or brownfield_sites:
            parts = []
            if superfund_sites:
                parts.append(f"Superfund (NPL) site(s) within 2km: {', '.join(superfund_sites)}")
            if brownfield_sites:
                parts.append(f"Brownfield/ACRES site(s) within 2km: {', '.join(brownfield_sites)}")
            unknown_note = ""
            if superfund_sites is None:
                unknown_note = " (Superfund could not be checked this time -- treat as unknown, not clear.)"
            elif brownfield_sites is None:
                unknown_note = " (Brownfields could not be checked this time -- treat as unknown, not clear.)"
            findings.append(make_finding(
                category="Environmental contamination (Superfund/Brownfields)",
                status=STATUS_CONCERN,
                evidence_level=EVIDENCE_LIKELY,
                source="EPA Superfund (NPL) + Brownfields/ACRES",
                establishes="; ".join(parts) + "." + unknown_note,
                does_not_establish="The exact distance, contamination type, cleanup status, or whether this parcel itself (vs. a nearby one) is affected -- proximity only.",
                explanation="A federal contaminated/assessed site within 2km can mean real remediation liability, financing difficulty, or marketability problems, even if this specific parcel was never itself listed.",
                limitation="Checked at a 2km radius from the parcel's center point, federal databases only -- state contamination registries are not checked.",
                next_step="Look up the specific site(s) on EPA's Superfund/Brownfields site pages and consult an environmental attorney before proceeding.",
            ))
        else:
            note = ""
            if superfund_sites is None or brownfield_sites is None:
                missing = "Superfund" if superfund_sites is None else "Brownfields"
                note = f" ({missing} could not be checked this time -- that half is unknown, not confirmed clear.)"
            findings.append(make_finding(
                category="Environmental contamination (Superfund/Brownfields)",
                status=STATUS_CLEAR if not note else STATUS_CAUTION,
                evidence_level=EVIDENCE_LIKELY,
                source="EPA Superfund (NPL) + Brownfields/ACRES",
                establishes="No EPA Superfund or Brownfields/ACRES site was found within 2km of this parcel." + note,
                does_not_establish="That the parcel is free of all contamination -- only 2 specific federal databases, at a 2km radius, were checked.",
                explanation="A clean result on 2 specific federal datasets, not a full environmental site assessment.",
                limitation="State contamination registries and any assessment closer than the parcel's own site are not checked.",
                next_step="A Phase I Environmental Site Assessment is the standard way to be certain before a commercial-scale purchase.",
            ))

    # --- Wildfire risk (fine-grained, USFS) ---------------------------------
    # Also computed all along and never surfaced here before this fix.
    # Kept at INDICATED, never LIKELY/VERIFIED -- the "tier" cutoffs are
    # this toolkit's own rough approximation, not an official USFS
    # classification (see check_fine_grained_wildfire_risk's docstring).
    wildfire = buildability.get("wildfire_fine_grained")
    if wildfire is None:
        findings.append(_no_data_finding(
            "Wildfire risk (fine-grained)",
            "US Forest Service wildfire hazard potential (30m resolution)",
            "Check state/local wildfire hazard severity zone maps directly.",
        ))
    elif wildfire.get("raw_value") is None:
        findings.append(make_finding(
            category="Wildfire risk (fine-grained)",
            status=STATUS_UNKNOWN,
            evidence_level=EVIDENCE_UNKNOWN,
            source="US Forest Service wildfire hazard potential (30m resolution)",
            establishes="No usable value returned for this exact point (common for water, urban, or other non-burnable areas).",
            does_not_establish="That wildfire risk is low -- this specific dataset simply has no rating here.",
            explanation="USFS's fine-grained layer doesn't rate every point (e.g. open water, dense urban areas).",
            limitation="A gap in this one dataset, not a confirmed absence of wildfire risk.",
            next_step="Check state/local wildfire hazard severity zone maps directly.",
        ))
    else:
        tier = wildfire.get("tier") or ""
        status = STATUS_CONCERN if "High" in tier else (STATUS_CAUTION if "Moderate" in tier else STATUS_CLEAR)
        findings.append(make_finding(
            category="Wildfire risk (fine-grained)",
            status=status,
            evidence_level=EVIDENCE_INDICATED,
            source="US Forest Service wildfire hazard potential (30m resolution)",
            establishes=f"Fine-grained wildfire hazard tier: {tier} (raw index value {wildfire.get('raw_value')}).",
            does_not_establish="An official USFS risk classification -- the tier cutoffs used here are this toolkit's own rough approximation, not an official breakpoint.",
            explanation="30-meter-resolution federal data, much finer than the county-wide FEMA hazard rating, but the tier labels themselves are this toolkit's own estimate.",
            limitation="Rough approximation only -- treat the raw index value as the more trustworthy relative signal.",
            next_step="Check your state/local wildfire hazard severity zone maps and insurer requirements directly before relying on this.",
        ))

    # --- Nearby development / surrounding houses (context, not risk) -------
    if nearby is not None:
        findings.append(make_finding(
            category="Nearby development / surrounding houses",
            status=STATUS_CAUTION,
            evidence_level=EVIDENCE_LIKELY,
            source="OpenStreetMap",
            establishes=f"{nearby.get('nearby_building_count')} mapped buildings within 400 meters.",
            does_not_establish="Property condition, occupancy, or values of those buildings -- building count only.",
            explanation="OpenStreetMap completeness varies significantly by area; some regions are mapped in detail, others sparsely.",
            limitation="A building-count proxy, not a verified neighborhood survey.",
            next_step="Review satellite imagery or visit the area to see actual surrounding development.",
        ))

    # --- Buildable area / development potential (the redesigned metric) ----
    if flood_wetland is not None:
        findings.append(make_finding(
            category="Physical usable area (screening only)",
            status=STATUS_CLEAR if pct_clear >= 90 else (STATUS_CAUTION if pct_clear >= 50 else STATUS_CONCERN),
            evidence_level=EVIDENCE_LIKELY,
            source="Derived: FEMA + USFWS overlap on the analyzed boundary",
            establishes=f"Approximately {pct_clear:.1f}% of the analyzed boundary does not overlap a mapped flood zone or wetland.",
            does_not_establish=(
                "That this percentage of the parcel is LEGALLY DEVELOPABLE. This is a physical "
                "screening measure of one specific factor (flood/wetland overlap) against an "
                "approximate boundary -- it does not account for zoning, setbacks, minimum lot "
                "size, legal access, septic feasibility, or any other requirement above. A "
                "high percentage here is not permission to build; a low percentage does not by "
                "itself mean unbuildable."
            ),
            explanation="Deliberately renamed and reframed from an earlier 'buildability score' -- that name implied more than this measurement actually establishes.",
            limitation="Measured against a placeholder boundary, one factor among many required for actual development.",
            next_step="Treat this as one input among many, not a standalone buildability answer -- confirm the boundary, zoning, access, and utilities separately.",
        ))

    return _finalize_evidence_report(findings, tax_flags, owner_mailing)


def _finalize_evidence_report(findings: list[dict], tax_flags: dict, owner_mailing: dict) -> dict:
    """Buckets findings into the 5 customer-facing groups the user
    explicitly asked for, computes the category scores, and derives the
    overall verdict from explicit, inspectable rules -- never a single
    blended percentage that would hide how much is actually unknown."""

    what_we_know = [f["category"] for f in findings if f["evidence_level"] in (EVIDENCE_VERIFIED, EVIDENCE_LIKELY)]
    what_we_think = [f["category"] for f in findings if f["evidence_level"] == EVIDENCE_INDICATED]
    what_we_dont_know = [f["category"] for f in findings if f["evidence_level"] in (EVIDENCE_UNKNOWN, EVIDENCE_REQUIRES_VERIFICATION, EVIDENCE_CONFLICTING)]
    what_could_kill_the_deal = [f["category"] for f in findings if f["status"] == STATUS_CONCERN]

    if tax_flags and tax_flags.get("flagged"):
        what_could_kill_the_deal.append("Tax/lien flags on file (see owner & tax section)")

    # Deduplicated verification steps, concerns first, then everything else.
    seen = set()
    what_to_verify_next = []
    for f in sorted(findings, key=lambda f: 0 if f["status"] == STATUS_CONCERN else 1):
        if f["next_step"] not in seen:
            seen.add(f["next_step"])
            what_to_verify_next.append(f["next_step"])

    scores = _build_scores(findings)

    # Overall verdict: computed ONLY from categories this toolkit can
    # actually screen. Two kinds of category are excluded, for different
    # reasons:
    #   1. Always-REQUIRES_VERIFICATION categories (easements, setbacks,
    #      subdivision restrictions) -- unknown by design in EVERY report,
    #      so including them would make every parcel look identically
    #      "insufficient data" regardless of how it screens on what this
    #      toolkit CAN check.
    #   2. Permanently proxy-only categories (utilities, nearby
    #      development) -- these can never rise above an indirect signal
    #      no matter how good the actual result is (a real utility hookup
    #      is never confirmed by counting nearby buildings), so scoring
    #      them like a normal risk category would make "PROMISING" an
    #      unreachable verdict for every single parcel, which is its own
    #      kind of dishonesty (a tier nothing can ever earn is as
    #      misleading as a tier everything earns too easily).
    screenable = _screenable_findings(findings)
    concern_count = sum(1 for f in screenable if f["status"] == STATUS_CONCERN)
    unknown_count = sum(1 for f in screenable if f["status"] == STATUS_UNKNOWN)
    caution_count = sum(1 for f in screenable if f["status"] == STATUS_CAUTION)

    if not screenable or unknown_count > len(screenable) / 2:
        overall_verdict = "INSUFFICIENT DATA TO SCREEN"
        overall_explanation = "Too many of the checkable categories returned no usable data to form a meaningful screening verdict."
    elif concern_count > 0:
        overall_verdict = "SIGNIFICANT CONCERNS FOUND"
        overall_explanation = f"{concern_count} categor{'y' if concern_count == 1 else 'ies'} came back as a real concern. Review those before anything else."
    elif caution_count > 0:
        overall_verdict = "MIXED SIGNALS"
        overall_explanation = "No outright red flags, but multiple categories carry real caveats worth resolving before an offer."
    else:
        overall_verdict = "PROMISING SCREENING RESULT"
        overall_explanation = "The categories this toolkit can check came back clean. This is a SCREENING result, not a clearance -- the items in 'what we don't know' still need direct verification."

    return {
        "findings": findings,
        "what_we_know": what_we_know,
        "what_we_think": what_we_think,
        "what_we_dont_know": what_we_dont_know,
        "what_could_kill_the_deal": what_could_kill_the_deal,
        "what_to_verify_next": what_to_verify_next,
        "scores": scores,
        "overall_verdict": overall_verdict,
        "overall_verdict_explanation": overall_explanation,
    }


def _build_scores(findings: list[dict]) -> dict:
    """
    Only the scores that can actually be defended from the data on hand.
    Each is a category-level status derived from its own findings, not a
    single blended number pretending to summarize everything at once.
    """
    by_category = {f["category"]: f for f in findings}

    def status_of(*categories):
        statuses = [by_category[c]["status"] for c in categories if c in by_category]
        if not statuses:
            return STATUS_UNKNOWN
        if STATUS_CONCERN in statuses:
            return STATUS_CONCERN
        if STATUS_CAUTION in statuses:
            return STATUS_CAUTION
        if STATUS_UNKNOWN in statuses:
            return STATUS_UNKNOWN
        return STATUS_CLEAR

    physical = by_category.get("Physical usable area (screening only)")
    return {
        "physical_usable_area_pct": physical["establishes"] if physical else None,
        "access_score": status_of("Physical road access", "Legal road access", "Parcel frontage", "Easements"),
        "utility_score": status_of("Utilities (electric/water/sewer)"),
        "septic_sanitation_risk": status_of("Septic / sanitation"),
        "environmental_risk": status_of("Wetlands", "Flood / floodway", "Environmental restrictions / protected areas", "Environmental contamination (Superfund/Brownfields)"),
        "zoning_risk": status_of("Zoning", "Minimum lot size, setbacks & frontage requirements"),
    }


# --------------------------------------------------------------------------
# Decision engine -- turns the evidence report above into an actual
# recommendation. Deliberately NOT a black-box score: every tier and every
# bullet below is a plain if/else over statuses this toolkit already
# computed and can point back to. The question this whole layer exists to
# answer, in the customer's own words: "does this parcel deserve the next
# 10 minutes of my time?"
# --------------------------------------------------------------------------

DEAL_TIER_COLOR = {
    "Strong": "GREEN",
    "Moderate": "YELLOW",
    "Weak": "YELLOW",
    "Avoid": "RED",
    "Insufficient information": "GRAY",
}


def _insufficient_decision(reason: str) -> dict:
    return {
        "deal_potential": "Insufficient information",
        "color": "GRAY",
        "headline": reason,
        "recommended_action": "Not enough data came back to screen this parcel -- try running the report again, or verify the basics (road access, flood zone, zoning) manually before spending time on it.",
        "top_positive_factors": [],
        "top_risks": [],
        "deal_killer_risks": [],
        "key_unknowns": [],
        "next_steps": ["Re-run the parcel report; if it fails again, verify manually with the county before investing time."],
    }


def build_decision_summary(evidence: dict, buildability: dict, tax_flags: dict) -> dict:
    """
    Pure synthesis over build_evidence_report's already-computed output plus
    two raw buildability fields (nearby-building count, zoning coverage)
    that the evidence report deliberately keeps at a capped CAUTION status
    for evidence-level honesty, but which are still genuine, informative
    signal for a fast go/no-go read -- the same underlying number just
    means something different at this layer ("is this worth 10 minutes?")
    than it does at the evidence layer ("how much can we trust this?").
    Zero new network calls.

    Returns:
        {
            "deal_potential": "Strong" | "Moderate" | "Weak" | "Avoid" | "Insufficient information",
            "color": "GREEN" | "YELLOW" | "RED" | "GRAY",
            "headline": short one-line summary for the search-results list,
            "recommended_action": one sentence,
            "top_positive_factors": [str, ...],
            "top_risks": [str, ...],
            "deal_killer_risks": [str, ...],
            "key_unknowns": [str, ...],   # parcel-specific, not the standing 5 always-unknown categories
            "next_steps": [str, ...],     # top 3, concern-first
        }
    """
    if not evidence or evidence.get("error") or not evidence.get("findings"):
        return _insufficient_decision("Evidence report unavailable for this parcel.")

    findings = evidence["findings"]
    by_cat = {f["category"]: f for f in findings}
    screenable = _screenable_findings(findings)

    if not screenable:
        return _insufficient_decision("No checkable categories returned usable data.")

    concerns = [f for f in screenable if f["status"] == STATUS_CONCERN]
    cautions = [f for f in screenable if f["status"] == STATUS_CAUTION]
    clears = [f for f in screenable if f["status"] == STATUS_CLEAR]
    unknowns = [f for f in screenable if f["status"] == STATUS_UNKNOWN]
    hard_stops = [f for f in concerns if f["category"] in _HARD_STOP_CATEGORIES]

    tax_flagged = bool(tax_flags and tax_flags.get("flagged"))

    # ---- tier -------------------------------------------------------------
    if len(unknowns) > len(screenable) / 2:
        return _insufficient_decision(
            f"{len(unknowns)} of {len(screenable)} checkable categories came back with no usable data."
        )
    elif hard_stops or len(concerns) >= 3:
        tier = "Avoid"
    elif concerns:
        tier = "Weak"
    elif len(cautions) >= 3 or len(unknowns) > len(screenable) / 3:
        tier = "Moderate"
    else:
        tier = "Strong"
    color = DEAL_TIER_COLOR[tier]

    # ---- positive factors (level 1/2 material) -----------------------------
    # The two manually-derived items go first -- they're the most decision-
    # salient signals (matching the user-facing worked example: "existing
    # homes nearby", "electric infrastructure nearby") and shouldn't get
    # crowded out of the top-N by a long run of plainer CLEAR findings.
    positives = []

    nearby = buildability.get("nearby_development") or {}
    count = nearby.get("nearby_building_count")
    if isinstance(count, (int, float)) and count >= 5:
        positives.append(
            f"{int(count)} buildings mapped nearby -- electric/water infrastructure is likely "
            f"present in the area (not a confirmed hookup)."
        )

    zf = by_cat.get("Zoning")
    if zf and zf["evidence_level"] == EVIDENCE_INDICATED:
        positives.append(f"Zoning is on file for this location: {zf['establishes']}")

    positives.extend(f["establishes"] for f in clears)

    # ---- risks (manageable, yellow) -----------------------------------------
    risks = [f"{f['category']}: {f['explanation']}" for f in cautions]
    if isinstance(count, (int, float)) and count == 0:
        risks.append("No buildings found nearby -- utility access is unconfirmed and could require costly extension.")
    if zf and zf["evidence_level"] != EVIDENCE_INDICATED:
        risks.append("Zoning is not covered by this toolkit for this location -- entirely unknown, not confirmed clear.")

    # ---- deal-killer risks (red) ---------------------------------------------
    ordered_concerns = sorted(concerns, key=lambda f: 0 if f["category"] in _HARD_STOP_CATEGORIES else 1)
    deal_killers = [f"{f['category']}: {f['explanation']}" for f in ordered_concerns]
    if tax_flagged:
        lien_ct = tax_flags.get("lien_count")
        deal_killers.append(
            f"Tax/lien flags on file{f' ({lien_ct} lien(s))' if lien_ct else ''} -- may complicate clear "
            f"title at closing. Note: tax delinquency can also indicate a motivated seller, which may "
            f"still make this worth pursuing -- verify the lien situation before assuming either way."
        )

    # ---- unknowns worth naming (parcel-specific, not the standing 5) --------
    key_unknowns = [f["category"] for f in unknowns]

    next_steps = evidence.get("what_to_verify_next", [])[:3]

    # ---- recommended action --------------------------------------------------
    if tier == "Avoid":
        if hard_stops:
            lead = hard_stops[0]
            if lead["category"] == "Physical road access":
                recommended_action = "Do not spend time pursuing this parcel until legal access is verified."
            elif lead["category"] == "Physical usable area (screening only)":
                recommended_action = "Do not spend time pursuing this parcel until the flood/wetland impact on usable area is resolved."
            else:
                recommended_action = f"Do not spend time pursuing this parcel until {lead['category'].lower()} is resolved."
        else:
            names = ", ".join(f["category"] for f in concerns[:3])
            recommended_action = f"Do not spend time pursuing this parcel -- {len(concerns)} independent concerns were found ({names}); resolve or rule out each before any further work."
    elif tier == "Weak":
        lead = concerns[0]["category"]
        recommended_action = f"Proceed cautiously, if at all -- verify {lead.lower()} directly before investing more time or money."
    elif tier == "Moderate":
        lead_cat = (cautions[0]["category"] if cautions else (key_unknowns[0] if key_unknowns else "the open items above"))
        recommended_action = f"Proceed to owner research, but verify {lead_cat.lower()} before making a serious offer."
    else:  # Strong
        still_verify = "legal access and title"
        if zf and zf["evidence_level"] != EVIDENCE_INDICATED:
            still_verify += " and zoning (not covered by this toolkit for this location)"
        recommended_action = f"This parcel screens well on everything this toolkit can check. Proceed to owner research and outreach, but still verify {still_verify} before a serious offer -- no free data source confirms those."

    headline = (
        f"{len(positives)} positive{'s' if len(positives) != 1 else ''}, "
        f"{len(deal_killers)} deal-killer{'s' if len(deal_killers) != 1 else ''}, "
        f"{len(risks)} risk{'s' if len(risks) != 1 else ''}, "
        f"{len(key_unknowns)} unknown{'s' if len(key_unknowns) != 1 else ''}"
    )

    return {
        "deal_potential": tier,
        "color": color,
        "headline": headline,
        "recommended_action": recommended_action,
        "top_positive_factors": positives[:5],
        "top_risks": risks[:5],
        "deal_killer_risks": deal_killers[:5],
        "key_unknowns": key_unknowns[:5],
        "next_steps": next_steps,
    }


# --------------------------------------------------------------------------
# Buyer fit + Owner Outreach Brief -- the CONTACT OWNER / OFFER-NEGOTIATE
# steps of the wholesaling workflow this toolkit is built around:
#   DISCOVER -> SCREEN -> PRIORITIZE -> RESEARCH -> CONTACT OWNER ->
#   DUE DILIGENCE -> OFFER/NEGOTIATE
# Both functions are pure synthesis over data already computed above --
# zero new network calls -- and both deliberately stop short of two
# things this toolkit has no real basis to provide: a suggested offer
# or market price (there is no comp-sale data pipeline here, only links
# to go check comps yourself -- see build_listing_search_links), and
# anything resembling legal or contract advice. Every wholesaler-facing
# string from these two functions is expected to carry, or sit next to,
# the same standing disclaimer: this is a screening/research aid, not a
# substitute for title, survey, county, engineering, environmental,
# septic, or zoning verification by a licensed professional.
# --------------------------------------------------------------------------

OUTREACH_DISCLAIMER = (
    "Generated from public/government data and search-time signals only -- not verified with "
    "the owner, the county, a title company, or any licensed professional. Confirm anything "
    "material (access, taxes, condition, zoning) before making an offer or signing anything."
)

def build_buyer_fit_tags(acres: float | None, buildability: dict, evidence: dict) -> list[dict]:
    """
    Tags a parcel with which kind of buyer it's likely to fit -- a
    wholesaler typically works several buyer lists at once (owner-
    builders, land bankers, recreational buyers, infill developers) and
    needs to know fast which list a given parcel belongs on. A parcel
    can match more than one tag; every tag names the concrete number(s)
    behind it, never a vibe.
    """
    tags: list[dict] = []
    buildability = buildability or {}
    nearby = buildability.get("nearby_development") or {}
    building_count = nearby.get("nearby_building_count")
    by_cat = {f["category"]: f for f in (evidence or {}).get("findings", [])}
    physical = by_cat.get("Physical usable area (screening only)")
    road = by_cat.get("Physical road access")

    if acres is not None and acres <= 3:
        bits = [f"{acres:.2f} acres (single home-site scale)"]
        if road and road["status"] == STATUS_CLEAR:
            bits.append("mapped road access")
        if isinstance(building_count, (int, float)) and building_count >= 5:
            bits.append(f"{int(building_count)} buildings nearby")
        tags.append({"tag": "Owner-builder / small residential lot buyers", "reason": "; ".join(bits) + "."})

    if acres is not None and acres >= 10:
        tags.append({
            "tag": "Land banking / hold-for-appreciation buyers",
            "reason": f"{acres:.2f} acres -- large raw acreage is often bought to hold, independent of near-term buildability.",
        })
    elif acres is not None and 3 < acres < 10:
        rural = isinstance(building_count, (int, float)) and building_count < 5
        tags.append({
            "tag": "Recreational / rural acreage buyers",
            "reason": f"{acres:.2f} acres, " + ("low nearby building density (rural context)." if rural else "moderate nearby development."),
        })

    if isinstance(building_count, (int, float)) and building_count >= 15:
        tags.append({
            "tag": "Infill / near-town buyers",
            "reason": f"{int(building_count)} buildings mapped within 400m -- an established, built-up area.",
        })

    if physical and physical["status"] == STATUS_CONCERN:
        tags.append({
            "tag": "Discount / as-is opportunistic buyers only",
            "reason": f"{physical['establishes']} Likely a harder sell to a buyer planning to build soon.",
        })

    if not tags:
        tags.append({"tag": "Not enough data yet for a buyer-fit read", "reason": "Acreage or screening data is missing for this parcel."})
    return tags


# Maps an evidence finding (category + the status that should trigger it)
# to a plain, conversational question a wholesaler can actually ask an
# owner on the phone -- turning "verify X" (aimed at the wholesaler) into
# "ask the owner about X" (aimed at the conversation). Every question
# traces back to a specific, real finding; nothing here is invented.
_OWNER_QUESTION_MAP = [
    ("Physical road access", STATUS_CONCERN, "How do you currently get to the property? No mapped road was found in this toolkit's data, so it's worth asking directly."),
    ("Septic / sanitation", STATUS_CONCERN, "Has a septic system, well, or perc test ever been done on the property?"),
    ("Septic / sanitation", STATUS_CAUTION, "Has a septic system, well, or perc test ever been done on the property?"),
    ("Wetlands", STATUS_CAUTION, "Are you aware of any standing water, wet areas, or drainage issues on the property?"),
    ("Flood / floodway", STATUS_CONCERN, "Has the property ever flooded, or do you carry flood insurance on it?"),
    ("Zoning", STATUS_UNKNOWN, "Do you know the current zoning, or has anyone ever tried to get a building permit on it?"),
    ("Slope / terrain", STATUS_CONCERN, "Is the lot fairly flat, or does it have a noticeable slope or grade change?"),
]


def build_owner_outreach_brief(
    current_owner: dict, owner_mailing: dict, tax_flags: dict, instant_priority: dict,
    decision: dict, evidence: dict, acres: float | None,
    property_city: str | None, property_state: str | None,
) -> dict:
    """
    Synthesizes everything already computed into a short "what to know
    before you call" brief -- the CONTACT OWNER step of the workflow.
    Not a CRM: nothing here is saved, logged, or tracked across calls;
    it's regenerated fresh from the parcel's own data every time.
    """
    property_summary = (
        f"{acres:.2f} acres in {property_city or 'an unknown city'}, {property_state or '?'}"
        if acres else f"Parcel in {property_city or 'an unknown city'}, {property_state or '?'}"
    )

    owner_name = (current_owner or {}).get("owner_name") or "Unknown owner name"
    owner_type = (current_owner or {}).get("owner_type")
    years = (current_owner or {}).get("ownership_years")
    owner_bits = [owner_name]
    if owner_type and owner_type != "unknown":
        owner_bits.append(f"({owner_type})")
    if isinstance(years, (int, float)):
        owner_bits.append(f"-- owned {years:.0f} years")
    owner_summary = " ".join(owner_bits)

    mail_city = (owner_mailing or {}).get("mail_city")
    mail_state = (owner_mailing or {}).get("mail_state")
    absentee_note = None
    if mail_state and property_state and mail_state.upper() != property_state.upper():
        absentee_note = f"Mailing address is out of state ({mail_city}, {mail_state}) -- likely does not live at or near the property."
    elif mail_city and property_city and mail_city.upper() != property_city.upper():
        absentee_note = f"Mailing address ({mail_city}) is a different city than the property."

    why_reaching_out = list((instant_priority or {}).get("reasons") or [])
    if decision:
        why_reaching_out += list(decision.get("top_positive_factors") or [])[:2]

    by_cat = {f["category"]: f for f in (evidence or {}).get("findings", [])}
    questions: list[str] = []
    for cat, trigger_status, question in _OWNER_QUESTION_MAP:
        f = by_cat.get(cat)
        if f and f["status"] == trigger_status and question not in questions:
            questions.append(question)
    if tax_flags and tax_flags.get("flagged"):
        questions.append("Are there any back taxes or liens on the property, and what's the plan to resolve them?")
    questions.append("Are you the sole owner, or does anyone else need to sign off on a sale?")
    if not questions:
        questions.append("No specific red flags to probe -- confirm general condition, access, and that they're the decision-maker on a sale.")

    caution_notes = []
    if decision:
        caution_notes = list(decision.get("deal_killer_risks") or [])[:3]
        if not caution_notes:
            caution_notes = list(decision.get("top_risks") or [])[:3]

    return {
        "property_summary": property_summary,
        "owner_summary": owner_summary,
        "absentee_note": absentee_note,
        "why_reaching_out": why_reaching_out[:5],
        "questions_to_ask": questions[:6],
        "caution_notes": caution_notes,
        "disclaimer": OUTREACH_DISCLAIMER,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Search for vacant land parcels via a licensed property API.")
    parser.add_argument("--min-acres", type=float, required=True)
    parser.add_argument("--max-acres", type=float, required=True)
    parser.add_argument("--state", dest="target_state", required=True, help="Two-letter state code, e.g. TX")
    parser.add_argument("--provider", choices=["regrid", "attom"], default=None)
    parser.add_argument("--no-persist", action="store_true", help="Skip writing results to PostGIS")
    args = parser.parse_args()

    try:
        results = search_vacant_land_parcels(
            min_acres=args.min_acres,
            max_acres=args.max_acres,
            target_state=args.target_state,
            provider=args.provider,
            persist=not args.no_persist,
        )
    except Exception:
        log.exception("Search failed")
        return 1

    for r in results:
        print(f"{r['apn']}\t{r['lat']}\t{r['lon']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())


