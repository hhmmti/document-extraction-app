"""The confirmation writer: a request in; the document's design record and one well-entries row out.

    confirm(objects, key, deadline=...)   one request, on S3 (lambda_function.confirm_handler) or on a local store (the
                                          confirm CLI's local mode): the same code over extraction/confirmation.py's
                                          two backends, in the same layout

1. The result is created `running`, create-only, with the deadline.  Already there (412) or being created (409): the
   event is a duplicate, and nothing else is read or written.
2. The request is checked (`check`).  Each refusal names its reason; a refused request gets only its result.  The
   writer cannot list, so S3 answers 403, not 404, for a key that is not there: a 403 on the request, on
   runs/<run_id>/... or on intake/<run_id>.pdf reads as not there, and its reason names the key and says so.  A 403
   on assets/ is a failure (ASSET_INVALID).
3. An accepted one is written in this order: the document (design-records/<sha>/document.pdf, create-only; already
   there is fine), the version (design-records/<sha>/versions/<id>.json, create-only), the row
   (well-entries/environment=<env>/<id>.json, create-only: the commit point), then the result, overwritten once.
4. Anything unexpected before the row writes a failed result if it can.  Once the row is written the confirmation
   stands; a result that cannot be written then stays `running`, and reads as stale past its deadline.

The outcome returned names the request's organisation, well and run as far as they are known, the status, the
reasons and keys, and, on a failure, the error with its trace: the Lambda's run-log row is made from it.
"""

from __future__ import annotations

import hashlib
import traceback
from datetime import datetime, timezone

from . import confirmation as C
from . import well_entries_schema as schema
from .catalogue import EXACT, CatalogueNames
from .errors import ExtractionError
from .stage import values as V
from .stage.reports_reader import read_report
from .validate.validate import catalogue as read_catalogue
from .well_entry import final_string_checks, parameter_problems, section_value


def now() -> datetime:
    return datetime.now(timezone.utc)


def confirm(objects, key: str, *, deadline: datetime, catalogue=None, started=None) -> dict:
    """Check one request and write it, or refuse it.  `catalogue()` gives the catalogue snapshot (model -> has a
    head curve), hash-checked; by default the asset ESP_EXTRACTION_ASSETS_DIR holds.  `started(request)` is called
    once the request is read and names its organisation and well."""
    cid = C.request_id(key)
    outcome = {"confirmation_id": cid, "request_key": key, "status": None, "reasons": [], "run_id": None,
               "organization_id": None, "well_id": None, "row_key": None, "version_key": None, "error": None}
    if cid is None:
        return {**outcome, "status": "ignored", "reasons": [f"{key} is not {C.ROOT}confirmations/<id>/request.json"]}
    try:
        objects.put(C.result_key(cid), C.dump(C.result(cid, C.RUNNING, at=now(), deadline_at=C.utc(deadline))),
                    create=True)
    except C.Exists:
        return {**outcome, "status": "duplicate"}
    except Exception as err:
        return {**outcome, "status": C.FAILED, "error": error(err, "create")}

    stage, committed = "request", False
    try:
        try:
            request, why = read_json(objects, key)
            reasons = None if why is None else [f"contract: the request is not there{why}"]
        except ValueError as err:
            request, reasons = None, [f"contract: the request is not JSON ({err})"]
        if reasons is None:
            if isinstance(request, dict):
                outcome.update({k: request[k] for k in ("organization_id", "well_id", "run_id")
                                if isinstance(request.get(k), str) and request[k]})
                if outcome["organization_id"] and outcome["well_id"] and started:
                    started(request)
            stage = "check"
            reasons, ready = check(objects, request, cid, catalogue or read_catalogue)
        if reasons:
            stage = "result"
            objects.put(C.result_key(cid), C.dump(C.result(cid, C.REFUSED, at=now(), reasons=reasons)))
            return {**outcome, "status": C.REFUSED, "reasons": reasons}

        keys = {"row_key": ready["row_key"], "version_key": ready["version_key"]}
        stage = "document"
        try:
            objects.put(C.document_key(request["document_sha256"]), ready["pdf"], create=True)
        except C.Exists:
            pass                        # already there: the same bytes, named by their SHA-256
        stage = "version"
        objects.put(keys["version_key"], C.dump(ready["version"]), create=True)
        stage = "row"
        objects.put(keys["row_key"], C.row_bytes(ready["row"]), create=True)
        committed = True
        stage = "result"
        objects.put(C.result_key(cid), C.dump(C.result(cid, C.WRITTEN, at=now(), **keys)))
        return {**outcome, "status": C.WRITTEN, **keys}
    except Exception as err:
        failure = error(err, stage)
        if committed:                   # the row stands; the result stays running and reads as stale
            return {**outcome, "status": C.WRITTEN, **keys, "error": failure}
        try:
            objects.put(C.result_key(cid), C.dump(C.result(
                cid, C.FAILED, at=now(), reasons=[f"{failure['code']} at {stage}: {failure['message']}"])))
        except Exception:               # noqa: BLE001 - the running result stays; past its deadline it reads as stale
            pass
        return {**outcome, "status": C.FAILED, "error": failure}


def error(err: Exception, stage: str) -> dict:
    code, message = ((err.code, err.message) if isinstance(err, ExtractionError)
                     else ("INTERNAL_ERROR", f"{type(err).__name__}: {err}"))
    return {"code": code, "stage": stage, "message": message, "trace": traceback.format_exc()}


def read(objects, key: str) -> tuple[bytes | None, str | None]:
    """The object and None; when it is not there, None and how that was known: "" for a 404 and, for a 403, why that
    means not there too.  The readers (app/store.py) take only a 404 for not there."""
    try:
        return objects.get(key), None
    except FileNotFoundError:
        return None, ""
    except Exception as err:
        if not C.forbidden(err):
            raise
        return None, f" (S3 answered 403 for {key}: the writer cannot list, so S3 answers 403 for a key that is not there)"


def read_json(objects, key: str):
    body, why = read(objects, key)
    return (None if body is None else C.load(body)), why


def check(objects, request, cid: str, catalogue) -> tuple[list[str], dict]:
    """Every reason to refuse the request, and, when there is none, what the writes need."""
    problems = C.request_problems(request, cid)
    if problems:
        return [f"contract: {p}" for p in problems], {}
    reasons = []
    method, environment = request["entry_method"], request["environment"]
    if method == C.MANUAL:
        reasons.append("manual entry is not supported yet")
    elif method not in C.ENTRY_METHODS:
        reasons.append(f"entry_method {method!r} is not extracted or transcription")
    if environment not in C.ENVIRONMENTS:
        reasons.append(f"environment {environment!r} is not dev or prd")

    run_id, sha = request["run_id"], request["document_sha256"]
    run, why = read_json(objects, C.run_key(run_id, "run.json"))
    if run is None:
        reasons.append(f"runs/{run_id}/run.json is not there{why}")
    else:
        recorded = {"organization_id": run.get("organization_id"), "well_id": run.get("well_id"),
                    "document_sha256": ((run.get("source") or {}).get("pdf") or {}).get("sha256")}
        reasons += [f"{name} is {request[name]!r}; the run's is {value!r}"
                    for name, value in recorded.items() if value != request[name]]
    entry, why = read_json(objects, C.run_key(run_id, C.ENTRY))
    if entry is None:
        reasons.append(f"runs/{run_id}/{C.ENTRY} is not there{why or ': the run stored no entry'}")
    pdf, why = read(objects, C.pdf_key(run_id))
    if pdf is None:
        reasons.append(f"intake/{run_id}.pdf is not there{why}")
    elif hashlib.sha256(pdf).hexdigest() != sha:
        reasons.append(f"intake/{run_id}.pdf is not the document: its SHA-256 is {hashlib.sha256(pdf).hexdigest()}")
    if entry is None:
        return reasons, {}

    # Against the stored entry: pump_sections is reviewed as the string.
    stored = {v["key"]: v for v in entry["review"]["values"]}
    given = {v["key"]: v for v in request["values"]} | {C.PUMP_SECTIONS: request["pump_string"]}
    missing, extra = [k for k in stored if k not in given], [k for k in given if k not in stored]
    if missing:
        reasons.append(f"values missing: {', '.join(missing)}")
    if extra:
        reasons.append(f"values not in the entry: {', '.join(extra)}")
    keys = [k for k in stored if k in given]
    left = [k for k in keys if given[k]["status"] == C.TO_CHECK]
    if left:
        reasons.append(f"left to check: {', '.join(left)}")
    proposed_string = entry["review"]["pump_string"]

    def changed(k) -> bool:
        if given[k]["final"] != entry[stored[k]["map"]].get(k):
            return True
        return k == C.PUMP_SECTIONS and given[k]["sections"] != [
            {name: s.get(name) for name in C.SECTION_FIELDS} for s in proposed_string["sections"]]
    unmarked = [k for k in keys if given[k]["status"] != C.CORRECTED and changed(k)]
    if unmarked:
        reasons.append(f"changed from the proposal but not marked manually corrected: {', '.join(unmarked)}")

    string, names = request["pump_string"], CatalogueNames(catalogue())
    checks = final_string_checks(string["sections"], names)
    failed = {c["check"]: c for c in checks if c["result"] == "fail"}
    for k in keys:
        corrected_and_passes = given[k]["status"] == C.CORRECTED and not (k == C.PUMP_SECTIONS and "V-08" in failed)
        if stored[k]["status"] == C.BLOCKS and not (corrected_and_passes or given[k].get("accept_anyway")):
            reasons.append(f"{k} blocks publish: neither corrected so that its check passes, nor accepted anyway")
    if "V-08" in failed and not string.get("accept_anyway"):
        reasons.append(f"pump string: the order check fails ({failed['V-08']['why']}), with no accept-anyway reason")
    other = [f"{c['check']}: {c['why']}" for name, c in failed.items() if name != "V-08"]
    if other and string["status"] not in C.FINAL:
        reasons.append(f"pump string: {'; '.join(other)}; and it is neither settled nor manually corrected")
    final = string["final"]
    if final is not None and final["value"] != [section_value(s) for s in string["sections"]]:
        reasons.append("pump string: its final value is not its final sections")
    if (given.get(C.DESIGN_DOCUMENT_REF) or {}).get("final") is not None:
        reasons.append("design_document_ref is the writer's to set: leave it empty")
    models = [s["pump_model"] for s in string["sections"]]
    models += [s.get("pump_model") for s in final["value"] if isinstance(s, dict)] if (
        final is not None and isinstance(final["value"], list)) else []
    for model in dict.fromkeys(m for m in models if m is not None):
        match = names.lookup(model)
        if match.how != EXACT:
            reasons.append(f"pump_model {model!r} is not a catalogue spelling"
                           + (f": the catalogue spells it {match.name!r}" if match.name else ""))
    if missing or extra:                # the row needs every key in place
        return reasons, {}

    keys_ = {"row_key": C.row_key(environment, cid), "version_key": C.version_key(sha, cid)}
    row = C.build_row(request, design_date=printed_design_date(objects, run_id, reasons), version=keys_["version_key"])
    for field in schema.PARAMETERS:
        reasons += [f"#1218: {p}" for p in parameter_problems(row[field], field)]
    reasons += [f"row: {p}" for p in schema.problems(row)]
    if reasons:
        return list(dict.fromkeys(reasons)), {}
    return [], {**keys_, "pdf": pdf, "row": row,
                "version": version_record(request, run, entry, row, checks, keys_)}


def printed_design_date(objects, run_id: str, reasons: list) -> str | None:
    """The design date the report printed, its frontmatter's effective_from, as staging reads it; None if none.  A
    report that is not there gives none; one S3 answered 403 for is a reason to refuse, since the date may be there."""
    body, why = read(objects, C.run_key(run_id, "report.md"))
    if why:
        reasons.append(f"runs/{run_id}/report.md is not there{why}")
    if body is None:
        return None
    return V.parse_date(read_report(body.decode("utf-8", errors="replace")).frontmatter.get("effective_from"))


def version_record(request, run, entry, row, checks, keys) -> dict:
    """The design record's version: what was confirmed against what was proposed, the row, and where the rest is."""
    given = {v["key"]: v for v in request["values"]} | {C.PUMP_SECTIONS: request["pump_string"]}
    values = []
    for v in entry["review"]["values"]:
        g = given[v["key"]]
        values.append({"key": v["key"], "map": v["map"], "proposed": entry[v["map"]].get(v["key"]), "final": g["final"],
                       "starting_status": v["status"], "status": g["status"],
                       **({"accept_anyway": g["accept_anyway"]} if g.get("accept_anyway") else {}),
                       "reasons": v["reasons"], "printed": v.get("printed"), "page": v.get("page"),
                       **({"page_basis": v["page_basis"]} if v.get("page_basis") else {}), "history": g["history"]})
    return {
        "schema_version": C.SCHEMA_VERSION, "confirmation_id": request["confirmation_id"],
        "request_key": C.request_key(request["confirmation_id"]),
        "labels": {"organization_id": run["organization_id"], "well_id": run["well_id"],
                   "source_name": (run.get("source") or {}).get("source_name"),
                   "document_sha256": request["document_sha256"]},
        "run": {"run_id": request["run_id"], "family": run.get("family"), "route": run.get("route"),
                "ended_at": run.get("ended_at")},
        "environment": request["environment"], "entry_method": request["entry_method"],
        "confirmed_by": request["confirmed_by"], "confirmed_at": request["confirmed_at"],
        "design_date": row["design_date"],
        "document_key": C.document_key(request["document_sha256"]),
        "values": values,
        "pump_string": {"proposed": entry["review"]["pump_string"], "final_sections": request["pump_string"]["sections"],
                        "checks_on_final_sections": checks},
        "row_key": keys["row_key"], "row": row,
        "not_reviewed": {name: C.run_key(request["run_id"], file) for name, file in C.NOT_REVIEWED.items()},
    }
