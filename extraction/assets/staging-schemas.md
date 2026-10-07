---
title: "Staging schemas — stg_* tables"
created: 2026-08-18
status: draft — awaiting Hamed's confirmation
milestone: M2/M5
tags:
  - schema
  - staging
  - tapered_pumps
  - extraction
related:
  - "[[m2-extraction-method]]"
  - "[[m2-validator-spec]]"
  - "[[m1-extraction-spec]]"
  - "[[design-doc-extraction-plan]]"
---

# Staging schemas — `stg_*` tables

> [!note] **Why this document exists.** M1 defined the **load** schemas — `pump_config`, `esp_well_design_context`, `curve_observations`. Everything downstream then told extractors to "use the staging schemas", inlined the load schemas, and left `stg_gas_cascade` and `stg_alias_evidence` undefined entirely. Extractors invented what they needed. Mostly they invented sensibly — but a key that exists in no schema is unloadable, and a field with no key gets written into prose where nothing can find it.
> **Staging is deliberately wider than load.** That is ordinary ETL, not a defect: scenario identity has to survive until the D17 collapse, and as-printed values have to survive until the transform. This document is the contract for that width.
> **Closes:** CF-16 (`stg_gas_cascade` had no destination), CF-23 (five F1 fields with no column), CF-26 (composites and well names with nowhere to go).

## Rules

1. **Every key an extractor emits must appear below.** A key not listed is `V-10`.
2. **Absent means not printed.** Omit the key; never `null`, `""`, `"-"`.
3. **Every value is a verbatim string** with its unit token attached (D8). Typing happens at load.
4. **Disposition** — what M5 does with each staging-only column: `carry` into load · `collapse` per D17 · `resolve` into another column · `drop` after use.
5. `organization_id`, `well_id`, `effective_from` are **populated at load**, not by extraction.

---

## `stg_pump_config`

Load schema (`pump_config`, M1) plus the staging columns below.

| Staging-only column | Purpose | Disposition |
|---|---|---|
| `scenario_label_staged` | scenario title verbatim | collapse (D17) |
| `scenario_ordinal` | order of appearance, 1-based | collapse (D17) |
| `is_design_scenario` | which scenario is the design case | drop after the collapse selects |
| `printed_order_direction` | verbatim note of how the document ordered sections | drop; evidence for `section_order` |
| `section_grain` | `per_section` \| `model_grouped` — see CF-28 | carry |
| `model_normalization_flag` | `composite` \| `not_a_model` \| unset — set by `normalize-models.py` | resolve: routes the row to `stg_alias_evidence` or rejects it |
| `pump_model_as_printed_alt` | second verbatim model string where a document prints two (F1 `Sizing` token, F2 `MSC_` string, F5 diagram form, F6 p1-vs-p7 cross-read) | carry — D31 resolves |
| `pump_series_as_printed` | series printed as its own labelled field (F4, F7); not recoverable from the model token | carry |
| `staging_configuration_as_printed` | F3 `CR-CT` vs `C-CT` — compression vs floater staging on one hydraulic model; deliberately out of model identity | carry |
| `stages_per_housing_as_printed` | per-housing stage count where the document prints both it and a group total (F5 `6 × 123 = 738`, F7) — `V-09` compares them | carry |
| `stages_out` | F7 `<n> Stages Out`, a real deduction; the net count is M5 arithmetic (D8) | carry |
| `sensitivity_case` | **F5 only** — `"true"` on a p7 sensitivity row. Stops D17 firing spuriously on `Setting Depth`. Not a corpus-wide flag | drop after D17 |

**`stages_basis` enum extended** to `per_housing | total_reported | group_total`. `group_total` is required where `section_grain = model_grouped`: a row carrying `housing_count: 5, stages: 360` is reporting the group's total, and labelling that `per_housing` is false.

> **CF-28 — grain.** F1 produced both forms. Tier 1 is indifferent (`Σ nᵢ·hᵢ` gives the same composite for 5×72 as for 1×360), and Tier 2′ cannot use per-section grain anyway because the documents report α per model group, not per housing. **Recommendation: `model_grouped`**, with `housing_count` and `stages_basis = group_total` making the expansion recoverable. Per-section rows would carry five copies of one α — fabricated precision, not information.

---

## `stg_design_context`

Load schema (`esp_well_design_context`, M1) plus:

| Staging-only column | Purpose | Disposition |
|---|---|---|
| `scenario_label_staged` | scenario title verbatim | collapse (D17) |
| `scenario_ordinal` | order of appearance | collapse (D17) |
| `is_design_scenario` | selects the surviving row | drop after the collapse |
| `extraction_status` | `extracted` \| `validated` \| `conflict` | drop; conflicts halt the load |
| `surface_power_factor` | F2 p7 Surface Equipment PF — a different quantity from the motor PF and must never substitute for it (T1) | carry |
| `vendor_liquid_phase_sg` | F2 `Liquid Phase SG`; the `calc_mixture_sg` benchmark takes this, `sg_for_dp` takes `vendor_mixture_sg` | carry |
| `pump_setting_md_ft`, `pump_setting_vd_ft` | `Pump Setting MD` / `VD` — `vendor input`, distinct from the derived `Intake Set Depth` (F2 T5; F7 prints both MD and TVD) | carry |
| `vendor_comments_as_printed` | F3 `Comments / Comments-Purpose` — the only record that a printed value was overridden by the engineer (D14 signal) | carry |
| `scenario_selection_rule` | why a scenario was chosen as the design case; M5's D16 ladder needs the basis auditable | carry |
| `vendor_required_tdh_ft` | `Required TDH` where the document also prints a produced TDH (F2, F4) — K-TIER1's benchmark is the comparison | carry |
| `vendor_calculated_water_cut_frac` | F4 `Calculated Water cut` (derived) alongside the printed `Water Cut` (input) — D13 check, never an input | carry |
| `vendor_system_tdh_range_as_printed` | F6 p1 `System TDH Range`, the third of T6's three TDH grains; a printed range is one printed value | carry |
| `motor_frequency_hz` | **F5** motor-side frequency on a permanent-magnet motor running at 2× shaft Hz. Never `design_frequency_hz`; `V-F5-1` checks the ratio | carry |
| `lateral_depth_tvd_ft` | F6 prints intake depth in MD only; C-HYD's hydrostatic term needs TVD. Not `intake_set_depth_tvd_ft` — different point | carry |
| `oil_rate_as_printed`, `water_rate_as_printed` | **F6 only** — the family prints no water cut, so these are C-SG's only source. Every other family's raw rates are dropped (D22) | resolve into `water_cut_design_frac` |
| `gas_sg_mixture` | F7 `Spg Gas (Mix)`; `gas_sg` holds the hydrocarbon basis C-BUBBLE's `gamma_g` wants | carry |
| `sensitivity_case` | **F5 only** — see `stg_pump_config` | drop after D16 |

**Not in this table, decided:** raw Oil/Water rate split · operating Motor Volts · Motor Input HP · Operating HP at design frequency. All four are vendor-derived duplicates of values we compute or already hold (`f1-spyglass.md` §8, D13/D14).

---

## `stg_curve_observations`

Load schema (`curve_observations`, M1) plus:

| Staging-only column | Purpose | Disposition |
|---|---|---|
| `curve_observation_role` | which printed table the point came from: `head_curve` \| `bhp_curve` \| `single_pump_chart` \| `multiscenario` \| `tapered_composite` \| `design` | carry — M6 needs the provenance to weight a fit |
| `scenario_label_staged`, `scenario_ordinal`, `is_design_scenario` | scenario identity | collapse / drop |
| `extraction_status` | `extracted` \| `validated` \| `conflict` | drop |
| `discharge_pressure_psi_as_printed` | **Test A benchmark** — with intake pressure it brackets the pump's ΔP | carry |
| `section_intake_pressure_psi_as_printed` | F6/F7 section-boundary pressure — **never PIP** (D11, plan amendment 17) | carry |
| `free_gas_at_discharge_pct` | pairs with the existing `free_gas_at_inlet_pct` | carry |
| `free_allowed_gas_pct_as_printed` | gas-handling capability — **not NPSHr** (D12) | carry |
| `fluid_density_as_printed` | F1 Single Pump Charts prints it per section | carry |
| `lift_as_printed` | F1 per-section total; `head_as_printed` with `head_basis = per_section_total` | resolve into `head_as_printed` |
| `envelope_status` | `graphical_only` \| `numeric` \| `absent` — distinguishes a family whose envelope is plotted (F2) from one that has none (F4, F5). M6 fires D7 off this | carry |
| `digitization_candidate_page` | names the page a D7 digitization would target, e.g. `p3-4 Performance Curve` | carry |
| `speed_rpm_as_printed` | RPM emitted unconverted (F3 T7, F7 `(<n> RPM)`) — there is no other RPM slot, and converting is what D8 forbids | carry |
| `flow_reference_as_printed` | verbatim label of where a flow or density was measured, where `point_type` cannot express it (F6 `at Intake` vs `at Primary`, intake/discharge/surface densities) | carry |
| `viscosity_correction_factors_as_printed` | F7 `Avg Cq` / `Ch` / `Cbhp` as printed; `viscosity_corrected` is a judgment made from them and D8 keeps the inputs | carry |
| `sensitivity_case` | **F5 only** — a p7 case row, never fitted as a real operating point | drop |

> **Correction, recorded not rewritten.** On 2026-08-18 `f1-spyglass.md` §8 re-mapped `Discharge Pressure` to `stg_curve_observations` on the stated grounds that "the target column already exists". **It did not** — `curve_observations` has no such column. The re-mapping was right; the justification was wrong. The column is created here.

**`point_type` enum extended** to `ror_min | bep | ror_max | design_point | shutoff | curve_point | q_intake | q_discharge`. F1's Single Pump Charts print Q-INT and Q-DIS as separate values against one `flow_as_printed` column; two objects differing by `point_type` is the honest encoding. `curve_point` was already in use for multi-frequency rows.

---

## `stg_gas_cascade` — *(new; closes CF-16)*

Grain: **well × scenario × cascade stage**. Nineteen F2–F7 fields traced to `K-TIER2P` had no destination anywhere; this is it.

```sql
well_id                      varchar   -- populated at load
scenario_label_staged        varchar
scenario_ordinal             varchar
stage_order                  varchar   -- 1 = furthest upstream (below intake)
stage_label_as_printed       varchar   -- "Free Gas Below Intake", "Free Gas exiting Sep. #1", ...
component_as_printed         varchar   -- the equipment the stage refers to, where named
component_is_pump_body       varchar   -- "true" for taper/primary/gas-handler stages
gas_pct_as_printed           varchar
gas_rate_as_printed          varchar
gas_rate_unit_as_printed     varchar   -- bpd | mcf/d (F4 prints mcf/d — a unitless read is wrong by orders of magnitude)
separation_efficiency_pct    varchar
turpin_indicator_as_printed  varchar   -- F6 prints 0.26 / 0.07 at two stages
dunbar_indicator_as_printed  varchar   -- F7 prints Turpin AND Dunbar at the SAME stage (2026-08-20)
correlation_as_printed       varchar   -- Turpin | Dunbar | ...
sensitivity_case             varchar   -- F5 only: "true" on a p7 sensitivity-case row
vendor_family                varchar
source_document              varchar
source_page                  varchar
extraction_status            varchar
```

**Disposition at load — the point of the table.** Tier 2′ needs α at each *pump section's* inlet, and that is exactly the subset where `component_is_pump_body = true`; those rows **resolve** into `curve_observations.free_gas_at_inlet_pct` on the matching section. The separator and intake stages are **context, not calculator input** — they stay in staging as the audit record and are carried nowhere at load. *(An earlier draft of this paragraph proposed carrying them into a `gas_cascade_json` blob on the design-context row. Dropped: it would add a load column nothing reads. The staging files answer any later question about the full cascade — see* Open for confirmation *item 2.)*

So CF-16's answer is: **a staging table, resolved at load, not a new load table.** That is why extraction was never blocked by it.

---

## `stg_alias_evidence` — *(new)*

Grain: **printed string × occurrence**. Feeds D31's alias table.

```sql
model_as_printed             varchar   -- verbatim, exactly as the document printed it
proposed_canonical           varchar   -- normalize-models.py output; null where unresolved
vendor_family                varchar
evidence_kind                varchar   -- schematic_row | pumps_page | chart_title | sizing_field
                                       -- | composite_string_token | not_a_model
evidence_value               varchar   -- the surrounding context, where useful
model_normalization_flag     varchar   -- clean | repaired | composite | not_a_model | unresolved
source_document              varchar
source_page                  varchar
```

**`composite_string_token`** — a p1 Sizing string naming several pumps plus a motor (`SF3550-SF4300-HFGS-400hp 420MTR`). It is **not a model** and must never resolve to one. **`not_a_model`** — a well name or document title that landed in a model field. Both are recorded so the alias lookup can reject them by name rather than by silent join failure (CF-26).

---

## Open for confirmation

All three resolved 2026-08-18, Hamed's calls.

**1. `pump_config` grain — `model_grouped`. Locked (CF-28 resolved).**
One row per distinct model per scenario, with `housing_count` and `stages_basis = group_total` making the expansion recoverable. Rationale as above: Tier 1 is indifferent, Tier 2′ cannot use finer grain because documents report α per model group, and per-section rows would carry duplicated α — fabricated precision. **F2–F7 contracts must state this explicitly**; F1's output already contains both forms and normalizes to grouped at load.

**2. Separator cascade stages — retained in staging, not carried to load.**
`stg_gas_cascade` keeps every stage, because they are already extracted and the file is the audit record. Only rows with `component_is_pump_body = true` resolve into `curve_observations.free_gas_at_inlet_pct`. **No `gas_cascade_json` on the design-context row** — the separator and intake stages are not calculator input, and carrying them into load would add a column nothing reads. If a later question needs the full cascade, the staging files answer it.

**3. Staging is file-level, not physical tables.**
The `stg_*` schemas define the **shape of the JSONL in the report files**, not database tables. M5 reads the reports directly and transforms into the three load tables. No migration, no intermediate load step, and the reports stay the audit record under git. `stg_*` naming is retained because it names the shape and the disposition, which is what extractors and M5 both need.

## Log

| Date | Update |
|---|---|
| 2026-08-18 | Created. Load schemas from M1 as the base; staging columns evidenced by what F1's 30 reports actually emitted. Defines `stg_gas_cascade` and `stg_alias_evidence` for the first time. Corrects the `Discharge Pressure` re-mapping justification in `f1-spyglass.md` §8. |

| 2026-08-18 | **Confirmed and locked.** Grain = `model_grouped` (CF-28 resolved) · separator cascade stages retained in staging only, no `gas_cascade_json` at load · staging is file-level JSONL shape, not physical tables. Status moves from draft to agreed. |

---

### Columns added by the F2 audit — 2026-08-18

Auditing `f2-championx.md` against the tables above found seven fields the contract requires and the schemas could not hold. Five are structural and will recur; two are F2-specific. Added here rather than per-family.

#### `stg_design_context`

| Column | Why | Found in |
|---|---|---|
| `surface_power_factor` | **T1 is unimplementable without it.** ChampionX prints `Operating Power Factor 0.811` (p6, motor) and `Power Factor 0.77` (p7, surface equipment) — different quantities that "must never substitute for each other". `motor_power_factor` + `motor_power_factor_basis` is one slot on a well-grain row, so with one column the second value has nowhere to go and the trap fails silently. | F2 |
| `vendor_liquid_phase_sg` | **T2 requires both.** `Fluid Composite SG 0.8162` → `vendor_mixture_sg`; `Liquid Phase SG 1.0059` has no slot. They answer different questions: `sg_for_dp` takes composite, the `calc_mixture_sg` benchmark takes liquid-phase. Emitting one loses a named benchmark. | F2 |
| `pump_setting_md_ft`, `pump_setting_vd_ft` | **T5 distinguishes them from intake depth**, with different provenance — `Pump Setting MD/VD` is `vendor input`, `Intake Set Depth` is `vendor derived`. The single `intake_set_depth_*` pair plus a basis flag cannot hold both, and F2 prints both in the same document. | F2, likely F5 (discharge-referenced) |

#### `stg_curve_observations`

| Column | Why | Found in |
|---|---|---|
| `envelope_status` | `graphical_only` \| `numeric` \| `absent`. **T3's whole point.** F2 prints the envelope as a plot with reversed label strings and no numbers; the contract says emit no BEP/ROR rows and mark it. Without the column the family is indistinguishable from one that simply has no envelope — and M6 needs that distinction to fire D7. | F2, F3, F4, F5 |
| `digitization_candidate_page` | Names the page a D7 digitization would target (`p3-4 Performance Curve`). Amendment 13 raised D7's likelihood; this is the marker that makes it actionable rather than a re-hunt. | F2 |

#### `stg_pump_config`

| Column | Why | Found in |
|---|---|---|
| `pump_model_as_printed_alt` | **§4 requires two verbatim strings.** p1 prints `400UNB35H`; p2 prints `PUMP MSC_400UNB_35H_93 STG_…`. The contract forbids reassembling one into the other at extraction — that is D31's job — so both must survive. F1 has the same shape (`Sizing` token vs schematic string). | F2, F1 |

#### Not added, and why

**`library_row_exists`** — every contract's §7 sets it, but it is a **join result**, not an extracted value: M5 can compute it against the consolidated library at load. Extracting it would bake a point-in-time answer into the record and go stale the moment MC's library changes.

**Dropped as untraced** (D13 — no contract upgraded, no calculation benchmarked): `Startup Frequency` · motor `Model No.` · pump `PN` / `Length` / `Weight` · raw `BOPD` / `BWPD` / gas production split. The last matches the F1 precedent — `water_cut_design_frac` plus the design-point total already carries the information.

**Non-pump equipment rows** (`GAS SEPARATOR`, `PROTECTOR`, `MOTOR`, `SENSOR`, `CABLE`, `De-Sander`, `Tail Pipe`) — F2 §3 says these are "section context only, never a `pump_config` row", but names no destination. **Resolved: they are not extracted to any staging table.** Their ordering information is already implied by the pump-body rows' `section_order`, and the gas-separator hydraulics that do matter go to `stg_gas_cascade`.

#### Contract text needing a follow-up edit

`f2-championx.md` §9 still marks `stg_gas_cascade` as "⚠️ **no destination column yet — schema gap G1**". **G1 is closed** — the table is defined above and its pump-body rows resolve into `curve_observations.free_gas_at_inlet_pct`. The same stale marker is likely in F3–F7 §9.

---

### Columns added by the F3 audit — 2026-08-18

`f3-slb.md` needs four columns that do not exist. Three are unrecoverable if dropped at extraction.

#### `stg_pump_config`

| Column | Why |
|---|---|
| `staging_configuration_as_printed` | **§4 names this column explicitly and the schema has no such thing.** `Staging Configuration CR-CT` vs `C-CT` distinguishes compression from floater staging on the same hydraulic model. The contract deliberately keeps it *out* of model identity — folding it in would fragment the D31 lookup and split one model's observations into two under-populated fits — but keeps it because it changes the shaft and thrust rating, and losing it at extraction is unrecoverable. Both halves of that reasoning fail without the column. |

#### `stg_curve_observations`

| Column | Why |
|---|---|
| `speed_rpm_as_printed` | **T7:** `Operation Speed` is a cross-check on the printed frequency, emitted as RPM with its unit and explicitly *not* converted to Hz (D8). There is no RPM slot — only `frequency_hz_as_printed` — so the only way to store it today is the conversion the trap forbids. |

#### `stg_design_context`

| Column | Why |
|---|---|
| `vendor_comments_as_printed` | **T6:** `Comments / Comments-Purpose` carries vendor overrides — *"separation efficiency lowered to 60 %"* — and is **the only place this family records that a printed value was overridden by the engineer**. That is a D14 provenance signal for `C-SG` and `C-BUBBLE`, and prose is not a destination. |
| `scenario_selection_rule` | Records *why* a scenario was chosen as the design case, and it differs by family: F3 is `named_case_initial` (Initial is as-installed; Future and Max are look-aheads at conditions that do not exist yet). `is_design_scenario` carries the outcome but not the basis, and M5's D16 selection ladder needs the basis to be auditable. |

#### Considered and not added

**`bubble_point_plausible`** (T1) — this is **validator output**, not extraction output. `V-12` range-checks the printed value (`0 < Pb ≤ 10,000 psi`); the extractor's job is to emit `19985.3 psig` verbatim and not correct it. The flag belongs in the validator findings, and M5 can recompute it from the value at any time.

**`power_factor_absent_confirmed`** (T4) — already covered. A proposed field absent from the document goes in the report's ***Fields not found*** section, which is exactly the checked-and-absent record T4 wants. Adding a per-field boolean would duplicate it and invite one per absent field.

**`case_comparison_present`** (§2) — the report frontmatter's `pages_present` already carries it, and `scenario_ordinal` carries the consequence.

#### Inconsistency to resolve across contracts

**`Discharge Pressure` has two different targets.** F3-16 and F3-31 send it to `stg_design_context`; `f1-spyglass.md` §8, amended 2026-08-18, sends it to `stg_curve_observations`. The F1 reasoning holds and should win — intake and discharge pressure bracket the pump's ΔP, which is an observation-grain benchmark for Test A, not a well-grain property. **F3's §8 targets need correcting to match**, and F4–F7 should be checked for the same split.

#### Stale marker

`f3-slb.md` §9 and rows F3-14, F3-36 still mark `stg_gas_cascade` as "⚠️ **no destination column yet — schema gap G1**". **G1 is closed.** Same stale text as F2; expect it in F4–F7.

### Columns added by the F4 audit — 2026-08-20

`f4-els.md`, 28 fields. Three columns, four drops, and one contract error that would have emptied the family.

#### `stg_pump_config`

| Column | Why |
|---|---|
| `pump_series_as_printed` | **§4 names it and it does not exist** — the F2/F3 pattern again. F4 is the only family printing `Manufacture` / `Series` / `Model` as three separately labelled fields, and `ELS-1750` carries no series token, so `400` is unrecoverable from the model string. §4 forbids concatenating them at extraction (that is D31's job), which makes the column mandatory rather than convenient. |

#### `stg_design_context`

| Column | Why |
|---|---|
| `vendor_required_tdh_ft` | F4-23 `Pump TDH` and F4-26 `Required TDH` are **different quantities** and both targeted `vendor_tdh_at_design_ft`. K-TIER1's benchmark *is* the comparison of produced against required head; one column collapses the benchmark into a single number and destroys it. |
| `vendor_calculated_water_cut_frac` | F4-17 `Water Cut` (vendor input) and F4-25 `Calculated Water cut` (vendor derived) both targeted `water_cut_design_frac`. D13 — the derived value is a check, never an input. Structurally identical to F2's `vendor_liquid_phase_sg`. |

#### Contract text needing a follow-up edit

- **§7 and T5 say F4 "emits no curve rows at all". False, and load-bearing.** Eight §8 rows target `stg_curve_observations` — F4-05, 07, 08, 20, 23, 26, 27, 28. T5 means no *curve* points: no BEP, no ROR, no plotted head curve. The design-point observation is the only flow/head/power record this family produces, and an extractor reading §7 literally emits an empty block and loses the family.
- **F4-22's `stg_design_context` target is the unit error T2 exists to prevent.** `vendor_free_gas_into_pump_pct` is a **percent** column; F4 prints `8.43 (mcf/d)`. Route to `stg_gas_cascade` only, where `gas_rate_as_printed` + `gas_rate_unit_as_printed` carry it. Both confirmed present.
- **T4** sets `power_factor_absent_confirmed = true` — not a column (settled by the F3 audit). *Fields not found* is the record.
- **T7** sets `sg_basis = 'composite'` — no such column. The basis is carried by choosing `vendor_mixture_sg`.
- **F4-09**'s second target `stg_design_context` is unneeded. Only `Rot. Sep Efficiency` is proposed and `stg_gas_cascade.separation_efficiency_pct` holds it.
- **F4-06** — `Pump Setting Depth` prints no MD/VD qualifier. `pump_setting_md_ft` with `depth_reference_basis = 'pump_setting'`; the contract should say which rather than leave the extractor to choose.
- **`null` language** in §3 (`top_md_ft` / `bottom_md_ft`), §5 (`scenario_label_staged`) and T4 (`motor_power_factor`). Staging Rule 2 and the JSONL rules say *omit the key*. Written in the CSV era, when null was a cell. Expect it in F5–F7.
- **Stale G1** in §9 and inline on rows F4-09 and F4-22.
- **CF-28 grain** unstated; §9 still reads "section × scenario". F4 prints `Nr Stages` per Top/Bottom column with no housing count → `stages_basis = total_reported`.

#### Dropped as untraced (D22)

- **F4-20 `Total volume at Intake`** — vendor-derived duplicate of F4-08 `Intake Production Rate`; both targeted `flow_as_printed` on one design-point row. Hamed's call 2026-08-20, on the F1 precedent for derived duplicates.
- **F4-24 raw `BOPD` / `BWPD` / `Gas Production`** — the F2 audit dropped exactly this row shape.
- **F4-21 downhole `Oil Rate` / `Water Rate`** — the derived water-cut check it would support is captured directly by F4-25 now that `vendor_calculated_water_cut_frac` exists.
- **F4-18 sub-fields `Series` and `Shroud`** — no column, no trace. `Type` / `Hp` / `Volts` stay; `Amps` already excluded by D2. Matches the F2 audit's drop of motor `Model No.`
#### Checked, nothing to do

`envelope_status` (the F2 audit already anticipated F4) · `bubble_point_calculated_flag` / `_unit_as_printed` / `_provenance` · `motor_type` · `observation_is_composite` and `power_basis_as_printed = 'string_total'` for F4-27 · `pump_model_as_printed_alt` is **not** needed here — the p2 chart title is alias evidence (`evidence_kind = chart_title`), not a second model string · **F4-01 is not a gap**: `well_name` lives in the report frontmatter and `well_id` is pre-matched into the prompt.

**F4 never prints `Discharge Pressure`** — the cross-contract split does not occur in this family.
### Columns added by the F5 audit — 2026-08-20

`f5-baker.md`, 30 fields. Three columns — one of them the flag a mandatory trap depends on and which existed nowhere.

#### `stg_design_context`

| Column | Why |
|---|---|
| `motor_frequency_hz` | **T1 names it and it does not exist.** The table carries `design_frequency_hz` / `_min_hz` / `_max_hz` only, so `Motor Frequency 103.18 Hz` has no destination but the two columns T1 explicitly forbids. `V-F5-1` computes the motor-to-shaft ratio, so both values must survive. This is the permanent-magnet 2× error the trap exists to catch: 2× flow and **4× head**, large enough to read as a different pump rather than a bug. |

#### `sensitivity_case` — on `stg_design_context`, `stg_curve_observations` and `stg_gas_cascade`

**§5 and T4 both require this flag and it exists in no table.** T4 is the rule that stops D17 firing spuriously on `Setting Depth`, which varies per sensitivity case in every F5 document. Without the flag, p7's rows are indistinguishable from the p1 base case. Needed on all three tables, not just design context: F5-25 and F5-26 emit curve observations per case — which must never be fitted as real operating points — and F5-27 emits cascade rows per case.

**Scope: F5-specific.** Not generalized to a corpus-wide staging flag (Hamed, 2026-08-20). The three columns exist to serve T4 and the p7 sensitivity table; another family printing variation rows gets its own decision, not this one.

#### `stg_pump_config`

| Column | Why |
|---|---|
| `stages_per_housing_as_printed` | **T3 / `V-09` is unimplementable under the `model_grouped` grain.** The identity is `housing_count × stages_per_housing == total_reported` (6 × 123 = 738), and T3 says check it rather than trust either number — so both printed values must survive to be compared. One `stages` slot with a single-valued `stages_basis` holds 738 or 123, not both, and recovering 123 as 738/6 is the arithmetic D8 forbids and defeats the check. |

#### Contract text needing a follow-up edit

- **The `Discharge Pressure` split is here, twice.** F5-10 `Pump Discharge Pressure` and F5-26's per-case `Discharge Pressure` both target `stg_design_context`, which has no discharge column at all — so they were homeless as well as mis-targeted. Both go to `stg_curve_observations.discharge_pressure_psi_as_printed`, on F1's reasoning.
- **§4's `stages_basis` paragraph contradicts CF-28.** It reads: the summary prints `738 STG` (`total_reported`), the diagram six housings at `123 STG` each (`per_housing`). That is two bases for one model. Under `model_grouped` it is one row — `housing_count = 6`, `stages_basis = group_total` — with the per-housing figure in the new column. Rewrite alongside it.
- **`V-F5-1` names `pump_shaft_frequency_hz`** — no such column. The shaft value lands in `design_frequency_hz` and `frequency_hz_as_printed` per F5-11; the rule should say so.
- **T5's `bubble_point_absent_confirmed = true`** — third instance of this pattern after F3 T4 and F4 T4. Not a column; *Fields not found* is the record. T5 also states the three bubble-point fields "are all `null`" — under the JSONL rules those keys are omitted.
- **F5-24 proposes `GLR`**, which this contract's own *Not extracted* section lists as a corpus-wide exclusion. Handle it as F4-25 handles GOR: name it in the row, exclude it explicitly.
- **Stale G1** in §9 and inline on rows F5-13 and F5-27.
- **CF-28 grain** unstated; §9 still reads "section × scenario".

#### Dropped as untraced (D22)

- **F5-05 `Surface Flow Rate`** — the contract itself names F5-09 `Average Mixture Flow Rate` as the curve x-axis for this family. Surface rate is a production number, not a point on a pump curve, and `water_cut_design_frac` carries the split. Matches the raw-rate drops in F1, F2 and F4.
- **F5-14 motor `<model>` and `Nameplate at rpm`** — F2 audit precedent for motor `Model No.`; no contract consumes nameplate rpm, since T1 reads the 2× off `motor_type`.
- **F5-15 / F5-29 `Operating Power` and `Motor Load`** — F1 precedent, which dropped `Motor Input HP` and `Operating HP at design frequency` as vendor-derived duplicates. `Power Cons. kW` → `vendor_motor_power_kw` stays.
- **F5-22 `Swap angle` and `Tuning Factor`** — untraced. The weakest of the six: a *tuned* correlation is arguably a D13 qualifier on the printed TDH. Dropped, flagged to Hamed.
- **F5-26 `Flowing BHP`** — no column, and it sits in the same row as the D11-excluded intake pressure.
- **F5-30 `PN` / `OD` / `Length` / `Mass`** — the F2 audit dropped pump `PN` / `Length` / `Weight`. `Q-ty` and `Bottom` are the load-bearing columns and stay.
#### Checked, nothing to do — and one not to "fix" later

**Depth is correct as written. Do not reroute it.** M1's schema comment on `depth_reference_basis` reads `intake | pump_discharge (F5) | pump_setting` — the schema anticipated this family by name. T2's routing of the discharge-referenced value into `intake_set_depth_md_ft` with the basis flag set is the intended design. **The F2 pair `pump_setting_md_ft` / `pump_setting_vd_ft` does not apply here**: it exists because F2 prints two distinct depths with different provenance, and F5 prints one.

`motor_type = 'permanent_magnet'` · `pump_model_as_printed_alt` covers §4's two grammars · `vendor_free_gas_into_pump_pct` · `speed_rpm_as_printed` (F3 audit; F5-25 needs it) · `envelope_status` · `scenario_selection_rule` · `static_datum_pressure_psi` · `perf_top_md_ft` · `vendor_total_system_efficiency_pct` · `vendor_motor_power_kw` · `vendor_pump_efficiency_pct` · `motor_efficiency_ratio`.

### Columns added by the F6 audit — 2026-08-20

`f6-xsize.md`, 48 fields, the richest document in the corpus. Two columns, nine drops, and a trap that names three columns and a grain that do not exist.

#### `stg_design_context`

| Column | Why |
|---|---|
| `vendor_system_tdh_range_as_printed` | **T6 requires three TDH grains recorded separately and only two have homes.** Per-section `Total Dynamic Head` (F6-42) → `head_as_printed` with `head_basis_as_printed = 'per_section_total'`; per-production-point system TDH (F6-20) → `vendor_tdh_at_design_ft` on the scenario row; the p1 `System TDH Range` (F6-12) has nothing. T6 keeps the 9,271 vs 6,578 ft discrepancy open and checkable for Test A at M8 — that test can only run if all three are recorded, and one of them currently cannot be. Verbatim string; a printed range is one printed value (F1 precedent). |
| `oil_rate_as_printed`, `water_rate_as_printed` | **F6 prints no water cut anywhere in its 48 fields.** F6-19's `Oil Rate` / `Water Rate` are the only source C-SG has in this family, and D8 forbids the extractor computing the cut. This is the deliberate **exception** to the raw-rate drops in F1, F2, F4 and F5 — every one of those rested on the family printing `Water Cut` directly, and F6 does not. `Gas Rate` is dropped; the cascade carries free gas. |

#### Contract text needing a follow-up edit

- **T2 names three columns and a grain that do not exist.** It instructs the extractor to emit each per-section SG to `stg_design_context` as an "`sg_observation`" with `sg_basis`, `sg_scope` and `sg_section_ref`. None of those columns exist, and design context is well/scenario grain — there is no per-section observation row in it to hold them. **Resolved without new columns:** `fluid_sg_at_observation` already carries the section's SG on every observation row, which is what T2's first instruction says. Drop the design-context emission; M5's `sg_for_dp` selection rule reaches the `section_order = 1` value through the D31 model join. `sg_for_dp_basis = 'section_primary'` is an M5 load column, not a staging key, and T2 should say so.
- **§7's `curve_observation_role = 'cross_check'` and `source_of_truth` are not enum values.** The enum is `head_curve | bhp_curve | single_pump_chart | multiscenario | tapered_composite | design`. The point §7 is making — that a 30 %-worn F6 row can never outrank a library row — is an M5/M6 weighting concern, not a staging role. Reword rather than extend the enum.
- **T1's "record the legend string separately as corroboration"** has no destination key. *Extraction notes* is the destination; the tabulated `Assumed Pump Wear` value is the data, and `assumed_pump_wear_pct` holds it.
- **Stale G1** in §9, in T7's ⚠️ callout with its "Owner: Hamed, before M5", and inline on rows F6-24, F6-25, F6-30, F6-31, F6-32, F6-33. **Also outside the contracts** — `m2-validator-spec` §9 still tells M5 the cascade has no destination column and must not be loaded.
- **CF-28 grain** unstated; §9 still reads "section × scenario". F6's three sections carry three distinct models, so `model_grouped` and per-section coincide here — state it anyway.
- **T1's `V-05` "rejects a null `assumed_pump_wear_pct`"** — under JSONL the failure is an *absent key*. Covered by the blanket §8 amendment.

#### Dropped as untraced (D22)

- **F6-13 `Surface Fluid Rate Range` / `Surface Gas Rate Range`** — F6-45/46/47 print min, BEP and max directly at two frequencies, which is far stronger evidence for C-BEP's envelope than a surface range. A surface rate is not a point on a pump curve. Consistent with the F5-05 drop.
- **F6-14 `Free Gas into Pump Range`** — the derived envelope of F6-24's six per-point values.
- **F6-15 `OD` / `Weight` / `Length`** — F2 and F5 precedent. `Run Depth (md)` and the equipment description string stay.
- **F6-16 `Inflow Modeling` and `Pipe Roughness Factor`** — untraced. `Multi-Phase Flow` → `vendor_multiphase_correlation`.
- **F6-19 `Gas Rate`** — the cascade carries free gas. Oil and water rates are kept; see above.
- **F6-25 tubing `Oil` / `Water` flow rates** — vendor-derived downhole duplicates, as dropped in F4-21 and F5. The `Free Gas Rate` half goes to `stg_gas_cascade`.
- **F6-27 `Fluid Density` and `Gas Density`** — `Average Density` is the mixture value the C-SG benchmark wants and lands in `fluid_density_as_printed`; the two components benchmark nothing on their own.
- **F6-33 `Surface Fluid Rate` / `Surface Gas Rate`** — same as F6-13.
- **F6-10's `Motor (Np)`** — duplicates F6-34 `Motor Rtg (Np)` from the p6 Motor Summary, which is the authoritative block. Everything else in F6-10 was already not proposed, so the row drops entirely.
#### Checked, nothing to do

`assumed_pump_wear_pct` on every row (T1) · `fluid_sg_at_observation` (T2) · `section_intake_pressure_psi_as_printed` — exists and is D11-safe, T9 needs nothing · `motor_power_factor_basis = 'max'`, whose enum names F6 by name · `pump_model_as_printed_alt` for the F6-09 / F6-36 cross-read · `turpin_indicator_as_printed` + `correlation_as_printed`, two stage rows covering both F6-32 indicators · `solution_gor_rso_scf_stb` · `bubble_point_correlation` · `static_datum_pressure_psi` · `scenario_selection_rule = 'first_printed_fallback'` · `point_type` `q_intake` / `q_discharge` maps F6-43 cleanly · per-stage head and power bases (T4).

**F6-44 already targets `stg_curve_observations` for `Discharge Pressure`.** No split to correct in this family.

#### Added on Hamed's ruling — 2026-08-20

Both raised as flags after the F6 pass and confirmed.

| Table | Column | Why |
|---|---|---|
| `stg_design_context` | `lateral_depth_tvd_ft` | **C-HYD's hydrostatic term needs TVD, not MD** (Hamed, 2026-08-20). F6-04 prints `Intake Depth` in **MD only**, so F6-03 `Lateral Depth (TVD)` is the family's only vertical reference — drop it and F6 supplies none at all, and C-HYD silently runs on a measured depth in a horizontal well. Not `intake_set_depth_tvd_ft`: the lateral's TVD and the intake's TVD are different points, and conflating them is the error the column exists to prevent. |
| `stg_curve_observations` | `flow_reference_as_printed` | Verbatim label of where a flow or density was measured. `point_type`'s `q_intake` / `q_discharge` maps F6-43, F7-07 and F7-31 cleanly, but not F6-23's `Total Volume at Intake` vs `at Primary`, F6-27's densities at intake / discharge / surface, or F6-33's `Total DH Volume` — each of which otherwise emits rows identical in every key. Preferred over extending `point_type` with F6-specific values, which would put family-specific tokens in a corpus-wide enum. |
### Columns added by the F7 audit — 2026-08-20

`f7-valiant.md`, 46 fields. Four columns, one of them a **rename** with a grain change, and a long drop list of surface-rate and mechanical-rating fields.

#### `stg_gas_cascade` — two PHI indicators at one stage *(rename proposed, overruled — see below)*

**T3 requires two correlations at one cascade stage and the table can hold one.** F7 prints `Turpin PHI` and `Dunbar PHI` (0.000 and 0.007) at the *same* point — Pump Intake — so unlike F6's two indicators, which are two stages and therefore two rows, these collide on `stage_order`.

The column is already paired with `correlation_as_printed`, which makes a Turpin-named column a mislabelling waiting to happen the moment a Dunbar value lands in it. **Rename it correlation-neutral and extend the grain to well × scenario × stage × correlation.** T3's stated reason survives only this way: Dunbar has no consumer today and is the corpus's only cross-check on D26's choice of Turpin, which is worth nothing if the two values arrive indistinguishable. Cheap to do now — F1 is the only family run, and it emits no cascade rows carrying an indicator.

> **Overruled 2026-08-20 — no rename, no grain change.** Hamed's call: F1 is 30 of the 47 in-scope documents, and a `stg_gas_cascade` grain change would invalidate that extraction. Re-running it costs ~$1 per document and the audit finding does not justify it.
> **Instead:** add `dunbar_indicator_as_printed` alongside `turpin_indicator_as_printed`, both paired with `correlation_as_printed`. The grain stays well × scenario × stage, F1's reports stay valid, and T3's two values arrive distinguishable.
> **Cost, recorded rather than argued away:** the pair does not generalize — a third correlation needs a third column — and `correlation_as_printed` is now partly redundant with the column names. Revisit only if a later family prints a correlation that is neither Turpin nor Dunbar.
> *(Claude had proposed the rename on the basis that F1 emits no indicator-bearing cascade rows and the change would therefore be free. That was asserted, not verified — the check against F1's 30 reports timed out and was never run. The proposition should not have been made without it.)*

#### `stg_design_context`

| Column | Why |
|---|---|
| `gas_sg_mixture` | **T4 requires both bases and there is one `gas_sg` slot.** F7 prints `Spg Gas (HC)` and `Spg Gas (Mix)` as different numbers; C-BUBBLE's `gamma_g` wants the hydrocarbon basis. `gas_sg_basis` is a flag on a single column, not a second column — the M1 schema comment even reads "hydrocarbon \| mixture (F7 prints both)" while the column can hold one of them. `gas_sg` keeps the hydrocarbon value; the mixture value gets its own slot. Same shape as F2's `surface_power_factor` and `vendor_liquid_phase_sg`. |

#### `stg_pump_config`

| Column | Why |
|---|---|
| `stages_out` | **§3 requires both `stages` and `stages_out` emitted verbatim** — `Total: <n> stages / <n> Stages Out` — and states that the net count is arithmetic belonging to M5 (D8). There is no `stages_out` column, so the only way to obey §3 today is the subtraction it forbids. |

#### `stg_curve_observations`

| Column | Why |
|---|---|
| `viscosity_correction_factors_as_printed` | **T2 says emit all three of `Avg Cq` / `Avg Ch` / `Avg Cbhp` verbatim**, and only the derived `viscosity_corrected` boolean has a home. The boolean is a judgment the extractor makes from three printed numbers; D8's whole posture is that the inputs to a judgment survive it. Where a factor differs from 1.0000 the magnitude also matters to a later correction, not just the fact of it. One verbatim string. |

#### Contract text needing a follow-up edit

- **The `Discharge Pressure` split is here.** F7-08 `Pump Discharge Pressure` targets `stg_design_context`, which has no discharge column. → `stg_curve_observations.discharge_pressure_psi_as_printed`, on F1's reasoning. F7-30's per-section `Discharge` is already correctly targeted.
- **F7-30 routes `PHI` to `stg_curve_observations`**, which has no indicator column. PHI belongs to `stg_gas_cascade` with F7-40; the pressures stay on the observation row.
- **§7's `curve_observation_role = 'cross_check'` and `'contribution'` are not enum values** — same error as F6 §7. The enum is `head_curve | bhp_curve | single_pump_chart | multiscenario | tapered_composite | design`, and library precedence is an M5/M6 weighting concern.
- **§4 requires `pump_series_as_printed`** — the column added by the F4 audit above. F4 and F7 are its two users; the F4 entry's "found in" should read F4, F7.
- **F7-21 `Pump Depth (MD)` / `Pump Depth TVD`** → the F2 pair `pump_setting_md_ft` / `pump_setting_vd_ft`, not `intake_set_depth_*`. This is a pump-setting depth by its printed name, and F7 is the first family to print the pair in both MD and TVD.
- **Stale G1** in §9 and inline on rows F7-11, F7-25, F7-38, F7-39, F7-40, F7-41.
- **CF-28 grain** unstated; §9 still reads "section × scenario". F7 prints per-housing and total stage counts, so `stages_basis = group_total` with `housing_count` and `stages_per_housing_as_printed` (the column added by the F5 audit — F5 and F7 are its two users).
- **§5's `scenario_label_staged = null`** — omit the key. Covered by the blanket §8 amendment.

#### Dropped as untraced (D22)

**The surface- and system-rate group.** F7 prints `Water Cut` directly (F7-16) *and* downhole flows (F7-07 `Pump Intake` / `Pump Discharge Flow`, F7-31 per-section), which is the condition under which every previous family's raw and surface rates were dropped — F1, F2, F4, F5 and F6-13. On that rule: **F7-03** `Total Flow Min/Design/Max` · **F7-05** `Design Properties: Oil / Water / Gas` · **F7-06** `Production Summary` rates and total · **F7-19** `Desired Rate` · **F7-20** `Design Flow (Qsc)` and the reservoir test data already not proposed. The section envelope C-BEP wants is F7-29, printed as numbers.

**Mechanical ratings.** F7-36's `Pressure @100%H2O` / `Limit` / `Load %` / `Selected` and F7-37's `Shaft Load` / `Limit` / `Load %` are housing and shaft rating checks that upgrade no contract. `Description`, `Housing`, `Stages` and **`Order`** stay — `Order` is the whole reason §3 calls this the one family with no stacking guess.

**The rest.** F7-04 (`BH Temp` and `Pump Depth` duplicate F7-23 and F7-21; casing and tubing not proposed) · F7-11 separator `HP` / `ft` · F7-12 motor `Model` (F2/F5 precedent) · F7-22 `KOP` — `TPI Depth (MD)` → `perf_top_md_ft` stays · F7-33 `Avg Spg Fluid`, an intermediate between the two values that do land (`Spg Fluid` → `fluid_sg_at_observation`, `Avg Pump Spg Fluid` → `vendor_mixture_sg`) · F7-35 `Intake Shaft Power HP`, cumulative shaft load, which F7-37 already covers for stacking · F7-38 separator `Liquid Flow` and per-separator `Specific Gravity` · F7-41 `Total Flow` / `Total Liq Flow` / `Specific Gravity` — `PFG` and `Total Gas Flow` go to the cascade · F7-42 `Operating HP` / `V` / `F`, the exact set F1 dropped as vendor-derived duplicates · F7-45 `Total KW` — `Motor KW` → `vendor_motor_power_kw` is the C-ENERGY term · F7-46 `OD` / `WT` / `Length` / `Part#`, with `Start MD` / `Stop MD` → `top_md_ft` / `bottom_md_ft` staying.
#### Checked, nothing to do

`speed_rpm_as_printed` for F7-27's `(<n> RPM)` — F3 and F7 are its two users · `point_type` `q_intake` / `q_discharge` maps F7-07 and F7-31 cleanly, and F7-31's `Avg Flow` is the `design_point` row, no new column needed · `section_intake_pressure_psi_as_printed` (T8, D11-safe) · `intake_temp_f` / `reservoir_temp_f` / `surface_temp_f`, three columns for T5's three temperatures · `bubble_point_correlation` for T1's Al-Shammasi · `vendor_mixture_gradient_psi_per_ft` for F7-24's `Flowing Gradient` · `static_datum_pressure_psi` for `P Res` · `solution_gor_rso_scf_stb` for `Rsb` · `motor_power_factor_basis = 'operating'` · `efficiency_pct` carries T7's BEP and design values on their matching rows.
