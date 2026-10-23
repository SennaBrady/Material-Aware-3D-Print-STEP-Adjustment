"""Compensation rules R1-R4 and R6, and the hold check, for single dimensions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .calibration import EffectiveValues
from .job import Dimension
from .materials import Material


def scale_factor(s_pct: float) -> float:
    """R1: k = 1 / (1 - s)."""
    return 1.0 / (1.0 - s_pct / 100.0)


@dataclass
class DimResult:
    dim: Dimension
    print_value: float
    correction: str  # human-readable description of what was applied
    hold: str  # holds | fails | unknown | n/a
    spread_mm: Optional[float]
    flags: list[str] = field(default_factory=list)
    feature: Optional[str] = None  # matched feature id


def compensate_dimension(dim: Dimension, ev: EffectiveValues, mesh_input: bool) -> tuple[float, str, list[str]]:
    flags: list[str] = []
    if dim.type == "z":
        return dim.nominal, "none (Z is not compensated)", flags
    k = ev.k if ev.k is not None else 1.0
    if ev.k is None:
        flags.append("no shrinkage value for this material: calibrate it first")
    if dim.type == "position":
        return k * dim.nominal, f"×{k:.5f}", flags
    if dim.type == "hole":
        if mesh_input:
            flags.append("hole uncorrected: STL input gets XY scale only (export STEP to apply the hole offset)")
            return k * dim.nominal, f"×{k:.5f} only", flags
        h = ev.h_mm
        if h is None:
            flags.append("no hole offset for this material: run the calibration coupon before trusting holes")
            h = 0.0
        return k * dim.nominal + 2 * h, f"×{k:.5f} + 2×{h:.3f}", flags
    # contour
    c = ev.c_mm if not mesh_input else 0.0
    corr = f"×{k:.5f}" + (f" + 2×{c:.3f}" if c else "")
    return k * dim.nominal + 2 * c, corr, flags


def hold_check(dim: Dimension, ev: EffectiveValues) -> tuple[str, Optional[float]]:
    """spread = L × (s_max - s_min); the dimension holds if spread <= T."""
    if dim.type == "z":
        return "n/a", None
    if ev.s_min_pct is None or ev.s_max_pct is None:
        return "unknown", None
    spread = dim.nominal * (ev.s_max_pct - ev.s_min_pct) / 100.0
    return ("holds" if spread <= dim.band + 1e-12 else "fails"), spread


def feature_size_flags(dim: Dimension, material: Material) -> list[str]:
    mf = material.min_feature_mm
    if not mf or dim.type == "z":
        return []
    if mf.get("diameter") and dim.nominal < mf["diameter"]:
        return [f"below {material.name}'s minimum feature size of {mf['diameter']:g} mm"]
    return []


def evaluate(dim: Dimension, material: Material, ev: EffectiveValues, mesh_input: bool) -> DimResult:
    value, corr, flags = compensate_dimension(dim, ev, mesh_input)
    hold, spread = hold_check(dim, ev)
    size_flags = feature_size_flags(dim, material)
    if size_flags:
        hold = "fails"
        flags += size_flags
    return DimResult(dim, value, corr, hold, spread, flags, dim.feature)
