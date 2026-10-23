"""The adjust pipeline: load, classify, look up values, check, compensate, advise, write."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import export3mf
from .advisor import AdvisorResult, advise
from .calibration import CalibrationStore, EffectiveValues, effective_values
from .compensation import DimResult, evaluate
from .geometry import input_kind
from .job import Dimension, Job, PrintSettings
from .materials import Material, MaterialDB
from .settings_advisor import Suggestion, advise_settings


class AdjustError(RuntimeError):
    pass


@dataclass
class RunResult:
    job: Job
    material: Material
    ev: EffectiveValues
    kind: str  # step | stl
    dims: list[DimResult]
    features: list = field(default_factory=list)
    advisor: Optional[AdvisorResult] = None
    suggestions: list[Suggestion] = field(default_factory=list)
    final_settings: Optional[PrintSettings] = None
    outputs: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    part_size: Optional[tuple[float, float, float]] = None
    elapsed_s: float = 0.0

    @property
    def failing(self) -> list[DimResult]:
        return [d for d in self.dims if d.hold == "fails"]

    @property
    def all_hold(self) -> bool:
        return all(d.hold in ("holds", "n/a") for d in self.dims)


def material_slug(material: Material) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", material.name).strip("-")


def output_stem(part: Path, material: Material) -> str:
    return f"{part.stem}_PRINT_{material_slug(material)}"


def _match_feature(dim: Dimension, features: list) -> tuple[Optional[str], Optional[str]]:
    """Return (feature id, warning)."""
    by_id = {f.id: f for f in features}
    if dim.feature:
        if dim.feature not in by_id:
            return None, f"{dim.name}: feature {dim.feature} not found in the model (run `matprint features`)"
        return dim.feature, None
    if dim.type == "hole":
        pool = [f for f in features if f.kind in ("hole", "pocket")]
    elif dim.type == "contour":
        pool = [f for f in features if f.kind == "boss" or f.id in ("BBOX_X", "BBOX_Y")]
    elif dim.type == "z":
        pool = [f for f in features if f.id == "Z_HEIGHT"]
    else:
        return None, None
    close = [f for f in pool if abs(f.size - dim.nominal) <= max(0.5, 0.05 * dim.nominal)]
    if not close:
        if dim.type == "hole":
            return None, (f"{dim.name}: no vertical hole near {dim.nominal:g} mm found; "
                          "check build_up_axis, or the offset won't reach this hole")
        return None, None
    best = min(abs(f.size - dim.nominal) for f in close)
    ids = [f.id for f in close if abs(abs(f.size - dim.nominal) - best) < 1e-6]
    return ", ".join(ids), None


def evaluate_job(job: Job, db: MaterialDB, store: Optional[CalibrationStore], material_name: Optional[str] = None,
                 kind: str = "step", features: Optional[list] = None,
                 part_size: Optional[tuple[float, float, float]] = None) -> RunResult:
    """Everything except geometry: values, hold check, advisors."""
    material = db.get(material_name or job.material)
    record = store.get(material.name, job.printer, job.profile) if store else None
    ev = effective_values(material, record)
    res = RunResult(job, material, ev, kind, [], features or [], part_size=part_size)
    for d in job.dimensions:
        r = evaluate(d, material, ev, mesh_input=(kind == "stl"))
        if features:
            r.feature, warn = _match_feature(d, features)
            if warn:
                res.warnings.append(warn)
        if r.hold == "unknown":
            r.flags.append(f"{material.name} has no shrinkage range: calibrate to check this dimension")
        res.dims.append(r)
    if res.failing or job.hard.any:
        adv = advise(db, material, job.dimensions, job.hard, store, job.printer, job.profile)
        if res.failing or adv.original_hard_failures:
            res.advisor = adv
    res.suggestions, res.final_settings = advise_settings(
        material, ev, job.print_settings, job.dimensions, part_size, record)
    if ev.h_source == "none" and any(d.type == "hole" for d in job.dimensions):
        res.warnings.append(f"{material.name} has no hole offset yet; holes get the XY scale only until calibrated")
    return res


def run(job: Job, db: MaterialDB, store: Optional[CalibrationStore], out_dir: Optional[Path] = None,
        material_name: Optional[str] = None, write: bool = True) -> RunResult:
    t0 = time.perf_counter()
    if job.part is None:
        raise AdjustError("The job file has no 'part:' entry")
    part = Path(job.part)
    if not part.exists():
        raise AdjustError(f"Model not found: {part}")
    kind = input_kind(part)

    if kind == "step":
        from .geometry import step_model as geo
        model = geo.analyse(geo.orient(geo.load_step(part), job.build_up_axis))
        features, size = model.features, model.size
    else:
        from .geometry import mesh_model as geo
        mesh = geo.orient(geo.load_stl(part), job.build_up_axis)
        features, size = [], geo.size(mesh)

    res = evaluate_job(job, db, store, material_name, kind, features, size)
    if kind == "stl":
        res.warnings.append("STL input: XY scale only (R6). Holes are not corrected; use STEP for hole offsets.")
    if not write:
        res.elapsed_s = time.perf_counter() - t0
        return res

    ev = res.ev
    if ev.k is None:
        raise AdjustError(f"{res.material.name} has no shrinkage value. Print a calibration coupon "
                          "(`matprint coupon`) and enter the measurements (`matprint calibrate`) first.")

    out_dir = Path(out_dir) if out_dir else part.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem(part, res.material)
    paths = {
        "stl": out_dir / f"{stem}.stl",
        "3mf": out_dir / f"{stem}.3mf",
        "settings": out_dir / f"{stem}_slicer_settings.txt",
        "report": out_dir / f"{stem}_report.html",
        "json": out_dir / f"{stem}_report.json",
    }
    if kind == "step":
        paths = {"step": out_dir / f"{stem}.step", **paths}
    src = part.resolve()
    for p in paths.values():
        if p.resolve() == src:
            raise AdjustError(f"Refusing to overwrite the source model {part}")

    import trimesh

    if kind == "step":
        h = ev.h_mm if ev.h_mm is not None else 0.0
        shape, warns = geo.compensate(model, ev.k, h, ev.c_mm)
        res.warnings += warns
        geo.export_step(shape, paths["step"])
        geo.export_stl(shape, paths["stl"])
        mesh = trimesh.load(str(paths["stl"]), force="mesh")
    else:
        mesh = geo.scale_xy(mesh, ev.k)
        geo.export_stl(mesh, paths["stl"])

    final = res.final_settings
    export3mf.write_3mf(paths["3mf"], mesh.vertices, mesh.faces, stem, final, res.material.name)
    paths["settings"].write_text(export3mf.settings_list(final, res.material.name), "utf-8")
    res.outputs = paths
    res.elapsed_s = time.perf_counter() - t0

    from .report import render_html, to_json
    paths["report"].write_text(render_html(res), "utf-8")
    paths["json"].write_text(json.dumps(to_json(res), indent=2), "utf-8")
    return res
