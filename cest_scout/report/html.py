"""Self-contained HTML report renderer for a CEST_Scout FinalReport.

Plots and structure diagrams are embedded as base64 data URIs in one .html file (no server, no
sidecar asset files), so it opens standalone in a browser after an HPC run or a single scp.
"""
from __future__ import annotations

import html as _html
import re

from cest_scout.pipeline import CONTRAST_THRESHOLD_PERCENT, chemist_shift_note, detectability_of, verdict_label
from cest_scout.report.plots import render_zspectra_png_base64
from cest_scout.report.prose import MAX_BLOCK_WORDS, bullets, word_count
from cest_scout.report.sequence_plots import (
    render_optimization_landscape_png_base64,
    render_roi_contrast_png_base64,
    render_saturation_scheme_png_base64,
)
from cest_scout.report.structures import polymer_units, render_polymer_units_png_base64, render_structure_png_base64
from cest_scout.schemas import Citation, FinalMetabolite, FinalReport, InVivoVerificationPlan, RunProvenance

_STYLE = """
<style>
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; max-width: 980px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.5; }
nav.toc { font-size: .82rem; line-height: 1.35; background: #f6f6f4; border: 1px solid #ddd; border-radius: 8px; padding: .6rem .8rem; margin: 0 0 1rem 0; }
nav.toc ol { margin: .3rem 0 0 1.1rem; padding: 0; }
nav.toc li { margin: .15rem 0; }
nav.toc a { color: #06c; text-decoration: none; }
nav.toc .t-yes { color: #0072B2; } nav.toc .t-no { color: #D55E00; } nav.toc .t-unk { color: #E69F00; }
@media (min-width: 1360px) {
  body { margin-left: 300px; }
  nav.toc { position: fixed; top: 1rem; left: 1rem; width: 250px; max-height: calc(100vh - 2rem); overflow-y: auto; }
}
h2 { scroll-margin-top: .5rem; }
h1 { border-bottom: 2px solid #333; padding-bottom: .3rem; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid #ccc; padding-bottom: .2rem; }
.card { background: #fafafa; border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.25rem; margin: .75rem 0; }
.tag { display: inline-block; background: #eef; border-radius: 4px; padding: 0 .4rem; font-size: .8rem; margin-left: .3rem; color: #335; }
.animal-tag { background: #fee; color: #833; }
details.quote { margin: .3rem 0 .6rem 0; }
details.quote summary { cursor: pointer; color: #06c; font-size: .9rem; }
details.quote .quote-body { background: #fffbe6; border-left: 3px solid #e0c000; padding: .5rem .75rem; margin-top: .3rem; font-style: italic; }
details.popup summary { cursor: pointer; color: #06c; font-weight: 600; }
button.plot-toggle { cursor: pointer; background: #2a6; color: white; border: none; border-radius: 5px; padding: .4rem .9rem; font-size: .9rem; }
button.plot-toggle:hover { background: #184; }
.plot-wrap { display: none; margin-top: .75rem; }
.plot-wrap img { max-width: 100%; }
.structure-wrap img { max-width: 100%; }
table.citations { border-collapse: collapse; width: 100%; font-size: .9rem; }
table.citations td, table.citations th { border: 1px solid #ddd; padding: .3rem .5rem; text-align: left; }
a.pmid-link { text-decoration: none; }
/* Okabe-Ito colour-blind-safe palette; every coloured verdict also carries a text mark. */
.badge-yes { color: #0072B2; font-weight: 600; }
.badge-no { color: #D55E00; font-weight: 600; }
.badge-unknown { color: #E69F00; font-weight: 600; }
ul.prose { margin: .2rem 0 .6rem 1.2rem; padding: 0; }
ul.prose li { margin: .15rem 0; font-size: .93rem; }
table.params { border-collapse: collapse; width: 100%; font-size: .82rem; }
table.params td, table.params th { border: 1px solid #ddd; padding: .2rem .4rem; text-align: left; vertical-align: top; }
table.params th { background: #f3f3f3; }
a.src { text-decoration: none; } a.src sup { color: #666; font-size: .7em; }
.units { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: .6rem; }
.units figure { margin: 0; min-width: 0; }
.units figure img { width: 100%; height: auto; display: block; }
.units figcaption { font-size: .78rem; color: #444; overflow-wrap: anywhere; }
.structure-wrap img { max-width: 440px; width: 100%; height: auto; }
.v-abstract { color: #009E73; } .v-table { color: #E69F00; } .v-default { color: #D55E00; } .v-estimated { color: #CC79A7; font-style: italic; }
.v-abstract::after { content: " (a)"; font-size: .75em; } .v-table::after { content: " (t)"; font-size: .75em; }
.v-default::after { content: " (d)"; font-size: .75em; } .v-estimated::after { content: " (e)"; font-size: .75em; }
ul.overlap-warnings li { color: #D55E00; }
p.verify-note { background: #fff4f4; border-left: 3px solid #D55E00; padding: .5rem .75rem; font-size: .9rem; }
p.est-note { background: #f4f8ff; border-left: 3px solid #0072B2; padding: .5rem .75rem; font-size: .9rem; }
</style>
<script>
function togglePlot(id) {
  var el = document.getElementById(id);
  el.style.display = (el.style.display === 'block') ? 'none' : 'block';
}
</script>
"""


def _esc(s) -> str:
    return _html.escape(str(s), quote=True)


def _prose(label: str, text: str) -> str:
    """A labelled free-text field. Short text is one paragraph; anything longer is a list of
    short bullets, and a very long field is collapsed behind its first bullet, so the report
    never shows a block of prose longer than MAX_BLOCK_WORDS words."""
    if not text:
        return ""
    if word_count(text) <= 80:
        return f"<p><strong>{_fmt(label)}:</strong> {_fmt(text)}</p>"
    items = bullets(text)
    body = "<ul class='prose'>" + "".join(f"<li>{_fmt(b)}</li>" for b in items) + "</ul>"
    if word_count(text) > MAX_BLOCK_WORDS:
        return (f"<details class='popup'><summary>{_fmt(label)} ({len(items)} points, {word_count(text)} words)"
                f" &mdash; <span style='font-weight:400;color:#444;'>{_fmt(items[0][:160])}&hellip;</span></summary>{body}</details>")
    return f"<p><strong>{_fmt(label)}:</strong></p>{body}"


_UNIT_SUBS = (
    (re.compile(r"\bs\^-1\b"), "s<sup>&minus;1</sup>"), (re.compile(r"\bk_ex\b"), "k<sub>ex</sub>"),
    (re.compile(r"\bB1_rms\b"), "B<sub>1,rms</sub>"), (re.compile(r"\bB([01])\b"), r"B<sub>\1</sub>"),
    (re.compile(r"\bT2b\b"), "T<sub>2b</sub>"), (re.compile(r"\bT([12])\b"), r"T<sub>\1</sub>"),
    (re.compile(r"\bTsat\b"), "T<sub>sat</sub>"), (re.compile(r"\buT\b"), "&micro;T"),
    (re.compile(r"(\d) ?x ?(\d)"), r"\1&times;\2"),
)


def _fmt(s) -> str:
    """Escape, then typeset units: s^-1 -> s<sup>-1</sup>, k_ex -> k<sub>ex</sub>, B0/B1/T1/T2 and
    Tsat subscripts, uT -> µT. For text content only (never attribute values)."""
    text = _esc(s)
    for pat, rep in _UNIT_SUBS:
        text = pat.sub(rep, text)
    return text


def _badge(m: FinalMetabolite) -> str:
    label = verdict_label(m)
    if label.startswith("detectable"):
        cls, mark = "badge-yes", "&#10003; "
    elif label.startswith("unknown"):
        cls, mark = "badge-unknown", "? "
    else:
        cls, mark = "badge-no", "&#10007; "
    return f"<span class='{cls}'>{mark}{_esc(label)}</span>"


def _tissue_parameters_block(rows: list) -> str:
    """The water/MT parameters used, as one compact table per tissue: target tissue and each
    verification ROI at 3 T shown; the other field strengths behind a toggle."""
    if not rows:
        return ""
    params = ["water T1", "water T2", "MT fraction", "MT k_ex", "MT T2b"]

    sources: list[str] = []          # numbered source list under the tables; values link to it

    def ref(r) -> int:
        text = r.source or "no source"
        if text not in sources:
            sources.append(text)
        return sources.index(text) + 1

    def table(subset, caption):
        by_key: dict[tuple, dict] = {}
        for r in subset:
            by_key.setdefault((r.role, r.tissue, r.field_T), {})[r.parameter] = r
        head = "".join(f"<th>{_fmt(p)}</th>" for p in params)
        out = [f"<table class='params'><thead><tr><th>Tissue</th><th>Role</th><th>Field</th>{head}</tr></thead><tbody>"]
        for (role, tissue, field), d in by_key.items():
            cells = []
            for pname in params:
                r = d.get(pname)
                if r is None:
                    cells.append("<td>&mdash;</td>")
                    continue
                k = ref(r)
                cells.append(f"<td><a class='src' href='#tsrc-{k}'><span class='v-{_esc(r.verified)}'>{_esc(r.value)}</span>"
                             f"<sup>[{k}]</sup></a></td>")
            out.append(f"<tr><td>{_fmt(tissue)}</td><td>{_fmt(role)}</td><td>{field:g} T</td>{''.join(cells)}</tr>")
        out.append("</tbody></table>")
        return f"<p style='font-size:.85rem;margin:.4rem 0 .1rem;'><strong>{caption}</strong></p>" + "".join(out)

    main = [r for r in rows if abs(r.field_T - 3.0) < 1e-6]
    other = [r for r in rows if abs(r.field_T - 3.0) >= 1e-6]
    parts = ["<div class='card' id='tissue'>", "<h3 style='margin-top:0;'>Tissue parameters used in the simulations</h3>",
             "<p style='font-size:.85rem;color:#444;margin:.2rem 0;'>Colour = provenance: <span class='v-abstract'>abstract-verified</span>, "
             "<span class='v-table'>paper table value (not abstract-verified)</span>, <span class='v-default'>generic default (tissue not in the table)</span>, "
             "<span class='v-estimated'>estimated by field scaling</span> (no measurement at that field: T1 &prop; B0<sup>b</sup> with the tissue's own or the "
             "table-median exponent, T2 nearly field-independent; power-law form per PMID 6482839). Click a value to jump to its source.</p>",
             table(main, "At 3 T (target tissue and verification-scan ROIs)")]
    if other:
        parts.append("<details class='popup'><summary>Target tissue at the other swept field strengths</summary>"
                     + table(other, "0.5 / 1.5 / 5 / 7 T") + "</details>")
    def linkify(text: str) -> str:
        return re.sub(r"PMID (\d+)", lambda m: f"<a href='https://pubmed.ncbi.nlm.nih.gov/{m.group(1)}/' target='_blank' "
                      f"rel='noopener'>PMID {m.group(1)}</a>", _fmt(text))
    parts.append("<ol class='prose' style='font-size:.8rem;'>" + "".join(
        f"<li id='tsrc-{i}'>{linkify(t)}</li>" for i, t in enumerate(sources, 1)) + "</ol>")
    parts.append("</div>")
    return "\n".join(parts)


def _pmid_link(pmid: str) -> str:
    return f"https://pubmed.ncbi.nlm.nih.gov/{_fmt(pmid)}/"


def _citation_row(c: Citation) -> str:
    year = f" ({c.year})" if c.year else ""
    return (
        f"<tr><td><a class='pmid-link' href='{_pmid_link(c.pmid)}' target='_blank' rel='noopener'>"
        f"PMID {_fmt(c.pmid)}</a></td><td>{_fmt(c.title)}{year}</td><td>{_fmt(c.species)}</td>"
        f"<td>{c.n_subjects if c.n_subjects else '&mdash;'}</td><td>{_fmt(c.study_setting)}</td></tr>"
    )


def _citations_table(citations: list[Citation]) -> str:
    if not citations:
        return "<p><em>No citations.</em></p>"
    rows = "\n".join(_citation_row(c) for c in citations)
    return (
        "<table class='citations'><thead><tr><th>PMID</th><th>Title</th><th>Species</th>"
        f"<th>N</th><th>Setting</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _sequence_diagrams_block(context_name: str, sequences: list, dom_id: str) -> str:
    """One labeled saturation-scheme diagram per recommended sequence, behind a toggle so a
    metabolite with five field strengths doesn't push the rest of the report off the page.
    """
    images = []
    for seq in sorted(sequences, key=lambda s: s.field_strength_T):
        png_b64 = render_saturation_scheme_png_base64(
            title=f"{context_name} — {seq.field_strength_T:g} T saturation scheme",
            b1_uT=seq.b1_uT,
            saturation_time_ms=seq.saturation_time_ms,
            n_pulses=seq.n_pulses,
            pulse_duration_ms=seq.pulse_duration_ms,
            interpulse_delay_ms=seq.interpulse_delay_ms,
            pulse_shape=seq.duty_cycle_or_pulses,
        )
        if png_b64:
            images.append(
                f"<img alt='Saturation scheme for {_esc(context_name)} at {seq.field_strength_T:g} T' "
                f"src='data:image/png;base64,{png_b64}'>"
            )
        # The sweep surface behind the numbers, so the recommendation is checkable rather than
        # merely asserted.
        if seq.sweep_contrast:
            land = render_optimization_landscape_png_base64(
                title=f"{context_name} \u2014 {seq.field_strength_T:g} T saturation optimization",
                b1_grid_uT=seq.sweep_b1_grid_uT, tsat_grid_ms=seq.sweep_tsat_grid_ms,
                contrast=seq.sweep_contrast, chosen_b1_uT=seq.b1_uT,
                chosen_tsat_ms=seq.saturation_time_ms,
            )
            if land:
                images.append(
                    f"<img alt='B1/Tsat optimization landscape at {seq.field_strength_T:g} T' "
                    f"src='data:image/png;base64,{land}'>"
                )
    if not images:
        return ""
    return (
        f"<p><button class='plot-toggle' onclick=\"togglePlot('{dom_id}')\">Show saturation-scheme "
        f"diagrams + optimization landscapes ({len(images)} plots)</button></p>"
        f"<div class='plot-wrap' id='{dom_id}'>{''.join(images)}</div>"
    )


def _clinical_section(ce) -> str:
    """'Potential clinical impact': verified disease applications of this pool's CEST/NOE signal,
    one row per study, counts of humans and animals, and pooled group differences when computable."""
    if ce is None:
        return ""
    parts = ["<h3>Potential clinical impact</h3>"]
    if not ce.studies:
        parts.append(f"<p>No verified disease application of this pool's CEST/NOE signal was found. {_fmt(ce.search_note)}</p>")
        return "\n".join(parts)
    parts.append(f"<p><strong>{_fmt(ce.pool or ce.metabolite_name)}:</strong> {ce.n_cest_studies} CEST/NOE application and {ce.n_change_studies} "
                 f"metabolite-change studies verified ({ce.n_human_studies} human, {ce.n_animal_studies} animal; {ce.n_humans} humans, "
                 f"{ce.n_animals} animals; {ce.n_studies_dropped} dropped by verification). {_fmt(ce.summary)}</p>")
    parts.append("<table class='citations'><thead><tr><th>Evidence</th><th>Disease</th><th>Species / setting</th><th>n disease / control</th>"
                 "<th>Metric / method</th><th>Disease vs control</th><th>Result</th><th>PMID</th></tr></thead><tbody>")
    for st in ce.studies:
        n = f"{st.n_disease if st.n_disease is not None else '?'} / {st.n_control if st.n_control is not None else '?'}"
        spread = "SEM" if st.spread_is_sem else "SD"
        means = (f"{st.disease_mean:g} &plusmn; {st.disease_sd:g} vs {st.control_mean:g} &plusmn; {st.control_sd:g} {_fmt(st.unit)} ({spread})"
                 + (f" <span class='verify-note'>[{_fmt(st.stats_source)}]</span>" if st.stats_source.startswith("full text") else "")
                 if None not in (st.disease_mean, st.disease_sd, st.control_mean, st.control_sd) else "&mdash;")
        result = _fmt("; ".join(x for x in (st.direction if st.direction != "not stated" else "", st.p_value_text, st.note) if x)) or "&mdash;"
        ft = f"<div class='quote-body'>full text: &ldquo;{_fmt(st.fulltext_quote)}&rdquo;</div>" if st.fulltext_quote else ""
        quote = f"<details class='quote'><summary>quote</summary><div class='quote-body'>&ldquo;{_fmt(st.quote)}&rdquo;</div>{ft}</details>"
        evidence = "CEST/NOE" if st.evidence_type == "cest_application" else "metabolite change"
        metric = st.cest_metric if st.evidence_type == "cest_application" else st.measurement
        parts.append(f"<tr><td>{evidence}</td><td>{_fmt(st.disease)} <span style='color:#666;'>({_fmt(st.disease_category)})</span></td>"
                     f"<td>{_fmt(st.species)}, {_fmt(st.study_setting)}</td><td>{n}</td><td>{_fmt(metric) or '&mdash;'}</td>"
                     f"<td>{means}</td><td>{result}</td><td><a href='{_pmid_link(st.pmid)}' target='_blank' rel='noopener'>"
                     f"PMID {_fmt(st.pmid)}</a>{quote}</td></tr>")
    parts.append("</tbody></table>")
    for p in ce.pooled_effects:
        what = "CEST/NOE signal" if p.evidence_type == "cest_application" else "metabolite level"
        parts.append(f"<p><strong>Pooled group difference in the {what}, {_fmt(p.disease_category)}</strong> ({p.n_studies} studies, {p.n_disease} disease / "
                     f"{p.n_control} control): Hedges' g = {p.hedges_g:+.2f} [{p.ci_low:+.2f}, {p.ci_high:+.2f}], I&sup2; = {p.i2_percent:.0f} % "
                     f"&mdash; {_fmt(p.direction)}. <span class='verify-note'>{_fmt(p.note)}.</span></p>")
    if not ce.pooled_effects:
        parts.append("<p class='verify-note'>Pooled group difference not computed: fewer than 3 verified studies with group means, SDs and sizes for one evidence type and disease category.</p>")
    if ce.verification_note:
        parts.append(f"<p class='verify-note'><strong>Dropped by verification:</strong> {_fmt(ce.verification_note)}</p>")
    return "\n".join(parts)


def _render_metabolite(m: FinalMetabolite, idx: int) -> str:
    parts = [f"<h2 id='m-{idx}'>{_esc(m.name)} &mdash; CEST: {_badge(m)}</h2>", "<div class='card'>"]

    animal_tag = " <span class='tag animal-tag'>animal evidence only</span>" if m.animal_evidence_only else ""
    parts.append(f"<p><strong>Concentration:</strong> {m.typical_concentration_mM:.2f} mM typical "
                 f"({_fmt(m.concentration_range_mM)}){animal_tag}</p>")
    if m.concentration_evidence_quote:
        pmid_bit = f" &mdash; <a href='{_pmid_link(m.concentration_evidence_pmid)}' target='_blank' " \
                   f"rel='noopener'>PMID {_fmt(m.concentration_evidence_pmid)}</a>" if m.concentration_evidence_pmid else ""
        parts.append(
            "<details class='quote'><summary>Show literature excerpt supporting this concentration"
            f"{pmid_bit}</summary><div class='quote-body'>&ldquo;{_fmt(m.concentration_evidence_quote)}&rdquo;</div></details>"
        )
    elif m.concentration_reasoning or m.reasoning_steps:
        parts.append(
            "<p class='est-note'><strong>Estimated concentration &mdash; no direct literature value "
            f"found. Reasoning:</strong> {_fmt(m.concentration_reasoning)}</p>"
        )
        if m.reasoning_steps:
            parts.append("<table class='citations'><thead><tr><th>Derivation step</th><th>Value</th>"
                          "<th>Source</th></tr></thead><tbody>")
            for step in m.reasoning_steps:
                unsourced = step.source_type in ("assumption",) or (
                    not step.source_detail.strip() and step.source_type != "calculation")
                src = _fmt(step.source_detail) if step.source_detail else "&mdash;"
                label = f"{_fmt(step.source_type)}" + (f": {src}" if step.source_detail else "")
                cell = (f"<span style='color:#833;'>{label}</span>" if unsourced else label)
                parts.append(f"<tr><td>{_fmt(step.statement)}</td><td>{_fmt(step.value)}</td>"
                             f"<td>{cell}</td></tr>")
            parts.append("</tbody></table>")
    else:
        parts.append(
            "<p class='est-note'><em>No verified literature excerpt and no estimation reasoning was "
            "provided for this concentration &mdash; treat the number as unsupported.</em></p>"
        )

    parts.append(f"<p><strong>Exchangeable protons:</strong> {_fmt(m.exchangeable_protons_summary)}</p>")

    if m.reference_concentrations:
        parts.append("<p class='est-note'><strong>Reference database (biofluid &mdash; for comparison, "
                      f"not a tissue value):</strong> {_fmt(m.reference_concentrations)}</p>")

    if m.structure_verification_note:
        parts.append(
            f"<p class='verify-note'><strong>Structure check:</strong> {_fmt(m.structure_verification_note)}</p>"
        )

    units = polymer_units(m.name)
    if units:
        family, _ = units
        parts.append(f"<p><strong>Basic units of {_fmt(family)}</strong> (every type; a macromolecule has no single "
                     "structure). Orange: exchangeable H, labelled with the reference shift the simulation used "
                     "(or 'not simulated'); blue: C-H protons giving the rNOE.</p>")
        try:
            rendered = render_polymer_units_png_base64(m.name, m.exchangeable_protons_detail,
                                                       simulation_parameters=m.simulation_parameters)
            parts.append("<div class='units'>")
            notes = set()
            for label, png, note, unmatched in rendered:
                parts.append(f"<figure><img alt='{_esc(label)}' src='data:image/png;base64,{png}'>"
                             f"<figcaption>{_fmt(note)}</figcaption></figure>")
                notes.update(unmatched)
            parts.append("</div>")
            if notes:
                parts.append("<p style='font-size:.85rem;color:#666;'>Not shown on the units (no substructure match): "
                             f"{'; '.join(_fmt(u) for u in sorted(notes))}</p>")
            if m.smiles:
                parts.append("<p style='font-size:.85rem;color:#666;'>The chemist's own SMILES for this pool is not drawn; "
                             "the units above are the reference structures.</p>")
        except Exception as exc:
            parts.append(f"<p style='color:#833;'><em>Could not render the repeating units ({_fmt(exc)}).</em></p>")
    elif m.smiles:
        try:
            png_b64, unmatched = render_structure_png_base64(m.smiles, m.exchangeable_protons_detail,
                                                             simulation_parameters=m.simulation_parameters)
            parts.append("<div class='structure-wrap'>")
            parts.append(f"<img alt='2D structure of {_esc(m.name)}' src='data:image/png;base64,{png_b64}'>")
            parts.append("<p style='font-size:.8rem;color:#666;margin:.2rem 0;'>Orange: exchangeable H, labelled with the "
                         "reference shift the simulation used (or 'not simulated'), drawn in the physiological protonation "
                         "state; blue: rNOE C-H.</p>")
            if unmatched:
                parts.append("<p style='font-size:.85rem;color:#666;'>Not shown on the structure "
                              f"(no substructure match): {'; '.join(_fmt(u) for u in unmatched)}</p>")
            parts.append("</div>")
        except Exception as exc:
            parts.append(f"<p style='color:#833;'><em>Could not render structure ({_fmt(exc)}).</em></p>")
    else:
        parts.append("<p><em>No SMILES provided by the chemist agent for this metabolite.</em></p>")

    if m.exchangeable_protons_detail:
        parts.append("<table class='citations'><thead><tr><th>Group</th><th>ppm shift (chemist estimate, not "
                      "simulated: the simulation uses the reference table, see Simulation inputs)</th>"
                      "<th>Regime</th><th>Frequency offset by field</th></tr></thead><tbody>")
        for ep in m.exchangeable_protons_detail:
            freq_bits = ", ".join(f"{row.field_strength_T:g}T: {row.offset_from_water_Hz:.0f} Hz"
                                   for row in ep.frequency_table)
            parts.append(f"<tr><td>{_fmt(ep.group)} ({ep.n_protons}H)</td><td>{ep.ppm_shift_estimate:+.2f}</td>"
                         f"<td>{_fmt(ep.exchange_regime)}</td><td style='font-size:.82rem;'>{_fmt(freq_bits)}</td></tr>")
        parts.append("</tbody></table>")

    if m.simulation_parameters:
        parts.append(
            "<p><strong>Simulation inputs</strong> &mdash; exchange rate and shift from the cited reference table "
            "for each group's class (the model chose only the class); concentration from the literature agent.</p>"
            "<table class='citations'><thead><tr><th>Group</th><th>Class</th><th>Mechanism</th><th>Rate (s<sup>-1</sup>)</th>"
            "<th>Offset (ppm)</th><th>Conc. (mM) &times; H</th><th>Max contrast by field</th><th>Source / status</th></tr></thead><tbody>"
        )
        for p in m.simulation_parameters:
            mech = getattr(p, "mechanism", "chemical exchange")
            if p.status == "simulated":
                kex, shift = f"{p.kex_per_s:g}", f"{p.shift_ppm:g}"
                if chemist_shift_note(p):
                    shift += f"<br><span style='font-size:.78rem;color:#666;'>chemist {p.chemist_shift_ppm:+.1f}, not used</span>"
                contrast = ", ".join(f"{float(k):g}T: {v:.2f}%" for k, v in sorted(p.contrast_percent_by_field.items(), key=lambda kv: float(kv[0])))
                source = "<ul class='prose'>" + "".join(f"<li>{_fmt(b)}</li>" for b in bullets(p.provenance, 40)) + "</ul>"
                if p.borrowed_from_other_tissue:
                    source = "<span style='color:#833;font-weight:600;'>borrowed from another tissue/setting</span>" + source
            else:
                kex, shift, contrast = "unknown", "unknown", "&mdash;"
                source = f"<span class='badge-no'>NOT SIMULATED ({_fmt(p.status)})</span> &mdash; {_fmt(p.provenance)}"
            parts.append(
                f"<tr><td>{_fmt(p.group_text)}</td><td>{_fmt(p.group_class)}</td><td>{_fmt(mech)}</td><td>{kex}</td><td>{shift}</td>"
                f"<td>{p.concentration_mM:g} &times; {p.n_exchangeable_protons}</td>"
                f"<td style='font-size:.82rem;'>{contrast}</td><td style='font-size:.82rem;'>{source}</td></tr>"
            )
        parts.append("</tbody></table>")

    if m.zspectra_by_field:
        plot_b64 = render_zspectra_png_base64(m.name, m.zspectra_by_field)
        plot_id = f"plot-{idx}"
        parts.append(f"<p><button class='plot-toggle' onclick=\"togglePlot('{plot_id}')\">Plot simulation</button></p>")
        parts.append(f"<div class='plot-wrap' id='{plot_id}'>")
        if plot_b64:
            parts.append(f"<img alt='Z-spectrum simulation for {_esc(m.name)}' src='data:image/png;base64,{plot_b64}'>")
        # One caption line per panel, lettered like the panels, so the text and the subplots
        # can be read together: contrast, the swept B1/Tsat, and that panel's own warnings.
        seq_by_field = {round(s.field_strength_T, 3): s for s in m.best_sequence_by_field}
        parts.append("<ol class='prose' style='list-style:none;margin-left:0;'>")
        for i, c in enumerate(sorted(m.zspectra_by_field, key=lambda z: z.field_strength_T)):
            letter = chr(ord("A") + i)
            seq = seq_by_field.get(round(c.field_strength_T, 3))
            bits = [f"with-vs-without contrast {c.contrast_diff_percent:.3f}%"]
            if c.direct_saturation_percent is not None:
                bits.append(f"at that offset water alone loses {c.direct_saturation_percent:.2f}% to direct saturation, "
                            f"the MT pool ({c.mt_fraction_of_water:g} of water) and background pools {c.mt_background_percent:.2f}%")
            if seq:
                bits.append(f"swept B1 {seq.b1_uT:g} uT, Tsat {seq.saturation_time_ms:g} ms")
            if c.shift_ppm:
                bits.append(f"pool at {c.shift_ppm:+.2f} ppm, rate {c.exchange_rate_per_s:g} s^-1")
            line = f"<li><strong>{letter}</strong> &mdash; {c.field_strength_T:g} T: {_fmt('; '.join(bits))}"
            if c.overlap_warnings:
                line += ("<details class='popup' style='display:inline;'><summary style='display:inline;font-weight:400;'>"
                         f" {len(c.overlap_warnings)} warning(s)</summary><ul class='overlap-warnings'>"
                         + "".join(f"<li>{_fmt(w)}</li>" for w in c.overlap_warnings) + "</ul></details>")
            parts.append(line + "</li>")
        parts.append("</ol>")
        parts.append("</div>")
    if m.field_strengths_recommended_T:
        fields_str = ", ".join(f"{f:g} T" for f in m.field_strengths_recommended_T)
        parts.append(f"<p><strong>Recommended field strength(s):</strong> {_fmt(fields_str)}</p>")
    if m.detectability_robustness != "no" or m.robustness_note:
        cls = {"robust": "badge-yes", "fragile": "badge-no", "upper-bound only": "badge-no", "unknown": "badge-unknown"}.get(m.detectability_robustness, "")
        parts.append(f"<p><strong>Robustness to input uncertainty:</strong> <span class='{cls}'>{_fmt(m.detectability_robustness)}</span></p>")
        parts.append(_prose("Uncertainty corners", m.robustness_note))

    parts.append(_prose("Frequency-collision analysis", m.frequency_collision_analysis))
    parts.append(_prose("Direct water saturation", m.direct_water_saturation_note))
    parts.append(_prose("Ex vivo / in vivo exchange-rate caveat", m.ex_vivo_in_vivo_exchange_caveat))

    if m.best_sequence_by_field:
        swept = any(s.derived_from == "bloch_mcconnell_sweep" for s in m.best_sequence_by_field)
        source = ("derived from a Bloch-McConnell B1&times;Tsat sweep" if swept
                  else "<span class='badge-no'>model-asserted, not swept</span>")
        parts.append(f"<p><strong>Optimized saturation sequence by field strength</strong> ({source}):</p><ul>")
        for seq in sorted(m.best_sequence_by_field, key=lambda s: s.field_strength_T):
            parts.append(f"<li>{seq.field_strength_T:g} T: B1={seq.b1_uT:g} &micro;T, "
                         f"Tsat={seq.saturation_time_ms:g} ms, {_fmt(seq.duty_cycle_or_pulses)} &mdash; {_fmt(seq.rationale)}")
            if seq.derived_from == "bloch_mcconnell_sweep":
                max_bit = (f"; max achievable at this B1: {seq.max_achievable_contrast_percent:.3f}% "
                           "at a longer Tsat" if abs(seq.max_achievable_contrast_percent
                           - seq.contrast_at_optimum_percent) > 1e-6 else "")
                parts.append(f"<br><span style='font-size:.85rem;color:#555;'>recommended "
                             f"sequence: {seq.contrast_at_optimum_percent:.3f}% "
                             f"contrast{max_bit}; no SAR ceiling applied</span>")
            parts.append("</li>")
        parts.append("</ul>")
        parts.append(_sequence_diagrams_block(m.name, m.best_sequence_by_field, f"seq-{idx}"))

    if m.b1_tsat_extraction_method_note:
        parts.append(
            "<details class='popup'><summary>B1/Tsat vs. extraction method (click for note + literature)</summary>"
            + _prose("Note", m.b1_tsat_extraction_method_note) + _citations_table(m.extraction_method_citations) + "</details>"
        )

    parts.append(_clinical_section(m.clinical_evidence))

    parts.append("<h3>Key literature</h3>")
    parts.append(_citations_table(m.key_citations))

    if m.caveats:
        parts.append(_prose("Caveats / critique", m.caveats))

    parts.append("</div>")
    return "\n".join(parts)


def _render_verification_plan(plan: InVivoVerificationPlan) -> str:
    plane = f" &mdash; {_fmt(plan.imaging_plane)}" if plan.imaging_plane else ""
    parts = [
        f"<h2 id='verification'>In vivo verification: ROI-differential CEST scan{plane}</h2>",
        "<div class='card'>",
        _prose("Why this scan", plan.scan_rationale),
    ]
    if plan.roi_set:
        parts.append("<table class='citations'><thead><tr><th>ROI</th><th>Where it is drawn</th><th>Role</th></tr></thead><tbody>")
        for r in plan.roi_set:
            tag = " <span class='tag'>target tissue</span>" if r.is_target_tissue else ""
            parts.append(f"<tr><td>{_fmt(r.name)}{tag}</td><td>{_fmt(r.anatomical_definition)}</td><td>{_fmt(r.role)}</td></tr>")
        parts.append("</tbody></table>")

    sp = plan.scan_protocol
    parts.append(
        f"<p><strong>Scan protocol:</strong> {sp.field_strength_T:g} T, {_fmt(sp.pulse_shape)}, "
        f"B1={sp.b1_uT:g} &micro;T, {sp.n_pulses} pulses &times; {sp.pulse_duration_ms:g} ms "
        f"(interpulse delay {sp.interpulse_delay_ms:g} ms, duty cycle {sp.duty_cycle_percent:g}%), "
        f"total Tsat={sp.total_saturation_time_ms:g} ms, TR={sp.tr_ms:g} ms</p>"
    )
    protocol_png = render_saturation_scheme_png_base64(
        title="Verification CEST scan — compromise saturation scheme",
        b1_uT=sp.b1_uT,
        saturation_time_ms=sp.total_saturation_time_ms,
        n_pulses=sp.n_pulses,
        pulse_duration_ms=sp.pulse_duration_ms,
        interpulse_delay_ms=sp.interpulse_delay_ms,
        tr_ms=sp.tr_ms,
        pulse_shape=sp.pulse_shape,
    )
    if protocol_png:
        parts.append(
            "<div class='structure-wrap'><img alt='Saturation scheme for the verification CEST scan' "
            f"src='data:image/png;base64,{protocol_png}'></div>"
        )

    parts.append(_prose("SAR reasoning (the experimentalist agent's own; the pipeline applies no SAR model)", sp.sar_rationale))
    parts.append(_prose("Compromise rationale", sp.compromise_rationale))
    parts.append(_prose("Fat suppression", plan.fat_suppression))
    parts.append(_prose("Motion mitigation", plan.motion_mitigation))
    parts.append(_prose("Slice coverage", plan.slice_coverage))
    parts.append(f"<p><strong>Structures in FOV:</strong> {_fmt(', '.join(plan.organs_in_fov))}</p>")

    if plan.zspectrum_offsets:
        parts.append("<p><strong>Z-spectrum sampling offsets:</strong></p><ul>")
        for off in sorted(plan.zspectrum_offsets, key=lambda o: o.offset_ppm):
            parts.append(f"<li>{off.offset_ppm:+.2f} ppm: {_fmt(', '.join(off.targets))}</li>")
        parts.append("</ul>")

    if plan.organ_predictions:
        by_metabolite: dict[str, list] = {}
        for pred in plan.organ_predictions:
            by_metabolite.setdefault(pred.metabolite_name, []).append(pred)
        parts.append(
            "<h3>Predicted ROI-differential contrast</h3>"
            "<p style='font-size:.85rem;color:#666;'>Simulated by the pipeline at the chosen compromise protocol "
            "with each ROI's tabulated water T1/T2/MT and the reference-table exchange rate -- not the model's "
            "own transcription.</p>"
            "<table class='citations'><thead><tr><th>Metabolite</th><th>ROI</th><th>ROI conc. (mM)</th>"
            "<th>Predicted contrast</th><th>Range over B1 &minus;/+ inhomogeneity</th><th>Simulated as</th></tr></thead><tbody>"
        )
        for name, preds in by_metabolite.items():
            for p in preds:
                if p.simulation_note.startswith("not simulated"):
                    note = f"<span class='badge-no'>{_fmt(p.simulation_note)}</span>"
                else:
                    note = "<ul class='prose'>" + "".join(f"<li>{_fmt(b)}</li>" for b in bullets(p.simulation_note, 40)) + "</ul>"
                rng = "&mdash;"
                if len(p.predicted_contrast_range_percent) == 2 and p.b1_scale_range:
                    rng = (f"{p.predicted_contrast_range_percent[0]:.3f}&ndash;{p.predicted_contrast_range_percent[1]:.3f}% "
                           f"<span style='color:#666;'>(B1 &times; {p.b1_scale_range[0]:g}&ndash;{p.b1_scale_range[1]:g})</span>")
                parts.append(
                    f"<tr><td>{_fmt(name)}</td><td>{_fmt(p.organ)}</td><td>{p.organ_concentration_mM:g}</td>"
                    f"<td>{p.predicted_contrast_percent:.3f}%</td><td>{rng}</td><td style='font-size:.8rem;'>{note}</td></tr>"
                )
        parts.append("</tbody></table>")
        chart = render_roi_contrast_png_base64(plan.organ_predictions, CONTRAST_THRESHOLD_PERCENT)
        if chart:
            parts.append("<div class='structure-wrap'><img alt='Predicted ROI-differential contrast' "
                         f"src='data:image/png;base64,{chart}'></div>")
        all_citations = [c for pred in plan.organ_predictions for c in pred.citations]
        if all_citations:
            parts.append("<details class='popup'><summary>Organ-concentration citations</summary>")
            parts.append(_citations_table(all_citations))
            parts.append("</details>")

    parts.append(_prose("Differential analysis plan", plan.differential_analysis_plan))
    parts.append(_prose("Not actively targeted", plan.excluded_metabolites_note))
    parts.append(_prose("Caveats", plan.caveats))

    parts.append("</div>")
    return "\n".join(parts)


def _check_label(passed: bool | None) -> str:
    if passed is None:
        return "n/a"
    return "<span class='badge-yes'>passed</span>" if passed else "<span class='badge-no'>FAILED</span>"


def _render_provenance(p: RunProvenance) -> str:
    parts = [
        "<h2 id='provenance'>Run provenance</h2>", "<div class='card'>",
        f"<p>Started {_fmt(p.started_at_utc)}, finished {_fmt(p.finished_at_utc)} "
        f"({p.elapsed_s / 60:.1f} min total)."
        + (f" cest_scout code version <code>{_fmt(p.code_version)}</code>." if p.code_version else "") + "</p>",
        "<table class='citations'><tr><th>Agent</th><th>Model</th><th>Endpoint</th><th>Thinking</th>"
        "<th>Minutes</th><th>LLM calls</th><th>Prompt tokens</th><th>Completion tokens</th>"
        "<th>Tool calls (errors)</th><th>Nudges / JSON repairs / output retries</th>"
        "<th>Completeness check</th></tr>",
    ]
    for s in p.stages:
        parts.append(
            f"<tr><td>{_fmt(s.agent)}</td><td>{_fmt(s.model)}</td><td>{_fmt(s.endpoint)}</td>"
            f"<td>{'disabled' if s.thinking_disabled else 'default'}</td><td>{s.elapsed_s / 60:.1f}</td>"
            f"<td>{s.llm_calls}</td><td>{s.prompt_tokens:,}</td><td>{s.completion_tokens:,}</td>"
            f"<td>{s.tool_calls} ({s.tool_errors})</td><td>{s.nudges} / {s.json_repairs} / {s.output_retries}</td>"
            f"<td>{_check_label(s.output_check_passed)}</td></tr>"
        )
    parts.append("</table>")
    if p.settings:
        parts.append("<details class='popup'><summary>Deterministic settings this run used</summary><ul style='font-size:.85rem;'>"
                     + "".join(f"<li><code>{_fmt(k)}</code>: {_fmt(v)}</li>" for k, v in p.settings.items()) + "</ul></details>")
    parts.append(
        "<p class='est-note'>A FAILED completeness check means the agent's structured output never covered "
        "everything it was asked to (e.g. a verdict for every metabolite) within its retry budget, and the "
        "incomplete output was kept; the deterministic checks above flag what is missing.</p>"
    )
    parts.append("</div>")
    return "\n".join(parts)


def render_html(report: FinalReport) -> str:
    detectable = [m for m in report.metabolites if m.detectable_by_cest]
    unknown = [m for m in report.metabolites if detectability_of(m) == "unknown"]
    not_detectable = [m for m in report.metabolites if not m.detectable_by_cest and m not in unknown]

    ordered = detectable + not_detectable + unknown
    summary = "".join(f"<li>{_fmt(m.name)}: {_badge(m)}</li>" for m in ordered)
    excluded = getattr(report, "excluded_candidates", []) or []
    toc = ["<nav class='toc'><strong>Outline</strong><ol>"]
    for idx, m in enumerate(ordered):
        cls = {"detectable": "t-yes", "unknown": "t-unk"}.get(detectability_of(m), "t-no")
        toc.append(f"<li><a href='#m-{idx}'>{_esc(m.name[:40])}</a> <span class='{cls}'>{_esc(verdict_label(m).split(' (')[0])}</span></li>")
    toc.append("</ol><ol style='list-style:none;margin-left:0;'>")
    if report.tissue_parameters:
        toc.append("<li><a href='#tissue'>Tissue parameters</a></li>")
    if report.in_vivo_verification_plan:
        toc.append("<li><a href='#verification'>Verification scan</a></li>")
    if report.provenance:
        toc.append("<li><a href='#provenance'>Run provenance</a></li>")
    toc.append("</ol></nav>")
    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>CEST_Scout: {_esc(report.body_part)}</title>", _STYLE, "</head><body>",
        "".join(toc),
        f"<h1>CEST-detectable metabolite candidates: {_fmt(report.body_part)}</h1>",
        f"<p>{len(detectable)} of {len(report.metabolites)} candidates clear the simulated with-vs-without contrast "
        f"threshold ({CONTRAST_THRESHOLD_PERCENT}%) at some field strength, by chemical exchange (downfield, +ppm) "
        f"or by the relayed NOE (upfield, &minus;ppm). {len(not_detectable)} fall below it; {len(unknown)} are "
        "<span class='badge-unknown'>unknown</span> because no group had a verified reference rate, so nothing could be "
        "simulated. The threshold applies to our own simulation, never to a literature MTRasym.</p>",
        f"<ul class='prose'>{summary}</ul>",
    ]
    if excluded:
        parts.append("<p class='est-note'><strong>Excluded before simulation (not stored in the tissue):</strong> "
                     + "; ".join(_fmt(x) for x in excluded) + "</p>")
    rows = getattr(report, "tissue_parameters", [])
    if rows:
        parts.append(_tissue_parameters_block(rows))
    elif report.tissue_parameters_note:
        parts.append(_prose("Tissue parameters used in the simulations", report.tissue_parameters_note))
    for idx, m in enumerate(ordered):
        parts.append(_render_metabolite(m, idx))
    if report.in_vivo_verification_plan:
        parts.append(_render_verification_plan(report.in_vivo_verification_plan))
    if report.provenance:
        parts.append(_render_provenance(report.provenance))
    parts.append("</body></html>")
    return "\n".join(parts)
