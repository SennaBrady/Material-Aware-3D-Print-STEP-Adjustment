"""Command line interface: `matprint <command>`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import yaml

from . import __version__
from .calibration import CalibrationStore, effective_values, record_coupon
from .geometry import UnsupportedFormat, input_kind
from .job import load_job
from .materials import MaterialDB
from .pipeline import AdjustError, material_slug, run


def _db(ctx) -> MaterialDB:
    return MaterialDB.load(ctx.obj.get("db"))


def _store(ctx) -> CalibrationStore:
    return CalibrationStore(ctx.obj.get("calibration"))


def _floats(text: str) -> list[float]:
    return [float(v) for v in text.replace(";", ",").split(",") if v.strip()]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="matprint")
@click.option("--db", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Use a custom materials.yaml instead of the built-in database.")
@click.option("--calibration", type=click.Path(dir_okay=False, path_type=Path),
              help="Calibration file (default ~/.matprint/calibration.json, or $MATPRINT_HOME).")
@click.pass_context
def main(ctx, db, calibration):
    """Material-aware print compensation: write a print model that comes off the printer at size."""
    ctx.ensure_object(dict)
    ctx.obj.update(db=db, calibration=calibration)


@main.command()
@click.option("--port", default=8765, show_default=True)
@click.option("--no-browser", is_flag=True, help="Don't open a browser window.")
@click.option("--stay", is_flag=True, help="Keep running after the page is closed.")
@click.pass_context
def app(ctx, port, no_browser, stay):
    """Open the point-and-click app in your browser."""
    from .app.server import serve

    serve(port, not no_browser, ctx.obj.get("db"), ctx.obj.get("calibration"), idle_exit=not stay)


@main.command()
@click.argument("name", required=False)
@click.pass_context
def materials(ctx, name):
    """List materials, or show one material's record."""
    db = _db(ctx)
    if name:
        m = db.get(name)
        click.echo(yaml.safe_dump({k: v for k, v in vars(m).items() if k != "shrinkage"}, sort_keys=False))
        click.echo(f"shrinkage: {vars(m.shrinkage)}")
        return
    click.echo(f"{'Material':<16}{'s typ':>7}{'s range':>13}{'hole h':>9}{'max °C':>8}  base / filler")
    for m in db:
        sh = m.shrinkage
        s = f"{sh.typical:.1f}%" if sh.typical is not None else "-"
        rng = f"{sh.min:g}–{sh.max:g}%" if sh.known else "calibrate"
        h = f"{m.hole_offset_mm:.2f}" if m.hole_offset_mm is not None else "-"
        t = f"{m.max_temp_conservative:g}" if m.max_temp_conservative is not None else "-"
        click.echo(f"{m.name:<16}{s:>7}{rng:>13}{h:>9}{t:>8}  {m.base}" + (f" / {m.filler}" if m.filler else ""))


def _load_features(model: Path, up: str):
    kind = input_kind(model)
    if kind != "step":
        from .geometry import mesh_model
        mesh = mesh_model.orient(mesh_model.load_stl(model), up)
        x, y, z = mesh_model.size(mesh)
        from .geometry.step_model import Feature
        return kind, [Feature("BBOX_X", "extent", x), Feature("BBOX_Y", "extent", y),
                      Feature("Z_HEIGHT", "extent", z)]
    from .geometry import step_model
    return kind, step_model.analyse(step_model.orient(step_model.load_step(model), up)).features


UP = click.option("--up", "up", default="+Z", show_default=True,
                  type=click.Choice(["+X", "-X", "+Y", "-Y", "+Z", "-Z"], case_sensitive=False),
                  help="Model axis that points up on the build plate.")


@main.command()
@click.argument("model", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@UP
def features(model, up):
    """List the holes, pockets, bosses and extents found in MODEL (in print orientation)."""
    try:
        kind, feats = _load_features(model, up.upper())
    except UnsupportedFormat as exc:
        raise click.ClickException(str(exc))
    if kind == "stl":
        click.echo("STL mesh: only overall extents are available.")
    for f in feats:
        click.echo(f.describe())


@main.command()
@click.argument("model", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-m", "--material", required=True, help="Material name, e.g. PETG or 'Nylon PA6'.")
@UP
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path),
              help="Job file to write (default <model>_job.yaml next to the model).")
@click.pass_context
def init(ctx, model, material, up, output):
    """Write a starter job file for MODEL with every detected feature listed as a dimension."""
    db = _db(ctx)
    mat = db.get(material)
    try:
        kind, feats = _load_features(model, up.upper())
    except UnsupportedFormat as exc:
        raise click.ClickException(str(exc))
    dims = []
    for f in feats:
        if f.kind == "hole":
            dims.append({"name": f"Hole {f.id}", "type": "hole", "feature": f.id, "nominal": round(f.size, 3), "tol": 0.1})
        elif f.kind == "boss":
            dims.append({"name": f"Boss {f.id}", "type": "contour", "feature": f.id, "nominal": round(f.size, 3), "tol": 0.1})
        elif f.id in ("BBOX_X", "BBOX_Y"):
            dims.append({"name": f"Overall {f.id[-1]}", "type": "contour", "feature": f.id, "nominal": round(f.size, 3), "tol": 0.2})
        elif f.id == "Z_HEIGHT":
            dims.append({"name": "Height", "type": "z", "feature": f.id, "nominal": round(f.size, 3), "tol": 0.2})
    job = {
        "part": model.name,
        "material": mat.name,
        "build_up_axis": up.upper(),
        "printer": "Bambu Lab",
        "profile": "default",
        "print_settings": {"wall_loops": 2, "line_width_mm": 0.42, "infill_pct": 15, "purpose": "functional"},
        "hard_requirements": {"max_temp_c": None, "chemicals": {}},
        "dimensions": dims,
    }
    output = output or model.with_name(f"{model.stem}_job.yaml")
    header = ("# matprint job file. Edit the tolerances, delete dimensions that aren't critical,\n"
              "# and add any others (types: hole, contour, position, z). Tolerance: 0.05 for ±0.05,\n"
              "# or [-0.02, 0.08] for unequal limits. Chemical levels: resistant, short, not.\n")
    output.write_text(header + yaml.safe_dump(job, sort_keys=False, allow_unicode=True), "utf-8")
    click.echo(f"Wrote {output} with {len(dims)} dimensions. Edit the tolerances, then run: matprint adjust {output}")


def _run_loop(ctx, job_path: Path, material: str | None, out_dir: Path | None, write: bool, prompt: bool):
    from .report import console_summary

    db, store = _db(ctx), _store(ctx)
    try:
        job = load_job(job_path)
    except (ValueError, KeyError) as exc:
        raise click.ClickException(str(exc))
    while True:
        try:
            res = run(job, db, store, out_dir, material, write=False)
        except (AdjustError, UnsupportedFormat, KeyError, ValueError) as exc:
            raise click.ClickException(str(exc))
        alts = res.advisor.alternatives if res.advisor else []
        if res.failing and alts and prompt and sys.stdin.isatty():
            click.echo(console_summary(res))
            click.echo("")
            choice = click.prompt(
                f"Pick an alternative 1-{len(alts)} and re-check, or press Enter to keep {res.material.name}",
                default="", show_default=False)
            if choice.strip().isdigit() and 1 <= int(choice) <= len(alts):
                material = alts[int(choice) - 1].material.name
                click.echo(f"\nRe-checking with {material}...\n")
                continue
        break
    if write:
        try:
            res = run(job, db, store, out_dir, material, write=True)
        except (AdjustError, UnsupportedFormat) as exc:
            raise click.ClickException(str(exc))
    click.echo(console_summary(res))
    return res


@main.command()
@click.argument("job", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-m", "--material", help="Override the job file's material.")
@click.pass_context
def check(ctx, job, material):
    """Hold check and advice only; writes no files."""
    res = _run_loop(ctx, job, material, None, write=False, prompt=False)
    sys.exit(0 if res.all_hold else 2)


@main.command()
@click.argument("job", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-m", "--material", help="Override the job file's material.")
@click.option("-o", "--out-dir", type=click.Path(file_okay=False, path_type=Path),
              help="Where to write outputs (default: next to the model).")
@click.option("--no-prompt", is_flag=True, help="Don't ask to switch material when a dimension fails.")
@click.pass_context
def adjust(ctx, job, material, out_dir, no_prompt):
    """Write the print model (STEP/STL/3MF), slicer settings and report for JOB."""
    _run_loop(ctx, job, material, out_dir, write=True, prompt=not no_prompt)


@main.command()
@click.option("-m", "--material", required=True)
@click.option("--square", default=20.0, show_default=True, help="Outer square size, mm.")
@click.option("--holes", default="4,6,8", show_default=True, help="Hole diameters, mm, comma separated.")
@click.option("--thickness", default=4.0, show_default=True, help="Coupon thickness, mm.")
@click.option("--compensated", is_flag=True,
              help="Pre-compensate with the current values, for the confirmation print.")
@click.option("--printer", default="Bambu Lab", show_default=True)
@click.option("--profile", default="default", show_default=True)
@click.option("-o", "--out-dir", type=click.Path(file_okay=False, path_type=Path), default=Path("."))
@click.pass_context
def coupon(ctx, material, square, holes, thickness, compensated, printer, profile, out_dir):
    """Generate a calibration coupon (STEP, STL and a sizes file for `calibrate`)."""
    from .coupon import make_coupon, write_coupon

    db, store = _db(ctx), _store(ctx)
    mat = db.get(material)
    k, h = 1.0, 0.0
    if compensated:
        ev = effective_values(mat, store.get(mat.name, printer, profile))
        if ev.k is None:
            raise click.ClickException(f"{mat.name} has no shrinkage value yet; print an uncompensated coupon first")
        k, h = ev.k, ev.h_mm or 0.0
    try:
        shape, meta = make_coupon(square, _floats(holes), thickness, k, h)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    meta.update(material=mat.name, printer=printer, profile=profile)
    name = f"coupon_{material_slug(mat)}" + ("_compensated" if compensated else "")
    paths = write_coupon(out_dir, name, shape, meta)
    click.echo(f"Wrote {paths['step']}, {paths['stl']} and {paths['json']}")
    click.echo("Print it with every slicer compensation off (shrinkage 100%, X-Y hole/contour 0, auto circle off),")
    click.echo("let it cool fully, measure the square in X and Y and each hole, then run:")
    click.echo(f"  matprint calibrate --coupon {paths['json']} --square-x <mm> --square-y <mm> --holes <d1,d2,...>")


@main.command()
@click.option("--coupon", "coupon_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="The coupon's .json sizes file (sets material, printer, profile and model sizes).")
@click.option("-m", "--material", help="Needed when no --coupon file is given.")
@click.option("--printer", default=None)
@click.option("--profile", default=None)
@click.option("--square-model", type=float, help="Modelled square size when no --coupon file is given.")
@click.option("--hole-models", help="Modelled hole diameters when no --coupon file is given.")
@click.option("--square-x", type=float, required=True, help="Measured square size in X, mm.")
@click.option("--square-y", type=float, required=True, help="Measured square size in Y, mm.")
@click.option("--holes", "holes_measured", default="", help="Measured hole diameters, in coupon order.")
@click.option("--dried/--not-dried", default=None, help="Was the filament dried before printing?")
@click.option("--walls", type=int, help="Wall loops the coupon was printed with.")
@click.option("--infill", type=float, help="Infill % the coupon was printed with.")
@click.option("--tolerance", default=0.05, show_default=True, help="Pass band for a compensated coupon, ± mm.")
@click.pass_context
def calibrate(ctx, coupon_file, material, printer, profile, square_model, hole_models, square_x, square_y,
              holes_measured, dried, walls, infill, tolerance):
    """Enter coupon measurements; stores measured shrinkage and hole offset."""
    db, store = _db(ctx), _store(ctx)
    meta = json.loads(coupon_file.read_text("utf-8")) if coupon_file else {}
    mat = db.get(material or meta.get("material") or "")
    printer = printer or meta.get("printer", "Bambu Lab")
    profile = profile or meta.get("profile", "default")
    sq_model = square_model or meta.get("square_model")
    if not sq_model:
        raise click.ClickException("Give --coupon <file>.json, or --square-model")
    if coupon_file:
        model_holes = [h["model"] for h in meta.get("holes", [])]
        designed_holes = [h["designed"] for h in meta.get("holes", [])]
    else:
        model_holes = _floats(hole_models or "")
        designed_holes = model_holes
    measured = _floats(holes_measured)
    try:
        out = record_coupon(
            store, mat.name, printer, profile, sq_model, [square_x, square_y], model_holes, measured,
            compensated=bool(meta.get("compensated")), square_designed=meta.get("square_designed"),
            holes_designed=designed_holes, dried=dried, walls=walls, infill=infill, tolerance=tolerance)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    if out.confirmed is not None:
        click.echo(f"Confirmation coupon: worst error {out.worst_error_mm:.3f} mm against designed sizes "
                   f"({'within' if out.confirmed else 'outside'} ±{tolerance:g} mm).")
        if out.confirmed:
            click.echo("Values confirmed. Nothing changed.")
            return
        click.echo("Still off, so this coupon is added as a new calibration run.")
    rec = out.record
    click.echo(f"{mat.name} on {printer} / {profile}: this coupon s = {out.s_pct:.3f}%"
               + (f", h = {out.h_mm:+.3f} mm" if out.h_mm is not None else ""))
    click.echo(f"Stored {len(rec.runs)} run(s); using s = {rec.s_pct:.3f}%"
               + (f", h = {rec.h_mm:+.3f} mm" if rec.h_mm is not None else "")
               + (f", run-to-run range {rec.s_range[0]:.3f}–{rec.s_range[1]:.3f}%" if rec.s_range else
                  " (print a second coupon to measure run-to-run spread for the hold check)"))
    if dried is None:
        click.echo("Tip: pass --dried or --not-dried; damp filament prints holes tighter.")
    click.echo(f"Saved to {store.path}. Confirm with: matprint coupon -m \"{mat.name}\" --compensated")


@main.group()
def calibration():
    """Show or clear stored calibration values."""


@calibration.command("list")
@click.pass_context
def calibration_list(ctx):
    store = _store(ctx)
    if not store.records:
        click.echo(f"No calibration stored yet ({store.path}).")
        return
    for rec in store.records.values():
        rng = f", range {rec.s_range[0]:.3f}–{rec.s_range[1]:.3f}%" if rec.s_range else ""
        h = f", h {rec.h_mm:+.3f} mm" if rec.h_mm is not None else ""
        click.echo(f"{rec.material} | {rec.printer} | {rec.profile}: s {rec.s_pct:.3f}%{h}{rng} "
                   f"({len(rec.runs)} run(s), last {rec.runs[-1].date})")


@calibration.command("clear")
@click.option("-m", "--material", required=True)
@click.option("--printer", default="Bambu Lab", show_default=True)
@click.option("--profile", default="default", show_default=True)
@click.pass_context
def calibration_clear(ctx, material, printer, profile):
    store = _store(ctx)
    name = _db(ctx).get(material).name
    click.echo("Cleared." if store.clear(name, printer, profile) else "Nothing stored for that combination.")


if __name__ == "__main__":
    main()
