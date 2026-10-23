"""Formula-level tests: R1-R4, R6, hold check, calibration maths, job parsing."""

import pytest

from matprint.calibration import CalibrationRecord, CalibrationRun, compute_run, effective_values
from matprint.compensation import evaluate, hold_check, scale_factor
from matprint.job import Dimension, parse_tolerance
from matprint.materials import MaterialDB

DB = MaterialDB.load()


def dim(type_, nominal, tol=0.05):
    lo, hi = parse_tolerance(tol)
    return Dimension("d", type_, nominal, lo, hi)


def test_scale_factor():
    assert scale_factor(0.8) == pytest.approx(1 / 0.992)
    # Overview example: at 0.8% a 100 mm feature is modelled at 100.81 mm.
    assert 100 * scale_factor(0.8) == pytest.approx(100.81, abs=0.005)


def test_hole_and_contour_rules():
    m = DB.get("PETG")
    ev = effective_values(m, None)
    k = 1 / (1 - 0.004)
    assert evaluate(dim("hole", 8), m, ev, False).print_value == pytest.approx(k * 8 + 2 * 0.10)
    assert evaluate(dim("contour", 50), m, ev, False).print_value == pytest.approx(k * 50)
    assert evaluate(dim("z", 10), m, ev, False).print_value == 10  # R4


def test_stl_holes_get_scale_only_and_a_flag():
    m = DB.get("PLA")
    r = evaluate(dim("hole", 8), m, effective_values(m, None), mesh_input=True)
    assert r.print_value == pytest.approx(8 / (1 - 0.003))
    assert any("uncorrected" in f for f in r.flags)


def test_hold_check_examples_from_spec():
    # A 50 mm feature spreads about 1.15 mm in PA6 and 0.15 mm in PLA.
    pa6 = effective_values(DB.get("PA6"), None)
    pla = effective_values(DB.get("PLA"), None)
    status, spread = hold_check(dim("contour", 50, 0.1), pa6)
    assert status == "fails" and spread == pytest.approx(1.15)
    status, spread = hold_check(dim("contour", 50, 0.1), pla)
    assert status == "holds" and spread == pytest.approx(0.15)
    assert hold_check(dim("z", 50, 0.01), pa6)[0] == "n/a"


def test_unknown_shrinkage_is_unknown_not_pass():
    ev = effective_values(DB.get("PAHT CF15"), None)
    assert ev.k is None
    assert hold_check(dim("contour", 50, 0.1), ev)[0] == "unknown"


def test_min_feature_size_fails_conductive_pla():
    m = DB.get("Conductive PLA")
    record = CalibrationRecord("Conductive PLA", "p", "q", [
        CalibrationRun("2026-01-01", 0.3, 0.05, 20, [19.94, 19.94], []),
        CalibrationRun("2026-01-02", 0.32, 0.05, 20, [19.94, 19.94], []),
    ])
    r = evaluate(dim("contour", 0.8, 0.5), m, effective_values(m, record), False)
    assert r.hold == "fails" and any("minimum feature" in f for f in r.flags)


def test_calibration_maths():
    # 20 mm square measures 19.94 -> s = 0.3 %. An 8 mm hole that should come out
    # at 7.976 after shrinkage measures 7.876 -> h = 0.05 mm per side.
    s, h = compute_run(20.0, [19.94, 19.94], [(8.0, 7.876)])
    assert s == pytest.approx(0.3)
    assert h == pytest.approx(0.05)


def test_calibration_overrides_table_and_spread_needs_two_runs():
    m = DB.get("PLA")
    one = CalibrationRecord("PLA", "p", "q", [CalibrationRun("d", 0.25, 0.07, 20, [19.95, 19.95], [])])
    ev = effective_values(m, one)
    assert (ev.s_pct, ev.h_mm, ev.s_source, ev.spread_source) == (0.25, 0.07, "calibration", "table")
    two = CalibrationRecord("PLA", "p", "q", one.runs + [CalibrationRun("d", 0.27, 0.05, 20, [19.95, 19.95], [])])
    ev = effective_values(m, two)
    assert ev.spread_source == "calibration"
    assert (ev.s_min_pct, ev.s_max_pct) == (0.25, 0.27)
    assert ev.s_pct == pytest.approx(0.26) and ev.h_mm == pytest.approx(0.06)


def test_calibrated_pla_part_prints_within_005():
    """Acceptance: a calibrated PLA 20 mm square and 8 mm hole print within ±0.05 mm.

    Simulated printer: outer sizes shrink by s, holes shrink by s and close up by h per side.
    """
    s_true, h_true = 0.0034, 0.07
    coupon_sq = 20 * (1 - s_true)
    coupon_hole = 8 * (1 - s_true) - 2 * h_true
    s_pct, h = compute_run(20.0, [coupon_sq, coupon_sq], [(8.0, coupon_hole)])
    m = DB.get("PLA")
    rec = CalibrationRecord("PLA", "p", "q", [CalibrationRun("d", s_pct, h, 20, [coupon_sq] * 2, [])])
    ev = effective_values(m, rec)
    sq_model = evaluate(dim("contour", 20), m, ev, False).print_value
    hole_model = evaluate(dim("hole", 8), m, ev, False).print_value
    assert abs(sq_model * (1 - s_true) - 20) <= 0.05
    assert abs(hole_model * (1 - s_true) - 2 * h_true - 8) <= 0.05


@pytest.mark.parametrize("raw,expected", [
    (0.05, (-0.05, 0.05)),
    ([0.0, 0.1], (0.0, 0.1)),
    ([0.1, -0.02], (-0.02, 0.1)),
    ({"lower": -0.01, "upper": 0.03}, (-0.01, 0.03)),
])
def test_parse_tolerance(raw, expected):
    assert parse_tolerance(raw) == pytest.approx(expected)


def test_material_lookup_aliases():
    assert DB.get("pa6").name == "Nylon PA6"
    assert DB.get("PAHT-CF15").name == "PAHT CF15"
    with pytest.raises(KeyError):
        DB.get("unobtainium")
