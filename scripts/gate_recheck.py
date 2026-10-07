"""Gate re-check: the adjudicated worksheet's failing gate fields against a later parity run. No model, no AWS.

    .venv/bin/python scripts/gate_recheck.py --worksheet <adjudication.csv> --run <run dir> --out <dir> [--families F3,F5,F7]

Selects worksheet rows of the chosen families (default F1, F3, F5 and F7) on pump_config
or a trap field whose verdict is `old right` or `both wrong`, finds the same row key and
field in the later run's
report, and writes <out>/gate_recheck.csv: the worksheet columns plus `high_value`
and `status` (matches_old | matches_medium | other | absent | needs_review). The
status compares the whole `high_value` with the old and medium values.
The worksheet is only read.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from extraction.compare.gate import GATE_FAMILIES, read_worksheet, recheck, selected  # noqa: E402
from extraction.compare.parity import stg_blocks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--worksheet", required=True, type=Path)
    ap.add_argument("--run", "--high-run", dest="run", required=True, type=Path,
                    help="the later parity run directory, holding <stem>/report.md (--high-run is an alias)")
    ap.add_argument("--families", default=",".join(GATE_FAMILIES),
                    help=f"comma-separated families to re-check (default: {','.join(GATE_FAMILIES)})")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--encoding", default="mac_roman",
                    help="encoding of cells typed into the worksheet by a spreadsheet (default: mac_roman); "
                         "cells still in valid UTF-8 are read as UTF-8")
    args = ap.parse_args()

    out = args.out.resolve()
    if out == LAB or LAB in out.parents:
        sys.exit(f"refusing to write inside {LAB}: the output holds client data")
    header, rows = read_worksheet(args.worksheet, args.encoding)
    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    picked = selected(rows, families)
    print("selected per family:", {f: sum(r["family"].strip() == f for r in picked) for f in families})

    reports = {}
    for doc in sorted({r["doc"] for r in picked}):
        path = args.run / doc / "report.md"
        reports[doc] = stg_blocks(path.read_text(encoding="utf-8"))[0] if path.is_file() else None

    out.mkdir(parents=True, exist_ok=True)
    statuses = Counter()
    with open(out / "gate_recheck.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header + ["high_value", "status"], lineterminator="\n")
        w.writeheader()
        for r in picked:
            high_value, status = recheck(r, reports[r["doc"]])
            statuses[status] += 1
            w.writerow({**r, "high_value": high_value, "status": status})
    print(f"wrote {out / 'gate_recheck.csv'}: {len(picked)} rows {dict(sorted(statuses.items()))}")
    missing = [d for d, b in reports.items() if b is None]
    if missing:
        print(f"no report in the high run for: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
