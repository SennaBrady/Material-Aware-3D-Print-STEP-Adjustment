# Material-Aware 3D Print STEP Adjustment (`matprint`)

This program takes a STEP model with dimensions and tolerances and writes a separate print model that comes off the printer at those dimensions in the chosen material, because shrinkage differs between materials. The output is a separate STEP, STL and 3MF. Writing a separate model means SolidWorks or Solid Edge never change the part drawing when print adjustments are made.

For each run it:

- scales X and Y for the material's shrinkage, and leaves Z at nominal;
- enlarges holes and pockets by the material's hole offset, and moves outer contours by its contour offset;
- checks whether each critical dimension can be held in that material, and when one can't, ranks up to three alternative materials that can, with similar temperature, chemical, strength, UV and conductivity properties;
- suggests wall and infill changes when they protect a critical dimension or keep the part inside the material's recommended range;
- writes the slicer settings to use (every slicer-side compensation turned off), embedded in the 3MF and as a plain list;
- writes a one-page HTML report plus a JSON copy.

The original model is only read, never written. Bambu Studio is supported first, and the rest of the design doesn't depend on any one slicer.

## Install

You need Python 3.10 to 3.13. On Linux:

```bash
cd Material-Aware-3D-Print-STEP-Adjustment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt -e .
matprint --help
```

On Windows, activate the environment with `.venv\Scripts\activate` instead. CadQuery brings its own OpenCascade CAD kernel, so the first install downloads about 300 MB.

### VS Code

Open the folder, accept the recommended Python extension, and pick `.venv` as the interpreter. Run and Debug has three ready-made launch configurations, and the Testing panel runs the pytest suite.

### PyCharm

Open the folder, set the project interpreter to `.venv`, and mark `src` as a Sources Root. To run the tool, create a Python run configuration with **Module name** `matprint` and parameters such as `adjust samples/locating_fixture_job.yaml -o output`. Right-click `tests` and choose **Run pytest** to run the tests.

## Quick start

```bash
# 1. See what the tool finds in your model (in print orientation)
matprint features samples/locating_fixture.step

# 2. Write a starter job file listing every detected feature, then edit the tolerances
matprint init my_part.step -m PETG --up +Z

# 3. Check, then write the print model
matprint check my_part_job.yaml
matprint adjust my_part_job.yaml -o output
```

Try it on the samples:

```bash
matprint adjust samples/locating_fixture_job.yaml -o output       # PETG: fails dowel spacing, suggests ASA / PC
matprint check  samples/locating_fixture_pa6_job.yaml             # PA6: 50 mm ±0.1 can't be held
matprint adjust samples/locating_fixture_stl_job.yaml -o output   # STL: XY scale only, holes flagged
```

When a dimension fails and you run `adjust` in a terminal, the tool offers the alternatives, re-checks with the one you pick, and repeats until you keep a material.

## The job file

```yaml
part: locating_fixture.step   # relative to this file. STEP (.step/.stp) or STL
material: PETG                # see `matprint materials`
build_up_axis: +Z             # model axis that points up on the plate: +X -X +Y -Y +Z -Z
printer: Bambu Lab            # calibration is stored per material + printer + profile
profile: default

print_settings:               # what you'd print with today
  wall_loops: 2
  line_width_mm: 0.42
  infill_pct: 15
  purpose: functional         # functional | decorative (changes PLA's infill range)

hard_requirements:            # alternatives must meet these; unknown data counts as a miss
  max_temp_c: 55
  chemicals: {water: resistant}   # acids/bases/water/solvents: resistant | short | not

dimensions:
  - name: Dowel hole
    type: hole                # hole | contour | position | z
    feature: H1               # optional; from `matprint features`
    nominal: 8.0
    tol: [0.0, 0.1]           # 8.00 +0.10/-0.00; or 0.05 for ±0.05
```

There are four dimension types:

| Type | Use it for | Print model value |
|---|---|---|
| `hole` | A bore or pocket, meaning an internal closed loop in XY | k·D + 2h |
| `contour` | An outer edge or a boss | k·L + 2c |
| `position` | A centre-to-centre distance, such as dowel spacing (an addition to the spec) | k·L |
| `z` | Anything along the build axis | nominal, reported but never compensated |

## How the compensation works

| Rule | What it does |
|---|---|
| R1 | Scale X and Y by k = 1 / (1 − s), where s is the calibrated shrinkage if one exists, otherwise the table value |
| R2 | Each hole and pocket wall moves out by h per side, the material's hole offset |
| R3 | Outer walls and bosses move by c per side. c is 0 in the table and can be negative |
| R4 | Z stays at nominal |
| R5 | In the slicer: filament shrinkage 100%, X-Y hole compensation 0, X-Y contour compensation 0, auto circle holes-contour compensation off. Compensating in both places leaves the part oversized |
| R6 | STL meshes get R1 only, and the report flags every hole as uncorrected |

On STEP input the offsets are applied to the exact B-rep first, by h/k and c/k, and the XY scale comes after, so the result is exactly k·D + 2h. Holes are the faces on inner loops of the cross-section, so rectangular pockets get the offset as well as round holes. That matches Bambu's X-Y hole compensation.

The hold check calculates spread = L × (s_max − s_min) and compares it with the tolerance band T. A dimension holds when spread ≤ T. Once a material has two or more calibration coupons, their measured run-to-run range replaces the table's.

## Calibration

```bash
matprint coupon -m PLA                       # writes coupon_PLA.step/.stl/.json (20 mm square, Ø4/6/8 holes)
# print it with slicer compensation off (R5), let it cool, then measure with calipers
matprint calibrate --coupon coupon_PLA.json --square-x 19.94 --square-y 19.95 --holes 3.82,5.80,7.81 --dried --walls 2 --infill 15
matprint coupon -m PLA --compensated         # confirmation print using the new values
matprint calibrate --coupon coupon_PLA_compensated.json --square-x 20.01 --square-y 19.99 --holes 4.02,5.98,8.03
matprint calibration list
```

The tool calculates s = 1 − measured / modelled from the square. The hole offset h is the hole error left after shrinkage, averaged over the holes: h = mean((1 − s)·D_model − D_measured) / 2. Values are stored in `~/.matprint/calibration.json`, keyed by material, printer and profile. Set `MATPRINT_HOME` to keep the file somewhere else, such as a shared folder.

If a confirmation coupon comes out within ±0.05 mm, the values are confirmed. If not, the coupon is added as a new run. Recording `--dried` matters because damp filament prints holes tighter. Recording `--walls` and `--infill` lets rule S6 warn you when a job's walls or infill go above what the coupon was printed at.

## Wall and infill rules

| Rule | Trigger | Suggestion |
|---|---|---|
| S1 | Walls × line width are under the material's minimum wall | Raise the wall count to meet it |
| S2 *(provisional)* | A critical hole or contour has a tolerance band of 0.2 mm or less | 4 walls, so the feature sits in solid perimeter |
| S3 | Infill is outside the material's range | Move it into the range |
| S4 | 100% infill while relying on slicer circle compensation | Never fires, because R5 always turns slicer compensation off |
| S5 *(provisional)* | Part is ≥ 100 mm long and ≥ 10 mm thick in a material with ≥ 1% shrinkage | Use the low end of the infill range |
| S6 *(provisional)* | Walls or infill are above what your calibration coupon was printed at | Print a calibration coupon at these settings |

S2, S5 and S6 come from general practice, not the sources. They stay marked provisional until test prints confirm them, and their thresholds sit at the top of `src/matprint/settings_advisor.py`.

## Materials

`matprint materials` lists the database and `matprint materials PETG` shows one record. The data lives in `src/matprint/data/materials.yaml`, which you can edit directly or copy and pass with `--db my_materials.yaml`. It was seeded from the project's property tables: GrandpaCAD shrinkage, Bambu hole-offset starting values, the CREATE whitepaper, Xometry, SourceCAD and Stopforth 2021. Where sources disagree, both figures are kept and the advisor uses the more conservative one. A blank value means the sources didn't give one, and the tool never fills it with a guess. So:

- ASA, HIPS, PC, PP and TPU have no hole offset yet. Holes in those materials get the XY scale only until you calibrate.
- PETG-CF, PAHT CF15, PP GF30, TPU 98A and conductive PLA have no shrinkage data. They can't be adjusted until calibrated. The advisor still points out a same-base fibre grade such as PAHT CF15 for PA6 as worth calibrating, and ranks it first once it is.
- Conductive PLA has a minimum feature size of 1.0 mm diameter and 40 mm length. Smaller critical features fail the check.

## Outputs

`adjust` writes these files to `-o` or next to the model:

| File | What's in it |
|---|---|
| `<part>_PRINT_<material>.step` | Compensated B-rep for CAD (STEP input only) |
| `<part>_PRINT_<material>.stl` | Compensated mesh |
| `<part>_PRINT_<material>.3mf` | Mesh centred on a 256 mm bed, with per-object wall/infill/compensation settings for Bambu Studio |
| `<part>_PRINT_<material>_slicer_settings.txt` | The same settings as a plain list, for any slicer |
| `<part>_PRINT_<material>_report.html` / `.json` | Per-dimension table, alternatives, suggestions, where the values came from |

The print model is written in print orientation, with the build-up axis as +Z.

## Known limits

- **SolidWorks .sldprt.** The format is closed and can't be read without SolidWorks. Export to STEP first with File > Save As > STEP AP214/AP242. On Windows with SolidWorks installed, `tools/sldprt_to_step.py` batch-exports a folder of parts, opening them read-only. That helper hasn't been run against SolidWorks yet. A SolidWorks add-in is planned for a later version.
- **Bambu settings inside the 3MF are best-effort.** The per-object keys `wall_loops`, `sparse_infill_density`, `xy_hole_compensation` and `xy_contour_compensation` are written in Bambu Studio's `model_settings.config` format, but haven't been verified in Bambu Studio yet. Filament-level settings (shrinkage, auto circle compensation) can't be set per object. Check them against the settings list or the report the first time.
- **Hole offset sign.** Positive enlarges, following Bambu Studio's convention. Confirm it on your first test print.
- Only vertical walls get offsets. Sloped or drafted faces get the XY scale only, and the report says so.
- No Z compensation, warp simulation or non-FDM processes. All of these are out of scope for v1.

## Development

```bash
pytest -q                       # unit tests plus end-to-end runs on the samples
python samples/make_samples.py  # rebuild the sample models
```

```
src/matprint/
  cli.py               commands
  pipeline.py          load → classify → look up values → check → compensate → advise → write
  geometry/            step_model.py (B-rep features, offsets, XY scale), mesh_model.py (STL)
  compensation.py      R1–R4, R6 and the hold check
  advisor.py           material alternatives
  settings_advisor.py  S1–S6 and the R5 slicer settings
  calibration.py       coupon maths and the calibration store
  coupon.py            coupon geometry
  export3mf.py         3MF writer
  report.py            HTML / JSON / console report
  data/materials.yaml  material database
```
