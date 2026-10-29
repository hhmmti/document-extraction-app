"""The well's proposed entry, `well_entry.json`: one per run that staged rows for a well.

  pump_parameters, wellbore_parameters
      the product's two maps, each value an envelope `{value, unit, source, note}` with source
      `design_document` and the unit #1218 records the key in (the monorepo's
      `esp_resources_v2/well_parameters.py`).  A key with no value is left out.
  review
      each of the final field list's 18 keys with its starting status (`settled`, `to check` or
      `blocks publish`), its reasons, the printed text, its page and, where scenarios disagree, every
      scenario's value; and the pump string: its sections, its checks and one status.

Values come from the report's staged rows, the rows review.jsonl describes, through the staging
columns the final field list names for each key.  Built after Validate and stage from the StageResult
and the report; nothing here changes a grain, a finding or the review list.  The file holds no
filesystem path: the S3 upload relocates paths only in run.json and source.json.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .catalogue import AMBIGUOUS, CatalogueNames, key
from .stage import transform as T
from .stage import values as V
from .stage.aliases import AliasLookup
from .stage.generic import GENERIC, GenericLookup, canonical_names
from .stage.reports_reader import read_report
from .stage.stage import CATALOGUE, SEED, asset, production_evidence
from .validate.review import _informational, ground
from .validate.rules import v08, v09
from .validate.validate import catalogue

ENTRY = "well_entry.json"
SOURCE = "design_document"
PUMP, WELLBORE = "pump_parameters", "wellbore_parameters"
SETTLED, TO_CHECK, BLOCKS = "settled", "to check", "blocks publish"
PC, DC, CO = "stg_pump_config", "stg_design_context", "stg_curve_observations"
PSIG_TO_PSIA = Decimal("14.7")

# The final field list's 18 keys, in its order, with their map and the unit #1218 records them in.
KEYS = {
    "pump_sections": (PUMP, "none"),
    "setting_depth_tvd_ft": (PUMP, "ft"),
    "setting_depth_md_ft": (PUMP, "ft"),
    "motor_type": (PUMP, "none"),
    "motor_power_factor_fraction": (PUMP, "fraction"),
    "motor_nameplate_hp": (PUMP, "hp"),
    "motor_efficiency_fraction": (PUMP, "fraction"),
    "design_document_ref": (PUMP, "none"),
    "fluid_specific_gravity": (WELLBORE, "sg"),
    "oil_sg": (WELLBORE, "sg"),
    "water_sg": (WELLBORE, "sg"),
    "gas_sg": (WELLBORE, "sg"),
    "bubble_point_psia": (WELLBORE, "psia"),
    "reservoir_pressure_psia": (WELLBORE, "psia"),
    "bottomhole_temperature_f": (WELLBORE, "°F"),
    "perforation_top_depth_ft": (WELLBORE, "ft"),
    "solution_gor_scfstb": (WELLBORE, "scf/stb"),
    "vapor_pressure_psia": (WELLBORE, "psia"),
}
# Never written here, so always empty and settled.
NOT_FROM_THE_DOCUMENT = {
    "design_document_ref": "not from the document: points to the design record",
    "vapor_pressure_psia": "not from the document: entered by hand",
}
# Keys read straight from one staging column: (column, the note's columns, a note builder).
SINGLE = {
    "motor_type": ("motor_type", (), None),
    "motor_power_factor_fraction": ("motor_power_factor", ("motor_power_factor_basis",),
                                    lambda o: f"basis: {o['motor_power_factor_basis']}"
                                    if o.get("motor_power_factor_basis") else None),
    "motor_nameplate_hp": ("motor_nameplate_hp", (), None),
    "motor_efficiency_fraction": ("motor_efficiency_ratio", (), lambda o: "value at design load"),
    "fluid_specific_gravity": ("vendor_mixture_sg", (), None),
    "water_sg": ("water_sg", (), None),
    "gas_sg": ("gas_sg", ("gas_sg_basis",), lambda o: f"basis: {o['gas_sg_basis']}" if o.get("gas_sg_basis") else None),
    "bottomhole_temperature_f": ("reservoir_temp_f", (), None),
    "perforation_top_depth_ft": ("perf_top_md_ft", (), lambda o: "MD"),
    "solution_gor_scfstb": ("solution_gor_rso_scf_stb", (), None),
}
PRESSURE_CLASS = {"psia": "psia", "psig": "psig", "psi": "psi", "(psi)": "psi"}


# ---------------------------------------------------------------------------------------------------
# The staged design-context rows, the review list over them, and Prep's page text
# ---------------------------------------------------------------------------------------------------

@dataclass
class Staged:
    row_key: int           # the row's 1-based position in stg_design_context, as review.jsonl keys it
    scenario: str
    label: str | None
    column: str
    raw: object
    row: dict


@dataclass
class Value:
    key: str
    value: object = None
    note: str | None = None
    staged: Staged | None = None
    columns: tuple = ()               # the staging columns whose review items speak for this value
    reasons: list = field(default_factory=list)
    scenarios: list = field(default_factory=list)
    beside: list = field(default_factory=list)


class Context:
    def __init__(self, report, review, prep, generic):
        self.rows = report.blocks[DC]
        self.keys = T.scenario_keys(self.rows)          # positional where a row prints no key, as D16 reads them
        self.first = "1" if "1" in self.keys else (self.keys[0] if self.keys else None)
        self.items = {(r["table"], r["row_key"], r["field"]): r for r in review if r["row_key"] is not None}
        self.missing = {(r["table"], r["field"]): r for r in review if r["row_key"] is None and r["field"]}
        self.pages = None if prep is None else {p: " ".join(t.split()) for p, t in prep.page_text.items()}
        self.generic = generic

    def staged(self, column) -> list[Staged]:
        return [Staged(k, s, o.get("scenario_label_staged") or o.get("scenario_label"), column, o[column], o)
                for k, (s, o) in enumerate(zip(self.keys, self.rows), 1) if column in o]

    def review_reasons(self, table, row_key, columns) -> list[str]:
        """The reasons of the row's review items that need review; informational ones are not signals."""
        out = []
        for column in (*columns, "extraction_status"):
            item = self.items.get((table, row_key, column))
            if item and item["needs_review"]:
                out += [r for r in item["reasons"] if not _informational(r)]
        return out

    def missing_reasons(self, columns) -> list[str]:
        out = []
        for column in columns:
            item = self.missing.get((DC, column))
            if item and item["needs_review"]:
                out += [r for r in item["reasons"] if not _informational(r)]
        return out

    def page(self, raw, cited) -> tuple[str | None, str]:
        """review's grounding, reused: the cited page if the printed text is on it, else the pages it is on."""
        grounding, found_on = ground(raw, cited, self.pages)
        if grounding == "cited_page":
            return ", ".join(f"p{p}" for p in found_on), "cited page"
        if grounding == "other_page":
            return ", ".join(f"p{p}" for p in found_on), "found on"
        return None, "not found" if grounding == "not_found" else "not evaluated"


def typed(column, raw):
    """The stage's own typing of one staged value (transform.take): None where the stage would quarantine it."""
    if column == "motor_type":
        return str(raw).strip() or None
    parsed = V.parse_scalar(raw, column)
    return None if parsed.reason else parsed.value


def propose(ctx, column, compare):
    """Scenario 1's row that stages `column` (else the first that does), every row that does, and
    whether their values disagree."""
    staged = ctx.staged(column)
    if not staged:
        return None, [], False
    chosen = next((s for s in staged if s.scenario == ctx.first), staged[0])
    values = {compare(s) for s in staged} - {None}
    return chosen, staged, len(values) > 1


def scenarios(staged, value_of) -> list[dict]:
    return [{"scenario": s.scenario, "label": s.label, "printed": s.raw, "value": value_of(s)} for s in staged]


# ---------------------------------------------------------------------------------------------------
# One value per key
# ---------------------------------------------------------------------------------------------------

def single(ctx, key) -> Value:
    column, note_columns, note = SINGLE[key]
    chosen, staged, varies = propose(ctx, column, lambda s: typed(column, s.raw))
    v = Value(key, columns=(column, *note_columns))
    if chosen is None:
        return v
    v.staged, v.value = chosen, typed(column, chosen.raw)
    v.note = note(chosen.row) if note and v.value is not None else None
    if v.value is None:
        v.reasons.append("value_not_typed")
    if varies:
        v.reasons.append("varies_by_scenario")
        v.scenarios = scenarios(staged, lambda s: typed(column, s.raw))
    return v


def depth(ctx, key, intake_column, setting_column) -> Value:
    """The intake depth when printed, the pump-setting depth beside it; else the pump-setting depth, to check."""
    intake, intake_all, intake_varies = propose(ctx, intake_column, lambda s: typed(intake_column, s.raw))
    setting, setting_all, setting_varies = propose(ctx, setting_column, lambda s: typed(setting_column, s.raw))
    v = Value(key, columns=(intake_column, setting_column))
    chosen, every, varies = (intake, intake_all, intake_varies) if intake else (setting, setting_all, setting_varies)
    if chosen is None:
        return v
    v.staged, v.value, v.columns = chosen, typed(chosen.column, chosen.raw), (chosen.column, "depth_reference_basis")
    basis = chosen.row.get("depth_reference_basis")
    if intake:
        v.beside = [{"column": s.column, "scenario": s.scenario, "printed": s.raw,
                     "value": typed(setting_column, s.raw)} for s in setting_all]
        v.note = "; ".join(["intake"] + ([f"pump setting {setting.raw}"] if setting else [])
                           + ([f"depth_reference_basis {basis}"] if basis else []))
    else:
        v.reasons.append("depth_basis_pump_setting")
        v.note = "; ".join(["pump setting, not intake"] + ([f"depth_reference_basis {basis}"] if basis else []))
    if v.value is None:
        v.reasons.append("value_not_typed")
    if varies:
        v.reasons.append("varies_by_scenario")
        v.scenarios = scenarios(every, lambda s: typed(s.column, s.raw))
    return v


def oil_sg(ctx) -> Value:
    """oil_sg as printed; derived from °API only when no SG is printed."""
    def from_api(s):
        api = typed("oil_api_deg", s.raw)
        return None if api is None else round(141.5 / (131.5 + api), 4)

    chosen, staged, varies = propose(ctx, "oil_sg", lambda s: typed("oil_sg", s.raw))
    derive = not any(str(s.raw).strip() for s in staged)          # an empty SG cell is not a printed SG
    if derive:
        chosen, staged, varies = propose(ctx, "oil_api_deg", from_api)
    v = Value("oil_sg", columns=("oil_sg", "oil_api_deg"))
    if chosen is None:
        return v
    value_of = from_api if derive else (lambda s: typed("oil_sg", s.raw))
    v.staged, v.value = chosen, value_of(chosen)
    v.columns = ("oil_api_deg", "oil_sg") if derive else ("oil_sg",)    # an empty SG cell's own review item still counts
    v.note = f"from API {chosen.raw}: 141.5 / (131.5 + API)" if derive else "printed as SG"
    if v.value is None:
        v.reasons.append("value_not_typed")
    if varies:
        v.reasons.append("varies_by_scenario")
        v.scenarios = scenarios(staged, value_of)
    return v


def pressure(ctx, key, column, extras) -> Value:
    """psia as printed; psig + 14.7; bare psi kept as printed, to check; no printed unit, to check."""
    def unit_of(s):
        parsed = V.parse_scalar(s.raw, column)
        token = parsed.unit_token or (str(s.row.get("bubble_point_unit_as_printed") or "").strip().lower() or None
                                      if column == "bubble_point_psi" else None)
        return PRESSURE_CLASS.get(token) if token else None

    def psia(s):
        number = typed(column, s.raw)
        if number is None:
            return None
        return float(Decimal(str(number)) + PSIG_TO_PSIA) if unit_of(s) == "psig" else number

    chosen, staged, varies = propose(ctx, column, lambda s: (typed(column, s.raw), unit_of(s)))
    unit_columns = ("bubble_point_unit_as_printed",) if column == "bubble_point_psi" else ()
    v = Value(key, columns=(column, *unit_columns, *(c for c, _ in extras)))
    if chosen is None:
        return v
    v.staged, v.value = chosen, psia(chosen)
    printed_unit = unit_of(chosen)
    note = [f'printed "{chosen.raw}"' + (f' with unit "{chosen.row.get("bubble_point_unit_as_printed")}"'
                                          if printed_unit and not V.parse_scalar(chosen.raw, column).unit_token else "")]
    if printed_unit == "psig":
        note.append("+14.7")
    elif printed_unit == "psi":
        note.append("basis unstated")
        v.reasons.append("pressure_basis_unstated")
    elif printed_unit is None:
        note.append("no unit printed")
        v.reasons.append("pressure_unit_not_printed")
    note += [f"{label} {chosen.row[c]}" for c, label in extras if chosen.row.get(c)]
    v.note = "; ".join(note)
    if v.value is None:
        v.reasons.append("value_not_typed")
    if varies:
        v.reasons.append("varies_by_scenario")
        v.scenarios = scenarios(staged, psia)
    return v


BUBBLE_EXTRAS = (("bubble_point_provenance", "provenance"), ("bubble_point_correlation", "correlation"),
                 ("bubble_point_calculated_flag", "calculated"), ("bubble_point_equals_static_datum", "equals static"))
RESERVOIR_EXTRAS = (("datum_depth_ft", "datum"),)


# ---------------------------------------------------------------------------------------------------
# The pump string
# ---------------------------------------------------------------------------------------------------

def d31(report, family, evidence):
    """The run's own D31 lookup, rebuilt as stage() builds it (for matching curve rows and scenario strings)."""
    lookup = (GenericLookup(asset(SEED), canonical_names(asset(CATALOGUE))) if family == GENERIC
              else AliasLookup(asset(SEED)))
    lookup.evidence_flags = dict(evidence)
    return lookup


def integer(raw, column):
    parsed = V.parse_int(raw, column)
    return None if parsed.reason else parsed.value


def scenario_strings(report, lookup) -> dict:
    """Each scenario's pump rows, typed and model-grouped as the pump grain is (transform.to_model_grouped)."""
    by_scenario = defaultdict(list)
    for o in report.blocks[PC]:
        printed = o.get("pump_model_as_printed")
        by_scenario[T.scenario_key(o)].append({
            "section_order": integer(o.get("section_order"), "section_order"), "section_role": o.get("section_role"),
            "pump_model_as_printed": printed, "pump_model_canonical": lookup.resolve(printed, report.vendor_family).canonical,
            "stages": integer(o.get("stages"), "stages"), "housing_count": integer(o.get("housing_count"), "housing_count"),
            "stages_per_housing_as_printed": o.get("stages_per_housing_as_printed"),
            "top_md_ft": typed("top_md_ft", o["top_md_ft"]) if "top_md_ft" in o else None,
            "bottom_md_ft": typed("bottom_md_ft", o["bottom_md_ft"]) if "bottom_md_ft" in o else None,
            "source_page": o.get("source_page"), "extraction_status": o.get("extraction_status"),
            "section_grain": o.get("section_grain")})
    return {s: T.to_model_grouped(rows) for s, rows in by_scenario.items()}


def first_scenario(keys):
    return "1" if "1" in keys else sorted(keys, key=lambda s: (len(s), s))[0]


def members(report, row, scenario, lookup) -> list[int]:
    """The 1-based pump-row positions of one model-grouped section: its model, its orders, the scenario kept."""
    orders = {s.strip() for s in str(row.get("section_orders_grouped") or row.get("section_order") or "").split(",")}
    canonical, printed = row.get("pump_model_canonical"), str(row.get("pump_model_as_printed")).strip()
    return [k for k, o in enumerate(report.blocks[PC], 1)
            if T.scenario_key(o) == scenario and str(o.get("section_order") or "").strip() in orders
            and (lookup.resolve(o.get("pump_model_as_printed"), report.vendor_family).canonical == canonical
                 if canonical else str(o.get("pump_model_as_printed")).strip() == printed)]


def section(row, position, names, models, ctx, report, scenario, lookup, generic) -> dict:
    canonical, printed = row.get("pump_model_canonical"), row.get("pump_model_as_printed")
    match = names.lookup(printed if canonical is None else canonical)
    reasons, signals = [], []
    if match.name is None:
        reasons.append(f"model_ambiguous: {', '.join(match.candidates)}" if match.how == AMBIGUOUS
                       else f"model_absent: {canonical}" if canonical is not None else f"model_unresolved: {printed}")
    order, stages = row.get("section_order"), row.get("stages")
    reasons += section_incomplete(order, stages, position)
    for k in members(report, row, scenario, lookup):
        for reason in ctx.review_reasons(PC, k, ("section_order", "section_role", "pump_model_as_printed",
                                                 "pump_model_as_printed_alt", "stages", "housing_count",
                                                 "stages_per_housing_as_printed", "top_md_ft", "bottom_md_ft")):
            (reasons if reason.startswith("conflict:") else signals).append(reason)
    if generic:
        reasons.append("generic_path")
    reasons = list(dict.fromkeys(reasons))
    review = [r for r in dict.fromkeys(signals) if r not in reasons]
    return {"section_order": order, "pump_model": match.name, "match": match.how,
            **({"candidates": list(match.candidates)} if match.candidates else {}),
            "head_curve": models.get(match.name) if match.name else None,
            "pump_model_as_printed": printed, "pump_model_d31": canonical, "stages": stages,
            "section_role": row.get("section_role"),
            "housings": row.get("housing_count"), "stages_per_housing_as_printed": row.get("stages_per_housing_as_printed"),
            "top_md_ft": row.get("top_md_ft"), "bottom_md_ft": row.get("bottom_md_ft"), "page": row.get("source_page"),
            "status": TO_CHECK if reasons or review else SETTLED, "reasons": reasons + review,
            "review_reasons": review}                    # the string's `review` check reads these


def section_incomplete(order, stages, position) -> list[str]:
    """A section's own completeness: its order is its position, and it has a whole number of stages."""
    out = []
    if not isinstance(order, int) or order != position:
        out.append(f"section_incomplete: section_order {order} at position {position}")
    if not isinstance(stages, int) or stages < 1:
        out.append(f"section_incomplete: stages {stages}")
    return out


def section_value(s) -> dict:
    """One item of the pump_sections value."""
    return {"section_order": s["section_order"], "pump_model": s["pump_model"], "stages": s["stages"],
            **({"section_role": s["section_role"]} if isinstance(s["section_role"], str) and s["section_role"].strip()
               else {})}


def check(name, result, why) -> dict:
    return {"check": name, "result": result, "why": why}


def pump_string(result, report, ctx, names, models, lookup, generic):
    pc = report.blocks[PC]
    d17 = (result.log["d17_log"] or [None])[0]
    halted = bool(d17) and d17["status"] == "D17-HALT"
    keys = {T.scenario_key(o) for o in pc}
    strings = scenario_strings(report, lookup) if halted else {}
    if halted:
        scenario = first_scenario(keys)
        rows = strings[scenario]
    else:
        rows = [{k: None if isinstance(v, float) and v != v else v for k, v in r.items()}      # NaN: a pandas gap
                for r in result.pump_config.to_dict("records")]
        rows.sort(key=lambda r: (r["section_order"] is None, r["section_order"] or 0))
        design = (result.log["d16_log"] or [{}])[0].get("selected_scenario")
        scenario = design if design in keys else (sorted(keys)[0] if keys else None)   # as transform.d17_collapse keeps it
    sections = [section(r, i, names, models, ctx, report, scenario, lookup, generic) for i, r in enumerate(rows, 1)]

    findings = defaultdict(list)
    for f in result.findings:
        findings[f["rule_id"]].append(f["message"])
    conflicts = Counter(r for k in range(1, len(pc) + 1)
                        for r in (ctx.items.get((PC, k, "pump_model_as_printed")) or ctx.items.get((PC, k, "extraction_status"))
                                  or {"reasons": []})["reasons"] if r.startswith(("conflict:", "conflict_benign:")))
    hard = {r: n for r, n in conflicts.items() if r.startswith("conflict:")}
    unresolved = [s for s in sections if s["pump_model"] is None]
    incomplete = [(i, s) for i, s in enumerate(sections, 1) if any(r.startswith("section_incomplete") for r in s["reasons"])]
    flagged = [f"position {i}: {r}" for i, s in enumerate(sections, 1) for r in s["review_reasons"]]
    heads = head_sum(report, lookup)
    checks = [
        check("sections", "pass" if sections else "fail", f"{len(sections)} sections" if sections else "no sections"),
        check("V-08", "fail" if findings["V-08"] else "pass" if pc else "not run",
              "; ".join(findings["V-08"]) or ("section order contiguous, 1 deepest" if pc else "no pump rows")),
        check("D17", "not run" if d17 is None else "fail" if halted else "pass",
              "no pump rows reached the load" if d17 is None else
              f"{d17['n_scenarios']} scenarios present different pump strings" if halted else
              {"single_scenario": "one scenario", "collapsed": "the scenarios agree"}.get(
                  d17["status"], f"the scenarios agree; {', '.join(d17['detail'].get('varying_non_identity_fields', []))} vary")),
        check("V-09", "fail" if findings["V-09"] else "pass" if pc else "not run",
              "; ".join(findings["V-09"]) or ("stages agree with housings" if pc else "no pump rows")),
        check("conflict", "fail" if hard else "pass",
              "; ".join(f"{r.split(': ', 1)[1]} ({n} rows)" for r, n in conflicts.items()) or "no conflict rows"),
        check("models", "fail" if unresolved else "pass" if sections else "not run",
              "; ".join(f"{s['pump_model_d31'] or s['pump_model_as_printed']} "
                        f"{'ambiguous: ' + ', '.join(s['candidates']) if s['match'] == AMBIGUOUS else 'not in the catalogue'}"
                        for s in unresolved) or ("every model is a catalogue name" if sections else "no sections")),
        check("complete", "fail" if incomplete else "pass" if sections else "not run",
              "; ".join(f"position {i}: section_order {s['section_order']}, stages {s['stages']}" for i, s in incomplete)
              or ("every section has its order and stages" if sections else "no sections")),
        check("review", "fail" if flagged else "pass" if sections else "not run",      # informational items are not signals
              "; ".join(flagged) or ("no section value needs review" if sections else "no sections")),
        check("head sum", heads["result"], heads["why"]),
    ]
    failed = {c["check"] for c in checks if c["result"] == "fail"}
    reasons = [f"{c['check']}: {c['why']}" for c in checks if c["result"] == "fail"] + (["generic_path"] if generic else [])
    status = BLOCKS if failed & {"V-08", "D17"} else TO_CHECK if reasons else SETTLED
    value = None
    if sections and not unresolved and not incomplete:
        value = [section_value(s) for s in sections]
    record = {"status": status, "reasons": reasons, "scenario": scenario, "checks": checks, "sections": sections,
              "head_sum": heads["scenarios"]}
    if halted:
        record["scenario_strings"] = [{"scenario": s, "sections": [
            {"section_order": r["section_order"], "pump_model_canonical": r["pump_model_canonical"],
             "pump_model_as_printed": r["pump_model_as_printed"], "stages": r["stages"]} for r in strings[s]]}
            for s in sorted(strings, key=lambda s: (len(s), s))]
    return value, record


# ---------------------------------------------------------------------------------------------------
# The head sum, per scenario: the sections' design-point heads against the printed TDH
# ---------------------------------------------------------------------------------------------------

FT = {None, "ft", "(ft)", "feet", "'"}
TDH_RESTATED_FT = 1.0
_NUMBER = re.compile(r"[-+]?[\d,]*\.?\d+")


def printed_number(raw) -> tuple[Decimal, Decimal] | None:
    """(the number as printed, half its last printed digit)."""
    m = _NUMBER.search(str(raw))
    if not m:
        return None
    text = m.group(0).replace(",", "")
    places = len(text.split(".")[1]) if "." in text else 0
    return Decimal(text), Decimal(5) / Decimal(10) ** (places + 1)


def head_ft(o):
    parsed = V.parse_scalar(o.get("head_as_printed"), "head_as_printed")
    unit = (o.get("head_unit_as_printed") or "").strip().lower() or None
    return parsed.value if parsed.value is not None and parsed.unit_token in FT and unit in FT else None


def keyless(o) -> bool:
    return not (str(o.get("scenario_ordinal") or "").strip() or o.get("scenario_label_staged") or o.get("scenario_label"))


def same_model(row_model, row_canonical, section_):
    """D31 canonical equal, else the letters-and-digits keys equal, or one the prefix of the other."""
    if row_canonical and section_["canonical"] and row_canonical == section_["canonical"]:
        return True
    a, b = key(row_model or ""), key(section_["printed"])
    return bool(a and b and (a == b or a.startswith(b) or b.startswith(a)))


def head_sum(report, lookup) -> dict:
    """Sections are a scenario's pump rows grouped by D31 model; candidates are design_point heads in ft.
    Whole-string rows are left out (a string_total head within 1 ft of the scenario's TDH, a row off the
    tapered-composite chart), and per-stage heads.  A row naming a model goes to that model's section; a
    row naming none, to the one section left without a head.  Run on strings of two or more sections
    with every head assigned and the TDH printed; passes when |sum - TDH| is within half the last printed
    digit of each head."""
    fam, pc, dc = report.vendor_family, report.blocks[PC], [o for o in report.blocks[DC] if "vendor_tdh_at_design_ft" in o]
    orders = [str(o.get("section_order")).strip() for o in pc]
    if len(dc) > 1 and any(keyless(o) for o in dc):
        return {"result": "not run", "why": "scenario keys missing on the design-context rows", "scenarios": []}
    if len(orders) != len(set(orders)) and any(keyless(o) for o in pc):
        return {"result": "not run", "why": "scenario keys missing on the pump rows", "scenarios": []}
    pumps, tdhs, curves = defaultdict(list), defaultdict(list), defaultdict(list)
    for o in pc:
        pumps[T.scenario_key(o)].append(o)
    for o in dc:
        tdhs[T.scenario_key(o)].append(o["vendor_tdh_at_design_ft"])
    for o in report.blocks[CO]:
        if o.get("point_type") == "design_point" and head_ft(o) is not None:
            curves[T.scenario_key(o)].append(o)
    out = []
    for s in sorted(set(pumps) | set(tdhs) | set(curves), key=lambda x: (len(x), x)):
        groups = {}
        for o in pumps.get(s, []):
            printed = o.get("pump_model_as_printed")
            canonical = lookup.resolve(printed, fam).canonical
            g = groups.setdefault(canonical or str(printed).strip(),
                                  {"printed": str(printed).strip(), "canonical": canonical, "orders": [], "heads": []})
            n = integer(o.get("section_order"), "section_order")
            g["orders"].append(n if n is not None else 0)
        sections = sorted(groups.values(), key=lambda g: min(g["orders"] or [0]))
        tdh_values = {V.parse_scalar(t, "vendor_tdh_at_design_ft").value for t in tdhs.get(s, [])} - {None}
        tdh_raw = next((t for t in tdhs.get(s, []) if V.parse_scalar(t, "vendor_tdh_at_design_ft").value is not None), None)
        orphans = []
        for o in curves.get(s, []):
            h, basis = head_ft(o), (o.get("head_basis_as_printed") or "").strip()
            if (o.get("curve_observation_role") or "").strip() == "tapered_composite" or basis == "per_stage":
                continue
            if basis == "string_total" and len(tdh_values) == 1 and abs(h - next(iter(tdh_values))) <= TDH_RESTATED_FT:
                continue
            model = o.get("pump_model_as_printed")
            if not model:
                orphans.append(o)
                continue
            canonical = lookup.resolve(model, fam).canonical
            hit = next((g for g in sections if same_model(model, canonical, g)), None)
            if hit:
                hit["heads"].append(o)
        left = [g for g in sections if not g["heads"]]
        if len(left) == 1 and len({head_ft(o) for o in orphans}) == 1:
            left[0]["heads"] += orphans
        result = {"scenario": s, "sections": len(sections),
                  "terms": [{"model": g["canonical"] or g["printed"], "printed": g["heads"][0]["head_as_printed"],
                             "page": g["heads"][0].get("source_page")} for g in sections if g["heads"]],
                  "tdh_printed": tdh_raw}
        several = [g for g in sections if len({head_ft(o) for o in g["heads"]}) > 1]
        if not sections:
            result["result"], result["why"] = "not run", "no pump rows"
        elif len(tdh_values) > 1:
            result["result"], result["why"] = "not run", "the TDH is not one value in the scenario"
        elif several:
            result["result"], result["why"] = "not run", "a section has two different heads"
        elif any(not g["heads"] for g in sections):
            result["result"], result["why"] = "not run", ("no section prints a head" if not any(g["heads"] for g in sections)
                                                          else "only some sections print a head")
        elif not tdh_values:
            result["result"], result["why"] = "not run", "no TDH printed"
        elif len(sections) < 2:
            result["result"], result["why"] = "not run", "a single section"
        else:
            terms = [printed_number(g["heads"][0]["head_as_printed"]) for g in sections]
            total, tolerance = sum(t[0] for t in terms), sum(t[1] for t in terms)
            tdh = printed_number(tdh_raw)[0]
            diff = total - tdh
            result.update(sum=str(total), tdh=str(tdh), diff=str(diff), tolerance=str(tolerance),
                          result="pass" if abs(diff) <= tolerance else "fail",
                          why=f"{total} vs TDH {tdh}: {diff:+} ft, within ±{tolerance}" if abs(diff) <= tolerance
                          else f"{total} vs TDH {tdh}: {diff:+} ft, outside ±{tolerance}")
        out.append(result)
    ran = [r for r in out if r["result"] != "not run"]
    if not ran:
        return {"result": "not run", "why": "; ".join(dict.fromkeys(r["why"] for r in out)) or "no pump rows",
                "scenarios": out}
    failed = [r for r in ran if r["result"] == "fail"]
    return {"result": "fail" if failed else "pass",
            "why": "; ".join(f"scenario {r['scenario']}: {r['why']}" for r in (failed or ran)), "scenarios": out}


# ---------------------------------------------------------------------------------------------------
# The entry
# ---------------------------------------------------------------------------------------------------

def build_entry(result, report_text, family, organization_id, well_id, *, prep=None, evidence=None) -> dict | None:
    """The entry for one staged run, or None: no well, or an empty extraction (EXTRACTION_EMPTY)."""
    if well_id is None or any(f["rule_id"] == "G-E1" for f in result.findings):
        return None
    report = read_report(report_text, family)
    generic = family == GENERIC
    ctx = Context(report, result.review, prep, generic)
    models = catalogue()
    names = CatalogueNames(models)
    lookup = d31(report, family, production_evidence(report_text) if evidence is None else evidence)

    values = {}
    for key in KEYS:
        if key in NOT_FROM_THE_DOCUMENT:
            values[key] = Value(key, reasons=[NOT_FROM_THE_DOCUMENT[key]])
        elif key in SINGLE:
            values[key] = single(ctx, key)
        elif key == "setting_depth_tvd_ft":
            values[key] = depth(ctx, key, "intake_set_depth_tvd_ft", "pump_setting_vd_ft")
        elif key == "setting_depth_md_ft":
            values[key] = depth(ctx, key, "intake_set_depth_md_ft", "pump_setting_md_ft")
        elif key == "oil_sg":
            values[key] = oil_sg(ctx)
        elif key == "bubble_point_psia":
            values[key] = pressure(ctx, key, "bubble_point_psi", BUBBLE_EXTRAS)
        elif key == "reservoir_pressure_psia":
            values[key] = pressure(ctx, key, "static_datum_pressure_psi", RESERVOIR_EXTRAS)
    sections, string = pump_string(result, report, ctx, names, models, lookup, generic)

    maps, reviewed = {PUMP: {}, WELLBORE: {}}, []
    for key, (map_, unit) in KEYS.items():
        if key == "pump_sections":
            if sections is not None:
                maps[map_][key] = {"value": sections, "unit": unit, "source": SOURCE,
                                   "note": "section order as printed, 1 = deepest"}
            reviewed.append({"key": key, "map": map_, "status": string["status"], "reasons": string["reasons"],
                             "value": sections, "unit": unit, "written": sections is not None})
            continue
        v = values[key]
        if key in NOT_FROM_THE_DOCUMENT:
            reviewed.append({"key": key, "map": map_, "status": SETTLED, "reasons": [], "value": None, "unit": unit,
                             "written": False, "note": v.reasons[0]})
            continue
        reasons = list(v.reasons)
        reasons += (ctx.review_reasons(DC, v.staged.row_key, v.columns) if v.staged else ctx.missing_reasons(v.columns))
        if generic:
            reasons.append("generic_path")
        reasons = list(dict.fromkeys(reasons))
        page, basis = ctx.page(v.staged.raw, v.staged.row.get("source_page")) if v.staged else (None, None)
        written = v.value is not None
        if written:
            envelope = {"value": v.value, "unit": unit, "source": SOURCE}
            if v.note:
                envelope["note"] = v.note
            maps[map_][key] = envelope
        reviewed.append({"key": key, "map": map_, "status": TO_CHECK if reasons else SETTLED, "reasons": reasons,
                         "value": v.value, "unit": unit, "written": written,
                         "printed": None if v.staged is None else v.staged.raw,
                         "column": None if v.staged is None else v.staged.column,
                         "scenario": None if v.staged is None else (v.staged.label or v.staged.scenario),
                         "page": page, "page_basis": basis, "note": v.note,
                         **({"scenarios": v.scenarios} if v.scenarios else {}),
                         **({"beside": v.beside} if v.beside else {})})
    counts = {s: sum(1 for r in reviewed if r["status"] == s) for s in (SETTLED, TO_CHECK, BLOCKS)}
    document = [r for item in result.review if item["field"] is None and item["needs_review"] for r in item["reasons"]]
    return {"organization_id": organization_id, "well_id": well_id, **maps,
            "review": {"family": family, "route": GENERIC if generic else "template",
                       "summary": {**counts, "nothing_to_check": counts[SETTLED] == len(KEYS)},
                       **({"document_reasons": document} if document else {}),
                       "values": reviewed, "pump_string": string}}


def entry_bytes(entry: dict) -> bytes:
    return (json.dumps(entry, indent=1, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------------------------------
# The string's checks, re-run on a confirmation's final sections (extraction/confirm_writer.py)
# ---------------------------------------------------------------------------------------------------

STAGED_AS = (("section_order", "section_order"), ("stages", "stages"), ("housing_count", "housings"),
             ("stages_per_housing_as_printed", "stages_per_housing_as_printed"), ("top_md_ft", "top_md_ft"),
             ("bottom_md_ft", "bottom_md_ft"))


class FinalSections:
    """Final sections as the validator's rules read a report: one scenario's stg_pump_config rows, each value the
    string a report would print."""

    def __init__(self, sections):
        self.pump_rows = [{column: self.printed(s[name]) for column, name in STAGED_AS if s.get(name) is not None}
                          for s in sections]

    @staticmethod
    def printed(value) -> str:
        return ", ".join(map(str, value)) if isinstance(value, list) else str(value)

    def rows(self, table):
        return enumerate(self.pump_rows if table == PC else [], 1)


def order_check(sections) -> dict:
    """V-08 on final sections: the validator's own rule (contiguous 1..N, 1 deepest by the depth columns)."""
    fired = [message for *_, message in v08(FinalSections(sections))]
    return check("V-08", "fail" if fired else "pass" if sections else "not run",
                 "; ".join(fired) or ("section order contiguous, 1 deepest" if sections else "no sections"))


def final_string_checks(sections, names) -> list[dict]:
    """The string's checks that read its sections alone, re-run on final sections with the functions that made
    them: sections, V-08 and V-09 (the validator's rules), models (the catalogue lookup) and complete
    (`section_incomplete`).  D17, conflict, review and the head sum read the report; they stay as the entry has them."""
    fired = [message for *_, message in v09(FinalSections(sections))]
    lookups = [(i, s.get("pump_model"), names.lookup(s.get("pump_model"))) for i, s in enumerate(sections, 1)]
    unresolved = [(i, model, m) for i, model, m in lookups if m.name is None]
    short = [(i, s) for i, s in enumerate(sections, 1) if section_incomplete(s.get("section_order"), s.get("stages"), i)]
    return [
        check("sections", "pass" if sections else "fail", f"{len(sections)} sections" if sections else "no sections"),
        order_check(sections),
        check("V-09", "fail" if fired else "pass" if sections else "not run",
              "; ".join(fired) or ("stages agree with housings" if sections else "no pump rows")),
        check("models", "fail" if unresolved else "pass" if sections else "not run",
              "; ".join(f"position {i}: no model" if model is None else
                        f"position {i}: {model} ambiguous: {', '.join(m.candidates)}" if m.how == AMBIGUOUS else
                        f"position {i}: {model} not in the catalogue" for i, model, m in unresolved)
              or ("every model is a catalogue name" if sections else "no sections")),
        check("complete", "fail" if short else "pass" if sections else "not run",
              "; ".join(f"position {i}: section_order {s.get('section_order')}, stages {s.get('stages')}" for i, s in short)
              or ("every section has its order and stages" if sections else "no sections")),
    ]


# ---------------------------------------------------------------------------------------------------
# The product's rules for the two maps (#1218's well_parameters.py at 6831cbd6), restated: the confirmation
# writer checks every row's maps with them, and tests/test_confirm.py checks they agree with the product's own.
# ---------------------------------------------------------------------------------------------------

SOURCES = ("measured", "ops_entered", "estimated", SOURCE)
ENVELOPE_KEYS = ("value", "unit", "source", "as_of", "note")
UNITS = {"none", "id", "ft", "in", "psi", "psia", "scf/stb", "°F", "bbl/day", "mcf/day", "A", "V", "kW", "hp", "Hz",
         "count", "sg", "fraction", "%"}
UNIT_BY_SUFFIX = {"id": "id", "ft": "ft", "in": "in", "psi": "psi", "psia": "psia", "scfstb": "scf/stb", "f": "°F",
                  "bpd": "bbl/day", "mcfd": "mcf/day", "amps": "A", "volts": "V", "kw": "kW", "hp": "hp", "hz": "Hz",
                  "count": "count", "sg": "sg", "fraction": "fraction", "pct": "%"}
RETIRED = {"pump_id": (PUMP, "id"), "stages": (PUMP, "count"), "setting_depth_ft": (PUMP, "ft")}   # read, not written
DESCRIBED = {**KEYS, **RETIRED}
SECTION_KEYS = ("section_order", "pump_model", "stages", "section_role")


def _number(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float, Decimal))


def _whole(v) -> bool:
    return _number(v) and math.isfinite(v) and float(v) == int(float(v))


def sections_problem(key, sections) -> str | None:
    """The pump string's first problem, as #1218's validate_pump_sections finds it; None when there is none."""
    if not isinstance(sections, list) or not sections:
        return f"{key}: expected a non-empty list of sections, deepest section first"
    for position, s in enumerate(sections, 1):
        where = f"{key}[{position}]"
        if not isinstance(s, dict):
            return f"{where}: expected a section object, got {type(s).__name__}"
        unknown = sorted(set(s) - set(SECTION_KEYS))
        if unknown:
            return f"{where}: unknown key(s) {unknown}"
        missing = [k for k in SECTION_KEYS[:3] if s.get(k) is None]
        if missing:
            return f"{where}: missing {missing}"
        if not _whole(s["section_order"]) or int(s["section_order"]) != position:
            return f"{where}: section_order must be {position} here, got {s['section_order']!r}"
        if not isinstance(s["pump_model"], str) or not s["pump_model"].strip():
            return f"{where}: pump_model must be a model name"
        if not _whole(s["stages"]) or int(s["stages"]) < 1:
            return f"{where}: stages must be a whole number of at least 1, got {s['stages']!r}"
        role = s.get("section_role")
        if role is not None and (not isinstance(role, str) or not role.strip()):
            return f"{where}: section_role must be a name when set"
    return None


def envelope_problem(key, envelope) -> str | None:
    """The first rule an envelope breaks, in the order #1218's validate_envelope checks them; None when it passes."""
    if not isinstance(envelope, dict):
        return f"{key}: expected {{value, unit, source}}, got {type(envelope).__name__}"
    unknown = sorted(set(envelope) - set(ENVELOPE_KEYS))
    if unknown:
        return f"{key}: unknown key(s) {unknown}"
    missing = [k for k in ENVELOPE_KEYS[:3] if envelope.get(k) is None]
    if missing:
        return f"{key}: missing {missing}"
    if envelope["source"] not in SOURCES:
        return f"{key}: source {envelope['source']!r} is not one of {list(SOURCES)}"
    unit, value = envelope["unit"], envelope["value"]
    if _number(value):
        if unit == "none":
            return f"{key}: a number must state a real unit, not 'none'"
        if unit not in UNITS:
            return f"{key}: unknown unit {unit!r}"
        if unit in ("id", "count") and not _whole(value):
            return f"{key}: a {unit} must be a whole number, got {value}"
    elif unit != "none":
        return f"{key}: a non-numeric value must use unit 'none'"
    bare = str(key).rsplit(".", 1)[-1]
    declared = UNIT_BY_SUFFIX.get(str(key).rsplit("_", 1)[-1].lower())
    if declared is not None and declared != unit:
        return f"{key}: the key name declares {declared!r} but the value says {unit!r}"
    if declared is None and bare in DESCRIBED and DESCRIBED[bare][1] != unit:
        return f"{key}: {bare!r} is recorded in {DESCRIBED[bare][1]!r} but the value says {unit!r}"
    return sections_problem(key, value) if bare == "pump_sections" else None


def parameter_problems(mapping, field) -> list[str]:
    """#1218's validate_parameter_map, listing each refused key's first problem where the product raises on the
    first: [] when the product's validator accepts the map.  None and {} pass, as there."""
    if mapping is None:
        return []
    if not isinstance(mapping, dict):
        return [f"{field}: expected a map of parameters"]
    out = []
    for key, envelope in mapping.items():
        if key in DESCRIBED and DESCRIBED[key][0] != field:
            out.append(f"{field}.{key}: {key!r} belongs to {DESCRIBED[key][0]}, not {field}")
        elif problem := envelope_problem(f"{field}.{key}", envelope):
            out.append(problem)
    return out
