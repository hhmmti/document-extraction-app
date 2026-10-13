"""Load-table schemas, staging key sets, and the unit vocabulary.

Load schemas are m1-extraction-spec.md § Schemas, verbatim in column set.
Staging key sets are staging-schemas.md (load columns plus the staging-only
columns each table names). A key in neither set is a `V-10` out-of-schema key:
its value has no destination and goes to quarantine rather than being dropped
in silence.

`stg_gas_cascade` deliberately has no load table. Rows with
`component_is_pump_body = true` resolve into `curve_observations`; separator and
intake stages stay in staging as the audit record (staging-schemas.md,
*Open for confirmation* item 2, closing CF-16).
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Load table column sets (m1-extraction-spec.md § Schemas)
# --------------------------------------------------------------------------

PUMP_CONFIG_COLUMNS = [
    "organization_id", "well_id", "effective_from",
    "section_order", "section_role",
    "pump_model_as_printed", "pump_model_canonical",
    "manufacturer", "vendor_family",
    "housing_count", "stages", "stages_basis",
    "top_md_ft", "bottom_md_ft",
    "source_document", "source_page",
    "scenario_label_staged", "extraction_status", "provenance",
]

DESIGN_CONTEXT_COLUMNS = [
    "organization_id", "well_id", "effective_from",
    "intake_set_depth_md_ft", "intake_set_depth_tvd_ft", "depth_reference_basis",
    "perf_top_md_ft", "datum_depth_ft", "bottom_of_equipment_md_ft",
    "oil_api_deg", "oil_sg", "water_sg", "gas_sg", "gas_sg_basis",
    "water_cut_design_frac",
    "bubble_point_psi", "bubble_point_unit_as_printed", "bubble_point_provenance",
    "bubble_point_correlation", "bubble_point_calculated_flag",
    "bubble_point_equals_static_datum", "static_datum_pressure_psi",
    "solution_gor_rso_scf_stb",
    "reservoir_temp_f", "intake_temp_f", "surface_temp_f",
    "motor_power_factor", "motor_power_factor_basis", "motor_efficiency_ratio",
    "motor_nameplate_hp", "motor_nameplate_volts", "motor_type",
    "design_frequency_hz", "design_frequency_min_hz", "design_frequency_max_hz",
    "vendor_mixture_sg", "vendor_mixture_gradient_psi_per_ft",
    "vendor_tdh_at_design_ft", "vendor_pump_efficiency_pct",
    "vendor_total_system_efficiency_pct", "vendor_motor_power_kw",
    "vendor_free_gas_into_pump_pct", "vendor_multiphase_correlation",
    "vendor_family", "source_document", "provenance_json",
]

CURVE_OBSERVATION_COLUMNS = [
    "observation_id",
    "pump_model_as_printed", "pump_model_canonical", "manufacturer",
    "point_type",
    "frequency_hz_as_printed",
    "flow_as_printed", "flow_unit_as_printed",
    "head_as_printed", "head_unit_as_printed", "head_basis_as_printed",
    "power_as_printed", "power_unit_as_printed", "power_basis_as_printed",
    "stages_at_observation", "efficiency_pct",
    "flow_bpd_60hz", "head_ft_per_stage_60hz", "power_hp_per_stage_60hz",
    "normalization_basis",
    "assumed_pump_wear_pct", "viscosity_corrected", "fluid_sg_at_observation",
    "free_gas_at_inlet_pct", "observation_is_composite",
    "vendor_family", "source_document", "source_page",
    "scenario_label", "well_id", "provenance",
]

# --------------------------------------------------------------------------
# Staging-only columns (staging-schemas.md).  Legal to receive, not all loaded.
# --------------------------------------------------------------------------

STG_PUMP_CONFIG_ONLY = {
    "scenario_label_staged", "scenario_ordinal", "is_design_scenario",
    "printed_order_direction", "section_grain", "model_normalization_flag",
    "pump_model_as_printed_alt", "pump_series_as_printed",
    "staging_configuration_as_printed", "stages_per_housing_as_printed",
    "stages_out", "sensitivity_case",
}

STG_DESIGN_CONTEXT_ONLY = {
    "scenario_label_staged", "scenario_ordinal", "is_design_scenario",
    "extraction_status", "surface_power_factor", "vendor_liquid_phase_sg",
    "pump_setting_md_ft", "pump_setting_vd_ft", "vendor_comments_as_printed",
    "scenario_selection_rule", "vendor_required_tdh_ft",
    "vendor_calculated_water_cut_frac", "vendor_system_tdh_range_as_printed",
    "motor_frequency_hz", "lateral_depth_tvd_ft",
    "oil_rate_as_printed", "water_rate_as_printed", "gas_sg_mixture",
    "sensitivity_case",
}

STG_CURVE_OBSERVATION_ONLY = {
    "curve_observation_role", "scenario_label_staged", "scenario_ordinal",
    "is_design_scenario", "extraction_status",
    "discharge_pressure_psi_as_printed", "section_intake_pressure_psi_as_printed",
    "free_gas_at_discharge_pct", "free_allowed_gas_pct_as_printed",
    "fluid_density_as_printed", "lift_as_printed", "envelope_status",
    "digitization_candidate_page", "speed_rpm_as_printed",
    "flow_reference_as_printed", "viscosity_correction_factors_as_printed",
    "sensitivity_case",
}

STG_GAS_CASCADE_KEYS = {
    "well_id", "scenario_label_staged", "scenario_ordinal", "stage_order",
    "stage_label_as_printed", "component_as_printed", "component_is_pump_body",
    "gas_pct_as_printed", "gas_rate_as_printed", "gas_rate_unit_as_printed",
    "separation_efficiency_pct", "turpin_indicator_as_printed",
    "dunbar_indicator_as_printed", "correlation_as_printed", "sensitivity_case",
    "vendor_family", "source_document", "source_page", "extraction_status",
}

STG_ALIAS_EVIDENCE_KEYS = {
    "model_as_printed", "proposed_canonical", "vendor_family", "evidence_kind",
    "evidence_value", "model_normalization_flag", "source_document", "source_page",
}

LEGAL_KEYS = {
    "stg_pump_config": set(PUMP_CONFIG_COLUMNS) | STG_PUMP_CONFIG_ONLY,
    "stg_design_context": set(DESIGN_CONTEXT_COLUMNS) | STG_DESIGN_CONTEXT_ONLY,
    "stg_curve_observations": set(CURVE_OBSERVATION_COLUMNS) | STG_CURVE_OBSERVATION_ONLY,
    "stg_gas_cascade": STG_GAS_CASCADE_KEYS,
    "stg_alias_evidence": STG_ALIAS_EVIDENCE_KEYS,
}

# The alias block was written with per-family key spellings that never reached
# the schema.  These are containers for the same two facts -- the printed string
# and the proposed canonical -- so they are accepted under their schema names.
# This normalises a *key*, never a value.
ALIAS_PRINTED_SYNONYMS = (
    "model_as_printed", "pump_model_as_printed", "printed_string",
    "alias_string_as_printed", "raw_string_as_printed", "alias_text_as_printed",
    "alias_as_printed",
)
ALIAS_CANONICAL_SYNONYMS = ("proposed_canonical", "pump_model_canonical")
ALIAS_TOLERATED = set(ALIAS_PRINTED_SYNONYMS) | set(ALIAS_CANONICAL_SYNONYMS) | {
    "manufacturer", "provenance", "scenario_label", "pump_model_context",
}

# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

# staging-schemas.md, `point_type` enum extended
POINT_TYPE_ENUM = {
    "ror_min", "bep", "ror_max", "design_point", "shutoff", "curve_point",
    "q_intake", "q_discharge",
}

CURVE_OBSERVATION_ROLE_ENUM = {
    "head_curve", "bhp_curve", "single_pump_chart", "multiscenario",
    "tapered_composite", "design",
}

# Withdrawn 2026-08-20.  f2-championx.md was never corrected and still reads as
# though these are live; they are not.  Rows carrying them keep the printed
# value and are flagged, never re-mapped.
CURVE_OBSERVATION_ROLE_WITHDRAWN = {"cross_check", "contribution"}

STAGES_BASIS_ENUM = {"per_housing", "total_reported", "group_total"}

# m2-validator-spec.md §5.2 -- adding to this list is a decision, not a fix.
SCENARIO_VARIANT_ALLOWLIST = {
    "bubble_point_psi", "static_datum_pressure_psi", "water_cut_design_frac",
    "oil_api_deg", "water_sg", "gas_sg",
    "reservoir_temp_f", "intake_temp_f", "surface_temp_f",
}

# §5.2: scenario-grained by nature; they belong to curve_observations, and their
# presence in stg_design_context is an extraction error, not a D16 event.
NOT_D16_EVENTS = {
    "design_frequency_hz", "design_frequency_min_hz", "design_frequency_max_hz",
}

# --------------------------------------------------------------------------
# Unit vocabulary
# --------------------------------------------------------------------------
# `dimension` is what the column name promises.  A token naming a *different*
# dimension means the value was written into the wrong column: that is not a
# conversion, it is a mapping error, and it is quarantined (the F3 corpus
# `vendor_motor_power_kw: "93.2 hp"` case).  A token naming the same dimension
# at a different *scale* (% vs fraction) is canonicalised here, because D8
# layer 3 puts unit canonicalisation in the transform -- every such conversion
# is written to unit_conversions.jsonl so none of it is hidden.

UNIT_DIMENSION = {
    "ft": "length", "'": "length", "feet": "length", "(ft)": "length",
    "psi": "pressure", "psia": "pressure", "psig": "pressure", "(psi)": "pressure",
    "kw": "power", "hp": "power", "bhp": "power",
    "hz": "frequency", "(hz)": "frequency",
    "v": "voltage", "volts": "voltage",
    "f": "temperature", "f°": "temperature", "°f": "temperature",
    "ºf": "temperature", "(f)": "temperature", "degf": "temperature",
    # A bare degree sign names no dimension of its own: `42°` in `oil_api_deg`
    # is 42 °API, and treating it as a temperature would reject a good value.
    "°": None, "º": None,
    "api": "api", "ºapi": "api", "°api": "api",
    "spgr": "sg", "sg": "sg",
    "lb/ft3": "density", "lb/ft³": "density", "lbm/ft3": "density",
    "%": "ratio", "(0-1)": "ratio", "frac": "ratio",
    "rpm": "speed",
    "bpd": "flow", "bbl/d": "flow", "stb/d": "flow", "stb/day": "flow",
    "b/d": "flow", "bbl/day": "flow",
    "psi/ft": "gradient",
    "scf/stb": "gor", "scf/bbl": "gor", "scf/bl": "gor",
    "mcf/d": "gas_rate", "mscf/d": "gas_rate", "scf/d": "gas_rate", "bpd_gas": "gas_rate",
}

# column suffix -> (dimension it promises, canonical scale)
COLUMN_DIMENSION = {
    "_ft": "length", "_md_ft": "length", "_tvd_ft": "length", "_depth_ft": "length",
    "_psi": "pressure",
    "_kw": "power", "_hp": "power",
    "_hz": "frequency",
    "_volts": "voltage",
    "_f": "temperature",
    "_deg": "api",
    "_sg": "sg",
    "_pct": "ratio", "_frac": "ratio", "_ratio": "ratio",
    "_rpm": "speed",
    "_psi_per_ft": "gradient",
    "_scf_stb": "gor",
}

# Columns whose canonical scale is a fraction in [0,1]; a `%` token is divided
# by 100.  Everything else on the `ratio` dimension is stored as a percentage.
FRACTION_COLUMNS = {
    "water_cut_design_frac", "motor_efficiency_ratio", "motor_power_factor",
    "vendor_calculated_water_cut_frac",
}


# Columns whose name does not end in a unit suffix but which still promise a
# dimension.  `head_as_printed` is the load-bearing one: D9 forbids head in psi,
# and without this entry a psi token on a head value would load as a number.
COLUMN_DIMENSION_EXACT = {
    "head_as_printed": "length",
    "lift_as_printed": "length",
    "flow_as_printed": "flow",
    "q_intake_as_printed": "flow",
    "q_discharge_as_printed": "flow",
    "power_as_printed": "power",
    "frequency_hz_as_printed": "frequency",
    "fluid_sg_at_observation": "sg",
    "fluid_density_as_printed": "density",
    "gas_pct_as_printed": "ratio",
    "efficiency_pct": "ratio",
    "discharge_pressure_psi_as_printed": "pressure",
    "section_intake_pressure_psi_as_printed": "pressure",
    "speed_rpm_as_printed": "speed",
    "assumed_pump_wear_pct": "ratio",
    "free_allowed_gas_pct_as_printed": "ratio",
    "vendor_liquid_phase_sg": "sg",
    "vendor_mixture_sg": "sg",
    "oil_sg": "sg",
    "gas_sg_mixture": "sg",
    "stages": None,
    "housing_count": None,
    "stages_at_observation": None,
    "stages_out": None,
    "section_order": None,
    "stage_order": None,
    "scenario_ordinal": None,
    "motor_nameplate_hp": "power",
}

# D9, corpus-wide: head is never stored in psi.
HEAD_COLUMNS = ("head_as_printed", "lift_as_printed")
PRESSURE_DIMENSION = "pressure"


def column_dimension(column: str):
    """The dimension a column name promises, or None if it promises nothing."""
    if column in COLUMN_DIMENSION_EXACT:
        return COLUMN_DIMENSION_EXACT[column]
    for suffix in sorted(COLUMN_DIMENSION, key=len, reverse=True):
        if column.endswith(suffix):
            return COLUMN_DIMENSION[suffix]
    return None
