---
title: "F2 ChampionX — Pass A (extraction) prompt"
created: 2026-08-20
updated: 2026-08-20
milestone: M4
family: F2
pass: A
prompt_version: 2
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - prompt
related:
  - "[[m2-extraction-method]]"
  - "[[m2-validator-spec]]"
  - "[[f2-championx]]"
  - "[[staging-schemas]]"
---

# F2 ChampionX — Pass A (extraction)

> [!warning] **v2, 2026-08-20 — answers five measured failures from the v1 GOUDA run.** v1's structure was clean (0 unparseable lines, 0 K-values, footer matched) and every trap fired correctly. What failed was **key spelling**: the batch script inlined the staging *additions* without the M1 *base* schemas, so roughly 28 baseline keys were invented by naming convention — `oil_sg_as_printed` for `oil_sg`, `motor_nameplate_power_as_printed` for `motor_nameplate_hp`, `stages_as_printed` for `stages_at_observation`, and so on. Every one is a `V-10` violation. Fixed in the script by inlining both schema sources; the four rules below close what the script cannot.

> [!warning] **The precedence block is load-bearing.** `f2-championx.md` is inlined by the batch script *after* this body, and it still carries three statements the F4–F7 audits overturned: `library_row_exists` as an extracted field, `curve_observation_role = 'cross_check' / 'contribution'`, and the stale G1 marker. Until the contract is corrected, that block is the only thing stopping the executor from obeying the older text. Do not remove it before those edits land.

````
# Context
You are extracting structured ESP design data from ONE ChampionX design document, under
extraction contract F2 (ChampionX ESPReport). This is one of 5 documents in this family.
Milestone M4.

Your input is LAYOUT-PRESERVED TEXT, one file per page. Column alignment is preserved with
whitespace, so a table's rows and columns are still readable as a table — read it as one. A
value's meaning comes from the block and column it sits in, not from its position in the
character stream. If a page's text is ambiguous or a table looks mangled, open that page in the
sliced PDF and read it there; say so in Extraction Notes when you do. Every page of this family
extracted cleanly to text — there are no image-routed pages.

The extraction is VERBATIM. You do not convert units, do arithmetic, normalize frequency,
canonicalize model names, or resolve conflicts. Every transform happens later, in the load layer.

YOUR JOB IS TO TRANSCRIBE PRINTED TABLES, NOT TO DESCRIBE THEM AND NOT TO READ CHARTS.
Those are two different failures and this prompt guards both. Between them, err toward
transcribing: an extra row is visible and removable, a missing one is neither.

# ===================== PRECEDENCE — READ BEFORE THE CONTRACT =====================
The F2 contract is inlined below this prompt. It was written before a schema audit and three of
its statements are superseded. WHERE THE CONTRACT AND THIS PROMPT DISAGREE, THIS PROMPT WINS:

1. Contract §7 says to set `library_row_exists`. DO NOT. It is a join computed at load.
2. Contract §7 says head rows carry `curve_observation_role = 'cross_check'` and power rows
   `'contribution'`. Neither is a value in the enum. Every F2 observation row is `'design'`.
3. Contract §9 marks `stg_gas_cascade` as "schema gap G1 — no destination column". G1 IS CLOSED.
   The table is defined in the staging schemas below and its rows are emitted normally.

Everything else in the contract stands.

# Goal
One markdown report per m2-validator-spec.md §8: frontmatter, wikilinks, body blocks, and five
JSONL data blocks whose keys come from the inlined schemas.

# Inputs
- Page text (primary): prep/text/f2-championx/{{source_stem}}/p*.txt — one file per page
- Sliced PDF (fallback, ambiguous tables): prep/sliced/f2-championx/{{source_stem}}.pdf
- Page manifest rows, contract, report contract, BASE LOAD SCHEMAS, STAGING COLUMNS, and the
  closed 42-field list — all inlined below
- Document: {{source_document}}
- Pre-matched well_id: {{well_id}}   ·   match status: {{well_id_match_status}}
- Extraction date: {{extraction_date}}

# ===================== JSONL OUTPUT RULES — READ FIRST =====================

1. **One JSON object per line.** No wrapping array, no commas between lines, no pretty-printing.
   Each line is a complete, independently parseable object.

2. **KEYS COME FROM THE INLINED SCHEMAS, SPELLED EXACTLY AS THEY ARE SPELLED THERE.**
   Two blocks are inlined below and you need BOTH:
   - `# ===== BASE LOAD SCHEMAS (M1) =====` — the base column list for each table.
   - `# ===== STAGING COLUMNS =====` — additional columns that ADD to the base list.
   A table's legal keys are the union of the two.

   **DO NOT INVENT A KEY, AND DO NOT INFER ONE BY NAMING CONVENTION.** A previous run guessed
   `oil_sg_as_printed` where the schema says `oil_sg`, `motor_nameplate_power_as_printed` where
   it says `motor_nameplate_hp`, and `stages_as_printed` where it says `stages_at_observation`.
   Every guess was plausible and every guess was unloadable. Suffixes like `_as_printed` are part
   of specific column names, NOT a pattern you may apply to other columns.

   Before emitting any object, check each key against the inlined schemas by eye. If a value has
   no key there, it goes under *Schema gaps* — never into an invented key, never into prose.
   `well_name` is not a schema key; it belongs in the frontmatter.

3. **Enumerated columns take only their listed values.** The schema comment beside a column is
   the closed list. `power_basis_as_printed` is `per_stage | per_section_total | string_total` —
   exactly those three. A gas-handler-only power is `per_section_total`; a whole-string power is
   `string_total`. Do not mint `gas_handler_only` or `total`. Same for `head_basis_as_printed`,
   `point_type`, `stages_basis`, `section_role`, `curve_observation_role`, `extraction_status`.

4. **Omit a key entirely when the document does not print that value.** Do not emit
   `"key": null`, `""`, `"-"` or `"n/a"`. An absent key means "not printed", which is a fact, and
   it is recorded separately under *Fields not found*.

5. **Every value is a JSON string, verbatim as printed, including its unit token.**
   `"head_as_printed": "17.08 ft"` · `"stages": "93"` · `"frequency_hz_as_printed": "58.20"`.
   Commas inside a value are ordinary characters. Do not coerce to number, do not strip
   separators or units, do not reformat.

6. **`organization_id`, `well_id` and `effective_from` are populated at load** — normally absent
   from the data blocks. **If the document prints no design date anywhere, leave the frontmatter
   `effective_from` EMPTY and say so in Extraction Notes. DO NOT substitute the extraction date.**
   D18 selects the latest design by this field, so a placeholder date silently wins against real
   ones and rewrites which design the well is believed to have.

7. **A grain with no data emits an empty fenced block** — the fence with nothing between it.

# Step 0 — confirm the family
Page 1 must show ALL of:
- the title `EQUIPMENT AND PERFORMANCE REPORT`
- a `ChampionX Representative` block
- the headings `WELL DEPTHS`, `OPERATING PERFORMANCE`, `MAIN PUMP`, `FLUID PROPERTIES`,
  `DOWNHOLE OPERATING PERFORMANCE`, all on page 1

If page 1 instead reads `Well Name :` with a `Powered by LiftXP` footer, this is F4 ELS. STOP,
name what was expected and found, set validation_status: failed, and do NOT fall through to
another contract. Both families print through Telerik — the producer string never decides the
family.

# Step 1 — map the pages before reading any value
This family has NINE PAGES, FIXED, at n=5. Read the manifest rows first, then read every page.

  p1  header · WELL DEPTHS · OPERATING PERFORMANCE · MAIN PUMP · equipment strings ·
      FLUID PROPERTIES · DOWNHOLE OPERATING PERFORMANCE
  p2  Summary · Pump Series block (PUMP MSC_... per-housing rows) · accessory blocks
  p3-4  Performance Curve — PLOTTED, with a text-layer title and Target Conditions
  p5  Gas Separator / BOI block · Gas Handler block · Main Pump block
  p6  Motor block
  p7  Surface Equipment block
  p8-9  remaining plots or survey pages

There are NO optional page groups and NO continuation sheets in this family. A missing page is a
HALT, not an absence — report it and stop. A page carrying none of the 42 proposed fields is read
in full and recorded as out of scope in Extraction Notes, not skipped.

# Step 2 — single scenario, and section order from the printed depths
This is a SINGLE-SCENARIO family. Emit exactly one row per section. Omit
`scenario_label_staged` entirely, set `"scenario_ordinal": "1"` and `"is_design_scenario":
"true"`.

If you find a second scenario, an alternative case table, or a repeated design block, the
document is not what this contract describes — HALT and report it. D16 and D17 cannot fire here.

`section_order` 1 = DEEPEST (intake side), ascending toward discharge. Determine it from the p2
`Set Depth` column, not from the order the blocks are printed. Record `printed_order_direction`
verbatim.

`section_role`: `gas_handler` for the Gas Handler body; `primary` for the MAIN PUMP body.
GAS HANDLERS ARE PUMP BODIES AND ARE IN SCOPE — in this family the gas handler is the only body
that prints a per-stage lift, so dropping it loses the family's most valuable row.

# Step 2a — pump_config grain is `model_grouped`
Emit ONE ROW PER DISTINCT MODEL, not one row per housing. Set `section_grain = 'model_grouped'`.

- `stages` = the model group's TOTAL stage count (p1 MAIN PUMP / p5 block).
- `housing_count` = how many p2 `PUMP MSC_...` housings carry that model.
- `stages_per_housing_as_printed` = the per-housing stage counts, as printed.
- `stages_basis` = `group_total`.

**HOUSINGS ARE NOT ALWAYS UNIFORM.** A five-housing string printing one `42 STG` row and four
`93 STG` rows is normal. Where they differ, emit `stages_per_housing_as_printed` as **every
printed count in section order, comma-separated** — `"42, 93, 93, 93, 93"` — and set
`extraction_status = 'conflict'`. **DO NOT pick a dominant or modal value**: a single `93` there
reads as a uniform string, makes `housing_count × stages_per_housing` disagree with `stages` for
a reason that is not a real disagreement, and loses the 42-stage housing entirely.

Emit all counts as printed. DO NOT multiply, divide, or reconcile them against `stages`.

# Step 3 — transcribe the printed hydraulics
Emit one `stg_curve_observations` object per printed data point. For this family every
observation row carries `curve_observation_role = 'design'`.

- **p5 Gas Handler block** — `Lift / Stage` with `head_basis_as_printed = 'per_stage'`; `TDH`
  with `head_basis_as_printed = 'per_section_total'`; `BHP Gas Handler Only` and `Total Power`,
  both `power_basis_as_printed = 'per_section_total'` and `'string_total'` respectively;
  `Efficiency`. `point_type = 'design_point'`.
- **p5 Main Pump block** — `Intake volume` / `Discharge volume` as two objects differing by
  `point_type` (`q_intake` / `q_discharge`); `Pressure at intake` →
  `section_intake_pressure_psi_as_printed`; `Pressure at discharge` →
  `discharge_pressure_psi_as_printed`; `Free gas at intake` → `free_gas_at_inlet_pct`;
  `Free gas at discharge` → `free_gas_at_discharge_pct`.
- **p1 DOWNHOLE OPERATING PERFORMANCE** — `Total volume at intake` as the design-point flow.
- **p3-4 `Target Conditions: <q> bpd at <h> ft`** — ONE composite object:
  `observation_is_composite = "true"`, `head_basis_as_printed = 'string_total'`,
  `point_type = 'design_point'`. It is NOT a per-section observation and must never be
  attributed to a single model.
- **p3-4 Performance Curve title** (`<model> / <n> Stages / <f> Hz`) — names the model and
  frequency the plotted curve belongs to. Emit the title string to `stg_alias_evidence`
  (`evidence_kind = 'chart_title'`).

# Step 3b — the gas cascade, and the one value not to put in it
The p5 Gas Separator / BOI block goes to `stg_gas_cascade`, one row per cascade stage, with
`component_is_pump_body = "false"` on separator stages.

**`gas_rate_as_printed` IS THE FREE GAS VOLUME, NEVER THE TOTAL FLUID VOLUME.** A previous run
wrote the p1 `Total volume at intake` figure into it, which reads as a well producing pure gas.
If the block prints a free-gas percentage but no separate gas volume beside it, emit
`gas_pct_as_printed` and OMIT `gas_rate_as_printed`. Intake and discharge *fluid* volumes from
that block belong on the observation rows, not in the cascade.

# Step 3a — what is NOT a printed table
p3-4 and any remaining plot pages are PLOT PAGES. `K`-abbreviated numbers (`8.5K ft`,
`1.09K bpd`), values that align to gridlines, and any number whose only source is a plotted trace
are NOT data. If a number's source is a plot, do not emit that object; record the page as
graphical-only in Extraction Notes. A printed table adjacent to a plot IS a table and must be
transcribed — the test is whether the number appears as text in a table cell, not whether it
appears near a chart.

This applies to plot-sourced values ONLY. It is not a general licence to omit rows you are unsure
of: for anything printed in a table, transcribe it and note the doubt in Extraction Notes.

# Requirements

## The traps — these are the reason this contract is family-specific
- **TWO POWER FACTORS, AND THEY ARE DIFFERENT QUANTITIES.** `Operating Power Factor` on **p6
  (Motor block)** → `motor_power_factor` with `motor_power_factor_basis = 'operating'`.
  `Power Factor` on **p7 (Surface Equipment block)** → `surface_power_factor`. They must NEVER
  substitute for each other. If you find only one, record which page it came from in Extraction
  Notes and DO NOT assume it is the motor one. A p7 value written into `motor_power_factor`
  silently corrupts every BHP proxy for this well.
- **TWO SGs, AND THEY ANSWER DIFFERENT QUESTIONS.** `Fluid Composite SG` → `vendor_mixture_sg`.
  `Liquid Phase SG` → `vendor_liquid_phase_sg`. Emit both; choose neither. There is no `sg_basis`
  key — the column you pick carries the basis.
- **NO NUMERIC BEP AND NO NUMERIC OPERATING RANGE. THIS IS EXPECTED.** The text layer holds only
  the reversed label strings `MinOperatingFlow` / `BEPFlow` / `MaxOperatingFlow` with no numbers.
  Those are LABELS, NOT DATA. Emit NO BEP or ROR rows. Set `envelope_status = 'graphical_only'`
  and `digitization_candidate_page = 'p3-4 Performance Curve'` on the observation rows.
  DO NOT read numbers off the plotted curve.
- **BUBBLE-POINT UNIT IS `psi` HERE.** `bubble_point_unit_as_printed` is required wherever a
  bubble point is emitted. It is a different token from other families' `PSIA` and `psig` —
  record what is printed and reconcile nothing.
- **FOUR DEPTHS, FOUR KEYS.** `Pump Setting MD` (p1) → `pump_setting_md_ft`; `Pump Setting VD`
  (p1) → `pump_setting_vd_ft`; `Intake Set Depth` (p2) → `intake_set_depth_md_ft`; `DATUM` (p1) →
  `datum_depth_ft`. The p1 pair is `vendor input`, the p2 value is `vendor derived`. Do not merge
  them. `depth_reference_basis = 'pump_setting'`.
- **`Regional PVT` IS PROVENANCE.** Emit the string verbatim into `bubble_point_correlation` —
  it is the only thing that says this bubble point came from a regional correlation rather than
  a lab measurement.
- **TWO TDH VALUES ON p1.** `Required System TDH` → `vendor_required_tdh_ft`; `Total System TDH`
  → `vendor_tdh_at_design_ft`. Different quantities; the benchmark is the comparison between
  them, so emitting one destroys it.
- **MODEL GRAMMAR — TWO STRINGS, NEITHER CANONICAL.** p1 `MAIN PUMP` prints `400UNB35H` →
  `pump_model_as_printed`. p2 prints `PUMP MSC_400UNB_35H_93 STG_...` →
  `pump_model_as_printed_alt`, VERBATIM, INCLUDING the `MSC_` prefix and every underscore.
  Grammar for the p1 form: three digits, two to four capitals, two to four digits, an optional
  trailing capital. DO NOT reassemble `MSC_400UNB_35H_` into `400UNB35H` — that is a
  normalization and belongs to the load layer.
- **NON-PUMP EQUIPMENT IS NOT EXTRACTED TO ANY TABLE.** `GAS SEPARATOR`, `PROTECTOR`, `MOTOR`,
  `SENSOR`, `CONTROLLER`, `CABLE`, `De-Sander`, `Tail Pipe`, `Accessories Above ESP` are section
  context only. They get no `stg_pump_config` row and no row anywhere else. Their ordering is
  already implied by the pump-body rows' `section_order`. The gas-separator HYDRAULICS that do
  matter (free gas, volumes, pressures, separator count) go to `stg_gas_cascade` or, where they
  are fluid volumes, to the observation rows.
- **`Free Allowed Gas` under its own name, never NPSHr.** PI, PIP, Calculated Pip, Desired Pip
  excluded. Motor amperage excluded — there is none in the proposed set for this family, so do
  not add one.

## Rules for every field
- Values EXACTLY as printed, with unit tokens.
- A proposed field not printed on any page: omit the key AND list the field under
  *Fields not found*.
- **A proposed field that IS printed but has no destination key in the schemas: list it under
  *Schema gaps*** with its contract-stated target. Never write it into prose instead, and never
  invent a key for it.
- **A field that lands in neither block has not landed.** *Fields not found* may read "None" ONLY
  if *Schema gaps* also reads "None". If either lists entries, say plainly in Extraction Notes how
  many of the 42 were emitted and how many were not.
- Never guess, interpolate, or fill from a sibling document. Absent beats plausible.
- Nothing outside the 42-field list. Note candidates in Extraction Notes instead.
- Where two printed values disagree, record BOTH and set `extraction_status = 'conflict'`.
- Record `pages_present` and `vendor_family` on every row that has the key.

# Definition of done
- Frontmatter complete per §8, including `prompt_version: 2`; wikilinks present;
  `effective_from` empty if no design date is printed.
- All nine pages read; none skipped as "just a plot" or "just a survey".
- Five JSONL blocks, keys from the inlined schemas, every line independently parseable.
- **Every key checked by eye against the inlined schemas. No key invented by naming convention.**
- Every enumerated column carries one of its listed values.
- One `stg_pump_config` row per distinct model, with `housing_count`,
  `stages_per_housing_as_printed` (every count where housings differ) and
  `stages_basis = group_total`.
- Both power factors present in separate keys, or the single one found reported with its page.
- Both SGs present in separate keys.
- `envelope_status = 'graphical_only'` set; no BEP or ROR rows emitted.
- No `K`-abbreviated or plot-derived value anywhere.
- *Fields not found* and *Schema gaps* both complete and consistent with each other.
- *Extraction notes* lists ambiguities, conflicts, anomalies, and every page treated as
  graphical-only or out of scope.
- The final `## row_counts` block is present and its numbers match the blocks above it.

# Do NOT
- Do NOT emit a key that is not in the inlined schemas.
- Do NOT infer a key name from a naming convention. `_as_printed` is part of particular column
  names, not a suffix you may add to others.
- Do NOT emit a value outside an enumerated column's listed set.
- Do NOT emit `null`, `""` or `"-"` — omit the key instead.
- Do NOT put the extraction date into `effective_from` when no design date is printed.
- Do NOT write a total fluid volume into `gas_rate_as_printed`.
- Do NOT emit `library_row_exists`. This family DOES hit the library model list, which is exactly
  why it must not be extracted — it is a join computed at load, and extracting it bakes in a
  point-in-time answer.
- Do NOT emit `curve_observation_role = 'cross_check'` or `'contribution'`. Those are not values
  in the enum. Every F2 observation row is `role = 'design'`.
- Do NOT coerce a value to a number or strip its unit token.
- Do NOT transcribe a chart axis label or any `K`-abbreviated number.
- Do NOT read a BEP, minimum or maximum flow off the Performance Curve plot.
- Do NOT summarize a table in prose instead of transcribing its objects.
- Do NOT convert units, divide, sum, average, or normalize frequency.
- Do NOT canonicalize, strip, split or repair a model string.
- Do NOT reassemble the p2 `MSC_` string into the p1 model token.
- Do NOT emit a `stg_pump_config` row for any non-pump equipment.
- Do NOT pick a dominant per-housing stage count where housings differ.
- Do NOT resolve a conflict — record both sides.
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

Each heading is followed immediately by a fenced ```jsonl block, one object per line, like this:

## stg_pump_config
```jsonl
{"section_order": "2", "section_role": "primary", "pump_model_as_printed": "400UNB35H", "pump_model_as_printed_alt": "PUMP MSC_400UNB_35H_93 STG_FLT_CSH_416SSHB_INC718", "manufacturer": "ChampionX", "vendor_family": "championx", "section_grain": "model_grouped", "housing_count": "5", "stages": "414", "stages_per_housing_as_printed": "42, 93, 93, 93, 93", "stages_basis": "group_total", "source_document": "{{source_document}}", "source_page": "p2", "scenario_ordinal": "1", "is_design_scenario": "true", "extraction_status": "conflict", "provenance": "vendor input"}
```

Then, as the last thing in the file:

## row_counts
```jsonl
{"stg_pump_config": <n>, "stg_design_context": <n>, "stg_curve_observations": <n>, "stg_gas_cascade": <n>, "stg_alias_evidence": <n>}
```
````

## Log

| Date | Update |
|---|---|
| 2026-08-20 | v1 created. Ported from `f1-pass-a.md` v7. F2-specific: page-1 signature, nine fixed pages, two-string model grammar, T1 two power factors, T2 two SGs, T3 graphical-only envelope, T5 four depths, `model_grouped` grain per CF-28. Carries a precedence block overriding three superseded statements in `f2-championx.md`. |
| 2026-08-20 | *Correction, recorded not rewritten:* Claude's first write of this file dropped the literal JSONL fence examples from *Output format*; the follow-up patch to restore them targeted the section's last block, which was the whole four-backtick fence, and replaced the entire prompt body with the two-paragraph tail. `f2-batch.sh` caught it immediately — `FATAL: no fenced body found`. File rewritten whole. The preflight did its job; the patch targeting did not. |
| 2026-08-20 | **v2 — five failures measured on the v1 GOUDA run.** (1) **~28 invented keys**, the run's only serious defect: the batch script inlined the staging *additions* without M1's *base* schemas, so baseline columns were guessed by naming convention (`oil_sg_as_printed`, `motor_nameplate_power_as_printed`, `stages_as_printed`). *Claude's error* — the sed edit replaced the M1 schema source instead of adding to it. Fixed in the script by inlining both; rule 2 now names the failure explicitly. (2) `power_basis_as_printed` values `gas_handler_only` / `total` invented outside the enum — rule 3 added. (3) `effective_from` filled with the extraction date as a placeholder, which would silently win D18's latest-wins selection — rule 6 added. (4) The p1 total fluid volume written into `gas_rate_as_printed` — Step 3b added. (5) *Fields not found* read "None" while *Schema gaps* listed four entries. Also: `stages_per_housing_as_printed` cannot represent a non-uniform string (GOUDA is `42 + 93×4`), and taking the modal value loses a housing — Step 2a now requires every printed count. That column was added by the F5 audit from a uniform `6 × 123` string; F2 is the counter-example, and it should be recorded against the F5 audit entry in `staging-schemas.md`. |
