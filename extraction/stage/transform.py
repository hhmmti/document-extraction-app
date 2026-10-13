"""Staging rows -> the three load tables, with both fail-loud rules.

Order matters and is not negotiable: **alias resolution runs before the D17
collapse.**  D17's identity tuple is built on `pump_model_canonical`, so that
F5's two printed forms of one pump (`ESP B 400 1750 PK 738 STG` and
`ESP B 4001750 CW 6.5M HSG 123 STG ...`, 6 x 123 = 738) are one model and not
two.  §6.3 names F5 the known-safe exception; collapsing on raw strings is what
would make it fire anyway.

Epoch is keyed on `effective_from` and nothing else.  A document with no
printed design date is its own epoch with `effective_from = NULL`, sorted last.
The extraction date is never substituted: D18 selects the latest design by this
field, so a placeholder would win against every real date in the corpus.  F2
prints no date on any of its five documents; those five wells load single-epoch
with NULL, and that is correct.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from . import schemas
from . import values as V

TOLERANCE = 1e-6


# --------------------------------------------------------------------------
# Quarantine
# --------------------------------------------------------------------------


class Quarantine:
    """One object per rejected value.  Nothing here is ever coerced."""

    def __init__(self):
        self.rows: list = []
        self.conversions: list = []

    def reject(self, report, table, column, raw_value, reason, source_page=None, extra=None):
        row = {
            "source_document": report.source_document or report.slug,
            "source_page": source_page,
            "table": table,
            "column": column,
            "raw_value": raw_value,
            "reason": reason,
        }
        if extra:
            row.update(extra)
        self.rows.append(row)

    def converted(self, report, table, column, raw_value, note, source_page=None):
        self.conversions.append({
            "source_document": report.source_document or report.slug,
            "source_page": source_page,
            "table": table,
            "column": column,
            "raw_value": raw_value,
            "conversion": note,
        })


def take(report, quarantine, table, obj, column, kind="scalar"):
    """Type one staging value, quarantining it rather than coercing it."""
    if column not in obj:
        return None
    raw = obj[column]
    page = obj.get("source_page")

    if kind == "bool":
        parsed = V.parse_bool(raw)
    elif kind == "int":
        parsed = V.parse_int(raw, column)
    else:
        parsed = V.parse_scalar(raw, column)

    if parsed.reason:
        quarantine.reject(report, table, column, raw, parsed.reason, page)
        return None
    if parsed.conversion:
        quarantine.converted(report, table, column, raw, parsed.conversion, page)
    return parsed.value


def unit_token(obj, column):
    parsed = V.parse_scalar(obj.get(column), column)
    return parsed.unit_token


# --------------------------------------------------------------------------
# Out-of-schema keys (V-10)
# --------------------------------------------------------------------------


def check_schema_keys(report, quarantine):
    """A key in no schema has no destination; its value is quarantined, not dropped."""
    for table, rows in report.blocks.items():
        legal = schemas.LEGAL_KEYS[table]
        tolerated = schemas.ALIAS_TOLERATED if table == "stg_alias_evidence" else set()
        for obj in rows:
            for key, value in obj.items():
                if key in legal or key in tolerated:
                    continue
                quarantine.reject(
                    report, table, key, value, "key_not_in_schema_v10",
                    obj.get("source_page"),
                )


def quarantine_truncated(report, quarantine):
    for table, raw_line in report.truncated:
        quarantine.reject(
            report, table, None, raw_line[:400], "truncated_jsonl_line", None,
        )


# --------------------------------------------------------------------------
# Scenario identity
# --------------------------------------------------------------------------


def scenario_key(obj, position=None):
    """Scenario identity for one staging object.

    Falls back to **order of appearance** when neither key is printed.  This is
    not a guess: §8 defines `scenario_ordinal` as "order of appearance, 1-based",
    and the block's line order is that order.  Without the fallback the D16
    demonstration case silently cannot fire -- one F1 corpus document emits
    two `stg_design_context` objects carrying neither `scenario_ordinal` nor
    `scenario_label_staged`, so both would collapse onto one key and the
    1,800 / 1,200 PSIA bubble-point variance would never be compared.
    """
    ordinal = obj.get("scenario_ordinal")
    label = obj.get("scenario_label_staged") or obj.get("scenario_label")
    if ordinal is not None and str(ordinal).strip():
        return str(ordinal).strip()
    if label:
        return str(label).strip()
    if position is not None:
        return str(position + 1)
    return "1"


def scenario_keys(rows):
    """Positional scenario keys for a whole block, in printed order."""
    return [scenario_key(obj, i) for i, obj in enumerate(rows)]


def is_sensitivity(obj) -> bool:
    return str(obj.get("sensitivity_case", "")).strip().lower() == "true"


# --------------------------------------------------------------------------
# D16 -- the design-scenario selector (validator spec §5.3 ladder)
# --------------------------------------------------------------------------


def select_design_scenario(report, rows):
    """Return (scenario_key, rule_text).  The ladder is the contract's, not ours."""
    if not rows:
        return None, "no scenario rows"

    by_scenario = defaultdict(list)
    for key, obj in zip(scenario_keys(rows), rows):
        by_scenario[key].append(obj)
    keys = sorted(by_scenario, key=lambda k: (len(k), k))

    if len(keys) == 1:
        return keys[0], "single scenario; D16 cannot fire"

    # rung 0 -- the extractor recorded the ladder's own result
    flagged = [k for k in keys
               if any(str(o.get("is_design_scenario", "")).lower() == "true"
                      for o in by_scenario[k])]
    if len(flagged) == 1:
        return flagged[0], "is_design_scenario = true, as recorded by extraction"

    family = report.vendor_family

    if family == "slb":
        # f3-slb §5: the named case `Initial` (as-installed).
        for key in keys:
            labels = [str(o.get("scenario_label_staged") or o.get("scenario_label") or "")
                      for o in by_scenario[key]]
            if any(re.match(r"^\s*initial\b", lab, re.I) for lab in labels):
                return key, "F3 §5: named case `Initial` (Future/Max are look-aheads)"

    if family == "baker_prolift":
        # f5-baker §5: the p1 ProLift Summary base case; never a p7 sensitivity row.
        base = [k for k in keys if not all(is_sensitivity(o) for o in by_scenario[k])]
        if base:
            return base[0], "F5 §5: p1 ProLift Summary base case; p7 sensitivity rows excluded"

    if family == "spyglass":
        # f1-spyglass §5 rungs 1 and 2 need the `Design Overview` design rate and
        # the document-level operating frequency.  Neither is carried by any
        # staging column, so neither is evaluable here.  Rung 3 is the contract's
        # own last rung -- taken, and named, rather than a selector invented to
        # replace the two that cannot run.
        return keys[0], (
            "F1 §5 rung 3: scenario_ordinal = 1. Rungs 1-2 not evaluable — the "
            "Design Overview design rate and the document-level operating "
            "frequency are not carried in staging"
        )

    return keys[0], "last rung: scenario_ordinal = 1"


# --------------------------------------------------------------------------
# D16 tiering.  Both lists are closed: a column not named here is Tier C.
# --------------------------------------------------------------------------

# Tier A -- these do not vary "invariantly"; they restate a value that
# `curve_observations` already holds at its proper scenario grain.  Dropped from
# the sidecar rather than quarantined, but only once the restatement is
# confirmed against the document's own loaded observations.
D16_TIER_A = {
    "vendor_tdh_at_design_ft": "head_as_printed",
    "vendor_free_gas_into_pump_pct": "free_gas_at_inlet_pct",
}

# Tier B -- genuinely scenario-grained, and not duplicated anywhere else.  Take
# the design scenario's value by the same selector the allowlist uses, keep the
# column, and provenance-tag it with every scenario's value.
D16_TIER_B = {
    "vendor_total_system_efficiency_pct",
    "vendor_pump_efficiency_pct",
    "vendor_motor_power_kw",
    "motor_power_factor",
    "motor_efficiency_ratio",
}


def confirm_tier_a(column, raw_value, observations):
    """Is the design-scenario value actually present in this document's observations?

    Returns (confirmed, reason).  A Tier A column is only dropped on a
    confirmed match -- an unconfirmed drop would delete the sole copy of a
    value on the strength of an assumption about where its duplicate lives.
    """
    parsed = V.parse_scalar(raw_value, column)
    if parsed.value is None:
        return False, f"design-scenario value {raw_value!r} did not parse"

    target = D16_TIER_A[column]
    for obs in observations:
        held = obs.get(target)
        if held is None:
            continue
        if abs(held - parsed.value) > max(TOLERANCE, abs(parsed.value) * 1e-9):
            continue
        if column == "vendor_tdh_at_design_ft":
            # the composite design point, not an arbitrary head value
            if obs.get("point_type") != "design_point" and not obs.get("observation_is_composite"):
                continue
        return True, None
    return False, (f"no loaded curve_observations row in this document carries "
                   f"{target} = {parsed.value}")


def d16_check(report, rows, design_key, quarantine, observations=None,
              tier_a_fallback=None):
    """Compare every design-context column across a document's scenarios.

    pass                              -- one distinct value
    D16-WARN, allowlisted (§5.2)      -- resolve by the selector, record the span
    D16-TIER-A-DROP                   -- restates an observation; dropped, not quarantined
    D16-WARN, Tier B                  -- scenario-grained; selector + provenance tag
    D16-HALT, Tier C                  -- the *column* is not loaded

    §5.1 is written per column ("For each column of `esp_well_design_context`,
    compare its value across all scenarios"), and its `do not load` names no
    well -- unlike §6.2, which says explicitly that D17-HALT lists the well.  So
    a halt withholds the offending column and leaves it NULL; the well's other
    columns still load, and both values go to quarantine.  Rejecting whole wells
    on the wider reading would drop most of F1 and F3 from the sidecar over
    vendor-derived columns that vary by construction.
    """
    by_scenario = defaultdict(list)
    for key, obj in zip(scenario_keys(rows), rows):
        by_scenario[key].append(obj)
    if len(by_scenario) <= 1:
        return {}, [], "pass", []

    observations = observations or []
    findings, halts, dropped = {}, [], []
    # provenance_json is provenance, not data: it varies per scenario by
    # construction and carries D16's own record (§5.3 step 5).
    columns = {k for obj in rows for k in obj
               if k in schemas.DESIGN_CONTEXT_COLUMNS and k != "provenance_json"}

    for column in sorted(columns):
        per_scenario = {}
        for key, objs in by_scenario.items():
            printed = [o[column] for o in objs if column in o]
            if printed:
                per_scenario[key] = printed[0]
        # Absent means not printed, not a disagreement -- compare what is there.
        distinct = {str(v).strip() for v in per_scenario.values()}
        if len(distinct) <= 1:
            continue

        if column in schemas.NOT_D16_EVENTS:
            findings[column] = {
                "status": "not_a_d16_event",
                "all_values": per_scenario,
                "note": "§5.2: scenario-grained by nature; belongs to "
                        "curve_observations. Presence here is an extraction error.",
            }
            continue

        selected = per_scenario.get(design_key)

        def warn(tier):
            findings[column] = {
                "status": "D16-WARN",
                "tier": tier,
                "all_values": per_scenario,
                "selected_scenario": design_key,
                "selected": selected,
            }

        if column in schemas.SCENARIO_VARIANT_ALLOWLIST:
            warn("allowlist")

        elif column in D16_TIER_A:
            confirmed, reason = confirm_tier_a(column, selected, observations)
            if confirmed:
                findings[column] = {
                    "status": "D16-TIER-A-DROP",
                    "tier": "A",
                    "all_values": per_scenario,
                    "restates": D16_TIER_A[column],
                }
                dropped.append(column)
            else:
                # Unconfirmed: this document may hold the only copy, so it is
                # kept under Tier B rather than dropped on an assumption.
                warn("A-fallback-to-B")
                if tier_a_fallback is not None:
                    tier_a_fallback.append({
                        "source_document": report.source_document or report.slug,
                        "column": column,
                        "reason": reason,
                    })

        elif column in D16_TIER_B:
            warn("B")

        else:
            findings[column] = {"status": "D16-HALT", "tier": "C",
                                "all_values": per_scenario}
            halts.append(column)
            quarantine.reject(
                report, "esp_well_design_context", column,
                json.dumps(per_scenario), "d16_halt_invariant_field_varies",
            )

    return findings, halts, ("D16-HALT" if halts else
                             ("D16-WARN" if findings else "pass")), dropped


# --------------------------------------------------------------------------
# D17 -- collapse scenario-grained pump_config to well-grained (§6.1)
# --------------------------------------------------------------------------

IDENTITY_FIELDS = ("section_order", "section_role", "pump_model_canonical",
                   "stages", "housing_count")


def identity_tuple(row):
    return tuple(str(row.get(f) if row.get(f) is not None else "").strip()
                 for f in IDENTITY_FIELDS)


def d17_collapse(report, rows, design_key):
    """Returns (surviving rows, status, detail)."""
    by_scenario = defaultdict(list)
    for row in rows:
        by_scenario[row["_scenario"]].append(row)

    if len(by_scenario) <= 1:
        only = next(iter(by_scenario.values())) if by_scenario else []
        return only, "single_scenario", {}

    identities = {k: sorted(identity_tuple(r) for r in v) for k, v in by_scenario.items()}
    distinct = {tuple(v) for v in identities.values()}

    if len(distinct) == 1:
        chosen = design_key if design_key in by_scenario else sorted(by_scenario)[0]
        survivors = [dict(r) for r in by_scenario[chosen]]
        # §6.1: do non-identity fields agree too?
        varying = set()
        # `source_page` and `provenance` are provenance, not data: F1 prints one
        # scenario per page, so they vary on every multi-scenario document and
        # would mark every collapse as a variance while saying nothing about the
        # string.  §6.1's example names depth fields, which are data.
        for field in ("top_md_ft", "bottom_md_ft",
                      "staging_configuration_as_printed"):
            seen = {str(r.get(field)) for rows_ in by_scenario.values() for r in rows_
                    if r.get(field) is not None}
            if len(seen) > 1:
                varying.add(field)
        for row in survivors:
            row["scenario_label_staged"] = None
            row["extraction_status"] = "extracted" if varying else "validated"
        status = "collapsed_with_variance" if varying else "collapsed"
        return survivors, status, {"varying_non_identity_fields": sorted(varying)}

    # §6.2 -- the document is presenting equipment options, not an installed
    # string.  Retain every variant; this needs a human decision, not a rule.
    survivors = []
    for key in sorted(by_scenario):
        for row in by_scenario[key]:
            row = dict(row)
            row["extraction_status"] = "conflict"
            survivors.append(row)
    return survivors, "D17-HALT", {
        "identity_sets": {k: [list(t) for t in v] for k, v in identities.items()}
    }


# --------------------------------------------------------------------------
# CF-28 -- collapse to `model_grouped` grain
# --------------------------------------------------------------------------


def to_model_grouped(rows):
    """One row per distinct canonical model, `stages_basis = group_total`.

    `stages_per_housing_as_printed` is a list, not a scalar: one F2 document prints
    `42 + 93x4 = 414` and reading it as a scalar loses 372 stages.
    """
    groups = defaultdict(list)
    for row in rows:
        key = row.get("pump_model_canonical") or f"__unresolved__{row.get('pump_model_as_printed')}"
        groups[key].append(row)

    out = []
    for key, members in groups.items():
        members = sorted(members, key=lambda r: (r.get("section_order") or 0))
        first = members[0]
        already_grouped = any(
            str(m.get("section_grain", "")).strip() == "model_grouped" for m in members
        )

        per_housing = []
        for member in members:
            printed = V.parse_list(member.get("stages_per_housing_as_printed"))
            if printed:
                per_housing.extend(printed)
            elif member.get("stages") is not None and not already_grouped:
                per_housing.append(str(member["stages"]))

        if already_grouped:
            stages_total = sum(m["stages"] for m in members if m.get("stages") is not None) or None
            housings = sum(m["housing_count"] for m in members
                           if m.get("housing_count") is not None) or None
        else:
            stages_total = sum(m["stages"] for m in members if m.get("stages") is not None) or None
            # F1 prints one `Pn` row per housing, so the group's housing count is
            # the number of rows in it.  The per-row `housing_count` of 15 does
            # not reconcile against the printed string total on any F1 document
            # (5 x 124 = 620 does; 15 x 124 does not) and is left in
            # `housing_count_as_printed` rather than trusted here.
            housings = len(members)

        tops = [m["top_md_ft"] for m in members if m.get("top_md_ft") is not None]
        bottoms = [m["bottom_md_ft"] for m in members if m.get("bottom_md_ft") is not None]
        statuses = {m.get("extraction_status") for m in members}

        out.append({
            **{k: first.get(k) for k in (
                "organization_id", "well_id", "effective_from", "section_role",
                "pump_model_as_printed", "pump_model_canonical", "manufacturer",
                "vendor_family", "source_document", "source_page",
                "scenario_label_staged", "provenance",
            )},
            "section_order": first.get("section_order"),
            "housing_count": housings,
            "housing_count_as_printed": first.get("housing_count"),
            "stages": stages_total,
            "stages_basis": "group_total",
            "stages_per_housing_as_printed": ", ".join(per_housing) if per_housing else None,
            "top_md_ft": min(tops) if tops else None,
            "bottom_md_ft": max(bottoms) if bottoms else None,
            "section_grain": "model_grouped",
            "section_orders_grouped": ", ".join(
                str(m.get("section_order")) for m in members if m.get("section_order") is not None
            ) or None,
            "extraction_status": ("conflict" if "conflict" in statuses else
                                  "validated" if statuses == {"validated"} else "extracted"),
            "pump_model_alias_flag": first.get("pump_model_alias_flag"),
            "pump_model_alias_basis": first.get("pump_model_alias_basis"),
        })
    return sorted(out, key=lambda r: (r.get("section_order") or 0))


# --------------------------------------------------------------------------
# D10 -- affinity normalisation to 60 Hz
# --------------------------------------------------------------------------


def normalize_to_60hz(frequency, flow, head, power, stages, head_basis, power_basis):
    """Raw values are never discarded; these are written alongside them.

    F5 runs a permanent-magnet motor at twice shaft frequency (103.18 Hz against
    51.59 Hz).  `frequency` here is always `frequency_hz_as_printed`, the shaft
    frequency -- normalising on `motor_frequency_hz` would give 2x flow and 4x
    head, which reads as a different pump rather than as a bug.
    """
    if frequency is None or frequency <= 0:
        return None, None, None, None

    ratio = 60.0 / frequency
    flow_60 = flow * ratio if flow is not None else None

    head_60 = None
    if head is not None:
        per_stage = head
        if head_basis in ("string_total", "per_section_total") and stages:
            per_stage = head / stages
        elif head_basis in ("string_total", "per_section_total") and not stages:
            per_stage = None
        if per_stage is not None:
            head_60 = per_stage * ratio ** 2

    power_60 = None
    if power is not None:
        per_stage = power
        if power_basis in ("string_total", "per_section_total") and stages:
            per_stage = power / stages
        elif power_basis in ("string_total", "per_section_total") and not stages:
            per_stage = None
        if per_stage is not None:
            power_60 = per_stage * ratio ** 3

    return flow_60, head_60, power_60, f"affinity_from_{frequency:g}hz"
