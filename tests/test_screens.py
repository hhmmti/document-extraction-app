"""No identifier or code on an engineer's screen.  The review mode (its header, groups, value lines and string block),
the four pop-ups, the Write pop-up, the string's editor and the confirmed view, with every message on them, say what
was noticed and what to check in plain words.  Run details (technical) keeps the pipeline's own files and is not
scanned.

    pytest -m store tests/test_screens.py    every run with an entry in ESP_RUN_STORE_DIR: its review mode, each
                                             pop-up and the Write pop-up, then the string's editor (a section without
                                             a model marked not in the catalogue, and the string saved so); or, once
                                             written, its confirmed view

The rest needs no input: every message the app makes, scanned the same way.  What a scan leaves out (`exempt`): the
organisation and the well, as the well list names them; pump models, the catalogue's names among them; values and
text as printed; the document's file name; who confirms; and what the engineer typed.
"""

import json
import os
import re
from pathlib import Path

import pandas as pd
import pytest
from google.protobuf.json_format import MessageToDict
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.element_tree import Dataframe

import app.review as R
import extraction.confirmation as C
from extraction.catalogue import CatalogueNames
from extraction.validate.validate import catalogue as read_catalogue
from extraction.well_entries_schema import VALUE_TYPE

APP = str(Path(__file__).resolve().parents[1] / "app" / "main.py")
PATTERNS = {
    "rule or check id": r"\bV-X?\d+\b|\bD\d{2}\b",
    "family code": r"\bF\d\b",
    "snake_case key": r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b",
    "code": r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b",
    "null": r"\b(?:None|null|NaN)\b",
    "run or confirmation id": r"\b\d{8}T\d{6}Z\b",
    "hex hash": r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,}\b",
    "position": r"\bposition \d+",
    "prd or dev": r"(?i)\b(?:prd|dev)\b",
    "object key or file": r"\b(?:confirmations|design-records|well-entries|runs|intake)/|environment=|\b[\w.-]+\."
                          r"(?:json|jsonl|parquet|md)\b",
    "ticket": r"#\d+",
    "exception": r"\b\w+(?:Error|Exception)\b",
    "internal word": r"(?i)\b(?:alias(?:es)?|seeds?|staging|staged|grains?|routes?|quarantin\w*|extraction[ _]status"
                     r"|kept pages?|templates?|generic|grounding|envelope|contract|writer|S3|Lambda|STS|bucket"
                     r"|design record)\b",
}
VISIBLE = {"label", "body", "help", "placeholder", "options", "title", "caption", "content", "value", "message",
           "delta"}
ICON = re.compile(r":material/[a-z0-9_]+:")
DIRECTIVE = re.compile(r":(?:red|orange|yellow|green|blue|violet|gray|grey|rainbow|primary)(?:-badge|-background)?\[")
REASON = "Checked against page 3 of the document."


# ---- the scan ----

def strings(value, field=None):
    """The visible strings of a proto as a dict: those under VISIBLE's field names, at any depth."""
    if isinstance(value, dict):
        for name, inner in value.items():
            yield from strings(inner, name)
    elif isinstance(value, list):
        for inner in value:
            yield from strings(inner, field)
    elif isinstance(value, str) and field in VISIBLE:
        yield value


def screen(at) -> list[str]:
    """Every string the page shows: text, labels, help, options, a table's columns and cells, pop-up and expander
    titles.  Icons and Markdown syntax are not text."""
    out = []
    for node in at._tree:
        if isinstance(node, Dataframe):
            df = node.value
            out += [str(c) for c in df.columns] + [str(v) for v in df.to_numpy().ravel() if not pd.isna(v)]
        elif getattr(node, "proto", None) is not None:
            out += strings(MessageToDict(node.proto, preserving_proto_field_name=True))
    return [normalise(s) for s in out]


def normalise(text: str) -> str:
    text = DIRECTIVE.sub("", ICON.sub("", text))
    text = re.sub(r"(?<!\\)[\]*]", "", text)            # a directive's closing bracket; emphasis
    return re.sub(r"\\(.)", r"\1", text).replace(" ", " ")


def exempt(record=None, entry=None, version=None, *more) -> list[str]:
    """What a scan leaves out for one run, longest first."""
    record, out = record or {}, set(more)
    out |= {str(record.get("organization_id") or ""), str(record.get("well_id") or ""), R.document_name(record) or ""}
    review = (entry or {}).get("review") or {}
    for s in [*((review.get("pump_string") or {}).get("sections") or []),
              *(((version or {}).get("pump_string") or {}).get("final_sections") or [])]:
        out |= {str(s.get(k) or "") for k in ("pump_model", "pump_model_as_printed")}
    reasons = [r for s in (review.get("pump_string") or {}).get("sections") or [] for r in s.get("reasons") or []]
    for v in review.get("values") or []:
        reasons += v.get("reasons") or []
        out |= {str(v.get("printed") or "")} | set(re.findall(r'"([^"]*)"', str(v.get("note") or "")))
        out |= {v["value"]} if isinstance(v.get("value"), str) else set()
    for name in ("pump_parameters", "wellbore_parameters"):
        out |= {e["value"] for e in ((entry or {}).get(name) or {}).values()
                if isinstance(e, dict) and isinstance(e.get("value"), str)}
    for reason in reasons:                              # the models a reason names: pump models
        code, _, detail = reason.partition(": ")
        if code in ("model_unresolved", "model_absent", "model_ambiguous"):
            out |= {detail, *detail.split(", ")}
        elif code == "models":
            for m in re.finditer(r"(?:position \d+: )?(.+?) (?:not in the catalogue|ambiguous: ([^;]+))(?:; |$)", detail):
                out |= {m[1], *(m[2] or "").split(", ")}
    return sorted((x for x in out if x.strip()), key=len, reverse=True)


def ids(texts, leave_out=()) -> list[str]:
    """`<pattern>: <match> in "<text>"` for every id or code left in `texts` once `leave_out` is taken out."""
    hits, whole = [], set(leave_out)
    for text in texts:
        if text in whole:                               # all of it left out, as a catalogue name in a pick's options
            continue
        rest = text
        for name in leave_out:
            rest = rest.replace(name, " ")
        hits += [f"{kind}: {m[0]!r} in {text[:160]!r}" for kind, pattern in PATTERNS.items()
                 for m in re.finditer(pattern, rest)]
    return hits


def unplain(at, *leave_out) -> list[str]:
    assert not at.exception, [e.value for e in at.exception]
    return ids(screen(at), leave_out)


# ---- every message the app makes ----

SAMPLE_REASONS = [
    "generic_path", "varies_by_scenario", "depth_basis_pump_setting", "pressure_basis_unstated",
    "pressure_unit_not_printed", "value_not_typed", "conflict", "conflict: extraction_status = conflict in staging",
    "frequency_trap", "frequency_trap: scenario_title_frequency_disagrees = true in this scenario", "expected_missing",
    "known_issue: F3", "model_unresolved: REDA 400/400 AGH", "model_absent: ELS1234", "model_ambiguous: SF2700, SF2700X",
    "section_incomplete: stages None", "section_incomplete: section_order None at position 2", "sections",
    "V-08", "V-08: scenario 1: section_order is missing or not an integer",
    "V-08: scenario None: section_order runs against the depth columns (1 must be deepest)",
    "V-08: scenario 2: section_order is not a contiguous 1..3", "D17", "D17: 2 scenarios present different pump strings",
    "V-09", "V-09: housing_count 2 × 100 per housing ≠ stages 300", "models", "models: UNBGAS not in the catalogue",
    "models: position 1: SF2700 ambiguous: SF2700, SF2700X; position 2: no model; position 3: SF4300 not in the catalogue",
    "complete", "complete: position 1: section_order 1, stages None; position 2: section_order None, stages None",
    "review", "review: position 1: rule: V-X3; position 2: grounding: not_found on any kept page", "head sum",
    "head sum: 1 vs TDH 2", "rule: V-06", "rule: V-X3", "rule: V-12", "rule: D16", "rule: V-08", "rule: D17",
    "rule: V-99", "grounding: not_found on any kept page", "grounding: other_page (cited p1; found on p2, p6)",
    "quarantined: empty_string", "quarantined: composite_value", "quarantined: unrecognized_unit_token",
    "quarantined: something_new", "a_code_never_seen"]
SAMPLE_NOTES = [
    "not from the document: points to the design record", "not from the document: entered by hand",
    "intake; depth_reference_basis intake", "intake; pump setting 11099 ft; depth_reference_basis pump_setting",
    "pump setting, not intake; depth_reference_basis pump_setting", "intake; depth_reference_basis pump_discharge",
    "intake; depth_reference_basis kelly_bushing", "value at design load", "MD", "basis: operating",
    "basis: hydrocarbon_only", "from API 38 API: 141.5 / (131.5 + API)", "printed as SG",
    'printed "1800.00" with unit "psi"; basis unstated; provenance vendor input; calculated No',
    'printed "1,500 PSIA"; provenance vendor assumption; equals static true',
    'printed "19985.3 psig"; +14.7; provenance vendor derived', 'printed "3,000"; no unit printed; datum 10,400 ft',
    'printed "1800 psi"; basis unstated; provenance vendor assumption; correlation Regional PVT: United States',
    'printed "2,500 PSIA"; provenance vendor_estimate; equals static False', "section order as printed, 1 = deepest",
    "some_new_note_part"]
RUN, CID = "20261028T000000Z-01a29d17", "20261031T201922Z-f43c2295"
SHA = "4e7fbd14e8e0e2545ad0da804f4c1ca7441ad4dce07bc6bf45e0b34460c807e0"
# The writer's refusals (extraction/confirm_writer.py), the contract's (extraction/confirmation.py) and the app's.
SAMPLE_REFUSALS = [
    f"contract: missing field pump_string", f"contract: run_id {RUN!r} is not in its format",
    "manual entry is not supported yet", "entry_method 'typed' is not extracted or transcription",
    "environment 'qa' is not dev or prd", f"runs/{RUN}/run.json is not there",
    f"runs/{RUN}/run.json is not there (S3 answered 403 for x/runs/{RUN}/run.json: the writer cannot list)",
    "organization_id is 'org-1'; the run's is 'org-2'", "well_id is 'A 1H'; the run's is 'A 2H'",
    f"document_sha256 is {SHA!r}; the run's is {SHA[::-1]!r}", f"runs/{RUN}/well_entry.json is not there: the run "
    "stored no entry", f"intake/{RUN}.pdf is not there", f"intake/{RUN}.pdf is not the document: its SHA-256 is {SHA}",
    "values missing: oil_sg, gas_sg", "values not in the entry: tubing_size", "left to check: oil_sg, bubble_point_psia",
    "changed from the proposal but not marked manually corrected: oil_sg",
    "bubble_point_psia blocks publish: neither corrected so that its check passes, nor accepted anyway",
    "pump string: the order check fails (scenario None: section_order is not a contiguous 1..2), with no "
    "accept-anyway reason", "pump string: models: position 1: X not in the catalogue; and it is neither settled nor "
    "manually corrected", "pump string: its final value is not its final sections",
    "design_document_ref is the writer's to set: leave it empty", "pump_model 'SF 2700' is not a catalogue spelling: "
    "the catalogue spells it 'SF2700'", "pump_model 'XYZ' is not a catalogue spelling",
    "#1218: pump_parameters.setting_depth_md_ft: unknown unit 'm'",
    "#1218: wellbore_parameters.oil_sg: a number must state a real unit, not 'none'",
    "#1218: pump_parameters.motor_nameplate_hp: a hp must be a whole number, got 12.5",
    "#1218: pump_parameters.motor_type: a non-numeric value must use unit 'none'",
    "#1218: pump_parameters.setting_depth_md_ft: the key name declares 'ft' but the value says 'm'",
    "#1218: pump_parameters.pump_sections[2]: section_order must be 2 here, got None",
    "#1218: pump_parameters.pump_sections: expected a non-empty list of sections, deepest section first",
    "#1218: wellbore_parameters: expected a map of parameters", "row: confirmed_by is not a string",
    f"runs/{RUN}/report.md is not there", "INTERNAL_ERROR at version: OSError: disk full",
    f"a request {CID} is already there", R.environment_problem(R.LAB_CHECK, "prd"), "something the app has not seen"]


def test_every_reason_says_what_was_noticed_and_what_to_check_without_a_code():
    said = [R.in_words(r) for r in SAMPLE_REASONS]
    assert ids(said) == []
    assert all(s.endswith(".") and re.search(r"\b(?:Check|Look for|Find)\b", s) for s in said), said


def test_every_note_reads_without_a_key():
    assert ids([R.note_words(n) for n in SAMPLE_NOTES], ["1800.00", "1,500 PSIA", "19985.3 psig", "3,000", "1800 psi",
                                                         "2,500 PSIA"]) == []


def test_every_refusal_is_plain_and_none_falls_through():
    said = [R.refusal_words(r) for r in SAMPLE_REFUSALS]
    assert ids(said, ["org-1", "org-2", "A 1H", "A 2H", "SF 2700", "SF2700", "XYZ"]) == []
    fallback = R.refusal_words("something the app has not seen")
    assert [r for r, s in zip(SAMPLE_REFUSALS[:-1], said[:-1]) if s == fallback] == []


def test_the_apps_own_messages_are_plain(tmp_path):
    def broken():
        raise RuntimeError("expired")

    notes = [R.confirmer("s3", {}, caller=broken)[1], R.confirmer("local", {}, tmp_path)[1],
             R.confirmer("local", {R.OFFLINE_ENV: "tester"}, tmp_path)[1],
             R.confirmer("local", {R.OFFLINE_ENV: "tester"}, Path(R.CLI.LAB) / "store")[1],
             R.environment_problem(R.LAB_CHECK, "prd")]
    notes += [R.place(e) for e in C.ENVIRONMENTS]
    notes += [R.report_words({"route": "template", "family": code}) for code in R.VENDORS]
    notes += [R.report_words({"route": "generic"}), R.report_words({"route": "generic", "forced": True}), R.NOT_KNOWN]
    notes += [R.run_problem_words({"error": {"code": c, "message": m}}) for c, m in (
        ("LOAD_HALTED", "D17-HALT: scenarios present different pump strings"), ("STORE_FAILED", "x"))]
    notes += [R.step_words(s, "tester's confirmation of 2026-10-31 20:19 UTC") for s in (
        C.step(C.TO_CHECK, "extraction", "2026-10-28T20:18:58Z", "generic_path; varies_by_scenario"),
        C.step(C.SETTLED, "tester", "2026-10-31T20:19:21.134313", 'batch 1 · found · "These 2 values match."'),
        C.step(C.TO_CHECK, "tester", "2026-10-31T20:19:21.134313", "batch 2 · not found · pulled out"),
        C.step(C.SETTLED, "tester", "2026-10-31T20:19:21.134313", f"carried from {CID}"),
        C.step(C.CORRECTED, "tester", "2026-10-31T20:19:21.134313", None))]
    notes += [R.confirmation_words({"confirmed_by": "tester", "confirmed_at": "2026-10-31T20:19:21.134313"})]
    for status in (C.WRITTEN, C.REFUSED, C.FAILED, C.STALE, C.RUNNING):
        notes.append(R.outcome(C.LocalObjects(tmp_path), CID, "prd", {"status": status, "reasons": []})["note"] or "")
    notes.append(R.outcome(C.LocalObjects(tmp_path), CID, "prd", None)["note"])
    for key in VALUE_TYPE:
        bad = {"value": "x" if VALUE_TYPE[key] == "double" else 1.0, "unit": "furlong", "source": "guess"}
        notes += R.correction_problems(key, bad) if key in R.LABELS else []
    assert ids(notes, ["tester"]) == []


# The string as an engineer may leave it in the editor: a section marked not in the catalogue, one whose printed model
# matched two names, one of a model not spelled as the catalogue does, one with nothing; housings and stages that
# disagree each way V-09 has; depths against the order.
EDITED_SECTIONS = [
    {"section_order": 1, "pump_model": None, "stages": None, "section_role": "gas_handler", "housings": 2,
     "stages_per_housing_as_printed": "2X117 STG", "top_md_ft": 9600.0, "bottom_md_ft": 9620.0},
    {"section_order": 2, "pump_model": None, "stages": 300, "section_role": "taper", "housings": 2,
     "stages_per_housing_as_printed": "100", "top_md_ft": None, "bottom_md_ft": 9650.0},
    {"section_order": 3, "pump_model": "SF 2700", "stages": 500, "section_role": None, "housings": 3,
     "stages_per_housing_as_printed": "100, 100, 100, 100", "top_md_ft": None, "bottom_md_ft": None},
    {"section_order": None, "pump_model": None, "stages": None, "section_role": None, "housings": None,
     "stages_per_housing_as_printed": None, "top_md_ft": None, "bottom_md_ft": None}]
EDITED_ROWS = [{"printed": "REDA 400/400 AGH", "candidates": [], "absent": True},
               {"printed": "PUMP MSC_400UNB_35H_93 STG", "candidates": ["SF2700", "SF2700X"], "absent": False},
               {"printed": "SF 2700", "candidates": [], "absent": False},
               {"printed": None, "candidates": [], "absent": False}]
PRINTED = ["REDA 400/400 AGH", "PUMP MSC_400UNB_35H_93 STG", "SF 2700", "SF2700", "SF2700X"]
NOT_RUN = ["no pump rows", "no pump rows reached the load", "no sections", "no section prints a head",
           "only some sections print a head", "no TDH printed", "a single section", "a section has two different heads",
           "the TDH is not one value in the scenario", "scenario keys missing on the design-context rows",
           "scenario keys missing on the pump rows", "something not seen before"]


def test_every_message_of_the_string_editor_is_plain():
    names = CatalogueNames({"SF2700": True, "SF2700X": True, "ELS1750": False})
    said = [w for _, w in R.string_checks(EDITED_SECTIONS, EDITED_ROWS, names)]
    said += [w for _, w in R.string_checks(EDITED_SECTIONS[1:2], EDITED_ROWS[1:2], names)]     # numbered 2, alone
    said += [w for _, w in R.string_checks([], [], names)]
    checks = [{"check": c, "result": r, "why": w} for c in R.AS_READ for r, w in (
        ("pass", "x"), ("fail", "scenario 1: 3 scenarios present different pump strings"),
        *(("not run", w) for w in NOT_RUN))]
    said += [w for _, w in R.document_checks({"review": {"pump_string": {"checks": checks}}})]
    rows = [{**dict.fromkeys(R.EDITABLE), "id": f"n{i}", "page": None, "printed": p, "candidates": [], "absent": a}
            for i, (p, a) in enumerate(((None, True), (None, False), ("X 1", True)), 1)]
    said += R.save_problems({"editor": {"rows": rows, "reason": " "}}) + R.save_problems({"editor": {"rows": [],
                                                                                                    "reason": ""}})
    said += R.save_problems({"editor": {"rows": [{**rows[0], "absent": False, "pump_model": "SF2700"}], "reason": ""}})
    for v in ({"final": None, "status": C.TO_CHECK, "sections": [], "unwritten": None},
              {"final": None, "status": C.BLOCKS, "sections": EDITED_SECTIONS, "unwritten": None},
              {"final": None, "status": C.CORRECTED, "sections": EDITED_SECTIONS, "unwritten": REASON,
               "display": [{"printed": "REDA 400/400 AGH", "absent": True}, {"printed": None, "absent": False}]},
              {"final": None, "status": C.CORRECTED, "sections": EDITED_SECTIONS, "unwritten": None,
               "display": [{"printed": None, "absent": False}]},
              {"final": None, "status": C.CORRECTED, "sections": EDITED_SECTIONS[2:3], "unwritten": REASON,
               "display": [{"printed": None, "absent": False}]}):
        said.append(R.unwritten_words({"values": {R.STRING: v}}))
    said += [R.field_problem(f, v, names) for f, v in (("stages", 0), ("housings", 1.5), ("top_md_ft", float("inf")),
                                                ("bottom_md_ft", "deep"), ("section_role", "booster"),
                                                ("pump_model", "XYZ"), ("absent", None), ("section_order", 1))]
    said += [R.step_words(C.step(C.CORRECTED, "tester", "2026-11-02T10:00:00.000001", r)) for r in (
        "edited · changed section_order, pump_model, stages, section_role, housings, stages_per_housing_as_printed, "
        "top_md_ft, bottom_md_ft",
        'edited · added 2 · removed 1 · moved 3 · not in the catalogue: "REDA 400/400 AGH", "X \\"2\\"" · not written: '
        '"because"', 'edited · changed stages · not written: "because"', "edited")]
    said += [R.UNSAVED]
    assert all(said) and ids(said, ["tester", REASON, *PRINTED]) == []
    assert all(s.endswith((".", "”", ")")) or s.startswith("sections edited") for s in said), said


# ---- every engineer screen of every run in a store ----

STORE = os.environ.get("ESP_RUN_STORE_DIR", "")


def open_run(run_id):
    at = AppTest.from_file(APP, default_timeout=120)
    at.query_params["run"] = run_id
    return at.run()


def walk(at, rid):
    """The run's screens, in the order an engineer meets them: (name, the App as it stands)."""
    if at.get("button_group")[0].options[0] == "Confirmed entry":
        yield "confirmed view", at
        return
    yield "review mode", at
    state = at.session_state[f"review-{rid}"]
    review, entry = state["review"], state["entry"]
    groups = R.groups(review, entry)
    number = next((k for g in ("found", "not found") for k in groups[g] if VALUE_TYPE.get(k) == "double"), None)
    if number:                                          # a correction refused, with its reason shown
        at.text_input(key=f"fix-{rid}-{number}-value").input("x").run()
        at.button(key=f"fix-{rid}-{number}-save").click().run()
        yield "review mode, a correction refused", at
    if at.button(key=f"batch-{rid}").disabled:
        at.session_state["dialog"] = (rid, "write")
        yield "Write pop-up", at.run()
        yield from edited(at, rid)
        return
    at.button(key=f"batch-{rid}").click().run()
    while (step := at.session_state["dialog"][1]) != "write":
        yield f"{step} pop-up", at
        if step == "string" and R.blocking(review, entry, R.STRING):
            at.text_area(key=f"dlg-anyway-{rid}").input(REASON).run()
            yield "string pop-up, with a reason", at
        elif step != "string":
            tick = next((t for t in at.checkbox if t.key and t.key.startswith(f"tick-{rid}-{step}-")
                         and not t.disabled), None)
            if tick:                                    # one pulled out: the sentence's count follows
                tick.uncheck().run()
                yield f"{step} pop-up, one pulled out", at
        at.button(key=f"dlg-go-{rid}-{step}").click().run()
    yield "Write pop-up, over the review after the batches", at
    yield from edited(at, rid)


def edited(at, rid):
    """The string's editor, opened from its block once the pop-ups are closed: as it opens; with each section that has
    no model marked not in the catalogue (or, when every section has one, the first one's stage count cleared), not
    saved; then, with a reason, saved, or else cancelled."""
    at.session_state["dialog"] = None
    at.run()
    at.button(key=f"sections-{rid}").click().run()
    yield "editor", at
    ed = at.session_state[f"review-{rid}"]["review"]["editor"]
    k, bare = f"ed-{rid}-{ed['gen']}", [r["id"] for r in ed["rows"] if r["pump_model"] is None]
    for row_id in bare:
        at.checkbox(key=f"{k}-{row_id}-absent").check().run()
    if ed["rows"] and not bare:
        at.number_input(key=f"{k}-{ed['rows'][0]['id']}-stages").set_value(None).run()
    yield "editor, a change not saved" + (", sections marked not in the catalogue" if bare else ", a stage count cleared"), at
    if ed["rows"]:                                      # either way the string would not be written: a reason first
        at.text_area(key=f"{k}-reason").input(REASON).run()
        if not at.button(key=f"ed-{rid}-save").disabled:
            at.button(key=f"ed-{rid}-save").click().run()
            yield "review mode, the string recorded and not written", at
            return
    at.button(key=f"ed-{rid}-cancel").click().run()


@pytest.mark.store
@pytest.mark.skipif(not STORE, reason="the runs to render need ESP_RUN_STORE_DIR")
def test_no_id_or_code_on_any_engineer_screen(monkeypatch):
    monkeypatch.setenv(R.OFFLINE_ENV, "reviewer")       # Write's checks run, so their refusals show
    try:
        names = list(read_catalogue())                  # the editor's model pick shows the catalogue's names
    except Exception:  # noqa: BLE001 - no snapshot: the editor shows none
        names = []
    root, hits, screens = Path(STORE), [], 0
    runs = sorted(p.name for p in (root / "runs").iterdir() if (p / C.ENTRY).is_file())
    for rid in runs:
        record = json.loads((root / "runs" / rid / "run.json").read_text(encoding="utf-8"))
        entry = json.loads((root / "runs" / rid / C.ENTRY).read_text(encoding="utf-8"))
        if record.get("well_id") is None:
            continue
        versions = [json.loads(p.read_text(encoding="utf-8")) for p in root.glob("design-records/*/versions/*.json")]
        mine = [v for v in versions if (v.get("run") or {}).get("run_id") == rid]
        leave_out = exempt(record, entry, mine[0] if mine else None, "reviewer", REASON,
                           *(v["confirmed_by"] for v in versions), *names)
        for name, at in walk(open_run(rid), rid):
            screens += 1
            hits += [f"{rid} · {name} · {hit}" for hit in unplain(at, *leave_out)]
    assert runs and screens and not hits, "\n".join(hits[:60]) + f"\n… {len(hits)} in all"
