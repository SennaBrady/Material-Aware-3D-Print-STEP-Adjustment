"""Material advisor: rank alternatives when a critical dimension can't be held."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .calibration import CalibrationStore, effective_values
from .compensation import feature_size_flags, hold_check
from .job import Dimension, HardRequirements
from .materials import CHEM_CATEGORIES, CHEM_LEVELS, Material, MaterialDB

LEVEL_WORDS = {0: "not resistant", 1: "short exposure", 2: "resistant"}


@dataclass
class Alternative:
    material: Material
    score: float
    tradeoff: str
    worst: str  # worst-case spread vs tolerance, for the report
    needs_calibration: bool = False


@dataclass
class AdvisorResult:
    alternatives: list[Alternative] = field(default_factory=list)
    needs_calibration: list[Alternative] = field(default_factory=list)
    original_hard_failures: list[str] = field(default_factory=list)


def hard_failures(m: Material, hard: HardRequirements) -> list[str]:
    """Reasons a material misses a hard requirement. Unknown values count as a miss."""
    out = []
    if hard.max_temp_c is not None:
        t = m.max_temp_conservative
        if t is None:
            out.append(f"max use temperature not listed (need {hard.max_temp_c:g} °C)")
        elif t < hard.max_temp_c:
            out.append(f"max use temperature {t:g} °C is below {hard.max_temp_c:g} °C")
    for cat, need in hard.chemicals.items():
        need_level = CHEM_LEVELS.get(need)
        if need_level is None:
            raise ValueError(f"Chemical requirement for {cat} must be resistant, short or not (got {need!r})")
        have = m.chem_level(cat)
        if have is None:
            out.append(f"{cat} resistance not listed (need {need})")
        elif have < need_level:
            out.append(f"{cat}: {LEVEL_WORDS[have]}, need {need}")
    return out


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-9)


def closeness(orig: Material, cand: Material) -> float:
    """1.0 = same properties. Compares temperature, chemicals, strength, UV and conductivity.

    Only properties the original has are compared; a candidate missing one of them
    takes a half penalty, so unknown data never looks like a perfect match.
    """
    diffs: list[float] = []

    def cmp(a, b, fn):
        if a is None:
            return
        diffs.append(0.5 if b is None else min(1.0, fn(a, b)))

    cmp(orig.max_temp_conservative, cand.max_temp_conservative, lambda a, b: abs(a - b) / 100.0)
    for cat in CHEM_CATEGORIES:
        cmp(orig.chem_level(cat), cand.chem_level(cat), lambda a, b: abs(a - b) / 2.0)
    cmp(orig.tensile_mpa, cand.tensile_mpa, _rel)
    cmp(orig.modulus_gpa, cand.modulus_gpa, _rel)
    cmp(orig.elongation_pct, cand.elongation_pct, _rel)
    cmp(orig.uv_resistant, cand.uv_resistant, lambda a, b: 0.0 if a == b else 1.0)
    diffs.append(0.0 if orig.conductive == cand.conductive else 1.0)
    return 1.0 - sum(diffs) / len(diffs)


def tradeoff_line(orig: Material, cand: Material) -> str:
    parts = []
    if cand.base == orig.base:
        parts.append(f"keeps {orig.base} chemistry")
    to, tc = orig.max_temp_conservative, cand.max_temp_conservative
    if tc is not None:
        parts.append(f"{tc:g} °C" + (f" vs {to:g} °C" if to is not None and to != tc else ""))
    elif to is not None:
        parts.append("max temperature not listed")
    worse, better = [], []
    for cat in CHEM_CATEGORIES:
        a, b = orig.chem_level(cat), cand.chem_level(cat)
        if a is not None and b is not None:
            if b < a:
                worse.append(cat)
            elif b > a:
                better.append(cat)
    if worse:
        parts.append("less resistant to " + "/".join(worse))
    if better:
        parts.append("more resistant to " + "/".join(better))
    if orig.uv_resistant and cand.uv_resistant is not True:
        parts.append("UV resistance not listed")
    if orig.conductive != cand.conductive:
        parts.append("conductive" if cand.conductive else "not conductive")
    if cand.notes:
        parts.append(cand.notes.split(";")[0].strip())
    return ", ".join(parts)


def advise(
    db: MaterialDB,
    original: Material,
    dims: list[Dimension],
    hard: HardRequirements,
    store: Optional[CalibrationStore] = None,
    printer: str = "",
    profile: str = "",
    top: int = 3,
) -> AdvisorResult:
    result = AdvisorResult(original_hard_failures=hard_failures(original, hard))
    xy_dims = [d for d in dims if d.type != "z"]
    ranked: list[tuple[tuple, Alternative]] = []
    for m in db:
        if m is original:
            continue
        if hard_failures(m, hard):
            continue
        record = store.get(m.name, printer, profile) if store else None
        ev = effective_values(m, record)
        checks = [(d, *hold_check(d, ev)) for d in xy_dims]
        if any(feature_size_flags(d, m) for d in xy_dims):
            continue
        tradeoff = tradeoff_line(original, m)
        score = closeness(original, m)
        if any(status == "unknown" for _, status, _ in checks):
            # Same base plastic with no shrinkage data: worth calibrating, never ranked.
            if m.base == original.base:
                result.needs_calibration.append(Alternative(m, score, tradeoff, "no shrinkage data", True))
            continue
        if any(status == "fails" for _, status, _ in checks):
            continue
        worst_d, _, worst_spread = max(checks, key=lambda c: (c[2] or 0) / c[0].band) if checks else (None, None, None)
        worst = f"{worst_spread:.3f} of {worst_d.band:.3f} mm on {worst_d.name}" if worst_d else ""
        # Fibre-filled grades of the same base plastic first, then same base, then closeness.
        same_base_fibre = m.base == original.base and m.fibre_filled
        key = (not same_base_fibre, m.base != original.base, -score)
        ranked.append((key, Alternative(m, score, tradeoff, worst)))
    ranked.sort(key=lambda kv: kv[0])
    result.alternatives = [a for _, a in ranked[:top]]
    return result
