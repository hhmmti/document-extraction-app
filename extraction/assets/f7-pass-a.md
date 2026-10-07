---
title: "F7 Valiant — Pass A (extraction) prompt"
created: 2026-08-20
updated: 2026-08-20
milestone: M4
family: F7
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
  - "[[f7-valiant]]"
  - "[[staging-schemas]]"
---

# F7 Valiant — Pass A (extraction)

> [!info] **v1, 2026-08-20.** Ported from `f5-pass-a.md` v1, which carries `f2-pass-a.md` v2's hard-won rules: the schema-union rule, the invented-key ban with the actual guesses named, enum closure, the `effective_from` rule, the non-uniform-housing rule, and the *Fields not found* / *Schema gaps* consistency rule. Sensitivity-case machinery removed — F7 is single-scenario and D16/D17 cannot fire.

> [!warning] **This family prints FOUR pairs of values that look like duplicates and are not.** Each pair has two columns and putting both numbers in one destroys a check:
> `Spg Gas (HC)` / `(Mix)` · `Turpin PHI` / `Dunbar PHI` at the same stage · `Total: <n> stages` / `<n> Stages Out` · `Housings #n <n> stgs` / the group total.
> Emitting one of each looks tidy and is wrong. Emit both, every time, and never reconcile them.

> [!info] **F7 is the one family that prints stacking `Order` explicitly.** Every other contract infers `section_order` from depths or labels. Here it is printed — read it, do not derive it.

> [!info] **`f7-valiant.md` was corrected on 2026-08-20** before this prompt was written. The inlined contract carries its original text with correction subsections under §5, §6, §7 and §8 — where they disagree, the corrections win, and this prompt already reflects them.

````
# Context
You are extracting structured ESP design data from ONE Valiant design document, under extraction
contract F7 (Valiant). Milestone M4.

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
JSONL data blocks whose keys come from the inlined schemas.

# Inputs
- Page text (primary): prep/text/f7-valiant/{{source_stem}}/p*.txt — one file per page
- Sliced PDF (fallback, ambiguous tables): prep/sliced/f7-valiant/{{source_stem}}.pdf
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
   `section_role`, `curve_observation_role`, `extraction_status`, `envelope_status`,
   `motor_power_factor_basis`.
   **Every F7 observation row is `curve_observation_role = 'design'`.** `cross_check` and
   `contribution` are NOT enum values — §7 of the contract said otherwise and was corrected.

4. **Omit a key entirely when the document does not print that value.** Do not emit
   `"key": null`, `""`, `"-"` or `"n/a"`. An absent key means "not printed", which is a fact, and
   it is recorded separately under *Fields not found*. **The contract's §5 says
   `scenario_label_staged = null`** — that is CSV-era wording. Omit the key.

5. **Every value is a JSON string, verbatim as printed, including its unit token.**
   `"gas_sg": "0.75"` · `"turpin_indicator_as_printed": "0.000"` · `"stages_out": "12"`.
   Commas inside a value are ordinary characters. Do not coerce to number, do not strip
   separators or units.

6. **`organization_id`, `well_id` and `effective_from` are populated at load** — normally absent
   from the data blocks. If the document prints a design date, put it in the frontmatter
   `effective_from` as `YYYY-MM-DD`. **If it prints none, leave it EMPTY** and say so in
   Extraction Notes — never substitute the extraction date, because D18 selects the latest design
   by this field and a placeholder silently wins against real ones.

7. **A grain with no data emits an empty fenced block** — the fence with nothing between it.

# Step 0 — confirm the family
Confirm the page-1 signature named in the inlined contract §1 before extracting anything. If the
page instead shows another vendor's signature — `EQUIPMENT AND PERFORMANCE REPORT` with a
`ChampionX Representative` block (F2), `Well Name :` with `Powered by LiftXP` (F4),
`Schematics Report` with `SLB Engineer:` (F3), or `ProLift Summary Report` (F5) — STOP, name what
was expected and found, set validation_status: failed, and do NOT fall through to another
contract. The producer string and the filename never decide the family; page 1 does.

# Step 1 — pages
Read the manifest rows first, then read every page, including any plot-only pages. Record
plot-only pages as graphical-only in Extraction Notes rather than skipping them. A missing page
named as required in the contract's §2 is a HALT, not an absence.

# Step 2 — single scenario
**This is a SINGLE-SCENARIO family.** Omit `scenario_label_staged` entirely, set
`"scenario_ordinal": "1"` and `"is_design_scenario": "true"` on every row that carries them.

**D16 and D17 cannot fire on F7.** If you find a second scenario, an alternative case table or a
repeated design block, the document is not what this contract describes — HALT and report it.

# Step 3 — sections: ORDER IS PRINTED, NOT INFERRED
**F7 is the only family in the corpus that prints stacking `Order` explicitly**, in the p-housing
pressure table and the shaft power table. Read `section_order` from that column.

- `section_order` **1 = deepest**, per the printed `Order`.
- Cross-read against the Equipment List's `Start MD` / `Stop MD` → `top_md_ft` / `bottom_md_ft`.
  Where the printed `Order` and the depths disagree, emit both readings and set
  `extraction_status = 'conflict'`. **Do not choose, and do not silently prefer the depths** —
  the printed order is this family's distinguishing feature and a disagreement is a finding.
- Gas handlers are PUMP BODIES and are in scope. Never a section row: separators, seals, sensors,
  motor, cable, tubing.

## pump_config grain is `model_grouped`
Emit ONE ROW PER DISTINCT MODEL. Set `section_grain = 'model_grouped'`,
`stages_basis = 'group_total'`, `housing_count`, `stages_per_housing_as_printed`, and
**`stages_out`**.

**FOUR NUMBERS, FOUR KEYS — this is the family's most common way to go wrong.**
  `Housings #n <n> stgs`  → `stages_per_housing_as_printed`
  `Total: <n> stages`     → `stages`
  `<n> Stages Out`        → `stages_out`
  housing count           → `housing_count`

**Emit all of them. DO NOT subtract `Stages Out` from `Total`** — the net count is arithmetic and
belongs to M5 (D8), and the contract's §3 says so explicitly. Do not multiply or divide either;
where `housing_count × stages_per_housing` disagrees with `stages`, emit all values and set
`extraction_status = 'conflict'`.

Where housings are NOT uniform, emit every printed count in section order, comma-separated
(`"87, 87, 42"`). Never a modal value.

# Step 4 — model, series and manufacturer
The p3 `Series` goes to **`pump_series_as_printed`**, a separate column. Do NOT fold it into
`pump_model_as_printed`, and do not reconstruct a combined token — that is a normalization
belonging to the load layer. Emit any composite printed string (chart title, equipment
description) verbatim to `stg_alias_evidence` as D31 evidence; it must not overwrite the
separate fields.

# Step 5 — THE FOUR PAIRS
Each pair prints two numbers that look redundant. Each needs two keys.

**1. Gas gravity, two bases.** `Spg Gas (HC)` → `gas_sg`. `Spg Gas (Mix)` → `gas_sg_mixture`.
   `gas_sg_basis` is a FLAG on one column, not a second column — it cannot carry both.
   C-BUBBLE's `gamma_g` wants the **hydrocarbon** basis, which is why that one keeps `gas_sg`.

**2. Two PHI indicators at ONE stage.** `Turpin PHI` → `turpin_indicator_as_printed`.
   `Dunbar PHI` → `dunbar_indicator_as_printed`. **Both on the same `stg_gas_cascade` row**, at
   the Pump Intake stage, with `correlation_as_printed` as printed. Do NOT put a Dunbar value in
   the Turpin column and do NOT emit two rows at the same `stage_order` — the grain is
   well × scenario × stage and a second row collides.
   Dunbar has no consumer today; it is the corpus's only cross-check on the choice of Turpin, and
   that is worth nothing if the two arrive indistinguishable.

**3. Stage counts.** See Step 3 — four numbers, four keys, no arithmetic.

**4. Viscosity factors.** `Avg Cq` / `Avg Ch` / `Avg Cbhp` → **one verbatim string** in
   `viscosity_correction_factors_as_printed`, AND the derived `viscosity_corrected` boolean.
   The boolean is a judgment made from those three numbers; emit the inputs alongside it. Where a
   factor differs from 1.0000 the magnitude matters to a later correction, not just the fact.

# Step 6 — flows, pressures and depths
- `Flow @Intake` → `point_type = 'q_intake'`; `Flow @Discharge` → `'q_discharge'`;
  **`Avg Flow` → `point_type = 'design_point'`** — it is the flow the vendor evaluated the
  viscosity factors at. No new key is needed for it.
- `Pressure @Intake` → `section_intake_pressure_psi_as_printed` (D11-safe).
- **`Pump Discharge Pressure` and the per-section `Discharge` BOTH go to
  `stg_curve_observations`** → `discharge_pressure_psi_as_printed`. Design context has no
  discharge column at all.
- **`PHI` on the pressure row goes to `stg_gas_cascade`, not to the observation row.**
  Observations have no indicator column. The pressures on that row stay where they are.
- `Pump Depth (MD)` → `pump_setting_md_ft`; `Pump Depth TVD` → `pump_setting_vd_ft`. F7 is the
  first family to print the pair in both. `TPI Depth (MD)` → `perf_top_md_ft`.
- `Operating Range Min / BEP / Max` → `point_type` `ror_min` / `bep` / `ror_max`, with
  `flow_unit_as_printed`. **This is the numeric envelope C-BEP wants** — set
  `envelope_status = 'numeric'`.
- `Series/Model @ f Hz (n RPM)` — emit the RPM into `speed_rpm_as_printed`, **unconverted**, and
  the Hz into `frequency_hz_as_printed`.

# Step 6a — what is NOT a printed table
`K`-abbreviated numbers (`8.5K ft`, `1.09K bpd`), values that align to gridlines, and any number
whose only source is a plotted trace are NOT data. If a number's source is a plot, do not emit
that object; record the page as graphical-only in Extraction Notes. A printed table adjacent to a
plot IS a table and must be transcribed — the test is whether the number appears as text in a
table cell, not whether it appears near a chart.

This applies to plot-sourced values ONLY. It is not a general licence to omit rows you are unsure
of: for anything printed in a table, transcribe it and note the doubt in Extraction Notes.

# Requirements

## Not extracted — eleven rows are narrowed rather than dropped whole
Six whole rows are gone and are not in the field list below. Within eleven rows that DO stand,
these sub-fields are **not extracted**:

- **Housing and shaft RATING checks** — `Pressure @100%H2O`, `Limit`, `Load %`, `Selected`,
  `Shaft Load`. **`Description`, `Housing`, `Stages` and `Order` STAY** — `Order` is the whole
  reason this family needs no stacking guess.
- separator `HP` / `ft` · motor `Model` · `KOP` (but `TPI Depth (MD)` stays) · `Avg Spg Fluid`
  (but `Spg Fluid` → `fluid_sg_at_observation` and `Avg Pump Spg Fluid` → `vendor_mixture_sg`
  stay) · `Intake Shaft Power HP` (but `Pump Power Consumption HP` stays) · separator
  `Liquid Flow` and per-separator `Specific Gravity` (but `PFG` and `Total Gas Flow` go to the
  cascade) · `Total Flow` / `Total Liq Flow` · `Operating HP` / `V` / `F` (nameplate values stay)
  · `Total KW` (but `Motor KW` → `vendor_motor_power_kw` stays) · `OD` / `WT` / `Length` /
  `Part#` (but `Start MD` / `Stop MD` → `top_md_ft` / `bottom_md_ft` stay).
- Motor amperage excluded (D2). PI and PIP excluded (D11). Producing GOR/GLR excluded.
  `Free Allowed Gas` under its own name, never NPSHr.

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
- Every page read, plot pages included and recorded as graphical-only.
- Five JSONL blocks, keys from the inlined schemas, every line independently parseable.
- **Every key checked by eye against the inlined schemas. No key invented by naming convention.**
- **All four pairs emitted as pairs**: `gas_sg` + `gas_sg_mixture` · `turpin_indicator_as_printed`
  + `dunbar_indicator_as_printed` on ONE cascade row · `stages` + `stages_out` ·
  `stages_per_housing_as_printed` + `housing_count`.
- `section_order` read from the printed `Order`, with any depth disagreement flagged not resolved.
- `pump_series_as_printed` present and NOT folded into the model token.
- `discharge_pressure_psi_as_printed` on observation rows; `PHI` in the cascade, not observations.
- `envelope_status = 'numeric'` with `ror_min` / `bep` / `ror_max` rows.
- `speed_rpm_as_printed` in RPM, unconverted.
- No `K`-abbreviated or plot-derived value anywhere.
- *Fields not found* and *Schema gaps* both complete and consistent with each other.
- The final `## row_counts` block is present and its numbers match the blocks above it.

# Do NOT
- Do NOT emit a key that is not in the inlined schemas.
- Do NOT infer a key name from a naming convention.
- Do NOT emit a value outside an enumerated column's listed set.
- **Do NOT put a Dunbar value in `turpin_indicator_as_printed`.**
- **Do NOT emit two `stg_gas_cascade` rows at the same `stage_order`** to hold the two PHIs.
- **Do NOT put both gas gravities in `gas_sg`.**
- **Do NOT subtract `Stages Out` from `Total`, or multiply/divide the stage counts.**
- Do NOT fold `Series` into the model token.
- Do NOT derive `section_order` from depths when `Order` is printed.
- Do NOT send `Discharge Pressure` to design context, or `PHI` to observations.
- Do NOT convert `(n RPM)` to Hz.
- Do NOT extract housing or shaft rating columns, `Operating HP/V/F`, `Total KW`, `Total Flow`,
  `OD`, `WT`, `Length`, `Part#`, motor `Model`, `KOP`, `Avg Spg Fluid`, `Intake Shaft Power`,
  separator `Liquid Flow`, or motor amperage.
- Do NOT emit `library_row_exists`. It is a join computed at load.
- Do NOT emit `curve_observation_role = 'cross_check'` or `'contribution'`. Not enum values.
- Do NOT emit `null`, `""` or `"-"` — omit the key instead.
- Do NOT put the extraction date into `effective_from`.
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
{"section_order": "1", "section_role": "primary", "pump_model_as_printed": "<model as printed>", "pump_series_as_printed": "<series as printed>", "manufacturer": "Valiant", "vendor_family": "valiant", "section_grain": "model_grouped", "housing_count": "3", "stages": "261", "stages_per_housing_as_printed": "87, 87, 87", "stages_out": "12", "stages_basis": "group_total", "top_md_ft": "<Start MD>", "bottom_md_ft": "<Stop MD>", "source_document": "{{source_document}}", "source_page": "p3", "scenario_ordinal": "1", "is_design_scenario": "true", "extraction_status": "extracted", "provenance": "vendor input"}
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
| 2026-08-20 | v1 created. Ported from `f5-pass-a.md` v1 (carrying `f2-pass-a.md` v2's schema-union rule, invented-key ban, enum closure and `effective_from` rule). Sensitivity-case machinery removed — F7 is single-scenario. F7-specific and written fresh: **Step 5's four pairs**, each of which prints two numbers that look redundant and needs two keys — the gas-gravity bases, the two PHI indicators on ONE cascade row, the four stage counts, the viscosity factors; **Step 3's printed `Order`**, this being the only family that prints stacking explicitly, with depth disagreement flagged rather than resolved; Step 6's flow / pressure / depth routing including the two corrections from the audit (`Discharge Pressure` to observations, `PHI` to the cascade). The not-extracted list is written out by sub-field because eleven of the rows are narrowed rather than dropped whole, and a bare field list would not convey that `Order` stays while `Load %` goes. |
