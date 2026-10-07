"""Conflict review: one row per pump_config row a run's report marks `extraction_status = conflict`. No model, no AWS.

    .venv/bin/python scripts/conflict_review.py --run-dir <parity run dir> --family F1 --out <csv>

For the family's one document in the run, each conflict row gets: its row key (as the
adjudication worksheet writes it), the model strings, the report's own stated reason
(the Extraction notes lines that give it, quoted with their line numbers), every string
for that model in the report's stg_alias_evidence with its evidence_kind and
source_page, and blank `verdict` and `note` columns for re-adjudication.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from extraction.compare.parity import Alignment, raw, row_key, stg_blocks  # noqa: E402

COLUMNS = ["row_key", "pump_model_as_printed", "pump_model_as_printed_alt", "stated_reason", "alias_evidence",
           "verdict", "note"]


def notes_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) for the report's `## Extraction notes` section."""
    out, inside = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            inside = line.strip() == "## Extraction notes"
            continue
        if inside and line.strip():
            out.append((n, line.strip()))
    return out


def _stems(row: dict) -> list[str]:
    """The model stems a row names: the first token of each printed model string."""
    return [raw(row[k]).split()[0] for k in ("pump_model_as_printed", "pump_model_as_printed_alt")
            if raw(row.get(k)) and raw(row[k]).split()]


def stated_reason(lines: list, row: dict) -> str:
    """Notes lines that state a conflict on pump_config or under a validator rule, and notes
    lines that name this row's model in a quoted string or a table row."""
    stems = _stems(row)
    keep = [(n, l) for n, l in lines
            if ("conflict" in l and ("pump_config" in l or "V-" in l))
            or (any(re.search(rf"\b{re.escape(s)}\b", l) for s in stems) and ("`" in l or l.startswith("|")))]
    return " / ".join(f"L{n}: {l}" for n, l in keep)


def alias_evidence(rows: list, row: dict) -> str:
    stems = _stems(row)
    found = []
    for a in rows:
        model = raw(a.get("model_as_printed") or a.get("pump_model_as_printed"))
        if model and any(re.search(rf"\b{re.escape(s)}\b", model) for s in stems):
            found.append(f"`{model}` ({raw(a.get('evidence_kind'))}, {raw(a.get('source_page'))})")
    return " | ".join(found)


def review(text: str) -> list[dict]:
    blocks = stg_blocks(text)[0]
    al, lines = Alignment.of(blocks), notes_lines(text)
    out = []
    for r in blocks.get("stg_pump_config", []):
        if raw(r.get("extraction_status")) != "conflict":
            continue
        key = row_key("stg_pump_config", al.scenario(r)[0], r)
        out.append({"row_key": json.dumps(list(key), ensure_ascii=False),
                    "pump_model_as_printed": raw(r.get("pump_model_as_printed")) or "",
                    "pump_model_as_printed_alt": raw(r.get("pump_model_as_printed_alt")) or "",
                    "stated_reason": stated_reason(lines, r),
                    "alias_evidence": alias_evidence(blocks.get("stg_alias_evidence", []), r),
                    "verdict": "", "note": ""})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--family", default="F1")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    out = args.out.resolve()
    if LAB in out.parents:
        sys.exit(f"refusing to write inside {LAB}: the output holds client data")
    docs = [p.parent for p in args.run_dir.glob("*/run.json")
            if json.loads(p.read_text(encoding="utf-8")).get("family") == args.family and (p.parent / "report.md").is_file()]
    if len(docs) != 1:
        sys.exit(f"expected one {args.family} report in {args.run_dir}, found {len(docs)}")
    rows = review((docs[0] / "report.md").read_text(encoding="utf-8"))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
