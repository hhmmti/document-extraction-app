"""Per-value review: one record per in-scope staged value, with the signals that fired.

Scope (design sheet §5): the pump-string fields of `stg_pump_config` and the
design-context columns the well item draws on.  Signals:

  grounding         the verbatim value is not on its cited page (`other_page`) or
                    on no kept page (`not_found`); see `ground`
  rule              a finding names this value, its row, or its column
  conflict          the staging row says extraction_status = conflict (read here,
                    before D17 rewrites it).  One item per conflict row, on the row's
                    model field, or on extraction_status where the row has none.
                    `conflict_benign` (informational, no review) where both printed
                    forms resolve through D31 to the same canonical model
  quarantined       the stage quarantined this value
  known_issue       the field is the one its family's known-issue notice names
  frequency_trap    F1: scenario_title_frequency_disagrees = true in the value's scenario
  expected_missing  a field the family prints in every corpus report is not staged;
                    where the family's supply is partial, the signal is not evaluated

The emptiness guards G-E2 and G-E3 each add one item of their own, with no field
and the guard's message as its reason: `no pump rows extracted`, `model halt: <rule>`.
On the generic route every item also carries `generic_path` and needs review:
no vendor template checked any value.

A review never blocks a run and never changes a value.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from ..stage import transform as T
from ..stage.generic import GENERIC
from .rules import page_numbers, text

PUMP_STRING = ("section_order", "section_role", "pump_model_as_printed", "pump_model_as_printed_alt",
               "manufacturer", "housing_count", "stages", "stages_basis", "stages_per_housing_as_printed",
               "top_md_ft", "bottom_md_ft")
DESIGN_CONTEXT = ("intake_set_depth_tvd_ft", "pump_setting_vd_ft", "intake_set_depth_md_ft", "pump_setting_md_ft",
                  "depth_reference_basis", "motor_type", "motor_power_factor", "motor_power_factor_basis",
                  "motor_nameplate_hp", "motor_efficiency_ratio", "vendor_mixture_sg", "oil_sg", "oil_api_deg",
                  "water_sg", "gas_sg", "gas_sg_basis", "bubble_point_psi", "bubble_point_unit_as_printed",
                  "static_datum_pressure_psi", "datum_depth_ft", "reservoir_temp_f", "perf_top_md_ft",
                  "solution_gor_rso_scf_stb")
SCOPE = (("stg_pump_config", PUMP_STRING), ("stg_design_context", DESIGN_CONTEXT))

# Never printed, so never grounded: staging vocabulary, and the extractor's own
# bookkeeping (metadata).
VOCABULARY = {"section_order", "section_role", "stages_basis", "depth_reference_basis",
              "motor_power_factor_basis", "gas_sg_basis", "motor_type"}
METADATA = {"extraction_status", "source_page", "source_document", "vendor_family", "scenario_ordinal",
            "is_design_scenario", "scenario_selection_rule", "sensitivity_case", "section_grain",
            "printed_order_direction", "model_normalization_flag", "provenance", "provenance_json",
            "curve_observation_role", "point_type", "envelope_status", "digitization_candidate_page",
            "observation_is_composite", "viscosity_corrected", "head_basis_as_printed", "power_basis_as_printed",
            "bubble_point_provenance", "bubble_point_equals_static_datum"}
NOT_GROUNDED = VOCABULARY | METADATA
MODEL_FIELD = {"stg_pump_config": "pump_model_as_printed"}

KNOWN_ISSUE = {"F3": ("stg_pump_config", "pump_model_as_printed"),
               "F5": (None, "scenario_label_staged"),
               "F7": ("stg_gas_cascade", "turpin_indicator_as_printed")}

# The design sheet's per-family mapping: corpus reports that stage each column,
# out of the family's corpus reports.  All of them -> expected; none -> not printed;
# some -> supply uncertain, so expected_missing is not evaluated.
CORPUS_REPORTS = {"F1": 30, "F2": 5, "F3": 5, "F4": 3, "F5": 2, "F7": 1}
_ALL = dict(CORPUS_REPORTS)
COVERAGE = {
    "section_order": _ALL, "pump_model_as_printed": _ALL, "stages": _ALL, "section_role": _ALL,
    "intake_set_depth_tvd_ft": {"F1": 24},
    "pump_setting_vd_ft": {"F2": 5, "F7": 1},
    "intake_set_depth_md_ft": {"F1": 30, "F2": 5, "F3": 5, "F5": 2},
    "pump_setting_md_ft": {"F2": 5, "F4": 3, "F7": 1},
    "depth_reference_basis": {"F1": 30, "F2": 5, "F4": 3, "F5": 2, "F7": 1},
    "motor_type": {"F3": 1, "F4": 1, "F5": 1},
    "motor_power_factor": {"F1": 28, "F2": 5, "F5": 2, "F7": 1},
    "motor_power_factor_basis": {"F1": 21, "F2": 5, "F5": 1, "F7": 1},
    "motor_nameplate_hp": _ALL,
    "motor_efficiency_ratio": {"F1": 28, "F2": 5, "F3": 4, "F5": 2, "F7": 1},
    "vendor_mixture_sg": {"F2": 5, "F4": 3, "F7": 1},
    "oil_sg": {"F1": 1, "F2": 5, "F5": 1, "F7": 1},
    "oil_api_deg": _ALL, "water_sg": _ALL, "gas_sg": _ALL,
    "gas_sg_basis": {"F7": 1},
    "bubble_point_psi": {"F1": 30, "F2": 5, "F3": 5, "F4": 3, "F7": 1},
    "bubble_point_unit_as_printed": {"F1": 30, "F2": 5, "F3": 5, "F4": 3, "F7": 1},
    "static_datum_pressure_psi": {"F1": 30, "F2": 1, "F3": 2, "F5": 2},
    "datum_depth_ft": {"F1": 30, "F2": 5},
    "reservoir_temp_f": {"F1": 2, "F2": 5, "F3": 5, "F4": 3, "F5": 2, "F7": 1},
    "perf_top_md_ft": {"F1": 30, "F3": 5, "F5": 2, "F7": 1},
    "solution_gor_rso_scf_stb": {"F2": 2, "F7": 1},
}
# The sheet marks this one in doubt: 0 of 5 corpus reports, 3 of 3 new runs.
UNCERTAIN = {("F3", "depth_reference_basis")}

_LOAD_TO_STG = {"pump_config": "stg_pump_config", "esp_well_design_context": "stg_design_context",
                "curve_observations": "stg_curve_observations"}
_NUMBER = re.compile(r"[-+]?[\d,]*\.?\d+")


def supply(family: str, column: str) -> str:
    """`expected`, `absent` or `uncertain`, from the sheet's per-family mapping."""
    n, of = COVERAGE[column].get(family, 0), CORPUS_REPORTS.get(family, 0)
    if (family, column) in UNCERTAIN or 0 < n < of:
        return "uncertain"
    return "expected" if of and n == of else "absent"


def _pattern(needle: str):
    pre = r"(?<![\d.,])" if needle[0].isdigit() else ""
    post = r"(?![\d]|[.,]\d)" if needle[-1].isdigit() else ""
    return re.compile(pre + re.escape(needle) + post)


def _found(page: str, needle: str) -> bool:
    """The verbatim string, or failing that the value's leading number as printed."""
    if _pattern(needle).search(page):
        return True
    m = _NUMBER.match(needle)
    return bool(m) and m.group(0) != needle and _pattern(m.group(0)).search(page) is not None


def ground(value, cited, pages) -> tuple[str, list]:
    """(grounding, pages found on).  `pages` is whitespace-collapsed kept-page text."""
    needle = " ".join(str(value).split())
    if pages is None or not needle:
        return "not_evaluated", []
    cited_pages = [p for p in page_numbers(cited) if p in pages]
    if any(_found(pages[p], needle) for p in cited_pages):
        return "cited_page", cited_pages
    elsewhere = [p for p in sorted(pages) if p not in cited_pages and _found(pages[p], needle)]
    return ("other_page", elsewhere) if elsewhere else ("not_found", [])


def review(doc, findings, quarantine_rows) -> list[dict]:
    pages = None if doc.pages is None else {p: " ".join(t.split()) for p, t in doc.pages.items()}
    cell, row, column, table_wide = (defaultdict(list) for _ in range(4))
    for f in findings:
        if f["table"] is None:
            continue
        if f["row_key"] is not None and f["column"]:
            cell[(f["table"], f["row_key"], f["column"])].append(f["rule_id"])
        elif f["row_key"] is not None:
            row[(f["table"], f["row_key"])].append(f["rule_id"])
        elif f["column"]:
            column[(f["table"], f["column"])].append(f["rule_id"])
        else:
            table_wide[f["table"]].append(f["rule_id"])
    quarantined = {(_LOAD_TO_STG.get(q["table"], q["table"]), q["column"], json.dumps(q["raw_value"]),
                    q["source_page"]): q["reason"] for q in quarantine_rows}
    trap = {T.scenario_key(o) for t in doc.report.blocks for _, o in doc.rows(t)
            if text(o, "scenario_title_frequency_disagrees").lower() == "true"} if doc.family == "F1" else set()
    known = KNOWN_ISSUE.get(doc.family)

    out = []
    for table, fields in SCOPE:
        rows = list(doc.rows(table))
        for k, o in rows:
            conflict = _conflict(doc, o) if text(o, "extraction_status") == "conflict" else None
            anchor = MODEL_FIELD.get(table) if MODEL_FIELD.get(table) in o else "extraction_status"
            for field in fields:
                if field not in o:
                    continue
                value, cited = o[field], o.get("source_page")
                grounding, found_on = ("not_evaluated", []) if field in NOT_GROUNDED else ground(value, cited, pages)
                reasons = []
                if grounding == "not_found":
                    reasons.append("grounding: not_found on any kept page")
                elif grounding == "other_page" and page_numbers(cited):
                    reasons.append(f"grounding: other_page (cited {cited}; found on "
                                   f"{', '.join(f'p{p}' for p in found_on)})")
                rules = (cell[(table, k, field)] + row[(table, k)] + column[(table, field)] + table_wide[table])
                reasons += [f"rule: {r}" for r in dict.fromkeys(rules)]
                if conflict and field == anchor:
                    reasons.append(conflict)
                reason = quarantined.get((table, field, json.dumps(value), o.get("source_page")))
                if reason:
                    reasons.append(f"quarantined: {reason}")
                if known and known[1] == field and known[0] in (None, table):
                    reasons.append(f"known_issue: {doc.family}")
                if T.scenario_key(o) in trap:
                    reasons.append("frequency_trap: scenario_title_frequency_disagrees = true in this scenario")
                out.append(_record(field, table, k, value, cited, grounding, reasons))
            if conflict and anchor not in fields:
                out.append(_record(anchor, table, k, o.get(anchor), o.get("source_page"), "not_evaluated", [conflict]))
        for field in fields:
            if field not in COVERAGE or any(field in o for _, o in rows):
                continue
            state = supply(doc.family, field)
            if state == "expected":
                out.append(_record(field, table, None, None, None, "not_evaluated",
                                   ["expected_missing: staged in every corpus report of this family"]))
            elif state == "uncertain":
                n, of = COVERAGE[field].get(doc.family, 0), CORPUS_REPORTS[doc.family]
                out.append(_record(field, table, None, None, None, "not_evaluated",
                                   [f"expected_missing: not_evaluated (supply uncertain: {n} of {of} corpus reports)"]))
    out += [_record(None, f["table"], None, None, None, "not_evaluated", [f["message"]])
            for f in findings if f["rule_id"] in ("G-E2", "G-E3")]
    if doc.family == GENERIC:
        for item in out:
            item["reasons"].append("generic_path")
            item["needs_review"] = True
    return out


def _conflict(doc, o) -> str:
    """The reason for one conflict row: benign when both printed forms are one model."""
    printed, alt = text(o, "pump_model_as_printed"), text(o, "pump_model_as_printed_alt")
    if printed and alt:
        canonical = doc.lookup.resolve(printed, doc.report.vendor_family).canonical
        if canonical and canonical == doc.lookup.resolve(alt, doc.report.vendor_family).canonical:
            return f"conflict_benign: both printed forms resolve to {canonical}"
    return "conflict: extraction_status = conflict in staging"


def _informational(reason: str) -> bool:
    return "not_evaluated" in reason or reason.startswith("conflict_benign")


def _record(field, table, row_key, value, cited, grounding, reasons) -> dict:
    return {"field": field, "table": table, "row_key": row_key, "value": value, "cited_page": cited,
            "grounding": grounding, "reasons": reasons,
            "needs_review": any(not _informational(r) for r in reasons)}
