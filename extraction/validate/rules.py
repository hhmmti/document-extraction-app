"""The validator spec's implementable rules, as the approved design sheet lists them.

Each rule reads one report's parsed staging rows (before the stage transforms
anything) and yields `(table, row_key, column, value, message)` for every
place it fires.  `row_key` is the row's 1-based position in its `stg_*` block.
Spec ids, severities and scopes are the spec's; round-1 severity comes from
the locked map in `finding`.  A rule that needs Detect's page roles or Prep's
page text says so in `needs` and is skipped, with a notice, without them.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from ..families import PAGE_MAP
from ..stage import schemas
from ..stage import transform as T
from ..stage import values as V

STRUCTURE = {"G-S1", "G-S2", "G-S3"}
EMPTY = "G-E1"
BLOCKS_PUBLISH = {"V-08", "D17", EMPTY}
ENVELOPE = {"bep", "ror_min", "ror_max"}


def finding(rule_id, spec_severity, scope, table, row_key, column, value, message) -> dict:
    """One finding.  Round-1 severity is the locked map: structure guards fail the run,
    D17 halts the load, an empty extraction fails the run, everything else is a warning."""
    round1 = ("REPORT_INVALID" if rule_id in STRUCTURE else "LOAD_HALTED" if rule_id == "D17"
              else "EXTRACTION_EMPTY" if rule_id == EMPTY else "warning")
    return {"rule_id": rule_id, "spec_severity": spec_severity, "round1_severity": round1, "scope": scope,
            "table": table, "row_key": row_key, "column": column, "value": value, "message": message,
            "blocks_publish": rule_id in BLOCKS_PUBLISH}


@dataclass
class Doc:
    report: object          # reports_reader.Report
    family: str             # F1..F7
    lookup: object          # aliases.AliasLookup, with the run's evidence
    roles: dict | None      # page -> role names, from Detect
    pages: dict | None      # page -> text, from Prep (kept pages only)

    def rows(self, table):
        return enumerate(self.report.blocks[table], 1)


def text(obj, column) -> str:
    v = obj.get(column)
    return "" if v is None else str(v).strip()


def has(obj, column) -> bool:
    return text(obj, column) != ""


def num(obj, column):
    """The stage's own typing of a staged value; None when absent or unparseable."""
    return V.parse_scalar(obj.get(column), column).value if has(obj, column) else None


_PAGES = re.compile(r"p?\s*(\d+)(?:\s*[-–]\s*p?\s*(\d+))?", re.I)


def page_numbers(source_page) -> list[int]:
    """Every page a `source_page` value names: `p5`, `p5, p6`, `p13-14`, `p2 (Pumps)`."""
    out = []
    for a, b in _PAGES.findall(str(source_page or "")):
        a = int(a)
        b = int(b) if b else a
        out += list(range(a, b + 1)) if a <= b <= a + 50 else [a]
    return sorted(set(out))


def keys(doc, pattern):
    """(table, row_key, key, value) for every staged key matching `pattern`."""
    for table in doc.report.blocks:
        for k, o in doc.rows(table):
            for key, value in o.items():
                if pattern.search(key):
                    yield table, k, key, value


RULES = []      # (rule_id, spec_severity, scope, family or None, needs, function), in coverage-table order


def rule(rule_id, spec_severity, scope, family=None, needs=()):
    def register(fn):
        RULES.append((rule_id, spec_severity, scope, family, tuple(needs), fn))
        return fn
    return register


def run(doc):
    """(findings, skipped) where skipped is [(rule_id, missing inputs)]."""
    findings, skipped = [], []
    for rule_id, spec, scope, family, needs, fn in RULES:
        if family and family != doc.family:
            continue
        missing = [n for n in needs if getattr(doc, n) is None]
        if missing:
            skipped.append((rule_id, missing))
            continue
        for table, row_key, column, value, message in fn(doc):
            findings.append(finding(rule_id, spec, scope, table, row_key, column, value, message))
    return findings, skipped


# Contract §4 model grammars, verbatim, keyed by family (F5: the summary form, which
# is what pump_model_as_printed holds).  tests/test_validate.py checks each against
# the lab's contract copy.
GRAMMAR = {
    "F1": r"^(SD|SF|SFGH)\d{3,4}[A-Z]?(\s+TS\d)?(\s+XR)?(\s+\(HS\s+Shaft\))?$",
    "F2": r"^\d{3}[A-Z]{2,4}\d{2,4}[A-Z]?$",
    "F3": r"^REDA\s+\d{3}\s+[A-Z]{1,3}\d{3,4}$",
    "F4": r"^ELS-\d{3,4}$",
    "F5": r"^ESP\s+B\s+(\d{3})\s+(\d{3,4})\s+([A-Z]{2})\s+(\d+)\s+STG$",
    "F7": r"^VC\d{4}(\s*-\s*[A-Z]{2})?(\s+Modular)?(\s+EHP)?$",
}

# V-07's component list, verbatim from the spec, as words.
_COMPONENT = re.compile(
    r"(?<![A-Za-z])(intake|seal|protector|sensor|separator|motor|cable|tubing|bod|sand[\s_-]?guard"
    r"|discharge[\s_-]?sub|de[\s_-]?sander|tail[\s_-]?pipe)(?![A-Za-z])", re.I)
_GAS_HANDLER = re.compile(r"gas[\s_-]?handler", re.I)
_NPSH = re.compile(r"npsh|cavitation", re.I)
_AMPS = re.compile(r"(^|_)amp(s|erage)?(_|$)", re.I)
_PIP = re.compile(r"(^|_)pip(_|$)|pump_intake_pressure|calculated_pip|desired_pip", re.I)
_PI = re.compile(r"(^|_)pi(_|$)|productivity_index", re.I)
_BOUNDARY = "section_intake_pressure_psi_as_printed"
_HZ = re.compile(r"(\d+(?:\.\d+)?)\s*hz", re.I)
_SHORT_NAME = re.compile(r"^[^.~]{1,6}~\d+\.pdf$", re.I)
_MSC = re.compile(r"MSC_\d{3}[A-Z]+_\d{2,4}[A-Z]?_", re.I)


# ==========================================================================
# §2 global: structure and identity
# ==========================================================================

@rule("V-03", "NOTE", "DOC", needs=("roles",))
def v03(doc):
    present = {r for roles in doc.roles.values() for r in roles}
    for role, status, _ in PAGE_MAP.get(doc.family, []):
        if status == "optional" and role not in present:
            yield None, None, None, role, f"optional page `{role}` is absent (recorded, never an error)"


@rule("V-04", "HALT", "CTX")
def v04(doc):
    for k, o in doc.rows("stg_design_context"):
        if has(o, "bubble_point_psi") and not has(o, "bubble_point_unit_as_printed"):
            yield ("stg_design_context", k, "bubble_point_unit_as_printed", o.get("bubble_point_psi"),
                   "bubble_point_psi is staged without bubble_point_unit_as_printed")


@rule("V-06", "HALT", "SEC")
def v06(doc):
    grammar = GRAMMAR.get(doc.family)
    for k, o in doc.rows("stg_pump_config"):
        printed = o.get("pump_model_as_printed")
        if grammar and not (isinstance(printed, str) and re.match(grammar, printed)):
            yield ("stg_pump_config", k, "pump_model_as_printed", printed,
                   f"does not match the {doc.family} contract §4 grammar {grammar}")


@rule("V-07", "ERROR", "SEC")
def v07(doc):
    for k, o in doc.rows("stg_pump_config"):
        role, printed = text(o, "section_role"), text(o, "pump_model_as_printed")
        if _GAS_HANDLER.search(role) or _GAS_HANDLER.search(printed):
            continue
        hit = _COMPONENT.search(role) or _COMPONENT.search(printed)
        if hit:
            yield ("stg_pump_config", k, "section_role" if _COMPONENT.search(role) else "pump_model_as_printed",
                   o.get("section_role") if _COMPONENT.search(role) else printed,
                   f"section row names `{hit.group(1)}`, which is not a pump body")


@rule("V-08", "ERROR", "SEC")
def v08(doc):
    by_scenario = defaultdict(list)
    for k, o in doc.rows("stg_pump_config"):
        by_scenario[T.scenario_key(o)].append((k, o))
    for scenario in sorted(by_scenario, key=lambda s: (len(s), s)):
        rows = by_scenario[scenario]
        try:
            orders = [int(text(o, "section_order")) for _, o in rows]
        except ValueError:
            yield ("stg_pump_config", None, "section_order", [o.get("section_order") for _, o in rows],
                   f"scenario {scenario}: section_order is missing or not an integer")
            continue
        if sorted(orders) != list(range(1, len(rows) + 1)):
            yield ("stg_pump_config", None, "section_order", sorted(orders),
                   f"scenario {scenario}: section_order is not a contiguous 1..{len(rows)}")
            continue
        depth = sorted((int(text(o, "section_order")), num(o, "bottom_md_ft") if has(o, "bottom_md_ft") else num(o, "top_md_ft"))
                       for _, o in rows)
        depth = [(n, d) for n, d in depth if d is not None]
        if any(b[1] > a[1] for a, b in zip(depth, depth[1:])):
            yield ("stg_pump_config", None, "section_order", depth,
                   f"scenario {scenario}: section_order runs against the depth columns (1 must be deepest)")


@rule("V-09", "WARN", "SEC")
def v09(doc):
    for k, o in doc.rows("stg_pump_config"):
        listed = V.parse_list(o.get("stages_per_housing_as_printed")) or []
        try:
            per_housing = [int(x) for x in listed]
        except ValueError:
            continue
        total = num(o, "stages")
        housings = num(o, "housing_count")
        if total is None or not per_housing:
            continue
        if len(per_housing) == 1 and housings is not None and housings * per_housing[0] != total:
            yield ("stg_pump_config", k, "stages", o.get("stages"),
                   f"housing_count {housings:g} × {per_housing[0]} per housing ≠ stages {total:g}")
        elif len(per_housing) > 1:
            if sum(per_housing) != total:
                yield ("stg_pump_config", k, "stages", o.get("stages"),
                       f"per-housing stages sum to {sum(per_housing)}, not stages {total:g}")
            if housings is not None and len(per_housing) != housings:
                yield ("stg_pump_config", k, "housing_count", o.get("housing_count"),
                       f"{len(per_housing)} per-housing counts listed against housing_count {housings:g}")


# ==========================================================================
# §3 global: values
# ==========================================================================

@rule("V-12", "WARN", "CTX")
def v12(doc):
    for k, o in doc.rows("stg_design_context"):
        v = num(o, "bubble_point_psi")
        if v is not None and not 0 < v <= 10000:
            yield ("stg_design_context", k, "bubble_point_psi", o.get("bubble_point_psi"),
                   "bubble_point_psi outside (0, 10000]: bubble_point_plausible = false; value kept verbatim")


@rule("V-13", "ERROR", "OBS")
def v13(doc):
    for k, o in doc.rows("stg_curve_observations"):
        if has(o, "flow_as_printed") and not has(o, "flow_unit_as_printed"):
            yield ("stg_curve_observations", k, "flow_unit_as_printed", o.get("flow_as_printed"),
                   "flow_as_printed is staged without flow_unit_as_printed")


@rule("V-14", "ERROR", "OBS")
def v14(doc):
    for k, o in doc.rows("stg_curve_observations"):
        if has(o, "head_as_printed") and (text(o, "head_unit_as_printed") != "ft"
                                          or not has(o, "head_basis_as_printed")):
            yield ("stg_curve_observations", k, "head_as_printed", o.get("head_as_printed"),
                   f"head unit `{text(o, 'head_unit_as_printed')}` is not `ft`, or head_basis_as_printed is missing")


@rule("V-15", "ERROR", "OBS")
def v15(doc):
    for k, o in doc.rows("stg_curve_observations"):
        if has(o, "power_as_printed") and not has(o, "power_basis_as_printed"):
            yield ("stg_curve_observations", k, "power_basis_as_printed", o.get("power_as_printed"),
                   "power_as_printed is staged without power_basis_as_printed")


@rule("V-17", "ERROR", "SCEN")
def v17(doc):
    design = defaultdict(set)
    for _, o in doc.rows("stg_curve_observations"):
        f = num(o, "frequency_hz_as_printed")
        if text(o, "point_type") == "design_point" and f is not None:
            design[T.scenario_key(o)].add(f)
    fired = set()
    for table in doc.report.blocks:
        for k, o in doc.rows(table):
            m = _HZ.search(text(o, "scenario_title_as_printed"))
            scenario = T.scenario_key(o)
            if not m or scenario in fired:
                continue
            # The scenario's table frequency is its design point's; a multi-frequency
            # curve row at another frequency is not it.  None or several: undecidable.
            if len(design[scenario]) != 1:
                continue
            table_hz, title_hz = next(iter(design[scenario])), float(m.group(1))
            if title_hz == table_hz:
                continue
            if text(o, "scenario_title_frequency_disagrees").lower() == "true":
                continue
            fired.add(scenario)
            yield (table, k, "scenario_title_frequency_disagrees", o.get("scenario_title_as_printed"),
                   f"title frequency {title_hz:g} Hz differs from table frequency {table_hz:g} Hz "
                   f"and scenario_title_frequency_disagrees is not true")


@rule("V-19", "HALT", "CTX")
def v19(doc):
    for k, o in doc.rows("stg_design_context"):
        basis = text(o, "depth_reference_basis")
        if not basis or (doc.report.vendor_family == "baker_prolift" and basis == "intake"):
            yield ("stg_design_context", k, "depth_reference_basis", o.get("depth_reference_basis"),
                   "depth_reference_basis is missing" if not basis else "`intake` on a baker_prolift document")


# ==========================================================================
# §4 family rules
# ==========================================================================

@rule("V-F1-1", "WARN", "DOC", "F1", needs=("roles",))
def v_f1_1(doc):
    role_pages = {p for p, roles in doc.roles.items()
                  if {"Design Schematic", "Pumps", "Single Pump Charts"} & set(roles)}
    strings = defaultdict(set)
    for table, columns in (("stg_pump_config", ("pump_model_as_printed",)),
                           ("stg_curve_observations", ("pump_model_as_printed",)),
                           ("stg_alias_evidence", schemas.ALIAS_PRINTED_SYNONYMS)):
        for _, o in doc.rows(table):
            if not set(page_numbers(o.get("source_page"))) & role_pages:
                continue
            printed = next((text(o, c) for c in columns if has(o, c)), "")
            canonical = doc.lookup.resolve(printed, doc.report.vendor_family).canonical if printed else None
            if canonical:
                strings[canonical].add(" ".join(printed.split()))
    for canonical in sorted(strings):
        if len(strings[canonical]) > 1:
            yield (None, None, "pump_model_as_printed", "; ".join(sorted(strings[canonical])),
                   f"{canonical} is printed {len(strings[canonical])} ways across Design Schematic, "
                   f"Pumps and Single Pump Charts; all recorded, none picked")


@rule("V-F1-2", "ERROR", "OBS", "F1")
def v_f1_2(doc):
    for k, o in doc.rows("stg_curve_observations"):
        if "*" in text(o, "flow_as_printed") and text(o, "viscosity_corrected").lower() != "true":
            yield ("stg_curve_observations", k, "viscosity_corrected", o.get("flow_as_printed"),
                   "a `*` (corrected viscosity) rate without viscosity_corrected = true")


@rule("V-F1-3", "HALT", "OBS", "F1")
def v_f1_3(doc):
    for k, o in doc.rows("stg_curve_observations"):
        basis = text(o, "head_basis_as_printed")
        if basis == "per_stage" or (has(o, "lift_as_printed") and basis != "per_section_total"):
            yield ("stg_curve_observations", k, "head_basis_as_printed", o.get("head_basis_as_printed"),
                   "a per-stage head, or Lift with head_basis ≠ per_section_total, in F1 output")


@rule("V-F1-4", "HALT", "DOC", "F1")
def v_f1_4(doc):
    for table, k, key, value in keys(doc, _NPSH):
        yield table, k, key, value, f"`{key}` maps a value to NPSHr or a cavitation margin (D12)"


@rule("V-F1-5", "WARN", "CTX", "F1")
def v_f1_5(doc):
    for k, o in doc.rows("stg_design_context"):
        if has(o, "bubble_point_psi") and text(o, "bubble_point_unit_as_printed") != "PSIA":
            yield ("stg_design_context", k, "bubble_point_unit_as_printed", o.get("bubble_point_unit_as_printed"),
                   "F1 bubble-point unit is not `PSIA`; verify on the page")


@rule("V-F1-6", "ERROR", "DOC", "F1")
def v_f1_6(doc):
    for table, k, key, value in keys(doc, _AMPS):
        yield (table, k, key, value,
               f"motor amperage `{key}` in the output (the rule cites motor_rated_amps, not a live field)")


@rule("V-F2-1", "HALT", "DOC", "F2")
def v_f2_1(doc):
    rows = [o for _, o in doc.rows("stg_design_context")]
    motor = any(has(o, "motor_power_factor") for o in rows)
    surface = any(has(o, "surface_power_factor") for o in rows)
    if motor != surface:
        column = "motor_power_factor" if motor else "surface_power_factor"
        yield ("stg_design_context", None, column, next(o.get(column) for o in rows if has(o, column)),
               "only one power factor is staged and design-context rows record no source page")


@rule("V-F2-2", "ERROR", "DOC", "F2")
def v_f2_2(doc):
    rows = [o for _, o in doc.rows("stg_design_context")]
    mixture = [o for o in rows if has(o, "vendor_mixture_sg")]
    liquid = [o for o in rows if has(o, "vendor_liquid_phase_sg")]
    if bool(mixture) != bool(liquid):
        yield ("stg_design_context", None, "vendor_mixture_sg" if mixture else "vendor_liquid_phase_sg", None,
               "Fluid Composite SG and Liquid Phase SG: only one is staged")
    for o in rows:
        if o in mixture and o in liquid and num(o, "vendor_mixture_sg") == num(o, "vendor_liquid_phase_sg"):
            yield ("stg_design_context", None, "vendor_mixture_sg", o.get("vendor_mixture_sg"),
                   "Fluid Composite SG and Liquid Phase SG collapsed into one value")


@rule("V-F2-3", "HALT", "OBS", "F2")
def v_f2_3(doc):
    for k, o in doc.rows("stg_curve_observations"):
        if text(o, "point_type") in ENVELOPE and (num(o, "flow_as_printed") is not None
                                                 or num(o, "head_as_printed") is not None):
            yield ("stg_curve_observations", k, "point_type", o.get("point_type"),
                   "a numeric BEP/ROR value in F2 output (plot annotations only)")


@rule("V-F2-4", "ERROR", "SEC", "F2", needs=("pages",))
def v_f2_4(doc):
    """Only where the page shows the alteration: the alias is not on its cited page
    verbatim, and that page prints an `MSC_` form."""
    for k, o in doc.rows("stg_pump_config"):
        alt = " ".join(text(o, "pump_model_as_printed_alt").split())
        if not alt or _MSC.search(alt):
            continue
        cited = [" ".join(doc.pages[p].split()) for p in page_numbers(o.get("source_page")) if p in doc.pages]
        if cited and not any(alt in page for page in cited) and any("MSC_" in page for page in cited):
            yield ("stg_pump_config", k, "pump_model_as_printed_alt", o.get("pump_model_as_printed_alt"),
                   "pump_model_as_printed_alt lost its `MSC_` prefix or its underscores: not on its cited page "
                   "verbatim, where an `MSC_` form is printed")


@rule("V-F2-5", "HALT", "DOC", "F2")
def v_f2_5(doc):
    for table in doc.report.blocks:
        for k, o in doc.rows(table):
            ordinal = text(o, "scenario_ordinal")
            if (ordinal.isdigit() and int(ordinal) > 1) or has(o, "scenario_label_staged"):
                yield (table, k, "scenario_ordinal" if ordinal.isdigit() and int(ordinal) > 1 else "scenario_label_staged",
                       o.get("scenario_ordinal") or o.get("scenario_label_staged"),
                       "F2 is single-scenario; a second scenario or a scenario label is staged")


@rule("V-F3-1", "ERROR", "SEC", "F3")
def v_f3_1(doc):
    evidence = defaultdict(list)
    for _, o in doc.rows("stg_alias_evidence"):
        printed = next((text(o, c) for c in schemas.ALIAS_PRINTED_SYNONYMS if has(o, c)), "")
        evidence[printed] += [text(o, c) for c in schemas.ALIAS_CANONICAL_SYNONYMS if has(o, c)]
    for k, o in doc.rows("stg_pump_config"):
        config = " ".join(text(o, "staging_configuration_as_printed").split())
        printed = text(o, "pump_model_as_printed")
        if config and (config in " ".join(printed.split())
                       or any(config in " ".join(c.split()) for c in evidence.get(printed, []))):
            yield ("stg_pump_config", k, "pump_model_as_printed", printed,
                   f"Staging Configuration `{config}` is folded into the model string or its proposed canonical")


@rule("V-F3-2", "NOTE", "CTX", "F3")
def v_f3_2(doc):
    for k, o in doc.rows("stg_design_context"):
        gradient, cut, water = (num(o, c) for c in ("vendor_mixture_gradient_psi_per_ft",
                                                    "water_cut_design_frac", "water_sg"))
        oil = num(o, "oil_sg")
        if oil is None and num(o, "oil_api_deg") is not None:
            oil = 141.5 / (131.5 + num(o, "oil_api_deg"))
        if None in (gradient, cut, water, oil) or not 0 <= cut <= 1:
            continue
        implied = 0.433 * (oil * (1 - cut) + water * cut)
        if abs(gradient - implied) > 0.05 * implied:
            yield ("stg_design_context", k, "vendor_mixture_gradient_psi_per_ft",
                   {"printed": gradient, "implied": round(implied, 4)},
                   f"F3-GRADIENT-DIVERGENCE: printed {gradient:g} psi/ft vs SG-implied {implied:.4f} psi/ft "
                   f"(0.433 psi/ft per unit SG); a finding, never a correction")


@rule("V-F3-4", "WARN", "CTX", "F3")
def v_f3_4(doc):
    for k, o in doc.rows("stg_design_context"):
        if has(o, "motor_power_factor"):
            yield ("stg_design_context", k, "motor_power_factor", o.get("motor_power_factor"),
                   "F3 prints no power factor; this value came from elsewhere")


# Curve rows: head-curve points and the BEP/ROR envelope.  A design point's printed
# Total Dynamic Head is not one (checked on the PDFs, 2026-10-13).
CURVE_POINT_TYPES = {"curve_point", "shutoff"} | ENVELOPE


@rule("V-F3-5", "HALT", "OBS", "F3")
def v_f3_5(doc):
    for k, o in doc.rows("stg_curve_observations"):
        curve_row = text(o, "point_type") in CURVE_POINT_TYPES or text(o, "curve_observation_role") == "head_curve"
        if has(o, "head_as_printed") and curve_row:
            yield ("stg_curve_observations", k, "head_as_printed", o.get("head_as_printed"),
                   "a head-curve, BEP or ROR head in F3 output (envelope_status = absent)")


@rule("V-F3-6", "ERROR", "SCEN", "F3")
def v_f3_6(doc):
    intake = defaultdict(set)
    for _, o in doc.rows("stg_design_context"):
        if num(o, "intake_set_depth_md_ft") is not None:
            intake[T.scenario_key(o)].add(num(o, "intake_set_depth_md_ft"))
    every = set().union(*intake.values()) if intake else set()
    for k, o in doc.rows("stg_pump_config"):
        bottom = num(o, "bottom_md_ft")
        if text(o, "section_order") != "1" or bottom is None:
            continue
        depths = intake.get(T.scenario_key(o)) or (every if len(every) == 1 else set())
        if len(depths) != 1:
            continue
        depth = next(iter(depths))
        if abs(bottom - depth) > 500 or (bottom < 1000 and depth > 5000):
            yield ("stg_pump_config", k, "bottom_md_ft", o.get("bottom_md_ft"),
                   f"deepest section bottom {bottom:g} ft against intake depth {depth:g} ft")


@rule("V-F3-7", "ERROR", "DOC", "F3")
def v_f3_7(doc):
    fm = doc.report.frontmatter
    if _SHORT_NAME.match(str(fm.get("source_document", "")).strip()) \
            and str(fm.get("well_name_source", "")).strip() != "document_body":
        yield (None, None, "well_name_source", fm.get("well_name_source"),
               "an 8.3 short file name carries no usable well token; well_name_source must be document_body")


@rule("V-F4-1", "ERROR", "DOC", "F4", needs=("pages",))
def v_f4_1(doc):
    printed = any("calculated pb" in t.lower() for t in doc.pages.values())
    if printed and not any(has(o, "bubble_point_calculated_flag") for _, o in doc.rows("stg_design_context")):
        yield ("stg_design_context", None, "bubble_point_calculated_flag", None,
               "`Calculated Pb` is on the page but bubble_point_calculated_flag is not staged")


@rule("V-F4-2", "ERROR", "CTX", "F4")
def v_f4_2(doc):
    expected = {"no": "vendor input", "yes": "vendor derived"}
    for k, o in doc.rows("stg_design_context"):
        flag = text(o, "bubble_point_calculated_flag").lower()
        if flag in expected and text(o, "bubble_point_provenance").lower() != expected[flag]:
            yield ("stg_design_context", k, "bubble_point_provenance", o.get("bubble_point_provenance"),
                   f"Calculated Pb `{flag}` requires bubble_point_provenance `{expected[flag]}`")


@rule("V-F4-3", "HALT", "DOC", "F4")
def v_f4_3(doc):
    for k, o in doc.rows("stg_gas_cascade"):
        if has(o, "gas_rate_as_printed") and "mcf/d" not in (text(o, "gas_rate_as_printed") + " "
                                                            + text(o, "gas_rate_unit_as_printed")).lower():
            yield ("stg_gas_cascade", k, "gas_rate_unit_as_printed", o.get("gas_rate_as_printed"),
                   "Free gas at Intake without its `mcf/d` unit token")
    for k, o in doc.rows("stg_design_context"):
        if has(o, "vendor_free_gas_into_pump_pct") and "mcf/d" not in text(o, "vendor_free_gas_into_pump_pct").lower():
            yield ("stg_design_context", k, "vendor_free_gas_into_pump_pct", o.get("vendor_free_gas_into_pump_pct"),
                   "Free gas at Intake without its `mcf/d` unit token")


@rule("V-F4-5", "ERROR", "SEC", "F4")
def v_f4_5(doc):
    for k, o in doc.rows("stg_pump_config"):
        model, series = text(o, "pump_model_as_printed"), text(o, "pump_series_as_printed")
        if (series and re.search(rf"(?<!\d){re.escape(series)}(?!\d)", model)) \
                or re.match(r"^\d{3}\s*ELS\s*-?\d", model, re.I) or re.match(r"^ELS\s+\d{3}\s+", model, re.I):
            yield ("stg_pump_config", k, "pump_model_as_printed", model,
                   "Series and Model concatenated into one token")


@rule("V-F4-6", "WARN", "SEC", "F4")
def v_f4_6(doc):
    for k, o in doc.rows("stg_pump_config"):
        for column in ("top_md_ft", "bottom_md_ft"):
            if has(o, column):
                yield ("stg_pump_config", k, column, o.get(column),
                       "F4 prints no per-section depth; this value is derived")


@rule("V-F4-7", "WARN", "CTX", "F4")
def v_f4_7(doc):
    for k, o in doc.rows("stg_design_context"):
        if has(o, "motor_power_factor"):
            yield ("stg_design_context", k, "motor_power_factor", o.get("motor_power_factor"),
                   "F4 prints no power factor; this value came from elsewhere")


def _permanent_magnet(doc) -> bool:
    return any(text(o, "motor_type").lower() == "permanent_magnet" for _, o in doc.rows("stg_design_context"))


@rule("V-F5-1", "WARN", "CTX", "F5")
def v_f5_1(doc):
    if not _permanent_magnet(doc):
        return
    for k, o in doc.rows("stg_design_context"):
        motor, shaft = num(o, "motor_frequency_hz"), num(o, "design_frequency_hz")
        if motor is not None and shaft and not 1.9 <= motor / shaft <= 2.1:
            yield ("stg_design_context", k, "motor_frequency_hz", o.get("motor_frequency_hz"),
                   f"motor / shaft frequency {motor:g} / {shaft:g} = {motor / shaft:.3f}, outside [1.9, 2.1]")


@rule("V-F5-2", "HALT", "CTX", "F5")
def v_f5_2(doc):
    if not _permanent_magnet(doc):
        return
    motor = {num(o, "motor_frequency_hz") for _, o in doc.rows("stg_design_context")} - {None}
    for table, column in (("stg_design_context", "design_frequency_hz"),
                          ("stg_curve_observations", "frequency_hz_as_printed")):
        for k, o in doc.rows(table):
            if num(o, column) in motor:
                yield (table, k, column, o.get(column), f"{column} equals a Motor Frequency value; take Pump Shaft Frequency")


@rule("V-F5-3", "ERROR", "DOC", "F5")
def v_f5_3(doc):
    if not any(has(o, "motor_type") for _, o in doc.rows("stg_design_context")):
        yield "stg_design_context", None, "motor_type", None, "motor_type is required for F5 and is not staged"


@rule("V-F5-4", "HALT", "CTX", "F5")
def v_f5_4(doc):
    for k, o in doc.rows("stg_design_context"):
        if text(o, "depth_reference_basis") != "pump_discharge":
            yield ("stg_design_context", k, "depth_reference_basis", o.get("depth_reference_basis"),
                   "F5 depth_reference_basis must be the literal `pump_discharge`")


@rule("V-F5-6", "HALT", "SEC", "F5", needs=("roles",))
def v_f5_6(doc):
    sensitivity = {p for p, roles in doc.roles.items() if "Sensitivity case table" in roles}
    for k, o in doc.rows("stg_pump_config"):
        if text(o, "sensitivity_case").lower() == "true" or set(page_numbers(o.get("source_page"))) & sensitivity:
            yield ("stg_pump_config", k, "source_page", o.get("source_page"),
                   "a pump_config row from the Sensitivity table (the string comes from p1 and p8 only)")


@rule("V-F5-7", "HALT", "CTX", "F5")
def v_f5_7(doc):
    for k, o in doc.rows("stg_design_context"):
        if has(o, "bubble_point_psi"):
            yield ("stg_design_context", k, "bubble_point_psi", o.get("bubble_point_psi"),
                   "F5 prints no bubble point; set bubble_point_absent_confirmed")


@rule("V-F5-8", "HALT", "OBS", "F5")
def v_f5_8(doc):
    for k, o in doc.rows("stg_curve_observations"):
        composite_tdh = (text(o, "observation_is_composite").lower() == "true"
                         and text(o, "head_basis_as_printed") == "string_total")
        if text(o, "point_type") in ENVELOPE or (has(o, "head_as_printed") and not composite_tdh):
            yield ("stg_curve_observations", k, "point_type", o.get("point_type"),
                   "a head, BEP or ROR observation in F5 output (TDH is string_total and composite only)")


@rule("V-F7-1", "ERROR", "CTX", "F7")
def v_f7_1(doc):
    rows = list(doc.rows("stg_design_context"))
    if not any(has(o, "bubble_point_correlation") for _, o in rows):
        yield "stg_design_context", None, "bubble_point_correlation", None, "bubble_point_correlation is not staged"
    for k, o in rows:
        if text(o, "bubble_point_correlation").lower().startswith("standing"):
            yield ("stg_design_context", k, "bubble_point_correlation", o.get("bubble_point_correlation"),
                   "F7 prints Al-Shammasi (1999), not Standing")


@rule("V-F7-3", "ERROR", "OBS", "F7")
def v_f7_3(doc):
    rows = list(doc.rows("stg_curve_observations"))
    if not any(has(o, "viscosity_correction_factors_as_printed") for _, o in rows):
        yield ("stg_curve_observations", None, "viscosity_correction_factors_as_printed", None,
               "Avg Cq / Ch / Cbhp are not staged")
    for k, o in rows:
        factors = re.findall(r"\d+(?:\.\d+)?", text(o, "viscosity_correction_factors_as_printed"))
        if factors and all(float(f) == 1 for f in factors) and text(o, "viscosity_corrected").lower() != "false":
            yield ("stg_curve_observations", k, "viscosity_corrected", o.get("viscosity_corrected"),
                   "all three factors are 1.0000, so viscosity_corrected must be false")


@rule("V-F7-4", "ERROR", "SEC", "F7")
def v_f7_4(doc):
    for k, o in doc.rows("stg_gas_cascade"):
        if has(o, "turpin_indicator_as_printed") and not has(o, "dunbar_indicator_as_printed"):
            yield ("stg_gas_cascade", k, "dunbar_indicator_as_printed", o.get("turpin_indicator_as_printed"),
                   "a Turpin PHI without its Dunbar PHI")


@rule("V-F7-5", "ERROR", "CTX", "F7")
def v_f7_5(doc):
    for k, o in doc.rows("stg_design_context"):
        if not has(o, "gas_sg"):
            continue
        if not has(o, "gas_sg_basis") or "mix" in text(o, "gas_sg_basis").lower():
            yield ("stg_design_context", k, "gas_sg", o.get("gas_sg"),
                   "gas_sg without gas_sg_basis, or with Spg Gas (Mix) recorded as its source")


@rule("V-F7-6", "ERROR", "DOC", "F7")
def v_f7_6(doc):
    temps = ("intake_temp_f", "reservoir_temp_f", "surface_temp_f")
    present = {c for _, o in doc.rows("stg_design_context") for c in temps if has(o, c)}
    if len(present) < 3:
        yield ("stg_design_context", None, ", ".join(sorted(set(temps) - present)), None,
               "the three temperatures are not three staged columns")


def _pressure_keys(doc, pattern):
    """Keys matching `pattern`, plus the F6/F7 boundary pressure anywhere but an observation row."""
    yield from keys(doc, pattern)
    for table in doc.report.blocks:
        if table != "stg_curve_observations":
            for k, o in doc.rows(table):
                if _BOUNDARY in o:
                    yield table, k, _BOUNDARY, o[_BOUNDARY]


@rule("V-F7-9", "HALT", "DOC", "F7")
def v_f7_9(doc):
    for table, k, key, value in _pressure_keys(doc, _PIP):
        yield table, k, key, value, f"per-section intake pressure written to `{key}` (D11)"


@rule("V-F7-10", "WARN", "OBS", "F7")
def v_f7_10(doc):
    for k, o in doc.rows("stg_curve_observations"):
        if text(o, "point_type") in ENVELOPE and has(o, "flow_as_printed") and text(o, "flow_unit_as_printed") != "bbl/d":
            yield ("stg_curve_observations", k, "flow_unit_as_printed", o.get("flow_unit_as_printed"),
                   "F7 envelope flow is printed in `bbl/d`; record as printed, never reconcile")


# ==========================================================================
# §7 cross-document rules checkable within one report
# ==========================================================================

@rule("V-X3", "NOTE", "SEC")
def v_x3(doc):
    seen = set()
    for k, o in doc.rows("stg_pump_config"):
        printed = text(o, "pump_model_as_printed")
        if printed and printed not in seen and (doc.report.vendor_family, printed.lower()) not in doc.lookup.seed:
            seen.add(printed)
            yield ("stg_pump_config", k, "pump_model_as_printed", printed,
                   "no row in the D31 seed; append it with its basis")


@rule("V-X6", "HALT", "DOC")
def v_x6(doc):
    for table, k, key, value in keys(doc, _NPSH):
        yield table, k, key, value, f"`{key}` maps a value to NPSHr (D12)"


@rule("V-X7", "HALT", "DOC")
def v_x7(doc):
    for table, k, key, value in _pressure_keys(doc, re.compile(f"{_PIP.pattern}|{_PI.pattern}", re.I)):
        yield table, k, key, value, f"`{key}` is a PI/PIP field in a loaded table (D11)"
