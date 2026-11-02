"""Run viewer: every run in the run store, and each run in full; its upload form starts a run, and a run's proposed
well entry is reviewed and confirmed here.

    ESP_RUN_STORE_DIR=<run store dir> streamlit run app/main.py              # local mode, the default
    ESP_APP_MODE=s3 ESP_BUCKET=… ESP_INTAKE_PREFIX=… ESP_RUNS_PREFIX=… streamlit run app/main.py

Local mode reads a run store directory and starts a run as a process.  S3 mode reads the bucket the
Lambda writes and starts a run by uploading to its intake prefix (README, "Run the app against S3").
The store layout is the one `extraction/run.py` writes (README, "Running a document").
Each panel reads its own file and reports a missing, empty or malformed file in place,
so a failed run still shows everything it stored.

A run with a well_entry.json opens in "Review & confirm" until it has a written confirmation, then shows what was
written; its files are under "Run details".  The decisions are app/review.py's; S3 mode writes through the writer
Lambda, local mode only reviews unless ESP_CONFIRM_OFFLINE_AS names who writes into the local store (README,
"Confirming an entry").
"""

from __future__ import annotations

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

import pandas as pd
import plotly.graph_objects as go
import pypdfium2 as pdfium
import streamlit as st

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:              # `streamlit run` puts only app/ on the path
    sys.path.insert(0, ROOT)

from app import intake  # noqa: E402
from app import review as R  # noqa: E402
from app.store import (MODE_ENV, PDF_DIRS_ENV, S3_ENV, STORE_ENV, local_store_from_env, run_status,  # noqa: E402
                       s3_store_from_env)
from app.wells import WELL_LIST_ENV, SnapshotError, load as load_wells  # noqa: E402
from extraction import confirmation as C  # noqa: E402
from extraction.catalogue import CatalogueNames  # noqa: E402
from extraction.stage.reports_reader import read_report  # noqa: E402
from extraction.stage.stage import FAMILY_LABEL  # noqa: E402
from extraction.validate.rules import page_numbers  # noqa: E402
from extraction.validate.validate import catalogue as read_catalogue  # noqa: E402

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
COST = ("This calls Opus on Bedrock in the datascience account; recent runs cost about USD 0.55–1.32 "
        "and take 3–8 minutes.")
POLL_S = 3
FINAL = {"launch failed", "succeeded", "failed", "stale", "expired"}
WAITING = ("queued", "not picked up", "expired")        # S3 mode: an upload with no run record yet
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


def family(record) -> str:
    code = dig(record, "family")
    return f"{code} ({FAMILIES[code]})" if isinstance(code, str) and code in FAMILIES else text(code)


def started(record, run_id) -> str:
    when = dig(record, "started_at")
    if isinstance(when, str):
        return when.replace("T", " ").rstrip("Z")
    return f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]} {run_id[9:11]}:{run_id[11:13]}:{run_id[13:15]}"


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


def list_rows(store) -> pd.DataFrame:
    now, latest, rows = datetime.now(timezone.utc), set(), []
    runs = set(store.run_ids())
    queue = waiting(store, now)                                              # S3 mode: uploads with no record
    for run_id in sorted(runs | set(store.launch_ids()), reverse=True):     # newest first
        row = {"Run id": run_id, "Started (UTC)": started({}, run_id), "Organisation": "—", "Well": "—",
               "Latest": "", "Family": "—", "Route": "—", "Pipeline": "", "Document review": "—",
               "Error": "", "Cost (USD)": None}
        if run_id in queue:
            (state, waited), labels = queue[run_id], store.intake_labels(run_id) or {}
            rows.append({**row, "Organisation": text(labels.get("organization-id")),
                         "Well": text(labels.get("well-id")), "Pipeline": state, "Waited": wait_text(waited)})
            continue
        if run_id not in runs:
            rows.append({**row, "Pipeline": "launched, no record"})
            continue
        try:
            record = store.record(run_id)
            org, well, error = dig(record, "organization_id"), dig(record, "well_id"), dig(record, "error")
            if org is None and (labels := store.intake_labels(run_id)):      # S3: a running record names neither
                org, well = labels["organization-id"], labels["well-id"]
            run = pipeline(record, now)
            key = (text(org), text(well))
            is_latest = run == "completed" and well is not None and key not in latest
            latest |= {key} if is_latest else set()
            row.update({
                "Started (UTC)": started(record, run_id),
                "Organisation": text(org),
                "Well": text(well, "no well"),
                "Latest": "latest" if is_latest else "",
                "Family": family(record),
                "Route": route_of(record),
                "Pipeline": run,
                "Document review": review_line(record),
                "Error": f"{text(dig(error, 'code'))} at {text(dig(error, 'stage'))}" if isinstance(error, dict) else "",
                "Cost (USD)": cost if isinstance(cost := dig(record, "cost_usd"), (int, float)) else None,
            })
        except FileNotFoundError:
            row["Pipeline"] = "no run.json"
        except Exception:  # noqa: BLE001 - an unreadable record still gets its row
            row["Pipeline"] = "run.json unreadable"
        rows.append(row)
    return pd.DataFrame(rows)


def show_list(store):
    st.title("Design-document runs")
    st.write("Each run takes one design document through detection, extraction, checks and storage. "
             "**Pipeline** says whether extraction ran. **Document review** says whether the values need an "
             "engineer's check.")
    st.button("New run from a PDF", icon=":material/upload_file:", on_click=goto, kwargs={"upload": "1"})
    df = list_rows(store)
    if df.empty:
        st.info("This run store has no runs yet.")
        return
    filters = {"Organisation": "org", "Well": "well", "Route": "route", "Pipeline": "status"}
    picked = {col: box.multiselect(col, sorted(df[col].unique()), key=key, placeholder="All", bind="query-params")
              for box, (col, key) in zip(st.columns(4), filters.items())}
    view = df
    for col, chosen in picked.items():
        if chosen:
            view = view[view[col].isin(chosen)]
    st.caption(f"{len(view)} of {len(df)} runs, newest first. Click a run id to open the run.")
    st.dataframe(view, hide_index=True, key="runs", column_config={
        "Run id": st.column_config.ButtonColumn(key="open-run", on_click=open_run, type="tertiary",
                                                alignment="left", help="Open this run"),
        "Latest": st.column_config.TextColumn(help="The newest completed run for its organisation and well"),
        "Family": st.column_config.TextColumn(help="`family`: the vendor template, as F-code (vendor)"),
        "Route": st.column_config.TextColumn(
            help="`route`: template (the vendor's contract) or generic (no template: every value needs review)"),
        "Pipeline": st.column_config.TextColumn(
            help="`status` and `error.stage`: did extraction run? A `running` record past its `deadline_at` reads "
                 "as failed (stale)."),
        "Document review": st.column_config.TextColumn(
            help="`review.needs_review`: values that need an engineer's check. Separate from the pipeline."),
        "Error": st.column_config.TextColumn(help="`error.code` at `error.stage`"),
        "Cost (USD)": st.column_config.NumberColumn(
            format="$%.2f", help="`cost_usd`: the model call's cost; empty without a model call"),
        **({"Waited": st.column_config.TextColumn(
            help=f"S3 mode: how long an upload has waited for the function. Queued; not picked up after "
                 f"{intake.PICKUP_S // 60} min with no run running; expired after {intake.EXPIRY_S // 3600} h")}
           if "Waited" in view else {}),
    })


# ---- one run ----

def goto(**view):
    """Switch views: drop the current view's parameter and set the next one's (none: the run list)."""
    for key in ("run", "watch", "upload"):
        if key in st.query_params:
            del st.query_params[key]
    for key, value in view.items():
        st.query_params[key] = value


def open_run():
    click = st.session_state.get("open-run")
    if click:
        goto(run=click["label"])


def status_cell(label, shown, look, field, raw):
    """The plain-language status, in colour with an icon, above the raw field it comes from."""
    color, icon = look
    st.markdown(f"#### {label}: :{color}[{icon} {plain(shown)}]")
    st.caption(f"`{field}` = {plain(text(raw, 'null'))}")


UNKNOWN_LOOK = ("gray", ":material/help:")


def pipeline_cell(record):
    status, stale = run_status(record) if record else (None, False)
    status_cell("Pipeline", pipeline(record) if record else "unknown",
                ("red", ":material/timer_off:") if stale else RUN_LOOK.get(text(status), UNKNOWN_LOOK),
                "status", dig(record, "status"))


def review_cell(record):
    shown = review_line(record) if record else "unknown"
    look = REVIEW_LOOK.get("no issues" if shown == "no issues" else "to check" if shown.endswith("to check") else "",
                           UNKNOWN_LOOK)
    status_cell("Document review", shown, look, "spec_validation_status", dig(record, "spec_validation_status"))


def error_line(record):
    error = dig(record, "error")
    if isinstance(error, dict):
        st.error(f"**{plain(text(error.get('code')))}** at {plain(text(error.get('stage')))}: "
                 f"{plain(text(error.get('message')))}")


def title_block(run_id, record, problem, labels=None):
    """`labels`: in S3 mode the upload's, for a record that does not name its organisation and well yet."""
    org, well = dig(record, "organization_id"), dig(record, "well_id")
    if org is None and labels:
        org, well = labels["organization-id"], labels["well-id"]
    with st.container(border=True):
        st.subheader(f"Run {plain(run_id)}")
        if dig(record, "route") == "generic":
            st.badge(GENERIC_BADGE, icon=":material/rate_review:", color="orange")
            st.caption(plain(generic_context(record)))
        if problem:
            st.warning(problem)
        with panel("run.json", record):
            cost = dig(record, "cost_usd")
            for col, (label, value) in zip(st.columns([2, 3, 2, 3, 2]), [
                    ("Organisation", text(org)),
                    ("Well", text(well, "no well")),
                    ("Family", family(record)),
                    ("Started (UTC)", started(record, run_id)),
                    ("Cost", f"${cost:.2f}" if isinstance(cost, (int, float)) else text(cost, "no model call"))]):
                col.metric(label, value)
            status, stale = run_status(record) if record else (None, False)
            left, right = st.columns(2)
            with left:
                pipeline_cell(record)
            with right:
                review_cell(record)
            st.info(BANNER)
            error_line(record)
            if stale:
                st.warning(f"Still recorded as running, but its deadline ({plain(dig(record, 'deadline_at'))}) has "
                           "passed, so it reads as failed. The process most likely stopped before writing its "
                           "final record.")
            elif status == "running":
                st.info(f"Still running. It reads as failed if it has not finished by "
                        f"{plain(dig(record, 'deadline_at'))}.")


def summary_tab(record):
    with panel("run.json", record):
        if not record:
            st.info("No run record to summarise.")
            return
        model = dig(record, "model")
        st.markdown("#### What ran")
        facts([
            ("Mode", {"pdf": "PDF: full chain, one model call",
                      "report": "existing report: no model call"}.get(dig(record, "mode"), dig(record, "mode")),
             "mode"),
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
            *([("Model", "no model call", "model")] if not isinstance(model, dict) else [
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


def show_run(store, run_id):
    if run_id not in store.run_ids() and run_id in store.launch_ids():
        return show_watch(store, run_id)                 # launched, no record yet
    st.button("All runs", icon=":material/arrow_back:", on_click=goto, type="tertiary")
    if run_id not in store.run_ids():
        st.warning(f"There is no run {plain(run_id)} in this store.")
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

    page_key = f"page-{run_id}"
    entry = stored_entry(get) if record and dig(record, "well_id") is not None else None
    if entry is None:
        title_block(run_id, record, problem, store.intake_labels(run_id))
        return run_details(store, get, record, page_key)
    confirmed = store.latest_version(dig(record, "source", "pdf", "sha256"), record.get("organization_id"),
                                     record["well_id"], run_id=run_id)
    views = ("Confirmed entry" if confirmed else "Review & confirm", "Run details (technical)")
    view = st.segmented_control("View", views, default=views[0], required=True, label_visibility="collapsed",
                                key=f"view-{run_id}-{bool(confirmed)}")
    if view == views[1]:
        st.caption(TECHNICAL)
        title_block(run_id, record, problem, store.intake_labels(run_id))
        return run_details(store, get, record, page_key)
    if confirmed:
        confirmed_view(store, run_id, record, confirmed, get, page_key)
    else:
        review_view(store, run_id, record, entry, get, page_key)
    return True                                     # an engineer's screen: the sidebar names no bucket


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
GROUP_HINT = {"found": "Read from the document, to check against it.",
              "not found": "Nothing was read: settle each as empty if the document does not print it."}
BATCH_TITLE = {"string": "The pump string", "found": "Values found", "not found": "Values not found"}
TECHNICAL = "The pipeline's own files for this run, exactly as it stored them: for checking the pipeline, not the design."
NO_CATALOGUE = ("The pump catalogue could not be read on this computer, so models cannot be picked or checked here. "
                "Whoever runs the app should check its set-up.")
ROLE_WORDS = {None: "none", "primary": "primary", "taper": "taper", "gas_handler": "gas handler"}
PRINTED_HELP = ("Give every name the document prints for this section, as printed: the equipment list's and the "
                "schematic's, for instance, separated by semicolons.")
NO_PDF = ("The document's PDF is not available here, so its pages cannot be shown. Check the values against your own "
          "copy.")
BAD_PDF = "The document's PDF could not be opened, so its pages cannot be shown. Check the values against your own copy."


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
    except Exception:  # noqa: BLE001 - no snapshot on this computer: the string's screens say so
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
        org, well = record.get("organization_id"), record["well_id"]
        environment, kept = R.environment_for(org), R.Kept(store.objects)
        version = store.latest_version(dig(record, "source", "pdf", "sha256"), org, well)
        st.session_state[key] = {
            "entry": entry, "kept": kept, "environment": environment,
            "review": R.start(entry, record, version, at=now_text(), names=catalogue_names()[1]),
            "version": version and {k: version.get(k) for k in ("confirmation_id", "confirmed_by", "confirmed_at")},
            "dates": R.design_dates(kept, run_id, store.current_entry(environment, org, well))}
    return st.session_state[key]


def context(store, run_id, record, state, page_key) -> SimpleNamespace:
    if "confirmer" not in st.session_state:                     # read once per app session
        st.session_state["confirmer"] = R.confirmer(MODE, os.environ, getattr(store, "root", None))
    name, who = st.session_state["confirmer"]
    sha, pdf = dig(record, "source", "pdf", "sha256"), source_pdf(store, record)
    models, names = catalogue_names()
    return SimpleNamespace(run_id=run_id, record=record, entry=state["entry"], items=R.reviewed(state["entry"]),
                           review=state["review"], kept=state["kept"], env=state["environment"],
                           dates=state["dates"], version=state["version"], name=name, who=who,
                           offline=MODE == "local" and name is not None, sha=sha, pdf=pdf, page_key=page_key,
                           models=models, names=names)


def source_pdf(store, record) -> bytes | None:
    """The run's source PDF, or None when it is not where the store keeps it (the panes say so in plain words)."""
    sha = dig(record, "source", "pdf", "sha256")
    try:
        return store.source_pdf(sha, dig(record, "source", "pdf", "path")) if sha else None
    except Exception:  # noqa: BLE001 - not there, or not readable: the pane says the pages cannot be shown
        return None


def document_pane(store, record, page_key, pdf=None):
    """The source document beside an engineer's screen: its file name and its pages, nothing technical."""
    st.markdown("#### Source document")
    name = R.document_name(record)
    if name:
        st.caption(plain(name))
    sha = dig(record, "source", "pdf", "sha256")
    pdf = pdf if pdf is not None else source_pdf(store, record)
    if pdf is None:
        st.info(NO_PDF)
        return
    try:
        total = page_count(sha, pdf)
        page = st.number_input("Page", min_value=1, step=1, key=page_key)
        if page > total:
            st.warning(f"Page {page} is past the end of this {total}-page document.")
            return
        with st.container(height=PANE - 120, border=False):
            st.image(page_png(sha, page - 1, pdf), caption=f"Page {page} of {total}", width="stretch")
    except Exception:  # noqa: BLE001 - a PDF that will not open: say so, the review carries on
        st.warning(BAD_PDF)


def gate(ctx) -> tuple[bool, str]:
    """(whether Write is on, why not).  On only when the writer would accept the request as it stands, and the
    string's editor holds no unsaved edits."""
    if R.unsaved(ctx.review):
        return False, " ".join(filter(None, (ctx.who if ctx.name is None else "", R.UNSAVED)))
    groups = R.groups(ctx.review, ctx.entry)
    left = sum(len(groups[g]) for g in R.GROUPS)
    waiting = f"{left} value{'s' * (left != 1)} left to check" if left else ""
    if groups["string"] and R.blocking(ctx.review, ctx.entry, R.STRING):
        waiting += ", the string among them: it blocks publish"
    if ctx.name is None:
        return False, " ".join(filter(None, (ctx.who, f"{R.sentence_case(waiting)}." if waiting else "")))
    if left:
        return False, waiting
    try:
        reasons = R.check(ctx.kept, ctx.review, ctx.entry, ctx.record, confirmed_by=ctx.name, environment=ctx.env,
                          catalogue=catalogue, at=datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - e.g. no catalogue snapshot: say why Write is off
        return False, ("Write is off: the checks before writing could not run on this computer. Whoever runs the app "
                       "should check its set-up.")
    return (False, "Write is off. " + " ".join(dict.fromkeys(map(R.refusal_words, reasons)))) if reasons else (True, "")


def next_step(review, entry, after=None) -> str:
    """The next pop-up with something in it, after `after`: Write when none is left."""
    groups, steps = R.groups(review, entry), [*R.GROUPS, "write"]
    return next(s for s in steps[steps.index(after) + 1 if after else 0:] if s == "write" or groups[s])


def start_batch(run_id):
    state = st.session_state[f"review-{run_id}"]
    open_dialog(run_id, next_step(state["review"], state["entry"]))


def decide(run_id, how, key):
    """A decision made one by one: settle, or settle as empty."""
    state = st.session_state[f"review-{run_id}"]
    how(state["review"], state["entry"], key, at=now_text())


def accept(run_id, key):
    state = st.session_state[f"review-{run_id}"]
    R.accept_anyway(state["review"], state["entry"], key, st.session_state.get(f"anyway-{run_id}-{key}") or "",
                    at=now_text())


def save_correction(run_id, key):
    ss, base = st.session_state, f"fix-{run_id}-{key}"
    state = ss[f"review-{run_id}"]
    value, problem = R.parse_value(key, ss.get(f"{base}-value"))
    ss[f"{base}-problems"] = [problem] if problem else R.correct(
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
        st.caption(f"The well's current entry in {R.place(ctx.env)} is dated {d['current'] or 'nothing'}; this document "
                   f"{'is dated ' + d['incoming'] if d['incoming'] else 'prints no design date'}: can't compare.")
    elif d["case"] == "not older":
        st.caption(f"The well's current entry in {R.place(ctx.env)} is dated {d['current']}; this document is not older.")


def review_header(ctx, on, why):
    org, well = ctx.record.get("organization_id"), ctx.record["well_id"]
    with st.container(border=True):
        facts_ = st.container(horizontal=True, gap="large")
        for label, value in (("Organisation", visible(text(org))), ("Well", visible(well)),
                             ("Writes to", R.place(ctx.env)), ("Report", R.report_words(ctx.record)),
                             ("Design date", ctx.dates["incoming"] or "not printed"),
                             ("Confirming as", ctx.name or "nobody")):
            with facts_.container(gap=None, width="content"):
                st.caption(label)
                st.markdown(f"**{plain(value)}**")
        notes = st.container(horizontal=True, vertical_alignment="center")
        if dig(ctx.record, "route") == "generic":
            notes.badge(R.NOT_KNOWN, icon=":material/rate_review:", color="orange")
        if ctx.who:
            notes.caption(plain(ctx.who))
        if problem := R.run_problem_words(ctx.record):     # a run that stopped after reading keeps its entry
            st.warning(plain(problem), icon=":material/report:")
        dates_note(ctx)
        if ctx.version:
            carried = sum(v["carried"] for v in ctx.review["values"].values())
            st.caption(f"Confirmed before: {carried} value{'s' * (carried != 1)} come{'s' * (carried == 1)} back from "
                       f"{plain(R.confirmation_words(ctx.version))}.")
        buttons = st.container(horizontal=True, vertical_alignment="center")
        nothing = next_step(ctx.review, ctx.entry) == "write"
        buttons.button("Review in batches", icon=":material/done_all:", key=f"batch-{ctx.run_id}", disabled=nothing,
                       on_click=start_batch, args=(ctx.run_id,),
                       help="Nothing is left to batch" if nothing else "The string, then the values found, then those not found")
        buttons.button(f"Write to {R.place(ctx.env)}", type="primary", icon=":material/save:", key=f"write-{ctx.run_id}",
                       disabled=not on, on_click=open_dialog, args=(ctx.run_id, "write"))
        if not on:
            buttons.caption(plain(why))


def value_line(ctx, key):
    """One value: its label, value and unit, printed text, page chips, status, reasons, and what can be done."""
    v, item, rid = ctx.review["values"][key], ctx.items[key], ctx.run_id
    with st.container(border=True, key=f"line-{rid}-{key}"):
        head = st.container(horizontal=True, vertical_alignment="center", gap="small")
        head.markdown(f"**{plain(R.label(key))}**")
        head.markdown(plain(shown(v["final"])) if v["final"] is not None else ":gray[*not found*]")
        if item.get("printed") is not None:
            head.caption(f"printed “{plain(item['printed'])}”")
        chips(head, item.get("page"), ctx.page_key, f"chip-{rid}-{key}")
        status_badges(ctx, head, key)
        if v["moved"]:
            st.caption(f"The new run reads {plain(shown(R.proposal(ctx.entry, key)))}; the correction above is carried "
                       "from the earlier confirmation.")
        words = [R.in_words(r) for r in item["reasons"]] + ([f"Note: {R.note_words(item['note'])}"] if item.get("note") else [])
        if words:
            st.caption(plain(" ".join(dict.fromkeys(words))))
        if key != C.DESIGN_DOCUMENT_REF:
            actions(ctx, key)


def status_badges(ctx, parent, key):
    v = ctx.review["values"][key]
    color, icon, label = STATUS_LOOK.get(v["status"], ("gray", ":material/help:", v["status"]))
    parent.badge(label, icon=icon, color=color)
    if v["accept_anyway"]:
        parent.badge("accepted anyway", icon=":material/gavel:", color="red", help=v["accept_anyway"])
    if (n := R.pulled_out(ctx.review, key)) is not None:
        parent.badge(f"pulled out of batch {n}", icon=":material/undo:", color="violet")
    if v["carried"]:
        parent.badge("carried", icon=":material/history:", color="gray",
                     help=f"From {R.confirmation_words(ctx.version or {})}")


def actions(ctx, key):
    """Settle, correct, settle as empty, accept anyway: what the value's state allows."""
    v, rid = ctx.review["values"][key], ctx.run_id
    proposed, blocks = R.proposal(ctx.entry, key), R.blocking(ctx.review, ctx.entry, key)
    row = st.container(horizontal=True, gap="small")
    as_proposed = v["status"] == C.SETTLED and v["final"] == proposed and not v["accept_anyway"]
    if not R.proposal_blocks(ctx.entry, key) and not as_proposed:
        row.button("Settle as proposed" if key == R.STRING else "Settle", icon=":material/check:",
                   key=f"settle-{rid}-{key}", on_click=decide, args=(rid, R.settle, key))
    if key != R.STRING:
        with row.popover("Correct", icon=":material/edit:", key=f"fixpop-{rid}-{key}"):
            correction_form(ctx, key)
        if (v["final"] is not None or v["status"] in R.UNDECIDED) and not (blocks and proposed is None):
            row.button("Settle as empty", icon=":material/do_not_disturb_on:", key=f"empty-{rid}-{key}",
                       on_click=decide, args=(rid, R.settle_empty, key))
    if blocks and not (v["accept_anyway"] and v["status"] in C.FINAL):
        with row.popover("Accept anyway", icon=":material/gavel:", key=f"anywaypop-{rid}-{key}"):
            why = st.text_area("Why accept it uncorrected?", key=f"anyway-{rid}-{key}",
                               placeholder="Recorded with the value as its accept-anyway reason")
            st.button("Accept anyway", type="primary", key=f"anyway-go-{rid}-{key}", disabled=not (why or "").strip(),
                      on_click=accept, args=(rid, key))
    if key == R.STRING:
        row.button("Edit sections", icon=":material/table_edit:", key=f"sections-{rid}", on_click=edit,
                   args=(rid, R.open_editor), help="Edit the string's sections here, beside the document")


def correction_form(ctx, key):
    base, v = f"fix-{ctx.run_id}-{key}", ctx.review["values"][key]
    current = v["final"] or R.proposal(ctx.entry, key) or {}
    st.text_input("Value", value=text(current.get("value"), ""), key=f"{base}-value")
    st.selectbox("Unit", R.allowed_units(key), key=f"{base}-unit", help="The units the product takes for this value")
    st.selectbox("Source", R.SOURCES, index=R.SOURCES.index("ops_entered") if key == "vapor_pressure_psia" else 0,
                 format_func=R.SOURCE_WORDS.get, key=f"{base}-source")
    st.text_input("Note", key=f"{base}-note", placeholder="Optional")
    st.button("Save the correction", type="primary", key=f"{base}-save", on_click=save_correction,
              args=(ctx.run_id, key))
    for problem in st.session_state.get(f"{base}-problems") or []:
        st.error(plain(problem))


def sections_table(ctx, page_key, table_key):
    """The string as read: one row per section, its page a button that turns `page_key`'s viewer."""
    block = ctx.entry["review"]["pump_string"]
    rows = [{"page": s.get("page"), "order": s.get("section_order"), "model": s.get("pump_model"),
             "as printed": s.get("pump_model_as_printed"), "stages": s.get("stages"),
             "role": s.get("section_role") and R.words_of(s["section_role"]), "status": s.get("status"),
             "why": " ".join(dict.fromkeys(R.in_words(r) for r in s.get("reasons") or []))}
            for s in block["sections"]]
    if rows:
        table(cells(rows), table_key, page_key, "page")
    else:
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
        st.caption(f"Carried from {plain(R.confirmation_words(ctx.version or {}))}:" if carried else "As corrected:")
        st.dataframe(cells(section_rows(v["sections"], v["display"])), hide_index=True)
        if v["moved"]:
            st.caption("The new run reads the string differently; the string above comes from the earlier confirmation.")
        with st.expander("As read"):
            sections_table(ctx, page_key, table_key)
    words = R.unwritten_words(ctx.review)
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
    buttons.button("Back to the string as read", icon=":material/undo:", key=f"ed-{rid}-reset", on_click=edit_entry,
                   args=(rid, R.reset_editor))
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
    ctx = context(store, run_id, record, review_state(store, run_id, record, entry), page_key)
    on, why = gate(ctx)
    outcome_box(run_id)
    review_header(ctx, on, why)
    groups = R.groups(ctx.review, ctx.entry)
    left, right = st.columns([1, 1], gap="medium")
    with right:
        document_pane(store, record, page_key, ctx.pdf)
    with left, st.container(height=PANE, border=False):
        string_block(ctx)
        for group in ("found", "not found"):
            if groups[group]:
                st.markdown(f"#### {group.capitalize()}: {len(groups[group])}")
                st.caption(GROUP_HINT[group])
                for key in groups[group]:
                    value_line(ctx, key)
        with st.expander(f"Decided: {len(groups['decided'])}", icon=":material/task_alt:"):
            for key in groups["decided"]:
                value_line(ctx, key)
    dialog = st.session_state.get("dialog")
    if dialog and dialog[0] == run_id:          # statements, not an expression: magic would write its None
        if dialog[1] == "write":
            write_dialog(ctx, on, why)
        else:
            batch_dialog(ctx, dialog[1])


def dialog_pages(ctx, step, keys):
    """The pop-up's own page viewer, on the app's page renderer: opened at its first item's page, every page there."""
    page_key = f"dlgpage-{ctx.run_id}-{step}"
    if ctx.pdf is None:
        st.info(NO_PDF)
        return
    try:
        total = page_count(ctx.sha, ctx.pdf)
        if page_key not in st.session_state:
            pages = [p for k in keys for p in pages_of(ctx, k)]
            st.session_state[page_key] = min(pages[0], total) if pages else 1
        page = st.number_input("Page", min_value=1, max_value=total, step=1, key=page_key)
        st.image(page_png(ctx.sha, page - 1, ctx.pdf), caption=f"Page {page} of {total}", width="stretch")
    except Exception:  # noqa: BLE001 - a PDF that will not open: say so, the pop-up carries on
        st.warning(BAD_PDF)


def pages_of(ctx, key) -> list[int]:
    """The pages a value, or the string's sections, cite: each one's first page as printed first."""
    cited = ([s.get("page") for s in ctx.entry["review"]["pump_string"]["sections"]] if key == R.STRING
             else [ctx.items[key].get("page")])
    return [int(n) for c in cited for n in re.findall(r"\d+", str(c or ""))]


def batch_dialog(ctx, step):
    rid, keys = ctx.run_id, R.groups(ctx.review, ctx.entry)[step]

    @st.dialog(f"Batch: {BATCH_TITLE[step].lower()}", width="large", on_dismiss=close_dialog)
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
                    chosen = {R.STRING} if st.checkbox("The pump string, as read above", value=True,
                                                       key=f"tick-{rid}-string") else set()
            else:
                reason, chosen = None, ticks(ctx, step, keys, page_key)
            go = st.container(horizontal=True)
            if anyway:                  # the button's sentence is the affirmation; a reason must come first
                affirm = go.button(R.STRING_ANYWAY, type="primary", key=f"dlg-go-{rid}-{step}", disabled=not chosen)
                pull = go.button("Leave it to check", key=f"dlg-pull-{rid}-{step}")
            else:                       # nothing ticked: the button pulls everything out
                affirm = go.button(R.sentence(step, ctx.entry, sorted(chosen)) if chosen else
                                   f"Pull {'it' if len(keys) == 1 else f'all {len(keys)}'} out",
                                   type="primary" if chosen else "secondary", key=f"dlg-go-{rid}-{step}")
                pull = False
            if step == "string" and go.button("Edit sections", icon=":material/table_edit:", key=f"dlg-sections-edit-{rid}",
                                              help="Pull the string out of this batch and edit its sections on the "
                                                   "review, beside the document"):
                R.batch(ctx.review, ctx.entry, "string", set(), at=now_text())      # this batch's pull-out step
                R.open_editor(ctx.review)
                close_dialog()
                st.rerun()
            if step != "string":
                st.caption("Unticked values are pulled out: they stay to check and are marked on the review. A value "
                           "that blocks publish cannot be ticked: decide it one by one.")
            elif not anyway:
                st.caption("Unticked, the string is pulled out: it stays to check and is marked on the review.")
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
    def body():
        st.markdown(f"Writes to **{R.place(ctx.env)}**, organisation **{plain(visible(text(org)))}**, well "
                    f"**{plain(visible(well))}**, confirming as **{plain(ctx.name or 'nobody')}**.")
        name = R.document_name(ctx.record)
        st.caption(f"Document: {plain(name) if name else 'the design document'}, design dated "
                   f"{ctx.dates['incoming'] or 'not printed'}.")
        dates_note(ctx)
        tally = R.tally(ctx.review)
        st.markdown("What will be recorded:")
        st.dataframe(pd.DataFrame([(k, tally[k]) for k in (
            "settled one by one", "settled in batches", "pulled out", "corrected", "accepted anyway", "carried",
            "settled by the extraction", "left to check")], columns=["Values", "Count"]), hide_index=True)
        if not on:
            st.warning(plain(why))
        if st.button(f"Write to {R.place(ctx.env)}", type="primary", key=f"write-go-{rid}", disabled=not on):
            with st.spinner(f"Writing, and waiting up to {R.WAIT_S} s for the answer"):
                out = R.write(ctx.kept, ctx.review, ctx.entry, ctx.record, environment=ctx.env, confirmed_by=ctx.name,
                              catalogue=catalogue, offline=ctx.offline)
            st.session_state[f"outcome-{rid}"] = out
            if out["status"] == C.WRITTEN:
                st.session_state.pop(f"review-{rid}", None)
            close_dialog()
            st.rerun()
    body()


def confirmed_view(store, run_id, record, version, get, page_key):
    """What was written, read only, beside the PDF."""
    outcome_box(run_id)
    earlier = earlier_confirmations(store, record, version)
    with st.container(border=True):
        st.markdown(f"**Confirmed**: written to **{R.place(version['environment'])}** by "
                    f"**{plain(version['confirmed_by'])}** on {R.when_words(version['confirmed_at'])}.")
        name = R.document_name(record)
        st.caption(f"Document: {plain(name) if name else 'the design document'}, design dated "
                   f"{plain(version.get('design_date') or 'not printed')}.")
    left, right = st.columns([1, 1], gap="medium")
    with right:
        document_pane(store, record, page_key)
    with left, st.container(height=PANE, border=False):
        final_sections = version["pump_string"]["final_sections"]
        unwritten = final_sections and not dig(next(v for v in version["values"] if v["key"] == R.STRING), "final")
        rows = [{"value": R.label(v["key"]),
                 "as written": (("recorded below, not written" if unwritten else "the string below")
                                if v["key"] == R.STRING and final_sections else shown(v["final"])),
                 "from": R.SOURCE_WORDS.get(dig(v, "final", "source")), "status": v["status"],
                 "started as": v["starting_status"], "accepted anyway because": v.get("accept_anyway"),
                 "how it was decided": decided_words(v, earlier)}
                for v in version["values"]]
        st.dataframe(cells(rows), hide_index=True)
        st.markdown("#### The pump string as recorded, not written" if unwritten else "#### The pump string as written")
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
                    out[cid] = R.confirmation_words(C.load(store.objects.get(C.version_key(sha, cid))))
                except Exception:  # noqa: BLE001 - not readable: the step says "an earlier confirmation"
                    out[cid] = None
    return out


def decided_words(v, earlier) -> str | None:
    """How a written value was decided: its last step, in words."""
    if not v.get("history"):
        return None
    step = v["history"][-1]
    return R.step_words(step, earlier.get((step.get("reason") or "").removeprefix("carried from ")))


# ---- a new run, and watching it ----

def visible(value: str) -> str:
    """An id as the form shows it: edge spaces and runs of spaces become no-break spaces, so none collapse."""
    return re.sub(r"^ +| +$| {2,}", lambda m: "\u00a0" * len(m.group()), value)


def well_list():
    """(snapshot, problem).  A problem disables the form and says why."""
    path = os.environ.get(WELL_LIST_ENV)
    if not path:
        return None, f"Set {WELL_LIST_ENV} to the well list exported from Athena, then reload."
    try:
        snapshot = load_wells(path)
    except SnapshotError as err:
        return None, f"The well list {WELL_LIST_ENV} names cannot be used: {err}."
    return snapshot, None if snapshot.pairs else "The well list has a header and no wells."


def same_request(store, run_id, record, sha256, organisation, well) -> bool:
    """Whether a run is for this PDF, organisation and well.  In S3 mode the upload's labels say: a running
    record there names none of them yet."""
    labels = store.intake_labels(run_id)
    if labels is not None:
        return (labels["sha256"], labels["organization-id"], labels["well-id"]) == (sha256, organisation, well)
    return (dig(record, "source", "pdf", "sha256") == sha256 and record.get("organization_id") == organisation
            and record.get("well_id") == well)


def running_duplicate(store, sha256, organisation, well):
    """(run id, state) of a live run of the same PDF, organisation and well, or None: a run still running and
    not stale, or, in S3 mode, an upload not yet picked up that has not expired."""
    now = datetime.now(timezone.utc)
    for run_id in store.run_ids():
        try:
            record = store.record(run_id)
        except Exception:  # noqa: BLE001 - an unreadable record is no duplicate
            continue
        if run_status(record, now) == ("running", False) and same_request(store, run_id, record, sha256,
                                                                           organisation, well):
            return run_id, "running"
    for run_id, (state, _) in waiting(store, now).items():
        if state != "expired" and same_request(store, run_id, {}, sha256, organisation, well):
            return run_id, state
    return None


def show_upload(store):
    st.button("All runs", icon=":material/arrow_back:", on_click=goto, type="tertiary")
    st.title("New run from a PDF")
    st.write("Choose the organisation and the well the design document belongs to, then upload the PDF. "
             "The run starts in the background, and you can watch it here.")
    snapshot, problem = well_list()
    if problem:
        st.error(problem)
    if snapshot:
        st.caption(f"Well list: {snapshot.rows} rows, modified {snapshot.modified:%Y-%m-%d %H:%M} UTC, "
                   f"SHA-256 {snapshot.sha256[:12]}.")
    off = problem is not None
    org = st.selectbox("Organisation", [] if off else snapshot.organisations(), index=None, format_func=visible,
                       placeholder="Choose an organisation", disabled=off, key="new-org")
    if org is not None and snapshot.case_variants(org):
        st.caption("Also in the list, differing only by case: "
                   + ", ".join(plain(visible(v)) for v in snapshot.case_variants(org)))
    well = st.selectbox("Well", [] if org is None else snapshot.wells(org), index=None, format_func=visible,
                        placeholder="Choose a well", disabled=org is None, key="new-well")
    upload = st.file_uploader("Design document (PDF)", type="pdf", disabled=off, key="new-pdf")
    data = upload.getvalue() if upload is not None else b""
    if upload is not None and not data:
        st.error("This file is empty.")
    elif upload is not None and not data.startswith(b"%PDF-"):
        st.error("This file is not a PDF: it does not start with %PDF-.")
    ready = well is not None and data.startswith(b"%PDF-")
    if ready and (dup := running_duplicate(store, hashlib.sha256(data).hexdigest(), org, well)):
        dup, state = dup
        st.warning(f"Run {plain(dup)} for this PDF, organisation and well is still running. You can start "
                   "another." if state == "running" else
                   f"Run {plain(dup)} for this PDF, organisation and well is waiting for the function ({state}). "
                   "You can start another.")
    st.warning(COST, icon=":material/payments:")
    confirmed = st.checkbox("I understand this starts a paid model call", disabled=not ready, key="new-confirm")
    if st.button("Start the run", type="primary", disabled=not (ready and confirmed)):
        try:
            if MODE == "s3":
                run_id = intake.new_run_id(datetime.now(timezone.utc))
                intake.upload(store, run_id, data, org, well, upload.name)
            else:
                pdf = intake.save_pdf(store.root, data)
                run_id = intake.new_run_id(datetime.now(timezone.utc))
                intake.launch(store.root, pdf, org, well, run_id, upload.name)
        except Exception as err:  # noqa: BLE001 - say why nothing started
            st.error(f"The run could not be started ({type(err).__name__}: {plain(err)}).")
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


def render_watch(store, run_id) -> str:
    state, record = watch_now(store, run_id)
    if state in WAITING:
        uploaded = store.uploaded_at(run_id)
        waited = wait_text((datetime.now(timezone.utc) - uploaded).total_seconds()) if uploaded else "—"
        if state == "queued":
            st.info(f"Queued for {waited}: the upload is waiting for the function to pick it up.",
                    icon=":material/hourglass_top:")
        elif state == "not picked up":
            st.warning(f"Not picked up: queued for {waited}, while no run is running. Look for this run id in the "
                       "run log (DynamoDB `esp_design_extraction_logging`, as its `pipeline_execution_id`): a "
                       "`FAILED` row there means the function could not start the run. Its CloudWatch logs "
                       "(`/aws/lambda/esp-design-extraction`) say more.")
        else:
            st.error(f"Expired: queued for {waited}, past the 6 hours Lambda keeps a waiting event. This upload "
                     "will not run; upload the PDF again.")
    elif state == "starting":
        st.info("Starting: waiting for the run to write its record.", icon=":material/hourglass_top:")
    elif state == "no record yet":
        age = int(intake.age_s(run_id, datetime.now(timezone.utc)))
        st.info(f"No record yet, still running ({age} s).", icon=":material/hourglass_top:")
    elif state == "unreadable":
        st.warning("`run.json` is there but cannot be read. Checking again.")
    elif state == "launch failed":
        reason = "the process has exited" if intake.exited(run_id) else f"{intake.STARTUP_S} s have passed"
        st.error(f"Launch failed: the run wrote no record, and {reason}.")
        try:
            lines = store.launch_log(run_id).decode("utf-8", "replace").splitlines()
            st.caption("The last 50 lines of the launch log:")
            st.code("\n".join(lines[-50:]) or "(empty)", language=None, wrap_lines=True)
        except FileNotFoundError:
            st.caption("There is no launch log for this run.")
    else:
        pipeline_cell(record)
        start, deadline = when(record.get("started_at")), when(record.get("deadline_at"))
        end = when(record.get("ended_at")) or datetime.now(timezone.utc)
        if start and deadline:
            st.metric("Elapsed", f"{clock((end - start).total_seconds())} of "
                                 f"{clock((deadline - start).total_seconds())}")
            st.caption(f"`deadline_at` = {plain(record['deadline_at'])}")
        if state == "running":
            st.caption("Current stage: not in the record, which is written when the run starts and again "
                       "when it ends.")
        else:
            review_cell(record)
            error_line(record)
            st.button("Open the run", type="primary", on_click=goto, kwargs={"run": run_id})
    return state


def show_watch(store, run_id):
    st.button("All runs", icon=":material/arrow_back:", on_click=goto, type="tertiary")
    st.subheader(f"Run {plain(run_id)}")
    if run_id not in store.run_ids() and run_id not in store.launch_ids():
        st.warning(f"There is no run {plain(run_id)} in this store.")
        return
    if watch_now(store, run_id)[0] in FINAL:
        render_watch(store, run_id)
        return

    @st.fragment(run_every=POLL_S)
    def live():
        if render_watch(store, run_id) in FINAL:
            st.rerun()                      # final: render it once more, without the timer
    live()


def route(store):
    if "upload" in st.query_params:
        show_upload(store)
    elif st.query_params.get("watch"):
        show_watch(store, st.query_params["watch"])
    elif st.query_params.get("run"):
        return show_run(store, st.query_params["run"])
    else:
        show_list(store)


def s3_sidebar(store, technical=True):
    """Where S3 mode reads, and Refresh.  Beside an engineer's screen, only when the runs were read."""
    with st.sidebar:
        listed = store.listed_at
        if technical:
            st.caption(f"S3 mode: runs under `s3://{store.bucket}/{store.runs_prefix}`, uploads under "
                       f"`{store.intake_prefix}`." + (f" Listed at {listed:%H:%M:%S} UTC." if listed else ""))
        elif listed:
            st.caption(f"Runs read at {listed:%H:%M:%S} UTC.")
        st.button("Refresh", icon=":material/refresh:", on_click=store.refresh,
                  help="List the bucket again, and re-read every run that is not final" if technical
                  else "Read the runs again")


st.set_page_config(page_title="Design-document runs", page_icon=":material/description:", layout="wide")
STORE = local_store_from_env() if MODE == "local" else s3_store_from_env() if MODE == "s3" else None
if STORE is None:
    st.title("Design-document runs")
    if MODE == "local":
        st.info(f"Set {STORE_ENV} to a run store directory, then start the viewer again.")
        st.code(f"{STORE_ENV}=<run store dir> streamlit run app/main.py", language="bash")
    elif MODE == "s3":
        st.info(f"S3 mode reads the bucket the Lambda writes: set {', '.join(S3_ENV)} as the stack sets them, "
                "each prefix ending in /, then start the viewer again.")
    else:
        st.error(f"{MODE_ENV} is {plain(MODE)}: it must be local or s3.")
elif MODE == "s3":
    engineer = False
    try:
        engineer = route(STORE) is True
    except Exception as err:  # noqa: BLE001 - credentials, network or permissions: say so, not a trace
        if st.query_params.get("run"):              # a run opens on an engineer's screen: plain words
            st.error("The run could not be read. Your AWS sign-in may have expired: sign in again, then reload the "
                     "page.")
        else:
            st.error(f"S3 could not be read ({type(err).__name__}: {plain(err)}). Check the AWS credentials "
                     "(AWS_PROFILE; an expired SSO session needs `aws sso login`) and the bucket variables.")
    s3_sidebar(STORE, technical=not engineer)
else:
    route(STORE)
