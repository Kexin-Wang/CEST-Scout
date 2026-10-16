"""Live network test against the real NCBI E-utilities API. Requires network
access and a valid NCBI_EMAIL in .env; skipped automatically if unreachable.
"""
import pytest

from cest_scout.skills import pubmed


def test_search_pubmed_returns_results_and_mesh_flags():
    try:
        results = pubmed.search_pubmed("creatine concentration in human brain", max_results=5)
    except Exception as exc:  # network unavailable in this environment
        pytest.skip(f"PubMed unreachable: {exc}")

    assert len(results) > 0
    r = results[0]
    assert r["pmid"]
    assert r["title"]
    assert isinstance(r["is_human_evidence"], bool)
    assert isinstance(r["is_animal_evidence"], bool)


def test_verify_titles_accepts_real_title():
    real_title = "Comparison of Calcium and Barium Microcapsules as Scaffolds in the Development of Artificial Dermal Papillae."
    try:
        results = pubmed.verify_titles([("27123456", real_title)])
    except Exception as exc:  # network unavailable in this environment
        pytest.skip(f"PubMed unreachable: {exc}")

    assert results["27123456"]["verified"] is True


def test_verify_titles_rejects_fabricated_title():
    # PMID 27123456 is a real PubMed record, but its actual title has nothing to do with CEST/
    # liver metabolites -- a real fabrication observed from a local Qwen3-32B run cited it as a
    # liver aspartate MRS paper. verify_titles must reject that claim and surface the real title.
    fabricated_title = "Metabolic profiling of aspartate in murine liver using 1H MRS"
    try:
        results = pubmed.verify_titles([("27123456", fabricated_title)])
    except Exception as exc:  # network unavailable in this environment
        pytest.skip(f"PubMed unreachable: {exc}")

    assert results["27123456"]["verified"] is False
    assert "Microcapsules" in results["27123456"]["real_title"]
