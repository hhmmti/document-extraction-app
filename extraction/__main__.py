"""Run one design document end to end into the local run store.

    python -m extraction --pdf <path> --org <id> (--well <id> | --no-well)
    python -m extraction --report <path> [--pdf <path>] --org <id> (--well <id> | --no-well)

With --pdf alone: Detect -> Prep -> Extract (one Bedrock call, in the datascience
account; the credentials are checked first) -> Validate and stage -> Store -> Record.
With --report: no model is called; add --pdf for Detect, Prep and grounding, or
those checks are not evaluated and the notices say so.  --no-well stages an
unmatched well (no load rows; no key is minted).

The store root is $ESP_RUN_STORE_DIR, outside this folder (runs hold client data);
assets come from $ESP_EXTRACTION_ASSETS_DIR.  Prints one summary line with the run_id.
Exit status: 0 the run succeeded, 1 it failed (its run.json says why), 2 it did not start.

--run-id <id> gives the run its id (the tool's own format), refused if runs/<id>/ exists.
--source-name <name> is the file name prep records and the prompt names, in place of the
PDF path's basename (with --pdf only).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .bedrock import EFFORT, MODEL_ID, REGION
from .errors import ExtractionError
from .run import BUDGET_S, MAX_TOKENS, RUN_ID, STORE_ENV, run

ACCOUNT = "640168431387"
LAB = Path(__file__).resolve().parents[1]


def run_id_arg(value: str) -> str:
    if not RUN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError(f"not a run id (YYYYMMDDTHHMMSSZ-<8 hex>): {value!r}")
    return value


def source_name_arg(value: str) -> str:
    if not value or Path(value).name != value or not value.isprintable():
        raise argparse.ArgumentTypeError(f"not a plain file name: {value!r}")
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m extraction", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", type=Path)
    ap.add_argument("--report", type=Path, help="reuse this extraction report; no model call")
    ap.add_argument("--org", required=True, help="organization_id")
    well = ap.add_mutually_exclusive_group(required=True)
    well.add_argument("--well", help="well_id")
    well.add_argument("--no-well", action="store_true", help="an unmatched well")
    ap.add_argument("--budget-s", type=float, default=BUDGET_S, help=f"run budget (default {BUDGET_S})")
    ap.add_argument("--model-id", default=MODEL_ID)
    ap.add_argument("--effort", choices=("medium", "high"), default=EFFORT)
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--clarifications", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--run-id", type=run_id_arg, help="use this run id; refused if runs/<id>/ exists")
    ap.add_argument("--source-name", type=source_name_arg,
                    help="the source file name to record and send, in place of the PDF path's basename")
    args = ap.parse_args(argv)

    if not args.pdf and not args.report:
        ap.error("give --pdf, --report, or both")
    if args.source_name and not args.pdf:
        ap.error("--source-name names the PDF: give --pdf")
    root = os.environ.get(STORE_ENV)
    if not root:
        print(f"set {STORE_ENV} to the run store directory", file=sys.stderr)
        return 2
    root = Path(root).resolve()
    if root == LAB or LAB in root.parents:
        print(f"refusing a run store inside {LAB}: runs hold client data", file=sys.stderr)
        return 2
    if args.run_id and (root / "runs" / args.run_id).exists():
        print(f"refusing run id {args.run_id}: runs/{args.run_id}/ already exists", file=sys.stderr)
        return 2
    if not args.report:
        import boto3
        account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
        if account != ACCOUNT:
            print(f"credentials are for account {account}, not {ACCOUNT}; nothing run", file=sys.stderr)
            return 2

    try:
        record = run(organization_id=args.org, well_id=None if args.no_well else args.well, pdf=args.pdf,
                     report=args.report, store_root=root, budget_s=args.budget_s, model_id=args.model_id,
                     effort=args.effort, max_tokens=args.max_tokens, clarifications=args.clarifications,
                     run_id=args.run_id, source_name=args.source_name)
    except ExtractionError as err:          # the run directory itself could not be created
        print(f"- failed {err.code}: {err.message}", file=sys.stderr)
        return 2
    print(summary(record))
    return 0 if record["status"] == "succeeded" else 1


def summary(record: dict) -> str:
    head = f"{record['run_id']} {record['status']} {record['family'] or '-'}"
    if record["error"]:
        head += f" {record['error']['code']} at {record['error']['stage']}"
    if record["rows"] is not None:
        rows = "/".join(str(n) for n in record["rows"].values())
        head += (f" rows {rows} findings {record['findings']['total']} spec {record['spec_validation_status']}"
                 f" review {record['review']['needs_review']}/{record['review']['items']}")
    if record["cost_usd"] is not None:
        head += f" cost ${record['cost_usd']:.3f}"
    return f"{head} -> {record['store_dir']}"


if __name__ == "__main__":
    raise SystemExit(main())
