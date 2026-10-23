"""Rebuild the sample models in this folder: python samples/make_samples.py"""

from pathlib import Path

import cadquery as cq

HERE = Path(__file__).parent


def locating_fixture() -> cq.Shape:
    """An 80 x 50 x 12 mm fixture plate: two Ø8 dowel holes, a Ø12 bore, a pocket and a Ø10 boss."""
    return (
        cq.Workplane("XY")
        .box(80, 50, 12, centered=(True, True, False))
        .edges("|Z").fillet(5)
        .faces(">Z").workplane()
        .pushPoints([(-30, -15), (30, -15)]).hole(8)
        .faces(">Z").workplane().center(0, -5).hole(12)
        .faces(">Z").workplane().center(-20, 12).rect(16, 10).cutBlind(-6)
        .faces(">Z").workplane().center(20, 12).circle(5).extrude(8)
        .val()
    )


if __name__ == "__main__":
    part = locating_fixture()
    cq.exporters.export(part, str(HERE / "locating_fixture.step"), exportType="STEP")
    cq.exporters.export(part, str(HERE / "locating_fixture.stl"), exportType="STL",
                        tolerance=0.005, angularTolerance=0.05)
    print("Wrote locating_fixture.step and locating_fixture.stl")
