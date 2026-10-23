"""The job file: part, material, critical dimensions, print settings, hard requirements."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

DIM_TYPES = ("hole", "contour", "position", "z")
AXES = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")


@dataclass
class Dimension:
    name: str
    type: str  # hole | contour | position | z
    nominal: float
    lower: float  # signed deviation, e.g. -0.05
    upper: float  # signed deviation, e.g. +0.05
    feature: Optional[str] = None  # feature id from `matprint features`, e.g. H1

    @property
    def band(self) -> float:
        """Tolerance band T = upper limit - lower limit."""
        return self.upper - self.lower

    def tol_text(self) -> str:
        if abs(self.upper + self.lower) < 1e-9:
            return f"±{self.upper:g}"
        lo = f"-{abs(self.lower):g}" if self.lower <= 0 else f"+{self.lower:g}"
        hi = f"+{self.upper:g}" if self.upper >= 0 else f"-{abs(self.upper):g}"
        return f"{hi}/{lo}"


@dataclass
class PrintSettings:
    wall_loops: int = 2
    line_width_mm: float = 0.42
    infill_pct: float = 15.0
    purpose: str = "functional"  # functional | decorative

    @property
    def wall_thickness_mm(self) -> float:
        return self.wall_loops * self.line_width_mm


# Bambu Studio defaults for a 0.4 mm nozzle, used when the job gives none.
DEFAULT_SETTINGS = PrintSettings()


@dataclass
class HardRequirements:
    max_temp_c: Optional[float] = None
    chemicals: dict[str, str] = field(default_factory=dict)  # category -> required level

    @property
    def any(self) -> bool:
        return self.max_temp_c is not None or bool(self.chemicals)


@dataclass
class Job:
    part: Optional[Path]
    material: str
    dimensions: list[Dimension]
    build_up_axis: str = "+Z"
    printer: str = "Bambu Lab"
    profile: str = "default"
    print_settings: PrintSettings = field(default_factory=PrintSettings)
    settings_given: bool = False
    hard: HardRequirements = field(default_factory=HardRequirements)


def parse_tolerance(raw) -> tuple[float, float]:
    """Accept 0.05 (symmetric), [-0.05, 0.1], or {lower: -0.05, upper: 0.1}."""
    if isinstance(raw, (int, float)):
        t = abs(float(raw))
        return -t, t
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        lo, hi = sorted(float(v) for v in raw)
        return lo, hi
    if isinstance(raw, dict):
        return float(raw["lower"]), float(raw["upper"])
    raise ValueError(f"Can't read tolerance {raw!r}; use 0.05, [-0.05, 0.1] or {{lower:, upper:}}")


def _parse_dimension(d: dict) -> Dimension:
    dtype = str(d.get("type", "")).lower()
    if dtype not in DIM_TYPES:
        raise ValueError(f"Dimension '{d.get('name')}' needs type hole, contour, position or z (got {dtype!r})")
    lo, hi = parse_tolerance(d.get("tol", d.get("tolerance")))
    return Dimension(
        name=str(d["name"]),
        type=dtype,
        nominal=float(d["nominal"]),
        lower=lo,
        upper=hi,
        feature=d.get("feature"),
    )


def load_job(path: Path) -> Job:
    path = Path(path)
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    part = raw.get("part")
    part_path = (path.parent / part).resolve() if part else None
    axis = str(raw.get("build_up_axis", "+Z")).upper()
    if not axis.startswith(("+", "-")):
        axis = "+" + axis
    if axis not in AXES:
        raise ValueError(f"build_up_axis must be one of {', '.join(AXES)}")
    ps_raw = raw.get("print_settings") or {}
    ps = PrintSettings(
        wall_loops=int(ps_raw.get("wall_loops", DEFAULT_SETTINGS.wall_loops)),
        line_width_mm=float(ps_raw.get("line_width_mm", DEFAULT_SETTINGS.line_width_mm)),
        infill_pct=float(ps_raw.get("infill_pct", DEFAULT_SETTINGS.infill_pct)),
        purpose=str(ps_raw.get("purpose", DEFAULT_SETTINGS.purpose)),
    )
    hard_raw = raw.get("hard_requirements") or {}
    hard = HardRequirements(
        max_temp_c=hard_raw.get("max_temp_c"),
        chemicals={k: str(v) for k, v in (hard_raw.get("chemicals") or {}).items()},
    )
    return Job(
        part=part_path,
        material=str(raw.get("material", "")),
        dimensions=[_parse_dimension(d) for d in raw.get("dimensions") or []],
        build_up_axis=axis,
        printer=str(raw.get("printer", "Bambu Lab")),
        profile=str(raw.get("profile", "default")),
        print_settings=ps,
        settings_given=bool(ps_raw),
        hard=hard,
    )
