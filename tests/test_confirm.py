"""Confirming an entry, offline: the contracts, the writer and its Lambda entry point, the confirm CLI and the
readers.  The stored run is synthetic: a well_entry.json in the builder's shape, its run.json, report and PDF.  The
catalogue is a stand-in of three names, except in the tests that need ESP_EXTRACTION_ASSETS_DIR.  S3 and DynamoDB
are test_lambda's and test_app_s3's fakes.  No AWS.

Two optional groups:
  * ESP_WELL_ENTRIES_STORE, a store holding confirmed rows: every row conforms to the schema, and (with the next)
    gets the same verdict from this repo's map rules as from #1218's validator;
  * ESP_MONOREPO_DIR, the monorepo clone: #1218's validator is read from it at 6831cbd6 with `git show` and run on
    those maps and on the negative cases.  Nothing of it is copied here.
"""

import hashlib
import json
import os
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

import app.store as S
import extraction.confirm as CLI
import extraction.confirm_writer as W
import extraction.confirmation as C
import extraction.well_entries_schema as schema
import lambda_function as L
import test_app_s3 as TA
import test_lambda as TL
from extraction.assets_dir import ASSETS_ENV
from extraction.well_entry import (BLOCKS, KEYS, SETTLED, TO_CHECK, parameter_problems, section_incomplete,
                                   section_value)
from test_validate import report_text

RUN_ID = "20261028T120000Z-0123abcd"
CID = "20261028T130000Z-0000feed"
PDF = b"%PDF-1.4\n% a design document\n"
SHA = hashlib.sha256(PDF).hexdigest()
ORG, WELL = "org-1", "Well  #1"                 # kept exactly as given: two spaces, a hash
AT = "2026-10-28T13:00:00.000001"
DEADLINE = datetime(2099, 1, 1, tzinfo=timezone.utc)
CATALOGUE = {"ELS1750": True, "ELS3000": True, "400 DAL1200": False}
REPORT = report_text("F4", {"effective_from": "2025-06-01"})
FUNCTION = "esp-design-extraction-writer"       # the writer's function name, as the stack names it
needs_assets = pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"the catalogue asset; set {ASSETS_ENV}")


# ---- a stored run ----

def section(order, model, stages, role="primary", **extra) -> dict:
    """A section of the entry's string block."""
    return {"section_order": order, "pump_model": model, "match": "exact", "head_curve": True,
            "pump_model_as_printed": model, "pump_model_d31": model, "stages": stages, "section_role": role,
            "housings": 1, "stages_per_housing_as_printed": str(stages), "top_md_ft": None, "bottom_md_ft": None,
            "page": "p2", "status": SETTLED, "reasons": [], "review_reasons": [], **extra}


WRITTEN = {"setting_depth_md_ft": ({"value": 9100.0, "unit": "ft", "source": "design_document", "note": "intake"}, SETTLED, []),
           "oil_sg": ({"value": 0.8251, "unit": "sg", "source": "design_document",
                       "note": "from API 40: 141.5 / (131.5 + API)"}, TO_CHECK, ["varies_by_scenario"]),
           "bubble_point_psia": ({"value": 1514.7, "unit": "psia", "source": "design_document",
                                  "note": 'printed "1500 psig"; +14.7'}, SETTLED, [])}


def entry(sections=None, string_status=SETTLED, string_reasons=()) -> dict:
    """A well_entry.json in the builder's shape: all 18 keys, three scalars written, and the string."""
    sections = [section(1, "ELS1750", 490)] if sections is None else sections
    whole = sections and all(s["pump_model"] and not section_incomplete(s["section_order"], s["stages"], i)
                             for i, s in enumerate(sections, 1))
    maps, values = {"pump_parameters": {}, "wellbore_parameters": {}}, []
    for key, (map_, unit) in KEYS.items():
        if key == "pump_sections":
            value = [section_value(s) for s in sections] if whole else None
            if value:
                maps[map_][key] = {"value": value, "unit": unit, "source": "design_document",
                                   "note": "section order as printed, 1 = deepest"}
            values.append({"key": key, "map": map_, "status": string_status, "reasons": list(string_reasons),
                           "value": value, "unit": unit, "written": value is not None})
        elif key in WRITTEN:
            envelope, status, reasons = WRITTEN[key]
            maps[map_][key] = envelope
            values.append({"key": key, "map": map_, "status": status, "reasons": reasons, "value": envelope["value"],
                           "unit": unit, "written": True, "printed": str(envelope["value"]), "column": key,
                           "scenario": "1", "page": "p3", "page_basis": "cited page", "note": envelope["note"]})
        else:
            values.append({"key": key, "map": map_, "status": SETTLED, "reasons": [], "value": None, "unit": unit,
                           "written": False, "printed": None, "column": None, "scenario": None, "page": None,
                           "page_basis": None, "note": None})
    return {"organization_id": ORG, "well_id": WELL, **maps,
            "review": {"family": "F4", "route": "template", "summary": {}, "values": values,
                       "pump_string": {"status": string_status, "reasons": list(string_reasons), "scenario": "1",
                                       "checks": [], "sections": sections, "head_sum": []}}}


BLOCKED = dict(sections=[section(1, "ELS1750", 490), section(3, "ELS3000", 28)], string_status=BLOCKS,
               string_reasons=["V-08: scenario 1: section_order is not a contiguous 1..2"])


def run_record(**changes) -> dict:
    return {"run_id": RUN_ID, "status": "succeeded", "organization_id": ORG, "well_id": WELL, "family": "F4",
            "route": "template", "started_at": "2026-10-28T12:00:00Z", "ended_at": "2026-10-28T12:01:00Z",
            "source": {"pdf": {"path": f"s3://{C.BUCKET}/{C.pdf_key(RUN_ID)}", "sha256": SHA}, "report": None,
                       "source_name": "design.pdf"}, **changes}


def put_run(objects, the_entry=None, *, run=None, pdf=PDF, with_entry=True):
    objects.put(C.run_key(RUN_ID, "run.json"), C.dump(run or run_record()))
    if with_entry:
        objects.put(C.run_key(RUN_ID, C.ENTRY), C.dump(the_entry or entry()))
    if pdf is not None:
        objects.put(C.pdf_key(RUN_ID), pdf)
    objects.put(C.run_key(RUN_ID, "report.md"), REPORT.encode())


def request(the_entry=None, *, as_proposed=True, finals=None, sections=None, accept_anyway=None, leave=(), **changes):
    """A request as the CLI builds it, with `changes` on top."""
    q = CLI.build_request(the_entry or entry(), run_record(), cid=CID, run_id=RUN_ID, document=SHA, confirmed_at=AT,
                          confirmed_by="tester", environment="prd", as_proposed=as_proposed, finals=finals or {},
                          sections=sections, accept_anyway=accept_anyway, leave=set(leave))
    return {**q, **changes}


def item(q, key) -> dict:
    return q["pump_string"] if key == "pump_sections" else next(v for v in q["values"] if v["key"] == key)


def submit(objects, q, cid=CID, catalogue=CATALOGUE) -> dict:
    objects.put(C.request_key(cid), q if isinstance(q, bytes) else C.dump(q), create=True)
    return W.confirm(objects, C.request_key(cid), deadline=DEADLINE, catalogue=lambda: catalogue)


@pytest.fixture
def store(tmp_path):
    objects = C.LocalObjects(tmp_path / "store")
    put_run(objects)
    return objects


def written_keys(objects) -> list[str]:
    return [k for k in objects.keys(C.ROOT) if not k.startswith((f"{C.ROOT}runs/", f"{C.ROOT}intake/"))]


# ---- the contracts ----

def test_ids_and_times_are_in_the_run_formats():
    at = datetime(2026, 10, 28, 13, 0, 0, tzinfo=timezone.utc)
    assert C.timestamp(at) == "2026-10-28T13:00:00.000000"          # microseconds written even when zero
    assert C.timestamp(at.replace(microsecond=5)) == L.standard_timestamp(at.replace(microsecond=5))
    cid = C.new_confirmation_id(at)
    assert C.RUN_ID.fullmatch(cid) and cid.startswith("20261028T130000Z-")
    assert C.request_id(C.request_key(cid)) == cid
    assert C.request_id(f"{C.ROOT}confirmations/{cid}/result.json") is None


def test_the_restated_statuses_are_the_builders():
    import extraction.well_entry as E
    assert (C.SETTLED, C.TO_CHECK, C.BLOCKS, C.ENTRY) == (E.SETTLED, E.TO_CHECK, E.BLOCKS, E.ENTRY)


def test_no_key_is_built_from_an_organisation_or_a_well():
    keys = [C.request_key(CID), C.result_key(CID), C.row_key("prd", CID), C.version_key(SHA, CID), C.document_key(SHA)]
    assert all(k.startswith("esp-design-extraction/") and ORG not in k and WELL not in k for k in keys)


def test_local_objects_create_only_and_stay_in_their_root(tmp_path):
    objects = C.LocalObjects(tmp_path)
    key = f"{C.ROOT}confirmations/{CID}/result.json"
    objects.put(key, b"one", create=True)
    with pytest.raises(C.Exists):
        objects.put(key, b"two", create=True)
    objects.put(key, b"three")
    assert objects.get(key) == b"three" and objects.keys(f"{C.ROOT}confirmations/") == [key]
    assert not list(tmp_path.rglob(".*"))                           # no temporary file left behind
    for bad in ("elsewhere/x.json", f"{C.ROOT}../x.json", f"{C.ROOT}a//b"):
        with pytest.raises(ValueError):
            objects.path(bad)


def test_the_row_is_one_line_in_the_schemas_column_order(store):
    assert submit(store, request())["status"] == C.WRITTEN
    [key] = store.keys(C.row_prefix("prd"))
    body = store.get(key)
    assert body.endswith(b"\n") and body.count(b"\n") == 1
    row = json.loads(body)
    assert list(row) == [name for name, _ in schema.COLUMNS + schema.PARTITION_KEYS] and schema.problems(row) == []
    assert (row["organization_id"], row["well_id"], row["design_date"], row["environment"]) == (ORG, WELL, "2025-06-01", "prd")


# ---- the writer: each refusal names its reason, and only the result is written ----

def drop(q, field):
    return {k: v for k, v in q.items() if k != field}


def set_item(q, key, **fields):
    item(q, key).update(fields)
    return q


def unordered(objects):
    """A string corrected to sections out of order, with no accept-anyway reason."""
    return request(finals={"pump_sections": {"value": [{"section_order": 1, "pump_model": "ELS1750", "stages": 490},
                                                       {"section_order": 3, "pump_model": "ELS3000", "stages": 28}],
                                             "unit": "none", "source": "design_document"}},
                   sections=[{**{f: None for f in C.SECTION_FIELDS}, "section_order": 1, "pump_model": "ELS1750", "stages": 490},
                             {**{f: None for f in C.SECTION_FIELDS}, "section_order": 3, "pump_model": "ELS3000", "stages": 28}])


def unknown_model_left_to_check(objects):
    """As the builder leaves a model the catalogue lacks: no catalogue name, so no value."""
    lacking = entry([section(1, None, 100, match="none", pump_model_d31="ELS1234")], TO_CHECK,
                    ["models: ELS1234 not in the catalogue"])
    put_run(objects, lacking)
    return request(lacking, leave=["pump_sections"])


def blocked_settled_without_reason(objects):
    put_run(objects, entry(**BLOCKED))
    q = request(entry(**BLOCKED), accept_anyway="kept")
    item(q, "pump_sections").pop("accept_anyway")
    return q


def case_mismatched_model(objects):
    sections = [{**{f: None for f in C.SECTION_FIELDS}, "section_order": 1, "pump_model": "els1750", "stages": 490}]
    return request(finals={"pump_sections": {"value": [section_value(s) for s in sections], "unit": "none",
                                             "source": "design_document"}}, sections=sections)


REFUSALS = [
    ("not JSON", lambda o: b"{", "contract: the request is not JSON"),
    ("a field missing", lambda o: drop(request(), "confirmed_by"), "contract: missing field confirmed_by"),
    ("the key's id is not the body's", lambda o: request(confirmation_id="20261028T130000Z-00000000"),
     "contract: the key names confirmation 20261028T130000Z-0000feed, the body '20261028T130000Z-00000000'"),
    ("a final status outside the contract", lambda o: set_item(request(), "oil_sg", status="blocks publish"),
     "contract: oil_sg: status 'blocks publish' is not settled or manually corrected"),
    ("an envelope without its unit", lambda o: set_item(request(), "oil_sg", final={"value": 1, "source": "design_document"}),
     "contract: oil_sg.final: unit missing"),
    ("confirmed_at without microseconds", lambda o: request(confirmed_at="2026-10-28T13:00:00"),
     "contract: confirmed_at '2026-10-28T13:00:00' is not in its format"),
    ("no run", lambda o: request(run_id="20261028T120000Z-99999999"), "runs/20261028T120000Z-99999999/run.json is not there"),
    ("another organisation", lambda o: request(organization_id="org-2"), "organization_id is 'org-2'; the run's is 'org-1'"),
    ("the well normalised", lambda o: request(well_id="Well #1"), "well_id is 'Well #1'; the run's is 'Well  #1'"),
    ("another document", lambda o: request(document_sha256="0" * 64), f"document_sha256 is '{'0' * 64}'; the run's is '{SHA}'"),
    ("no entry", lambda o: (o.path(C.run_key(RUN_ID, C.ENTRY)).unlink(), request())[-1],
     f"runs/{RUN_ID}/well_entry.json is not there: the run stored no entry"),
    ("no intake PDF", lambda o: (o.path(C.pdf_key(RUN_ID)).unlink(), request())[-1], f"intake/{RUN_ID}.pdf is not there"),
    ("another intake PDF", lambda o: (o.put(C.pdf_key(RUN_ID), b"%PDF other"), request())[-1],
     f"intake/{RUN_ID}.pdf is not the document"),
    ("manual", lambda o: request(entry_method="manual"), "manual entry is not supported yet"),
    ("another entry method", lambda o: request(entry_method="typed"), "entry_method 'typed' is not extracted or transcription"),
    ("another environment", lambda o: request(environment="qa"), "environment 'qa' is not dev or prd"),
    ("a value missing", lambda o: {**request(), "values": [v for v in request()["values"] if v["key"] != "oil_sg"]},
     "values missing: oil_sg"),
    ("a value not in the entry", lambda o: {**request(), "values": request()["values"] + [
        {**item(request(), "oil_sg"), "key": "pump_id"}]}, "values not in the entry: pump_id"),
    ("left to check", lambda o: request(as_proposed=False), "left to check: oil_sg"),
    ("changed but not marked", lambda o: set_item(request(), "oil_sg", final={**WRITTEN["oil_sg"][0], "value": 0.9}),
     "changed from the proposal but not marked manually corrected: oil_sg"),
    ("blocks publish, neither corrected nor accepted", blocked_settled_without_reason,
     "pump_sections blocks publish: neither corrected so that its check passes, nor accepted anyway"),
    ("an order check that fails, no reason", unordered,
     "pump string: the order check fails (scenario 1: section_order is not a contiguous 1..2), with no accept-anyway reason"),
    ("another check fails, left to check", unknown_model_left_to_check,
     "pump string: models: position 1: no model; and it is neither settled nor manually corrected"),
    ("a final value that is not its sections",
     lambda o: set_item(request(), "pump_sections", status=C.CORRECTED, final={
         "value": [{"section_order": 1, "pump_model": "ELS1750", "stages": 1}], "unit": "none", "source": "design_document"}),
     "pump string: its final value is not its final sections"),
    ("design_document_ref set", lambda o: set_item(request(), "design_document_ref", status=C.CORRECTED, final={
        "value": "s3://elsewhere", "unit": "none", "source": "design_document"}),
     "design_document_ref is the writer's to set: leave it empty"),
    ("#1218: a unit its key does not declare", lambda o: request(finals={"oil_sg": {"value": 0.85, "unit": "psia",
                                                                                    "source": "design_document"}}),
     "#1218: wellbore_parameters.oil_sg: the key name declares 'sg' but the value says 'psia'"),
    ("a value the table cannot type", lambda o: request(finals={"motor_type": {"value": True, "unit": "none",
                                                                               "source": "design_document"}}),
     "row: pump_parameters.motor_type.value: not string"),
    ("a model that is not the catalogue's spelling", case_mismatched_model,
     "pump_model 'els1750' is not a catalogue spelling: the catalogue spells it 'ELS1750'"),
]


@pytest.mark.parametrize("make,reason", [r[1:] for r in REFUSALS], ids=[r[0] for r in REFUSALS])
def test_each_refusal_names_its_reason_and_writes_only_the_result(store, make, reason):
    out = submit(store, make(store))
    result = C.load(store.get(C.result_key(CID)))
    assert out["status"] == result["status"] == C.REFUSED
    assert any(r.startswith(reason) for r in result["reasons"]), result["reasons"]
    assert (result["row_key"], result["version_key"], "deadline_at" in result) == (None, None, False)
    assert written_keys(store) == [C.request_key(CID), C.result_key(CID)]


def test_a_request_gone_or_not_an_object_is_refused(store):
    out = W.confirm(store, C.request_key(CID), deadline=DEADLINE, catalogue=lambda: CATALOGUE)     # never written
    assert (out["status"], out["reasons"]) == (C.REFUSED, ["contract: the request is not there"])
    other = "20261028T140000Z-0000beef"
    assert submit(store, b"[]", cid=other)["reasons"] == ["contract: the request is not a JSON object"]


def test_a_refusal_lists_every_reason_at_once(store):
    out = submit(store, request(as_proposed=False, environment="qa", organization_id="org-2"))
    assert [r.split(" ")[0] for r in out["reasons"]] == ["environment", "organization_id", "left"]


# ---- the writer: what it writes ----

def test_as_proposed_writes_the_document_the_version_and_the_row(store):
    out = submit(store, request())
    assert (out["status"], out["organization_id"], out["well_id"], out["run_id"]) == (C.WRITTEN, ORG, WELL, RUN_ID)
    version_key, row_key = C.version_key(SHA, CID), C.row_key("prd", CID)
    assert sorted(written_keys(store)) == sorted([C.request_key(CID), C.result_key(CID), C.document_key(SHA),
                                                   version_key, row_key])
    assert store.get(C.document_key(SHA)) == PDF
    result = C.load(store.get(C.result_key(CID)))
    assert (result["status"], result["reasons"], result["row_key"], result["version_key"]) == (C.WRITTEN, [], row_key, version_key)
    row = json.loads(store.get(row_key))
    assert row["pump_parameters"]["design_document_ref"] == {
        "value": f"s3://roam-ds-assets/{version_key}", "unit": "none", "source": "design_document"}
    assert row["pump_parameters"]["pump_sections"]["value"] == [
        {"section_order": 1, "pump_model": "ELS1750", "stages": 490, "section_role": "primary"}]
    assert row["wellbore_parameters"] == {k: WRITTEN[k][0] for k in ("oil_sg", "bubble_point_psia")}   # empty ones left out
    version = C.load(store.get(version_key))
    assert version["labels"] == {"organization_id": ORG, "well_id": WELL, "source_name": "design.pdf", "document_sha256": SHA}
    assert (version["run"]["run_id"], version["entry_method"], version["confirmed_by"], version["confirmed_at"],
            version["design_date"]) == (RUN_ID, "extracted", "tester", AT, "2025-06-01")
    assert version["row"] == row and version["row_key"] == row_key
    oil = next(v for v in version["values"] if v["key"] == "oil_sg")
    assert (oil["proposed"], oil["final"], oil["starting_status"], oil["status"], oil["reasons"], oil["page"]) == (
        WRITTEN["oil_sg"][0], WRITTEN["oil_sg"][0], TO_CHECK, SETTLED, ["varies_by_scenario"], "p3")
    assert [h["by"] for h in oil["history"]] == ["extraction", "tester"]
    assert [v["key"] for v in version["values"]] == list(KEYS)
    assert version["pump_string"]["proposed"] == entry()["review"]["pump_string"]
    assert {c["check"]: c["result"] for c in version["pump_string"]["checks_on_final_sections"]} == {
        "sections": "pass", "V-08": "pass", "V-09": "pass", "models": "pass", "complete": "pass"}
    assert version["not_reviewed"] == {name: f"{C.ROOT}runs/{RUN_ID}/{file}" for name, file in C.NOT_REVIEWED.items()}


def test_a_correction_and_an_accepted_block_are_written(store):
    put_run(store, entry(**BLOCKED))
    q = request(entry(**BLOCKED), accept_anyway="the vendor numbers sections from the top",
                finals={"oil_sg": {"value": 0.85, "unit": "sg", "source": "design_document"}})
    assert (item(q, "oil_sg")["status"], item(q, "pump_sections")["status"]) == (C.CORRECTED, SETTLED)
    assert item(q, "pump_sections")["accept_anyway"] == "the vendor numbers sections from the top"
    assert "accept_anyway" not in item(q, "oil_sg")
    assert submit(store, q)["status"] == C.WRITTEN
    row = json.loads(store.get(C.row_key("prd", CID)))
    assert row["wellbore_parameters"]["oil_sg"] == {"value": 0.85, "unit": "sg", "source": "design_document"}
    assert "pump_sections" not in row["pump_parameters"]          # the blocked string was never a value


def test_a_string_corrected_so_that_its_order_check_passes_needs_no_reason(store):
    put_run(store, entry(**BLOCKED))
    sections = [{**{f: None for f in C.SECTION_FIELDS}, "section_order": n, "pump_model": m, "stages": s}
                for n, m, s in ((1, "ELS1750", 490), (2, "ELS3000", 28))]
    finals = {"pump_sections": {"value": [section_value(s) for s in sections], "unit": "none", "source": "design_document"}}
    assert submit(store, request(entry(**BLOCKED), finals=finals, sections=sections))["status"] == C.WRITTEN


def test_a_second_confirmation_of_the_document_finds_it_already_there(store):
    assert submit(store, request())["status"] == C.WRITTEN
    other = "20261028T140000Z-0000beef"
    assert submit(store, request(confirmation_id=other), cid=other)["status"] == C.WRITTEN
    assert len(store.keys(f"{C.record_prefix(SHA)}versions/")) == 2 and len(store.keys(C.row_prefix("prd"))) == 2


def test_a_duplicate_request_reads_and_writes_nothing_more(store):
    store.put(C.result_key(CID), b"{}")
    store.put(C.request_key(CID), C.dump(request()))
    out = W.confirm(store, C.request_key(CID), deadline=DEADLINE, catalogue=lambda: CATALOGUE)
    assert out["status"] == "duplicate" and store.get(C.result_key(CID)) == b"{}"
    assert written_keys(store) == [C.request_key(CID), C.result_key(CID)]


def test_a_failure_before_the_row_writes_a_failed_result(store, monkeypatch):
    def broken():
        raise RuntimeError("catalogue unreadable")
    store.put(C.request_key(CID), C.dump(request()), create=True)
    out = W.confirm(store, C.request_key(CID), deadline=DEADLINE, catalogue=broken)
    result = C.load(store.get(C.result_key(CID)))
    assert out["status"] == result["status"] == C.FAILED
    assert result["reasons"] == ["INTERNAL_ERROR at check: RuntimeError: catalogue unreadable"]
    assert "Traceback" in out["error"]["trace"] and written_keys(store) == [C.request_key(CID), C.result_key(CID)]


def test_once_the_row_is_written_the_confirmation_stands(store, monkeypatch):
    put = store.put

    def result_fails(key, body, *, create=False):
        if key == C.result_key(CID) and not create:
            raise OSError("disk full")
        put(key, body, create=create)
    monkeypatch.setattr(store, "put", result_fails)
    out = submit(store, request())
    assert out["status"] == C.WRITTEN and out["error"]["stage"] == "result"
    assert C.load(store.get(C.result_key(CID)))["status"] == C.RUNNING    # stays running; stale past its deadline
    assert store.keys(C.row_prefix("prd")) == [C.row_key("prd", CID)]


# ---- the Lambda entry point: write order, the run log, duplicates, failures ----

class Bucket(TA.FakeS3):
    """test_app_s3's fake bucket as S3 answers a caller with s3:ListBucket, as the app and the CLI have: a missing
    key is a 404.  test_app_s3's own answers as S3 answers the writer, which cannot list: a 403."""

    def get_object(self, *, Bucket, Key):
        if Key in self.objects or ("GetObject", Key) in self.fail:
            return super().get_object(Bucket=Bucket, Key=Key)
        self._call("GetObject", Bucket, Key)
        raise TL.client_error(404, "NoSuchKey", "GetObject")


@pytest.fixture
def bucket(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", FUNCTION)
    monkeypatch.setenv(ASSETS_ENV, os.environ.get(ASSETS_ENV, ""))     # the handler repoints it; restored after the test
    monkeypatch.setattr(L, "CONFIRM_WORK", tmp_path / "tmp" / "esp-design-confirmation")
    fake = TA.FakeS3()                  # the writer cannot list
    put_run(C.S3Objects(fake))
    fake.puts.clear()
    fake.calls.clear()
    return fake


def handle(fake, q=None, *, key=None, table=None, catalogue=lambda: CATALOGUE):
    key = key or C.request_key(CID)
    if q is not None:
        fake.objects[key] = q if isinstance(q, bytes) else C.dump(q)
    table = table or TL.FakeTable()
    out = L.confirm_handler(TL.s3_event(key), TL.Context(60_000), s3=fake, table=table, catalogue=catalogue)
    return out, table


def assert_writer_row(row, *, status, organization_id=ORG, well_ids=(WELL,), execution_id=CID):
    assert set(row) == TL.ENDED
    assert (row["type"], row["organization_id"], row["organization_id_type"]) == (
        "PIPELINE_EXECUTION", organization_id, f"{organization_id}#PIPELINE_EXECUTION")
    assert (row["pipeline_execution_id"], row["uuid"], row["well_ids"]) == (execution_id, execution_id, list(well_ids))
    assert (row["status"], row["success"], row["resource_name"]) == (status, status == "SUCCEEDED", FUNCTION)


def test_the_handler_writes_in_order_and_logs_one_row(bucket, capsys):
    out, table = handle(bucket, request())
    assert out == [{"confirmation_id": CID, "status": C.WRITTEN}]
    version_key, row_key = C.version_key(SHA, CID), C.row_key("prd", CID)
    assert TL.put_keys(bucket) == [C.result_key(CID), C.document_key(SHA), version_key, row_key, C.result_key(CID)]
    assert [bucket.headers[k]["IfNoneMatch"] for k in (C.document_key(SHA), version_key, row_key)] == ["*"] * 3
    assert bucket.headers[C.result_key(CID)]["IfNoneMatch"] is None              # the last put: the overwrite
    assert json.loads(bucket.puts[0][1])["status"] == C.RUNNING and json.loads(bucket.puts[0][1])["deadline_at"]
    [row] = table.items
    assert TL.writes(table) == [("PutItem", "RUNNING", (ORG, row["timestamp"])), ("UpdateItem", "SUCCEEDED", (ORG, row["timestamp"]))]
    assert_writer_row(row, status="SUCCEEDED")
    assert TL.data(row) == {"request_id": TL.REQUEST_ID, "request_key": C.request_key(CID), "run_id": RUN_ID,
                            "row_key": row_key, "version_key": version_key, "outcome": C.WRITTEN, "reasons": []}
    written = json.dumps(table.writes)
    assert not any(leak in written for leak in ("Records", "userIdentity", TL.UPLOADER["principalId"], TL.UPLOADER_IP))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {line["run_id"] for line in lines} == {CID} and lines[-1]["stage"] == "end"


def test_a_refusal_updates_the_row_to_failed_with_its_reasons(bucket):
    out, table = handle(bucket, request(environment="qa"))
    assert out == [{"confirmation_id": CID, "status": C.REFUSED}]
    assert TL.put_keys(bucket) == [C.result_key(CID), C.result_key(CID)]
    [row] = table.items
    assert [op for op, _, _ in TL.writes(table)] == ["PutItem", "UpdateItem"]
    assert_writer_row(row, status="FAILED")
    assert (TL.data(row)["outcome"], TL.data(row)["reasons"]) == (C.REFUSED, ["environment 'qa' is not dev or prd"])


@pytest.mark.parametrize("status", [412, 409])
def test_a_duplicate_event_stops_at_the_create_with_no_row(bucket, status):
    if status == 412:
        bucket.objects[C.result_key(CID)] = b"{}"
    else:
        bucket.fail["PutObject", C.result_key(CID)] = TL.client_error(409, "ConditionalRequestConflict", "PutObject")
    out, table = handle(bucket, request())
    assert out == [{"confirmation_id": CID, "status": "duplicate"}]
    assert bucket.calls == [("PutObject", C.result_key(CID))] and bucket.puts == [] and table.items == []


def test_an_exception_writes_a_failed_result_a_failed_row_and_the_trace(bucket, capsys):
    bucket.fail["GetObject", C.run_key(RUN_ID, "run.json")] = TL.client_error(500, "InternalError", "GetObject")
    out, table = handle(bucket, request())
    assert out == [{"confirmation_id": CID, "status": C.FAILED}]
    result = json.loads(bucket.objects[C.result_key(CID)])
    assert result["status"] == C.FAILED and result["reasons"][0].startswith("INTERNAL_ERROR at check: ClientError")
    [row] = table.items
    assert_writer_row(row, status="FAILED")
    data = TL.data(row)
    assert (data["outcome"], data["error_code"], data["stage"]) == (C.FAILED, "INTERNAL_ERROR", "check")
    assert "Traceback" in data["stack_trace"]
    traced = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"trace"' in line]
    assert traced and "Traceback" in traced[0]["trace"]


def test_an_unreadable_request_gets_one_failed_row_under_unknown(bucket):
    out, table = handle(bucket, b"not json")
    assert out == [{"confirmation_id": CID, "status": C.REFUSED}]
    [row] = table.items
    assert [op for op, _, _ in TL.writes(table)] == ["PutItem"] and row["end_timestamp"] == row["timestamp"]
    assert_writer_row(row, status="FAILED", organization_id="_unknown", well_ids=())


def test_a_key_that_is_not_a_request_is_rejected(bucket):
    out, table = handle(bucket, key=f"{C.ROOT}confirmations/{CID}/notes.json")
    assert out == [{"key": f"{C.ROOT}confirmations/{CID}/notes.json", "status": "rejected"}] and bucket.calls == []
    [row] = table.items
    assert_writer_row(row, status="FAILED", organization_id="_unknown", well_ids=(),
                      execution_id=f"ADHOC#{TL.REQUEST_ID}")


def test_a_failed_run_log_write_never_fails_the_confirmation(bucket):
    out, table = handle(bucket, request(), table=TL.FakeTable(error=TL.client_error(400, "ValidationException", "PutItem")))
    assert out == [{"confirmation_id": CID, "status": C.WRITTEN}] and table.items == []


@needs_assets
def test_the_handler_reads_the_catalogue_snapshot_after_its_hash_check(bucket):
    assets = Path(os.environ[ASSETS_ENV])
    bucket.objects[C.ASSETS_PREFIX + L.CATALOGUE] = (assets / L.CATALOGUE).read_bytes()
    out, _ = handle(bucket, request(), catalogue=None)
    assert out == [{"confirmation_id": CID, "status": C.WRITTEN}]
    bucket.objects[C.ASSETS_PREFIX + L.CATALOGUE] = b"changed"
    other = "20261028T140000Z-0000beef"
    out, table = handle(bucket, request(confirmation_id=other), key=C.request_key(other), catalogue=None)
    assert out == [{"confirmation_id": other, "status": C.FAILED}]
    assert TL.data(table.items[0])["error_code"] == "ASSET_INVALID"


# ---- the writer cannot list: a 403 reads as not there, in the writer only ----

FORBIDDEN = "the writer cannot list, so S3 answers 403 for a key that is not there"


@pytest.mark.parametrize("key,reason", [
    (C.run_key(RUN_ID, "run.json"), f"runs/{RUN_ID}/run.json is not there"),
    (C.run_key(RUN_ID, C.ENTRY), f"runs/{RUN_ID}/{C.ENTRY} is not there"),
    (C.pdf_key(RUN_ID), f"intake/{RUN_ID}.pdf is not there"),
    (C.run_key(RUN_ID, "report.md"), f"runs/{RUN_ID}/report.md is not there"),
], ids=["run.json", "well_entry.json", "pdf", "report.md"])
def test_a_403_on_the_run_or_its_pdf_is_a_refusal_naming_the_key(bucket, key, reason):
    del bucket.objects[key]
    out, table = handle(bucket, request())
    assert out == [{"confirmation_id": CID, "status": C.REFUSED}]
    assert TL.data(table.items[0])["reasons"] == [f"{reason} (S3 answered 403 for {key}: {FORBIDDEN})"]
    assert TL.put_keys(bucket) == [C.result_key(CID), C.result_key(CID)]


def test_a_403_on_the_request_is_a_refusal_naming_the_key(bucket):
    out, table = handle(bucket)                                         # no request at its key
    assert out == [{"confirmation_id": CID, "status": C.REFUSED}]
    assert TL.data(table.items[0])["reasons"] == [
        f"contract: the request is not there (S3 answered 403 for {C.request_key(CID)}: {FORBIDDEN})"]


def test_a_403_on_assets_stays_a_failure(bucket):
    out, table = handle(bucket, request(), catalogue=None)              # no catalogue snapshot under assets/
    assert out == [{"confirmation_id": CID, "status": C.FAILED}]
    data = TL.data(table.items[0])
    assert (data["outcome"], data["error_code"], data["stage"]) == (C.FAILED, "ASSET_INVALID", "check")
    assert "(AccessDenied)" in data["message"]
    assert json.loads(bucket.objects[C.result_key(CID)])["status"] == C.FAILED


def test_the_readers_take_only_a_404_for_not_there():
    missing = C.result_key(CID)
    with pytest.raises(FileNotFoundError):
        C.S3Objects(Bucket()).get(missing)
    with pytest.raises(ClientError):
        C.S3Objects(TA.FakeS3()).get(missing)
    fake = Bucket()
    store = S.S3RunStore(fake, C.BUCKET, "esp-design-extraction/intake/", "esp-design-extraction/runs/")
    stored_row(store.objects, CID, "2026-10-28T10:00:00.000000")
    store.objects.put(missing, C.dump(C.result(CID, C.WRITTEN, at=DEADLINE)))
    for key in (C.row_key("prd", CID), C.version_key(SHA, CID), missing):      # listed, but S3 answers 403
        fake.fail["GetObject", key] = TL.client_error(403, "AccessDenied", "GetObject")
    for read in (lambda: store.current_entries("prd"), lambda: store.latest_version(SHA, ORG, WELL),
                 lambda: store.result(CID)):
        with pytest.raises(ClientError):
            read()


# ---- the CLI ----

@pytest.fixture
def local(tmp_path, monkeypatch):
    root = tmp_path / "store"
    put_run(C.LocalObjects(root))
    monkeypatch.setenv(CLI.STORE_ENV, str(root))
    monkeypatch.setattr(W, "read_catalogue", lambda: CATALOGUE)
    return C.LocalObjects(root)


def cli(*args) -> int:
    return CLI.main(["--run-id", RUN_ID, "--confirmed-by", "tester", *args])


def only_request(objects) -> dict:
    [key] = [k for k in objects.keys(f"{C.ROOT}confirmations/") if k.endswith("/request.json")]
    return C.load(objects.get(key))


def test_cli_as_proposed(local, capsys):
    assert cli("--as-proposed") == 0
    out = capsys.readouterr().out
    q = only_request(local)
    assert out.startswith(f"{q['confirmation_id']} written -> {C.row_key('prd', q['confirmation_id'])}")
    assert {v["status"] for v in q["values"]} | {q["pump_string"]["status"]} == {SETTLED}
    assert C.CONFIRMED_AT.fullmatch(q["confirmed_at"]) and q["confirmation_id"][:8] == q["confirmed_at"][:10].replace("-", "")
    assert [h["reason"] for h in item(q, "oil_sg")["history"]] == ["varies_by_scenario", "as proposed"]


def test_cli_waits_180_seconds_by_default(local, monkeypatch):
    deadlines, real = [], W.confirm

    def confirm(objects, key, *, deadline, **kwargs):
        deadlines.append(deadline)
        return real(objects, key, deadline=deadline, **kwargs)
    monkeypatch.setattr(W, "confirm", confirm)
    assert cli("--as-proposed") == 0
    at = datetime.fromisoformat(only_request(local)["confirmed_at"]).replace(tzinfo=timezone.utc)
    assert deadlines == [at + timedelta(seconds=180)]


def test_cli_one_correction(local, capsys):
    assert cli("--as-proposed", "--set", "oil_sg=0.85", "--set", "vapor_pressure_psia=12.5") == 0
    q = only_request(local)
    assert (item(q, "oil_sg")["status"], item(q, "oil_sg")["final"]) == (
        C.CORRECTED, {"value": 0.85, "unit": "sg", "source": "design_document"})
    assert item(q, "vapor_pressure_psia")["final"] == {"value": 12.5, "unit": "psia", "source": "ops_entered"}
    row = json.loads(local.get(C.row_key("prd", q["confirmation_id"])))
    assert row["wellbore_parameters"]["vapor_pressure_psia"]["source"] == "ops_entered"


def test_cli_leave_to_check_is_refused(local, capsys):
    assert cli("--as-proposed", "--leave-to-check", "oil_sg") == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith(" refused") and out[1] == "  - left to check: oil_sg"
    assert local.keys(f"{C.ROOT}well-entries/") == []


def test_cli_without_as_proposed_leaves_what_is_to_check(local, capsys):
    assert cli() == 1 and "  - left to check: oil_sg" in capsys.readouterr().out


@pytest.mark.parametrize("args,message", [
    (("--set", "oil_sg=heavy"), "--set oil_sg: 'heavy' is not a number"),
    (("--set", "oil_sg=nan"), "--set oil_sg: 'nan' is not a finite number"),
    (("--set", "pump_id=3"), "--set 'pump_id=3': not KEY=VALUE for a top-level key of the entry"),
    (("--set", "design_document_ref=x"), "--set design_document_ref: the writer sets it"),
    (("--leave-to-check", "stages"), "--leave-to-check: not keys of the entry: stages"),
])
def test_cli_refuses_to_send_what_it_cannot_build(local, capsys, args, message):
    assert cli(*args) == 2 and message in capsys.readouterr().err
    assert local.keys(f"{C.ROOT}confirmations/") == []


def test_cli_local_mode_needs_a_name_and_a_store_outside_the_lab(local, monkeypatch, capsys):
    assert CLI.main(["--run-id", RUN_ID]) == 2 and "give --confirmed-by" in capsys.readouterr().err
    monkeypatch.setenv(CLI.STORE_ENV, str(CLI.LAB / "store"))
    assert cli() == 2 and "inside" in capsys.readouterr().err


def test_cli_s3_mode_writes_the_request_and_polls_the_writers_result(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(CLI.MODE_ENV, "s3")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", FUNCTION)
    monkeypatch.setattr(L, "CONFIRM_WORK", tmp_path / "work")

    class Triggered(Bucket):              # the S3 trigger: a request's creation invokes the writer
        def put_object(self, **kwargs):
            super().put_object(**kwargs)
            if C.request_id(kwargs["Key"]):
                L.confirm_handler(TL.s3_event(kwargs["Key"]), TL.Context(60_000), s3=self, table=TL.FakeTable(),
                                  catalogue=lambda: CATALOGUE)

    class Sts:
        def get_caller_identity(self):
            return {"Arn": "arn:aws:sts::000000000000:assumed-role/Reviewer/analyst-1"}
    fake = Triggered()
    put_run(C.S3Objects(fake))
    import boto3
    monkeypatch.setattr(boto3, "client", lambda name, **kw: {"s3": fake, "sts": Sts()}[name])
    assert CLI.main(["--run-id", RUN_ID, "--as-proposed", "--wait", "5"]) == 0
    q = only_request(C.S3Objects(fake))
    assert q["confirmed_by"] == "analyst-1" and "written" in capsys.readouterr().out


@needs_assets
def test_a_real_entry_confirmed_end_to_end(tmp_path, monkeypatch, capsys):
    """A run of the pipeline in report mode, with the synthetic F4 PDF, stores a real entry; the CLI confirms it."""
    import extraction.run as R
    from test_unit import F4_P1, F4_P2, make_pdf
    from test_well_entry import CONTEXT, PUMP
    pdf, report = tmp_path / "doc.pdf", tmp_path / "report.md"
    pdf.write_bytes(make_pdf([F4_P1, F4_P2]))
    report.write_text(report_text("F4", pc=[PUMP], dc=[CONTEXT]), encoding="utf-8")
    record = R.run(organization_id=ORG, well_id=WELL, pdf=pdf, report=report, store_root=tmp_path / "store")
    objects = C.LocalObjects(tmp_path / "store")
    objects.put(C.pdf_key(record["run_id"]), pdf.read_bytes())
    monkeypatch.setenv(CLI.STORE_ENV, str(tmp_path / "store"))
    assert CLI.main(["--run-id", record["run_id"], "--as-proposed", "--confirmed-by", "tester"]) == 0
    [key] = objects.keys(C.row_prefix("prd"))
    row = json.loads(objects.get(key))
    assert row["pump_parameters"]["pump_sections"]["value"][0]["pump_model"] == "ELS1750"
    assert schema.problems(row) == [] and not parameter_problems(row["pump_parameters"], "pump_parameters")


# ---- the readers, both backends ----

def stored_row(objects, cid, confirmed_at, *, well=WELL, environment="prd", sha=SHA, version=True, row=True):
    """A confirmed row and its version, as the writer leaves them (`row=False`: a version that never committed)."""
    the_row = {"organization_id": ORG, "well_id": well, "pump_parameters": {}, "wellbore_parameters": {},
               "run_id": RUN_ID, "document_sha256": sha, "design_date": None, "entry_method": "extracted",
               "confirmation_id": cid, "confirmed_at": confirmed_at, "confirmed_by": "tester", "environment": environment}
    if version:
        objects.put(C.version_key(sha, cid), C.dump({"labels": {"organization_id": ORG, "well_id": well},
                                                     "environment": environment, "confirmation_id": cid,
                                                     "confirmed_at": confirmed_at, "row_key": C.row_key(environment, cid),
                                                     "row": the_row}))
    if row:
        objects.put(C.row_key(environment, cid), C.row_bytes(the_row))
    return the_row


@pytest.fixture(params=["local", "s3"])
def reader(request, tmp_path):
    if request.param == "local":
        store = S.LocalRunStore(tmp_path / "store")
        return store, store.objects, None
    fake = Bucket()
    return S.S3RunStore(fake, C.BUCKET, "esp-design-extraction/intake/", "esp-design-extraction/runs/"), C.S3Objects(fake), fake


def test_the_latest_confirmed_row_is_the_current_entry_and_the_id_breaks_ties(reader):
    store, objects, _ = reader
    stored_row(objects, "20261028T090000Z-ffffffff", "2026-10-28T09:00:00.000000")     # sorts last by id, earlier in time
    later = stored_row(objects, "20261028T080000Z-00000001", "2026-10-28T10:00:00.000000")
    assert store.current_entry("prd", ORG, WELL) == later
    tie = stored_row(objects, "20261028T080000Z-00000002", "2026-10-28T10:00:00.000000")
    assert store.current_entry("prd", ORG, WELL) == tie
    other = stored_row(objects, "20261028T070000Z-00000003", "2026-10-28T07:00:00.000000", well="Well #1")
    dev = stored_row(objects, "20261028T070000Z-00000004", "2026-10-28T11:00:00.000000", environment="dev")
    assert store.current_entries("prd") == {(ORG, WELL): tie, (ORG, "Well #1"): other}
    assert store.current_entries("dev") == {(ORG, WELL): dev} and store.current_entry("prd", ORG, "WELL  #1") is None


def test_rows_are_read_once(reader):
    store, objects, fake = reader
    stored_row(objects, "20261028T080000Z-00000001", "2026-10-28T10:00:00.000000")
    store.current_entries("prd")
    if fake is not None:
        gets = [c for c in fake.calls if c[0] == "GetObject"]
        store.current_entries("prd")
        assert [c for c in fake.calls if c[0] == "GetObject"] == gets             # listed again, not read again
    else:
        objects.path(C.row_key("prd", "20261028T080000Z-00000001")).write_bytes(b"not read again")
        assert store.current_entry("prd", ORG, WELL)["confirmed_by"] == "tester"


def test_the_latest_version_is_the_latest_with_a_row(reader):
    store, objects, _ = reader
    stored_row(objects, "20261028T080000Z-00000001", "2026-10-28T08:00:00.000000")
    stored_row(objects, "20261028T090000Z-00000002", "2026-10-28T09:00:00.000000", row=False)    # never committed
    stored_row(objects, "20261028T100000Z-00000003", "2026-10-28T10:00:00.000000", well="Well #1")
    assert store.latest_version(SHA, ORG, WELL)["confirmation_id"] == "20261028T080000Z-00000001"
    assert store.latest_version(SHA, ORG, "Well #1")["confirmation_id"] == "20261028T100000Z-00000003"
    assert store.latest_version("0" * 64, ORG, WELL) is None and store.latest_version("../x", ORG, WELL) is None


def test_a_running_result_past_its_deadline_reads_as_stale(reader):
    store, objects, _ = reader
    assert store.result(CID) is None and store.result("../x") is None
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    objects.put(C.result_key(CID), C.dump(C.result(CID, C.RUNNING, at=past, deadline_at=C.utc(past))))
    assert store.result(CID)["status"] == C.STALE
    objects.put(C.result_key(CID), C.dump(C.result(CID, C.RUNNING, at=past, deadline_at=C.utc(DEADLINE))))
    assert store.result(CID)["status"] == C.RUNNING


# ---- the schema ----

def test_the_schema_types_each_key_once_as_the_builder_maps_it():
    assert schema.MAP_OF == {key: map_ for key, (map_, _) in KEYS.items()}
    columns = dict(schema.COLUMNS)
    assert columns["pump_parameters"].startswith(
        "struct<pump_sections:struct<value:array<struct<section_order:int,pump_model:string,stages:int,"
        "section_role:string>>,unit:string,source:string,as_of:string,note:string>,"
        "setting_depth_tvd_ft:struct<value:double,unit:string,source:string,as_of:string,note:string>,")
    assert "motor_type:struct<value:string," in columns["pump_parameters"]
    assert columns["wellbore_parameters"].count("struct<value:double,") == 10
    assert [name for name, _ in schema.PARTITION_KEYS] == ["environment"]


@pytest.mark.parametrize("change,problem", [
    (lambda r: r["pump_parameters"]["pump_sections"]["value"][0].update(stages=490.0),
     "pump_parameters.pump_sections.value: not the section array"),
    (lambda r: r["wellbore_parameters"]["oil_sg"].update(value="0.85"), "wellbore_parameters.oil_sg.value: not double"),
    (lambda r: r["wellbore_parameters"]["oil_sg"].update(as_of=20261028), "wellbore_parameters.oil_sg.as_of: not a string"),
    (lambda r: r["wellbore_parameters"].update(api_gravity={"value": 40}), "wellbore_parameters.api_gravity: not a field"),
    (lambda r: r.update(extra=1), "extra: not a column"),
    (lambda r: r.update(confirmed_by=None), "confirmed_by: missing"),
])
def test_the_schema_refuses_what_the_table_cannot_type(store, change, problem):
    submit(store, request())
    row = json.loads(store.get(C.row_key("prd", CID)))
    change(row)
    assert problem in " | ".join(schema.problems(row))


ROWS = os.environ.get("ESP_WELL_ENTRIES_STORE", "")
needs_rows = pytest.mark.skipif(not ROWS, reason="the confirmed rows need ESP_WELL_ENTRIES_STORE")


def confirmed_rows() -> list[dict]:
    objects = C.LocalObjects(ROWS)
    return [json.loads(objects.get(k)) for e in C.ENVIRONMENTS for k in objects.keys(C.row_prefix(e))]


@needs_rows
def test_every_confirmed_row_conforms_to_the_schema():
    rows = confirmed_rows()
    assert rows and all(schema.problems(row) == [] for row in rows)


# ---- #1218: this repo's map rules against the product's own validator ----

PRODUCT_FILE = "python-packages/esp_resources_v2/esp_resources_v2/well_parameters.py"
PRODUCT_COMMIT = "6831cbd6"
MONOREPO = os.environ.get("ESP_MONOREPO_DIR", "")


@pytest.fixture(scope="module")
def product():
    if not MONOREPO:
        pytest.skip("#1218's validator needs ESP_MONOREPO_DIR, the monorepo clone")
    source = subprocess.run(["git", "-C", MONOREPO, "show", f"{PRODUCT_COMMIT}:{PRODUCT_FILE}"], check=True,
                            capture_output=True, text=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout
    module = types.ModuleType(f"product_well_parameters_{PRODUCT_COMMIT}")
    sys.modules[module.__name__] = module
    exec(compile(source, f"{PRODUCT_FILE}@{PRODUCT_COMMIT}", "exec"), module.__dict__)
    return module


def accepts(product, mapping, field) -> bool:
    try:
        product.validate_parameter_map(mapping, field)
        return True
    except product.ParameterError:
        return False


def env(value, unit, source="design_document", **extra):
    return {"value": value, "unit": unit, "source": source, **extra}


def sections_env(*sections, unit="none"):
    return env(list(sections), unit)


ONE = {"section_order": 1, "pump_model": "ELS1750", "stages": 490}
REFUSED_MAPS = [
    ({"oil_sg": 0.85}, "wellbore_parameters"),
    ({"oil_sg": {"value": 0.85, "unit": "sg"}}, "wellbore_parameters"),
    ({"oil_sg": env(None, "sg")}, "wellbore_parameters"),
    ({"oil_sg": env(0.85, "sg", source="guess")}, "wellbore_parameters"),
    ({"oil_sg": env(0.85, "sg", printed_unit="API")}, "wellbore_parameters"),
    ({"oil_sg": env(0.85, "none")}, "wellbore_parameters"),
    ({"oil_sg": env(0.85, "furlong")}, "wellbore_parameters"),
    ({"oil_sg": env(0.85, "sg")}, "pump_parameters"),
    ({"bubble_point_psia": env(1500, "psig")}, "wellbore_parameters"),
    ({"bubble_point_psia": env(1500, "psi")}, "wellbore_parameters"),
    ({"setting_depth_tvd_ft": env(True, "ft")}, "pump_parameters"),
    ({"motor_type": env("induction", "hp")}, "pump_parameters"),
    ({"motor_nameplate_hp": env("300", "none")}, "pump_parameters"),
    ({"design_document_ref": env("s3://x", "ft")}, "pump_parameters"),
    ({"pump_id": env(1.5, "id")}, "pump_parameters"),
    ({"stages": env(300, "ft")}, "pump_parameters"),
    ({"pump_sections": env([], "none")}, "pump_parameters"),
    ({"pump_sections": sections_env({**ONE, "section_order": 2})}, "pump_parameters"),
    ({"pump_sections": sections_env({**ONE, "section_order": True})}, "pump_parameters"),
    ({"pump_sections": sections_env({**ONE, "stages": 0})}, "pump_parameters"),
    ({"pump_sections": sections_env({**ONE, "stages": 2.5})}, "pump_parameters"),
    ({"pump_sections": sections_env({**ONE, "pump_model": " "})}, "pump_parameters"),
    ({"pump_sections": sections_env({**ONE, "pump_id": 1005})}, "pump_parameters"),
    ({"pump_sections": sections_env({**ONE, "section_role": ""})}, "pump_parameters"),
    ({"pump_sections": sections_env({"section_order": 1, "stages": 5})}, "pump_parameters"),
    ({"pump_sections": sections_env(ONE, unit="count")}, "pump_parameters"),
    ({"pump_sections": sections_env(ONE, "not a section")}, "pump_parameters"),
    (["not a map"], "pump_parameters"),
]
ACCEPTED_MAPS = [
    (None, "pump_parameters"),
    ({}, "wellbore_parameters"),
    ({"pump_sections": sections_env(ONE, {"section_order": 2, "pump_model": "ELS3000", "stages": 28.0,
                                          "section_role": "taper"})}, "pump_parameters"),
    ({"bubble_point_psia": env(1514.7, "psia", note='printed "1500 psig"; +14.7', as_of="yesterday")}, "wellbore_parameters"),
    ({"vapor_pressure_psia": env(12, "psia", source="ops_entered")}, "wellbore_parameters"),
    ({"a_count_the_product_does_not_describe": env(3, "count", source="measured")}, "pump_parameters"),
    ({"motor_type": env("permanent magnet", "none")}, "pump_parameters"),
]


@pytest.mark.parametrize("mapping,field", REFUSED_MAPS)
def test_the_map_rules_refuse_the_negative_cases(mapping, field):
    assert parameter_problems(mapping, field)


@pytest.mark.parametrize("mapping,field", ACCEPTED_MAPS)
def test_the_map_rules_accept_what_the_product_accepts(mapping, field):
    assert parameter_problems(mapping, field) == []


@pytest.mark.parametrize("mapping,field", REFUSED_MAPS + ACCEPTED_MAPS)
def test_the_map_rules_agree_with_the_product_on_each_case(product, mapping, field):
    assert (not parameter_problems(mapping, field)) == accepts(product, mapping, field)


def test_the_map_rules_agree_with_the_product_on_the_maps_written_here(product, store):
    assert submit(store, request())["status"] == C.WRITTEN
    row = json.loads(store.get(C.row_key("prd", CID)))
    for field in schema.PARAMETERS:
        assert parameter_problems(row[field], field) == [] and accepts(product, row[field], field)


@needs_rows
def test_the_map_rules_agree_with_the_product_on_every_confirmed_map(product):
    rows = confirmed_rows()
    assert rows
    for row in rows:
        for field in schema.PARAMETERS:
            assert parameter_problems(row[field], field) == [] and accepts(product, row[field], field), row["confirmation_id"]
