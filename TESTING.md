# Testing strategy

The goal of this suite is not code coverage. The goal is **confidence that
the application gives customers correct conclusions** -- specifically,
that it never turns "we don't know" into a confident-sounding answer.
Everything here is organized around that goal first, and around the
usual categories (search, filters, mapping, etc.) second.

## Run everything

```
python run_tests.py            # everything
python run_tests.py --fast      # skip the Playwright browser tests (no browser binary needed)
```

One-time setup for the full suite:
```
pip install -r requirements-dev.txt
playwright install chromium
```

`requirements-dev.txt` (pytest, playwright, Pillow) is separate from
`requirements.txt` on purpose -- the deployed app on Render never needs
any of these, only a developer/CI machine running the test suite does.

## Why two suites instead of one

**16 legacy `test_*.py` scripts at the repo root** (`test_vacant_land.py`,
`test_realie.py`, `test_security_audit_fixes.py`, etc.) predate this
testing phase. Each is a plain, directly-executable script: assertions +
print statements, run top-to-bottom, `python test_x.py`. They already
cover a large amount of real, previously-verified ground (buildability,
zoning, tax delinquency across 8 real government sources, the no-shared-
key guarantee, the security-audit fixes, Overpass retry/backoff, etc.)
and were deliberately **left as-is rather than mass-migrated into
pytest** -- rewriting ~1,730 lines of already-verified assertions carries
real risk of silently changing what's being checked, for a refactor with
no functional payoff. They're still run automatically by `run_tests.py`.

**`tests/` is a real pytest suite**, used for everything built or
identified as a gap during this testing phase: dangerous-false-conclusion
tests, conflicting-data tests, backend contract tests (customer
isolation, key-leak sweeps, rate limiting on the 2 previously-untested
endpoints), and real-browser frontend e2e tests (filters, sorting, CSV
export, print rendering, the mobile-vs-desktop layout split). New tests
going forward should go here, using pytest's fixtures/parametrization/
per-test isolation rather than another top-to-bottom script.

`tests/fixtures.py` holds representative parcel-analysis fixtures used
across multiple test files: a complete/clean buildability result, a
total-API-outage result (every optional source is `None`), and a few
named single-category variants for the specific dangerous-conclusion
scenarios below.

## The most important file: `tests/test_dangerous_false_conclusions.py`

This is the one that matters most. It locks in, as executable tests, the
exact failure modes this project exists to prevent:

| Dangerous conclusion | Test |
|---|---|
| "API returned nothing" read as "no wetlands" | `test_missing_wetland_and_flood_data_is_never_reported_as_clear` |
| Road adjacency known -> "legal access confirmed" | `test_known_road_adjacency_never_implies_legal_access_confirmed` |
| Utility proximity known -> "utilities available" | `test_known_utility_proximity_never_implies_service_available` |
| Large parcel + unknown zoning -> "definitely buildable" | `test_large_parcel_with_uncovered_zoning_is_never_definitively_buildable` |
| API failure treated as a genuine negative result | `test_septic_source_unavailable_vs_no_coverage_vs_genuine_result_are_all_distinct`, `test_zoning_source_unavailable_vs_not_covered_vs_found_are_all_distinct`, `test_contamination_*` |
| Total data outage forced into a confident "Avoid" | `test_total_data_outage_produces_insufficient_information_not_avoid` |

`tests/test_conflicting_data.py` covers the adjacent case: two real
signals genuinely disagreeing (e.g. a residential zoning code over a
rural 0-nearby-buildings signal). The framework has no cross-category
"smoothing" logic anywhere, so the right behavior is: report both
honestly, never silently resolve the conflict in one direction, never
crash, never hide an inconvenient caveat (e.g. a septic rating based on a
tiny sampled-coverage percentage).

## Coverage map, against the categories this phase was scoped around

| Category | Where it's tested |
|---|---|
| Search | `test_realie.py` (mocked), `tests/test_backend_contracts.py` (customer isolation, key handling), `tests/test_frontend_e2e.py` (validation errors) |
| Filters | `tests/test_frontend_e2e.py` (real browser, real client-side logic -- the only place this was ever tested at all before this phase) |
| Parcel retrieval | `test_realie.py`, `tests/test_backend_contracts.py::test_full_parcel_report_generation_end_to_end` |
| Parcel analysis / scoring / buildability / access / utility / wetlands / flood / slope / septic | `test_evidence_framework.py`, `test_decision_engine.py`, `test_comprehensive.py`, `test_contamination_wiring.py`, `test_new_gov_checks.py`, and now `tests/test_dangerous_false_conclusions.py` + `tests/test_conflicting_data.py` for the honesty guarantees specifically |
| Mapping | `tests/test_frontend_e2e.py` (mobile Map tab, marker click -> drawer), Leaflet rendering itself is a third-party library and out of scope |
| API failures / missing data / conflicting data | The two dedicated new test files above -- previously the least-tested category in the whole project |
| Tax data | `test_tax_flags.py`, `test_tax_delinquency_expansion.py` (8 real government sources), `tests/test_conflicting_data.py` (internal-consistency invariant) |
| Skip tracing | `test_tracerfy.py` (mocked; disclosed as mocked-only in its own docstring -- no real paid key available in this environment), `tests/test_backend_contracts.py` (cross-customer key isolation) |
| CSV exports | `tests/test_frontend_e2e.py::test_csv_export_neutralizes_formula_injection_and_has_correct_columns` -- exercises the REAL shipped `downloadCsv()`/`csvEscape()` via a captured browser download, not a reimplementation |
| Report generation | `tests/test_backend_contracts.py::test_full_parcel_report_generation_end_to_end`, `tests/test_frontend_e2e.py::test_print_view_...` |
| Authentication | **Not applicable -- there is no authentication system yet.** This is a known, explicit pre-launch gap (see the security audit notes), not silently missing. No vacuous "auth doesn't exist" test was written; this gap needs a real design pass before real accounts/billing, not a test. |
| Customer isolation | `tests/test_backend_contracts.py::test_two_customers_parcel_reports_never_cross`, `test_two_customers_skiptrace_keys_never_cross` |
| API key handling | `test_no_shared_key_usage.py` (no server-side fallback ever used), `tests/test_backend_contracts.py` (a key never echoes back in any response, success or error) |
| Rate limiting | `test_security_audit_fixes.py` (parcel report endpoint), `tests/test_backend_contracts.py` (the 2 endpoints -- score/bulk, delinquency -- that had no dedicated test before this phase) |
| Error handling | Threaded through every file above rather than one dedicated file -- "what happens when this specific thing fails" is answered next to the thing itself |

## A real bug this test suite found and fixed

Building `tests/test_frontend_e2e.py` surfaced a genuine, previously-
undetected production defect: the PWA's `controllerchange` auto-reload
handler (added when the service worker first shipped) fired not only on
a real update, but on **every brand-new visitor's very first page load**
-- `clients.claim()` finishing for the first time ever also fires
`controllerchange`, and the handler couldn't tell the two cases apart.
Every first-time visitor got a silent, unintended extra page reload
moments after loading. Fixed in `webapp/templates/index.html` by
recording whether a controller already existed at load time and only
reloading when a real transition (old -> new) happens. This is exactly
the kind of thing the testing strategy is FOR: not just checking things
work, but building real end-to-end coverage that can catch something
manual testing (even a real Galaxy S22 Ultra) wouldn't likely notice
because the effect (a single extra reload within the first second) is
easy to miss.

## What could not be tested, and why

- **A real, paid Realie or Tracerfy API call.** No credentials exist in
  this environment (confirmed: nothing in the shell env, no `.env`
  file). Every test touching these two integrations either mocks the
  network layer or, where a live check is safe and useful, hits the real
  endpoint with a deliberately invalid key to confirm reachability/error-
  handling without spending real money (see `test_overpass.py`'s live
  smoke check and the live reachability checks performed in a prior
  session, documented in project memory).
- **A physical Android/iOS device.** The Playwright e2e tests use
  headless Chromium with emulated mobile viewports/touch, confirmed
  against the real production URL in a prior session and against a real
  Galaxy S22 Ultra by the user directly -- but this automated suite
  itself only runs the emulated version.
- **Actually installing the PWA to a home screen** and confirming the OS
  install prompt appears -- needs a real device/Chrome, not headless
  Chromium.
- **Leaflet's own map rendering correctness** (tile loading, pan/zoom
  physics) -- that's third-party library behavior, not this
  application's own logic, and out of scope for this suite.
- **Load/stress testing** (concurrent users, sustained traffic) -- this
  suite tests correctness, not capacity; no load-testing tool is set up.

## Adding a new test

1. If it's about a NEW dangerous-conclusion risk (a new data source, a
   new proxy signal), it belongs in `tests/test_dangerous_false_conclusions.py`
   -- ask specifically: "if this data is missing, is a customer told
   that honestly? If this data is present but limited, does the finding
   say what it doesn't prove?"
2. If it's a new backend route/contract, it belongs in
   `tests/test_backend_contracts.py`, using the `client` fixture
   (werkzeug test client, no real browser, no real network).
3. If it's client-side JS behavior (filters, sorting, export, layout),
   it belongs in `tests/test_frontend_e2e.py`, using the
   `live_server_url` fixture (a real subprocess) + Playwright. Keep these
   free of real network calls to Realie/Tracerfy/government APIs --
   inject fake `currentResults` directly via `page.evaluate()` instead of
   driving a real search.
4. Add any new representative parcel shape to `tests/fixtures.py` rather
   than inlining it, if more than one test file will want it.
