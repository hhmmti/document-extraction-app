"""Validate run: stage and validate the corpus and the new reports, with Detect and Prep. No model, no AWS.

    .venv/bin/python scripts/validate_run.py --out <dir>

Inputs, as arguments or environment variables (as scripts/stage_parity.py), plus
ESP_EXTRACTION_ASSETS_DIR for the stage assets:

    --old-reports  ESP_OLD_REPORTS_DIR  the corpus extraction reports
    --load-ref     ESP_LOAD_REF_DIR     the batch loader's outputs, including load_summary.md
    --corpus       ESP_CORPUS_DIR       the source PDFs (Detect and Prep run on each)
    --runs         ESP_RUNS_DIR         run directories; every <run set>/<document>/report.md

Writes, under --out (outside this folder: outputs hold client data):

    corpus/artifacts/             the ten stage artifacts, corpus evidence
    corpus/<report>/              findings, review, unknown_models, notices (.jsonl) and status.json
    new/<run set>/<document>/     the same per new report, production evidence
    validate_run.md               parity, findings by rule, unknown vs blocked models, new reports
    hashes.txt                    SHA-256 of every file above, for comparing two runs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from extraction import ExtractionError, detect, prep  # noqa: E402
from extraction.compare import stage_parity as SP  # noqa: E402
from extraction.stage import corpus_evidence, frozen_evidence, production_evidence, stage  # noqa: E402

PER_REPORT = ("findings", "review", "unknown_models", "notices")


def md(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def dump(rows) -> str:
    return "".join(json.dumps(r, sort_keys=True, default=str, ensure_ascii=False) + "\n" for r in rows)


def write_report(result, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in PER_REPORT:
        (out / f"{name}.jsonl").write_text(dump(getattr(result, name)), encoding="utf-8")
    (out / "status.json").write_text(json.dumps({
        "spec_validation_status": result.spec_validation_status,
        "findings_by_rule": dict(sorted(Counter(f["rule_id"] for f in result.findings).items())),
        "review_records": len(result.review),
        "needs_review": sum(r["needs_review"] for r in result.review),
    }, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def blocked_models(load_summary: Path) -> dict[str, set]:
    """Blocking entries of load_summary.md's D30 table -> the wells they block."""
    out, in_table = defaultdict(set), False
    for line in load_summary.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_table = line.startswith("## Blocked wells")
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if in_table and len(cells) == 5 and cells[0] not in ("Well", "---") and not cells[0].startswith("-"):
            for entry in cells[4].split(", "):
                out[entry.replace("[unresolved] ", "")].add(cells[0])
    return out


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

    pdf = {}

    def detect_prep(text):
        name = SP.source_document(text)
        if name not in pdf:
            data = (Path(args.corpus) / name).read_bytes()
            detection = detect(data)
            pdf[name] = (detection, prep(data, detection, source_file=name))
        return pdf[name]

    L = ["# Validate run", ""]

    # ---- the corpus ----
    reports = SP.read_reports(args.old_reports)
    names, texts = [n for n, _ in reports], [t for _, t in reports]
    dp = [detect_prep(t) for t in texts]
    detections, preps = [d for d, _ in dp], [p for _, p in dp]
    families, wells = [d.family for d in detections], [SP.matched_well(t) for t in texts]
    results = SP.run(texts, families, wells, corpus_evidence(texts), detections, preps)
    artifacts = SP.combine(results)
    SP.write_artifacts(artifacts, out / "corpus" / "artifacts")
    for name, result in zip(names, results):
        write_report(result, out / "corpus" / Path(name).stem)

    L += ["## Corpus parity (corpus evidence)", "", "| Artifact | Reference rows | Port rows | Outcome |", "|---|---|---|---|"]
    for v in SP.compare(artifacts, args.load_ref):
        L.append(f"| `{v.artifact}` | {v.ref_rows} | {v.port_rows} | {SP.parity_outcome(v)} |")

    L += ["", "## Corpus findings by rule", "",
          "| Rule | Spec severity | Round 1 | Findings | Reports |", "|---|---|---|---|---|"]
    by_rule = defaultdict(list)
    for name, result in zip(names, results):
        for f in result.findings:
            by_rule[f["rule_id"]].append((name, f))
    for rule_id in sorted(by_rule, key=lambda r: (r[0] != "G", r)):
        rows = by_rule[rule_id]
        L.append(f"| {rule_id} | {rows[0][1]['spec_severity'] or '—'} | {rows[0][1]['round1_severity']} | "
                 f"{len(rows)} | {len({n for n, _ in rows})} |")
    L += ["", "Spec validation status: " + ", ".join(
        f"{k} {v}" for k, v in sorted(Counter(r.spec_validation_status for r in results).items())), ""]

    # ---- unknown models against the summer's blocked models ----
    blocked = blocked_models(Path(args.load_ref) / "load_summary.md")
    hits = defaultdict(set)
    for well, result in zip(wells, results):
        for u in result.unknown_models:
            hits[(u["model"], u["class"])].add(well)
    L += ["## Corpus unknown models against the summer's blocked models", "",
          "| Model or printed string | 3b class | Wells hit | Summer blocked wells |", "|---|---|---|---|"]
    for model in sorted({m for m, _ in hits} | set(blocked)):
        classes = sorted(c for m, c in hits if m == model)
        wells_hit = set().union(*(hits[(model, c)] for c in classes)) if classes else set()
        L.append(f"| `{md(model)}` | {', '.join(classes) or '— (not a hit)'} | {len(wells_hit)} | "
                 f"{len(blocked.get(model, ()))} |")
    hit_wells = set().union(*hits.values()) if hits else set()
    blocked_wells = set().union(*blocked.values()) if blocked else set()
    L += ["", f"Wells with at least one hit: {len(hit_wells)}; summer blocked wells: {len(blocked_wells)}; "
              f"blocked but not hit: {len(blocked_wells - hit_wells)}; hit but not blocked: {len(hit_wells - blocked_wells)}.", ""]

    # ---- the new reports ----
    well_by_document = {SP.source_document(t): w for t, w in zip(texts, wells)}
    frozen = frozen_evidence()
    L += ["## New reports (production evidence)", "",
          "| Run set | Family | Document | Status | Findings by rule | Notices | Unknown models | Review: records / needs review |",
          "|---|---|---|---|---|---|---|---|"]
    for report_md in sorted(Path(args.runs).glob("*/*/report.md")):
        run_set, doc = report_md.parent.parent.name, report_md.parent.name
        text = report_md.read_text(encoding="utf-8", errors="replace")
        family = json.loads((report_md.parent / "run.json").read_text(encoding="utf-8"))["family"]
        detection, prepared = detect_prep(text)
        try:
            result = stage(text, None, well_by_document.get(SP.source_document(text)), family,
                           production_evidence(text, frozen), detection=detection, prep=prepared)
        except ExtractionError as err:
            L.append(f"| {md(run_set)} | {family} | {md(doc)} | **{err.code}** | {md(err.message)} | | | |")
            continue
        write_report(result, out / "new" / run_set / doc)
        counts = Counter(f["rule_id"] for f in result.findings)
        L.append(f"| {md(run_set)} | {family} | {md(doc)} | {result.spec_validation_status} | "
                 f"{', '.join(f'{r} {n}' for r, n in sorted(counts.items()))} | "
                 f"{'; '.join(n['rule_id'] for n in result.notices) or '—'} | "
                 f"{'; '.join(u['model'] + ' (' + u['class'] + ')' for u in result.unknown_models) or '—'} | "
                 f"{len(result.review)} / {sum(r['needs_review'] for r in result.review)} |")
    L.append("")

    (out / "validate_run.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    hashes = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out)}"
              for p in sorted(out.rglob("*")) if p.is_file() and p.name != "hashes.txt"]
    (out / "hashes.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(f"wrote {out / 'validate_run.md'}")


if __name__ == "__main__":
    main()
