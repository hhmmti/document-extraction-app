"""Entering a run by hand, offline: the empty proposal (extraction/well_entry.py), the writer's manual path
(extraction/confirm_writer.py) on a local store and on S3 fakes that cannot list, and the app's offer, review, gate,
request and confirmed view (app/review.py, app/main.py), in local mode and, through the writer Lambda, in S3 mode.
The run is test_confirm's synthetic one, failed at Detect as a scan fails: no entry and no report.  The catalogue is
test_confirm's stand-in.  No AWS."""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import app.review as R
import app.store as S
import extraction.confirm as CLI
import extraction.confirm_writer as W
import extraction.confirmation as C
import extraction.well_entries_schema as schema
import lambda_function as L
import test_app_s3 as TA
import test_lambda as TL
from extraction.assets_dir import ASSETS_ENV
from extraction.intake import encode_labels
from extraction.well_entry import (ENTERED_BY_HAND, KEYS, MANUAL, NOT_FROM_THE_DOCUMENT, SETTLED, TO_CHECK, empty_entry,
                                   entry_bytes, section_value)
from test_confirm import AT, CATALOGUE, CID, DEADLINE, FUNCTION, ORG, PDF, RUN_ID, SHA, WELL, entry, run_record
from test_editor import fresh_catalogue  # noqa: F401 - a fixture
from test_review import VALIDATE, app, s3_mode  # noqa: F401 - s3_mode: a fixture
from test_screens import exempt, unplain
from test_transcription import made  # noqa: F401 - a fixture: a scan and its transcription
from test_validate import report_text
from test_viewer import texts

IMAGE_ONLY = {"code": "IMAGE_ONLY", "stage": "detect", "details": {"page": 1, "n_chars": 0, "floor": 20},
              "message": "page 1 has no usable text layer (0 non-whitespace characters, floor 20); the document "
                         "cannot be extracted"}
SCAN_WORDS = ("This document is a scan: its pages are pictures, with no text to read, so it cannot be read here. "
              "Transcribe the scan with the transcribe-design-scan skill, then upload the transcription it makes as a "
              "new run.")
SECOND = "20261029T120000Z-0123abce"
PRESSURE = {"value": 2014.7, "unit": "psia", "source": "design_document", "note": 'printed "2,000 psig"'}
DEPTH = {"value": 9641.0, "unit": "ft", "source": "design_document"}
SECTIONS = [{"section_order": 1, "pump_model": "ELS1750", "stages": 490, "section_role": "primary", "housings": 5,
             "stages_per_housing_as_printed": "98, 98, 98, 98, 98", "top_md_ft": None, "bottom_md_ft": None}]


def failed(**changes) -> dict:
    """The run's record as a scan leaves it: failed at Detect, before any family or route."""
    return run_record(**{"status": "failed", "family": None, "route": None, "error": IMAGE_ONLY,
                         "deadline_at": "2026-10-28T12:15:00Z", **changes})


def put_failed(objects, run=None, *, run_id=RUN_ID, pdf=PDF, report=False, stored_entry=None):
    objects.put(C.run_key(run_id, "run.json"), C.dump(run or failed(run_id=run_id)))
    if pdf is not None:
        objects.put(C.pdf_key(run_id), pdf)
    if report:
        objects.put(C.run_key(run_id, "report.md"), report_text("F4", {"effective_from": "2025-06-01"}).encode())
    if stored_entry is not None:
        objects.put(C.run_key(run_id, C.ENTRY), C.dump(stored_entry))


def by_hand(*, finals=None, sections=SECTIONS, run=None, **changes) -> dict:
    """A manual request: `finals` corrected (the pressure and the depth by default), `sections` the string's, every
    other value settled as empty."""
    finals = {"bubble_point_psia": PRESSURE, "setting_depth_md_ft": DEPTH} if finals is None else dict(finals)
    if sections:
        finals["pump_sections"] = {"value": [section_value(s) for s in sections], "unit": "none",
                                   "source": "design_document"}
    q = CLI.build_request(empty_entry(ORG, WELL), run or failed(), cid=CID, run_id=RUN_ID, document=SHA, confirmed_at=AT,
                          confirmed_by="tester", environment="prd", as_proposed=True, finals=finals,
                          sections=sections or None, accept_anyway=None, leave=set())
    return {**q, "entry_method": C.MANUAL, **changes}


def submit(objects, q, catalogue=CATALOGUE) -> dict:
    objects.put(C.request_key(CID), C.dump(q), create=True)
    return W.confirm(objects, C.request_key(CID), deadline=DEADLINE, catalogue=lambda: catalogue)


@pytest.fixture
def store(tmp_path):
    objects = C.LocalObjects(tmp_path / "store")
    put_failed(objects)
    return objects


# ---- the empty proposal ----

def test_the_empty_proposal_holds_every_key_to_check_as_entered_by_hand():
    e = empty_entry(ORG, WELL)
    items = e["review"]["values"]
    assert [v["key"] for v in items] == list(KEYS) and (e["organization_id"], e["well_id"]) == (ORG, WELL)
    assert e["pump_parameters"] == e["wellbore_parameters"] == {}
    for v in items:
        assert (v["map"], v["unit"], v["value"], v["written"]) == (*KEYS[v["key"]], None, False)
        if v["key"] in NOT_FROM_THE_DOCUMENT:              # as build_entry leaves them; the writer sets the reference
            assert (v["status"], v["reasons"], v["note"]) == (SETTLED, [], NOT_FROM_THE_DOCUMENT[v["key"]])
        else:
            assert (v["status"], v["reasons"]) == (TO_CHECK, [ENTERED_BY_HAND])
    base = ["key", "map", "status", "reasons", "value", "unit", "written"]            # build_entry's items, key by key
    assert {v["key"]: list(v) for v in items} == {
        k: base if k == "pump_sections" else [*base, "note"] if k in NOT_FROM_THE_DOCUMENT else
        [*base, "printed", "column", "scenario", "page", "page_basis", "note"] for k in KEYS}
    assert e["review"]["pump_string"] == {"status": TO_CHECK, "reasons": [ENTERED_BY_HAND], "scenario": None,
                                          "checks": [], "sections": [], "head_sum": []}
    assert e["review"]["route"] == MANUAL and e["review"]["family"] is None
    assert json.loads(entry_bytes(e)) == e


def test_no_reader_takes_the_empty_proposal_for_an_extraction():
    e = empty_entry(ORG, WELL)
    assert R.from_scan(e) is e                              # only the template route is started as a scan's
    assert R.manual(e) and not R.manual(entry())
    words = R.in_words(ENTERED_BY_HAND)
    assert words != R.UNKNOWN_REASON and "by hand" in words
    review = R.start(e, failed(), at=AT)
    assert R.groups(review, e) == {"string": ["pump_sections"], "not found": [k for k in KEYS if k not in (
        "pump_sections", *NOT_FROM_THE_DOCUMENT)], "found": [], "decided": list(NOT_FROM_THE_DOCUMENT)}
    assert not R.blocking(review, e, "pump_sections") and R.document_checks(e) == []


# ---- the writer, for entry_method manual ----

def test_a_manual_entry_is_written_on_a_local_store(store):
    out = submit(store, by_hand())
    assert (out["status"], out["reasons"]) == (C.WRITTEN, [])
    row = json.loads(store.get(C.row_key("prd", CID)))
    assert (row["entry_method"], row["design_date"]) == (C.MANUAL, None) and schema.problems(row) == []
    assert row["wellbore_parameters"] == {"bubble_point_psia": PRESSURE}
    assert row["pump_parameters"]["setting_depth_md_ft"] == DEPTH
    assert row["pump_parameters"]["pump_sections"]["value"] == [section_value(s) for s in SECTIONS]
    version = C.load(store.get(C.version_key(SHA, CID)))
    assert (version["entry_method"], version["design_date"], version["row"]) == (C.MANUAL, None, row)
    assert version["pump_string"]["proposed"] == empty_entry(ORG, WELL)["review"]["pump_string"]
    assert version["pump_string"]["final_sections"] == SECTIONS
    values = {v["key"]: v for v in version["values"]}
    assert [v["key"] for v in version["values"]] == list(KEYS)
    assert all(v["proposed"] is None for v in values.values())
    assert (values["bubble_point_psia"]["starting_status"], values["bubble_point_psia"]["status"],
            values["bubble_point_psia"]["reasons"], values["bubble_point_psia"]["final"]) == (
        TO_CHECK, C.CORRECTED, [ENTERED_BY_HAND], PRESSURE)
    assert (values["gas_sg"]["status"], values["gas_sg"]["final"]) == (SETTLED, None)
    assert values["vapor_pressure_psia"]["starting_status"] == SETTLED


def test_on_s3_fakes_that_cannot_list_the_missing_entry_and_report_are_none(monkeypatch, tmp_path):
    """The writer's role cannot list, so S3 answers 403 for a key that is not there: well_entry.json reads as none on
    this path, and report.md is never read."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", FUNCTION)
    monkeypatch.setenv(ASSETS_ENV, os.environ.get(ASSETS_ENV, ""))
    monkeypatch.setattr(L, "CONFIRM_WORK", tmp_path / "tmp" / "esp-design-confirmation")
    fake = TA.FakeS3()                                        # 403 for every key that is not there
    put_failed(C.S3Objects(fake))
    fake.objects[C.request_key(CID)] = C.dump(by_hand())
    table = TL.FakeTable()
    out = L.confirm_handler(TL.s3_event(C.request_key(CID)), TL.Context(60_000), s3=fake, table=table,
                            catalogue=lambda: CATALOGUE)
    assert out == [{"confirmation_id": CID, "status": C.WRITTEN}]
    assert ("GetObject", C.run_key(RUN_ID, C.ENTRY)) in fake.calls
    assert ("GetObject", C.run_key(RUN_ID, "report.md")) not in fake.calls
    assert json.loads(fake.objects[C.row_key("prd", CID)])["design_date"] is None
    assert TL.data(table.items[0])["outcome"] == C.WRITTEN


def test_a_report_the_run_stored_is_not_read_on_this_path(tmp_path):
    objects = C.LocalObjects(tmp_path / "store")
    put_failed(objects, report=True)                          # a run that failed after its report: no entry
    reads = []
    get = objects.get
    objects.get = lambda key: (reads.append(key), get(key))[1]
    assert submit(objects, by_hand())["status"] == C.WRITTEN
    assert C.run_key(RUN_ID, "report.md") not in reads
    assert json.loads(objects.get(C.row_key("prd", CID)))["design_date"] is None


def test_a_500_on_the_entry_is_a_failure_not_none(monkeypatch, tmp_path):
    fake = TA.FakeS3()
    put_failed(C.S3Objects(fake))
    fake.fail["GetObject", C.run_key(RUN_ID, C.ENTRY)] = TL.client_error(500, "InternalError", "GetObject")
    out = submit(C.S3Objects(fake), by_hand())
    assert out["status"] == C.FAILED and out["error"]["stage"] == "check"


def running(deadline) -> dict:
    return failed(status="running", error=None, ended_at=None, deadline_at=C.utc(deadline))


MANUAL_REFUSALS = [
    ("a run still running", lambda o: put_failed(o, running(datetime.now(timezone.utc) + timedelta(hours=1))),
     by_hand, f"runs/{RUN_ID}/run.json says 'running': the run has not ended"),
    ("a run with an entry", lambda o: put_failed(o, stored_entry=entry()), by_hand,
     f"runs/{RUN_ID}/well_entry.json is there: this run has an extraction to review"),
    ("nothing entered", None, lambda: by_hand(finals={}, sections=[]),
     "nothing was entered: no value is manually corrected to a value, and the pump string has no section"),
    ("a correction to empty only", None,
     lambda: by_hand(finals={"gas_sg": None}, sections=[]), "nothing was entered"),
    ("left to check", None, lambda: CLI.build_request(
        empty_entry(ORG, WELL), failed(), cid=CID, run_id=RUN_ID, document=SHA, confirmed_at=AT, confirmed_by="tester",
        environment="prd", as_proposed=False, finals={}, sections=None, accept_anyway=None, leave=set())
     | {"entry_method": C.MANUAL}, "left to check: pump_sections, setting_depth_tvd_ft"),
    ("changed but not marked", None,
     lambda: (q := by_hand(), next(v for v in q["values"] if v["key"] == "gas_sg").update(final={
         "value": 0.8, "unit": "sg", "source": "design_document"}), q)[-1],
     "changed from the proposal but not marked manually corrected: gas_sg"),
    ("a model that is not a catalogue spelling", None,
     lambda: by_hand(sections=[{**SECTIONS[0], "pump_model": "els1750"}]),
     "pump_model 'els1750' is not a catalogue spelling: the catalogue spells it 'ELS1750'"),
    ("an order check that fails", None,
     lambda: by_hand(sections=[SECTIONS[0], {**SECTIONS[0], "section_order": 3, "pump_model": "ELS3000", "stages": 28,
                                             "stages_per_housing_as_printed": "28", "housings": 1}]),
     "pump string: the order check fails"),
    ("#1218: a unit the key does not declare", None,
     lambda: by_hand(finals={"oil_sg": {"value": 0.82, "unit": "psia", "source": "design_document"}}),
     "#1218: wellbore_parameters.oil_sg: the key name declares 'sg' but the value says 'psia'"),
    ("the upload's environment", lambda o: o.meta(C.pdf_key(RUN_ID)).write_text(json.dumps({
        **encode_labels(ORG, WELL, "design.pdf", SHA), "origin": "product-upload", "environment": "dev"})), by_hand,
     "environment is 'prd'; the upload's is 'dev'"),
]


@pytest.mark.parametrize("arrange,make,reason", [r[1:] for r in MANUAL_REFUSALS], ids=[r[0] for r in MANUAL_REFUSALS])
def test_each_manual_refusal_names_its_reason_and_writes_only_the_result(store, arrange, make, reason):
    if arrange:
        arrange(store)
    out = submit(store, make())
    assert out["status"] == C.REFUSED and any(r.startswith(reason) for r in out["reasons"]), out["reasons"]
    assert [k for k in store.keys(C.ROOT) if not k.startswith((f"{C.ROOT}runs/", f"{C.ROOT}intake/"))] == [
        C.request_key(CID), C.result_key(CID)]


def test_a_run_past_its_deadline_has_ended(store):
    put_failed(store, running(datetime.now(timezone.utc) - timedelta(seconds=1)))       # stale
    assert submit(store, by_hand(run=running(datetime.now(timezone.utc))))["status"] == C.WRITTEN


def test_the_extracted_path_still_needs_its_entry(store):
    q = {**by_hand(), "entry_method": "extracted"}
    out = submit(store, q)
    assert f"runs/{RUN_ID}/well_entry.json is not there: the run stored no entry" in out["reasons"]


# ---- the app's decisions ----

def test_the_offer_holds_for_a_run_that_ended_with_nothing_to_review():
    def offers(run, entry_stored=False, pdf_found=True):
        return R.by_hand(run, entry_stored=entry_stored, pdf_found=pdf_found)
    soon, gone = datetime.now(timezone.utc) + timedelta(hours=1), datetime.now(timezone.utc) - timedelta(seconds=1)
    assert offers(failed()) and offers(failed(status="succeeded", error=None)) and offers(running(gone))
    assert not offers(running(soon))
    assert not offers(failed(), entry_stored=True) and not offers(failed(), pdf_found=False)
    for missing in ({"organization_id": None}, {"well_id": None}, {"well_id": ""},
                    {"source": {"pdf": {"path": "x", "sha256": None}, "report": None}}, {"source": None}):
        assert not offers(failed(**missing)), missing


def test_the_plain_line_says_when_uploading_again_retries():
    retry = "Uploading the document again retries the extraction."
    for code in ("MODEL_TIMEOUT", "MODEL_ERROR"):
        assert retry in R.failure_words(failed(error={"code": code, "stage": "extract"}))
    for error in ({"code": "PREP_FAILED", "stage": "prep"}, {"code": "REPORT_INVALID", "stage": "stage"}, None):
        assert retry not in R.failure_words(failed(error=error)) and R.failure_words(failed(error=error))


def test_what_is_typed_counts_as_the_writer_counts_it():
    e = empty_entry(ORG, WELL)
    review = R.start(e, failed(), at=AT)
    assert not R.entered(review)
    for key in R.groups(review, e)["not found"]:
        R.settle_empty(review, e, key, at=AT)
    R.settle(review, e, "pump_sections", at=AT)
    assert not R.entered(review)
    q = R.request(review, e, failed(), cid=CID, confirmed_at=AT, confirmed_by="tester", environment="prd")
    assert not W.entered(q) and q["entry_method"] == C.MANUAL
    R.correct(review, e, "bubble_point_psia", at=AT, **{k: PRESSURE[k] for k in ("value", "unit", "source", "note")})
    q = R.request(review, e, failed(), cid=CID, confirmed_at=AT, confirmed_by="tester", environment="prd")
    assert R.entered(review) and W.entered(q)


def test_a_section_alone_counts_as_typed():
    e = empty_entry(ORG, WELL)
    review = R.start(e, failed(), at=AT)
    R.open_editor(review)
    R.add_section(review, 1)
    row = review["editor"]["rows"][0]["id"]
    for field, value in (("pump_model", "ELS1750"), ("stages", 490)):
        R.set_field(review, row, field, value)
    R.save(review, e, at=AT)
    assert R.entered(review) and review["values"]["pump_sections"]["status"] == C.CORRECTED


def test_a_transcription_entered_by_hand_goes_as_manual():
    e = empty_entry(ORG, WELL)
    review = R.start(e, failed(), at=AT, scan=True)
    q = R.request(review, e, failed(), cid=CID, confirmed_at=AT, confirmed_by="tester", environment="prd")
    assert q["entry_method"] == C.MANUAL
    extracted = R.start(entry(), run_record(), at=AT, scan=True)        # a transcription's extraction, as before
    assert R.request(extracted, entry(), run_record(), cid=CID, confirmed_at=AT, confirmed_by="tester",
                     environment="prd")["entry_method"] == "transcription"


def test_the_writers_new_refusals_in_plain_words():
    said = [R.refusal_words(r) for r in (
        f"runs/{RUN_ID}/run.json says 'running': the run has not ended (its deadline_at is 2026-10-28T12:15:00Z)",
        f"runs/{RUN_ID}/well_entry.json is there: this run has an extraction to review",
        "nothing was entered: no value is manually corrected to a value, and the pump string has no section")]
    fallback = R.refusal_words("something the app has not seen")
    assert fallback not in said and len(set(said)) == 3
    assert R.refusal_words("manual entry is not supported yet") == fallback             # that translation is gone


def test_the_pressure_keys_say_absolute_and_how_to_add_a_gauge_reading():
    assert R.PRESSURES == ("bubble_point_psia", "reservoir_pressure_psia", "vapor_pressure_psia")
    assert "14.7" in R.PRESSURE_HELP and "absolute" in R.PRESSURE_HELP and "printed" in R.PRESSURE_HELP


# ---- the app, on screen ----

@pytest.fixture
def offline(tmp_path, monkeypatch, fresh_catalogue):  # noqa: F811 - the fixture above
    """A local store holding the failed run, its PDF where the writer reads it and where the viewer finds it;
    offline writes as `tester`."""
    root = tmp_path / "store"
    objects = C.LocalObjects(root)
    put_failed(objects)
    monkeypatch.setenv(S.STORE_ENV, str(root))
    monkeypatch.setenv(S.PDF_DIRS_ENV, str(root / "intake"))
    monkeypatch.setenv(R.OFFLINE_ENV, "tester")
    monkeypatch.setattr(VALIDATE, "catalogue", lambda: CATALOGUE)
    return objects


def plain_words(at, *typed):
    assert unplain(at, *exempt(failed(), empty_entry(ORG, WELL), None, "tester", *typed)) == []


def enter(at, run_id=RUN_ID):
    at.button(key=f"enter-{run_id}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def write_button(at, run_id=RUN_ID):
    return at.button(key=f"write-{run_id}")


def test_a_scan_offers_entry_by_hand_beside_its_message(offline):
    at = app(run=RUN_ID)
    assert SCAN_WORDS in texts(at.info) and at.button(key=f"enter-{RUN_ID}").label == "Enter by hand"
    assert not at.get("button_group")                                    # the run's page, as before
    for error, retry in (({"code": "MODEL_TIMEOUT", "stage": "extract", "message": "x"}, True),
                         ({"code": "PREP_FAILED", "stage": "prep", "message": "x"}, False)):
        put_failed(offline, failed(error=error))
        at = app(run=RUN_ID)
        said = [i for i in texts(at.info) if i == R.failure_words(failed(error=error))]
        assert said and ("Uploading the document again" in said[0]) == retry and at.button(key=f"enter-{RUN_ID}")
    put_failed(offline, running(datetime.now(timezone.utc) + timedelta(hours=1)))
    assert not [b for b in app(run=RUN_ID).button if b.key == f"enter-{RUN_ID}"]       # not while it runs


def test_entered_by_hand_then_written_then_confirmed(offline):
    at = enter(app(run=RUN_ID))
    [view] = at.get("button_group")
    assert view.options == ["Enter by hand", "Run details (technical)"]
    assert R.BY_HAND in texts(at.info) and "**entered by hand**" in " ".join(texts(at.markdown))
    assert write_button(at).disabled
    assert f"Write is off. {R.NOTHING_ENTERED} 16 values left to check." in texts(at.caption)
    assert at.text_input(key=f"fix-{RUN_ID}-bubble_point_psia-value").help == R.PRESSURE_HELP
    assert not at.text_input(key=f"fix-{RUN_ID}-gas_sg-value").help
    plain_words(at)
    at.text_input(key=f"fix-{RUN_ID}-bubble_point_psia-value").input("2014.7").run()
    at.text_input(key=f"fix-{RUN_ID}-bubble_point_psia-note").input(PRESSURE["note"]).run()
    at.button(key=f"fix-{RUN_ID}-bubble_point_psia-save").click().run()
    assert write_button(at).disabled and "15 values left to check" in texts(at.caption)
    at.button(key=f"batch-{RUN_ID}").click().run()
    plain_words(at, PRESSURE["note"])
    at.button(key=f"dlg-go-{RUN_ID}-string").click().run()
    assert at.button(key=f"dlg-go-{RUN_ID}-not found").label == ("I looked through the document: these 14 values are "
                                                                 "not printed.")
    plain_words(at, PRESSURE["note"])
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    go = at.button(key=f"write-go-{RUN_ID}")
    assert not go.disabled
    plain_words(at, PRESSURE["note"])
    go.click().run()
    assert texts(at.success)[0].startswith("**Written to production**")
    assert at.get("button_group")[0].options == ["Confirmed entry", "Run details (technical)"]
    assert any("Entered by hand: nothing was read from it" in c for c in texts(at.caption))
    plain_words(at, PRESSURE["note"])
    [key] = [k for k in offline.keys(f"{C.ROOT}confirmations/") if k.endswith("request.json")]
    q = C.load(offline.get(key))
    assert q["entry_method"] == C.MANUAL
    assert next(v for v in q["values"] if v["key"] == "bubble_point_psia")["final"] == PRESSURE
    at = app(run=RUN_ID)                                                  # reopened: the confirmed view
    assert at.get("button_group")[0].options[0] == "Confirmed entry"


def test_write_stays_off_until_something_is_typed(offline):
    at = enter(app(run=RUN_ID))
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-string").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()            # every value settled as not printed
    assert at.button(key=f"write-go-{RUN_ID}").disabled
    assert f"Write is off. {R.NOTHING_ENTERED}" in texts(at.warning)
    plain_words(at)
    assert offline.keys(f"{C.ROOT}confirmations/") == []


def test_a_section_added_in_the_editor_turns_write_on(offline):
    at = enter(app(run=RUN_ID))
    assert at.button(key=f"settle-{RUN_ID}-pump_sections").label == "Settle as empty"      # nothing is proposed
    assert not [b for b in at.button if b.key == f"settle-{RUN_ID}-gas_sg"]                  # Settle as empty does it
    at.button(key=f"sections-{RUN_ID}").click().run()
    assert at.button(key=f"ed-{RUN_ID}-reset").label == "Back to no sections"
    at.button(key=f"ed-{RUN_ID}-add").click().run()
    review = at.session_state[f"review-{RUN_ID}"]["review"]
    k = f"ed-{RUN_ID}-{review['editor']['gen']}-{review['editor']['rows'][0]['id']}"
    at.selectbox(key=f"{k}-model").select("ELS1750").run()
    at.number_input(key=f"{k}-stages").set_value(490).run()
    plain_words(at)
    at.button(key=f"ed-{RUN_ID}-save").click().run()
    assert R.entered(at.session_state[f"review-{RUN_ID}"]["review"])
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()                                # the rest not printed
    assert not at.button(key=f"write-go-{RUN_ID}").disabled
    plain_words(at)


def test_a_transcription_entered_by_hand_keeps_its_warning_and_scan_and_goes_as_manual(offline, made):
    scan, typed = made
    source = {"pdf": {"path": "x", "sha256": hashlib.sha256(typed).hexdigest()}, "report": None,
              "source_name": "Design scan - transcription.pdf"}
    put_failed(offline, failed(error={"code": "MODEL_TIMEOUT", "stage": "extract", "message": "x"}, source=source),
               pdf=typed)
    at = enter(app(run=RUN_ID))
    assert any(w.startswith("This document is a transcription typed by Claude") for w in texts(at.warning))
    assert next(b for b in at.get("button_group") if b.key == f"page-{RUN_ID}-shown").value == "Original scan"
    plain_words(at, "Design scan.pdf")
    at.text_input(key=f"fix-{RUN_ID}-bubble_point_psia-value").input("2014.7").run()
    at.button(key=f"fix-{RUN_ID}-bubble_point_psia-save").click().run()
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-string").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to production**")
    [key] = [k for k in offline.keys(f"{C.ROOT}confirmations/") if k.endswith("request.json")]
    assert C.load(offline.get(key))["entry_method"] == C.MANUAL


def test_a_second_run_of_the_document_opens_with_its_values_back(offline):
    at = enter(app(run=RUN_ID))
    at.text_input(key=f"fix-{RUN_ID}-bubble_point_psia-value").input("2014.7").run()
    at.button(key=f"fix-{RUN_ID}-bubble_point_psia-save").click().run()
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-string").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    at.button(key=f"write-go-{RUN_ID}").click().run()
    put_failed(offline, failed(run_id=SECOND), run_id=SECOND)            # the same PDF and well, again a scan
    at = enter(app(run=SECOND), SECOND)
    state = at.session_state[f"review-{SECOND}"]["review"]
    assert state["values"]["bubble_point_psia"]["status"] == C.CORRECTED and state["values"]["bubble_point_psia"]["carried"]
    assert any(c.startswith("Confirmed before: 18 values come back from tester's confirmation of") for c in texts(at.caption))
    assert not write_button(at, SECOND).disabled                        # everything came back, the pressure typed
    plain_words(at)


def test_s3_mode_enters_by_hand_and_the_writer_lambda_writes_it(s3_mode, fresh_catalogue):  # noqa: F811 - fixtures
    """S3 mode: the run as the Lambda leaves a scan (its record and the upload), read through the app's listing, and the
    writer Lambda answering through the trigger."""
    fake = s3_mode
    for key in (C.run_key(RUN_ID, C.ENTRY), C.run_key(RUN_ID, "report.md")):
        del fake.objects[key]
    put_failed(C.S3Objects(fake))
    at = enter(app(run=RUN_ID))
    assert R.BY_HAND in texts(at.info) and "**analyst-1**" in " ".join(texts(at.markdown))
    at.text_input(key=f"fix-{RUN_ID}-bubble_point_psia-value").input("2014.7").run()
    at.button(key=f"fix-{RUN_ID}-bubble_point_psia-save").click().run()
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-string").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to production**")
    [row] = [json.loads(body) for key, body in fake.objects.items() if key.startswith(C.row_prefix("prd"))]
    assert (row["entry_method"], row["design_date"], row["confirmed_by"]) == (C.MANUAL, None, "analyst-1")
    plain_words(at, "analyst-1")
