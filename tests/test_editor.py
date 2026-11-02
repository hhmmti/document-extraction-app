"""Editing the pump string in the app (app/review.py's editor and its view in app/main.py), offline.  The stored run is
test_confirm's synthetic one, and the catalogue its stand-in of three names (plus 400RC1000, without a head curve,
where a gas-handler string needs it).  Each path's request keeps the contract and passes the writer's check, and is
written: (a) a corrected string; (b) gas handlers not in the catalogue, recorded and not written; (c) an order check
failing after an edit, accepted anyway with the edits kept; (d) the String pop-up's Edit sections, then a save; (e) an
empty string built up; (f) a stored order or stage check that passed, failing on the sections; (g) a re-run carrying
a corrected string, then edited again; (h) a catalogue model with no stage count, recorded and not written for a reason.
No AWS.

With ESP_RUNS_DIR and ESP_EXTRACTION_ASSETS_DIR, the replayed corpus entry whose stored V-09 passed and whose sections
fail it is read as stored.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import streamlit as st

import app.review as R
import app.store as S
import extraction.confirm_writer as W
import extraction.confirmation as C
from extraction.assets_dir import ASSETS_ENV
from extraction.catalogue import CatalogueNames
from extraction.well_entry import check, section_value
from test_confirm import BLOCKED, CATALOGUE, ORG, RUN_ID, SHA, WELL, entry, put_run, run_record, section
from test_review import AT, RERUN, app, offline, passes, plain_words, rerun, store, to_check, write  # noqa: F401 - fixtures
from test_viewer import texts

LATER = "2026-10-31T10:05:00.000001"
NAMES = CatalogueNames(CATALOGUE)
GAS = {**CATALOGUE, "400RC1000": False}
WHY = "The two gas handlers are not in the pump catalogue."
TWO = dict(sections=[section(1, "ELS3000", 28, "taper"), section(2, "ELS1750", 490)])
STRING = "pump_sections"


def settle_rest(review, the_entry):
    """Every value but the string, decided one by one."""
    for key in R.groups(review, the_entry)["found"]:
        R.settle(review, the_entry, key, at=AT)
    for key in R.groups(review, the_entry)["not found"]:
        R.settle_empty(review, the_entry, key, at=AT)


def accepted(objects, review, the_entry, catalogue=CATALOGUE, run=None) -> dict:
    """The request keeps the contract and the writer's checks accept it, against `catalogue`."""
    run = run or run_record()
    cid = C.new_confirmation_id(datetime.now(timezone.utc))
    q = R.request(review, the_entry, run, cid=cid, confirmed_at=AT, confirmed_by="tester", environment="prd")
    assert C.request_problems(q, cid) == []
    assert W.check(objects, q, cid, lambda: catalogue)[0] == []
    assert all(set(s) == set(C.SECTION_FIELDS) for s in q["pump_string"]["sections"])
    return q


def string(review) -> dict:
    return review["values"][STRING]


def recorded(version) -> dict:
    """The string's item in a written version: its final, status, accept-anyway reason and history."""
    return next(v for v in version["values"] if v["key"] == STRING)


def ids(review) -> list[str]:
    return [r["id"] for r in review["editor"]["rows"]]


def gas_handlers(review, deepest=1):
    """Two gas handlers added below the string, marked not in the catalogue, and a reason."""
    for printed, stages in (("REDA 400/400 AGH", 234), ("REDA 400/400 MGH", 20)):
        R.add_section(review, deepest)
        new = review["editor"]["rows"][deepest - 1]["id"]
        for field, value in (("absent", True), ("printed", printed), ("stages", stages), ("section_role", "gas_handler")):
            assert R.set_field(review, new, field, value) is None
    review["editor"]["reason"] = f" {WHY} "


# ---- (a) a corrected string, and how a save is counted ----

def test_a_corrected_string_is_written(store):
    e = to_check(entry(**TWO))
    put_run(store, e)
    review = R.start(e, run_record(), at=AT, names=NAMES)
    R.open_editor(review)
    assert ids(review) == ["p1", "p2"] and not R.unsaved(review)
    R.move_section(review, "p2", -1)                                    # ELS1750 to section 1, the deepest
    assert R.set_field(review, "p1", "stages", 30) is None
    R.add_section(review, 3)                                            # a third section, at the top
    new = ids(review)[2]
    R.set_field(review, new, "pump_model", "400 DAL1200")
    R.set_field(review, new, "stages", 12)
    assert R.unsaved(review) and R.save_problems(review) == []
    assert R.save(review, e, at=LATER) == {"status": C.CORRECTED, "by": None, "at": LATER,
                                           "reason": "edited · added 1 · moved 1 · changed stages"}
    v = string(review)
    assert [(s["section_order"], s["pump_model"], s["stages"]) for s in v["sections"]] == [
        (1, "ELS1750", 490), (2, "ELS3000", 30), (3, "400 DAL1200", 12)]
    assert v["final"] == {"value": [section_value(s) for s in v["sections"]], "unit": "none", "source": "design_document"}
    assert review["editor"] is None and not R.blocking(review, e, STRING) and not R.undecided(review, e, STRING)
    settle_rest(review, e)
    q = passes(store, review, e)
    assert q["pump_string"]["status"] == C.CORRECTED
    out = write(store, review, e)
    assert out["status"] == C.WRITTEN
    assert json.loads(store.get(out["row_key"]))["pump_parameters"][STRING] == v["final"]
    version = C.load(store.get(out["version_key"]))
    assert version["pump_string"]["final_sections"] == v["sections"]
    assert [s["reason"] for s in recorded(version)["history"][1:]] == ["edited · added 1 · moved 1 · changed stages"]
    assert R.step_words({**recorded(version)["history"][-1], "by": "tester"}) == (
        "sections edited (1 added; 1 moved; stages changed), by tester, 2026-10-31 10:05 UTC")


def test_counting_a_save_against_the_string_before_it():
    e = entry(**TWO)
    review = R.start(e, run_record(), at=AT, names=NAMES)
    R.open_editor(review)
    assert R.save(review, e, at=LATER) is None and len(string(review)["history"]) == 1     # unchanged: nothing
    R.open_editor(review)
    R.set_field(review, "p1", "stages", 29)
    assert R.unsaved(review)
    R.set_field(review, "p1", "stages", 28)                             # back as it was
    assert not R.unsaved(review)
    for field, value, said in (("stages", 0, "Stages must be a whole number of at least 1."),
                               ("pump_model", "ELS9999", "Model must be a name in the pump catalogue."),
                               ("section_role", "booster", "Role must be primary, taper or gas handler."),
                               ("top_md_ft", float("nan"), "Top MD (ft) must be a depth in feet.")):
        assert R.set_field(review, "p1", field, value, names=NAMES) == said
    R.remove_section(review, "p1")
    R.reset_editor(review, e)                                           # back to the string as read
    assert not R.unsaved(review)
    R.remove_section(review, "p1")
    assert R.save(review, e, at=LATER)["reason"] == "edited · removed 1"
    assert string(review)["status"] == C.CORRECTED
    R.open_editor(review)
    R.reset_editor(review, e)
    step = R.save(review, e, at=LATER)                                  # back as proposed: as it started
    assert step == {"status": C.SETTLED, "by": None, "at": LATER, "reason": "edited · added 1"}
    assert (string(review)["final"], string(review)["sections"]) == (R.proposal(e, STRING), R.sections(e))
    R.open_editor(review)
    for _ in range(3):                                                  # three swaps: one section moved
        R.move_section(review, "p1", 1)
        R.move_section(review, "p1", -1)
    R.move_section(review, "p1", 1)
    R.set_field(review, "p2", "top_md_ft", 9500)
    assert R.save(review, e, at=LATER)["reason"] == "edited · moved 1 · changed top_md_ft"
    assert R.EDITED.fullmatch("edited · added 2 · removed 1 · moved 3 · changed pump_model, stages")


# ---- (b) gas handlers not in the catalogue: recorded, not written ----

def test_b_gas_handlers_not_in_the_catalogue_are_recorded_and_not_written(store):
    e = to_check()
    put_run(store, e)
    review = R.start(e, run_record(), at=AT, names=NAMES)
    R.open_editor(review)
    R.add_section(review, 1)
    R.set_field(review, ids(review)[0], "absent", True)
    assert R.save_problems(review) == [
        "Section 1, the deepest, is marked not in the catalogue: type its model as printed.",
        "A section is not in the catalogue, so the string is recorded and not written: say why."]
    R.reset_editor(review, e)
    gas_handlers(review)
    assert [(r["absent"], r["printed"], r["pump_model"]) for r in review["editor"]["rows"]] == [
        (True, "REDA 400/400 MGH", None), (True, "REDA 400/400 AGH", None), (False, "ELS1750", "ELS1750")]
    step = R.save(review, e, at=LATER)
    assert step["reason"] == (f'edited · added 2 · not in the catalogue: "REDA 400/400 MGH", "REDA 400/400 AGH" · '
                              f'not written: "{WHY}"')
    m = R.EDITED.fullmatch(step["reason"])
    assert [json.loads(q) for q in re.findall(R.QUOTED, m[5])] == ["REDA 400/400 MGH", "REDA 400/400 AGH"]
    assert json.loads(m[6]) == WHY
    v = string(review)
    assert (v["status"], v["final"], v["unwritten"]) == (C.CORRECTED, None, WHY)
    assert [s["pump_model"] for s in v["sections"]] == [None, None, "ELS1750"]
    assert R.unwritten_words(review) == (
        "Recorded, not written: the well's entry gets no pump string, as REDA 400/400 MGH and REDA 400/400 AGH are "
        f"not in the pump catalogue. The reason given: “{WHY}”")
    assert (False, "Marked not in the pump catalogue: section 1, the deepest (REDA 400/400 MGH) and section 2 "
                   "(REDA 400/400 AGH). The string is recorded, not written.") in R.string_checks(
        v["sections"], R.string_rows(review), NAMES)
    settle_rest(review, e)
    passes(store, review, e)
    out = write(store, review, e)
    assert out["status"] == C.WRITTEN
    assert STRING not in json.loads(store.get(out["row_key"]))["pump_parameters"]          # no pump string written
    version = C.load(store.get(out["version_key"]))
    assert version["pump_string"]["final_sections"] == v["sections"]                         # the version keeps it
    assert {c["check"]: c["result"] for c in version["pump_string"]["checks_on_final_sections"]}["models"] == "fail"
    assert R.step_words({**recorded(version)["history"][-1], "by": "tester"}).startswith(
        "sections edited (2 added; not written, 2 sections not in the catalogue), by tester")


# ---- (c) an order check failing after an edit: accepted anyway, the edits kept ----

def edited_against_the_depths(e):
    review = R.start(e, run_record(), at=AT, names=NAMES)
    R.open_editor(review)
    R.set_field(review, "p1", "bottom_md_ft", 9000.0)
    R.set_field(review, "p2", "bottom_md_ft", 9500.0)                   # section 2 deeper than section 1
    assert R.save(review, e, at=LATER)["reason"] == "edited · changed bottom_md_ft"
    return review


def test_c_an_order_check_failing_after_an_edit_is_accepted_anyway_with_the_edits_kept(store):
    e = to_check(entry(**TWO))
    put_run(store, e)
    review = edited_against_the_depths(e)
    assert R.blocking(review, e, STRING) and R.groups(review, e)["string"] == [STRING]
    assert (False, "The sections are not numbered 1 to N from the deepest (the order runs against the depths, where 1 "
                   "must be the deepest). Check the order of the sections in the document.") in R.string_checks(
        string(review)["sections"], R.string_rows(review), NAMES)
    settle_rest(review, e)
    reasons = R.check(store, review, e, run_record(), confirmed_by="tester", environment="prd",
                      catalogue=lambda: CATALOGUE, at=datetime.now(timezone.utc))
    assert len(reasons) == 1 and reasons[0].startswith("pump string: the order check fails")
    edited = [dict(s) for s in string(review)["sections"]]
    R.accept_anyway(review, e, STRING, " the vendor numbers from the top ", at=LATER)
    v = string(review)
    assert (v["status"], v["accept_anyway"], v["sections"]) == (C.CORRECTED, "the vendor numbers from the top", edited)
    assert v["history"][-1] == {"status": C.CORRECTED, "by": None, "at": LATER, "reason": None}
    assert R.groups(review, e)["string"] == []
    q = passes(store, review, e)
    assert q["pump_string"]["accept_anyway"] == "the vendor numbers from the top"
    out = write(store, review, e)
    assert out["status"] == C.WRITTEN
    version = C.load(store.get(out["version_key"]))
    assert version["pump_string"]["final_sections"] == edited
    assert recorded(version)["accept_anyway"] == "the vendor numbers from the top"
    # the same in the String pop-up: accepted anyway there, the edits kept too
    again = edited_against_the_depths(e)
    assert R.batch(again, e, "string", {STRING}, at=LATER, reason="vendor order") == (1, "Accept the string anyway")
    v = string(again)
    assert (v["status"], v["accept_anyway"], v["sections"]) == (C.CORRECTED, "vendor order", edited)
    assert v["history"][-1]["reason"] == 'batch 1 · string · "Accept the string anyway"'


# ---- (d) Edit sections in the String pop-up: pulled out of the batch, then a save ----

def test_d_edit_sections_in_the_string_pop_up_pulls_it_out_then_a_save(offline, fresh_catalogue):
    e = to_check(entry(**BLOCKED))
    put_run(offline, e)
    at = app(run=RUN_ID)
    at.button(key=f"batch-{RUN_ID}").click().run()
    assert at.session_state["dialog"] == (RUN_ID, "string")
    plain_words(at, e)
    at.button(key=f"dlg-sections-edit-{RUN_ID}").click().run()
    assert not at.exception and "dialog" not in at.session_state                    # the pop-up is closed
    review = at.session_state[f"review-{RUN_ID}"]["review"]
    assert [s["reason"] for s in string(review)["history"][1:]] == ["batch 1 · string · pulled out"]
    assert review["editor"] is not None
    g = review["editor"]["gen"]
    assert at.button(key=f"write-{RUN_ID}").disabled and R.UNSAVED in texts(at.caption)
    plain_words(at, e)
    at.number_input(key=f"ed-{RUN_ID}-{g}-p2-stages").set_value(30).run()
    at.button(key=f"ed-{RUN_ID}-save").click().run()
    review = at.session_state[f"review-{RUN_ID}"]["review"]
    assert [s["reason"] for s in string(review)["history"][1:]] == ["batch 1 · string · pulled out",
                                                                   "edited · changed section_order, stages"]
    plain_words(at, e)
    at.button(key=f"batch-{RUN_ID}").click().run()
    assert at.session_state["dialog"] == (RUN_ID, "found")                          # the next group with values
    at.button(key=f"dlg-go-{RUN_ID}-found").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to production**")
    [key] = [k for k in offline.keys(f"{C.ROOT}confirmations/") if k.endswith("request.json")]
    q = C.load(offline.get(key))
    assert [s["reason"] for s in q["pump_string"]["history"][1:]] == ["batch 1 · string · pulled out",
                                                                     "edited · changed section_order, stages"]
    assert [s["by"] for s in q["pump_string"]["history"][1:]] == ["tester", "tester"]
    assert C.request_problems(q, q["confirmation_id"]) == []


# ---- (e) an empty string, built up ----

def test_e_an_empty_string_is_built_up(store):
    e = to_check(entry(sections=[], string_status=C.TO_CHECK, string_reasons=["sections: no sections"]))
    put_run(store, e)
    review = R.start(e, run_record(), at=AT, names=NAMES)
    assert R.unwritten_words(review) == ("As read, this writes no pump string to the well's entry: no sections "
                                            "were read. Edit sections can fix it.")
    R.open_editor(review)
    assert review["editor"]["rows"] == [] and not R.unsaved(review)
    assert R.save_problems(review) == ["Add a section first: a string has at least one, the deepest first."]
    assert R.string_checks([], [], NAMES) == [(False, "No sections: add them from the document, the deepest first.")]
    for model, stages, role in (("ELS1750", 490, "primary"), ("ELS3000", 28, "taper")):
        R.add_section(review, 1)
        new = ids(review)[0]
        for field, value in (("pump_model", model), ("stages", stages), ("section_role", role)):
            R.set_field(review, new, field, value, names=NAMES)
    assert R.save(review, e, at=LATER)["reason"] == "edited · added 2"
    v = string(review)
    assert [(s["section_order"], s["pump_model"]) for s in v["sections"]] == [(1, "ELS3000"), (2, "ELS1750")]
    assert v["status"] == C.CORRECTED and v["final"]["value"] == [section_value(s) for s in v["sections"]]
    settle_rest(review, e)
    passes(store, review, e)
    assert write(store, review, e)["status"] == C.WRITTEN


# ---- (f) a stored check that passed, failing on the sections ----

# The corpus entry's string, as its well_entry.json stores it: a gas handler not in the catalogue, then a grouped
# section of 500 stages whose per-housing list sums to 400.  Its stored V-09 passed.
GROUPED = [section(1, None, 117, "gas_handler", pump_model_as_printed="RC W5-21", match="none", head_curve=None,
                   pump_model_d31=None, stages_per_housing_as_printed=None, top_md_ft=9626.8, bottom_md_ft=9648.6),
           section(None, "400RC1000", 500, housings=4, stages_per_housing_as_printed="100, 100, 100, 100",
                   top_md_ft=9538.8, bottom_md_ft=9626.8, head_curve=False)]
STORED = [check("sections", "pass", "2 sections"), check("V-08", "fail", "scenario 1: section_order is missing or not "
                                                                         "an integer"),
          check("D17", "pass", "one scenario"), check("V-09", "pass", "stages agree with housings"),
          check("conflict", "fail", "extraction_status = conflict in staging (6 rows)"),
          check("models", "fail", "RC W5-21 not in the catalogue"),
          check("complete", "fail", "position 2: section_order None, stages 500"),
          check("review", "fail", "position 1: rule: V-X3"), check("head sum", "not run", "no section prints a head")]


def grouped(status):
    e = to_check(entry(sections=GROUPED, string_status=status, string_reasons=["V-08: x"] if status == C.BLOCKS else []))
    e["review"]["pump_string"]["checks"] = STORED
    return e


def test_f_a_stored_check_that_passed_fails_on_the_sections(tmp_path):
    names = CatalogueNames(GAS)
    e = grouped(C.BLOCKS)
    review = R.start(e, run_record(), at=AT, names=names)
    v = string(review)
    assert (v["status"], v["history"][0]["status"]) == (C.BLOCKS, C.BLOCKS)        # blocks publish: as stored
    checks = R.string_checks(v["sections"], R.string_rows(review), names)
    assert [ok for ok, _ in checks] == [True, False, False, False, False]          # sections, order, stages, models, complete
    assert checks[2][1] == ("Section 2: its stages per housing add up to 400, not its 500 stages. Check the stage "
                            "counts in the document.")
    assert R.document_checks(e) == [("pass", "The document prints one pump string."), ("fail", R.REASONS["conflict"]),
                                    ("fail", R.REASONS["review"]), ("not run", "Not checked: no section prints a head.")]
    settled = grouped(C.SETTLED)                        # had it started settled: to check, with the stage check's reason
    v = string(R.start(settled, run_record(), at=AT, names=names))
    assert (v["status"], v["history"][0]["status"]) == (C.TO_CHECK, C.TO_CHECK)
    assert v["history"][0]["reason"] == "V-09: per-housing stages sum to 400, not stages 500"
    assert string(R.start(settled, run_record(), at=AT))["status"] == C.SETTLED     # no catalogue here: as stored
    # its edits: the gas handler marked, the stages as the schematic prints them (4 housings of 100)
    store_ = C.LocalObjects(tmp_path / "store")
    put_run(store_, e)
    R.open_editor(review)
    R.set_field(review, "p1", "absent", True)
    assert review["editor"]["rows"][0]["printed"] == "RC W5-21"                     # pre-filled as printed
    R.set_field(review, "p2", "stages", 400)
    review["editor"]["reason"] = "The gas handler is not in the pump catalogue."
    assert R.save(review, e, at=LATER)["reason"] == (
        'edited · changed section_order, stages · not in the catalogue: "RC W5-21" · not written: "The gas handler is '
        'not in the pump catalogue."')
    assert [ok for ok, _ in R.string_checks(string(review)["sections"], R.string_rows(review), names)] == [
        True, True, True, False, True]
    settle_rest(review, e)
    accepted(store_, review, e, GAS)
    out = R.write(store_, review, e, run_record(), environment="prd", confirmed_by="tester", catalogue=lambda: GAS,
                  offline=True)
    assert out["status"] == C.WRITTEN


RUNS = os.environ.get("ESP_RUNS_DIR", "")


@pytest.mark.skipif(not (RUNS and os.environ.get(ASSETS_ENV)), reason=f"the stored entry needs ESP_RUNS_DIR and {ASSETS_ENV}")
def test_f_the_replayed_corpus_entry_as_stored():
    from extraction.validate.validate import catalogue
    path = next(Path(RUNS).rglob("20261028T000000Z-d0497041/well_entry.json"))
    e = json.loads(path.read_text(encoding="utf-8"))
    names = CatalogueNames(catalogue())
    review = R.start(e, run_record(), at=AT, names=names)
    v = string(review)
    assert v["status"] == C.BLOCKS == R.reviewed(e)[STRING]["status"]
    stored = {c["check"]: c["result"] for c in e["review"]["pump_string"]["checks"]}
    assert stored["V-09"] == "pass"
    assert (False, "Section 2: its stages per housing add up to 400, not its 500 stages. Check the stage counts in the "
                   "document.") in R.string_checks(v["sections"], R.string_rows(review), names)


# ---- (g) a re-run carrying a corrected string, edited again ----

def test_g_a_rerun_carries_a_corrected_string_and_it_is_edited_again(store):
    e = to_check()
    first = R.start(e, run_record(), at=AT, names=NAMES)
    R.open_editor(first)
    gas_handlers(first)
    R.save(first, e, at=LATER)
    settle_rest(first, e)
    out = write(store, first, e)
    assert out["status"] == C.WRITTEN
    version = S.LocalRunStore(store.root).latest_version(SHA, ORG, WELL)
    e2 = to_check()
    run = rerun(store, e2)
    review = R.start(e2, run, version, at=AT, names=NAMES)
    v = string(review)
    assert (v["status"], v["carried"], v["final"], v["unwritten"]) == (C.CORRECTED, True, None, WHY)
    assert v["sections"] == version["pump_string"]["final_sections"]
    assert [(d["absent"], d["printed"]) for d in v["display"]] == [(True, "REDA 400/400 MGH"), (True, "REDA 400/400 AGH"),
                                                                    (False, None)]
    assert v["history"][-1]["reason"] == f"carried from {out['confirmation_id']}"
    R.open_editor(review)
    assert not R.unsaved(review) and ids(review) == ["c1", "c2", "c3"]
    R.set_field(review, "c3", "stages", 480)
    step = R.save(review, e2, at=LATER)
    assert step["reason"] == (f'edited · changed stages · not in the catalogue: "REDA 400/400 MGH", "REDA 400/400 AGH" '
                              f'· not written: "{WHY}"')
    assert [s["reason"] for s in string(review)["history"][-2:]] == [f"carried from {out['confirmation_id']}",
                                                                    step["reason"]]
    passes(store, review, e2, run)
    second = write(store, review, e2, run)
    assert second["status"] == C.WRITTEN
    assert C.load(store.get(second["version_key"]))["pump_string"]["final_sections"][2]["stages"] == 480


def test_g_the_rerun_on_screen(offline, fresh_catalogue):
    e = to_check()
    first = R.start(e, run_record(), at=AT, names=NAMES)
    R.open_editor(first)
    gas_handlers(first)
    R.save(first, e, at=LATER)
    settle_rest(first, e)
    assert write(offline, first, e)["status"] == C.WRITTEN
    e2 = to_check()
    rerun(offline, e2)
    at = app(run=RERUN)
    said = texts(at.caption)
    assert any(c.startswith("Carried from tester's confirmation of") for c in said)
    assert any("Recorded, not written" in i for i in texts(at.info))
    plain_words(at, e2, WHY, "REDA 400/400 MGH", "REDA 400/400 AGH")


# ---- the editor on screen, in place beside the document ----

# ---- (h) a catalogue model with no stage count: recorded, not written, for a reason ----

NO_COUNT = "The stage count of the ELS1750 is not printed."


def test_h_a_catalogue_model_with_no_stage_count_is_recorded_for_a_reason(store):
    e = to_check(entry(**TWO))
    put_run(store, e)
    review = R.start(e, run_record(), at=AT, names=NAMES)
    R.open_editor(review)
    assert R.set_field(review, "p2", "stages", None) is None                       # the count cleared
    assert R.not_written(review["editor"]["rows"])
    assert R.save_problems(review) == ["A section has no stage count, so the string is recorded and not written: say why."]
    with pytest.raises(ValueError, match="say why"):                               # Save off without a reason
        R.save(review, e, at=LATER)
    review["editor"]["reason"] = f" {NO_COUNT} "
    assert R.save_problems(review) == []                                           # and on with one
    step = R.save(review, e, at=LATER)
    assert step["reason"] == f'edited · changed stages · not written: "{NO_COUNT}"'
    m = R.EDITED.fullmatch(step["reason"])
    assert (m[4], m[5], json.loads(m[6])) == ("stages", None, NO_COUNT)
    v = string(review)
    assert (v["status"], v["final"], v["unwritten"]) == (C.CORRECTED, None, NO_COUNT)
    assert R.unwritten_words(review) == ("Recorded, not written: the well's entry gets no pump string, as a section has "
                                         f"no stage count. The reason given: “{NO_COUNT}”")
    settle_rest(review, e)
    q = passes(store, review, e)
    assert q["pump_string"]["final"] is None and q["pump_string"]["sections"][1]["stages"] is None
    out = write(store, review, e)
    assert out["status"] == C.WRITTEN
    assert STRING not in json.loads(store.get(out["row_key"]))["pump_parameters"]      # no pump string written
    version = C.load(store.get(out["version_key"]))
    assert recorded(version)["history"][-1]["reason"] == step["reason"]
    assert R.step_words({**recorded(version)["history"][-1], "by": "tester"}).startswith(
        "sections edited (stages changed; not written), by tester")
    e2 = to_check(entry(**TWO))                                         # a re-run carries the reason back
    review2 = R.start(e2, rerun(store, e2), S.LocalRunStore(store.root).latest_version(SHA, ORG, WELL), at=AT,
                      names=NAMES)
    assert string(review2)["unwritten"] == NO_COUNT and "The reason given" in R.unwritten_words(review2)


def test_h_on_screen(offline, fresh_catalogue):
    e = to_check(entry(**TWO))
    put_run(offline, e)
    at = app(run=RUN_ID)
    at.button(key=f"sections-{RUN_ID}").click().run()
    k = f"ed-{RUN_ID}-{at.session_state[f'review-{RUN_ID}']['review']['editor']['gen']}"
    assert at.number_input(key=f"{k}-p2-stages").value == 490
    at.number_input(key=f"{k}-p2-stages").set_value(None).run()
    assert at.number_input(key=f"{k}-p2-stages").value is None
    assert at.button(key=f"ed-{RUN_ID}-save").disabled
    assert "A section has no stage count, so the string is recorded and not written: say why." in texts(at.caption)
    plain_words(at, e)
    at.text_area(key=f"{k}-reason").input(NO_COUNT).run()
    assert not at.button(key=f"ed-{RUN_ID}-save").disabled
    at.button(key=f"ed-{RUN_ID}-save").click().run()
    assert (f"Recorded, not written: the well's entry gets no pump string, as a section has no stage count. The reason "
            f"given: “{NO_COUNT}”") in texts(at.info)
    plain_words(at, e, NO_COUNT)


def test_the_readme_states_review_edited():
    readme = (Path(R.__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert f"`{R.EDITED.pattern.replace(R.QUOTED, '<q>')}`" in readme and f"`{R.QUOTED}`" in readme


@pytest.fixture
def fresh_catalogue():
    st.cache_resource.clear()                   # the app keeps the snapshot it read; these tests read the stand-in
    yield
    st.cache_resource.clear()


def test_the_editor_on_screen(offline, fresh_catalogue):
    e = to_check(entry(sections=[section(1, None, 300, pump_model_as_printed="PUMP X 93 STG", match="none", page="p1"),
                                 section(2, "ELS3000", 28, "taper", page="p2")],
                       string_status=C.TO_CHECK, string_reasons=["models: PUMP X 93 STG not in the catalogue"]))
    put_run(offline, e)
    at = app(run=RUN_ID)
    assert ("As read, this writes no pump string to the well's entry: a model is not in the pump catalogue. Edit "
            "sections can fix it.") in texts(at.info)
    assert at.button(key=f"settle-{RUN_ID}-{STRING}")                 # settling as proposed stays allowed
    plain_words(at, e)
    at.button(key=f"sections-{RUN_ID}").click().run()
    g = at.session_state[f"review-{RUN_ID}"]["review"]["editor"]["gen"]
    k = f"ed-{RUN_ID}-{g}"
    assert at.selectbox(key=f"{k}-p1-model").options == sorted(CATALOGUE)
    assert at.selectbox(key=f"{k}-p2-model").options[0] == "ELS3000"                # its model first
    plain_words(at, e)
    at.selectbox(key=f"{k}-p1-model").select("400 DAL1200").run()
    said = texts(at.caption)
    assert "In the pump catalogue, without a head curve." in said and R.UNSAVED in said
    assert at.button(key=f"write-{RUN_ID}").disabled
    at.button(key=f"{k}-p1-p1").click().run()                                       # a page chip turns the main viewer
    assert at.session_state[f"page-{RUN_ID}"] == 1
    at.button(key=f"{k}-p2-up").click().run()
    at.selectbox(key=f"{k}-p2-role").select("gas_handler").run()
    at.number_input(key=f"{k}-p2-bottom").set_value(9900.0).run()
    at.text_input(key=f"{k}-p1-per").input("100, 100, 100").run()
    at.number_input(key=f"{k}-p1-housings").set_value(3).run()
    at.selectbox(key=f"ed-{RUN_ID}-{g}-place-2").select(3).run()
    at.button(key=f"ed-{RUN_ID}-add").click().run()
    review = at.session_state[f"review-{RUN_ID}"]["review"]
    assert ids(review) == ["p2", "p1", "n1"]
    at.button(key=f"{k}-n1-remove").click().run()
    at.checkbox(key=f"{k}-p2-absent").check().run()
    assert at.text_input(key=f"{k}-p2-printed").value == "ELS3000"                  # pre-filled as printed
    assert at.button(key=f"ed-{RUN_ID}-save").disabled                              # a reason first
    plain_words(at, e)
    at.button(key=f"ed-{RUN_ID}-reset").click().run()                               # back to the string as read
    review = at.session_state[f"review-{RUN_ID}"]["review"]
    assert ids(review) == ["p1", "p2"] and review["editor"]["gen"] == g + 1 and not R.unsaved(review)
    g += 1
    at.selectbox(key=f"ed-{RUN_ID}-{g}-p1-model").select("ELS1750").run()
    at.button(key=f"ed-{RUN_ID}-save").click().run()
    review = at.session_state[f"review-{RUN_ID}"]["review"]
    assert review["editor"] is None and string(review)["history"][-1]["reason"] == "edited · changed pump_model"
    assert ["As corrected:"] == [c.value for c in at.caption if c.value == "As corrected:"]
    plain_words(at, e)
    at.button(key=f"sections-{RUN_ID}").click().run()
    at.button(key=f"ed-{RUN_ID}-cancel").click().run()
    assert at.session_state[f"review-{RUN_ID}"]["review"]["editor"] is None
    groups = R.groups(at.session_state[f"review-{RUN_ID}"]["review"], e)
    for key in groups["found"]:
        at.button(key=f"settle-{RUN_ID}-{key}").click().run()
    for key in groups["not found"]:
        at.button(key=f"empty-{RUN_ID}-{key}").click().run()
    assert not at.button(key=f"write-{RUN_ID}").disabled
    at.button(key=f"write-{RUN_ID}").click().run()
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to production**")
    plain_words(at, e)
