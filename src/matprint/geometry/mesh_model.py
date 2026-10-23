"""STL meshes: R6 fallback, XY scale only."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

from .step_model import ROTATIONS


def load_stl(path: Path) -> trimesh.Trimesh:
    mesh = trimesh.load(str(path), force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ValueError(f"{path} has no triangles")
    return mesh


def orient(mesh: trimesh.Trimesh, up_axis: str) -> trimesh.Trimesh:
    rot = ROTATIONS[up_axis]
    mesh = mesh.copy()
    if rot is not None:
        axis, deg = rot
        mesh.apply_transform(trimesh.transformations.rotation_matrix(np.radians(deg), axis))
    return mesh


def scale_xy(mesh: trimesh.Trimesh, k: float) -> trimesh.Trimesh:
    mesh = mesh.copy()
    mesh.apply_transform(np.diag([k, k, 1.0, 1.0]))
    return mesh


def size(mesh: trimesh.Trimesh) -> tuple[float, float, float]:
    x, y, z = mesh.extents
    return float(x), float(y), float(z)


def export_stl(mesh: trimesh.Trimesh, path: Path) -> None:
    mesh.export(str(path), file_type="stl")
