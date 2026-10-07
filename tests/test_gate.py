"""The gate re-check, and the --effort flag of the parity run. No corpus, no AWS."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from extraction.bedrock import ExtractResult
from extraction.compare.gate import read_worksheet, recheck, selected
from extraction.compare.parity import stg_blocks
from test_unit import F4_P1, F4_P2, make_pdf

LAB = Path(__file__).resolve().parents[1]


def blocks(**tables) -> dict:
    text = "".join(f"## {t}\n```jsonl\n" + "".join(json.dumps(r) + "\n" for r in rows) + "```\n"
                   for t, rows in tables.items())
    return stg_blocks(text)[0]


PUMP = [{"scenario_ordinal": "1", "section_order": "1", "manufacturer": "SLB/REDA", "source_page": "p1"}]
CTX = [{"scenario_ordinal": "1", "scenario_label_staged": "Case #1 Target"},
       {"scenario_ordinal": "2", "scenario_label_staged": "Case #2"}]


def item(**kw) -> dict:
    row = {"doc": "doc A", "family": "F3", "table": "stg_pump_config", "row_key": '["1", 1.0, false]',
           "field": "manufacturer", "category": "conflict", "old_value": "SLB/REDA", "new_value": "SLB",
           "is_trap": "FALSE", "verdict": "old right", "note": ""}
    return row | kw


def test_worksheet_cells_are_decoded_one_by_one(tmp_path):
    path = tmp_path / "adjudication.csv"
    path.write_bytes("doc,row_key,note,\r\n".encode() + "A,f5-baker.md:§6 T4,".encode("utf-8")
                     + "F3 §3: p1 ≠ p6".encode("mac_roman") + b",\r\n")
    header, rows = read_worksheet(path)
    assert header == ["doc", "row_key", "note"]
    assert rows == [{"doc": "A", "row_key": "f5-baker.md:§6 T4", "note": "F3 §3: p1 ≠ p6"}]


def test_selection():
    rows = [item(), item(family="F2"), item(verdict="new right"), item(verdict="old right "),
            item(table="stg_design_context"), item(table="stg_design_context", is_trap="TRUE"),
            item(verdict="Both Wrong"), item(family="F7", table="stg_gas_cascade", is_trap="TRUE")]
    assert len(selected(rows)) == 5


def test_row_keyed_statuses():
    assert recheck(item(), blocks(stg_pump_config=PUMP)) == ("SLB/REDA", "matches_old")
    assert recheck(item(), blocks(stg_pump_config=[PUMP[0] | {"manufacturer": "SLB"}]))[1] == "matches_medium"
    assert recheck(item(), blocks(stg_pump_config=[PUMP[0] | {"manufacturer": "Schlumberger"}]))[1] == "other"
    assert recheck(item(), blocks(stg_pump_config=[{"scenario_ordinal": "1", "section_order": "2"}])) == ("", "absent")
    assert recheck(item(verdict="both wrong"), blocks(stg_pump_config=PUMP))[1] == "needs_review"
    assert recheck(item(), None) == ("", "absent")


def test_dropped_whole_row_comes_back():
    row = item(field="(row)", category="dropped", old_value=json.dumps(PUMP[0], sort_keys=True), new_value="")
    assert recheck(row, blocks(stg_pump_config=PUMP))[1] == "matches_old"


def test_a_found_value_is_never_absent():
    # The old report printed the gradient twice; the later one prints it once. A value is
    # present, so the item is judged on that value, never `absent`.
    trap = item(table="stg_design_context", row_key="f3-slb.md:§6 T3", field="vendor_mixture_gradient_psi_per_ft",
                category="dropped", old_value="0.433 psi/ft", new_value="", is_trap="TRUE")
    assert recheck(trap, blocks(stg_design_context=[{"vendor_mixture_gradient_psi_per_ft": "0.433 psi/ft"}])) \
        == ("0.433 psi/ft", "matches_old")
    assert recheck(trap, blocks(stg_design_context=[{"vendor_mixture_gradient_psi_per_ft": "0.407"}]))[1] == "other"


def test_matches_old_compares_the_whole_value():
    trap = item(family="F5", table="stg_design_context", row_key="f5-baker.md:§6 T4",
                field="scenario_label_staged", old_value="Case #1 Target", new_value="Case #1", is_trap="TRUE")
    assert recheck(trap, blocks(stg_design_context=CTX)) == ("Case #1 Target; Case #2", "other")
    assert recheck(trap, blocks(stg_design_context=[CTX[0]])) == ("Case #1 Target", "matches_old")
    assert recheck(trap, blocks(stg_design_context=[CTX[0] | {"scenario_label_staged": "Case #1"}]))[1] == "matches_medium"


def test_effort_flag_reaches_extract_and_run_json(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("parity_run", LAB / "scripts" / "parity_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    def fake_extract(prompt, **kw):
        seen.update(kw)
        return ExtractResult(report="---\n## row_counts\n", reasoning_text="", stop_reason="end_turn",
                             input_tokens=1, output_tokens=1, reasoning_tokens=None, cache_read_input_tokens=None,
                             cache_write_input_tokens=None, time_to_first_token_s=0.1, time_to_first_text_s=0.1,
                             duration_s=0.2, server_latency_ms=None, model_id=kw["model_id"], effort=kw["effort"],
                             max_tokens=kw["max_tokens"], prompt_sha256="x", trailing_newlines_stripped=1,
                             preamble_stripped_chars=0)

    monkeypatch.setattr(mod, "extract", fake_extract)
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(make_pdf([F4_P1, F4_P2]))
    args = SimpleNamespace(force=False, model_id="m", max_tokens=1000, budget_s=10, effort="high", clarifications=False)
    assert mod.run_one(pdf, tmp_path / "out", args, "000") == "completed"
    assert seen["effort"] == "high"
    assert json.loads((tmp_path / "out" / "synthetic" / "run.json").read_text())["effort"] == "high"


def test_conflict_review_quotes_reasons_and_alias_strings():
    spec = importlib.util.spec_from_file_location("conflict_review", LAB / "scripts" / "conflict_review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = ("## Extraction notes\n\n- **V-F1-1:** all pump_config rows are set to `conflict`.\n"
            "  | SF1750 | `SF1750 TS4 XR (HS Shaft)` | `SF1750 TS4 XR` |\n- Section role rule conflict: unrelated.\n\n"
            "## stg_pump_config\n```jsonl\n"
            + json.dumps({"scenario_ordinal": "1", "section_order": "1", "pump_model_as_printed": "SF1750 TS4 XR (HS Shaft)",
                          "extraction_status": "conflict"}) + "\n"
            + json.dumps({"scenario_ordinal": "1", "section_order": "2", "pump_model_as_printed": "SF2700 TS4 XR",
                          "extraction_status": "extracted"}) + "\n```\n"
            "## stg_alias_evidence\n```jsonl\n"
            + json.dumps({"model_as_printed": "SF1750 TS4 XR", "evidence_kind": "pumps_page", "source_page": "p13"}) + "\n```\n")
    rows = mod.review(text)
    assert len(rows) == 1 and rows[0]["row_key"] == '["1", 1.0, false]'
    assert rows[0]["stated_reason"].startswith("L3: - **V-F1-1:**") and "L4: | SF1750" in rows[0]["stated_reason"]
    assert "unrelated" not in rows[0]["stated_reason"]
    assert rows[0]["alias_evidence"] == "`SF1750 TS4 XR` (pumps_page, p13)"
    assert rows[0]["verdict"] == rows[0]["note"] == ""


def test_clarifications_flag_is_recorded_with_draft_hashes(tmp_path, monkeypatch):
    import hashlib
    spec = importlib.util.spec_from_file_location("parity_run", LAB / "scripts" / "parity_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    prompts = []

    def fake_extract(prompt, **kw):
        prompts.append(prompt)
        return ExtractResult(report="---\n## row_counts\n", reasoning_text="", stop_reason="end_turn",
                             input_tokens=1, output_tokens=1, reasoning_tokens=None, cache_read_input_tokens=None,
                             cache_write_input_tokens=None, time_to_first_token_s=0.1, time_to_first_text_s=0.1,
                             duration_s=0.2, server_latency_ms=None, model_id=kw["model_id"], effort=kw["effort"],
                             max_tokens=kw["max_tokens"], prompt_sha256="x", trailing_newlines_stripped=1,
                             preamble_stripped_chars=0)

    monkeypatch.setattr(mod, "extract", fake_extract)
    pdf = tmp_path / "f5.pdf"   # a minimal F5 document: its signature and every required page role
    pdf.write_bytes(make_pdf([["ProLift Summary Report", "Surface Electrical 480 V"],
                              ["ProLift Detailed Report", "Parameter Case 1", "String diagram", "Motor"]]))
    args = SimpleNamespace(force=False, model_id="m", max_tokens=1000, budget_s=10, effort="medium", clarifications=True)
    assert mod.run_one(pdf, tmp_path / "out", args, "000") == "completed"
    run = json.loads((tmp_path / "out" / "f5" / "run.json").read_text())
    draft = LAB / "extraction" / "assets" / "amendments" / "f5.md"
    assert run["family"] == "F5" and run["clarifications"] is True
    assert run["clarification_sha256"] == {"f5.md": hashlib.sha256(draft.read_bytes()).hexdigest()}
    assert prompts[0].count("# ===== CONTRACT CLARIFICATIONS: f5-baker.md =====") == 1
