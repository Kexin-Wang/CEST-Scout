"""Tests for the reference-data grounding skill.

The HMDB index tests are fully offline -- they read the committed
cest_scout/data/hmdb_concentrations.json, which is the point of that index existing. The one
BioNumbers test hits the live site and skips on network failure, matching tests/test_pubmed_live.py.
"""
import pytest

from cest_scout.skills import reference_data


def test_index_lookup_returns_records_with_citations():
    # Taurine (HMDB0000251) is one of the metabolites this project actually cares about for liver
    # CEST, and HMDB carries quantified normal blood values for it.
    result = reference_data.lookup_concentrations("taurine")

    assert result["found"] is True
    assert result["matched_name"].lower() == "taurine"
    assert result["accession"] == "HMDB0000251"
    assert result["records"]

    for record in result["records"]:
        assert record["value"]
        assert record["biospecimen"]
        # The whole reason this module exists: every number carries its source.
        assert record["reference_text"]

    # At least one record must be citable all the way back to PubMed, otherwise the number is no
    # better grounded than the LLM's recall was.
    assert any(r["pubmed_id"] for r in result["records"])


def test_index_lookup_resolves_informal_names_through_synonyms():
    """HMDB's primary names are the formal ones ("D-Glucose", "L-Lactic acid"), but the agents
    talk about "glucose" and "lactate". Synonym matching is what makes the index usable at all.
    """
    glucose = reference_data.lookup_concentrations("glucose")
    assert glucose["found"] is True
    assert glucose["matched_name"] == "D-Glucose"
    assert glucose["accession"] == "HMDB0000122"

    # An agent will sometimes have only an alias list; that is the escape hatch, as in
    # pubchem.verify_structure.
    lactate = reference_data.lookup_concentrations("NotAMetabolite", aliases=["lactate"])
    assert lactate["found"] is True
    assert lactate["matched_query"] == "lactate"
    assert lactate["matched_name"] == "L-Lactic acid"
    assert lactate["accession"] == "HMDB0000190"


def test_index_contains_no_solid_tissue_biospecimens():
    """A guard on the central limitation of this source. If a future HMDB release ever did add a
    solid-tissue biospecimen, this test failing is the signal to revisit
    lookup_tissue_concentration rather than keep scraping BioNumbers for it.
    """
    taurine = reference_data.lookup_concentrations("taurine")
    specimens = {r["biospecimen"].lower() for r in taurine["records"]}
    assert specimens
    assert not specimens & {"liver", "muscle", "brain", "kidney", "heart"}


def test_index_lookup_misses_cleanly_on_a_nonsense_name():
    result = reference_data.lookup_concentrations("zzzzz-not-a-real-metabolite-qqq")

    assert result["found"] is False
    assert result["matched_name"] is None
    assert result["records"] == []


def test_noisy_search_hits_are_filtered_out():
    """The filter, not the network. BioNumbers' keyword search returns 'Plasma glucose
    concentration' near the top for the query 'glucose liver'; returning that as a liver value
    would be exactly the kind of fabrication this module exists to prevent.
    """
    plasma = {"title": "Plasma glucose concentration", "value": "3 to 9", "organism": "Human"}
    liver = {"title": "Concentration of Glucose in Liver", "value": "9860-10200", "organism": "Rat"}
    unrelated = {"title": "Apparent penetration of glucose-6-P through microsomal membrane",
                 "value": "1.2", "organism": "Rat"}
    table_only = {"title": "Metabolite concentrations in human and rat liver",
                  "value": "Excel Table - link", "organism": "Mammals"}

    assert reference_data._is_plausible_tissue_hit(liver, "glucose", "liver") is True
    assert reference_data._is_plausible_tissue_hit(plasma, "glucose", "liver") is False
    assert reference_data._is_plausible_tissue_hit(unrelated, "glucose", "liver") is False
    assert reference_data._is_plausible_tissue_hit(table_only, "glucose", "liver") is False


def test_live_bionumbers_liver_glucose_lookup():
    """Live network test against BioNumbers; skipped automatically if unreachable."""
    try:
        result = reference_data.lookup_tissue_concentration("glucose", "liver")
    except Exception as exc:  # network unavailable in this environment
        pytest.skip(f"BioNumbers unreachable: {exc}")

    assert result["found"] is True
    assert result["records"]

    record = result["records"][0]
    assert record["bnid"]
    assert record["organism"]
    assert record["reference"]
    # BNID 101265 is the canonical rat-liver glucose record and should survive the filter.
    assert any(r["bnid"] == "101265" for r in result["records"])
