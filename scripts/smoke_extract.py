"""Smoke run: one PDF through detect -> prep -> assemble -> extract, against Bedrock.

Calls STS and Bedrock. Run it by hand, once, with an output directory outside
this folder:

    AWS_PROFILE=roam-ai .venv/bin/python scripts/smoke_extract.py <pdf> <out-dir>

Writes <out-dir>/report.md (on success) and <out-dir>/run.json (always, once
the preflight passes). Contract clarifications are on; --no-clarifications turns
them off.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import boto3

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from extraction import ExtractionError, detect, prep  # noqa: E402
from extraction.assemble import asset_hashes, assemble_prompt, clarification_hashes  # noqa: E402
from extraction.bedrock import MAX_TOKENS, MODEL_ID, REGION, extract  # noqa: E402

ACCOUNT = "640168431387"
BUDGET_S = 780  # 13 minutes: the round-1 Lambda's 15 minutes less a reserve


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--budget-s", type=float, default=BUDGET_S)
    ap.add_argument("--model-id", default=MODEL_ID)
    ap.add_argument("--clarifications", action=argparse.BooleanOptionalAction, default=True,
                    help="each family's approved contract clarification in the prompt (default: on)")
    args = ap.parse_args()

    out = args.out_dir.resolve()
    if out == LAB or LAB in out.parents:
        print(f"refusing to write inside {LAB}; pick an output directory outside it", file=sys.stderr)
        return 2

    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    if account != ACCOUNT:
        print(f"credentials are for account {account}, not {ACCOUNT}; Bedrock not called", file=sys.stderr)
        return 2

    run: dict = {"source_file": args.pdf.name, "account": account, "region": REGION,
                 "model_id": args.model_id, "max_tokens": MAX_TOKENS, "budget_s": args.budget_s,
                 "stage_durations_s": {}}
    stages = run["stage_durations_s"]
    code = 0

    def timed(stage, fn, *a, **kw):
        t = time.monotonic()
        try:
            return fn(*a, **kw)
        finally:
            stages[stage] = round(time.monotonic() - t, 3)

    try:
        pdf = args.pdf.read_bytes()
        det = timed("detect", detect, pdf)
        run["family"] = det.family
        res = timed("prep", prep, pdf, det, source_file=args.pdf.name)
        run["kept_pages"] = res.kept_pages
        run["asset_sha256"] = asset_hashes(det.family)
        run["clarifications"] = args.clarifications
        if args.clarifications:
            run["clarification_sha256"] = clarification_hashes(det.family)
        prompt = timed("assemble", assemble_prompt, det.family, res, args.pdf.name,
                       clarifications=args.clarifications)
        run["prompt_bytes"] = len(prompt.encode("utf-8"))
        result = timed("extract", extract, prompt, budget_s=args.budget_s,
                       max_tokens=MAX_TOKENS, model_id=args.model_id)
        run.update({k: v for k, v in asdict(result).items() if k not in ("report", "reasoning_text")})
        run["report_bytes"] = len(result.report.encode("utf-8"))
        run["reasoning_chars"] = len(result.reasoning_text)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(result.report, encoding="utf-8")
        if result.reasoning_text:
            (out / "reasoning.txt").write_text(result.reasoning_text, encoding="utf-8")
    except ExtractionError as exc:
        run["error"] = {"code": exc.code, "message": exc.message, "details": exc.details}
        code = 1

    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps(run, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: run.get(k) for k in ("family", "prompt_bytes", "stop_reason", "input_tokens",
                                              "output_tokens", "duration_s", "error")}, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
