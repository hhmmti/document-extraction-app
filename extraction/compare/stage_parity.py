"""Stage parity: the ported stage against the batch loader's committed outputs.

Deterministic. No model, no AWS. Nothing here writes except `write_artifacts`.

The corpus is staged one report at a time, in file-name order, with the batch
loader's own well match (`matched_well`) and the family Detect assigns to each
report's source PDF. The per-report results are concatenated and compared
with the ten reference artifacts.

**Sort key, every artifact:** the full row as canonical JSON (keys sorted),
which is a total order, so "identical" means the same multiset of rows. For
the JSONL logs that is the line exactly as the batch loader wrote it. For the
three Parquet grains the port's frame must have the reference's columns, in
order, and is first put under the reference's Parquet schema with pyarrow's
safe cast, so a value that would need a lossy or impossible cast fails the
artifact instead of being coerced.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..detect import detect
from ..stage import stage
from ..stage import values as V
from ..stage.reports_reader import read_report

GRAINS = ("pump_config", "esp_well_design_context", "curve_observations")
LOGS = ("load_quarantine", "unit_conversions", "d16_log", "d16_tier_a_fallback",
        "d17_log", "gas_log", "reconciliation")
ARTIFACTS = GRAINS + LOGS


def read_reports(directory: Path) -> list[tuple[str, str]]:
    """(file name, text) for every report, in file-name order, read as the batch loader read them."""
    return [(p.name, p.read_text(encoding="utf-8", errors="replace"))
            for p in sorted(Path(directory).glob("*.md"))]


def matched_well(report_text: str) -> str | None:
    """The batch loader's well match: the frontmatter `well_id`, unless it is absent or
    `well_id_match_status` is `unmatched`. It carried no organisation."""
    fm = read_report(report_text).frontmatter
    well_id = fm.get("well_id")
    match_status = str(fm.get("well_id_match_status", "")).strip()
    if V.is_absent_well_id(well_id) or match_status == "unmatched":
        return None
    return str(well_id).strip()


def source_document(report_text: str) -> str:
    return read_report(report_text).source_document


def detected_family(report_text: str, corpus_dir: Path) -> str:
    """The family Detect assigns to the report's source PDF."""
    return detect((Path(corpus_dir) / source_document(report_text)).read_bytes()).family


def run(texts, families, wells, evidence, detections=None, preps=None) -> list:
    """Stage each report. `evidence` is one dict for all, or a function of the report text."""
    n = len(texts)
    return [stage(text, None, well, family, evidence(text) if callable(evidence) else evidence,
                  detection=detection, prep=prep)
            for text, family, well, detection, prep
            in zip(texts, families, wells, detections or [None] * n, preps or [None] * n)]


def combine(results) -> dict:
    """Concatenate per-report results into the ten artifacts, in report order."""
    out = {}
    for grain in GRAINS:
        frames = [getattr(r, grain) for r in results if len(getattr(r, grain).columns)]
        out[grain] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(dtype=object)
    out["load_quarantine"] = [row for r in results for row in r.quarantine]
    for name in LOGS[1:]:
        out[name] = [row for r in results for row in r.log[name]]
    return out


def jsonl_lines(rows) -> list[str]:
    """One line per row, exactly as the batch loader serialised its logs."""
    return [json.dumps(r, sort_keys=True, default=str) for r in rows]


def write_artifacts(artifacts: dict, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for grain in GRAINS:
        paths[grain] = out_dir / f"{grain}.parquet"
        pq.write_table(pa.Table.from_pandas(artifacts[grain], preserve_index=False), paths[grain])
    for name in LOGS:
        paths[name] = out_dir / f"{name}.jsonl"
        paths[name].write_text("".join(line + "\n" for line in jsonl_lines(artifacts[name])),
                               encoding="utf-8")
    return paths


@dataclass
class Verdict:
    artifact: str
    ref_rows: int
    port_rows: int
    identical: bool = False
    same_order: bool = False         # also identical before sorting
    only_ref: list = field(default_factory=list)
    only_port: list = field(default_factory=list)
    error: str | None = None


def _verdict(name: str, ref: list[str], port: list[str]) -> Verdict:
    only_ref = sorted((Counter(ref) - Counter(port)).elements())
    only_port = sorted((Counter(port) - Counter(ref)).elements())
    return Verdict(name, len(ref), len(port), identical=not only_ref and not only_port,
                   same_order=ref == port, only_ref=only_ref, only_port=only_port)


def compare(artifacts: dict, ref_dir: Path) -> list[Verdict]:
    """Every artifact against the reference output directory."""
    ref_dir = Path(ref_dir)
    verdicts = []
    for grain in GRAINS:
        ref = pq.read_table(ref_dir / f"{grain}.parquet")
        frame = artifacts[grain]
        if list(frame.columns) != ref.column_names:
            verdicts.append(Verdict(grain, ref.num_rows, len(frame), error=(
                f"columns differ: missing {sorted(set(ref.column_names) - set(frame.columns))}, "
                f"extra {sorted(set(frame.columns) - set(ref.column_names))}, "
                f"order equal on the shared set: "
                f"{[c for c in frame.columns if c in ref.column_names] == [c for c in ref.column_names if c in frame.columns]}")))
            continue
        try:
            port = pa.Table.from_pandas(frame, schema=ref.schema, preserve_index=False)
        except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError) as err:
            verdicts.append(Verdict(grain, ref.num_rows, len(frame), error=f"cast to reference schema: {err}"))
            continue
        verdicts.append(_verdict(grain, jsonl_lines(ref.to_pylist()), jsonl_lines(port.to_pylist())))
    for name in LOGS:
        ref = (ref_dir / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        verdicts.append(_verdict(name, ref, jsonl_lines(artifacts[name])))
    return verdicts


# The one named, expected difference from the batch loader's outputs: flag 2
# (`provenance_json` no longer takes part in D16).  Two wells halted on it alone.
FLAG2 = ("d16_log", "load_quarantine", "esp_well_design_context")
FLAG2_WELLS = 2


def flag2_check(verdict: Verdict) -> str | None:
    """None when `verdict`'s difference is exactly flag 2's, else what is not."""
    ref = [json.loads(r) for r in verdict.only_ref]
    port = [json.loads(r) for r in verdict.only_port]
    if verdict.artifact == "load_quarantine":
        ok = (not port and len(ref) == FLAG2_WELLS
              and all(r["column"] == "provenance_json" and r["reason"] == "d16_halt_invariant_field_varies" for r in ref))
        return None if ok else "expected only the provenance_json D16-HALT quarantine rows, one per flag-2 well"
    if verdict.artifact not in ("d16_log", "esp_well_design_context") or len(ref) != FLAG2_WELLS or len(port) != FLAG2_WELLS:
        return f"expected {FLAG2_WELLS} rows on each side"
    by_well = {r["well_id"]: r for r in port}
    for r in ref:
        p = by_well.get(r["well_id"])
        if p is None:
            return "a flag-2 well is missing on the port side"
        if verdict.artifact == "d16_log":
            ok = (r["halted_columns"] == ["provenance_json"] and p["halted_columns"] == []
                  and p["status"] != "D16-HALT"
                  and {k: v for k, v in r["findings"].items() if k != "provenance_json"} == p["findings"]
                  and {k: v for k, v in r.items() if k not in ("status", "halted_columns", "findings")}
                  == {k: v for k, v in p.items() if k not in ("status", "halted_columns", "findings")})
        else:
            ok = (r["d16_halted_columns"] == "provenance_json" and p["d16_halted_columns"] is None
                  and p["d16_status"] != "D16-HALT"
                  and {k: v for k, v in r.items() if k not in ("d16_status", "d16_halted_columns")}
                  == {k: v for k, v in p.items() if k not in ("d16_status", "d16_halted_columns")})
        if not ok:
            return "a row differs beyond the flag-2 columns"
    return None


def parity_outcome(verdict: Verdict) -> str:
    """`identical`, `flag 2 (expected)`, or a failure description."""
    if verdict.error:
        return f"ERROR: {verdict.error}"
    if verdict.identical:
        return "identical"
    if verdict.artifact in FLAG2:
        problem = flag2_check(verdict)
        return "flag 2 (expected)" if problem is None else f"NO: {problem}"
    return "NO"
