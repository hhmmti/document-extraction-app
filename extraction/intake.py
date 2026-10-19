"""The intake contract between the app's S3 mode and the Lambda: one upload, one run.

An upload is `<intake prefix><run_id>.pdf`, the run id in `extraction.run`'s format, labelled with
four object-metadata values, each percent-encoded UTF-8: `organization-id`, `well-id`, `source-name`
(a plain file name) and `sha256` (the PDF's, lower-case hex).  The app encodes the labels, the Lambda
decodes them, and both read the key here.  Standard library only, so the viewer can import it
without loading the AWS SDK.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote

from .errors import ExtractionError

LABELS = ("organization-id", "well-id", "source-name", "sha256")
RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")    # extraction.run.RUN_ID, restated: that module loads botocore
PERCENT_ENCODED = re.compile(r"(?:[^%]|%[0-9A-Fa-f]{2})+")
SHA256 = re.compile(r"[0-9a-f]{64}")


def intake_key(prefix: str, run_id: str) -> str:
    return f"{prefix}{run_id}.pdf"


def intake_run_id(prefix: str, key: str) -> str | None:
    """The run id an intake key names, or None when the key is not `<prefix><run_id>.pdf`."""
    match = re.fullmatch(f"{re.escape(prefix)}({RUN_ID.pattern})\\.pdf", key)
    return match[1] if match else None


def encode_labels(organization_id: str, well_id: str, source_name: str, sha256: str) -> dict:
    """The upload's object metadata.  A value the Lambda would refuse raises INTAKE_INVALID here, before
    anything is sent."""
    metadata = {name: quote(value, safe="")
                for name, value in zip(LABELS, (organization_id, well_id, source_name, sha256))}
    decode_labels(metadata)
    return metadata


def decode_label(name: str, raw) -> str | None:
    """One label, percent-decoded as UTF-8; None when it is missing or does not decode."""
    raw = raw if isinstance(raw, str) else ""
    try:
        value = unquote(raw, errors="strict") if PERCENT_ENCODED.fullmatch(raw) else ""
    except UnicodeDecodeError:
        value = ""
    if name == "source-name" and Path(value).name != value:         # a plain file name, as --source-name
        value = ""
    if name == "sha256" and not SHA256.fullmatch(value):
        value = ""
    return value if value.strip() and value.isprintable() else None


def decode_labels(metadata: dict) -> dict:
    """The four labels, percent-decoded as UTF-8.  INTAKE_INVALID names each one missing or undecodable."""
    labels = {name: decode_label(name, metadata.get(name)) for name in LABELS}
    bad = [name for name, value in labels.items() if value is None]
    if bad:
        raise ExtractionError("INTAKE_INVALID", f"object metadata missing or undecodable: {', '.join(bad)}",
                              {"labels": bad})
    return labels
