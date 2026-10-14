"""The run viewer (app/), on synthetic run stores built in temporary directories.  No AWS.

The store-layer tests and the AppTest tests need nothing else.  The forced-failure test runs
the real writer (extraction.run) and needs ESP_EXTRACTION_ASSETS_DIR, like test_run.py.  The
`store` test renders every run of the store ESP_RUN_STORE_DIR names: `pytest -m store`.
"""

import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app.store import PDF_DIRS_ENV, STORE_ENV, LocalRunStore, local_store_from_env, run_status
from extraction.assets_dir import ASSETS_ENV
from test_unit import F4_P1, F4_P2, make_pdf
from test_validate import report_text

APP = str(Path(__file__).resolve().parents[1] / "app" / "main.py")
PDF = make_pdf(list((["Synthetic page one"], ["Synthetic page two"])))
LINK = "[" * 2 + "not a link" + "]" * 2      # a wiki-link string, built so no literal one is tracked
PDF_SHA = hashlib.sha256(PDF).hexdigest()
MISSING_SHA = "0" * 64

OLD, GOOD, PARTIAL, RUNNING, STALE, NO_PDF, BROKEN, ODD = (
    "20990101T000000Z-00000000", "20990101T000001Z-0000000a", "20990101T000002Z-0000000b",
    "20990101T000003Z-0000000c", "20990101T000004Z-0000000d", "20990101T000005Z-0000000e",
    "20990101T000006Z-0000000f", "20990101T000007Z-00000010")


# ---- synthetic runs ----

def record(run_id, **over):
    base = {
        "run_id": run_id, "status": "succeeded", "started_at": "2099-01-01T00:00:01Z",
        "deadline_at": "2099-01-01T00:15:01Z", "ended_at": "2099-01-01T00:04:01Z", "budget_s": 900, "mode": "pdf",
        "organization_id": "org-test", "well_id": "well-a", "family": "F4", "family_source": "detect",
        "source": {"pdf": {"path": "/nowhere/recorded.pdf", "sha256": PDF_SHA}, "report": None},
        "pages": {"page_count": 2, "kept_pages": [1, 2], "roles": {"1": ["header"]}},
        "rows": {"pump_config": 1, "esp_well_design_context": 1, "curve_observations": 9}, "quarantine": 1,
        "findings": {"total": 2, "by_round1_severity": {"warning": 2}, "by_spec_severity": {"HALT": 1, "none": 1}},
        "spec_validation_status": "needs_review", "blocks_publish": False,
        "review": {"items": 2, "needs_review": 1, "informational": 0},
        "unknown_models": [{"model": "MODEL1", "class": "no_head_curve", "pump_model_as_printed": "MODEL-1"}],
        "error": None,
        "model": {"model_id": "model-x", "effort": "medium", "max_tokens": 1000, "clarifications": True,
                  "stop_reason": "end_turn", "model_time_s": 2.0, "time_to_first_token_s": 1.0},
        "hashes": {"report_sha256": "x"}, "tokens": {"input": 10, "output": 5}, "cost_usd": 0.5,
        "stages_s": {"detect": 0.1, "prep": 0.1, "extract": 2.0, "stage": 0.1, "store": 0.0, "record": 0.0},
        "store": {"files": [], "failed_at": None},
    }
    return {**base, **over}


def parquet(rows) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_parquet(buf, index=False)
    return buf.getvalue()


def jsonl(rows) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def curve(point_type, flow, head, composite=True, **over):
    return {"pump_model_canonical": None, "pump_model_as_printed": None, "point_type": point_type,
            "frequency_hz_as_printed": 60.0, "flow_as_printed": flow, "flow_unit_as_printed": "bpd",
            "head_as_printed": head, "head_unit_as_printed": "ft", "head_basis_as_printed": "string_total",
            "observation_is_composite": composite, "curve_observation_role": "head_curve", "scenario_label": "Case A",
            "scenario_ordinal": "1", "sensitivity_case": False, "source_page": "p2", **over}


REPORT = report_text("F4", pc=[{"section_order": "1", "pump_model_as_printed": "MODEL-1", "source_page": "p1"}],
                     dc=[{"oil_api_deg": "40", "source_page": "p2"}]) + f"\nNote: {LINK}\n"
FULL = {
    "source.json": json.dumps({"pdf": {"path": "/nowhere/recorded.pdf", "sha256": PDF_SHA}, "report": None}),
    "report.md": REPORT,
    "prep_manifest.csv": "original_page_no,page_role,included\n1,header,yes\n2,performance,yes\n",
    "pump_config.parquet": parquet([{"section_order": 1, "pump_model_as_printed": "MODEL-1", "source_page": "p1"}]),
    "esp_well_design_context.parquet": parquet([{"oil_api_deg": 40.0, "provenance_json": "{}"}]),
    "curve_observations.parquet": parquet([
        *(curve("curve_point", q, h) for q, h in ((0, 6000), (1000, 5600), (2000, 4800), (3000, 3400))),
        curve("design_point", 1800, 5000), curve("bep", 2100, 4700), curve("ror_min", 900, 5650),
        curve("bep", 2100, None, composite=False, pump_model_canonical="MODEL1", curve_observation_role="single"),
        curve("design_point", None, 900, composite=None, pump_model_as_printed="MODEL-2", source_page="p6, p7"),
    ]),
    "quarantine.jsonl": jsonl([{"table": "curve_observations", "column": "head_as_printed", "raw_value": "300 psi",
                                "reason": "d9_head_in_pressure_units", "source_page": "p2"}]),
    "findings.jsonl": jsonl([
        {"rule_id": "V-06", "spec_severity": "HALT", "round1_severity": "warning", "scope": "SEC",
         "table": "stg_pump_config", "row_key": 1, "column": "pump_model_as_printed", "value": "MODEL-1",
         "message": "does not match the contract grammar", "blocks_publish": False},
        {"rule_id": "G-S4", "spec_severity": None, "round1_severity": "warning", "scope": "DOC", "table": None,
         "row_key": None, "column": None, "value": 3, "message": "footer declares 3, 2 parsed", "blocks_publish": False},
    ]),
    "review.jsonl": jsonl([
        {"field": "oil_api_deg", "table": "stg_design_context", "row_key": 1, "value": "40", "cited_page": "p2",
         "grounding": "cited_page", "reasons": [], "needs_review": False},
        {"field": "pump_model_as_printed", "table": "stg_pump_config", "row_key": 1, "value": "MODEL-1",
         "cited_page": "p1", "grounding": "not_found", "reasons": ["grounding", "rule: V-06"], "needs_review": True},
    ]),
    "notices.jsonl": jsonl([{"kind": "known_issue", "rule_id": "G-K0", "family": "F4", "field": "x",
                             "message": "a synthetic known issue"}]),
    "unknown_models.jsonl": jsonl([{"model": "MODEL1", "class": "no_head_curve", "pump_model_as_printed": "MODEL-1"}]),
    "d16_log.jsonl": jsonl([{"family": "F4", "n_scenarios": 1}]),
    "d17_log.jsonl": jsonl([{"family": "F4", "status": "collapsed"}]),
    "gas_log.jsonl": "",
}


def write_run(root: Path, run_id: str, rec, files):
    run = root / "runs" / run_id
    run.mkdir(parents=True)
    for name, data in {**({"run.json": json.dumps(rec)} if isinstance(rec, dict) else {"run.json": rec}),
                       **files}.items():
        if data is not None:
            (run / name).write_bytes(data if isinstance(data, bytes) else data.encode())


@pytest.fixture
def store(tmp_path, monkeypatch):
    root, pdfs = tmp_path / "store", tmp_path / "pdfs" / "nested"
    pdfs.mkdir(parents=True)
    (pdfs / "synthetic.pdf").write_bytes(PDF)
    write_run(root, OLD, record(OLD), FULL)
    write_run(root, GOOD, record(GOOD), FULL)
    write_run(root, PARTIAL, record(PARTIAL, status="failed", well_id="well-b", spec_validation_status=None,
                                    error={"code": "MODEL_TIMEOUT", "message": "forced by a test", "details": {},
                                           "stage": "extract"}),
              {k: FULL[k] for k in ("source.json", "prep_manifest.csv")})
    write_run(root, RUNNING, record(RUNNING, status="running", deadline_at="2999-01-01T00:00:00Z"), {})
    write_run(root, STALE, record(STALE, status="running", deadline_at="2000-01-01T00:00:00Z"), {})
    missing = json.dumps({"pdf": {"path": "/nowhere/missing.pdf", "sha256": MISSING_SHA}, "report": None})
    write_run(root, NO_PDF, record(NO_PDF, well_id="well-b"), {**FULL, "source.json": missing})
    write_run(root, BROKEN, "{not json", {**FULL, "findings.jsonl": FULL["findings.jsonl"] + "{oops\n",
                                         "pump_config.parquet": b"", "curve_observations.parquet": b"not parquet",
                                         "report.md": ""})
    write_run(root, ODD, record(ODD, status=["?"], family=["F4"], spec_validation_status={"x": 1}, cost_usd="abc",
                                error="not an object", stages_s=[1], unknown_models=7, pages="none", well_id=None),
              {"source.json": "[]"})
    monkeypatch.setenv(STORE_ENV, str(root))
    monkeypatch.setenv(PDF_DIRS_ENV, str(tmp_path / "pdfs"))
    return root


def app(run_id=None):
    at = AppTest.from_file(APP, default_timeout=60)
    if run_id:
        at.query_params["run"] = run_id
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def texts(elements):
    """Element text as shown: colour and icon directives dropped, Markdown escapes undone."""
    return [re.sub(r"\\(.)", r"\1", re.sub(r":[a-z]+\[(?::material/[a-z_]+: )?(.*?)\]", r"\1", e.value))
            for e in elements]


# ---- the storage layer ----

def test_run_ids_newest_first_and_only_run_directories(store):
    (store / "runs" / ".tmp").mkdir()
    (store / "runs" / "stray.txt").write_text("x")
    assert LocalRunStore(store).run_ids() == [ODD, BROKEN, NO_PDF, STALE, RUNNING, PARTIAL, GOOD, OLD]
    assert LocalRunStore(store / "absent").run_ids() == []


def test_read_files_and_refuse_paths_outside_the_store(store):
    s = LocalRunStore(store)
    assert s.read(GOOD, "report.md").decode() == REPORT
    assert s.record(GOOD)["run_id"] == GOOD
    with pytest.raises(FileNotFoundError):
        s.read(PARTIAL, "findings.jsonl")
    for bad in ("../x", "a/b", ".hidden", "", "/etc/passwd"):
        with pytest.raises(ValueError):
            s.read(GOOD, bad)
        with pytest.raises(ValueError):
            s.read(bad, "run.json")
    with pytest.raises(ValueError):
        s.record(BROKEN)


def test_source_pdf_by_hash_from_intake_then_the_search_dirs(store, tmp_path):
    s = LocalRunStore(store, [tmp_path / "pdfs"])
    assert s.source_pdf(PDF_SHA) == PDF
    assert s.source_pdf(MISSING_SHA) is None and s.source_pdf("not-a-hash") is None and s.source_pdf(None) is None
    assert LocalRunStore(store).source_pdf(PDF_SHA) is None          # not in intake, no search dirs
    (store / "intake").mkdir()
    (store / "intake" / f"{PDF_SHA}.pdf").write_bytes(PDF)
    assert LocalRunStore(store).source_pdf(PDF_SHA) == PDF
    (store / "intake" / f"{MISSING_SHA}.pdf").write_bytes(PDF)        # named for a hash it does not have
    assert LocalRunStore(store).source_pdf(MISSING_SHA) is None


def test_source_pdf_never_opens_the_recorded_path(store, tmp_path):
    recorded = tmp_path / "recorded" / "doc.pdf"
    recorded.parent.mkdir()
    recorded.write_bytes(PDF)
    s = LocalRunStore(store)                                          # the recorded folder is not a search dir
    assert s.source_pdf(PDF_SHA) is None


def test_hashes_are_cached_by_path_mtime_and_size(store, tmp_path, monkeypatch):
    import app.store as S
    calls = []
    real = S.hashlib.file_digest
    monkeypatch.setattr(S.hashlib, "file_digest", lambda f, alg: calls.append(1) or real(f, alg))
    monkeypatch.setattr(S, "_HASHES", {})
    s = LocalRunStore(store, [tmp_path / "pdfs"])
    s.source_pdf(MISSING_SHA)
    s.source_pdf(MISSING_SHA)
    assert len(calls) == 1
    pdf = tmp_path / "pdfs" / "nested" / "synthetic.pdf"
    pdf.write_bytes(PDF + b"\n")
    s.source_pdf(MISSING_SHA)
    assert len(calls) == 2


def test_store_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv(STORE_ENV, raising=False)
    assert local_store_from_env() is None
    monkeypatch.setenv(STORE_ENV, str(tmp_path))
    monkeypatch.setenv(PDF_DIRS_ENV, os.pathsep.join(["a", "", "b"]))
    s = local_store_from_env()
    assert s.root == tmp_path and s.pdf_dirs == [Path("a"), Path("b")]


def test_run_status_matches_the_writers_reader(tmp_path):
    import extraction.run as R
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for status, deadline in (("running", now - timedelta(seconds=1)), ("running", now + timedelta(seconds=1)),
                             ("succeeded", now - timedelta(days=1)), ("failed", now + timedelta(days=1))):
        rec = {"status": status, "deadline_at": R.utc(deadline)}
        (tmp_path / "run.json").write_text(json.dumps(rec))
        theirs = R.read_record(tmp_path, now=now)
        assert run_status(rec, now) == (theirs["status"], theirs.get("stale", False))


# ---- the list ----

def test_list_shows_every_run_with_both_statuses_and_the_latest_marker(store):
    at = app()
    df = at.dataframe[0].value.set_index("Run id")
    assert list(df.index) == [ODD, BROKEN, NO_PDF, STALE, RUNNING, PARTIAL, GOOD, OLD]
    assert df.loc[GOOD, "Run"] == "succeeded" and df.loc[GOOD, "Document checks"] == "needs review"
    assert df.loc[PARTIAL, "Run"] == "failed" and df.loc[PARTIAL, "Document checks"] == "not run"
    assert df.loc[PARTIAL, "Error"] == "MODEL_TIMEOUT at extract"
    assert df.loc[RUNNING, "Run"] == "running" and df.loc[STALE, "Run"] == "failed (stale)"
    assert df.loc[BROKEN, "Run"] == "run.json unreadable"
    assert df.loc[GOOD, "Cost (USD)"] == 0.5
    assert {r for r in df.index if df.loc[r, "Latest"]} == {GOOD, NO_PDF}      # newest succeeded per well


def test_list_filters_by_status(store):
    at = app()
    at.multiselect(key="status").set_value(["failed (stale)"]).run()
    assert list(at.dataframe[0].value["Run id"]) == [STALE]


# ---- one run ----

def test_succeeded_run_shows_everything(store):
    at = app(GOOD)
    md = " ".join(texts(at.markdown))
    assert "Run: succeeded" in md and "Document checks: needs review" in md
    assert any("spec_validation_status" in c and "needs_review" in c for c in texts(at.caption))
    assert not at.error and not [i for i in texts(at.info) if "not stored" in i]
    frames = {tuple(df.value.columns)[:2]: df.value for df in at.dataframe}
    findings = next(df for cols, df in frames.items() if cols[0] == "row's page")
    assert findings["row's page"].tolist() == ["p1", None]                    # HALT first, page from report.md
    review = next(df for cols, df in frames.items() if cols[0] == "cited_page")
    assert review["needs_review"].tolist() == [True, False]
    assert len(at.get("plotly_chart")) == 1                                   # one curve with points to draw
    skipped = [df.value for df in at.dataframe if "Not drawn because" in df.value.columns]
    assert [v for df in skipped for v in df["Not drawn because"]] == ["no head value",
                                                                       "no point has both flow and head"]
    assert skipped[1]["pages"].tolist() == ["p6, p7"]
    assert LINK in texts(at.code)[-1] and LINK not in md
    assert len(at.image) == 1 and "Page 1 of 2" in at.image[0].proto.imgs[0].caption


def test_a_page_reference_turns_the_source_pane(store):
    at = app(GOOD)
    at.session_state[f"page-{GOOD}"] = 2
    at.run()
    assert "Page 2 of 2" in at.image[0].proto.imgs[0].caption


def test_failed_partial_run_shows_its_error_and_what_it_stored(store):
    at = app(PARTIAL)
    assert texts(at.error)[0] == "**MODEL_TIMEOUT** at extract: forced by a test"
    notes = texts(at.info)
    assert "`findings.jsonl` is not stored: the run failed at extract before writing it." in notes
    assert any("original_page_no" in df.value.columns for df in at.dataframe)  # the prep manifest it did store


def test_running_and_stale_runs(store):
    md = " ".join(texts(app(RUNNING).markdown))
    assert "Run: running" in md
    at = app(STALE)
    assert "Run: failed (stale)" in " ".join(texts(at.markdown))
    assert any("deadline" in w for w in texts(at.warning))


def test_missing_pdf_shows_the_recorded_path_as_text_only(store):
    at = app(NO_PDF)
    assert "Source not in store" in texts(at.warning) and not at.image
    assert "/nowhere/missing.pdf" in texts(at.code)


def test_malformed_files_each_get_a_notice(store):
    at = app(BROKEN)
    warnings = " ".join(texts(at.warning))
    assert "`run.json` could not be read" in warnings
    assert "`findings.jsonl`: line 3 is not valid JSON" in warnings
    assert "`pump_config.parquet` is stored but empty." in warnings
    assert "`curve_observations.parquet` could not be shown" in warnings
    assert "`report.md` is stored but empty." in warnings


def test_a_record_with_odd_types_still_renders(store):
    assert app().dataframe[0].value.set_index("Run id").loc[ODD, "Run"] == '["?"]'
    at = app(ODD)
    assert "Run: [\"?\"]" in " ".join(texts(at.markdown))
    assert "No source information for this run." in texts(at.info)


def test_unknown_run_id(store):
    at = app("../../etc")
    assert any("no run" in w for w in texts(at.warning))


# ---- the real writer's runs, including a failure forced at each of its seven points ----

@pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"the writer reads the assets; set {ASSETS_ENV}")
def test_the_writers_runs_render(tmp_path, monkeypatch):
    import extraction.run as R
    from test_run import HALTING, INVALID, fail, fake_extract

    root, pdf, report = tmp_path / "store", tmp_path / "doc.pdf", tmp_path / "report.md"
    pdf.write_bytes(make_pdf([F4_P1, F4_P2]))
    report.write_text(report_text("F4", pc=[{"section_order": "1", "pump_model_as_printed": "ELS-1234"}]))
    real_write = R.atomic_write

    def store_fails(path, data):
        if path.name == "findings.jsonl":
            raise OSError("disk full")
        real_write(path, data)

    writes = []

    def record_fails(path, data):
        if path.name == "run.json" and writes.count("run.json") == 1:
            raise OSError("disk full")
        writes.append(path.name)
        real_write(path, data)

    cases = {  # expected code -> (patch, run kwargs)
        None: ({}, dict(report=report, pdf=pdf)),
        "UNKNOWN_TEMPLATE": ({"detect": fail("UNKNOWN_TEMPLATE")}, dict(pdf=pdf)),
        "PREP_FAILED": ({"prep": fail("PREP_FAILED")}, dict(pdf=pdf)),
        "MODEL_TIMEOUT": ({"extract": fail("MODEL_TIMEOUT")}, dict(pdf=pdf)),
        "REPORT_INVALID": ({"extract": fake_extract(INVALID)}, dict(pdf=pdf)),
        "LOAD_HALTED": ({"extract": fake_extract(HALTING)}, dict(pdf=pdf)),
        "STORE_FAILED": ({"atomic_write": store_fails}, dict(report=report)),
        "stale": ({"atomic_write": record_fails}, dict(report=report, budget_s=-1)),   # the last run.json is lost
    }
    ids = {}
    for code, (patches, kwargs) in cases.items():
        with monkeypatch.context() as m:
            for name, fn in patches.items():
                m.setattr(R, name, fn)
            ids[code] = R.run(organization_id="org-test", well_id="well-a", store_root=root, **kwargs)["run_id"]
    monkeypatch.setenv(STORE_ENV, str(root))
    monkeypatch.delenv(PDF_DIRS_ENV, raising=False)
    app()
    for code, run_id in ids.items():
        at = app(run_id)
        if "pdf" in cases[code][1]:                                            # the recorded path is never opened
            assert "Source not in store" in texts(at.warning)
        if code is None:
            assert not at.error and not [i for i in texts(at.info) if "not stored" in i]
        elif code == "stale":
            assert "Run: failed (stale)" in " ".join(texts(at.markdown))
        else:
            assert code in texts(at.error)[0]


# ---- every run of a real store ----

@pytest.mark.store
@pytest.mark.skipif(not os.environ.get(STORE_ENV), reason=f"renders the store {STORE_ENV} names; set it")
def test_every_run_in_the_store_renders():
    run_ids = local_store_from_env().run_ids()
    assert run_ids
    app()
    failed = {}
    for run_id in run_ids:
        at = AppTest.from_file(APP, default_timeout=60)
        at.query_params["run"] = run_id
        at.run()
        if at.exception:
            failed[run_id] = [e.value for e in at.exception]
    assert not failed
