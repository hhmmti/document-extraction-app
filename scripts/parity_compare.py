"""Parity compare: each new report in --out against the old batch report. No model, no AWS.

    .venv/bin/python scripts/parity_compare.py --out <dir> --old-reports <dir> [--batch-prompts <dir>]

Reads <out>/<stem>/run.json and report.md as written by parity_run.py, and the
old report <family>-<slug>-pass-a.md from --old-reports. Writes
<out>/parity_report.md, and the adjudication worksheet <out>/adjudication.csv
with <out>/added_sample.csv. A worksheet that already carries verdicts is never
overwritten: the new one goes to <out>/adjudication.regenerated.csv instead.
--baseline-run <dir> adds side-by-side metrics against an earlier run and lists
gate differences that run did not have. --batch-prompts (the runners' saved prompts) answers
whether an old F1 run was sent raw private-use glyphs; without it the answer
is `unknown`.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from extraction.assemble import well_slug  # noqa: E402
from extraction.compare.gate import read_worksheet  # noqa: E402
from extraction.compare.parity import (  # noqa: E402
    CATEGORIES, CURVE_COUNT_TOLERANCE_PCT, CURVE_IDENTITY, MATCH_KEY, WORKSHEET_COLUMNS, added_sample, compare,
    cost_usd, model_prices, worksheet,
)

PRICING = LAB / "extraction" / "compare" / "pricing.json"
FAILING_ORDER = ("conflict", "dropped")
_PUA = re.compile(r"[\ue000-\uf8ff]")


def md(v) -> str:
    if v is None:
        return "—"
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, tuple)) else str(v)
    return s.replace("|", "\\|").replace("\n", " ")


def num(v, fmt="{:,.3f}") -> str:
    return "—" if v is None else fmt.format(v)


def yn(flag) -> str:
    return "unknown" if flag is None else "yes" if flag else "no"


def counts_cell(cnt) -> str:
    return " · ".join(f"{k[0]} {cnt[k]}" for k in CATEGORIES)


def judged(ok: bool, cnt) -> str:
    return f"{'pass' if ok else '**FAIL**'} ({counts_cell(cnt)})"


def clip(v, n=200) -> str:
    s = md(v)
    return s if len(s) <= n else s[:n] + "…"


def old_input_check(stem: str, batch: Path | None) -> str:
    if batch is None:
        return "unknown"
    saved = batch / f"{stem}.prompt.txt"
    if not saved.is_file():
        return "unknown"
    n = len(_PUA.findall(saved.read_bytes().decode("utf-8")))
    return f"yes ({n} private-use codepoints in the saved prompt)" if n else "no"


def load_and_compare(run_dir: Path, old_dir: Path) -> tuple[list[dict], dict]:
    """The runs in a parity run directory, in prompt-size order, and each completed one compared with its old report."""
    runs = []
    for run_json in run_dir.glob("*/run.json"):
        run = json.loads(run_json.read_text(encoding="utf-8"))
        run["_dir"] = run_json.parent
        runs.append(run)
    runs.sort(key=lambda r: r.get("prompt_bytes") or 0)
    results = {}
    for run in runs:
        if run.get("status") != "completed":
            continue
        fam = run["family"]
        old_path = old_dir / f"{fam.lower()}-{well_slug(run['stem'])}-pass-a.md"
        if not old_path.is_file():
            results[run["stem"]] = f"old report not found: {old_path.name}"
            continue
        results[run["stem"]] = compare(fam, old_path.read_text(encoding="utf-8"),
                                       (run["_dir"] / "report.md").read_text(encoding="utf-8"))
    return runs, results


def short_model(run: dict) -> str:
    return str(run.get("model_id") or "?").rsplit(".", 1)[-1].removeprefix("claude-")


def gate_items(stem: str, c) -> dict:
    """Conflict and dropped items on a gate field (pump_config, or any trap field), keyed for comparison across runs."""
    return {(r["table"], r["row_key"], r["field"], r["category"]): r for r in worksheet(stem, c)
            if r["category"] in ("conflict", "dropped") and (r["table"] == "stg_pump_config" or r["is_trap"] == "true")}


def against_baseline(runs: list, results: dict, base_runs: list, base_results: dict, pricing: dict) -> list[str]:
    """The side-by-side metrics and the new gate differences, for documents in both runs."""
    base = {r["stem"]: r for r in base_runs}
    both = [r for r in runs if r["stem"] in base]
    L = ["", "## Against the baseline run", "",
         "Each cell reads baseline / this run. Total tokens is input plus output; each run is priced for its own model.", "",
         "| Document | Model · effort | Input tokens | Output tokens | Total tokens | TTFT s | Model time s | Cost USD "
         "| % of max_tokens |", "|---|---|---|---|---|---|---|---|---|"]

    def cell(pair, fmt):
        return " / ".join(num(v, fmt) for v in pair)

    for run in both:
        pair = (base[run["stem"]], run)
        m = [r.get("metrics", {}) for r in pair]
        tin, tout = [x.get("input_tokens") for x in m], [x.get("output_tokens") for x in m]
        total = [None if a is None or b is None else a + b for a, b in zip(tin, tout)]
        cost = [cost_usd(a, b, model_prices(pricing, r.get("model_id"))) for a, b, r in zip(tin, tout, pair)]
        share = [None if x.get("max_tokens_share") is None else x["max_tokens_share"] * 100 for x in m]
        L.append(f"| {md(run['stem'])} | {' / '.join(md(short_model(r) + ' · ' + str(r.get('effort'))) for r in pair)} | "
                 f"{cell(tin, '{:,}')} | {cell(tout, '{:,}')} | {cell(total, '{:,}')} | "
                 f"{cell([x.get('time_to_first_token_s') for x in m], '{:.1f}')} | "
                 f"{cell([x.get('model_time_s') for x in m], '{:.1f}')} | {cell(cost, '{:.4f}')} | "
                 f"{cell(share, '{:.1f}')} |")

    L += ["", "### New gate differences", "",
          "Conflict or dropped items on pump_config or a trap field in this run that the baseline run did not have.", ""]
    new_items = []
    for run in both:
        c, b = results.get(run["stem"]), base_results.get(run["stem"])
        if c is None or b is None or isinstance(c, str) or isinstance(b, str):
            continue
        before = gate_items(run["stem"], b)
        new_items += [r for k, r in gate_items(run["stem"], c).items() if k not in before]
    L += [f"- {md(r['doc'])} · `{r['table']}` · `{md(r['row_key'])}` · `{r['field']}` · {r['category']}: "
          f"old `{md(r['old_value'])}` → new `{md(r['new_value'])}`" for r in new_items] or ["None."]
    return L


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--old-reports", type=Path, default=os.environ.get("ESP_OLD_REPORTS_DIR"),
                    help="directory of old batch reports (default: $ESP_OLD_REPORTS_DIR)")
    ap.add_argument("--batch-prompts", type=Path, default=os.environ.get("ESP_BATCH_PROMPT_DIR"),
                    help="the runners' saved prompts (default: $ESP_BATCH_PROMPT_DIR); optional")
    ap.add_argument("--baseline-run", type=Path,
                    help="an earlier parity run directory; adds side-by-side metrics and new gate differences")
    args = ap.parse_args()

    out = args.out.resolve()
    if out == LAB or LAB in out.parents:
        sys.exit(f"refusing to write inside {LAB}: the report holds client data")
    if not args.old_reports or not Path(args.old_reports).is_dir():
        sys.exit("set --old-reports or ESP_OLD_REPORTS_DIR to the directory of old batch reports")
    old_dir = Path(args.old_reports)
    batch = Path(args.batch_prompts) if args.batch_prompts and Path(args.batch_prompts).is_dir() else None
    pricing = json.loads(PRICING.read_text(encoding="utf-8"))

    runs, results = load_and_compare(out, old_dir)
    if not runs:
        sys.exit(f"no */run.json under {out}")

    L = ["# Parity report", "",
         f"Generated {date.today().isoformat()} from {len(runs)} run(s) in this directory. "
         "The comparison is deterministic; no model is involved.", "",
         "## Definitions", "",
         "- **Scenario alignment.** Each report's rows are mapped to a scenario ordinal using that report's "
         "own scenario table: every row, in any `stg_*` table, that prints a label beside an ordinal. A row "
         "with an ordinal keeps it; a row with only a label takes that label's ordinal; a row with no scenario "
         "key belongs to the report's only scenario when the report has exactly one. Anything else is "
         "`unaligned` and never guessed. The mapping is listed per document below.",
         f"- **Curve identity**: `{' · '.join(CURVE_IDENTITY)}`. **Row match key**: "
         f"`{' · '.join(MATCH_KEY)}`. `scenario` is the aligned ordinal; frequency and flow are compared as "
         "parsed numbers. A composite row (`observation_is_composite` true) is a whole-string point, never attributed to one model (f2-championx.md §6 T6), so its model is left out of its identity; a model it prints is still compared as a field. Rows sharing a key pair in canonical order. Other tables match on scenario plus "
         "`section_order` (pump_config), `stage_order` (gas cascade) or nothing more (design context), and "
         "`sensitivity_case` where printed.",
         "- **Categories**, one per differing field: `added` (absent in old), `dropped` (absent in new), "
         "`format` (both sides read as the same number(s), and the unit token is the same ignoring case or "
         "printed on one side only) and `conflict` (anything else, including any differing unit token and any "
         "differing text without numbers). Outside the curves table a row present on one side only is one "
         "`added` or `dropped` difference.",
         "- **Structural** (curves): a per-section row against a composite row for the same point, or rows "
         "split or merged on the same identity. Listed in full; judged through the curve bar.",
         "- **Bars.** pump_config and the trap fields fail on `conflict` or `dropped`; `added` and `format` "
         f"are counted and do not fail. Curves fail when the row count moves more than "
         f"±{CURVE_COUNT_TOLERANCE_PCT:g} %, an old curve is missing, or a matched row carries a `conflict`. "
         "Other tables are information.",
         "- **Traps**: for each trap field, the values per `stg_*` table compared as a multiset; the differing "
         "values pair up as `format` first, then in order, and leftovers are `added` or `dropped`.", "",
         "## Verdict", "",
         "Counts are field differences across every table (a, d, f, c = added, dropped, format, conflict).", "",
         "| Document | Family | added | dropped | format | conflict | structural | unaligned rows old / new "
         "| pump_config | traps | curves | Result |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for run in runs:
        c = results.get(run["stem"])
        if run.get("status") != "completed":
            L.append(f"| {md(run['stem'])} | {md(run.get('family'))} |" + " — |" * 9
                     + f" NOT RUN: {md(run.get('error', {}).get('code'))} |")
        elif isinstance(c, str):
            L.append(f"| {md(run['stem'])} | {run['family']} |" + " — |" * 9 + f" NOT COMPARED: {md(c)} |")
        else:
            cnt = c.counts()
            delta = "n/a (old has 0 rows)" if c.curve_delta_pct is None else f"{c.curve_delta_pct:+.1f} %"
            curves = (f"{'pass' if c.curves_ok else '**FAIL**'} ({c.curve_old}→{c.curve_new} rows, {delta}; "
                      f"{len(c.missing_curves)} missing; {c.curve_conflicts} conflict)")
            L.append(f"| {md(run['stem'])} | {run['family']} | " + " | ".join(str(cnt[k]) for k in CATEGORIES)
                     + f" | {len(c.structural)} | {c.alignment['old'].methods['unaligned']} / "
                     f"{c.alignment['new'].methods['unaligned']} | {judged(c.pump_ok, c.counts('stg_pump_config'))} "
                     f"| {judged(c.traps_ok, c.trap_counts())} | {curves} | {'PASS' if c.passed else 'FAIL'} |")

    L += ["", "### Category counts by table", "",
          "| Document | Table | added | dropped | format | conflict |", "|---|---|---|---|---|---|"]
    for run in runs:
        c = results.get(run["stem"])
        if run.get("status") != "completed" or isinstance(c, str):
            continue
        for table in sorted({d.table for d in c.diffs}):
            cnt = c.counts(table)
            L.append(f"| {md(run['stem'])} | `{table}` | " + " | ".join(str(cnt[k]) for k in CATEGORIES) + " |")
        cnt = c.trap_counts()
        L.append(f"| {md(run['stem'])} | trap fields | " + " | ".join(str(cnt[k]) for k in CATEGORIES) + " |")

    L += ["", "## Metrics and cost", "",
          "Output tokens per second is output tokens over model time less time to first token. "
          "Output tokens include reasoning; ConverseStream reports no separate reasoning count.", "",
          "| Document | Prompt bytes | Kept pages | Input tokens | Output tokens | Reasoning tokens | "
          "TTFT s | Model time s | Output tok/s | Report bytes | Bytes/output token | stop_reason | "
          "% of max_tokens | Cost USD | Cost per kept page USD |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    costs = []
    for run in runs:
        m = run.get("metrics", {})
        cost = cost_usd(m.get("input_tokens"), m.get("output_tokens"), model_prices(pricing, run.get("model_id")))
        pages = run.get("kept_page_count")
        if run.get("status") == "completed":
            costs.append(cost)
        share = m.get("max_tokens_share")
        L.append(" | ".join([
            f"| {md(run['stem'])}", num(run.get("prompt_bytes"), "{:,}"), num(pages, "{}"),
            num(m.get("input_tokens"), "{:,}"), num(m.get("output_tokens"), "{:,}"),
            "not reported" if m and m.get("reasoning_tokens") is None else num(m.get("reasoning_tokens"), "{:,}"),
            num(m.get("time_to_first_token_s"), "{:.1f}"), num(m.get("model_time_s"), "{:.1f}"),
            num(m.get("output_tokens_per_s"), "{:.1f}"), num(m.get("report_bytes"), "{:,}"),
            num(m.get("bytes_per_output_token"), "{:.2f}"), md(m.get("stop_reason")),
            num(None if share is None else share * 100, "{:.1f}"),
            "pending" if cost is None else f"{cost:.4f}",
            "pending" if cost is None or not pages else f"{cost / pages:.4f}",
        ]) + " |")
    total = None if not costs or None in costs else sum(costs)
    mean = None if total is None else total / len(costs)
    L += ["", f"- Total, {len(costs)} completed document(s): "
          + ("pending" if total is None else f"USD {total:.4f}"),
          "- Mean per document: " + ("pending" if mean is None else f"USD {mean:.4f}"),
          "- Projected per 100 documents at that mean: " + ("pending" if mean is None else f"USD {mean * 100:.2f}"),
          f"- Prices: {md(pricing.get('region'))}, {md(pricing.get('tier'))}, source {pricing.get('source_url')}."]
    for model_id in sorted({r.get("model_id") or "?" for r in runs}):
        p = model_prices(pricing, model_id)
        priced = p.get("input_usd_per_1k_tokens") is not None and p.get("output_usd_per_1k_tokens") is not None
        L.append(f"  - `{md(model_id)}`: " + (
            f"input USD {p['input_usd_per_1k_tokens']} and output USD {p['output_usd_per_1k_tokens']} per 1,000 tokens, "
            f"retrieved {p.get('retrieved')}" if priced
            else "**pending**: " + md(p.get("reason") or "no entry in pricing.json")) + ".")
    L += ["",
          "| Document | detect s | prep s | assemble s | extract s |", "|---|---|---|---|---|"]
    for run in runs:
        s = run.get("stage_durations_s", {})
        L.append(f"| {md(run['stem'])} | " + " | ".join(num(s.get(k), "{:.2f}")
                                                      for k in ("detect", "prep", "assemble", "extract")) + " |")

    longest = max((r for r in runs if r.get("status") == "completed"), key=lambda r: r.get("prompt_bytes") or 0,
                  default=None)
    if args.baseline_run:
        base_runs, base_results = load_and_compare(args.baseline_run, old_dir)
        L += against_baseline(runs, results, base_runs, base_results, pricing)

    L += ["", "## Timing verdict", ""]
    if longest is None:
        L.append("No completed run.")
    else:
        m = longest["metrics"]
        t, share = m.get("model_time_s"), m.get("max_tokens_share")
        L += [f"Longest document by prompt bytes: {md(longest['stem'])} ({longest['prompt_bytes']:,} bytes).", "",
              f"- Model time over 10 minutes: **{yn(None if t is None else t > 600)}** ({num(t, '{:.1f}')} s).",
              f"- Output over 90 % of max_tokens: **{yn(None if share is None else share > 0.9)}** "
              f"({num(m.get('output_tokens'), '{:,}')} of {m.get('max_tokens'):,})."]

    L += ["", "## Old-input check (F1)", "",
          "Whether the old batch run was sent raw private-use glyphs, read from its saved prompt.", "",
          "| Document | Old run used raw private-use glyphs |", "|---|---|"]
    for run in runs:
        if run.get("family") == "F1":
            L.append(f"| {md(run['stem'])} | {old_input_check(run['stem'], batch)} |")

    L += ["", "## Differences", "",
          "Every difference, for classification. Nothing here is judged beyond the mechanical verdict above."]
    for run in runs:
        c = results.get(run["stem"])
        L += ["", f"### {md(run['stem'])} ({run.get('family')})", ""]
        if run.get("status") != "completed":
            L.append(f"Not run: {md(run.get('error'))}")
            continue
        if isinstance(c, str):
            L.append(md(c))
            continue
        for side, errs in c.parse_errors.items():
            L += [f"- {side} report: {md(e)}" for e in errs]

        ao, an = c.alignment["old"], c.alignment["new"]
        L += ["#### Scenario alignment", "", "| Ordinal | Old labels | New labels |", "|---|---|---|"]
        for o in sorted(ao.ordinals | an.ordinals, key=lambda x: (len(x), x)):
            labels = [", ".join(f"`{md(l)}`" for l, v in sorted(a.labels.items()) if v == o) or "—" for a in (ao, an)]
            L.append(f"| {md(o)} | {labels[0]} | {labels[1]} |")
        L += ["", f"Curve and section rows aligned by: old {dict(ao.methods)}; new {dict(an.methods)}."]
        for side, a in (("old", ao), ("new", an)):
            L += [f"- {side} `unaligned`: `{md(l)}` ({n} rows)" for l, n in sorted(a.unaligned.items())]
            L += [f"- {side} label beside more than one ordinal (left unaligned): `{md(l)}` {o}"
                  for l, o in sorted(a.ambiguous.items())]

        trap = [(d, "trap") for d in c.trap_diffs]
        rows = [(d, "row") for d in c.diffs]
        for cat in FAILING_ORDER:
            items = [(d, how) for d, how in rows + trap if d.category == cat]
            L += ["", f"#### {cat} ({len(items)})", ""]
            for d, how in items:
                where = f"trap {d.key[0]}" if how == "trap" else f"key `{md(d.key)}`"
                L.append(f"- `{d.table}` · {where} · `{d.field}`: old `{md(d.old)}` → new `{md(d.new)}`")

        L += ["", f"#### structural ({len(c.structural)})", ""]
        for kind, key, old_rows, new_rows in c.structural:
            L.append(f"- **{kind}** at `{md(key)}`")
            L += [f"  - old: `{md(r)}`" for r in old_rows] + [f"  - new: `{md(r)}`" for r in new_rows]

        delta = "n/a" if c.curve_delta_pct is None else f"{c.curve_delta_pct:+.1f} %"
        L += ["", f"#### curves: {c.curve_old} old rows, {c.curve_new} new rows ({delta})", ""]
        L += [f"- missing curve: `{md(k)}`" for k in c.missing_curves]
        L += [f"- extra curve: `{md(k)}`" for k in c.extra_curves]
        L += [f"- unmatched old row: `{md(r)}`" for r in c.unmatched_old]
        L += [f"- unmatched new row: `{md(r)}`" for r in c.unmatched_new]

        added = defaultdict(list)
        for d, how in rows + trap:
            if d.category == "added":
                added[(d.table, d.field, how)].append(d)
        L += ["", f"#### added ({sum(len(v) for v in added.values())}), by field", ""]
        for (table, fld, how), ds in sorted(added.items()):
            L.append(f"- `{table}` · `{fld}`{' (trap)' if how == 'trap' else ''}: {len(ds)}; e.g. "
                     + "; ".join(f"`{clip(d.new)}`" for d in ds[:5]))

        fmt = Counter((d.table, d.field, how) for d, how in rows + trap if d.category == "format")
        L += ["", f"#### format ({sum(fmt.values())}), counts only", ""]
        L += [f"- `{table}` · `{fld}`{' (trap)' if how == 'trap' else ''}: {n}" for (table, fld, how), n in sorted(fmt.items())]

    (out / "parity_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {out / 'parity_report.md'}")

    compared = sorted((stem, c) for stem, c in results.items() if not isinstance(c, str))
    sheet = "adjudication.csv"
    if (out / sheet).is_file() and any(r.get("verdict", "").strip() for r in read_worksheet(out / sheet)[1]):
        sheet = "adjudication.regenerated.csv"
        print(f"adjudication.csv carries verdicts; leaving it as it is and writing {sheet}")
    for name, rows in ((sheet, [r for stem, c in compared for r in worksheet(stem, c)]),
                       ("added_sample.csv", [r for stem, c in compared for r in added_sample(stem, c)])):
        with open(out / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=WORKSHEET_COLUMNS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {out / name} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
