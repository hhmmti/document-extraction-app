"""Confirming a run's proposed well entry: the request, its result, the design record and the row.

An engineer confirms a run's `well_entry.json` (extraction/well_entry.py) with the confirm CLI
(extraction/confirm.py), which writes a request.  The writer (extraction/confirm_writer.py; on Lambda,
`lambda_function.confirm_handler`) checks it against the run's stored entry, writes the document's design record
and appends one row to the well-entries table (extraction/well_entries_schema.py).  The app reads them back
(app/store.py).  This module is what the three share: the keys, the shapes and the two storage backends.  It
needs only the standard library.

Every key is under s3://roam-ds-assets/esp-design-extraction/ (`ROOT`), and none is built from an organisation or a
well id.  A local store has the same layout under its directory.

    confirmations/<confirmation_id>/request.json          the request: create-only, by the CLI
    confirmations/<confirmation_id>/result.json           its result: created `running` (create-only), overwritten once
    design-records/<document_sha256>/document.pdf         the document, copied create-only from intake/<run_id>.pdf
    design-records/<document_sha256>/versions/<confirmation_id>.json   the confirmation: create-only
    well-entries/environment=<env>/<confirmation_id>.json  the row: create-only, the commit point

The table only grows.  A well's current entry is its latest confirmed row per environment, organisation and well
(the latest `confirmed_at`; `confirmation_id` breaks a tie).  Nothing is overwritten or deleted.

The request, schema_version 1:
    confirmation_id      the run id's format, at the UTC moment of confirming
    run_id, document_sha256, organization_id, well_id
    environment          dev or prd
    entry_method         extracted, transcription or manual
    confirmed_by, confirmed_at   confirmed_at: UTC ISO 8601 with microseconds and no offset, as the run log writes
    values               every reviewed key of the stored entry but pump_sections, once each:
                         {key, final, status, [accept_anyway], history}
    pump_string          pump_sections, reviewed as the string: {final, sections, status, [accept_anyway], history},
                         `sections` holding each section's final SECTION_FIELDS
`final` is an envelope {value, unit, source[, as_of][, note]}, or null for an empty value.  A final status is settled
or manually corrected; `to check` is allowed through the contract so that the writer refuses it by name.
`accept_anyway` is the reason a value that blocks publish, or a string whose order check fails, is accepted
uncorrected.  `history` is a list of {status, by, at, reason}.

The result: schema_version, confirmation_id, status (running, written, refused or failed), reasons, row_key,
version_key, deadline_at (only while running), written_at.  A reader sees a running result past its deadline as
`stale` (`as_read`).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import well_entries_schema as schema
from .intake import RUN_ID, SHA256, intake_key

BUCKET = "roam-ds-assets"
ROOT = "esp-design-extraction/"
SCHEMA_VERSION = 1
ENVIRONMENTS = ("dev", "prd")
ENTRY_METHODS = ("extracted", "transcription")          # what the writer takes
MANUAL = "manual"                                        # in the contract; refused until manual entry exists
# The entry's statuses, as extraction/well_entry.py writes them (restated: that module loads the stage), and
# the one a confirmation adds.  A final status is settled or manually corrected.
SETTLED, TO_CHECK, BLOCKS, CORRECTED = "settled", "to check", "blocks publish", "manually corrected"
FINAL = (SETTLED, CORRECTED)
RUNNING, WRITTEN, REFUSED, FAILED, STALE = "running", "written", "refused", "failed", "stale"
PUMP_SECTIONS, DESIGN_DOCUMENT_REF, ENTRY = "pump_sections", "design_document_ref", "well_entry.json"
# A section's fields as the entry's string block holds them, less what the review derived (match, head curve,
# printed name, page, status, reasons).
SECTION_FIELDS = ("section_order", "pump_model", "stages", "section_role", "housings", "stages_per_housing_as_printed",
                  "top_md_ft", "bottom_md_ft")
# The run's files a confirmation does not review: the design record references them, never copies them.
NOT_REVIEWED = {"design_context": "esp_well_design_context.parquet", "curve_observations": "curve_observations.parquet",
                "pump_config": "pump_config.parquet", "report": "report.md", "well_entry": ENTRY}
CONFIRMED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}")
REQUEST_FIELDS = ("schema_version", "confirmation_id", "run_id", "document_sha256", "organization_id", "well_id",
                  "environment", "entry_method", "confirmed_by", "confirmed_at", "values", "pump_string")


# ---- keys ----

def request_key(confirmation_id: str) -> str:
    return f"{ROOT}confirmations/{confirmation_id}/request.json"


def result_key(confirmation_id: str) -> str:
    return f"{ROOT}confirmations/{confirmation_id}/result.json"


def row_prefix(environment: str) -> str:
    return f"{ROOT}well-entries/environment={environment}/"


def row_key(environment: str, confirmation_id: str) -> str:
    return f"{row_prefix(environment)}{confirmation_id}.json"


def record_prefix(document_sha256: str) -> str:
    return f"{ROOT}design-records/{document_sha256}/"


def document_key(document_sha256: str) -> str:
    return f"{record_prefix(document_sha256)}document.pdf"


def version_key(document_sha256: str, confirmation_id: str) -> str:
    return f"{record_prefix(document_sha256)}versions/{confirmation_id}.json"


def run_key(run_id: str, name: str) -> str:
    return f"{ROOT}runs/{run_id}/{name}"


def pdf_key(run_id: str) -> str:
    return intake_key(f"{ROOT}intake/", run_id)


ASSETS_PREFIX = f"{ROOT}assets/"


def s3_uri(key: str) -> str:
    return f"s3://{BUCKET}/{key}"


def request_id(key: str) -> str | None:
    """The confirmation id a request key names, or None when the key is not confirmations/<id>/request.json."""
    match = re.fullmatch(f"{re.escape(ROOT)}confirmations/({RUN_ID.pattern})/request\\.json", key)
    return match[1] if match else None


# ---- ids and times ----

def new_confirmation_id(at: datetime) -> str:
    """The run id's format (extraction.run.new_run_id, restated: that module loads pyarrow and botocore)."""
    return f"{at.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def timestamp(at: datetime) -> str:
    """confirmed_at: the run log's UTC ISO 8601 with no offset, with its six microsecond digits always written."""
    return at.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def utc(at: datetime) -> str:
    """deadline_at and written_at, in run.json's format."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest(doc: dict) -> tuple:
    """A row's or a version's place in time: its confirmed_at, then its confirmation id."""
    return datetime.fromisoformat(doc["confirmed_at"]), doc["confirmation_id"]


# ---- JSON ----

def dump(obj) -> bytes:
    """The request, the result and the version, as run.json is written."""
    return (json.dumps(obj, indent=1, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def row_bytes(row: dict) -> bytes:
    """One JSON object on one line, ending in a newline."""
    return (json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def load(body: bytes):
    """JSON as the contracts take it: no NaN or Infinity."""
    def refuse(name):
        raise ValueError(f"{name} is not JSON")
    return json.loads(body, parse_constant=refuse)


# ---- the request ----

def request_problems(request, confirmation_id: str) -> list[str]:
    """Where `request` breaks the contract; [] when it keeps it.  `confirmation_id` is the one its key names."""
    if not isinstance(request, dict):
        return ["the request is not a JSON object"]
    out = [f"unknown field {name}" for name in request if name not in REQUEST_FIELDS]
    out += [f"missing field {name}" for name in REQUEST_FIELDS if name not in request]
    if out:
        return out
    if request["schema_version"] != SCHEMA_VERSION:
        out.append(f"schema_version {request['schema_version']!r} is not {SCHEMA_VERSION}")
    if request["confirmation_id"] != confirmation_id:
        out.append(f"the key names confirmation {confirmation_id}, the body {request['confirmation_id']!r}")
    for name, pattern in (("run_id", RUN_ID), ("document_sha256", SHA256), ("confirmed_at", CONFIRMED_AT)):
        if not (isinstance(request[name], str) and pattern.fullmatch(request[name])):
            out.append(f"{name} {request[name]!r} is not in its format")
    if isinstance(request["confirmed_at"], str) and CONFIRMED_AT.fullmatch(request["confirmed_at"]):
        try:
            datetime.fromisoformat(request["confirmed_at"])
        except ValueError:
            out.append(f"confirmed_at {request['confirmed_at']!r} is not a time")
    for name in ("organization_id", "well_id", "environment", "entry_method", "confirmed_by"):
        if not (isinstance(request[name], str) and request[name]):
            out.append(f"{name} is not a non-empty string")
    values = request["values"]
    if not isinstance(values, list):
        out.append("values is not a list")
    else:
        keys = []
        for n, item in enumerate(values, 1):
            key = item.get("key") if isinstance(item, dict) else None
            if not (isinstance(key, str) and key):
                out.append(f"values[{n}] has no key")
                continue
            keys.append(key)
            out += _reviewed_problems(item, key, ("key", "final", "status", "accept_anyway", "history"))
        out += [f"{key} is given more than once" for key in sorted({k for k in keys if keys.count(k) > 1})]
        if PUMP_SECTIONS in keys:
            out.append(f"{PUMP_SECTIONS} is reviewed as pump_string, not in values")
    string = request["pump_string"]
    out += _reviewed_problems(string, "pump_string", ("final", "sections", "status", "accept_anyway", "history"))
    if isinstance(string, dict):
        sections = string.get("sections")
        if not isinstance(sections, list) or not all(isinstance(s, dict) and set(s) == set(SECTION_FIELDS)
                                                      for s in sections):
            out.append(f"pump_string.sections is not a list of sections with exactly {', '.join(SECTION_FIELDS)}")
    return out


def _reviewed_problems(item, where: str, fields: tuple) -> list[str]:
    if not isinstance(item, dict):
        return [f"{where} is not an object"]
    out = [f"{where}: unknown field {name}" for name in item if name not in fields]
    out += [f"{where}: missing field {name}" for name in fields if name not in item and name != "accept_anyway"]
    final = item.get("final")
    if final is not None:
        out += [f"{where}.final: {p}" for p in envelope_problems(final)]
    if item.get("status") not in (SETTLED, CORRECTED, TO_CHECK):
        out.append(f"{where}: status {item.get('status')!r} is not settled or manually corrected")
    if "accept_anyway" in item and not (isinstance(item["accept_anyway"], str) and item["accept_anyway"].strip()):
        out.append(f"{where}: accept_anyway is not a reason")
    if not (isinstance(item.get("history"), list) and all(map(_step, item.get("history") or []))):
        out.append(f"{where}: history is not a list of {{status, by, at, reason}}")
    return out


def envelope_problems(envelope) -> list[str]:
    """The envelope's shape (#1218's rules on its content run on the row's maps)."""
    if not isinstance(envelope, dict):
        return ["not an envelope"]
    out = [f"unknown field {name}" for name in envelope if name not in schema.ENVELOPE]
    out += [f"{name} missing" for name in ("value", "unit", "source") if envelope.get(name) is None]
    out += [f"{name} is not a string" for name in schema.ENVELOPE[1:]
            if envelope.get(name) is not None and not isinstance(envelope[name], str)]
    return out


def _step(step) -> bool:
    return (isinstance(step, dict) and set(step) == {"status", "by", "at", "reason"}
            and all(isinstance(step[k], str) and step[k] for k in ("status", "by", "at"))
            and (step["reason"] is None or isinstance(step["reason"], str)))


def step(status: str, by: str, at: str, reason: str | None) -> dict:
    return {"status": status, "by": by, "at": at, "reason": reason}


# ---- the result ----

def result(confirmation_id: str, status: str, *, at: datetime, reasons=(), row_key=None, version_key=None,
           deadline_at: str | None = None) -> dict:
    out = {"schema_version": SCHEMA_VERSION, "confirmation_id": confirmation_id, "status": status,
           "reasons": list(reasons), "row_key": row_key, "version_key": version_key}
    if deadline_at is not None:
        out["deadline_at"] = deadline_at
    return {**out, "written_at": utc(at)}


def as_read(doc: dict, now: datetime | None = None) -> dict:
    """A result as a reader should see it: `running` past its deadline_at is `stale`."""
    now_ = utc(now or datetime.now(timezone.utc))
    if doc.get("status") == RUNNING and isinstance(doc.get("deadline_at"), str) and now_ > doc["deadline_at"]:
        return {**doc, "status": STALE}
    return doc


def read_result(objects, confirmation_id: str, now: datetime | None = None) -> dict | None:
    """The result as a reader should see it, or None while there is none.  Whether it is there comes from the
    listing, never from a failed read."""
    key = result_key(confirmation_id)
    if key not in objects.keys(key.rsplit("/", 1)[0] + "/"):
        return None
    return as_read(load(objects.get(key)), now)


# ---- the row ----

def build_row(request: dict, *, design_date: str | None, version: str) -> dict:
    """The row: each final value in its map, empty ones left out, and design_document_ref pointing at the
    version.  Raises KeyError on a key the table has no field for."""
    finals = [(v["key"], v["final"]) for v in request["values"]] + [(PUMP_SECTIONS, request["pump_string"]["final"])]
    finals.append((DESIGN_DOCUMENT_REF, {"value": s3_uri(version), "unit": "none", "source": "design_document"}))
    maps = {field: {} for field in schema.PARAMETERS}
    for key, final in finals:
        if final is not None:
            maps[schema.MAP_OF[key]][key] = final
    row = {"organization_id": request["organization_id"], "well_id": request["well_id"],
           **{field: {key: maps[field][key] for key, _ in keys if key in maps[field]}
              for field, keys in schema.PARAMETERS.items()},
           "run_id": request["run_id"], "document_sha256": request["document_sha256"], "design_date": design_date,
           "entry_method": request["entry_method"], "confirmation_id": request["confirmation_id"],
           "confirmed_at": request["confirmed_at"], "confirmed_by": request["confirmed_by"],
           "environment": request["environment"]}
    return {name: row[name] for name, _ in schema.COLUMNS + schema.PARTITION_KEYS}


# ---- storage: a local store or the bucket, as objects under ROOT ----

class Exists(Exception):
    """A create-only write found its key already there, or being created."""


class LocalObjects:
    """A local store as objects: the key `ROOT<path>` is the file `<root>/<path>`."""

    def __init__(self, root):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        parts = key[len(ROOT):].split("/") if key.startswith(ROOT) else []
        if not parts or any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"not a key under {ROOT}: {key!r}")
        return self.root.joinpath(*parts)

    def get(self, key: str) -> bytes:
        """FileNotFoundError when the key is not there."""
        return self.path(key).read_bytes()

    def put(self, key: str, body: bytes, *, create: bool = False) -> None:
        """Written whole under a temporary name, then renamed into place; with `create`, linked into place,
        which fails when the name exists (Exists)."""
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        tmp.write_bytes(body)
        try:
            if create:
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    raise Exists(key) from None
            else:
                os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def keys(self, prefix: str) -> list[str]:
        """Every key under `prefix` (a folder: it ends in `/`), sorted."""
        if not prefix.endswith("/"):
            raise ValueError(f"not a folder: {prefix!r}")
        top = self.path(prefix + "_").parent
        if not top.is_dir():
            return []
        return sorted(ROOT + p.relative_to(self.root).as_posix() for p in top.rglob("*")
                      if p.is_file() and not p.name.startswith("."))


def _response(err) -> tuple:
    response = getattr(err, "response", None) or {}
    return response.get("ResponseMetadata", {}).get("HTTPStatusCode"), response.get("Error", {}).get("Code")


def forbidden(err) -> bool:
    """Whether S3 answered 403: what it answers, instead of 404, for a key that is not there when the caller cannot
    list its prefix."""
    return _response(err)[0] == 403


class S3Objects:
    """The bucket as objects.  A missing key reads as missing only when S3 answers 404, which needs s3:ListBucket on
    its prefix; without it S3 answers 403, which is raised like any other error (the writer reads it as not there)."""

    def __init__(self, client, bucket: str = BUCKET):
        self.client, self.bucket = client, bucket

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as err:
            status, code = _response(err)
            if status == 404 or code in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from err
            raise

    def put(self, key: str, body: bytes, *, create: bool = False) -> None:
        """With `create`, IfNoneMatch="*": a 412 (there) or a 409 (another create racing it) raises Exists."""
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body, **({"IfNoneMatch": "*"} if create else {}))
        except Exception as err:
            status, code = _response(err)
            if create and (status == 412 or (status == 409 and code == "ConditionalRequestConflict")):
                raise Exists(key) from err
            raise

    def keys(self, prefix: str) -> list[str]:
        out, kwargs = [], {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            page = self.client.list_objects_v2(**kwargs)
            out += [obj["Key"] for obj in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return sorted(out)
            kwargs["ContinuationToken"] = page["NextContinuationToken"]
