"""Intake for the upload form: start a run, then watch it.

Local mode: the PDF becomes `<store>/intake/<sha256>.pdf`; `python -m extraction` runs detached,
its output in `<store>/launch/<run_id>.log`.  Organisation and well ids travel only as process
arguments: nothing derived from them becomes part of a path.

S3 mode: the PDF becomes `<intake prefix><run_id>.pdf` in the bucket, with the labels the Lambda
reads (`extraction.intake`), and the upload starts the run.  No process is started.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.store import file_sha256, run_status
from extraction.intake import encode_labels, intake_key

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
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


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


def upload(store, run_id: str, data: bytes, organization_id: str, well_id: str, source_name: str) -> None:
    """S3 mode: the PDF as `<intake prefix><run_id>.pdf`, labelled for the Lambda, which starts on it.
    A label the Lambda would refuse raises INTAKE_INVALID before anything is sent."""
    metadata = encode_labels(organization_id, well_id, source_name, hashlib.sha256(data).hexdigest())
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
