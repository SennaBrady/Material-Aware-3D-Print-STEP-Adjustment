"""Print settings advisor: wall and infill rules S1-S6, and the slicer settings to export (R5)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .calibration import CalibrationRecord, EffectiveValues
from .job import DEFAULT_SETTINGS, Dimension, PrintSettings
from .materials import Material

# A critical XY dimension with a tolerance band at or below this is "tight" for S2.
TIGHT_BAND_MM = 0.2
S2_WALLS = 4
# S5 thresholds: a "large, thick" part in a "high-shrinkage" material.
S5_SHRINK_PCT = 1.0
S5_LARGE_MM = 100.0
S5_THICK_MM = 10.0


@dataclass
class Suggestion:
    rule: str
    setting: str  # wall_loops | infill_pct | note
    value: Optional[float]
    reason: str  # one line, names the dimension or "whole part"
    provisional: bool = False


# R5: all compensation lives in the print model, so every slicer-side compensation is off.
R5_SLICER_SETTINGS = {
    "filament_shrink": "100%",
    "xy_hole_compensation": "0",
    "xy_contour_compensation": "0",
    "auto_circle_holes_contour_compensation": "off",
}
R5_LABELS = {
    "filament_shrink": "Filament > Shrinkage",
    "xy_hole_compensation": "Quality > X-Y hole compensation",
    "xy_contour_compensation": "Quality > X-Y contour compensation",
    "auto_circle_holes_contour_compensation": "Filament > Auto circle holes-contour compensation",
}


def advise_settings(
    material: Material,
    ev: EffectiveValues,
    settings: PrintSettings,
    dims: list[Dimension],
    part_size_mm: Optional[tuple[float, float, float]] = None,
    record: Optional[CalibrationRecord] = None,
) -> tuple[list[Suggestion], PrintSettings]:
    """Return suggestions and the settings after accepting all of them."""
    out: list[Suggestion] = []
    walls = settings.wall_loops
    infill = settings.infill_pct

    # S1: wall count x line width below the material's minimum wall.
    if material.min_wall_mm and settings.wall_thickness_mm < material.min_wall_mm - 1e-9:
        need = math.ceil(material.min_wall_mm / settings.line_width_mm - 1e-9)
        walls = max(walls, need)
        out.append(Suggestion(
            "S1", "wall_loops", need,
            f"whole part: {settings.wall_loops} walls × {settings.line_width_mm:g} mm = "
            f"{settings.wall_thickness_mm:.2f} mm is under {material.name}'s {material.min_wall_mm:g} mm minimum wall",
        ))

    # S2: a tight critical hole or edge should sit fully in solid perimeter, not partly in infill.
    tight = [d for d in dims if d.type in ("hole", "contour") and d.band <= TIGHT_BAND_MM]
    if tight and walls < S2_WALLS:
        names = ", ".join(f"{d.name} ({d.nominal:g} {d.tol_text()})" for d in tight)
        walls = S2_WALLS
        out.append(Suggestion(
            "S2", "wall_loops", S2_WALLS,
            f"{names}: tight tolerance; {S2_WALLS} walls keep the feature in solid perimeter so it can be "
            "reamed or sanded to size without opening into infill",
            provisional=True,
        ))

    # S3: infill outside the material's range.
    rng = material.infill_range(settings.purpose)
    if rng and not (rng[0] <= infill <= rng[1]):
        new = rng[0] if infill < rng[0] else rng[1]
        out.append(Suggestion(
            "S3", "infill_pct", new,
            f"whole part: {infill:g}% infill is outside {material.name}'s {settings.purpose} range "
            f"{rng[0]:g}–{rng[1]:g}%",
        ))
        infill = new

    # S4 (100% infill weakens slicer circle compensation) never applies here: under R5
    # slicer compensation is always off, and the model carries the hole correction.

    # S5: large, thick part in a high-shrinkage material -> lower infill to cut internal stress.
    if part_size_mm and ev.s_pct is not None and ev.s_pct >= S5_SHRINK_PCT and rng:
        x, y, z = part_size_mm
        if max(x, y) >= S5_LARGE_MM and min(x, y, z) >= S5_THICK_MM and infill > rng[0]:
            out.append(Suggestion(
                "S5", "infill_pct", rng[0],
                f"whole part: {max(x, y):.0f} mm long and {min(x, y, z):.0f} mm thick in {ev.s_pct:.1f}% shrinkage "
                f"material; {rng[0]:g}% infill cuts internal stress and warping",
                provisional=True,
            ))
            infill = rng[0]

    # S6: the user raised walls or infill above what their calibration coupon was printed at.
    # Without a calibration the table values aren't tied to any settings, so there's nothing to compare.
    last = record.runs[-1] if record and record.runs else None
    if last and last.wall_loops is not None:
        ref_infill = last.infill_pct if last.infill_pct is not None else DEFAULT_SETTINGS.infill_pct
        if settings.wall_loops > last.wall_loops or settings.infill_pct > ref_infill:
            out.append(Suggestion(
                "S6", "note", None,
                f"whole part: {settings.wall_loops} walls / {settings.infill_pct:g}% infill is above your calibration "
                f"print ({last.wall_loops} walls / {ref_infill:g}%); shrinkage may change, so print a calibration "
                "coupon at these settings",
                provisional=True,
            ))

    final = PrintSettings(walls, settings.line_width_mm, infill, settings.purpose)
    return out, final


def apply_suggestions(settings: PrintSettings, suggestions: list[Suggestion], accepted: set[str]) -> PrintSettings:
    """Settings after accepting only the named rules' suggestions."""
    walls, infill = settings.wall_loops, settings.infill_pct
    for s in suggestions:
        if s.rule not in accepted or s.value is None:
            continue
        if s.setting == "wall_loops":
            walls = max(walls, int(s.value))
        elif s.setting == "infill_pct":
            infill = s.value
    return PrintSettings(walls, settings.line_width_mm, infill, settings.purpose)
