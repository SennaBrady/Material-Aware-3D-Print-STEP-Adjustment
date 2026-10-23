"""One-page HTML report, JSON report and the console summary."""

from __future__ import annotations

from datetime import datetime
from html import escape

from . import __version__
from .settings_advisor import R5_LABELS, R5_SLICER_SETTINGS

HOLD_LABEL = {"holds": "holds", "fails": "FAILS", "unknown": "unknown", "n/a": "n/a (Z)"}


def _source(src: str) -> str:
    return {"calibration": "your calibration", "table": "default table", "none": "not available"}[src]


def value_lines(res) -> list[str]:
    ev = res.ev
    lines = []
    if ev.s_pct is not None:
        lines.append(f"Shrinkage s = {ev.s_pct:.3f}% ({_source(ev.s_source)}), XY scale k = {ev.k:.5f}")
    else:
        lines.append("Shrinkage: not available, calibrate this material")
    if ev.s_min_pct is not None:
        lines.append(f"Hold-check range {ev.s_min_pct:.2f}–{ev.s_max_pct:.2f}% ({_source(ev.spread_source)})")
    lines.append(f"Hole offset h = {ev.h_mm:.3f} mm per side ({_source(ev.h_source)})" if ev.h_mm is not None
                 else "Hole offset: not available, holes get the XY scale only")
    if ev.c_mm:
        lines.append(f"Contour offset c = {ev.c_mm:.3f} mm per side")
    return lines


def console_summary(res) -> str:
    out = [f"{res.job.part.name if res.job.part else 'job'} in {res.material.name}  "
           f"(build-up axis {res.job.build_up_axis}, {res.kind.upper()} input)"]
    out += ["  " + line for line in value_lines(res)]
    out.append("")
    out.append(f"  {'Dimension':<22}{'Type':<9}{'Nominal':>9} {'Tol':>11} {'Print':>10}  {'Spread':>7}  Hold")
    for d in res.dims:
        spread = f"{d.spread_mm:.3f}" if d.spread_mm is not None else "-"
        out.append(f"  {d.dim.name[:21]:<22}{d.dim.type:<9}{d.dim.nominal:>9.3f} {d.dim.tol_text():>11} "
                   f"{d.print_value:>10.4f}  {spread:>7}  {HOLD_LABEL[d.hold]}")
        for f in d.flags:
            out.append(f"      ! {f}")
    if res.advisor:
        a = res.advisor
        for f in a.original_hard_failures:
            out.append(f"  ! {res.material.name} misses a hard requirement: {f}")
        out.append("")
        if a.alternatives:
            out.append("  Materials that hold every critical dimension:")
            for i, alt in enumerate(a.alternatives, 1):
                out.append(f"    {i}. {alt.material.name}: {alt.tradeoff} (worst spread {alt.worst})")
        else:
            out.append("  No material in the database holds every critical dimension and meets the hard requirements.")
        for alt in a.needs_calibration:
            out.append(f"    ? {alt.material.name} might: {alt.tradeoff}. Calibrate it to check.")
    if res.suggestions:
        out.append("")
        out.append("  Print settings:")
        for s in res.suggestions:
            tag = " (provisional)" if s.provisional else ""
            val = f" -> {s.value:g}" if s.value is not None else ""
            out.append(f"    {s.rule} {s.setting}{val}: {s.reason}{tag}")
    if res.warnings:
        out.append("")
        out += [f"  ! {w}" for w in res.warnings]
    if res.outputs:
        out.append("")
        out.append("  Wrote:")
        out += [f"    {p}" for p in res.outputs.values()]
        out.append(f"  Done in {res.elapsed_s:.1f} s")
    return "\n".join(out)


def to_json(res) -> dict:
    ev = res.ev
    return {
        "tool": f"matprint {__version__}",
        "created": datetime.now().isoformat(timespec="seconds"),
        "part": str(res.job.part) if res.job.part else None,
        "input": res.kind,
        "material": res.material.name,
        "build_up_axis": res.job.build_up_axis,
        "values": {
            "s_pct": ev.s_pct, "k": ev.k, "s_min_pct": ev.s_min_pct, "s_max_pct": ev.s_max_pct,
            "h_mm": ev.h_mm, "c_mm": ev.c_mm,
            "s_source": ev.s_source, "spread_source": ev.spread_source, "h_source": ev.h_source,
        },
        "dimensions": [
            {
                "name": d.dim.name, "type": d.dim.type, "nominal": d.dim.nominal,
                "lower": d.dim.lower, "upper": d.dim.upper, "print_value": d.print_value,
                "correction": d.correction, "hold": d.hold, "spread_mm": d.spread_mm,
                "feature": d.feature, "flags": d.flags,
            }
            for d in res.dims
        ],
        "alternatives": [
            {"material": a.material.name, "tradeoff": a.tradeoff, "worst": a.worst, "score": round(a.score, 3)}
            for a in (res.advisor.alternatives if res.advisor else [])
        ],
        "needs_calibration": [a.material.name for a in (res.advisor.needs_calibration if res.advisor else [])],
        "settings_suggestions": [
            {"rule": s.rule, "setting": s.setting, "value": s.value, "reason": s.reason, "provisional": s.provisional}
            for s in res.suggestions
        ],
        "slicer_settings": {
            **R5_SLICER_SETTINGS,
            "wall_loops": res.final_settings.wall_loops if res.final_settings else None,
            "sparse_infill_density_pct": res.final_settings.infill_pct if res.final_settings else None,
        },
        "warnings": res.warnings,
        "outputs": {k: str(v) for k, v in res.outputs.items()},
        "elapsed_s": round(res.elapsed_s, 2),
    }


CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#666;--line:#ddd;--ok:#1a7f37;--bad:#c62828;--warn:#9a6700;--head:#f5f5f7}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#eee;--muted:#aaa;--line:#333;--ok:#4ac26b;
--bad:#ff6b6b;--warn:#d4a72c;--head:#222225}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;
margin:24px auto;max-width:980px;padding:0 16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px}
.muted{color:var(--muted)}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{background:var(--head);font-weight:600}td.num{text-align:right;font-variant-numeric:tabular-nums}
.holds{color:var(--ok);font-weight:600}.fails{color:var(--bad);font-weight:700}
.unknown,.na{color:var(--warn)}.flag{color:var(--warn);font-size:12px}
ul{margin:4px 0;padding-left:20px}code{font-size:12px}
@media print{body{margin:0;font-size:11px}}
"""


def render_html(res) -> str:
    e = escape
    part = res.job.part.name if res.job.part else ""
    rows = []
    for d in res.dims:
        cls = {"holds": "holds", "fails": "fails", "unknown": "unknown", "n/a": "na"}[d.hold]
        flags = "".join(f'<div class="flag">⚠ {e(f)}</div>' for f in d.flags)
        spread = f"{d.spread_mm:.3f}" if d.spread_mm is not None else "–"
        rows.append(
            f"<tr><td>{e(d.dim.name)}{flags}</td><td>{e(d.dim.type)}</td><td>{e(d.feature or '–')}</td>"
            f'<td class="num">{d.dim.nominal:.3f}</td><td class="num">{e(d.dim.tol_text())}</td>'
            f'<td class="num">{d.print_value:.4f}</td><td><code>{e(d.correction)}</code></td>'
            f'<td class="num">{spread} / {d.dim.band:.3f}</td><td class="{cls}">{HOLD_LABEL[d.hold]}</td></tr>'
        )
    alt_html = ""
    if res.advisor:
        a = res.advisor
        items = "".join(
            f"<li><b>{e(x.material.name)}</b>: {e(x.tradeoff)} <span class='muted'>(worst spread {e(x.worst)})</span></li>"
            for x in a.alternatives
        ) or "<li>No material in the database holds every critical dimension and meets the hard requirements.</li>"
        cal = "".join(
            f"<li><b>{e(x.material.name)}</b> might hold: {e(x.tradeoff)}. It has no shrinkage data; calibrate it to check.</li>"
            for x in a.needs_calibration
        )
        hard = "".join(f"<li class='fails'>{e(res.material.name)} misses a hard requirement: {e(f)}</li>"
                       for f in a.original_hard_failures)
        alt_html = f"<h2>Material alternatives</h2><ul>{hard}{items}{cal}</ul>"
    sug = "".join(
        f"<li><b>{e(s.rule)}</b> {e(s.setting)}{' → ' + format(s.value, 'g') if s.value is not None else ''}: "
        f"{e(s.reason)}{' <span class=muted>(provisional)</span>' if s.provisional else ''}</li>"
        for s in res.suggestions
    ) or "<li>No wall or infill changes needed.</li>"
    fs = res.final_settings
    slicer = "".join(f"<tr><td>{e(R5_LABELS[k])}</td><td>{e(v)}</td></tr>" for k, v in R5_SLICER_SETTINGS.items())
    if fs:
        slicer += (f"<tr><td>Strength &gt; Wall loops</td><td>{fs.wall_loops}</td></tr>"
                   f"<tr><td>Strength &gt; Sparse infill density</td><td>{fs.infill_pct:g}%</td></tr>")
    warns = "".join(f"<li>{e(w)}</li>" for w in res.warnings)
    outs = "".join(f"<li><code>{e(p.name)}</code></li>" for p in res.outputs.values())
    values = "".join(f"<li>{e(v)}</li>" for v in value_lines(res))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(part)} print report</title><style>{CSS}</style></head><body>
<h1>{e(part)} → {e(res.material.name)}</h1>
<div class="muted">matprint {__version__} · {datetime.now():%Y-%m-%d %H:%M} · {res.kind.upper()} input ·
build-up axis {e(res.job.build_up_axis)} · {e(res.job.printer)} / {e(res.job.profile)}</div>
<h2>Values used</h2><ul>{values}</ul>
<h2>Critical dimensions</h2>
<table><tr><th>Dimension</th><th>Type</th><th>Feature</th><th>Nominal</th><th>Tol</th><th>Print model</th>
<th>Correction</th><th>Spread / band</th><th>Hold</th></tr>{''.join(rows)}</table>
{alt_html}
<h2>Wall and infill suggestions</h2><ul>{sug}</ul>
<h2>Slicer settings</h2>
<p class="muted">All compensation is in the print model. Every slicer-side compensation must stay off, or the part prints oversized.</p>
<table>{slicer}</table>
{f'<h2>Warnings</h2><ul>{warns}</ul>' if warns else ''}
{f'<h2>Files</h2><ul>{outs}</ul>' if outs else ''}
<p class="muted">The original model was not modified. Provisional rules (S2, S5, S6) come from general practice and
need test prints to confirm.</p>
</body></html>
"""
