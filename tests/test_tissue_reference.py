"""The tissue table gives the simulation each ROI's water T1/T2 and MT pool with its source, or
says plainly that it does not know."""
from __future__ import annotations

from cest_scout.skills import tissue_reference
from cest_scout.skills.tissue_reference import TissueEntry, describe, known_tissues, lookup


def _e(tissue, param, field, value, verified=True, mtype="human in vivo", aliases=(), pmid="1"):
    return TissueEntry(tissue=tissue, aliases=list(aliases), parameter=param, field_T=field, value=value,
                       species="human", measurement_type=mtype, conditions="", pmid=pmid, title="t", quote="q",
                       abstract_verified=verified)


ENTRIES = (
    _e("nucleus pulposus", "T1", 3.0, 1.3, aliases=["NP"], pmid="11"),
    _e("nucleus pulposus", "T2", 3.0, 0.120, pmid="11"),
    _e("nucleus pulposus", "T2", 1.5, 0.140, pmid="12"),
    _e("nucleus pulposus", "T2", 3.0, 0.100, verified=False, pmid="13"),   # table value: outranked
    _e("liver", "T1", 3.0, 0.81, verified=False, pmid="14990831"),
    _e("liver", "T2", 3.0, 0.034, verified=False, pmid="14990831"),
    _e("liver", "MT_fraction", None, 0.065, verified=False, mtype="ex vivo tissue", pmid="16086319"),
)


def test_lookup_matches_roi_names_containing_the_tissue_and_prefers_abstract_verified_values():
    found = lookup("nucleus pulposus of the L4/L5 disc", 3.0, entries=ENTRIES)
    assert found.tissue == "nucleus pulposus"
    assert found.overrides["water_t1_s"] == 1.3
    assert found.overrides["water_t2_s"] == 0.120          # the abstract-verified 3 T value, not the table value
    assert found.any_abstract_verified and "PMID 11" in found.provenance
    assert "not in the table for nucleus pulposus -- generic defaults used" in found.provenance  # MT pool
    assert lookup("NP", 3.0, entries=ENTRIES) is not None                  # alias
    assert lookup("annulus fibrosus", 3.0, entries=ENTRIES) is None        # no entries at all
    # A tissue with only a T2: the T2 is used and the T1 is the stated generic default.
    t2_only = lookup("cartilage", 3.0, entries=ENTRIES + (_e("cartilage", "T2", 3.0, 0.040, pmid="99"),))
    assert t2_only.overrides["water_t2_s"] == 0.040 and t2_only.overrides["water_t1_s"] == 1.3
    assert "T1 unknown for cartilage" in t2_only.provenance


def test_lookup_interpolates_and_labels_table_values():
    np_2t = lookup("nucleus pulposus", 2.25, entries=ENTRIES)
    assert 0.120 < np_2t.overrides["water_t2_s"] < 0.140 and "interpolation" in np_2t.provenance
    assert next(r for r in np_2t.rows if r["parameter"] == "water T2")["verified"] == "estimated"
    liver = lookup("liver parenchyma", 3.0, entries=ENTRIES)
    assert liver.overrides["mt_fraction_of_water"] == 0.065
    assert not liver.any_abstract_verified and "not abstract-verified" in liver.provenance


def test_describe_says_dont_know_for_an_unknown_tissue():
    text = describe("mars colony", 3.0, entries=ENTRIES)
    assert "no verified water T1/T2" in text and "generic defaults" in text and "do not know" in text
    assert known_tissues(ENTRIES) == ["liver", "nucleus pulposus"]


def test_shipped_table_keeps_legacy_organs_labelled_and_prefers_verified_values():
    tissue_reference.load_entries.cache_clear()
    entries = tissue_reference.load_entries()
    legacy = [e for e in entries if e.pmid == "14990831" and not e.abstract_verified]
    assert legacy, "the carried-over table values must stay in the file, labelled"
    liver = lookup("liver", 3.0)
    assert liver is not None and liver.any_abstract_verified
    assert set(known_tissues()) >= {"liver", "renal cortex", "spleen", "paraspinal muscle"}


def test_relaxation_times_are_scaled_with_field_never_clamped():
    """A 3 T value must not be reused unchanged at 0.5 or 7 T: T1 rises with B0 (power law), T2
    stays about flat, and the row says the value is an estimate and which rule made it."""
    tissue_reference.table_exponent.cache_clear()
    entries = ENTRIES + (
        _e("liver", "T1", 1.5, 0.586, verified=False, pmid="14990831"),   # liver now spans 1.5-3 T
        _e("liver", "T1", 7.0, 1.20, pmid="77"),
    )
    lo = lookup("liver", 0.5, entries=entries)
    hi = lookup("nucleus pulposus", 7.0, entries=entries)
    assert lo.overrides["water_t1_s"] < 0.586                     # below the 1.5 T value, not clamped to it
    assert hi.overrides["water_t1_s"] > 1.3                       # above the 3 T value
    t1_row = next(r for r in hi.rows if r["parameter"] == "water T1")
    assert t1_row["verified"] == "estimated" and "scaled from 3 T" in t1_row["note"] and "6482839" in t1_row["note"]
    # T2 exponent is small: 7 T T2 within a factor ~1.5 of the 3 T value
    assert 0.6 * 0.120 < hi.overrides["water_t2_s"] < 1.5 * 0.120
    # a measured field is returned exactly, unlabelled
    exact = lookup("liver", 7.0, entries=entries)
    assert exact.overrides["water_t1_s"] == 1.20
    assert next(r for r in exact.rows if r["parameter"] == "water T1")["verified"] == "abstract"
    # the generic defaults scale too
    d05 = tissue_reference.default_rows(0.5); d3 = tissue_reference.default_rows(3.0)
    assert float(d05[0]["value"].split()[0]) < float(d3[0]["value"].split()[0]) and "scaled from 3 T" in d05[0]["note"]
    b, how = tissue_reference.table_exponent("T1", entries)
    assert 0.1 <= b <= 0.6 and "dispersions" in how
    tissue_reference.table_exponent.cache_clear()


def test_builder_canonicalizes_heart_tissue_names():
    from cest_scout.skills.build_tissue_reference import canonical_tissue
    for text in ("left ventricular myocardium (healthy)", "myocardial septum", "heart", "cardiac muscle"):
        name, aliases = canonical_tissue(text)
        assert name == "myocardium" and "heart" in aliases


def test_fat_entries_and_fat_rois_only_match_each_other():
    entries = ENTRIES + (_e("breast fibroglandular tissue", "T1", 3.0, 1.68, aliases=["breast"], pmid="20882630"),
                         _e("breast adipose tissue", "T1", 3.0, 0.423, pmid="20882630"))
    assert lookup("breast", 3.0, entries=entries).overrides["water_t1_s"] == 1.68
    assert lookup("breast adipose tissue", 3.0, entries=entries).overrides["water_t1_s"] == 0.423
