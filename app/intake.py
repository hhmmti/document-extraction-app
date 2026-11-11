"""Intake for the upload form: start a run, then watch it.

Local mode: the PDF becomes `<store>/intake/<sha256>.pdf`; `python -m extraction` runs detached,
its output in `<store>/launch/<run_id>.log`.  Organisation and well ids travel only as process
arguments: nothing derived from them becomes part of a path.  A copy, `<store>/intake/<run_id>.pdf`, carries
the upload's labels beside it, where the confirmation writer and the store read them (`label`).

S3 mode: the PDF becomes `<intake prefix><run_id>.pdf` in the bucket, with the labels the Lambda
reads (`extraction.intake`), and the upload starts the run.  No process is started.

Either way the labels are the four and the optional label `environment` (`labels`): where the form's list came
from, dev or prd, so that the run and its confirmation stay there.  The labels take printable values only, so the form
asks `refused`, by the labels' own rule, as soon as an organisation or a well is chosen and once a PDF is added: a name
or a file name they refuse starts no run.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from app.store import file_sha256, run_status
from extraction import confirmation as C
from extraction.errors import ExtractionError
from extraction.intake import decode_label, encode_labels, intake_key, optional_labels

LAB = Path(__file__).resolve().parents[1]
STARTUP_S = 120          # no run.json by then, and no process known to be alive: the launch failed
PICKUP_S = 5 * 60        # S3 mode: an upload waiting longer, while no run is running, was not picked up
EXPIRY_S = 6 * 3600      # S3 mode: Lambda keeps a waiting asynchronous event for 6 hours by default
PROCESSES: dict[str, subprocess.Popen] = {}     # this app process's launches, by run id


def new_run_id(now: datetime) -> str:
    """extraction.run.new_run_id, restated: importing extraction.run loads botocore."""
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def save_pdf(root, data: bytes) -> Path:
    """The upload as `<root>/intake/<sha256>.pdf`, written atomically; a copy already there is
    verified and reused."""
    sha = hashlib.sha256(data).hexdigest()
    path = Path(root) / "intake" / f"{sha}.pdf"
    if path.exists():
        if file_sha256(path) != sha:
            raise ValueError(f"intake/{path.name} does not hold the content its name promises; remove it")
        return path
    write(path, data)
    return path


def write(path: Path, data: bytes) -> None:
    """Written whole under a temporary name, then renamed into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def labels(organization_id: str, well_id: str, source_name: str, sha256: str, environment: str) -> dict:
    """The upload's object metadata: the four labels and the optional label `environment`, each percent-encoded as
    extraction/intake.py encodes them.  A value the Lambda or the writer would refuse raises INTAKE_INVALID here,
    before anything is sent."""
    metadata = {**encode_labels(organization_id, well_id, source_name, sha256),
                "environment": quote(environment, safe="")}
    if optional_labels(metadata).get("environment") != environment:
        raise ExtractionError("INTAKE_INVALID", f"environment {environment!r} is not dev or prd",
                              {"labels": ["environment"]})
    return metadata


def refused(organization_id: str | None = None, well_id: str | None = None,
            source_name: str | None = None) -> list[str]:
    """The labels, of `organization-id`, `well-id` and `source-name` (the PDF's file name), that the upload's would
    refuse for these values: by their own rule, extraction/intake.py's `decode_label`, on each value as `encode_labels`
    encodes it.  The rule takes printable values only (and, for the file name, a plain file name), so a name with a
    no-break space, a tab, another kind of space or a zero-width space can start no run.  None, nothing chosen or added
    yet, is not refused."""
    return [name for name, value in (("organization-id", organization_id), ("well-id", well_id),
                                     ("source-name", source_name))
            if value is not None and decode_label(name, quote(value, safe="")) is None]


def label(root, run_id: str, data: bytes, metadata: dict) -> None:
    """Local mode: the PDF as `intake/<run_id>.pdf`, its labels beside it as the store keeps an object's metadata
    (extraction/confirmation.py's LocalObjects: `.<run_id>.pdf.metadata.json`), so that the local store's
    `intake_labels` and the writer read them as S3 mode's.  The labels go first; the PDF is create-only."""
    objects = C.LocalObjects(root)
    write(objects.meta(C.pdf_key(run_id)), json.dumps(metadata).encode())
    objects.put(C.pdf_key(run_id), data, create=True)


def launch(root, pdf: Path, organization_id: str, well_id: str, run_id: str, source_name: str):
    """Start the pipeline on the intake copy as a detached process; it keeps running if the app stops."""
    log = Path(root) / "launch" / f"{run_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # `--flag=value`, so that argparse reads a value starting with '-' as a value, not as an option.
    args = [sys.executable, "-m", "extraction", f"--pdf={pdf}", f"--org={organization_id}", f"--well={well_id}",
            f"--run-id={run_id}", f"--source-name={source_name}"]
    with open(log, "xb") as out:
        PROCESSES[run_id] = subprocess.Popen(args, cwd=LAB, stdin=subprocess.DEVNULL, stdout=out,
                                             stderr=subprocess.STDOUT, start_new_session=True)
    return PROCESSES[run_id]


def exited(run_id: str) -> bool | None:
    """Whether this app's process for the run has ended; None when this app process did not launch it."""
    proc = PROCESSES.get(run_id)
    return None if proc is None else proc.poll() is not None


def age_s(run_id: str, now: datetime) -> float | None:
    """Seconds since the launch, from the run id's UTC second; None for an id not in that form."""
    try:
        launched = datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - launched).total_seconds()


def watch_state(record: dict | None, run_id: str, now: datetime, ended: bool | None) -> str:
    """`starting`, `no record yet`, `launch failed`, or the record's status as a reader sees it (`stale`
    past its deadline).  Without a record: an ended process failed the launch at once; a live one is
    still `no record yet` after STARTUP_S; an unknown one (another app process) fails it after STARTUP_S."""
    if record is None:
        age = age_s(run_id, now)
        if ended or age is None:
            return "launch failed"
        if age <= STARTUP_S:
            return "starting"
        return "launch failed" if ended is None else "no record yet"
    status, stale = run_status(record, now)
    return "stale" if stale else str(status)


def upload(store, run_id: str, data: bytes, organization_id: str, well_id: str, source_name: str,
           environment: str) -> None:
    """S3 mode: the PDF as `<intake prefix><run_id>.pdf`, labelled for the Lambda, which starts on it.
    A label the Lambda would refuse raises INTAKE_INVALID before anything is sent."""
    metadata = labels(organization_id, well_id, source_name, hashlib.sha256(data).hexdigest(), environment)
    store.client.put_object(Bucket=store.bucket, Key=intake_key(store.intake_prefix, run_id), Body=data,
                            ContentType="application/pdf", Metadata=metadata, IfNoneMatch="*")
    store.refresh()


def queue_state(waited_s: float, running: bool) -> str:
    """S3 mode, an upload with no run record yet: `expired` after EXPIRY_S; `not picked up` after PICKUP_S
    while no run is running (with one running, it waits its turn); else `queued`."""
    if waited_s > EXPIRY_S:
        return "expired"
    if waited_s > PICKUP_S and not running:
        return "not picked up"
    return "queued"
