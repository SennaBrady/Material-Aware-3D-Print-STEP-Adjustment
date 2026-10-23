"""3MF packaging of the print model, with slicer settings embedded.

The 3MF holds the mesh (3MF core spec) and, best-effort, Bambu Studio's per-object
settings file (Metadata/model_settings.config) carrying wall count, infill and the R5
compensation-off values. Filament-level settings (shrinkage, auto circle compensation)
can't be set per object, so they go in the plain settings list and the report.
A JSON copy of everything sits in Metadata/matprint_settings.json for other tools.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

import numpy as np

from .job import PrintSettings
from .settings_advisor import R5_LABELS, R5_SLICER_SETTINGS

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="config" ContentType="text/xml"/>
 <Default Extension="json" ContentType="application/json"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

IDENTITY = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"


def object_settings(settings: PrintSettings) -> dict[str, str]:
    """Per-object Bambu Studio keys: accepted walls/infill plus the R5 model-level compensation."""
    return {
        "wall_loops": str(settings.wall_loops),
        "sparse_infill_density": f"{settings.infill_pct:g}%",
        "xy_hole_compensation": R5_SLICER_SETTINGS["xy_hole_compensation"],
        "xy_contour_compensation": R5_SLICER_SETTINGS["xy_contour_compensation"],
    }


def settings_list(settings: PrintSettings, material: str) -> str:
    lines = [
        f"# Slicer settings for this print model ({material})",
        "# All compensation is already in the model. Turn every slicer-side compensation off,",
        "# or the part prints oversized.",
        "",
    ]
    for key, value in R5_SLICER_SETTINGS.items():
        lines.append(f"{R5_LABELS[key]:<52} = {value}")
    lines.append(f"{'Strength > Wall loops':<52} = {settings.wall_loops}")
    lines.append(f"{'Strength > Sparse infill density':<52} = {settings.infill_pct:g}%")
    lines.append(f"{'(line width assumed)':<52} = {settings.line_width_mm:g} mm")
    return "\n".join(lines) + "\n"


def _model_xml(vertices: np.ndarray, faces: np.ndarray, name: str) -> str:
    vs = "\n".join(f'     <vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>' for x, y, z in vertices)
    ts = "\n".join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <metadata name="Title">{name}</metadata>
 <metadata name="Application">matprint</metadata>
 <resources>
  <object id="1" type="model" name={quoteattr(name)}>
   <mesh>
    <vertices>
{vs}
    </vertices>
    <triangles>
{ts}
    </triangles>
   </mesh>
  </object>
 </resources>
 <build>
  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
 </build>
</model>
"""


def _bambu_config(name: str, settings: dict[str, str]) -> str:
    meta = "\n".join(f"    <metadata key={quoteattr(k)} value={quoteattr(v)}/>" for k, v in settings.items())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="1">
    <metadata key="name" value={quoteattr(name)}/>
    <metadata key="extruder" value="1"/>
{meta}
    <part id="1" subtype="normal_part">
      <metadata key="name" value={quoteattr(name)}/>
      <metadata key="matrix" value="{IDENTITY}"/>
    </part>
  </object>
</config>
"""


def write_3mf(
    path: Path,
    vertices: np.ndarray,
    faces: np.ndarray,
    name: str,
    settings: PrintSettings,
    material: str,
    bed_center: tuple[float, float] = (128.0, 128.0),
) -> None:
    v = np.asarray(vertices, dtype=float).copy()
    lo, hi = v.min(axis=0), v.max(axis=0)
    # Sit the part on the bed, centred (256 mm beds: X1, P1, A1).
    v[:, 0] += bed_center[0] - (lo[0] + hi[0]) / 2
    v[:, 1] += bed_center[1] - (lo[1] + hi[1]) / 2
    v[:, 2] -= lo[2]
    obj = object_settings(settings)
    payload = {
        "material": material,
        "object_settings": obj,
        "filament_settings": {
            "filament_shrink": R5_SLICER_SETTINGS["filament_shrink"],
            "auto_circle_holes_contour_compensation": R5_SLICER_SETTINGS["auto_circle_holes_contour_compensation"],
        },
        "line_width_mm": settings.line_width_mm,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", _model_xml(v, np.asarray(faces, dtype=int), name))
        z.writestr("Metadata/model_settings.config", _bambu_config(name, obj))
        z.writestr("Metadata/matprint_settings.json", json.dumps(payload, indent=2))
