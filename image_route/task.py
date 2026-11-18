"""The image route's task: one request's scan read, built into a transcription and handed back, under a cap.

    python -m image_route.task          the container's command: the supervisor
    python -m image_route.task work     the work, which the supervisor runs as its child

The starter (lambda_function.transcription_start_handler) runs it on Fargate and passes four variables:
ESP_TRANSCRIPTION_RUN_ID and _REQUEST_ID, _ROW_KEY (the key of the request's row in the run log, as JSON) and
_DEADLINE_AT (UTC, to the second).  The task's own are ESP_BUCKET, ESP_INTAKE_PREFIX, ESP_RUNS_PREFIX,
ESP_TRANSCRIPTIONS_PREFIX and ESP_RUN_LOG_TABLE, as the stack's functions have them; ESP_TRANSCRIPTION_TASK_NAME, the
`resource_name` it ends a row with (NAME unless set); the two margins below; and the reader's (image_route/settings.py),
of which ESP_IMAGE_ROUTE_CACHE is on here unless it is set, and ESP_IMAGE_ROUTE_DEADLINE_S is not read: the soft limit
takes its place.

The work (`work`), each step a stage:

    original   the scan and its labels, from the original run's intake object, and that run's run.json, which must
               say the run failed IMAGE_ONLY
    pages      every page read by the model (image_route/reader.py); no call starts after the soft limit
    build      the transcription, built by the skill's script as the reader calls it
    check      the hand-back's two checks (image_route/handback.py)
    put        the transcription at intake, as an upload of its own

The cap is two limits, both counted back from the deadline the starter passed:

    soft   ESP_TRANSCRIPTION_SOFT_MARGIN_S before it (600: 50 minutes after the starter's start).  The work starts no
           call after it, lets the calls already running finish, and ends the request as TRANSCRIPTION_TIMEOUT.
    hard   ESP_TRANSCRIPTION_HARD_MARGIN_S before it (120: 58 minutes).  The supervisor kills the work if it is still
           running then, whatever it hangs on, and ends the request as TRANSCRIPTION_TIMEOUT itself.

Every ending goes through lambda_function.end_transcription: `result.json`, create-only, then the row.  The work ends
the request as SUCCEEDED with the new run id, or as TRANSCRIPTION_FAILED with the stage that failed; the supervisor
ends it (stage supervisor) when the work is killed or exits without having ended it, and changes nothing when the work
ended it first.  Each page's time, calls, tokens and cost, and their totals, go into both whenever pages were read; a
kill loses them.  The exit code is 0 for a transcription handed back and 1 otherwise.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

import lambda_function as L
from extraction.intake import decode_labels, intake_key
from extraction.run import sha256

from . import handback, render
from .reader import TOKENS, read_document
from .settings import PREFIX, load

NAME = "esp-design-extraction-transcription"
SOFT_MARGIN_S, HARD_MARGIN_S = 600, 120
PAGE = ("page", "status", "error", "wall_s", "calls", "crops", "retries", *TOKENS, "cost_usd")    # a page, as recorded
TIMEOUT = "TRANSCRIPTION_TIMEOUT"


class Stop(Exception):
    """The work ends the request here, failed, at the stage it is in; the message says why."""

    def __init__(self, message: str, code: str = "TRANSCRIPTION_FAILED"):
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Job:
    run_id: str             # the original run
    request_id: str
    row: dict               # the key of the request's row
    soft: datetime
    hard: datetime
    by: str                 # this task, as a row names it
    data: dict              # what every ending's data_json starts from


def job() -> Job:
    """The request this task was started for, from its environment.  Anything missing or malformed raises: the task
    then ends nothing, and the watcher ends the request."""
    run_id, request_id = os.environ[L.TASK_ENV + "RUN_ID"], os.environ[L.TASK_ENV + "REQUEST_ID"]
    row, deadline_at = json.loads(os.environ[L.TASK_ENV + "ROW_KEY"]), os.environ[L.TASK_ENV + "DEADLINE_AT"]
    key = L.transcription_key(run_id, request_id)
    if not L.transcription_ids(key):
        raise ValueError(f"{run_id!r} and {request_id!r} do not name a request")
    if not (isinstance(row, dict) and set(row) == set(L.ROW_KEY) and all(isinstance(v, str) for v in row.values())):
        raise ValueError(f"{L.TASK_ENV}ROW_KEY is not a row's key")
    deadline = datetime.strptime(deadline_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    soft, hard = (deadline - timedelta(seconds=float(os.environ.get(L.TASK_ENV + name) or default))
                  for name, default in (("SOFT_MARGIN_S", SOFT_MARGIN_S), ("HARD_MARGIN_S", HARD_MARGIN_S)))
    return Job(run_id, request_id, row, soft, hard, os.environ.get(L.TASK_ENV + "TASK_NAME") or NAME,
               {"request_key": key, "deadline_at": deadline_at})


def work(job: Job, *, s3, table, client=None, sleep=time.sleep) -> int:
    """Does the request's work and ends it.  `client` stands in for Bedrock in tests."""
    bucket, intake, runs = (os.environ[name] for name in ("ESP_BUCKET", "ESP_INTAKE_PREFIX", "ESP_RUNS_PREFIX"))
    outcome, error, stage = {}, None, "original"
    try:
        with tempfile.TemporaryDirectory(prefix="transcription-") as folder:
            got = s3.get_object(Bucket=bucket, Key=intake_key(intake, job.run_id))
            original, metadata = got["Body"].read(), got.get("Metadata") or {}
            name = decode_labels(metadata)["source-name"]
            record = json.loads(s3.get_object(Bucket=bucket, Key=f"{runs}{job.run_id}/run.json")["Body"].read())
            code = (record.get("error") or {}).get("code")
            if record.get("status") != "failed" or code != "IMAGE_ONLY":
                raise Stop(f"the run did not fail IMAGE_ONLY: its run.json says {record.get('status')}"
                           + (f", {code}" if code else ""))
            L.log(job.run_id, stage, request_id=job.request_id, bytes=len(original))

            stage = "pages"
            settings = load()
            if not os.environ.get(PREFIX + "CACHE", "").strip():
                settings = dataclasses.replace(settings, cache=True)
            left = (job.soft - L.now()).total_seconds()
            if left <= 0:
                raise Stop(f"the soft limit, {L.utc(job.soft)}, had passed before a page was started", TIMEOUT)
            # Under the name the skill would attach it by, so the transcription is named as the skill names it.
            scan = Path(folder) / render.scripts(settings.skill_dir).B.attachment_name(Path(name))
            scan.write_bytes(original)
            account = read_document(scan, Path(folder) / "out", client=client, sleep=sleep,
                                    settings=dataclasses.replace(settings, deadline_s=left))
            total = account["total"]
            outcome = {"pages": [{field: page[field] for field in PAGE} for page in account["pages"]], "total": total,
                       "cost_usd": total["cost_usd"]}
            L.log(job.run_id, stage, request_id=job.request_id, pages=total["pages"], failed=total["pages_failed"],
                  wall_s=total["wall_s"], cost_usd=total["cost_usd"])
            if account["transcription"] is None:
                if any(call.get("error") == "Deadline" for call in account["calls"]):
                    raise Stop(f"the soft limit, {L.utc(job.soft)}, passed with pages unread; {account['error']}",
                               TIMEOUT)
                stage = "pages" if total["pages_failed"] else "build"
                raise Stop(account["error"])

            stage = "check"
            built = Path(account["transcription"])
            pdf = built.read_bytes()
            if why := handback.problem(pdf):
                raise Stop(why)

            stage = "put"
            new_run_id = handback.put(s3, bucket, intake, pdf, metadata, built.name, job.run_id)
            outcome.update(new_run_id=new_run_id, transcription={
                "key": intake_key(intake, new_run_id), "source_name": built.name, "sha256": sha256(pdf),
                "bytes": len(pdf)})
            L.log(job.run_id, stage, request_id=job.request_id, new_run_id=new_run_id)
    except Stop as stop:
        error = L.failure(stop.code, stage, str(stop), None)
    except Exception as err:
        error = L.failure("TRANSCRIPTION_FAILED", stage, f"{type(err).__name__}: {err}", traceback.format_exc())
    if error:
        L.log(job.run_id, stage, request_id=job.request_id, error=error["error_code"], message=error["message"],
              trace=error["stack_trace"])
    L.end_transcription(s3, table, job.run_id, job.request_id, job.row, job.by, job.data, error, **outcome)
    return 1 if error else 0


def supervise(job: Job, *, s3, table, command=None) -> int:
    """Runs the work as a child and waits for it until the hard limit.  `command` stands in for the work in tests."""
    child = subprocess.Popen(command or [sys.executable, "-m", "image_route.task", "work"])
    try:
        code = child.wait(timeout=max(0.0, (job.hard - L.now()).total_seconds()))
        if code == 0:
            return 0
        error = L.failure("TRANSCRIPTION_FAILED", "supervisor", f"the work ended with exit code {code}, and had not "
                                                                "ended the request", None)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        error = L.failure(TIMEOUT, "supervisor", f"the work was still running at the hard limit, {L.utc(job.hard)}, "
                                                 "and was killed", None)
    # Create-only: nothing changes when the work ended the request itself and then exited 1.
    if L.end_transcription(s3, table, job.run_id, job.request_id, job.row, job.by, job.data, error):
        L.log(job.run_id, "supervisor", request_id=job.request_id, error=error["error_code"], message=error["message"])
    return 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    the_job = job()
    s3, table = boto3.client("s3"), boto3.resource("dynamodb").Table(os.environ["ESP_RUN_LOG_TABLE"])
    return (work if argv == ["work"] else supervise)(the_job, s3=s3, table=table)


if __name__ == "__main__":
    sys.exit(main())
