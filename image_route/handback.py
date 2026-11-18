"""The hand-back: a built transcription, checked, then put at intake as an upload of its own, which the extraction's
trigger runs as it runs any upload.

Two checks come before the put (`problem`), and a PDF that fails either is not put:

    Detect, as the extraction calls it (`extraction.detect.route`), must not raise IMAGE_ONLY: the transcription has
    the text layer the scan lacked.  Any other answer of Detect's is the new run's to report.
    `app.transcription.recognise` must take the PDF as a transcription: the mark, and the scan attached, page for page.

The upload (`put`) is `<intake prefix><new run id>.pdf`, the id fresh and in the upload form's own pattern
(`extraction.run.new_run_id`), create-only.  Its labels (`labels`) are the original's, decoded from the original intake
object's metadata and encoded again by extraction/intake.py's rules: the four the extraction requires, and each
optional one the original carries with a value it may take, so an original with no `environment` gives a transcription
with none.  Two differ: `source-name` is the transcription's file name, as the skill's build names it, and `sha256` is
the transcription's.  One is new: `transcribed-from`, the original run id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from app.transcription import recognise
from extraction.confirmation import S3Objects
from extraction.detect import route
from extraction.errors import ExtractionError
from extraction.intake import decode_labels, encode_labels, intake_key, optional_labels
from extraction.run import new_run_id, sha256
from lambda_function import TRANSCRIBED_FROM, conflict

PUT_TRIES = 3


def problem(pdf: bytes) -> str | None:
    """Why `pdf` is not handed back, or None when it passes both checks."""
    try:
        route(pdf)
    except ExtractionError as err:
        if err.code == "IMAGE_ONLY":
            return f"Detect finds no text layer in the transcription: {err.message}"
    scan = recognise(pdf)
    if scan is None:
        return "the built PDF does not carry the transcription mark"
    return f"the built PDF is a broken transcription: {scan.problem}" if scan.problem else None


def labels(metadata: dict, source_name: str, pdf_sha256: str, run_id: str) -> dict:
    """The transcription's object metadata, from the original's.  INTAKE_INVALID when a required label of the
    original's does not decode, or the new ones would be refused."""
    original = decode_labels(metadata)
    out = encode_labels(original["organization-id"], original["well-id"], source_name, pdf_sha256)
    out |= {name: quote(value, safe="") for name, value in optional_labels(metadata).items()}
    return {**out, TRANSCRIBED_FROM: run_id}


def put(s3, bucket: str, intake: str, pdf: bytes, metadata: dict, source_name: str, run_id: str) -> str:
    """Puts the transcription at intake, create-only, and returns its run id.  A key found there is this put's own
    when its labels are these, hash and all: the put landed and the SDK tried it again.  Otherwise it is another
    upload's, and a fresh id is tried, PUT_TRIES in all."""
    labelled = labels(metadata, source_name, sha256(pdf), run_id)
    for _ in range(PUT_TRIES):
        new_id = new_run_id(datetime.now(timezone.utc))
        key = intake_key(intake, new_id)
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=pdf, ContentType="application/pdf", Metadata=labelled,
                          IfNoneMatch="*")
            return new_id
        except Exception as err:
            if not conflict(err):
                raise
            if S3Objects(s3, bucket).head(key) == labelled:
                return new_id
    raise RuntimeError(f"{PUT_TRIES} fresh run ids were each taken under {intake}")
