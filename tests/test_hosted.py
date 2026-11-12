"""The app as it runs hosted, behind a sign-in, for engineers (app/hosted.py, and what app/main.py and app/review.py do
with it): the technical switch, each page's plain words with the switch off, whom to tell, the one AWS session, who is
signed in, the pump catalogue's fetch and the bundled fonts.  Fakes only: no AWS, no network, invented names.

Every other suite runs with the switch on (tests/conftest.py); the tests here turn it off (`off`, `turn`).

    pytest -m store tests/test_hosted.py    every run in ESP_RUN_STORE_DIR with the switch off: the run list, each
                                            run's own page and its watch page, scanned whole
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import streamlit as st
from botocore.exceptions import ClientError, NoCredentialsError
from google.protobuf.json_format import MessageToDict
from streamlit.runtime.context import StreamlitHeaders
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.element_tree import Dataframe

import app.intake as intake
import app.review as R
import app.store as S
import app.transcription as T
import app.wells as W
import extraction.confirm as CLI
import extraction.confirmation as C
import test_app_s3 as TA
import test_lambda as TL
import test_screens as TS
from app import hosted
from extraction.assets_dir import ASSETS_ENV
from extraction.errors import ExtractionError
from extraction.well_entry import empty_entry
from test_app_s3 import fake  # noqa: F401 - a fixture: S3 mode on a fake bucket
from test_confirm import CATALOGUE, ORG, PDF, RUN_ID, WELL, put_run, run_record
from test_editor import fresh_catalogue  # noqa: F401 - a fixture
from test_intake import LIVE, TEST, FakeProcess, env, launch_log, popen, run_id  # noqa: F401 - env, popen: fixtures
from test_manual import IMAGE_ONLY, SCAN_WORDS, failed, put_failed
from test_review import (AT, FOUND, NOT_FOUND, RERUN, Triggered, confirmed_first, offline, rerun,  # noqa: F401
                         s3_mode, to_check, write_button)                                         # - fixtures
from test_viewer import APP, FULL, record, texts, write_run

LAB = Path(__file__).resolve().parents[1]
ATHENA_CLIENT = W.athena_client             # the app's own, before tests/conftest.py puts its guard in its place
S3_CLIENT = S.s3_client                     # the app's own, before test_app_s3.fake puts its bucket in its place
stage = sys.modules[hosted.asset.__module__]    # extraction.stage.stage: the package's name is a function
NO_ONE = "whoever looks after this app"
NOT_SET_UP = f"This app is not set up yet, so it has nothing to show. Tell {NO_ONE}."
NOT_READ = f"The runs could not be read. Reload the page; if it fails again, tell {NO_ONE}."
NO_CATALOGUE = f"The pump catalogue could not be read, so models cannot be picked or checked here. Tell {NO_ONE}."
NOT_TOLD = f"Write is off: the app was not told who is signed in. Reload the page; if Write stays off, tell {NO_ONE}."
ROLE = "arn:aws:iam::000000000000:role/an-invented-role"        # a placeholder: no account has this number
HEADER, USER = "X-Forwarded-User", "Engineer.One@example.com"   # a placeholder address
TIMEOUT = {"code": "MODEL_TIMEOUT", "stage": "extract", "message": "an invented message"}
# What no page shows with the switch off: tests/test_screens.py's ids and codes (run and confirmation ids, hashes and
# their prefixes, codes, keys, dev and prd, object keys, exception names, the pipeline's own words), and besides
# them the names of AWS and its services, of settings, and commands.
HOSTED = {**TS.PATTERNS,
          "AWS or a service": r"(?i)\b(?:AWS|Amazon|S3|Athena|Bedrock|DynamoDB|CloudWatch|Lambda|STS|IAM|SSO|boto3?"
                              r"|Streamlit|Opus)\b",
          "setting": r"\b(?:ESP|AWS|STREAMLIT)_[A-Z0-9_]+\b",
          "command": r"`|\bstreamlit run\b|\baws sso\b|\bpython -m\b",
          "account or other long number": r"\b\d{9,}\b"}
SHOWN = pytest.mark.parametrize("show", [False, True], ids=["hidden", "shown"])
SETTLED = f"Write is off: the checks before writing could not run. Tell {NO_ONE}."


# ---- helpers ----

def turn(monkeypatch, show: bool):
    monkeypatch.setenv(hosted.SHOW_ENV, "1") if show else monkeypatch.delenv(hosted.SHOW_ENV, raising=False)


@pytest.fixture
def off(monkeypatch):
    """The switch off, as a hosted engineer has the app."""
    turn(monkeypatch, False)


def app(**params):
    at = AppTest.from_file(APP, default_timeout=60)
    for key, value in params.items():
        at.query_params[key] = value
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def unplain(at, *names) -> list[str]:
    """Every id, code, AWS or setting name on the whole page, `names` (organisations, wells, documents, who confirms:
    as they are and as the screens mark them) left out.  Nothing is technical with the switch off, so nothing is
    skipped."""
    assert not at.exception, [e.value for e in at.exception]
    leave_out = sorted({*names, *map(W.marked, names)}, key=len, reverse=True)
    hits = []
    for text in TS.screen(at):
        rest = text
        for name in leave_out:
            rest = rest.replace(name, " ")
        hits += [f"{kind}: {m[0]!r} in {text[:160]!r}" for kind, pattern in HOSTED.items()
                 for m in re.finditer(pattern, rest)]
    return hits


def page(at) -> str:
    return " ".join(TS.screen(at))


def technical_sections(at) -> list[str]:
    return [e.label for e in at.get("expander") if e.label.endswith("(technical)")]


def logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == hosted.LOG.name]


def no_entry(objects, error=TIMEOUT):
    """The synthetic run as one that failed and stored nothing to review; its PDF stays, so it can be entered by
    hand."""
    put_failed(objects, failed(error=error))
    objects.path(C.run_key(RUN_ID, C.ENTRY)).unlink(missing_ok=True)


def settle_all(at):
    for key in FOUND:
        at.button(key=f"settle-{RUN_ID}-{key}").click().run()
    for key in NOT_FOUND:
        at.button(key=f"empty-{RUN_ID}-{key}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def review_names(*more) -> list[str]:
    return TS.exempt(run_record(), to_check(), None, "tester", "analyst-1", *more)


# ---- R1: the switch ----

@pytest.mark.parametrize("value,shows", [("1", True), (None, False), ("", False), ("0", False), ("true", False),
                                         ("yes", False), (" 1", False), ("11", False)])
def test_only_1_shows_the_technical_views(value, shows):
    assert hosted.technical({} if value is None else {hosted.SHOW_ENV: value}) is shows


@SHOWN
def test_a_run_details_and_its_tabs_are_drawn_only_with_the_switch_on(offline, monkeypatch, show):  # noqa: F811
    turn(monkeypatch, show)
    at = app(run=RUN_ID)
    assert write_button(at).label == "Write to Live wells"                          # the review, either way
    assert [g.options for g in at.get("button_group")] == (
        [["Review & confirm", "Run details (technical)"]] if show else [])
    if show:
        at.get("button_group")[0].set_value("Run details (technical)").run()
        assert [t.label for t in at.tabs][:2] == ["Summary", "Extracted numbers"]
        assert f"Run {RUN_ID}" in texts(at.subheader)
    else:
        assert not at.tabs and "technical" not in page(at) and unplain(at, *review_names()) == []


@SHOWN
def test_b_a_run_with_no_entry_opens_on_its_tabs_or_with_the_switch_off_on_a_plain_page(offline, monkeypatch,  # noqa: F811
                                                                                         show):
    turn(monkeypatch, show)
    no_entry(offline)
    at = app(run=RUN_ID)
    assert at.button(key=f"enter-{RUN_ID}").label == "Enter by hand"                # offered either way
    assert R.failure_words(failed(error=TIMEOUT)) in texts(at.info)
    if show:
        assert f"Run {RUN_ID}" in texts(at.subheader) and len(at.tabs) == 8
        return
    assert texts(at.subheader) == ["design.pdf"] and not at.tabs and not at.get("expander")
    assert "#### Run: failed" in texts(at.markdown)
    assert "Open the run" not in [b.label for b in at.button]                       # this is the run's page
    assert unplain(at, ORG, WELL, "design.pdf") == []
    at.button(key=f"enter-{RUN_ID}").click().run()                                  # the same flow: the review, by hand
    assert R.BY_HAND in texts(at.info) and not at.get("button_group")
    assert unplain(at, *TS.exempt(failed(error=TIMEOUT), empty_entry(ORG, WELL), None, "tester")) == []


@SHOWN
def test_c_the_run_lists_ids_and_codes_and_its_cost_column(env, monkeypatch, show):  # noqa: F811
    turn(monkeypatch, show)
    run = run_id(600)
    write_run(env, run, record(run), FULL)
    at = app()
    assert ("Run ids and codes (technical)" in technical_sections(at)) == show
    assert ("Cost" in at.dataframe[0].value.columns) == show and len(at.dataframe) == (2 if show else 1)
    if not show:
        assert not at.get("expander") and unplain(at, "org-test", "well-a") == []


@SHOWN
def test_d_details_on_the_upload_page_are_drawn_or_logged_once(env, monkeypatch, caplog, show):  # noqa: F811
    turn(monkeypatch, show)
    W.athena_client().fail = RuntimeError("AccessDeniedException: an invented refusal")
    at = app(upload="1")
    at.run()                                                                        # a rerun: the same failure, kept
    assert texts(at.error) == [f"The live wells could not be read from the product, so the form is off. Retry; if it "
                               f"fails again, ask {NO_ONE}."]
    said = "RuntimeError: AccessDeniedException: an invented refusal"
    if show:
        assert technical_sections(at) == ["Details (technical)"] and said in texts(at.code)[0] and not logged(caplog)
    else:
        assert not at.get("expander") and said not in page(at) and unplain(at) == []
        assert logged(caplog) == [f"the live wells could not be read: {said}"]      # once, for the two runs


@SHOWN
def test_d_a_run_that_cannot_start_says_so_and_its_error_is_drawn_or_logged(env, popen, monkeypatch, caplog,  # noqa: F811
                                                                            show):
    turn(monkeypatch, show)

    def full(root, data):
        raise OSError("an invented fault: no space left")
    monkeypatch.setattr(intake, "save_pdf", full)
    at = app(upload="1")
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    at.checkbox(key="new-confirm").check().run()
    next(b for b in at.button if b.label == "Start the run").click().run()
    assert texts(at.error) == [f"The run could not be started. Try again; if it fails again, ask {NO_ONE}."]
    if show:
        assert "OSError: an invented fault: no space left" in texts(at.code)[-1] and not logged(caplog)
    else:
        assert not at.get("expander") and "OSError" not in page(at)
        assert logged(caplog) == ["a run could not be started: OSError: an invented fault: no space left"]
        assert unplain(at, *(n for pair in LIVE for n in pair), "Upload name.pdf") == []
    assert not popen


@SHOWN
def test_d_the_watch_pages_launch_log_and_run_record(env, monkeypatch, caplog, show):  # noqa: F811
    turn(monkeypatch, show)
    late, done = run_id(intake.STARTUP_S + 5), run_id(900)
    launch_log(env, late, "Traceback: ExtractionError INTAKE_INVALID\n")
    at = app(watch=late)
    at.run()
    assert any(e.startswith("The run did not start: nothing was heard from it for 120 s.") and e.endswith(
        f"tell {NO_ONE}.") for e in texts(at.error))
    assert technical_sections(at) == (["Launch log (technical)"] if show else [])
    write_run(env, done, record(done, status="failed", error={"code": "PREP_FAILED", "stage": "prep",
                                                              "message": "an invented message"}), {})
    at2 = app(watch=done)
    at2.run()
    assert technical_sections(at2) == (["Run record (technical)"] if show else [])
    if show:
        assert not logged(caplog)
        return
    assert "INTAKE_INVALID" not in page(at) and unplain(at) == [] and unplain(at2, "org-test", "well-a") == []
    assert logged(caplog) == [
        f"a run did not start: run {late}: nothing was heard from it for 120 s; what it printed is in the run store, "
        f"launch/{late}.log", f"run {done} failed: PREP_FAILED at prep: an invented message"]       # each once


@SHOWN
def test_d_the_could_not_read_screen(fake, monkeypatch, caplog, show):  # noqa: F811
    turn(monkeypatch, show)
    fake.fail["ListObjectsV2", TA.INTAKE] = TL.client_error(400, "ExpiredToken", "ListObjectsV2")
    at = app()
    assert texts(at.error) == [NOT_READ]
    if show:
        assert technical_sections(at) == ["Details (technical)"] and "aws sso login" in texts(at.code)[0]
        assert "ExpiredToken" in texts(at.code)[0] and not [m for m in logged(caplog) if "could not be shown" in m]
    else:
        assert not at.get("expander") and "ExpiredToken" not in page(at) and unplain(at) == []
        [line] = [r for r in caplog.records if r.name == hosted.LOG.name and "could not be shown" in r.getMessage()]
        assert "ClientError" in line.getMessage() and "ExpiredToken" in line.getMessage() and line.exc_info


@SHOWN
def test_d_a_role_that_cannot_be_assumed_is_the_could_not_read_screen(fake, monkeypatch, caplog, show):  # noqa: F811
    """The role is assumed when the store is made, before the page's own handling: a failure there (no credentials,
    the role refusing) is the same plain screen, never Streamlit's own error box."""
    turn(monkeypatch, show)
    monkeypatch.setenv(hosted.ROLE_ENV, ROLE)
    monkeypatch.setenv(hosted.EXTERNAL_ENV, "an-external-id")
    monkeypatch.setattr(S, "s3_client", S3_CLIENT)
    asked = []

    def refused(service, **kwargs):
        asked.append(service)
        raise NoCredentialsError()
    monkeypatch.setattr(boto3, "client", refused)
    at = app()
    assert texts(at.error) == [NOT_READ] and asked == ["sts"]
    if show:
        assert technical_sections(at) == ["Details (technical)"] and "NoCredentialsError" in texts(at.code)[0]
    else:
        assert not at.get("expander") and unplain(at) == []
        [line] = [r for r in caplog.records if r.name == hosted.LOG.name and "could not be shown" in r.getMessage()]
        assert "NoCredentialsError" in line.getMessage() and line.exc_info


@SHOWN
def test_e_the_sidebar_names_the_bucket_only_with_the_switch_on(fake, monkeypatch, show):  # noqa: F811
    turn(monkeypatch, show)
    run = TA.rid(1)
    TA.upload(fake, run)
    TA.stored_run(fake, run, record(run))                   # a record and no entry: a technical page, when shown
    at = app(run=run)
    said = texts(at.sidebar.caption)
    assert len(said) == 1 and (TA.BUCKET in said[0]) == show
    if not show:
        assert re.fullmatch(r"The run list is as of \d\d:\d\d UTC\. Refresh reads it again\.", said[0])
        assert TA.BUCKET not in page(at)
        assert [(b.label, b.help or None) for b in at.sidebar.button] == [("Refresh", None)]   # W9f: no tooltip, which
                                                                        # stays up while a pressed button keeps focus


@SHOWN
@pytest.mark.parametrize("case,names", [
    ("no store", ["ESP_RUN_STORE_DIR", "streamlit run"]),
    ("s3, a variable missing", ["ESP_BUCKET, ESP_INTAKE_PREFIX, ESP_RUNS_PREFIX"]),
    ("no such mode", ["ESP_APP_MODE", "must be local or s3"]),
    ("the role without its external id", [hosted.ROLE_ENV, hosted.EXTERNAL_ENV]),
    ("the external id without its role", [hosted.ROLE_ENV, hosted.EXTERNAL_ENV]),
])
def test_f_a_set_up_message_names_its_settings_only_with_the_switch_on(tmp_path, monkeypatch, caplog, show, case,
                                                                      names):
    turn(monkeypatch, show)
    for name in (S.STORE_ENV, *S.S3_ENV):
        monkeypatch.delenv(name, raising=False)
    if case == "s3, a variable missing":
        monkeypatch.setenv(S.MODE_ENV, "s3")
    elif case == "no such mode":
        monkeypatch.setenv(S.MODE_ENV, "aws")
    elif case != "no store":
        monkeypatch.setenv(S.STORE_ENV, str(tmp_path))      # a store that could be read
        monkeypatch.setenv(hosted.ROLE_ENV if case.startswith("the role") else hosted.EXTERNAL_ENV, "an-invented-value")
    at = app()
    at.run()
    shown_ = " ".join([*texts(at.info), *texts(at.error), *texts(at.code)])
    if show:
        assert all(name in shown_ for name in names) and NOT_SET_UP not in texts(at.error) and not logged(caplog)
    else:
        assert texts(at.error) == [NOT_SET_UP] and not at.info and not at.code and unplain(at) == []
        [line] = logged(caplog)                                                     # once, for the two runs
        assert line.startswith("the app is not set up: ") and all(name in line for name in names[:1])
        assert "an-invented-value" not in line


@SHOWN
def test_g_an_uncaught_errors_traceback(env, monkeypatch, caplog, show):  # noqa: F811
    turn(monkeypatch, show)

    def broken(self):
        raise RuntimeError("an invented fault in run_ids")
    monkeypatch.setattr(S.LocalRunStore, "run_ids", broken)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert st.get_option("client.showErrorDetails") == ("full" if show else "none")
    if show:                                                                        # local mode, as it was
        assert "an invented fault in run_ids" in at.exception[0].value and at.exception[0].stack_trace
    else:
        assert not at.exception and texts(at.error) == [NOT_READ] and unplain(at) == []
        [line] = [r for r in caplog.records if r.name == hosted.LOG.name]
        assert line.getMessage() == "the page could not be shown: RuntimeError: an invented fault in run_ids"
        assert line.exc_info and line.exc_info[0] is RuntimeError                   # the traceback: in the log


def test_what_is_hidden_is_logged_once_per_occurrence(caplog):
    error = RuntimeError("an invented fault")
    for _ in range(3):                                      # one exception, shown by three reruns
        hosted.hidden("a read", error)
    hosted.hidden("a read", RuntimeError("an invented fault"))                      # it happened again
    for _ in range(3):                                      # a failure that is not one exception object: by its key
        hosted.hidden("run x failed", "CODE at stage: a message", key=("run failed", "x"))
    hosted.hidden("run y failed", "CODE at stage: a message", key=("run failed", "y"))
    assert logged(caplog) == ["a read: RuntimeError: an invented fault"] * 2 + [
        "run x failed: CODE at stage: a message", "run y failed: CODE at stage: a message"]


def test_the_log_is_one_logger_to_standard_error():
    out = subprocess.run([sys.executable, "-c", "from app import hosted\n"
                          "hosted.hidden('a read', RuntimeError('an invented fault'))"],
                         cwd=LAB, capture_output=True, text=True, check=True)
    assert out.stdout == "" and out.stderr.rstrip().endswith(
        "ERROR esp_design_extraction.app: a read: RuntimeError: an invented fault")
    assert len(out.stderr.splitlines()) == 1 and len(hosted.LOG.handlers) == 1


# ---- R2: every page, with the switch off ----

def test_the_run_list_says_what_it_is_for_and_shows_no_id_or_code(env, off):  # noqa: F811
    assert "There are no runs yet. Start one with New run from a PDF." in texts(app().info)
    good, bad, launched = run_id(900), run_id(600), run_id(intake.STARTUP_S + 5)
    write_run(env, good, record(good), FULL)
    write_run(env, bad, record(bad, status="failed", error=TIMEOUT), {})
    launch_log(env, launched, "boom\n")
    at = app()
    intro = texts(at.markdown)[0]
    assert intro.startswith("Each run reads one ESP design document") and "New run from a PDF" in intro
    assert list(at.dataframe[0].value["Run"]) == ["launched, no record", "failed", "completed"]
    assert unplain(at, "org-test", "well-a") == []


@pytest.mark.parametrize("wells_", ["Live wells", "Test wells"])
def test_the_upload_page_on_either_list(env, off, wells_):  # noqa: F811
    at = app(upload="1")
    at.get("button_group")[0].set_value(wells_).run()
    at.selectbox(key="new-org").select_index(0).run()
    assert texts(at.info) == ["A run takes 3 to 8 minutes. When it finishes, you check what it read against the "
                              "document, and confirm it."] and not at.warning
    assert at.checkbox(key="new-confirm").label == "I've checked the organisation, the well and the document"
    assert not re.search(r"(?i)opus|bedrock|usd|\$|account|paid|model call", page(at))
    assert unplain(at, *(n for pair in (*LIVE, *TEST) for n in pair), R.LAB_CHECK) == []


def test_the_watch_page_in_each_state(env, off):  # noqa: F811
    launched = run_id()
    launch_log(env, launched)
    at = app(watch=launched)
    assert any(i.startswith("Starting: the run is getting under way. This page follows the run")
               for i in texts(at.info)) and unplain(at) == []
    write_run(env, launched, record(launched, status="running", ended_at=None), {})
    at.run()
    assert any(c.startswith("The document is being read, which takes 3 to 8 minutes.") for c in texts(at.caption))
    assert unplain(at, "org-test", "well-a") == []
    alive = run_id(intake.STARTUP_S + 30)
    launch_log(env, alive)
    intake.PROCESSES[alive] = FakeProcess(None)
    at = app(watch=alive)
    assert texts(at.info)[0].startswith("Still starting, after ") and unplain(at) == []


def test_the_watch_page_finished_and_failed(offline, off):  # noqa: F811
    at = app(watch=RUN_ID)                                  # it read the document: open it to review
    assert ("The document has been read. Open the run to check its values against the document, and confirm them."
            in texts(at.caption))
    assert [b.label for b in at.button if b.label == "Open the run"] and unplain(at, ORG, WELL, "design.pdf") == []
    no_entry(offline)                                       # failed, nothing read: by hand, and nothing to open
    at = app(watch=RUN_ID)
    assert at.button(key=f"enter-{RUN_ID}").label == "Enter by hand" and "Open the run" not in [
        b.label for b in at.button]
    assert unplain(at, ORG, WELL, "design.pdf") == []
    no_entry(offline, IMAGE_ONLY)
    at = app(watch=RUN_ID)
    assert SCAN_WORDS in texts(at.info) and R.OFFER in texts(at.caption)
    assert unplain(at, ORG, WELL, "design.pdf") == []


@pytest.mark.parametrize("age,word", [(timedelta(seconds=2), "Waiting to start: the upload is waiting its turn."),
                                      (timedelta(minutes=1), "Waiting to start, for 1 min so far: the upload is "
                                                             "waiting its turn."),
                                      (timedelta(minutes=10), "Not picked up: the upload has waited 10 min"),
                                      (timedelta(hours=7), "Expired: the upload waited 7 h 0 min")])
def test_the_watch_page_of_an_upload_that_waits(fake, off, caplog, age, word):  # noqa: F811
    run = TA.rid(1)
    TA.upload(fake, run, age=age)
    at = app(watch=run)
    said = [*texts(at.info), *texts(at.warning), *texts(at.error)]
    assert any(s.startswith(word) for s in said) and not at.get("expander")
    assert unplain(at, TA.ORG, TA.WELL, "doc.pdf") == []
    if word.startswith("Not picked up"):
        assert said[0].endswith(f"Upload the PDF again; if that upload is not picked up either, tell {NO_ONE}.")
        assert f"an upload was not picked up: run {run}, queued for 10 min" in logged(caplog)


def test_a_run_with_no_entry_in_each_state(offline, off, caplog):  # noqa: F811
    for error, said in ((IMAGE_ONLY, SCAN_WORDS), (TIMEOUT, R.failure_words(failed(error=TIMEOUT)))):
        no_entry(offline, error)
        at = app(run=RUN_ID)
        assert said in texts(at.info) and at.button(key=f"enter-{RUN_ID}") and not at.tabs
        assert unplain(at, ORG, WELL, "design.pdf") == []
    offline.path(C.pdf_key(RUN_ID)).unlink()                # nothing to enter it beside
    at = app(run=RUN_ID)
    assert not [b for b in at.button if b.key == f"enter-{RUN_ID}"]
    assert "Nothing was read from this document. Uploading the document again retries the extraction." in texts(at.info)
    assert unplain(at, ORG, WELL, "design.pdf") == []
    offline.path(C.run_key(RUN_ID, "run.json")).write_text("not a record")
    at = app(run=RUN_ID)
    assert texts(at.warning) == [f"This run's own record is missing or cannot be read, so there is nothing to show for "
                                 f"it. Upload the document again; if you need this run itself, tell {NO_ONE}."]
    assert unplain(at) == [] and any(m.startswith(f"run {RUN_ID}: ") for m in logged(caplog))
    at = app(run="20990101T000000Z-0000ffff")
    assert texts(at.warning) == ["There is no such run. Go back to all runs, and open one from the list."]
    assert unplain(at) == []
    assert texts(app(watch="20990101T000000Z-0000ffff").warning) == texts(at.warning)


def test_the_review_its_pop_ups_and_the_confirmed_view(offline, off):  # noqa: F811
    at = app(run=RUN_ID)
    assert any(c.startswith("Check each value below against the document beside it") for c in texts(at.caption))
    assert unplain(at, *review_names()) == []
    at.button(key=f"batch-{RUN_ID}").click().run()
    assert at.session_state["dialog"] == (RUN_ID, "found") and unplain(at, *review_names()) == []
    at.button(key=f"dlg-go-{RUN_ID}-found").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    assert at.session_state["dialog"] == (RUN_ID, "write") and unplain(at, *review_names()) == []
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to Live wells**") and not at.get("button_group")
    assert any(m.startswith("**Confirmed**: written to **Live wells** on ") for m in texts(at.markdown))
    assert texts(at.text) == ["tester"] and "Confirmed by" in texts(at.caption)
    assert any(c.startswith("This entry is written, and nothing here can change it. To change the well's entry, "
                            "start a new run from the same document") for c in texts(at.caption))
    assert unplain(at, *review_names()) == []


@pytest.mark.store
@pytest.mark.skipif(not os.environ.get(S.STORE_ENV), reason="the runs to render need ESP_RUN_STORE_DIR")
def test_every_run_of_the_store_is_plain_with_the_switch_off(off, monkeypatch):
    """The run list, then each run's own page (its review, its confirmed view, or the plain page of a run with nothing
    to review) and its watch page, each scanned whole.  The review's pop-ups and the editor are tests/test_screens.py's
    walk, which the switch does not change."""
    monkeypatch.setenv(R.OFFLINE_ENV, "reviewer")
    root, hits = Path(os.environ[S.STORE_ENV]), []
    try:
        models = list(TS.read_catalogue())
    except Exception:  # noqa: BLE001 - no snapshot: no model is shown
        models = []
    versions = [json.loads(p.read_text(encoding="utf-8")) for p in root.glob("design-records/*/versions/*.json")]
    store = S.local_store_from_env()
    names, per_run = set(), {}
    for rid in store.run_ids():
        try:
            rec = store.record(rid)
        except Exception:  # noqa: BLE001 - no record, or not readable: nothing of it to leave out
            rec = {}
        labels = store.intake_labels(rid) or {}
        stored = (root / "runs" / rid / C.ENTRY).is_file()
        entry = json.loads((root / "runs" / rid / C.ENTRY).read_text(encoding="utf-8")) if stored else None
        if entry is None and rec.get("organization_id") and rec.get("well_id"):
            entry = empty_entry(rec["organization_id"], rec["well_id"])
        pdf = root / "intake" / f"{((rec.get('source') or {}).get('pdf') or {}).get('sha256')}.pdf"
        scan = T.recognise(pdf.read_bytes()) if pdf.is_file() else None
        mine = [v for v in versions if (v.get("run") or {}).get("run_id") == rid]
        per_run[rid] = TS.exempt(rec, entry, mine[0] if mine else None, "reviewer", *models,
                                 *(v["confirmed_by"] for v in versions), *TS.typed_by_hand(versions),
                                 *(str(labels[k]) for k in ("organization-id", "well-id", "source-name") if labels.get(k)),
                                 *([scan.name] if scan and scan.name else []))
        names |= {str(v) for v in (rec.get("organization_id"), rec.get("well_id"), labels.get("organization-id"),
                                   labels.get("well-id"), R.document_name(rec),
                                   R.document_name({"source": {"source_name": labels.get("source-name")}})) if v}
    hits += [f"the run list · {hit}" for hit in unplain(app(), *names)]
    for rid, leave_out in per_run.items():
        for name, at in (("its page", app(run=rid)), ("its watch page", app(watch=rid))):
            assert not at.tabs and not technical_sections(at), (rid, name)
            hits += [f"{rid} · {name} · {hit}" for hit in unplain(at, *leave_out)]
    assert per_run and not hits, "\n".join(hits[:60]) + f"\n… {len(hits)} in all"


# ---- R3: whom to tell, and words for a hosted reader ----

def test_every_message_that_says_whom_to_tell_names_the_contact(monkeypatch, tmp_path):
    def broken():
        raise RuntimeError("no credentials")

    def said():
        return [R.app_fault(), R.files(), R.refusal_words("something the app has not seen"),
                R.failed_words({"status": "failed"}, entry_stored=False),
                R.outcome(C.LocalObjects(tmp_path), "20261031T201922Z-f43c2295", "prd",
                          {"status": C.STALE, "reasons": []})["note"],
                R.confirmer("s3", {}, caller=broken)[1],
                R.confirmer("s3", {hosted.USER_HEADER_ENV: HEADER}, headers=StreamlitHeaders([]))[1],
                *(R.refusal_words(r) for r in (
                    f"intake/{RUN_ID}.pdf is not there", f"intake/{RUN_ID}.pdf is not the document: its SHA-256 is x",
                    f"runs/{RUN_ID}/run.json is not there", "environment is 'prd'; the upload's is 'dev'",
                    "INTERNAL_ERROR at version: OSError: disk full", "contract: missing field pump_string"))]
    assert all(NO_ONE in s for s in said()) and hosted.contact() == NO_ONE
    monkeypatch.setenv(hosted.CONTACT_ENV, "  the well-data team (wells@example.com) ")
    assert hosted.contact() == "the well-data team (wells@example.com)"
    assert all("the well-data team (wells@example.com)" in s and NO_ONE not in s for s in said())
    assert TS.ids(said(), ["wells@example.com"]) == []


def test_the_pages_name_the_contact_as_it_is_set(env, monkeypatch, off):  # noqa: F811
    monkeypatch.setenv(hosted.CONTACT_ENV, "the well-data team (wells_1@example.com), in #esp-help")
    W.athena_client().fail = RuntimeError("an invented refusal")
    assert texts(app(upload="1").error) == ["The live wells could not be read from the product, so the form is off. "
                                            "Retry; if it fails again, ask the well-data team (wells_1@example.com), "
                                            "in #esp-help."]
    monkeypatch.delenv(S.STORE_ENV)
    assert texts(app().error) == ["This app is not set up yet, so it has nothing to show. Tell the well-data team "
                                  "(wells_1@example.com), in #esp-help."]


def test_no_message_names_whoever_runs_the_app_this_computer_or_an_aws_sign_in():
    """The old words are gone from the app; an AWS hint stays only as a technical detail."""
    for path in (LAB / "app").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for old in ("runs the app", "sign-in may have expired", "on this computer", "check its set-up", "paid model"):
            assert old not in source, (path.name, old)
        assert "arn:aws" not in source and not re.search(r"\b\d{12}\b", source.replace(W.OUTPUT_DEFAULT, "")), path.name


# ---- R4: the one AWS session ----

class FakeSts:
    """STS as the builder calls it: each AssumeRole answers credentials numbered by the call, living `lifetimes[n]`."""

    def __init__(self, *lifetimes):
        self.calls, self.lifetimes = [], lifetimes

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        n = len(self.calls)
        return {"Credentials": {"AccessKeyId": f"key-{n}", "SecretAccessKey": f"secret-{n}", "SessionToken": f"token-{n}",
                                "Expiration": datetime.now(timezone.utc) + self.lifetimes[n - 1]}}


def no_aws(*args, **kwargs):
    raise AssertionError(f"an AWS call was about to be made: {args} {kwargs}")


def test_both_settings_assume_the_role_with_the_external_id_in_us_east_1_and_refresh_before_expiry(monkeypatch):
    sts, made = FakeSts(timedelta(minutes=5), timedelta(hours=1)), []
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: made.append((service, kwargs)) or sts)
    environ = {hosted.ROLE_ENV: ROLE, hosted.EXTERNAL_ENV: "an-external-id"}
    session = hosted.session(environ)
    assert made == [("sts", {"region_name": "us-east-1"})]
    assert sts.calls == [{"RoleArn": ROLE, "RoleSessionName": "esp-design-extraction-app",
                          "ExternalId": "an-external-id"}]
    assert session.region_name == "us-east-1"
    first = session.get_credentials()
    assert first.method == "sts-assume-role"
    frozen = first.get_frozen_credentials()                 # five minutes from expiry: refreshed before they are used
    assert (frozen.access_key, frozen.secret_key, frozen.token) == ("key-2", "secret-2", "token-2")
    assert sts.calls == [sts.calls[0]] * 2
    assert first.get_frozen_credentials().access_key == "key-2" and len(sts.calls) == 2     # an hour left: kept
    for service in ("s3", "athena"):
        assert session.client(service).meta.region_name == "us-east-1"
    assert hosted.session(environ) is session and len(sts.calls) == 2                       # one session a process


def test_neither_setting_is_boto3s_default_chain_in_us_east_1(monkeypatch):
    monkeypatch.setattr(boto3, "client", no_aws)            # no STS
    session = hosted.session({})
    assert isinstance(session, boto3.Session) and session.region_name == "us-east-1"
    assert session._session._credentials is None            # nothing set: botocore's own chain resolves them
    assert hosted.session({hosted.ROLE_ENV: " ", hosted.EXTERNAL_ENV: ""}) is session


@pytest.mark.parametrize("name", [hosted.ROLE_ENV, hosted.EXTERNAL_ENV])
def test_one_setting_without_the_other_is_refused_before_any_aws_call(monkeypatch, name):
    monkeypatch.setattr(boto3, "client", no_aws)
    monkeypatch.setattr(boto3, "Session", no_aws)
    with pytest.raises(hosted.SetUp, match=f"{hosted.ROLE_ENV} and {hosted.EXTERNAL_ENV} go together"):
        hosted.session({name: "an-invented-value"})
    monkeypatch.setenv(name, "an-invented-value")
    for client in (S.s3_client, ATHENA_CLIENT):
        with pytest.raises(hosted.SetUp) as err:
            client()
        assert "an-invented-value" not in str(err.value)    # which setting, never its value


@pytest.mark.parametrize("mode", ["local", "s3"])
@pytest.mark.parametrize("name", [hosted.ROLE_ENV, hosted.EXTERNAL_ENV])
def test_one_setting_alone_is_a_plain_set_up_message_logged_with_no_aws_call(off, monkeypatch, caplog, tmp_path, name,
                                                                             mode):
    for key, value in {S.MODE_ENV: mode, S.STORE_ENV: str(tmp_path), "ESP_BUCKET": TA.BUCKET,
                       "ESP_INTAKE_PREFIX": TA.INTAKE, "ESP_RUNS_PREFIX": TA.RUNS, name: "an-invented-value"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(S, "_S3_STORES", {})
    monkeypatch.setattr(boto3, "client", no_aws)            # the app's own clients, unpatched: any AWS call would
    monkeypatch.setattr(boto3, "Session", no_aws)           # raise on the page
    monkeypatch.setattr(W, "athena_client", ATHENA_CLIENT)
    for params in ({}, {"upload": "1"}, {"run": RUN_ID}):
        at = app(**params)
        assert texts(at.error) == [NOT_SET_UP] and unplain(at) == []
    [line] = logged(caplog)
    assert line.startswith(f"the app is not set up: {hosted.ROLE_ENV} and {hosted.EXTERNAL_ENV} go together")


def test_the_s3_client_and_the_athena_client_come_from_the_one_session(monkeypatch):
    asked = []

    class Session:
        def client(self, service, **kwargs):
            asked.append((service, kwargs))
            return service
    monkeypatch.setattr(hosted, "session", Session)
    assert (S.s3_client(), ATHENA_CLIENT()) == ("s3", "athena")
    assert asked == [("s3", {}), ("athena", {"region_name": "us-east-1"})]


# ---- R5: who confirmed ----

def headers(*pairs):
    return StreamlitHeaders(pairs)


def never():
    raise AssertionError("behind a sign-in, STS is never asked who confirms")


def test_who_confirms_is_the_sign_in_headers_value_exactly_and_nothing_else(tmp_path):
    environ = {hosted.USER_HEADER_ENV: HEADER}
    assert R.confirmer("s3", environ, caller=never, headers=headers((HEADER, USER))) == (USER, "")
    assert R.confirmer("s3", environ, caller=never, headers=headers(("x-forwarded-user", USER)))[0] == USER
    assert R.confirmer("s3", environ, caller=never, headers=headers((HEADER, " Engineer One ")))[0] == " Engineer One "
    for sent in ([], [(HEADER, "")], [(HEADER, "   ")], [(HEADER, USER), (HEADER, "another.one@example.com")],
                 [("Cookie", f"{HEADER}={USER}")], [("X-Remote-User", USER)], [("Authorization", USER)]):
        assert R.confirmer("s3", environ, caller=never, headers=headers(*sent)) == (None, NOT_TOLD), sent
    assert R.confirmer("s3", environ, caller=never) == (None, NOT_TOLD)
    name, note = R.confirmer("s3", environ, caller=never, headers=headers(), technical=True)
    assert name is None and note.startswith(NOT_TOLD) and HEADER in note and hosted.USER_HEADER_ENV in note
    # local mode writes only when it is set to, and then as the signed-in user, never as that setting's value
    assert R.confirmer("local", environ, tmp_path, headers=headers((HEADER, USER))) == (
        None, "This copy of the app only reviews: Write is off.")
    offline_ = {**environ, R.OFFLINE_ENV: "tester"}
    assert R.confirmer("local", offline_, tmp_path, headers=headers((HEADER, USER)))[0] == USER
    assert R.confirmer("local", offline_, tmp_path, headers=headers()) == (None, NOT_TOLD)
    assert R.confirmer("local", offline_, R.CLI.LAB / "store", headers=headers((HEADER, USER)))[0] is None


def test_with_the_setting_unset_who_confirms_is_as_before(tmp_path):
    sent = headers((HEADER, USER))
    assert R.confirmer("s3", {}, caller=lambda: "analyst-1", headers=sent) == ("analyst-1", "")
    assert R.confirmer("s3", {hosted.USER_HEADER_ENV: "  "}, caller=lambda: "analyst-1", headers=sent)[0] == "analyst-1"
    assert R.confirmer("local", {R.OFFLINE_ENV: "tester"}, tmp_path, headers=sent)[0] == "tester"
    assert R.confirmer("local", {}, tmp_path, headers=sent)[0] is None


def signed_in(monkeypatch, *pairs):
    monkeypatch.setenv(hosted.USER_HEADER_ENV, HEADER)
    monkeypatch.setattr(hosted, "request_headers", lambda: StreamlitHeaders(pairs))


def test_the_signed_in_user_goes_in_the_request_and_its_history_and_nowhere_else(s3_mode, off, monkeypatch,  # noqa: F811
                                                                              caplog):
    monkeypatch.setattr(CLI, "caller", never)
    signed_in(monkeypatch, (HEADER, USER))
    at = app(run=RUN_ID)
    assert texts(at.text) == [USER] and unplain(at, *review_names(USER)) == []
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-found").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    assert texts(at.text) == [USER, USER]                                           # the header's, the Write pop-up's
    assert unplain(at, *review_names(USER)) == []
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.success)[0].startswith("**Written to Live wells**")
    [key] = [k for k in s3_mode.objects if k.endswith("/request.json")]
    q = C.load(s3_mode.objects[key])
    assert q["confirmed_by"] == USER
    assert {s["by"] for v in (*q["values"], q["pump_string"]) for s in v["history"]} == {"extraction", USER}
    assert USER not in " ".join(logged(caplog)) and not [k for k in s3_mode.objects if USER in k]
    assert not [name for name, value in os.environ.items() if USER in value]


def test_with_no_signed_in_user_write_stays_off_and_nothing_else_names_one(s3_mode, off, monkeypatch,  # noqa: F811
                                                                       caplog):
    monkeypatch.setenv(hosted.USER_HEADER_ENV, HEADER)      # read from the session's own request, which carries none
    at = AppTest.from_file(APP, default_timeout=60)
    for name, value in {"run": RUN_ID, HEADER: USER, "user": USER, "confirmed_by": USER}.items():
        at.query_params[name] = value                       # a query parameter does not count
    at.run()
    settle_all(at)
    assert write_button(at).disabled and f"{NOT_TOLD}" in texts(at.caption)
    assert texts(at.text) == ["no one signed in"]
    assert USER not in page(at) and "analyst-1" not in page(at)                    # nor the AWS session's name
    assert unplain(at, *review_names()) == []
    assert not [k for k in s3_mode.objects if k.endswith("/request.json")]
    assert [m for m in logged(caplog) if m.startswith("who is confirming: The request carried no ")]


# ---- R6: the pump catalogue ----

KEY = C.ASSETS_PREFIX + stage.CATALOGUE


def snapshot(models=CATALOGUE) -> bytes:
    """A small catalogue snapshot, in the two columns the app reads."""
    out = io.BytesIO()
    pq.write_table(pa.table({"pump_model_canonical": list(models),
                             "curve_basis": ["head" if has_curve else None for has_curve in models.values()]}), out)
    return out.getvalue()


def recorded(monkeypatch, tmp_path, data: bytes):
    """The stage manifest, recording this snapshot's SHA-256 as the real one records the real snapshot's."""
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / "MANIFEST.txt").write_text(f"{sha}  {sha}  {stage.CATALOGUE}\n")
    monkeypatch.setattr(stage, "MANIFEST", tmp_path / "MANIFEST.txt")


@pytest.fixture
def nowhere(monkeypatch, tmp_path):
    """No assets folder is named, and the system's temporary folder is the test's own."""
    monkeypatch.setenv(ASSETS_ENV, "")                      # names none; set, so that it is restored after the test,
                                                            # whatever a fetch makes of it
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    return tmp_path / "tmp"


def test_the_catalogue_is_fetched_once_checked_and_is_the_only_object_read(nowhere, monkeypatch, tmp_path):
    data, bucket = snapshot(), TL.FakeS3()
    recorded(monkeypatch, tmp_path, data)
    bucket.objects.update({KEY: data, C.ASSETS_PREFIX + "another-asset.csv": b"never read"})
    path = hosted.fetch_catalogue(bucket, TL.BUCKET)
    assert path == Path(os.environ[ASSETS_ENV]) / stage.CATALOGUE and path.read_bytes() == data
    assert path.parent.parent == nowhere and stage.asset(stage.CATALOGUE) == path   # the loader's own check passes
    assert hosted.fetch_catalogue(bucket, TL.BUCKET) is None                        # there: not fetched again
    assert bucket.calls == [("GetObject", KEY)]


def test_a_catalogue_that_is_not_the_manifests_or_is_not_there_is_refused_and_nothing_is_kept(nowhere):
    bucket = TL.FakeS3()
    bucket.objects[KEY] = b"not the snapshot the manifest records"
    with pytest.raises(ExtractionError) as refused:
        hosted.fetch_catalogue(bucket, TL.BUCKET)
    assert refused.value.code == "ASSET_INVALID" and ASSETS_ENV not in os.environ and not list(nowhere.iterdir())
    del bucket.objects[KEY]
    with pytest.raises(ClientError):
        hosted.fetch_catalogue(bucket, TL.BUCKET)
    assert ASSETS_ENV not in os.environ and not list(nowhere.iterdir())
    assert bucket.calls == [("GetObject", KEY)] * 2


@pytest.fixture
def bucket(monkeypatch, nowhere, fresh_catalogue):  # noqa: F811
    """S3 mode on a fake bucket that holds the synthetic run; the app has no catalogue but the one it fetches."""
    held = Triggered()
    put_run(C.S3Objects(held), to_check())
    for name, value in {S.MODE_ENV: "s3", "ESP_BUCKET": TL.BUCKET, "ESP_INTAKE_PREFIX": TL.INTAKE,
                        "ESP_RUNS_PREFIX": TL.RUNS}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(S, "s3_client", lambda: held)
    monkeypatch.setattr(S, "_S3_STORES", {})
    monkeypatch.setattr(CLI, "caller", lambda: "analyst-1")
    return held


@pytest.mark.parametrize("held", [None, b"not the snapshot the manifest records"], ids=["missing", "hash mismatch"])
def test_without_the_catalogue_the_review_says_so_in_plain_words(bucket, off, caplog, held):
    if held is not None:
        bucket.add(KEY, held)
    at = settle_all(app(run=RUN_ID))
    assert NO_CATALOGUE in texts(at.caption) and write_button(at).disabled and SETTLED in texts(at.caption)
    assert unplain(at, *review_names()) == [] and ASSETS_ENV not in os.environ
    fetches = [m for m in logged(caplog) if m.startswith("the pump catalogue could not be fetched: ")]
    assert len(fetches) == 1 and ("ExtractionError" if held else "ClientError") in fetches[0]   # once a session
    assert [c for c in bucket.calls if c[1].startswith(C.ASSETS_PREFIX)] == [("GetObject", KEY)]


def test_the_app_fetches_the_catalogue_once_for_the_process_and_checks_models_against_it(bucket, off, monkeypatch,
                                                                                         tmp_path):
    data = snapshot()
    recorded(monkeypatch, tmp_path, data)
    bucket.add(KEY, data)
    bucket.add(C.ASSETS_PREFIX + "another-asset.csv", b"never read")
    for _ in range(2):                                      # two sessions of one process
        at = app(run=RUN_ID)
        assert NO_CATALOGUE not in texts(at.caption)
        assert any(c.endswith("Every model is a pump catalogue name.") for c in texts(at.caption))
    assert [c for c in bucket.calls if c[1].startswith(C.ASSETS_PREFIX)] == [("GetObject", KEY)]
    assert Path(os.environ[ASSETS_ENV], stage.CATALOGUE).read_bytes() == data


def test_a_named_assets_folder_is_left_as_it_is(bucket, off, monkeypatch, tmp_path):
    monkeypatch.setenv(ASSETS_ENV, str(tmp_path / "named"))
    app(run=RUN_ID)
    assert os.environ[ASSETS_ENV] == str(tmp_path / "named")
    assert not [c for c in bucket.calls if c[1].startswith(C.ASSETS_PREFIX)]


# ---- R7: the fonts ----

CONFIG, FONTS = LAB / ".streamlit" / "config.toml", LAB / "app" / "static" / "fonts"
FACES = {("Barlow", 400), ("Barlow", 500), ("Barlow", 600), ("Barlow Semi Condensed", 500),
         ("Barlow Semi Condensed", 600), ("IBM Plex Mono", 400), ("IBM Plex Mono", 500)}


def test_the_config_names_no_outside_address_and_serves_the_bundled_fonts():
    text = CONFIG.read_text(encoding="utf-8")
    assert not re.search(r"(?i)https?:|//|\bwww\.|googleapis|gstatic", text)
    config = tomllib.loads(text)
    assert config["server"] == {"address": "localhost", "enableStaticServing": True}
    assert config["browser"] == {"gatherUsageStats": False} and config["client"] == {"toolbarMode": "minimal"}
    theme = config["theme"]
    assert (theme["font"], theme["headingFont"], theme["codeFont"]) == (
        "Barlow, sans-serif", "Barlow Semi Condensed, sans-serif", "IBM Plex Mono, monospace")
    assert (theme["primaryColor"], theme["backgroundColor"], theme["baseRadius"]) == ("#1D4F7A", "#F6F7F4", "small")
    assert {(f["family"], f["weight"]) for f in theme["fontFaces"]} == FACES and len(theme["fontFaces"]) == len(FACES)
    for face in theme["fontFaces"]:                         # app/static/ is the folder beside app/main.py
        assert face["url"].startswith("app/static/fonts/") and (LAB / face["url"]).is_file(), face


def test_each_bundled_font_is_the_file_its_manifest_records_beside_its_licence():
    lines = [line.split() for line in (FONTS / "MANIFEST.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    files = {str(p.relative_to(FONTS)) for p in FONTS.rglob("*") if p.is_file() and not p.name.startswith(".")}
    assert {name for _, name, _ in lines} == files - {"MANIFEST.txt"} and len(lines) == 10
    for sha, name, url in lines:
        assert hashlib.sha256((FONTS / name).read_bytes()).hexdigest() == sha, name
        assert url == f"https://raw.githubusercontent.com/google/fonts/main/ofl/{name}"
    for family in (p for p in FONTS.iterdir() if p.is_dir()):
        assert "SIL Open Font License, Version 1.1" in (family / "OFL.txt").read_text(encoding="utf-8")
        assert [p for p in family.iterdir() if p.suffix == ".ttf" and p.read_bytes()[:4] == b"\x00\x01\x00\x00"]


# ---- the follow-up: a role with no sign-in header, who confirms as text, labels in an engineer's words ----

ROLE_SET = {hosted.ROLE_ENV: ROLE, hosted.EXTERNAL_ENV: "an-external-id"}
NOT_ABLE = f"Write is off: this app is not set up to tell who is signed in. Tell {NO_ONE}."
OLD_WORDS = r"pulled out|\bcarried\b|by the extraction|\bnobody\b|stale"


def test_a_role_with_no_sign_in_header_named_keeps_write_off_in_either_mode_and_asks_sts_nothing(tmp_path, caplog):
    sent = headers((HEADER, USER))
    assert R.confirmer("s3", ROLE_SET, caller=never, headers=sent) == (None, NOT_ABLE)
    assert R.confirmer("local", {**ROLE_SET, R.OFFLINE_ENV: "tester"}, tmp_path, headers=sent) == (None, NOT_ABLE)
    assert R.confirmer("local", ROLE_SET, tmp_path) == (None, NOT_ABLE)
    [line] = logged(caplog)                                 # a set-up fault: once a process, not once a session
    name, note = R.confirmer("s3", ROLE_SET, caller=never, technical=True)
    assert name is None and note.startswith(f"{NOT_ABLE} ")
    for detail in (line, note):                             # the settings by name, never their values
        assert all(n in detail for n in (hosted.ROLE_ENV, hosted.EXTERNAL_ENV, hosted.USER_HEADER_ENV))
        assert ROLE not in detail and "an-external-id" not in detail
    signed = {**ROLE_SET, hosted.USER_HEADER_ENV: HEADER}   # a role and the header named: the header's rule
    assert R.confirmer("s3", signed, caller=never, headers=sent) == (USER, "")
    assert R.confirmer("s3", signed, caller=never, headers=headers()) == (None, NOT_TOLD)
    assert R.confirmer("s3", {}, caller=lambda: "analyst-1", headers=sent) == ("analyst-1", "")      # neither: as it was
    assert R.confirmer("local", {R.OFFLINE_ENV: "tester"}, tmp_path, headers=sent)[0] == "tester"


@SHOWN
@pytest.mark.parametrize("mode", ["s3", "local"])
def test_on_the_page_a_role_with_no_header_named_keeps_write_off(request, monkeypatch, show, mode):
    turn(monkeypatch, show)
    request.getfixturevalue("s3_mode" if mode == "s3" else "offline")
    for name, value in ROLE_SET.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(CLI, "caller", never)               # STS is not asked who confirms,
    monkeypatch.setattr(boto3, "client", no_aws)            # nor anything else
    at = settle_all(app(run=RUN_ID))
    assert write_button(at).disabled and texts(at.text) == ["no one signed in"]
    [said] = [c for c in texts(at.caption) if c.startswith("Write is off")]
    if show:
        assert said.startswith(f"{NOT_ABLE} ") and hosted.USER_HEADER_ENV in said
    else:
        assert said == NOT_ABLE and unplain(at, *review_names()) == []


def markdown_with(at, value) -> list[str]:
    """The page's strings that hold `value` and that Markdown draws: every element's but st.text's and a table's
    cells'.  Markdown makes a link of a bare address; those two show text as it is."""
    out = []
    for node in at._tree:
        if isinstance(node, Dataframe) or getattr(node, "type", "") == "text" or getattr(node, "proto", None) is None:
            continue
        out += [s for s in map(TS.normalise, TS.strings(MessageToDict(node.proto, preserving_proto_field_name=True)))
                if value in s]                              # as shown: Markdown's escapes undone
    return out


def test_who_confirms_and_who_confirmed_show_as_text_exactly_and_in_no_markdown(offline, off, monkeypatch):  # noqa: F811
    signed_in(monkeypatch, (HEADER, USER))                  # local mode, set to write: the signed-in user confirms
    at = app(run=RUN_ID)
    assert texts(at.text) == [USER] and markdown_with(at, USER) == []              # the review's "Confirming as"
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-found").click().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    assert texts(at.text) == [USER, USER] and markdown_with(at, USER) == []        # and the Write pop-up's
    assert texts(at.caption).count("Confirming as") == 2
    at.button(key=f"write-go-{RUN_ID}").click().run()
    assert texts(at.text) == [USER] and "Confirmed by" in texts(at.caption)        # the confirmed view
    assert markdown_with(at, USER) == []
    assert any(USER in str(cell) for cell in at.dataframe[0].value["how it was decided"])     # a table's cells: text
    rerun(offline, to_check())                              # a second run of the document: the earlier confirmation
    at = app(run=RERUN)
    name, before = texts(at.text)
    assert name == USER and re.fullmatch(rf"Confirmed before: 18 values come back from {re.escape(USER)}'s "
                                         r"confirmation of \d{4}-\d\d-\d\d \d\d:\d\d UTC\.", before)
    assert markdown_with(at, USER) == [] and any("kept from before" in m for m in texts(at.markdown))


def test_the_contacts_address_stays_where_markdown_makes_it_a_link(offline, off, monkeypatch):  # noqa: F811
    monkeypatch.setenv(hosted.CONTACT_ENV, "the well-data team (wells@example.com)")
    monkeypatch.setenv(hosted.USER_HEADER_ENV, HEADER)      # and no header: Write is off, and says whom to tell
    at = app(run=RUN_ID)
    assert markdown_with(at, "wells@example.com") and texts(at.text) == ["no one signed in"]


def pop_up_table(at) -> dict:
    table = next(d.value for d in at.dataframe if list(d.value.columns) == ["Values", "Count"])
    return dict(zip(table["Values"], table["Count"]))


def test_the_batches_the_badges_and_the_write_pop_up_use_no_word_of_the_apps_own(offline, off):  # noqa: F811
    at = app(run=RUN_ID)
    at.button(key=f"batch-{RUN_ID}").click().run()
    at.checkbox(key=f"tick-{RUN_ID}-found-oil_sg").uncheck().run()
    assert any(c.startswith("Unticked values are left out of this group check: they stay to check")
               for c in texts(at.caption))
    at.button(key=f"dlg-go-{RUN_ID}-found").click().run()
    for key in NOT_FOUND:
        at.checkbox(key=f"tick-{RUN_ID}-not found-{key}").uncheck().run()
    assert at.button(key=f"dlg-go-{RUN_ID}-not found").label == "Leave all 2 out"
    for key in NOT_FOUND:
        at.checkbox(key=f"tick-{RUN_ID}-not found-{key}").check().run()
    at.button(key=f"dlg-go-{RUN_ID}-not found").click().run()
    assert at.session_state["dialog"] == (RUN_ID, "write")
    assert pop_up_table(at) == {"settled in group checks": 4, "left out of a group check": 1,
                                "needed no check: kept as read": 13, "left to check": 1}    # no row with none
    assert any("left out of a group check" in m for m in texts(at.markdown))       # the value's badge
    assert not re.search(r"(?i)\bbatch", page(at))                                 # W8: no batch, numbered or not
    assert not re.search(OLD_WORDS, page(at)) and unplain(at, *review_names()) == []
    review = at.session_state[f"review-{RUN_ID}"]["review"]                        # what is recorded: as it was
    assert review["values"]["oil_sg"]["history"][-1]["reason"] == "batch 1 · found · pulled out"
    assert R.tally(review)["pulled out"] == 1 and R.tally(review)["settled by the extraction"] == 13


def test_a_second_run_says_kept_from_an_earlier_confirmation(offline, off):  # noqa: F811
    _, version = confirmed_first(offline)
    rerun(offline, to_check())
    at = app(run=RERUN)
    assert any("kept from before" in m for m in texts(at.markdown))
    at.session_state["dialog"] = (RERUN, "write")
    at.run()
    assert pop_up_table(at)["kept from an earlier confirmation"] == 18
    assert not re.search(OLD_WORDS, page(at))
    assert at.session_state[f"review-{RERUN}"]["review"]["values"]["oil_sg"]["history"][-1]["reason"] == (
        f"carried from {version['confirmation_id']}")                               # recorded: as it was
    said = "tester's confirmation of 2026-10-31 20:19 UTC"
    assert R.step_words(C.step(C.SETTLED, "tester", AT, f"carried from {version['confirmation_id']}"), said) == (
        f"kept from {said}")
    assert R.step_words(C.step(C.TO_CHECK, "tester", AT, "batch 2 · not found · pulled out")).startswith(
        "left out of a group check (values not found), by tester")
    assert R.step_words(C.step(C.SETTLED, "tester", AT, 'batch 2 · found · "These 2 values match."')).startswith(
        "settled in a group check (values found): “These 2 values match.”, by tester")


def test_a_run_that_stopped_without_finishing_and_the_elapsed_time_without_its_limit(env, monkeypatch):  # noqa: F811
    stopped = run_id(3600)
    write_run(env, stopped, record(stopped, status="running", deadline_at="2000-01-01T00:00:00Z", ended_at=None), {})
    for show in (False, True):
        turn(monkeypatch, show)
        at = app()
        assert list(at.dataframe[0].value["Run"]) == ["stopped without finishing"]
        assert at.multiselect(key="status").options == ["stopped without finishing"]         # the filter's word too
        watch = app(watch=stopped)
        assert "#### Run: stopped without finishing" in texts(watch.markdown)
        [elapsed] = [m.value for m in watch.metric if m.label == "Elapsed"]
        assert (" of " in elapsed) == show                  # its limit: only with the technical views
        if show:                                            # the technical table keeps the record's own words
            assert list(at.dataframe[1].value["Pipeline, raw"]) == ["failed (stale)"]
        else:
            assert "stale" not in page(at) and "stale" not in page(watch)
            assert unplain(watch, "org-test", "well-a") == []
