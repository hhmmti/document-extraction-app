---
title: F4 ELS — Extraction Contract
created: 2026-08-14
status: corrected 2026-08-20 against the staging-schema audit — M4 runs this version
milestone: M2
family: F4
vendor_family: els
documents: 3
proposed_fields: 28
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - contract
  - els
related:
  - "[[m2-extraction-method]]"
  - "[[m1-extraction-spec]]"
  - "[[design-doc-extraction-plan]]"
  - "[[design-doc-extraction-kickoff]]"
---

# F4 ELS — Extraction Contract

> [!note] **One of seven. Not interchangeable with any other contract.** The umbrella is [[m2-extraction-method]]; the field record is `m1-field-inventory.csv` (28 rows with `proposed = yes`, family `ELS`); the decisions are locked in [[design-doc-extraction-kickoff]]. Extraction is **verbatim** (D8) — no unit conversion, no arithmetic, no canonicalization, no conflict resolution. Every transform belongs to M5.

## 0. Applicability

**Vendor:** Endurance Lift Solutions (formerly "ELS"). **Tool:** LiftXP. **Documents: 3.**

`Batman Fed Com 134H Design 2.25.25.pdf` · `Doc Gardner C 19H Design 1.7.25.pdf` · `Moran 9 Fed Com 172H Design 2.11.26.pdf`

> **The thinnest family in the corpus: 5–6 pages, 28 proposed fields, no curve observations, no BEP, no ROR, no power factor.** It is small because the documents are thin, not because the contract is unfinished. **It must not be merged with F2 ChampionX** — both print through Telerik (16.1.22 / 17.0.23 here vs 17.2.23 there) and share no field names, no page titles and no model grammar.

---

## 1. Page-1 signature — confirm before extracting

Page 1 must show **all** of:

- a **`Well Name :`** block (with the space before the colon, as printed)
- a **`Powered by LiftXP`** footer
- a `Pumps` block with **`Manufacture`** printed as a field label (note: `Manufacture`, not `Manufacturer`)

If page 1 reads `EQUIPMENT AND PERFORMANCE REPORT` with a `ChampionX Representative` block, this is **F2** — HALT and route to `f2-championx.md`.

---

## 2. Pages

**5–6 pages.**

| Page | Contents |
|---|---|
| p1 | header · `Pumps` block (Top / Bottom) · `Well Completion` · `Operating Performance` · `Fluid Properties` · `Motor M1` |
| p2 | `OPERATING PARAMETERS` · `FLUID PROPERTIES` · `OPERATING PERFORMANCE` · pump chart title |
| p3+ | `Frequency Head chart`, `Frequency Power chart` — **plot-only**, no text-layer numbers |

The 5-vs-6 page difference is trailing plots. No optional data pages. A missing p1 or p2 is a **HALT**.

---

## 3. Sections — pump bodies only

The `Pumps` block on p1 prints **`Top` and `Bottom`** columns. Those are the sections.

- **`Bottom` = deepest = `section_order` 1.**
- Where only one column is populated the string is single-section; that is normal, not an error.
- Never a section row: intake section, packer, seals, sensor, motor, cable. `F4-09` names `Packer installed / Intake Section / Rot. Sep Efficiency` because they are printed together — **only `Rot. Sep Efficiency` is proposed** (it traces to `K-TIER2P`); packer and intake section are not extracted.
- This family prints **no per-section depths**. `top_md_ft` and `bottom_md_ft` are `null` for F4, and that is correct — `pump_config` allows null there. Do not derive them from `Pump Setting Depth`.

---

## 4. Model grammar

**Three separate printed fields — do not concatenate them.**

| Printed field | Example | Column |
|---|---|---|
| `Manufacture` (`F4-02`) | `ELS` | `manufacturer` |
| `Series` (`F4-03`) | `400` | `pump_series_as_printed` |
| `Model` (`F4-04`) | `ELS-1750` | `pump_model_as_printed` |

```
^ELS-\d{3,4}$
```

This is the only family that prints manufacturer and series as **separate labelled fields** rather than folding them into the model token. Emit all three verbatim. Building `400ELS1750` or `ELS 400 1750` is a normalization and belongs to M5 via D31.

> Track 1's first-iteration failure was **confusing the report's vendor format with the equipment manufacturer**. Here they happen to coincide (Endurance authors the report and makes the pump), which makes the trap invisible. `manufacturer` comes from the **`Manufacture` field**, not from the footer, not from the filename, not from `vendor_family`.

The p2 chart title (`F4-28`) prints `<manufacturer> <series> <model>` as one string. Emit it to `stg_alias_evidence` verbatim — it is D31 evidence for exactly how this vendor concatenates, and it must not overwrite the three separate fields.

---

## 5. Scenarios

**Single-scenario family.** `scenario_label_staged = null`, `scenario_ordinal = 1`, `is_design_scenario = true`. **D16 and D17 cannot fire on F4.** If either does, the document is not what this contract describes — HALT.

---

##### `pump_config` grain — `model_grouped` *(CF-28, stated 2026-08-20)*

Emit **one row per distinct model**, not one row per housing. Set `section_grain = 'model_grouped'`.

F4 prints `Nr Stages` per `Top` / `Bottom` column with **no housing count**, so `stages_basis = 'total_reported'` and `housing_count` is omitted. Where a future ELS document does print per-housing counts and they are not uniform, emit every printed count in section order (`"42, 93, 93"`), never a modal value — F2's GOUDA prints `42 + 93×4 = 414` and a dominant `93` loses a housing.

**Absence is an omitted key, not a null.** §3 above says `top_md_ft` and `bottom_md_ft` are "`null` for F4", and §5 says `scenario_label_staged = null`. Both were written in the CSV era, when a null was a cell. Under the JSONL rules an unprinted value is an **omitted key** — never `null`, `""` or `"-"`. The finding that F4 prints no per-section depths stands unchanged; only the encoding differs.

## 6. Traps — mandatory guardrails

**T1 — `Calculated Pb  No` is the corpus's only explicit D14 provenance flag. Extract it, and let it set the class.**
`Batman 134H` prints `Calculated Pb` **`No`** alongside `Pb 1800.00 (psi)`. **No other family states whether the bubble point was typed or computed.**
- Emit `bubble_point_calculated_flag` verbatim (`Yes` / `No`) — `F4-15`.
- Set `bubble_point_provenance` **from that flag**: `No` → `vendor input` (the engineer typed it); `Yes` → `vendor derived` (LiftXP computed it).
- If the flag is absent, `bubble_point_provenance = 'vendor assumption'` and record why. **Never guess the flag.**

**T2 — Free gas is in `mcf/d`, not a percentage. Unique to this family.**
`Free gas at Intake 8.43 (mcf/d)` (`F4-22`). Every other family prints a **percentage or a volume fraction**. Emit the number **and** the unit token `mcf/d` verbatim (D8). An extractor that drops the unit here produces a value that reads as 8.43 % free gas — off by orders of magnitude and physically plausible enough to survive review.

**T3 — Bubble-point unit is `psi` here, printed in parentheses: `Pb 1800.00 (psi)`.**
Strip nothing. `bubble_point_unit_as_printed = 'psi'`. Required field (`V-04`).

**T4 — No power factor anywhere (0/3). `C-BHP` cannot be upgraded from F4.**
Do not substitute anything from the `Motor M1` block. Set `motor_power_factor = null`, `power_factor_absent_confirmed = true`. This is the second family with this finding (see F3 T4) and it is *the* reason `C-BHP` upgrades on 5/7 rather than 7/7.

**T5 — No curve observations, no BEP, no ROR (0/3).**
`Frequency Head chart` and `Frequency Power chart` are plot-only. Set `envelope_status = 'absent'`. `F4-27` `Total Required BHP` is a **string-level** power number: `power_basis_as_printed = 'string_total'`, `observation_is_composite = true`. It is not attributable to a section.

**T6 — `Email` is a literal placeholder string at n=3.** It is not proposed and must not be extracted. Do not treat its constancy as a data quality signal.

**T7 — `Composite SG` (`F4-19`) is `vendor derived` and goes to the benchmark column, not to `sg_for_dp` as an input.**
`sg_basis = 'composite'`. It is a check on `calc_mixture_sg`, not a replacement for it (D13).

**T8 — Motor amperage: `F4-18` names `Series / Type / Hp / Amps / Volts / Shroud`. `Amps` is excluded (D2). Extract the rest.**

**T9 — `GOR` on p2 is not proposed; `Calculated Water cut` on the same row is.**
`F4-25` is a composite row. GOR is already `Direct` from production data and carries no upgrade. Extract only `Calculated Water cut`.

---

##### Trap corrections — 2026-08-20 *(schema audit; originals above left standing)*

**T5 is wrong in a way that would empty this family, and §7 repeats it.** Both say F4 emits "no curve observations" and "no curve rows at all". **Eight §8 rows target `stg_curve_observations`** — `F4-05`, `F4-07`, `F4-08`, `F4-20`, `F4-23`, `F4-26`, `F4-27`, `F4-28`. What T5 means is that F4 has **no *curve points*** — no BEP, no ROR, no plotted head curve, `envelope_status = 'absent'`. The **design-point observation is real** and is the only flow / head / power record this family produces. An extractor reading T5 or §7 literally emits an empty block and loses F4 entirely.

**T4 — `power_factor_absent_confirmed` is not a column.** A proposed field absent from the document goes under ***Fields not found***, which is exactly the checked-and-absent record T4 wants. `motor_power_factor = null` is likewise not how absence is recorded — omit the key. The finding that F4 has no power factor at 0/3, and that this is why `C-BHP` upgrades on 5/7 rather than 7/7, stands unchanged.

**T7 — there is no `sg_basis` column.** `Composite SG` goes to **`vendor_mixture_sg`**, and the column you choose carries the basis. The D13 point is unaffected: it is a check on `calc_mixture_sg`, never an input to `sg_for_dp`.

**T2 gains a destination.** `Free gas at Intake 8.43 (mcf/d)` goes to `stg_gas_cascade` — `gas_rate_as_printed` with `gas_rate_unit_as_printed = 'mcf/d'`. Both columns exist. See the §8 row corrections for why it must **not** also go to design context.

## 7. Library-held models — do not re-extract the head curve

`400DAL650H · 400DAL1200 · 400DAL1200H · 400DAL1750H · 400DAL3000H · 400DAL4300H · SD2000 · SF900 · SF1750 · SF2700 · SF4300 · SFGH2500 · SFGH4300`

**No current F4 model matches** — `ELS-1750` is Endurance and is absent from the 13. Run the check on every section anyway (match by D31 `proposed_canonical`). Given T5, this family emits no curve rows at all, so the rule is a formality here — but it is stated because a future ELS document could print a rebadged model, and a silent overwrite of a library row is exactly the failure the rule exists to prevent.

---

## 8. Fields to extract

**28 fields.** Every one is a `proposed = yes` row of `m1-field-inventory.csv` with `family = ELS`. No field outside this table may be extracted.

| ID | Field, as printed | Page | Cardinality | Class (D14) | Trace | Target |
|---|---|---|---|---|---|---|
| `F4-01` | Well Name / Project Name / company | p1 header | `per_document` | vendor input | join key -> well_id | *(join key — all four)* |
| `F4-02` | Pumps Top / Bottom: Manufacture | p1 Pumps block | `per_section` | vendor input | D31 manufacturer resolution (ELS = Endurance Lift Solutions) | `stg_pump_config` + `stg_alias_evidence` |
| `F4-03` | Pumps Top / Bottom: Series | p1 Pumps block | `per_section` | vendor input | D31 model grammar (400 series prefix is printed separately from the model) | `stg_pump_config` + `stg_alias_evidence` |
| `F4-04` | Pumps Top / Bottom: Model | p1 Pumps block | `per_section` | vendor input | C-NARROW + C-IDEAL + D31 model grammar | `stg_pump_config` + `stg_alias_evidence` |
| `F4-05` | Pumps Top / Bottom: Nr Stages | p1 Pumps block | `per_section` | vendor input | C-IDEAL (stages) + K-TIER1 | `stg_pump_config` + `stg_curve_observations` |
| `F4-06` | Well Completion: Pump Setting Depth | p1 Well Completion | `per_document` | vendor input | C-HYD + C-DPREC (well_depth_ft) | `stg_design_context` |
| `F4-07` | Operating Performance: Operating Frequency | p1 | `per_document` | vendor input | K-AFFNORM (D10 as-printed frequency) | `stg_design_context` + `stg_curve_observations` |
| `F4-08` | Operating Performance: Intake Production Rate | p1 | `per_document` | vendor input | K-CURVEFIT (downhole flow at the design point) | `stg_curve_observations` |
| `F4-09` | Operating Performance: Packer installed / Intake Section / Rot. Sep Efficiency | p1 | `per_document` | vendor input | Rot. Sep Efficiency traces to K-TIER2P; packer and intake section not proposed | `stg_gas_cascade` ⚠️G1 + `stg_design_context` |
| `F4-10` | Fluid Properties: Oil Gravity | p1 | `per_document` | vendor input | C-SG (sg_oil) + C-BUBBLE (API) | `stg_design_context` |
| `F4-11` | Fluid Properties: Water Gravity | p1 | `per_document` | vendor input | C-SG (sg_water) | `stg_design_context` |
| `F4-12` | Fluid Properties: Gas Gravity | p1 | `per_document` | vendor input | C-BUBBLE (gamma_g - replaces the 0.75 default) | `stg_design_context` |
| `F4-13` | Fluid Properties: Bottom Hole Temp | p1 | `per_document` | vendor input | C-BUBBLE (T_f - replaces the 150 F default) | `stg_design_context` |
| `F4-14` | Fluid Properties: Surface Fluid Temp | p1 | `per_document` | vendor input | C-BUBBLE (T_f gradient endpoint) | `stg_design_context` |
| `F4-15` | Fluid Properties: Calculated Pb (Yes/No) | p1 | `per_document` | vendor input | C-BUBBLE provenance - the only family that prints the input-vs-derived flag explicitly (D14) | `stg_design_context` |
| `F4-16` | Fluid Properties: Pb | p1 | `per_document` | vendor assumption | C-BUBBLE (selected_bubble_point_psi); classification read off the Calculated Pb flag | `stg_design_context` |
| `F4-17` | Fluid Properties: Water Cut | p1 | `per_document` | vendor input | C-SG (water_cut) | `stg_design_context` |
| `F4-18` | Motor M1 (Series / Type / Hp / Amps / Volts / Shroud) | p1 | `per_document` | vendor input | C-ENERGY nameplate context; Amps excluded (duplicates v2 motor_rated_amps) | `stg_design_context` |
| `F4-19` | OPERATING PARAMETERS: Composite SG | p2 | `per_document` | vendor derived | C-SG benchmark (calc_mixture_sg check) + C-HEADDP (sg_for_dp candidate) | `stg_design_context` |
| `F4-20` | FLUID PROPERTIES: Total volume at Intake | p2 | `per_document` | vendor derived | K-CURVEFIT (downhole flow at the design point) | `stg_curve_observations` |
| `F4-21` | FLUID PROPERTIES: Oil Rate / Water Rate | p2 | `per_document` | vendor derived | C-SG (water cut basis at downhole conditions) | `stg_design_context` |
| `F4-22` | FLUID PROPERTIES: Free gas at Intake / Free gas into Pump | p2 | `per_document` | vendor derived | K-TIER2P (alpha cascade) - printed in mcf/d not % so the unit token must be carried verbatim (D8) | `stg_gas_cascade` ⚠️G1 + `stg_design_context` |
| `F4-23` | FLUID PROPERTIES: Pump TDH | p2 | `per_document` | vendor derived | K-TIER1 / Test A benchmark | `stg_design_context` + `stg_curve_observations` |
| `F4-24` | OPERATING PERFORMANCE: BOPD / BWPD / Gas Production | p2 | `per_document` | vendor input | C-SG (surface water cut basis) | `stg_design_context` |
| `F4-25` | FLUID PROPERTIES: GOR / Calculated Water cut | p2 | `per_document` | vendor derived | Calculated Water cut traces to C-SG; GOR not proposed (already Direct from production data) | `stg_design_context` |
| `F4-26` | OPERATING PARAMETERS: Required TDH | p2 | `per_document` | vendor derived | K-TIER1 benchmark (required vs produced head) | `stg_design_context` + `stg_curve_observations` |
| `F4-27` | OPERATING PARAMETERS: Total Required BHP | p2 | `per_document` | vendor derived | K-CURVEFIT (string power at the design point) | `stg_curve_observations` |
| `F4-28` | Pump chart : <manufacturer> <series> <model> | p2 chart title | `per_section` | vendor input | K-CURVEFIT (names the model the plotted curve belongs to) | `stg_alias_evidence` + `stg_curve_observations` |

---

##### Row corrections — 2026-08-20 *(schema audit; the table above is left standing)*

| Row | Was | **Is** | Why |
|---|---|---|---|
| `F4-22` | `stg_gas_cascade` ⚠️G1 **+ `stg_design_context`** | **`stg_gas_cascade` only** | The design-context column is `vendor_free_gas_into_pump_pct` — a **percent** column. F4 prints `8.43 (mcf/d)`. Sending a rate to a percent column is precisely the error T2 exists to prevent, and it survives review because 8.43 reads as a plausible percentage. `gas_rate_as_printed` + `gas_rate_unit_as_printed` hold it correctly. |
| `F4-09` | `stg_gas_cascade` ⚠️G1 + `stg_design_context` | **`stg_gas_cascade` only** | Only `Rot. Sep Efficiency` is proposed from this row, and `separation_efficiency_pct` holds it. Packer and intake section are not extracted, so the design-context half has nothing to carry. |
| `F4-06` | `stg_design_context` | **`pump_setting_md_ft`** with `depth_reference_basis = 'pump_setting'` | `Pump Setting Depth` prints no MD/VD qualifier. Naming the key removes the choice from the extractor. Do **not** derive `top_md_ft` / `bottom_md_ft` from it — §3 already says so. |
| `F4-23` | `vendor_tdh_at_design_ft` | unchanged, **but see `F4-26`** | `Pump TDH` is the produced head. |
| `F4-26` | `stg_design_context` (implicitly the same column) | **`vendor_required_tdh_ft`** | `Required TDH` and `Pump TDH` are **different quantities** and were sharing one column. K-TIER1's benchmark *is* the comparison between them; one slot collapses it to a single number and destroys the check. Column added by the F4 audit. |
| `F4-03` | `stg_pump_config` + `stg_alias_evidence` | unchanged, key is **`pump_series_as_printed`** | §4 names the column and the schema had none until the F4 audit created it. F4 and F7 are its only users. `ELS-1750` carries no series token, so `400` is unrecoverable from the model string. |
| `F4-25` | `stg_design_context` | **`vendor_calculated_water_cut_frac`** | `Calculated Water cut` (derived) and `F4-17` `Water Cut` (input) were both targeting `water_cut_design_frac`. D13: the derived value is a check, never an input. |
| `F4-14`, `F4-22`, `F4-09` | ⚠️G1 markers | **struck** | **G1 is closed** (CF-16). `stg_gas_cascade` is defined in `staging-schemas.md`; pump-body rows resolve into `curve_observations.free_gas_at_inlet_pct` at load, separator stages stay in staging as the audit record. |

##### Dropped from the 28 — D22, no contract upgraded, no calculation benchmarked

- **`F4-20` `Total volume at Intake`** — a vendor-*derived* duplicate of `F4-08` `Intake Production Rate` (vendor input). Both targeted `flow_as_printed` on one design-point row, and an extractor would silently pick one. Hamed's call, 2026-08-20, on the F1 precedent for derived duplicates.
- **`F4-24` raw `BOPD` / `BWPD` / `Gas Production`** — `F4-17` prints `Water Cut` directly and the design-point total carries the rest. The F2 audit dropped this exact row shape, and F1, F5 and F7 followed.
- **`F4-21` downhole `Oil Rate` / `Water Rate`** — the derived water-cut check it would support is now captured directly by `F4-25`.
- **`F4-18` sub-fields `Series` and `Shroud`** — no column, no trace. `Type` / `Hp` / `Volts` stay; `Amps` was already excluded by D2.

**The proposed count is therefore 25, not 28** — three whole rows dropped plus two `F4-18` sub-fields. Set the batch script's field-count band against the **CSV's 28**, which still carries the undropped rows; the prompt's inlined field list is what binds the extractor.

> **Correction, recorded not rewritten — 2026-08-20.** This subsection first read "the proposed count is therefore 38, not 42". **F4 has 28 proposed fields, not 42** — stated in this contract's own frontmatter (`proposed_fields: 28`), in §8's header line, and by the row IDs running `F4-01`…`F4-28`. Claude carried F2's count of 42 across without checking, and put the same wrong number in `f4-batch.sh`'s field-count gate, where the CSV's real 28 tripped the FATAL on the first dry run. No credits were spent — the gate fires before any model call, which is what it is for.

## 9. Target tables and cardinality

Extraction writes to **staging**. M5 owns every collapse, normalization and division.

| Staging target | Grain emitted by this contract | Destination |
|---|---|---|
| `stg_pump_config` | **model** (`model_grouped`, CF-28; single scenario) | `pump_config` (well × section × epoch) |
| `stg_design_context` | one row — **single-scenario family** | `esp_well_design_context` (well) |
| `stg_curve_observations` | model × point — **design points only, no curve points** | `curve_observations` (model × point) |
| `stg_gas_cascade` | cascade stage | pump-body rows resolve into `curve_observations.free_gas_at_inlet_pct`; separator and intake stages stay in staging as the audit record (**CF-16 closed**) |
| `stg_alias_evidence` | printed string | the **D31** lookup (a component, not a table) |

> **G1 is closed.** This table previously marked `stg_gas_cascade` as having no destination column. `staging-schemas.md` defines it, and `m2-validator-spec` §9's matching instruction — that M5 must not load cascade rows — is stale for the same reason and still needs correcting.

**D16 and D17 cannot fire on F4.** Single scenario, so the collapse rules are formalities here, unlike F3. If either fires, the document is not what this contract describes.

**F4 never prints `Discharge Pressure`.** The cross-contract split corrected in F1, F3, F5 and F7 does not occur in this family.

## 10. Family validator rules

Global rules `V-01` … `V-20` in [[m2-validator-spec]] apply to every family. The family-specific rules for F4 are `V-F4-*` in that document's §4. Pass B runs both sets.

---

## Not extracted

Per **D22**, the not-extracted list for this family lives in `m1-field-inventory.csv` (`family = ELS`, `proposed = no`, 17 rows, each with a one-line reason) and in the R1 template atlas. It is **not** restated here and **not** restated per document. A per-document report notes only *anomalies against this contract*.

Corpus-wide exclusions that apply here without exception: **PI** and **PIP** (D11) · **NPSHr** — no mapping from any field, including `Free Allowed Gas` (D12) · motor amperage (D2, duplicates `esp_well_configuration_v2.motor_rated_amps`) · producing GOR/GLR · cable / VSD / transformer / seal / sensor selection strings · design-time tubing and casing setpoints.

---

## Log

| Date | Update |
|---|---|
| 2026-08-14 | Contract issued at M2. Not yet run — M3 pilots it on `Batman Fed Com 134H Design 2.25.25.pdf`. || 2026-08-20 | **Corrected against the staging-schema audit, ahead of the M4 F4 run.** §5 gains the `model_grouped` grain statement (CF-28) with `stages_basis = total_reported`, and the CSV-era `null` language in §3 and §5 is superseded by omitted keys. §6 gains trap corrections — the important one being that **T5 and §7 are wrong**: they say F4 emits no curve rows while eight §8 rows target `stg_curve_observations`, and an extractor obeying them literally would empty the family. Also: `power_factor_absent_confirmed` is not a column (T4), there is no `sg_basis` column (T7), and T2's `mcf/d` value now has a destination. §8 gains row corrections — `F4-22` and `F4-09` lose their `stg_design_context` halves (a percent column cannot hold an `mcf/d` rate), `F4-26` moves to the new `vendor_required_tdh_ft`, `F4-25` to `vendor_calculated_water_cut_frac`, `F4-03` to `pump_series_as_printed`, `F4-06` to `pump_setting_md_ft`, and the ⚠️G1 markers are struck. Four items dropped under D22 (`F4-20`, `F4-24`, `F4-21`, and `F4-18`'s `Series` / `Shroud`), taking the proposed count from 42 to **38**. Original text left standing throughout; corrections override rather than replace. || 2026-08-20 | *Correction:* the row-corrections subsection above initially said the drops took the count "from 42 to 38". **F4 proposes 28 fields, not 42** — per this contract's frontmatter, §8's header and the `F4-01`…`F4-28` row IDs. Claude carried F2's 42 across unchecked and repeated it in `f4-batch.sh`'s gate. Corrected to **28 → 25**. Caught by the gate on a dry run, before any model call. |
