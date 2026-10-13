"""Stage parity: the ported stage against the batch loader's outputs, then the new reports. No model, no AWS.

    .venv/bin/python scripts/stage_parity.py --out <dir>

Inputs, as arguments or environment variables:

    --old-reports  ESP_OLD_REPORTS_DIR  the corpus extraction reports the batch loader read
    --load-ref     ESP_LOAD_REF_DIR     the batch loader's output directory (read only)
    --corpus       ESP_CORPUS_DIR       the source PDFs, for the family Detect assigns
    --runs         ESP_RUNS_DIR         run directories; every <run set>/<document>/report.md is staged

Writes, under --out (outside this folder: outputs hold client data):

    corpus/, production/        the ten artifacts from each alias-evidence mode on the corpus
    new/<run set>/<document>/   the same artifacts for each new report, production mode
    stage_parity.md             parity, production vs corpus, new reports, F1 normaliser check
    hashes.txt                  SHA-256 of every file above, for comparing two runs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from extraction import ExtractionError  # noqa: E402
from extraction.compare import stage_parity as SP  # noqa: E402
from extraction.stage import corpus_evidence, frozen_evidence, production_evidence, stage  # noqa: E402
from extraction.stage.aliases import AliasLookup  # noqa: E402
from extraction.stage.reports_reader import read_report  # noqa: E402
from extraction.stage.stage import EVIDENCE, SEED, asset  # noqa: E402

SORT_KEY = "full row, canonical JSON"


def md(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def parity_table(verdicts) -> list[str]:
    L = ["| Artifact | Reference rows | Port rows | Sort key | Outcome | Same order unsorted? |",
         "|---|---|---|---|---|---|"]
    for v in verdicts:
        outcome = SP.parity_outcome(v)
        L.append(f"| `{v.artifact}` | {v.ref_rows} | {v.port_rows} | {SORT_KEY} | "
                 f"{outcome if outcome in ('identical', 'flag 2 (expected)') else '**' + md(outcome) + '**'} | "
                 f"{'yes' if v.same_order else 'no'} |")
    diffs = [v for v in verdicts if v.error or not v.identical]
    L.append("")
    if not diffs:
        L.append("No differences.")
    for v in diffs:
        L.append(f"**`{v.artifact}`**" + (f": {md(v.error)}" if v.error else ""))
        L += [f"- only in reference: `{md(r)}`" for r in v.only_ref]
        L += [f"- only in port: `{md(r)}`" for r in v.only_port]
    return L


def file_hashes(root: Path) -> list[str]:
    return [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root)}"
            for p in sorted(root.rglob("*")) if p.is_file() and p.name != "hashes.txt"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    for flag, env in (("--old-reports", "ESP_OLD_REPORTS_DIR"), ("--load-ref", "ESP_LOAD_REF_DIR"),
                      ("--corpus", "ESP_CORPUS_DIR"), ("--runs", "ESP_RUNS_DIR")):
        ap.add_argument(flag, type=Path, default=os.environ.get(env), help=f"default: ${env}")
    args = ap.parse_args()

    out = args.out.resolve()
    if out == LAB or LAB in out.parents:
        sys.exit(f"refusing to write inside {LAB}: outputs hold client data")
    for name in ("old_reports", "load_ref", "corpus", "runs"):
        if not getattr(args, name) or not Path(getattr(args, name)).is_dir():
            sys.exit(f"--{name.replace('_', '-')} is not a directory; see the docstring")

    L = ["# Stage parity", ""]

    # ---- the corpus, one report at a time ----
    reports = SP.read_reports(args.old_reports)
    names = [n for n, _ in reports]
    texts = [t for _, t in reports]
    families = [SP.detected_family(t, args.corpus) for t in texts]
    wells = [SP.matched_well(t) for t in texts]
    by_name_family = [n for n, f in zip(names, families) if n.split("-", 1)[0].upper() != f]
    L += [f"Corpus: {len(texts)} reports, {sum(w is None for w in wells)} unmatched wells (staged with "
          f"`well_id = None`: quarantine and reconciliation only, no load rows). Detect's family differs from "
          f"the file-name family the batch loader used on {len(by_name_family)}: {by_name_family or 'none'}.", ""]

    corpus_ev = corpus_evidence(texts)
    frozen = frozen_evidence()
    evidence_sha = hashlib.sha256(asset(EVIDENCE).read_bytes()).hexdigest()
    L += [f"Alias evidence: {len(corpus_ev)} flags from the corpus; frozen asset `{EVIDENCE}` "
          f"SHA-256 `{evidence_sha}`, {len(frozen)} flags, equal to the corpus build: {frozen == corpus_ev}.", ""]

    modes = {"corpus": corpus_ev, "production": lambda text: production_evidence(text, frozen)}
    written = {}
    for mode, evidence in modes.items():
        artifacts = SP.combine(SP.run(texts, families, wells, evidence))
        written[mode] = SP.write_artifacts(artifacts, out / mode)
        verdicts = SP.compare(artifacts, args.load_ref)
        L += [f"## {mode.capitalize()} mode against the reference", ""] + parity_table(verdicts) + [""]
        print(f"{mode}: " + ", ".join(f"{v.artifact} {SP.parity_outcome(v)}" for v in verdicts))

    L += ["## Production vs corpus, byte for byte", "", "| Artifact | Identical bytes? |", "|---|---|"]
    for name in SP.ARTIFACTS:
        same = written["corpus"][name].read_bytes() == written["production"][name].read_bytes()
        L.append(f"| `{name}` | {'yes' if same else '**NO**'} |")
    L.append("")

    # ---- the new reports, production mode ----
    well_by_document = {SP.source_document(t): w for t, w in zip(texts, wells)}
    L += ["## New reports (production mode)", "",
          "Organisation: none (the batch loader's match carries none). Well: the batch loader's match for the "
          "same source document in the corpus.", "",
          "| Run set | Family | Document | Well | pump_config | design_context | curve_observations "
          "| Quarantine | D16 | D17 |", "|---|---|---|---|---|---|---|---|---|---|"]
    f1_checks = []
    for report_md in sorted(Path(args.runs).glob("*/*/report.md")):
        run_set, doc = report_md.parent.parent.name, report_md.parent.name
        family = json.loads((report_md.parent / "run.json").read_text(encoding="utf-8"))["family"]
        text = report_md.read_text(encoding="utf-8", errors="replace")
        document = SP.source_document(text)
        well = well_by_document.get(document)
        well_note = well or ("unmatched" if document in well_by_document else "no corpus report")
        evidence = production_evidence(text, frozen)
        try:
            result = stage(text, None, well, family, evidence)
        except ExtractionError as err:
            L.append(f"| {md(run_set)} | {family} | {md(doc)} | {md(well_note)} | — | — | — | — | "
                     f"**{err.code}** | {md(err.message)} |")
            continue
        artifacts = SP.combine([result])
        SP.write_artifacts(artifacts, out / "new" / run_set / doc)
        d16 = result.log["d16_log"][0] if result.log["d16_log"] else None
        d17 = result.log["d17_log"][0] if result.log["d17_log"] else None
        d16_text = "—" if d16 is None else d16["status"] + (
            f" ({', '.join(d16['halted_columns'])})" if d16["halted_columns"] else "")
        L.append(f"| {md(run_set)} | {family} | {md(doc)} | {md(well_note)} | {len(result.pump_config)} | "
                 f"{len(result.esp_well_design_context)} | {len(result.curve_observations)} | "
                 f"{len(result.quarantine)} | {md(d16_text)} | {'—' if d17 is None else d17['status']} |")
        if family == "F1":
            f1_checks.append((run_set, doc, text, evidence, result))
    L.append("")

    # ---- F1: every pump section through D31 ----
    L += ["## F1 normaliser check", "",
          "Every `stg_pump_config` row's `pump_model_as_printed`, resolved through the D31 lookup the stage "
          "uses (seed + production evidence), and every staged `pump_config` row with a null "
          "`pump_model_canonical`.", ""]
    for run_set, doc, text, evidence, result in f1_checks:
        report = read_report(text)
        lookup = AliasLookup(asset(SEED))
        lookup.evidence_flags = dict(evidence)
        sections = report.blocks["stg_pump_config"]
        printed = {}
        for obj in sections:
            printed.setdefault(obj.get("pump_model_as_printed"), []).append(obj)
        nulls = result.pump_config[result.pump_config["pump_model_canonical"].isna()]
        L += [f"**{md(run_set)} / {md(doc)}**: {len(sections)} sections, {len(printed)} distinct printed "
              f"strings, {len(result.pump_config)} staged model groups, {len(nulls)} with a null canonical.", "",
              "| Printed | Sections | Canonical | Flag | Basis | Any conflict row |", "|---|---|---|---|---|---|"]
        for text_printed, objs in printed.items():
            r = lookup.resolve(text_printed, report.vendor_family)
            conflict = any(o.get("extraction_status") == "conflict" for o in objs)
            L.append(f"| `{md(text_printed)}` | {len(objs)} | {md(r.canonical)} | {r.flag} | {md(r.basis)} | "
                     f"{'yes' if conflict else 'no'} |")
        for _, row in nulls.iterrows():
            L.append(f"- null canonical: `{md(row['pump_model_as_printed'])}`, sections "
                     f"{row['section_orders_grouped']}, flag {row['pump_model_alias_flag']}")
        L.append("")

    (out / "stage_parity.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / "hashes.txt").write_text("\n".join(file_hashes(out)) + "\n", encoding="utf-8")
    print(f"wrote {out / 'stage_parity.md'}")


if __name__ == "__main__":
    main()
