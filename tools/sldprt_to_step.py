"""Export SolidWorks parts (.sldprt) to STEP so matprint can read them.

Windows only, needs SolidWorks installed and `pip install pywin32`.
Parts are opened read-only and silently, so the .sldprt files and their drawings are never changed.

    python tools/sldprt_to_step.py C:\\parts\\bracket.sldprt          # one part
    python tools/sldprt_to_step.py C:\\parts                          # every .sldprt in a folder
    python tools/sldprt_to_step.py C:\\parts -o C:\\parts\\step        # write the STEP files elsewhere

The STEP flavour (AP203/AP214/AP242) follows SolidWorks' own export options
(Tools > Options > Export > STEP). AP214 or AP242 are both fine.

NOTE: this helper has not been run against a SolidWorks install yet; please report problems.
"""

import argparse
import sys
from pathlib import Path

SW_DOC_PART = 1
SW_OPEN_SILENT = 1
SW_OPEN_READ_ONLY = 2
SW_SAVE_AS_CURRENT_VERSION = 0
SW_SAVE_AS_SILENT = 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path, help="A .sldprt file or a folder of them")
    ap.add_argument("-o", "--out-dir", type=Path, help="Output folder (default: next to each part)")
    args = ap.parse_args()

    try:
        import pythoncom
        import win32com.client
    except ImportError:
        print("This helper needs Windows, SolidWorks and pywin32 (pip install pywin32).", file=sys.stderr)
        return 1

    parts = sorted(args.source.glob("*.sldprt")) + sorted(args.source.glob("*.SLDPRT")) \
        if args.source.is_dir() else [args.source]
    if not parts:
        print(f"No .sldprt files found in {args.source}", file=sys.stderr)
        return 1

    sw = win32com.client.Dispatch("SldWorks.Application")
    failures = 0
    for part in parts:
        out_dir = args.out_dir or part.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{part.stem}.step"
        err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        model = sw.OpenDoc6(str(part.resolve()), SW_DOC_PART, SW_OPEN_SILENT | SW_OPEN_READ_ONLY, "", err, warn)
        if model is None:
            print(f"FAILED to open {part} (error {err.value})")
            failures += 1
            continue
        no_export_data = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        ok = model.Extension.SaveAs(str(out.resolve()), SW_SAVE_AS_CURRENT_VERSION, SW_SAVE_AS_SILENT,
                                    no_export_data, err, warn)
        sw.CloseDoc(model.GetTitle())
        print(f"{'wrote' if ok else 'FAILED'} {out}")
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
