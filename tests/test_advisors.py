from matprint.advisor import advise
from matprint.calibration import effective_values
from matprint.job import Dimension, HardRequirements, PrintSettings
from matprint.materials import MaterialDB
from matprint.settings_advisor import advise_settings

DB = MaterialDB.load()


def test_pa6_50mm_fails_and_an_alternative_passes():
    """Acceptance: a 50 mm ±0.1 feature in PA6 fails, and at least one alternative passes."""
    d = Dimension("Width", "contour", 50.0, -0.1, 0.1)
    res = advise(DB, DB.get("PA6"), [d], HardRequirements())
    assert res.alternatives, "expected at least one alternative"
    for alt in res.alternatives:
        ev = effective_values(alt.material, None)
        assert 50 * (ev.s_max_pct - ev.s_min_pct) / 100 <= 0.2
    assert len(res.alternatives) <= 3
    assert all(a.tradeoff for a in res.alternatives)
    # The same-base fibre grade has no shrinkage data, so it's suggested for calibration, not ranked.
    assert "PAHT CF15" in [a.material.name for a in res.needs_calibration]


def test_hard_requirements_filter_and_unknown_counts_as_miss():
    d = Dimension("Width", "contour", 50.0, -0.1, 0.1)
    res = advise(DB, DB.get("PA6"), [d], HardRequirements(max_temp_c=120))
    assert [a.material.name for a in res.alternatives] == ["PC"]
    res = advise(DB, DB.get("PETG"), [d], HardRequirements(chemicals={"water": "resistant"}))
    assert all(a.material.chem_level("water") == 2 for a in res.alternatives)


def test_fibre_grade_of_same_base_ranks_first_once_calibrated(tmp_path):
    from matprint.calibration import CalibrationRun, CalibrationStore

    store = CalibrationStore(tmp_path / "cal.json")
    for s in (0.30, 0.34):
        store.add_run("PAHT CF15", "Bambu Lab", "default", CalibrationRun("d", s, 0.1, 20, [19.9, 19.9], []))
    d = Dimension("Width", "contour", 50.0, -0.1, 0.1)
    res = advise(DB, DB.get("PA6"), [d], HardRequirements(), store, "Bambu Lab", "default")
    assert res.alternatives[0].material.name == "PAHT CF15"
    assert res.alternatives[0].tradeoff.startswith("keeps PA chemistry")


def test_settings_rules_name_rule_and_dimension():
    m = DB.get("PETG")
    dims = [Dimension("Bore", "hole", 8.0, 0.0, 0.1)]
    sugg, final = advise_settings(m, effective_values(m, None), PrintSettings(2, 0.42, 15), dims)
    rules = {s.rule: s for s in sugg}
    assert rules["S1"].value == 3  # 1.2 mm / 0.42 mm -> 3 walls
    assert "Bore" in rules["S2"].reason and rules["S2"].provisional
    assert rules["S3"].value == 20
    assert final.wall_loops == 4 and final.infill_pct == 20
    for s in sugg:
        assert s.reason.startswith("whole part") or "Bore" in s.reason


def test_s5_large_thick_high_shrink_part():
    m = DB.get("PA6")
    sugg, final = advise_settings(m, effective_values(m, None), PrintSettings(3, 0.42, 45), [], (150, 60, 20))
    assert {s.rule for s in sugg} == {"S5"}
    assert final.infill_pct == 25


def test_s6_when_settings_exceed_calibration_print():
    from matprint.calibration import CalibrationRecord, CalibrationRun

    m = DB.get("PLA")
    rec = CalibrationRecord("PLA", "p", "q", [
        CalibrationRun("d", 0.3, 0.05, 20, [19.94, 19.94], [], wall_loops=2, infill_pct=40)])
    ev = effective_values(m, rec)
    sugg, _ = advise_settings(m, ev, PrintSettings(5, 0.42, 60), [], None, rec)
    assert [s.rule for s in sugg] == ["S6"]
    sugg, _ = advise_settings(m, ev, PrintSettings(2, 0.42, 40), [], None, rec)
    assert sugg == []


def test_no_suggestions_when_settings_already_fit():
    m = DB.get("PLA")
    sugg, _ = advise_settings(m, effective_values(m, None), PrintSettings(2, 0.42, 40), [])
    assert sugg == []
