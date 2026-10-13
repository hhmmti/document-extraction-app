"""The run store, the run record and the command-line tool, on synthetic documents.
No corpus, no model: Extract is replaced in-process, and each forced failure is injected
the same way.  Needs ESP_EXTRACTION_ASSETS_DIR (staging and prompt assembly read assets)."""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import extraction.run as R
from extraction import ExtractionError
from extraction.__main__ import main
from extraction.assets_dir import ASSETS_ENV
from extraction.bedrock import ExtractResult
from test_unit import F4_P1, F4_P2, make_pdf
from test_validate import report_text

pytestmark = pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"runs read the assets; set {ASSETS_ENV}")

GOOD = report_text("F4", pc=[{"section_order": "1", "pump_model_as_printed": "ELS-1234", "stages": "100"}],
                   dc=[{"depth_reference_basis": "intake", "oil_api_deg": "40"}])
HALTING = report_text("F4", pc=[{"scenario_ordinal": "1", "section_order": "1", "pump_model_as_printed": "ELS-1234"},
                                {"scenario_ordinal": "2", "section_order": "1", "pump_model_as_printed": "ELS-5678"}])
INVALID = GOOD.split("## row_counts")[0]


@pytest.fixture
def files(tmp_path):
    pdf, report = tmp_path / "doc.pdf", tmp_path / "report.md"
    pdf.write_bytes(make_pdf([F4_P1, F4_P2]))
    report.write_text(GOOD, encoding="utf-8")
    return pdf, report, tmp_path / "store"


def fake_extract(text=GOOD):
    def extract(prompt, **kw):
        return ExtractResult(report=text, reasoning_text="", stop_reason="end_turn", input_tokens=40_000,
                             output_tokens=15_000, reasoning_tokens=None, cache_read_input_tokens=None,
                             cache_write_input_tokens=None, time_to_first_token_s=1.0, time_to_first_text_s=1.0,
                             duration_s=2.0, server_latency_ms=None, model_id=kw["model_id"], effort=kw["effort"],
                             max_tokens=kw["max_tokens"], prompt_sha256="x", trailing_newlines_stripped=0,
                             preamble_stripped_chars=0)
    return extract


def stored(record):
    return sorted(p.name for p in (R.Path(record["store_dir"])).iterdir())


def on_disk(record):
    return json.loads((R.Path(record["store_dir"]) / "run.json").read_text())


# ---- the two modes ----

def test_report_mode_succeeds_and_stores_everything(files):
    pdf, report, root = files
    record = R.run(organization_id="org-1", well_id="WELL 1", report=report, pdf=pdf, store_root=root)
    assert record["status"] == "succeeded" and record["error"] is None and record["model"] is None
    assert stored(record) == sorted(["run.json", "source.json", "prep_manifest.csv", "report.md",
                                     *(f"{g}.parquet" for g in R.GRAINS), "quarantine.jsonl",
                                     *(f"{n}.jsonl" for n in R.LISTS + R.LOGS)])
    disk = on_disk(record)
    assert disk["status"] == "succeeded" and disk["rows"]["pump_config"] == 1 and disk["family"] == "F4"
    assert disk["source"]["pdf"]["sha256"] == R.sha256(pdf.read_bytes())
    assert set(disk["hashes"]["stage_assets"]) == {R.SEED, R.EVIDENCE, R.CATALOGUE}


def test_report_mode_without_the_pdf_is_not_evaluated_where_it_needs_it(files):
    _, report, root = files
    record = R.run(organization_id="org-1", well_id="WELL 1", report=report, store_root=root)
    assert record["status"] == "succeeded" and record["family_source"] == "report_frontmatter"
    notices = [json.loads(line) for line in (R.Path(record["store_dir"]) / "notices.jsonl").read_text().splitlines()]
    assert "review.grounding" in {n["rule_id"] for n in notices}


def test_pdf_mode_runs_the_chain_and_records_the_model(files, monkeypatch):
    pdf, _, root = files
    monkeypatch.setattr(R, "extract", fake_extract())
    record = R.run(organization_id="org-1", well_id="WELL 1", pdf=pdf, store_root=root)
    assert record["status"] == "succeeded" and record["mode"] == "pdf"
    assert record["tokens"] == {"input": 40_000, "output": 15_000}
    assert record["model"]["model_id"] == R.MODEL_ID and record["hashes"]["prompt_sha256"]
    assert record["cost_usd"] == pytest.approx(40 * 0.004 + 15 * 0.02)
    assert set(record["stages_s"]) >= {"detect", "prep", "assemble", "extract", "stage", "store", "record"}


def test_run_id_is_sortable_and_unique():
    now = datetime(2026, 10, 13, 18, 0, 0, tzinfo=timezone.utc)
    a, b = R.new_run_id(now), R.new_run_id(now)
    assert re.fullmatch(r"20261013T180000Z-[0-9a-f]{8}", a) and a != b


def test_no_temporary_file_is_left_behind(files):
    _, report, root = files
    record = R.run(organization_id="org-1", well_id="WELL 1", report=report, store_root=root)
    assert not [n for n in stored(record) if n.startswith(".")]


# ---- forced failures: each run stores what it has, with the right code ----

def fail(code):
    def raiser(*a, **kw):
        raise ExtractionError(code, "forced by a test", {})
    return raiser


@pytest.mark.parametrize("target,code,stage,kept", [
    ("detect", "UNKNOWN_TEMPLATE", "detect", ["run.json", "source.json"]),
    ("prep", "PREP_FAILED", "prep", ["run.json", "source.json"]),
    ("extract", "MODEL_TIMEOUT", "extract", ["prep_manifest.csv", "run.json", "source.json"]),
])
def test_failure_before_staging(files, monkeypatch, target, code, stage, kept):
    pdf, _, root = files
    monkeypatch.setattr(R, target, fail(code))
    record = R.run(organization_id="org-1", well_id="WELL 1", pdf=pdf, store_root=root)
    assert (record["status"], record["error"]["code"], record["error"]["stage"]) == ("failed", code, stage)
    assert stored(record) == kept and on_disk(record)["status"] == "failed"


def test_report_invalid_stores_the_report_and_its_findings(files, monkeypatch):
    pdf, _, root = files
    monkeypatch.setattr(R, "extract", fake_extract(INVALID))
    record = R.run(organization_id="org-1", well_id="WELL 1", pdf=pdf, store_root=root)
    assert (record["error"]["code"], record["error"]["stage"]) == ("REPORT_INVALID", "stage")
    assert {"report.md", "findings.jsonl"} <= set(stored(record)) and "pump_config.parquet" not in stored(record)
    assert record["findings"]["by_round1_severity"] == {"REPORT_INVALID": 1}


def test_load_halted_stores_grains_findings_and_review(files, monkeypatch):
    pdf, _, root = files
    monkeypatch.setattr(R, "extract", fake_extract(HALTING))
    record = R.run(organization_id="org-1", well_id="WELL 1", pdf=pdf, store_root=root)
    assert (record["status"], record["error"]["code"]) == ("failed", "LOAD_HALTED")
    assert {"pump_config.parquet", "findings.jsonl", "review.jsonl"} <= set(stored(record))
    assert record["blocks_publish"] is True and record["findings"]["by_round1_severity"]["LOAD_HALTED"] == 1


def test_store_failed_names_the_file(files, monkeypatch):
    _, report, root = files
    real = R.atomic_write

    def flaky(path, data):
        if path.name == "findings.jsonl":
            raise OSError("disk full")
        real(path, data)
    monkeypatch.setattr(R, "atomic_write", flaky)
    record = R.run(organization_id="org-1", well_id="WELL 1", report=report, store_root=root)
    assert (record["error"]["code"], record["store"]["failed_at"]) == ("STORE_FAILED", "findings.jsonl")
    assert "findings.jsonl" not in stored(record) and on_disk(record)["store"]["failed_at"] == "findings.jsonl"


def test_a_record_that_cannot_be_finished_reads_as_stale_after_its_deadline(files, monkeypatch):
    _, report, root = files
    real, writes = R.atomic_write, []

    def last_write_fails(path, data):
        if path.name == "run.json" and writes.count("run.json") == 1:
            raise OSError("disk full")
        writes.append(path.name)
        real(path, data)
    monkeypatch.setattr(R, "atomic_write", last_write_fails)
    record = R.run(organization_id="org-1", well_id="WELL 1", report=report, store_root=root, budget_s=60)
    assert record["store"]["failed_at"] == "run.json" and on_disk(record)["status"] == "running"
    started = datetime.strptime(on_disk(record)["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert R.read_record(record["store_dir"], now=started + timedelta(seconds=30))["status"] == "running"
    late = R.read_record(record["store_dir"], now=started + timedelta(seconds=61))
    assert (late["status"], late["stale"]) == ("failed", True)


# ---- the command-line tool ----

def test_cli_exit_codes_and_summary_line(files, monkeypatch, capsys):
    _, report, root = files
    monkeypatch.setenv(R.STORE_ENV, str(root))
    assert main(["--report", str(report), "--org", "org-1", "--no-well"]) == 0
    line = capsys.readouterr().out.strip()
    assert re.match(r"\d{8}T\d{6}Z-[0-9a-f]{8} succeeded F4 ", line)
    bad = report.with_name("bad.md")
    bad.write_text(INVALID, encoding="utf-8")
    assert main(["--report", str(bad), "--org", "org-1", "--well", "W"]) == 1
    assert "failed F4 REPORT_INVALID at stage" in capsys.readouterr().out
    monkeypatch.delenv(R.STORE_ENV)
    assert main(["--report", str(report), "--org", "org-1", "--no-well"]) == 2
