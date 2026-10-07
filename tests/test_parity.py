"""The parity comparator, on synthetic reports. No corpus, no AWS."""

import json
import re
from types import SimpleNamespace

from extraction.assemble import ASSETS
from extraction.compare.parity import (
    PARITY_DOCS, TRAP_FIELDS, WORKSHEET_COLUMNS, Alignment, added_sample, classify, compare, cost_usd, model_prices, run_metrics,
    stg_blocks, worksheet,
)

PUMP = [{"section_order": "1", "pump_model_as_printed": "P-3000", "stages": "28", "scenario_ordinal": "1"},
        {"section_order": "2", "pump_model_as_printed": "P-1750", "stages": "490", "scenario_ordinal": "1"}]
CURVES = [
    {"pump_model_as_printed": "P-3000", "point_type": "design_point", "frequency_hz_as_printed": "59.00",
     "flow_as_printed": "1900.00", "curve_observation_role": "design", "envelope_status": "absent",
     "scenario_ordinal": "1"},
    {"head_as_printed": "9512.55", "head_basis_as_printed": "string_total", "point_type": "design_point",
     "observation_is_composite": "true", "curve_observation_role": "design", "envelope_status": "absent",
     "scenario_ordinal": "1"},
]
GAS = [{"stage_order": "1", "gas_rate_as_printed": "8.43", "gas_rate_unit_as_printed": "mcf/d"}]
CONTEXT = [{"scenario_ordinal": "1", "bubble_point_psi": "1800.00", "bubble_point_unit_as_printed": "psi"}]


def report(pump=PUMP, curves=CURVES, gas=GAS, context=CONTEXT, alias=()) -> str:
    def block(name, rows):
        return f"## {name}\n```jsonl\n" + "".join(json.dumps(r) + "\n" for r in rows) + "```\n"
    return ("---\nfamily: test\n---\n\n## Sections\nprose, ignored\n\n" + block("stg_pump_config", pump)
            + block("stg_design_context", context) + block("stg_curve_observations", curves)
            + block("stg_gas_cascade", gas) + block("stg_alias_evidence", alias) + "## row_counts\n```jsonl\n{}\n```\n")


def edited(rows, i, drop=(), **changes):
    rows = [dict(r) for r in rows]
    rows[i].update(changes)
    for k in drop:
        rows[i].pop(k)
    return rows


def test_blocks_parse_including_empty_and_bad_lines():
    blocks, errors = stg_blocks(report() + "## stg_extra\n```jsonl\nnot json\n```\n")
    assert [len(blocks[t]) for t in ("stg_pump_config", "stg_curve_observations", "stg_alias_evidence")] == [2, 2, 0]
    assert errors == ["stg_extra line 1: not a JSON object"]


def test_classification():
    assert classify(None, "x") == "added"
    assert classify("x", None) == "dropped"
    assert classify("59.00", "59.00 (Hz)") == "format"       # a unit on one side only
    assert classify("10300.00 (ft)", "10300 ft") == "format"  # same unit token
    assert classify("1800.00 (PSI)", "1800 psi") == "format"  # unit case
    assert classify("1,900", "1900") == "format"              # thousands separator
    assert classify("8.43 mcf/d", "8.43 %") == "conflict"     # a differing unit token
    assert classify("1800 psi", "1800 psig") == "conflict"
    assert classify("Vendor input", "vendor input") == "conflict"  # format is for numbers only
    assert classify("59.00", "60") == "conflict"
    assert classify("Case #1", "Case #1 Target") == "conflict"  # same number, different words
    assert classify("psi", "psig") == "conflict"
    assert classify("No", "false") == "conflict"


def test_alignment_maps_labels_through_the_reports_own_table():
    blocks = stg_blocks(report(
        pump=[dict(PUMP[0], scenario_label_staged="1000 pip"),
              dict(PUMP[1], scenario_ordinal="2", scenario_label_staged="500 pip")],
        curves=[{"scenario_label": "500 pip"}, {"scenario_label": "500 pip - Q-INT"}, {}]))[0]
    al = Alignment.of(blocks)
    assert al.labels == {"1000 pip": "1", "500 pip": "2"}
    assert al.scenario({"scenario_label": "500 pip"}) == ("2", "label")
    assert al.scenario({"scenario_ordinal": "1.0"}) == ("1", "ordinal")
    assert al.scenario({"scenario_label": "500 pip - Q-INT"}) == ("unaligned: 500 pip - Q-INT", "unaligned")
    # Two scenarios in the table: a row without a scenario key cannot be placed.
    assert al.scenario({}) == ("unaligned: (no scenario key)", "unaligned")


def test_a_row_without_scenario_keys_joins_the_only_scenario():
    old = report(curves=[{k: v for k, v in r.items() if k != "scenario_ordinal"} for r in CURVES])
    c = compare("F4", old, report())
    assert c.passed and not c.missing_curves
    assert c.alignment["old"].methods["sole scenario"] == 2
    assert [d.field for d in c.diffs if d.table == "stg_curve_observations"] == ["scenario_ordinal"] * 2
    assert {d.category for d in c.diffs} == {"added"}


def test_ambiguous_label_stays_unaligned():
    al = Alignment.of(stg_blocks(report(pump=[dict(PUMP[0], scenario_label_staged="Case #1"),
                                              dict(PUMP[1], scenario_ordinal="2", scenario_label_staged="Case #1")]))[0])
    assert al.ambiguous == {"Case #1": ["1", "2"]}
    assert al.scenario({"scenario_label": "Case #1"})[1] == "unaligned"


def test_identical_reports_pass():
    c = compare("F4", report(), report())
    assert c.passed and not c.diffs and not c.trap_diffs and not c.structural


def test_pump_config_fails_on_conflict_and_dropped_only():
    assert not compare("F4", report(), report(pump=edited(PUMP, 1, stages="491"))).pump_ok
    assert not compare("F4", report(), report(pump=edited(PUMP, 1, drop=("stages",)))).pump_ok
    c = compare("F4", report(), report(pump=edited(PUMP, 1, stages="490 stages", housing_count="1")))
    assert c.pump_ok and c.counts("stg_pump_config") == {"format": 1, "added": 1}
    c = compare("F4", report(), report(pump=PUMP[:1]))
    assert not c.pump_ok and c.counts("stg_pump_config") == {"dropped": 1}  # a whole row


def test_trap_categories():
    c = compare("F4", report(), report(gas=edited(GAS, 0, gas_rate_unit_as_printed="%")))
    assert not c.traps_ok and [(d.field, d.category) for d in c.trap_diffs] == [("gas_rate_unit_as_printed", "conflict")]
    c = compare("F4", report(), report(context=edited(CONTEXT, 0, bubble_point_psi="1,800.00 psi")))
    assert c.traps_ok  # bubble_point_psi is not an F4 trap; the unit field is
    c = compare("F4", report(), report(gas=edited(GAS, 0, bubble_point_provenance="vendor input")))
    assert c.traps_ok and c.trap_counts() == {"added": 1}


def test_curve_format_passes_and_conflict_fails():
    c = compare("F4", report(), report(curves=edited(CURVES, 0, flow_as_printed="1,900", frequency_hz_as_printed="59 Hz")))
    assert c.passed and c.counts("stg_curve_observations") == {"format": 2}
    c = compare("F4", report(), report(curves=edited(CURVES, 1, head_as_printed="9512.56")))
    assert not c.curves_ok and c.curve_conflicts == 1


def test_missing_curve_and_row_delta():
    c = compare("F4", report(), report(curves=CURVES[1:]))
    assert not c.curves_ok and c.curve_delta_pct == -50.0 and len(c.missing_curves) == 1


def test_per_section_against_composite_is_structural():
    composite = {"point_type": "design_point", "frequency_hz_as_printed": "59.00", "flow_as_printed": "1900.00",
                 "observation_is_composite": "true", "curve_observation_role": "design", "scenario_ordinal": "1"}
    new = [edited(CURVES, 0, drop=("flow_as_printed",))[0], CURVES[1], composite]
    c = compare("F4", report(), report(curves=new))
    assert [s[0] for s in c.structural] == ["per-section vs composite"]
    assert c.unmatched_new == [new[0]]  # the per-section row, now without its flow


def test_a_composite_rows_model_is_not_its_identity():
    old = edited(CURVES, 1, pump_model_as_printed="P-3000 STRING")
    c = compare("F4", report(curves=old), report())
    assert c.passed and not c.missing_curves
    assert [(d.field, d.category) for d in c.diffs] == [("pump_model_as_printed", "dropped")]


def test_a_key_field_added_in_new_is_an_added_field_not_a_missing_curve():
    old = edited(CURVES, 0, drop=("frequency_hz_as_printed",))
    c = compare("F4", report(curves=old), report())
    assert c.passed and not c.missing_curves and not c.extra_curves
    assert [(d.field, d.category) for d in c.diffs] == [("frequency_hz_as_printed", "added")]


def test_rows_that_disagree_on_a_printed_key_field_do_not_pair():
    new = edited(CURVES, 0, point_type="bep")
    c = compare("F4", report(), report(curves=new))
    # Listed on both sides as unmatched; the curve itself is present, so the curve bar holds.
    assert c.unmatched_old == [CURVES[0]] and c.unmatched_new == [new[0]] and not c.diffs
    assert c.curves_ok and not c.missing_curves


def test_split_rows_are_structural():
    head = {k: v for k, v in CURVES[1].items() if k != "head_as_printed"}
    new = [CURVES[0], dict(head, head_as_printed="9512.55"), dict(head, head_as_printed="4000.00")]
    c = compare("F4", report(), report(curves=new))
    assert [s[0] for s in c.structural] == ["split"] and not c.curves_ok  # +50 % rows


def test_metrics_and_cost():
    r = SimpleNamespace(output_tokens=3000, time_to_first_token_s=10.0, duration_s=70.0, input_tokens=40000,
                        reasoning_tokens=None, cache_read_input_tokens=None, cache_write_input_tokens=None,
                        time_to_first_text_s=12.0, server_latency_ms=69000, stop_reason="end_turn",
                        max_tokens=128000, reasoning_text="")
    m = run_metrics(r, report_bytes=9000)
    assert (m["output_tokens_per_s"], m["bytes_per_output_token"], m["max_tokens_share"]) == (50.0, 3.0, 0.0234)
    assert cost_usd(40000, 3000, {"input_usd_per_1k_tokens": None, "output_usd_per_1k_tokens": None}) is None
    assert cost_usd(1000, 1000, {"input_usd_per_1k_tokens": 0.5, "output_usd_per_1k_tokens": 2.0}) == 2.5


def test_prices_are_looked_up_per_model():
    pricing = json.loads((ASSETS.parent / "compare" / "pricing.json").read_text(encoding="utf-8"))
    assert model_prices(pricing, "us.anthropic.claude-opus-5-5")["output_usd_per_1k_tokens"] == 0.02
    assert cost_usd(1000, 1000, model_prices(pricing, "us.anthropic.claude-sonnet-5-5")) is None  # not filled in
    assert model_prices(pricing, "some.other-model") == {}


def test_trap_table_is_grounded_in_the_assets():
    m1 = (ASSETS / "m1-extraction-spec.md").read_text(encoding="utf-8")
    schemas = m1[m1.index("## Schemas"):] + (ASSETS / "staging-schemas.md").read_text(encoding="utf-8")
    assert sorted(f for f, _ in PARITY_DOCS) == sorted(TRAP_FIELDS)
    for fields in TRAP_FIELDS.values():
        for key, cite in fields:
            contract, trap = re.fullmatch(r"(\S+):§6 (T\d+).*", cite).groups()
            section = (ASSETS / contract).read_text(encoding="utf-8").split("## 6. Traps")[1].split("## 7.")[0]
            assert f"**{trap} " in section and key in section, cite
            assert re.search(rf"(^|`){key}\b", schemas, re.M), key


def test_worksheet_rows_order_and_blank_verdicts():
    composite = {"point_type": "design_point", "frequency_hz_as_printed": "59.00", "flow_as_printed": "1900.00",
                 "observation_is_composite": "true", "curve_observation_role": "design", "scenario_ordinal": "1"}
    curves = [edited(CURVES, 0, drop=("flow_as_printed",))[0], CURVES[1], composite]
    new = report(pump=edited(PUMP, 1, stages="491", source_page="p7"),
                 context=edited(CONTEXT, 0, drop=("bubble_point_unit_as_printed",)), curves=curves)
    old = report(pump=edited(PUMP, 1, source_page="p1"))
    rows = worksheet("doc A", compare("F4", old, new))
    assert all(list(r) == WORKSHEET_COLUMNS and r["verdict"] == r["note"] == "" for r in rows)
    assert [(r["table"], r["field"], r["category"], r["is_trap"]) for r in rows] == [
        ("stg_pump_config", "source_page", "conflict", "false"),
        ("stg_pump_config", "stages", "conflict", "false"),
        ("stg_design_context", "bubble_point_unit_as_printed", "dropped", "true"),   # the traps group
        ("stg_design_context", "bubble_point_unit_as_printed", "dropped", "true"),   # design_context group
        ("stg_curve_observations", "per-section vs composite", "structural", "false"),
    ]
    assert rows[1]["old_source_page"] == "p1" and rows[1]["new_source_page"] == "p7"
    assert rows[2]["row_key"] == "f4-els.md:§6 T3" and rows[3]["row_key"] == '["1", false]'


def test_added_sample_keeps_five_per_field():
    new = [dict(CURVES[1], head_as_printed=str(9000 + i), extraction_status="extracted") for i in range(7)]
    old = [{k: v for k, v in r.items() if k != "extraction_status"} for r in new]
    c = compare("F4", report(curves=old), report(curves=new))
    rows = added_sample("doc A", c)
    assert len(rows) == 5 and {r["field"] for r in rows} == {"extraction_status"}
    assert all(r["category"] == "added" for r in rows)


def test_missing_curves_are_worksheet_rows_with_their_old_pages():
    old = [dict(CURVES[0], source_page="p1"),
           {"scenario_label": "500 pip - Q-INT", "pump_model_as_printed": "P-3000", "point_type": "q_intake",
            "curve_observation_role": "single_pump_chart", "source_page": "p15"}]
    rows = [r for r in worksheet("doc A", compare("F4", report(curves=old), report(curves=CURVES[1:])))
            if r["category"] == "missing"]
    assert [(r["field"], r["old_source_page"], r["new_source_page"], r["note"]) for r in rows] == [
        ('["1", "design", "P-3000", 59.0, false]', "p1", "", ""),
        ('["unaligned: 500 pip - Q-INT", "single_pump_chart", "P-3000", null, false]', "p15", "",
         "unaligned scenario label: 500 pip - Q-INT"),
    ]
    assert all(r["row_key"] == r["field"] and r["verdict"] == "" for r in rows)


def test_normalizer_copy_applies_in_memory():
    from extraction.normalize import normalize_row
    row = {"pump_model_as_printed": "SF3550 TS4 XR HS Shaft))", "extraction_status": "extracted"}
    out = normalize_row(row)
    assert out == row | {"pump_model_canonical": "SF3550 TS4 XR (HS Shaft)"}  # the script's own example
    assert normalize_row({"pump_model_as_printed": "SF3550-SF4300-HFGS-400hp 420MTR"})["model_normalization_flag"] == "composite"
    assert "pump_model_canonical" not in row  # the input row is untouched
