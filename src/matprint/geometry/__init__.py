"""Geometry back ends: STEP (B-rep, full compensation) and STL (mesh, XY scale only)."""

from pathlib import Path

STEP_SUFFIXES = {".step", ".stp"}
STL_SUFFIXES = {".stl"}
SLDPRT_SUFFIXES = {".sldprt"}

SLDPRT_HELP = (
    "SolidWorks .sldprt files are a closed, proprietary format that can't be read without SolidWorks. "
    "In SolidWorks use File > Save As > STEP AP214 or AP242 (*.step), then run matprint on the .step file. "
    "On Windows with SolidWorks installed, tools/sldprt_to_step.py can batch-export a folder of parts."
)


class UnsupportedFormat(ValueError):
    pass


def input_kind(path: Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in STEP_SUFFIXES:
        return "step"
    if suffix in STL_SUFFIXES:
        return "stl"
    if suffix in SLDPRT_SUFFIXES:
        raise UnsupportedFormat(SLDPRT_HELP)
    raise UnsupportedFormat(f"Unsupported model format '{suffix}'. Use STEP (.step/.stp) or STL (.stl).")
