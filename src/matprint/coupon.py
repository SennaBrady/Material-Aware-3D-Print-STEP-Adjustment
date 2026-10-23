"""Calibration coupon: an outer square for shrinkage and a grid of holes for the hole offset."""

from __future__ import annotations

import json
import math
from pathlib import Path

import cadquery as cq

MIN_WEB_MM = 1.0


def layout(square: float, holes: list[float]) -> list[tuple[float, float, float]]:
    """Place holes on a grid inside the square. Returns (x, y, diameter)."""
    n = len(holes)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    cell_x, cell_y = square / cols, square / rows
    out = []
    for i, d in enumerate(holes):
        r, c = divmod(i, cols)
        if d > min(cell_x, cell_y) - 2 * MIN_WEB_MM:
            raise ValueError(
                f"A {d:g} mm hole doesn't fit a {square:g} mm coupon with {n} holes; "
                f"use a bigger --square or fewer holes")
        out.append((-square / 2 + cell_x * (c + 0.5), square / 2 - cell_y * (r + 0.5), d))
    return out


def make_coupon(square: float = 20.0, holes: list[float] = (4.0, 6.0, 8.0), thickness: float = 4.0,
                k: float = 1.0, h: float = 0.0) -> tuple[cq.Shape, dict]:
    """Build the coupon. With k and h it is pre-compensated, for the confirmation print.

    Returns the solid and a dict of designed and modelled sizes for the sidecar file.
    """
    sq_model = k * square
    placed = layout(square, list(holes))
    wp = cq.Workplane("XY").box(sq_model, sq_model, thickness, centered=(True, True, False))
    model_holes = []
    for x, y, d in placed:
        dm = k * d + 2 * h
        wp = wp.cut(cq.Workplane("XY").center(k * x, k * y).circle(dm / 2).extrude(thickness))
        model_holes.append({"designed": d, "model": dm})
    meta = {
        "square_designed": square,
        "square_model": sq_model,
        "holes": model_holes,
        "thickness": thickness,
        "compensated": k != 1.0 or h != 0.0,
        "k": k,
        "h": h,
    }
    return wp.val(), meta


def write_coupon(out_dir: Path, name: str, shape: cq.Shape, meta: dict) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "step": out_dir / f"{name}.step",
        "stl": out_dir / f"{name}.stl",
        "json": out_dir / f"{name}.json",
    }
    cq.exporters.export(shape, str(paths["step"]), exportType="STEP")
    cq.exporters.export(shape, str(paths["stl"]), exportType="STL", tolerance=0.005, angularTolerance=0.05)
    paths["json"].write_text(json.dumps(meta, indent=2), "utf-8")
    return paths
