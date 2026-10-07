"""Parity run: detect -> prep -> assemble -> extract for the parity documents.

Calls STS and Bedrock. Run it by hand. Outputs hold client data and go to
--out, which must be outside this folder:

    AWS_PROFILE=roam-ai .venv/bin/python scripts/parity_run.py --doc all --out <dir>

Per document, <out>/<stem>/ receives report.md, run.json and, when the stream
carried reasoning text, reasoning.txt. A document whose run.json records a
completed run is not rerun unless --force is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import boto3

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from extraction import ExtractionError, detect, prep  # noqa: E402
from extraction.assemble import asset_hashes, assemble_prompt, clarification_hashes  # noqa: E402
from extraction.bedrock import EFFORT, MODEL_ID, REGION, extract  # noqa: E402
from extraction.compare.parity import PARITY_DOCS, run_metrics  # noqa: E402

ACCOUNT = "640168431387"
_PDF = re.compile(r"(.+)\.[Pp][Dd][Ff]")


def corpus_pdfs(corpus: Path) -> dict[str, Path]:
    return {m[1]: p for p in sorted(corpus.iterdir()) if (m := _PDF.fullmatch(p.name))}


def select(doc: str, corpus: Path) -> list[Path]:
    """One stem; `all`; or family codes such as `F1,F3,F5`, which pick those families' parity documents."""
    pdfs = corpus_pdfs(corpus)
    families = [f.strip() for f in doc.split(",")]
    wanted = PARITY_DOCS if doc == "all" else [(f, sha) for f, sha in PARITY_DOCS if f in families]
    if not wanted:
        if doc not in pdfs:
            sys.exit(f"no PDF with stem {doc!r} in the corpus directory")
        return [pdfs[doc]]
    by_hash = {hashlib.sha256(p.read_bytes()).hexdigest(): p for p in pdfs.values()}
    missing = [fam for fam, sha in wanted if sha not in by_hash]
    if missing:
        sys.exit(f"parity documents not found in the corpus directory, by hash: {missing}")
    return [by_hash[sha] for _fam, sha in wanted]


def run_one(pdf_path: Path, out: Path, args, account: str) -> str:
    stem = _PDF.fullmatch(pdf_path.name)[1]
    d = out / stem
    run_json = d / "run.json"
    if run_json.is_file() and not args.force:
        if json.loads(run_json.read_text(encoding="utf-8")).get("status") == "completed":
            print(f"skip  {stem}: completed run.json exists (use --force to rerun)")
            return "skipped"

    data = pdf_path.read_bytes()
    run: dict = {"status": "failed", "source_file": pdf_path.name, "stem": stem,
                 "pdf_sha256": hashlib.sha256(data).hexdigest(), "account": account, "region": REGION,
                 "model_id": args.model_id, "effort": args.effort, "max_tokens": args.max_tokens,
                 "budget_s": args.budget_s, "clarifications": args.clarifications, "stage_durations_s": {}}
    stages = run["stage_durations_s"]

    def timed(stage, fn, *a, **kw):
        t = time.monotonic()
        try:
            return fn(*a, **kw)
        finally:
            stages[stage] = round(time.monotonic() - t, 3)

    d.mkdir(parents=True, exist_ok=True)
    try:
        det = timed("detect", detect, data)
        run["family"] = det.family
        res = timed("prep", prep, data, det, source_file=pdf_path.name)
        run["kept_pages"] = res.kept_pages
        run["kept_page_count"] = len(res.kept_pages)
        run["asset_sha256"] = asset_hashes(det.family)
        if args.clarifications:
            run["clarification_sha256"] = clarification_hashes(det.family)
        prompt = timed("assemble", assemble_prompt, det.family, res, pdf_path.name,
                       clarifications=args.clarifications)
        run["prompt_bytes"] = len(prompt.encode("utf-8"))
        print(f"run   {stem}: {det.family}, {run['prompt_bytes']} prompt bytes, {len(res.kept_pages)} pages")
        result = timed("extract", extract, prompt, budget_s=args.budget_s,
                       max_tokens=args.max_tokens, model_id=args.model_id, effort=args.effort)
        report = result.report.encode("utf-8")
        (d / "report.md").write_bytes(report)
        if result.reasoning_text:
            (d / "reasoning.txt").write_text(result.reasoning_text, encoding="utf-8")
        run.update({"status": "completed", "prompt_sha256": result.prompt_sha256,
                    "trailing_newlines_stripped": result.trailing_newlines_stripped,
                    "preamble_stripped_chars": result.preamble_stripped_chars,
                    "metrics": run_metrics(result, len(report))})
    except ExtractionError as exc:
        run["error"] = {"code": exc.code, "message": exc.message, "details": exc.details}
    run_json.write_text(json.dumps(run, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"{run['status']:<9} {stem}" + (f": {run['error']['code']}" if "error" in run else ""))
    return run["status"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--doc", required=True,
                    help="one document stem; 'all' for the six parity documents; or family codes, e.g. F1,F3,F5")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--model-id", default=MODEL_ID)
    ap.add_argument("--max-tokens", type=int, default=128_000)
    ap.add_argument("--budget-s", type=float, default=1800)
    ap.add_argument("--effort", choices=("medium", "high"), default=EFFORT,
                    help="thinking effort, sent as output_config.effort (default: medium)")
    ap.add_argument("--clarifications", action=argparse.BooleanOptionalAction, default=True,
                    help="each family's approved contract clarification in the prompt (default: on; "
                         "--no-clarifications sends the batch runners' prompt byte for byte)")
    ap.add_argument("--force", action="store_true", help="rerun documents that already completed")
    ap.add_argument("--corpus", type=Path, default=os.environ.get("ESP_CORPUS_DIR"),
                    help="directory of design-document PDFs (default: $ESP_CORPUS_DIR)")
    args = ap.parse_args()

    out = args.out.resolve()
    if out == LAB or LAB in out.parents:
        sys.exit(f"refusing to write inside {LAB}: run outputs hold client data")
    if not args.corpus or not Path(args.corpus).is_dir():
        sys.exit("set --corpus or ESP_CORPUS_DIR to the directory of design-document PDFs")
    docs = select(args.doc, Path(args.corpus))

    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    if account != ACCOUNT:
        sys.exit(f"credentials are for account {account}, not {ACCOUNT}; Bedrock not called")

    statuses = [run_one(p, out, args, account) for p in docs]
    return 0 if "failed" not in statuses else 1


if __name__ == "__main__":
    raise SystemExit(main())
