"""The Lambda's entry point, `lambda_function.lambda_handler`: an intake PDF in, a run folder out.  The same image's
second entry point, `confirm_handler`, is the confirmation writer, and its third, `product_upload_handler`, the starter
that turns a design PDF pushed by the ESP product into an intake upload (both at the end of this file).

S3 calls it for each `.pdf` created under ESP_INTAKE_PREFIX in ESP_BUCKET (the stack sets these and
the other ESP_* variables).  For each record of the event (S3 sends one):

1. The key, URL-decoded, must be `<intake prefix><run_id>.pdf`, the run id in `extraction.run`'s
   format.  Any other key gets one `FAILED` row in the run log, and nothing else.
   `extraction.intake`, shared with the app's S3 mode, reads the key and the labels.
2. `<runs prefix><run_id>/run.json` is created as `running`, create-only, with `deadline_at` the
   invocation's start plus the time it had left then.  If it exists (412), or another create is
   racing it (409), the event is a duplicate: logged, and nothing is written.
3. The labels are the object's metadata `organization-id`, `well-id`, `source-name` and `sha256`,
   each percent-encoded UTF-8.
4. The run's row in the run log, written `RUNNING`.
5. The PDF, downloaded to /tmp; its SHA-256 must be the label's.
6. The data assets, downloaded by name from ESP_ASSETS_PREFIX (the role cannot list the bucket) and
   put through the pipeline's own loaders, whose hash checks fail the run with ASSET_INVALID before
   any model call.
7. The pipeline, `extraction.run.run`, into a run store under /tmp, with a Bedrock client built for
   this run.  The model's budget is the time left less a 90 s reserve, at most 780 s.
8. The run folder, uploaded to `<runs prefix><run_id>/`: every other file first, `run.json` last.
   Local paths in `run.json` and `source.json` become S3 locations, so the source is the intake
   object; `started_at` and `deadline_at` stay the invocation's.
9. The row, updated to `SUCCEEDED` or `FAILED`.

The run log has one row per run, in the layout of the ESP product's `esp_logging_v2` and shaped as
its file-upload pipeline's `PIPELINE_EXECUTION` row (`log_row`).  A failure before step 4 (a bad
key, a failed create, bad labels) writes one `FAILED` row instead; a duplicate writes none.  Every
failure after step 2 leaves a failed `run.json` and a `FAILED` row.  The codes are the pipeline's,
plus INTAKE_INVALID (bad key, a label missing or undecodable, hash mismatch) and INTERNAL_ERROR
(anything unexpected; its trace goes to the log).  A run-log write that fails is logged and never
fails the run.  Each stage prints one JSON line with the run id to stdout, which
Lambda sends to CloudWatch.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote_plus

import boto3
from botocore.exceptions import ClientError

from extraction.assemble import ASSETS, INVENTORY
from extraction.assets_dir import ASSETS_ENV, asset
from extraction.bedrock import make_client
from extraction.confirm_writer import confirm
from extraction.confirmation import ASSETS_PREFIX, BUCKET, WRITTEN, S3Objects, request_id
from extraction.confirmation import ROOT as CONFIRMATION_ROOT
from extraction.errors import ExtractionError
from extraction.intake import (LABELS, PRODUCT_UPLOAD, RUN_ID, decode_label, decode_labels, encode_labels, intake_key,
                               intake_run_id)
from extraction.run import run, sha256, utc
from extraction.stage.stage import CATALOGUE, EVIDENCE, MANIFEST, SEED
from extraction.validate.validate import catalogue as read_catalogue

WORK = Path("/tmp/esp-design-extraction")   # intake/, assets/, and the run store's runs/<run_id>/
RESERVE_S = 90                              # kept from the model for staging, the upload and the last row write
MODEL_CAP_S = 780
# Each data asset, with the manifest its loader checks it against.
ASSET_MANIFESTS = {SEED: MANIFEST, EVIDENCE: MANIFEST, CATALOGUE: MANIFEST, INVENTORY: ASSETS / "MANIFEST.txt"}
PIPELINE_EXECUTION = "PIPELINE_EXECUTION"
UNKNOWN_ORGANIZATION = "_unknown"            # a row's organisation when the label is missing or does not decode
ROW_KEY = ("organization_id", "timestamp")


def now() -> datetime:
    return datetime.now(timezone.utc)


def model_budget(remaining_ms: int) -> float:
    """The model's seconds: the time left less the reserve, at most MODEL_CAP_S."""
    return round(min(remaining_ms / 1000 - RESERVE_S, MODEL_CAP_S), 3)


def log(run_id, stage: str, **fields) -> None:
    print(json.dumps({"run_id": run_id, "stage": stage, **fields}, default=str), flush=True)


def dump(obj) -> bytes:
    """As the pipeline writes run.json and source.json."""
    return (json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n").encode()


def relocate(obj, places):
    """`obj` with each local directory in `places` replaced, in every string, by its S3 location."""
    if isinstance(obj, dict):
        return {k: relocate(v, places) for k, v in obj.items()}
    if isinstance(obj, list):
        return [relocate(v, places) for v in obj]
    if isinstance(obj, str):
        for local, remote in places:
            obj = obj.replace(local, remote)
    return obj


def fetch_assets(s3, bucket: str, prefix: str, directory: Path, names=tuple(ASSET_MANIFESTS)) -> None:
    """Each data asset (or those named), downloaded by name, then through its loader: the loaders' hash checks are
    the gate."""
    directory.mkdir(parents=True, exist_ok=True)
    os.environ[ASSETS_ENV] = str(directory)
    for name in names:
        manifest = ASSET_MANIFESTS[name]
        try:
            body = s3.get_object(Bucket=bucket, Key=prefix + name)["Body"].read()
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "Unknown")
            raise ExtractionError("ASSET_INVALID", f"{name} could not be read from s3://{bucket}/{prefix}{name} ({code})",
                                  {"asset": name, "aws_error_code": code}) from err
        (directory / name).write_bytes(body)
        asset(name, manifest)


def standard_timestamp(at: datetime) -> str:
    """The run log's timestamp, as esp_resources_v2's get_standard_timestamp() writes it: UTC, ISO 8601, no
    offset; microseconds, which isoformat leaves out when they are zero."""
    return at.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def log_row(organization_id, pipeline_execution_id, well_id, status, data, at: datetime) -> dict:
    """The run's row, shaped as the file-upload pipeline's PIPELINE_EXECUTION row in esp_logging_v2, whose
    `uuid` is its pipeline execution id.  The function is `resource_name`, as esp_logging_v2's LOG rows name
    theirs; everything else of this pipeline's goes in `data_json`."""
    organization_id = organization_id or UNKNOWN_ORGANIZATION
    return {
        "organization_id": organization_id,
        "timestamp": standard_timestamp(at),
        "type": PIPELINE_EXECUTION,
        "organization_id_type": f"{organization_id}#{PIPELINE_EXECUTION}",
        "pipeline_execution_id": pipeline_execution_id,
        "uuid": pipeline_execution_id,
        "resource_name": os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        "well_ids": [well_id] if well_id else [],
        "status": status,
        "success": status == "SUCCEEDED",
        "data_json": json.dumps(data, default=str, sort_keys=True),
    }


def failed_row(organization_id, pipeline_execution_id, well_id, data) -> dict:
    """The one row of a run that failed before its RUNNING row: FAILED, and ended when it started."""
    row = log_row(organization_id, pipeline_execution_id, well_id, "FAILED", data, now())
    return {**row, "end_timestamp": row["timestamp"]}


def write_row(table, run_id, row, *, update=False) -> None:
    """PutItem, or with `update` an UpdateItem on the row's key that sets every other attribute, so the row
    is whole even if its PutItem failed.  Every name is aliased (`status`, `type` and `timestamp` are reserved
    words) and no key attribute is set.  A write that fails is logged and never fails the run."""
    write = "update" if update else "put"
    try:
        if update:
            names = [name for name in row if name not in ROW_KEY]
            table.update_item(Key={name: row[name] for name in ROW_KEY},
                              UpdateExpression="SET " + ", ".join(f"#{name} = :{name}" for name in names),
                              ExpressionAttributeNames={f"#{name}": name for name in names},
                              ExpressionAttributeValues={f":{name}": row[name] for name in names})
        else:
            table.put_item(Item=row)
        log(run_id, "run_log", write=write, status=row["status"])
    except Exception as err:
        log(run_id, "run_log", write=write, status=row["status"], error=f"{type(err).__name__}: {err}")


def failure(code, stage, message, trace) -> dict:
    return {"error_code": code, "stage": stage, "message": message, "stack_trace": trace}


def lambda_handler(event, context, *, s3=None, table=None, bedrock=make_client):
    """Runs each S3 record of `event`.  The keywords stand in for AWS in tests; `bedrock(budget_s)` builds
    the run's Bedrock client."""
    s3 = s3 or boto3.client("s3")
    table = table or boto3.resource("dynamodb").Table(os.environ["ESP_RUN_LOG_TABLE"])
    return [handle(s3_record, context, s3, table, bedrock) for s3_record in event.get("Records", [])]


def handle(s3_record, context, s3, table, bedrock) -> dict:
    started = now()
    deadline = started + timedelta(milliseconds=context.get_remaining_time_in_millis())
    bucket = os.environ["ESP_BUCKET"]
    intake, runs, assets = (os.environ[f"ESP_{name}_PREFIX"] for name in ("INTAKE", "RUNS", "ASSETS"))
    key = unquote_plus(s3_record["s3"]["object"]["key"])
    # The row's data_json: the object by its location, never the event, which names the uploader and their IP.
    row_data = {"request_id": context.aws_request_id, "bucket": s3_record["s3"]["bucket"]["name"], "key": key}
    run_id = intake_run_id(intake, key)
    if run_id is None:
        message = f"the key is not {intake}<run id>.pdf"
        log(None, "key", key=key, error="INTAKE_INVALID", message=message)
        write_row(table, None, failed_row(None, f"ADHOC#{context.aws_request_id}", None,
                                          {**row_data, **failure("INTAKE_INVALID", "key", message, None)}))
        return {"key": key, "status": "rejected"}

    prefix = f"{runs}{run_id}/"
    record = {"run_id": run_id, "status": "running", "started_at": utc(started), "deadline_at": utc(deadline),
              "ended_at": None, "mode": "pdf", "organization_id": None, "well_id": None, "family": None,
              "source": {"pdf": {"path": f"s3://{bucket}/{key}", "sha256": None}, "report": None, "source_name": None},
              "error": None, "store": {"files": ["run.json"], "failed_at": None}}
    try:
        s3.put_object(Bucket=bucket, Key=prefix + "run.json", Body=dump(record), IfNoneMatch="*")
    except ClientError as err:
        status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = err.response.get("Error", {}).get("Code")
        if status == 412 or (status == 409 and code == "ConditionalRequestConflict"):
            log(run_id, "duplicate", http_status=status, code=code)
            return {"run_id": run_id, "status": "duplicate"}
        trace = traceback.format_exc()
        log(run_id, "create", error="STORE_FAILED", trace=trace)
        write_row(table, run_id, failed_row(None, run_id, None, {
            **row_data, **failure("STORE_FAILED", "create", f"creating run.json: {err}", trace)}))
        return {"run_id": run_id, "status": "failed"}
    log(run_id, "create", deadline_at=record["deadline_at"])
    row_data.update(run_prefix=f"s3://{bucket}/{prefix}", deadline_at=record["deadline_at"])

    work = WORK.resolve()
    shutil.rmtree(work, ignore_errors=True)
    try:
        metadata = labels = start = trace = None
        stage = "labels"
        try:
            metadata = s3.head_object(Bucket=bucket, Key=key)["Metadata"]
            labels = decode_labels(metadata)
            record.update(organization_id=labels["organization-id"], well_id=labels["well-id"])
            record["source"]["pdf"]["sha256"] = labels["sha256"]
            record["source"]["source_name"] = labels["source-name"]
            log(run_id, stage, organization_id=labels["organization-id"], well_id=labels["well-id"])
            row_data["source_name"] = labels["source-name"]
            start = log_row(labels["organization-id"], run_id, labels["well-id"], "RUNNING", row_data, now())
            write_row(table, run_id, start)

            stage = "download"
            data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            if sha256(data) != labels["sha256"]:
                raise ExtractionError("INTAKE_INVALID", f"the PDF's SHA-256 is {sha256(data)}, not its label's",
                                      {"sha256": sha256(data), "label": labels["sha256"]})
            pdf = work / "intake" / f"{run_id}.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(data)
            log(run_id, stage, bytes=len(data))

            stage = "assets"
            fetch_assets(s3, bucket, assets, work / "assets")
            log(run_id, stage, checked=list(ASSET_MANIFESTS))

            stage = "run"
            budget = model_budget(context.get_remaining_time_in_millis())
            if budget <= 0:
                raise ExtractionError("MODEL_TIMEOUT", f"{budget + RESERVE_S:.0f} s left, inside the {RESERVE_S} s "
                                      "reserve; no model call was made", {"budget_s": budget})
            result = run(organization_id=labels["organization-id"], well_id=labels["well-id"], pdf=pdf,
                         store_root=work, budget_s=budget, model_id=os.environ["ESP_INFERENCE_PROFILE_ID"],
                         client=bedrock(budget), run_id=run_id, source_name=labels["source-name"])
            result.pop("store_dir")
            record = {**result, "started_at": record["started_at"], "deadline_at": record["deadline_at"]}
            log(run_id, stage, budget_s=budget, status=record["status"], family=record["family"],
                error=(record["error"] or {}).get("code"), cost_usd=record["cost_usd"])
        except Exception as err:
            trace = traceback.format_exc()
            code, message, details = ((err.code, err.message, err.details) if isinstance(err, ExtractionError)
                                      else ("INTERNAL_ERROR", f"{type(err).__name__}: {err}", {}))
            record.update(status="failed", ended_at=utc(now()),
                          error={"code": code, "message": message, "details": details, "stage": stage})
            log(run_id, stage, error=code, message=message, trace=trace if code == "INTERNAL_ERROR" else None)

        # Upload: every other file first, run.json last.  The first failure stops it.
        local = work / "runs" / run_id
        places = [(f"{work}/{sub}/", f"s3://{bucket}/{remote}")
                  for sub, remote in (("intake", intake), ("runs", runs), ("assets", assets))]
        names = sorted(p.name for p in local.iterdir()
                       if p.is_file() and p.name != "run.json" and not p.name.startswith(".")) if local.is_dir() else []
        stored = []
        try:
            for name in names:
                body = (local / name).read_bytes()
                if name == "source.json":
                    body = dump(relocate(json.loads(body), places))
                s3.put_object(Bucket=bucket, Key=prefix + name, Body=body)
                stored.append(name)
        except Exception as err:
            failed_at = names[len(stored)]
            log(run_id, "upload", error="STORE_FAILED", failed_at=failed_at, message=str(err))
            if record["error"] is None:
                trace = traceback.format_exc()
                record["error"] = {"code": "STORE_FAILED", "message": f"uploading {failed_at}: {err}",
                                   "details": {"failed_at": failed_at, "stored": list(stored)}, "stage": "upload"}
            record["status"] = "failed"
            record["store"]["failed_at"] = failed_at
        record["store"]["files"] = stored + ["run.json"]
        record = relocate(json.loads(dump(record)), places)
        try:
            s3.put_object(Bucket=bucket, Key=prefix + "run.json", Body=dump(record))
            log(run_id, "upload", files=record["store"]["files"])
        except Exception as err:            # the `running` run.json stays; past deadline_at it reads as stale
            log(run_id, "upload", error="STORE_FAILED", failed_at="run.json", message=str(err))

        if record["status"] == "succeeded":
            outcome = {"route": record["route"], "family": record["family"], "rows": record["rows"],
                       "review": record["review"], "cost_usd": record["cost_usd"]}
        else:
            error = record["error"]
            outcome = failure(error["code"], error["stage"], error["message"], trace)
        if start is None:                   # failed before its RUNNING row: the labels as far as each decodes
            known = {name: decode_label(name, (metadata or {}).get(name)) for name in LABELS}
            row_data.update({"source_name": known["source-name"]} if known["source-name"] else {})
            write_row(table, run_id, failed_row(known["organization-id"], run_id, known["well-id"],
                                                {**row_data, **outcome}))
        else:
            status = "SUCCEEDED" if record["status"] == "succeeded" else "FAILED"
            write_row(table, run_id, {**start, "status": status, "success": status == "SUCCEEDED",
                                      "end_timestamp": standard_timestamp(now()),
                                      "data_json": json.dumps({**row_data, **outcome}, default=str, sort_keys=True)},
                      update=True)
        log(run_id, "end", status=record["status"], error=(record["error"] or {}).get("code"))
        return {"run_id": run_id, "status": record["status"]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---- the confirmation writer: a second entry point in the same image ----

CONFIRM_WORK = Path("/tmp/esp-design-confirmation")    # the catalogue snapshot, fetched for each request


def confirm_handler(event, context, *, s3=None, table=None, catalogue=None):
    """The confirmation writer's entry point, `lambda_function.confirm_handler`.  S3 calls it for each
    `confirmations/<id>/request.json` created under s3://roam-ds-assets/esp-design-extraction/; the work is
    extraction/confirm_writer.py's, with the layout extraction/confirmation.py fixes, so it reads no ESP_* prefix:
    only ESP_RUN_LOG_TABLE.

    The run log gets one PIPELINE_EXECUTION row per request, through `log_row` and `write_row` as a run's: its ids
    are the confirmation id, its `resource_name` this function's name, and its `data_json` the request key, the run
    id, the row and version keys and the outcome, never the event.  It is written RUNNING once the request names its
    organisation and well, then updated to SUCCEEDED (written) or FAILED (refused or failed).  A request that cannot
    be read gets one FAILED row instead, a duplicate none, and a key that is not a request one FAILED row under
    `ADHOC#<request id>`.  Each step prints one JSON line whose `run_id` is the confirmation id; a failure's trace goes
    there too.  The keywords stand in for AWS in tests; `catalogue()` replaces fetching the snapshot."""
    s3 = s3 or boto3.client("s3")
    table = table or boto3.resource("dynamodb").Table(os.environ["ESP_RUN_LOG_TABLE"])
    return [confirm_one(s3_record, context, s3, table, catalogue) for s3_record in event.get("Records", [])]


def confirm_one(s3_record, context, s3, table, catalogue) -> dict:
    deadline = now() + timedelta(milliseconds=context.get_remaining_time_in_millis())
    key = unquote_plus(s3_record["s3"]["object"]["key"])
    cid = request_id(key)
    row_data = {"request_id": context.aws_request_id, "request_key": key}
    if cid is None:
        message = f"the key is not {CONFIRMATION_ROOT}confirmations/<id>/request.json"
        log(None, "key", key=key, message=message)
        write_row(table, None, failed_row(None, f"ADHOC#{context.aws_request_id}", None,
                                          {**row_data, "outcome": "rejected", "reasons": [message]}))
        return {"key": key, "status": "rejected"}

    def fetched():
        fetch_assets(s3, BUCKET, ASSETS_PREFIX, CONFIRM_WORK / "assets", names=(CATALOGUE,))
        return read_catalogue()

    start = {}

    def started(request):
        start["row"] = log_row(request["organization_id"], cid, request["well_id"], "RUNNING",
                               {**row_data, "run_id": request.get("run_id")}, now())
        write_row(table, cid, start["row"])

    shutil.rmtree(CONFIRM_WORK, ignore_errors=True)
    try:
        outcome = confirm(S3Objects(s3), key, deadline=deadline, catalogue=catalogue or fetched, started=started)
    finally:
        shutil.rmtree(CONFIRM_WORK, ignore_errors=True)
    failure_ = outcome["error"]
    if failure_:
        log(cid, failure_["stage"], error=failure_["code"], message=failure_["message"], trace=failure_["trace"])
    if outcome["status"] == "duplicate":
        log(cid, "duplicate")
        return {"confirmation_id": cid, "status": "duplicate"}
    data = {**row_data, "run_id": outcome["run_id"], "row_key": outcome["row_key"],
            "version_key": outcome["version_key"], "outcome": outcome["status"], "reasons": outcome["reasons"],
            **(failure(failure_["code"], failure_["stage"], failure_["message"], failure_["trace"]) if failure_ else {})}
    status = "SUCCEEDED" if outcome["status"] == WRITTEN else "FAILED"
    if "row" in start:
        write_row(table, cid, {**start["row"], "status": status, "success": status == "SUCCEEDED",
                               "end_timestamp": standard_timestamp(now()),
                               "data_json": json.dumps(data, default=str, sort_keys=True)}, update=True)
    else:
        write_row(table, cid, failed_row(outcome["organization_id"], cid, outcome["well_id"], data))
    log(cid, "end", status=outcome["status"], run=outcome["run_id"], reasons=len(outcome["reasons"]))
    return {"confirmation_id": cid, "status": outcome["status"]}


# ---- the product-upload starter: a third entry point in the same image ----

PUSH_KEY = re.compile(r"(dev|prd)/([^/]+)/well-documents/([^/]+)/esp_design/([^/]+\.pdf)")
PUSH_KEY_WORDS = "<dev|prd>/<organization id>/well-documents/<well id>/esp_design/<file>.pdf"
NOT_THERE = ("NoSuchKey", "NoSuchVersion", "AccessDenied")     # S3's codes for a read refused as not there (with 403, 404)


def product_upload_handler(event, context, *, s3=None, table=None):
    """The starter's entry point, `lambda_function.product_upload_handler`.  The ESP product puts each design PDF an
    engineer uploads there into ESP_PUSH_BUCKET, at dev/ or prd/ followed by the product's own key, so
    `<dev|prd>/<organization id>/well-documents/<well id>/esp_design/<file>.pdf`, with the metadata `uploaded-by` and
    `original-filename`; S3 calls this for each .pdf created.  Each record starts one run, as the app's upload form
    does: the PDF is put at `<ESP_INTAKE_PREFIX><run_id>.pdf` in ESP_BUCKET with its labels (extraction/intake.py),
    create-only, and that upload starts the extraction.

    1. The key, decoded with unquote_plus as the extraction decodes its own, must be a push's, with an organisation and
       a well, in ESP_PUSH_BUCKET.
    2. The run id: the event's eventTime to the second, in extraction.run's format, then the first 8 hex digits of the
       SHA-256 of `<bucket>/<key>@<versionId, or the event's sequencer>`: a repeated event is the same run, and each
       version of a key its own.
    3. The event's object version (its versionId when the event has one) is read, with its metadata.
    4. The labels: organization-id and well-id from the key, exactly as decoded; sha256 of the bytes; source-name the
       push's original-filename when that decodes to a plain file name ending .pdf, else the key's file name; and the
       optional ones: origin product-upload, environment from the key, uploaded-by when the push carries it, and
       pushed-from `s3://<bucket>/<key>?versionId=<id>`.
    5. The intake object, ContentType application/pdf, create-only.  A 412, or a 409 ConditionalRequestConflict, is a
       duplicate: logged, and nothing else is written.

    A key or an event refused, an object S3 refuses as not there (it answers 403 or 404, or NoSuchKey, NoSuchVersion or
    AccessDenied), or a label refused (as the required labels' rules refuse it) gets one FAILED row in the run log
    (INTAKE_INVALID), made as the extraction's bad-key row is, under the run id once there is one (else ADHOC#<request
    id>), and nothing is put.  A read that fails otherwise (a 500, a throttle, a timeout: INTERNAL_ERROR, stage read,
    with the trace) and an intake put that fails otherwise (STORE_FAILED) each get a FAILED row and are raised, so
    that Lambda retries the event: the same run, create-only.  A run that starts writes no row: the extraction's is
    the run's.  Each step prints one JSON line with the run id.  The event is never logged or stored, as it names the
    uploader and their IP, and the uploader goes only on the intake object, as its label.  The keywords stand in for
    AWS in tests."""
    s3 = s3 or boto3.client("s3")
    table = table or boto3.resource("dynamodb").Table(os.environ["ESP_RUN_LOG_TABLE"])
    return [start_run(s3_record, context, s3, table) for s3_record in event.get("Records", [])]


def start_run(s3_record, context, s3, table) -> dict:
    push, bucket, intake = os.environ["ESP_PUSH_BUCKET"], os.environ["ESP_BUCKET"], os.environ["ESP_INTAKE_PREFIX"]
    source, pushed = s3_record["s3"]["bucket"]["name"], s3_record["s3"]["object"]
    key, version = unquote_plus(pushed["key"]), pushed.get("versionId")
    # The row's data_json: the pushed object by its location and version, never the event.
    row_data = {"request_id": context.aws_request_id, "bucket": source, "key": key, "version_id": version}
    run_id = organization_id = well_id = None

    def rejected(stage, message) -> dict:
        log(run_id, stage, key=key, error="INTAKE_INVALID", message=message)
        write_row(table, run_id, failed_row(organization_id, run_id or f"ADHOC#{context.aws_request_id}", well_id,
                                            {**row_data, **failure("INTAKE_INVALID", stage, message, None)}))
        return {"key": key, "status": "rejected"}

    match = PUSH_KEY.fullmatch(key)
    if source != push or match is None:
        return rejected("key", f"the key is not {PUSH_KEY_WORDS} in {push}")
    environment, organization_id, well_id, file_name = match.groups()
    try:
        at, sequence = datetime.fromisoformat(s3_record["eventTime"]), version or pushed["sequencer"]
        made = f"{at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{sha256(f'{push}/{key}@{sequence}'.encode())[:8]}"
        if at.tzinfo is None or not sequence or not RUN_ID.fullmatch(made):
            raise ValueError(made)
    except (KeyError, TypeError, ValueError):
        return rejected("run_id", "the event gives no run id: it needs an eventTime with its zone, and a versionId "
                                  "or a sequencer")
    run_id = made

    try:
        got = s3.get_object(Bucket=push, Key=key, **({"VersionId": version} if version else {}))
        data, metadata = got["Body"].read(), got.get("Metadata") or {}
    except Exception as err:
        response = getattr(err, "response", None) or {}
        status, code = response.get("ResponseMetadata", {}).get("HTTPStatusCode"), response.get("Error", {}).get("Code")
        if status in (403, 404) or code in NOT_THERE:          # refused as not there: retrying would not change it
            return rejected("read", f"s3://{push}/{key} could not be read ({code or status})")
        trace = traceback.format_exc()                          # a reason that may pass: a row, then Lambda retries
        log(run_id, "read", error="INTERNAL_ERROR", trace=trace)
        write_row(table, run_id, failed_row(organization_id, run_id, well_id, {
            **row_data, **failure("INTERNAL_ERROR", "read", f"reading s3://{push}/{key}: {type(err).__name__}: {err}",
                                  trace)}))
        raise
    log(run_id, "read", bytes=len(data), version_id=version)

    original = decode_label("source-name", metadata.get("original-filename"))
    optional = {"origin": PRODUCT_UPLOAD, "environment": environment,
                "uploaded-by": decode_label("uploaded-by", metadata.get("uploaded-by")),
                "pushed-from": f"s3://{push}/{key}" + (f"?versionId={version}" if version else "")}
    try:
        labels = encode_labels(organization_id, well_id, original if original and original.lower().endswith(".pdf")
                               else file_name, sha256(data))
    except ExtractionError as err:
        return rejected("labels", err.message)
    labels |= {name: quote(value, safe="") for name, value in optional.items() if value is not None}
    log(run_id, "labels", organization_id=organization_id, well_id=well_id, environment=environment,
        optional=[name for name, value in optional.items() if value is not None])

    target = intake_key(intake, run_id)
    try:
        s3.put_object(Bucket=bucket, Key=target, Body=data, ContentType="application/pdf", Metadata=labels,
                      IfNoneMatch="*")
    except Exception as err:
        response = getattr(err, "response", None) or {}
        status, code = response.get("ResponseMetadata", {}).get("HTTPStatusCode"), response.get("Error", {}).get("Code")
        if status == 412 or (status == 409 and code == "ConditionalRequestConflict"):
            log(run_id, "duplicate", http_status=status, code=code)
            return {"run_id": run_id, "status": "duplicate"}
        trace = traceback.format_exc()
        log(run_id, "intake", error="STORE_FAILED", trace=trace)
        write_row(table, run_id, failed_row(organization_id, run_id, well_id, {
            **row_data, **failure("STORE_FAILED", "intake", f"putting {target}: {err}", trace)}))
        raise
    log(run_id, "intake", key=target)
    return {"run_id": run_id, "status": "started"}
