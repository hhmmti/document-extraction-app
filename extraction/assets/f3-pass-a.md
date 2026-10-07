---
title: "F3 SLB — Pass A (extraction) prompt"
created: 2026-08-20
updated: 2026-08-20
milestone: M4
family: F3
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
  - "[[f3-slb]]"
  - "[[staging-schemas]]"
---

# F3 SLB — Pass A (extraction)

> [!info] **v1, 2026-08-20.** Ported from `f2-pass-a.md` **v2**, not from F1 v7 — v2 is where the schema-union rule, the enum-closure rule, the `effective_from` rule and the non-uniform-housing rule live, each of them written against a measured failure. Carried over verbatim: the JSONL output rules, the invented-key ban, the `K`/chart-axis ban, schema-gap reporting, the `## row_counts` gate.

> [!warning] **F3 is the first multi-scenario family, and the only one of F2–F4 where D16 and D17 can fire.** F2's five documents each produced exactly one `stg_design_context` row and the collapse rules were formalities. Here the scenario grain is real: `Case Comparison Report` prints `Initial / Future / Max` columns, per-section blocks repeat per scenario, and `Initial` is the design case. Where that report is absent the document is single-scenario. **A silent collapse is the failure mode this family invites** — every scenario is emitted, none is chosen, none is merged.

> [!info] **`f3-slb.md` was corrected on 2026-08-20** before this prompt was written, so unlike F2 there is no precedence block: the inlined contract and this prompt agree. If you re-issue the contract, check §7 and §8's row corrections still stand.

````
# Context
You are extracting structured ESP design data from ONE SLB / REDA design document, under
extraction contract F3 (SLB ESPdesign). This is one of 5 documents in this family.
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
- Page text (primary): prep/text/f3-slb/{{source_stem}}/p*.txt — one file per page
- Sliced PDF (fallback, ambiguous tables): prep/sliced/f3-slb/{{source_stem}}.pdf
- Page manifest rows, contract, report contract, BASE LOAD SCHEMAS, STAGING COLUMNS, and the
  closed 39-field list — all inlined below
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
   **Every F3 observation row is `curve_observation_role = 'design'`.** `cross_check` and
   `contribution` are NOT enum values — an older version of the contract said otherwise and was
   corrected.

4. **Omit a key entirely when the document does not print that value.** Do not emit
   `"key": null`, `""`, `"-"` or `"n/a"`. An absent key means "not printed", which is a fact, and
   it is recorded separately under *Fields not found*.

5. **Every value is a JSON string, verbatim as printed, including its unit token.**
   `"bubble_point_psi": "19985.3 psig"` · `"speed_rpm_as_printed": "3475 RPM"`. Commas inside a
   value are ordinary characters. Do not coerce to number, do not strip separators or units, do
   not reformat.

6. **`organization_id` and `well_id` are populated at load** — normally absent from the data
   blocks. **`effective_from` comes from the p1 `Date` field** (`F3-01`), which this family does
   print. Put it in the frontmatter as `YYYY-MM-DD`. If a document prints no date anywhere, leave
   it EMPTY and say so in Extraction Notes — never substitute the extraction date, because D18
   selects the latest design by this field and a placeholder silently wins against real ones.

7. **A grain with no data emits an empty fenced block** — the fence with nothing between it.

# Step 0 — confirm the family
Page 1 must show ALL of:
- the document title `Schematics Report`
- an `SLB Engineer:` field
- an `SLB | … Report` footer

If page 1 reads `ProLift Summary Report` with an `ESP B …` model, this is **F5 Baker**. STOP,
name what was expected and found, set validation_status: failed, and do NOT fall through to
another contract. Both families carry the `ESPD_` filename prefix; that prefix spans two vendors
and is not a family signal.

# Step 0a — one document where the filename lies
If `{{source_document}}` is `PERMIA~1.PDF`, it is a NORMAL F3 document with an 8.3 short
filename. Page 1 reads `Company: Permian Resources`, `Project: Mid-States East Unit 37-5 6D`.
**Take the well name from the document body, never from the filename**, and set
`well_name_source: document_body` in the frontmatter. The `6D` suffix is real. An earlier triage
pass read the filename and produced `MID STATES EAST UNIT 37 5 6D`, which joins to nothing.

For every other document, `well_name_source` is `document_body` or `document_header` as printed.

# Step 1 — map the pages before reading any value
Page count VARIES in this family (11–14 pages). Read the manifest rows first, then read every
page.

**Required spine:**
  p1     `Schematics Report` — the `Item / Description / Length / Top Depth / Bottom Depth`
         table. THIS IS THE SECTION SOURCE.
  p4-7   `General Report` — `Input Data`, `Equipment and Results`,
         `Conditions at Operating Frequency`

**Optional, and its absence is normal:**
  p2-3   `Case Comparison Report` — `Initial / Future / Max` scenario columns.
         Present on `Midway 2H` (14 pages), ABSENT on `BRAVE STATE 132H` (11 pages), which runs
         `Schematics → General Report` directly. Where absent, the document is SINGLE-SCENARIO
         and the fields that would come from p2-3 come from the `General Report` instead. Record
         the absence in Extraction Notes. Do not treat it as a missing page or a HALT.

**Seven chart-only pages:** `Inflow Performance`, `Inflow/Outflow`, `TDH Curve`, `VSD H-Q Curve`,
`Actual Bottom Pump Curve`, `Actual Top Pump Curve`, `Catalog Top Pump Curve`. Read no numbers
off any of them.

`Catalog Top Pump Curve` is the ONLY catalog-basis curve SLB prints and this family's strongest
D7 digitization candidate. Set `digitization_candidate_page = 'Catalog Top Pump Curve'` on the
observation rows and move on.

# Step 2 — scenarios: emit every one, choose none, merge none
THIS IS THE FIRST MULTI-SCENARIO FAMILY. Get this wrong and the rest is worthless.

Where `Case Comparison Report` is present it prints `Initial`, `Future` and `Max` as COLUMNS.
Each column is a scenario. Per-section blocks repeat per scenario, so the true grain of the
per-section rows is **section × scenario**.

- Emit one `stg_design_context` object PER SCENARIO. Not one per document.
- Emit one `stg_pump_config` object per model PER SCENARIO.
- `scenario_label_staged` = the column heading verbatim (`Initial`, `Future`, `Max`).
- `scenario_ordinal` = order of appearance, 1-based.
- `is_design_scenario = "true"` on **`Initial` only**, with
  `scenario_selection_rule = 'named_case_initial'`. `Initial` is the as-installed design case;
  `Future` and `Max` are forward look-aheads at conditions that do not exist yet.
- Set `n_scenarios` in the frontmatter to the number emitted.

**DO NOT collapse, average, or pick.** If a value is identical across all three columns, emit it
three times anyway — sameness is a fact the load layer checks (D16), not a reason to deduplicate.
If a value that should be invariant DIFFERS across scenarios, emit both and set
`extraction_status = 'conflict'`; do not decide which is right.

Where `Case Comparison Report` is ABSENT: single scenario. Omit `scenario_label_staged`, set
`"scenario_ordinal": "1"` and `"is_design_scenario": "true"`, and note the absence.

# Step 3 — sections from the printed depths, and the Track 1 trap
Sections come from the **p1 `Item / Description / Length / Top Depth / Bottom Depth` table**.

- Rows where `Item = Pump` are sections.
- `AGH` and `MGH` rows are **GAS HANDLERS, WHICH ARE PUMP BODIES AND ARE IN SCOPE**.
  `section_role = 'gas_handler'`.
- NEVER a section row: `Casing`, `Producing Perforations`, `Tubing`, gas SEPARATORS, seals,
  sensors, motor. They are depth-provenance and gas-cascade context only and get no
  `stg_pump_config` row.

`section_order` **1 = DEEPEST**. SLB names its pumps `Bottom` and `Top` explicitly, which makes
this the easy family — but take the order from the `Top Depth` / `Bottom Depth` COLUMNS and
confirm the naming agrees. Where the label and the depths disagree, emit both readings and set
`extraction_status = 'conflict'`. Do not choose.

**`bottom_md_ft` IS MEASURED DEPTH FROM THE WELLHEAD**, read from the table's own `Bottom Depth`
column. It is NOT a running tally down the assembly. Sanity band: the deepest pump body's
`bottom_md_ft` should sit within a few hundred feet of `Intake Depth` — roughly 9,600 ft on these
wells. **If you have produced a number in the tens or hundreds of feet, you read the `Length`
column.** Go back and read `Bottom Depth`. This is the exact error that broke an earlier project.

Cross-read the models against p6 `Equipment and Results → Bottom / Top Pump Information` and,
where present, p2 `Case Comparison → Pump (Bottom) / Pump (Top) Information`. All must name the
same models; where they disagree, record both and set `extraction_status = 'conflict'`.

# Step 3a — pump_config grain is `model_grouped`
Emit ONE ROW PER DISTINCT MODEL PER SCENARIO. Set `section_grain = 'model_grouped'`,
`stages_basis = 'group_total'`, and `housing_count`.

`stages_per_housing_as_printed` carries the per-housing counts. **Where housings are not uniform,
emit EVERY printed count in section order, comma-separated** (`"73, 73, 42"`). Do not pick a
dominant value: another family printed `42 + 93×4 = 414` and a modal `93` lost a housing and made
`housing_count × stages_per_housing` disagree with `stages` for a reason that was not a real
disagreement. Emit all counts as printed; do not multiply, divide, or reconcile.

# Step 4 — model grammar
```
^REDA\s+\d{3}\s+[A-Z]{1,3}\d{3,4}$
```
Observed: `REDA 400 RC1000`, `REDA 400 DN1750`. Manufacturer, series and model are three
space-separated parts of ONE printed string. **Capture the whole string verbatim** into
`pump_model_as_printed`. Do not split it into manufacturer / series / model fields.

**`Staging Configuration` (`CR-CT` vs `C-CT`) IS NOT PART OF THE MODEL IDENTITY.** It goes to
`staging_configuration_as_printed` on `stg_pump_config`, and `pump_model_as_printed` ignores it.
It describes how the same hydraulic model is assembled — compression versus floater staging and
its thrust path — not what curve the stage produces. Folding it into the model string would split
one model's observations into two under-populated fits. Emit the printed string to
`stg_alias_evidence` as well.

# Step 5 — what this family does and does not contribute
**NO CURVE NUMBERS ANYWHERE. 0 of 5 on BEP, 0 of 5 on ROR.** Seven pages are chart-only. Set
`envelope_status = 'absent'` on the observation rows. **Emit NO head observations.**

What F3 DOES contribute as observations, and it is a short list:
- per-section `Required Power` → `power_as_printed`, `power_basis_as_printed = 'per_section_total'`
- per-section `Pump Efficiency` → `efficiency_pct`
- per-section `Operation Speed` → `speed_rpm_as_printed`, IN RPM, UNCONVERTED (see traps)
- `Number of Stages` → `stages_at_observation`
- flows: `Design Rate` / `Operation Rate`, `Total Rate at Inlet` / `Liquid Rate at Inlet` /
  `Gas Rate into Pump`, `Production Rate`
- `Discharge Pressure` → `discharge_pressure_psi_as_printed`
- `Total Dynamic Head` → `head_as_printed`, `head_basis_as_printed = 'string_total'`

Everything else in the 39 is design context, pump config, gas cascade or alias evidence.

# Step 5a — what is NOT a printed table
The seven chart pages are PLOT PAGES. `K`-abbreviated numbers (`8.5K ft`, `1.09K bpd`), values
that align to gridlines, and any number whose only source is a plotted trace are NOT data. If a
number's source is a plot, do not emit that object; record the page as graphical-only in
Extraction Notes. A printed table adjacent to a plot IS a table and must be transcribed — the
test is whether the number appears as text in a table cell, not whether it appears near a chart.

This applies to plot-sourced values ONLY. It is not a general licence to omit rows you are unsure
of: for anything printed in a table, transcribe it and note the doubt in Extraction Notes.

# Requirements

## The traps — these are the reason this contract is family-specific
- **THE BUBBLE POINT CAN BE NONSENSE, AND YOU EMIT IT ANYWAY.** `Midway 45-46 Unit 2H` prints
  `Bubble Point 19985.3 psig` against `GOR 12485.71 SCF/STB`; `PERMIA~1` prints a plausible
  `2130 psig`. SLB computes it, so it is `vendor derived` for this family. **Extract the printed
  value verbatim. Do not correct it, do not omit it, do not flag it as implausible.** The range
  check is the validator's job (`V-12`), not yours, and a plausibility flag is validator output —
  do not emit one.
- **BUBBLE-POINT UNIT IS `psig` HERE.** Not `psi`, not `PSIA`. `bubble_point_unit_as_printed` is
  required wherever a bubble point is emitted. `psig` versus `PSIA` is a 14.7 psi offset and the
  downstream bands are ±10 %, so on a low-pressure well that offset sits inside the decision
  margin. Record the token; convert nowhere.
- **`Mixture Gradient (psi/ft)` IS A BENCHMARK AND MUST NEVER BECOME AN INPUT.** Emit to
  `vendor_mixture_gradient_psi_per_ft`. `Midway 2H` prints `0.433 psi/ft` while its own
  `Water Cut 93 %` and `Water Spec. Gravity 1.1` imply about `0.464`; `PERMIA~1` prints `0.407`,
  so it is not a constant. **Never back an SG out of it** and never treat it as the hydrostatic
  input. The divergence is a finding for a later milestone, not something to reconcile here.
- **NO POWER FACTOR ANYWHERE IN THIS FAMILY, 0 of 5. That is a finding, not a gap.** Do not go
  looking for one. Do NOT substitute `Load Factor` or `Slip` — both are motor detail with no
  contract behind them. Omit the key and list the field under *Fields not found*.
- **`Comments / Comments-Purpose` IS DATA, NOT DECORATION.** Capture the text verbatim into
  `vendor_comments_as_printed`. It carries vendor overrides — *"separation efficiency lowered to
  60 %"* — and is the ONLY place this family records that a printed value was overridden by the
  engineer. That is a provenance signal, and prose is not a destination.
- **`Operation Speed` IS RPM AND STAYS RPM.** Emit into `speed_rpm_as_printed` with its unit
  token. **Do NOT convert it to Hz** and do not use it for frequency normalization where
  `Operating Frequency` is printed. It is a cross-check on the printed frequency, not a second
  frequency.
- **MOTOR AMPERAGE IS EXCLUDED.** The motor nameplate row names `Volts / Power / Speed /
  Rating Factor / Winding Number` — extract those; omit `Amp`. Likewise the
  `Conditions at Operating Frequency` block: `Total Motor Load` is proposed; `Motor Amp` and
  `KVA @ Junction Box` are not.
- **`Discharge Pressure` GOES TO `stg_curve_observations`**, not to design context, both on the
  Case Comparison page and in `Pumping Conditions` on p6. With intake pressure it brackets the
  pump's ΔP, which is an observation-grain benchmark.
- **GAS SEPARATORS ARE NOT PUMP BODIES.** Their efficiencies —
  `Natural Separation Efficiency`, `Separator 1 / 2 Efficiency`, `Total Separation Efficiency` —
  go to `stg_gas_cascade` with `component_is_pump_body = "false"`, one row per cascade stage.
  `gas_rate_as_printed` there is the FREE GAS volume only; if the block prints a percentage but
  no separate gas volume, emit `gas_pct_as_printed` and omit the rate.
- **`Free Allowed Gas` under its own name, never NPSHr.** PI and PIP excluded. Producing GOR/GLR
  excluded — but the `GOR` printed beside the bubble point is context for T1 and is recorded in
  Extraction Notes, not extracted.

## Rules for every field
- Values EXACTLY as printed, with unit tokens.
- A proposed field not printed on any page: omit the key AND list the field under
  *Fields not found*.
- **A proposed field that IS printed but has no destination key in the schemas: list it under
  *Schema gaps*** with its contract-stated target. Never write it into prose instead, and never
  invent a key for it.
- **A field that lands in neither block has not landed.** *Fields not found* may read "None" ONLY
  if *Schema gaps* also reads "None". If either lists entries, say plainly in Extraction Notes how
  many of the 39 were emitted and how many were not.
- Never guess, interpolate, or fill from a sibling document. Absent beats plausible.
- Nothing outside the 39-field list. Note candidates in Extraction Notes instead.
- Where two printed values disagree, record BOTH and set `extraction_status = 'conflict'`.
- Record `pages_present`, `n_scenarios`, `n_sections`, and `vendor_family` on every row that has
  the key.

# Definition of done
- Frontmatter complete per §8, including `prompt_version: 1`; wikilinks present;
  `effective_from` from the p1 `Date`; `well_name_source` set, and `document_body` on `PERMIA~1`.
- Every page read, including the seven chart pages, none skipped as "just a plot".
- Five JSONL blocks, keys from the inlined schemas, every line independently parseable.
- **Every key checked by eye against the inlined schemas. No key invented by naming convention.**
- Every enumerated column carries one of its listed values; every observation row is
  `curve_observation_role = 'design'`.
- **One `stg_design_context` row per scenario**, `Initial` carrying `is_design_scenario = "true"`
  and `scenario_selection_rule = 'named_case_initial'` — or a single row where
  `Case Comparison Report` is absent, with the absence noted.
- `stg_pump_config` at `model_grouped` grain, per scenario, with `housing_count`,
  `stages_per_housing_as_printed` (every count where housings differ), `stages_basis`, and
  `staging_configuration_as_printed` where printed.
- Deepest `bottom_md_ft` within a few hundred feet of `Intake Depth` — not a tens-of-feet number.
- `envelope_status = 'absent'`; no head observations emitted; `digitization_candidate_page` set.
- Bubble point emitted verbatim with its `psig` token, however implausible.
- `speed_rpm_as_printed` in RPM, unconverted.
- No `K`-abbreviated or plot-derived value anywhere.
- *Fields not found* and *Schema gaps* both complete and consistent with each other.
- *Extraction notes* records: whether `Case Comparison Report` was present, every chart page
  treated as graphical-only, and any conflict.
- The final `## row_counts` block is present and its numbers match the blocks above it.

# Do NOT
- Do NOT emit a key that is not in the inlined schemas.
- Do NOT infer a key name from a naming convention. `_as_printed` is part of particular column
  names, not a suffix you may add to others.
- Do NOT emit a value outside an enumerated column's listed set.
- Do NOT emit `curve_observation_role = 'cross_check'` or `'contribution'`. Not enum values.
- Do NOT emit `library_row_exists`. It is a join computed at load.
- Do NOT emit `bubble_point_plausible`, `power_factor_absent_confirmed`, or
  `case_comparison_present`. None is a column; the first is validator output and the other two
  are recorded in *Fields not found* and *Extraction notes*.
- Do NOT emit `null`, `""` or `"-"` — omit the key instead.
- Do NOT put the extraction date into `effective_from`.
- **Do NOT collapse, average, deduplicate or choose between scenarios.**
- **Do NOT read `bottom_md_ft` from the `Length` column.**
- Do NOT correct, omit or flag the bubble point, however implausible it looks.
- Do NOT convert `Operation Speed` from RPM to Hz.
- Do NOT feed `Mixture Gradient` to a hydrostatic calculation or back an SG out of it.
- Do NOT substitute `Load Factor` or `Slip` for a power factor.
- Do NOT fold `Staging Configuration` into the model string.
- Do NOT split the `REDA 400 RC1000` string into separate manufacturer / series / model fields.
- Do NOT write a total fluid volume into `gas_rate_as_printed`.
- Do NOT emit a `stg_pump_config` row for a casing, tubing, perforation, separator, seal, sensor
  or motor row.
- Do NOT coerce a value to a number or strip its unit token.
- Do NOT transcribe a chart axis label or any `K`-abbreviated number.
- Do NOT summarize a table in prose instead of transcribing its objects.
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
{"section_order": "1", "section_role": "primary", "pump_model_as_printed": "REDA 400 RC1000", "staging_configuration_as_printed": "CR-CT", "manufacturer": "SLB", "vendor_family": "slb", "section_grain": "model_grouped", "housing_count": "3", "stages": "219", "stages_per_housing_as_printed": "73, 73, 73", "stages_basis": "group_total", "bottom_md_ft": "9612.4 ft", "top_md_ft": "9498.1 ft", "source_document": "{{source_document}}", "source_page": "p1", "scenario_label_staged": "Initial", "scenario_ordinal": "1", "is_design_scenario": "true", "extraction_status": "extracted", "provenance": "vendor input"}
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
| 2026-08-20 | v1 created. Ported from `f2-pass-a.md` v2 — carries its schema-union rule, invented-key ban, enum-closure rule, `effective_from` rule and non-uniform-housing rule, each written against a measured F2 failure. F3-specific and written fresh: the `Schematics Report` signature with the F5 HALT, the variable page count and optional `Case Comparison Report`, **the multi-scenario emission rules (first family where D16/D17 can fire)**, the `^REDA\s+\d{3}\s+[A-Z]{1,3}\d{3,4}$` grammar with `Staging Configuration` held out of identity, the Track 1 `Bottom Depth` vs `Length` trap with its sanity band, T1's implausible bubble point emitted verbatim, T2's `psig`, T3's gradient-as-benchmark, T6's vendor comments, T7's unconverted RPM. `PERMIA~1.PDF`'s body-sourced well name handled in Step 0a, since the batch script has no per-document branch. No precedence block needed — `f3-slb.md` was corrected the same day and the two agree. |
