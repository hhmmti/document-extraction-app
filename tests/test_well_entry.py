"""The well entry, on synthetic reports staged in-process: values, units and notes, starting statuses, the pump
string and its head sum.  Two optional groups:
  * ESP_RUNS_DIR, a folder holding stored runs: three stored reports, found by the SHA-256 of their text,
    check the head sum and V-08 on real documents (no file name or well is kept here);
  * ESP_WELL_PARAMETERS_PY, the monorepo's esp_resources_v2/well_parameters.py (#1218): the built maps go
    through the product's own validator.  Nothing of it is copied here.
Staging reads the assets: without ESP_EXTRACTION_ASSETS_DIR everything skips.  No model, no AWS."""

import hashlib
import importlib.util
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

import extraction.run as R
from extraction.assets_dir import ASSETS_ENV
from extraction.stage import production_evidence, stage
from extraction.stage.generic import GENERIC
from extraction.well_entry import ENTRY, KEYS, build_entry, printed_number
from test_validate import report_text

pytestmark = pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"staging reads the assets; set {ASSETS_ENV}")

PUMP = {"section_order": "1", "section_role": "primary", "pump_model_as_printed": "ELS-1750", "stages": "490",
        "housing_count": "1", "source_page": "p2"}                   # ELS1750: in the catalogue
CONTEXT = {"depth_reference_basis": "intake", "oil_api_deg": "40"}


def build(family="F4", well="W", fm=None, **blocks):
    if family == GENERIC:
        fm, family_code = {"vendor_family": GENERIC, **(fm or {})}, "F4"
    else:
        family_code = family
    text = report_text(family_code, fm, **blocks)
    return build_entry(stage(text, "org-1", well, family, production_evidence(text)), text, family, "org-1", well)


def value(entry, key):
    return next(v for v in entry["review"]["values"] if v["key"] == key)


def maps(entry):
    return {**entry["pump_parameters"], **entry["wellbore_parameters"]}


# ---- pressures ----

def test_psig_is_converted_to_psia_with_the_printed_text_in_the_note():
    e = build(pc=[PUMP], dc=[{**CONTEXT, "bubble_point_psi": "1500 psig", "bubble_point_unit_as_printed": "psig"}])
    assert maps(e)["bubble_point_psia"] == {"value": 1514.7, "unit": "psia", "source": "design_document",
                                            "note": 'printed "1500 psig"; +14.7'}
    assert not {"pressure_basis_unstated", "pressure_unit_not_printed"} & set(value(e, "bubble_point_psia")["reasons"])


def test_bare_psi_is_kept_as_printed_and_checked():
    e = build(pc=[PUMP], dc=[{**CONTEXT, "bubble_point_psi": "1800 psi", "bubble_point_unit_as_printed": "psi"}])
    v = value(e, "bubble_point_psia")
    assert (v["value"], v["unit"], v["status"]) == (1800.0, "psia", "to check")
    assert "pressure_basis_unstated" in v["reasons"]
    assert maps(e)["bubble_point_psia"]["note"] == 'printed "1800 psi"; basis unstated'


def test_a_pressure_with_no_printed_unit_is_checked_and_a_unit_column_counts_as_printed():
    e = build(pc=[PUMP], dc=[{**CONTEXT, "static_datum_pressure_psi": "3,000", "datum_depth_ft": "10,400 ft",
                              "bubble_point_psi": "3,000", "bubble_point_unit_as_printed": "PSIA"}])
    reservoir, bubble = value(e, "reservoir_pressure_psia"), value(e, "bubble_point_psia")
    assert (reservoir["value"], reservoir["status"]) == (3000.0, "to check")
    assert "pressure_unit_not_printed" in reservoir["reasons"]
    assert maps(e)["reservoir_pressure_psia"]["note"] == 'printed "3,000"; no unit printed; datum 10,400 ft'
    assert bubble["value"] == 3000.0 and not {"pressure_basis_unstated", "pressure_unit_not_printed"} & set(bubble["reasons"])
    assert maps(e)["bubble_point_psia"]["note"] == 'printed "3,000" with unit "PSIA"'


# ---- scenarios and depths ----

def test_scenarios_that_disagree_propose_scenario_1_and_list_every_value():
    rows = [{**CONTEXT, "scenario_ordinal": s, "scenario_label_staged": label, "bubble_point_psi": bp,
             "bubble_point_unit_as_printed": "PSIA"} for s, label, bp in (("1", "1000 pip", "1,800 PSIA"),
                                                                          ("2", "500 pip", "1,200 PSIA"))]
    v = value(build(pc=[PUMP], dc=rows), "bubble_point_psia")
    assert (v["value"], v["scenario"], v["status"]) == (1800.0, "1000 pip", "to check")
    assert "varies_by_scenario" in v["reasons"]
    assert [(s["scenario"], s["label"], s["printed"], s["value"]) for s in v["scenarios"]] == [
        ("1", "1000 pip", "1,800 PSIA", 1800.0), ("2", "500 pip", "1,200 PSIA", 1200.0)]


def test_the_intake_depth_wins_and_the_pump_setting_depth_is_listed_beside_it():
    e = build(pc=[PUMP], dc=[{**CONTEXT, "intake_set_depth_md_ft": "3820.27 ft", "pump_setting_md_ft": "3905 ft"}])
    v = value(e, "setting_depth_md_ft")
    assert v["value"] == 3820.27 and "depth_basis_pump_setting" not in v["reasons"]
    assert [(b["column"], b["printed"]) for b in v["beside"]] == [("pump_setting_md_ft", "3905 ft")]
    assert maps(e)["setting_depth_md_ft"]["note"] == "intake; pump setting 3905 ft; depth_reference_basis intake"


def test_a_pump_setting_depth_alone_is_used_and_checked_and_md_never_fills_tvd():
    e = build(pc=[PUMP], dc=[{**CONTEXT, "depth_reference_basis": "pump_setting", "pump_setting_md_ft": "10300.00 (ft)"}])
    v = value(e, "setting_depth_md_ft")
    assert (v["value"], v["status"]) == (10300.0, "to check") and "depth_basis_pump_setting" in v["reasons"]
    assert maps(e)["setting_depth_md_ft"]["note"] == "pump setting, not intake; depth_reference_basis pump_setting"
    assert value(e, "setting_depth_tvd_ft")["value"] is None and "setting_depth_tvd_ft" not in maps(e)


def test_oil_sg_is_derived_from_api_only_when_no_sg_is_printed():
    assert maps(build(pc=[PUMP], dc=[CONTEXT]))["oil_sg"] == {
        "value": 0.8251, "unit": "sg", "source": "design_document", "note": "from API 40: 141.5 / (131.5 + API)"}
    assert maps(build(pc=[PUMP], dc=[{**CONTEXT, "oil_sg": "0.82029"}]))["oil_sg"]["value"] == 0.82029


# ---- empty keys and the generic route ----

def test_empty_keys_are_left_out_checked_only_where_the_family_prints_them():
    e = build(pc=[PUMP], dc=[CONTEXT])
    bubble, gor, vapour = (value(e, k) for k in ("bubble_point_psia", "solution_gor_scfstb", "vapor_pressure_psia"))
    assert bubble["value"] is None and bubble["status"] == "to check"                     # every ELS report prints one
    assert any(r.startswith("expected_missing") for r in bubble["reasons"])
    assert (gor["value"], gor["status"], vapour["status"]) == (None, "settled", "settled")  # ELS prints no GOR
    assert not {"bubble_point_psia", "solution_gor_scfstb", "vapor_pressure_psia", "design_document_ref",
                "pump_id"} & set(maps(e))
    assert [v["key"] for v in e["review"]["values"]] == list(KEYS)


def test_on_the_generic_route_every_document_value_is_checked_empty_ones_included():
    e = build(GENERIC, pc=[PUMP], dc=[CONTEXT])
    for v in e["review"]["values"]:
        if v["key"] in ("design_document_ref", "vapor_pressure_psia"):
            assert v["status"] == "settled"
        else:
            assert v["status"] in ("to check", "blocks publish") and "generic_path" in v["reasons"], v["key"]


def test_no_entry_without_a_well_or_for_an_empty_extraction():
    assert build(pc=[PUMP], dc=[CONTEXT], well=None) is None
    assert build() is None                                                # no rows at all: G-E1, EXTRACTION_EMPTY


# ---- the pump string ----

def test_a_whole_string_is_written_and_settled():
    e = build(pc=[PUMP], dc=[CONTEXT])
    assert maps(e)["pump_sections"] == {"value": [{"section_order": 1, "pump_model": "ELS1750", "stages": 490,
                                                   "section_role": "primary"}], "unit": "none",
                                        "source": "design_document", "note": "section order as printed, 1 = deepest"}
    string = e["review"]["pump_string"]
    assert string["status"] == "settled" and string["sections"][0]["match"] == "exact"
    assert {c["check"]: c["result"] for c in string["checks"]}["head sum"] == "not run"


def test_a_model_the_catalogue_lacks_keeps_the_string_out_of_the_map():
    e = build(pc=[{**PUMP, "pump_model_as_printed": "ELS-1234"}], dc=[CONTEXT])
    string = e["review"]["pump_string"]
    assert "pump_sections" not in maps(e) and string["status"] == "to check"
    assert string["sections"][0]["reasons"][0] == "model_absent: ELS1234"
    assert {c["check"]: c["result"] for c in string["checks"]}["models"] == "fail"


def test_a_section_value_that_needs_review_makes_the_string_to_check():
    e = build(pc=[{**PUMP, "housing_count": "two"}], dc=[CONTEXT])          # quarantined: not a number
    string = e["review"]["pump_string"]
    assert string["status"] == "to check" and string["reasons"] == ["review: position 1: quarantined: not_numeric"]
    section = string["sections"][0]
    assert (section["status"], section["reasons"]) == ("to check", ["quarantined: not_numeric"]) and "pump_sections" in maps(e)


def test_an_informational_item_alone_leaves_the_string_settled():
    text = report_text("F4", pc=[{**PUMP, "pump_model_as_printed_alt": "els-1750", "extraction_status": "conflict"}],
                       dc=[CONTEXT])
    result = stage(text, "org-1", "W", "F4", production_evidence(text))
    [item] = [r for r in result.review if r["table"] == "stg_pump_config" and r["field"] == "pump_model_as_printed"]
    assert item["reasons"] == ["conflict_benign: both printed forms resolve to ELS1750"] and not item["needs_review"]
    string = build_entry(result, text, "F4", "org-1", "W")["review"]["pump_string"]
    assert string["status"] == "settled" and {c["check"]: c["result"] for c in string["checks"]}["review"] == "pass"


def test_a_section_with_only_an_informational_item_stays_settled():
    e = build(pc=[{**PUMP, "pump_model_as_printed_alt": "els-1750", "extraction_status": "conflict"}], dc=[CONTEXT])
    [section] = e["review"]["pump_string"]["sections"]                      # conflict_benign: informational
    assert (section["status"], section["reasons"], section["review_reasons"]) == ("settled", [], [])


def test_v08_blocks_publish():
    e = build(pc=[PUMP, {**PUMP, "section_order": "3", "pump_model_as_printed": "ELS-3000", "stages": "28"}], dc=[CONTEXT])
    assert e["review"]["pump_string"]["status"] == "blocks publish" and "pump_sections" not in maps(e)


HEADS = [{"point_type": "design_point", "pump_model_as_printed": "ELS-3000", "head_as_printed": "1019.95 ft",
          "head_unit_as_printed": "ft", "head_basis_as_printed": "per_section_total", "source_page": "p5"},
         {"point_type": "design_point", "head_as_printed": "7247.97 ft", "head_unit_as_printed": "ft",
          "head_basis_as_printed": "string_total", "observation_is_composite": "true", "source_page": "p3, p4"}]
TWO = [{**PUMP, "pump_model_as_printed": "ELS-3000", "stages": "43"}, {**PUMP, "section_order": "2", "stages": "279"}]


@pytest.mark.parametrize("tdh,result", [("8267.92 ft", "pass"), ("8268 ft", "fail")])
def test_the_head_sum(tdh, result):
    e = build(pc=TWO, dc=[{**CONTEXT, "vendor_tdh_at_design_ft": tdh}], co=HEADS)
    heads = next(c for c in e["review"]["pump_string"]["checks"] if c["check"] == "head sum")
    assert heads["result"] == result, heads["why"]


def test_half_the_last_printed_digit():
    assert printed_number("7247.97 ft") == (Decimal("7247.97"), Decimal("0.005"))
    assert printed_number("2,275") == (Decimal("2275"), Decimal("0.5"))


def test_a_run_stores_its_entry_and_no_path(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(report_text("F4", pc=[PUMP], dc=[CONTEXT]), encoding="utf-8")
    record = R.run(organization_id="org-1", well_id="WELL 1", report=report, store_root=tmp_path / "store")
    text = (Path(record["store_dir"]) / ENTRY).read_text(encoding="utf-8")
    assert record["status"] == "succeeded" and ENTRY in record["store"]["files"]
    assert str(tmp_path) not in text and json.loads(text)["well_id"] == "WELL 1"


# ---- three stored reports, found by the SHA-256 of their text under ESP_RUNS_DIR ----

RUNS = os.environ.get("ESP_RUNS_DIR", "")
needs_runs = pytest.mark.skipif(not RUNS, reason="stored-report cases need ESP_RUNS_DIR")
STORED = {"f2_two_sections": ("728de9e5df8b4e4cac05b14d6ffccd41f4ccff87384c5ce478e254430cecddb5", "F2"),
          "f1_three_sections": ("4b8249ad06c5ee88b00c2b5ebb09ae3bd2edf3365e996662af8ed4a0709263bc", "F1"),
          "generic_no_order": ("186ba26a29fda27039e56d0b659fbaba500286c4b771b4a6cafb5d1627077599", GENERIC)}


def stored(name):
    sha, family = STORED[name]
    for path in Path(RUNS).rglob("report.md"):
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() == sha:
            text = data.decode("utf-8", errors="replace")
            return build_entry(stage(text, "org-1", "W", family, production_evidence(text)), text, family, "org-1", "W")
    pytest.skip(f"no report with SHA-256 {sha[:12]} under ESP_RUNS_DIR")


@needs_runs
def test_stored_f2_head_sum_is_exact():
    [scenario] = stored("f2_two_sections")["review"]["pump_string"]["head_sum"]
    assert (scenario["result"], scenario["sum"], scenario["tdh"], scenario["diff"]) == ("pass", "8267.92", "8267.92", "0.00")


@needs_runs
def test_stored_f1_head_sum_passes_within_rounding():
    scenarios = stored("f1_three_sections")["review"]["pump_string"]["head_sum"]
    assert [(s["result"], s["diff"], s["tolerance"]) for s in scenarios] == [("pass", "-0.67", "1.5"), ("pass", "-0.54", "1.5")]


@needs_runs
def test_stored_generic_string_without_section_order_blocks_publish():
    string = stored("generic_no_order")["review"]["pump_string"]
    assert string["status"] == "blocks publish"
    assert {c["check"]: c["result"] for c in string["checks"]}["V-08"] == "fail"


# ---- the product's own validator (#1218), by path ----

PRODUCT = os.environ.get("ESP_WELL_PARAMETERS_PY", "")
needs_product = pytest.mark.skipif(not PRODUCT, reason="#1218's validator needs ESP_WELL_PARAMETERS_PY")


def product():
    spec = importlib.util.spec_from_file_location("product_well_parameters", PRODUCT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_product
def test_every_key_has_the_map_and_unit_the_product_records():
    wp = product()
    assert {k: (wp.FIELDS_BY_KEY[k].field, wp.FIELDS_BY_KEY[k].unit) for k in KEYS} == KEYS


@needs_product
def test_built_maps_pass_the_product_validator():
    wp = product()
    entries = [build(pc=[PUMP], dc=[{**CONTEXT, "bubble_point_psi": "1500 psig", "bubble_point_unit_as_printed": "psig",
                                     "static_datum_pressure_psi": "1800 psi", "pump_setting_vd_ft": "9100 ft",
                                     "motor_type": "induction", "motor_power_factor": "85 %", "reservoir_temp_f": "212 °F",
                                     "solution_gor_rso_scf_stb": "650 scf/stb", "perf_top_md_ft": "9450 ft"}]),
               build(GENERIC, pc=TWO, dc=[{**CONTEXT, "vendor_tdh_at_design_ft": "8267.92 ft"}], co=HEADS)]
    for entry in entries:
        assert entry["pump_parameters"] and entry["wellbore_parameters"]
        for field in ("pump_parameters", "wellbore_parameters"):
            wp.validate_parameter_map(entry[field], field)
