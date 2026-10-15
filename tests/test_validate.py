"""Validation on synthetic reports: every implemented rule fires on one fixture and stays
silent on another; so do the guards and every review signal. Needs no corpus and no assets."""

import json

import pytest

from extraction import ExtractionError
from extraction.assemble import ASSETS
from extraction.families import PAGE_MAP
from extraction.stage import transform as T
from extraction.stage.aliases import AliasLookup
from extraction.stage.reports_reader import BLOCKS, read_report
from extraction.stage.stage import stage
from extraction.validate import rules as R
from extraction.validate import spec_validation_status, structure_findings, unknown_models, validate
from extraction.validate.review import review, supply
from extraction.validate.validate import _guards, declared_halt

VENDOR = {"F1": "spyglass", "F2": "championx", "F3": "slb", "F4": "els", "F5": "baker_prolift", "F7": "valiant"}
SHORT = {"pc": "stg_pump_config", "dc": "stg_design_context", "co": "stg_curve_observations",
         "gc": "stg_gas_cascade", "ae": "stg_alias_evidence"}


def report_text(family, fm=None, **blocks):
    front = {"source_document": "test.pdf", "vendor_family": VENDOR[family],
             "well_name_source": "document_body", **(fm or {})}
    lines = ["---", *(f"{k}: {v}" for k, v in front.items()), "---", ""]
    for short, block in SHORT.items():
        lines += [f"## {block}", "```jsonl", *(json.dumps(r) for r in blocks.get(short, [])), "```", ""]
    counts = {block: len(blocks.get(short, [])) for short, block in SHORT.items()}
    return "\n".join(lines + ["## row_counts", "```jsonl", json.dumps(counts), "```", ""])


@pytest.fixture(scope="module")
def lookup(tmp_path_factory):
    seed = tmp_path_factory.mktemp("seed") / "seed.csv"
    seed.write_text("pump_model_as_printed,vendor_family,proposed_canonical,basis\nELS-1234,els,ELS1234,test\n")
    return AliasLookup(seed)


def make(lookup, family, fm=None, roles=None, pages=None, **blocks):
    return R.Doc(read_report(report_text(family, fm, **blocks), family), family, lookup, roles, pages)


def fired(doc, rule_id):
    return [f for f in R.run(doc)[0] if f["rule_id"] == rule_id]


F1_ALL_ROLES = {i: [role] for i, (role, _, _) in enumerate(PAGE_MAP["F1"], 1)}
PM = {"motor_type": "permanent_magnet", "depth_reference_basis": "pump_discharge"}

# rule id, family, the fixture that fires, the fixture that stays silent
CASES = [
    ("V-03", "F1", dict(roles={1: ["SIZING REPORT"]}), dict(roles=F1_ALL_ROLES)),
    ("V-04", "F4", dict(dc=[{"bubble_point_psi": "1800"}]),
     dict(dc=[{"bubble_point_psi": "1800", "bubble_point_unit_as_printed": "psi"}])),
    ("V-06", "F4", dict(pc=[{"pump_model_as_printed": "ELS 1234"}]), dict(pc=[{"pump_model_as_printed": "ELS-1234"}])),
    ("V-07", "F4", dict(pc=[{"section_role": "intake"}]), dict(pc=[{"section_role": "gas_handler"}])),
    ("V-08", "F4", dict(pc=[{"section_order": "1"}, {"section_order": "3"}]),
     dict(pc=[{"section_order": "1", "bottom_md_ft": "9000 ft"}, {"section_order": "2", "bottom_md_ft": "8900 ft"}])),
    ("V-09", "F3", dict(pc=[{"stages": "400", "housing_count": "3", "stages_per_housing_as_printed": "100, 100, 100"}]),
     dict(pc=[{"stages": "400", "housing_count": "4", "stages_per_housing_as_printed": "100, 100, 100, 100"}])),
    ("V-12", "F3", dict(dc=[{"bubble_point_psi": "19985.3 psig"}]), dict(dc=[{"bubble_point_psi": "1800 psig"}])),
    ("V-13", "F4", dict(co=[{"flow_as_printed": "500"}]),
     dict(co=[{"flow_as_printed": "500", "flow_unit_as_printed": "bbl/d"}])),
    ("V-14", "F4", dict(co=[{"head_as_printed": "100", "head_unit_as_printed": "ft"}]),
     dict(co=[{"head_as_printed": "100", "head_unit_as_printed": "ft", "head_basis_as_printed": "per_stage"}])),
    ("V-15", "F4", dict(co=[{"power_as_printed": "10"}]),
     dict(co=[{"power_as_printed": "10", "power_basis_as_printed": "per_stage"}])),
    ("V-17", "F1", dict(co=[{"point_type": "design_point", "scenario_title_as_printed": "100bpd 50.15hz",
                             "frequency_hz_as_printed": "50.16"}]),
     dict(co=[{"point_type": "design_point", "scenario_title_as_printed": "100bpd 50.15hz",
               "frequency_hz_as_printed": "50.16", "scenario_title_frequency_disagrees": "true"}])),
    ("V-19", "F4", dict(dc=[{"oil_api_deg": "40"}]), dict(dc=[{"depth_reference_basis": "intake"}])),
    ("V-F1-1", "F1",
     dict(roles={2: ["Design Schematic"], 6: ["Pumps"]},
          pc=[{"pump_model_as_printed": "SF1750 TS4 XR (HS Shaft)", "source_page": "p2"}],
          ae=[{"model_as_printed": "SF1750 TS4 XR", "source_page": "p6"}]),
     dict(roles={2: ["Design Schematic"], 6: ["Pumps"]},
          pc=[{"pump_model_as_printed": "SF1750 TS4 XR (HS Shaft)", "source_page": "p2"}],
          ae=[{"model_as_printed": "SF1750 TS4 XR (HS Shaft)", "source_page": "p6"}])),
    ("V-F1-2", "F1", dict(co=[{"flow_as_printed": "500*", "viscosity_corrected": "false"}]),
     dict(co=[{"flow_as_printed": "500*", "viscosity_corrected": "true"}])),
    ("V-F1-3", "F1", dict(co=[{"head_basis_as_printed": "per_stage"}]),
     dict(co=[{"lift_as_printed": "900", "head_basis_as_printed": "per_section_total"}])),
    ("V-F1-4", "F1", dict(dc=[{"npshr_ft": "10"}]), dict(dc=[{"free_allowed_gas_pct_as_printed": "10 %"}])),
    ("V-F1-5", "F1", dict(dc=[{"bubble_point_psi": "1800", "bubble_point_unit_as_printed": "psia"}]),
     dict(dc=[{"bubble_point_psi": "1800", "bubble_point_unit_as_printed": "PSIA"}])),
    ("V-F1-6", "F1", dict(dc=[{"motor_rated_amps": "50"}]), dict(dc=[{"motor_nameplate_hp": "200"}])),
    ("V-F2-1", "F2", dict(dc=[{"motor_power_factor": "0.81"}]),
     dict(dc=[{"motor_power_factor": "0.81", "surface_power_factor": "0.77"}])),
    ("V-F2-2", "F2", dict(dc=[{"vendor_mixture_sg": "0.82"}]),
     dict(dc=[{"vendor_mixture_sg": "0.82", "vendor_liquid_phase_sg": "1.01"}])),
    ("V-F2-3", "F2", dict(co=[{"point_type": "bep", "flow_as_printed": "500"}]),
     dict(co=[{"point_type": "curve_point", "flow_as_printed": "500"}])),
    ("V-F2-4", "F2", dict(pages={2: "PUMP MSC_400UNB_35H_93 STG_X"},
                          pc=[{"pump_model_as_printed_alt": "400UNB35H", "source_page": "p2"}]),
     dict(pages={2: "PUMP MSC_400UNB_35H_93 STG_X", 5: "GAS HANDLER"},
          pc=[{"pump_model_as_printed_alt": "GAS HANDLING_400_GAS2900", "source_page": "p5"}])),
    ("V-F2-5", "F2", dict(pc=[{"scenario_ordinal": "2"}]), dict(pc=[{"scenario_ordinal": "1"}])),
    ("V-F3-1", "F3", dict(pc=[{"pump_model_as_printed": "REDA 400 RC1000 4x100", "staging_configuration_as_printed": "4x100"}]),
     dict(pc=[{"pump_model_as_printed": "REDA 400 RC1000", "staging_configuration_as_printed": "4x100"}])),
    ("V-F3-2", "F3", dict(dc=[{"vendor_mixture_gradient_psi_per_ft": "0.433 psi/ft", "oil_api_deg": "39 ºAPI",
                               "water_sg": "1.1", "water_cut_design_frac": "93 %"}]),
     dict(dc=[{"vendor_mixture_gradient_psi_per_ft": "0.468 psi/ft", "oil_api_deg": "39 ºAPI",
               "water_sg": "1.1", "water_cut_design_frac": "93 %"}])),
    ("V-F3-4", "F3", dict(dc=[{"motor_power_factor": "0.8"}]), dict(dc=[{"motor_efficiency_ratio": "0.8"}])),
    ("V-F3-5", "F3", dict(co=[{"point_type": "curve_point", "head_as_printed": "100"}]),
     dict(co=[{"point_type": "design_point", "curve_observation_role": "design", "head_as_printed": "6101.47 ft",
               "head_basis_as_printed": "string_total"}])),
    ("V-F3-6", "F3", dict(pc=[{"section_order": "1", "bottom_md_ft": "5000 ft"}], dc=[{"intake_set_depth_md_ft": "9000 ft"}]),
     dict(pc=[{"section_order": "1", "bottom_md_ft": "9100 ft"}], dc=[{"intake_set_depth_md_ft": "9000 ft"}])),
    ("V-F3-7", "F3", dict(fm={"source_document": "ABCDEF~1.PDF", "well_name_source": "document_header"}),
     dict(fm={"source_document": "ABCDEF~1.PDF", "well_name_source": "document_body"})),
    ("V-F4-1", "F4", dict(pages={1: "Calculated Pb   No"}, dc=[{"bubble_point_psi": "1800"}]),
     dict(pages={1: "Calculated Pb   No"}, dc=[{"bubble_point_calculated_flag": "No"}])),
    ("V-F4-2", "F4", dict(dc=[{"bubble_point_calculated_flag": "No", "bubble_point_provenance": "vendor derived"}]),
     dict(dc=[{"bubble_point_calculated_flag": "No", "bubble_point_provenance": "vendor input"}])),
    ("V-F4-3", "F4", dict(gc=[{"gas_rate_as_printed": "8.43"}]),
     dict(gc=[{"gas_rate_as_printed": "8.43", "gas_rate_unit_as_printed": "mcf/d"}])),
    ("V-F4-5", "F4", dict(pc=[{"pump_model_as_printed": "400ELS1234", "pump_series_as_printed": "400"}]),
     dict(pc=[{"pump_model_as_printed": "ELS-1234", "pump_series_as_printed": "400"}])),
    ("V-F4-6", "F4", dict(pc=[{"top_md_ft": "9000 ft"}]), dict(pc=[{"stages": "100"}])),
    ("V-F4-7", "F4", dict(dc=[{"motor_power_factor": "0.8"}]), dict(dc=[{"motor_nameplate_hp": "200"}])),
    ("V-F5-1", "F5", dict(dc=[{**PM, "motor_frequency_hz": "51.59 Hz", "design_frequency_hz": "51.59 Hz"}]),
     dict(dc=[{**PM, "motor_frequency_hz": "103.18 Hz", "design_frequency_hz": "51.59 Hz"}])),
    ("V-F5-2", "F5", dict(dc=[{**PM, "motor_frequency_hz": "103.18 Hz", "design_frequency_hz": "103.18 Hz"}]),
     dict(dc=[{**PM, "motor_frequency_hz": "103.18 Hz", "design_frequency_hz": "51.59 Hz"}])),
    ("V-F5-3", "F5", dict(dc=[{"depth_reference_basis": "pump_discharge"}]), dict(dc=[PM])),
    ("V-F5-4", "F5", dict(dc=[{"depth_reference_basis": "intake"}]), dict(dc=[PM])),
    ("V-F5-6", "F5", dict(roles={}, pc=[{"sensitivity_case": "true"}]),
     dict(roles={7: ["Sensitivity case table"]}, pc=[{"source_page": "p1"}])),
    ("V-F5-7", "F5", dict(dc=[{**PM, "bubble_point_psi": "1800"}]), dict(dc=[PM])),
    ("V-F5-8", "F5", dict(co=[{"point_type": "bep"}]),
     dict(co=[{"head_as_printed": "9000", "observation_is_composite": "true", "head_basis_as_printed": "string_total"}])),
    ("V-F7-1", "F7", dict(dc=[{"bubble_point_correlation": "Standing"}]),
     dict(dc=[{"bubble_point_correlation": "Al-Shammasi (1999)"}])),
    ("V-F7-3", "F7", dict(co=[{"flow_as_printed": "500"}]),
     dict(co=[{"viscosity_correction_factors_as_printed": "1.0000 / 1.0000 / 1.0000", "viscosity_corrected": "false"}])),
    ("V-F7-4", "F7", dict(gc=[{"turpin_indicator_as_printed": "0.000"}]),
     dict(gc=[{"turpin_indicator_as_printed": "0.000", "dunbar_indicator_as_printed": "0.100"}])),
    ("V-F7-5", "F7", dict(dc=[{"gas_sg": "0.75"}]),
     dict(dc=[{"gas_sg": "0.85", "gas_sg_basis": "hydrocarbon", "gas_sg_mixture": "0.85"}])),
    ("V-F7-6", "F7", dict(dc=[{"intake_temp_f": "150 F", "surface_temp_f": "80 F"}]),
     dict(dc=[{"intake_temp_f": "150 F", "reservoir_temp_f": "180 F", "surface_temp_f": "80 F"}])),
    ("V-F7-9", "F7", dict(dc=[{"pump_intake_pressure_psi": "500"}]),
     dict(co=[{"section_intake_pressure_psi_as_printed": "500 psi"}])),
    ("V-F7-10", "F7", dict(co=[{"point_type": "bep", "flow_as_printed": "500", "flow_unit_as_printed": "STB/D"}]),
     dict(co=[{"point_type": "bep", "flow_as_printed": "500", "flow_unit_as_printed": "bbl/d"}])),
    ("V-X3", "F4", dict(pc=[{"pump_model_as_printed": "ELS-9999"}]), dict(pc=[{"pump_model_as_printed": "ELS-1234"}])),
    ("V-X6", "F4", dict(dc=[{"npshr_ft": "10"}]), dict(dc=[{"motor_nameplate_hp": "200"}])),
    ("V-X7", "F4", dict(dc=[{"productivity_index": "1.2"}]),
     dict(co=[{"section_intake_pressure_psi_as_printed": "500 psi"}])),
]


def test_every_implemented_rule_has_a_case():
    implemented = [rule_id for rule_id, *_ in R.RULES]
    assert len(implemented) == 52
    assert sorted(implemented) == sorted(case[0] for case in CASES)


@pytest.mark.parametrize("rule_id,family,fire,silent", CASES, ids=[c[0] for c in CASES])
def test_rule_fires_and_stays_silent(lookup, rule_id, family, fire, silent):
    assert fired(make(lookup, family, **fire), rule_id)
    assert not fired(make(lookup, family, **silent), rule_id)


def test_v08_fires_on_order_against_depth(lookup):
    doc = make(lookup, "F4", pc=[{"section_order": "1", "bottom_md_ft": "8900 ft"},
                                 {"section_order": "2", "bottom_md_ft": "9000 ft"}])
    assert fired(doc, "V-08")[0]["blocks_publish"] is True


def test_v17_reads_the_design_point_not_a_multi_frequency_curve_row(lookup):
    doc = make(lookup, "F1", co=[
        {"scenario_label": "s", "point_type": "design_point", "frequency_hz_as_printed": "54.14"},
        {"scenario_label": "s", "point_type": "curve_point", "frequency_hz_as_printed": "65",
         "scenario_title_as_printed": "2898bpd 54.14hz"}])
    assert not fired(doc, "V-17")


def test_v_f3_2_records_the_implied_gradient(lookup):
    doc = make(lookup, "F3", **CASES[[c[0] for c in CASES].index("V-F3-2")][2])
    oil = 141.5 / (131.5 + 39)
    assert fired(doc, "V-F3-2")[0]["value"] == {"printed": 0.433, "implied": round(0.433 * (oil * 0.07 + 1.1 * 0.93), 4)}


def test_v_f3_7_takes_no_file_name_only_the_short_name_shape(lookup):
    for name, fires in (("ABCDEF~1.PDF", True), ("abc~12.pdf", True), ("ABCDEFG~1.PDF", False), ("test.pdf", False)):
        doc = make(lookup, "F3", fm={"source_document": name, "well_name_source": "document_header"})
        assert bool(fired(doc, "V-F3-7")) is fires, name


def test_rules_needing_detect_or_prep_are_skipped_without_them(lookup):
    findings, skipped = R.run(make(lookup, "F1"))
    assert {"V-03", "V-F1-1"} <= {rule_id for rule_id, _ in skipped}
    assert not {f["rule_id"] for f in findings} & {"V-03", "V-F1-1"}


def test_contract_grammars_are_verbatim():
    contracts = {"F1": "f1-spyglass.md", "F2": "f2-championx.md", "F3": "f3-slb.md", "F4": "f4-els.md",
                 "F5": "f5-baker.md", "F7": "f7-valiant.md"}
    for family, regex in R.GRAMMAR.items():
        assert regex in (ASSETS / contracts[family]).read_text(encoding="utf-8"), family


# ---- severity (decision 1) ----

def test_round1_severity_map_and_blocks_publish():
    assert R.finding("G-S2", None, "DOC", None, None, None, None, "")["round1_severity"] == "REPORT_INVALID"
    d17 = R.finding("D17", "HALT", "DOC", None, None, None, None, "")
    assert (d17["round1_severity"], d17["blocks_publish"]) == ("LOAD_HALTED", True)
    assert R.finding("V-08", "ERROR", "SEC", None, None, None, None, "")["blocks_publish"] is True
    v06 = R.finding("V-06", "HALT", "SEC", None, None, None, None, "")
    assert (v06["round1_severity"], v06["blocks_publish"], v06["spec_severity"]) == ("warning", False, "HALT")


def test_spec_validation_status_follows_spec_1_2():
    def status(*severities):
        return spec_validation_status([{"spec_severity": s} for s in severities])
    assert status() == status("NOTE") == status(None) == "passed"
    assert status("NOTE", "WARN") == status("ERROR") == "needs_review"
    assert status("WARN", "HALT") == "failed"


# ---- guards ----

def test_structure_guards_fire_and_stay_silent():
    good = report_text("F4")
    assert structure_findings(good, read_report(good, "F4")) == []
    cases = {
        "G-S1": good.split("---\n", 2)[2],
        "G-S2": good.replace("## stg_gas_cascade\n", "## gas cascade\n"),
        "G-S3": good.split("## row_counts")[0],
    }
    for guard, text in cases.items():
        assert guard in {f["rule_id"] for f in structure_findings(text, read_report(text, "F4"))}, guard
    unparseable = good.replace('{"stg_pump_config": 0', '{"stg_pump_config": "zero"')
    assert [f["rule_id"] for f in structure_findings(unparseable, read_report(unparseable, "F4"))] == ["G-S3"]


def test_a_structurally_invalid_report_is_not_staged():
    with pytest.raises(ExtractionError) as err:
        stage(report_text("F4").split("## row_counts")[0], None, "WELL 1", "F4", {})
    assert err.value.code == "REPORT_INVALID"
    assert err.value.details["findings"][0]["round1_severity"] == "REPORT_INVALID"


def test_footer_and_label_guards(lookup):
    def guards(doc, reconciliation=()):
        return [f["rule_id"] for f in _guards(doc.report, list(reconciliation))]
    plain = make(lookup, "F5")
    row = {"block": "stg_pump_config", "footer_declared": 3, "objects_parsed": 2, "truncated_lines": 0,
           "duplicates_dropped": 0}
    assert guards(plain, [row]) == ["G-S4"]
    assert guards(plain, [{**row, "objects_parsed": 3}]) == []
    two = make(lookup, "F5", pc=[{"scenario_ordinal": "1", "scenario_label_staged": "Case #1"}],
               dc=[{"scenario_ordinal": "1", "scenario_label_staged": "Case #1 Target"}])
    one = make(lookup, "F5", pc=[{"scenario_ordinal": "1", "scenario_label_staged": "Case #1"}],
               dc=[{"scenario_ordinal": "1", "scenario_label_staged": "Case #1"}])
    assert guards(two) == ["G-L1"] and guards(one) == []


def test_known_issue_notices(lookup):
    def notices(family):
        return [n["rule_id"] for n in validate(make(lookup, family).report, family, lookup, [], [], [], [], [],
                                              detection=None, prep=None)["notices"] if n["kind"] == "known_issue"]
    assert (notices("F3"), notices("F5"), notices("F7"), notices("F4")) == (["G-K3"], ["G-K5"], ["G-K7"], [])


# ---- flag 2 (decision 2) ----

def test_provenance_json_is_not_a_d16_comparison():
    report = read_report(report_text("F1"), "F1")
    def check(rows):
        return T.d16_check(report, rows, T.scenario_keys(rows)[0], T.Quarantine())[2]
    assert check([{"scenario_ordinal": "1", "provenance_json": "{}"},
                  {"scenario_ordinal": "2", "provenance_json": '{"x": 1}'}]) == "pass"
    assert check([{"scenario_ordinal": "1", "perf_top_md_ft": "1"},
                  {"scenario_ordinal": "2", "perf_top_md_ft": "2"}]) == "D16-HALT"


# ---- unknown models (decisions 4, 5) ----

def test_unknown_model_classes():
    models = {"SF1750": True, "VC4300": False}
    rows = [{"pump_model_canonical": "SF1750", "pump_model_as_printed": "a"},
            {"pump_model_canonical": "VC4300", "pump_model_as_printed": "b"},
            {"pump_model_canonical": "XYZ1", "pump_model_as_printed": "c"},
            {"pump_model_canonical": None, "pump_model_as_printed": "d"}]
    assert [(u["model"], u["class"]) for u in unknown_models(rows, models)] == [
        ("VC4300", "no_head_curve"), ("XYZ1", "absent"), ("d", "unresolved")]


# ---- review signals ----

def reasons(records, field, table="stg_pump_config", row_key=1):
    return next(r for r in records if (r["field"], r["table"], r["row_key"]) == (field, table, row_key))["reasons"]


def test_grounding(lookup):
    doc = make(lookup, "F4", pages={2: "MODEL ELS-1234 STAGES 100", 3: "other"},
               pc=[{"pump_model_as_printed": "ELS-1234", "source_page": "p2"},
                   {"pump_model_as_printed": "ELS-1234", "source_page": "p3"},
                   {"pump_model_as_printed": "ELS-7777", "source_page": "p2"}])
    records = {r["row_key"]: r for r in review(doc, [], []) if r["field"] == "pump_model_as_printed"}
    assert (records[1]["grounding"], records[1]["needs_review"]) == ("cited_page", False)
    assert records[2]["grounding"] == "other_page" and records[2]["needs_review"]
    assert records[3]["grounding"] == "not_found" and records[3]["needs_review"]
    unchecked = make(lookup, "F4", pc=[{"pump_model_as_printed": "ELS-1234", "source_page": "p2"}])
    assert reasons(review(unchecked, [], []), "pump_model_as_printed") == []
    assert review(unchecked, [], [])[0]["grounding"] == "not_evaluated"


def test_rule_conflict_quarantine_signals(lookup):
    doc = make(lookup, "F4", pc=[{"pump_model_as_printed": "ELS-1234", "stages": "12x", "source_page": "p1",
                                  "extraction_status": "conflict"},
                                 {"pump_model_as_printed": "ELS-1234", "stages": "12", "source_page": "p1"}])
    finding = R.finding("V-06", "HALT", "SEC", "stg_pump_config", 1, "pump_model_as_printed", "ELS-1234", "")
    quarantine = [{"table": "pump_config", "column": "stages", "raw_value": "12x", "source_page": "p1",
                   "reason": "composite_value"}]
    records = review(doc, [finding], quarantine)
    assert "rule: V-06" in reasons(records, "pump_model_as_printed")
    assert any(r.startswith("conflict:") for r in reasons(records, "pump_model_as_printed"))
    assert not any(r.startswith("conflict") for r in reasons(records, "stages"))
    assert "quarantined: composite_value" in reasons(records, "stages")
    assert reasons(records, "stages", row_key=2) == []


def test_known_issue_and_frequency_trap_signals(lookup):
    f3 = make(lookup, "F3", pc=[{"pump_model_as_printed": "REDA 400 RC1000"}])
    f4 = make(lookup, "F4", pc=[{"pump_model_as_printed": "ELS-1234"}])
    assert "known_issue: F3" in reasons(review(f3, [], []), "pump_model_as_printed")
    assert reasons(review(f4, [], []), "pump_model_as_printed") == []
    def trap(flag):
        doc = make(lookup, "F1", dc=[{"scenario_ordinal": "1", "water_sg": "1.1"}],
                   co=[{"scenario_ordinal": "1", "scenario_title_frequency_disagrees": flag}])
        return reasons(review(doc, [], []), "water_sg", "stg_design_context")
    assert any(r.startswith("frequency_trap") for r in trap("true"))
    assert not any(r.startswith("frequency_trap") for r in trap("false"))


def test_expected_missing_signal(lookup):
    def missing(doc, field):
        return [r for r in review(doc, [], []) if r["field"] == field and r["row_key"] is None]
    assert supply("F4", "vendor_mixture_sg") == "expected"
    absent = missing(make(lookup, "F4", dc=[{"water_sg": "1.1"}]), "vendor_mixture_sg")
    assert absent and absent[0]["needs_review"] and absent[0]["reasons"][0].startswith("expected_missing: staged")
    assert missing(make(lookup, "F4", dc=[{"vendor_mixture_sg": "0.9"}]), "vendor_mixture_sg") == []
    assert supply("F1", "motor_power_factor") == "uncertain"
    unsure = missing(make(lookup, "F1", dc=[{"water_sg": "1.1"}]), "motor_power_factor")
    assert unsure and not unsure[0]["needs_review"] and "not_evaluated" in unsure[0]["reasons"][0]
    assert supply("F4", "motor_type") == "uncertain" and supply("F1", "vendor_mixture_sg") == "absent"
    assert missing(make(lookup, "F1", dc=[{"water_sg": "1.1"}]), "vendor_mixture_sg") == []


def test_review_fields_are_the_fixed_shape(lookup):
    record = review(make(lookup, "F4", pc=[{"stages": "100"}]), [], [])[0]
    assert list(record) == ["field", "table", "row_key", "value", "cited_page", "grounding", "reasons", "needs_review"]
    finding = R.finding("V-06", "HALT", "SEC", None, None, None, None, "")
    assert list(finding) == ["rule_id", "spec_severity", "round1_severity", "scope", "table", "row_key", "column",
                             "value", "message", "blocks_publish"]


# ---- 3b-3 changes ----

def test_v_f2_4_needs_both_page_conditions(lookup):
    def fires(page, alt="400UNB35H"):
        return bool(fired(make(lookup, "F2", pages={2: page}, pc=[{"pump_model_as_printed_alt": alt, "source_page": "p2"}]),
                          "V-F2-4"))
    assert fires("PUMP MSC_400UNB_35H_93 STG")                      # not verbatim, MSC_ form on the page
    assert not fires("PUMP 400UNB35H 93 STG")                       # not verbatim, no MSC_ form
    assert not fires("400UNB35H and PUMP MSC_400UNB_35H_93 STG")    # verbatim on the cited page
    assert not fires("PUMP MSC_400UNB_35H_93 STG", alt="PUMP MSC_400UNB_35H_93 STG")   # the MSC_ form itself


def test_v_f7_5_fires_on_a_mixture_basis(lookup):
    assert fired(make(lookup, "F7", dc=[{"gas_sg": "0.85", "gas_sg_basis": "mixture"}]), "V-F7-5")


def test_conflict_is_one_item_on_the_model_field(lookup):
    doc = make(lookup, "F2", pc=[{"pump_model_as_printed": "400UNB35H", "stages": "93", "extraction_status": "conflict"}])
    records = review(doc, [], [])
    assert reasons(records, "pump_model_as_printed") == ["conflict: extraction_status = conflict in staging"]
    assert reasons(records, "stages") == []
    assert next(r for r in records if r["field"] == "pump_model_as_printed")["needs_review"]


def test_conflict_is_benign_when_both_forms_are_one_model(lookup):
    def model_record(alt):
        doc = make(lookup, "F2", pc=[{"pump_model_as_printed": "400UNB35H", "pump_model_as_printed_alt": alt,
                                      "extraction_status": "conflict"}])
        return next(r for r in review(doc, [], []) if r["field"] == "pump_model_as_printed")
    benign = model_record("PUMP MSC_400UNB_35H_93 STG_X")
    assert benign["reasons"] == ["conflict_benign: both printed forms resolve to 400UNB35H"]
    assert benign["needs_review"] is False
    different = model_record("PUMP MSC_400UNB_43H_73 STG_X")
    assert different["reasons"] == ["conflict: extraction_status = conflict in staging"] and different["needs_review"]


def test_a_conflict_row_without_a_model_field_gets_one_item(lookup):
    doc = make(lookup, "F3", dc=[{"water_sg": "1.1", "oil_api_deg": "39", "extraction_status": "conflict"}])
    records = review(doc, [], [])
    items = [r for r in records if any(x.startswith("conflict") for x in r["reasons"])]
    assert [(r["field"], r["grounding"], r["needs_review"]) for r in items] == [("extraction_status", "not_evaluated", True)]


def test_metadata_is_never_grounded(lookup, monkeypatch):
    import extraction.validate.review as RV
    monkeypatch.setattr(RV, "SCOPE", (("stg_pump_config", ("pump_model_as_printed", "extraction_status", "source_page")),))
    doc = make(lookup, "F4", pages={2: "ELS-1234 extracted p2"},
               pc=[{"pump_model_as_printed": "ELS-1234", "extraction_status": "extracted", "source_page": "p2"}])
    grounding = {r["field"]: r["grounding"] for r in RV.review(doc, [], [])}
    assert grounding == {"pump_model_as_printed": "cited_page", "extraction_status": "not_evaluated",
                         "source_page": "not_evaluated"}


# ---- 3c: V-F3-5 narrowed to curve rows ----

def test_v_f3_5_fires_on_bep_ror_and_head_curve_rows_only(lookup):
    def fires(**row):
        return bool(fired(make(lookup, "F3", co=[{"head_as_printed": "100", **row}]), "V-F3-5"))
    assert fires(point_type="bep") and fires(point_type="ror_max") and fires(curve_observation_role="head_curve")
    assert not fires(point_type="design_point") and not fires(point_type="q_intake") and not fires()



# ---- the emptiness guards: an empty extraction, no pump rows, a halt the model declared ----

HALT_LINE = "> [!warning] **HALT — V-02 (required page absent).** Stopped at Step 1: the document prints `Page: 1 / 7`."
F4_PUMP = [{"section_order": "1", "pump_model_as_printed": "ELS-1234", "stages": "100"}]
F4_CONTEXT = [{"depth_reference_basis": "intake", "oil_api_deg": "40"}]


def with_prose(text, *lines):
    """The report with prose lines between its frontmatter and its data blocks."""
    head, rest = text.split("\n## stg_pump_config", 1)
    return head + "\n" + "\n".join(lines) + "\n\n## stg_pump_config" + rest


def checked(lookup, text, well_matched=True):
    return validate(read_report(text, "F4"), "F4", lookup, [], [], [], [], [], report_text=text,
                    well_matched=well_matched)


def notice(out):
    return next(n["message"] for n in out["notices"] if n["rule_id"] == "unknown_models")


def test_declared_halt_takes_rule_ids_from_a_halt_line_only():
    def halt(*lines, fm=None, after=""):
        text = with_prose(report_text("F4", fm), *lines) + after
        return declared_halt(text, read_report(text, "F4"))
    assert halt(HALT_LINE) == ["V-02"]
    assert halt("**HALT**: V-F2-5 and D17 both apply") == ["V-F2-5", "D17"]
    assert halt("- HALT (no rule named)") == [] and halt(fm={"validation_status": "failed"}) == []
    assert halt("- The document has 7 pages; this is not a HALT.", "**Emission count.** None, because of the V-02 HALT.",
                "HALT conditions: none") is None                        # mentions, not declarations
    assert halt(after="HALT — V-02, written after the data blocks\n") is None


def test_an_empty_extraction_is_g_e1_and_names_only_the_rule(lookup):
    out = checked(lookup, with_prose(report_text("F4"), HALT_LINE))
    empty = [f for f in out["findings"] if f["rule_id"] == "G-E1"]
    assert [(f["round1_severity"], f["blocks_publish"], f["value"]) for f in empty] == [("EXTRACTION_EMPTY", True, ["V-02"])]
    assert empty[0]["message"] == ("no pump-configuration and no design-context rows were extracted; "
                                   "the model declared a halt naming V-02")
    assert "1 / 7" not in json.dumps(out)                                 # nothing the document printed
    assert out["spec_validation_status"] == "failed"
    assert notice(out).endswith("(empty extraction; the model declared a halt naming V-02)")
    quiet = checked(lookup, report_text("F4"))
    assert [f["message"] for f in quiet["findings"] if f["rule_id"] == "G-E1"] == [
        "no pump-configuration and no design-context rows were extracted; the report declares no halt"]


def test_no_pump_rows_beside_other_rows_needs_review(lookup):
    out = checked(lookup, report_text("F4", dc=F4_CONTEXT))
    assert [f["rule_id"] for f in out["findings"] if f["rule_id"].startswith("G-E")] == ["G-E2"]
    assert out["spec_validation_status"] == "needs_review"
    assert spec_validation_status([f for f in out["findings"] if f["rule_id"] != "G-E2"]) == "passed"
    items = [r for r in out["review"] if r["reasons"] == ["no pump rows extracted"]]
    assert [(r["field"], r["table"], r["needs_review"]) for r in items] == [(None, "stg_pump_config", True)]


def test_a_declared_halt_that_left_rows_needs_review_and_names_the_rule(lookup):
    out = checked(lookup, with_prose(report_text("F4", pc=F4_PUMP, dc=F4_CONTEXT), HALT_LINE))
    assert [f["message"] for f in out["findings"] if f["rule_id"].startswith("G-E")] == ["model halt: V-02"]
    assert out["spec_validation_status"] == "needs_review"
    assert spec_validation_status([f for f in out["findings"] if f["rule_id"] != "G-E3"]) == "passed"
    assert [r["reasons"] for r in out["review"] if r["field"] is None] == [["model halt: V-02"]]


def test_the_no_pump_rows_notice_names_the_reason(lookup):
    def reason(text, well_matched=True):
        message = notice(checked(lookup, text, well_matched))
        return message[message.index("(") + 1:-1]
    rows = report_text("F4", pc=F4_PUMP, dc=F4_CONTEXT)
    assert reason(rows, well_matched=False) == "unmatched well"
    assert reason(report_text("F4")) == "empty extraction"
    assert reason(report_text("F4"), well_matched=False) == "unmatched well; empty extraction"
    assert reason(report_text("F4", dc=F4_CONTEXT)) == "no pump rows extracted"
    assert reason(rows) == "none of the report's pump rows reached the load"


def test_emptiness_guards_set_a_floor_on_the_status():
    def status(*rule_ids, severity=None):
        return spec_validation_status([{"spec_severity": severity, "rule_id": r} for r in rule_ids])
    assert status("G-E1") == "failed" and status("G-E2") == status("G-E3") == "needs_review"
    assert status("G-E2", severity="HALT") == "failed" and status("V-03") == "passed"
