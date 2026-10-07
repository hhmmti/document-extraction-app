---
title: "F1 SpyGlass — Pass A (extraction) prompt"
created: 2026-08-14
updated: 2026-08-18
milestone: M3
family: F1
pass: A
prompt_version: 7
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - prompt
related:
  - "[[m2-extraction-method]]"
  - "[[m2-validator-spec]]"
---

# F1 SpyGlass — Pass A (extraction)

> [!warning] **v7, 2026-08-18 — output format changed from CSV to JSONL.** v6's content was right: correct section ordering and taper identification, three scenarios, real transcription volume, the frequency traps caught to 0.01 Hz. What never converged across four versions was CSV bookkeeping — 586 rows with field-count deltas of −1, −2, −3, −6 and +1, +2 against 31- and 46-column headers, each gap silently shifting values onto neighbouring columns. **The format was the defect.** In JSONL a missing value is an absent key, not a shifted column, and a comma inside a value is an ordinary character. See [[m2-validator-spec]] §8.

> [!info] **Version history.** v6 — v4 restored plus the p18/p19 plot fix; v5 reverted (over-constrained: a rule about the first CSV field caused an extra empty field to be prepended, and "drop the row" generalised until output collapsed). v4 — CSV rules to the top, `K` ban, exact block headings, `row_counts`; first version run with the staging schemas actually inlined. v3 — curve-observation cardinality. v2 — text not rendered pages (D35), CF-19 signature, CF-20 continuation sheets.

````
# Context
You are extracting structured ESP design data from ONE Summit ESP (Halliburton) design document,
under extraction contract F1 (SpyGlass). This is one of 30 documents in this family.

Your input is LAYOUT-PRESERVED TEXT, one file per page. Column alignment is preserved with
whitespace, so a table's rows and columns are still readable as a table — read it as one. A
value's meaning comes from the block and column it sits in, not from its position in the
character stream. If a page's text is ambiguous or a table looks mangled, open that page in the
sliced PDF and read it there; say so in Extraction Notes when you do.

The extraction is VERBATIM. You do not convert units, do arithmetic, normalize frequency,
canonicalize model names, or resolve conflicts. Every transform happens later, in the load layer.

YOUR JOB IS TO TRANSCRIBE PRINTED TABLES, NOT TO DESCRIBE THEM AND NOT TO READ CHARTS.
Those are two different failures and this prompt guards both. Between them, err toward
transcribing: an extra row is visible and removable, a missing one is neither.

# Goal
One markdown report per m2-validator-spec.md §8: frontmatter, wikilinks, body blocks, and five
JSONL data blocks whose keys come from the inlined staging schemas.

# Inputs
- Page text (primary): prep/text/f1-spyglass/{{source_stem}}/p*.txt — one file per page
- Sliced PDF (fallback, ambiguous tables): prep/sliced/f1-spyglass/{{source_stem}}.pdf
- Page manifest rows, contract, report contract, STAGING SCHEMAS, and the closed 57-field list —
  all inlined below
- Document: {{source_document}}
- Pre-matched well_id: {{well_id}}   ·   match status: {{well_id_match_status}}
- Extraction date: {{extraction_date}}

# ===================== JSONL OUTPUT RULES — READ FIRST =====================

1. **One JSON object per line.** No wrapping array, no commas between lines, no pretty-printing.
   Each line is a complete, independently parseable object.

2. **Keys come from the STAGING SCHEMAS block inlined below**, spelled exactly as the schema
   spells them. Key order within an object does not matter. Do not invent a key.
   `well_name` is not a schema key — it belongs in the frontmatter.

3. **Omit a key entirely when the document does not print that value.** Do not emit
   `"key": null`, `""`, `"-"` or `"n/a"`. An absent key means "not printed", which is a fact, and
   it is recorded separately under *Fields not found*.

4. **Every value is a JSON string, verbatim as printed, including its unit token.**
   `"head_as_printed": "4,903.92 ft"` · `"stages": "124"` · `"frequency_hz_as_printed": "54.64"`.
   Commas inside a value are ordinary characters. Do not coerce to number, do not strip
   separators or units, do not reformat.

5. **`organization_id`, `well_id` and `effective_from` are populated at load** — normally absent.

6. **A grain with no data emits an empty fenced block** — the fence with nothing between it.

# Step 0 — confirm the family
Page 1 must show BOTH `SIZING REPORT` and a `Well` / `Customer` / `Prepared by` / `Date Generated`
header block. If not, STOP, name what was expected and found, set validation_status: failed, and
do NOT fall through to another contract.

`Summit ESP Representative` is NOT required — five of thirty documents omit the disclaimer
paragraph it sits in. Record `summit_representative_block_present` true/false and continue.

# Step 1 — map the pages before reading any value
Read the manifest rows first: per page, the original number, the resolved role, and for an
untitled page, which role it continues.

A chart group spans ONE PAGE PER SCENARIO and only its FIRST page carries a title. Head Curve at
p9-11 is scenario 1, 2, 3 — pages 10 and 11 print no heading. Same for BHP Curve, Single Pump
Charts, Multifrequency Tapered Pumps. Locating by title alone MISSES THE REST. Read EVERY page.

# Step 2 — section order from the printed depths
`section_order` 1 = DEEPEST (intake side), ascending toward discharge.

Determine it from the printed depth columns and the schematic stack, NOT from the P1..P6 labels —
that vendor numbering may run either direction, and on some documents it runs discharge-to-intake.
Physical check: the TAPER is the higher-capacity pump at the INTAKE end, deepest, because free gas
has not yet been compressed out there. In an SF3550/SF4300 string, SF4300 is section_order 1.

`section_role`: `taper` where a section's model differs from the string's repeated primary model;
`primary` for the repeated body; `gas_handler` for an `SFGH*` body. A string of identical bodies
has no taper — all `primary`. Record `printed_order_direction` verbatim.

# Step 3 — transcribe every printed curve table, for EVERY scenario
Emit one `stg_curve_observations` object per printed data point.

- `Multi-Frequency Head Curve` — one object per FREQUENCY per MODEL per SCENARIO, typically six
  frequencies. `curve_observation_role = 'head_curve'`, `point_type = 'curve_point'`.
  **Do this for all scenarios**, not scenario 1 only.
- `Multi-Frequency BHP Curve` — same grain, power. `role = 'bhp_curve'`.
- `Single Pump Charts` — per SECTION per SCENARIO: `ROR (bbl/d)`, `Best Efficiency (STB/D)`,
  `Operating Range (STB/D)`, `Lift (ft)`, `Q-INT`, `Q-DIS`, gas at intake/discharge, density,
  `Free Allowed Gas`. `role = 'single_pump_chart'`.
- The design point. `point_type = 'design_point'`.
- `Multiscenario Pump Curve` (p18) and `Multifrequency Tapered Pumps` (p19) ARE PLOT PAGES. Emit
  an object ONLY where a printed numeric table accompanies the plot. If the page carries only a
  chart with axis labels, emit nothing for it and record it as graphical-only in Extraction Notes.
  Where a real table exists: `role = 'multiscenario'` or `'tapered_composite'`,
  `head_basis_as_printed = 'string_total'`.

A range printed as one cell (`1087 - 5184`) is ONE printed value: emit it verbatim in the flow
field. Do not split it into two objects, do not compute a midpoint.

`head_basis_as_printed` ∈ `per_stage` | `per_section_total` | `string_total`. Exactly these three.

An `N/A` cell (F1 prints these where discharge pressure is insufficient or the well pumps off) is
a printed fact: emit the object with the frequency key present and the flow and head keys omitted,
and note the reason in Extraction Notes.

# Step 3a — what is NOT a printed table
A previous run transcribed values like `8.5K ft`, `1.09K STB/D`, `8.92K ft` from p18 and p19.
Those are CHART AXIS LABELS. `K`-abbreviated numbers, values that align to gridlines, and any
number whose only source is a plotted trace are NOT data.

If a number's source is a plot, do not emit that object. Record the page as graphical-only in
Extraction Notes. A printed table adjacent to a plot IS a table and must be transcribed — the test
is whether the number appears as text in a table cell, not whether it appears near a chart.

This applies to plot-sourced values ONLY. It is not a general licence to omit rows you are unsure
of: for anything printed in a table, transcribe it and note the doubt in Extraction Notes.

# Requirements
- **Scenarios repeat.** Design Schematic, Design Overview, Head Curve, BHP Curve, Single Pump
  Charts, Tapered Pumps, Gas Curve repeat once per scenario. One object per section PER SCENARIO,
  `scenario_label_staged` verbatim, `scenario_ordinal` by order of appearance.
- **TABLE OVER PAGE TITLE, ALWAYS — absolute.** Titles carry a frequency (`1575bpd 51.98hz`);
  tables carry the real one (`41.99`). Gaps of 0.03 and 0.01 Hz have been observed — the small ones
  are the dangerous ones. Take the table value every time, including when they agree. Record the
  title in `scenario_title_as_printed`, set `scenario_title_frequency_disagrees` to `"true"` on
  ANY difference.
- **`Lift (ft)` is a section total. Do NOT divide by `Stages`** (D8). Emit both separately.
- **Never write a string-total value into a per-section field.** The string's `Total Dynamic Head`
  is `string_total` and must not appear as a model's own head. Same for operating horsepower.
- **`housing_count` is that section's housings**, from the schematic — not the string's stage
  count, not a page number.
- **`Free Allowed Gas` under its own name, never NPSHr** (D12).
- **`bbl/d` and `STB/D` are different quantities.** Capture `flow_unit_as_printed` on every flow.
- **The `*` marker is data.** Set `viscosity_corrected` to `"true"` ONLY on rates carrying the
  marker; the legend alone is not the marker.
- **Bubble point is per scenario.** Emit each. Do not collapse, do not pick.
- **`bubble_point_unit_as_printed` is required** wherever a bubble point is emitted. F1 prints
  `PSIA`.
- **Four depth fields, four keys.** Do not merge. `depth_reference_basis = 'intake'`.
- **Motor amperage excluded** (D2). PI, PIP, Calculated Pip, Desired Pip excluded (D11).
- **Dropped by contract amendment, do not extract:** raw Oil Rate / Water Rate split · operating
  Motor Volts · Motor Input HP · Operating HP at design frequency. `Discharge Pressure` is
  extracted, and it belongs to `stg_curve_observations`, not `stg_design_context`.
- Model grammar `^(SD|SF|SFGH)\d{3,4}[A-Z]?(\s+TS\d)?(\s+XR)?(\s+\(HS Shaft\))?$`. `TS4`, `XR`,
  `(HS Shaft)` are PART OF THE MODEL.
- The p1 `Sizing` token (`SF3550-SF4300-HFGS-400hp 420MTR`) is NOT a model — emit to
  `stg_alias_evidence` as `evidence_kind = 'composite_string_token'`.
- Record `pages_present`.

## Rules for every field
- Values EXACTLY as printed, with unit tokens.
- A proposed field not on the page: omit the key AND list the field under *Fields not found*.
- **A proposed field with no destination key in the schema: list it under *Schema gaps*** with its
  contract-stated target. Never write it into prose instead — a value in prose is invisible to the
  loader and absent from the not-found list, which is worse than either.
- Never guess, interpolate, or fill from a sibling document. Absent beats plausible.
- Nothing outside the 57-field list. Note candidates in Extraction Notes instead.
- Pump bodies only: no intakes, seals, sensors, gas separators, motors, cables, tubing, sand
  guards, discharge subs. GAS HANDLERS ARE PUMP BODIES AND ARE IN SCOPE.
- Where two printed values disagree, record BOTH and set `extraction_status = 'conflict'`.

# Definition of done
- Frontmatter complete per §8, including `prompt_version: 7`; wikilinks present.
- Every scenario of every chart group transcribed — not scenario 1 only.
- Five JSONL blocks, keys from the staging schemas, every line independently parseable.
- No `K`-abbreviated or plot-derived value anywhere.
- *Fields not found* lists every proposed field absent from the page.
- *Schema gaps* lists every proposed field with no destination key, or states none.
- *Extraction notes* lists ambiguities, conflicts, anomalies, and every page treated as
  graphical-only.
- The final `## row_counts` block is present and its numbers match the blocks above it.

# Do NOT
- Do NOT emit a key that is not in the staging schemas.
- Do NOT emit `null`, `""` or `"-"` — omit the key instead.
- Do NOT coerce a value to a number or strip its unit token.
- Do NOT transcribe a chart axis label or any `K`-abbreviated number.
- Do NOT summarize a table in prose instead of transcribing its objects.
- Do NOT convert units, divide, sum, average, or normalize frequency.
- Do NOT canonicalize, strip, split or repair a model string.
- Do NOT resolve a conflict — record both sides.
- Do NOT skip an untitled page. It is a scenario, not a blank.
- Do NOT write a value into prose because it has no destination key.
- Do NOT write a Validation block or declare pass/fail.
- Do NOT vary the section heading text below. A parser depends on it.

# Output format
Per m2-validator-spec.md §8. The five data blocks come last, each introduced by a heading written
EXACTLY as shown — no backticks, no extra words, in this order:

## stg_pump_config
## stg_design_context
## stg_curve_observations
## stg_gas_cascade
## stg_alias_evidence

Each heading is followed immediately by a fenced ```jsonl block, one object per line.

Then, as the last thing in the file:

## row_counts
```jsonl
{"stg_pump_config": <n>, "stg_design_context": <n>, "stg_curve_observations": <n>, "stg_gas_cascade": <n>, "stg_alias_evidence": <n>}
```
````
