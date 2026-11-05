"""A transcription: the PDF the transcribe-design-scan skill (skills/transcribe-design-scan/) types from a scanned design
document, recognised from the PDF's bytes alone.

The skill types each scan page as one page, page N for page N, attaches the scan byte for byte, and marks the document
information with /RoamTranscription, one JSON object: schema 1, the skill and its version, the original's name,
SHA-256, page count and size, the attachment's name, when and by which model it was typed, and one {page, line} per
reading marked unsure.

    recognise(pdf) -> Scan | None

None: no mark, an ordinary PDF.  A Scan is a transcription when the mark parses with a known schema, exactly one
attachment has the mark's SHA-256, and that attachment's page count is the mark's and the transcription's.  The mark
there and anything else not so is a broken transcription: the Scan's `problem` says why, in plain words.  Results are
kept by the PDF's SHA-256.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass

from pypdf import PdfReader

MARK = "/RoamTranscription"
SCHEMA = 1
KEPT = 16                                   # recognitions kept, the most recent, by the PDF's SHA-256
SHA256 = re.compile(r"[0-9a-f]{64}")
logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass(frozen=True)
class Scan:
    name: str | None = None                 # the original's file name, as the mark gives it
    pages: int | None = None
    sha256: str | None = None               # the original's
    unsure: tuple = ()                      # the pages with a reading marked unsure, in order
    original: bytes | None = None           # the scan, as attached
    problem: str | None = None              # a broken transcription: why, in plain words


_SEEN: dict[str, Scan | None] = {}


def recognise(pdf: bytes) -> Scan | None:
    """The PDF as a transcription, a broken one, or None for an ordinary PDF."""
    sha = hashlib.sha256(pdf).hexdigest()
    if sha not in _SEEN:
        if len(_SEEN) >= KEPT:
            _SEEN.pop(next(iter(_SEEN)))
        _SEEN[sha] = _recognise(pdf)
    return _SEEN[sha]


def _count(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and n >= 1


def _recognise(pdf: bytes) -> Scan | None:
    try:
        reader = PdfReader(io.BytesIO(pdf))
        raw = (reader.metadata or {}).get(MARK)
    except Exception:  # noqa: BLE001 - not readable as a PDF: no mark to see, and the run says what is wrong
        return None
    if raw is None:
        return None
    try:
        mark = json.loads(str(raw))
    except ValueError:
        return Scan(problem="its transcription mark cannot be read")
    if not isinstance(mark, dict) or mark.get("schema") != SCHEMA:
        return Scan(problem="its transcription mark is not one this app knows")
    original, unsure = mark.get("original"), mark.get("unsure")
    if not (isinstance(original, dict) and isinstance(original.get("name"), str) and original["name"]
            and SHA256.fullmatch(str(original.get("sha256"))) and _count(original.get("pages"))
            and isinstance(unsure, list) and all(isinstance(u, dict) and _count(u.get("page")) for u in unsure)):
        return Scan(problem="its transcription mark is incomplete")
    try:
        attached = [data for datas in reader.attachments.values() for data in datas]
        found = [data for data in attached if hashlib.sha256(data).hexdigest() == original["sha256"]]
    except Exception:  # noqa: BLE001 - its attachments cannot be read
        return Scan(problem="the files attached to it cannot be read")
    if not found:
        return Scan(problem="the original scan is not attached to it")
    if len(found) > 1:
        return Scan(problem="the original scan is attached to it more than once")
    try:
        scanned = len(PdfReader(io.BytesIO(found[0])).pages)
    except Exception:  # noqa: BLE001
        return Scan(problem="the original scan attached to it cannot be opened")
    typed = len(reader.pages)
    if not scanned == typed == original["pages"]:
        return Scan(problem=f"its pages do not match the original scan's: {typed} typed, {scanned} in the scan "
                            f"attached, {original['pages']} in its mark")
    return Scan(name=original["name"], pages=typed, sha256=original["sha256"], original=found[0],
                unsure=tuple(sorted({u["page"] for u in unsure})))
