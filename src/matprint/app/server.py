"""Local web app: a small HTTP server on 127.0.0.1 that serves the page and a JSON API.

Nothing leaves the computer. The browser is only the window: models are read from disk
(native file dialog) or uploaded to this server, and print files are written locally.
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..calibration import CalibrationStore, effective_values, record_coupon
from ..geometry import SLDPRT_HELP, UnsupportedFormat
from ..job import (DEFAULT_SETTINGS, Dimension, HardRequirements, Job, PrintSettings, parse_tolerance)
from ..materials import MaterialDB
from ..pipeline import AdjustError, LoadedModel, load_model, material_slug, run
from ..report import to_json

DEFAULT_PORT = 8765
IDLE_EXIT_S = 150  # quit when no page has checked in for this long
STATIC = resources.files("matprint.app").joinpath("static")


def settings_path() -> Path:
    home = os.environ.get("MATPRINT_HOME")
    return (Path(home) if home else Path.home() / ".matprint") / "app_settings.json"


def default_output_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "matprint output"


@dataclass
class OpenModel:
    id: str
    name: str  # original file name
    loaded: LoadedModel
    source: str  # "disk" (path known) or "upload"
    preview: Optional[bytes] = None


class AppState:
    def __init__(self, db_path: Optional[Path] = None, calibration_path: Optional[Path] = None):
        self.db_path = db_path
        self.calibration_path = calibration_path
        self.models: dict[str, OpenModel] = {}
        self.runs: dict[str, Path] = {}  # run id -> folder of files it may serve
        self.files: dict[str, dict[str, Path]] = {}  # run id -> name -> path
        self.tmp = Path(tempfile.mkdtemp(prefix="matprint-"))
        self.lock = threading.Lock()
        self.last_ping = time.time()
        self.busy = 0
        self.settings = self._load_settings()

    # --- settings that survive restarts (output folder etc.)
    def _load_settings(self) -> dict:
        defaults = {"save_mode": "next_to_original", "output_dir": str(default_output_dir()),
                    "printer": "Bambu Lab", "profile": "default", "material": "PLA"}
        try:
            defaults.update(json.loads(settings_path().read_text("utf-8")))
        except (OSError, ValueError):
            pass
        return defaults

    def save_settings(self, updates: dict) -> dict:
        allowed = {"save_mode", "output_dir", "printer", "profile", "material", "last_dir"}
        self.settings.update({k: v for k, v in updates.items() if k in allowed})
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.settings, indent=2), "utf-8")
        return self.settings

    @property
    def db(self) -> MaterialDB:
        return MaterialDB.load(self.db_path)

    @property
    def store(self) -> CalibrationStore:
        return CalibrationStore(self.calibration_path)

    def register_files(self, paths: dict[str, Path]) -> str:
        rid = uuid.uuid4().hex[:12]
        self.files[rid] = {p.name: p for p in paths.values()}
        return rid


# ---------- request -> engine objects ----------

def job_from_request(data: dict, model: OpenModel) -> Job:
    ps = data.get("print_settings") or {}
    settings = PrintSettings(
        wall_loops=int(ps.get("wall_loops") or DEFAULT_SETTINGS.wall_loops),
        line_width_mm=float(ps.get("line_width_mm") or DEFAULT_SETTINGS.line_width_mm),
        infill_pct=float(ps.get("infill_pct") if ps.get("infill_pct") not in (None, "") else DEFAULT_SETTINGS.infill_pct),
        purpose=str(ps.get("purpose") or "functional"),
    )
    hard_raw = data.get("hard_requirements") or {}
    max_t = hard_raw.get("max_temp_c")
    hard = HardRequirements(
        max_temp_c=float(max_t) if max_t not in (None, "") else None,
        chemicals={k: v for k, v in (hard_raw.get("chemicals") or {}).items() if v},
    )
    dims = []
    for d in data.get("dimensions") or []:
        if not d.get("critical", True):
            continue
        lo, hi = parse_tolerance([-abs(float(d.get("minus") or 0)), abs(float(d.get("plus") or 0))])
        dims.append(Dimension(str(d.get("name") or "Dimension"), str(d["type"]), float(d["nominal"]), lo, hi,
                              d.get("feature") or None))
    return Job(part=model.loaded.path, material=str(data.get("material") or "PLA"), dimensions=dims,
               build_up_axis=model.loaded.up_axis, printer=str(data.get("printer") or "Bambu Lab"),
               profile=str(data.get("profile") or "default"), print_settings=settings, settings_given=True,
               hard=hard)


def suggested_dimensions(features: list) -> list[dict]:
    out = []
    for f in features:
        if f.kind == "hole":
            out.append({"name": f"Hole {f.id}", "type": "hole", "feature": f.id,
                        "nominal": round(f.size, 3), "minus": 0.1, "plus": 0.1, "critical": True})
        elif f.kind == "pocket":
            out.append({"name": f"Pocket {f.id}", "type": "hole", "feature": f.id,
                        "nominal": round(f.size, 3), "minus": 0.2, "plus": 0.2, "critical": False})
        elif f.kind == "boss":
            out.append({"name": f"Boss {f.id}", "type": "contour", "feature": f.id,
                        "nominal": round(f.size, 3), "minus": 0.1, "plus": 0.1, "critical": True})
        elif f.id in ("BBOX_X", "BBOX_Y"):
            out.append({"name": f"Overall {f.id[-1]}", "type": "contour", "feature": f.id,
                        "nominal": round(f.size, 3), "minus": 0.2, "plus": 0.2, "critical": True})
        elif f.id == "Z_HEIGHT":
            out.append({"name": "Height", "type": "z", "feature": f.id,
                        "nominal": round(f.size, 3), "minus": 0.2, "plus": 0.2, "critical": True})
    return out


def model_info(m: OpenModel) -> dict:
    lm = m.loaded
    return {
        "id": m.id,
        "name": m.name,
        "kind": lm.kind,
        "source": m.source,
        "path": str(lm.path) if m.source == "disk" else None,
        "up": lm.up_axis,
        "size": [round(v, 3) for v in lm.size],
        "features": [{"id": f.id, "kind": f.kind, "size": f.size, "center": list(f.center),
                      "z_range": list(f.z_range)} for f in lm.features],
        "dimensions": suggested_dimensions(lm.features) if lm.kind == "step" else [
            {"name": "Overall X", "type": "contour", "feature": "", "nominal": round(lm.size[0], 3),
             "minus": 0.2, "plus": 0.2, "critical": True},
            {"name": "Overall Y", "type": "contour", "feature": "", "nominal": round(lm.size[1], 3),
             "minus": 0.2, "plus": 0.2, "critical": True},
            {"name": "Height", "type": "z", "feature": "", "nominal": round(lm.size[2], 3),
             "minus": 0.2, "plus": 0.2, "critical": True},
        ],
        "warnings": ([] if lm.kind == "step" else [
            "STL is a mesh, so the tool can only scale it in X and Y. Holes can't be corrected; "
            "open the STEP file instead for hole offsets."]),
    }


def material_list(state: AppState, printer: str, profile: str) -> list[dict]:
    store = state.store
    out = []
    for m in state.db:
        rec = store.get(m.name, printer, profile)
        ev = effective_values(m, rec)
        out.append({
            "name": m.name, "base": m.base, "filler": m.filler,
            "s_pct": ev.s_pct, "s_min": ev.s_min_pct, "s_max": ev.s_max_pct, "h_mm": ev.h_mm,
            "s_source": ev.s_source, "h_source": ev.h_source, "spread_source": ev.spread_source,
            "max_temp_c": m.max_temp_conservative, "chemical": m.chemical, "uv": m.uv_resistant,
            "conductive": m.conductive, "notes": m.notes, "usable": ev.k is not None,
            "min_wall_mm": m.min_wall_mm, "infill": m.infill_pct,
        })
    return out


def result_json(res) -> dict:
    data = to_json(res)
    data["final_settings"] = {
        "wall_loops": res.final_settings.wall_loops,
        "infill_pct": res.final_settings.infill_pct,
    }
    data["all_hold"] = res.all_hold
    data["needs_calibration"] = [
        {"material": a.material.name, "tradeoff": a.tradeoff} for a in (res.advisor.needs_calibration if res.advisor else [])
    ]
    data["hard_failures"] = res.advisor.original_hard_failures if res.advisor else []
    data["can_build"] = res.ev.k is not None
    return data


ALL_RULES = {"S1", "S2", "S3", "S4", "S5", "S6"}


def accepted_rules(data: dict) -> Optional[set[str]]:
    """The page sends the suggestions the user unticked; everything else counts as accepted."""
    if data.get("rejected") is not None:
        return ALL_RULES - set(data["rejected"])
    if data.get("accepted") is not None:
        return set(data["accepted"])
    return None


MODEL_PATTERNS = "*.step *.stp *.STEP *.STP *.stl *.STL *.sldprt *.SLDPRT"


def _desktop_dialog(kind: str, initial: str) -> Optional[str]:
    """KDE's or GNOME's own picker on Linux. None if neither is installed."""
    if sys.platform != "linux":
        return None
    if shutil.which("kdialog"):
        cmd = (["kdialog", "--getexistingdirectory", initial] if kind == "folder" else
               ["kdialog", "--getopenfilename", initial, f"{MODEL_PATTERNS}|Part models"])
    elif shutil.which("zenity"):
        cmd = (["zenity", "--file-selection", "--directory", f"--filename={initial}/"] if kind == "folder" else
               ["zenity", "--file-selection", f"--filename={initial}/", f"--file-filter=Part models | {MODEL_PATTERNS}"])
    else:
        return None
    out = subprocess.run(cmd, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def native_dialog(kind: str, initial: str = "") -> Optional[str]:
    """Show the system's file or folder picker. Returns a path, '' if cancelled, None if unavailable."""
    initial = initial if initial and Path(initial).exists() else str(Path.home())
    try:
        picked = _desktop_dialog(kind, initial)
    except OSError:
        picked = None
    if picked is not None:
        return picked
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog as fd\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        f"kind, init = {kind!r}, {initial!r}\n"
        "if kind == 'folder':\n"
        "    p = fd.askdirectory(initialdir=init, title='Choose a folder for the print files')\n"
        "else:\n"
        "    p = fd.askopenfilename(initialdir=init, title='Open a part model', filetypes=[\n"
        f"        ('Part models', {MODEL_PATTERNS!r}), ('All files', '*')])\n"
        "print(p or '')\n"
    )
    try:
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=900)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    state: AppState  # set on the class by serve()
    server_version = f"matprint/{__version__}"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # helpers
    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200):
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _error(self, message: str, status: int = 400):
        self._json({"error": message}, status)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json_body(self) -> dict:
        raw = self._body()
        return json.loads(raw or b"{}")

    def _model(self, mid: str) -> OpenModel:
        m = self.state.models.get(mid)
        if not m:
            raise KeyError("That model is no longer open. Please open it again.")
        return m

    # routing
    def do_GET(self):
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/ping":
                self.state.last_ping = time.time()
                return self._json({"ok": True, "version": __version__})
            if path == "/api/config":
                return self._json({"version": __version__, "settings": self.state.settings})
            if path == "/api/materials":
                s = self.state.settings
                return self._json(material_list(self.state, q.get("printer", [s["printer"]])[0],
                                                q.get("profile", [s["profile"]])[0]))
            if path == "/api/calibration":
                return self._json(self._calibration_list())
            if path.startswith("/api/model/") and path.endswith("/preview.stl"):
                mid = path.split("/")[3]
                return self._preview(mid)
            if path.startswith("/api/files/"):
                _, _, _, rid, name = path.split("/", 4)
                return self._file(rid, unquote(name), download="download" in q)
            return self._error("Not found", 404)
        except KeyError as exc:
            return self._error(str(exc.args[0]) if exc.args else "Not found", 404)
        except Exception as exc:  # noqa: BLE001 - show the problem in the page
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):
        path = urlparse(self.path).path
        st = self.state
        st.busy += 1
        try:
            if path == "/api/open-dialog":
                p = native_dialog("file", st.settings.get("last_dir", ""))
                if p is None:
                    return self._json({"unavailable": True})
                if not p:
                    return self._json({"cancelled": True})
                st.save_settings({"last_dir": str(Path(p).parent)})
                return self._json(self._open_path(Path(p)))
            if path == "/api/folder-dialog":
                p = native_dialog("folder", st.settings.get("output_dir", ""))
                if p is None:
                    return self._json({"unavailable": True})
                return self._json({"path": p, "cancelled": not p})
            if path == "/api/upload":
                name = Path(unquote(self.headers.get("X-Filename") or "model.step")).name
                folder = st.tmp / uuid.uuid4().hex[:8]
                folder.mkdir()
                dest = folder / name
                dest.write_bytes(self._body())
                return self._json(self._open_path(dest, source="upload"))
            if path == "/api/orient":
                data = self._json_body()
                m = self._model(data["model_id"])
                m.loaded = load_model(m.loaded.path, data["up"])
                m.preview = None
                return self._json(model_info(m))
            if path == "/api/check":
                data = self._json_body()
                m = self._model(data["model_id"])
                job = job_from_request(data, m)
                res = run(job, st.db, st.store, write=False, loaded=m.loaded, accepted=accepted_rules(data))
                return self._json(result_json(res))
            if path == "/api/build":
                return self._build(self._json_body())
            if path == "/api/settings":
                return self._json(st.save_settings(self._json_body()))
            if path == "/api/coupon":
                return self._coupon(self._json_body())
            if path == "/api/calibrate":
                return self._calibrate(self._json_body())
            if path == "/api/calibration/clear":
                data = self._json_body()
                st.store.clear(data["material"], data["printer"], data["profile"])
                return self._json(self._calibration_list())
            return self._error("Not found", 404)
        except UnsupportedFormat as exc:
            return self._error(str(exc))
        except (AdjustError, ValueError) as exc:
            return self._error(str(exc))
        except KeyError as exc:
            return self._error(str(exc.args[0]) if exc.args else "Missing value")
        except Exception as exc:  # noqa: BLE001
            return self._error(f"Something went wrong: {type(exc).__name__}: {exc}", 500)
        finally:
            st.busy -= 1

    # handlers
    def _static(self, rel: str):
        target = STATIC
        for part in rel.split("/"):
            if part in ("", ".", ".."):
                return self._error("Not found", 404)
            target = target.joinpath(part)
        if not target.is_file():
            return self._error("Not found", 404)
        ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        if rel.endswith(".js"):
            ctype = "text/javascript"
        self._send(200, target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    def _open_path(self, p: Path, source: str = "disk") -> dict:
        if p.suffix.lower() == ".sldprt":
            raise UnsupportedFormat(SLDPRT_HELP)
        loaded = load_model(p, "+Z")
        m = OpenModel(uuid.uuid4().hex[:10], p.name, loaded, source)
        self.state.models[m.id] = m
        return model_info(m)

    def _preview(self, mid: str):
        m = self._model(mid)
        if m.preview is None:
            mesh = m.loaded.preview_mesh()
            m.preview = mesh.export(file_type="stl")
        self._send(200, m.preview, "model/stl")

    def _file(self, rid: str, name: str, download: bool):
        p = self.state.files.get(rid, {}).get(name)
        if not p or not p.exists():
            return self._error("File not found", 404)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        extra = {"Content-Disposition": f'attachment; filename="{name}"'} if download else None
        self._send(200, p.read_bytes(), ctype, extra)

    def _out_dir(self, m: OpenModel, data: dict) -> Path:
        mode = data.get("save_mode") or self.state.settings.get("save_mode")
        if mode == "next_to_original" and m.source == "disk":
            return m.loaded.path.parent
        return Path(data.get("output_dir") or self.state.settings.get("output_dir") or default_output_dir()).expanduser()

    def _build(self, data: dict):
        st = self.state
        m = self._model(data["model_id"])
        job = job_from_request(data, m)
        accepted = accepted_rules(data)
        out_dir = self._out_dir(m, data)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"Can't create the folder {out_dir}: {exc}")
        res = run(job, st.db, st.store, out_dir, write=True, loaded=m.loaded, accepted=accepted,
                  stem_name=m.name)
        rid = st.register_files(res.outputs)
        data_out = result_json(res)
        data_out["run_id"] = rid
        data_out["output_dir"] = str(out_dir)
        data_out["files"] = [{"kind": k, "name": p.name, "url": f"/api/files/{rid}/{p.name}"}
                             for k, p in res.outputs.items()]
        return self._json(data_out)

    def _coupon(self, data: dict):
        from ..coupon import make_coupon, write_coupon
        from .. import export3mf
        import trimesh

        st = self.state
        mat = st.db.get(data["material"])
        printer, profile = data.get("printer") or "Bambu Lab", data.get("profile") or "default"
        holes = [float(h) for h in data.get("holes") or [4, 6, 8]]
        k, h = 1.0, 0.0
        if data.get("compensated"):
            ev = effective_values(mat, st.store.get(mat.name, printer, profile))
            if ev.k is None:
                raise ValueError(f"{mat.name} has no shrinkage value yet. Print a normal coupon first.")
            k, h = ev.k, ev.h_mm or 0.0
        shape, meta = make_coupon(float(data.get("square") or 20), holes, float(data.get("thickness") or 4), k, h)
        meta.update(material=mat.name, printer=printer, profile=profile)
        name = f"coupon_{material_slug(mat)}" + ("_confirm" if data.get("compensated") else "")
        out_dir = Path(data.get("output_dir") or st.settings.get("output_dir") or default_output_dir()).expanduser()
        paths = write_coupon(out_dir, name, shape, meta)
        mesh = trimesh.load(str(paths["stl"]), force="mesh")
        paths["3mf"] = out_dir / f"{name}.3mf"
        export3mf.write_3mf(paths["3mf"], mesh.vertices, mesh.faces, name, PrintSettings(2, 0.42, 15), mat.name)
        rid = st.register_files(paths)
        return self._json({"meta": meta, "output_dir": str(out_dir),
                           "files": [{"kind": k2, "name": p.name, "url": f"/api/files/{rid}/{p.name}"}
                                     for k2, p in paths.items()]})

    def _calibrate(self, data: dict):
        st = self.state
        meta = data["coupon"]
        mat = st.db.get(meta["material"])
        holes_meas = [float(v) for v in data.get("holes") or [] if v not in (None, "")]
        out = record_coupon(
            st.store, mat.name, meta.get("printer", "Bambu Lab"), meta.get("profile", "default"),
            float(meta["square_model"]), [float(data["square_x"]), float(data["square_y"])],
            [h["model"] for h in meta["holes"]] if holes_meas else [], holes_meas,
            compensated=bool(meta.get("compensated")), square_designed=meta.get("square_designed"),
            holes_designed=[h["designed"] for h in meta["holes"]],
            dried=data.get("dried"), walls=data.get("walls") or None, infill=data.get("infill") or None,
        )
        rec = out.record
        return self._json({
            "s_pct": out.s_pct, "h_mm": out.h_mm, "confirmed": out.confirmed, "worst_error_mm": out.worst_error_mm,
            "stored": rec is not None,
            "record": None if rec is None else {
                "runs": len(rec.runs), "s_pct": rec.s_pct, "h_mm": rec.h_mm,
                "s_range": list(rec.s_range) if rec.s_range else None},
            "calibration": self._calibration_list(),
        })

    def _calibration_list(self) -> list[dict]:
        return [{"material": r.material, "printer": r.printer, "profile": r.profile, "runs": len(r.runs),
                 "s_pct": r.s_pct, "h_mm": r.h_mm, "s_range": list(r.s_range) if r.s_range else None,
                 "last": r.runs[-1].date if r.runs else None}
                for r in self.state.store.records.values()]


def _existing_instance() -> Optional[int]:
    """Port of an app already running for this user, if any."""
    try:
        info = json.loads((settings_path().parent / "app_running.json").read_text("utf-8"))
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/api/ping", timeout=1) as r:
            if r.status == 200:
                return int(info["port"])
    except Exception:  # noqa: BLE001
        return None
    return None


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, db: Optional[Path] = None,
          calibration: Optional[Path] = None, idle_exit: bool = True) -> None:
    existing = _existing_instance()
    if existing:
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{existing}/")
        print(f"matprint is already running at http://127.0.0.1:{existing}/")
        return
    state = AppState(db, calibration)
    Handler.state = state
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    marker = settings_path().parent / "app_running.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"port": port, "pid": os.getpid()}), "utf-8")
    url = f"http://127.0.0.1:{port}/"
    print(f"matprint is running at {url} (close the browser tab to quit)")

    def watchdog():
        while True:
            time.sleep(5)
            if idle_exit and state.busy == 0 and time.time() - state.last_ping > IDLE_EXIT_S:
                httpd.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        shutil.rmtree(state.tmp, ignore_errors=True)
        try:
            marker.unlink()
        except OSError:
            pass


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Open the matprint app in your browser.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="Don't open a browser window.")
    ap.add_argument("--stay", action="store_true", help="Keep running when the page is closed.")
    args = ap.parse_args()
    serve(args.port, not args.no_browser, idle_exit=not args.stay)


if __name__ == "__main__":
    main()
