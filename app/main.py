"""Run viewer: every run in the run store, and each run in full; its upload form starts a run on a well from the
product's live well lists (app/wells.py), and a run's proposed well entry is reviewed and confirmed here.  The run list,
the upload form and the watch page name a run in plain words; ids and codes are under each one's technical section.
Every technical view and detail (Run details, run ids and codes, error text, set-up hints) is drawn only with
ESP_SHOW_TECHNICAL=1: without it every page is an engineer's, and what a page leaves out goes to the app's log
(app/hosted.py, which also holds the app's AWS session, who is signed in, and the pump catalogue's fetch).

    ESP_RUN_STORE_DIR=<run store dir> streamlit run app/main.py              # local mode, the default
    ESP_APP_MODE=s3 ESP_BUCKET=… ESP_INTAKE_PREFIX=… ESP_RUNS_PREFIX=… streamlit run app/main.py

Local mode reads a run store directory and starts a run as a process.  S3 mode reads the bucket the
Lambda writes and starts a run by uploading to its intake prefix (README, "Run the app against S3").
The store layout is the one `extraction/run.py` writes (README, "Running a document").
Each panel reads its own file and reports a missing, empty or malformed file in place,
so a failed run still shows everything it stored.

A run with a well_entry.json opens in "Review & confirm" until it has a written confirmation, then shows what was
written; its files are under "Run details".  A run that ended with no entry offers "Enter by hand" on its page: the
same review over the empty proposal, every value typed from the document.  The decisions are app/review.py's; S3 mode
writes through the writer Lambda, local mode only reviews unless ESP_CONFIRM_OFFLINE_AS names who writes into the
local store (README, "Confirming an entry").
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import pypdfium2 as pdfium
import streamlit as st

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:              # `streamlit run` puts only app/ on the path
    sys.path.insert(0, ROOT)

from app import hosted  # noqa: E402
from app import intake  # noqa: E402
from app import review as R  # noqa: E402
from app import transcription as T  # noqa: E402
from app import wells  # noqa: E402
from app.store import (MODE_ENV, PDF_DIRS_ENV, S3_ENV, STORE_ENV, local_store_from_env, run_status,  # noqa: E402
                       s3_store_from_env)
from extraction import confirmation as C  # noqa: E402
from extraction.catalogue import CatalogueNames  # noqa: E402
from extraction.stage.reports_reader import read_report  # noqa: E402
from extraction.stage.stage import FAMILY_LABEL  # noqa: E402
from extraction.validate.rules import page_numbers  # noqa: E402
from extraction.validate.validate import catalogue as read_catalogue  # noqa: E402
from extraction.well_entry import empty_entry  # noqa: E402

SHOW = hosted.technical()             # the technical views and details: drawn only with ESP_SHOW_TECHNICAL=1
FAMILIES = {code: slug for slug, code in FAMILY_LABEL.items()}
GRAINS = {"pump_config": "Pump string", "esp_well_design_context": "Design context",
          "curve_observations": "Curve observations"}
LOGS = {"d16_log": "Scenario-variance log", "d17_log": "Identity log", "gas_log": "Gas log"}
STAGES = ("detect", "prep", "assemble", "extract", "stage", "store", "record")
RUN_LOOK = {"succeeded": ("green", ":material/check_circle:"), "failed": ("red", ":material/error:"),
            "running": ("blue", ":material/hourglass_top:")}
REVIEW_LOOK = {"no issues": ("green", ":material/check_circle:"), "to check": ("orange", ":material/flag:")}
BANNER = ("Pipeline says whether extraction ran. Document review says whether the values need an engineer's "
          "check. A completed run can still need review.")
GENERIC_BADGE = "Generic extraction: review every value"
MODEL_LEVEL = {"absent": "not in the catalogue", "no_head_curve": "in the catalogue, without a head curve",
               "unresolved": "the printed name did not resolve to a model"}
SPEC_RANK = {"HALT": 0, "ERROR": 1, "WARN": 2, "NOTE": 3}
PANE = 720                            # px: the tabs and the source page scroll side by side

# Charts: the marker shape gives the point type, the colour the frequency.
SURFACE = "#F6F7F4"
MARKS = {  # point_type -> label, marker symbol, size
    "curve_point": ("curve point", "circle", 8),
    "design_point": ("design point", "diamond", 12),
    "bep": ("BEP", "star", 13),
    "shutoff": ("shutoff", "square", 10),
    "ror_min": ("ROR min", "triangle-right", 11),
    "ror_max": ("ROR max", "triangle-left", 11),
}
OTHER_MARK = "#8a948f"
# Charts group a curve's frequencies into one chart: a series per frequency, light to dark as it
# rises (the dataviz sequential blue, from step 250, which stays visible on the light surface).
CURVE = ["scenario_ordinal", "curve_observation_role", "pump_model", "sensitivity_case"]
FREQUENCY = "frequency_hz_as_printed"
RAMP = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
        "#0d366b"]
DURATION = ("A run takes 3 to 8 minutes. When it finishes, you check what it read against the document, and confirm "
            "it.")
FOLLOWS = "This page follows the run; you can leave it and find the run again in the run list."
POLL_S = 3
FINAL = {"launch failed", "succeeded", "failed", "stale", "expired"}
WAITING = ("queued", "not picked up", "expired")        # S3 mode: an upload with no run record yet
ORIGIN = "How it started"                              # the run list's column: an upload in the product, or the form here
SIDE = "Wells"                                         # the run list's column for where a run writes: Live or Test wells
RUN, NEWEST = "Run", "For its well"                    # the run list's columns: did the run read the document; the newest
WAIT_WORDS = {"queued": "waiting to start"}            # an upload not yet started, where `intake.queue_state` says queued
ORIGIN_ICONS = {R.AUTOMATIC: ":material/cloud_upload:", R.STARTED_HERE: ":material/upload_file:"}  # how a run started
WELL_LISTS = R.PLACES   # the form's switch, in the review's words for where a run writes: never dev or prd
NEAR = ("Some names in this list differ only by case or by characters that don't show. The product keeps them apart: "
        "choose the exact one you mean.")
STOPPED = "stopped without finishing"                  # a run still recorded as running past its deadline
NOBODY = "no one signed in"                            # after "Confirming as", when no name confirms
# The Write pop-up's rows: how each value ends this confirmation (`R.tally`'s names), in the engineer's words.
TALLY = {"settled one by one": "settled one by one", "settled in batches": "settled in group checks",
         "pulled out": "left out of a group check", "corrected": "corrected", "accepted anyway": "accepted anyway",
         "carried": "kept from an earlier confirmation", "settled by the extraction": "needed no check: kept as read",
         "left to check": "left to check"}
UNTOUCHED = "left empty: not from the document"        # entered by hand: the values that are never typed
TECH = "Details (technical)"                           # a plain screen's technical section, as Run details is: `tech`
REFUSED = {"organization-id": "organisation", "well-id": "well"}   # the form's words for a name the labels refuse
RENAME = "This file's name holds a character a run can't carry: rename the file and add it again."
# The run list: what it shows (a run's cost only with the technical views), then what its technical section shows
# (`list_rows`).
PLAIN_COLUMNS = ("Open", "Organisation", "Well", SIDE, "Document", RUN, "Document review", "Started", NEWEST, ORIGIN,
                 *(("Cost",) if SHOW else ()), "Waited")     # what a run is and where it stands first: the rest may
                                                             # need the table scrolled sideways
TECH_COLUMNS = ("Run id", "Started (UTC)", "Organisation", "Well", "Family", "Route", "Pipeline, raw", "Error",
                "Cost (USD)")
MODE = os.environ.get(MODE_ENV) or "local"
SECTIONS = ((True, "Whole string", "observation_is_composite = true"),
            (False, "Per section", "observation_is_composite = false"),
            (None, "Section not stated", "observation_is_composite is empty"))


class EmptyFile(Exception):
    pass


# ---- small helpers ----

def dig(value, *path):
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def text(value, missing="—") -> str:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return missing
    return json.dumps(value) if isinstance(value, (list, dict)) else str(value)


def wait_text(seconds) -> str:
    """A wait, coarsely: `40 s`, `12 min`, `6 h 5 min`."""
    s = max(0, int(seconds))
    return f"{s} s" if s < 60 else f"{s // 60} min" if s < 3600 else f"{s // 3600} h {s % 3600 // 60} min"


def plain(value) -> str:
    """Record text for a Markdown element, with the Markdown syntax escaped."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~$:])", r"\\\1", str(value))


CONTACT = plain(hosted.contact())     # whom to tell or ask when something goes wrong, in every message that says so
# A run that failed because its document is a scan.  The pipeline transcribes it by itself, and nothing tells the app
# whether that worked, so this says the same whatever became of the transcription.
IMAGE_ONLY = ("This document is a scan, so it has no text to read. The pipeline is transcribing it: the transcription "
              "arrives as a new run in the list, usually within a few minutes, and you review that one. If no "
              f"transcription has arrived within the hour, tell {CONTACT}.")


def tech(what, error, key=None, trace=False) -> bool:
    """Whether a page's technical detail is drawn.  With the switch off it is not, and `error` (the exception, or the
    detail in words) goes to the app's log instead, once per occurrence (`hosted.hidden`)."""
    if not SHOW:
        hosted.hidden(what, error, key, trace)
    return SHOW


def failed(err):
    """An error nothing else caught: credentials, network or permissions in S3 mode, a fault anywhere.  Plain words
    with whom to tell; the error and S3 mode's AWS hints are the page's technical section, and with the switch off go
    to the log with the traceback.  Local mode with the switch on: raised, so Streamlit shows its traceback."""
    if SHOW and MODE != "s3":
        raise err
    st.error(f"The run{'' if st.query_params.get('run') else 's'} could not be read. Reload the page; if it fails "
             f"again, tell {CONTACT}.")
    if tech("the page could not be shown", err, trace=True):
        with st.expander(TECH):
            st.code(f"S3 could not be read ({type(err).__name__}: {err}). Check the AWS credentials (AWS_PROFILE; an "
                    "expired SSO session needs `aws sso login`) and the bucket variables.", language=None,
                    wrap_lines=True)


def guarded(body):
    """A fragment's or a pop-up's body, which reruns outside the page's own handling: an error in it ends as
    `failed`."""
    @functools.wraps(body)
    def run(*args, **kwargs):
        try:
            return body(*args, **kwargs)
        except Exception as err:  # noqa: BLE001 - as the page's own: plain words, the rest technical
            failed(err)
    return run


def named(label, name):
    """Who confirms or confirmed, under its label, exactly as recorded and as plain text: `st.text` takes no Markdown,
    which would make an address a link."""
    with st.container(gap=None):
        st.caption(label)
        st.text(name)


def family(record) -> str:
    code = dig(record, "family")
    return f"{code} ({FAMILIES[code]})" if isinstance(code, str) and code in FAMILIES else text(code)


def started(record, run_id) -> str:
    """Technical: the record's own start, in UTC as recorded, else the run id's."""
    when = dig(record, "started_at")
    if isinstance(when, str):
        return when.replace("T", " ").rstrip("Z")
    return f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]} {run_id[9:11]}:{run_id[11:13]}:{run_id[13:15]}"


def zone():
    """The time zone every time an engineer sees shows in, and is named by (`R.when_words`): the browser's, as
    Streamlit reports it; None, which shows as UTC, when the browser gives none or one this computer does not know."""
    try:
        return ZoneInfo(hosted.timezone())
    except Exception:  # noqa: BLE001 - none given, or not a zone known here: UTC
        return None


def started_at(record, run_id) -> datetime | None:
    """A run's one start time, wherever an engineer sees it: the moment its id carries, when it was uploaded, which
    every page has from the first; the record's own start only for an id not in that form."""
    try:
        return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return R.moment(dig(record, "started_at"))


def started_words(record, run_id, name=True) -> str:
    return R.when_words(started_at(record, run_id), zone(), name=name)


def pipeline(record, now=None) -> str:
    """`completed`, `failed at <stage>`, `failed (stale)`, or the status as recorded."""
    status, stale = run_status(record, now)
    if stale:
        return "failed (stale)"
    if status == "succeeded":
        return "completed"
    stage = dig(record, "error", "stage")
    if status == "failed":
        return f"failed at {stage}" if isinstance(stage, str) else "failed"
    return text(status)


def review_line(record) -> str:
    """`N items to check`, `no issues`, or `not run` when the run stored no review."""
    n = dig(record, "review", "needs_review")
    if not isinstance(n, int) or isinstance(n, bool):
        return "not run"
    return "no issues" if n == 0 else f"{n} item{'s' * (n != 1)} to check"


def route_of(record) -> str:
    """`template`, `generic` or `generic (forced)`.  A record from before routes were recorded is
    `template` when it names a vendor family."""
    route = dig(record, "route")
    if route == "generic":
        return "generic (forced)" if dig(record, "forced") is True else "generic"
    code = dig(record, "family")
    if route == "template" or (route is None and isinstance(code, str) and code in FAMILIES):
        return "template"
    return text(route)


def generic_context(record) -> str:
    """What Detect saw, for the generic badge: the family hint and its missing pages, or why none."""
    hint, missing, forced = dig(record, "family_hint"), dig(record, "missing_roles"), dig(record, "forced_from")
    if dig(record, "forced") is True:
        return f"Sent down the generic route on purpose: Detect matched {family({'family': forced})}."
    if isinstance(hint, str):
        roles = "; ".join(map(str, missing)) if isinstance(missing, list) and missing else "none recorded"
        return f"Family hint: {family({'family': hint})}. Missing required pages: {roles}."
    return "No vendor template matched this document."


def blob(store, run_id, name) -> bytes:
    data = store.read(run_id, name)
    if not data.strip() and not name.endswith(".jsonl"):   # an empty JSONL file is zero rows
        raise EmptyFile(name)
    return data


def jsonl(data: bytes, name: str) -> list[dict]:
    rows, bad = [], []
    for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
            except ValueError:
                bad.append(n)
                continue
            rows.append(row if isinstance(row, dict) else {"value": row})
    if bad:
        st.warning(f"`{name}`: line {', '.join(map(str, bad[:10]))}{' …' if len(bad) > 10 else ''} "
                   "is not valid JSON and is left out.")
    return rows


def frame(rows: list[dict]) -> pd.DataFrame:
    """JSONL rows as a table.  A column of mixed or nested values is shown as text."""
    df = pd.DataFrame(rows, dtype=object)
    df = df.where(df.notna(), None)
    for col in df.columns:
        kinds = {type(v) for v in df[col] if v is not None}
        if len(kinds) > 1 or kinds & {list, dict}:
            df[col] = df[col].map(lambda v: None if v is None else "; ".join(map(str, v)) if isinstance(v, list)
                                  else text(v))
    return df


def not_stored(name, record) -> str:
    if dig(record, "store", "failed_at") == name:
        return f"`{name}` is not stored: writing it failed (STORE_FAILED)."
    if dig(record, "status") == "running" and not run_status(record)[1]:
        return f"`{name}` is not stored yet: the run is still running."
    stage = dig(record, "error", "stage")
    if stage:
        return f"`{name}` is not stored: the run failed at {plain(stage)} before writing it."
    return f"`{name}` is not stored for this run."


@contextmanager
def panel(name, record):
    """One stored file's panel.  A missing, empty or malformed file is reported here; the page carries on."""
    try:
        yield
    except FileNotFoundError:
        st.info(not_stored(name, record))
    except EmptyFile:
        st.warning(f"`{name}` is stored but empty.")
    except Exception as err:  # noqa: BLE001 - one bad file must not take the page down
        st.warning(f"`{name}` could not be shown ({type(err).__name__}: {plain(err)}).")


def jump(click_key, page_key):
    """A page button was clicked: turn the source pane to the first page its label names."""
    click = st.session_state.get(click_key)
    pages = page_numbers(click["label"]) if click else []
    if pages:
        st.session_state[page_key] = pages[0]


def table(df, key, page_key, page_col=None, config=None):
    """A read-only table.  `page_col` becomes buttons that turn the source pane to that page."""
    config = dict(config or {})
    if page_col in df:
        df = df.assign(**{page_col: df[page_col].map(lambda v: None if text(v, "") == "" else text(v))})
        df = df.loc[:, [page_col, *(c for c in df.columns if c != page_col)]]
        config[page_col] = st.column_config.ButtonColumn(
            page_col, key=f"{key}-click", on_click=jump, args=(f"{key}-click", page_key), pinned=True,
            help="Show this page in the source pane")
    st.dataframe(df, hide_index=True, column_config=config, key=key)


def facts(rows):
    """(plain label, value, field in run.json) as a small table."""
    st.dataframe(pd.DataFrame([(a, text(b), c) for a, b, c in rows], columns=["What", "Value", "Field in run.json"]),
                 hide_index=True, column_config={"Value": st.column_config.TextColumn(width="large")})


# ---- the run list ----

def live(store, run_id, now) -> bool:
    """Whether the run's record says running and its deadline has not passed."""
    try:
        return run_status(store.record(run_id), now) == ("running", False)
    except Exception:  # noqa: BLE001 - an unreadable record is not running
        return False


def waiting(store, now) -> dict:
    """S3 mode: each upload with no run record yet -> (state, seconds waited).  Empty for the local store."""
    runs = set(store.run_ids())
    uploads = {r: t for r in store.launch_ids() if r not in runs and (t := store.uploaded_at(r)) is not None}
    running = bool(uploads) and any(live(store, r, now) for r in runs)
    return {r: (intake.queue_state((now - t).total_seconds(), running), (now - t).total_seconds())
            for r, t in uploads.items()}


def run_words(record, now=None) -> str:
    """The pipeline's status in plain words, for the run list and the watch page: completed, failed, stopped without
    finishing (still recorded as running past its deadline), running, or unknown.  Run details' `pipeline` keeps the
    record's own words and adds the stage a run failed at."""
    status, stale = run_status(record, now)
    if stale:
        return STOPPED
    return {"succeeded": "completed", "failed": "failed", "running": "running"}.get(
        status if isinstance(status, str) else "", "unknown")


def document(record, labels) -> str:
    """The document's file name, from the run's record or else its upload's labels; `—` when neither names it plainly."""
    return text(R.document_name(record or {}) or R.document_name({"source": {"source_name": labels.get("source-name")}}))


def confirmations(store) -> tuple[dict, dict]:
    """The store's confirmed rows, as the run list and the watch page need them: {(environment, run id): the run's
    latest row}, a confirmed run; and {(environment, document, organisation, well): the well's latest row for that
    document}, whose version a new review of the document starts from."""
    by_run, by_well = {}, {}
    for row in store.rows():
        for found, key in ((by_run, (row["environment"], row.get("run_id"))),
                           (by_well, (row["environment"], row.get("document_sha256"), row["organization_id"],
                                      row["well_id"]))):
            if key not in found or C.latest(row) > C.latest(found[key]):
                found[key] = row
    return by_run, by_well


def side(organisation, labels) -> str:
    """Live wells or Test wells: where the run writes, by the review's own rule (`R.environment_for`)."""
    return R.place(R.environment_for(organisation, labels))


def review_words(store, run_id, record, labels, found) -> str:
    """The run's review, as the run list and the watch page say it.  A confirmed run (`found`, `confirmations`) says
    so, with where and the day.  A run with an entry to review: what the review page counts when it opens
    (`R.left_to_check`: the entry as the run left it, against the catalogue, with what the document's latest confirmed
    version for the well carries), in its words.  A run still running, not yet; any other, nothing to review."""
    by_run, by_well = found
    org, well = dig(record, "organization_id"), dig(record, "well_id")
    environment = R.environment_for(org, labels)
    if row := by_run.get((environment, run_id)):
        return f"confirmed: {R.place(environment)}, {R.when_words(row['confirmed_at'], zone(), date=True)}"
    if run_status(record) == ("running", False):
        return "not yet"
    entry = run_entry(lambda name: blob(store, run_id, name), record)
    if entry is None:
        return "nothing to review"
    carried = by_well.get((environment, dig(record, "source", "pdf", "sha256"), org, well))
    return R.left_words(R.left_to_check(entry, record, store.version_of(carried) if carried else None,
                                        catalogue_names()[1]))


def list_rows(store) -> pd.DataFrame:
    """One row per run, newest first: the columns the list shows (PLAIN_COLUMNS) and those its technical section
    shows (TECH_COLUMNS)."""
    now, latest, rows = datetime.now(timezone.utc), set(), []
    runs = set(store.run_ids())
    queue = waiting(store, now)                                              # S3 mode: uploads with no record
    found = confirmations(store)                                             # the confirmed rows, read once
    for run_id in sorted(runs | set(store.launch_ids()), reverse=True):     # newest first
        labels = store.intake_labels(run_id) or {}                           # the upload's, when it kept them
        row = {"Open": "Open", "Run id": run_id, "Started (UTC)": started({}, run_id),
               "Started": started_words({}, run_id, name=False),
               "Organisation": text(labels.get("organization-id")), "Well": text(labels.get("well-id")),
               SIDE: side(labels["organization-id"], labels) if labels else "—", ORIGIN: R.origin_words(labels),
               "Document": document({}, labels), NEWEST: "", "Family": "—", "Route": "—", RUN: "",
               "Pipeline, raw": "", "Document review": "—", "Error": "", "Cost": "", "Cost (USD)": None}
        if run_id in queue:
            state, waited = queue[run_id]
            rows.append({**row, RUN: WAIT_WORDS.get(state, state), "Pipeline, raw": state,
                         "Document review": "not yet", "Waited": wait_text(waited)})
            continue
        if run_id not in runs:
            rows.append({**row, RUN: "launched, no record", "Pipeline, raw": "launched, no record"})
            continue
        try:
            record = store.record(run_id)
            org, well, error = dig(record, "organization_id"), dig(record, "well_id"), dig(record, "error")
            if org is None and labels:                                          # S3: a running record names neither
                org, well = labels["organization-id"], labels["well-id"]
            run = pipeline(record, now)
            key = (text(org), text(well))
            is_latest = run == "completed" and well is not None and key not in latest
            latest |= {key} if is_latest else set()
            row.update({
                "Started (UTC)": started(record, run_id),
                "Started": started_words(record, run_id, name=False),
                "Organisation": text(org),
                "Well": text(well, "no well"),
                SIDE: side(org, labels) if org is not None else "—",
                "Document": document(record, labels),
                NEWEST: "newest completed run" if is_latest else "",
                "Family": family(record),
                "Route": route_of(record),
                RUN: run_words(record, now),
                "Pipeline, raw": run,
                "Document review": review_words(store, run_id, record, labels, found),
                "Error": f"{text(dig(error, 'code'))} at {text(dig(error, 'stage'))}" if isinstance(error, dict) else "",
                "Cost (USD)": cost if isinstance(cost := dig(record, "cost_usd"), (int, float)) else None,
                "Cost": f"${cost:.2f}" if isinstance(cost, (int, float)) else "",
            })
        except FileNotFoundError:
            row.update({RUN: "no record", "Pipeline, raw": "no run.json"})
        except Exception:  # noqa: BLE001 - an unreadable record still gets its row
            row.update({RUN: "record unreadable", "Pipeline, raw": "run.json unreadable"})
        rows.append(row)
    df = pd.DataFrame(rows)
    if "Waited" in df:                  # empty for every run but an upload still waiting
        df["Waited"] = df["Waited"].fillna("")
    return df


def show_list(store, opened=False):
    """The run list.  `opened`: its page has just opened (`route`), so the store's cached reads are forgotten and the
    list is read afresh; a rerun within the page keeps that read, and the sidebar's Refresh reads it again."""
    if opened:
        store.refresh()
    st.title("Design-document runs")
    st.write("Each run reads one ESP design document and proposes the well's pump string and design values, for an "
             "engineer to check against the document and confirm. Start one with **New run from a PDF**, or open a "
             f"run below to review what it read. **{RUN}** says whether the document was read. **Document review** "
             "says how many of its values are left for an engineer to check, or that the run is confirmed. **Wells** "
             "says where a run writes: Live wells or Test wells.")
    st.button("New run from a PDF", icon=":material/upload_file:", on_click=goto, kwargs={"upload": "1"})
    df = list_rows(store)
    if df.empty:
        st.info("There are no runs yet. Start one with New run from a PDF.")
        return
    names = ["Organisation", "Well"]
    shown = df.assign(**{c: df[c].map(wells.labels(df[c])) for c in names})    # marked, and no two alike
    filters = {"Organisation": "org", "Well": "well", RUN: "status"}
    picked = {col: box.multiselect(col, sorted(shown[col].unique()), key=key, placeholder="All", bind="query-params")
              for box, (col, key) in zip(st.columns(3), filters.items())}
    view = shown
    for col, chosen in picked.items():
        if chosen:
            view = view[view[col].isin(chosen)]
    signs = wells.key(df.loc[view.index, names].values.ravel())        # the signs the rows shown show
    st.caption(f"{len(view)} of {len(df)} runs, newest first. Open a run with its button."
               + (f" {signs}" if signs else ""))
    st.session_state["runs-shown"] = list(view["Run id"])           # Open's row, as a run id (`open_run`)
    st.dataframe(view[[c for c in PLAIN_COLUMNS if c in view]], hide_index=True, key="runs", column_config={
        "Open": st.column_config.ButtonColumn(key="open-run", on_click=open_run, type="tertiary", alignment="left",
                                              pinned=True, help="Open this run"),
        "Document": st.column_config.TextColumn(help="The PDF's file name, as it was uploaded"),
        "Started": st.column_config.TextColumn(f"Started ({R.zone_name(zone())})",
                                               help="When the run was started, in your browser's time zone"),
        SIDE: st.column_config.TextColumn(help="Where the run writes when it is confirmed: Live wells are the "
                                               "product's production wells, Test wells its test system's"),
        NEWEST: st.column_config.TextColumn(help="Marks the newest completed run for its organisation and well"),
        RUN: st.column_config.TextColumn(
            help=f"Whether the document was read: completed, failed or running; {STOPPED} when a run ended without "
                 "recording its end. An upload not yet started is waiting to start, not picked up, or expired."),
        "Document review": st.column_config.TextColumn(
            help="A confirmed run says so, with where it was written and the day. Otherwise: how many values the "
                 "review opens with left to check; a review in progress in a browser is not counted until it is "
                 "written."),
        "Cost": st.column_config.TextColumn(help="The model call's cost, in US dollars; empty when none is recorded"),
        "Waited": st.column_config.TextColumn(
            help=f"How long an upload has waited to start: not picked up after {intake.PICKUP_S // 60} min with no "
                 f"run running; expired after {intake.EXPIRY_S // 3600} h"),
        ORIGIN: st.column_config.TextColumn(
            help="Here, with New run from a PDF; or automatically, by a design PDF an engineer uploaded in the ESP "
                 "product"),
    })
    if not SHOW:
        return
    with st.expander("Run ids and codes (technical)"):
        st.caption("Each run as recorded: its run id, its vendor family and route, its pipeline status with the stage "
                   "it failed at, and its error code.")
        st.dataframe(df.loc[view.index, [c for c in (*TECH_COLUMNS, "Waited", ORIGIN) if c in df]],
                     hide_index=True, key="runs-technical", column_config={
            "Family": st.column_config.TextColumn(help="`family`: the vendor template, as F-code (vendor)"),
            "Route": st.column_config.TextColumn(
                help="`route`: template (the vendor's contract) or generic (no template: every value needs review)"),
            "Pipeline, raw": st.column_config.TextColumn(
                help="`status` and `error.stage`: did extraction run? A `running` record past its `deadline_at` reads "
                     "as failed (stale)."),
            "Error": st.column_config.TextColumn(help="`error.code` at `error.stage`"),
            "Cost (USD)": st.column_config.NumberColumn(
                format="$%.2f", help="`cost_usd`: the model call's cost; empty when the record has none"),
            ORIGIN: st.column_config.TextColumn(help="S3 mode: the upload's `origin` label"),
        })


# ---- one run ----

def goto(**view):
    """Switch views: drop the current view's parameter and set the next one's (none: the run list).  The places a
    review's cards were drawn in are forgotten (`card_groups`): the next visit draws each where it now belongs."""
    for key in [k for k in st.session_state if str(k).startswith("placed-")]:
        del st.session_state[key]
    for key in ("run", "watch", "upload"):
        if key in st.query_params:
            del st.query_params[key]
    for key, value in view.items():
        st.query_params[key] = value


def open_run():
    """The run list's Open: its row, among the runs the list showed, as a run id."""
    click, shown = st.session_state.get("open-run"), st.session_state.get("runs-shown") or []
    if click and click["row"] < len(shown):
        goto(run=shown[click["row"]])


def status_cell(label, shown, look, field, raw, technical=True):
    """The plain-language status, in colour with an icon, above the raw field it comes from (`technical`)."""
    color, icon = look
    st.markdown(f"#### {label}: :{color}[{icon} {plain(shown)}]")
    if technical:
        st.caption(f"`{field}` = {plain(text(raw, 'null'))}")


UNKNOWN_LOOK = ("gray", ":material/help:")


def pipeline_cell(record, technical=True):
    """Technical, the status with the stage a run failed at, above its field; else in plain words alone."""
    status, stale = run_status(record) if record else (None, False)
    status_cell("Pipeline" if technical else RUN, ((pipeline if technical else run_words)(record)) if record else "unknown",
                ("red", ":material/timer_off:") if stale else RUN_LOOK.get(text(status), UNKNOWN_LOOK),
                "status", dig(record, "status"), technical)


def review_cell(record):
    """Technical: the review as the run's record counts it, above its field."""
    shown = review_line(record) if record else "unknown"
    look = REVIEW_LOOK.get("no issues" if shown == "no issues" else "to check" if shown.endswith("to check") else "",
                           UNKNOWN_LOOK)
    status_cell("Document review", shown, look, "spec_validation_status", dig(record, "spec_validation_status"))


def review_status(words):
    """The engineer's: the review in the run list's words (`review_words`), in colour."""
    look = (REVIEW_LOOK["no issues"] if words.startswith(("confirmed", "nothing left")) else
            REVIEW_LOOK["to check"] if words.endswith("left to check") else UNKNOWN_LOOK)
    status_cell("Document review", words, look, None, None, technical=False)


def error_line(record):
    error = dig(record, "error")
    if isinstance(error, dict):
        st.error(f"**{plain(text(error.get('code')))}** at {plain(text(error.get('stage')))}: "
                 f"{plain(text(error.get('message')))}")


def image_only(record):
    """A run that failed because its document has no text layer: what to do, in plain words."""
    if dig(record, "error", "code") == "IMAGE_ONLY":
        st.info(IMAGE_ONLY, icon=":material/document_scanner:")


def enter_by_hand(run_id, go=False):
    st.session_state[f"by-hand-{run_id}"] = True
    if go:                                          # from the watch page: the run's page, in the same flow
        goto(run=run_id)


def by_hand_offer(run_id, record, go=False):
    """Enter by hand, on the run's page or (`go`) on the last screen of watching it: beside the image-only message
    (shown above it), else with a plain line."""
    if dig(record, "error", "code") == "IMAGE_ONLY":
        st.caption(R.OFFER)
    else:
        st.info(R.failure_words(record), icon=":material/edit_note:")
    st.button("Enter by hand", type="primary", icon=":material/edit_note:", key=f"enter-{run_id}",
              on_click=enter_by_hand, args=(run_id, go),
              help="Open the review with nothing read, and type each value from the document")


def came_from(environment, how):
    """Where a run came from, in its header: its wells and the product's system they belong to, then how it started in
    the run list's words (`R.origin_words`), which say nothing here when the start is not recorded."""
    line = st.container(horizontal=True, vertical_alignment="center", gap="small")
    line.markdown(f"**{plain(R.place(environment))}**, {plain(R.SYSTEMS[environment])}.")
    if how in ORIGIN_ICONS:                         # who uploaded is never shown
        line.badge(how, icon=ORIGIN_ICONS[how], color="blue")


def title_block(run_id, record, problem, labels=None, offer=False):
    """`labels`: in S3 mode the upload's, for a record that does not name its organisation and well yet.  `offer`:
    the run can be entered by hand (`R.by_hand`)."""
    org, well = dig(record, "organization_id"), dig(record, "well_id")
    if org is None and labels:
        org, well = labels["organization-id"], labels["well-id"]
    with st.container(border=True):
        st.subheader(f"Run {plain(run_id)}")
        if org is not None:
            came_from(R.environment_for(org, labels), R.origin_words(labels))
        if dig(record, "route") == "generic":
            st.badge(GENERIC_BADGE, icon=":material/rate_review:", color="orange")
            st.caption(plain(generic_context(record)))
        if problem:
            st.warning(problem)
        with panel("run.json", record):
            for col, (label, value) in zip(st.columns([2, 3, 2, 3, 2]), [
                    ("Organisation", text(org)),
                    ("Well", text(well, "no well")),
                    ("Family", family(record)),
                    ("Started (UTC)", started(record, run_id)),
                    ("Cost", cost_words(record))]):
                col.metric(label, value)
            status, stale = run_status(record) if record else (None, False)
            left, right = st.columns(2)
            with left:
                pipeline_cell(record)
            with right:
                review_cell(record)
            st.info(BANNER)
            error_line(record)
            image_only(record)
            if stale:
                st.warning(f"Still recorded as running, but its deadline ({plain(dig(record, 'deadline_at'))}) has "
                           "passed, so it reads as failed. The process most likely stopped before writing its "
                           "final record.")
            elif status == "running":
                st.info(f"Still running. It reads as failed if it has not finished by "
                        f"{plain(dig(record, 'deadline_at'))}.")
            if offer:
                by_hand_offer(run_id, record)


def summary_tab(record):
    with panel("run.json", record):
        if not record:
            st.info("No run record to summarise.")
            return
        model = dig(record, "model")
        st.markdown("#### What ran")
        facts([
            ("Mode", mode_words(record), "mode"),
            ("Family", family(record), "family"),
            ("Family taken from", {"detect": "Detect, from the PDF",
                                   "report_frontmatter": "the report's frontmatter"}.get(
                dig(record, "family_source"), dig(record, "family_source")), "family_source"),
            ("Route", route_of(record), "route"),
            ("Family hint", dig(record, "family_hint"), "family_hint"),
            ("Missing required pages", dig(record, "missing_roles"), "missing_roles"),
            ("Near misses", dig(record, "near_misses"), "near_misses"),
            ("Forced from", dig(record, "forced_from"), "forced_from"),
            ("Organisation", dig(record, "organization_id"), "organization_id"),
            ("Well", text(dig(record, "well_id"), "no well: unmatched, so no load rows"), "well_id"),
            *([("Model", "no model call" if model_call(record) is False else "no model call recorded", "model")]
              if not isinstance(model, dict) else [
                ("Model", model.get("model_id"), "model.model_id"),
                ("Effort", model.get("effort"), "model.effort"),
                ("Max tokens", model.get("max_tokens"), "model.max_tokens"),
                ("Contract clarifications", model.get("clarifications"), "model.clarifications"),
                ("Stop reason", model.get("stop_reason"), "model.stop_reason"),
                ("Time to first token (s)", model.get("time_to_first_token_s"), "model.time_to_first_token_s"),
                ("Model time (s)", model.get("model_time_s"), "model.model_time_s")]),
            ("Input tokens", dig(record, "tokens", "input"), "tokens.input"),
            ("Output tokens", dig(record, "tokens", "output"), "tokens.output"),
            ("Cost (USD)", dig(record, "cost_usd"), "cost_usd"),
        ])
        st.markdown("#### Time")
        stages = dig(record, "stages_s") or {}
        st.dataframe(pd.DataFrame([(s, text(stages.get(s))) for s in (*STAGES, *(k for k in stages if k not in STAGES))],
                                  columns=["Stage", "Seconds"]), hide_index=True)
        facts([("Started", dig(record, "started_at"), "started_at"), ("Ended", dig(record, "ended_at"), "ended_at"),
               ("Deadline", dig(record, "deadline_at"), "deadline_at"), ("Budget (s)", dig(record, "budget_s"), "budget_s")])
        st.markdown("#### What it produced")
        facts([
            *((f"Rows: {GRAINS[g].lower()}", dig(record, "rows", g), f"rows.{g}") for g in GRAINS),
            ("Quarantined values", dig(record, "quarantine"), "quarantine"),
            ("Findings", dig(record, "findings", "total"), "findings.total"),
            ("Findings by run severity", dig(record, "findings", "by_round1_severity"), "findings.by_round1_severity"),
            ("Findings by spec severity", dig(record, "findings", "by_spec_severity"), "findings.by_spec_severity"),
            ("Blocks publishing", dig(record, "blocks_publish"), "blocks_publish"),
            ("Values reviewed", dig(record, "review", "items"), "review.items"),
            ("Values needing review", dig(record, "review", "needs_review"), "review.needs_review"),
            ("Informational review items", dig(record, "review", "informational"), "review.informational"),
            ("Unknown models", len(dig(record, "unknown_models") or []), "unknown_models"),
            ("Pages in the PDF", dig(record, "pages", "page_count"), "pages.page_count"),
            ("Pages kept for extraction", dig(record, "pages", "kept_pages"), "pages.kept_pages"),
        ])
        st.markdown("#### Source and store")
        facts([
            ("PDF path (recorded, not opened)", dig(record, "source", "pdf", "path"), "source.pdf.path"),
            ("PDF SHA-256", dig(record, "source", "pdf", "sha256"), "source.pdf.sha256"),
            ("PDF file name as uploaded", dig(record, "source", "source_name"), "source.source_name"),
            ("Report path (recorded, not opened)", dig(record, "source", "report", "path"), "source.report.path"),
            ("Report SHA-256", dig(record, "source", "report", "sha256"), "source.report.sha256"),
            ("Files stored", dig(record, "store", "files"), "store.files"),
            ("Failed writing", dig(record, "store", "failed_at"), "store.failed_at"),
        ])
        with st.expander("Hashes"):
            st.json(dig(record, "hashes") or {})
        if isinstance(dig(record, "error"), dict):
            with st.expander("Error details"):
                st.json(dig(record, "error"))
        with st.expander("Full record (run.json)"):
            st.json(record)


def model_call(record) -> bool | None:
    """Whether the run made its model call: True, False, or None when its record does not say.  run.json's `model` is
    set just before the call and never otherwise (extraction/run.py; the Lambda's own record of a run that failed
    before the pipeline has none), and run.json is written only at a run's start and end.  So a report run made none,
    a failed run without `model` failed before the call, and a running or stale one's record does not say yet."""
    if dig(record, "mode") == "report":
        return False
    if isinstance(dig(record, "model"), dict):
        return True
    return False if run_status(record or {}) == ("failed", False) else None


def mode_words(record) -> str:
    """The run's mode, and whether it made its model call (`model_call`)."""
    mode = dig(record, "mode")
    if mode == "report":
        return "existing report: no model call"
    if mode != "pdf":
        return mode
    return {True: "PDF: full chain, one model call", False: "PDF: no model call was made: the run failed before it",
            None: "PDF: no model call recorded"}[model_call(record)]


def cost_words(record) -> str:
    """The run's cost, or why it has none, by `model_call`'s rule: a call made, as a timed-out one, has no cost
    recorded; it is not a run with no model call."""
    cost = dig(record, "cost_usd")
    if isinstance(cost, (int, float)):
        return f"${cost:.2f}"
    if cost is not None:
        return text(cost)
    return {True: "not recorded", False: "no model call", None: "none recorded"}[model_call(record)]


def grains_tab(get, record, page_key):
    for name, heading in GRAINS.items():
        st.markdown(f"#### {heading}")
        st.caption(f"{name}.parquet")
        with panel(f"{name}.parquet", record):
            df = pd.read_parquet(io.BytesIO(get(f"{name}.parquet")))
            if df.empty:
                st.caption("No rows." + (" The well is unmatched, so nothing was loaded."
                                         if dig(record, "well_id") is None and record else ""))
            elif "source_page" in df:
                table(df, f"grain-{name}", page_key, "source_page")
            else:
                st.caption("This grain stores no page reference. The review list cites a page for each of its values.")
                table(df, f"grain-{name}", page_key)
    st.markdown("#### Quarantined values")
    st.caption("quarantine.jsonl: values kept out of the grains, never coerced")
    with panel("quarantine.jsonl", record):
        rows = jsonl(get("quarantine.jsonl"), "quarantine.jsonl")
        table(frame(rows), "quarantine", page_key, "source_page") if rows else st.caption("No values quarantined.")


def hz(freq) -> str:
    return "no frequency" if pd.isna(freq) else f"{float(freq):g} Hz"


def frequencies(rows) -> list[str]:
    found = rows[FREQUENCY]
    return [hz(f) for f in sorted(found.dropna().unique())] + (["no frequency"] if found.isna().any() else [])


def curve_name(ident, rows) -> str:
    scenario, role, model, sensitivity = ident
    labels = [v for v in rows.get("scenario_label", pd.Series(dtype=object)) if isinstance(v, str) and v]
    parts = [f"Scenario {text(scenario)}" + (f" ({labels[0]})" if labels else ""), text(role, "no role"),
             text(model, "no model")]
    return ", ".join(parts + (["sensitivity case"] if text(sensitivity) == "True" else []))


def axis_title(name, units) -> str:
    found = sorted({str(u) for u in units.dropna()})
    return f"{name} ({', '.join(found)})" if found else name


def curve_figure(rows) -> go.Figure:
    """A series per frequency (colour), each point marked by its type (shape).  Values as printed."""
    drawn = rows[rows["flow_as_printed"].notna() & rows["head_as_printed"].notna()]
    freqs = sorted(drawn[FREQUENCY].dropna().unique())
    step = (len(RAMP) - 1) / max(len(freqs) - 1, 1)
    colors = {f: RAMP[4] if len(freqs) == 1 else RAMP[round(n * step)] for n, f in enumerate(freqs)}
    kinds = [*MARKS, *dict.fromkeys(k for k in drawn["point_type"].fillna("no point type") if k not in MARKS)]
    fig = go.Figure()
    for freq in [*freqs, None]:
        series = drawn[drawn[FREQUENCY].isna()] if freq is None else drawn[drawn[FREQUENCY] == freq]
        color, shown = OTHER_MARK if freq is None else colors[freq], False
        for kind in kinds:
            points = series[series["point_type"].fillna("no point type") == kind]
            if points.empty:
                continue
            label, symbol, size = MARKS.get(kind, (kind, "x", 9))
            if kind == "curve_point":
                points = points.sort_values("flow_as_printed")
            hover = points.loc[:, ["flow_unit_as_printed", "head_unit_as_printed", "head_basis_as_printed",
                                   "source_page"]]
            fig.add_scatter(
                x=points["flow_as_printed"], y=points["head_as_printed"], name=hz(freq), legendgroup=hz(freq),
                showlegend=not shown, mode="lines+markers" if kind == "curve_point" else "markers",
                cliponaxis=False, line=dict(color=color, width=2),
                marker=dict(color=color, symbol=symbol, size=size, line=dict(color=SURFACE, width=2)),
                customdata=hover.astype(object).where(hover.notna(), "—").to_numpy(),
                hovertemplate=(f"<b>{label}</b> at {hz(freq)}<br>Flow %{{x:,}} %{{customdata[0]}}<br>"
                               "Head %{y:,} %{customdata[1]}, %{customdata[2]}<br>Page %{customdata[3]}"
                               "<extra></extra>"))
            shown = True
    fig.update_layout(height=300, margin=dict(l=0, r=8, t=8, b=0), showlegend=True,
                      legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0))
    fig.update_xaxes(title=axis_title("Flow", drawn["flow_unit_as_printed"]), rangemode="tozero")
    fig.update_yaxes(title=axis_title("Head", drawn["head_unit_as_printed"]), rangemode="tozero")
    return fig


def curves_tab(get, record, page_key):
    st.caption("curve_observations.parquet. One chart of head against flow per curve: a curve is one "
               f"{', '.join(CURVE[:-1])} and {CURVE[-1]} (pump_model: pump_model_canonical, else "
               f"pump_model_as_printed), with one series per {FREQUENCY}, lighter to darker as it rises. "
               "Marker shape gives the point type: circle curve point (joined by a line), diamond design point, "
               "star BEP, square shutoff, triangles ROR min and max. Values are drawn as printed: nothing is "
               "scaled, fitted or interpolated.")
    with panel("curve_observations.parquet", record):
        df = pd.read_parquet(io.BytesIO(get("curve_observations.parquet")))
        if df.empty:
            st.caption("No curve observations.")
            return
        df = df.assign(pump_model=df["pump_model_canonical"].fillna(df["pump_model_as_printed"]))
        section = df["observation_is_composite"].map(lambda v: {True: 0, False: 1}.get(v, 2))
        for i, (_, heading, field) in enumerate(SECTIONS):
            part = df[section == i]
            if part.empty:
                continue
            drawn, skipped = [], []
            for ident, rows in part.groupby(CURVE, dropna=False, sort=False):
                both = rows["flow_as_printed"].notna() & rows["head_as_printed"].notna()
                if both.any():
                    drawn.append((curve_name(ident, rows), rows, int(both.sum())))
                else:
                    skipped.append({
                        "pages": ", ".join(dict.fromkeys(text(p) for p in rows["source_page"].dropna())) or None,
                        "Curve": curve_name(ident, rows), "frequencies": ", ".join(frequencies(rows)),
                        "Rows": len(rows),
                        "Not drawn because": "no head value" if rows["head_as_printed"].isna().all()
                        else "no point has both flow and head"})
            total = len(drawn) + len(skipped)
            st.markdown(f"#### {heading}")
            st.caption(f"{field}: {total} curve{'s' * (total != 1)}, {len(drawn)} with a point to draw.")
            columns = st.columns(2)
            for n, (name, rows, count) in enumerate(drawn):
                with columns[n % 2]:
                    st.markdown(f"**{plain(name)}**")
                    st.caption(f"{count} of {len(rows)} rows have both flow and head, and are drawn.")
                    st.plotly_chart(curve_figure(rows), key=f"curve-{i}-{n}", config={"displaylogo": False})
            if skipped:
                st.caption("Curves not drawn: a head-against-flow chart needs a point with both values.")
                table(pd.DataFrame(skipped), f"skipped-{i}", page_key, "pages")


def row_pages(get) -> dict:
    """(staging block, row_key) -> the page that row cites, from the stored report: a finding's page."""
    try:
        report = read_report(get("report.md").decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - no report, no pages; the findings still show
        return {}
    return {(block, n): row.get("source_page") for block, rows in report.blocks.items()
            for n, row in enumerate(rows, 1)}


def findings_tab(get, record, page_key):
    st.caption("findings.jsonl. Run severity is what the pipeline did: only REPORT_INVALID, LOAD_HALTED and "
               "EXTRACTION_EMPTY fail a run, everything else is a warning. Spec severity is the validator specification's own level, "
               "reported and not enforced.")
    with panel("findings.jsonl", record):
        rows = jsonl(get("findings.jsonl"), "findings.jsonl")
        if not rows:
            st.caption("No findings.")
            return
        df = frame(rows)
        pages = row_pages(get)
        df.insert(0, "row's page", [pages.get((t, k)) for t, k in zip(df.get("table", [None] * len(df)),
                                                                        df.get("row_key", [None] * len(df)))])
        order = [(r != "warning", -SPEC_RANK.get(s, 9)) for r, s in
                 zip(df.get("round1_severity", [None] * len(df)), df.get("spec_severity", [None] * len(df)))]
        df = df.iloc[sorted(range(len(df)), key=lambda n: order[n], reverse=True)]
        for col in ("round1_severity", "spec_severity"):
            if col in df:
                counts = df[col].fillna("none").value_counts()
                st.caption(f"`{col}`: " + ", ".join(f"{plain(k)} {v}" for k, v in counts.items()))
        st.caption("row's page: the page the staging row named by `table` and `row_key` cites, read from report.md. "
                   "Empty for findings about the whole document.")
        table(df, "findings", page_key, "row's page", {
            "round1_severity": st.column_config.TextColumn("run severity (round1_severity)"),
            "spec_severity": st.column_config.TextColumn("spec severity (spec_severity)")})


def signals_tab(get, record, page_key):
    st.caption("review.jsonl, read only: the signals each value raised. One item per value of the pump string and the "
               "well fields; a value needs a check when any deterministic signal fires. The list never blocks a run.")
    with panel("review.jsonl", record):
        rows = jsonl(get("review.jsonl"), "review.jsonl")
        if not rows:
            st.caption("No review items.")
            return
        df = frame(rows)
        needs = df["needs_review"].map(lambda v: v is True) if "needs_review" in df else pd.Series(False, df.index)
        reasons = df["reasons"].map(bool) if "reasons" in df else pd.Series(False, df.index)
        st.caption(f"{int(needs.sum())} of {len(df)} values need review; "
                   f"{int((reasons & ~needs).sum())} informational (reasons listed, no review needed).")
        table(df.loc[needs.sort_values(ascending=False, kind="stable").index], "review", page_key, "cited_page")


def notices_tab(get, record):
    st.markdown("#### Notices")
    st.caption("notices.jsonl: accepted known issues of the vendor family, and checks not evaluated for this run")
    with panel("notices.jsonl", record):
        rows = jsonl(get("notices.jsonl"), "notices.jsonl")
        st.dataframe(frame(rows), hide_index=True) if rows else st.caption("No notices.")
    st.markdown("#### Unknown models")
    st.caption("unknown_models.jsonl: pump models checked against the catalogue snapshot")
    with panel("unknown_models.jsonl", record):
        rows = jsonl(get("unknown_models.jsonl"), "unknown_models.jsonl")
        if rows:
            df = frame(rows)
            if "class" in df:
                df.insert(df.columns.get_loc("class") + 1, "meaning", df["class"].map(MODEL_LEVEL))
            st.dataframe(df, hide_index=True, column_config={"class": st.column_config.TextColumn("level (class)")})
        else:
            st.caption("No unknown models.")


def logs_tab(get, record, page_key):
    with st.expander("Prep manifest (prep_manifest.csv)"):
        with panel("prep_manifest.csv", record):
            table(pd.read_csv(io.BytesIO(get("prep_manifest.csv")), dtype=str, keep_default_na=False),
                  "manifest", page_key, "original_page_no")
    for name, label in LOGS.items():
        with st.expander(f"{label} ({name}.jsonl)"):
            with panel(f"{name}.jsonl", record):
                rows = jsonl(get(f"{name}.jsonl"), f"{name}.jsonl")
                st.json(rows) if rows else st.caption("No entries.")


def report_tab(get, record):
    with panel("report.md", record):
        report = get("report.md").decode("utf-8", "replace")
        st.caption("report.md, shown as plain text exactly as stored.")
        st.code(report, language=None, wrap_lines=True)


@st.cache_data(max_entries=128, show_spinner=False)
def page_count(sha256: str, _pdf: bytes) -> int:
    with pdfium.PdfDocument(_pdf) as pdf:
        return len(pdf)


@st.cache_data(max_entries=64, show_spinner=False)
def page_png(sha256: str, index: int, _pdf: bytes) -> bytes:
    """One page as PNG, cached by the PDF's hash and the page, so the bytes are never hashed."""
    with pdfium.PdfDocument(_pdf) as pdf:
        image = pdf[index].render(scale=2).to_pil()
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def source_pane(store, get, record, page_key):
    st.markdown("#### Source document")
    try:
        source = json.loads(get("source.json"))
    except Exception:  # noqa: BLE001 - run.json carries the same block
        source = dig(record, "source")
        st.caption("`source.json` could not be read" + ("; using the copy in run.json." if source else "."))
    if not isinstance(source, dict):
        st.info("No source information for this run.")
        return
    pdf = source.get("pdf")
    if not isinstance(pdf, dict):
        st.info("This run has no source PDF: it reused a report without one.")
        return
    sha = text(pdf.get("sha256"), "")
    with panel("the source PDF", record):
        data = store.source_pdf(sha, pdf.get("path"))
        if data is None and MODE == "s3":
            st.warning("Source not in store")
            st.caption("Recorded location:")
            st.code(text(pdf.get("path")), language=None, wrap_lines=True)
            st.caption(f"SHA-256 {sha or '—'}. The PDF is shown only from its upload under the intake prefix, "
                       "and only when its content has this SHA-256.")
            return
        if data is None:
            st.warning("Source not in store")
            st.caption("Recorded path, shown as text only:")
            st.code(text(pdf.get("path")), language=None, wrap_lines=True)
            st.caption(f"SHA-256 {sha or '—'}. To show it, put the PDF at `intake/<sha256>.pdf` in the run store, "
                       f"or name its folder in {PDF_DIRS_ENV}.")
            return
        total = page_count(sha, data)
        page = st.number_input("Page", min_value=1, step=1, key=page_key)
        if page > total:
            st.warning(f"Page {page} is past the end of this {total}-page document.")
            return
        roles = dig(record, "pages", "roles", str(page)) or []
        kept = dig(record, "pages", "kept_pages")
        note = [f"Page {page} of {total}."]
        if isinstance(kept, list):
            note.append("Kept for extraction." if page in kept else "Not kept for extraction.")
        if roles:
            note.append(f"Detect found: {'; '.join(map(str, roles))}.")
        with st.container(height=PANE - 120, border=False):
            st.image(page_png(sha, page - 1, data), caption=" ".join(note), width="stretch")


def run_entry(get, record) -> dict | None:
    """The entry a run offers for review: its well_entry.json, when the run names a well."""
    return stored_entry(get) if record and dig(record, "well_id") is not None else None


def show_run(store, run_id):
    if run_id not in store.run_ids() and run_id in store.launch_ids():
        return show_watch(store, run_id)                 # launched, no record yet
    st.button("All runs", icon=":material/arrow_back:", on_click=goto, type="tertiary")
    if run_id not in store.run_ids():
        st.warning(f"There is no run {plain(run_id)} in this store." if SHOW else NO_SUCH_RUN)
        return
    record, problem = {}, None
    try:
        record = store.record(run_id)
    except FileNotFoundError:
        problem = "`run.json` is not stored, so this run's status is unknown. The files it stored are shown below."
    except Exception as err:  # noqa: BLE001
        problem = (f"`run.json` could not be read ({type(err).__name__}: {plain(err)}). "
                   "The files the run stored are shown below.")

    def get(name):
        return blob(store, run_id, name)

    page_key, labels = f"page-{run_id}", store.intake_labels(run_id)
    entry = run_entry(get, record)
    by_hand = entry is None and bool(record) and R.by_hand(record, entry_stored=stored(get, C.ENTRY),
                                                           pdf_found=source_pdf(store, record) is not None)
    confirmed = store.latest_version(dig(record, "source", "pdf", "sha256"), record.get("organization_id"),
                                     record["well_id"], R.environment_for(record.get("organization_id"), labels),
                                     run_id=run_id) if entry is not None or by_hand else None
    if entry is None and not confirmed and not (by_hand and st.session_state.get(f"by-hand-{run_id}")):
        if not SHOW:
            return plain_run(store, run_id, problem)
        title_block(run_id, record, problem, labels, offer=by_hand)   # the offer, if any
        return run_details(store, get, record, page_key)
    views = ("Confirmed entry" if confirmed else "Enter by hand" if entry is None else "Review & confirm",
             "Run details (technical)")
    view = views[0] if not SHOW else st.segmented_control(     # the switch off: the engineer's view, and no other
        "View", views, default=views[0], required=True, label_visibility="collapsed",
        key=f"view-{run_id}-{bool(confirmed)}")
    if view == views[1]:
        st.caption(TECHNICAL)
        title_block(run_id, record, problem, labels)
        return run_details(store, get, record, page_key)
    if confirmed:
        confirmed_view(store, run_id, record, confirmed, get, page_key)
    else:                                           # entered by hand: over the empty proposal
        review_view(store, run_id, record, entry or empty_entry(record["organization_id"], record["well_id"]), get,
                    page_key)
    return True                                     # an engineer's screen: the sidebar names no bucket


def plain_run(store, run_id, problem) -> bool:
    """A run with nothing to review, with the switch off: in place of Run details, the watch page's words on what
    happened to it and what to do next (Enter by hand, where the run offers it); or, its record missing or unreadable,
    that.  An engineer's screen: True."""
    if problem:
        st.warning(f"This run's own record is missing or cannot be read, so there is nothing to show for it. Upload "
                   f"the document again; if you need this run itself, tell {CONTACT}.")
        hosted.hidden(f"run {run_id}", problem, key=("record", run_id))
        return True
    return watch(store, run_id, here=True)


def run_details(store, get, record, page_key):
    """Every file the run stored, in tabs, beside the source PDF."""
    left, right = st.columns([3, 2], gap="medium")
    with right:
        source_pane(store, get, record, page_key)
    with left:
        tabs = st.tabs(["Summary", "Extracted numbers", "Curves", "Findings", "Signals", "Notices and models",
                        "Logs", "Report"])
        with tabs[0], st.container(height=PANE, border=False):
            summary_tab(record)
        with tabs[1], st.container(height=PANE, border=False):
            grains_tab(get, record, page_key)
        with tabs[2], st.container(height=PANE, border=False):
            curves_tab(get, record, page_key)
        with tabs[3], st.container(height=PANE, border=False):
            findings_tab(get, record, page_key)
        with tabs[4], st.container(height=PANE, border=False):
            signals_tab(get, record, page_key)
        with tabs[5], st.container(height=PANE, border=False):
            notices_tab(get, record)
        with tabs[6], st.container(height=PANE, border=False):
            logs_tab(get, record, page_key)
        with tabs[7], st.container(height=PANE, border=False):
            report_tab(get, record)


# ---- review and confirm a run's proposed well entry (app/review.py decides; this draws) ----

STATUS_LOOK = {C.SETTLED: ("green", ":material/check_circle:", "settled"),
               C.CORRECTED: ("blue", ":material/edit:", "corrected"),
               C.TO_CHECK: ("orange", ":material/flag:", "to check"),
               C.BLOCKS: ("red", ":material/block:", "blocks publish")}
# A value nobody checked is never shown as settled: kept as read, or, the two Write or operations fill, not from the
# document.
KEPT_LOOK = ("gray", ":material/radio_button_unchecked:", R.KEPT_AS_READ)
OUTSIDE_LOOK = ("gray", ":material/radio_button_unchecked:", "not from the document")
TO_CHECK_GROUPS = ("found", "not found")
GROUP_NAME = {"found": "Found", "not found": "Not found"}
GROUP_HINT = {"found": "Read from the document, to check against it.",
              "not found": "Nothing was read: settle each as empty if the document does not print it.",
              "checked": "Settled or corrected by an engineer, here or in an earlier confirmation of this document.",
              "kept": "Read with nothing flagged, so no one has checked these: each is written as read unless you "
                      "correct it."}
FOLDED = {"checked": ("Settled or corrected", ":material/task_alt:"),            # the groups shown folded
          "kept": ("Kept as read, not checked", ":material/radio_button_unchecked:")}
BATCH_TITLE = {"string": "The pump string", "found": "Values found", "not found": "Values not found"}
BATCH = "Check group by group"                      # the button that opens the group checks, one pop-up each
STARTED_AS = {C.SETTLED: "needed no check", C.TO_CHECK: "to check", C.BLOCKS: "blocks publish"}
TECHNICAL = "The pipeline's own files for this run, exactly as it stored them: for checking the pipeline, not the design."
NO_CATALOGUE = f"The pump catalogue could not be read, so models cannot be picked or checked here. Tell {CONTACT}."
NO_SUCH_RUN = "There is no such run. Go back to all runs, and open one from the list."
REVIEW = ("Check each value below against the document beside it: settle it, correct it, or settle it as empty when "
          f"the document does not print it. **{BATCH}** does the same a group at a time: the pump string, then the "
          "values found, then those not found. When nothing is left to check, Write saves the well's entry.")
WRITTEN = ("This entry is written, and nothing here can change it. To change the well's entry, start a new run from "
           "the same document and confirm that one: what was corrected here comes back in it.")
ROLE_WORDS = {None: "none", "primary": "primary", "taper": "taper", "gas_handler": "gas handler"}
PRINTED_HELP = ("Give every name the document prints for this section, as printed: the equipment list's and the "
                "schematic's, for instance, separated by semicolons.")
NO_PDF = ("The document's PDF is not available here, so its pages cannot be shown. Check the values against your own "
          "copy.")
SCAN_VIEWS = ("Original scan", "Transcription")
BAD_PDF = "The document's PDF could not be opened, so its pages cannot be shown. Check the values against your own copy."


def stored(get, name) -> bool:
    """Whether the run stored the file, readable or not."""
    try:
        get(name)
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 - there, if empty or unreadable
        pass
    return True


def stored_entry(get) -> dict | None:
    """The run's well_entry.json, or None when it stored none or it cannot be read."""
    try:
        entry = json.loads(get(C.ENTRY))
    except Exception:  # noqa: BLE001 - not stored, empty or malformed: the run has no review
        return None
    return entry if isinstance(dig(entry, "review", "values"), list) else None


def now_text() -> str:
    return C.timestamp(datetime.now(timezone.utc))


@st.cache_resource(show_spinner=False)
def catalogue() -> dict:
    """The catalogue snapshot the writer checks pump models against: ESP_EXTRACTION_ASSETS_DIR's, hash-checked."""
    return read_catalogue()


def catalogue_names():
    """(the snapshot, model -> has a head curve; its names), or (None, None) when it cannot be read here."""
    try:
        models = catalogue()
    except Exception as err:  # noqa: BLE001 - no snapshot here: the string's screens say so, the log says why
        hosted.hidden("the pump catalogue could not be read", err, key=("catalogue", str(err)))
        return None, None
    return models, CatalogueNames(models)


def open_dialog(run_id, step):
    st.session_state["dialog"] = (run_id, step)


def close_dialog():
    st.session_state.pop("dialog", None)


def turn(page_key, page):
    st.session_state[page_key] = page


def chips(parent, pages, page_key, prefix):
    """A chip per page `pages` names (`p3`, `p1, p4`); each turns the page viewer to its page."""
    for p in page_numbers(pages):
        parent.button(f"p{p}", key=f"{prefix}-p{p}", type="tertiary", icon=":material/description:",
                      on_click=turn, args=(page_key, p), help=f"Show page {p}")


def shown(envelope) -> str:
    """An envelope's value and unit; `not found` for none."""
    if envelope is None:
        return "not found"
    value, unit = envelope.get("value"), envelope.get("unit")
    value = f"{value:,.10g}" if isinstance(value, float) else text(value)
    return value if unit in (None, "none") else f"{value} {unit}"


def review_state(store, run_id, record, entry) -> dict:
    """The run's review, kept in the session until it is written: started once, with what the document's latest
    confirmed version for the well carries, and the design dates."""
    key = f"review-{run_id}"
    if key not in st.session_state:
        org, well, labels = record.get("organization_id"), record["well_id"], store.intake_labels(run_id)
        environment, kept = R.environment_for(org, labels), R.Kept(store.objects)
        version = store.latest_version(dig(record, "source", "pdf", "sha256"), org, well, environment)
        pdf = source_pdf(store, record)
        scan = None if pdf is None else T.recognise(pdf)            # a transcription: reviewed against its scan
        typed = scan is not None and scan.problem is None
        entry = R.from_scan(entry) if typed else entry                    # an entry by hand stays as it is
        st.session_state[key] = {
            "entry": entry, "kept": kept, "environment": environment, "scan": scan, "how": R.origin_words(labels),
            "review": R.start(entry, record, version, at=now_text(), names=catalogue_names()[1], scan=typed),
            "version": version and {k: version.get(k) for k in ("confirmation_id", "confirmed_by", "confirmed_at")},
            "dates": R.design_dates(kept, run_id, store.current_entry(environment, org, well),
                                    by_hand=R.manual(entry))}
    return st.session_state[key]


def context(store, run_id, record, state, page_key) -> SimpleNamespace:
    if "confirmer" not in st.session_state:                     # read once per app session
        st.session_state["confirmer"] = R.confirmer(MODE, os.environ, getattr(store, "root", None),
                                                    headers=hosted.request_headers(), technical=SHOW)
    name, who = st.session_state["confirmer"]
    sha, pdf = dig(record, "source", "pdf", "sha256"), source_pdf(store, record)
    models, names = catalogue_names()
    return SimpleNamespace(run_id=run_id, record=record, entry=state["entry"], items=R.reviewed(state["entry"]),
                           review=state["review"], kept=state["kept"], env=state["environment"],
                           dates=state["dates"], version=state["version"], name=name, who=who,
                           offline=MODE == "local" and name is not None, sha=sha, pdf=pdf, page_key=page_key,
                           models=models, names=names, scan=state["scan"], how=state["how"],
                           manual=R.manual(state["entry"]), store=store, just=state.get("just"))


def source_pdf(store, record) -> bytes | None:
    """The run's source PDF, or None when it is not where the store keeps it (the panes say so in plain words)."""
    sha = dig(record, "source", "pdf", "sha256")
    try:
        return store.source_pdf(sha, dig(record, "source", "pdf", "path")) if sha else None
    except Exception:  # noqa: BLE001 - not there, or not readable: the pane says the pages cannot be shown
        return None


def shown_pdf(scan, pdf, sha, key):
    """The pages a viewer shows: a transcription's original scan, with a switch to the transcription, page N being page
    N in both; else the PDF itself.  (bytes, SHA-256)."""
    if scan is None or scan.problem:
        return pdf, sha
    view = st.segmented_control("Pages shown", SCAN_VIEWS, default=SCAN_VIEWS[0], required=True, key=key,
                                label_visibility="collapsed")
    return (scan.original, scan.sha256) if view == SCAN_VIEWS[0] else (pdf, sha)


def document_pane(store, record, page_key, pdf=None):
    """The source document beside an engineer's screen: its file name and its pages, nothing technical.  A
    transcription opens on its original scan."""
    st.markdown("#### Source document")
    sha = dig(record, "source", "pdf", "sha256")
    pdf = pdf if pdf is not None else source_pdf(store, record)
    scan = None if pdf is None else T.recognise(pdf)
    name = R.document_name(record, scan)
    if name:
        st.caption(plain(name))
    if pdf is None:
        st.info(NO_PDF)
        return
    try:
        pdf, sha = shown_pdf(scan, pdf, sha, f"{page_key}-shown")
        total = page_count(sha, pdf)
        turner = st.container(horizontal=True, vertical_alignment="bottom")
        page = turner.number_input("Page", min_value=1, step=1, key=page_key)
        if page > total:
            st.warning(f"Page {page} is past the end of this {total}-page document.")
            return
        if turner.button("Full size", icon=":material/zoom_out_map:", key=f"{page_key}-full"):
            full_page(sha, pdf, total, page_key, name)
        with st.container(height=PANE - 120, border=False):
            st.image(page_png(sha, page - 1, pdf), caption=f"Page {page} of {total}", width="stretch")
    except Exception:  # noqa: BLE001 - a PDF that will not open: say so, the review carries on
        st.warning(BAD_PDF)


def follow(key, page_key):
    """The full-size pop-up's page was turned: the pane's page follows it."""
    st.session_state[page_key] = st.session_state[key]


def full_page(sha, pdf, total, page_key, name):
    """One page at the pop-up's full width, large enough to read its print, opened on the pane's page (so on the page a
    chip turned it to), with its own page turner, which the pane follows."""
    @st.dialog(name or "Source document", width="large", on_dismiss="rerun")
    @guarded
    def body():
        key = f"{page_key}-full-page"
        page = st.number_input("Page", min_value=1, max_value=total, step=1, key=key, on_change=follow,
                               value=min(st.session_state.get(page_key) or 1, total), args=(key, page_key))
        st.image(page_png(sha, page - 1, pdf), caption=f"Page {page} of {total}", width="stretch")
    body()


def gate(ctx) -> tuple[bool, str]:
    """(whether Write is on, why not).  On only when the writer would accept the request as it stands, the run's
    organisation may write where the entry goes (`R.environment_problem`: lab-check to test only, as R.write refuses it),
    the string's editor holds no unsaved edits, the source PDF is not a broken transcription, and, entered by hand,
    something is typed (`R.entered`, the writer's rule)."""
    if ctx.scan is not None and ctx.scan.problem:
        return False, (f"Write is off: this file is marked as a transcription, but {ctx.scan.problem}. Upload the file "
                       "exactly as the skill made it, as a new run.")
    if refused := R.environment_problem(ctx.record.get("organization_id"), ctx.env):      # whatever is decided
        return False, f"Write is off. {refused}"
    if R.unsaved(ctx.review):
        return False, " ".join(filter(None, (ctx.who if ctx.name is None else "", R.UNSAVED)))
    groups = R.groups(ctx.review, ctx.entry)
    left = sum(len(groups[g]) for g in R.GROUPS)
    waiting = f"{left} value{'s' * (left != 1)} left to check" if left else ""
    if groups["string"] and R.blocking(ctx.review, ctx.entry, R.STRING):
        waiting += ", the string among them: it blocks publish"
    nothing = R.NOTHING_ENTERED if ctx.manual and not R.entered(ctx.review) else ""
    still = f"{R.sentence_case(waiting)}." if waiting else ""
    if ctx.name is None:
        return False, " ".join(filter(None, (ctx.who, nothing, still)))
    if nothing:
        return False, " ".join(filter(None, (f"Write is off. {nothing}", still)))
    if left:
        return False, waiting
    try:
        reasons = R.check(ctx.kept, ctx.review, ctx.entry, ctx.record, confirmed_by=ctx.name, environment=ctx.env,
                          catalogue=catalogue, at=datetime.now(timezone.utc))
    except Exception as err:  # noqa: BLE001 - e.g. no catalogue snapshot: say that Write is off; the log says why
        hosted.hidden("the checks before Write could not run", err, key=("gate", str(err)))
        return False, f"Write is off: the checks before writing could not run. Tell {hosted.contact()}."
    return (False, "Write is off. " + " ".join(dict.fromkeys(map(R.refusal_words, reasons)))) if reasons else (True, "")


def next_step(review, entry, after=None) -> str:
    """The next pop-up with something in it, after `after`: Write when none is left."""
    groups, steps = R.groups(review, entry), [*R.GROUPS, "write"]
    return next(s for s in steps[steps.index(after) + 1 if after else 0:] if s == "write" or groups[s])


def start_batch(run_id):
    state = st.session_state[f"review-{run_id}"]
    open_dialog(run_id, next_step(state["review"], state["entry"]))


def decide(run_id, how, key):
    """A decision made one by one, by a press on the page: settle, settle as empty, or back to the value as read.  The
    value is marked as just decided (`actions`)."""
    state = st.session_state[f"review-{run_id}"]
    how(state["review"], state["entry"], key, at=now_text())
    state["just"] = key


def accept(run_id, key):
    state = st.session_state[f"review-{run_id}"]
    R.accept_anyway(state["review"], state["entry"], key, st.session_state.get(f"anyway-{run_id}-{key}") or "",
                    at=now_text())
    state["just"] = key


def save_correction(run_id, key) -> list[str]:
    """The Correct form's fields, as the correction of `key`, the value it was opened from: [] when it is made, else
    why not."""
    ss, base = st.session_state, f"fix-{run_id}-{key}"
    state = ss[f"review-{run_id}"]
    value, problem = R.parse_value(key, ss.get(f"{base}-value"))
    return [problem] if problem else R.correct(
        state["review"], state["entry"], key, value=value, unit=ss.get(f"{base}-unit"), source=ss.get(f"{base}-source"),
        note=ss.get(f"{base}-note"), at=now_text())


def outcome_box(run_id):
    """What the last write of this run came to."""
    out = st.session_state.get(f"outcome-{run_id}")
    if not out:
        return
    head = {C.WRITTEN: f"Written to {R.place(out.get('environment'))}", C.REFUSED: "Not written: Write refused it",
            C.FAILED: "Not written: Write failed", "not sent": "Not sent", "no result": "No answer yet",
            C.STALE: "Not written", C.RUNNING: "Still writing"}
    lines = [f"**{head.get(out['status'], 'Not written')}**" + (f": {plain(out['note'])}" if out.get("note") else ""),
             *(f"- {plain(r)}" for r in dict.fromkeys(map(R.refusal_words, out["reasons"])))]
    box = st.success if out["status"] == C.WRITTEN else st.error if out["status"] in (C.REFUSED, C.FAILED, "not sent") \
        else st.warning
    box("\n\n".join(lines))


def dates_note(ctx):
    """The older-design warning, or why there is none."""
    d = ctx.dates
    if d["case"] == "older":
        st.warning(f"Older design: this document is dated {d['incoming']}, before the {d['current']} of the well's "
                   f"current entry in {R.place(ctx.env)}. Writing it makes the older design the current entry.",
                   icon=":material/history:")
    elif d["case"] == "can't compare":
        undated = "has no design date, entered by hand" if ctx.manual else "prints no design date"
        current = f"is dated {d['current']}" if d["current"] else "has no design date"
        st.caption(f"The well's current entry in {R.place(ctx.env)} {current}; this document "
                   f"{'is dated ' + d['incoming'] if d['incoming'] else undated}: can't compare.")
    elif d["case"] == "not older":
        st.caption(f"The well's current entry in {R.place(ctx.env)} is dated {d['current']}; this document is not older.")


def review_header(ctx, on, why):
    org, well = ctx.record.get("organization_id"), ctx.record["well_id"]
    with st.container(border=True):
        facts_ = st.container(horizontal=True, gap="large")
        names = {"Organisation": wells.marked(text(org)), "Well": wells.marked(well)}  # no emphasis: an edge space
        for label, value in (*names.items(),                                              # would break it
                             ("Writes to", R.place(ctx.env)),
                             ("Report", "entered by hand" if ctx.manual else R.report_words(ctx.record, ctx.scan)),
                             ("Design date", "none, entered by hand" if ctx.manual else
                              ctx.dates["incoming"] or "not printed"),
                             ("Confirming as", ctx.name or NOBODY)):
            with facts_.container(gap=None, width="content"):
                st.caption(label)
                if label == "Confirming as":                # as text, never Markdown: an address is no link
                    st.text(value)
                else:
                    st.markdown(plain(value) if label in names else f"**{plain(value)}**")
        if signs := wells.key([text(org), well]):
            st.caption(signs)
        came_from(ctx.env, ctx.how)
        if not ctx.manual:                                  # entered by hand, BY_HAND below says what to do
            st.caption(REVIEW)
        notes = st.container(horizontal=True, vertical_alignment="center")
        if dig(ctx.record, "route") == "generic" and not ctx.manual:
            notes.badge(R.NOT_KNOWN, icon=":material/rate_review:", color="orange")
        if ctx.who and ctx.name is not None:                # with no one to confirm as, the line beside Write says why
            notes.caption(plain(ctx.who))
        if ctx.manual:                                      # nothing was read: every value is typed
            st.info(plain(R.BY_HAND), icon=":material/edit_note:")
        if ctx.scan is not None:                            # a transcription: checked against its original scan
            st.warning(plain(R.scan_words(ctx.scan)), icon=":material/document_scanner:")
        if not ctx.manual and (problem := R.run_problem_words(ctx.record)):     # a run that stopped after reading
            st.warning(plain(problem), icon=":material/report:")                # keeps its entry
        dates_note(ctx)
        if ctx.version:
            carried = sum(v["carried"] for v in ctx.review["values"].values())
            st.text(f"Confirmed before: {carried} value{'s' * (carried != 1)} come{'s' * (carried == 1)} back from "
                    f"{R.confirmation_words(ctx.version, zone())}.")       # as text: it names who confirmed
        buttons = st.container(horizontal=True, vertical_alignment="center")
        nothing = next_step(ctx.review, ctx.entry) == "write"
        buttons.button(BATCH, icon=":material/done_all:", key=f"batch-{ctx.run_id}", disabled=nothing,
                       on_click=start_batch, args=(ctx.run_id,),    # what it does: REVIEW, above.  No tooltip while it
                       help="Nothing is left to check" if nothing else None)   # can be pressed: a pressed button's stays up
        buttons.button(f"Write to {R.place(ctx.env)}", type="primary", icon=":material/save:", key=f"write-{ctx.run_id}",
                       disabled=not on, on_click=open_dialog, args=(ctx.run_id, "write"))
        if not on:
            buttons.caption(plain(why))
        if MODE == "s3":                    # through the writer; local mode's own line (`ctx.who`) says what it does
            st.caption(plain(R.write_words(org, ctx.env)))


def value_line(ctx, key):
    """One value: its label, value and unit, printed text, page chips, status, reasons, and what can be done."""
    v, item, rid = ctx.review["values"][key], ctx.items[key], ctx.run_id
    corrected = v["status"] == C.CORRECTED
    with st.container(border=True, key=f"line-{rid}-{key}"):
        head = st.container(horizontal=True, vertical_alignment="center", gap="small")
        head.markdown(f"**{plain(R.label(key))}**")
        head.markdown(f":gray[*{R.REF_WORDS}*]" if key == C.DESIGN_DOCUMENT_REF else     # as the confirmed view has it
                      plain(shown(v["final"])) if v["final"] is not None else
                      ":gray[*empty*]" if corrected else ":gray[*not found*]")
        if item.get("printed") is not None:
            head.caption(f"printed “{plain(item['printed'])}”")
        chips(head, item.get("page"), ctx.page_key, f"chip-{rid}-{key}")
        status_badges(ctx, head, key)
        if corrected:
            st.caption(plain(correction_words(ctx, key)))
        if v["moved"] and not ctx.manual:                  # entered by hand, the new run read nothing
            st.caption(f"The new run reads {plain(shown(R.proposal(ctx.entry, key)))}; the correction above comes "
                       "from the earlier confirmation.")
        words = [R.in_words(r) for r in item["reasons"]] + ([f"Note: {R.note_words(item['note'])}"] if item.get("note") else [])
        if words:                                           # once corrected, they are about what was read
            st.caption(plain(("About the value as read: " if corrected and not ctx.manual else "")
                             + " ".join(dict.fromkeys(words))))
        if key != C.DESIGN_DOCUMENT_REF:
            actions(ctx, key)


def correction_words(ctx, key) -> str:
    """A corrected value's correction, under it: what it was corrected from, where the new value comes from when not
    the document, and the note typed with it."""
    v, read = ctx.review["values"][key], R.proposal(ctx.entry, key)
    final = v["final"] or {}
    said = ("Typed by hand." if ctx.manual else
            f"Corrected from {shown(read)}, as read." if read is not None else "Corrected: nothing was read.")
    if final.get("source") == "ops_entered":
        said += f" From {R.SOURCE_WORDS['ops_entered']}."
    return said + (f" Note with the correction: “{final['note']}”" if final.get("note") else "")


def status_badges(ctx, parent, key):
    v = ctx.review["values"][key]
    color, icon, label = ((OUTSIDE_LOOK if key in R.NOT_FROM_THE_DOCUMENT else KEPT_LOOK)   # nobody checked it
                          if R.unchecked(ctx.review, key) else
                          STATUS_LOOK.get(v["status"], ("gray", ":material/help:", v["status"])))
    parent.badge(label, icon=icon, color=color)
    if v["accept_anyway"]:
        parent.badge("accepted anyway", icon=":material/gavel:", color="red", help=v["accept_anyway"])
    if R.pulled_out(ctx.review, key) is not None:
        parent.badge("left out of a group check", icon=":material/undo:", color="violet")
    if v["carried"]:                                        # the header names whose confirmation, as text
        parent.badge("kept from before", icon=":material/history:", color="gray",
                     help="Kept from the confirmation of "
                          f"{R.when_words((ctx.version or {}).get('confirmed_at'), zone())}")


def actions(ctx, key):
    """Settle, correct, settle as empty, accept anyway, back to the value as read: what the value's state allows, each
    button saying what it does.  A value decided a moment ago by a press here (`ctx.just`) shows one button only, at
    the row's far end, which brings these back: its card has not moved (`card_groups`) and no button is where the
    press was, so a second press does nothing."""
    v, rid = ctx.review["values"][key], ctx.run_id
    if ctx.just == key:
        st.container(horizontal=True, horizontal_alignment="right").button(
            "Change it", icon=":material/tune:", key=f"again-{rid}-{key}")
        return
    proposed, blocks = R.proposal(ctx.entry, key), R.blocking(ctx.review, ctx.entry, key)
    row = st.container(horizontal=True, gap="small")
    as_proposed = v["status"] == C.SETTLED and v["final"] == proposed and not v["accept_anyway"]
    settles = not R.proposal_blocks(ctx.entry, key) and not as_proposed and (key == R.STRING or not ctx.manual)
    corrected = key != R.STRING and v["status"] == C.CORRECTED     # its Settle would undo it: said as that, and last
    if settles and not corrected:
        row.button("Settle as empty" if key == R.STRING and ctx.manual else            # by hand, nothing is proposed
                   "Settle as proposed" if key == R.STRING else "Settle", icon=":material/check:",
                   key=f"settle-{rid}-{key}", on_click=decide, args=(rid, R.settle, key))
    if key != R.STRING:
        row.button("Correct", icon=":material/edit:", key=f"correct-{rid}-{key}", on_click=open_dialog,
                   args=(rid, f"fix:{key}"))
        if (v["final"] is not None or v["status"] in R.UNDECIDED) and not (blocks and proposed is None):
            row.button("Settle as empty", icon=":material/do_not_disturb_on:", key=f"empty-{rid}-{key}",
                       on_click=decide, args=(rid, R.settle_empty, key))
    if blocks and not (v["accept_anyway"] and v["status"] in C.FINAL):
        with row.popover("Accept anyway", icon=":material/gavel:", key=f"anywaypop-{rid}-{key}"):
            why = st.text_area("Why accept it uncorrected?", key=f"anyway-{rid}-{key}",
                               placeholder="Recorded with the value as its accept-anyway reason")
            st.button("Accept anyway", type="primary", key=f"anyway-go-{rid}-{key}", disabled=not (why or "").strip(),
                      on_click=accept, args=(rid, key))
    if settles and corrected and proposed is not None:
        row.button("Back to the value as read", icon=":material/undo:", key=f"settle-{rid}-{key}", on_click=decide,
                   args=(rid, R.settle, key))
    if key == R.STRING:
        row.button("Edit sections", icon=":material/table_edit:", key=f"sections-{rid}", on_click=edit,
                   args=(rid, R.open_editor), help="Edit the string's sections here, beside the document")


def correction_dialog(ctx, key):
    """The Correct form, as a pop-up named for its value: what was read and why it is flagged, the fields, and the
    document's pages beside them, opened at the value's page.  One form at a time, opened from one value
    (`open_dialog`) and saving that value only; a correction saved closes it, and nothing of it stays on screen."""
    rid, base, v, item = ctx.run_id, f"fix-{ctx.run_id}-{key}", ctx.review["values"][key], ctx.items[key]

    @st.dialog(f"Correct {R.label(key)}", width="large", on_dismiss=close_dialog)
    @guarded
    def body():
        form, pages = st.columns([2, 3], gap="medium")
        with pages:
            dialog_pages(ctx, f"fix-{key}", [key])
        with form:
            read, current = R.proposal(ctx.entry, key), v["final"] or R.proposal(ctx.entry, key) or {}
            st.caption(plain(" ".join(dict.fromkeys(
                ["Nothing was read for this value." if read is None else
                 f"Read as {shown(read)}" + (f", printed “{item['printed']}”." if item.get("printed") is not None
                                             else "."),
                 *(R.in_words(r) for r in item["reasons"])]))))
            st.text_input("Value", value=text(current.get("value"), ""), key=f"{base}-value",
                          help=R.PRESSURE_HELP if key in R.PRESSURES else None)
            st.selectbox("Unit", R.allowed_units(key), key=f"{base}-unit",
                         help="The units the product takes for this value")
            st.selectbox("Source", R.SOURCES, index=R.SOURCES.index("ops_entered") if key == "vapor_pressure_psia" else 0,
                         format_func=R.SOURCE_WORDS.get, key=f"{base}-source")
            st.text_input("Note", key=f"{base}-note", placeholder="Optional")
            problems = []
            if st.button("Save the correction", type="primary", key=f"{base}-save"):
                problems = save_correction(rid, key)
                if not problems:                    # made: the form closes, and its fields go with it
                    close_dialog()
                    st.rerun()
            for problem in problems:
                st.error(plain(problem))
    body()


def sections_table(ctx, page_key, table_key):
    """The string as read: one row per section, its page a button that turns `page_key`'s viewer; then, under the
    table, each flagged section's reason, whole: a table's cell would cut it, here and full screen."""
    block = ctx.entry["review"]["pump_string"]
    rows = [{"page": s.get("page"), "order": s.get("section_order"), "model": s.get("pump_model"),
             "as printed": s.get("pump_model_as_printed"), "stages": s.get("stages"),
             "role": s.get("section_role") and R.words_of(s["section_role"]),
             "flag": None if s.get("status") == C.SETTLED else s.get("status")}     # no flag is not a check passed
            for s in block["sections"]]
    if rows:
        table(cells(rows), table_key, page_key, "page")
        for i, section in enumerate(block["sections"], 1):
            if section.get("reasons"):
                name = section.get("pump_model_as_printed") or section.get("pump_model")
                st.caption(plain(f"{R.sentence_case(R.section_name(i))}{f' ({name})' if name else ''}: "
                                 + " ".join(dict.fromkeys(map(R.in_words, section["reasons"])))))
    elif not ctx.manual:                                  # by hand, the checks below say to add them
        st.caption("No sections were read.")


def cells(rows) -> pd.DataFrame:
    """Rows for an engineer's table: a missing value is an empty cell, not `None`."""
    return frame([{k: "" if v is None else v for k, v in r.items()} for r in rows])


def section_rows(sections, display=None) -> list[dict]:
    """Sections as the engineer reads them: plain column names, a role in words, and a section marked not in the
    catalogue shown with its model as printed."""
    def cell(k, v, d):
        if k == "pump_model" and d.get("absent"):
            return f"not in the catalogue: {d['printed']}"
        return R.words_of(v) if k == "section_role" and v else v
    return [{R.FIELD_WORDS.get(k, R.words_of(k)): cell(k, v, d) for k, v in s.items()}
            for s, d in zip(sections, display or [{}] * len(sections))]


def string_view(ctx, page_key, table_key):
    """The string as it stands (beside the string as read when they differ), what it puts in the well's entry, and its
    checks in words."""
    v = ctx.review["values"][R.STRING]
    if v["sections"] == R.sections(ctx.entry):
        sections_table(ctx, page_key, table_key)
    else:
        carried = v["carried"] and len(v["history"]) == v["since"]
        st.caption("Kept from the earlier confirmation:" if carried else
                   "As entered:" if ctx.manual else "As corrected:")
        st.dataframe(cells(section_rows(v["sections"], v["display"])), hide_index=True)
        if not ctx.manual:                              # by hand, nothing was read to show beside it
            if v["moved"]:
                st.caption("The new run reads the string differently; the string above comes from the earlier "
                           "confirmation.")
            with st.expander("As read"):
                sections_table(ctx, page_key, table_key)
    words = None if ctx.manual and not v["sections"] else R.unwritten_words(ctx.review)    # by hand: nothing "as read"
    if words:
        st.info(plain(words), icon=":material/info:")
    checks_box(ctx, v["sections"], R.string_rows(ctx.review))


CHECK_LOOK = {True: ":green[:material/check_circle:]", False: ":orange[:material/error:]",
              "pass": ":green[:material/check_circle:]", "fail": ":orange[:material/error:]"}


def checks_box(ctx, sections, rows):
    """The string's checks in words: on its sections as they stand, then those about the document as read."""
    st.markdown("**Checks on the sections as they stand**")
    if ctx.names is None:
        st.caption(NO_CATALOGUE)
    for ok, words in R.string_checks(sections, rows, ctx.names) if ctx.names else []:
        st.caption(f"{CHECK_LOOK[ok]} {plain(words)}")
    found = R.document_checks(ctx.entry)
    if found:
        st.markdown("**About the document as read**: edits do not change these")
        for result, words in found:
            st.caption(f"{CHECK_LOOK.get(result, ':gray[:material/remove_circle:]')} {plain(words)}")


def string_block(ctx):
    rid, key = ctx.run_id, R.STRING
    with st.container(border=True, key=f"line-{rid}-{key}"):
        head = st.container(horizontal=True, vertical_alignment="center", gap="small")
        head.markdown(f"**{R.label(key)}**")
        n = len(ctx.entry["review"]["pump_string"]["sections"])
        if not ctx.manual:
            head.caption(f"{n} section{'s' * (n != 1)} as read")
        status_badges(ctx, head, key)
        if ctx.review.get("editor") is not None:
            editor_view(ctx)
        else:
            string_view(ctx, ctx.page_key, f"sections-{rid}")
            actions(ctx, key)


# ---- the string's editor: app/review.py keeps the copy; each change goes there through a callback ----

def edit(run_id, how, *args):
    """An editor action on the run's review: `how(review, *args)`."""
    how(st.session_state[f"review-{run_id}"]["review"], *args)


def edit_entry(run_id, how):
    state = st.session_state[f"review-{run_id}"]
    how(state["review"], state["entry"])


def edit_field(run_id, row_id, field, key):
    """A field changed on screen, into the copy as review.set_field takes it; why not, if it does not."""
    state = st.session_state[f"review-{run_id}"]
    st.session_state[f"ed-{run_id}-problem"] = R.set_field(state["review"], row_id, field, st.session_state.get(key),
                                                           names=catalogue_names()[1])


def edit_reason(run_id, key):
    st.session_state[f"review-{run_id}"]["review"]["editor"]["reason"] = st.session_state.get(key) or ""


def save_edits(run_id):
    state = st.session_state[f"review-{run_id}"]
    R.save(state["review"], state["entry"], at=now_text())


def whole(value):
    return None if value is None else int(value)


def feet(value):
    return None if value is None else float(value)


def editor_view(ctx):
    """The string's sections, edited in place: a row per section, the checks after every change, Save, Cancel and
    Back to the string as read."""
    rid, ed = ctx.run_id, ctx.review["editor"]
    rows, n = ed["rows"], len(ed["rows"])
    st.caption("Editing the sections. Order numbers follow the rows: section 1, the first row, is the deepest.")
    if R.order_unprinted(ctx.entry):
        st.warning("The document's order numbers were not read, so the order shown is unchecked. Check it against the "
                   "document before you save.", icon=":material/swap_vert:")
    if ctx.names is None:
        st.warning(NO_CATALOGUE)
    for i, row in enumerate(rows, 1):
        editor_row(ctx, i, row, n)
    if not rows:
        st.caption("No sections yet: add them from the document, the deepest first.")
    add = st.container(horizontal=True, vertical_alignment="bottom")
    place = add.selectbox("Place for a new section", range(1, n + 2), index=n, key=f"ed-{rid}-{ed['gen']}-place-{n}",
                          format_func=lambda p: "1, the deepest" if p == 1 else f"{p}, the top" if p == n + 1 else str(p),
                          width=220)
    add.button("Add a section", icon=":material/add:", key=f"ed-{rid}-add", on_click=edit, args=(rid, R.add_section, place))
    if problem := st.session_state.get(f"ed-{rid}-problem"):
        st.error(plain(problem))
    checks_box(ctx, R.rows_sections(rows), rows)
    if R.not_written(rows):                     # a section not in the catalogue, or without its stage count
        st.text_area("Why record the string without writing it?", value=ed["reason"], key=f"ed-{rid}-{ed['gen']}-reason",
                     on_change=edit_reason, args=(rid, f"ed-{rid}-{ed['gen']}-reason"),
                     placeholder="Recorded with the string, which puts no pump string in the well's entry")
    problems = R.save_problems(ctx.review)
    buttons = st.container(horizontal=True, vertical_alignment="center")
    buttons.button("Save", type="primary", icon=":material/save:", key=f"ed-{rid}-save", disabled=bool(problems),
                   on_click=save_edits, args=(rid,))
    buttons.button("Cancel", key=f"ed-{rid}-cancel", on_click=edit, args=(rid, R.close_editor))
    buttons.button("Back to no sections" if ctx.manual else "Back to the string as read", icon=":material/undo:",
                   key=f"ed-{rid}-reset", on_click=edit_entry, args=(rid, R.reset_editor))
    if problems:
        st.caption(plain(" ".join(problems)))


def editor_row(ctx, i, row, n):
    """One section of the copy: its place and page chips, moves and removal, then its fields."""
    rid, r = ctx.run_id, row["id"]
    k = f"ed-{rid}-{ctx.review['editor']['gen']}-{r}"

    def field(name):
        return {"key": f"{k}-{name}", "on_change": edit_field, "args": (rid, r, FIELD_OF.get(name, name), f"{k}-{name}")}

    def number(parent, label, name, value, **kwargs):
        """A number the engineer can clear: a field whose own default is a value snaps back to it when emptied, so its
        default is empty and its value is seeded."""
        st.session_state.setdefault(f"{k}-{name}", value)
        parent.number_input(label, value=None, placeholder="not given", **kwargs, **field(name))

    with st.container(border=True, key=f"{k}-box"):
        head = st.container(horizontal=True, vertical_alignment="center", gap="small")
        head.markdown(f"**{R.sentence_case(R.section_name(i))}**")
        chips(head, row["page"], ctx.page_key, k)
        head.button("Up", icon=":material/arrow_upward:", key=f"{k}-up", disabled=i == 1, on_click=edit,
                    args=(rid, R.move_section, r, -1), help="Move it towards section 1, the deepest")
        head.button("Down", icon=":material/arrow_downward:", key=f"{k}-down", disabled=i == n, on_click=edit,
                    args=(rid, R.move_section, r, 1), help="Move it away from section 1, the deepest")
        head.button("Remove", icon=":material/delete:", key=f"{k}-remove", on_click=edit, args=(rid, R.remove_section, r))
        if row["printed"] and not row["absent"]:
            st.caption(f"printed “{plain(row['printed'])}”")
        model, mark = st.columns([3, 2], vertical_alignment="bottom")
        if row["absent"]:
            model.text_input("Model as printed", value=row["printed"] or "", help=PRINTED_HELP, **field("printed"))
        else:
            options = R.model_options(ctx.names, row) if ctx.names else []
            model.selectbox("Model", options, index=options.index(row["pump_model"]) if row["pump_model"] in options
                            else None, placeholder="Search the pump catalogue", disabled=ctx.names is None,
                            **field("model"))
        mark.checkbox("Not in the catalogue", value=row["absent"], help="The model is not in the pump catalogue: the "
                      "string is recorded with it, and no pump string is written to the well's entry", **field("absent"))
        if row["pump_model"] and (ctx.models or {}).get(row["pump_model"]) is False:
            st.caption("In the pump catalogue, without a head curve.")
        counts = st.columns(4)
        number(counts[0], "Stages", "stages", whole(row["stages"]), min_value=1, step=1)
        number(counts[1], "Housings", "housings", whole(row["housings"]), min_value=1, step=1)
        counts[2].text_input("Stages per housing", value=row["stages_per_housing_as_printed"] or "",
                             placeholder="e.g. 100, 100, 100", **field("per"))
        roles = [None, *R.ROLES, *([row["section_role"]] if row["section_role"] not in (None, *R.ROLES) else [])]
        counts[3].selectbox("Role", roles, index=roles.index(row["section_role"]),
                            format_func=lambda role: ROLE_WORDS.get(role, R.words_of(role)), **field("role"))
        depths = st.columns(4)
        number(depths[0], "Top MD (ft)", "top", feet(row["top_md_ft"]), min_value=0.0, step=10.0, format="%.1f")
        number(depths[1], "Bottom MD (ft)", "bottom", feet(row["bottom_md_ft"]), min_value=0.0, step=10.0, format="%.1f")


FIELD_OF = {"model": "pump_model", "per": "stages_per_housing_as_printed", "role": "section_role", "top": "top_md_ft",
            "bottom": "bottom_md_ft"}               # an editor field's key -> the section field it sets


def review_view(store, run_id, record, entry, get, page_key):
    """Review & confirm: the decisions beside the PDF, the Write button always in view."""
    state = review_state(store, run_id, record, entry)
    ctx = context(store, run_id, record, state, page_key)
    on, why = gate(ctx)
    outcome_box(run_id)
    review_header(ctx, on, why)
    drawn, now = card_groups(run_id, ctx.review, ctx.entry)
    left, right = st.columns([1, 1], gap="medium")
    with right:
        document_pane(store, record, page_key, ctx.pdf)
    with left:
        st.caption(plain(scroll_cue(ctx, now)))
        with st.container(height=PANE, border=False):
            string_block(ctx)
            for group in TO_CHECK_GROUPS:
                if drawn[group]:
                    st.markdown(f"#### {group_heading(group, drawn[group], now)}")
                    st.caption(R.BY_HAND_HINT if ctx.manual else GROUP_HINT[group])
                    for key in drawn[group]:
                        value_line(ctx, key)
            for group, (title, icon) in FOLDED.items():     # keyed: it stays open when its count changes
                if drawn[group]:
                    with st.expander(f"{title}: {sum(now[key] == group for key in drawn[group])}", icon=icon,
                                     key=f"{group}-{run_id}"):
                        st.caption(GROUP_HINT[group])
                        for key in drawn[group]:
                            value_line(ctx, key)
    state.pop("just", None)                     # the next press anywhere brings the value's buttons back
    dialog = st.session_state.get("dialog")
    if dialog and dialog[0] == run_id:          # statements, not an expression: magic would write its None
        if dialog[1] == "write":
            write_dialog(ctx, on, why)
        elif dialog[1].startswith("fix:"):
            correction_dialog(ctx, dialog[1].removeprefix("fix:"))
        else:
            batch_dialog(ctx, dialog[1])


def card_groups(run_id, review, entry) -> tuple[dict, dict]:
    """({group: the keys whose cards it draws}, {key: the group the value is in now}).  The groups: found and not
    found, the values still to check (`R.groups`); checked, those an engineer settled or corrected, here or in an
    earlier confirmation; kept, those kept as read, which nobody checked.  A card stays in the group it was first drawn
    in on this visit, so deciding a value moves no other card under the pointer; a value to check again is drawn
    among those to check.  `goto` forgets the places: the next visit draws each value where it now belongs."""
    now = {key: group for group, keys in R.groups(review, entry).items() if group in TO_CHECK_GROUPS for key in keys}
    places = st.session_state.setdefault(f"placed-{run_id}", {})
    drawn = {group: [] for group in (*TO_CHECK_GROUPS, *FOLDED)}
    for key in R.reviewed(entry):
        if key == R.STRING:
            continue
        now.setdefault(key, "kept" if R.unchecked(review, key) else "checked")
        if now[key] in TO_CHECK_GROUPS:
            places[key] = now[key]
        drawn[places.setdefault(key, now[key])].append(key)
    return drawn, now


def group_heading(group, keys, now) -> str:
    """`Found: 2`; once some of its cards are decided and still drawn there, `Found: 1 of 2 left to check`."""
    left = sum(now[key] == group for key in keys)
    return f"{GROUP_NAME[group]}: {left}" + ("" if left == len(keys) else f" of {len(keys)} left to check")


def scroll_cue(ctx, now) -> str:
    """Above the values, which scroll on their own beside the document: what among them is left to check, so that
    none waits below what shows with nothing to say so."""
    counts = {group: sum(g == group for g in now.values()) for group in TO_CHECK_GROUPS}
    left = [*(["the pump string"] if R.undecided(ctx.review, ctx.entry, R.STRING) else []),
            *([f"{counts['found']} value{'s' * (counts['found'] != 1)} found"] if counts["found"] else []),
            *([f"{counts['not found']} not found"] if counts["not found"] else [])]
    if not left:
        return "Nothing in this list is left to check. The list scrolls on its own."
    return (f"Left to check in this list: {R.join(left)}. The list scrolls on its own, so "
            f"{'it' if len(left) == 1 and left[0] == 'the pump string' else 'some'} may be below what shows.")


def dialog_pages(ctx, step, keys):
    """The pop-up's own page viewer, on the app's page renderer: opened at its first item's page, every page there."""
    page_key = f"dlgpage-{ctx.run_id}-{step}"
    if ctx.pdf is None:
        st.info(NO_PDF)
        return
    try:
        pdf, sha = shown_pdf(ctx.scan, ctx.pdf, ctx.sha, f"{page_key}-shown")
        total = page_count(sha, pdf)
        if page_key not in st.session_state:
            pages = [p for k in keys for p in pages_of(ctx, k)]
            st.session_state[page_key] = min(pages[0], total) if pages else 1
        page = st.number_input("Page", min_value=1, max_value=total, step=1, key=page_key)
        st.image(page_png(sha, page - 1, pdf), caption=f"Page {page} of {total}", width="stretch")
    except Exception:  # noqa: BLE001 - a PDF that will not open: say so, the pop-up carries on
        st.warning(BAD_PDF)


def pages_of(ctx, key) -> list[int]:
    """The pages a value, or the string's sections, cite: each one's first page as printed first."""
    cited = ([s.get("page") for s in ctx.entry["review"]["pump_string"]["sections"]] if key == R.STRING
             else [ctx.items[key].get("page")])
    return [int(n) for c in cited for n in re.findall(r"\d+", str(c or ""))]


def batch_dialog(ctx, step):
    rid, keys = ctx.run_id, R.groups(ctx.review, ctx.entry)[step]

    @st.dialog(f"Group check: {BATCH_TITLE[step].lower()}", width="large", on_dismiss=close_dialog)
    @guarded
    def body():
        items, pages = st.columns([3, 2], gap="medium")
        with pages:
            page_key = f"dlgpage-{rid}-{step}"
            dialog_pages(ctx, step, keys)
        with items:
            anyway = step == "string" and R.blocking(ctx.review, ctx.entry, R.STRING)
            if step == "string":
                string_view(ctx, page_key, f"dlg-sections-{rid}")
                if anyway:
                    st.warning("The string blocks publish. It can be accepted anyway, with a reason recorded with it, "
                               "or left to check.", icon=":material/block:")
                    reason = st.text_area("Why accept the string anyway?", key=f"dlg-anyway-{rid}")
                    chosen = {R.STRING} if (reason or "").strip() else set()
                else:
                    reason = None
                    chosen = {R.STRING} if st.checkbox("The pump string, as it stands above" if ctx.manual else
                                                       "The pump string, as read above", value=True,
                                                       key=f"tick-{rid}-string") else set()
            else:
                reason, chosen = None, ticks(ctx, step, keys, page_key)
            go = st.container(horizontal=True)
            if anyway:                  # the button's sentence is the affirmation; a reason must come first
                affirm = go.button(R.STRING_ANYWAY, type="primary", key=f"dlg-go-{rid}-{step}", disabled=not chosen)
                pull = go.button("Leave it to check", key=f"dlg-pull-{rid}-{step}")
            else:                       # nothing ticked: the button pulls everything out
                affirm = go.button(R.sentence(step, ctx.entry, sorted(chosen), scan=ctx.review.get("scan"))
                                   if chosen else f"Leave {'it' if len(keys) == 1 else f'all {len(keys)}'} out",
                                   type="primary" if chosen else "secondary", key=f"dlg-go-{rid}-{step}")
                pull = False
            if step == "string" and go.button("Edit sections", icon=":material/table_edit:", key=f"dlg-sections-edit-{rid}",
                                              help="Leave the string out of this group check and edit its sections "
                                                   "on the review, beside the document"):
                R.batch(ctx.review, ctx.entry, "string", set(), at=now_text())      # this batch's pull-out step
                R.open_editor(ctx.review)
                close_dialog()
                st.rerun()
            if step != "string":
                st.caption("Unticked values are left out of this group check: they stay to check and are marked on "
                           "the review. A value that blocks publish cannot be ticked: decide it one by one.")
            elif not anyway:
                st.caption("Unticked, the string is left out of this group check: it stays to check and is marked on "
                           "the review.")
            if affirm or pull:
                R.batch(ctx.review, ctx.entry, step, set() if pull else chosen, at=now_text(),
                        reason=reason if anyway and not pull else None)
                open_dialog(rid, next_step(ctx.review, ctx.entry, step))
                st.rerun()
    body()


def ticks(ctx, step, keys, page_key) -> set:
    """One tick per value, ticked by default; one that blocks publish shows, unticked and unable to be ticked.  The
    list scrolls, so the button below it stays in view."""
    chosen, generic = set(), dig(ctx.record, "route") == "generic"
    if generic:
        st.caption("Not a known vendor report: nothing cross-checked these values, so check each against the page.")
    with st.container(height=min(560, 96 * len(keys) + 16), border=False):
        for key in keys:
            v, item, blocks = ctx.review["values"][key], ctx.items[key], R.blocking(ctx.review, ctx.entry, key)
            with st.container(border=True, key=f"dlg-{ctx.run_id}-{key}"):
                row = st.container(horizontal=True, vertical_alignment="center", gap="small")
                label = f"**{plain(R.label(key))}**" + (f": {plain(shown(v['final']))}" if v["final"] is not None else "")
                if row.checkbox(label, value=not blocks, disabled=blocks, key=f"tick-{ctx.run_id}-{step}-{key}"):
                    chosen.add(key)
                if item.get("printed") is not None:
                    row.caption(f"printed “{plain(item['printed'])}”")
                chips(row, item.get("page"), page_key, f"dlgchip-{ctx.run_id}-{key}")
                words = [R.in_words(r) for r in item["reasons"] if not (generic and r == "generic_path")]
                if blocks:
                    words.insert(0, "Blocks publish: decide it one by one.")
                if words:
                    st.caption(plain(" ".join(words)))
    return chosen


def write_dialog(ctx, on, why):
    rid, org, well = ctx.run_id, ctx.record.get("organization_id"), ctx.record["well_id"]

    @st.dialog(f"Write to {R.place(ctx.env)}", width="medium", on_dismiss=close_dialog)
    @guarded
    def body():
        st.markdown(f"Writes to **{R.place(ctx.env)}**, organisation {plain(wells.marked(text(org)))}, well "  # names:
                    f"{plain(wells.marked(well))}."                                                     # no emphasis
                    + (f" {plain(R.write_words(org, ctx.env))}" if MODE == "s3" else ""))   # as under Write
        if signs := wells.key([text(org), well]):
            st.caption(signs)
        named("Confirming as", ctx.name or NOBODY)
        name = R.document_name(ctx.record, ctx.scan)
        st.caption(f"Document: {plain(name) if name else 'the design document'}, entered by hand, no design date."
                   if ctx.manual else
                   f"Document: {plain(name) if name else 'the design document'}"
                   f"{', typed from the scan' if ctx.review.get('scan') else ''}, design dated "
                   f"{ctx.dates['incoming'] or 'not printed'}.")
        dates_note(ctx)
        tally = R.tally(ctx.review)
        st.markdown("What will be recorded:")
        st.dataframe(pd.DataFrame([(UNTOUCHED if ctx.manual and k == "settled by the extraction" else words, tally[k])
                                   for k, words in TALLY.items() if tally[k]],       # a row with none is left out
                                  columns=["Values", "Count"]), hide_index=True)
        if not on:
            st.warning(plain(why))
        if st.button(f"Write to {R.place(ctx.env)}", type="primary", key=f"write-go-{rid}", disabled=not on):
            with st.spinner(f"Writing, and waiting up to {R.WAIT_S} s for the answer"):
                out = R.write(ctx.kept, ctx.review, ctx.entry, ctx.record, environment=ctx.env, confirmed_by=ctx.name,
                              catalogue=catalogue, offline=ctx.offline)
            st.session_state[f"outcome-{rid}"] = out
            if out["status"] == C.WRITTEN:
                st.session_state.pop(f"review-{rid}", None)
                ctx.store.refresh()                 # the run list then says this run is confirmed
            close_dialog()
            st.rerun()
    body()


def confirmed_view(store, run_id, record, version, get, page_key):
    """What was written, read only, beside the PDF."""
    outcome_box(run_id)
    earlier = earlier_confirmations(store, record, version)
    pdf = source_pdf(store, record)
    with st.container(border=True):
        st.markdown(f"**Confirmed**: written to **{R.place(version['environment'])}** on "
                    f"{plain(R.when_words(version['confirmed_at'], zone()))}.")
        labels = store.intake_labels(run_id)
        came_from(R.environment_for(record.get("organization_id"), labels), R.origin_words(labels))
        named("Confirmed by", text(version["confirmed_by"]))
        name = R.document_name(record, None if pdf is None else T.recognise(pdf))
        if version.get("entry_method") == C.MANUAL:
            st.caption(f"Document: {plain(name) if name else 'the design document'}. Entered by hand: nothing was read "
                       f"from it. {typed_words(version)} No design date.")
        else:
            st.caption(f"Document: {plain(name) if name else 'the design document'}"
                       f"{', typed from the scan' if version.get('entry_method') == 'transcription' else ''}, design "
                       f"dated {plain(version.get('design_date') or 'not printed')}.")
        st.caption(WRITTEN)
    left, right = st.columns([1, 1], gap="medium")
    with right:
        document_pane(store, record, page_key, pdf)
    with left, st.container(height=PANE, border=False):
        final_sections = version["pump_string"]["final_sections"]
        unwritten = final_sections and not dig(next(v for v in version["values"] if v["key"] == R.STRING), "final")
        rows = [{"value": R.label(v["key"]),
                 "as written": (WRITE_FILLED if v["key"] == C.DESIGN_DOCUMENT_REF else
                                ("kept on record below: not in the well's entry" if unwritten else "the string below")
                                if v["key"] == R.STRING and final_sections else
                                "empty" if v["final"] is None and v["status"] == C.CORRECTED else shown(v["final"])),
                 "from": "Write" if v["key"] == C.DESIGN_DOCUMENT_REF else R.SOURCE_WORDS.get(dig(v, "final", "source")),
                 "status": written_status(v),
                 "started as": STARTED_AS.get(v["starting_status"], v["starting_status"]),
                 "accepted anyway because": v.get("accept_anyway"),
                 "how it was decided": decided_words(v, earlier, by_hand=version.get("entry_method") == C.MANUAL)}
                for v in version["values"]]
        st.caption("Each value as written. Its status says who decided it: settled or corrected by an engineer, or "
                   "kept as read, which no one checked.")
        st.dataframe(cells(rows), hide_index=True)
        st.markdown("#### The pump string, kept on record: not in the well's entry" if unwritten else
                    "#### The pump string as written")
        history = next(v for v in version["values"] if v["key"] == R.STRING).get("history") or []
        st.dataframe(cells(section_rows(final_sections, R.carried_display(final_sections, history)[0])), hide_index=True)
        failed = [R.in_words(f"{c['check']}: {c['why']}") for c in version["pump_string"]["checks_on_final_sections"]
                  if c["result"] == "fail"]
        if failed:
            st.caption(plain(" ".join(dict.fromkeys(failed))))


def earlier_confirmations(store, record, version) -> dict:
    """{confirmation id: who confirmed it and when}, for each earlier confirmation a carried value came from."""
    sha, out = dig(record, "source", "pdf", "sha256"), {}
    for v in version["values"]:
        for s in v.get("history") or []:
            cid = (s.get("reason") or "").removeprefix("carried from ")
            if cid != (s.get("reason") or "") and cid not in out:
                try:
                    out[cid] = R.confirmation_words(C.load(store.objects.get(C.version_key(sha, cid))), zone())
                except Exception:  # noqa: BLE001 - not readable: the step says "an earlier confirmation"
                    out[cid] = None
    return out


WRITE_FILLED = "filled in by Write"                 # design_document_ref, as the review said it would be


def written_status(v) -> str:
    """A written value's status, in the review's badges' words: never settled for a value nobody checked, in this
    confirmation or in one it was kept from."""
    if v["status"] == C.SETTLED and not R.decided_by_someone(v.get("history")):
        return OUTSIDE_LOOK[2] if v["key"] in R.NOT_FROM_THE_DOCUMENT else R.KEPT_AS_READ
    return {C.SETTLED: "settled", C.CORRECTED: "corrected"}.get(v["status"], v["status"])


def typed_words(version) -> str:
    """What an entry by hand typed from its document: its values, counted, and the pump string."""
    n = sum(v["status"] == C.CORRECTED and v["final"] is not None for v in version["values"] if v["key"] != R.STRING)
    typed = [*([f"{n} value{'s' * (n != 1)}"] if n else []),
             *(["the pump string"] if version["pump_string"]["final_sections"] else [])]
    return (f"Typed from it: {R.join(typed)}; every other value was left empty." if typed else
            "Nothing was typed from it.")


def decided_words(v, earlier, by_hand=False) -> str | None:
    """How a written value was decided: its last step, in words.  Entered by hand, the proposal read nothing."""
    if v["key"] == C.DESIGN_DOCUMENT_REF:
        return "it points to this confirmation's record"
    if not v.get("history"):
        return None
    step = v["history"][-1]
    if by_hand and step.get("by") == "extraction":
        return UNTOUCHED
    return R.step_words(step, earlier.get((step.get("reason") or "").removeprefix("carried from ")), zone())


# ---- a new run, and watching it ----

def read_words(listed, shown: str) -> str:
    """When the form's list was read, in plain words, and the rows it left out."""
    at, n = listed.read_at.astimezone(zone() or timezone.utc), len(listed.pairs)
    skipped = (f" {listed.skipped} row{'s' * (listed.skipped != 1)} with no organisation or no well "
               f"{'were' if listed.skipped != 1 else 'was'} left out.") if listed.skipped else ""
    return (f"{shown}: {n} well{'s' * (n != 1)}, read from the product at {at:%H:%M} {R.zone_name(zone())} on {at.day} "
            f"{at:%B %Y}.{skipped}")


def forget_choice():
    """The switch moved: the organisation and well chosen from the other list go."""
    for key in ("new-org", "new-well", "new-typed"):
        st.session_state.pop(key, None)


def same_request(store, run_id, record, sha256, organisation, well, environment) -> bool:
    """Whether a run is for this PDF, organisation and well, and writes to this environment (`R.environment_for`, as
    its review does).  The upload's labels say, where it kept them: in S3 mode a running record names none yet."""
    labels = store.intake_labels(run_id)
    if labels is not None:
        same = (labels["sha256"], labels["organization-id"], labels["well-id"]) == (sha256, organisation, well)
    else:
        same = (dig(record, "source", "pdf", "sha256") == sha256 and record.get("organization_id") == organisation
                and record.get("well_id") == well)
    return same and R.environment_for(organisation, labels) == environment


def running_duplicate(store, sha256, organisation, well, environment):
    """(run id, state) of a live run of the same PDF, organisation and well in the same environment, or None: a run
    still running and not stale, or, in S3 mode, an upload not yet picked up that has not expired."""
    now = datetime.now(timezone.utc)
    for run_id in store.run_ids():
        try:
            record = store.record(run_id)
        except Exception:  # noqa: BLE001 - an unreadable record is no duplicate
            continue
        if run_status(record, now) == ("running", False) and same_request(store, run_id, record, sha256,
                                                                           organisation, well, environment):
            return run_id, "running"
    for run_id, (state, _) in waiting(store, now).items():
        if state != "expired" and same_request(store, run_id, {}, sha256, organisation, well, environment):
            return run_id, state
    return None


def show_upload(store):
    st.button("All runs", icon=":material/arrow_back:", on_click=goto, type="tertiary")
    st.title("New run from a PDF")
    st.write("Choose live or test wells, then the organisation and the well the design document belongs to, and "
             "upload the PDF. The run starts in the background, and you can watch it here.")
    shown = st.segmented_control("Wells", list(WELL_LISTS.values()), required=True, key="new-wells",
                                 default=st.session_state.get("wells-chosen", WELL_LISTS["prd"]),   # the side last
                                 on_change=forget_choice,                                           # chosen here
                                 help="Live wells are the product's production wells; test wells are its test "
                                      "system's, with lab-check.")
    st.session_state["wells-chosen"] = shown        # kept for the session: the form comes back on this side
    environment = next(e for e, words in WELL_LISTS.items() if words == shown)
    with st.spinner(f"Reading the {shown.lower()} from the product"):
        listed, err = wells.cached(environment)
    if err is not None:                     # the form stays off: no list is offered but the product's
        st.error(f"The {shown.lower()} could not be read from the product, so the form is off. Retry; if it fails "
                 f"again, ask {CONTACT}.", icon=":material/cloud_off:")
        st.button("Retry", icon=":material/refresh:", on_click=wells.refresh, args=(environment,), key="new-retry")
        if tech(f"the {shown.lower()} could not be read", err):
            with st.expander(TECH):
                st.code(f"{type(err).__name__}: {err}", language=None, wrap_lines=True)
    else:
        line = st.container(horizontal=True, vertical_alignment="center")
        line.caption(read_words(listed, shown))
        line.button("Refresh", icon=":material/refresh:", type="tertiary", on_click=wells.refresh, args=(environment,),
                    key="new-refresh")              # no tooltip: once pressed it would stay up, over the line beside it
    off = err is not None
    organisations = [] if off else [o for o in listed.organisations() if o != R.LAB_CHECK]
    if environment == "dev" and not off:                    # lab-check's wells are typed, never listed
        organisations = sorted({*organisations, R.LAB_CHECK}, key=wells.display_order)
    shown_orgs, names = wells.labels(organisations), []          # as every screen marks them, no two alike
    org = st.selectbox("Organisation", organisations, index=None, format_func=shown_orgs.__getitem__,
                       placeholder="Choose an organisation", disabled=off, key="new-org")
    if org == R.LAB_CHECK:
        typed = st.text_input("Well", key="new-typed", placeholder="Type the well's name",
                              help="Any name is taken, exactly as you type it: check its spelling.")
        well = typed if typed.strip() else None
    else:
        names = [] if org is None else listed.wells(org)
        shown_wells = wells.labels(names)
        well = st.selectbox("Well", names, index=None, format_func=shown_wells.__getitem__,
                            placeholder="Choose a well", disabled=org is None, key="new-well")
    near = wells.near(organisations) or wells.near(names)
    if line := " ".join(filter(None, [NEAR if near else "", wells.key([*organisations, *names])])):
        st.caption(line)                                          # one line: alike names, and what each sign means
    refused = [REFUSED[name] for name in intake.refused(org, well)]         # the upload's labels' own rule
    if refused:                             # said as soon as the name is chosen, before any upload; Start stays off
        st.error(f"No run can start for this {' and this '.join(refused)} yet, because "
                 f"{'its name holds' if len(refused) == 1 else 'each name holds'} a character a run can't carry.")
    upload = st.file_uploader("Design document (PDF)", type="pdf", disabled=off, key="new-pdf")
    data = upload.getvalue() if upload is not None else b""
    if upload is not None and not data:
        st.error("This file is empty.")
    elif upload is not None and not data.startswith(b"%PDF-"):
        st.error("This file is not a PDF: it does not start with %PDF-.")
    misnamed = upload is not None and bool(intake.refused(source_name=upload.name))   # the labels' own rule, too
    if misnamed:                            # said once the file is added; Start stays off
        st.error(RENAME)
    scan = T.recognise(data) if data.startswith(b"%PDF-") else None
    if scan is not None and scan.problem:
        st.error(f"This file is marked as a transcription, but {scan.problem}, so it is refused. Upload the file "
                 "exactly as the skill made it.")
    elif scan is not None:
        st.info(f"A transcription of {plain(scan.name)}, {scan.pages} page{'s' * (scan.pages != 1)} typed from the "
                "scan. The original scan is inside the file, and the review shows it beside the values.",
                icon=":material/document_scanner:")
    ready = (well is not None and not refused and not misnamed and data.startswith(b"%PDF-")
             and not (scan is not None and scan.problem))
    sha = hashlib.sha256(data).hexdigest()
    if ready and (dup := running_duplicate(store, sha, org, well, environment)):
        dup, state = dup
        st.warning(f"A run of this PDF for this organisation and well is still running, started "
                   f"{started_words({}, dup)}. You can start another." if state == "running" else
                   f"An upload of this PDF for this organisation and well has not started yet "
                   f"({WAIT_WORDS.get(state, state)}), since {started_words({}, dup)}. You can start another.")
    st.info(DURATION, icon=":material/schedule:")
    confirmed = st.checkbox("I've checked the organisation, the well and the document", disabled=not ready,
                            key="new-confirm")
    if st.button("Start the run", type="primary", disabled=not (ready and confirmed)):
        try:
            run_id = intake.new_run_id(datetime.now(timezone.utc))
            if MODE == "s3":
                intake.upload(store, run_id, data, org, well, upload.name, environment)
            else:                           # the labels first, where the store and the writer read them
                pdf = intake.save_pdf(store.root, data)
                intake.label(store.root, run_id, data, intake.labels(org, well, upload.name, sha, environment))
                intake.launch(store.root, pdf, org, well, run_id, upload.name)
        except Exception as err:  # noqa: BLE001 - say that nothing started; why is technical
            st.error(f"The run could not be started. Try again; if it fails again, ask {CONTACT}.")
            if tech("a run could not be started", err):
                with st.expander(TECH):
                    st.code(f"{type(err).__name__}: {err}", language=None, wrap_lines=True)
            return
        goto(watch=run_id)
        st.rerun()


def clock(seconds) -> str:
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{seconds:02d}"


def when(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def watch_now(store, run_id):
    """(state, record) as the watch view shows them."""
    store.refresh(run_id)                     # S3 mode: this run as it is now; the local store caches nothing
    try:
        record = store.record(run_id)
    except FileNotFoundError:
        record = None
    except Exception:  # noqa: BLE001 - written but not readable: keep watching
        return "unreadable", None
    now, uploaded = datetime.now(timezone.utc), store.uploaded_at(run_id)
    if record is None and uploaded is not None:                     # S3 mode: uploaded, no record yet
        waited = (now - uploaded).total_seconds()
        if waited > intake.PICKUP_S:
            store.refresh()                   # whether a run is running decides it: look at the runs again
        return intake.queue_state(waited, any(live(store, r, now) for r in store.run_ids())), None
    return intake.watch_state(record, run_id, now, intake.exited(run_id)), record


def render_watch(store, run_id, here=False) -> str:
    """The run's state in plain words, and what to do next.  What only whoever looks after the app needs is a
    technical section (`tech`): drawn with the switch on, logged with it off.  `here`: this is the run's own page
    (`plain_run`), so nothing offers to open it."""
    state, record = watch_now(store, run_id)
    if state in WAITING:
        uploaded = store.uploaded_at(run_id)
        seconds = (datetime.now(timezone.utc) - uploaded).total_seconds() if uploaded else 0
        waited = wait_text(seconds) if uploaded else "—"
        if state == "queued":                               # under a minute, how long says nothing
            st.info(f"Waiting to start{f', for {waited} so far' if seconds >= 60 else ''}: the upload is waiting its "
                    f"turn. {FOLLOWS}", icon=":material/hourglass_top:")
        elif state == "not picked up":
            st.warning(f"Not picked up: the upload has waited {waited}, while no run is running. Upload the PDF again; "
                       f"if that upload is not picked up either, tell {CONTACT}.")
            if tech("an upload was not picked up", f"run {run_id}, queued for {waited}", key=("not picked up", run_id)):
                with st.expander(TECH):
                    st.markdown(f"Look for run id `{run_id}` in the run log (DynamoDB `esp_design_extraction_logging`,"
                                " as its `pipeline_execution_id`): a `FAILED` row there means the function could not "
                                "start the run. Its CloudWatch logs (`/aws/lambda/esp-design-extraction`) say more.")
        else:
            st.error(f"Expired: the upload waited {waited}, longer than a waiting upload is kept. It will not run: "
                     "upload the PDF again.")
    elif state == "starting":
        st.info(f"Starting: the run is getting under way. {FOLLOWS}", icon=":material/hourglass_top:")
    elif state == "no record yet":
        age = int(intake.age_s(run_id, datetime.now(timezone.utc)))
        st.info(f"Still starting, after {age} s. {FOLLOWS}", icon=":material/hourglass_top:")
    elif state == "unreadable":
        st.warning("The run's status cannot be read yet. Checking again.")
    elif state == "launch failed":
        reason = ("it stopped as soon as it started" if intake.exited(run_id) else
                  f"nothing was heard from it for {intake.STARTUP_S} s")
        st.error(f"The run did not start: {reason}. Upload the PDF again; if that run does not start either, tell "
                 f"{CONTACT}.")
        if tech("a run did not start", f"run {run_id}: {reason}; what it printed is in the run store, "
                                       f"launch/{run_id}.log", key=("launch failed", run_id)):
            with st.expander("Launch log (technical)"):
                try:
                    lines = store.launch_log(run_id).decode("utf-8", "replace").splitlines()
                    st.caption("The last 50 lines of the launch log:")
                    st.code("\n".join(lines[-50:]) or "(empty)", language=None, wrap_lines=True)
                except FileNotFoundError:
                    st.caption("There is no launch log for this run.")
    else:
        pipeline_cell(record, technical=False)
        start, deadline = when(record.get("started_at")), when(record.get("deadline_at"))
        end = when(record.get("ended_at")) or datetime.now(timezone.utc)
        if start and (deadline or not SHOW):                            # its limit: a technical detail
            st.metric("Elapsed", clock((end - start).total_seconds())
                      + (f" of {clock((deadline - start).total_seconds())}" if SHOW else ""))
        failed = run_status(record)[0] == "failed"                      # stale too
        if state == "running":
            st.caption(f"The document is being read, which takes 3 to 8 minutes. {FOLLOWS} When it finishes, open the "
                       "run to check its values and confirm them.")
        else:
            words = review_words(store, run_id, record, store.intake_labels(run_id), confirmations(store))
            review_status(words)
            entry = run_entry(lambda name: blob(store, run_id, name), record)
            if words.startswith("confirmed"):
                st.caption("This run is confirmed. Open the run to see what was written.")
            elif failed or entry is None:                               # what happened, and what can be done
                failed_screen(store, run_id, record)
            else:
                st.caption("The document has been read. Open the run to check its values against the document, and "
                           "confirm them.")
            if not here and (SHOW or entry is not None or words.startswith("confirmed")):   # the switch off: only
                                                                        # a run with a review, or a confirmed one
                st.button("Open the run", type="secondary" if failed else "primary", on_click=goto,
                          kwargs={"run": run_id})
        error = dig(record, "error")
        if SHOW:
            with st.expander("Run record (technical)"):
                error_line(record)
                st.json({k: record.get(k) for k in ("run_id", "status", "started_at", "deadline_at", "ended_at",
                                                    "spec_validation_status")})
        elif failed:                                                    # the record's error, to the log instead
            hosted.hidden(f"run {run_id} failed",
                          f"{text(dig(error, 'code'))} at {text(dig(error, 'stage'))}: {text(dig(error, 'message'))}"
                          if isinstance(error, dict) else
                          f"stale: still recorded as running after its deadline, {text(record.get('deadline_at'))}",
                          key=("run failed", run_id))
    return state


def failed_screen(store, run_id, record):
    """A failed upload's last screen: what happened, in plain words (a scan's own message for an image-only one), and
    Enter by hand when the run offers it as on its page (`R.by_hand`, `show_run`), opening the same flow there."""
    entry_stored = stored(lambda name: blob(store, run_id, name), C.ENTRY)
    image_only(record)
    if R.by_hand(record, entry_stored=entry_stored, pdf_found=source_pdf(store, record) is not None):
        by_hand_offer(run_id, record, go=True)
    elif dig(record, "error", "code") != "IMAGE_ONLY":
        st.info(plain(R.failed_words(record, entry_stored=entry_stored)), icon=":material/info:")


def watch_title(store, run_id, labels):
    """The run as the engineer knows it, from its record or else its upload's labels: its document, organisation, well,
    where it writes (Live or Test wells) and its start; never its id."""
    try:
        record = store.record(run_id)
    except Exception:  # noqa: BLE001 - no record yet, or not readable yet: the labels name it, or nothing does
        record = {}
    labels = labels or {}
    org, well = dig(record, "organization_id") or labels.get("organization-id"), \
        dig(record, "well_id") or labels.get("well-id")
    name = document(record, labels)
    st.subheader(plain(name) if name != "—" else "A new run")
    st.caption(" · ".join([*([f"Organisation {plain(wells.marked(org))}"] if org else []),    # no emphasis: an
                           *([f"well {plain(wells.marked(well))}"] if well else []),           # edge space breaks it
                           *([side(org, labels)] if org else []),
                           f"started {started_words(record, run_id)}"]))
    if signs := wells.key([n for n in (org, well) if n]):
        st.caption(signs)
    if org:
        came_from(R.environment_for(org, labels), R.origin_words(labels))


def show_watch(store, run_id) -> bool:
    """Watching a run: True, an engineer's screen, whose sidebar names no bucket."""
    st.button("All runs", icon=":material/arrow_back:", on_click=goto, type="tertiary")
    if run_id not in store.run_ids() and run_id not in store.launch_ids():
        st.warning(NO_SUCH_RUN)
        return True
    return watch(store, run_id)


def watch(store, run_id, here=False) -> bool:
    """The run by name, then its state (`render_watch`), followed until it is final."""
    watch_title(store, run_id, store.intake_labels(run_id))
    if watch_now(store, run_id)[0] in FINAL:
        render_watch(store, run_id, here)
        return True

    @st.fragment(run_every=POLL_S)
    @guarded
    def live():
        if render_watch(store, run_id, here) in FINAL:
            st.rerun()                      # final: render it once more, without the timer
    live()
    return True


def route(store) -> bool | None:
    """Draws the page the query names.  True for an engineer's screen (the run list, the upload form, watching a run,
    a run's review), whose sidebar says only when the runs were read.  The run list's page opens on a session's first
    page (a new visit or a reload) and on coming back to it from another page of the app; any other run of the script
    on it is a rerun within the page."""
    listing = not ("upload" in st.query_params or st.query_params.get("watch") or st.query_params.get("run"))
    opened, st.session_state["on-list"] = listing and not st.session_state.get("on-list"), listing
    if "upload" in st.query_params:
        show_upload(store)
        return True
    if st.query_params.get("watch"):
        return show_watch(store, st.query_params["watch"])
    if st.query_params.get("run"):
        return show_run(store, st.query_params["run"])
    show_list(store, opened)
    return True


def s3_sidebar(store, technical=True):
    """Where S3 mode reads, and Refresh.  Beside an engineer's screen, only when the runs were read."""
    with st.sidebar:
        listed = store.listed_at
        if technical:
            st.caption(f"S3 mode: runs under `s3://{store.bucket}/{store.runs_prefix}`, uploads under "
                       f"`{store.intake_prefix}`." + (f" Listed at {listed:%H:%M:%S} UTC." if listed else ""))
        else:                                   # what Refresh does is said here, not in a tooltip: a pressed
            asof = (f"The run list is as of {listed.astimezone(zone() or timezone.utc):%H:%M} "    # button's stays up
                    f"{R.zone_name(zone())}. ") if listed else ""
            st.caption(f"{asof}Refresh reads it again.")
        st.button("Refresh", icon=":material/refresh:", on_click=store.refresh,
                  help="List the bucket again, and re-read every run that is not final" if technical else None)


st.set_page_config(page_title="Design-document runs", page_icon=":material/description:", layout="wide")
# An error outside the page's own handling (a button's callback): Streamlit's traceback, or, with the switch off, its
# one line that says nothing of the error; Streamlit logs the traceback to standard error either way.
st.set_option("client.showErrorDetails", "full" if SHOW else "none")
try:
    hosted.role()                           # one of the two role settings without the other: said before any AWS call
    STORE, NOT_SET, COMMAND = (local_store_from_env() if MODE == "local" else s3_store_from_env() if MODE == "s3"
                               else None), None, None
except hosted.SetUp as err:
    STORE, NOT_SET, COMMAND = None, str(err), None
except Exception as err:  # noqa: BLE001 - S3 mode's session not built (the role not assumed): as runs not read
    st.title("Design-document runs")
    failed(err)
    st.stop()
if STORE is None:
    st.title("Design-document runs")
    if NOT_SET is None and MODE == "local":
        NOT_SET, COMMAND = (f"Set {STORE_ENV} to a run store directory, then start the viewer again.",
                            f"{STORE_ENV}=<run store dir> streamlit run app/main.py")
    elif NOT_SET is None and MODE == "s3":
        NOT_SET = (f"S3 mode reads the bucket the Lambda writes: set {', '.join(S3_ENV)} as the stack sets them, "
                   "each prefix ending in /, then start the viewer again.")
    elif NOT_SET is None:
        NOT_SET = f"{MODE_ENV} is {plain(MODE)}: it must be local or s3."
    if not tech("the app is not set up", NOT_SET, key=("set-up", NOT_SET)):     # which setting: to the log
        st.error(f"This app is not set up yet, so it has nothing to show. Tell {CONTACT}.")
    elif MODE in ("local", "s3"):
        st.info(NOT_SET)
        if COMMAND:
            st.code(COMMAND, language="bash")
    else:
        st.error(NOT_SET)
else:
    if MODE == "s3" and "catalogue-asked" not in st.session_state:
        st.session_state["catalogue-asked"] = True      # a session asks once; a process fetches it once
        try:
            hosted.fetch_catalogue(STORE.client, STORE.bucket)
        except Exception as err:  # noqa: BLE001 - no catalogue: the review says so where it needs one, the log why
            hosted.hidden("the pump catalogue could not be fetched", err)
    engineer = not SHOW or not st.query_params.get("run")   # a run's own page is technical until it opens on a review
    try:
        engineer = route(STORE) is True or not SHOW
    except Exception as err:  # noqa: BLE001 - plain words, the rest technical
        failed(err)
    if MODE == "s3":
        s3_sidebar(STORE, technical=not engineer)
