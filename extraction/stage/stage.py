"""Stage: one extraction report -> the three load grains, its log and its quarantine.

The per-document body of the corpus batch loader, ported with its logic
unchanged: the per-report lines of its `build()`, and `_load_one` with its
helpers.  Lines that changed are listed, with reasons, in `MANIFEST.txt`.

Two things crossed documents in the batch loader and are passed in here:

  * the alias evidence (`evidence`): `stg_alias_evidence` flags learned from
    every report before any one was loaded -- see `corpus_evidence` and
    `production_evidence`;
  * the well match: the caller supplies `organization_id` and `well_id`.
    `well_id = None` is an unmatched well; as in the batch loader it gets the
    schema-key and truncation quarantine and its reconciliation rows, and no
    load rows.  No synthetic key is minted.

No database, no views, no blocked-wells table.  Nothing is written to disk.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..assets_dir import ASSETS_ENV, asset as _asset
from ..errors import ExtractionError
from . import schemas
from . import transform as T
from . import values as V
from .aliases import AliasLookup
from .reports_reader import read_report

# Assets are data, not code: they live outside the tree, in the directory
# ESP_EXTRACTION_ASSETS_DIR names, and are checked against the SHA-256 MANIFEST.txt records.
MANIFEST = Path(__file__).with_name("MANIFEST.txt")
SEED = "m2-d31-seed-aliases.csv"
EVIDENCE = "alias-evidence-v1.json"
CATALOGUE = "universal_catalog.parquet"


def asset(name: str) -> Path:
    """The path of one stage asset, after checking its SHA-256 against MANIFEST.txt."""
    return _asset(name, MANIFEST)


FAMILY_LABEL = {
    "spyglass": "F1", "championx": "F2", "slb": "F3", "els": "F4",
    "baker_prolift": "F5", "xsize": "F6", "valiant": "F7",
}


def observation_id(*parts) -> str:
    """Stable surrogate: a digest of the point's identity, not a counter."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return "obs_" + hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


@dataclass
class StageResult:
    pump_config: pd.DataFrame
    esp_well_design_context: pd.DataFrame
    curve_observations: pd.DataFrame
    # reconciliation, d16_log, d17_log, gas_log, d16_tier_a_fallback, unit_conversions
    log: dict
    quarantine: list
    # Validation; record shapes in extraction/validate/validate.py and the README.
    findings: list = field(default_factory=list)
    unknown_models: list = field(default_factory=list)
    review: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    spec_validation_status: str | None = None


# ==========================================================================
# Alias evidence (D31).  A dict: printed string, lowercased -> flag.
# ==========================================================================


def _learn(lookup, report_texts):
    """The batch loader's evidence pass, verbatim: a later report's flag wins."""
    reports = [read_report(text) for text in report_texts]
    for report in reports:
        for obj in report.blocks["stg_alias_evidence"]:
            printed = next((obj[k] for k in schemas.ALIAS_PRINTED_SYNONYMS if obj.get(k)), None)
            if printed:
                lookup.learn_evidence(
                    printed, report.vendor_family,
                    obj.get("model_normalization_flag"), obj.get("evidence_kind"),
                )
    return dict(lookup.evidence_flags)


def corpus_evidence(report_texts) -> dict:
    """Corpus mode: evidence from every report, in the order given.

    The batch loader read its reports in file-name order; pass them that way.
    """
    return _learn(AliasLookup(asset(SEED)), report_texts)


def frozen_evidence() -> dict:
    """The corpus evidence as frozen in the versioned asset (hash in MANIFEST.txt)."""
    return json.loads(asset(EVIDENCE).read_text(encoding="utf-8"))["flags"]


def production_evidence(report_text: str, frozen: dict | None = None) -> dict:
    """Production mode: the frozen corpus evidence, then this report's own, learned last."""
    lookup = AliasLookup(asset(SEED))
    lookup.evidence_flags = dict(frozen_evidence() if frozen is None else frozen)
    return _learn(lookup, [report_text])


# ==========================================================================
# Stage
# ==========================================================================


def stage(report_text: str, organization_id, well_id, family: str, evidence: dict,
          *, detection=None, prep=None) -> StageResult:
    """Stage and validate one report.  `family` is the code Detect assigned (F1..F7).

    `detection` and `prep` are the run's Detect and Prep results.  Without them the
    checks that read page roles or page text are not evaluated, and say so in
    `notices` and in each review record's `grounding`.
    """
    from ..validate import structure_findings, validate

    report = read_report(report_text, family)
    invalid = structure_findings(report_text, report)
    if invalid:
        raise ExtractionError("REPORT_INVALID", "; ".join(f["message"] for f in invalid),
                              {"findings": invalid})
    printed = FAMILY_LABEL.get(report.vendor_family)
    if printed != family:
        raise ExtractionError(
            "FAMILY_MISMATCH",
            f"report vendor_family {report.vendor_family!r} is {printed}, Detect assigned {family}",
            {"vendor_family": report.vendor_family, "report_family": printed, "detected_family": family},
        )

    lookup = AliasLookup(asset(SEED))
    lookup.evidence_flags = dict(evidence)
    quarantine = T.Quarantine()
    pump_config, design_context, observations = [], [], []
    d16_log, d17_log, gas_log, reconciliation = [], [], [], []
    tier_a_fallback = []

    T.check_schema_keys(report, quarantine)
    T.quarantine_truncated(report, quarantine)

    declared = report.row_counts or {}
    for block in report.blocks:
        reconciliation.append({
            "source_document": report.source_document or report.slug,
            "family": report.family_code,
            "block": block,
            "footer_declared": declared.get(block),
            "objects_parsed": len(report.blocks[block]),
            "duplicates_dropped": report.duplicates.get(block, 0),
            "truncated_lines": sum(1 for b, _ in report.truncated if b == block),
        })

    effective_from = V.parse_date(report.frontmatter.get("effective_from"))

    # The batch loader matched the well here, from frontmatter, and skipped an
    # unmatched one.  The match is now the caller's; None is unmatched.
    if well_id is not None:
        _load_one(report, organization_id, well_id, effective_from, lookup, quarantine,
                  pump_config, design_context, observations, d16_log, d17_log,
                  gas_log, tier_a_fallback)

    checked = validate(report, family, lookup, quarantine.rows, reconciliation, d16_log, d17_log,
                       pump_config, detection=detection, prep=prep)
    return StageResult(
        pump_config=_frame(pump_config),
        esp_well_design_context=_frame(design_context),
        curve_observations=_frame(observations),
        log={
            "reconciliation": reconciliation,
            "d16_log": d16_log,
            "d17_log": d17_log,
            "gas_log": gas_log,
            "d16_tier_a_fallback": tier_a_fallback,
            "unit_conversions": quarantine.conversions,
        },
        quarantine=quarantine.rows,
        **checked,
    )


def _frame(rows):
    """Columns in first-appearance order, as the batch loader's table writer took
    them.  Object dtype, so every value stays the Python value the loader built."""
    return pd.DataFrame(rows, columns=list(dict.fromkeys(c for r in rows for c in r)), dtype=object)


def _load_one(report, organization_id, well_id, effective_from, lookup, quarantine,
              pump_config, design_context, observations, d16_log, d17_log,
              gas_log, tier_a_fallback):
    family = report.vendor_family

    # ---------------- curve observations (D10, D9) ----------------
    # Built first: D16's Tier A guard confirms a column against this document's
    # own loaded observations before dropping it, so the rows have to exist.
    obs = _observations(report, well_id, lookup, quarantine)
    obs = _resolve_gas_cascade(report, obs, lookup, quarantine, gas_log, well_id)
    observations.extend(obs)

    # ---------------- design context (D16) ----------------
    dc_rows = report.blocks["stg_design_context"]
    dc_keys = T.scenario_keys(dc_rows)
    design_key, selection_rule = T.select_design_scenario(report, dc_rows)
    findings, halts, d16_status, dropped = T.d16_check(
        report, dc_rows, design_key, quarantine, obs, tier_a_fallback,
    )

    d16_log.append({
        "well_id": well_id,
        "source_document": report.source_document,
        "family": report.family_code,
        "status": d16_status,
        "n_scenarios": len(set(dc_keys)),
        "selected_scenario": design_key,
        "scenario_selection_rule": selection_rule,
        "halted_columns": halts,
        "tier_a_dropped_columns": dropped,
        "findings": findings,
    })

    if dc_rows:
        chosen = next((o for k, o in zip(dc_keys, dc_rows) if k == design_key), dc_rows[0])
        design_context.append(
            _design_context_row(report, chosen, dc_rows, organization_id, well_id, effective_from,
                                design_key, selection_rule, findings, d16_status,
                                halts, dropped, quarantine)
        )

    # ---------------- pump config (D31 then D17 then CF-28) ----------------
    typed = []
    for obj in report.blocks["stg_pump_config"]:
        printed = obj.get("pump_model_as_printed")
        resolved = lookup.resolve(printed, family)
        alt = obj.get("pump_model_as_printed_alt")
        alt_resolved = lookup.resolve(alt, family) if alt else None

        if obj.get("extraction_status") == "conflict" and alt:
            # Both readings are in the report; neither is picked here.
            quarantine.reject(
                report, "pump_config", "pump_model_as_printed",
                json.dumps({"as_printed": printed, "as_printed_alt": alt}),
                "extraction_status_conflict", obj.get("source_page"),
                extra={"canonical_as_printed": resolved.canonical,
                       "canonical_alt": alt_resolved.canonical if alt_resolved else None,
                       "converges": bool(alt_resolved and resolved.canonical
                                         and resolved.canonical == alt_resolved.canonical)},
            )
            if alt_resolved and resolved.canonical and alt_resolved.canonical \
                    and resolved.canonical != alt_resolved.canonical:
                # Two genuinely different models for one body: refuse to choose.
                resolved = type(resolved)(None, "conflict between printed forms; unresolved", "unresolved")

        if resolved.flag in ("composite", "not_a_model"):
            quarantine.reject(
                report, "pump_config", "pump_model_as_printed", printed,
                f"model_normalization_{resolved.flag}", obj.get("source_page"),
                extra={"basis": resolved.basis},
            )

        typed.append({
            "organization_id": organization_id,
            "well_id": well_id,
            "effective_from": effective_from,
            "section_order": T.take(report, quarantine, "pump_config", obj, "section_order", "int"),
            "section_role": obj.get("section_role"),
            "pump_model_as_printed": printed,
            "pump_model_canonical": resolved.canonical,
            "pump_model_alias_flag": resolved.flag,
            "pump_model_alias_basis": resolved.basis,
            "manufacturer": obj.get("manufacturer"),
            "vendor_family": family,
            "housing_count": T.take(report, quarantine, "pump_config", obj, "housing_count", "int"),
            "stages": T.take(report, quarantine, "pump_config", obj, "stages", "int"),
            "stages_basis": obj.get("stages_basis"),
            "stages_per_housing_as_printed": obj.get("stages_per_housing_as_printed"),
            "top_md_ft": T.take(report, quarantine, "pump_config", obj, "top_md_ft"),
            "bottom_md_ft": T.take(report, quarantine, "pump_config", obj, "bottom_md_ft"),
            "source_document": report.source_document,
            "source_page": obj.get("source_page"),
            "scenario_label_staged": obj.get("scenario_label_staged"),
            "extraction_status": obj.get("extraction_status"),
            "provenance": obj.get("provenance"),
            "section_grain": obj.get("section_grain"),
            "staging_configuration_as_printed": obj.get("staging_configuration_as_printed"),
            "_scenario": T.scenario_key(obj),
        })

    if typed:
        collapsed, d17_status, detail = T.d17_collapse(report, typed, design_key)
        d17_log.append({
            "well_id": well_id,
            "source_document": report.source_document,
            "family": report.family_code,
            "status": d17_status,
            "n_scenarios": len({r["_scenario"] for r in typed}),
            "detail": detail,
        })
        for row in T.to_model_grouped(collapsed):
            row["d17_status"] = d17_status
            pump_config.append(row)


def _design_context_row(report, chosen, all_rows, organization_id, well_id, effective_from,
                        design_key, selection_rule, findings, d16_status,
                        halts, dropped, quarantine):
    row = {"organization_id": organization_id, "well_id": well_id, "effective_from": effective_from}
    halted = set(halts)
    tier_a_dropped = set(dropped)

    numeric = {
        "intake_set_depth_md_ft", "intake_set_depth_tvd_ft", "perf_top_md_ft",
        "datum_depth_ft", "bottom_of_equipment_md_ft", "oil_api_deg", "oil_sg",
        "water_sg", "gas_sg", "water_cut_design_frac", "bubble_point_psi",
        "static_datum_pressure_psi", "solution_gor_rso_scf_stb", "reservoir_temp_f",
        "intake_temp_f", "surface_temp_f", "motor_power_factor",
        "motor_efficiency_ratio", "motor_nameplate_hp", "motor_nameplate_volts",
        "design_frequency_hz", "design_frequency_min_hz", "design_frequency_max_hz",
        "vendor_mixture_sg", "vendor_mixture_gradient_psi_per_ft",
        "vendor_tdh_at_design_ft", "vendor_pump_efficiency_pct",
        "vendor_total_system_efficiency_pct", "vendor_motor_power_kw",
        "vendor_free_gas_into_pump_pct",
    }
    for column in schemas.DESIGN_CONTEXT_COLUMNS:
        if column in ("organization_id", "well_id", "effective_from"):
            continue
        if column == "bubble_point_equals_static_datum":
            continue
        if column in halted:
            # D16-HALT (Tier C): an "invariant" column that varied.  Left NULL;
            # both values are already in quarantine.  Never silently one of them.
            row[column] = None
        elif column in tier_a_dropped:
            # D16 Tier A: confirmed to restate a value curve_observations already
            # holds at its proper grain.  Dropped, not quarantined -- nothing is
            # lost, so there is nothing to isolate.
            row[column] = None
        elif column in numeric:
            row[column] = T.take(report, quarantine, "esp_well_design_context", chosen, column)
        elif column == "vendor_family":
            row[column] = report.vendor_family
        elif column == "source_document":
            row[column] = report.source_document
        else:
            row[column] = chosen.get(column)

    # §5.3 step 6 -- true only where it holds in *every* scenario.
    flags = [V.parse_bool(o.get("bubble_point_equals_static_datum")).value
             for o in all_rows if "bubble_point_equals_static_datum" in o]
    row["bubble_point_equals_static_datum"] = (all(f is True for f in flags) if flags else None)

    # §5.3 steps 2-4 -- the observed span and the rung that fired, retained.
    bubble = findings.get("bubble_point_psi")
    row["bubble_point_scenario_variance"] = bool(bubble and bubble["status"] == "D16-WARN")
    span = []
    if bubble:
        for raw in bubble["all_values"].values():
            parsed = V.parse_scalar(raw, "bubble_point_psi")
            if parsed.value is not None:
                span.append(parsed.value)
    row["bubble_point_psi_min"] = min(span) if span else None
    row["bubble_point_psi_max"] = max(span) if span else None
    row["scenario_selection_rule"] = chosen.get("scenario_selection_rule") or selection_rule
    row["d16_status"] = d16_status
    row["d16_halted_columns"] = ", ".join(sorted(halted)) or None
    row["n_scenarios"] = len(set(T.scenario_keys(all_rows)))

    # §5.3 step 5 -- provenance_json gains one entry per allowlisted column that
    # varied, carrying every scenario's value and the rung that selected one.
    provenance = {}
    printed = chosen.get("provenance_json")
    if printed:
        try:
            provenance = json.loads(printed)
        except (ValueError, TypeError):
            provenance = {"_unparsed": printed}
    for column, finding in findings.items():
        if finding["status"] != "D16-WARN":
            continue
        provenance[column] = {
            "selected": finding["selected"],
            "all_values": finding["all_values"],
            "selection_rule": selection_rule,
        }
    row["provenance_json"] = json.dumps(provenance, sort_keys=True) if provenance else None
    return row


def _observations(report, well_id, lookup, quarantine):
    out, seen = [], {}
    for obj in report.blocks["stg_curve_observations"]:
        printed = obj.get("pump_model_as_printed")
        resolved = lookup.resolve(printed, report.vendor_family) if printed else None
        if resolved and resolved.flag in ("composite", "not_a_model"):
            quarantine.reject(
                report, "curve_observations", "pump_model_as_printed", printed,
                f"model_normalization_{resolved.flag}", obj.get("source_page"),
            )

        # D9, corpus-wide: head is never stored in psi.
        head_unit = str(obj.get("head_unit_as_printed", "")).strip().lower()
        if head_unit and schemas.UNIT_DIMENSION.get(head_unit) == schemas.PRESSURE_DIMENSION:
            quarantine.reject(
                report, "curve_observations", "head_as_printed", obj.get("head_as_printed"),
                "d9_head_in_pressure_units", obj.get("source_page"),
            )
            head = None
        else:
            head = T.take(report, quarantine, "curve_observations", obj, "head_as_printed")

        frequency = T.take(report, quarantine, "curve_observations", obj, "frequency_hz_as_printed")
        flow = T.take(report, quarantine, "curve_observations", obj, "flow_as_printed")
        power = T.take(report, quarantine, "curve_observations", obj, "power_as_printed")
        stages = T.take(report, quarantine, "curve_observations", obj, "stages_at_observation", "int")

        flow_60, head_60, power_60, basis = T.normalize_to_60hz(
            frequency, flow, head, power, stages,
            obj.get("head_basis_as_printed"), obj.get("power_basis_as_printed"),
        )

        point_type = obj.get("point_type")
        role = obj.get("curve_observation_role")
        scenario = T.scenario_key(obj)

        row = {
            "observation_id": observation_id(
                resolved.canonical if resolved else printed, point_type, frequency,
                report.source_document, scenario, obj.get("source_page"),
                flow, head, power,
            ),
            "pump_model_as_printed": printed,
            "pump_model_canonical": resolved.canonical if resolved else None,
            "manufacturer": obj.get("manufacturer"),
            "point_type": point_type,
            "point_type_in_enum": point_type in schemas.POINT_TYPE_ENUM if point_type else None,
            "frequency_hz_as_printed": frequency,
            "flow_as_printed": flow,
            "flow_unit_as_printed": obj.get("flow_unit_as_printed"),
            "head_as_printed": head,
            "head_unit_as_printed": obj.get("head_unit_as_printed"),
            "head_basis_as_printed": obj.get("head_basis_as_printed"),
            "power_as_printed": power,
            "power_unit_as_printed": obj.get("power_unit_as_printed"),
            "power_basis_as_printed": obj.get("power_basis_as_printed"),
            "stages_at_observation": stages,
            "efficiency_pct": T.take(report, quarantine, "curve_observations", obj, "efficiency_pct"),
            "flow_bpd_60hz": flow_60,
            "head_ft_per_stage_60hz": head_60,
            "power_hp_per_stage_60hz": power_60,
            "normalization_basis": basis,
            "assumed_pump_wear_pct": T.take(report, quarantine, "curve_observations", obj,
                                            "assumed_pump_wear_pct"),
            "viscosity_corrected": V.parse_bool(obj.get("viscosity_corrected")).value,
            "fluid_sg_at_observation": T.take(report, quarantine, "curve_observations", obj,
                                              "fluid_sg_at_observation"),
            "free_gas_at_inlet_pct": T.take(report, quarantine, "curve_observations", obj,
                                            "free_gas_at_inlet_pct"),
            "free_gas_at_inlet_source": ("observation" if "free_gas_at_inlet_pct" in obj else None),
            "observation_is_composite": V.parse_bool(obj.get("observation_is_composite")).value,
            "curve_observation_role": role,
            "curve_observation_role_withdrawn": role in schemas.CURVE_OBSERVATION_ROLE_WITHDRAWN,
            "envelope_status": obj.get("envelope_status"),
            "digitization_candidate_page": obj.get("digitization_candidate_page"),
            "vendor_family": report.vendor_family,
            "source_document": report.source_document,
            "source_page": obj.get("source_page"),
            "scenario_label": obj.get("scenario_label") or obj.get("scenario_label_staged"),
            "scenario_ordinal": scenario,
            "well_id": well_id,
            "provenance": obj.get("provenance"),
            "sensitivity_case": T.is_sensitivity(obj),
        }

        # A "point" is keyed on model x point_type x frequency x document x
        # scenario.  Scenario is part of the key: two scenarios at different
        # frequencies are two real points on the affinity family, and collapsing
        # them throws away the only multi-frequency evidence most models have.
        # Two scenarios at one frequency yielding identical values are a
        # duplicate, so the dedupe is on the value tuple.
        key = (row["pump_model_canonical"] or printed, point_type, frequency,
               report.source_document, scenario)
        value_tuple = (flow, head, power, stages, row["efficiency_pct"],
                       row["free_gas_at_inlet_pct"], obj.get("source_page"))
        if seen.get(key) == value_tuple:
            continue
        seen[key] = value_tuple
        out.append(row)
    return out


def _resolve_gas_cascade(report, obs, lookup, quarantine, gas_log, well_id):
    """`stg_gas_cascade` is staging and becomes no load table.

    Rows with `component_is_pump_body = true` resolve into
    `curve_observations.free_gas_at_inlet_pct`.  Separator and intake stages are
    context, not calculator input: they stay in staging and are carried nowhere.

    Where an observation already carries a value and a pump-body row also
    supplies one, the two are compared.  Equal -> the duplicate is dropped.
    Unequal -> halt and log both; neither is silently preferred.
    """
    for obj in report.blocks["stg_gas_cascade"]:
        if str(obj.get("component_is_pump_body", "")).strip().lower() != "true":
            continue

        label = str(obj.get("stage_label_as_printed", "")).lower()
        if "discharge" in label:
            side = "discharge"
        elif "intake" in label or "into pump" in label or "inlet" in label:
            side = "inlet"
        else:
            side = None

        component = obj.get("component_as_printed")
        canonical = lookup.resolve(component, report.vendor_family).canonical if component else None
        parsed = V.parse_scalar(obj.get("gas_pct_as_printed"), "gas_pct_as_printed")
        scenario = T.scenario_key(obj)

        entry = {
            "well_id": well_id,
            "source_document": report.source_document,
            "family": report.family_code,
            "stage_label": obj.get("stage_label_as_printed"),
            "component_as_printed": component,
            "component_canonical": canonical,
            "scenario_ordinal": scenario,
            "side": side,
            "cascade_value": parsed.value,
            "sensitivity_case": T.is_sensitivity(obj),
        }

        if side != "inlet" or parsed.value is None:
            entry["disposition"] = "not_an_inlet_value; stays in staging"
            gas_log.append(entry)
            continue
        if not canonical:
            entry["disposition"] = "component does not resolve to a model; stays in staging"
            gas_log.append(entry)
            continue

        targets = [o for o in obs
                   if o["pump_model_canonical"] == canonical
                   and str(o["scenario_ordinal"]) == str(scenario)
                   and o["point_type"] in ("q_intake", "design_point", "curve_point")]
        if not targets:
            entry["disposition"] = "no matching observation row; stays in staging"
            gas_log.append(entry)
            continue

        for target in targets:
            existing = target["free_gas_at_inlet_pct"]
            if existing is None:
                target["free_gas_at_inlet_pct"] = parsed.value
                target["free_gas_at_inlet_source"] = "gas_cascade_pump_body"
                entry["disposition"] = "resolved into curve_observations"
            elif abs(existing - parsed.value) <= 1e-6:
                entry["disposition"] = "equal to the observation value; duplicate dropped"
            else:
                entry["disposition"] = "HALT: cascade and observation disagree"
                entry["observation_value"] = existing
                quarantine.reject(
                    report, "curve_observations", "free_gas_at_inlet_pct",
                    json.dumps({"observation": existing, "gas_cascade": parsed.value,
                                "component": component, "stage": obj.get("stage_label_as_printed")}),
                    "gas_cascade_disagrees_with_observation", obj.get("source_page"),
                )
        gas_log.append(entry)
    return obs
