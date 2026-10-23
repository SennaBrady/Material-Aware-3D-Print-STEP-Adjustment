"""Calibration loop: measured shrinkage and hole offset, stored per material, printer and profile."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Optional

from .materials import Material


def default_store_path() -> Path:
    home = os.environ.get("MATPRINT_HOME")
    base = Path(home) if home else Path.home() / ".matprint"
    return base / "calibration.json"


@dataclass
class CalibrationRun:
    date: str
    s_pct: float  # measured shrinkage, percent
    h_mm: Optional[float]  # measured hole offset per side, mm
    square_model_mm: float
    square_measured_mm: list[float]
    holes: list[dict]  # {model: D in the coupon model, measured: D measured}
    dried: Optional[bool] = None
    wall_loops: Optional[int] = None
    infill_pct: Optional[float] = None
    compensated: bool = False


@dataclass
class CalibrationRecord:
    material: str
    printer: str
    profile: str
    runs: list[CalibrationRun] = field(default_factory=list)

    @property
    def s_pct(self) -> float:
        return mean(r.s_pct for r in self.runs)

    @property
    def h_mm(self) -> Optional[float]:
        hs = [r.h_mm for r in self.runs if r.h_mm is not None]
        return mean(hs) if hs else None

    @property
    def s_range(self) -> Optional[tuple[float, float]]:
        """Run-to-run shrinkage range; needs two or more coupons."""
        if len(self.runs) < 2:
            return None
        values = [r.s_pct for r in self.runs]
        return min(values), max(values)


def compute_run(
    square_model_mm: float,
    square_measured_mm: list[float],
    holes: list[tuple[float, float]],
) -> tuple[float, Optional[float]]:
    """Return (s in percent, h in mm) from one coupon.

    s = 1 - L_measured / L_model. The hole offset is the hole error left after
    shrinkage, per side, averaged over the holes: h = mean((1 - s) D_model - D_measured) / 2.
    Positive h means holes print small and the model must enlarge them.
    """
    s = 1.0 - mean(square_measured_mm) / square_model_mm
    h = None
    if holes:
        h = mean(((1.0 - s) * d_model - d_meas) / 2.0 for d_model, d_meas in holes)
    return s * 100.0, h


class CalibrationStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_store_path()
        self.records: dict[str, CalibrationRecord] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text("utf-8"))
            for key, rec in raw.get("records", {}).items():
                runs = [CalibrationRun(**r) for r in rec.pop("runs", [])]
                self.records[key] = CalibrationRecord(runs=runs, **rec)

    @staticmethod
    def key(material: str, printer: str, profile: str) -> str:
        return f"{material}|{printer}|{profile}".lower()

    def get(self, material: str, printer: str, profile: str) -> Optional[CalibrationRecord]:
        return self.records.get(self.key(material, printer, profile))

    def add_run(self, material: str, printer: str, profile: str, run: CalibrationRun) -> CalibrationRecord:
        k = self.key(material, printer, profile)
        rec = self.records.setdefault(k, CalibrationRecord(material, printer, profile))
        rec.runs.append(run)
        self.save()
        return rec

    def clear(self, material: str, printer: str, profile: str) -> bool:
        removed = self.records.pop(self.key(material, printer, profile), None) is not None
        if removed:
            self.save()
        return removed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"records": {k: asdict(r) for k, r in self.records.items()}}
        self.path.write_text(json.dumps(data, indent=2), "utf-8")


@dataclass
class EffectiveValues:
    """The numbers compensation and the hold check actually use, and where they came from."""

    s_pct: Optional[float]
    s_min_pct: Optional[float]
    s_max_pct: Optional[float]
    h_mm: Optional[float]
    c_mm: float
    s_source: str  # "calibration" | "table" | "none"
    spread_source: str  # "calibration" | "table" | "none"
    h_source: str  # "calibration" | "table" | "none"

    @property
    def k(self) -> Optional[float]:
        return None if self.s_pct is None else 1.0 / (1.0 - self.s_pct / 100.0)


def effective_values(material: Material, record: Optional[CalibrationRecord]) -> EffectiveValues:
    sh = material.shrinkage
    s, s_src = (sh.typical, "table") if sh.typical is not None else (None, "none")
    lo, hi = (sh.min, sh.max) if sh.known else (None, None)
    spread_src = "table" if sh.known else "none"
    h = material.hole_offset_mm
    h_src = "table" if h is not None else "none"
    if record and record.runs:
        s, s_src = record.s_pct, "calibration"
        if record.s_range:
            lo, hi = record.s_range
            spread_src = "calibration"
        if record.h_mm is not None:
            h, h_src = record.h_mm, "calibration"
    return EffectiveValues(s, lo, hi, h, material.contour_offset_mm, s_src, spread_src, h_src)


@dataclass
class CouponOutcome:
    s_pct: float
    h_mm: Optional[float]
    record: Optional[CalibrationRecord]  # None when a confirmation coupon passed and nothing was stored
    confirmed: Optional[bool] = None  # set for confirmation (compensated) coupons
    worst_error_mm: Optional[float] = None


def record_coupon(
    store: CalibrationStore,
    material: str,
    printer: str,
    profile: str,
    square_model: float,
    square_measured: list[float],
    hole_models: list[float],
    holes_measured: list[float],
    compensated: bool = False,
    square_designed: Optional[float] = None,
    holes_designed: Optional[list[float]] = None,
    dried: Optional[bool] = None,
    walls: Optional[int] = None,
    infill: Optional[float] = None,
    tolerance: float = 0.05,
) -> CouponOutcome:
    """Turn one coupon's measurements into s and h and store them.

    A confirmation (compensated) coupon that lands within tolerance of the designed sizes
    confirms the stored values and changes nothing; one that doesn't is stored as a new run.
    """
    if holes_measured and len(holes_measured) != len(hole_models):
        raise ValueError(f"Expected {len(hole_models)} hole measurements, got {len(holes_measured)}")
    s_pct, h = compute_run(square_model, square_measured, list(zip(hole_models, holes_measured)))
    outcome = CouponOutcome(s_pct, h, None)
    if compensated:
        designed = square_designed if square_designed is not None else square_model
        designed_holes = holes_designed or hole_models
        errs = [m - designed for m in square_measured]
        errs += [d - dd for d, dd in zip(holes_measured, designed_holes)]
        outcome.worst_error_mm = max(abs(x) for x in errs)
        outcome.confirmed = outcome.worst_error_mm <= tolerance
        if outcome.confirmed:
            return outcome
    run = CalibrationRun(
        date=today(), s_pct=round(s_pct, 4), h_mm=None if h is None else round(h, 4),
        square_model_mm=square_model, square_measured_mm=list(square_measured),
        holes=[{"model": m, "measured": d} for m, d in zip(hole_models, holes_measured)],
        dried=dried, wall_loops=walls, infill_pct=infill, compensated=compensated,
    )
    outcome.record = store.add_run(material, printer, profile, run)
    return outcome


def today() -> str:
    return date.today().isoformat()
