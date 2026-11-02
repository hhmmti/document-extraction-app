"""Reviewing a run's proposed well entry and confirming it from the app: the decisions, the record they leave, and the
write.  No Streamlit here: app/main.py draws it, tests/test_review.py checks it.

A review starts from the run's well_entry.json (extraction/well_entry.py), each value at its starting status (settled,
to check or blocks publish), and takes what the document's latest confirmed version for the well carries (`carry`).
The engineer then decides each value, one by one or in batches:

    settle            as proposed: settled
    correct           a new envelope (a value, a unit #1218 allows for the key, source design_document or
                      ops_entered, an optional note): manually corrected
    settle_empty      final null: settled when nothing was proposed, else manually corrected
    accept_anyway     what blocks publish, uncorrected, with a written reason: settled (an edited string keeps its
                      edits and stays manually corrected)
    batch             a pop-up's ticked values settled together; the unticked ones pulled out, still to check

Blocks publish clears only by a correction whose check passes, or by accept anyway with a reason.  The pump string is
settled as proposed, accepted anyway, or edited: the editor (`open_editor` … `save`) works on a copy, one row per
section, its order numbers following the rows, 1 the deepest; `save` applies it.  A section's model is a catalogue
name, or the section is marked not in the catalogue with every name the document prints for it.  A string with a
marked section, or with a section that has no stage count, is recorded and not written (final null), for a written
reason (`not_written`).  What the app shows beside a section (its page, the model as printed, the
mark) stays in the app: the request's sections hold exactly SECTION_FIELDS.  The string's checks that read only its
sections run on the sections as they stand (`string_checks`), beside those that read the document (`document_checks`).

The record is each value's `history` in the request, a list of {status, by, at, reason} (extraction/confirmation.py):

    the proposal          the entry's starting status, by `extraction`, at the run's end, its reasons joined by "; "
    settled in a batch    settled, reason   batch <n> · <group> · "<sentence>"
    pulled out            to check, reason  batch <n> · <group> · pulled out
    one by one            the status reached, reason null; an accept-anyway reason is the item's accept_anyway
    the string edited     manually corrected (or, brought back to the proposal, its starting status), reason
                          edited · added <n> · removed <n> · moved <n> · changed <field>, <field>
                          [[· not in the catalogue: "<printed>", …] · not written: "<reason>"]
    carried on a re-run   the earlier version's steps, then the carried status, reason  carried from <confirmation_id>

<n> numbers this confirmation's batches from 1, <group> is string, found or not found, and <sentence> is the pop-up's
button, verbatim.  An edited step's counts are against the string before that save, and each part shows only when it
is not zero (`edited_reason`).  `by` is the confirmer for every step this confirmation adds.  The README has the same.
"""

from __future__ import annotations

import bisect
import json
import math
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path

from extraction import confirm as CLI
from extraction import confirm_writer as W
from extraction import confirmation as C
from extraction.validate.rules import v09
from extraction.well_entries_schema import VALUE_TYPE
from extraction.well_entry import (KEYS, SOURCE, UNITS, FinalSections, envelope_problem, final_string_checks,
                                   order_check, section_incomplete)

LAB_CHECK = "lab-check"                    # the lab's own organisation: it writes to dev, every other one to prd
OFFLINE_ENV = "ESP_CONFIRM_OFFLINE_AS"     # local mode: write into the local store under this name, writer in process
WAIT_S = 180                               # the CLI's --wait
STRING = C.PUMP_SECTIONS
GROUPS = ("string", "found", "not found")  # the batch pop-ups, in order; Write follows them
UNDECIDED = (C.TO_CHECK, C.BLOCKS)
SOURCES = (SOURCE, "ops_entered")
STRING_SENTENCE, STRING_ANYWAY = "The string matches the document.", "Accept the string anyway"
BATCH = re.compile(r'batch (\d+) · (string|found|not found) · (?:"(.*)"|pulled out)')   # a batch step's reason
QUOTED = r'"(?:[^"\\]|\\.)*"'                                   # a JSON string: text as printed or typed, quotes escaped
EDITED = re.compile(rf"edited(?: · added (\d+))?(?: · removed (\d+))?(?: · moved (\d+))?"
                    rf"(?: · changed ([a-z_]+(?:, [a-z_]+)*))?"
                    rf"(?:(?: · not in the catalogue: ({QUOTED}(?:, {QUOTED})*))? · not written: ({QUOTED}))?")  # a save's
EDITABLE = tuple(f for f in C.SECTION_FIELDS if f != "section_order")   # a section's order follows its row
DISPLAY = ("id", "page", "printed", "candidates", "absent")             # what the app keeps beside a section, unsent
ROLES = ("primary", "taper", "gas_handler")
# A section's fields as the engineer reads them: table columns, and the edited step's fields in words.
FIELD_WORDS = {"section_order": "order", "pump_model": "model", "stages": "stages", "section_role": "role",
               "housings": "housings", "stages_per_housing_as_printed": "stages per housing",
               "top_md_ft": "top MD (ft)", "bottom_md_ft": "bottom MD (ft)"}
# The Found pop-up's clauses: a judgement the extraction made, named when a ticked value carries its reason.
JUDGEMENTS = (("pressure_basis_unstated", "bare-psi pressures taken as absolute"),
              ("pressure_unit_not_printed", "pressures printed with no unit taken as absolute"),
              ("varies_by_scenario", "scenario 1 as the design case"),
              ("depth_basis_pump_setting", "the pump-setting depth for the intake depth"))

# #1218's labels, as the product's UI writes them (v2-ui/utils/text/managementPage.js at 6831cbd6).
LABELS = {
    "pump_sections": "Pump string", "setting_depth_tvd_ft": "Pump setting depth (TVD)",
    "setting_depth_md_ft": "Pump setting depth (MD)", "motor_type": "Motor type",
    "motor_nameplate_hp": "Motor nameplate power", "motor_power_factor_fraction": "Motor power factor",
    "motor_efficiency_fraction": "Motor efficiency", "design_document_ref": "Design document reference",
    "fluid_specific_gravity": "Fluid specific gravity", "oil_sg": "Oil specific gravity",
    "water_sg": "Water specific gravity", "gas_sg": "Gas specific gravity", "bubble_point_psia": "Bubble point pressure",
    "vapor_pressure_psia": "Vapor pressure", "reservoir_pressure_psia": "Reservoir pressure",
    "bottomhole_temperature_f": "Bottomhole temperature", "solution_gor_scfstb": "Solution gas-oil ratio",
    "perforation_top_depth_ft": "Top perforation depth",
}

# Every reason code the entries carry (a value's, a section's, or the string's failed check), in the engineer's words:
# what was noticed, then what to check.  No code, key or position reaches the screen (`in_words`).
REASONS = {
    "generic_path": "The document is not a known vendor report, so nothing cross-checked this reading. Check it "
                    "against the page.",
    "varies_by_scenario": "The scenarios print different values; scenario 1's is proposed. Check which scenario is "
                          "the design case.",
    "depth_basis_pump_setting": "No intake depth is printed; the pump setting depth stands in for it. Check that it "
                                "is the depth you want for the intake.",
    "pressure_basis_unstated": "Printed in psi without saying gauge or absolute; taken as absolute. Check which the "
                               "document means.",
    "pressure_unit_not_printed": "No pressure unit is printed; taken as absolute psi. Check the unit in the document.",
    "value_not_typed": "The printed text could not be read as a value. Check what the document prints.",
    "conflict": "Two readings of this item disagree. Check it against the document.",
    "frequency_trap": "The scenario's title and its frequency disagree. Check which frequency the design uses.",
    "expected_missing": "This vendor's reports always print this value, but it was not found. Look for it in the "
                        "document.",
    "known_issue": "This vendor's reports are known to print this item unreliably. Check it against the document.",
    "model_unresolved": "The printed model is not a name in the pump catalogue. Check the model against the document.",
    "model_absent": "The model is not in the pump catalogue. Check the model name against the document.",
    "model_ambiguous": "The model matches more than one name in the pump catalogue. Check which one the document "
                       "means.",
    "section_incomplete": "The section's order number or stage count is missing. Check them in the document.",
    "sections": "No pump sections were read. Check the pump string in the document.",
    "V-08": "The sections are not numbered 1 to N from the deepest. Check the order of the sections in the document.",
    "D17": "The scenarios print different pump strings. Check which one is the design.",
    "V-09": "The stage counts disagree with the housings. Check the stage counts in the document.",
    "models": "Some models are not names in the pump catalogue. Check the model names against the document.",
    "complete": "Some sections lack their order number or stage count. Check them in the document.",
    "review": "Some sections need a check. Check each against its reason in the table.",
    "head sum": "The sections' heads do not add up to the total dynamic head printed. Check the models and stage "
                "counts.",
}
RULES = {"V-06": "The model's name does not follow this vendor's naming. Check the model against the document.",
         "V-X3": "This spelling of the model has not been seen in earlier documents. Check the model against the "
                 "document.",
         "V-12": "The bubble point is outside 0 to 10,000 psi. Check the value and its unit.",
         "D16": "A value that should be the same in every scenario differs between them. Check which scenario is the "
                "design case.",
         "V-08": "The section order is broken. Check the order of the sections in the document.",
         "D17": "The scenarios print different pump strings. Check which one is the design."}
UNKNOWN_REASON = "A check flagged this. Check it against the document."
SET_ASIDE = {"empty_string": "it is empty", "composite_value": "it holds more than one value",
             "unrecognized_unit_token": "its unit was not recognised"}


# ---- reading the entry ----

def proposal(entry, key):
    """The entry's envelope for `key`, or None when it proposes no value."""
    return entry[KEYS[key][0]].get(key)


def reviewed(entry) -> dict:
    """{key: the entry's review item}."""
    return {v["key"]: v for v in entry["review"]["values"]}


def sections(entry) -> list[dict]:
    """The string's proposed sections, as a request carries them."""
    return [{name: s.get(name) for name in C.SECTION_FIELDS} for s in entry["review"]["pump_string"]["sections"]]


def display(entry) -> list[dict]:
    """What the app keeps beside each proposed section and never sends (DISPLAY): an id, its page, the model as
    printed, the lookup's candidates, and whether it is marked not in the catalogue."""
    return [{"id": f"p{i}", "page": s.get("page"), "printed": s.get("pump_model_as_printed"),
             "candidates": list(s.get("candidates") or []), "absent": False}
            for i, s in enumerate(entry["review"]["pump_string"]["sections"], 1)]


def label(key) -> str:
    return LABELS.get(key) or key.replace("_", " ").capitalize()


def allowed_units(key) -> list[str]:
    """The units #1218's rules (extraction/well_entry.py, restated) accept for the key: one per key today."""
    sample = 1.0 if VALUE_TYPE[key] == "double" else "x"
    return [u for u in sorted(UNITS) if envelope_problem(key, {"value": sample, "unit": u, "source": SOURCE}) is None]


def in_words(reason: str) -> str:
    """One reason code as the engineer's words, with the detail that helps; UNKNOWN_REASON for a code not known."""
    code, _, detail = reason.partition(":")
    code, detail = code.strip(), detail.strip()
    if code == "rule":
        return RULES.get(detail, UNKNOWN_REASON)
    if code == "grounding":
        m = re.fullmatch(r"other_page \(cited (.+); found on (.+)\)", detail)
        return (f"It is cited on {pages_words(m[1])} but printed on {pages_words(m[2])}. Check it there." if m else
                "Its printed text was not found on the pages read. Find it in the document and check the value.")
    if code == "quarantined":
        return (f"The printed value was set aside: {SET_ASIDE.get(detail, 'it could not be used')}. Check what the "
                "document prints.")
    if code == "models" and detail:
        return (f"Some models are not names in the pump catalogue: {models_words(detail)}. Check the model names "
                "against the document.")
    if code == "model_ambiguous" and detail:
        return (f"The model matches more than one name in the pump catalogue ({detail}). Check which one the "
                "document means.")
    if code in ("complete", "section_incomplete") and (missing := missing_words(detail)):
        return f"{missing} Check {'them' if ' or ' in missing or ';' in missing else 'it'} in the document."
    if code == "V-08" and detail:
        return (f"The sections are not numbered 1 to N from the deepest{order_words(detail)}. Check the order of the "
                "sections in the document.")
    if code == "D17" and (m := re.match(r"(\d+) scenarios", detail)):
        return f"The document's {m[1]} scenarios print different pump strings. Check which one is the design."
    return REASONS.get(code, UNKNOWN_REASON)


def section_name(position) -> str:
    """A section by its place in the string, as the engineer counts: 1 to N, 1 the deepest."""
    return "section 1, the deepest" if str(position) == "1" else f"section {position}"


def sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:]


def pages_words(cited: str) -> str:
    """`p3` as `page 3`; `p2, p6` as `pages 2 and 6`."""
    pages = re.findall(r"\d+", cited)
    return f"page {pages[0]}" if len(pages) == 1 else f"pages {join(pages)}"


def models_words(detail: str) -> str:
    """The models check's detail: each model not in the catalogue, by its section when the detail names it."""
    out = []
    for part in detail.split("; "):
        m = re.fullmatch(r"(?:position (\d+): )?(?:(no model)|(.+?) ambiguous: (.+)|(.+?) not in the catalogue)", part)
        if not m:
            continue
        where = f"{section_name(m[1])}: " if m[1] else ""
        out.append(f"{where}no model" if m[2] else f"{where}{m[3]} (could be {join(m[4].split(', '))})" if m[3]
                   else f"{where}{m[5]}")
    return "; ".join(out) or "see the table"


def missing_words(detail: str) -> str:
    """What a section lacks, from `position 1: section_order 1, stages None; …`, `stages None` or
    `section_order None at position 2`: `Section 1, the deepest, has no stage count.`"""
    out = []
    for m in re.finditer(r"position (\d+): section_order (\S+), stages (\S+?)(?:;|$)", detail):
        lacks = [w for w, v in (("order number", m[2]), ("stage count", m[3])) if v == "None"]
        if lacks:
            out.append(f"{section_name(m[1])}{',' if m[1] == '1' else ''} has no {' or '.join(lacks)}")
    m = re.fullmatch(r"(section_order|stages) None(?: at position (\d+))?", detail)
    if m:
        what = "order number" if m[1] == "section_order" else "stage count"
        out.append(f"{section_name(m[2])}{',' if m[2] == '1' else ''} has no {what}" if m[2] else f"the section has no {what}")
    return sentence_case("; ".join(out)) + "." if out else ""


ORDER_WHY = ((r"section_order is missing or not an integer", "a section has no order number"),
             (r"section_order is not a contiguous 1\.\.(\d+)", "the order numbers are not 1 to {0}"),
             (r"section_order runs against the depth columns \(1 must be deepest\)",
              "the order runs against the depths, where 1 must be the deepest"))


def order_words(why: str) -> str:
    """The order check's detail, `scenario 1: section_order is …`, as ` (in scenario 1, a section has no …)`."""
    out = []
    for part in why.split("; "):
        m = re.fullmatch(r"(?:scenario (\S+): )?(.*)", part)
        for pattern, words in ORDER_WHY:
            if hit := re.fullmatch(pattern, m[2]):
                scenario = f"in scenario {m[1]}, " if m[1] not in (None, "None") else ""
                out.append(scenario + words.format(*hit.groups()))
    return f" ({'; '.join(out)})" if out else ""


# ---- the review ----

def start(entry, run, version=None, *, at: str, names=None) -> dict:
    """A review's first state: every value as the entry proposes it (the string at `starting`'s status, given the
    catalogue's `names`), then what `version`, the document's latest confirmed version for the well, carries."""
    proposed_at = run.get("ended_at") or run.get("started_at") or "unknown"        # the CLI's proposal step
    values = {}
    for v in entry["review"]["values"]:
        status, reasons = starting(entry, names) if v["key"] == STRING else (v["status"], v["reasons"])
        values[v["key"]] = {"status": status, "final": proposal(entry, v["key"]), "accept_anyway": None,
                            "history": [C.step(status, "extraction", proposed_at, "; ".join(reasons) or None)],
                            "since": 1, "carried": False, "moved": False}
    values[STRING].update(sections=sections(entry), display=display(entry), unwritten=None,
                          starting=values[STRING]["status"])
    review = {"values": values, "batches": 0, "carried_from": None, "editor": None}
    if version:
        carry(review, entry, version, at=at)
    return review


def starting(entry, names=None) -> tuple[str, list[str]]:
    """The string's starting status and reasons: the entry's, except that a string that would start settled starts to
    check where a check on its sections fails here (`final_string_checks`, against the catalogue's `names`) that
    passed when the entry was built.  That check's reason is added."""
    item = reviewed(entry)[STRING]
    if names is None or item["status"] != C.SETTLED:
        return item["status"], list(item["reasons"])
    stored = {c["check"]: c["result"] for c in entry["review"]["pump_string"]["checks"]}
    newly = [f"{c['check']}: {c['why']}" for c in final_string_checks(sections(entry), names)
             if c["result"] == "fail" and stored.get(c["check"]) == "pass"]
    return (C.TO_CHECK if newly else C.SETTLED), [*item["reasons"], *newly]


def proposal_blocks(entry, key) -> bool:
    """Whether the value as proposed blocks publish: its starting status, or, for the string, a failing order check on
    its proposed sections.  Settling as proposed is then not allowed."""
    return reviewed(entry)[key]["status"] == C.BLOCKS or (key == STRING and order_check(sections(entry))["result"] == "fail")


def blocking(review, entry, key) -> bool:
    """Whether the value blocks publish.  A value: its starting status; a correction or accept anyway with a reason
    decides it.  The string: a failing order check on its sections as they stand, or, while it is not corrected, its
    starting status; accept anyway with a reason decides it.  The writer's rule (extraction/confirm_writer.check)."""
    if key == STRING:
        v = review["values"][STRING]
        if order_check(v["sections"])["result"] == "fail":
            return True
        if v["status"] == C.CORRECTED:
            return False
    return reviewed(entry)[key]["status"] == C.BLOCKS


def undecided(review, entry, key) -> bool:
    """Still to decide: to check, or blocking publish with no accept-anyway reason (settled; or, for the string,
    corrected with its order check failing)."""
    v = review["values"][key]
    if v["status"] in UNDECIDED:
        return True
    return (not v["accept_anyway"] and (v["status"] == C.SETTLED or key == STRING)
            and blocking(review, entry, key))


def groups(review, entry) -> dict[str, list[str]]:
    """The keys of each group, in the entry's order: the string while undecided; found and not found, the values
    still to decide with a proposal and without one; decided, the rest."""
    out = {"string": [], "found": [], "not found": [], "decided": []}
    for key in reviewed(entry):
        if key == STRING:
            out["string"] += [key] if undecided(review, entry, key) else []
        elif not undecided(review, entry, key):
            out["decided"].append(key)
        else:
            out["found" if proposal(entry, key) is not None else "not found"].append(key)
    return out


def pulled_out(review, key) -> int | None:
    """The batch that pulled the value out, while it is still to check from it."""
    v = review["values"][key]
    own = v["history"][v["since"]:]
    m = BATCH.fullmatch(own[-1]["reason"] or "") if own and own[-1]["status"] == C.TO_CHECK else None
    return int(m[1]) if m else None


def _decided(review, entry, key, status, final, at, *, accept=None, reason=None):
    v = review["values"][key]
    v.update(status=status, final=final, accept_anyway=accept)
    if key == STRING:
        v.update(sections=sections(entry), display=display(entry), unwritten=None)
    v["history"].append(C.step(status, None, at, reason))          # by: the confirmer, filled in by `request`


def _kept_anyway(review, reason, at, step_reason=None):
    """An edited string accepted anyway: its edits kept, still manually corrected, the reason its accept_anyway."""
    v = review["values"][STRING]
    v["accept_anyway"] = reason
    v["history"].append(C.step(C.CORRECTED, None, at, step_reason))


def settle(review, entry, key, *, at):
    """As proposed.  A value whose proposal blocks publish needs a correction or accept anyway instead."""
    if proposal_blocks(entry, key):
        raise ValueError(f"{key} blocks publish: correct it, or accept it anyway with a reason")
    _decided(review, entry, key, C.SETTLED, proposal(entry, key), at)


def settle_empty(review, entry, key, *, at):
    """Final null: settled when nothing was proposed, else manually corrected."""
    if key == STRING:
        raise ValueError("the string is settled as proposed, accepted anyway or edited")
    empty = proposal(entry, key) is None
    if empty and blocking(review, entry, key):
        raise ValueError(f"{key} blocks publish: accept it anyway with a reason")
    _decided(review, entry, key, C.SETTLED if empty else C.CORRECTED, None, at)


def accept_anyway(review, entry, key, reason, *, at):
    """What blocks publish, uncorrected, for a written reason: settled.  An edited string keeps its edits: it stays
    manually corrected, with the reason."""
    if not blocking(review, entry, key):
        raise ValueError(f"{key} does not block publish")
    if not (reason or "").strip():
        raise ValueError("accept anyway needs a written reason")
    if key == STRING and review["values"][STRING]["status"] == C.CORRECTED:
        _kept_anyway(review, reason.strip(), at)
    else:
        _decided(review, entry, key, C.SETTLED, proposal(entry, key), at, accept=reason.strip())


def parse_value(key, text):
    """(the value typed for the key, None) or (None, why not)."""
    text = (text or "").strip()
    if not text:
        return None, "type a value, or settle it as empty"
    if VALUE_TYPE[key] != "double":
        return text, None
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None, f"{text!r} is not a number"
    return (number, None) if math.isfinite(number) else (None, f"{text!r} is not a finite number")


def correction_problems(key, envelope) -> list[str]:
    """Why the correction cannot be made: the key, the unit, the source, the value's type, #1218's rules."""
    if key in (STRING, C.DESIGN_DOCUMENT_REF):
        return [f"{label(key)} is not corrected here"]
    out = [] if envelope["unit"] in allowed_units(key) else [f"{label(key)} takes {join(allowed_units(key))} only"]
    out += [] if envelope["source"] in SOURCES else ["the source is the design document or outside it"]
    value = envelope["value"]
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if VALUE_TYPE[key] == "double" and not numeric:
        out.append("the value is not a number")
    if VALUE_TYPE[key] == "string" and not (isinstance(value, str) and value.strip()):
        out.append("the value is not a name")
    problem = None if out else envelope_problem(f"{KEYS[key][0]}.{key}", envelope)
    return out + ([product_words(problem.split(": ", 1)[-1], key)] if problem else [])


def correct(review, entry, key, *, value, unit, source, note=None, at) -> list[str]:
    """A new envelope, manually corrected; or, unchanged, the reasons it cannot be."""
    envelope = {"value": value, "unit": unit, "source": source, **({"note": note.strip()} if (note or "").strip() else {})}
    problems = correction_problems(key, envelope)
    if not problems:
        _decided(review, entry, key, C.CORRECTED, envelope, at)
    return problems


# ---- batches ----

def join(parts) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def sentence(group, entry, ticked, *, anyway=False) -> str:
    """A pop-up's button: the affirmation, recorded verbatim."""
    n = len(ticked)
    if group == "string":
        return STRING_ANYWAY if anyway else STRING_SENTENCE
    if group == "not found":
        return f"I looked through the document: {'this 1 value is' if n == 1 else f'these {n} values are'} not printed."
    reasons = {r for key in ticked for r in reviewed(entry)[key]["reasons"]}
    clauses = [text for code, text in JUDGEMENTS if code in reasons]
    return (f"{'This 1 value matches' if n == 1 else f'These {n} values match'} the document as printed"
            + (f", with {join(clauses)}" if clauses else "") + ".")


def batch(review, entry, group, chosen, *, at, reason=None) -> tuple[int, str]:
    """One pop-up: the group's chosen (ticked) values settled with its sentence, the others pulled out, still to
    check.  A value that blocks publish cannot be ticked and is left as it is, except the string, which is accepted
    anyway with `reason`.  Returns the batch's number and its sentence."""
    keys = groups(review, entry)[group]
    ticked = [k for k in keys if k in chosen]
    anyway = group == "string" and bool(keys) and blocking(review, entry, STRING)
    if anyway != bool((reason or "").strip()) and ticked:
        raise ValueError("the string blocks publish: accept it anyway with a reason" if anyway
                         else "only a string that blocks publish is accepted anyway")
    if any(blocking(review, entry, k) for k in ticked if k != STRING):
        raise ValueError("a value that blocks publish cannot be ticked")
    review["batches"] += 1
    n, said = review["batches"], sentence(group, entry, ticked, anyway=anyway)
    for key in keys:
        if key in ticked and anyway and review["values"][STRING]["status"] == C.CORRECTED:     # edits kept
            _kept_anyway(review, reason.strip(), at, f'batch {n} · {group} · "{said}"')
        elif key in ticked:
            _decided(review, entry, key, C.SETTLED, proposal(entry, key), at, reason=f'batch {n} · {group} · "{said}"',
                     accept=reason.strip() if anyway else None)
        elif not blocking(review, entry, key) or key == STRING:
            review["values"][key]["history"].append(C.step(C.TO_CHECK, None, at, f"batch {n} · {group} · pulled out"))
    return n, said


# ---- a re-run of a confirmed document ----

def carry(review, entry, version, *, at):
    """What the document's latest confirmed version for the well carries.  Every correction comes back, manually
    corrected, marked `moved` where the new proposal differs from that version's.  A settlement, with its accept-anyway
    reason, comes back only where the new proposal equals that version's, and never onto a value that now blocks
    publish without a reason.  Each carried value keeps the earlier steps, then gets a carried-from step."""
    cid, earlier = version["confirmation_id"], {v["key"]: v for v in version.get("values", [])}
    old_string = version.get("pump_string") or {}
    old_sections = [{n: s.get(n) for n in C.SECTION_FIELDS} for s in (old_string.get("proposed") or {}).get("sections", [])]
    for key, v in review["values"].items():
        e = earlier.get(key)
        if not e or e.get("status") not in C.FINAL:
            continue
        same = e.get("proposed") == proposal(entry, key) and (key != STRING or old_sections == sections(entry))
        if e["status"] == C.CORRECTED:
            v.update(status=C.CORRECTED, final=e["final"], accept_anyway=e.get("accept_anyway"), moved=not same)
            if key == STRING:
                v["sections"] = [dict(s) for s in old_string.get("final_sections", v["sections"])]
                v["display"], v["unwritten"] = carried_display(v["sections"], e.get("history", []))
        elif same and (e.get("accept_anyway") or not blocking(review, entry, key)):
            v.update(status=C.SETTLED, final=e["final"], accept_anyway=e.get("accept_anyway"))
        else:
            continue                                     # starts as the new entry says
        v["history"] = [*e.get("history", []), C.step(v["status"], None, at, f"carried from {cid}")]
        v.update(since=len(v["history"]), carried=True)
    review["carried_from"] = cid


def carried_display(sections_, history) -> tuple[list[dict], str | None]:
    """What the app keeps beside carried sections, and their not-written reason: the marks the latest edited step
    recorded, put back in order on the sections without a model."""
    m = next((m for s in reversed(history) if (m := EDITED.fullmatch(s.get("reason") or ""))), None)
    printed = [json.loads(q) for q in re.findall(QUOTED, m[5])] if m and m[5] else []
    bare = [i for i, s in enumerate(sections_, 1) if s.get("pump_model") is None]
    marks = dict(zip(bare, printed)) if len(bare) == len(printed) else {}
    return ([{"id": f"c{i}", "page": None, "printed": marks.get(i), "candidates": [], "absent": i in marks}
             for i in range(1, len(sections_) + 1)], json.loads(m[6]) if m and m[6] else None)


# ---- the string's sections, edited ----

def _rows(sections_, display_) -> list[dict]:
    return [{**{f: s.get(f) for f in EDITABLE}, **d} for s, d in zip(sections_, display_)]


def string_rows(review) -> list[dict]:
    """The string as it stands, one row per section: its fields but the order, which follows the row, and DISPLAY."""
    v = review["values"][STRING]
    return _rows(v["sections"], v["display"])


def rows_sections(rows) -> list[dict]:
    """Rows as the request's sections: exactly SECTION_FIELDS, each section's order its row, 1 the deepest."""
    return [{f: i if f == "section_order" else r[f] for f in C.SECTION_FIELDS} for i, r in enumerate(rows, 1)]


def _marks(rows) -> list:
    return [r["absent"] and r["printed"] for r in rows]


def not_written(rows) -> bool:
    """Whether the string, saved as `rows`, puts no pump string in the well's entry (final null): it has sections, and
    one is marked not in the catalogue or has no model or no stage count.  Such a save needs a reason."""
    return bool(rows) and any(r["absent"] or r["pump_model"] is None or r["stages"] is None for r in rows)


def open_editor(review):
    """The editor on a copy of the string as it stands, whatever its status; one already open keeps its copy."""
    if review.get("editor") is None:
        review["gen"] = review.get("gen", 0) + 1                  # the screen's fields start afresh
        review["editor"] = {"rows": string_rows(review), "reason": review["values"][STRING]["unwritten"] or "",
                            "gen": review["gen"]}


def close_editor(review):
    """Cancel: the copy is dropped."""
    review["editor"] = None


def reset_editor(review, entry):
    """Back to the string as read: the copy is the proposal again."""
    review["gen"] = review.get("gen", 0) + 1
    review["editor"].update(rows=_rows(sections(entry), display(entry)), gen=review["gen"])


def _index(review, row_id) -> int:
    return next(i for i, r in enumerate(review["editor"]["rows"]) if r["id"] == row_id)


def add_section(review, place: int):
    """An empty section at `place`: 1 the deepest, one past the last the top."""
    rows = review["editor"]["rows"]
    if not 1 <= place <= len(rows) + 1:
        raise ValueError(f"no place {place} in a string of {len(rows)} sections")
    review["added"] = review.get("added", 0) + 1                  # an id no section of the review has had
    rows.insert(place - 1, {**dict.fromkeys(EDITABLE), "id": f"n{review['added']}", "page": None, "printed": None,
                            "candidates": [], "absent": False})


def remove_section(review, row_id):
    review["editor"]["rows"].pop(_index(review, row_id))


def move_section(review, row_id, by: int):
    """One place towards section 1, the deepest (-1), or away from it (+1)."""
    rows, i = review["editor"]["rows"], _index(review, row_id)
    if 0 <= i + by < len(rows):
        rows[i], rows[i + by] = rows[i + by], rows[i]


def field_problem(field, value, names=None) -> str | None:
    """Why a row's field (an EDITABLE field, `absent` or `printed`) cannot take `value`; None when it can.  Every
    field but `absent` can be empty."""
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if field in ("stages", "housings"):
        ok, what = value is None or (number and float(value).is_integer() and value >= 1), "a whole number of at least 1"
    elif field in ("top_md_ft", "bottom_md_ft"):
        ok, what = value is None or (number and math.isfinite(value)), "a depth in feet"
    elif field == "section_role":
        ok, what = value is None or value in ROLES, "primary, taper or gas handler"
    elif field == "pump_model":
        ok, what = value is None or (isinstance(value, str) and (names is None or value in names.names)), \
            "a name in the pump catalogue"
    elif field in ("printed", "stages_per_housing_as_printed"):
        ok, what = value is None or isinstance(value, str), "text"
    elif field == "absent":
        ok, what = isinstance(value, bool), "yes or no"
    else:
        return "The order follows the rows: move the section instead." if field == "section_order" else (
            "That is not a field of a section.")
    return None if ok else f"{sentence_case(FIELD_WORDS.get(field, 'the mark'))} must be {what}."


def set_field(review, row_id, field, value, names=None) -> str | None:
    """One field of one row; or, the row unchanged, why not.  Text is stripped, and empty text is empty.  A model
    clears the not-in-the-catalogue mark, and the mark clears the model."""
    if isinstance(value, str):
        value = value.strip() or None
    problem = field_problem(field, value, names)
    if problem:
        return problem
    if value is not None and field in ("stages", "housings"):
        value = int(value)
    elif value is not None and field in ("top_md_ft", "bottom_md_ft"):
        value = float(value)
    row = review["editor"]["rows"][_index(review, row_id)]
    row[field] = value
    if field == "absent" and value:
        row["pump_model"] = None
    if field == "pump_model" and value is not None:
        row["absent"] = False
    return None


def unsaved(review) -> bool:
    """Whether the editor is open with a copy that differs from the string as it stands: its sections (numbered by
    row), its marks, or, when it would not be written, its reason."""
    ed, v = review.get("editor"), review["values"][STRING]
    if ed is None:
        return False
    reason = (ed["reason"].strip() or None) if not_written(ed["rows"]) else None          # an empty reason is none
    return (rows_sections(ed["rows"]), _marks(ed["rows"]), reason) != (v["sections"], _marks(v["display"]), v["unwritten"])


def section_subject(position) -> str:
    """A section as a sentence's subject: `Section 1, the deepest,` or `Section 2`."""
    return sentence_case(section_name(position)) + ("," if str(position) == "1" else "")


def save_problems(review) -> list[str]:
    """Why Save is off, in words; [] when the copy can be saved."""
    ed = review["editor"]
    out = [] if ed["rows"] else ["Add a section first: a string has at least one, the deepest first."]
    for i, r in enumerate(ed["rows"], 1):
        if r["absent"] and not r["printed"]:
            out.append(f"{section_subject(i)} is marked not in the catalogue: type its model as printed.")
        elif not r["absent"] and r["pump_model"] is None:
            out.append(f"{section_subject(i)} has no model: pick one from the catalogue, or mark it not in the "
                       "catalogue.")
    cause = ("A section is not in the catalogue" if any(r["absent"] for r in ed["rows"]) else
             "A section has no stage count" if any(r["stages"] is None for r in ed["rows"]) else None)
    if cause and not_written(ed["rows"]) and not ed["reason"].strip():
        out.append(f"{cause}, so the string is recorded and not written: say why.")
    return out


def save(review, entry, *, at) -> dict | None:
    """Apply the copy and close the editor; the step it records, or None when nothing changed.  Sections that differ
    from the proposal make the string manually corrected, with final the sections' value as the CLI builds it
    (`confirm.corrections`) when every section has a catalogue model and a stage count, else null: recorded, not
    written, for the reason given (`not_written`; Save waits for it).  Brought back to the proposal, the string is as it
    started; a save that changes only the marks keeps its status.  An accept-anyway reason does not survive a change of
    sections."""
    problems = save_problems(review)
    if problems:
        raise ValueError(problems[0])
    changed, ed, v = unsaved(review), review["editor"], review["values"][STRING]
    review["editor"] = None
    if not changed:
        return None
    rows, new = ed["rows"], rows_sections(ed["rows"])
    marked = [r["printed"] for r in rows if r["absent"]]
    why = ed["reason"].strip() if not_written(rows) else None
    reason = edited_reason(string_rows(review), v["sections"], rows, marked, why)
    if new != sections(entry):
        status, accept = C.CORRECTED, None
        final = None if not_written(rows) else CLI.corrections([f"{STRING}={json.dumps(new)}"], entry)[0][STRING]
    elif v["status"] == C.CORRECTED:                    # back to the proposal: as it started
        status, final, accept = v["starting"], proposal(entry, STRING), None
    else:                                               # only the marks or their reason changed
        status, final, accept = v["status"], v["final"], v["accept_anyway"]
    v.update(status=status, final=final, accept_anyway=accept, sections=new, unwritten=why,
             display=[{k: r[k] for k in DISPLAY} for r in rows])
    v["history"].append(C.step(status, None, at, reason))
    return v["history"][-1]


def quoted(text) -> str:
    return json.dumps(text, ensure_ascii=False)


def edited_reason(before_rows, before_sections, rows, marked, why) -> str:
    """A save's reason, counted against the string before the save, its rows matched by id: added, rows new to it;
    removed, its rows gone; moved, the fewest kept rows to move to give their new order (the kept rows, less the
    longest run of them still in their old order); changed, each field but the order that differs on a kept row, and
    section_order when a kept section's order was not its row's, since a save numbers sections by row."""
    place = {r["id"]: i for i, r in enumerate(before_rows)}
    kept = [r for r in rows if r["id"] in place]
    run = []                                            # the longest increasing run of old places, by patience
    for i in (place[r["id"]] for r in kept):
        k = bisect.bisect_left(run, i)
        run[k:k + 1] = [i]
    changed = [f for f in C.SECTION_FIELDS
               if (any(before_sections[place[r["id"]]]["section_order"] != place[r["id"]] + 1 for r in kept)
                   if f == "section_order" else any(r[f] != before_rows[place[r["id"]]][f] for r in kept))]
    counts = (("added", len(rows) - len(kept)), ("removed", len(before_rows) - len(kept)), ("moved", len(kept) - len(run)))
    parts = ["edited", *(f"{what} {n}" for what, n in counts if n), *([f"changed {', '.join(changed)}"] if changed else [])]
    parts += [f"not in the catalogue: {', '.join(map(quoted, marked))}"] if marked else []
    parts += [f"not written: {quoted(why)}"] if why is not None else []         # every save that writes no string
    return " · ".join(parts)


def model_options(names, row) -> list[str]:
    """The catalogue's names for a section's pick: its model, then the lookup's candidates, then the rest, sorted."""
    first = [m for m in dict.fromkeys((row["pump_model"], *row["candidates"])) if m in names.names]
    return [*first, *sorted(names.names.difference(first))]


def order_unprinted(entry) -> bool:
    """Whether a proposed section came without its order number: the order the editor shows is then unchecked."""
    return any(not isinstance(s.get("section_order"), int) for s in entry["review"]["pump_string"]["sections"])


# ---- the string's checks, in words ----

AS_READ = ("D17", "conflict", "review", "head sum")         # the checks that read the document, not only the sections
AS_READ_PASS = {"D17": "The document prints one pump string.", "conflict": "No two readings of a section disagree.",
                "review": "No section's reading is flagged.",
                "head sum": "The sections' heads add up to the total dynamic head printed."}
NOT_RUN = {"no pump rows": "no pump sections were read", "no pump rows reached the load": "no pump sections were read",
           "no sections": "no pump sections were read", "no section prints a head": "no section prints a head",
           "only some sections print a head": "only some sections print a head",
           "no TDH printed": "no total dynamic head is printed", "a single section": "the string has one section",
           "a section has two different heads": "a section prints two different heads",
           "the TDH is not one value in the scenario": "the total dynamic head is printed with different values",
           "scenario keys missing on the design-context rows": "the scenarios cannot be told apart",
           "scenario keys missing on the pump rows": "the scenarios cannot be told apart"}
V09_WORDS = ((r"housing_count (\S+) × (\d+) per housing ≠ stages (\S+)",
              lambda m: f"{m[1]} housings of {m[2]} stages make {round(float(m[1]) * int(m[2]))}, not its {m[3]} stages"),
             (r"per-housing stages sum to (\d+), not stages (\S+)",
              lambda m: f"its stages per housing add up to {m[1]}, not its {m[2]} stages"),
             (r"(\d+) per-housing counts listed against housing_count (\S+)",
              lambda m: f"{m[1]} stage counts per housing are listed for its {m[2]} housings"))


def string_checks(sections_, rows, names) -> list[tuple[bool, str]]:
    """The string's checks that read only its sections (`final_string_checks`, as the writer runs them), on the
    sections as they stand, in words, each section named as the engineer counts them: (passed, words); a check that
    did not run is left out.  `rows` give each section's model as printed, candidates and mark."""
    n, out = len(sections_), []
    for c in final_string_checks(sections_, names):
        name, ok = c["check"], c["result"] == "pass"
        if c["result"] == "not run":
            continue
        if name == "sections":
            words = f"{n} section{'s' * (n != 1)}." if ok else "No sections: add them from the document, the deepest first."
        elif name == "V-08":                    # one string: the rule's scenario, its row order, is not news
            words = f"Numbered 1 to {n} from the deepest." if ok else in_words(
                "V-08: " + re.sub(r"scenario \S+: ", "", c["why"]))
        elif name == "V-09":
            words = "The stage counts agree with the housings." if ok else v09_words(sections_)
        elif name == "models":
            words = "Every model is a pump catalogue name." if ok else models_check_words(c["why"], sections_, rows)
        else:
            words = "Every section has its order number and stage count." if ok else in_words(f"complete: {c['why']}")
        out.append((ok, words))
    return out


def v09_words(sections_) -> str:
    """V-09's findings on the sections (the validator's rule, as `final_string_checks` runs it), each section named."""
    out = []
    for _, k, _, _, message in v09(FinalSections(sections_)):
        what = next((words(m) for pattern, words in V09_WORDS if (m := re.fullmatch(pattern, message))),
                    "its stage counts disagree with its housings")
        out.append(f"{sentence_case(section_name(k))}: {what}.")
    return " ".join([*out, "Check the stage counts in the document."])


def models_check_words(why, sections_, rows) -> str:
    """The models check's failures, each section named: marked not in the catalogue, or without a catalogue model."""
    marked, other = [], []
    for i in map(int, re.findall(r"position (\d+)", why)):
        s, r = sections_[i - 1], rows[i - 1]
        if r["absent"]:
            marked.append(f"{section_name(i)} ({r['printed']})")
        elif s["pump_model"] is not None:
            other.append(f"{section_name(i)} ({s['pump_model']}, not a catalogue spelling)")
        else:
            seen = ([f"printed {r['printed']}"] if r["printed"] else []) + (
                [f"could be {join(r['candidates'])}"] if r["candidates"] else [])
            other.append(section_name(i) + (f" ({'; '.join(seen)})" if seen else ""))
    one = len(other) == 1
    out = [f"{sentence_case(join(other))} {'has' if one else 'have'} no catalogue model: pick {'one' if one else 'each'} "
           f"from the catalogue, or mark {'it' if one else 'them'} not in the catalogue. Check the model against the "
           "document."] if other else []
    if marked:
        out.append(f"Marked not in the pump catalogue: {join(marked)}. The string is recorded, not written.")
    return " ".join(out)


def document_checks(entry) -> list[tuple[str, str]]:
    """The string's checks that read the document (D17, conflict, the sections' review flags, the head sum), as the
    entry has them, in words: (result, words).  Edits do not change them."""
    out = []
    for c in entry["review"]["pump_string"]["checks"]:
        if c["check"] not in AS_READ:
            continue
        if c["result"] == "fail":
            words = in_words(f"{c['check']}: {c['why']}")
        elif c["result"] == "pass":
            words = AS_READ_PASS[c["check"]]
        else:
            why = list(dict.fromkeys(filter(None, (NOT_RUN.get(w) for w in c["why"].split("; ")))))
            words = f"Not checked: {join(why)}." if why else "Not checked."
        out.append((c["result"], words))
    return out


def unwritten_words(review) -> str | None:
    """Why the string as it stands puts no pump string in the well's entry, in words; None when it puts one."""
    v = review["values"][STRING]
    if v["final"] is not None:
        return None
    if v["unwritten"] is not None or v["status"] == C.CORRECTED:       # saved so, with its reason; or corrected so
        marked = [d["printed"] for d in v["display"] if d["absent"]]
        lacks = "model" if any(s["pump_model"] is None for s in v["sections"]) else "stage count"
        cause = (f"{join(marked)} {'is' if len(marked) == 1 else 'are'} not in the pump catalogue" if marked
                 else f"a section has no {lacks}")
        return (f"Recorded, not written: the well's entry gets no pump string, as {cause}. "
                + (f"The reason given: “{v['unwritten']}”" if v["unwritten"] else "Edit sections can fix it."))
    secs = v["sections"]
    why = [words for words, hit in (
        ("no sections were read", not secs),
        ("a model is not in the pump catalogue", any(s["pump_model"] is None for s in secs)),
        ("an order number or a stage count is missing",
         any(section_incomplete(s["section_order"], s["stages"], i) for i, s in enumerate(secs, 1)))) if hit]
    return (f"As read, this writes no pump string to the well's entry: {join(why or ['it is not complete'])}. Edit "
            "sections can fix it.")


# ---- what will be recorded ----

def tally(review) -> Counter:
    """Each value by how it ends this confirmation, and those pulled out of a batch on the way."""
    out = Counter()
    for v in review["values"].values():
        own = v["history"][v["since"]:]
        if any((s["reason"] or "").endswith("· pulled out") for s in own):
            out["pulled out"] += 1
        if v["status"] in UNDECIDED:
            out["left to check"] += 1
        elif v["accept_anyway"] and own:
            out["accepted anyway"] += 1
        elif v["status"] == C.CORRECTED and own:
            out["corrected"] += 1
        elif not own:
            out["carried" if v["carried"] else "settled by the extraction"] += 1
        else:
            out["settled in batches" if BATCH.fullmatch(own[-1]["reason"] or "") else "settled one by one"] += 1
    return out


def request(review, entry, run, *, cid, confirmed_at, confirmed_by, environment) -> dict:
    """The request: the CLI's, with each value as this review decided it.  A value not decided goes `to check`, so
    the writer refuses it by name."""
    document = ((run.get("source") or {}).get("pdf") or {}).get("sha256")
    q = CLI.build_request(entry, run, cid=cid, run_id=run["run_id"], document=document, confirmed_at=confirmed_at,
                          confirmed_by=confirmed_by, environment=environment, as_proposed=False, finals={},
                          sections=None, accept_anyway=None, leave=set())
    for item in [*q["values"], q["pump_string"]]:
        v = review["values"][item.get("key", STRING)]
        item.pop("accept_anyway", None)
        item.update(final=v["final"], status=v["status"] if v["status"] in C.FINAL else C.TO_CHECK,
                    history=[{**s, "by": s["by"] or confirmed_by} for s in v["history"]],
                    **({"accept_anyway": v["accept_anyway"]} if v["accept_anyway"] else {}))
    q["pump_string"]["sections"] = [dict(s) for s in review["values"][STRING]["sections"]]
    return q


# ---- around the write ----

def environment_for(organization_id) -> str:
    return "dev" if organization_id == LAB_CHECK else "prd"


def environment_problem(organization_id, environment) -> str | None:
    if organization_id == LAB_CHECK and environment != "dev":
        return f"{LAB_CHECK} writes to test only, never to {place(environment)}."
    return None


def confirmer(mode, environ, store_root=None, caller=None) -> tuple[str | None, str]:
    """(the name confirmations go out under, or None; a note on it, or "").  S3 mode: the STS caller's session
    name, as the CLI reads it.  Local mode reviews only, unless OFFLINE_ENV names who writes into the local store."""
    if mode == "s3":
        try:
            return (caller or CLI.caller)(), ""
        except Exception as err:  # noqa: BLE001 - no or expired credentials: say so, write nothing
            return None, "Not signed in to AWS, so Write is off: sign in, then reload the page."
    name = (environ.get(OFFLINE_ENV) or "").strip()
    if not name:
        return None, "This copy of the app only reviews: Write is off."
    root = Path(store_root or ".").resolve()
    if root == CLI.LAB or CLI.LAB in root.parents:
        return None, ("Write is off: this copy of the app keeps its runs inside its own code folder, where client "
                      "documents must not be written.")
    return name, "Offline: Write saves on this computer only, and nothing reaches production or test."


def design_dates(objects, run_id, current) -> dict:
    """The run's printed design date, read with the writer's own function, against the current entry's design_date
    in the target environment: `older`, `not older`, `can't compare` (either undated) or None (no current entry)."""
    incoming = W.printed_design_date(objects, run_id, [])
    existing = None if current is None else current.get("design_date")
    case = (None if current is None else "can't compare" if incoming is None or existing is None
            else "older" if incoming < existing else "not older")
    return {"incoming": incoming, "current": existing, "case": case}


class Kept:
    """`objects` with each read of a run's files or of its intake PDF kept: once the run has ended they never change.
    Everything else, results above all, is read each time."""

    PREFIXES = (f"{C.ROOT}runs/", f"{C.ROOT}intake/")

    def __init__(self, objects):
        self.objects, self.reads = objects, {}

    def get(self, key):
        if not key.startswith(self.PREFIXES):
            return self.objects.get(key)
        if key not in self.reads:
            self.reads[key] = self.objects.get(key)
        return self.reads[key]

    def put(self, key, body, *, create=False):
        self.objects.put(key, body, create=create)

    def keys(self, prefix):
        return self.objects.keys(prefix)


def check(objects, review, entry, run, *, confirmed_by, environment, catalogue, at) -> list[str]:
    """What the writer would refuse the request for, now: [] when it would accept it."""
    cid = C.new_confirmation_id(at)
    q = request(review, entry, run, cid=cid, confirmed_at=C.timestamp(at), confirmed_by=confirmed_by,
                environment=environment)
    problems = C.request_problems(q, cid)
    return [f"contract: {p}" for p in problems] if problems else W.check(objects, q, cid, catalogue)[0]


def write(objects, review, entry, run, *, environment, confirmed_by, catalogue, offline, wait=WAIT_S,
          poll=CLI.poll) -> dict:
    """Check the request as the writer will, put it create-only and wait for its result, as the CLI does.  `offline`:
    the writer runs here, on `objects` (a local store), instead of on Lambda."""
    refused = environment_problem(run["organization_id"], environment)
    if refused:
        return outcome(None, None, environment, None, reasons=[refused])
    at = W.now()
    cid = C.new_confirmation_id(at)
    q = request(review, entry, run, cid=cid, confirmed_at=C.timestamp(at), confirmed_by=confirmed_by,
                environment=environment)
    reasons = [f"contract: {p}" for p in C.request_problems(q, cid)] or W.check(objects, q, cid, catalogue)[0]
    if reasons:
        return outcome(None, None, environment, None, reasons=reasons)
    try:
        objects.put(C.request_key(cid), C.dump(q), create=True)
    except C.Exists:
        return outcome(None, None, environment, None, reasons=[f"a request {cid} is already there"])
    if offline:
        W.confirm(objects, C.request_key(cid), deadline=at + timedelta(seconds=wait), catalogue=catalogue)
        result = C.read_result(objects, cid)
    else:
        result = poll(objects, cid, wait)
    return outcome(objects, cid, environment, result, document=q["document_sha256"])


def outcome(objects, cid, environment, result, *, document=None, reasons=()) -> dict:
    """What the app says about a write: `not sent`, `no result`, or the result's status.  A result that reads as stale
    while the row is there is written: the row is the commit point."""
    out = {"status": "not sent", "confirmation_id": cid, "reasons": list(reasons), "row_key": None,
           "version_key": None, "note": None, "environment": environment}
    if cid is None:
        return out
    if result is None:
        return {**out, "status": "no result",
                "note": f"No answer within {WAIT_S} s. It may still be written: reopen this run in a few minutes."}
    out.update(status=result["status"], reasons=result.get("reasons") or [], row_key=result.get("row_key"),
               version_key=result.get("version_key"))
    if result["status"] == C.STALE:
        row = C.row_key(environment, cid)
        if row in objects.keys(C.row_prefix(environment)):
            return {**out, "status": C.WRITTEN, "row_key": row,
                    "note": "written; its result wasn't updated: the entry is saved, and there is nothing to redo",
                    "version_key": C.version_key(document, cid) if document else None}
        return {**out, "note": ("Write stopped without finishing and wrote nothing. Try again; if it happens again, "
                                "report it to whoever runs the app.")}
    if result["status"] == C.RUNNING:
        return {**out, "note": f"Still writing after {WAIT_S} s: reopen this run in a few minutes."}
    return out


# ---- the engineer's words: places, kinds of report, notes, refusals, confirmations ----
# No rule id, key, position, run or confirmation id, object key or hash goes on an engineer's screen; the record
# keeps all of them.

PLACES = {"prd": "production", "dev": "test"}
VENDORS = {"F1": "SpyGlass", "F2": "ChampionX", "F3": "SLB", "F4": "Endurance (ELS)", "F5": "Baker Hughes ProLift",
           "F6": "XSize", "F7": "Valiant"}      # extraction/stage/stage.py's family codes, as extraction/assemble.py names them
SOURCE_WORDS = {SOURCE: "the design document", "ops_entered": "outside the document, entered by operations"}
GROUP_WORDS = {"string": "the pump string", "found": "values found", "not found": "values not found"}
NOT_KNOWN = "Not a known vendor report: check every value"
UNSAVED = "Write is off while the pump string has edits not saved: save them, or cancel."
APP_FAULT = ("The app put together something Write cannot take: a fault in the app, not in the review. Report it to "
             "whoever runs the app.")
FILES = "Ask whoever runs the app to check this run's files."
DEPTH_BASIS = {"intake": "the intake", "pump_setting": "the pump setting depth", "pump_discharge": "the pump discharge"}
VENDOR_SAID = {"assumption": "the vendor assumed it", "derived": "the vendor worked it out",
               "input": "the vendor's input"}
YES = ("yes", "true")
ID_LIKE = re.compile(r"\d{8}T\d{6}Z|[0-9a-f]{16,}", re.I)


def place(environment) -> str:
    """Where a write goes, as the engineer says it: production (prd) or test (dev)."""
    return PLACES.get(environment, "an unknown place")


def words_of(text) -> str:
    return str(text).replace("_", " ")


def report_words(run) -> str:
    """What kind of document the run read, for the header: a known vendor's report, or none."""
    if run.get("route") == "generic":
        return "read without its known layout, on purpose" if run.get("forced") is True else "not a known vendor report"
    return f"{VENDORS[run['family']]} report" if run.get("family") in VENDORS else "not recorded"


def document_name(run) -> str | None:
    """The document's file name: the one the run recorded (`source.source_name`), else its PDF path's; None when the
    name is an id (an upload named by its SHA-256 or its run id), so it is not shown."""
    source = run.get("source") or {}
    path = (source.get("pdf") or {}).get("path")
    name = str(source.get("source_name") or (Path(str(path)).name if path else ""))
    return name if name and not ID_LIKE.search(name) else None


def when_words(at) -> str:
    """A recorded UTC time, `2026-10-31T20:19:21.134313`, as `2026-10-31 20:19 UTC`."""
    return f"{str(at).replace('T', ' ')[:16]} UTC" if at else "an unknown time"


def confirmation_words(version) -> str:
    """A confirmation as the engineer knows it: who confirmed, and when."""
    return f"{version.get('confirmed_by') or 'someone'}'s confirmation of {when_words(version.get('confirmed_at'))}"


def run_problem_words(run) -> str | None:
    """A run that stopped after reading the values still has its entry: why it stopped, and what that means."""
    error = run.get("error")
    if not isinstance(error, dict):
        return None
    if error.get("code") == "LOAD_HALTED" and "D17" in str(error.get("message")):
        return ("The document's scenarios print different pump strings, so this run stopped before loading its "
                "readings. The values below are what it read: check which scenario is the design before confirming.")
    return ("This run stopped part-way; the values below are what it read before it stopped. Check each against the "
            "document.")


NOTES = (
    (r"not from the document: points to the design record", lambda m: "not from the document: Write fills it in"),
    (r"not from the document: entered by hand", lambda m: "not from the document: entered by hand"),
    (r"intake", lambda m: "the intake depth as printed"),
    (r"pump setting, not intake", lambda m: "the pump setting depth, as no intake depth is printed"),
    (r"pump setting (.+)", lambda m: f"pump setting depth printed {m[1]}"),
    (r"depth_reference_basis (\w+)", lambda m: f"measured to {DEPTH_BASIS.get(m[1], 'a point the document names')}"),
    (r"value at design load", lambda m: "the value at design load"),
    (r"MD", lambda m: "measured depth"),
    (r"basis: (.+)", lambda m: f"{words_of(m[1])} basis"),
    (r"from API (.+): 141\.5 / \(131\.5 \+ API\)",
     lambda m: f"worked out from the API gravity printed, {m[1]}, as 141.5 / (131.5 + API)"),
    (r"printed as SG", lambda m: "printed as a specific gravity"),
    (r'printed "(.*)" with unit "(.*)"', lambda m: f'printed "{m[1]}" with the unit "{m[2]}"'),
    (r'printed "(.*)"', lambda m: f'printed "{m[1]}"'),
    (r"\+14\.7", lambda m: "14.7 psi added to make it absolute"),
    (r"basis unstated", lambda m: "gauge or absolute not stated"),
    (r"no unit printed", lambda m: "no unit printed"),
    (r"provenance vendor (\w+)", lambda m: VENDOR_SAID.get(m[1], f"from the vendor ({words_of(m[1])})")),
    (r"provenance (.+)", lambda m: f"from {words_of(m[1])}"),
    (r"correlation (.+)", lambda m: f"by the {m[1]} correlation"),
    (r"calculated (\w+)", lambda m: "printed as calculated" if m[1].lower() in YES else "printed as not calculated"),
    (r"equals static (\w+)", lambda m: "printed as equal to the static pressure at the datum" if m[1].lower() in YES
     else "printed as not equal to the static pressure at the datum"),
    (r"datum (.+)", lambda m: f"at a datum of {m[1]}"),
    (r"section order as printed, 1 = deepest", lambda m: "sections numbered as printed, 1 the deepest"),
)


def note_words(note) -> str:
    """A value's note (extraction/well_entry.py builds it from the columns it read) as the engineer's words."""
    parts = [next((words(m) for pattern, words in NOTES if (m := re.fullmatch(pattern, part))), words_of(part))
             for part in str(note).split("; ")]
    return "; ".join(parts) + "."


def keys_words(keys: str) -> str:
    return join([label(k.strip()) for k in keys.split(",")])


PRODUCT = ((r"a number must state a real unit, not 'none'", "{what} needs a unit."),
           (r"unknown unit '(.+)'", "{what} does not take the unit {0}."),
           (r"a (.+) must be a whole number, got (.+)", "{what} must be a whole number of {0}."),
           (r"a non-numeric value must use unit 'none'", "{what} takes no unit."),
           (r"the key name declares '(.+)' but the value says '(.+)'", "{what} is in {0}, not {1}."),
           (r"'.+' is recorded in '(.+)' but the value says '(.+)'", "{what} is in {0}, not {1}."),
           (r"source '.+' is not one of .*", "{what} comes from the design document or from outside it."),
           (r"section_order must be (\d+) here, got (.+)", "{what}: its order number must be {0}, not {1}."),
           (r"pump_model must be a model name", "{what} needs a model name."),
           (r"stages must be a whole number of at least 1, got (.+)",
            "{what}: its stage count must be a whole number of at least 1, not {0}."),
           (r"section_role must be a name when set", "{what}: its role must be a name."),
           (r"expected a non-empty list of sections, deepest section first",
            "The pump string needs at least one section, the deepest first."))


def product_words(detail: str, key: str, position=None) -> str:
    """One of the product's rules (#1218's, restated in extraction/well_entry.py) that a value breaks."""
    what = sentence_case(f"the pump string's {section_name(position)}" if position else label(key))
    for pattern, words in PRODUCT:
        if m := re.fullmatch(pattern, detail):
            return words.format(*("empty" if g == "None" else g for g in m.groups()), what=what)
    return APP_FAULT if re.match(r"expected |unknown key|missing |'\w+' belongs to", detail) else (
        f"The product does not take this value for {label(key)}. Check the value and its unit.")


REFUSALS = (
    (r"contract: .*|entry_method .*|environment .* is not dev or prd|values missing: .*|values not in the entry: .*"
     r"|changed from the proposal but not marked manually corrected: .*|pump string: its final value is not its final "
     r"sections|design_document_ref is the writer's to set.*|row: .*|.* is not .*confirmations/<id>/request\.json",
     lambda m: APP_FAULT),
    (r"manual entry is not supported yet", lambda m: "Entering a well by hand is not supported yet."),
    (re.escape(LAB_CHECK) + r" writes to test only.*", lambda m: m[0]),
    (r"runs/[^/]+/run\.json is not there.*", lambda m: f"This run's own record is missing, so it cannot be written. {FILES}"),
    (r"runs/[^/]+/well_entry\.json is not there.*",
     lambda m: f"This run's proposed entry is missing, so it cannot be written. {FILES}"),
    (r"runs/[^/]+/report\.md is not there.*",
     lambda m: f"This run's report is missing, so its design date cannot be read. {FILES}"),
    (r"intake/[^/]+\.pdf is not there.*", lambda m: "The document's PDF is not where Write keeps it, so it cannot be "
                                                     "written. Ask whoever runs the app to put the PDF back."),
    (r"intake/[^/]+\.pdf is not the document.*", lambda m: "The PDF kept for this run is not the document it read. "
                                                            "Ask whoever runs the app to put the right PDF back."),
    (r"(organization_id|well_id) is (.+); the run's is (.+)",
     lambda m: f"The {'organisation' if m[1] == 'organization_id' else 'well'} is {m[2]} here but {m[3]} in the run. "
               "Reopen the run and try again."),
    (r"document_sha256 is .*", lambda m: "The document is not the one the run read. Reopen the run and try again."),
    (r"left to check: (.+)", lambda m: f"Still to check: {keys_words(m[1])}."),
    (r"(\w+) blocks publish: neither corrected so that its check passes, nor accepted anyway",
     lambda m: f"{label(m[1])} blocks publish: correct it so that it passes its check, or accept it anyway with a "
               "reason."),
    (r"pump string: the order check fails \((.*)\), with no accept-anyway reason",
     lambda m: f"The pump string's sections are not numbered 1 to N from the deepest{order_words(m[1])}. Check the "
               "order in the document, then accept the string anyway with a reason."),
    (r"pump string: .*; and it is neither settled nor manually corrected",
     lambda m: "The pump string has failed checks and is not settled yet. Check it, then settle it or accept it anyway."),
    (r"pump_model '(.+)' is not a catalogue spelling(?:: the catalogue spells it '(.+)')?",
     lambda m: f"The pump model {m[1]} is not spelled as the pump catalogue spells it"
               f"{f' ({m[2]})' if m[2] else ''}. Check the model against the document."),
    (r"#1218: (?:\w+\.)?(\w+)(?:\[(\d+)\])?: (.*)", lambda m: product_words(m[3], m[1], m[2])),
    (r"[A-Z_]+ at \w+: .*", lambda m: "Write failed part-way and wrote nothing. Try again; if it fails again, report it "
                                      "to whoever runs the app."),
    (r"a request .* is already there", lambda m: "A request of the same name is already there. Try Write again."),
)


def refusal_words(reason: str) -> str:
    """Why Write would refuse, or refused, as the engineer's words: a refusal by the writer's checks
    (extraction/confirm_writer.check), the contract's, or the app's own."""
    for pattern, words in REFUSALS:
        if m := re.fullmatch(pattern, reason, re.S):
            return words(m)
    return "Write refused it for a reason the app cannot put in plain words. Report it to whoever runs the app."


def step_words(step, earlier=None) -> str:
    """One history step as the confirmed view shows it.  `earlier`: the confirmation a carried step came from, in
    words (`confirmation_words`)."""
    reason, by = step.get("reason"), step.get("by")
    if by == "extraction":
        return "as read"
    if m := BATCH.fullmatch(reason or ""):
        what = (f"settled in batch {m[1]} ({GROUP_WORDS[m[2]]}): “{m[3]}”" if m[3] is not None
                else f"pulled out of batch {m[1]} ({GROUP_WORDS[m[2]]})")
    elif (reason or "").startswith("carried from "):
        return f"carried from {earlier or 'an earlier confirmation'}"
    elif m := EDITED.fullmatch(reason or ""):
        parts = [f"{n} {what}" for n, what in ((m[1], "added"), (m[2], "removed"), (m[3], "moved")) if n]
        parts += [f"{join([FIELD_WORDS[f] for f in m[4].split(', ')])} changed"] if m[4] else []
        k = len(re.findall(QUOTED, m[5] or ""))
        parts += [f"not written, {k} section{'s' * (k != 1)} not in the catalogue" if k else "not written"] if m[6] else []
        what = "sections edited" + (f" ({'; '.join(parts)})" if parts else "")
    else:
        what = {C.SETTLED: "settled one by one", C.CORRECTED: "corrected", C.TO_CHECK: "left to check"}.get(
            step.get("status"), "decided")
    return f"{what}, by {by}, {when_words(step.get('at'))}"
