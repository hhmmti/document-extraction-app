"""Validate one staged report: findings, notices, unknown models, per-value review.

Record shapes, fixed here and in the README:

  finding        rule_id, spec_severity, round1_severity, scope, table, row_key,
                 column, value, message, blocks_publish
  review         field, table, row_key, value, cited_page, grounding, reasons,
                 needs_review
  unknown model  model, class, pump_model_as_printed
  notice         kind, rule_id, family, field, message

`row_key` is a row's 1-based position in its `stg_*` block as the reader parsed
it, or null.  `round1_severity` is the locked map: REPORT_INVALID for the
structure guards (the stage raises; nothing is staged), LOAD_HALTED for D17,
EXTRACTION_EMPTY for G-E1 (the run fails), `warning` for everything else.
`spec_severity` is the spec's, or null for a guard the spec does not have.
`spec_validation_status` follows spec §1.2, with a floor from the emptiness guards:
G-E1 (no pump-configuration and no design-context rows) is failed; G-E2 (no pump
rows beside other rows) and G-E3 (a halt the model declared, rows still staged)
are at least needs_review.  A generic-route report is at least needs_review too, and says
so in a notice: no vendor template checked it.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

import pyarrow.parquet as pq

from ..stage.reports_reader import BLOCKS, _regions
from ..stage.generic import GENERIC
from ..stage.stage import CATALOGUE, asset
from . import rules as R
from .review import KNOWN_ISSUE, review

NOTICE_TEXT = {
    "F3": ("G-K3", "`pump_model_as_printed` may omit the in-cell `(<n>stg)` annotation."),
    "F5": ("G-K5", "`scenario_label_staged` may carry a shortened label, inconsistent across tables."),
    "F7": ("G-K7", "one gas-cascade row may carry an unexplained Turpin indicator "
                   "(`0.0002` where `0.000` is printed)."),
}


def structure_findings(report_text: str, report) -> list[dict]:
    """G-S1..G-S3.  Any of these and the report is not staged at all."""
    out = []
    if not report.frontmatter:
        out.append(R.finding("G-S1", None, "DOC", None, None, None, None, "no parseable frontmatter"))
    regions = _regions(report_text)
    for block in BLOCKS:
        if block not in regions:
            out.append(R.finding("G-S2", None, "DOC", block, None, None, None,
                                 f"`## {block}` with its ```jsonl fence is missing"))
    if "row_counts" not in regions:
        out.append(R.finding("G-S3", None, "DOC", None, None, None, None, "no `## row_counts` footer"))
    elif not _footer_ok(regions["row_counts"]):
        out.append(R.finding("G-S3", None, "DOC", None, None, None, None,
                             "the `## row_counts` footer does not parse to an integer per block"))
    return out


def _footer_ok(segments) -> bool:
    for line in (ln.strip() for seg in segments for ln in seg):
        if line.startswith("{"):
            try:
                counts = json.loads(line)
            except ValueError:
                continue
            if isinstance(counts, dict) and all(type(counts.get(b)) is int for b in BLOCKS):
                return True
    return False


def _guards(report, reconciliation):
    for r in reconciliation:                                   # G-S4
        if r["footer_declared"] is not None and r["footer_declared"] != r["objects_parsed"]:
            yield R.finding("G-S4", None, "DOC", r["block"], None, None, r["footer_declared"],
                            f"footer declares {r['footer_declared']}, {r['objects_parsed']} parsed "
                            f"({r['truncated_lines']} truncated, {r['duplicates_dropped']} duplicates dropped)")
    labels = defaultdict(lambda: defaultdict(set))             # G-L1
    for table in BLOCKS:
        for o in report.blocks[table]:
            ordinal = R.text(o, "scenario_ordinal")
            label = R.text(o, "scenario_label_staged") or R.text(o, "scenario_label")
            if ordinal and label:
                labels[ordinal][label].add(table)
    for ordinal in sorted(labels, key=lambda s: (len(s), s)):
        if len(labels[ordinal]) > 1:
            detail = "; ".join(f"`{lab}` in {', '.join(sorted(tables))}" for lab, tables in sorted(labels[ordinal].items()))
            yield R.finding("G-L1", None, "SCEN", None, None, "scenario_label_staged", sorted(labels[ordinal]),
                            f"scenario_ordinal {ordinal} carries {len(labels[ordinal])} labels: {detail}")


# The prompts say "HALT and report it" or "STOP … set validation_status: failed" and fix no wording.
# A declaration is a prose line that starts with HALT and then a separator or a rule id
# (`> [!warning] **HALT — V-02 (…)`), or that frontmatter value.  A line that only mentions a
# halt (`… is not a HALT.`) is not one.
_HALT_LINE = re.compile(r"^[\s>*_#-]*(?:\[![A-Za-z]+\]\s*)?[*_]*HALT\b[*_]*\s*(?:[—–:.(-]|V-|D1[67])")
_RULE_ID = re.compile(r"\b(?:V-F\d-\d+|V-[A-Z]?\d{1,2}|D1[67])\b")
FLOOR = {"G-E1": "failed", "G-E2": "needs_review", "G-E3": "needs_review"}
GENERIC_NOTICE = "Extracted without a vendor template: every value is unverified."


def declared_halt(report_text, report) -> list[str] | None:
    """The rule ids of a halt the report declares, in order ([] when it names none), or None when it
    declares none.  Only the prose before the data blocks is read, and only rule ids are kept, so
    nothing the document prints is repeated."""
    prose = (report_text or "").split("\n## stg_pump_config", 1)[0]
    lines = [ln for ln in prose.splitlines() if _HALT_LINE.match(ln)]
    failed = str(report.frontmatter.get("validation_status") or "").strip().lower() == "failed"
    if not lines and not failed:
        return None
    return list(dict.fromkeys(rule for ln in lines for rule in _RULE_ID.findall(ln)))


def _halt_said(halt) -> str:
    return f"the model declared a halt naming {', '.join(halt) or 'no rule'}"


def _emptiness(report, halt):
    """G-E1..G-E3: an extraction with nothing to load, one with no pump rows, and a declared halt."""
    pumps, context = report.blocks["stg_pump_config"], report.blocks["stg_design_context"]
    if not pumps and not context:
        yield R.finding("G-E1", None, "DOC", None, None, None, halt,
                        "no pump-configuration and no design-context rows were extracted; "
                        + (_halt_said(halt) if halt is not None else "the report declares no halt"))
        return
    if not pumps:
        yield R.finding("G-E2", None, "DOC", "stg_pump_config", None, None, None, "no pump rows extracted")
    if halt is not None:
        yield R.finding("G-E3", None, "DOC", None, None, None, halt, f"model halt: {', '.join(halt) or 'no rule named'}")


def _why_no_pump_rows(report, halt, well_matched) -> str:
    why = [] if well_matched else ["unmatched well"]
    if not report.blocks["stg_pump_config"]:
        why.append("no pump rows extracted" if report.blocks["stg_design_context"] else "empty extraction")
    if halt is not None:
        why.append(_halt_said(halt))
    return "; ".join(why) or "none of the report's pump rows reached the load"


def _from_stage(quarantine_rows, d16_log, d17_log):
    """V-10, D16 and D17 are the stage's; surfaced here as findings."""
    for q in quarantine_rows:
        if q["reason"] == "key_not_in_schema_v10":
            yield R.finding("V-10", "HALT", "DOC", q["table"], None, q["column"], q["raw_value"],
                            f"`{q['column']}` is not a staging-schema key; quarantined")
    for entry in d16_log:
        for column in entry["halted_columns"]:
            yield R.finding("D16", "HALT", "CTX", "stg_design_context", None, column,
                            entry["findings"][column]["all_values"],
                            "D16-HALT: an invariant column varies across scenarios; left NULL in the load")
    for entry in d17_log:
        if entry["status"] == "D17-HALT":
            yield R.finding("D17", "HALT", "DOC", "stg_pump_config", None, None, entry["detail"],
                            "D17-HALT: scenarios present different pump strings; every variant kept, "
                            "the load is halted for a human decision")


def catalogue() -> dict[str, bool]:
    """Catalogue model -> has a row with a curve basis."""
    t = pq.read_table(asset(CATALOGUE), columns=["pump_model_canonical", "curve_basis"])
    out = {}
    for model, basis in zip(t.column(0).to_pylist(), t.column(1).to_pylist()):
        out[model] = out.get(model, False) or basis is not None
    return out


def unknown_models(pump_config_rows, models: dict[str, bool]) -> list[dict]:
    out = {}
    for r in pump_config_rows:
        canonical, printed = r.get("pump_model_canonical"), r.get("pump_model_as_printed")
        if canonical is None:
            key = ("unresolved", printed)
        elif canonical not in models:
            key = ("absent", canonical)
        elif not models[canonical]:
            key = ("no_head_curve", canonical)
        else:
            continue
        out.setdefault(key, {"model": key[1], "class": key[0], "pump_model_as_printed": printed})
    return list(out.values())


def spec_validation_status(findings) -> str:
    """Spec §1.2: any HALT -> failed; any ERROR or WARN -> needs_review; NOTE never blocks.
    The emptiness guards, which the spec does not have, set a floor (FLOOR)."""
    severities = {f["spec_severity"] for f in findings}
    floors = {FLOOR.get(f.get("rule_id")) for f in findings}
    if "HALT" in severities or "failed" in floors:
        return "failed"
    return "needs_review" if severities & {"ERROR", "WARN"} or "needs_review" in floors else "passed"


def validate(report, family, lookup, quarantine_rows, reconciliation, d16_log, d17_log, pump_config_rows,
             *, detection=None, prep=None, report_text=None, well_matched=True) -> dict:
    """`report_text` lets the halt check read the report's prose; without it only the frontmatter
    is read.  `well_matched` is false when the caller staged the report with no well."""
    roles = None if detection is None else {p: [r for r, _, _ in v] for p, v in detection.page_roles.items()}
    doc = R.Doc(report, family, lookup, roles, None if prep is None else prep.page_text)
    rule_findings, skipped = R.run(doc)
    halt = declared_halt(report_text, report)
    findings = [*_guards(report, reconciliation), *_emptiness(report, halt), *rule_findings,
                *_from_stage(quarantine_rows, d16_log, d17_log)]

    notices = []
    if family in NOTICE_TEXT:
        rule_id, message = NOTICE_TEXT[family]
        notices.append({"kind": "known_issue", "rule_id": rule_id, "family": family,
                        "field": KNOWN_ISSUE[family][1], "message": message})
    for rule_id, missing in skipped:
        notices.append({"kind": "not_evaluated", "rule_id": rule_id, "family": family, "field": None,
                        "message": f"{rule_id} not evaluated: needs Detect page roles" if missing == ["roles"]
                        else f"{rule_id} not evaluated: needs {' and '.join(missing)}"})
    if prep is None:
        notices.append({"kind": "not_evaluated", "rule_id": "review.grounding", "family": family, "field": None,
                        "message": "grounding not evaluated: no Prep page text"})
    if pump_config_rows:
        unknown = unknown_models(pump_config_rows, catalogue())
    else:
        unknown = []
        notices.append({"kind": "not_evaluated", "rule_id": "unknown_models", "family": family, "field": None,
                        "message": "unknown models not evaluated: no staged pump_config rows "
                                   f"({_why_no_pump_rows(report, halt, well_matched)})"})

    status = spec_validation_status(findings)
    if family == GENERIC:              # never trusted by default
        notices.append({"kind": "generic_route", "rule_id": "generic", "family": family, "field": None,
                        "message": GENERIC_NOTICE})
        status = "needs_review" if status == "passed" else status
    return {
        "findings": findings,
        "unknown_models": unknown,
        "review": review(doc, findings, quarantine_rows),
        "notices": notices,
        "spec_validation_status": status,
    }
