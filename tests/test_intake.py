"""Intake (app/wells.py, app/intake.py, the upload form, the watch view), the CLI's --run-id and
--source-name, chart grouping and the client-name check.  Invented ids only; no AWS, no real launch:
Popen is replaced wherever the app would start a process."""

import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app.intake as intake
from app.store import PDF_DIRS_ENV, STORE_ENV
from app.wells import WELL_LIST_ENV, SnapshotError, load
from extraction.__main__ import main
from extraction.assets_dir import ASSETS_ENV
from test_unit import F4_P1, F4_P2, make_pdf
from test_viewer import APP, FULL, PDF, PDF_SHA, curve, parquet, record, texts, write_run

LAB = Path(__file__).resolve().parents[1]
RUN = "20990101T000000Z-0000abcd"
# Invented ids: a double space and '#', an organisation that differs only by case, an exact duplicate row.
WELLS = ('"organization_id","well_id"\n'
         '"Org Alpha","Well  #1"\n'
         '"Org Alpha","well #2"\n'
         '"Org Alpha","Well  #1"\n'
         '"org alpha","Lower Well"\n'
         '"Beta Org","B 1"\n')
needs_assets = pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"needs {ASSETS_ENV}")


class FakeProcess:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


@pytest.fixture
def env(tmp_path, monkeypatch):
    root, wells = tmp_path / "store", tmp_path / "wells.csv"
    (root / "runs").mkdir(parents=True)
    wells.write_text(WELLS)
    monkeypatch.setenv(STORE_ENV, str(root))
    monkeypatch.setenv(WELL_LIST_ENV, str(wells))
    monkeypatch.delenv(PDF_DIRS_ENV, raising=False)
    monkeypatch.setattr(intake, "PROCESSES", {})
    return root


@pytest.fixture
def popen(monkeypatch):
    calls = []
    monkeypatch.setattr(intake.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)) or FakeProcess())
    return calls


def app(**params):
    at = AppTest.from_file(APP, default_timeout=60)
    for key, value in params.items():
        at.query_params[key] = value
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def launch_log(root, run_id, text=""):
    (root / "launch").mkdir(exist_ok=True)
    (root / "launch" / f"{run_id}.log").write_text(text)


def run_id(seconds_ago=0):
    return intake.new_run_id(datetime.now(timezone.utc) - timedelta(seconds=seconds_ago))


# ---- the well-list snapshot ----

def test_snapshot_keeps_ids_exactly_and_drops_only_exact_duplicates(tmp_path):
    (tmp_path / "w.csv").write_text(WELLS)
    s = load(tmp_path / "w.csv")
    assert (s.rows, len(s.pairs), len(s.sha256)) == (5, 4, 64)
    assert s.organisations() == ["Beta Org", "Org Alpha", "org alpha"]
    assert s.wells("Org Alpha") == ["Well  #1", "well #2"] and s.wells("org alpha") == ["Lower Well"]
    assert s.case_variants("Org Alpha") == ["org alpha"] and s.case_variants("Beta Org") == []


@pytest.mark.parametrize("text,problem", [
    ("", "no header row"),
    ('"organization_id","site"\n"Org Alpha","x"\n', "no well_id column"),
    ('"organization_id","well_id"\n"Org Alpha"\n', "line 2 has 1 fields"),
    ('"organization_id","well_id"\n"Org Alpha,"x\n', "not a readable CSV"),
])
def test_snapshot_problems_are_reported_without_values(tmp_path, text, problem):
    (tmp_path / "w.csv").write_text(text)
    with pytest.raises(SnapshotError, match=problem) as err:
        load(tmp_path / "w.csv")
    assert "Org Alpha" not in str(err.value)
    with pytest.raises(SnapshotError, match="cannot be read"):
        load(tmp_path / "missing.csv")


def test_header_only_snapshot_has_no_wells(tmp_path):
    (tmp_path / "w.csv").write_text('"organization_id","well_id"\n')
    assert load(tmp_path / "w.csv").pairs == ()


# ---- intake and launch ----

def test_intake_copy_is_by_hash_atomic_and_idempotent(tmp_path):
    first, second = intake.save_pdf(tmp_path, PDF), intake.save_pdf(tmp_path, PDF)
    assert first == second == tmp_path / "intake" / f"{PDF_SHA}.pdf" and first.read_bytes() == PDF
    assert [p.name for p in (tmp_path / "intake").iterdir()] == [first.name]      # no temporary file left
    first.write_bytes(b"%PDF-not the same")
    with pytest.raises(ValueError, match="does not hold the content"):
        intake.save_pdf(tmp_path, PDF)


def test_launch_is_a_detached_argument_list_never_a_shell(tmp_path, popen):
    pdf = intake.save_pdf(tmp_path, PDF)
    intake.launch(tmp_path, pdf, "Org Alpha", "Well  #1", RUN, "Upload name.pdf")
    (args, kw), = popen
    assert args == [sys.executable, "-m", "extraction", f"--pdf={pdf}", "--org=Org Alpha", "--well=Well  #1",
                    f"--run-id={RUN}", "--source-name=Upload name.pdf"]
    assert "shell" not in kw and "env" not in kw and kw["start_new_session"] is True and kw["cwd"] == LAB
    assert kw["stdout"].name == str(tmp_path / "launch" / f"{RUN}.log") and kw["stderr"] == subprocess.STDOUT
    assert intake.exited(RUN) is False and intake.exited("20990101T000000Z-00000000") is None


def test_an_id_starting_with_a_dash_reaches_the_run_intact(tmp_path, popen, monkeypatch):
    import boto3
    import extraction.__main__ as M

    pdf = intake.save_pdf(tmp_path, PDF)
    # No spaces: argparse reads any argument with a space as a value, which would hide the problem.
    intake.launch(tmp_path, pdf, "-Org-Dash", "-W9", RUN, "-draft.pdf")
    (args, _), = popen
    got = []

    class STS:                                        # no AWS: the account check sees the expected account
        def get_caller_identity(self):
            return {"Account": M.ACCOUNT}
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: STS())
    monkeypatch.setattr(M, "run", lambda **kw: got.append(kw) or {
        "run_id": RUN, "status": "succeeded", "family": "F4", "error": None, "rows": None, "cost_usd": None,
        "store_dir": "store"})
    monkeypatch.setenv(STORE_ENV, str(tmp_path / "store"))
    assert main(args[3:]) == 0                        # the launch's own arguments, read by the CLI
    assert (got[0]["organization_id"], got[0]["well_id"], got[0]["source_name"], got[0]["run_id"], got[0]["pdf"]) \
        == ("-Org-Dash", "-W9", "-draft.pdf", RUN, pdf)

def test_run_id_matches_the_tools_own_generator(monkeypatch):
    import extraction.run as R
    monkeypatch.setattr(secrets, "token_hex", lambda n: "ab" * n)
    now = datetime(2026, 10, 13, 18, 0, 1, tzinfo=timezone.utc)
    assert intake.new_run_id(now) == R.new_run_id(now) == "20261013T180001Z-abababab"


def test_watch_state_moves_from_starting_to_running_to_final():
    now = datetime(2026, 10, 13, 18, 0, 0, tzinfo=timezone.utc)
    fresh = intake.new_run_id(now - timedelta(seconds=5))
    old = intake.new_run_id(now - timedelta(seconds=intake.STARTUP_S + 1))
    assert intake.watch_state(None, fresh, now, None) == intake.watch_state(None, fresh, now, False) == "starting"
    assert intake.watch_state(None, fresh, now, True) == intake.watch_state(None, old, now, None) == "launch failed"
    running = {"status": "running", "deadline_at": "2026-10-13T18:15:00Z"}
    assert intake.watch_state(running, fresh, now, False) == "running"
    assert intake.watch_state({**running, "deadline_at": "2026-10-13T17:59:59Z"}, fresh, now, None) == "stale"
    assert intake.watch_state({"status": "succeeded"}, fresh, now, True) == "succeeded"


def test_launch_status_follows_the_process():
    now = datetime(2026, 10, 13, 18, 0, 0, tzinfo=timezone.utc)
    fresh = intake.new_run_id(now - timedelta(seconds=5))
    late = intake.new_run_id(now - timedelta(seconds=intake.STARTUP_S + 30))
    assert intake.watch_state(None, fresh, now, True) == intake.watch_state(None, late, now, True) == "launch failed"
    assert intake.watch_state(None, late, now, False) == "no record yet"        # alive: keep watching
    assert intake.watch_state(None, late, now, None) == "launch failed"         # handle unknown: the 120 s rule

# ---- the form ----

def test_form_lists_ids_exactly_with_the_case_hint(env):
    at = app(upload="1")
    assert any("5 rows" in c and "SHA-256" in c for c in texts(at.caption))
    org = at.selectbox(key="new-org")
    assert org.options == ["Beta Org", "Org Alpha", "org alpha"]
    org.select_index(1).run()
    assert at.selectbox(key="new-org").value == "Org Alpha"
    assert any("differing only by case: org alpha" in c for c in texts(at.caption))
    well = at.selectbox(key="new-well")
    assert well.options == ["Well  #1", "well #2"]          # the double space stays visible
    well.select_index(0).run()
    assert at.selectbox(key="new-well").value == "Well  #1"


def test_submit_needs_a_pdf_and_the_confirmation(env, popen):
    at = app(upload="1")
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    for name, content, message in (("a.pdf", b"", "empty"), ("b.pdf", b"not a pdf", "does not start with %PDF-")):
        at.file_uploader(key="new-pdf").upload(name, content, "application/pdf").run()
        assert any(message in e for e in texts(at.error))
        assert next(b for b in at.button if b.label == "Start the run").disabled
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    assert "This calls Opus on Bedrock in the datascience account; recent runs cost about USD 0.55–1.32 and " \
           "take 3–8 minutes." in texts(at.warning)
    assert next(b for b in at.button if b.label == "Start the run").disabled
    assert not popen and not (env / "intake").exists()


def test_a_confirmed_submit_copies_the_pdf_and_launches(env, popen):
    at = app(upload="1")
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    at.checkbox(key="new-confirm").check().run()
    next(b for b in at.button if b.label == "Start the run").click().run()
    assert not at.exception
    (args, kw), = popen
    launched = next(a.split("=", 1)[1] for a in args if a.startswith("--run-id="))
    assert args == [sys.executable, "-m", "extraction", f"--pdf={env / 'intake' / f'{PDF_SHA}.pdf'}",
                    "--org=Org Alpha", "--well=Well  #1", f"--run-id={launched}", "--source-name=Upload name.pdf"]
    assert "shell" not in kw and (env / "intake" / f"{PDF_SHA}.pdf").read_bytes() == PDF
    assert (env / "launch" / f"{launched}.log").exists()
    assert any("Starting" in i for i in texts(at.info))                      # now on the watch view


def test_a_running_duplicate_is_warned_about_but_allowed(env, popen):
    write_run(env, RUN, record(RUN, status="running", deadline_at="2999-01-01T00:00:00Z",
                               organization_id="Org Alpha", well_id="Well  #1"), {})
    at = app(upload="1")
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    assert any(RUN in w and "still running" in w for w in texts(at.warning))
    at.checkbox(key="new-confirm").check().run()
    assert not next(b for b in at.button if b.label == "Start the run").disabled


@pytest.mark.parametrize("snapshot,message", [
    (None, f"Set {WELL_LIST_ENV}"),
    ('"organization_id","well_id"\n', "a header and no wells"),
    ('"organization_id","site"\n', "no well_id column"),
])
def test_form_is_disabled_without_a_usable_well_list(env, monkeypatch, snapshot, message):
    if snapshot is None:
        monkeypatch.delenv(WELL_LIST_ENV)
    else:
        Path(os.environ[WELL_LIST_ENV]).write_text(snapshot)
    at = app(upload="1")
    assert any(message in e for e in texts(at.error))
    assert at.selectbox(key="new-org").disabled and at.file_uploader(key="new-pdf").disabled
    app()                                                                   # the viewer keeps working


# ---- watching a run ----

def test_watch_view_moves_through_starting_running_and_final(env):
    launched = run_id()
    launch_log(env, launched)
    at = app(watch=launched)
    assert any("Starting" in i for i in texts(at.info))
    write_run(env, launched, record(launched, status="running", deadline_at="2999-01-01T00:00:00Z",
                                    ended_at=None), {})
    at.run()
    assert "Pipeline: running" in " ".join(texts(at.markdown)) and [m.label for m in at.metric] == ["Elapsed"]
    assert any("Current stage: not in the record" in c for c in texts(at.caption))
    (env / "runs" / launched / "run.json").write_text(json.dumps(record(launched)))
    at.run()
    assert "Pipeline: completed" in " ".join(texts(at.markdown))
    assert "Open the run" in [b.label for b in at.button]


@pytest.mark.parametrize("age,process,reason", [
    (intake.STARTUP_S + 5, None, "120 s have passed"),
    (0, FakeProcess(2), "the process has exited"),
])
def test_launch_failure_shows_the_last_50_lines_of_the_log(env, age, process, reason):
    launched = run_id(age)
    launch_log(env, launched, "".join(f"line {n}\n" for n in range(1, 61)))
    if process:
        intake.PROCESSES[launched] = process
    at = app(watch=launched)
    assert any("Launch failed" in e and reason in e for e in texts(at.error))
    tail = texts(at.code)[-1].splitlines()
    assert (len(tail), tail[0], tail[-1]) == (50, "line 11", "line 60")


def test_a_live_process_without_a_record_is_still_watched(env):
    launched = run_id(intake.STARTUP_S + 30)
    launch_log(env, launched)
    intake.PROCESSES[launched] = FakeProcess(None)
    at = app(watch=launched)
    assert not at.error
    waiting = re.fullmatch(r"No record yet, still running \((\d+) s\)\.", texts(at.info)[0])
    assert waiting and int(waiting.group(1)) >= intake.STARTUP_S + 30

def test_run_list_shows_launches_without_a_record(env):
    launched = run_id(intake.STARTUP_S + 5)
    launch_log(env, launched, "boom\n")
    df = app().dataframe[0].value.set_index("Run id")
    assert df.loc[launched, "Pipeline"] == "launched, no record"
    assert any("Launch failed" in e for e in texts(app(run=launched).error))   # opening it watches it


# ---- charts: one chart per curve, a series per frequency ----

def test_a_curves_frequencies_share_one_chart(env):
    rows = [*(curve("curve_point", q, h) for q, h in ((0, 6000), (1000, 5600))),
            *(curve("curve_point", q, h, frequency_hz_as_printed=50.0) for q, h in ((0, 4200), (800, 3900)))]
    write_run(env, RUN, record(RUN), {**FULL, "curve_observations.parquet": parquet(rows)})
    charts = app(run=RUN).get("plotly_chart")
    assert len(charts) == 1
    traces = json.loads(charts[0].proto.spec)["data"]
    assert [t["name"] for t in traces] == ["50 Hz", "60 Hz"]
    assert all(" Hz<br>" in t["hovertemplate"] for t in traces)


def test_the_summary_shows_the_source_name_and_older_records_still_render(env):
    old = "20990101T000000Z-0000abce"
    write_run(env, RUN, record(RUN, source={"pdf": {"path": "/nowhere/x.pdf", "sha256": PDF_SHA}, "report": None,
                                            "source_name": "Upload name.pdf"}), FULL)
    write_run(env, old, record(old), FULL)                                      # written before the field

    def shown(at):
        facts = next(df.value for df in at.dataframe
                     if "source.source_name" in set(df.value.get("Field in run.json", ())))
        return facts.set_index("Field in run.json").loc["source.source_name", "Value"]
    assert shown(app(run=RUN)) == "Upload name.pdf"
    assert shown(app(run=old)) == "—"

# ---- the command-line tool's two flags ----

@pytest.fixture
def cli(tmp_path, monkeypatch):
    import extraction.__main__ as M
    calls = []

    def fake_run(**kw):
        calls.append(kw)
        return {"run_id": kw["run_id"] or RUN, "status": "succeeded", "family": "F4", "error": None, "rows": None,
                "cost_usd": None, "store_dir": "store"}
    monkeypatch.setattr(M, "run", fake_run)                                 # no pipeline, no AWS
    monkeypatch.setenv(STORE_ENV, str(tmp_path / "store"))
    return calls, ["--report", str(tmp_path / "report.md"), "--org", "org-test"]


def test_cli_passes_the_flags_through_and_defaults_to_none(cli):
    calls, base = cli
    assert main([*base, "--pdf", "doc.pdf", "--well", "well-a", "--run-id", RUN, "--source-name", "Up load.pdf"]) == 0
    assert (calls[-1]["run_id"], calls[-1]["source_name"]) == (RUN, "Up load.pdf")
    assert main([*base, "--no-well"]) == 0
    assert (calls[-1]["run_id"], calls[-1]["source_name"]) == (None, None)


@pytest.mark.parametrize("flags", [
    ["--run-id", "not-a-run-id"], ["--run-id", "20990101T000000Z-0000ABCD"], ["--run-id", f"{RUN}/x"],
    ["--source-name", "dir/name.pdf"], ["--source-name", ""], ["--source-name", "a\nb.pdf"],
    ["--source-name", "name.pdf"],                                         # without --pdf
])
def test_cli_rejects_bad_flags(cli, flags):
    calls, base = cli
    with pytest.raises(SystemExit) as err:
        main([*base, "--no-well", *flags])
    assert err.value.code == 2 and not calls


def test_cli_refuses_a_run_id_whose_directory_exists(cli, tmp_path, capsys):
    calls, base = cli
    (tmp_path / "store" / "runs" / RUN).mkdir(parents=True)
    assert main([*base, "--no-well", "--run-id", RUN]) == 2
    assert "already exists" in capsys.readouterr().err and not calls


@needs_assets
def test_the_run_carries_the_given_id_and_source_name(tmp_path, monkeypatch):
    import extraction.run as R
    from test_run import GOOD, fake_extract

    pdf, report, root = tmp_path / "doc.pdf", tmp_path / "report.md", tmp_path / "store"
    pdf.write_bytes(make_pdf([F4_P1, F4_P2]))
    report.write_text(GOOD, encoding="utf-8")
    given = R.run(organization_id="org-test", well_id="well-a", report=report, pdf=pdf, store_root=root,
                  run_id=RUN, source_name="Up load.pdf")
    stored = Path(given["store_dir"])
    assert given["run_id"] == stored.name == json.loads((stored / "run.json").read_text())["run_id"] == RUN

    def sources(record):
        return {line.split(",")[0] for line in (Path(record["store_dir"]) / "prep_manifest.csv").read_text().splitlines()[1:]}
    assert sources(given) == {"Up load.pdf"}
    assert sources(R.run(organization_id="org-test", well_id="well-a", report=report, pdf=pdf,
                         store_root=root)) == {"doc.pdf"}                   # without it: the basename, as before
    prompts, extract = [], fake_extract()
    monkeypatch.setattr(R, "extract", lambda prompt, **kw: prompts.append(prompt) or extract(prompt, **kw))
    R.run(organization_id="org-test", well_id="well-a", pdf=pdf, store_root=root, source_name="Up load.pdf")
    assert "Up load.pdf" in prompts[0] and "doc.pdf" not in prompts[0]
    with pytest.raises(ValueError, match="not a run id"):
        R.run(organization_id="org-test", well_id="well-a", report=report, store_root=root, run_id="x")


@needs_assets
def test_the_record_keeps_the_source_name(tmp_path):
    import extraction.run as R
    from test_run import GOOD

    pdf, report, root = tmp_path / "doc.pdf", tmp_path / "report.md", tmp_path / "store"
    pdf.write_bytes(make_pdf([F4_P1, F4_P2]))
    report.write_text(GOOD, encoding="utf-8")

    def names(**kw):
        stored = Path(R.run(organization_id="org-test", well_id="well-a", report=report, store_root=root,
                            **kw)["store_dir"])
        return (json.loads((stored / "run.json").read_text())["source"]["source_name"],
                json.loads((stored / "source.json").read_text())["source_name"])
    assert names(pdf=pdf, source_name="Up load.pdf") == ("Up load.pdf", "Up load.pdf")
    assert names(pdf=pdf) == ("doc.pdf", "doc.pdf")                            # without it: the basename
    assert names() == (None, None)                                              # no PDF, no name

# ---- the client-name check ----

def test_name_check_prints_locations_never_values(tmp_path):
    tree, snapshot = tmp_path / "tree", tmp_path / "wells.csv"
    (tree / "docs").mkdir(parents=True)
    snapshot.write_text(WELLS)
    (tree / "docs" / "old.md").write_text("intro\nsee WELL #2 here\n")      # well id, another case
    (tree / "new.py").write_text("x = 1\n# about beta org today\n")          # organisation id, whole word
    (tree / "notes.txt").write_text("beta organisation; B 1\n")             # not a whole word; too short
    (tree / ".gitignore").write_text("ignored.csv\n")
    (tree / "ignored.csv").write_text("Well  #1\n")

    def check(*changed):
        return subprocess.run([sys.executable, str(LAB / "tools" / "check_client_names.py"), str(snapshot),
                               *changed, "--root", str(tree)], capture_output=True, text=True, cwd=tree)
    before, after = check(), check("new.py")
    assert before.returncode == 0 and after.returncode == 1
    assert "docs/old.md:2" in before.stdout and "new.py:2" in before.stdout and "notes.txt" not in before.stdout
    assert "files changed this step: 1 hits in 1 files" in after.stdout
    for value in ("well  #1", "well #2", "beta org", "org alpha", "lower well"):
        assert value not in (before.stdout + after.stdout).casefold()
