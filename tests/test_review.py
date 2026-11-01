"""Review and confirm in the app (app/review.py and its views in app/main.py), offline.  The stored run is
test_confirm's synthetic one; its catalogue is test_confirm's stand-in of three names.  The writer is called directly
on a local store, or answers through test_confirm's Lambda trigger on a fake bucket in S3 mode.  No AWS.

Two optional inputs: with ESP_RUNS_DIR, every reason in every stored well_entry.json under it has a plain sentence;
with ESP_MONOREPO_DIR, the labels are #1218's UI labels, read with `git show 6831cbd6:...`.
"""

import copy
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app.review as R
import app.store as S
import extraction.confirm_writer as W
import extraction.confirmation as C
import lambda_function as L
import test_app_s3 as TA
import test_lambda as TL
from extraction.well_entry import KEYS
from test_confirm import BLOCKED, CATALOGUE, ORG, PDF, RUN_ID, SHA, WELL, entry, put_run, run_record
from test_screens import exempt, screen, unplain
from test_validate import report_text
from test_viewer import APP, texts

AT = "2026-10-31T10:00:00.000001"
VALIDATE = sys.modules[W.read_catalogue.__module__]     # extraction.validate.validate: the package's name is a function
RERUN = "20261029T120000Z-0123abce"
# test_confirm's entry, with values still to check: found (with a value) and not found (without one).
FOUND = {"oil_sg": ["varies_by_scenario"], "bubble_point_psia": ["pressure_basis_unstated"],
         "setting_depth_md_ft": ["depth_basis_pump_setting"]}
NOT_FOUND = {"motor_type": ["generic_path"], "gas_sg": ["grounding: not_found on any kept page"]}


def to_check(the_entry=None, **changes) -> dict:
    """An entry with FOUND and NOT_FOUND to check, and `changes` ({key: (status, reasons)}) on top."""
    the_entry = copy.deepcopy(the_entry or entry())
    items = R.reviewed(the_entry)
    for key, reasons in {**FOUND, **NOT_FOUND}.items():
        items[key].update(status=C.TO_CHECK, reasons=reasons)
    for key, (status, reasons) in changes.items():
        items[key].update(status=status, reasons=reasons)
    return the_entry


def keys_of(review, entry_, group):
    return R.groups(review, entry_)[group]


def last(review, key) -> dict:
    return review["values"][key]["history"][-1]


def passes(objects, review, the_entry, run=None, *, by="tester", environment="prd"):
    """The request the review makes keeps the contract, and the writer's checks accept it."""
    run = run or run_record()
    cid = C.new_confirmation_id(datetime.now(timezone.utc))
    q = R.request(review, the_entry, run, cid=cid, confirmed_at=AT, confirmed_by=by, environment=environment)
    assert C.request_problems(q, cid) == []
    assert W.check(objects, q, cid, lambda: CATALOGUE)[0] == []
    return q


def write(objects, review, the_entry, run=None, **kwargs) -> dict:
    run = run or run_record()
    return R.write(objects, review, the_entry, run, environment=kwargs.pop("environment", "prd"),
                   confirmed_by=kwargs.pop("by", "tester"), catalogue=lambda: CATALOGUE, offline=True, **kwargs)


@pytest.fixture
def store(tmp_path):
    objects = C.LocalObjects(tmp_path / "store")
    put_run(objects, to_check())
    return objects


# ---- the status rules ----

def test_settling_a_value_to_check_settles_it_one_by_one(store):
    e = to_check()
    review = R.start(e, run_record(), at=AT)
    assert keys_of(review, e, "found") == [k for k in KEYS if k in FOUND]                  # the entry's order
    assert keys_of(review, e, "not found") == [k for k in KEYS if k in NOT_FOUND]
    R.settle(review, e, "oil_sg", at=AT)
    v = review["values"]["oil_sg"]
    assert (v["status"], v["final"], last(review, "oil_sg")) == (C.SETTLED, R.proposal(e, "oil_sg"),
                                                                  {"status": C.SETTLED, "by": None, "at": AT, "reason": None})
    assert v["history"][0] == {"status": C.TO_CHECK, "by": "extraction", "at": "2026-10-28T12:01:00Z",
                               "reason": "varies_by_scenario"}              # the proposal, as the CLI writes it
    assert "oil_sg" in keys_of(review, e, "decided")


def test_touching_a_value_makes_it_manually_corrected(store):
    e = to_check()
    review = R.start(e, run_record(), at=AT)
    assert R.correct(review, e, "oil_sg", value=0.85, unit="sg", source="design_document", note=" read off p2 ", at=AT) == []
    assert review["values"]["oil_sg"]["final"] == {"value": 0.85, "unit": "sg", "source": "design_document",
                                                   "note": "read off p2"}
    assert review["values"]["oil_sg"]["status"] == C.CORRECTED
    assert R.correct(review, e, "vapor_pressure_psia", value=12.5, unit="psia", source="ops_entered", at=AT) == []
    for key, value, unit, source, problem in (
            ("bubble_point_psia", 1500.0, "psi", "design_document", "Bubble point pressure takes psia only"),
            ("oil_sg", 0.85, "sg", "guess", "the source is the design document or outside it"),
            ("oil_sg", "heavy", "sg", "design_document", "the value is not a number"),
            ("pump_sections", [], "none", "design_document", "Pump string is not corrected here"),
            ("design_document_ref", "s3://x", "none", "design_document", "Design document reference is not corrected here")):
        before = copy.deepcopy(review["values"][key])
        assert problem in R.correct(review, e, key, value=value, unit=unit, source=source, at=AT)
        assert review["values"][key] == before                              # refused: nothing changes
    assert R.parse_value("oil_sg", "1,234.5") == (1234.5, None) and R.parse_value("oil_sg", "nan")[0] is None
    assert R.parse_value("motor_type", " PMM ") == ("PMM", None)
    for key in [*keys_of(review, e, "found"), *keys_of(review, e, "not found")]:
        R.settle(review, e, key, at=AT)
    q = passes(store, review, e)
    assert next(v for v in q["values"] if v["key"] == "oil_sg")["final"]["value"] == 0.85


def test_settling_as_empty_is_settled_when_nothing_was_proposed_else_corrected():
    e = to_check()
    review = R.start(e, run_record(), at=AT)
    R.settle_empty(review, e, "motor_type", at=AT)
    R.settle_empty(review, e, "oil_sg", at=AT)
    assert [(review["values"][k]["status"], review["values"][k]["final"]) for k in ("motor_type", "oil_sg")] == [
        (C.SETTLED, None), (C.CORRECTED, None)]


def test_blocks_publish_clears_only_by_a_passing_correction_or_accept_anyway(store):
    e = to_check(entry(**BLOCKED))
    review = R.start(e, run_record(), at=AT)
    assert R.blocking(review, e, "pump_sections") and keys_of(review, e, "string") == ["pump_sections"]
    with pytest.raises(ValueError, match="blocks publish"):
        R.settle(review, e, "pump_sections", at=AT)
    with pytest.raises(ValueError, match="written reason"):
        R.accept_anyway(review, e, "pump_sections", "  ", at=AT)
    with pytest.raises(ValueError, match="does not block publish"):
        R.accept_anyway(review, e, "oil_sg", "because", at=AT)
    R.accept_anyway(review, e, "pump_sections", " the vendor numbers from the top ", at=AT)
    v = review["values"]["pump_sections"]
    assert (v["status"], v["accept_anyway"], last(review, "pump_sections")["reason"]) == (
        C.SETTLED, "the vendor numbers from the top", None)
    assert keys_of(review, e, "string") == []
    # a scalar that blocks publish: a correction clears it, a settlement does not
    blocked = to_check(e, oil_sg=(C.BLOCKS, ["rule: V-12"]))
    review = R.start(blocked, run_record(), at=AT)
    with pytest.raises(ValueError):
        R.settle(review, blocked, "oil_sg", at=AT)
    R.correct(review, blocked, "oil_sg", value=0.85, unit="sg", source="design_document", at=AT)
    assert review["values"]["oil_sg"]["status"] == C.CORRECTED and "oil_sg" in keys_of(review, blocked, "decided")


def test_a_review_decided_one_by_one_passes_the_writer_and_is_written(store):
    e = to_check(entry(**BLOCKED))
    put_run(store, e)
    review = R.start(e, run_record(), at=AT)
    for key in keys_of(review, e, "found"):
        R.settle(review, e, key, at=AT)
    for key in keys_of(review, e, "not found"):
        R.settle_empty(review, e, key, at=AT)
    with pytest.raises(AssertionError):
        passes(store, review, e)                                    # the string is still to decide
    R.accept_anyway(review, e, "pump_sections", "checked against p2", at=AT)
    q = passes(store, review, e)
    assert q["pump_string"]["accept_anyway"] == "checked against p2"
    assert all(s["by"] == "tester" for item in q["values"] for s in item["history"][1:])
    out = write(store, review, e)
    assert (out["status"], out["reasons"]) == (C.WRITTEN, [])
    version = C.load(store.get(out["version_key"]))
    assert {v["key"]: v["status"] for v in version["values"]}["oil_sg"] == C.SETTLED
    assert R.tally(review) == {"settled one by one": 5, "accepted anyway": 1, "settled by the extraction": 12}


def test_a_value_left_to_check_is_refused_by_name(store):
    e = to_check()
    review = R.start(e, run_record(), at=AT)
    reasons = R.check(store, review, e, run_record(), confirmed_by="tester", environment="prd",
                      catalogue=lambda: CATALOGUE, at=datetime.now(timezone.utc))
    assert reasons == [f"left to check: {', '.join(k for k in KEYS if k in {**FOUND, **NOT_FOUND})}"]
    out = write(store, review, e)
    assert out["status"] == "not sent" and store.keys(f"{C.ROOT}confirmations/") == []


# ---- batches: their steps, tags and pull-outs ----

def test_a_batch_settles_the_ticked_values_with_its_sentence_and_pulls_out_the_rest(store):
    e = to_check()
    review = R.start(e, run_record(), at=AT)
    said = R.sentence("found", e, list(FOUND))
    assert said == ("These 3 values match the document as printed, with bare-psi pressures taken as absolute, "
                    "scenario 1 as the design case and the pump-setting depth for the intake depth.")
    assert R.sentence("found", e, ["oil_sg"]) == ("This 1 value matches the document as printed, with scenario 1 as "
                                                  "the design case.")
    assert R.batch(review, e, "found", set(FOUND), at=AT) == (1, said)
    for key in FOUND:
        assert last(review, key) == {"status": C.SETTLED, "by": None, "at": AT, "reason": f'batch 1 · found · "{said}"'}
    n, said = R.batch(review, e, "not found", {"motor_type"}, at=AT)
    assert (n, said) == (2, "I looked through the document: this 1 value is not printed.")
    assert last(review, "motor_type")["reason"] == 'batch 2 · not found · "I looked through the document: this 1 value is not printed."'
    assert last(review, "gas_sg") == {"status": C.TO_CHECK, "by": None, "at": AT, "reason": "batch 2 · not found · pulled out"}
    assert R.pulled_out(review, "gas_sg") == 2 and keys_of(review, e, "not found") == ["gas_sg"]
    R.settle_empty(review, e, "gas_sg", at=AT)                      # then decided one by one
    assert R.pulled_out(review, "gas_sg") is None
    assert [s["reason"] for s in review["values"]["gas_sg"]["history"][1:]] == ["batch 2 · not found · pulled out", None]
    assert R.tally(review) == {"settled in batches": 4, "pulled out": 1, "settled one by one": 1,
                               "settled by the extraction": 13}
    reasons = [s["reason"] for v in review["values"].values() for s in v["history"][1:] if s["reason"]]
    assert reasons and all(R.BATCH.fullmatch(r) for r in reasons)
    passes(store, review, e)
    assert write(store, review, e)["status"] == C.WRITTEN


def test_the_string_batch_and_what_cannot_be_ticked(store):
    e = to_check(entry(**BLOCKED), oil_sg=(C.BLOCKS, ["rule: V-12"]))
    review = R.start(e, run_record(), at=AT)
    with pytest.raises(ValueError, match="accept it anyway"):
        R.batch(review, e, "string", {"pump_sections"}, at=AT)                      # no reason
    assert R.batch(review, e, "string", {"pump_sections"}, at=AT, reason="vendor order") == (1, "Accept the string anyway")
    v = review["values"]["pump_sections"]
    assert (v["status"], v["accept_anyway"], v["history"][-1]["reason"]) == (
        C.SETTLED, "vendor order", 'batch 1 · string · "Accept the string anyway"')
    with pytest.raises(ValueError, match="cannot be ticked"):
        R.batch(review, e, "found", {"oil_sg", "bubble_point_psia"}, at=AT)
    R.batch(review, e, "found", {"bubble_point_psia"}, at=AT)
    assert len(review["values"]["oil_sg"]["history"]) == 1                          # shown, not ticked, untouched
    assert last(review, "setting_depth_md_ft")["reason"] == "batch 2 · found · pulled out"
    plain = to_check()
    review = R.start(plain, run_record(), at=AT)
    review["values"]["pump_sections"]["status"] = C.TO_CHECK
    assert R.batch(review, plain, "string", set(), at=AT)[0] == 1                   # unticked: pulled out
    assert last(review, "pump_sections")["reason"] == "batch 1 · string · pulled out"
    with pytest.raises(ValueError, match="only a string that blocks publish"):
        R.batch(review, plain, "string", {"pump_sections"}, at=AT, reason="no need")
    R.batch(review, plain, "string", {"pump_sections"}, at=AT)
    assert last(review, "pump_sections")["reason"] == 'batch 2 · string · "The string matches the document."'


# ---- a re-run of a confirmed document: carry-over ----

def rerun(objects, the_entry):
    """A second run of the same document and well."""
    run = run_record(run_id=RERUN, source={"pdf": {"path": "x", "sha256": SHA}, "report": None, "source_name": "d.pdf"})
    objects.put(C.run_key(RERUN, "run.json"), C.dump(run))
    objects.put(C.run_key(RERUN, C.ENTRY), C.dump(the_entry))
    objects.put(C.pdf_key(RERUN), PDF)
    objects.put(C.run_key(RERUN, "report.md"), report_text("F4", {"effective_from": "2025-06-01"}).encode())
    return run


def confirmed_first(store) -> tuple[dict, dict]:
    """The first run confirmed: oil_sg corrected, bubble_point_psia settled, the others settled in batches."""
    e = to_check()
    review = R.start(e, run_record(), at=AT)
    R.correct(review, e, "oil_sg", value=0.85, unit="sg", source="design_document", at=AT)
    R.settle(review, e, "bubble_point_psia", at=AT)
    R.batch(review, e, "found", {"setting_depth_md_ft"}, at=AT)
    R.batch(review, e, "not found", set(NOT_FOUND), at=AT)
    out = write(store, review, e)
    assert out["status"] == C.WRITTEN
    return out, S.LocalRunStore(store.root).latest_version(SHA, ORG, WELL)


def test_a_rerun_with_unchanged_proposals_carries_every_decision(store):
    out, version = confirmed_first(store)
    e2 = to_check()
    run = rerun(store, e2)
    review = R.start(e2, run, version, at=AT)
    cid = out["confirmation_id"]
    assert review["carried_from"] == cid and R.groups(review, e2)["found"] == R.groups(review, e2)["not found"] == []
    oil, bubble = review["values"]["oil_sg"], review["values"]["bubble_point_psia"]
    assert (oil["status"], oil["final"]["value"], oil["moved"]) == (C.CORRECTED, 0.85, False)
    assert bubble["status"] == C.SETTLED
    earlier = {v["key"]: v["history"] for v in version["values"]}
    for key in ("oil_sg", "bubble_point_psia", "motor_type"):
        history = review["values"][key]["history"]
        assert history[:-1] == earlier[key] and history[-1]["reason"] == f"carried from {cid}"
    q = passes(store, review, e2, run)
    assert q["values"][0]["history"][-1]["by"] == "tester"
    assert R.tally(review) == {"carried": 18}
    second = write(store, review, e2, run)
    assert second["status"] == C.WRITTEN


def test_a_rerun_with_moved_proposals_keeps_corrections_and_drops_settlements(store):
    _, version = confirmed_first(store)
    e2 = to_check()
    e2["wellbore_parameters"]["oil_sg"] = {**e2["wellbore_parameters"]["oil_sg"], "value": 0.81}      # moved
    e2["wellbore_parameters"]["bubble_point_psia"] = {**e2["wellbore_parameters"]["bubble_point_psia"], "value": 1600.0}
    run = rerun(store, e2)
    review = R.start(e2, run, version, at=AT)
    oil, bubble = review["values"]["oil_sg"], review["values"]["bubble_point_psia"]
    assert (oil["status"], oil["final"]["value"], oil["moved"], oil["carried"]) == (C.CORRECTED, 0.85, True, True)
    assert R.proposal(e2, "oil_sg")["value"] == 0.81                                # the new reading, shown beside it
    assert (bubble["status"], bubble["carried"], len(bubble["history"])) == (C.TO_CHECK, False, 1)   # as the new entry says
    assert R.groups(review, e2)["found"] == ["bubble_point_psia"]
    R.settle(review, e2, "bubble_point_psia", at=AT)
    passes(store, review, e2, run)


def test_a_settlement_without_a_reason_is_not_carried_onto_a_value_that_now_blocks(store):
    _, version = confirmed_first(store)
    e2 = to_check(bubble_point_psia=(C.BLOCKS, ["rule: V-12"]))
    review = R.start(e2, rerun(store, e2), version, at=AT)
    assert review["values"]["bubble_point_psia"]["status"] == C.BLOCKS


# ---- the environment, who, the older-design warning, the outcome ----

def test_lab_check_writes_to_dev_and_is_refused_in_prd_in_code(tmp_path):
    assert [R.environment_for(o) for o in (R.LAB_CHECK, "org-1", "Lab-Check")] == ["dev", "prd", "prd"]
    objects = C.LocalObjects(tmp_path / "store")
    lab = run_record(organization_id=R.LAB_CHECK)
    e = {**to_check(), "organization_id": R.LAB_CHECK}
    put_run(objects, e, run=lab)
    review = R.start(e, lab, at=AT)
    for key in [*keys_of(review, e, "found"), *keys_of(review, e, "not found")]:
        R.settle(review, e, key, at=AT)
    out = write(objects, review, e, lab, environment="prd")
    assert (out["status"], out["reasons"]) == ("not sent", ["lab-check writes to test only, never to production."])
    assert objects.keys(f"{C.ROOT}confirmations/") == []
    assert R.environment_problem(R.LAB_CHECK, "dev") is None and R.environment_problem("org-1", "prd") is None
    out = write(objects, review, e, lab, environment="dev")
    assert out["status"] == C.WRITTEN and out["row_key"].startswith(C.row_prefix("dev"))


def test_who_confirms(tmp_path):
    def broken():
        raise RuntimeError("no credentials")
    assert R.confirmer("s3", {}, caller=lambda: "analyst-1") == ("analyst-1", "")
    assert R.confirmer("s3", {}, caller=broken) == (None, "Not signed in to AWS, so Write is off: sign in, then "
                                                          "reload the page.")
    assert R.confirmer("local", {}, tmp_path) == (None, "This copy of the app only reviews: Write is off.")
    assert R.confirmer("local", {R.OFFLINE_ENV: "tester"}, tmp_path) == (
        "tester", "Offline: Write saves on this computer only, and nothing reaches production or test.")
    assert R.confirmer("local", {R.OFFLINE_ENV: "tester"}, R.CLI.LAB / "store")[0] is None


@pytest.mark.parametrize("report,current,case", [
    ("2025-06-01", {"design_date": "2026-01-01"}, "older"),
    ("2025-06-01", {"design_date": "2024-01-01"}, "not older"),
    ("2025-06-01", {"design_date": "2025-06-01"}, "not older"),
    ("2025-06-01", {"design_date": None}, "can't compare"),
    (None, {"design_date": "2026-01-01"}, "can't compare"),
    ("2025-06-01", None, None),
], ids=["older", "newer", "same day", "current undated", "incoming undated", "no current entry"])
def test_the_older_design_warning(tmp_path, report, current, case):
    objects = C.LocalObjects(tmp_path)
    objects.put(C.run_key(RUN_ID, "report.md"), report_text("F4", {"effective_from": report} if report else {}).encode())
    assert R.design_dates(objects, RUN_ID, current) == {
        "incoming": report, "current": (current or {}).get("design_date"), "case": case}


def test_a_stale_result_is_written_when_its_row_is_there(store):
    e = to_check()
    review = R.start(e, run_record(), at=AT)
    for key in [*keys_of(review, e, "found"), *keys_of(review, e, "not found")]:
        R.settle(review, e, key, at=AT)

    def stale(objects, cid, wait, *, write_it):
        if write_it:
            W.confirm(objects, C.request_key(cid), deadline=datetime.now(timezone.utc) + timedelta(seconds=60),
                      catalogue=lambda: CATALOGUE)
        return {**C.result(cid, C.RUNNING, at=datetime.now(timezone.utc)), "status": C.STALE}

    out = R.write(store, review, e, run_record(), environment="prd", confirmed_by="tester", catalogue=lambda: CATALOGUE,
                  offline=False, poll=lambda o, cid, wait: stale(o, cid, wait, write_it=True))
    assert (out["status"], out["note"]) == (C.WRITTEN, "written; its result wasn't updated: the entry is saved, and "
                                                       "there is nothing to redo")
    assert out["row_key"] == C.row_key("prd", out["confirmation_id"]) and out["version_key"].endswith(".json")
    out = R.write(store, review, e, run_record(), environment="prd", confirmed_by="tester", catalogue=lambda: CATALOGUE,
                  offline=False, poll=lambda o, cid, wait: stale(o, cid, wait, write_it=False))
    assert out["status"] == C.STALE and "wrote nothing" in out["note"]
    assert R.outcome(store, "20261031T000000Z-00000000", "prd", None)["status"] == "no result"


# ---- what the review shows ----

def test_every_key_has_a_label_and_the_one_unit_1218_allows():
    for key, (_, unit) in KEYS.items():
        assert R.label(key) == R.LABELS[key]
        if key != "pump_sections":
            assert R.allowed_units(key) == [unit]


SAMPLE_REASONS = ["generic_path", "varies_by_scenario", "depth_basis_pump_setting", "pressure_basis_unstated",
                  "pressure_unit_not_printed", "value_not_typed", "conflict: extraction_status = conflict in staging",
                  "frequency_trap: scenario_title_frequency_disagrees = true in this scenario",
                  "expected_missing: staged in every corpus report of this family", "known_issue: F3",
                  "quarantined: composite_value", "grounding: not_found on any kept page",
                  "grounding: other_page (cited p1; found on p6)", "rule: V-06", "rule: V-X3", "rule: V-12", "rule: D16",
                  "model_unresolved: X", "model_absent: X", "model_ambiguous: A, B", "section_incomplete: stages None",
                  "sections: no sections", "V-08: scenario 1: x", "D17: 2 scenarios", "V-09: x", "models: X not in it",
                  "complete: position 1", "review: position 1: rule: V-X3", "head sum: 1 vs TDH 2"]


@pytest.mark.parametrize("reason", SAMPLE_REASONS)
def test_each_reason_code_has_a_plain_sentence(reason):
    said = R.in_words(reason)
    assert said != R.UNKNOWN_REASON and said.endswith(".") and "_" not in said


RUNS = os.environ.get("ESP_RUNS_DIR", "")


@pytest.mark.skipif(not RUNS, reason="the stored entries need ESP_RUNS_DIR")
def test_every_reason_in_the_stored_entries_has_a_plain_sentence():
    unmapped, seen = set(), 0
    for path in Path(RUNS).rglob(C.ENTRY):
        review = json.loads(path.read_text(encoding="utf-8"))["review"]
        reasons = [r for v in review["values"] for r in v["reasons"]]
        reasons += [r for s in review["pump_string"]["sections"] for r in s["reasons"]]
        seen += 1
        unmapped |= {r for r in reasons if R.in_words(r) == R.UNKNOWN_REASON}
    assert seen and not unmapped


@pytest.mark.skipif(not os.environ.get("ESP_MONOREPO_DIR"), reason="#1218's labels need ESP_MONOREPO_DIR")
def test_the_labels_are_1218s():
    source = subprocess.run(["git", "-C", os.environ["ESP_MONOREPO_DIR"], "show",
                             "6831cbd6:v2-ui/utils/text/managementPage.js"], check=True, capture_output=True, text=True,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout
    labels = dict(re.findall(r'^\s+(\w+): "([^"]+)",$', source.split("labels: {", 1)[1].split("}", 1)[0], re.M))
    assert {k: labels[k] for k in KEYS} == R.LABELS


# ---- the app: review mode, the pop-ups, the write, offline and in S3 mode ----

@pytest.fixture
def offline(tmp_path, monkeypatch):
    """A local store holding the synthetic run, its PDF where the writer reads it; offline writes as `tester`."""
    root = tmp_path / "store"
    objects = C.LocalObjects(root)
    put_run(objects, to_check())
    monkeypatch.setenv(S.STORE_ENV, str(root))
    monkeypatch.setenv(S.PDF_DIRS_ENV, str(root / "intake"))
    monkeypatch.setenv(R.OFFLINE_ENV, "tester")
    monkeypatch.setattr(VALIDATE, "catalogue", lambda: CATALOGUE)      # the app reads it at each run
    return objects


def app(**params):
    at = AppTest.from_file(APP, default_timeout=60)
    for name, value in params.items():
        at.query_params[name] = value
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def write_button(at):
    return at.button(key=f"write-{RUN_ID}")


def plain_words(at, the_entry=None, *typed):
    """No id or code on the screen: every engineer screen these tests reach is scanned as tests/test_screens.py
    scans the stored runs."""
    assert unplain(at, *exempt(run_record(), the_entry or to_check(), None, "tester", "analyst-1", *typed)) == []


def test_a_run_with_an_entry_opens_in_review_with_its_files_behind_run_details(offline):
    at = app(run=RUN_ID)
    [view] = at.get("button_group")
    assert view.options == ["Review & confirm", "Run details (technical)"] and not at.tabs
    md = " ".join(texts(at.markdown))
    for shown in (f"**{ORG}**", "**production**", "**Endurance (ELS) report**", "**2025-06-01**", "**tester**",
                  "#### Found: 3", "#### Not found: 2"):
        assert shown in md, shown
    assert "Offline: Write saves on this computer only, and nothing reaches production or test." in texts(at.caption)
    assert write_button(at).disabled and "5 values left to check" in texts(at.caption)
    assert write_button(at).label == "Write to production"
    plain_words(at)
    at.get("button_group")[0].set_value("Run details (technical)").run()
    assert "The pipeline's own files for this run, exactly as it stored them: for checking the pipeline, not the " \
           "design." in texts(at.caption)
    assert [t.label for t in at.tabs] == ["Summary", "Extracted numbers", "Curves", "Findings", "Signals",
                                          "Notices and models", "Logs", "Report"]


def test_local_mode_reviews_without_writing(offline, monkeypatch):
    monkeypatch.delenv(R.OFFLINE_ENV)
    at = app(run=RUN_ID)
    assert write_button(at).disabled
    assert "This copy of the app only reviews: Write is off. 5 values left to check." in texts(at.caption)
    plain_words(at)


def test_one_by_one_then_write_shows_what_was_written(offline):
    at = app(run=RUN_ID)
    for key in FOUND:
        at.button(key=f"settle-{RUN_ID}-{key}").click().run()
    for key in NOT_FOUND:
        at.button(key=f"empty-{RUN_ID}-{key}").click().run()
    assert not write_button(at).disabled
    write_button(at).click().run()
    assert at.button(key=f"write-go-{RUN_ID}").label == "Write to production"
    plain_words(at)
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert not at.exception and texts(at.success)[0].startswith("**Written to production**")
    assert at.get("button_group")[0].options == ["Confirmed entry", "Run details (technical)"]
    assert any("Confirmed**: written to **production** by **tester**" in m for m in texts(at.markdown))
    plain_words(at)
    [row] = offline.keys(C.row_prefix("prd"))
    assert json.loads(offline.get(row))["confirmed_by"] == "tester"


def test_the_batch_pop_ups_write_and_a_pulled_out_value_is_marked(offline):
    put_run(offline, to_check(entry(**BLOCKED)))
    at = app(run=RUN_ID)
    at.button(key=f"batch-{RUN_ID}").click().run()
    go = at.button(key=f"dlg-go-{RUN_ID}-string")
    assert go.label == "Accept the string anyway" and go.disabled                 # a reason first
    plain_words(at, to_check(entry(**BLOCKED)))
    at.text_area(key=f"dlg-anyway-{RUN_ID}").input("the vendor numbers from the top").run()
    at.button(key=f"dlg-go-{RUN_ID}-string").click().run()
    plain_words(at, to_check(entry(**BLOCKED)), "the vendor numbers from the top")
    assert at.button(key=f"dlg-go-{RUN_ID}-found").label.startswith("These 3 values match the document as printed, with")
    at.checkbox(key=f"tick-{RUN_ID}-found-oil_sg").uncheck().run()
    assert at.button(key=f"dlg-go-{RUN_ID}-found").label == ("These 2 values match the document as printed, with "
                                                            "bare-psi pressures taken as absolute and the pump-setting "
                                                            "depth for the intake depth.")
    at.button(key=f"dlg-go-{RUN_ID}-found").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    assert at.button(key=f"write-go-{RUN_ID}").disabled                          # oil_sg was pulled out
    assert any("pulled out of batch 2" in m for m in texts(at.markdown))
    plain_words(at, to_check(entry(**BLOCKED)), "the vendor numbers from the top")
    at.button(key=f"settle-{RUN_ID}-oil_sg").click().run()
    write_button(at).click().run()
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to production**")
    plain_words(at, to_check(entry(**BLOCKED)), "the vendor numbers from the top")
    [key] = [k for k in offline.keys(f"{C.ROOT}confirmations/") if k.endswith("request.json")]
    q = C.load(offline.get(key))
    steps = {item.get("key", "pump_sections"): [s["reason"] for s in item["history"][1:]]
             for item in [*q["values"], q["pump_string"]]}
    assert steps["pump_sections"] == ['batch 1 · string · "Accept the string anyway"']
    assert steps["oil_sg"] == ["batch 2 · found · pulled out", None]
    assert steps["gas_sg"] == ['batch 3 · not found · "I looked through the document: these 2 values are not printed."']


def test_a_rerun_says_what_comes_back_and_from_whose_confirmation(offline):
    _, version = confirmed_first(offline)
    e2 = to_check()
    e2["wellbore_parameters"]["oil_sg"] = {**e2["wellbore_parameters"]["oil_sg"], "value": 0.81}      # moved
    rerun(offline, e2)
    at = app(run=RERUN)
    said = texts(at.caption)
    assert f"Confirmed before: 18 values come back from tester's confirmation of " \
           f"{R.when_words(version['confirmed_at'])}." in said                     # oil_sg's correction too
    assert any(c.startswith("The new run reads 0.81 sg; the correction above is carried") for c in said)
    plain_words(at, e2)


def test_s3_mode_writes_the_request_and_the_writer_lambda_answers(tmp_path, monkeypatch):
    import extraction.confirm as CLI

    class Triggered(TA.FakeS3):          # the app can list; a request's creation invokes the writer
        def get_object(self, *, Bucket, Key):
            if Key not in self.objects:
                self._call("GetObject", Bucket, Key)
                raise TL.client_error(404, "NoSuchKey", "GetObject")
            return super().get_object(Bucket=Bucket, Key=Key)

        def put_object(self, **kwargs):
            super().put_object(**kwargs)
            if C.request_id(kwargs["Key"]):
                L.confirm_handler(TL.s3_event(kwargs["Key"]), TL.Context(60_000), s3=self, table=TL.FakeTable(),
                                  catalogue=lambda: CATALOGUE)

    fake = Triggered()
    put_run(C.S3Objects(fake), to_check())
    for name, value in {S.MODE_ENV: "s3", "ESP_BUCKET": TL.BUCKET, "ESP_INTAKE_PREFIX": TL.INTAKE,
                        "ESP_RUNS_PREFIX": TL.RUNS, "AWS_LAMBDA_FUNCTION_NAME": "esp-design-extraction-writer"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(S, "s3_client", lambda: fake)
    monkeypatch.setattr(S, "_S3_STORES", {})
    monkeypatch.setattr(L, "CONFIRM_WORK", tmp_path / "work")
    monkeypatch.setattr(CLI, "caller", lambda: "analyst-1")
    monkeypatch.setattr(VALIDATE, "catalogue", lambda: CATALOGUE)      # the app reads it at each run
    at = app(run=RUN_ID)
    assert "**analyst-1**" in " ".join(texts(at.markdown))
    plain_words(at)
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-found").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    plain_words(at)
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to production**")
    plain_words(at)
    [key] = [k for k in fake.objects if k.endswith("/request.json")]
    assert C.load(fake.objects[key])["confirmed_by"] == "analyst-1"
    assert [k for k in fake.objects if k.startswith(C.row_prefix("prd"))]
