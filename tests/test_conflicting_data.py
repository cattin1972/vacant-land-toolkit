"""
Tests for what happens when two real signals disagree, or when a value
carries its own internal caveat (e.g. a rating based on a tiny sampled
coverage percentage). The framework has no cross-category "smoothing"
logic anywhere -- each category's finding is independent -- so the right
behavior under conflicting input is: never silently resolve the conflict
in one direction, never crash, and never hide the caveat that makes the
data conflicting in the first place.
"""
import sys

import pytest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
import vacant_land_search as v
from fixtures import (
    CONFLICTING_TAX_FLAGS,
    MINIMAL_MAILING,
    MINIMAL_TAX_FLAGS_CLEAN,
    COMPLETE_CLEAN_BUILDABILITY,
    ZONING_NOT_COVERED,
)


def by_category(evidence):
    return {f["category"]: f for f in evidence["findings"]}


# ---------------------------------------------------------------------
# 1. get_tax_flags()'s own real output can never be internally
#    inconsistent -- "flagged" is derived FROM the same counts, not a
#    separately-set value that could drift out of sync with them.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("record,expected_flagged,expected_liens,expected_forecl", [
    ({}, False, 0, 0),
    ({"liens": []}, False, 0, 0),
    ({"liens": None}, False, 0, 0),
    ({"liens": [{"amount": 500}]}, True, 1, 0),
    ({"foreclosures": [{"date": "2020-01-01"}]}, True, 0, 1),
    ({"liens": [{"amount": 500}], "foreclosures": [{"date": "2020-01-01"}]}, True, 1, 1),
])
def test_get_tax_flags_flagged_is_always_derived_from_its_own_counts(record, expected_flagged, expected_liens, expected_forecl):
    result = v.get_tax_flags(record)
    assert result["flagged"] == expected_flagged
    assert result["lien_count"] == expected_liens
    assert result["foreclosure_count"] == expected_forecl
    # The invariant this whole test file exists to lock in: flagged can
    # never disagree with the counts it's supposedly summarizing.
    assert result["flagged"] == (result["lien_count"] > 0 or result["foreclosure_count"] > 0)


def test_decision_engine_tolerates_a_hypothetically_inconsistent_tax_flags_dict():
    """Defensive test: if tax_flags ever arrived already-inconsistent
    (flagged=True but both counts zero -- impossible from the real
    get_tax_flags today, but a future refactor or a hand-built caller
    could produce it), the decision engine must not crash, and must not
    silently trust one field over the other in a way that hides the
    conflict -- it keys off `flagged`, so a customer still sees the
    tax/lien caveat surfaced rather than the inconsistency being erased."""
    evidence = v.build_evidence_report(COMPLETE_CLEAN_BUILDABILITY, ZONING_NOT_COVERED, CONFLICTING_TAX_FLAGS, MINIMAL_MAILING)
    decision = v.build_decision_summary(evidence, COMPLETE_CLEAN_BUILDABILITY, CONFLICTING_TAX_FLAGS)
    # Must not crash (implicit -- an exception here would fail the test),
    # and the flag must still surface somewhere in the customer-facing
    # decision rather than silently vanishing because the counts disagree.
    all_text = " ".join(decision.get("deal_killer_risks") or []).lower()
    assert "tax" in all_text or "lien" in all_text


# ---------------------------------------------------------------------
# 2. Categories never smooth over each other. A rural building-count
#    signal and a covered/indicated zoning signal can genuinely disagree
#    (e.g. a freshly-platted residential subdivision with 0 built
#    structures yet) -- both must be reported independently and honestly,
#    neither adjusted or suppressed because of the other.
# ---------------------------------------------------------------------

def test_zero_nearby_buildings_and_residential_zoning_are_each_reported_independently():
    buildability = {**COMPLETE_CLEAN_BUILDABILITY, "nearby_development": {"nearby_building_count": 0, "likely_utilities_nearby": False}}
    zoning = {"zoning_code": "RS-1 (Single-Family Residential)", "source": "King County zoning GIS"}
    evidence = v.build_evidence_report(buildability, zoning, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    cats = by_category(evidence)

    utilities = cats["Utilities (electric/water/sewer)"]
    zoning_finding = cats["Zoning"]

    # The utilities finding reflects ONLY the building-density proxy...
    assert "0 buildings" in utilities["establishes"]
    # ...and is not silently upgraded to CLEAR just because zoning says
    # this is a residential district (zoning doesn't confirm utilities,
    # and the code has no path that would let it).
    assert utilities["status"] in (v.STATUS_CAUTION, v.STATUS_CONCERN, v.STATUS_UNKNOWN)

    # The zoning finding reflects ONLY the zoning code on file...
    assert "RS-1" in zoning_finding["establishes"]
    # ...and is not downgraded/hedged just because the building-density
    # proxy nearby happens to be zero -- the two categories are independent.
    assert zoning_finding["evidence_level"] == v.EVIDENCE_INDICATED


def test_decision_engine_surfaces_both_disagreeing_signals_as_separate_items():
    """When one proxy looks bad (0 nearby buildings -> a risk/caution
    line) and another looks fine (zoning indicated -> can count as a
    positive), the decision summary must list both independently rather
    than netting them against each other into a single blended verdict
    with no traceable reasoning."""
    buildability = {**COMPLETE_CLEAN_BUILDABILITY, "nearby_development": {"nearby_building_count": 0, "likely_utilities_nearby": False}}
    zoning = {"zoning_code": "RS-1 (Single-Family Residential)", "source": "King County zoning GIS"}
    evidence = v.build_evidence_report(buildability, zoning, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    decision = v.build_decision_summary(evidence, buildability, MINIMAL_TAX_FLAGS_CLEAN)

    positives_text = " ".join(decision["top_positive_factors"]).lower()
    risks_text = " ".join(decision["top_risks"]).lower()
    assert "zoning is on file" in positives_text or "rs-1" in positives_text
    assert "no buildings found nearby" in risks_text


# ---------------------------------------------------------------------
# 3. A rating built from a tiny sampled-coverage percentage must never
#    hide that caveat just because the rating itself sounds definitive.
# ---------------------------------------------------------------------

def test_low_coverage_septic_sample_still_discloses_its_own_coverage_percentage():
    buildability = {**COMPLETE_CLEAN_BUILDABILITY, "septic_suitability": {"rating": "Not limited", "soil_name": "Sparse Sample Soil", "coverage_pct": 4}}
    evidence = v.build_evidence_report(buildability, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    septic = by_category(evidence)["Septic / sanitation"]
    # The confident-sounding rating is shown...
    assert "Not limited" in septic["establishes"]
    # ...but so is the inconvenient small coverage number that qualifies it --
    # never silently dropped because it undercuts the headline rating.
    assert "4%" in septic["explanation"]
    assert "not a site-specific test" in septic["does_not_establish"].lower() or "not a site-specific test" in septic["limitation"].lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
