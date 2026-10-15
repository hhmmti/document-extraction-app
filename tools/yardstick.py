"""Yardstick: how far the generic route agrees with template extraction, document by document.

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tools/yardstick.py --template <store or run dir> --generic <store or run dir> [--fields] [--diff]

Each generic-route run is paired with the template-route run of the same PDF (by SHA-256; the newest of
each); two run directories are compared as given, whatever their routes.  The reports'
`stg_pump_config` and `stg_design_context` rows are paired as extraction/compare pairs them (scenario,
section order, sensitivity case) and every value is classified with its classifier.  Reads the stores
only: no model, no AWS.  Documents are named by family and PDF hash, never by file name.

Printed, per document and in total:
- row alignment: rows per table on each side, the template rows that found a partner, and the cases
  each side covers (scenario; `s` marks a sensitivity case; a scenario label is shown as `unaligned`);
- agreement on matched rows only: (same + format) / (same + format + conflict + dropped);
- every dropped or conflicting template value, by cause:
    grain       the rows do not line up: a template row with no partner in a case both sides have, or
                a pair in a case whose row counts differ (per-section rows against model groups)
    scenario    a template row whose case the other side does not have
    vocabulary  a field the generic contract decides differently (VOCABULARY), a value the other side
                printed under another key, or one it printed once and did not repeat in every case row
    genuine     printed and in scope, and left out (dropped) or different (wrong)
- the genuine misses by field.
`--fields` adds per-field counts; `--diff` lists every differing value, values included, for reading
locally.  Bookkeeping fields (source, page, status, provenance and the like) are never compared.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True

LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from extraction.compare.parity import Alignment, canon, classify, raw, row_key, stg_blocks  # noqa: E402
from extraction.validate.review import METADATA  # noqa: E402

TABLES = ("stg_pump_config", "stg_design_context")
SKIP = METADATA | {"vendor_family", "source_document"}
CAUSES = ("grain", "scenario", "vocabulary", "genuine")
COUNTED = ("same", "format", "conflict", "dropped", "added")
# Fields the generic contract (generic-contract.md) decides differently from the family contracts.
VOCABULARY = {
    "manufacturer",                         # §3: only a maker on the schema's list; families name their vendor
    "stages_basis",                         # §4: always group_total
    "design_frequency_hz", "design_frequency_min_hz", "design_frequency_max_hz",   # T4: on the design point
    "motor_power_factor_basis", "motor_type",                                      # family vocabulary it never sets
    "scenario_label_staged",                # §4: labels as printed; families shorten or rename them
    "scenario_title_as_printed", "scenario_title_frequency_disagrees",             # F1-only keys
}


@dataclass(frozen=True)
class Value:
    table: str
    case: tuple          # (scenario, sensitivity flag)
    field: str
    category: str        # same | format | conflict | dropped | added
    cause: str | None    # dropped and conflict only
    matched: bool        # on a matched pair of rows
    old: object = None
    new: object = None


def case_name(case: tuple) -> str:
    scenario, sensitivity = case
    return ("unaligned" if str(scenario).startswith("unaligned") else str(scenario)) + ("s" if sensitivity else "")


def keyed(blocks: dict, table: str) -> dict:
    """row key -> rows, as extraction/compare buckets them (canonical order within a key)."""
    al, out = Alignment.of(blocks), defaultdict(list)
    for r in blocks.get(table, []):
        out[row_key(table, al.scenario(r)[0], r)].append(r)
    return {k: sorted(v, key=canon) for k, v in out.items()}


def moved(value, field: str, other_row: dict) -> bool:
    """The other side printed this value under another key: a key choice, not a miss (two printings
    of one model swapped between `pump_model_as_printed` and its `_alt`, say).  A bare small integer
    is too common to tell."""
    text = raw(value) or ""
    if re.fullmatch(r"\s*\d{1,3}\s*", text):
        return False
    return any(g != field and g not in SKIP and (raw(v) == text or classify(value, v) == "format")
               for g, v in other_row.items())


def compare_rows(template_text: str, other_text: str):
    """(shape, values): per table, rows and cases on each side and template rows matched; every value."""
    sides = [stg_blocks(text)[0] for text in (template_text, other_text)]
    shape, values = {}, []
    for table in TABLES:
        old, new = keyed(sides[0], table), keyed(sides[1], table)
        per_case = [Counter(), Counter()]
        for i, side in enumerate((old, new)):
            for k, rows in side.items():
                per_case[i][(k[0], k[-1])] += len(rows)
        misaligned = {c for c in per_case[0] if c in per_case[1] and per_case[0][c] != per_case[1][c]}
        printed = {(f, raw(v)) for rows in new.values() for r in rows for f, v in r.items()}
        matched = 0
        for k in sorted(old.keys() | new.keys(), key=str):
            case, a_rows, b_rows = (k[0], k[-1]), old.get(k, []), new.get(k, [])
            for a, b in zip(a_rows, b_rows):
                matched += 1
                for f in sorted((a.keys() | b.keys()) - SKIP):
                    cat = "same" if raw(a.get(f)) == raw(b.get(f)) else classify(a.get(f), b.get(f))
                    cause = None
                    if cat in ("dropped", "conflict"):
                        cause = ("grain" if case in misaligned else "vocabulary"
                                 if f in VOCABULARY or moved(a[f], f, b)
                                 or (cat == "dropped" and (f, raw(a[f])) in printed) else "genuine")
                    values.append(Value(table, case, f, cat, cause, True, a.get(f), b.get(f)))
            for a in a_rows[len(b_rows):]:
                cause = "scenario" if case not in per_case[1] else "grain"
                values += [Value(table, case, f, "dropped", cause, False, a[f]) for f in sorted(set(a) - SKIP)]
            for b in b_rows[len(a_rows):]:
                values += [Value(table, case, f, "added", None, False, None, b[f]) for f in sorted(set(b) - SKIP)]
        shape[table] = {"rows": (sum(map(len, old.values())), sum(map(len, new.values()))), "matched": matched,
                        "cases": tuple(", ".join(sorted({case_name(c) for c in pc})) or "–" for pc in per_case)}
    return shape, values


def agreement(values) -> float | None:
    c = Counter(v.category for v in values)
    base = c["same"] + c["format"] + c["conflict"] + c["dropped"]
    return None if not base else (c["same"] + c["format"]) / base


def pct(a) -> str:
    return "–" if a is None else f"{a:.0%}"


def runs_in(path: Path) -> list[Path]:
    """A run directory, or every run directory of a store."""
    path = Path(path)
    if (path / "run.json").is_file():
        return [path]
    return sorted(d for d in (path / "runs").iterdir() if (d / "run.json").is_file()) if (path / "runs").is_dir() else []


def latest_by_pdf(paths, route: str | None) -> dict[str, tuple[Path, dict]]:
    """PDF SHA-256 -> (run dir, record) for the newest run of this route (any, if None) with a report."""
    out = {}
    for d in paths:
        record = json.loads((d / "run.json").read_text(encoding="utf-8"))
        taken = record.get("route") or ("template" if str(record.get("family", "")).startswith("F") else None)
        sha = ((record.get("source") or {}).get("pdf") or {}).get("sha256")
        if (route is None or taken == route) and sha and (d / "report.md").is_file():
            out[sha] = (d, record)            # run-id order, so the newest wins
    return out


def report(docs, fields: bool, diff: bool) -> None:
    print("## Row alignment\n")
    print("| Document | Table | Template rows | Other rows | Template rows matched | Template cases | Other cases |")
    print("|---|---|---|---|---|---|---|")
    for label, shape, _values in docs:
        for table, s in shape.items():
            print(f"| {label} | {table.removeprefix('stg_')} | {s['rows'][0]} | {s['rows'][1]} | {s['matched']} | "
                  f"{s['cases'][0]} | {s['cases'][1]} |")
    print("\n## Dropped or conflicting template values, by cause\n")
    print("| Document | Matched-row agreement | Grain | Scenario | Vocabulary | Genuine | Added |")
    print("|---|---|---|---|---|---|---|")
    every = [v for _label, _shape, values in docs for v in values]
    for label, _shape, values in docs + [("**all**", None, every)]:
        causes = Counter(v.cause for v in values if v.cause)
        added = sum(v.category == "added" for v in values)
        print(f"| {label} | {pct(agreement([v for v in values if v.matched]))} | "
              + " | ".join(str(causes[c]) for c in CAUSES) + f" | {added} |")
    print("\n## Genuine misses, by field\n")
    misses = defaultdict(lambda: [0, 0, set()])
    for label, _shape, values in docs:
        for v in values:
            if v.cause == "genuine":
                m = misses[f"{v.table.removeprefix('stg_')}.{v.field}"]
                m[0 if v.category == "dropped" else 1] += 1
                m[2].add(label.split()[0])
    print("| Field | Left out | Wrong | Documents |\n|---|---|---|---|")
    for name, (dropped, wrong, where) in sorted(misses.items(), key=lambda kv: (-kv[1][0] - kv[1][1], kv[0])):
        print(f"| `{name}` | {dropped} | {wrong} | {', '.join(sorted(where))} |")
    if not misses:
        print("| none | | | |")
    if fields:
        print("\n## Per field, every document\n")
        print("| Table | Field | " + " | ".join(c.capitalize() for c in COUNTED) + " | Agreement |")
        print("|---|---|" + "---|" * (len(COUNTED) + 1))
        per_field = defaultdict(list)
        for v in every:
            per_field[(v.table.removeprefix("stg_"), v.field)].append(v)
        for (table, field), vs in sorted(per_field.items()):
            c = Counter(v.category for v in vs)
            print(f"| {table} | `{field}` | " + " | ".join(str(c[k]) for k in COUNTED) + f" | {pct(agreement(vs))} |")
    if diff:
        print("\n## Every differing value\n")
        print("| Document | Table | Case | Field | Category | Cause | Template | Other |\n|---|---|---|---|---|---|---|---|")
        for label, _shape, values in docs:
            for v in values:
                if v.category != "same":
                    print(f"| {label} | {v.table.removeprefix('stg_')} | {case_name(v.case)} | `{v.field}` | {v.category} | "
                          f"{v.cause or ''} | {raw(v.old) or ''} | {raw(v.new) or ''} |".replace("\n", " "))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", type=Path, required=True, help="the template-route store, or one run directory")
    ap.add_argument("--generic", type=Path, required=True, help="the generic-route store, or one run directory")
    ap.add_argument("--fields", action="store_true", help="add per-field counts")
    ap.add_argument("--diff", action="store_true", help="list every differing value, values included")
    args = ap.parse_args(argv)
    direct = (args.template / "run.json").is_file() and (args.generic / "run.json").is_file()
    template = latest_by_pdf(runs_in(args.template), None if direct else "template")
    other = latest_by_pdf(runs_in(args.generic), None if direct else "generic")
    pairs = [(next(iter(template)), next(iter(other)))] if direct and template and other else \
        [(sha, sha) for sha in sorted(template) if sha in other]
    if not pairs:
        print("No PDF has both a template-route run and a generic-route run with a stored report.")
        return 1
    docs = []
    for t_sha, o_sha in pairs:
        (t_dir, t_rec), (o_dir, _o_rec) = template[t_sha], other[o_sha]
        shape, values = compare_rows((t_dir / "report.md").read_text(encoding="utf-8"),
                                     (o_dir / "report.md").read_text(encoding="utf-8"))
        docs.append((f"{t_rec.get('family')} {t_sha[:12]}", shape, values))
    print(f"Template side: {args.template}; other side: {args.generic}; {len(docs)} document(s).\n")
    report(docs, args.fields, args.diff)
    unpaired = sorted(set(template) ^ set(other)) if not direct else []
    if unpaired:
        print(f"\n{len(unpaired)} PDF(s) have a run on one route only: " + ", ".join(s[:12] for s in unpaired))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
