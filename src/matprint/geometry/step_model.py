"""STEP geometry: orientation, feature detection, hole/contour offsets and XY scaling.

All work happens on B-rep solids through OpenCascade (via CadQuery's OCP bindings).

Order of operations: offsets are applied to the exact original geometry first, then
the XY scale. Offsetting by h/k before scaling by k gives exactly k·D + 2h (R2) and
k·L + 2c (R3), and keeps cylinders analytic while the offsets are computed, which
the non-uniform scale would otherwise turn into B-splines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform, BRepBuilderAPI_MakeSolid
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepOffset import BRepOffset_MakeOffset, BRepOffset_Skin
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Intersection
from OCP.gp import gp_GTrsf, gp_Pln, gp_Pnt, gp_Dir
from OCP.ShapeFix import ShapeFix_Solid

VERTICAL_TOL = 1e-6
POS_TOL = 1e-4  # mm, for matching points to section wires
AXIS_TOL = 1e-3  # mm, for grouping coaxial cylinders

ROTATIONS = {  # build-up axis -> (rotation axis, degrees) that maps it onto +Z
    "+Z": None,
    "-Z": ((1, 0, 0), 180),
    "+X": ((0, 1, 0), -90),
    "-X": ((0, 1, 0), 90),
    "+Y": ((1, 0, 0), 90),
    "-Y": ((1, 0, 0), -90),
}


@dataclass
class Feature:
    id: str  # H1 hole, P1 pocket, B1 boss, BBOX_X / BBOX_Y / Z_HEIGHT extents
    kind: str  # hole | pocket | boss | extent
    size: float  # diameter for holes and bosses, length for extents, max XY span for pockets
    center: tuple[float, float] = (0.0, 0.0)
    z_range: tuple[float, float] = (0.0, 0.0)

    def describe(self) -> str:
        if self.kind in ("hole", "boss"):
            return (f"{self.id}  {self.kind:<6} Ø{self.size:.3f} at ({self.center[0]:.2f}, {self.center[1]:.2f}), "
                    f"z {self.z_range[0]:.2f}–{self.z_range[1]:.2f}")
        if self.kind == "pocket":
            return (f"{self.id}  pocket ~{self.size:.3f} across at ({self.center[0]:.2f}, {self.center[1]:.2f}), "
                    f"z {self.z_range[0]:.2f}–{self.z_range[1]:.2f}")
        return f"{self.id}  {self.size:.3f} mm"


@dataclass
class StepModel:
    shape: cq.Shape  # in print orientation (build-up axis = +Z)
    features: list[Feature] = field(default_factory=list)
    hole_faces: list[cq.Face] = field(default_factory=list)
    contour_faces: list[cq.Face] = field(default_factory=list)
    other_vertical: list[cq.Face] = field(default_factory=list)  # vertical faces not classified
    sloped: int = 0  # faces neither vertical nor flat (chamfers, drafts): XY scale only

    @property
    def size(self) -> tuple[float, float, float]:
        bb = self.shape.BoundingBox()
        return bb.xlen, bb.ylen, bb.zlen


def load_step(path: Path) -> cq.Shape:
    wp = cq.importers.importStep(str(path))
    solids = [s for v in wp.vals() if isinstance(v, cq.Shape) for s in v.Solids()]
    if not solids:
        raise ValueError(f"{path} contains no solid bodies")
    return solids[0] if len(solids) == 1 else cq.Compound.makeCompound(solids)


def orient(shape: cq.Shape, up_axis: str) -> cq.Shape:
    rot = ROTATIONS[up_axis]
    if rot is None:
        return shape
    axis, deg = rot
    return shape.rotate(cq.Vector(0, 0, 0), cq.Vector(*axis), deg)


def _is_vertical(face: cq.Face) -> bool:
    """True if the face normal is horizontal everywhere we sample it (a wall in XY)."""
    ad = BRepAdaptor_Surface(face.wrapped)
    u0, u1, v0, v1 = ad.FirstUParameter(), ad.LastUParameter(), ad.FirstVParameter(), ad.LastVParameter()
    if any(math.isinf(x) or abs(x) > 1e6 for x in (u0, u1, v0, v1)):
        u0, u1, v0, v1 = face._uvBounds()
    for a in (0.1, 0.5, 0.9):
        for b in (0.1, 0.5, 0.9):
            props = BRepLProp_SLProps(ad, u0 + (u1 - u0) * a, v0 + (v1 - v0) * b, 1, 1e-7)
            if props.IsNormalDefined() and abs(props.Normal().Z()) > VERTICAL_TOL:
                return False
    return True


def _point_on_face_at_mid_z(face: cq.Face) -> Optional[cq.Vector]:
    bb = face.BoundingBox()
    if bb.zlen < 1e-6:
        return None
    z = (bb.zmin + bb.zmax) / 2
    sec = BRepAlgoAPI_Section(face.wrapped, gp_Pln(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1)), False)
    sec.Build()
    edges = cq.Shape.cast(sec.Shape()).Edges()
    if not edges:
        return None
    return edges[0].positionAt(0.5)


class _Sectioner:
    """Cross-sections of the solid at given heights, cached."""

    def __init__(self, shape: cq.Shape):
        self.shape = shape
        bb = shape.BoundingBox()
        self.size = 4 * max(bb.xlen, bb.ylen, 1.0)
        self.cx, self.cy = bb.center.x, bb.center.y
        self.cache: dict[float, list[cq.Face]] = {}

    def faces_at(self, z: float) -> list[cq.Face]:
        key = round(z, 6)
        if key not in self.cache:
            plane = cq.Face.makePlane(self.size, self.size, basePnt=cq.Vector(self.cx, self.cy, z))
            self.cache[key] = self.shape.intersect(plane).Faces()
        return self.cache[key]

    def classify(self, p: cq.Vector) -> Optional[str]:
        """'hole' if p lies on an inner loop of the section at p.z, 'contour' if on an outer loop."""
        v = cq.Vertex.makeVertex(p.x, p.y, p.z)
        best, kind = POS_TOL, None
        for f in self.faces_at(p.z):
            d = f.outerWire().distance(v)
            if d < best:
                best, kind = d, "contour"
            for w in f.innerWires():
                d = w.distance(v)
                if d < best:
                    best, kind = d, "hole"
        return kind


def _cylinder_info(face: cq.Face):
    ad = BRepAdaptor_Surface(face.wrapped)
    if ad.GetType() != GeomAbs_Cylinder:
        return None
    cyl = ad.Cylinder()
    loc, d = cyl.Location(), cyl.Axis().Direction()
    if abs(abs(d.Z()) - 1.0) > 1e-6:
        return None
    span = abs(ad.LastUParameter() - ad.FirstUParameter())
    return (loc.X(), loc.Y()), cyl.Radius(), span


def _group_coaxial(faces: list[cq.Face]):
    """Group cylindrical faces by axis position and radius. Returns (groups, leftovers)."""
    groups: list[dict] = []
    left = []
    for f in faces:
        info = _cylinder_info(f)
        if info is None:
            left.append(f)
            continue
        (x, y), r, span = info
        for g in groups:
            if abs(g["x"] - x) < AXIS_TOL and abs(g["y"] - y) < AXIS_TOL and abs(g["r"] - r) < AXIS_TOL:
                g["faces"].append(f)
                g["span"] += span
                break
        else:
            groups.append({"x": x, "y": y, "r": r, "span": span, "faces": [f]})
    return groups, left


def _connected_groups(faces: list[cq.Face]) -> list[list[cq.Face]]:
    """Group faces that share edges (union-find over edge hashes)."""
    parent = list(range(len(faces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    edge_owner: dict[int, int] = {}
    for i, f in enumerate(faces):
        for e in f.Edges():
            h = e.hashCode()
            if h in edge_owner:
                parent[find(i)] = find(edge_owner[h])
            else:
                edge_owner[h] = i
    out: dict[int, list[cq.Face]] = {}
    for i, f in enumerate(faces):
        out.setdefault(find(i), []).append(f)
    return list(out.values())


def _z_range(faces: list[cq.Face]) -> tuple[float, float]:
    bbs = [f.BoundingBox() for f in faces]
    return min(b.zmin for b in bbs), max(b.zmax for b in bbs)


def analyse(shape: cq.Shape) -> StepModel:
    model = StepModel(shape)
    sectioner = _Sectioner(shape)
    for face in shape.Faces():
        if not _is_vertical(face):
            if not (face.geomType() == "PLANE" and abs(abs(face.normalAt().z) - 1) < VERTICAL_TOL):
                model.sloped += 1
            continue
        p = _point_on_face_at_mid_z(face)
        kind = sectioner.classify(p) if p is not None else None
        if kind == "hole":
            model.hole_faces.append(face)
        elif kind == "contour":
            model.contour_faces.append(face)
        else:
            model.other_vertical.append(face)

    feats: list[Feature] = []
    # Holes: closed inner loops. Fully circular ones become H#, anything else a pocket P#.
    n_h = n_p = 0
    for group in _connected_groups(model.hole_faces):
        cyl_groups, rest = _group_coaxial(group)
        z0, z1 = _z_range(group)
        if not rest and len(cyl_groups) == 1 and cyl_groups[0]["span"] >= 2 * math.pi - 1e-3:
            g = cyl_groups[0]
            n_h += 1
            feats.append(Feature(f"H{n_h}", "hole", 2 * g["r"], (g["x"], g["y"]), (z0, z1)))
        else:
            bb = cq.Compound.makeCompound(group).BoundingBox()
            n_p += 1
            feats.append(Feature(f"P{n_p}", "pocket", max(bb.xlen, bb.ylen),
                                 (bb.center.x, bb.center.y), (z0, z1)))
    # Bosses: full-circle cylinders on an outer contour.
    cyl_groups, _ = _group_coaxial(model.contour_faces)
    n_b = 0
    for g in cyl_groups:
        if g["span"] >= 2 * math.pi - 1e-3:
            n_b += 1
            feats.append(Feature(f"B{n_b}", "boss", 2 * g["r"], (g["x"], g["y"]), _z_range(g["faces"])))
    x, y, z = model.size
    feats += [Feature("BBOX_X", "extent", x), Feature("BBOX_Y", "extent", y), Feature("Z_HEIGHT", "extent", z)]
    model.features = feats
    return model


def _to_solid(shape) -> cq.Shape:
    s = cq.Shape.cast(shape)
    if s.ShapeType() == "Solid":
        fix = ShapeFix_Solid(s.wrapped)
    else:
        shells = s.Shells()
        if len(shells) != 1:
            raise RuntimeError("offset produced an unexpected shape")
        fix = ShapeFix_Solid(BRepBuilderAPI_MakeSolid(shells[0].wrapped).Solid())
    fix.Perform()
    return cq.Shape.cast(fix.Solid())


def offset_faces(shape: cq.Shape, offsets: list[tuple[cq.Face, float]]) -> cq.Shape:
    """Move individual faces along their outward normals; all other faces stay put."""
    mo = BRepOffset_MakeOffset()
    mo.Initialize(shape.wrapped, 0.0, 1e-6, BRepOffset_Skin, False, False, GeomAbs_Intersection, False, False)
    for face, value in offsets:
        mo.SetOffsetOnFace(face.wrapped, value)
    mo.MakeOffsetShape()
    if not mo.IsDone():
        raise RuntimeError(f"face offset failed: {mo.Error()}")
    result = _to_solid(mo.Shape())
    if not result.isValid():
        raise RuntimeError("face offset produced an invalid solid")
    return result


def _offset_per_solid(shape: cq.Shape, offsets: list[tuple[cq.Face, float]]) -> cq.Shape:
    solids = shape.Solids()
    if len(solids) == 1:
        return offset_faces(solids[0], offsets)
    out = []
    for solid in solids:
        own = {f.hashCode() for f in solid.Faces()}
        mine = [(f, v) for f, v in offsets if f.hashCode() in own]
        out.append(offset_faces(solid, mine) if mine else solid)
    return cq.Compound.makeCompound(out)


def _cut_round_holes(shape: cq.Shape, model: StepModel, dr: float) -> cq.Shape:
    """Fallback for R2: re-cut each round hole at radius r + dr (dr > 0 only)."""
    for f in model.features:
        if f.kind != "hole":
            continue
        z0, z1 = f.z_range
        tool = cq.Solid.makeCylinder(f.size / 2 + dr, z1 - z0, cq.Vector(f.center[0], f.center[1], z0))
        shape = shape.cut(tool)
    return shape


def scale_xy(shape: cq.Shape, k: float) -> cq.Shape:
    """R1/R4: scale X and Y by k about the origin, leave Z alone."""
    g = gp_GTrsf()
    g.SetValue(1, 1, k)
    g.SetValue(2, 2, k)
    g.SetValue(3, 3, 1.0)
    return cq.Shape.cast(BRepBuilderAPI_GTransform(shape.wrapped, g, True).Shape())


def compensate(model: StepModel, k: float, h: float, c: float) -> tuple[cq.Shape, list[str]]:
    """Apply R2/R3 offsets (pre-divided by k) and then the R1 XY scale. Returns (shape, warnings)."""
    warnings: list[str] = []
    shape = model.shape
    offsets = []
    if abs(h) > 1e-9:
        # Outward normal of a hole wall points into the hole, so enlarging it is a negative offset.
        offsets += [(f, -h / k) for f in model.hole_faces]
    if abs(c) > 1e-9:
        offsets += [(f, c / k) for f in model.contour_faces]
    if offsets:
        try:
            shape = _offset_per_solid(shape, offsets)
        except RuntimeError as exc:
            if abs(c) > 1e-9:
                warnings.append(f"contour offset could not be applied ({exc}); outer contours got the XY scale only")
            if abs(h) > 1e-9:
                if h > 0:
                    shape = _cut_round_holes(model.shape, model, h / k)
                    if any(f.kind == "pocket" for f in model.features):
                        warnings.append("hole offset applied to round holes only; pockets got the XY scale only")
                else:
                    warnings.append("negative hole offset could not be applied; holes got the XY scale only")
    if model.other_vertical:
        warnings.append(f"{len(model.other_vertical)} vertical face(s) could not be classified as hole or "
                        "contour and got the XY scale only")
    if model.sloped and (abs(h) > 1e-9 or abs(c) > 1e-9):
        warnings.append(f"{model.sloped} sloped face(s) (chamfers, drafts, fillets on edges) got the XY scale only")
    return scale_xy(shape, k), warnings


def export_step(shape: cq.Shape, path: Path) -> None:
    cq.exporters.export(shape, str(path), exportType="STEP")


def export_stl(shape: cq.Shape, path: Path, tolerance: float = 0.005, angular: float = 0.05) -> None:
    cq.exporters.export(shape, str(path), exportType="STL", tolerance=tolerance, angularTolerance=angular)
