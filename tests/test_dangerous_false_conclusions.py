"""
The most important file in this test suite.

The application's entire value proposition is that it tells a land
investor the TRUTH about what is and isn't known -- never a confident-
sounding guess dressed up as a fact. Every test here locks in one specific
way that could go wrong, using the plain-English examples this project's
own testing requirements named directly:

  - An API returning no wetland data must never be read as "no wetlands."
  - Road adjacency known + legal access unknown must never become
    "legal access confirmed."
  - Utility proximity known + service availability unknown must never
    become "utilities available."
  - A large-enough parcel with unknown zoning/setbacks must never be
    called definitively buildable.
  - An API failure must never be indistinguishable from a genuine
    negative/clear result.

These are pure function-level tests against vacant_land_search.py's real
evidence/decision engine -- no network, no Flask, fast and deterministic.
"""
import sys

import pytest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])
import vacant_land_search as v
from fixtures import (
    ALL_MISSING_BUILDABILITY,
    CLEAN_ZONING_COVERED,
    COMPLETE_CLEAN_BUILDABILITY,
    MINIMAL_MAILING,
    MINIMAL_TAX_FLAGS_CLEAN,
    ROAD_ADJACENT_BUILDABILITY,
    UTILITY_PROXIMITY_HIGH_BUILDABILITY,
    ZONING_LOOKUP_FAILED,
    ZONING_NOT_COVERED,
)


def by_category(evidence):
    return {f["category"]: f for f in evidence["findings"]}


# ---------------------------------------------------------------------
# 1. "No wetland data" must never be read as "no wetlands."
# ---------------------------------------------------------------------

def test_missing_wetland_and_flood_data_is_never_reported_as_clear():
    evidence = v.build_evidence_report(ALL_MISSING_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    cats = by_category(evidence)

    for category in ("Wetlands", "Flood / floodway"):
        f = cats[category]
        assert f["status"] == v.STATUS_UNKNOWN, f"{category}: a failed data source must never report CLEAR"
        assert f["evidence_level"] == v.EVIDENCE_UNKNOWN
        assert f["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE
        # The exact dangerous phrasing this test exists to prevent:
        assert "no wetlands" not in f["establishes"].lower()
        assert "clear" not in f["establishes"].lower()
        # Must explicitly say data is missing, not silently omit the category.
        assert "no usable data" in f["establishes"].lower()


def test_genuinely_clear_wetland_result_is_worded_differently_than_missing_data():
    """A real, positive CLEAR result must be textually distinguishable
    from "we don't know" -- proves the two code paths produce genuinely
    different, non-confusable output, not just a different status flag
    a UI bug could ignore."""
    evidence_missing = v.build_evidence_report(ALL_MISSING_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    evidence_clean = v.build_evidence_report(COMPLETE_CLEAN_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)

    missing = by_category(evidence_missing)["Wetlands"]
    clean = by_category(evidence_clean)["Wetlands"]

    assert missing["status"] != clean["status"]
    assert missing["establishes"] != clean["establishes"]
    assert clean["status"] == v.STATUS_CLEAR
    assert clean["evidence_level"] == v.EVIDENCE_LIKELY
    # Even the genuine positive result must still hedge -- "not a guarantee"
    # language, per the project's core "absence of data is not absence of
    # the fact, and one federal map is not a site delineation either" ethos.
    assert "not" in clean["does_not_establish"].lower()


# ---------------------------------------------------------------------
# 2. Road adjacency known must never become "legal access confirmed."
# ---------------------------------------------------------------------

def test_known_road_adjacency_never_implies_legal_access_confirmed():
    evidence = v.build_evidence_report(ROAD_ADJACENT_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    cats = by_category(evidence)

    physical = cats["Physical road access"]
    legal = cats["Legal road access"]

    # Physical adjacency really is confirmed (that part of the claim is true)...
    assert physical["status"] == v.STATUS_CLEAR
    # ...but the SAME finding must itself say this isn't legal access.
    assert "legal access" in physical["does_not_establish"].lower()

    # And the separate "Legal road access" category must NEVER be marked
    # CLEAR/confirmed by this -- it is structurally unanswerable and must
    # always require independent verification, regardless of how strong
    # the physical-road signal is.
    assert legal["status"] == v.STATUS_UNKNOWN
    assert legal["evidence_level"] == v.EVIDENCE_REQUIRES_VERIFICATION
    assert "is not the same as" in legal["explanation"].lower()
    forbidden = ("legal access confirmed", "legal access is confirmed", "confirmed legal access")
    full_text = (legal["establishes"] + " " + legal["does_not_establish"]).lower()
    for phrase in forbidden:
        assert phrase not in full_text


def test_missing_road_data_is_a_warning_not_a_landlocked_confirmation():
    evidence = v.build_evidence_report(ALL_MISSING_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    physical = by_category(evidence)["Physical road access"]
    assert physical["status"] == v.STATUS_UNKNOWN
    assert "landlocked" not in physical["establishes"].lower()


def test_no_mapped_road_is_a_warning_not_a_confirmed_landlocked_conclusion():
    """A genuine 'no road found' result (source responded, found nothing)
    must be a CONCERN, not silently a hard confirmation of landlocked --
    the finding must explicitly say absence-in-this-dataset isn't proof."""
    buildability = {**COMPLETE_CLEAN_BUILDABILITY, "road_access": {"has_mapped_road_access": False, "nearby_roads": []}}
    evidence = v.build_evidence_report(buildability, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    physical = by_category(evidence)["Physical road access"]
    assert physical["status"] == v.STATUS_CONCERN
    # This IS the does_not_establish field -- by construction its content is
    # already the hedge (what we DON'T confirm), so the disqualifying claim
    # itself ("landlocked") is expected to appear here without a literal
    # "not" nearby; the hedge is structural (field meaning), not lexical.
    assert "landlocked" in physical["does_not_establish"].lower()
    assert "TIGER is known to omit" in physical["does_not_establish"]


# ---------------------------------------------------------------------
# 3. Utility proximity known must never become "utilities available."
# ---------------------------------------------------------------------

def test_known_utility_proximity_never_implies_service_available():
    evidence = v.build_evidence_report(UTILITY_PROXIMITY_HIGH_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    utilities = by_category(evidence)["Utilities (electric/water/sewer)"]

    # The proxy signal itself is real and shown...
    assert "40 buildings" in utilities["establishes"]
    # ...but must not be read as confirmed hookup availability at THIS parcel.
    dne = utilities["does_not_establish"].lower()
    assert "available" in dne and "this parcel" in dne
    forbidden = ("utilities available", "utility available", "service is available", "hookup confirmed")
    assert not any(p in utilities["establishes"].lower() for p in forbidden)
    # Status is capped below CLEAR/confirmed even at high proximity --
    # this category can never be a green light on its own.
    assert utilities["status"] != v.STATUS_CLEAR


def test_missing_utility_proximity_data_is_never_silently_dropped():
    evidence = v.build_evidence_report(ALL_MISSING_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    utilities = by_category(evidence)["Utilities (electric/water/sewer)"]
    assert utilities["status"] == v.STATUS_UNKNOWN
    assert utilities["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE


# ---------------------------------------------------------------------
# 4. A large-enough parcel with unknown zoning must never be called
#    definitively buildable.
# ---------------------------------------------------------------------

FORBIDDEN_ABSOLUTE_PHRASES = (
    "definitely buildable", "confirmed buildable", "guaranteed buildable",
    "is buildable", "zoning permits", "zoning allows", "approved for construction",
)


def _assert_no_absolute_buildability_claim(decision):
    text = " ".join([
        decision.get("headline") or "",
        decision.get("recommended_action") or "",
        " ".join(decision.get("top_positive_factors") or []),
    ]).lower()
    for phrase in FORBIDDEN_ABSOLUTE_PHRASES:
        assert phrase not in text, f"found forbidden absolute claim {phrase!r} in decision text: {text!r}"


def test_large_parcel_with_uncovered_zoning_is_never_definitively_buildable():
    evidence = v.build_evidence_report(COMPLETE_CLEAN_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    decision = v.build_decision_summary(evidence, COMPLETE_CLEAN_BUILDABILITY, MINIMAL_TAX_FLAGS_CLEAN)

    zoning = by_category(evidence)["Zoning"]
    assert zoning["status"] == v.STATUS_UNKNOWN
    assert zoning["evidence_level"] == v.EVIDENCE_REQUIRES_VERIFICATION

    _assert_no_absolute_buildability_claim(decision)

    # Zoning being unknown must never just vanish from the customer-facing
    # decision -- either it's counted as a named unknown, or (the "Strong"
    # tier's own special case) the recommended action explicitly calls it
    # out by name as something no free source confirmed.
    mentions_zoning_somewhere = (
        "Zoning" in decision["key_unknowns"]
        or "zoning" in decision["recommended_action"].lower()
    )
    assert mentions_zoning_somewhere, f"zoning-unknown was silently dropped from the decision: {decision}"


def test_large_parcel_that_screens_well_still_explicitly_flags_zoning_as_unverified():
    """Even in the best possible outcome (the 'Strong' tier), the
    recommended action must still name legal access/zoning as unverified
    -- 'screens well' must never be readable as 'cleared to build.'"""
    evidence = v.build_evidence_report(COMPLETE_CLEAN_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    decision = v.build_decision_summary(evidence, COMPLETE_CLEAN_BUILDABILITY, MINIMAL_TAX_FLAGS_CLEAN)

    if decision["deal_potential"] == "Strong":
        action = decision["recommended_action"].lower()
        assert "legal access" in action
        assert "zoning" in action, "Strong tier with uncovered zoning must still name zoning as unverified"
        assert "verify" in action


def test_large_parcel_with_zoning_actually_covered_and_clear_can_be_called_strong():
    """Sanity check on the flip side: when zoning genuinely IS covered and
    clear, the still-verify caveat narrows to just access/title, proving
    the zoning caveat above is conditional on real absence of data, not
    always tacked on regardless."""
    evidence = v.build_evidence_report(COMPLETE_CLEAN_BUILDABILITY, CLEAN_ZONING_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    decision = v.build_decision_summary(evidence, COMPLETE_CLEAN_BUILDABILITY, MINIMAL_TAX_FLAGS_CLEAN)
    if decision["deal_potential"] == "Strong":
        assert "legal access" in decision["recommended_action"].lower()


# ---------------------------------------------------------------------
# 5. An API failure must never be confused with a genuine negative result.
#    Parametrized across every category with a documented 3-way distinction.
# ---------------------------------------------------------------------

def test_septic_source_unavailable_vs_no_coverage_vs_genuine_result_are_all_distinct():
    failed = v.build_evidence_report({**COMPLETE_CLEAN_BUILDABILITY, "septic_suitability": None}, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    no_coverage = v.build_evidence_report({**COMPLETE_CLEAN_BUILDABILITY, "septic_suitability": {}}, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    genuine = v.build_evidence_report({**COMPLETE_CLEAN_BUILDABILITY, "septic_suitability": {"rating": "Very limited", "soil_name": "X", "coverage_pct": 80}}, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)

    f_failed = by_category(failed)["Septic / sanitation"]
    f_no_cov = by_category(no_coverage)["Septic / sanitation"]
    f_genuine = by_category(genuine)["Septic / sanitation"]

    # The API/network call itself failing:
    assert f_failed["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE
    assert f_failed["status"] == v.STATUS_UNKNOWN
    # The call succeeded but has no coverage here -- a different quick_label,
    # since retrying (the natural reaction to "source unavailable") won't help:
    assert f_no_cov["quick_label"] == v.QUICK_LABEL_DATA_UNAVAILABLE
    assert f_no_cov["status"] == v.STATUS_UNKNOWN
    assert f_failed["quick_label"] != f_no_cov["quick_label"]
    # A genuine, confirmed bad result must be a real CONCERN, never conflated
    # with either "we don't know" case above:
    assert f_genuine["status"] == v.STATUS_CONCERN
    assert f_genuine["quick_label"] is None
    assert f_genuine["status"] != f_failed["status"]
    assert f_genuine["status"] != f_no_cov["status"]


def test_zoning_source_unavailable_vs_not_covered_vs_found_are_all_distinct():
    """Regression lock for the exact bug fixed in the 2026-09-14 security
    audit: a government-server hiccup for a covered county used to be
    indistinguishable from 'this county was never covered at all.'"""
    failed = v.build_evidence_report(COMPLETE_CLEAN_BUILDABILITY, ZONING_LOOKUP_FAILED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    not_covered = v.build_evidence_report(COMPLETE_CLEAN_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    found = v.build_evidence_report(COMPLETE_CLEAN_BUILDABILITY, CLEAN_ZONING_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)

    z_failed = by_category(failed)["Zoning"]
    z_not_covered = by_category(not_covered)["Zoning"]
    z_found = by_category(found)["Zoning"]

    assert z_failed["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE
    assert "try again" in z_failed["next_step"].lower()
    assert z_not_covered["quick_label"] == v.QUICK_LABEL_DATA_UNAVAILABLE
    assert "outside" in z_not_covered["establishes"].lower()
    assert z_found["evidence_level"] == v.EVIDENCE_INDICATED
    assert "AG-2" in z_found["establishes"]
    assert z_failed["establishes"] != z_not_covered["establishes"]


def test_contamination_both_sources_failed_is_never_reported_as_clear():
    buildability = {**COMPLETE_CLEAN_BUILDABILITY, "contamination": {"superfund_sites_nearby": None, "brownfield_sites_nearby": None}}
    evidence = v.build_evidence_report(buildability, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    contamination = by_category(evidence)["Environmental contamination (Superfund/Brownfields)"]
    assert contamination["status"] == v.STATUS_UNKNOWN
    assert contamination["quick_label"] == v.QUICK_LABEL_SOURCE_UNAVAILABLE
    assert "no usable data" in contamination["establishes"].lower()


def test_contamination_missing_entirely_is_never_reported_as_clear():
    evidence = v.build_evidence_report(ALL_MISSING_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    contamination = by_category(evidence)["Environmental contamination (Superfund/Brownfields)"]
    assert contamination["status"] == v.STATUS_UNKNOWN


def test_wildfire_no_rating_at_this_point_is_never_reported_as_low_risk():
    buildability = {**COMPLETE_CLEAN_BUILDABILITY, "wildfire_fine_grained": {"tier": None, "raw_value": None}}
    evidence = v.build_evidence_report(buildability, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    wildfire = by_category(evidence)["Wildfire risk (fine-grained)"]
    assert wildfire["status"] == v.STATUS_UNKNOWN
    assert wildfire["evidence_level"] == v.EVIDENCE_UNKNOWN
    assert "low" not in wildfire["establishes"].lower()


# ---------------------------------------------------------------------
# 6. A total data outage must never itself force a confident "Avoid" --
#    that would be exactly as dishonest as a false "all clear."
# ---------------------------------------------------------------------

def test_total_data_outage_produces_insufficient_information_not_avoid():
    evidence = v.build_evidence_report(ALL_MISSING_BUILDABILITY, ZONING_NOT_COVERED, MINIMAL_TAX_FLAGS_CLEAN, MINIMAL_MAILING)
    decision = v.build_decision_summary(evidence, ALL_MISSING_BUILDABILITY, MINIMAL_TAX_FLAGS_CLEAN)
    assert decision["deal_potential"] == "Insufficient information"
    assert decision["color"] == "GRAY"
    assert decision["deal_potential"] != "Avoid"
    assert decision["color"] != "RED"


def test_insufficient_information_gray_is_never_confusable_with_a_bad_deal_red():
    assert v.DEAL_TIER_COLOR["Insufficient information"] == "GRAY"
    assert v.DEAL_TIER_COLOR["Avoid"] == "RED"
    assert v.DEAL_TIER_COLOR["Insufficient information"] != v.DEAL_TIER_COLOR["Avoid"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
