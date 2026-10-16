"""Abstract-level meta-analysis of CEST/NOE group differences, deterministic and dependency-free.

Each verified clinical study that states, in its abstract, the CEST metric's mean and SD (or SEM)
and the group sizes for a disease and a control group yields one standardized mean difference
(Hedges' g, small-sample corrected). Studies of the same metabolite pool and disease category are
pooled with DerSimonian-Laird random effects when at least MIN_STUDIES of them exist; otherwise
the pool is reported as having too little data -- never a pooled number from one or two papers.
Everything here is screening-grade: the inputs are abstract sentences, not extracted full-text
data, and the output says so.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

MIN_STUDIES = 3
_Z95 = 1.959964

ABSTRACT_LEVEL_NOTE = ("pooled from numbers stated in abstracts (means, SDs and group sizes as written there), "
                       "not from extracted full-text data; a screening-grade estimate")


@dataclass
class StudyEffect:
    pmid: str
    n_disease: int
    n_control: int
    g: float            # Hedges' g, positive = metric higher in disease
    var: float          # sampling variance of g


@dataclass
class PooledEffect:
    evidence_type: str
    disease_category: str
    n_studies: int
    n_disease: int
    n_control: int
    g: float
    ci_low: float
    ci_high: float
    i2_percent: float
    tau2: float
    pmids: list[str] = field(default_factory=list)

    @property
    def direction(self) -> str:
        if self.ci_low > 0:
            return "higher in disease"
        if self.ci_high < 0:
            return "lower in disease"
        return "no consistent difference (95% CI crosses zero)"


def hedges_g(mean_d: float, sd_d: float, n_d: int, mean_c: float, sd_c: float, n_c: int) -> tuple[float, float] | None:
    """(g, variance) for disease vs control, or None when the inputs cannot give one (n < 2 in a
    group, a non-positive SD, or a zero pooled SD)."""
    if n_d < 2 or n_c < 2 or sd_d <= 0 or sd_c <= 0:
        return None
    df = n_d + n_c - 2
    s_pooled = math.sqrt(((n_d - 1) * sd_d ** 2 + (n_c - 1) * sd_c ** 2) / df)
    if s_pooled <= 0:
        return None
    d = (mean_d - mean_c) / s_pooled
    j = 1.0 - 3.0 / (4.0 * df - 1.0)          # Hedges' small-sample correction
    g = j * d
    var = (n_d + n_c) / (n_d * n_c) + g ** 2 / (2.0 * (n_d + n_c))
    return g, var


def sem_to_sd(sem: float, n: int) -> float:
    return sem * math.sqrt(n)


def pool(effects: list[StudyEffect]) -> tuple[float, float, float, float, float] | None:
    """DerSimonian-Laird random-effects pooling: (g, ci_low, ci_high, I2 percent, tau2)."""
    if len(effects) < 2:
        return None
    w = [1.0 / e.var for e in effects]
    g_fixed = sum(wi * e.g for wi, e in zip(w, effects)) / sum(w)
    q = sum(wi * (e.g - g_fixed) ** 2 for wi, e in zip(w, effects))
    df = len(effects) - 1
    c = sum(w) - sum(wi ** 2 for wi in w) / sum(w)
    tau2 = max(0.0, (q - df) / c) if c > 0 else 0.0
    w_re = [1.0 / (e.var + tau2) for e in effects]
    g_re = sum(wi * e.g for wi, e in zip(w_re, effects)) / sum(w_re)
    se = math.sqrt(1.0 / sum(w_re))
    i2 = max(0.0, (q - df) / q) * 100.0 if q > 0 else 0.0
    return g_re, g_re - _Z95 * se, g_re + _Z95 * se, i2, tau2


def pooled_effects(studies: list[dict]) -> list[PooledEffect]:
    """studies: dicts with pmid, evidence_type, disease_category, n_disease, n_control, disease_mean,
    disease_sd, control_mean, control_sd (SDs already converted from SEM where the abstract gave
    SEM). One PooledEffect per (evidence type, disease category) with >= MIN_STUDIES usable studies
    -- a CEST metric and a tissue concentration are never pooled together; one study per PMID."""
    by_cat: dict[tuple[str, str], dict[str, StudyEffect]] = defaultdict(dict)
    for s in studies:
        try:
            hg = hedges_g(float(s["disease_mean"]), float(s["disease_sd"]), int(s["n_disease"]),
                          float(s["control_mean"]), float(s["control_sd"]), int(s["n_control"]))
        except (KeyError, TypeError, ValueError):
            hg = None
        if hg is None:
            continue
        cat = ((s.get("evidence_type") or "cest_application").strip().lower(), (s.get("disease_category") or "other").strip().lower())
        pmid = str(s.get("pmid", ""))
        if pmid in by_cat[cat]:
            continue
        by_cat[cat][pmid] = StudyEffect(pmid=pmid, n_disease=int(s["n_disease"]), n_control=int(s["n_control"]), g=hg[0], var=hg[1])
    out = []
    for (etype, cat), effects_by_pmid in sorted(by_cat.items()):
        effects = list(effects_by_pmid.values())
        if len(effects) < MIN_STUDIES:
            continue
        res = pool(effects)
        if res is None:
            continue
        g, lo, hi, i2, tau2 = res
        out.append(PooledEffect(evidence_type=etype, disease_category=cat, n_studies=len(effects),
                                n_disease=sum(e.n_disease for e in effects), n_control=sum(e.n_control for e in effects),
                                g=round(g, 3), ci_low=round(lo, 3), ci_high=round(hi, 3), i2_percent=round(i2, 1),
                                tau2=round(tau2, 4), pmids=sorted(effects_by_pmid)))
    return out
