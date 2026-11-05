"""The transcription route in the app: recognition (app/transcription.py), the upload form, the review of a run whose
source PDF is a transcription (app/review.py, app/main.py), the template-route guard, a broken transcription held at
Write, and the image-only failure's message.  Every transcription is built with the skill's own build script, from
tests/test_transcription_skill.py's synthetic scan; the stored run and the writer's stand-in catalogue are
test_confirm's.  No AWS, no model.

Two optional inputs: with ESP_RUNS_DIR, the guard on every stored template-route entry under it; with
ESP_WELL_ENTRIES_STORE, every one-by-one step of every transcription confirmed in that store parses.
"""

import copy
import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from streamlit.testing.v1 import AppTest

import app.review as R
import app.store as S
import app.transcription as T
import extraction.confirmation as C
from extraction.well_entry import KEYS, NOT_FROM_THE_DOCUMENT
from test_confirm import BLOCKED, CATALOGUE, ORG, PDF, RUN_ID, WELL, entry, put_run, run_record
from test_intake import env, popen  # noqa: F401 - the upload form's fixtures
from test_review import VALIDATE, app, to_check
from test_screens import exempt, ids, unplain
from test_transcription_skill import transcribe
from test_viewer import texts, write_run
from test_viewer import record as viewer_record

PAGES = ["PUMP DATA\n  Model   SF2700\n  Stages  120", "Setting depth  9,850 [?] ft", "[Blank page]"]
NAME = "Design scan.pdf"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def made(tmp_path) -> tuple[bytes, bytes]:
    """(the scan, its transcription): three pages, a reading on page 2 marked unsure."""
    (tmp_path / "skill").mkdir()
    scan, out = transcribe(tmp_path / "skill", PAGES)
    return scan.read_bytes(), out.read_bytes()


def variant(data, *, mark=None, attachments=None, pages=None) -> bytes:
    """The transcription rebuilt with its mark, its attachments or its page count changed."""
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for page in reader.pages[:pages]:
        writer.add_page(page)
    kept = [(n, d) for n, ds in reader.attachments.items() for d in ds] if attachments is None else attachments
    for n, d in kept:
        writer.add_attachment(n, d)
    raw = reader.metadata[T.MARK]
    writer.add_metadata({T.MARK: raw if mark is None else mark if isinstance(mark, str) else json.dumps(mark)})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def changed_mark(data, change) -> dict:
    mark = json.loads(PdfReader(io.BytesIO(data)).metadata[T.MARK])
    change(mark)
    return mark


# ---- recognition ----

def test_a_transcription_is_recognised_from_its_bytes_alone(made):
    scan, typed = made
    got = T.recognise(typed)
    assert got == T.Scan(name=NAME, pages=3, sha256=sha(scan), unsure=(2,), original=scan)
    assert T.recognise(bytes(typed)) is got                         # kept by the PDF's SHA-256
    assert T.recognise(scan) is None and T.recognise(PDF) is None and T.recognise(b"not a pdf") is None


BROKEN = [
    ("its mark is not JSON", lambda t, s: variant(t, mark="{not json"), "its transcription mark cannot be read"),
    ("an unknown schema", lambda t, s: variant(t, mark=changed_mark(t, lambda m: m.update(schema=2))),
     "its transcription mark is not one this app knows"),
    ("an incomplete mark", lambda t, s: variant(t, mark=changed_mark(t, lambda m: m["original"].pop("sha256"))),
     "its transcription mark is incomplete"),
    ("no attachment", lambda t, s: variant(t, attachments=[]), "the original scan is not attached to it"),
    ("another file attached", lambda t, s: variant(t, attachments=[(NAME, s + b"\n")]),
     "the original scan is not attached to it"),
    ("the scan attached twice", lambda t, s: variant(t, attachments=[(NAME, s), ("copy.pdf", s)]),
     "the original scan is attached to it more than once"),
    ("a typed page dropped", lambda t, s: variant(t, pages=2),
     "its pages do not match the original scan's: 2 typed, 3 in the scan attached, 3 in its mark"),
    ("the mark's page count", lambda t, s: variant(t, mark=changed_mark(t, lambda m: m["original"].update(pages=4))),
     "its pages do not match the original scan's: 3 typed, 3 in the scan attached, 4 in its mark"),
]


@pytest.mark.parametrize("make,problem", [b[1:] for b in BROKEN], ids=[b[0] for b in BROKEN])
def test_a_broken_transcription_says_why_in_plain_words(made, make, problem):
    scan, typed = made
    got = T.recognise(make(typed, scan))
    assert got == T.Scan(problem=problem)
    assert ids([R.scan_words(got)]) == []


# ---- the upload form ----

def upload(at, name, data):
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    at.file_uploader(key="new-pdf").upload(name, data, "application/pdf").run()
    return at


def start_button(at):
    return next(b for b in at.button if b.label == "Start the run")


def test_the_form_says_a_transcription_is_one_and_refuses_a_broken_one(env, popen, made):  # noqa: F811
    scan, typed = made
    at = upload(app(upload="1"), "Design scan - transcription.pdf", typed)
    assert ("A transcription of Design scan.pdf, 3 pages typed from the scan. The original scan is inside the file, "
            "and the review shows it beside the values.") in texts(at.info)
    at.checkbox(key="new-confirm").check().run()
    assert not start_button(at).disabled
    at = upload(app(upload="1"), "Design scan - transcription.pdf", variant(typed, attachments=[]))
    assert ("This file is marked as a transcription, but the original scan is not attached to it, so it is refused. "
            "Upload the file exactly as the skill made it.") in texts(at.error)
    assert at.checkbox(key="new-confirm").disabled and start_button(at).disabled
    at = upload(app(upload="1"), "Design scan.pdf", scan)                   # an ordinary PDF: as before
    assert not at.error and not [i for i in texts(at.info) if "transcription" in i]


# ---- the review against the original scan ----

def put_transcription(objects, data, the_entry=None) -> dict:
    run = run_record(source={"pdf": {"path": f"s3://{C.BUCKET}/{C.pdf_key(RUN_ID)}", "sha256": sha(data)},
                             "report": None, "source_name": "Design scan - transcription.pdf"})
    put_run(objects, the_entry or entry(), run=run, pdf=data)
    return run


@pytest.fixture
def reviewing(tmp_path, monkeypatch, made):
    """A local store holding test_confirm's run with the transcription as its PDF; offline writes as `tester`."""
    root = tmp_path / "store"
    objects = C.LocalObjects(root)
    put_transcription(objects, made[1])
    monkeypatch.setenv(S.STORE_ENV, str(root))
    monkeypatch.setenv(S.PDF_DIRS_ENV, str(root / "intake"))
    monkeypatch.setenv(R.OFFLINE_ENV, "tester")
    monkeypatch.setattr(VALIDATE, "catalogue", lambda: CATALOGUE)
    return objects


def plain_words(at, *typed):
    assert unplain(at, *exempt(run_record(), entry(), None, "tester", NAME, *typed)) == []


def shown(at, key) -> str:
    return next(b for b in at.get("button_group") if b.key == key).value


def test_a_transcription_is_reviewed_against_its_original_scan_and_written_as_one(reviewing, made):
    scan, typed = made
    at = app(run=RUN_ID)
    md = " ".join(texts(at.markdown))
    assert "**typed from a scan**" in md and f"**{ORG}**" in md
    assert ("This document is a transcription typed by Claude from the scan's page images. Check every value against "
            "the original scan beside the review. Readings marked unsure: page 2.") in texts(at.warning)
    assert NAME in texts(at.caption)                                        # the original's name
    assert shown(at, f"page-{RUN_ID}-shown") == "Original scan"
    plain_words(at)
    next(b for b in at.get("button_group") if b.key == f"page-{RUN_ID}-shown").set_value("Transcription").run()
    assert not at.exception and shown(at, f"page-{RUN_ID}-shown") == "Transcription"
    # one by one: settled, and settled as empty
    at.button(key=f"settle-{RUN_ID}-oil_sg").click().run()
    at.button(key=f"empty-{RUN_ID}-motor_type").click().run()
    review = at.session_state[f"review-{RUN_ID}"]["review"]
    assert [review["values"][k]["history"][-1]["reason"] for k in ("oil_sg", "motor_type")] == [R.CHECKED] * 2
    # the batches name the original scan, each pop-up's viewer open on it
    at.button(key=f"batch-{RUN_ID}").click().run()
    said = {}
    while (step := at.session_state["dialog"][1]) != "write":
        assert shown(at, f"dlgpage-{RUN_ID}-{step}-shown") == "Original scan"
        plain_words(at)
        said[step] = at.button(key=f"dlg-go-{RUN_ID}-{step}").label
        at.button(key=f"dlg-go-{RUN_ID}-{step}").click().run()
    assert said == {"string": "The string matches the original scan.",
                    "found": "These 2 values match the original scan as printed.",
                    "not found": "I looked through the original scan: these 11 values are not printed."}
    assert "Document: Design scan.pdf, typed from the scan, design dated 2025-06-01." in texts(at.caption)
    plain_words(at)
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to production**")
    plain_words(at)
    assert any("typed from the scan" in c for c in texts(at.caption))      # the confirmed view
    # the request, the version, the row and the design record
    [key] = [k for k in reviewing.keys(f"{C.ROOT}confirmations/") if k.endswith("request.json")]
    q = C.load(reviewing.get(key))
    [row] = [json.loads(reviewing.get(k)) for k in reviewing.keys(C.row_prefix("prd"))]
    [version] = [C.load(reviewing.get(k)) for k in reviewing.keys(f"{C.record_prefix(sha(typed))}versions/")]
    assert q["entry_method"] == row["entry_method"] == version["entry_method"] == "transcription"
    steps = {v["key"]: [s["reason"] for s in v["history"][1:]] for v in version["values"]}
    assert steps["oil_sg"] == steps["motor_type"] == [R.CHECKED]
    assert steps["pump_sections"] == ['batch 1 · string · "The string matches the original scan."']
    assert steps["bubble_point_psia"] == ['batch 2 · found · "These 2 values match the original scan as printed."']
    assert steps["gas_sg"] == ['batch 3 · not found · "I looked through the original scan: these 11 values are not '
                               'printed."']
    assert {v["key"]: v["history"][0]["reason"] for v in version["values"]}["motor_type"] == R.SCAN_REASON
    document = reviewing.get(C.document_key(sha(typed)))
    assert document == typed
    assert sha(PdfReader(io.BytesIO(document)).attachments[NAME][0]) == T.recognise(typed).sha256 == sha(scan)


def test_the_sentences_keep_their_judgement_clauses_on_a_scan():
    e = to_check()
    found = ["oil_sg", "bubble_point_psia", "setting_depth_md_ft"]
    assert R.sentence("found", e, found, scan=True) == (
        "These 3 values match the original scan as printed, with bare-psi pressures taken as absolute, scenario 1 as "
        "the design case and the pump-setting depth for the intake depth.")
    assert R.sentence("found", e, found) == R.sentence("found", e, found, scan=True).replace("the original scan",
                                                                                            "the document")
    assert R.sentence("not found", e, ["motor_type"], scan=True) == ("I looked through the original scan: this 1 "
                                                                     "value is not printed.")
    assert R.sentence("string", e, [R.STRING], anyway=True, scan=True) == R.STRING_ANYWAY


def test_the_document_goes_by_its_scans_name_unless_that_is_an_id():
    run = run_record()                                                      # its own name: design.pdf
    assert R.document_name(run, T.Scan(name=NAME, pages=1)) == NAME
    assert R.document_name(run, T.Scan(name="20261020T175349Z-aa89ff59.pdf", pages=1)) is None
    assert R.document_name(run, T.Scan(name=f"{'ab' * 32}.pdf", pages=1)) is None
    assert R.document_name(run, T.Scan(problem="x")) == R.document_name(run) == "design.pdf"
    assert R.step_words(C.step(C.SETTLED, "tester", "2026-11-04T10:00:00.000001", R.CHECKED)) == (
        "settled one by one against the original scan, by tester, 2026-11-04 10:00 UTC")
    assert R.step_words(C.step(C.CORRECTED, "tester", "2026-11-04T10:00:00.000001", R.CHECKED)).startswith(
        "corrected against the original scan")


def test_an_ordinary_run_is_reviewed_as_before(tmp_path):
    review = R.start(entry(), run_record(), at="2026-11-04T10:00:00.000001")
    R.settle(review, entry(), "oil_sg", at="2026-11-04T10:00:00.000001")
    q = R.request(review, entry(), run_record(), cid="20261104T100000Z-00000001", confirmed_at="2026-11-04T10:00:00.000001",
                  confirmed_by="tester", environment="prd")
    assert review["scan"] is False and review["values"]["oil_sg"]["history"][-1]["reason"] is None
    assert q["entry_method"] == "extracted"


def test_a_broken_transcription_opens_with_a_warning_and_write_stays_off(reviewing, made):
    put_transcription(reviewing, variant(made[1], attachments=[]))
    at = app(run=RUN_ID)
    assert ("This file is marked as a transcription, but the original scan is not attached to it, so Write stays off. "
            "Upload the file exactly as the skill made it, as a new run.") in texts(at.warning)
    assert at.button(key=f"write-{RUN_ID}").disabled
    assert ("Write is off: this file is marked as a transcription, but the original scan is not attached to it. Upload "
            "the file exactly as the skill made it, as a new run.") in texts(at.caption)
    review = at.session_state[f"review-{RUN_ID}"]["review"]
    assert review["scan"] is False and not at.get("button_group")[1:]       # reviewed as read; no switch
    plain_words(at)


# ---- the guard: a transcription read on the template route starts as a generic run does ----

def check_guard(stored, guarded):
    """`guarded`, R.from_scan's copy of the template-route entry `stored`, starts as a generic run would."""
    for name in ("pump_parameters", "wellbore_parameters"):
        assert guarded.get(name) == stored.get(name)
    for old, new in zip(stored["review"]["values"], guarded["review"]["values"]):
        if old["key"] in NOT_FROM_THE_DOCUMENT:
            assert new == old
            continue
        assert new["reasons"] == list(dict.fromkeys([*old["reasons"], R.SCAN_REASON]))
        assert new["status"] == (C.BLOCKS if old["status"] == C.BLOCKS else C.TO_CHECK)
    old_string, new_string = stored["review"]["pump_string"], guarded["review"]["pump_string"]
    assert new_string["status"] == (C.BLOCKS if old_string["status"] == C.BLOCKS else C.TO_CHECK)
    assert R.SCAN_REASON in new_string["reasons"]
    assert all(s["status"] == C.TO_CHECK and s["reasons"][-1] == R.SCAN_REASON for s in new_string["sections"])


@pytest.mark.parametrize("the_entry", [entry(), entry(**BLOCKED)], ids=["settled string", "string blocks publish"])
def test_the_guard_starts_a_template_route_entry_as_a_generic_run(the_entry):
    stored = copy.deepcopy(the_entry)
    guarded = R.from_scan(stored)
    assert stored == the_entry                                              # the stored entry is not changed
    check_guard(stored, guarded)
    review = R.start(guarded, run_record(), at="2026-11-04T10:00:00.000001", scan=True)
    to_decide = {k for g in ("string", "found", "not found") for k in R.groups(review, guarded)[g]}
    assert to_decide == set(KEYS) - set(NOT_FROM_THE_DOCUMENT)
    assert review["values"]["motor_type"]["history"][0] == C.step(C.TO_CHECK, "extraction", "2026-10-28T12:01:00Z",
                                                                  R.SCAN_REASON)
    assert R.in_words(R.SCAN_REASON) == "Typed from a scan: check it against the original."
    generic = copy.deepcopy(the_entry)
    generic["review"]["route"] = "generic"
    assert R.from_scan(generic) is generic                                  # the generic route: nothing changes


RUNS = os.environ.get("ESP_RUNS_DIR", "")


@pytest.mark.skipif(not RUNS, reason="the stored entries need ESP_RUNS_DIR")
def test_the_guard_on_every_stored_template_route_entry():
    seen = 0
    for path in sorted(Path(RUNS).rglob(C.ENTRY)):
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored["review"].get("route") == "template":
            before = copy.deepcopy(stored)
            check_guard(stored, R.from_scan(stored))
            assert stored == before, path                               # the stored entry is not changed
            seen += 1
    assert seen


# ---- the record: every one-by-one step on a transcription parses ----

ROWS = os.environ.get("ESP_WELL_ENTRIES_STORE", "")


def one_by_one_problems(version) -> tuple[int, list[str]]:
    """(the one-by-one steps checked, those that do not parse): after the proposal, a step that is not a batch's, an
    edit's or a carry's is one by one, and its reason is CHECKED; null only for a correction or an accept anyway."""
    checked, problems = 0, []
    for v in version["values"]:
        for step in v["history"][1:]:
            reason = step["reason"]
            if reason is not None and (R.BATCH.fullmatch(reason) or R.EDITED.fullmatch(reason)
                                       or reason.startswith("carried from ")):
                continue
            checked += 1
            if reason is None and not (step["status"] == C.CORRECTED or v.get("accept_anyway")):
                problems.append(f"{v['key']}: a settlement with no reason")
            elif reason is not None and reason != R.CHECKED:
                problems.append(f"{v['key']}: {reason!r}")
    return checked, problems


@pytest.mark.skipif(not ROWS, reason="the confirmed versions need ESP_WELL_ENTRIES_STORE")
def test_every_one_by_one_step_of_a_stored_transcription_parses():
    objects = C.LocalObjects(ROWS)
    versions = [C.load(objects.get(k)) for k in objects.keys(f"{C.ROOT}design-records/") if "/versions/" in k]
    typed = [v for v in versions if v.get("entry_method") == "transcription"]
    if not typed:
        pytest.skip("no transcription is confirmed in this store")
    results = [one_by_one_problems(v) for v in typed]
    assert sum(n for n, _ in results) and [p for _, ps in results for p in ps] == []


def test_the_readme_states_the_one_by_one_reason_and_the_guard():
    readme = (Path(R.__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for said in (f"`{R.CHECKED}`", f"`{R.SCAN_REASON}`", R.in_words(R.SCAN_REASON), R.SCAN_STRING_SENTENCE,
                 "`/RoamTranscription`", "`entry_method` `transcription`"):
        assert said in readme, said


# ---- the image-only failure ----

def test_an_image_only_failure_says_to_transcribe_the_scan(tmp_path, monkeypatch):
    root, rid = tmp_path / "store", "20991231T000000Z-0000abcd"
    error = {"code": "IMAGE_ONLY", "stage": "detect", "details": {"page": 1, "n_chars": 0, "floor": 20},
             "message": "page 1 has no usable text layer (0 non-whitespace characters, floor 20); the document "
                        "cannot be extracted"}
    write_run(root, rid, viewer_record(rid, status="failed", error=error, rows=None, review=None), {})
    monkeypatch.setenv(S.STORE_ENV, str(root))
    words = ("This document is a scan: its pages are pictures, with no text to read, so it cannot be read here. "
             "Transcribe the scan with the transcribe-design-scan skill, then upload the transcription it makes as a "
             "new run.")
    for view in ({"run": rid}, {"watch": rid}):
        at = app(**view)
        assert words in texts(at.info), view
    assert ids([words]) == []
