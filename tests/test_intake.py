"""Intake (app/wells.py, app/intake.py, the upload form, the watch view), the CLI's --run-id and
--source-name, chart grouping and the client-name check.  Invented ids only; no AWS, no real launch:
Popen is replaced wherever the app would start a process, and Athena by a fake (`lists`)."""

import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from google.protobuf.json_format import MessageToDict
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError
from streamlit.testing.v1.element_tree import Dataframe, Expander

import app.intake as intake
import app.wells as W
import test_screens as TS
from app.store import PDF_DIRS_ENV, STORE_ENV, LocalRunStore
from app.wells import WELL_LIST_ENV, SnapshotError, load
from extraction.__main__ import main
from extraction.assets_dir import ASSETS_ENV
import extraction.confirmation as C
from extraction.intake import decode_labels, optional_labels
from test_unit import F4_P1, F4_P2, make_pdf
from test_viewer import APP, FULL, PDF, PDF_SHA, curve, parquet, record, runs, texts, write_run

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
# The product's live well lists as the fake Athena returns them: WELLS's rows for prd, another organisation for dev.
LIVE = [("Org Alpha", "Well  #1"), ("Org Alpha", "well #2"), ("Org Alpha", "Well  #1"), ("org alpha", "Lower Well"),
        ("Beta Org", "B 1")]
TEST = [("Test Org", "T 1"), ("Test Org", "T 2")]
NEAR = ("The list holds names that differ only by case or by characters that don't show; the exact name you choose is "
        "sent.")
SPACES = "␣ marks a space that would not show."
# The form's words for a name the upload's labels refuse (extraction/intake.py takes printable values only), and the
# names of test_screens.PAIRS they refuse: with a no-break space, a tab, a zero-width space, an en and an em space.
NO_RUN = {"organisation": "No run can start for this organisation yet, because its name holds a character the pipeline "
                          "can't carry.",
          "well": "No run can start for this well yet, because its name holds a character the pipeline can't carry.",
          "both": "No run can start for this organisation and this well yet, because each name holds a character the "
                  "pipeline can't carry."}
REFUSED = sorted({name for pair in TS.PAIRS.values() for name in pair if not name.isprintable()})
TRY_AGAIN = "The run could not be started. Try again; if it fails again, ask whoever runs the app."


class FakeAthena:
    """Athena's client as app/wells.py calls it.  A query reads RUNNING at its first poll, then `state`; its rows come
    two a page under the header row, None as a NULL.  With `fail` set, starting a query raises it."""

    def __init__(self, lists):
        self.lists, self.calls, self.fail, self.state, self.polls = lists, [], None, "SUCCEEDED", {}

    def start_query_execution(self, **kwargs):
        self.calls.append(("start", kwargs))
        if self.fail:
            raise self.fail
        environment = next(e for e, source in W.SOURCES.items() if f'"{source}"' in kwargs["QueryString"])
        qid = f"query-{len(self.calls)}-{environment}"
        self.polls[qid] = 0
        return {"QueryExecutionId": qid}

    def get_query_execution(self, *, QueryExecutionId):
        self.calls.append(("poll", QueryExecutionId))
        self.polls[QueryExecutionId] += 1
        state = "RUNNING" if self.polls[QueryExecutionId] == 1 else self.state
        return {"QueryExecution": {"Status": {"State": state, "StateChangeReason": "an invented reason"}}}

    def get_query_results(self, *, QueryExecutionId, NextToken="0"):
        self.calls.append(("page", QueryExecutionId, NextToken))
        rows, start = [W.COLUMNS, *self.lists[QueryExecutionId.rsplit("-", 1)[1]]], int(NextToken)
        page = {"ResultSet": {"Rows": [{"Data": [{} if v is None else {"VarCharValue": v} for v in row]}
                                       for row in rows[start:start + 2]]}}
        return {**page, "NextToken": str(start + 2)} if start + 2 < len(rows) else page

    def started(self) -> list[str]:
        """The environment of each query started, in order."""
        return [e for c in self.calls if c[0] == "start" for e, source in W.SOURCES.items()
                if f'"{source}"' in c[1]["QueryString"]]


def lists(monkeypatch, prd=LIVE, dev=TEST) -> FakeAthena:
    """The product's well lists, from a fake Athena, nothing kept from an earlier test and no wait between polls."""
    fake = FakeAthena({"prd": list(prd), "dev": list(dev)})
    monkeypatch.setattr(W, "athena_client", lambda: fake)
    monkeypatch.setattr(W, "_LISTS", {})
    monkeypatch.setattr(W, "POLL_S", 0)
    return fake


def plain_part(at) -> list[str]:
    """Every string the page shows outside its technical sections (an expander labelled `… (technical)`), read as
    tests/test_screens.py's `screen` reads a page."""
    out = []

    def walk(node):
        if isinstance(node, Expander) and node.label.endswith("(technical)"):
            return
        if isinstance(node, Dataframe):
            out.extend(TS.cells(node))
        elif getattr(node, "proto", None) is not None:
            out.extend(TS.strings(MessageToDict(node.proto, preserving_proto_field_name=True)))
        for child in getattr(node, "children", {}).values():
            walk(child)
    walk(at._tree)
    return [TS.normalise(t) for t in out]


def no_id_or_code(at, *names):
    """R5's check on the run list, the upload page and the watch page: outside the technical sections, no run id, hash,
    code, key or the words dev and prd, by tests/test_screens.py's patterns; `names` are the organisations, wells and
    document names the page may show, as they are and marked."""
    assert not at.exception, [e.value for e in at.exception]
    assert TS.ids(plain_part(at), sorted({*names, *map(W.marked, names)}, key=len, reverse=True)) == []


def every_string(value):
    if isinstance(value, dict):
        for inner in value.values():
            yield from every_string(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from every_string(inner)
    elif isinstance(value, str):
        yield value


def technical(at) -> str:
    """Everything the page's technical sections hold, code blocks and JSON included, as one text, Markdown undone."""
    return " ".join(TS.normalise(t) for e in at.get("expander") if e.label.endswith("(technical)") for node in e
                    if getattr(node, "proto", None) is not None
                    for t in every_string(MessageToDict(node.proto, preserving_proto_field_name=True)))


class FakeProcess:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "store"
    (root / "runs").mkdir(parents=True)
    monkeypatch.setenv(STORE_ENV, str(root))
    monkeypatch.delenv(WELL_LIST_ENV, raising=False)
    monkeypatch.delenv(PDF_DIRS_ENV, raising=False)
    monkeypatch.setattr(intake, "PROCESSES", {})
    lists(monkeypatch)
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

# ---- the live well lists ----

def test_a_list_is_one_query_polled_and_paged_with_its_values_exactly_as_returned(monkeypatch):
    rows = [("Org Alpha", "Well  #1"), ("Org Alpha", "Well  #1"), ("Org Alpha", "well  #1"), (" Org Alpha", "W 1 "),
            (None, "No Org"), ("Org Alpha", ""), ("", "x"), ("Beta Org", "B\t1")]
    fake = lists(monkeypatch, prd=rows)
    got = W.read("prd", client=fake, environ={})
    assert got.pairs == (("Org Alpha", "Well  #1"), ("Org Alpha", "well  #1"), (" Org Alpha", "W 1 "), ("Beta Org", "B\t1"))
    assert (got.environment, got.skipped) == ("prd", 3)                  # a NULL, then two empty values
    assert got.organisations() == [" Org Alpha", "Beta Org", "Org Alpha"]
    assert got.wells("Org Alpha") == ["Well  #1", "well  #1"]
    [start] = [c[1] for c in fake.calls if c[0] == "start"]
    assert start == {"QueryString": 'SELECT organization_id, well_id FROM "roam_prd_ddb"."default"'
                                    '."esp_well_configuration_v2"', "WorkGroup": "primary",
                     "ResultConfiguration": {"OutputLocation": "s3://aws-athena-query-results-640168431387-us-east-1/"}}
    assert [c[0] for c in fake.calls] == ["start", "poll", "poll", "page", "page", "page", "page", "page"]
    assert [c[2] for c in fake.calls if c[0] == "page"] == ["0", "2", "4", "6", "8"]       # 9 rows, the header first
    W.read("dev", client=fake, environ={W.OUTPUT_ENV: "s3://other-results/", W.WORKGROUP_ENV: "lab"})
    assert next(c[1] for c in reversed(fake.calls) if c[0] == "start") == {
        "QueryString": 'SELECT organization_id, well_id FROM "roam_dev_ddb"."default"."esp_well_configuration_v2"',
        "WorkGroup": "lab", "ResultConfiguration": {"OutputLocation": "s3://other-results/"}}
    fake.state = "FAILED"
    with pytest.raises(RuntimeError, match="FAILED: an invented reason"):
        W.read("prd", client=fake, environ={})


def test_names_that_differ_only_by_case_or_spacing_are_found_and_their_spacing_marked():
    names = ["Org Alpha", "org alpha", "Org Alpha ", " Org  Alpha", "Beta Org", "Beta\u00a0Org2", "Gamma", "Gamma\u200b"]
    assert W.near(names) == {"Org Alpha", "org alpha", "Org Alpha ", " Org  Alpha", "Gamma", "Gamma\u200b"}
    assert [W.marked(n) for n in names] == ["Org Alpha", "org alpha", "Org Alpha\u2423", "\u2423Org\u2423\u2423Alpha",
                                           "Beta Org", "Beta°Org2", "Gamma", "Gamma¦"]


def test_each_kind_of_character_that_does_not_show_has_its_own_sign_and_one_line_says_what_the_signs_shown_mean():
    """␣ a plain space only where it would not show (an edge, a run of two or more); ° a no-break space, → a tab,
    ▫ any other space character, ¦ a character with no width, wherever they are.  One sign per character."""
    assert W.marked(" Org  Alpha ") == "␣Org␣␣Alpha␣" and W.marked("Org Alpha") == "Org Alpha"
    others = {"\u00a0": "°", "\t": "→", "\u2002": "▫", "\u202f": "▫", "\u3000": "▫", "\n": "▫", "\x85": "▫",
              "\u200b": "¦", "\u200d": "¦", "\u2060": "¦", "\ufeff": "¦", "\u00ad": "¦", "\u200e": "¦", "\x01": "¦"}
    for char, sign in others.items():
        assert W.marked(f"{char}Org{char}Alpha{char}") == f"{sign}Org{sign}Alpha{sign}", repr(char)
    assert W.marked("Org \u00a0Alpha") == "Org °Alpha" and W.marked("\u200b Org") == "¦ Org"    # a space between
    assert W.marked("Org \u00a0 Alpha") == "Org ° Alpha"                                     # signs shows
    assert W.key(["Org Alpha", "Pad 90° → ¦"]) == ""                     # no sign shown; a sign written is no sign
    assert W.key(["Org\u00a0Alpha"]) == "° marks a no-break space."
    assert W.key([" Org", "Org\u00a0Alpha"]) == "␣ marks a space that would not show and ° a no-break space."
    assert W.key(["Org Alpha\u200b", "Org\u2002Alpha", "Org\tAlpha", "Org\u00a0Alpha", "Org  Alpha"]) == (
        "␣ marks a space that would not show, ° a no-break space, → a tab, ▫ another kind of space and ¦ a character "
        "with no width.")


def test_no_two_names_of_one_list_show_alike_even_where_two_mark_alike():
    """Two kinds of space behind ▫, or a sign written in a name, mark alike: each such name is followed by the
    characters behind its signs, in words; a name written as another's label is numbered."""
    assert W.labels(["Org\u2002Alpha", "Org\u2003Alpha", "Org Alpha ", "Org Alpha␣", "Org Alpha", "Org Alpha"]) == {
        "Org\u2002Alpha": "Org▫Alpha (en space)", "Org\u2003Alpha": "Org▫Alpha (em space)",
        "Org Alpha ": "Org Alpha␣ (space)", "Org Alpha␣": "Org Alpha␣ (open box)", "Org Alpha": "Org Alpha"}
    assert W.labels(["A\t\x01", "A→\x02"]) == {"A\t\x01": "A→¦ (tab, U+0001)", "A→\x02": "A→¦ (rightwards arrow, U+0002)"}
    assert W.labels(["A\u2002B", "A\u2003B", "A▫B (en space)"]) == {
        "A\u2002B": "A▫B (en space)", "A\u2003B": "A▫B (em space)", "A▫B (en space)": "A▫B (en space) (2)"}
    for pair in TS.PAIRS.values():
        assert len({TS.seen(label) for label in W.labels(pair).values()}) == 2, pair


def test_each_list_is_read_once_until_refresh(env):
    fake = W.athena_client()
    at = app(upload="1")
    assert fake.started() == ["prd"] and at.get("button_group")[0].value == "Live wells"
    [said] = [c for c in texts(at.caption) if c.startswith("Live wells: ")]
    assert re.fullmatch(r"Live wells: 4 wells, read from the product at \d\d:\d\d UTC on \d{1,2} [A-Z][a-z]+ \d{4}\.", said)
    at.run()
    app(upload="1")                                                      # another page: the list is kept
    assert fake.started() == ["prd"]
    at.get("button_group")[0].set_value("Test wells").run()
    at.get("button_group")[0].set_value("Live wells").run()
    at.get("button_group")[0].set_value("Test wells").run()
    assert fake.started() == ["prd", "dev"]
    at.button(key="new-refresh").click().run()                           # the list shown, read again
    assert fake.started() == ["prd", "dev", "dev"]


def test_a_failed_read_keeps_the_form_off_until_retry_and_never_falls_back_to_the_csv(env, monkeypatch, tmp_path):
    fake = W.athena_client()
    fake.fail = RuntimeError("AccessDeniedException: an invented refusal")
    (tmp_path / "wells.csv").write_text('"organization_id","well_id"\n"Csv Org","Csv Well"\n')
    monkeypatch.setenv(WELL_LIST_ENV, str(tmp_path / "wells.csv"))      # set, and never read
    at = app(upload="1")
    assert texts(at.error) == ["The live wells could not be read from the product, so the form is off. Retry; if it "
                               "fails again, ask whoever runs the app."]
    assert at.selectbox(key="new-org").disabled and at.selectbox(key="new-org").options == []
    assert at.file_uploader(key="new-pdf").disabled and next(b for b in at.button if b.label == "Start the run").disabled
    assert "RuntimeError: AccessDeniedException: an invented refusal" in technical(at)
    no_id_or_code(at)                                                    # the class and message: technical only
    at.run()
    assert fake.started() == ["prd"]                                     # the failure is kept until Retry
    fake.fail = None
    at.button(key="new-retry").click().run()
    assert not at.error and at.selectbox(key="new-org").options == ["Beta Org", "Org Alpha", "org alpha"]
    assert "Csv Org" not in " ".join(TS.screen(at)) and fake.started() == ["prd", "prd"]


# ---- the form ----

def test_form_lists_ids_exactly_and_marks_the_spacing_of_near_duplicates(env):
    at = app(upload="1")
    org = at.selectbox(key="new-org")
    assert org.options == ["Beta Org", "Org Alpha", "org alpha"]        # no lab-check among live wells
    assert NEAR in texts(at.caption)                                     # Org Alpha, org alpha: case only
    org.select_index(1).run()
    assert at.selectbox(key="new-org").value == "Org Alpha"
    well = at.selectbox(key="new-well")
    assert well.options == ["Well\u2423\u2423#1", "well #2"]          # the double space marked, as every screen marks it
    assert f"{NEAR} {SPACES}" in texts(at.caption)                       # one line: the organisations, and the sign
    well.select_index(0).run()
    assert at.selectbox(key="new-well").value == "Well  #1"


def test_near_duplicates_show_their_spacing_and_the_exact_one_is_sent(env, monkeypatch, popen):
    lists(monkeypatch, prd=[("Org Alpha", "Well 1"), ("Org Alpha ", "Well  1"), ("Org Alpha ", "well 1"),
                            ("Org Alpha ", "Well 2"), ("Beta  Org", "B 1")])
    at = app(upload="1")
    assert at.selectbox(key="new-org").options == ["Beta\u2423\u2423Org", "Org Alpha", "Org Alpha\u2423"]
    at.selectbox(key="new-org").set_value("Org Alpha ").run()
    assert at.selectbox(key="new-well").options == ["Well\u2423\u24231", "well 1", "Well 2"]
    assert [c for c in texts(at.caption) if SPACES in c] == [f"{NEAR} {SPACES}"]
    at.selectbox(key="new-well").set_value("Well  1").run()
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    no_id_or_code(at, "Org Alpha ", "Org Alpha", "Beta  Org", "Well  1", "well 1", "Well 1", "Well 2", "Upload name.pdf")
    at.checkbox(key="new-confirm").check().run()
    next(b for b in at.button if b.label == "Start the run").click().run()
    (args, _), = popen
    assert "--org=Org Alpha " in args and "--well=Well  1" in args


@pytest.mark.parametrize("box", ["new-org", "new-well"])
@pytest.mark.parametrize("pair", TS.PAIRS.values(), ids=list(TS.PAIRS))
def test_two_names_that_differ_only_by_what_does_not_show_never_show_alike_and_each_is_taken_exactly(
        env, monkeypatch, popen, pair, box):
    """Two organisations, or two wells of one organisation, that differ only by a character that doesn't show or by
    case show unalike, and the one chosen, by its place in the list, is exactly the one the form takes: the one it asks
    the upload's labels' rule about (`intake.refused`) and, when the labels take it, the one it sends them
    (`intake.labels`).  They take printable values only (extraction/intake.py), so a name with a tab, a space other
    than the plain one or a zero-width space starts no run, which the form says before any upload, Start off; any
    other starts its run, launched and labelled exactly."""
    lists(monkeypatch, prd=[(n, "Well 1") for n in pair] if box == "new-org" else [("Org Alpha", n) for n in pair])
    real_labels, real_refused, sent, asked, shown = intake.labels, intake.refused, [], [], set()
    monkeypatch.setattr(intake, "labels", lambda org, well, *rest: sent.append((org, well)) or real_labels(org, well,
                                                                                                           *rest))
    monkeypatch.setattr(intake, "refused", lambda *names, **file: (asked.append(names) if names else None)
                        or real_refused(*names, **file))                 # the names asked, not the file's
    flag, label = ("--org=", "organization-id") if box == "new-org" else ("--well=", "well-id")
    for i, name in enumerate(sorted(pair, key=W.display_order)):
        popen.clear()
        sent.clear()
        at = app(upload="1")
        if box == "new-well":
            at.selectbox(key="new-org").set_value("Org Alpha").run()
        shown |= {TS.seen(o) for o in at.selectbox(key=box).options}
        at.selectbox(key=box).select_index(i).run()
        if box == "new-org":
            at.selectbox(key="new-well").set_value("Well 1").run()
        at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
        assert asked[-1][box == "new-well"] == name                       # the one chosen, exactly
        if name.isprintable():
            at.checkbox(key="new-confirm").check().run()
            next(b for b in at.button if b.label == "Start the run").click().run()
            assert sent[-1][box == "new-well"] == name                    # and sent exactly
            (args, _), = popen
            run_id = next(a.split("=", 1)[1] for a in args if a.startswith("--run-id="))
            assert flag + name in args and LocalRunStore(env).intake_labels(run_id)[label] == name
        else:                                     # refused by the labels' own rule: said up front, and nothing sent
            assert texts(at.error) == [NO_RUN["organisation" if box == "new-org" else "well"]]
            assert next(b for b in at.button if b.label == "Start the run").disabled and not sent and not popen
    assert len(shown) == 2, shown                                        # never two alike as a person sees them


def unshown(name: str) -> str:
    """A test id for a refused name: its characters that are not printable, by code point."""
    return "+".join(f"U{ord(c):04X}" for c in name if not c.isprintable())


@pytest.mark.parametrize("box", ["new-org", "new-well"])
@pytest.mark.parametrize("name", REFUSED, ids=unshown)
def test_a_name_the_labels_refuse_is_said_in_plain_words_as_soon_as_it_is_chosen_and_start_stays_off(
        env, monkeypatch, popen, name, box):
    """Each name of test_screens.PAIRS the labels refuse, as an organisation or as a well: as soon as it is chosen,
    before any upload, the form says that no run can start for it, in plain words that show no id or code (not the
    name, no label, no error code), and Start stays off once the PDF is added; nothing is labelled, copied or launched,
    and no Try again."""
    pairs = [(name, "Well 1"), ("Org Alpha", "Well 1")] if box == "new-org" else [("Org Alpha", name),
                                                                                    ("Org Alpha", "Well 1")]
    lists(monkeypatch, prd=pairs)
    sent = []
    monkeypatch.setattr(intake, "labels", lambda *args: sent.append(args))
    message = NO_RUN["organisation" if box == "new-org" else "well"]
    at = app(upload="1")
    if box == "new-org":
        at.selectbox(key="new-org").select_index(sorted([name, "Org Alpha"], key=W.display_order).index(name)).run()
        assert texts(at.error) == [message]                               # before the well is chosen
        at.selectbox(key="new-well").set_value("Well 1").run()
    else:
        at.selectbox(key="new-org").set_value("Org Alpha").run()
        assert not at.error                                               # the organisation is taken
        at.selectbox(key="new-well").select_index(sorted([name, "Well 1"], key=W.display_order).index(name)).run()
    assert texts(at.error) == [message]                                   # before any upload
    assert TS.ids([message]) == [] and not any(word in message for word in (
        name, W.marked(name), "organization-id", "well-id", "label", "INTAKE_INVALID", "Try again"))
    no_id_or_code(at, name, "Org Alpha", "Well 1")
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    start = next(b for b in at.button if b.label == "Start the run")
    assert texts(at.error) == [message] and at.checkbox(key="new-confirm").disabled and start.disabled
    with pytest.raises(AppTestError):                                    # no one can press it
        start.click()
    no_id_or_code(at, name, "Org Alpha", "Well 1", "Upload name.pdf")
    assert not sent and not popen and not (env / "intake").exists()


PRINTABLE = {"plain": "Org Alpha", "edge-space": "Org Alpha ", "two-spaces": "Org  Alpha",
             "variation-selector": "Org Alpha\ufe0f"}


@pytest.mark.parametrize("box", ["new-org", "new-well"])
@pytest.mark.parametrize("name", PRINTABLE.values(), ids=list(PRINTABLE))
def test_a_printable_name_is_unaffected_even_one_shown_with_a_sign(env, monkeypatch, popen, name, box):
    """A name the labels take is unaffected: no message, Start on once the PDF is added and the cost accepted, and the
    run launched and labelled with exactly that name.  So is a name with a printable character that does not show, a
    variation selector, shown ¦: the labels' own rule decides, not the sign."""
    org, well = (name, "Well 1") if box == "new-org" else ("Org Alpha", name)
    lists(monkeypatch, prd=[(org, well)])
    at = app(upload="1")
    at.selectbox(key="new-org").select_index(0).run()
    at.selectbox(key="new-well").select_index(0).run()
    assert at.selectbox(key=box).options == [W.marked(name)] and not at.error
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    assert not at.error and not at.checkbox(key="new-confirm").disabled
    at.checkbox(key="new-confirm").check().run()
    next(b for b in at.button if b.label == "Start the run").click().run()
    (args, _), = popen
    run_id = next(a.split("=", 1)[1] for a in args if a.startswith("--run-id="))
    assert f"--org={org}" in args and f"--well={well}" in args
    assert LocalRunStore(env).intake_labels(run_id) == {"organization-id": org, "well-id": well,
                                                        "source-name": "Upload name.pdf", "sha256": PDF_SHA,
                                                        "environment": "prd"}


def test_a_pdf_whose_file_name_the_labels_refuse_is_said_once_added_and_start_stays_off(env, popen):
    """The PDF's file name goes as the upload's `source-name` label, under the same rule: a name with a narrow no-break
    space, as some systems put before AM or PM, is refused as soon as the file is added, in plain words that show no id
    or code, and Start stays off; nothing is labelled, copied or launched.  The same PDF under a plain name starts its
    run, labelled with exactly that name."""
    message = "This file's name holds a character the pipeline can't carry: rename the file and add it again."
    misnamed = "Design report 10.41\u202fAM.pdf"
    at = app(upload="1")
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    assert not at.error
    at.file_uploader(key="new-pdf").upload(misnamed, PDF, "application/pdf").run()
    start = next(b for b in at.button if b.label == "Start the run")
    assert texts(at.error) == [message] and at.checkbox(key="new-confirm").disabled and start.disabled
    with pytest.raises(AppTestError):                                    # no one can press it
        start.click()
    assert TS.ids([message]) == [] and not any(word in message for word in (
        misnamed, W.marked(misnamed), "source-name", "label", "INTAKE_INVALID", "Try again"))
    no_id_or_code(at, *{name for pair in LIVE for name in pair}, misnamed)              # every name the page lists
    assert not popen and not (env / "intake").exists()
    at.file_uploader(key="new-pdf").upload("Design report 10.41 AM.pdf", PDF, "application/pdf").run()
    assert not at.error
    at.checkbox(key="new-confirm").check().run()
    next(b for b in at.button if b.label == "Start the run").click().run()
    (args, _), = popen
    run_id = next(a.split("=", 1)[1] for a in args if a.startswith("--run-id="))
    assert "--source-name=Design report 10.41 AM.pdf" in args
    assert LocalRunStore(env).intake_labels(run_id)["source-name"] == "Design report 10.41 AM.pdf"


def test_the_message_says_which_name_it_is_the_organisation_the_well_or_both_and_a_typed_well_too(env, monkeypatch):
    """The message names what holds the character: the organisation, the well, or both; a well typed for lab-check is
    checked as a listed one is, and a printable one typed after it starts its run."""
    lists(monkeypatch, prd=[("Org\u00a0Alpha", "Well\t1"), ("Org\u00a0Alpha", "Well 2"), ("Org Alpha", "Well 3")])
    at = app(upload="1")
    at.selectbox(key="new-org").set_value("Org\u00a0Alpha").run()
    assert texts(at.error) == [NO_RUN["organisation"]]
    at.selectbox(key="new-well").set_value("Well\t1").run()
    assert texts(at.error) == [NO_RUN["both"]]
    at.selectbox(key="new-well").set_value("Well 2").run()
    assert texts(at.error) == [NO_RUN["organisation"]]
    at.get("button_group")[0].set_value("Test wells").run()
    at.selectbox(key="new-org").set_value("lab-check").run()
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    at.text_input(key="new-typed").input("Lab\u200bWell").run()
    assert texts(at.error) == [NO_RUN["well"]] and at.checkbox(key="new-confirm").disabled
    at.text_input(key="new-typed").input("Lab Well").run()
    assert not at.error and not at.checkbox(key="new-confirm").disabled


def test_the_form_shows_each_sign_and_one_line_says_what_each_means(env, monkeypatch):
    """The form: every name marked as the screens after it mark it, and one line for the signs shown."""
    lists(monkeypatch, prd=[("Org Alpha ", "W 1"), ("Org Alpha\u00a0", "W 1"), ("Org Alpha\t", "W 1"),
                            ("Org\u2002Alpha", "W 1"), ("Org Alpha\u200b", "W 1"), ("Plain Org", "W 1"),
                            ("Plain Org", "W\u2002\u2002"), ("Plain Org", "W\u3000\u2002")])
    at = app(upload="1")
    assert at.selectbox(key="new-org").options == ["Org Alpha→", "Org Alpha␣", "Org Alpha°", "Org Alpha¦", "Org▫Alpha",
                                                   "Plain Org"]                    # in code-point order
    line = (f"{NEAR} ␣ marks a space that would not show, ° a no-break space, → a tab, ▫ another kind of space and ¦ a "
            "character with no width.")
    assert line in texts(at.caption)
    at.selectbox(key="new-org").set_value("Plain Org").run()
    assert at.selectbox(key="new-well").options == ["W 1", "W▫▫ (en space, en space)", "W▫▫ (ideographic space, en space)"]
    assert [c for c in texts(at.caption) if "marks" in c] == [line]     # one line for both lists
    lists(monkeypatch, prd=[("Plain Org", "W 1"), ("Plain Org", "W\u2002")])
    at = app(upload="1")
    at.selectbox(key="new-org").set_value("Plain Org").run()
    assert at.selectbox(key="new-well").options == ["W 1", "W▫"]
    assert "▫ marks another kind of space." in texts(at.caption)          # no two names alike: the sign alone


def test_test_wells_add_lab_check_with_a_typed_well_and_the_upload_carries_dev(env, popen):
    at = app(upload="1")
    at.get("button_group")[0].set_value("Test wells").run()
    assert at.selectbox(key="new-org").options == ["lab-check", "Test Org"]
    at.selectbox(key="new-org").set_value("lab-check").run()
    assert not [s for s in at.selectbox if s.key == "new-well"]
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    at.text_input(key="new-typed").input("   ").run()
    assert at.checkbox(key="new-confirm").disabled                      # nothing typed but spaces: no well
    at.text_input(key="new-typed").input(" Lab  Well 7 ").run()
    at.checkbox(key="new-confirm").check().run()
    next(b for b in at.button if b.label == "Start the run").click().run()
    (args, _), = popen
    run_id = next(a.split("=", 1)[1] for a in args if a.startswith("--run-id="))
    assert "--org=lab-check" in args and "--well= Lab  Well 7 " in args             # exactly as typed
    assert LocalRunStore(env).intake_labels(run_id) == {
        "organization-id": "lab-check", "well-id": " Lab  Well 7 ", "source-name": "Upload name.pdf", "sha256": PDF_SHA,
        "environment": "dev"}


def test_the_switch_forgets_the_choice_made_on_the_other_list(env):
    at = app(upload="1")
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    at.get("button_group")[0].set_value("Test wells").run()
    assert at.selectbox(key="new-org").value is None and at.selectbox(key="new-well").disabled


@pytest.mark.parametrize("shown,environment", [("Live wells", "prd"), ("Test wells", "dev")])
def test_the_upload_records_its_environment_where_the_store_and_the_writer_read_it(env, popen, shown, environment):
    at = app(upload="1")
    at.get("button_group")[0].set_value(shown).run()
    org, well = ("Org Alpha", "Well  #1") if environment == "prd" else ("Test Org", "T 1")
    at.selectbox(key="new-org").set_value(org).run()
    at.selectbox(key="new-well").set_value(well).run()
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    at.checkbox(key="new-confirm").check().run()
    next(b for b in at.button if b.label == "Start the run").click().run()
    (args, _), = popen
    run_id = next(a.split("=", 1)[1] for a in args if a.startswith("--run-id="))
    meta = json.loads((env / "intake" / f".{run_id}.pdf.metadata.json").read_text())
    assert meta == intake.labels(org, well, "Upload name.pdf", PDF_SHA, environment)
    assert decode_labels(meta) == {"organization-id": org, "well-id": well, "source-name": "Upload name.pdf",
                                   "sha256": PDF_SHA}
    assert (env / "intake" / f"{run_id}.pdf").read_bytes() == PDF                # where the writer reads its labels
    assert LocalRunStore(env).intake_labels(run_id)["environment"] == environment
    assert optional_labels(C.LocalObjects(env).head(C.pdf_key(run_id))) == {"environment": environment}


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
    assert ("A run of this PDF for this organisation and well is still running, started 2099-01-01 00:00:00 UTC. You "
            "can start another.") in texts(at.warning)
    no_id_or_code(at, "Org Alpha", "org alpha", "Beta Org", "Well  #1", "well #2", "Upload name.pdf")
    at.checkbox(key="new-confirm").check().run()
    assert not next(b for b in at.button if b.label == "Start the run").disabled


@pytest.mark.parametrize("labelled,shown,warned", [
    (None, "Live wells", True), (None, "Test wells", False),          # no label: Org Alpha writes to prd, as before
    ("prd", "Live wells", True), ("prd", "Test wells", False),
    ("dev", "Live wells", False), ("dev", "Test wells", True),
])
def test_a_running_duplicate_is_one_in_the_uploads_environment_only(env, popen, monkeypatch, labelled, shown, warned):
    """A run writes where its upload's environment label says (`R.environment_for`): only a run of the same PDF,
    organisation and well writing to the same place is a duplicate."""
    lists(monkeypatch, dev=LIVE)                                     # both lists hold Org Alpha's Well  #1
    write_run(env, RUN, record(RUN, status="running", deadline_at="2999-01-01T00:00:00Z",
                               organization_id="Org Alpha", well_id="Well  #1"), {})
    if labelled:
        intake.label(env, RUN, PDF, intake.labels("Org Alpha", "Well  #1", "Other name.pdf", PDF_SHA, labelled))
    at = app(upload="1")
    at.get("button_group")[0].set_value(shown).run()
    at.selectbox(key="new-org").set_value("Org Alpha").run()
    at.selectbox(key="new-well").set_value("Well  #1").run()
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    assert [w for w in texts(at.warning) if "is still running" in w] == (
        ["A run of this PDF for this organisation and well is still running, started 2099-01-01 00:00:00 UTC. You can "
         "start another."] if warned else [])


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


def test_the_watch_page_marks_each_name_as_every_screen_does_and_says_what_the_signs_shown_mean(env):
    """The watch page: the names marked as the form, the run list and the review mark them, and one line
    for the signs shown."""
    launched = run_id()
    launch_log(env, launched)
    write_run(env, launched, record(launched, organization_id="Org\u00a0Alpha ", well_id="Well\t1\u200b"), {})
    at = app(watch=launched)
    assert any(c.startswith("Organisation Org°Alpha␣ · well Well→1¦ · started ") for c in texts(at.caption))
    assert "␣ marks a space that would not show, ° a no-break space, → a tab and ¦ a character with no width." \
           in texts(at.caption)
    no_id_or_code(at, "Org\u00a0Alpha ", "Well\t1\u200b")


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
    df = runs(app())
    assert df.loc[launched, "Pipeline"] == "launched, no record"
    assert any("Launch failed" in e for e in texts(app(run=launched).error))   # opening it watches it


# ---- no id or code on the run list, the upload page or the watch page ----

def test_the_run_list_names_runs_in_plain_words_and_keeps_the_raw_forms_technical(env):
    good, failed, running = "20990101T000001Z-0000000a", "20990101T000002Z-0000000b", "20990101T000003Z-0000000c"
    named = {"source": {"pdf": {"path": f"/nowhere/{PDF_SHA}.pdf", "sha256": PDF_SHA}, "report": None,
                        "source_name": "Upload name.pdf"}}
    write_run(env, good, record(good, **named), {})
    write_run(env, failed, record(failed, status="failed", error={"code": "MODEL_TIMEOUT", "stage": "extract",
                                                                    "message": "x", "details": {}}, **named), {})
    write_run(env, running, record(running, status="running", deadline_at="2999-01-01T00:00:00Z", **named), {})
    launched = run_id(5)
    launch_log(env, launched)
    at = app()
    df = runs(at)
    assert list(df.columns) == ["Open", "Organisation", "Well", "Document", "Started (UTC)", "Pipeline",
                                "Document review", "Latest", "Cost"]
    assert df.loc[[good, failed], "Cost"].tolist() == ["$0.50", "$0.50"] and df.loc[launched, "Cost"] == ""
    assert df.loc[[good, failed, running, launched], "Pipeline"].tolist() == ["completed", "failed", "running",
                                                                             "launched, no record"]
    assert df.loc[good, ["Open", "Organisation", "Well", "Document", "Started (UTC)"]].tolist() == [
        "Open", "org-test", "well-a", "Upload name.pdf", "2099-01-01 00:00:01"]
    raw = at.dataframe[1].value.set_index("Run id")
    assert (raw.loc[failed, "Error"], raw.loc[failed, "Pipeline, raw"]) == ("MODEL_TIMEOUT at extract",
                                                                            "failed at extract")
    assert raw.loc[good, "Family"].startswith("F4 (")
    no_id_or_code(at, "org-test", "well-a", "Upload name.pdf")
    assert at.session_state["runs-shown"] == list(raw.index)             # Open's row is a run id, in this order


def test_the_upload_page_on_either_list_and_the_watch_page_in_each_state_show_no_id_or_code(env, popen):
    names = ("Org Alpha", "org alpha", "Beta Org", "Well  #1", "well #2", "Test Org", "T 1", "T 2", "lab-check",
             "Upload name.pdf", "org-test", "well-a")
    at = app(upload="1")
    no_id_or_code(at, *names)
    at.get("button_group")[0].set_value("Test wells").run()
    no_id_or_code(at, *names)
    at.get("button_group")[0].set_value("Live wells").run()
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    at.file_uploader(key="new-pdf").upload("Upload name.pdf", PDF, "application/pdf").run()
    at.checkbox(key="new-confirm").check().run()
    no_id_or_code(at, *names)
    next(b for b in at.button if b.label == "Start the run").click().run()
    launched = next(a.split("=", 1)[1] for a in popen[0][0] if a.startswith("--run-id="))
    assert any("Starting" in i for i in texts(at.info))                  # watching it, by its names
    assert "Upload name.pdf" in texts(at.subheader) and launched not in " ".join(TS.screen(at))
    no_id_or_code(at, *names)
    source = {"pdf": {"path": f"/nowhere/{PDF_SHA}.pdf", "sha256": PDF_SHA}, "report": None,
              "source_name": "Upload name.pdf"}
    (env / "runs" / launched).mkdir()
    for rec in (record(launched, status="running", deadline_at="2999-01-01T00:00:00Z", ended_at=None, source=source),
                record(launched, source=source)):
        (env / "runs" / launched / "run.json").write_text(json.dumps(rec))
        at = app(watch=launched)
        no_id_or_code(at, *names)
    assert "Pipeline: completed" in " ".join(texts(at.markdown)) and launched in technical(at)
    late = run_id(intake.STARTUP_S + 5)
    launch_log(env, late, "Traceback: ExtractionError INTAKE_INVALID\n")
    at = app(watch=late)
    assert any("Launch failed" in e for e in texts(at.error)) and "INTAKE_INVALID" in technical(at)
    no_id_or_code(at)


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
