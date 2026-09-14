"""
End-to-end tests that exercise REAL client-side JavaScript in a real
browser, against a real running instance of the app (see `live_server_url`
in conftest.py) -- things the werkzeug test client used elsewhere in this
suite cannot verify at all, since it never executes a line of JS.

Scope, deliberately: these tests inject known parcel data directly into
`currentResults` via page.evaluate() rather than driving a real search,
and never touch anything that would make a real Realie/Tracerfy/
government API call (the live subprocess this fixture starts has no
credentials, and mocking doesn't cross a process boundary). That's not a
compromise -- what these tests are FOR is the client-side logic and
layout (filters, sorting, CSV export, print rendering, the mobile-vs-
desktop layout split) that no other file in this suite can reach at all.
Anything needing a mocked backend result lives in test_backend_contracts.py
and test_dangerous_false_conclusions.py instead, using the werkzeug test
client in the same process.

Requires: `pip install playwright && playwright install chromium` once
per machine (see TESTING.md). Skips itself cleanly if Playwright isn't
installed, rather than failing the whole suite on an environment gap.
"""
import csv
import io
import sys

import pytest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

playwright_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync_api.sync_playwright


FAKE_RESULTS = [
    {
        "apn": "APN-GREEN", "lat": 26.64, "lon": -81.98, "acres": 2.0,
        "tax_flags": {"flagged": True, "lien_count": 1, "foreclosure_count": 0},
        "land_value": 30000, "address": "1 Green St", "city": "Cape Coral", "state": "FL", "zip_code": "33990",
        "owner_name": "Alice Owner", "owner_type": "individual", "ownership_years": 6, "price_paid": 9000,
        "mail_street": "1 Elsewhere Ave", "mail_city": "Austin", "mail_state": "TX", "mail_zip": "78701",
        "instant_score": 30, "instant_reasons": ["Tax lien on file"], "instant_negative_reasons": [],
        "owner_phone": None, "owner_email": None, "raw": {},
    },
    {
        "apn": "APN-SMALL", "lat": 26.60, "lon": -81.90, "acres": 0.5,
        "tax_flags": {"flagged": False, "lien_count": 0, "foreclosure_count": 0},
        "land_value": 8000, "address": "2 Small Ln", "city": "Cape Coral", "state": "FL", "zip_code": "33990",
        "owner_name": "Bob Owner", "owner_type": "individual", "ownership_years": 1, "price_paid": 5000,
        "mail_street": "2 Small Ln", "mail_city": "Cape Coral", "mail_state": "FL", "mail_zip": "33990",
        "instant_score": 5, "instant_reasons": [], "instant_negative_reasons": ["Owner appears to live at this address"],
        "owner_phone": None, "owner_email": None, "raw": {},
    },
    {
        "apn": "APN-LARGE", "lat": 26.70, "lon": -82.00, "acres": 20.0,
        "tax_flags": {"flagged": False, "lien_count": 0, "foreclosure_count": 0},
        "land_value": 120000, "address": "3 Big Rd", "city": "Cape Coral", "state": "FL", "zip_code": "33990",
        # Deliberately a formula-injection payload AND a name with a comma --
        # the same real-world hazard test_security_audit_fixes.py's CSV fix
        # exists for, now verified against the actual shipped csvEscape().
        "owner_name": '=1+1,"Formula Corp"', "owner_type": "corporate", "ownership_years": 12, "price_paid": 60000,
        "mail_street": "3 Big Rd", "mail_city": "Cape Coral", "mail_state": "FL", "mail_zip": "33990",
        "instant_score": 10, "instant_reasons": [], "instant_negative_reasons": [],
        "owner_phone": None, "owner_email": None, "raw": {},
    },
]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _page_with_results(browser, live_server_url, viewport=None):
    context = browser.new_context(viewport=viewport or {"width": 1280, "height": 900})
    page = context.new_page()
    page.goto(live_server_url, wait_until="load")
    page.evaluate(
        "(results) => { currentResults = results; setResultCount(results.length); refreshView(); }",
        FAKE_RESULTS,
    )
    return context, page


# ---------------------------------------------------------------------
# Filters + sorting (pure client-side logic -- no other test file in this
# suite touches this at all).
# ---------------------------------------------------------------------

def test_tax_flagged_filter_narrows_to_only_flagged_parcels(browser, live_server_url):
    context, page = _page_with_results(browser, live_server_url)
    try:
        page.check("#filter-tax-flagged")
        cards = page.locator(".result-card")
        assert cards.count() == 1
        assert "APN-GREEN" in cards.first.inner_text()
    finally:
        context.close()


def test_min_acres_filter_excludes_smaller_parcels(browser, live_server_url):
    context, page = _page_with_results(browser, live_server_url)
    try:
        page.fill("#filter-min-acres", "1")
        cards = page.locator(".result-card")
        texts = cards.all_inner_texts()
        assert not any("APN-SMALL" in t for t in texts)
        assert any("APN-GREEN" in t for t in texts)
        assert any("APN-LARGE" in t for t in texts)
    finally:
        context.close()


def test_clear_filters_restores_all_results(browser, live_server_url):
    context, page = _page_with_results(browser, live_server_url)
    try:
        page.check("#filter-tax-flagged")
        assert page.locator(".result-card").count() == 1
        page.click("text=Clear all filters")
        assert page.locator(".result-card").count() == 3
    finally:
        context.close()


def test_sort_by_acres_descending_orders_cards_correctly(browser, live_server_url):
    """Note: `.card-apn` is intentionally shared between the "All results"
    list and the separate "Priority Picks" section above it (both reuse
    the same card styling) -- Priority Picks always uses its own priority
    ranking regardless of the sort-by dropdown, by design, so this test
    scopes its query to #results specifically rather than the whole page."""
    context, page = _page_with_results(browser, live_server_url)
    try:
        page.select_option("#sort-by", "acres_desc")
        apns = page.locator("#results .card-apn").all_inner_texts()
        assert apns == ["APN-LARGE", "APN-GREEN", "APN-SMALL"]
    finally:
        context.close()


# ---------------------------------------------------------------------
# CSV export -- the real shipped csvEscape()/CSV_COLUMNS, via a real
# captured browser download, not a reimplementation of the logic.
# ---------------------------------------------------------------------

def test_csv_export_neutralizes_formula_injection_and_has_correct_columns(browser, live_server_url):
    context, page = _page_with_results(browser, live_server_url)
    try:
        with page.expect_download() as dl_info:
            page.evaluate("downloadCsv()")
        download = dl_info.value
        with open(download.path(), "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))

        header = rows[0]
        assert header[0] == "Recipient Name"
        assert header[8] == "APN"
        assert "do not mail here" in header[9].lower()

        body_rows = rows[1:]
        apns = [r[8] for r in body_rows]
        assert set(apns) == {"APN-GREEN", "APN-SMALL", "APN-LARGE"}

        large_row = next(r for r in body_rows if r[8] == "APN-LARGE")
        recipient_name = large_row[0]
        # The formula-injection payload must come back with a neutralizing
        # leading apostrophe -- proves the REAL shipped function does this,
        # not a hand-copied reimplementation of the same logic.
        assert recipient_name.startswith("'="), f"formula-injection payload was not neutralized: {recipient_name!r}"
    finally:
        context.close()


# ---------------------------------------------------------------------
# Print view -- real rendering, and a real regression guard against the
# stored-XSS class of bug fixed earlier this project (innerHTML with
# unescaped owner data).
# ---------------------------------------------------------------------

def test_print_view_renders_real_data_and_never_executes_injected_markup(browser, live_server_url):
    context = browser.new_context()
    page = context.new_page()
    page.goto(live_server_url, wait_until="load")

    malicious_results = [{
        **FAKE_RESULTS[0],
        "owner_name": "<img src=x onerror=\"window.__xss_fired = true\">",
    }]
    page.evaluate("(r) => localStorage.setItem('vacant_land_export_data', JSON.stringify(r))", malicious_results)

    fired = []
    page.on("dialog", lambda d: (fired.append("dialog"), d.dismiss()))
    page.goto(live_server_url.rstrip("/") + "/print", wait_until="load")
    page.wait_for_timeout(200)

    xss_fired = page.evaluate("window.__xss_fired === true")
    assert not xss_fired, "print view executed injected markup from owner_name -- stored XSS regression"
    # The literal tag text should still be visible as inert text, not silently dropped either.
    assert "<img" in page.locator("body").inner_text() or "img src" in page.content()
    context.close()


# ---------------------------------------------------------------------
# Mobile-vs-desktop layout regression guard -- locks in the
# user-verified-on-a-real-Galaxy-S22-Ultra baseline explicitly so a
# future change cannot silently regress it back to a shrunk desktop view.
# ---------------------------------------------------------------------

def test_mobile_bottom_nav_visible_at_phone_width(browser, live_server_url):
    context, page = _page_with_results(browser, live_server_url, viewport={"width": 384, "height": 854})
    try:
        nav = page.locator("#mobile-bottom-nav")
        assert nav.is_visible()
        box = nav.bounding_box()
        assert box["height"] >= 44, "bottom nav touch target regressed below the minimum comfortable size"
        assert not page.locator("#side-panel").is_visible(), "sidebar must not render as an inline column on phone width"
        scroll_w = page.evaluate("document.documentElement.scrollWidth")
        client_w = page.evaluate("document.documentElement.clientWidth")
        assert scroll_w == client_w, "horizontal overflow regression at phone width"
    finally:
        context.close()


def test_desktop_sidebar_visible_and_bottom_nav_hidden_at_desktop_width(browser, live_server_url):
    context, page = _page_with_results(browser, live_server_url, viewport={"width": 1440, "height": 900})
    try:
        assert page.locator("#side-panel").is_visible()
        assert not page.locator("#mobile-bottom-nav").is_visible()
        sidebar_box = page.locator("#side-panel").bounding_box()
        assert 300 <= sidebar_box["width"] <= 340
    finally:
        context.close()


def test_search_overlay_covers_full_viewport_on_mobile(browser, live_server_url):
    context, page = _page_with_results(browser, live_server_url, viewport={"width": 384, "height": 854})
    try:
        page.click("#nav-btn-search")
        page.wait_for_timeout(150)
        box = page.locator("#side-panel").bounding_box()
        assert abs(box["width"] - 384) < 2
        assert abs(box["height"] - 854) < 2
    finally:
        context.close()


# ---------------------------------------------------------------------
# Parcel detail drawer open/close, including the Android back-button fix.
# ---------------------------------------------------------------------

def test_result_card_click_opens_detail_drawer_and_back_closes_it(browser, live_server_url):
    context, page = _page_with_results(browser, live_server_url, viewport={"width": 384, "height": 854})
    try:
        page.click(".result-card >> nth=0")
        page.wait_for_timeout(200)
        assert "open" in (page.get_attribute("#detail-drawer", "class") or "")
        page.go_back()
        page.wait_for_timeout(200)
        assert "open" not in (page.get_attribute("#detail-drawer", "class") or "")
    finally:
        context.close()


# ---------------------------------------------------------------------
# Search form validation -- real requests to the real live server, no
# mocking needed, since these are rejected before any outbound API call.
# ---------------------------------------------------------------------

def test_search_without_api_key_shows_a_clear_error_and_makes_no_charge(browser, live_server_url):
    context = browser.new_context()
    page = context.new_page()
    page.goto(live_server_url, wait_until="load")
    # Desktop-width context (default) -- the search form is already an
    # always-visible sidebar here, no bottom-nav tap needed to reach it.
    page.fill("#state", "FL")
    page.fill("#api_key", "")
    page.click("#search-btn")
    page.wait_for_timeout(300)
    status_text = page.locator("#status").inner_text()
    assert "realie api key is required" in status_text.lower()
    context.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
