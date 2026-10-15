"""One document, end to end, into the local run store, with its run record.

    Detect -> Prep -> Extract -> Validate and stage -> Store -> Record

or, from an existing report, Validate and stage -> Store -> Record (with Detect
and Prep first when the PDF is given too; without it the checks that need them
are not evaluated, and the run's notices say so).  Local only: no S3, no DynamoDB.

The store root is the directory ESP_RUN_STORE_DIR names; a run lives in
`<root>/runs/<run_id>/`.  `run.json` is written first with status `running` and
a `deadline_at`, and last with `succeeded` or `failed`; every file is written to a
temporary name and renamed into place.  A failed run keeps what it produced.
`read_record` treats a `running` record past its deadline as failed (stale).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .assemble import GENERIC_PROMPT_CAP, assemble_prompt, asset_hashes, clarification_hashes, manifest_csv, route_line
from .bedrock import EFFORT, MODEL_ID, extract
from .compare.parity import cost_usd, model_prices
from .detect import GENERIC, Detection, route
from .errors import ExtractionError
from .prep import prep, prep_generic
from .stage import production_evidence, stage
from .stage.reports_reader import read_report
from .stage.stage import CATALOGUE, EVIDENCE, FAMILY_LABEL, SEED, asset

STORE_ENV = "ESP_RUN_STORE_DIR"
BUDGET_S = 900
MAX_TOKENS = 128_000
PRICING = Path(__file__).with_name("compare") / "pricing.json"
GRAINS = ("pump_config", "esp_well_design_context", "curve_observations")
LOGS = ("d16_log", "d17_log", "gas_log")
LISTS = ("findings", "unknown_models", "review", "notices")
LABELS = {**FAMILY_LABEL, GENERIC: GENERIC}     # report vendor_family -> family, generic included


def utc(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(now: datetime) -> str:
    """Sortable and unique: the UTC start second, then 8 random hex digits."""
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")     # what new_run_id makes; a given run id must match


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Write to a temporary name in the same directory, then rename into place."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def jsonl(rows) -> bytes:
    return "".join(json.dumps(r, sort_keys=True, default=str, ensure_ascii=False) + "\n" for r in rows).encode()


def parquet(frame) -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), buf)
    return buf.getvalue()


class Store:
    """One run's directory.  A write that fails raises STORE_FAILED naming the file."""

    def __init__(self, root: Path, run_id: str):
        self.dir = Path(root) / "runs" / run_id
        self.files: list[str] = []
        self.seconds = 0.0
        try:
            self.dir.mkdir(parents=True, exist_ok=False)
        except OSError as err:
            raise ExtractionError("STORE_FAILED", f"creating {self.dir}: {err}",
                                  {"failed_at": "run directory", "stored": []}) from err

    def write(self, name: str, data: bytes) -> None:
        t = time.monotonic()
        try:
            atomic_write(self.dir / name, data)
        except OSError as err:
            raise ExtractionError("STORE_FAILED", f"writing {name}: {err}",
                                  {"failed_at": name, "stored": list(self.files)}) from err
        finally:
            self.seconds += time.monotonic() - t
        if name not in self.files:
            self.files.append(name)


def read_record(run_dir: Path, now: datetime | None = None) -> dict:
    """The run record as a reader should see it: `running` past its deadline is failed (stale)."""
    record = json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    now = now or datetime.now(timezone.utc)
    if record["status"] == "running" and utc(now) > record["deadline_at"]:
        record = {**record, "status": "failed", "stale": True}
    return record


STAGED = {"pump_config": "stg_pump_config", "esp_well_design_context": "stg_design_context",
          "curve_observations": "stg_curve_observations"}


def _summary(result) -> dict:
    findings = result.findings
    parsed = {r["block"]: r["objects_parsed"] for r in result.log["reconciliation"]}
    return {
        "staged": {g: parsed.get(block, 0) for g, block in STAGED.items()},   # the report's rows, before the load
        "rows": {g: len(getattr(result, g)) for g in GRAINS},
        "quarantine": len(result.quarantine),
        "findings": {
            "total": len(findings),
            "by_round1_severity": dict(sorted(Counter(f["round1_severity"] for f in findings).items())),
            "by_spec_severity": dict(sorted(Counter(f["spec_severity"] or "none" for f in findings).items())),
        },
        "spec_validation_status": result.spec_validation_status,
        "blocks_publish": any(f["blocks_publish"] for f in findings),
        "review": {"items": len(result.review),
                   "needs_review": sum(r["needs_review"] for r in result.review),
                   "informational": sum(1 for r in result.review if r["reasons"] and not r["needs_review"])},
        "unknown_models": result.unknown_models,
    }


def run(*, organization_id, well_id, pdf: Path | None = None, report: Path | None = None,
        store_root: Path, budget_s: float = BUDGET_S, model_id: str = MODEL_ID, effort: str = EFFORT,
        max_tokens: int = MAX_TOKENS, clarifications: bool = True, client=None,
        run_id: str | None = None, source_name: str | None = None, force_generic: bool = False) -> dict:
    """Run one document and return its final run record.  With `report`, no model is called.

    `run_id` replaces the generated id.  `source_name` is the file name that prep records and
    the prompt names, in place of the PDF path's basename; the record keeps it as `source.source_name`.
    Detect's route (`template` or `generic`) picks the prep and the prompt; `force_generic` sends a
    template document down the generic route, for measuring it.
    """
    if pdf is None and report is None:
        raise ValueError("a run needs a PDF, a report, or both")
    if run_id is not None and not RUN_ID.fullmatch(run_id):
        raise ValueError(f"not a run id: {run_id!r}")
    started = datetime.now(timezone.utc).replace(microsecond=0)
    t0 = time.monotonic()
    deadline = started + timedelta(seconds=budget_s)
    record = {
        "run_id": run_id or new_run_id(started), "status": "running", "started_at": utc(started),
        "deadline_at": utc(deadline), "ended_at": None, "budget_s": budget_s,
        "mode": "report" if report else "pdf", "organization_id": organization_id, "well_id": well_id,
        "family": None, "family_source": None, "route": None, "forced": False, "forced_from": None,
        "family_hint": None, "missing_roles": [], "near_misses": [],
        "source": {"pdf": None, "report": None, "source_name": None}, "pages": None,
        "rows": None, "quarantine": None, "findings": None, "spec_validation_status": None,
        "blocks_publish": None, "review": None, "unknown_models": None,
        "error": None, "model": None,
        "hashes": {"report_sha256": None, "prompt_sha256": None, "contract": None, "clarifications": None,
                   "stage_assets": None},
        "tokens": None, "cost_usd": None, "stages_s": {}, "store": {"files": [], "failed_at": None},
    }
    stages = record["stages_s"]
    at = ["start"]                     # the stage in progress, for a failure's `error.stage`

    def timed(name, fn, *a, **kw):
        at.append(name)
        t = time.monotonic()
        try:
            return fn(*a, **kw)
        finally:
            stages[name] = round(time.monotonic() - t, 3)

    store = Store(store_root, record["run_id"])

    def save_record():
        record["store"]["files"] = list(store.files) + ([] if "run.json" in store.files else ["run.json"])
        store.write("run.json", (json.dumps(record, indent=1, sort_keys=True, default=str) + "\n").encode())

    try:
        save_record()
        pdf_bytes = Path(pdf).read_bytes() if pdf else None
        report_text = Path(report).read_text(encoding="utf-8", errors="replace") if report else None
        for kind, path, data in (("pdf", pdf, pdf_bytes), ("report", report, report_text and Path(report).read_bytes())):
            if path:
                record["source"][kind] = {"path": str(Path(path).resolve()), "sha256": sha256(data)}
        record["source"]["source_name"] = source_name or (Path(pdf).name if pdf else None)
        store.write("source.json", (json.dumps(record["source"], indent=1, sort_keys=True) + "\n").encode())

        detection = prepared = None
        if pdf:
            routing = timed("detect", route, pdf_bytes, force_generic=force_generic)
            record.update(route=routing.route, forced=routing.forced, forced_from=routing.forced_from,
                          family_hint=routing.family_hint, missing_roles=list(routing.missing_roles),
                          near_misses=list(routing.near_misses))
            if routing.route == GENERIC:
                prepared = timed("prep", prep_generic, pdf_bytes, source_file=record["source"]["source_name"])
                detection = Detection(GENERIC, len(prepared.manifest), {r["original_page_no"]: [] for r in prepared.manifest})
            else:
                detection = routing.detection
                prepared = timed("prep", prep, pdf_bytes, detection, source_file=record["source"]["source_name"])
            record["family"], record["family_source"] = detection.family, "detect"
            store.write("prep_manifest.csv", manifest_csv(prepared.manifest).encode())
            record["pages"] = {"page_count": detection.page_count, "kept_pages": prepared.kept_pages,
                               "roles": {str(p): [r for r, _, _ in v] for p, v in detection.page_roles.items() if v}}
        if report_text is None:
            family = detection.family
            prompt = timed("assemble", assemble_prompt, family, prepared, record["source"]["source_name"], well_id=well_id,
                           clarifications=clarifications,
                           route_context=route_line(record["family_hint"], record["missing_roles"]))
            size = len(prompt.encode("utf-8"))
            if family == GENERIC and size > GENERIC_PROMPT_CAP:
                raise ExtractionError("GENERIC_TOO_LARGE", f"the generic prompt is {size:,} bytes, over the "
                                      f"{GENERIC_PROMPT_CAP:,}-byte cap; no model call was made",
                                      {"prompt_bytes": size, "cap": GENERIC_PROMPT_CAP})
            record["hashes"]["prompt_sha256"] = sha256(prompt.encode())
            record["hashes"]["contract"] = asset_hashes(family)
            record["hashes"]["clarifications"] = clarification_hashes(family) if clarifications else {}
            record["model"] = {"model_id": model_id, "effort": effort, "max_tokens": max_tokens,
                               "clarifications": clarifications, "prompt_bytes": size}
            remaining = budget_s - (time.monotonic() - t0)
            result = timed("extract", extract, prompt, budget_s=remaining, max_tokens=max_tokens,
                           model_id=model_id, effort=effort, client=client)
            report_text = result.report
            record["model"].update({"stop_reason": result.stop_reason, "model_time_s": round(result.duration_s, 3),
                                    "time_to_first_token_s": result.time_to_first_token_s})
            record["tokens"] = {"input": result.input_tokens, "output": result.output_tokens}
            pricing = json.loads(PRICING.read_text(encoding="utf-8"))
            cost = cost_usd(result.input_tokens, result.output_tokens, model_prices(pricing, model_id))
            record["cost_usd"] = None if cost is None else round(cost, 6)
        store.write("report.md", report_text.encode("utf-8"))
        record["hashes"]["report_sha256"] = sha256(report_text.encode("utf-8"))

        if detection is None:
            record["family"] = LABELS.get(read_report(report_text).vendor_family)
            record["family_source"] = "report_frontmatter"
            record["route"] = None if record["family"] is None else GENERIC if record["family"] == GENERIC else "template"
            if record["family"] is None:
                raise ExtractionError("FAMILY_MISMATCH", "no PDF to detect, and the report names no known vendor_family",
                                      {"vendor_family": read_report(report_text).vendor_family})
        record["hashes"]["stage_assets"] = {n: sha256(asset(n).read_bytes()) for n in (SEED, EVIDENCE, CATALOGUE)}
        result = timed("stage", stage, report_text, organization_id, well_id, record["family"],
                       production_evidence(report_text), detection=detection, prep=prepared)

        for g in GRAINS:
            store.write(f"{g}.parquet", parquet(getattr(result, g)))
        store.write("quarantine.jsonl", jsonl(result.quarantine))
        for name in LISTS:
            store.write(f"{name}.jsonl", jsonl(getattr(result, name)))
        for name in LOGS:
            store.write(f"{name}.jsonl", jsonl(result.log[name]))
        t = time.monotonic()
        record.update(_summary(result))
        stages["record"] = round(time.monotonic() - t, 3)
        empty = next((f for f in result.findings if f["round1_severity"] == "EXTRACTION_EMPTY"), None)
        if empty:
            raise ExtractionError("EXTRACTION_EMPTY", f"{empty['message']}; report, findings and review are stored",
                                  {"model_halt": empty["value"]})
        if any(f["round1_severity"] == "LOAD_HALTED" for f in result.findings):
            raise ExtractionError("LOAD_HALTED", "D17-HALT: scenarios present different pump strings; "
                                  "grains, findings and review are stored, the load is halted", {})
        record["status"] = "succeeded"
    except ExtractionError as err:
        record["status"] = "failed"
        record["error"] = {"code": err.code, "message": err.message, "details": err.details,
                           "stage": "store" if err.code == "STORE_FAILED" else at[-1]}
        if err.code == "STORE_FAILED":
            record["store"]["failed_at"] = err.details.get("failed_at")
        elif err.code == "REPORT_INVALID":
            try:
                store.write("findings.jsonl", jsonl(err.details.get("findings", [])))
                record["findings"] = {"total": len(err.details.get("findings", [])),
                                      "by_round1_severity": {"REPORT_INVALID": len(err.details.get("findings", []))},
                                      "by_spec_severity": {"none": len(err.details.get("findings", []))}}
            except ExtractionError as store_err:
                record["store"]["failed_at"] = store_err.details.get("failed_at")
    stages["store"] = round(store.seconds, 3)
    record["ended_at"] = utc(datetime.now(timezone.utc))
    try:
        save_record()
    except ExtractionError as err:      # the `running` record stays; past its deadline a reader sees it stale
        record["status"] = "failed"
        record["error"] = record["error"] or {"code": err.code, "message": err.message, "details": err.details,
                                              "stage": "record"}
        record["store"]["failed_at"] = err.details.get("failed_at")
    record["store_dir"] = str(store.dir)
    return record
