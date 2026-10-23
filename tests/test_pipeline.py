"""End-to-end runs on the sample models, against the spec's acceptance criteria."""

import hashlib
import time
import zipfile

import cadquery as cq
import pytest
import trimesh
from click.testing import CliRunner

from matprint.calibration import CalibrationStore
from matprint.cli import main
from matprint.geometry import UnsupportedFormat, input_kind
from matprint.geometry import step_model as sm
from matprint.job import load_job
from matprint.materials import MaterialDB
from matprint.pipeline import AdjustError, run

DB = MaterialDB.load()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inner_loops(shape, z):
    sec = shape.intersect(cq.Face.makePlane(400, 400, basePnt=cq.Vector(0, 0, z)))
    return sorted(round(w.BoundingBox().xlen, 4) for f in sec.Faces() for w in f.innerWires())


def test_feature_detection(workdir):
    model = sm.analyse(sm.load_step(workdir / "locating_fixture.step"))
    by_id = {f.id: f for f in model.features}
    assert by_id["H1"].size == pytest.approx(8) and by_id["H3"].size == pytest.approx(12)
    assert by_id["B1"].kind == "boss" and by_id["B1"].size == pytest.approx(10)
    assert by_id["P1"].kind == "pocket"
    assert by_id["Z_HEIGHT"].size == pytest.approx(20)
    assert not model.other_vertical


def test_step_run_geometry_and_acceptance(workdir, tmp_path):
    src = workdir / "locating_fixture.step"
    before = sha(src)
    job = load_job(workdir / "locating_fixture_job.yaml")
    t0 = time.perf_counter()
    res = run(job, DB, CalibrationStore(), tmp_path / "out")
    assert time.perf_counter() - t0 < 10  # acceptance: under 10 s
    assert sha(src) == before  # acceptance: source byte-for-byte unchanged
    k = 1 / (1 - 0.004)
    out = sm.load_step(res.outputs["step"])
    bb = out.BoundingBox()
    assert bb.zlen == pytest.approx(20, abs=1e-5)  # acceptance: Z equals nominal
    assert bb.xlen == pytest.approx(80 * k, abs=1e-4)
    # R2 on the real geometry: holes are k·D + 2h, pocket walls move out by h too.
    loops = inner_loops(out, 3.0)
    assert loops == pytest.approx(sorted([k * 8 + 0.2, k * 8 + 0.2, k * 12 + 0.2]), abs=2e-3)
    pocket = inner_loops(out, 9.0)
    assert pocket[-1] == pytest.approx(k * 16 + 0.2, abs=2e-3)
    # R3 with c = 0: the boss is scaled only.
    sec = out.intersect(cq.Face.makePlane(400, 400, basePnt=cq.Vector(0, 0, 16)))
    assert sec.BoundingBox().xlen == pytest.approx(k * 10, abs=2e-3)
    mesh = trimesh.load(res.outputs["stl"])
    assert mesh.is_watertight
    assert mesh.extents[2] == pytest.approx(20, abs=1e-4)


def test_exported_settings_have_all_compensation_off(workdir, tmp_path):
    res = run(load_job(workdir / "locating_fixture_job.yaml"), DB, CalibrationStore(), tmp_path)
    txt = res.outputs["settings"].read_text()
    assert "Shrinkage" in txt and "= 100%" in txt
    assert "X-Y hole compensation" in txt and "X-Y contour compensation" in txt
    assert "Auto circle holes-contour compensation" in txt and "= off" in txt
    with zipfile.ZipFile(res.outputs["3mf"]) as z:
        cfg = z.read("Metadata/model_settings.config").decode()
        assert 'key="xy_hole_compensation" value="0"' in cfg
        assert 'key="xy_contour_compensation" value="0"' in cfg
        assert 'key="wall_loops" value="4"' in cfg
    html = res.outputs["report"].read_text()
    assert "Critical dimensions" in html and "Dowel spacing" in html


def test_suggestions_name_rule_and_dimension(workdir):
    res = run(load_job(workdir / "locating_fixture_job.yaml"), DB, CalibrationStore(), write=False)
    assert res.suggestions
    for s in res.suggestions:
        assert s.rule.startswith("S")
        assert s.reason.startswith("whole part") or any(d.name in s.reason for d in res.job.dimensions)
    assert res.advisor and res.advisor.alternatives  # dowel spacing fails in PETG
    assert all(a.worst for a in res.advisor.alternatives)


def test_stl_input_scales_and_flags_holes(workdir, tmp_path):
    res = run(load_job(workdir / "locating_fixture_stl_job.yaml"), DB, CalibrationStore(), tmp_path)
    k = 1 / (1 - 0.003)
    mesh = trimesh.load(res.outputs["stl"])
    assert mesh.extents == pytest.approx([80 * k, 50 * k, 20], abs=1e-3)
    hole = next(d for d in res.dims if d.dim.type == "hole")
    assert any("uncorrected" in f for f in hole.flags)
    assert "step" not in res.outputs


def test_pa6_job_suggests_alternatives(workdir):
    res = run(load_job(workdir / "locating_fixture_pa6_job.yaml"), DB, CalibrationStore(), write=False)
    assert any(d.hold == "fails" and d.dim.nominal == 50 for d in res.dims)
    assert res.advisor.alternatives


def test_orientation_moves_build_axis_to_z(workdir):
    shape = sm.orient(sm.load_step(workdir / "locating_fixture.step"), "+Y")
    bb = shape.BoundingBox()
    assert bb.zlen == pytest.approx(50) and bb.ylen == pytest.approx(20)


def test_sldprt_gets_export_instructions(tmp_path):
    with pytest.raises(UnsupportedFormat, match="STEP"):
        input_kind(tmp_path / "part.SLDPRT")


def test_uncalibrated_material_refuses_to_write(workdir, tmp_path):
    with pytest.raises(AdjustError, match="calibration coupon"):
        run(load_job(workdir / "locating_fixture_job.yaml"), DB, CalibrationStore(), tmp_path,
            material_name="PAHT CF15")


def test_cli_init_adjust_and_calibration_loop(workdir, tmp_path):
    r = CliRunner()
    res = r.invoke(main, ["init", str(workdir / "locating_fixture.step"), "-m", "PLA"])
    assert res.exit_code == 0, res.output
    job = workdir / "locating_fixture_job.yaml"
    assert "Hole H1" in job.read_text()
    res = r.invoke(main, ["adjust", str(job), "-o", str(tmp_path / "o"), "--no-prompt"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "o" / "locating_fixture_PRINT_PLA.3mf").exists()

    res = r.invoke(main, ["coupon", "-m", "PLA", "-o", str(tmp_path / "c")])
    assert res.exit_code == 0, res.output
    sizes = tmp_path / "c" / "coupon_PLA.json"
    res = r.invoke(main, ["calibrate", "--coupon", str(sizes), "--square-x", "19.94", "--square-y", "19.94",
                          "--holes", "3.86,5.85,7.83", "--dried"])
    assert res.exit_code == 0, res.output
    assert "s = 0.300%" in res.output
    res = r.invoke(main, ["calibration", "list"])
    assert "PLA | Bambu Lab | default" in res.output
    res = r.invoke(main, ["check", str(job)])
    assert "your calibration" in res.output
