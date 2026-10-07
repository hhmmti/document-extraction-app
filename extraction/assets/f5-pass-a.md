---
title: "F5 Baker ProLift — Pass A (extraction) prompt"
created: 2026-08-20
updated: 2026-08-20
milestone: M4
family: F5
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
  - "[[f5-baker]]"
  - "[[staging-schemas]]"
---

# F5 Baker ProLift — Pass A (extraction)

> [!info] **v1, 2026-08-20.** Ported from `f4-pass-a.md` v1, which carries `f2-pass-a.md` v2's hard-won rules: the schema-union rule, the invented-key ban with the actual guesses named, enum closure, the `effective_from` rule, the non-uniform-housing rule, the `gas_rate_as_printed` guard, and the *Fields not found* / *Schema gaps* consistency rule.

> [!warning] **Two things in this family are wrong by 2× and 4× if misread, and both look plausible.**
> **The motor runs at twice the shaft frequency** — a permanent-magnet motor printing `Motor Frequency 103.18 Hz` beside `Pump Shaft Frequency 51.59 Hz`. Grab "the frequency" and every affinity normalization doubles: flow scales linearly and head quadratically, so a 2× frequency error is a 2× flow error and a **4× head error** — large enough to look like a different pump, not like a bug.
> **Depth is referenced to the pump DISCHARGE, not the intake** — `Setting Depth from pump discharge 9851.95 ft` against a string running to ~10,046 ft. Every other family references intake. Loaded as intake it understates the hydrostatic column by roughly the pump length: a couple of hundred feet on a taper, small enough to pass review and large enough to bias `C-HYD`.

> [!warning] **The p7 `Sensitivity` table is not a set of alternative designs.** It varies operating conditions on one string. Its rows carry `sensitivity_case = "true"` and **never produce a `pump_config` row**. Without that flag D17 fires spuriously on `Setting Depth` in every F5 document, and the per-case curve rows get fitted as real operating points.

> [!info] **`f5-baker.md` was corrected on 2026-08-20** before this prompt was written. The inlined contract carries its original text with correction subsections under §5, §6 and §8 — where they disagree, the corrections win, and this prompt already reflects them.

````
# Context
You are extracting structured ESP design data from ONE Baker Hughes design document, under
extraction contract F5 (Baker ProLift). This is one of 2 documents in this family.
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
- Page text (primary): prep/text/f5-baker/{{source_stem}}/p*.txt — one file per page
- Sliced PDF (fallback, ambiguous tables): prep/sliced/f5-baker/{{source_stem}}.pdf
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
   `depth_reference_basis`, `motor_type`.
   **Every F5 observation row is `curve_observation_role = 'design'`.**

4. **Omit a key entirely when the document does not print that value.** Do not emit
   `"key": null`, `""`, `"-"` or `"n/a"`. An absent key means "not printed", which is a fact, and
   it is recorded separately under *Fields not found*. **The contract's T5 says the bubble-point
   fields are "all null" for F5** — that is CSV-era wording. Omit those keys.

5. **Every value is a JSON string, verbatim as printed, including its unit token.**
   `"motor_frequency_hz": "103.18 Hz"` · `"stages": "738"` · `"bottom_md_ft": "10046.2 ft"`.
   Commas inside a value are ordinary characters. Do not coerce to number, do not strip
   separators or units.

6. **`organization_id`, `well_id` and `effective_from` are populated at load** — normally absent
   from the data blocks. **`effective_from` comes from the p1–p8 footer `Date`** (`F5-02`), which
   this family prints; put it in the frontmatter as `YYYY-MM-DD`. If a document prints no date,
   leave it EMPTY and say so — never substitute the extraction date, because D18 selects the
   latest design by this field and a placeholder silently wins against real ones. The preparer's
   name in the same footer is not proposed.

7. **A grain with no data emits an empty fenced block** — the fence with nothing between it.

# Step 0 — confirm the family
Page 1 must show ALL of:
- the document title `ProLift Summary Report`
- a model in the `ESP B …` grammar
- the blocks `Surface Electrical`, `Surface Production Data`, `Simulation Parameters`

If page 1 reads `Schematics Report` with an `SLB Engineer:` field, this is **F3**. If it reads
`SIZING REPORT` with a `Summit ESP Representative` footer, it is **F1**. Either way STOP, name
what was expected and found, set validation_status: failed, and do NOT fall through to another
contract.

**The `ESPD_` filename prefix is NOT a family signal** — it spans two vendors, and the other five
`ESPD_` files are F3. Neither the producer string nor the filename decides the family; page 1
does.

# Step 1 — pages
**8 pages.** Missing p1, p2, p7 or p8 is a HALT.

  p1  `ProLift Summary Report` — the BASE CASE: electrical, production, pump(s),
      simulation parameters, GVF cascade, motor
  p2  `ProLift Detailed Report` — fluids, multiphase correlation, reservoir temperature,
      top of perforation
  p5  Motor page — `Power Factor %`
  p7  **`Sensitivity` case table** — no counterpart in any other family
  p8  **String diagram table** — `Description / PN / Q-ty / OD / Length / Mass / Bottom`

`Pump Performance` chart is PLOT-ONLY; read no numbers from it. Read every page and record the
plot pages as graphical-only in Extraction Notes rather than skipping them.

# Step 2 — THE 2× FREQUENCY TRAP
**This is a PERMANENT-MAGNET motor and it runs at twice the shaft frequency.** `PRIEST 233H`
prints `Motor Frequency 103.18 Hz` against `Pump Shaft Frequency 51.59 Hz`.

  `Motor Frequency`      → `motor_frequency_hz`.  **NEVER** to `frequency_hz_as_printed`,
                            **NEVER** to `design_frequency_hz`.
  `Pump Shaft Frequency` → `frequency_hz_as_printed` AND `design_frequency_hz`.
                            **This is the hydraulic frequency and the only one affinity
                            normalization may ever consume.**
  `Motor: Type`          → `motor_type = 'permanent_magnet'` where the printed type says so.
                            **REQUIRED, not optional** — it is what tells a reader the 2× is
                            physics rather than a typo.

An extractor that grabs "the frequency" doubles every affinity normalization in this family:
2× flow, **4× head**. Both values must survive so the validator can check their ratio.

# Step 3 — THE DISCHARGE-REFERENCED DEPTH
`Simulation Parameters: Setting Depth from pump discharge 9851.95 ft`, against a string running
to about 10,046 ft. **Every other family references the intake.**

- Emit the printed value into `intake_set_depth_md_ft` **unchanged**.
- Set `depth_reference_basis = 'pump_discharge'` — a **literal, hardcoded** value for this
  contract. Never inferred from the number, never defaulted to `intake`, never omitted.
- **Do NOT add the string length to convert it to an intake-referenced depth.** That is
  arithmetic, and the load layer has the p8 `Bottom` column to do it with.
- Do **not** use `pump_setting_md_ft` / `pump_setting_vd_ft` here. Those exist because another
  family prints two distinct depths; F5 prints one, and the schema's `depth_reference_basis` enum
  names `pump_discharge (F5)` by name.

# Step 4 — sections, and the second Track 1 trap
Two sources, both required:
- **p1 `Pump(s)`** — the summary form, `ESP B 400 1750 PK 738 STG`, string-level total stages.
- **p8 string diagram** — per-housing rows with `Q-ty`, `Length` and `Bottom` depth. This is
  where `housing_count`, `section_order` and `bottom_md_ft` come from.

**`Bottom` IS MEASURED DEPTH FROM THE WELLHEAD. Take `bottom_md_ft` from it directly.** It is NOT
a running tally down the assembly. Sanity band: the deepest pump body should sit near the
~10,046 ft string bottom. **If you have produced a number in the tens or hundreds of feet, you
read `Length`.** Go back and read `Bottom`.

`section_order` **1 = deepest**. Never a section row: intakes, seals, gas separators, sensors,
motor, cable, tubing.

## pump_config grain is `model_grouped`
Emit ONE ROW PER DISTINCT MODEL. Set `section_grain = 'model_grouped'`,
`stages_basis = 'group_total'`, `housing_count`, and `stages_per_housing_as_printed`.

**Emit BOTH printed stage numbers.** The p1 summary prints `738 STG`; the p8 diagram prints six
housings at `123 STG` each. `6 × 123 = 738`. Put `738` in `stages`, `6` in `housing_count`, and
`123` in `stages_per_housing_as_printed`. **Do NOT multiply, divide, or reconcile them** — the
validator checks the identity rather than trusting either number, and it can only do that if both
survive. If the identity fails, emit all three and set `extraction_status = 'conflict'`. Do not
pick the one that looks rounder.

Where housings are NOT uniform, emit every printed count in section order, comma-separated
(`"123, 123, 123, 123, 123, 42"`). Never a modal value.

**No `stg_pump_config` row is ever emitted from p7.**

# Step 5 — the model, printed two ways in one document
  p1 summary  `ESP B 400 1750 PK 738 STG`                          → `pump_model_as_printed`
  p8 diagram  `ESP B 4001750 CW 6.5M HSG 123 STG PK CT HSS XA3 …`  → `pump_model_as_printed_alt`

The series and model **run together** in the diagram form (`4001750`) and are **space-separated**
in the summary form (`400 1750`). **Same pump.** Emit BOTH strings verbatim — reconciling them is
the load layer's job, and the alias seed file carries both. Do not repair, split or normalize
either one.

# Step 6 — the p7 Sensitivity table: flag every row
The p7 cases vary `Water Cut`, `GLR`, `Static BHP`, `PI`, `Surface Rate` and **`Setting Depth`**
— operating conditions, **not the pump string**.

- Emit sensitivity rows to `stg_design_context`, `stg_curve_observations` and `stg_gas_cascade`
  with `scenario_label_staged = <case name>` and **`sensitivity_case = "true"`**.
- The **p1 summary is the design scenario**: `is_design_scenario = "true"`,
  `scenario_selection_rule = 'p1_summary_base_case'`, and no `sensitivity_case` flag on it.
- **NO `stg_pump_config` ROW FROM p7, EVER.** The string comes from p1 and p8 only.

Without the flag, `Setting Depth` varying per case reads as the equipment changing between
scenarios and the load layer halts on every F5 document.

`PI` in the sensitivity table is **not extracted**. `Intake Pressure` in the same table is **not
extracted**. Both are named in the contract only because they are printed alongside things that
are.

# Step 7 — what this family does and does not contribute
**No bubble point anywhere (0 of 2).** Omit `bubble_point_psi`,
`bubble_point_unit_as_printed` and `bubble_point_provenance`, and list them under *Fields not
found*. **Do NOT derive a bubble point from `Static BHP`** in the sensitivity table — the
"bubble point ≈ static datum" pattern is another vendor's sizing-engineer assumption, observed
there, and importing it here is exactly the laundering the provenance rules exist to prevent.

**No BEP, no ROR, no per-stage head, no per-stage power.** Set `envelope_status = 'absent'` and
emit no head-curve observations. `Simulation Parameters: Total Dynamic Head` is a **string-level**
benchmark: `head_basis_as_printed = 'string_total'`, `observation_is_composite = "true"`.

**`H2S / CO2 / N2` compositional detail has no consumer and is not proposed. Do not extract it.**

# Step 7a — what is NOT a printed table
The `Pump Performance` chart is a PLOT. `K`-abbreviated numbers (`8.5K ft`, `1.09K bpd`), values
that align to gridlines, and any number whose only source is a plotted trace are NOT data. Record
the page as graphical-only in Extraction Notes and emit nothing from it.

This applies to plot-sourced values ONLY. It is not a general licence to omit rows you are unsure
of: for anything printed in a table, transcribe it and note the doubt in Extraction Notes.

# Requirements

## Further traps
- **`Discharge Pressure` GOES TO `stg_curve_observations`** → `discharge_pressure_psi_as_printed`,
  both the p1 `Pump Discharge Pressure` and the per-case one in the p7 table. Design context has
  no discharge column at all.
- **`GLR` IS NOT EXTRACTED.** It is printed in the p7 sensitivity row alongside things that are;
  producing GOR/GLR is a corpus-wide exclusion.
- **THE TWO MULTIPHASE CORRELATIONS GO IN ONE KEY.** F5 prints a vertical and a horizontal
  correlation. Emit what is printed as a **single verbatim string** into
  `vendor_multiphase_correlation`. `Swap angle` and `Tuning Factor` are **not extracted**.
- **`Top of Perforation`** → `perf_top_md_ft`. **`Reservoir temperature`** → `reservoir_temp_f`.
- **`System Power Consumption kW`** and the per-case `Power Cons. kW` → `vendor_motor_power_kw`.
  **`Operating Power` and `Motor Load` are NOT extracted** — vendor-derived duplicates, dropped
  2026-08-20. `Motor Efficiency` → `motor_efficiency_ratio` stays.
- **From the motor block, extract `Type`, `Nameplate Power`, `Nameplate Voltage`.** The motor
  `<model>` and `Nameplate at rpm` are **not extracted**. Motor amperage excluded.
- **From p8, extract `Description`, `Q-ty` and `Bottom`.** `PN`, `OD`, `Length` and `Mass` are
  **not extracted**.
- **`Surface Flow Rate` is NOT extracted.** `Average Mixture Flow Rate` is the downhole flow at
  the design point and the curve x-axis for this family; a surface rate is not a point on a pump
  curve, and `Water Cut` carries the split.
- **`Overall Pump Efficiency`** → `vendor_pump_efficiency_pct`; per-case `Overall Efficiency` →
  `vendor_total_system_efficiency_pct`.
- **`Free Allowed Gas` under its own name, never NPSHr.** PI and PIP excluded.

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
- Record `pages_present`, `n_scenarios`, `n_sections`, and `vendor_family` on every row that has
  the key.

# Definition of done
- Frontmatter complete per §8, including `prompt_version: 1`; wikilinks present;
  `effective_from` from the footer `Date`.
- All 8 pages read, the plot page included and recorded as graphical-only.
- Five JSONL blocks, keys from the inlined schemas, every line independently parseable.
- **Every key checked by eye against the inlined schemas. No key invented by naming convention.**
- **`motor_frequency_hz` and `frequency_hz_as_printed` BOTH present and DIFFERENT**, roughly 2:1,
  with `motor_type = 'permanent_magnet'`.
- `depth_reference_basis = 'pump_discharge'` present, and the printed depth unconverted.
- Deepest `bottom_md_ft` near the string bottom — not a tens-of-feet number.
- `stages`, `housing_count` and `stages_per_housing_as_printed` all three present and unreconciled.
- `pump_model_as_printed` and `pump_model_as_printed_alt` both present, both verbatim.
- Every p7-sourced row carries `sensitivity_case = "true"`; **no `stg_pump_config` row from p7**.
- `envelope_status = 'absent'`; no head-curve observations; no bubble point.
- No `K`-abbreviated or plot-derived value anywhere.
- *Fields not found* and *Schema gaps* both complete and consistent with each other.
- The final `## row_counts` block is present and its numbers match the blocks above it.

# Do NOT
- Do NOT emit a key that is not in the inlined schemas.
- Do NOT infer a key name from a naming convention.
- Do NOT emit a value outside an enumerated column's listed set.
- **Do NOT write `Motor Frequency` into `frequency_hz_as_printed` or `design_frequency_hz`.**
- **Do NOT convert the discharge-referenced depth to an intake-referenced one.**
- **Do NOT read `bottom_md_ft` from the `Length` column.**
- **Do NOT emit a `stg_pump_config` row from the p7 sensitivity table.**
- **Do NOT omit `sensitivity_case` on a p7-sourced row.**
- Do NOT multiply, divide or reconcile the stage counts — emit all three numbers.
- Do NOT reassemble, split or normalize either model string.
- Do NOT derive a bubble point from `Static BHP` or anything else.
- Do NOT extract `GLR`, `PI`, `Intake Pressure`, `Swap angle`, `Tuning Factor`,
  `Surface Flow Rate`, `Operating Power`, `Motor Load`, motor `<model>`, `Nameplate at rpm`,
  `PN`, `OD`, `Length`, `Mass`, or `H2S / CO2 / N2`.
- Do NOT emit `bubble_point_absent_confirmed`, `power_factor_absent_confirmed`,
  `library_row_exists`, or `pump_shaft_frequency_hz`. None is a column.
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
{"section_order": "1", "section_role": "primary", "pump_model_as_printed": "ESP B 400 1750 PK 738 STG", "pump_model_as_printed_alt": "ESP B 4001750 CW 6.5M HSG 123 STG PK CT HSS XA3 MTSCa HT", "manufacturer": "Baker Hughes", "vendor_family": "baker_prolift", "section_grain": "model_grouped", "housing_count": "6", "stages": "738", "stages_per_housing_as_printed": "123", "stages_basis": "group_total", "bottom_md_ft": "10046.2 ft", "source_document": "{{source_document}}", "source_page": "p1, p8", "scenario_ordinal": "1", "is_design_scenario": "true", "scenario_selection_rule": "p1_summary_base_case", "extraction_status": "extracted", "provenance": "vendor input"}
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
| 2026-08-20 | v1 created. Ported from `f4-pass-a.md` v1 (carrying `f2-pass-a.md` v2's schema-union rule, invented-key ban, enum closure, `effective_from` rule and non-uniform-housing rule). F5-specific and written fresh: the `ProLift Summary Report` signature with F1/F3 HALTs and the explicit note that the `ESPD_` prefix is not a family signal; **Step 2's 2× permanent-magnet frequency trap** with the 4×-head consequence stated; **Step 3's discharge-referenced depth**, hardcoded basis, no conversion; Step 4's `Bottom`-not-`Length` trap with a sanity band and the three-number stage identity; Step 5's two model grammars for one pump; **Step 6's sensitivity-case flagging**, the rule that stops D17 firing spuriously, with no `pump_config` row ever from p7. Nine items named explicitly in Do NOT as not-extracted, since five of the thirty rows are sub-field narrowings rather than whole-row drops and a bare field list would not convey that. |
