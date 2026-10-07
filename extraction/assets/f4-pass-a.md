---
title: "F4 ELS — Pass A (extraction) prompt"
created: 2026-08-20
updated: 2026-08-20
milestone: M4
family: F4
pass: A
prompt_version: 1
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - prompt
related:
  - "[[m2-extraction-method]]"
  - "[[m2-validator-spec]]"
  - "[[f4-els]]"
  - "[[staging-schemas]]"
---

# F4 ELS — Pass A (extraction)

> [!info] **v1, 2026-08-20.** Ported from `f3-pass-a.md` v1, which itself carries `f2-pass-a.md` v2's hard-won rules: the schema-union rule, the invented-key ban with the actual guesses named, enum closure, the `effective_from` rule, the non-uniform-housing rule, the `gas_rate_as_printed` guard, and the *Fields not found* / *Schema gaps* consistency rule. Dropped from the F3 port: everything multi-scenario. **F4 is single-scenario and D16/D17 cannot fire.**

> [!warning] **The thinnest family in the corpus, and the one most likely to be under-extracted.** 5–6 pages, 38 proposed fields, no BEP, no ROR, no power factor, no per-section depths. It is small because the documents are thin, not because anything is missing. **The contract's T5 and §7 say F4 "emits no curve rows at all" — that is false and was corrected on 2026-08-20.** Eight rows target `stg_curve_observations`. An extractor obeying the uncorrected text emits an empty block and loses the family; the rule below states it positively so there is nothing to misread.

> [!info] **`f4-els.md` was corrected on 2026-08-20** before this prompt was written. The inlined contract carries its original text with correction subsections appended under §5, §6 and §8 — where they disagree, the corrections win, and this prompt already reflects them.

````
# Context
You are extracting structured ESP design data from ONE Endurance Lift Solutions design document,
under extraction contract F4 (ELS LiftXP). This is one of 3 documents in this family.
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

# Goal
One markdown report per m2-validator-spec.md §8: frontmatter, wikilinks, body blocks, and five
JSONL data blocks whose keys come from the inlined schemas.

# Inputs
- Page text (primary): prep/text/f4-els/{{source_stem}}/p*.txt — one file per page
- Sliced PDF (fallback, ambiguous tables): prep/sliced/f4-els/{{source_stem}}.pdf
- Page manifest rows, contract, report contract, BASE LOAD SCHEMAS, STAGING COLUMNS, and the
  closed field list — all inlined below
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

   **DO NOT INVENT A KEY, AND DO NOT INFER ONE BY NAMING CONVENTION.** A previous family's run
   guessed `oil_sg_as_printed` where the schema says `oil_sg`, `motor_nameplate_power_as_printed`
   where it says `motor_nameplate_hp`, and `stages_as_printed` where it says
   `stages_at_observation`. Every guess was plausible and every guess was unloadable. Suffixes
   like `_as_printed` are part of specific column names, NOT a pattern you may apply to others.

   Before emitting any object, check each key against the inlined schemas by eye. If a value has
   no key there, it goes under *Schema gaps* — never into an invented key, never into prose.
   `well_name` is not a schema key; it belongs in the frontmatter.

3. **Enumerated columns take only their listed values.** The schema comment beside a column is
   the closed list. `power_basis_as_printed` is `per_stage | per_section_total | string_total` —
   exactly those three. Same for `head_basis_as_printed`, `point_type`, `stages_basis`,
   `section_role`, `curve_observation_role`, `extraction_status`, `envelope_status`.
   **Every F4 observation row is `curve_observation_role = 'design'`.**

4. **Omit a key entirely when the document does not print that value.** Do not emit
   `"key": null`, `""`, `"-"` or `"n/a"`. An absent key means "not printed", which is a fact, and
   it is recorded separately under *Fields not found*. **The contract's §3, §5 and T4 say certain
   fields are "null" for F4** — that is CSV-era wording. Omit those keys.

5. **Every value is a JSON string, verbatim as printed, including its unit token.**
   `"gas_rate_as_printed": "8.43"` with `"gas_rate_unit_as_printed": "mcf/d"` ·
   `"bubble_point_psi": "1800.00"` with `"bubble_point_unit_as_printed": "psi"`. Commas inside a
   value are ordinary characters. Do not coerce to number, do not strip separators or units.

6. **`organization_id`, `well_id` and `effective_from` are populated at load** — normally absent
   from the data blocks. If the document prints a design date, put it in the frontmatter
   `effective_from` as `YYYY-MM-DD`. **If it prints none, leave it EMPTY** and say so in
   Extraction Notes — never substitute the extraction date, because D18 selects the latest design
   by this field and a placeholder silently wins against real ones.

7. **A grain with no data emits an empty fenced block** — the fence with nothing between it.

# Step 0 — confirm the family
Page 1 must show ALL of:
- a `Well Name :` block — **with the space before the colon**, as printed
- a `Powered by LiftXP` footer
- a `Pumps` block with **`Manufacture`** printed as a field label — note `Manufacture`, NOT
  `Manufacturer`

If page 1 reads `EQUIPMENT AND PERFORMANCE REPORT` with a `ChampionX Representative` block, this
is **F2**. STOP, name what was expected and found, set validation_status: failed, and do NOT fall
through to another contract. Both families print through Telerik; the producer string never
decides the family.

# Step 1 — pages
**5 or 6 pages.** The difference is trailing plots. There are no optional data pages and no
continuation sheets. A missing p1 or p2 is a HALT.

  p1    header · `Pumps` block (Top / Bottom) · `Well Completion` · `Operating Performance` ·
        `Fluid Properties` · `Motor M1`
  p2    `OPERATING PARAMETERS` · `FLUID PROPERTIES` · `OPERATING PERFORMANCE` · pump chart title
  p3+   `Frequency Head chart`, `Frequency Power chart` — PLOT-ONLY, no text-layer numbers

Read every page, including the plot pages, and record them as graphical-only in Extraction Notes
rather than skipping them.

# Step 2 — single scenario
**This is a SINGLE-SCENARIO family.** Omit `scenario_label_staged` entirely, set
`"scenario_ordinal": "1"` and `"is_design_scenario": "true"` on every row that carries them.

**D16 and D17 cannot fire on F4.** If you find a second scenario, an alternative case table or a
repeated design block, the document is not what this contract describes — HALT and report it.

# Step 3 — sections
The `Pumps` block on p1 prints **`Top` and `Bottom`** columns. Those are the sections.

- **`Bottom` = deepest = `section_order` 1.**
- Where only one column is populated the string is single-section. That is normal, not an error.
- NEVER a section row: intake section, packer, seals, sensor, motor, cable.
- **This family prints NO per-section depths.** Omit `top_md_ft` and `bottom_md_ft` — do not
  emit them as null, and **do not derive them from `Pump Setting Depth`**.

## pump_config grain is `model_grouped`
Emit ONE ROW PER DISTINCT MODEL. Set `section_grain = 'model_grouped'` and
`stages_basis = 'total_reported'` — F4 prints `Nr Stages` per column with **no housing count**,
so omit `housing_count` and `stages_per_housing_as_printed` unless the document prints them.

# Step 4 — model grammar: THREE SEPARATE FIELDS, DO NOT CONCATENATE
This is the ONLY family that prints manufacturer and series as **separately labelled fields**
rather than folding them into the model token.

  `Manufacture`  →  `manufacturer`               e.g. `ELS`
  `Series`       →  `pump_series_as_printed`      e.g. `400`
  `Model`        →  `pump_model_as_printed`       e.g. `ELS-1750`   (grammar: `^ELS-\d{3,4}$`)

Emit all three verbatim. **Building `400ELS1750` or `ELS 400 1750` is a normalization and belongs
to the load layer.** `ELS-1750` carries no series token, so if you drop `Series` the `400` is
unrecoverable.

**`manufacturer` comes from the `Manufacture` FIELD** — not from the footer, not from the
filename, not from the vendor family. Here the report author and the pump maker happen to
coincide, which makes the usual trap invisible; read the field anyway.

The p2 chart title prints `<manufacturer> <series> <model>` as one string. Emit it verbatim to
`stg_alias_evidence` with `evidence_kind = 'chart_title'`. It is D31 evidence for exactly how
this vendor concatenates and **must not overwrite the three separate fields.**

# Step 5 — what this family DOES contribute
**Read this positively. The contract's T5 and §7 say F4 emits "no curve rows at all", and that
is wrong** — corrected 2026-08-20. What is true: F4 has **no curve POINTS**. No BEP, no ROR, no
plotted head curve. Set `envelope_status = 'absent'`.

**These rows ARE emitted to `stg_curve_observations`:**
- `Nr Stages` → `stages_at_observation`
- `Operating Frequency` → `frequency_hz_as_printed`
- `Intake Production Rate` → `flow_as_printed` + `flow_unit_as_printed`,
  `point_type = 'design_point'`
- `Pump TDH` → `head_as_printed`, `head_basis_as_printed = 'string_total'`
- `Required TDH` → `head_as_printed`, `head_basis_as_printed = 'string_total'` on its own row
- `Total Required BHP` → `power_as_printed`, `power_basis_as_printed = 'string_total'`,
  `observation_is_composite = "true"` — it is a STRING-level number, not attributable to a section
- the p2 chart title's model reference

If `stg_curve_observations` comes back empty, you have misread T5. Go back.

# Step 5a — what is NOT a printed table
`Frequency Head chart` and `Frequency Power chart` are PLOT PAGES with no text-layer numbers.
`K`-abbreviated numbers (`8.5K ft`, `1.09K bpd`), values that align to gridlines, and any number
whose only source is a plotted trace are NOT data. Record those pages as graphical-only in
Extraction Notes and emit nothing from them.

This applies to plot-sourced values ONLY. It is not a general licence to omit rows you are unsure
of: for anything printed in a table, transcribe it and note the doubt in Extraction Notes.

# Requirements

## The traps — these are the reason this contract is family-specific
- **`Calculated Pb` IS THE CORPUS'S ONLY EXPLICIT PROVENANCE FLAG. EXTRACT IT, AND LET IT SET THE
  CLASS.** `Batman 134H` prints `Calculated Pb  No` beside `Pb 1800.00 (psi)`. No other family
  states whether the bubble point was typed or computed.
  Emit `bubble_point_calculated_flag` verbatim (`Yes` / `No`), then set
  `bubble_point_provenance` FROM IT: `No` → `vendor input` (the engineer typed it);
  `Yes` → `vendor derived` (LiftXP computed it). If the flag is absent,
  `bubble_point_provenance = 'vendor assumption'` and say so in Extraction Notes.
  **Never guess the flag.**
- **FREE GAS IS IN `mcf/d`, NOT A PERCENTAGE. UNIQUE TO THIS FAMILY.** `Free gas at Intake
  8.43 (mcf/d)`. Every other family prints a percentage or a volume fraction. Emit to
  `stg_gas_cascade` as `gas_rate_as_printed` WITH `gas_rate_unit_as_printed = 'mcf/d'`.
  **Do NOT send it to `vendor_free_gas_into_pump_pct`** — that is a PERCENT column, and `8.43`
  landing there reads as 8.43 % free gas: wrong by orders of magnitude and plausible enough to
  survive review. This is the single most consequential trap in the family.
- **BUBBLE-POINT UNIT IS `psi` HERE**, printed in parentheses: `Pb 1800.00 (psi)`. Strip nothing.
  `bubble_point_unit_as_printed = 'psi'`, required wherever a bubble point is emitted.
- **NO POWER FACTOR ANYWHERE, 0 of 3. That is a finding, not a gap.** Do not substitute anything
  from the `Motor M1` block. Omit `motor_power_factor` and list the field under *Fields not
  found*. This is the second family with this finding and it is why `C-BHP` upgrades on 5 of 7.
- **`Composite SG` IS A BENCHMARK, NOT AN INPUT.** Emit to `vendor_mixture_sg`. There is no
  `sg_basis` key — the column carries the basis. It checks `calc_mixture_sg`; it does not replace
  `sg_for_dp`.
- **TWO TDH VALUES, TWO COLUMNS.** `Pump TDH` → `vendor_tdh_at_design_ft`;
  `Required TDH` → `vendor_required_tdh_ft`. Different quantities — produced head against
  required head — and the benchmark IS the comparison, so putting both in one column destroys it.
- **TWO WATER CUTS, TWO COLUMNS.** p1 `Water Cut` → `water_cut_design_frac` (vendor input);
  p2 `Calculated Water cut` → `vendor_calculated_water_cut_frac` (vendor derived, a check).
- **`Pump Setting Depth`** → `pump_setting_md_ft` with `depth_reference_basis = 'pump_setting'`.
  The document prints no MD/VD qualifier; use that key and do not guess a second one.
- **`Rot. Sep Efficiency`** → `stg_gas_cascade.separation_efficiency_pct`. The `Packer installed`
  and `Intake Section` values printed beside it are NOT proposed and are not extracted.
- **MOTOR AMPERAGE EXCLUDED.** From `Motor M1`, extract `Type`, `Hp` and `Volts`.
  **`Amps`, `Series` and `Shroud` are NOT extracted** — `Amps` by D2, the other two dropped
  2026-08-20 as untraced.
- **`Email` IS A LITERAL PLACEHOLDER STRING** at n=3. Not proposed, not extracted, and its
  constancy is not a data-quality signal.
- **`GOR` on p2 is not proposed; `Calculated Water cut` on the same row is.** Extract only the
  latter.
- **`Free Allowed Gas` under its own name, never NPSHr.** PI and PIP excluded. Producing GOR/GLR
  excluded.

## Rules for every field
- Values EXACTLY as printed, with unit tokens.
- A proposed field not printed on any page: omit the key AND list it under *Fields not found*.
- **A proposed field that IS printed but has no destination key in the schemas: list it under
  *Schema gaps*** with its contract-stated target. Never write it into prose, never invent a key.
- **A field that lands in neither block has not landed.** *Fields not found* may read "None" ONLY
  if *Schema gaps* also reads "None". If either lists entries, say plainly in Extraction Notes how
  many of the proposed fields were emitted and how many were not.
- Never guess, interpolate, or fill from a sibling document. Absent beats plausible.
- Nothing outside the inlined field list. Note candidates in Extraction Notes instead.
- Where two printed values disagree, record BOTH and set `extraction_status = 'conflict'`.
- Record `pages_present`, `n_sections`, and `vendor_family` on every row that has the key.

# Definition of done
- Frontmatter complete per §8, including `prompt_version: 1`; wikilinks present.
- All 5–6 pages read, plot pages included and recorded as graphical-only.
- Five JSONL blocks, keys from the inlined schemas, every line independently parseable.
- **Every key checked by eye against the inlined schemas. No key invented by naming convention.**
- **`stg_curve_observations` is NOT empty** — the design-point rows are the family's contribution.
- `manufacturer`, `pump_series_as_printed` and `pump_model_as_printed` all three present and
  unconcatenated.
- `bubble_point_calculated_flag` emitted and `bubble_point_provenance` set from it.
- Free gas in `stg_gas_cascade` with its `mcf/d` unit token, and NOT in a percent column.
- `vendor_tdh_at_design_ft` and `vendor_required_tdh_ft` both present where both are printed.
- `envelope_status = 'absent'`; no BEP or ROR rows.
- No `K`-abbreviated or plot-derived value anywhere.
- *Fields not found* and *Schema gaps* both complete and consistent with each other.
- The final `## row_counts` block is present and its numbers match the blocks above it.

# Do NOT
- Do NOT emit a key that is not in the inlined schemas.
- Do NOT infer a key name from a naming convention.
- Do NOT emit a value outside an enumerated column's listed set.
- Do NOT emit an empty `stg_curve_observations` block. T5 means no curve POINTS, not no rows.
- Do NOT write the `mcf/d` free-gas figure into `vendor_free_gas_into_pump_pct` or any other
  percent column.
- Do NOT concatenate `Manufacture`, `Series` and `Model` into one token.
- Do NOT take `manufacturer` from the footer, the filename or the vendor family.
- Do NOT let the p2 chart title overwrite the three separate model fields.
- Do NOT guess `Calculated Pb` when it is absent.
- Do NOT derive `top_md_ft` / `bottom_md_ft` from `Pump Setting Depth`.
- Do NOT put `Pump TDH` and `Required TDH` in the same column.
- Do NOT substitute anything from the `Motor M1` block for a power factor.
- Do NOT extract `Amps`, `Series`, `Shroud`, `Email`, `GOR`, `Packer installed` or
  `Intake Section`.
- Do NOT emit `null`, `""` or `"-"` — omit the key instead.
- Do NOT put the extraction date into `effective_from`.
- Do NOT emit `library_row_exists`, `power_factor_absent_confirmed`, or a `sg_basis` key. None is
  a column.
- Do NOT emit `curve_observation_role = 'cross_check'` or `'contribution'`. Not enum values.
- Do NOT coerce a value to a number or strip its unit token.
- Do NOT transcribe a chart axis label or any `K`-abbreviated number.
- Do NOT summarize a table in prose instead of transcribing its objects.
- Do NOT resolve a conflict — record both sides.
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
{"section_order": "1", "section_role": "primary", "manufacturer": "ELS", "pump_series_as_printed": "400", "pump_model_as_printed": "ELS-1750", "vendor_family": "els", "section_grain": "model_grouped", "stages": "212", "stages_basis": "total_reported", "source_document": "{{source_document}}", "source_page": "p1", "scenario_ordinal": "1", "is_design_scenario": "true", "extraction_status": "extracted", "provenance": "vendor input"}
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
| 2026-08-20 | v1 created. Ported from `f3-pass-a.md` v1 (which carries `f2-pass-a.md` v2's schema-union rule, invented-key ban, enum closure, `effective_from` rule and non-uniform-housing rule). Multi-scenario machinery removed — F4 is single-scenario and D16/D17 cannot fire. F4-specific and written fresh: the `Well Name :` / `Powered by LiftXP` / `Manufacture` signature with the F2 HALT, the three-separate-fields model grammar, T1's `Calculated Pb` provenance flag, T2's `mcf/d` free gas with its explicit ban on the percent column, the two-TDH and two-water-cut splits from the F4 audit's new columns. **Step 5 is written positively against the contract's own T5/§7, which say F4 emits no curve rows — false, and the correction is stated twice plus a Do-NOT, because an extractor obeying the original text empties the family.** |
