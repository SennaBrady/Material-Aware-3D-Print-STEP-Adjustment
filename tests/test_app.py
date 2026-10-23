"""The app's HTTP API, driven the way the page drives it."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from matprint.app.server import AppState, Handler


@pytest.fixture
def app(tmp_path):
    Handler.state = AppState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, data=data, headers=headers or {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                ctype = r.headers.get("Content-Type", "")
                payload = r.read()
                return json.loads(payload) if "json" in ctype else payload
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read())}

    yield call
    httpd.shutdown()


def job(model_id, dims, material="PETG", **extra):
    return {"model_id": model_id, "material": material, "printer": "Bambu Lab", "profile": "default",
            "print_settings": {"wall_loops": 2, "infill_pct": 15, "line_width_mm": 0.42, "purpose": "functional"},
            "hard_requirements": {"max_temp_c": None, "chemicals": {}}, "dimensions": dims, **extra}


def test_page_and_assets_are_served(app):
    assert b"Material-Aware Print" in app("/")
    assert b"three" in app("/static/app.js")
    assert len(app("/static/vendor/three/three.module.min.js")) > 100_000
    assert app("/static/../server.py").get("status") == 404


def test_open_check_build(app, workdir, tmp_path):
    step = (workdir / "locating_fixture.step").read_bytes()
    info = app("/api/upload", raw=step, headers={"X-Filename": quote("locating_fixture.step")})
    assert info["kind"] == "step" and info["source"] == "upload"
    assert {f["id"] for f in info["features"]} >= {"H1", "H2", "H3", "B1", "P1"}
    assert len(app(f"/api/model/{info['id']}/preview.stl")) > 1000

    dims = info["dimensions"]
    res = app("/api/check", job(info["id"], dims))
    assert res["material"] == "PETG"
    overall_x = next(d for d in res["dimensions"] if d["name"] == "Overall X")
    assert overall_x["hold"] == "fails" and res["alternatives"]
    rules = {s["rule"] for s in res["settings_suggestions"]}
    assert "S1" in rules and res["final_settings"]["wall_loops"] == 4

    # Untick S2: walls drop back to S1's 3.
    res = app("/api/check", job(info["id"], dims, rejected=["S2"]))
    assert res["final_settings"]["wall_loops"] == 3

    out = tmp_path / "prints"
    built = app("/api/build", job(info["id"], dims, rejected=["S2"], save_mode="folder", output_dir=str(out)))
    names = {f["name"] for f in built["files"]}
    assert "locating_fixture_PRINT_PETG.3mf" in names and "locating_fixture_PRINT_PETG.step" in names
    assert (out / "locating_fixture_PRINT_PETG.3mf").exists()
    report = next(f for f in built["files"] if f["kind"] == "report")
    assert b"Critical dimensions" in app(report["url"])


def test_unticked_dimensions_are_ignored(app, workdir):
    info = app("/api/upload", raw=(workdir / "locating_fixture.step").read_bytes(),
               headers={"X-Filename": "locating_fixture.step"})
    dims = [dict(d, critical=d["name"] != "Overall X") for d in info["dimensions"]]
    res = app("/api/check", job(info["id"], dims))
    assert "Overall X" not in [d["name"] for d in res["dimensions"]]


def test_sldprt_is_explained(app):
    res = app("/api/upload", raw=b"not really", headers={"X-Filename": "part.SLDPRT"})
    assert res["status"] == 400 and "STEP" in res["error"]


def test_orientation_and_stl(app, workdir):
    info = app("/api/upload", raw=(workdir / "locating_fixture.stl").read_bytes(),
               headers={"X-Filename": "locating_fixture.stl"})
    assert info["kind"] == "stl" and info["warnings"]
    turned = app("/api/orient", {"model_id": info["id"], "up": "+Y"})
    assert turned["size"][2] == pytest.approx(50, abs=1e-3)


def test_coupon_and_calibration_round_trip(app, tmp_path):
    c = app("/api/coupon", {"material": "PLA", "square": 20, "holes": [4, 6, 8], "output_dir": str(tmp_path)})
    assert {f["kind"] for f in c["files"]} >= {"step", "stl", "3mf"}
    res = app("/api/calibrate", {"coupon": c["meta"], "square_x": 19.94, "square_y": 19.94,
                                 "holes": [3.86, 5.85, 7.83], "dried": True, "walls": 2, "infill": 15})
    assert res["stored"] and res["s_pct"] == pytest.approx(0.3)
    mats = {m["name"]: m for m in app("/api/materials")}
    assert mats["PLA"]["s_source"] == "calibration"
    assert app("/api/calibration")[0]["material"] == "PLA"
    app("/api/calibration/clear", {"material": "PLA", "printer": "Bambu Lab", "profile": "default"})
    assert app("/api/calibration") == []
