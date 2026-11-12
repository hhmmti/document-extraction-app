"""What an engineer's first walk through the app found, as fixed, finding by finding (app/main.py, app/review.py,
app/store.py): the Correct form, the run list's review and side, the cue above the values, a flag's reason, the page at
full size, a value nobody checked, the words and the time zone, and the smaller ones.  Fakes only: no AWS, no network,
invented names.  Each test is named for the finding it holds (W1 to W9).
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import app.review as R
import app.store as S
import extraction.confirmation as C
import test_app_s3 as TA
import test_screens as TS
import test_viewer as TV
from app import hosted
from extraction.intake import encode_labels
from extraction.well_entry import KEYS
from test_app_s3 import fake  # noqa: F401 - a fixture: S3 mode on a fake bucket
from test_confirm import ORG, RUN_ID, SHA, WELL, entry, put_run, run_record, section
from test_editor import LATER, NAMES, fresh_catalogue, gas_handlers, settle_rest  # noqa: F401 - fresh_catalogue: a fixture
from test_hosted import app, off, page, settle_all, unplain  # noqa: F401 - off: a fixture
from test_intake import env, lists, record, run_id, write_run  # noqa: F401 - env: a fixture
from test_manual import enter, failed, put_failed
from test_review import (AT, FOUND, NOT_FOUND, RERUN, confirmed_first, offline, rerun, s3_mode, to_check,  # noqa: F401
                         write)                                                                    # - fixtures
from test_viewer import runs, texts

CHICAGO = "America/Chicago"
PRESSURE = "bubble_point_psia"
KEPT, CHECKED = "Kept as read, not checked", "Settled or corrected"
FLAGGED = "This spelling of the model has not been seen in earlier documents. Check the model against the document."
OLD_WORDS = (r"\bPipeline\b|\bnot run\b|\b[Ll]atest\b|Runs read at|Queued for|\bis sent\b|sent exactly|(?i:\bbatch)"
             r"|every value was typed|as recorded, not written")


def value(at, key, run=RUN_ID) -> dict:
    return at.session_state[f"review-{run}"]["review"]["values"][key]


def labels_of(at, key, run=RUN_ID) -> list[str]:
    """The buttons a value's card offers, by label."""
    return [b.label for b in at.button if b.key and b.key.endswith(f"-{run}-{key}")]


def open_form(at, key, run=RUN_ID):
    at.button(key=f"correct-{run}-{key}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def correct(at, key, typed, note=None, run=RUN_ID):
    open_form(at, key, run)
    at.text_input(key=f"fix-{run}-{key}-value").input(typed).run()
    if note:
        at.text_input(key=f"fix-{run}-{key}-note").input(note).run()
    at.button(key=f"fix-{run}-{key}-save").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def left(at) -> str:
    """The review page's own count, beside Write; `nothing left to check` when it shows none."""
    return next((c for c in texts(at.caption) if c.endswith("left to check")), "nothing left to check")


def write_it(at, run=RUN_ID):
    """Everything left settled in the group checks, then written."""
    at.button(key=f"batch-{run}").click().run()
    while at.session_state["dialog"][1] != "write":
        at.button(key=f"dlg-go-{run}-{at.session_state['dialog'][1]}").click().run()
    at.button(key=f"write-go-{run}").click().run()
    assert texts(at.success)[0].startswith("**Written to "), texts(at.error)
    return at


# ---- W1: the Correct form ----

def test_w1_a_saved_correction_closes_its_form_which_shows_and_saves_only_the_value_it_was_opened_from(offline):  # noqa: F811
    at = app(run=RUN_ID)
    assert not at.text_input and "dialog" not in at.session_state           # no form until one is opened
    open_form(at, "oil_sg")
    open_form(at, PRESSURE)                                                  # another value's: one form, its own
    assert [t.key for t in at.text_input] == [f"fix-{RUN_ID}-{PRESSURE}-value", f"fix-{RUN_ID}-{PRESSURE}-note"]
    assert "Correct Bubble point pressure" in TS.screen(at)                  # W9e: named for its value
    assert any(c.startswith("Read as 1,514.7 psia, printed “1514.7”.") for c in texts(at.caption))
    at.text_input(key=f"fix-{RUN_ID}-{PRESSURE}-value").input("a lot").run()
    at.button(key=f"fix-{RUN_ID}-{PRESSURE}-save").click().run()            # refused: it stays, and saves nothing
    assert texts(at.error) == ["'a lot' is not a number"] and at.session_state["dialog"] == (RUN_ID, f"fix:{PRESSURE}")
    assert value(at, PRESSURE)["status"] == C.TO_CHECK
    at.text_input(key=f"fix-{RUN_ID}-{PRESSURE}-value").input("1750").run()
    at.button(key=f"fix-{RUN_ID}-{PRESSURE}-save").click().run()
    assert "dialog" not in at.session_state and not at.text_input and not at.selectbox      # closed, and gone
    assert "Save the correction" not in [b.label for b in at.button] and not at.error
    assert (value(at, PRESSURE)["status"], value(at, PRESSURE)["final"]["value"]) == (C.CORRECTED, 1750.0)
    untouched = to_check()
    for key in (*FOUND, *NOT_FOUND):
        if key != PRESSURE:                                                  # no other value took anything from it
            assert (value(at, key)["status"], value(at, key)["final"]) == (C.TO_CHECK, R.proposal(untouched, key))
    assert left(at) == "4 values left to check"                              # others are still to check
    open_form(at, PRESSURE)                                                  # opened again: from the value as it stands
    assert at.text_input(key=f"fix-{RUN_ID}-{PRESSURE}-value").value == "1750.0"
    assert at.text_input(key=f"fix-{RUN_ID}-{PRESSURE}-note").value == ""


# ---- W2: the run list ----

def test_w2_the_list_says_a_confirmed_run_and_counts_the_others_as_their_review_page_does(offline):  # noqa: F811
    assert runs(app()).loc[RUN_ID, "Document review"] == left(app(run=RUN_ID)) == "5 values left to check"
    confirmed_first(offline)
    moved = to_check()                                           # a second run reads one value differently: only that
    moved[KEYS[PRESSURE][0]][PRESSURE]["value"] = 1600.0         # one is left, the rest come back from the first
    rerun(offline, moved)
    df = runs(app())
    assert re.fullmatch(r"confirmed: Live wells, \d{4}-\d\d-\d\d", df.loc[RUN_ID, "Document review"])
    assert df.loc[RERUN, "Document review"] == left(app(run=RERUN)) == "1 value left to check"
    assert R.left_to_check(moved, run_record(run_id=RERUN)) == 5                 # without what the first carries
    rerun(offline, to_check())                                   # read the same: everything comes back
    assert runs(app()).loc[RERUN, "Document review"] == left(app(run=RERUN)) == "nothing left to check"


def test_w2_a_run_entered_by_hand_and_confirmed_says_confirmed_not_failed_with_nothing(offline, off):  # noqa: F811
    put_failed(offline, failed(error={"code": "MODEL_TIMEOUT", "stage": "extract", "message": "x"}))
    offline.path(C.run_key(RUN_ID, C.ENTRY)).unlink()
    assert list(app().dataframe[0].value["Document review"]) == ["nothing to review"]
    at = correct(enter(app(run=RUN_ID)), PRESSURE, "2014.7")
    write_it(at)
    shown = app().dataframe[0].value
    assert shown["Run"].tolist() == ["failed"] and re.fullmatch(r"confirmed: Live wells, \d{4}-\d\d-\d\d",
                                                                shown["Document review"][0])


def test_w2_the_list_reads_one_more_object_per_run_within_what_the_role_allows(s3_mode):  # noqa: F811
    fake = s3_mode  # noqa: F811
    other = TA.rid(1)                                            # a second run: failed, with nothing to review
    TA.stored_run(fake, other, record(other, status="failed", error={"code": "MODEL_TIMEOUT", "stage": "extract"}))
    TA.upload(fake, other)
    fake.metadata[C.pdf_key(RUN_ID)] = encode_labels(ORG, WELL, "design.pdf", SHA)      # the run's upload, labelled
    fake.calls.clear()
    at = app()
    assert runs(at)["Document review"].tolist() == ["nothing to review", "5 values left to check"]
    per_run = {run: sorted((op, key.rsplit("/", 1)[-1]) for op, key in fake.calls if run in key)
               for run in (RUN_ID, other)}
    assert per_run == {RUN_ID: [("GetObject", "run.json"), ("GetObject", C.ENTRY), ("HeadObject", f"{RUN_ID}.pdf")],
                       other: [("GetObject", "run.json"), ("HeadObject", f"{other}.pdf")]}
    listed = Counter(key for op, key in fake.calls if op == "ListObjectsV2")
    assert {C.row_prefix("dev"): 1, C.row_prefix("prd"): 1}.items() <= listed.items()      # the rows: listed once
    assert {op for op, _ in fake.calls} == {"ListObjectsV2", "GetObject", "HeadObject"}    # what the role allows,
    assert all(key.startswith(C.ROOT) for _, key in fake.calls)                           # under the app's prefix
    fake.calls.clear()
    at.run()
    assert fake.calls == []                                      # drawn again: nothing is read again
    write_it(app(run=RUN_ID))
    fake.calls.clear()
    df = runs(app())                                             # Write had the list read again
    assert re.fullmatch(r"confirmed: Live wells, \d{4}-\d\d-\d\d", df.loc[RUN_ID, "Document review"])
    reads = [(op, key) for op, key in fake.calls if op != "ListObjectsV2"]
    assert [op for op, key in reads if RUN_ID in key] == []                              # the run's own reads: kept
    assert [key for op, key in reads if key.startswith(C.row_prefix("prd"))] == [
        key for key in fake.objects if key.startswith(C.row_prefix("prd"))]               # the one row, read once


def test_w2_the_count_is_the_reviews_own_start(tmp_path):
    e, run = to_check(), run_record()
    review = R.start(e, run, at=AT)
    assert R.left_to_check(e, run) == sum(len(R.groups(review, e)[g]) for g in R.GROUPS) == 5
    assert [R.left_words(n) for n in (0, 1, 2)] == ["nothing left to check", "1 value left to check",
                                                    "2 values left to check"]
    blocked = to_check(entry(string_status=C.TO_CHECK, string_reasons=["review"]))
    assert R.left_to_check(blocked, run) == 6                    # the string is among them, as on the page


# ---- W3: the side ----

def test_w3_the_list_and_the_watch_page_say_live_or_test_wells_by_the_reviews_rule(fake):  # noqa: F811
    form, pushed, lab = TA.rid(1), TA.rid(2), TA.rid(3)
    TA.upload(fake, form, metadata={**encode_labels(TA.ORG, TA.WELL, "doc.pdf", TA.PDF_SHA), "environment": "prd"})
    TA.upload(fake, pushed, metadata=TA.pushed_labels())                         # the product's, made on its test side
    TA.upload(fake, lab, metadata=encode_labels(R.LAB_CHECK, "bench 1", "doc.pdf", TA.PDF_SHA))    # no label: test
    TA.stored_run(fake, form, record(form, organization_id=TA.ORG, well_id=TA.WELL))
    df = runs(app())
    assert df.loc[[form, pushed, lab], "Wells"].tolist() == ["Live wells", "Test wells", "Test wells"]
    for run, side in ((form, "Live wells"), (pushed, "Test wells"), (lab, "Test wells")):
        store = S.s3_store_from_env()
        org = (store.intake_labels(run) or {})["organization-id"]
        assert side == R.place(R.environment_for(org, store.intake_labels(run)))          # the review page's rule
        assert any(f" · {side} · started " in c for c in texts(app(watch=run).caption))


def test_w3_the_new_run_form_comes_back_on_the_side_last_chosen(env):  # noqa: F811
    at = app(upload="1")
    assert at.get("button_group")[0].value == "Live wells"                       # a session's first visit
    at.get("button_group")[0].set_value("Test wells").run()
    next(b for b in at.button if b.label == "All runs").click().run()
    assert "upload" not in at.query_params
    next(b for b in at.button if b.label == "New run from a PDF").click().run()
    assert at.get("button_group")[0].value == "Test wells"
    assert any(c.startswith("Test wells: ") for c in texts(at.caption))


# ---- W4: nothing left to check waits below the fold with no cue ----

def test_w4_a_line_above_the_values_says_what_is_left_to_check_in_the_list_that_scrolls(offline):  # noqa: F811
    at = app(run=RUN_ID)
    cue = ("Left to check in this list: 3 values found and 2 not found. The list scrolls on its own, so some may be "
           "below what shows.")
    captions = texts(at.caption)
    assert cue in captions and captions.index(cue) < captions.index("1 section as read")     # above the list
    put_run(offline, to_check(entry(string_status=C.TO_CHECK, string_reasons=["review"])))
    assert ("Left to check in this list: the pump string, 3 values found and 2 not found. The list scrolls on its own, "
            "so some may be below what shows.") in texts(app(run=RUN_ID).caption)
    at = settle_all(app(run=RUN_ID))
    assert ("Left to check in this list: the pump string. The list scrolls on its own, so it may be below what shows."
            in texts(at.caption))
    at.button(key=f"settle-{RUN_ID}-{R.STRING}").click().run()
    assert "Nothing in this list is left to check. The list scrolls on its own." in texts(at.caption)


# ---- W5: a flag's reason, whole ----

def flagged_string() -> dict:
    e = to_check(entry(sections=[section(1, "ELS1750", 490, status=C.TO_CHECK, reasons=["rule: V-X3"]),
                                 section(2, "ELS3000", 28, "taper")],
                       string_status=C.TO_CHECK, string_reasons=["review"]))
    e["review"]["pump_string"]["checks"] = [{"check": "review", "result": "fail", "why": "1 section to check"}]
    return e


def test_w5_a_sections_reason_is_under_the_table_whole_on_the_review_and_in_the_group_check(offline):  # noqa: F811
    put_run(offline, flagged_string())
    at = app(run=RUN_ID)
    reason = f"Section 1, the deepest (ELS1750): {FLAGGED}"
    sections = at.dataframe[0].value
    assert list(sections.columns) == ["page", "order", "model", "as printed", "stages", "role", "flag"]   # no cell
    assert sections["flag"].tolist() == ["to check", ""]                                 # holds a reason to cut
    assert texts(at.caption).count(reason) == 1
    assert "Some sections need a check. Check each against its reason, under the table." in " ".join(texts(at.caption))
    at.button(key=f"batch-{RUN_ID}").click().run()                  # the group check: the same table, the same line
    assert at.session_state["dialog"] == (RUN_ID, "string") and texts(at.caption).count(reason) == 2
    assert not [d for d in at.dataframe if "why" in d.value.columns]


# ---- W6: a page at full size ----

def with_pages(objects):
    """The run with a PDF that has pages, its values citing them."""
    source = {"pdf": {"path": "x", "sha256": TV.PDF_SHA}, "report": None, "source_name": "design.pdf"}
    e = to_check()
    R.reviewed(e)[PRESSURE]["page"] = "p2"
    put_run(objects, e, run=run_record(source=source), pdf=TV.PDF)


def captions(at) -> list[str]:
    return [image.proto.imgs[0].caption for image in at.image]


def test_w6_a_page_opens_at_full_size_and_the_page_chips_still_turn_it(offline):  # noqa: F811
    with_pages(offline)
    at = app(run=RUN_ID)
    assert captions(at) == ["Page 1 of 2"]
    at.button(key=f"chip-{RUN_ID}-{PRESSURE}-p2").click().run()                  # a chip turns the pane's page
    assert captions(at) == ["Page 2 of 2"] and at.number_input(key=f"page-{RUN_ID}").value == 2
    at.button(key=f"page-{RUN_ID}-full").click().run()                           # full size: on the page it shows
    assert captions(at) == ["Page 2 of 2", "Page 2 of 2"]
    full = at.number_input(key=f"page-{RUN_ID}-full-page")
    assert (full.value, full.max) == (2, 2) and "design.pdf" in TS.screen(at)
    full.set_value(1).run()                                                      # turned there: the pane follows
    assert at.session_state[f"page-{RUN_ID}"] == 1
    at.button(key=f"chip-{RUN_ID}-{PRESSURE}-p2").click().run()                  # and the chips turn it still
    assert captions(at) == ["Page 2 of 2"]


# ---- W7: a value nobody checked ----

def group_labels(at) -> list[str]:
    return [e.label for e in at.get("expander")]


def test_w7_a_value_nobody_checked_reads_as_kept_as_read_and_one_an_engineer_decided_says_so(offline):  # noqa: F811
    at = app(run=RUN_ID)
    md = " ".join(e.value for e in at.markdown)
    assert group_labels(at) == [f"{KEPT}: 12"] and "Decided" not in page(at)
    assert md.count(":gray-badge[:material/radio_button_unchecked: kept as read]") == 11           # 10 values, the string
    assert md.count(":gray-badge[:material/radio_button_unchecked: not from the document]") == 2
    assert "settled" not in md                                           # nothing reads as settled: no one has
    assert all(R.unchecked(at.session_state[f"review-{RUN_ID}"]["review"], key)
               for key in R.reviewed(to_check()) if key not in (*FOUND, *NOT_FOUND))
    at.button(key=f"settle-{RUN_ID}-oil_sg").click().run()
    correct(at, "motor_nameplate_hp", "150")                             # one of those kept as read
    md = " ".join(e.value for e in at.markdown)
    assert md.count(":green-badge[:material/check_circle: settled]") == 1 and md.count(
        ":blue-badge[:material/edit: corrected]") == 1
    assert group_labels(at) == [f"{KEPT}: 11"]                           # its count, of those still not checked
    assert not R.unchecked(at.session_state[f"review-{RUN_ID}"]["review"], "oil_sg")
    write_it(at)                                                         # the confirmed view: the same words
    table = at.dataframe[0].value.set_index("value")
    assert set(table["status"]) == {"kept as read", "settled", "corrected", "not from the document"}
    assert table.loc["Motor efficiency", ["status", "started as", "how it was decided"]].tolist() == [
        "kept as read", "needed no check", "kept as read"]
    assert table.loc["Oil specific gravity", "status"] == "settled" and table.loc["Oil specific gravity",
                                                                                "started as"] == "to check"
    assert table.loc["Motor nameplate power", "status"] == "corrected"
    assert set(table["started as"]) == {"needed no check", "to check"}
    stored = C.load(offline.get(next(k for k in offline.keys(f"{C.ROOT}design-records/") if "/versions/" in k)))
    assert {v["status"] for v in stored["values"]} == {C.SETTLED, C.CORRECTED}          # as recorded: unchanged


def test_w7_a_second_run_folds_what_an_engineer_decided_apart_from_what_was_kept_as_read(offline):  # noqa: F811
    confirmed_first(offline)
    rerun(offline, to_check())
    at = app(run=RERUN)
    assert group_labels(at) == [f"{CHECKED}: 5", f"{KEPT}: 12"]
    assert texts(at.caption).count("Settled or corrected by an engineer, here or in an earlier confirmation of this "
                                   "document.") == 1


# ---- W8: the words, and the time zone ----

def new_words(at):
    """The page as it stands holds none of the words the walk found meant for a developer."""
    assert not at.exception, [e.value for e in at.exception]
    assert not re.search(OLD_WORDS, page(at)), re.search(OLD_WORDS, page(at))
    return at


def test_w8_the_engineers_pages_hold_none_of_the_old_words(offline, off, monkeypatch):  # noqa: F811
    lists(monkeypatch)                                                   # the form's well lists, from a fake
    listed = new_words(app()).dataframe[0].value                         # the list
    assert list(listed.columns) == ["Open", "Organisation", "Well", "Wells", "Document", "Run", "Document review",
                                    "Started", "For its well", "How it started"]
    assert listed["For its well"].tolist() == ["newest completed run"] and listed["How it started"].tolist() == [
        "not recorded"]                                                  # never blank
    form = new_words(app(upload="1"))                                    # the form, on the test side: a typed well
    form.get("button_group")[0].set_value("Test wells").run()
    form.selectbox(key="new-org").set_value(R.LAB_CHECK).run()
    assert new_words(form).text_input(key="new-typed").help == (
        "Any name is taken, exactly as you type it: check its spelling.")
    new_words(app(watch=RUN_ID))
    at = new_words(app(run=RUN_ID))                                      # the review, its group checks, Write
    at.button(key=f"batch-{RUN_ID}").click().run()
    assert "Group check: values found" in TS.screen(new_words(at))
    at.checkbox(key=f"tick-{RUN_ID}-found-oil_sg").uncheck().run()
    at.button(key=f"dlg-go-{RUN_ID}-found").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    assert new_words(at).session_state["dialog"] == (RUN_ID, "write")
    at.button(key=f"settle-{RUN_ID}-oil_sg").click().run()
    at.button(key=f"write-go-{RUN_ID}").click().run()
    decided = " ".join(new_words(at).dataframe[0].value["how it was decided"])      # written: the confirmed view
    assert "settled in a group check (values found): “These 2 values match the document as printed" in decided
    assert not re.search(r"\d", decided.split("settled in a group check", 1)[1].split(":", 1)[0])    # no number
    new_words(app())


def test_w8_the_group_checks_button_says_what_it_does(offline, off):  # noqa: F811
    at = app(run=RUN_ID)
    button = at.button(key=f"batch-{RUN_ID}")
    assert button.label == "Check group by group" and not button.help    # no tooltip to stay up once it is pressed
    assert any("**Check group by group** does the same a group at a time: the pump string, then the values found, "
               "then those not found." in c for c in texts(at.caption))


def test_w8_a_run_started_here_says_so(env, off, monkeypatch):  # noqa: F811
    import app.intake as intake
    here, by_command = run_id(60), run_id(120)
    for run in (here, by_command):
        write_run(env, run, record(run), {})
    intake.label(env, here, TV.PDF, intake.labels("org-test", "well-a", "doc.pdf", TV.PDF_SHA, "dev"))
    shown = app().dataframe[0].value
    assert shown["How it started"].tolist() == ["Started here, with New run", "not recorded"]
    assert shown["Wells"].tolist() == ["Test wells", "Live wells"]
    assert R.origin_words({"origin": "product-upload", "environment": "dev"}) == R.AUTOMATIC
    assert R.origin_words(None) == R.origin_words({}) == "not recorded"


@pytest.mark.parametrize("reported,name,start,confirmed", [
    (CHICAGO, CHICAGO, "2026-10-28 07:00", "2026-10-31 05:00"),              # UTC-5 on both days
    ("Asia/Kolkata", "Asia/Kolkata", "2026-10-28 17:30", "2026-10-31 15:30"),
    (None, "UTC", "2026-10-28 12:00", "2026-10-31 10:00"),                   # the browser gave none
    ("Not/A_Zone", "UTC", "2026-10-28 12:00", "2026-10-31 10:00"),           # or one not known here
])
def test_w8_every_time_shows_in_the_browsers_time_zone_named_or_in_utc(offline, off, monkeypatch, reported, name,  # noqa: F811
                                                                       start, confirmed):
    monkeypatch.setattr(hosted, "timezone", lambda: reported)
    at = app()
    assert list(at.dataframe[0].value["Started"]) == [start]
    assert json.loads(at.dataframe[0].proto.columns)["Started"]["label"] == f"Started ({name})"     # the zone, named
    assert f"Organisation {ORG} · well Well␣␣#1 · Live wells · started {start} {name}" in texts(app(watch=RUN_ID).caption)
    version = confirm_at(offline, "2026-10-31T10:00:00.000001")
    at = app(run=RUN_ID)
    assert f"Confirmed: written to Live wells on {confirmed} {name}." in page(at)
    decided_ = " ".join(at.dataframe[0].value["how it was decided"])
    assert f", by tester, {confirmed} {name}" in decided_ and ("UTC" in decided_) == (name == "UTC")
    assert runs_review(app()) == [f"confirmed: Live wells, {confirmed[:10]}"]
    assert version["confirmed_at"] == "2026-10-31T10:00:00.000001"           # as recorded: UTC, unchanged
    rerun(offline, to_check())
    at = app(run=RERUN)                                                      # what an earlier confirmation carries
    assert f"Confirmed before: 18 values come back from tester's confirmation of {confirmed} {name}." in texts(at.text)


def confirm_at(objects, at: str) -> dict:
    """The run confirmed, its confirmation's time moved to `at` in the version and the row, as if made then."""
    out, version = confirmed_first(objects)
    for key in (out["version_key"], out["row_key"]):
        doc = C.load(objects.get(key))
        doc["confirmed_at"] = at
        for v in doc.get("values", []):
            for step in v["history"]:
                step["at"] = at if step["by"] != "extraction" else step["at"]
        objects.put(key, C.dump(doc) if key == out["version_key"] else C.row_bytes(doc))
    return C.load(objects.get(out["version_key"]))


def runs_review(at) -> list[str]:
    return list(at.dataframe[0].value["Document review"])


def test_w8_the_words_of_a_time_and_the_sidebar_and_the_forms_read_line(fake, off, monkeypatch):  # noqa: F811
    zone = ZoneInfo(CHICAGO)
    assert R.when_words("2026-11-11T21:36:02.134313", zone) == "2026-11-11 15:36 America/Chicago"
    assert R.when_words("2026-11-11T21:36:02Z") == "2026-11-11 21:36 UTC" == R.when_words(
        datetime(2026, 11, 11, 21, 36, 2, tzinfo=timezone.utc))
    assert R.when_words("2026-11-12T03:10:00.000000", zone, date=True) == "2026-11-11"       # the day, there
    assert R.when_words(None) == R.when_words("soon") == "an unknown time"
    monkeypatch.setattr(hosted, "timezone", lambda: CHICAGO)
    TA.upload(fake, TA.rid(1))
    at = app()
    [said] = texts(at.sidebar.caption)
    assert re.fullmatch(r"The run list is as of \d\d:\d\d America/Chicago\. Refresh reads it again\.", said)
    there = datetime.now(zone)
    assert said.split()[6] in {f"{there:%H:%M}", f"{there.replace(minute=(there.minute - 1) % 60):%H:%M}"}
    form = app(upload="1")
    assert any(re.fullmatch(r"Live wells: \d+ wells, read from the product at \d\d:\d\d America/Chicago on \d+ \w+ "
                            r"\d{4}\.", c) for c in texts(form.caption))
    assert unplain(at, TA.ORG, TA.WELL, "doc.pdf") == [] and "UTC" not in page(at) + page(form)


def test_w8_an_entry_by_hand_says_what_was_typed_not_every_value(offline, off):  # noqa: F811
    put_failed(offline, failed(error={"code": "MODEL_TIMEOUT", "stage": "extract", "message": "x"}))
    offline.path(C.run_key(RUN_ID, C.ENTRY)).unlink()
    at = write_it(correct(enter(app(run=RUN_ID)), PRESSURE, "2014.7"))
    said = next(c for c in texts(at.caption) if c.startswith("Document: design.pdf."))
    assert said == ("Document: design.pdf. Entered by hand: nothing was read from it. Typed from it: 1 value; every "
                    "other value was left empty. No design date.")
    table = at.dataframe[0].value.set_index("value")
    assert table.loc["Bubble point pressure", ["as written", "status"]].tolist() == ["2,014.7 psia", "corrected"]


def test_w8_a_string_kept_on_record_is_not_called_not_written_under_written(offline, off, fresh_catalogue):  # noqa: F811
    e = to_check()
    review = R.start(e, run_record(), at=AT, names=NAMES)
    R.open_editor(review)
    gas_handlers(review)
    R.save(review, e, at=LATER)
    settle_rest(review, e)
    assert write(offline, review, e)["status"] == C.WRITTEN
    at = app(run=RUN_ID)
    assert "Confirmed: written to Live wells on " in page(at)
    assert "#### The pump string, kept on record: not in the well's entry" in texts(at.markdown)
    assert at.dataframe[0].value.set_index("value").loc["Pump string", "as written"] == (
        "kept on record below: not in the well's entry")
    assert "sections edited (2 added; kept on record only, 2 sections not in the catalogue), by tester" in page(at)
    assert "not written" not in page(at)                                 # nowhere under the banner that says written


# ---- W9: the smaller ones ----

def test_w9a_a_value_settled_keeps_its_card_in_place_with_nothing_that_decides_where_the_press_was(offline):  # noqa: F811
    at = app(run=RUN_ID)
    before = [node.key for node in at.main if str(getattr(node, "key", "")).startswith(f"line-{RUN_ID}-")]
    assert labels_of(at, "oil_sg") == ["Settle", "Correct", "Settle as empty"]
    at.button(key=f"settle-{RUN_ID}-oil_sg").click().run()
    after = [node.key for node in at.main if str(getattr(node, "key", "")).startswith(f"line-{RUN_ID}-")]
    assert after == before                                               # no card moved: none came under the pointer
    assert "#### Found: 2 of 3 left to check" in texts(at.markdown) and left(at) == "4 values left to check"
    assert labels_of(at, "oil_sg") == ["Change it"]                      # a second press there decides nothing
    at.button(key=f"again-{RUN_ID}-oil_sg").click().run()
    assert labels_of(at, "oil_sg") == ["Correct", "Settle as empty"]     # its buttons are back, for what it is now
    assert [node.key for node in at.main if str(getattr(node, "key", "")).startswith(f"line-{RUN_ID}-")] == before
    at.button(key=f"empty-{RUN_ID}-gas_sg").click().run()                # the same for Settle as empty
    assert labels_of(at, "gas_sg") == ["Change it"] and labels_of(at, "oil_sg") == ["Correct", "Settle as empty"]
    assert "#### Not found: 1 of 2 left to check" in texts(at.markdown)
    next(b for b in at.button if b.label == "All runs").click().run()    # a new visit: each where it now belongs
    at.query_params["run"] = RUN_ID
    at.run()
    assert "#### Found: 2" in texts(at.markdown) and "#### Not found: 1" in texts(at.markdown)
    assert group_labels(at) == [f"{CHECKED}: 2", f"{KEPT}: 12"]


def test_w9a_the_pump_string_settled_shows_no_button_where_settle_was(offline):  # noqa: F811
    put_run(offline, flagged_string())
    at = app(run=RUN_ID)
    assert [b.label for b in at.button if b.key in (f"settle-{RUN_ID}-{R.STRING}", f"sections-{RUN_ID}")] == [
        "Settle as proposed", "Edit sections"]
    at.button(key=f"settle-{RUN_ID}-{R.STRING}").click().run()
    assert labels_of(at, R.STRING) == ["Change it"] and not [b for b in at.button if b.key == f"sections-{RUN_ID}"]
    at.run()
    assert [b.label for b in at.button if b.key == f"sections-{RUN_ID}"] == ["Edit sections"]


def test_w9b_a_corrected_value_shows_its_correction_and_note_and_offers_no_bare_settle(offline):  # noqa: F811
    at = correct(app(run=RUN_ID), PRESSURE, "1750", "the vendor's sheet prints 1,750 psia")
    said = texts(at.caption)
    assert ("Corrected from 1,514.7 psia, as read. Note with the correction: “the vendor's sheet prints 1,750 psia”"
            in said)
    assert any(c.startswith("About the value as read: Printed in psi without saying gauge or absolute") for c in said)
    assert labels_of(at, PRESSURE) == ["Correct", "Settle as empty", "Back to the value as read"]
    at.button(key=f"settle-{RUN_ID}-{PRESSURE}").click().run()           # which does what it says
    assert (value(at, PRESSURE)["status"], value(at, PRESSURE)["final"]["value"]) == (C.SETTLED, 1514.7)
    correct(at, "gas_sg", "0.7")                                         # nothing was read: nothing to go back to
    assert "Corrected: nothing was read." in texts(at.caption)
    assert labels_of(at, "gas_sg") == ["Correct", "Settle as empty"]
    at.button(key=f"empty-{RUN_ID}-oil_sg").click().run()                # a value emptied is a correction too
    at.run()
    assert "Corrected from 0.8251 sg, as read." in texts(at.caption)
    assert labels_of(at, "oil_sg") == ["Correct", "Back to the value as read"]
    assert ":gray[*empty*]" in [e.value for e in at.markdown]


def test_w9c_the_review_and_the_confirmed_view_agree_on_the_design_document_reference(offline):  # noqa: F811
    at = app(run=RUN_ID)
    assert ":gray[*Write fills it in*]" in [e.value for e in at.markdown]
    write_it(at)
    row = at.dataframe[0].value.set_index("value").loc["Design document reference"]
    assert row[["as written", "from", "status", "how it was decided"]].tolist() == [
        "filled in by Write", "Write", "not from the document", "it points to this confirmation's record"]
    assert "not found" not in row.tolist()


def test_w9d_a_run_has_one_start_time_on_the_list_and_on_its_watch_page(env, off):  # noqa: F811
    run = "20990101T000001Z-0000000a"                                    # uploaded at 00:00:01
    write_run(env, run, record(run, started_at="2099-01-01T00:07:30Z"), {})       # picked up later
    assert list(app().dataframe[0].value["Started"]) == ["2099-01-01 00:00"]
    assert "Organisation org-test · well well-a · Live wells · started 2099-01-01 00:00 UTC" in texts(
        app(watch=run).caption)
    odd = "a-run-named-by-hand"                                          # no time in its id: the record's own start
    write_run(env, odd, record(odd, started_at="2099-01-01T00:07:30Z"), {})
    assert "2099-01-01 00:07" in list(app().dataframe[0].value["Started"])
