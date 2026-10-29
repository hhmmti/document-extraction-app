"""Confirm a run's proposed well entry: write the confirmation request, then wait for its result.

    python -m extraction.confirm --run-id <id> [--as-proposed] [--set KEY=VALUE ...] [--accept-anyway REASON]
                                 [--leave-to-check KEY ...] [--environment prd|dev] [--confirmed-by NAME]
                                 [--wait SECONDS]

It reads the run's run.json and well_entry.json, builds the request from the entry and the options, writes it
create-only to confirmations/<confirmation_id>/request.json, and prints its result (extraction/confirmation.py has
the layout and the shapes).

  S3 mode (ESP_APP_MODE=s3, as the app): s3://roam-ds-assets/esp-design-extraction/.  The writer Lambda answers, and
      the result is polled until --wait runs out.  --confirmed-by defaults to the STS caller's session name, the part
      of its ARN after the last slash.
  Local mode (the default): the store ESP_RUN_STORE_DIR names, outside this folder, in the same layout, the run's PDF
      at intake/<run_id>.pdf.  The writer runs in this process, its deadline --wait seconds away, with the catalogue
      from ESP_EXTRACTION_ASSETS_DIR.  Nothing calls AWS, so --confirmed-by is required.

Each value is sent as proposed, at its proposal's status, unless an option changes it:
  --as-proposed           every value to check is settled as proposed
  --set KEY=VALUE         KEY, a top-level key of the entry, becomes VALUE, manually corrected, with source
                          design_document (vapor_pressure_psia: ops_entered).  A number for a numeric key, a JSON list
                          of sections for pump_sections; nothing after `=` empties it.  Repeatable.
  --accept-anyway REASON  the reason, on each value that blocks publish and is not corrected (it is then settled),
                          and on a pump string whose order check fails
  --leave-to-check KEY    leaves KEY to check, so that the writer must refuse.  Repeatable.
A value still to check, or blocking publish with no reason, is sent `to check`, and the writer refuses it.

Prints the confirmation id and its status, then each reason.  Exit status: 0 written; 1 refused, failed, stale, or no
result within --wait; 2 nothing sent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

from . import confirm_writer
from . import confirmation as C
from .intake import RUN_ID
from .well_entries_schema import VALUE_TYPE
from .well_entry import order_check, section_value

MODE_ENV, STORE_ENV = "ESP_APP_MODE", "ESP_RUN_STORE_DIR"        # the app's (app/store.py)
LAB = Path(__file__).resolve().parents[1]
POLL_S = 2


def run_id_arg(value: str) -> str:
    if not RUN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError(f"not a run id (YYYYMMDDTHHMMSSZ-<8 hex>): {value!r}")
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m extraction.confirm", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True, type=run_id_arg)
    ap.add_argument("--as-proposed", action="store_true", help="settle every value to check as proposed")
    ap.add_argument("--set", action="append", default=[], dest="sets", metavar="KEY=VALUE",
                    help="correct a top-level key (repeatable)")
    ap.add_argument("--accept-anyway", metavar="REASON", help="accept what blocks publish, uncorrected, for this reason")
    ap.add_argument("--leave-to-check", action="append", default=[], metavar="KEY", help="leave a key to check (tests)")
    ap.add_argument("--environment", choices=C.ENVIRONMENTS, default="prd")
    ap.add_argument("--confirmed-by", help="default (S3 mode): the STS caller's session name")
    ap.add_argument("--wait", type=float, default=180, help="seconds to wait for the result (default 180)")
    args = ap.parse_args(argv)

    if os.environ.get(MODE_ENV) == "s3":
        import boto3
        objects = C.S3Objects(boto3.client("s3"))
    else:
        root = os.environ.get(STORE_ENV)
        if not root:
            print(f"set {STORE_ENV} to the run store directory, or {MODE_ENV}=s3", file=sys.stderr)
            return 2
        root = Path(root).resolve()
        if root == LAB or LAB in root.parents:
            print(f"refusing a run store inside {LAB}: runs hold client data", file=sys.stderr)
            return 2
        if not args.confirmed_by:
            print("local mode calls no AWS: give --confirmed-by", file=sys.stderr)
            return 2
        objects = C.LocalObjects(root)
    try:
        run = C.load(objects.get(C.run_key(args.run_id, "run.json")))
        entry = C.load(objects.get(C.run_key(args.run_id, C.ENTRY)))
    except FileNotFoundError as err:
        print(f"{args.run_id}: {err} is not there; nothing sent", file=sys.stderr)
        return 2
    document = ((run.get("source") or {}).get("pdf") or {}).get("sha256")
    keys = [v["key"] for v in entry["review"]["values"]]
    unknown = [k for k in args.leave_to_check if k not in keys]
    try:
        if not document:
            raise ValueError(f"{args.run_id}: the run records no source PDF to confirm against")
        if unknown:
            raise ValueError(f"--leave-to-check: not keys of the entry: {', '.join(unknown)}")
        finals, sections = corrections(args.sets, entry)
    except ValueError as err:
        print(f"{err}; nothing sent", file=sys.stderr)
        return 2

    at = confirm_writer.now()
    cid = C.new_confirmation_id(at)
    request = build_request(entry, run, cid=cid, run_id=args.run_id, document=document, confirmed_at=C.timestamp(at),
                            confirmed_by=args.confirmed_by or caller(), environment=args.environment,
                            as_proposed=args.as_proposed, finals=finals, sections=sections,
                            accept_anyway=args.accept_anyway, leave=set(args.leave_to_check))
    try:
        objects.put(C.request_key(cid), C.dump(request), create=True)
    except C.Exists:
        print(f"{cid}: a request with this id is already there; nothing sent", file=sys.stderr)
        return 2
    if isinstance(objects, C.LocalObjects):
        confirm_writer.confirm(objects, C.request_key(cid), deadline=at + timedelta(seconds=args.wait))
        result = C.read_result(objects, cid)
    else:
        result = poll(objects, cid, args.wait)
    print("\n".join(summary(cid, result)))
    return 0 if result and result["status"] == C.WRITTEN else 1


def caller() -> str:
    """The STS caller's session name: the part of its ARN after the last slash."""
    import boto3

    from .bedrock import REGION
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Arn"].rsplit("/", 1)[-1]


def corrections(sets, entry) -> tuple[dict, list | None]:
    """({key: its final envelope, or None to empty it}, pump_sections' final sections or None) from --set."""
    stored = {v["key"]: v for v in entry["review"]["values"]}
    finals, sections = {}, None
    for item in sets:
        key, sep, raw = item.partition("=")
        if not sep or key not in stored:
            raise ValueError(f"--set {item!r}: not KEY=VALUE for a top-level key of the entry")
        if key == C.DESIGN_DOCUMENT_REF:
            raise ValueError(f"--set {key}: the writer sets it")
        if raw == "":
            finals[key] = None
            continue
        if key == C.PUMP_SECTIONS:
            given = json.loads(raw)
            if not (isinstance(given, list) and given
                    and all(isinstance(s, dict) and set(s) <= set(C.SECTION_FIELDS) for s in given)):
                raise ValueError(f"--set {key}: a JSON list of sections, with fields from {', '.join(C.SECTION_FIELDS)}")
            sections = [{name: s.get(name) for name in C.SECTION_FIELDS} for s in given]
            value = [section_value(s) for s in sections]
        elif VALUE_TYPE[key] == "double":
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"--set {key}: {raw!r} is not a number") from None
            if not math.isfinite(value):
                raise ValueError(f"--set {key}: {raw!r} is not a finite number")
        else:
            value = raw
        finals[key] = {"value": value, "unit": stored[key]["unit"],
                       "source": "ops_entered" if key == "vapor_pressure_psia" else "design_document"}
    return finals, sections


def build_request(entry, run, *, cid, run_id, document, confirmed_at, confirmed_by, environment, as_proposed, finals,
                  sections, accept_anyway, leave) -> dict:
    """The request: every reviewed value of the entry, as proposed unless an option changed it, each with its
    history (the proposal, then this confirmation)."""
    proposed_at = run.get("ended_at") or run.get("started_at") or "unknown"
    proposed_sections = [{name: s.get(name) for name in C.SECTION_FIELDS} for s in entry["review"]["pump_string"]["sections"]]
    final_sections = proposed_sections if sections is None else sections

    def reviewed(v) -> dict:
        key, proposal = v["key"], entry[v["map"]].get(v["key"])
        if key in finals:
            final, status, why = finals[key], C.CORRECTED, "set by hand"
        elif key in leave:
            final, status, why = proposal, C.TO_CHECK, "left to check"
        elif v["status"] == C.BLOCKS:
            final, status, why = ((proposal, C.SETTLED, f"accepted anyway: {accept_anyway}") if accept_anyway
                                  else (proposal, C.TO_CHECK, "blocks publish, and no accept-anyway reason was given"))
        elif v["status"] == C.TO_CHECK and not as_proposed:
            final, status, why = proposal, C.TO_CHECK, "not confirmed"
        else:
            final, status, why = proposal, C.SETTLED, "as proposed"
        needs_reason = ((v["status"] == C.BLOCKS and status != C.CORRECTED)
                        or (key == C.PUMP_SECTIONS and order_check(final_sections)["result"] == "fail"))
        history = [C.step(v["status"], "extraction", proposed_at, "; ".join(v["reasons"]) or None),
                   C.step(status, confirmed_by, confirmed_at, why)]
        return {"final": final, "status": status,
                **({"accept_anyway": accept_anyway} if accept_anyway and needs_reason else {}), "history": history}

    values = entry["review"]["values"]
    string = next(v for v in values if v["key"] == C.PUMP_SECTIONS)
    return {"schema_version": C.SCHEMA_VERSION, "confirmation_id": cid, "run_id": run_id, "document_sha256": document,
            "organization_id": run["organization_id"], "well_id": run["well_id"], "environment": environment,
            "entry_method": "extracted", "confirmed_by": confirmed_by, "confirmed_at": confirmed_at,
            "values": [{"key": v["key"], **reviewed(v)} for v in values if v["key"] != C.PUMP_SECTIONS],
            "pump_string": {**reviewed(string), "sections": final_sections}}


def poll(objects, cid: str, wait: float) -> dict | None:
    """The result, read until it is no longer running or `wait` seconds have passed."""
    end = time.monotonic() + wait
    while True:
        result = C.read_result(objects, cid)
        if (result and result["status"] != C.RUNNING) or time.monotonic() >= end:
            return result
        time.sleep(POLL_S)


def summary(cid: str, result: dict | None) -> list[str]:
    if result is None:
        return [f"{cid} no result yet; the request is {C.request_key(cid)}"]
    head = f"{cid} {result['status']}"
    if result["status"] == C.WRITTEN:
        head += f" -> {result['row_key']} (version {result['version_key']})"
    elif result["status"] == C.STALE:
        head += ": the writer stopped without a result; its run-log row and log say why"
    elif result["status"] == C.RUNNING:
        head += f": still running after --wait; read {C.result_key(cid)} later"
    return [head, *(f"  - {reason}" for reason in result["reasons"])]


if __name__ == "__main__":
    raise SystemExit(main())
