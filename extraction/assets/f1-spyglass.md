---
title: "F1 SpyGlass — Extraction Contract"
created: 2026-08-14
status: issued — M3 runs this unchanged
milestone: M2
family: F1
vendor_family: spyglass
documents: 30
proposed_fields: 57
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - contract
  - spyglass
related:
  - "[[m2-extraction-method]]"
  - "[[m1-extraction-spec]]"
  - "[[design-doc-extraction-plan]]"
  - "[[design-doc-extraction-kickoff]]"
---

# F1 SpyGlass — Extraction Contract

> [!note] **One of seven. Not interchangeable with any other contract.** The umbrella is [[m2-extraction-method]]; the field record is `m1-field-inventory.csv` (57 rows with `proposed = yes`, family `SpyGlass`); the decisions are locked in [[design-doc-extraction-kickoff]]. Extraction is **verbatim** (D8) — no unit conversion, no arithmetic, no canonicalization, no conflict resolution. Every transform belongs to M5.

## 0. Applicability

**Vendor:** Summit ESP (Halliburton). **Tool:** SpyGlass. **Documents: 30** — the largest family in the corpus and the one whose page count varies most.

<details><summary>The 30 documents</summary>

`ADOBE CHEVELLE B 332H` · `AFFIRMED C 333H` · `ARUBA STATE 15-14 UNIT 223H` · `CAPE BUFFALO 12-11 D 224H` · `CHEDDAR 3BS FEDERAL COM 1H` · `DOC SEUSS B 332H` · `EL CAMPEON FED COM 401H` · `EL CAMPEON FED COM 412H` · `EL CAMPEON FED COM 432H (DEPTH NOT CORRECT)` · `EL CAMPEON FEDERAL COM 404H` · `EVAN WILLIAMS 23-13W UNIT 1H` · `GUAM STATE 26-21 UNIT 1H` · `HALEY NE I 154H` · `HALEY NW A 411H` · `JERSEY LILLY 17-7 UNIT B 333H` · `MORAN 9 FED COM 701H` · `MORAN 9 FEDERAL COM 504H` · `MORAN 9 FEDERAL COM 506H` · `MORAN 9 FEDERAL COM 604H (HF GS)` · `Oryx Roan State H 1303H` · `PERMIAN RESOURCES-HALEY NW E 333H-SF5800` · `Permian Eileen 25 Fed Com 142H` · `Permian Moran 9 Fed Com 502H` · `ROBIN FED 113H` · `ROBIN FED 203H` · `THORNY ROSE A5 332H` · `THUNDERBALL FEDERAL COM 323H` · `WILD SALSA FED COM 224H` · `WILD SALSA FED COM 404H` · `WYATT EARP 22-21 UNIT A 332H`

</details>

> `EL CAMPEON FED COM 432H DESIGN - DEPTH NOT CORRECT.pdf` carries an operator warning **in its filename**. Extract it normally; record `source_document_filename_warning = 'DEPTH NOT CORRECT'` in the report's Extraction Notes. Do not silently drop it and do not silently trust its depths.

---

## 1. Page-1 signature — confirm before extracting

Page 1 must show **both** of:

- the document title **`SIZING REPORT`**
- a `Well` / `Customer` / `Prepared by` / `Date Generated` header block

If page 1 does not match, **HALT**. Do not fall through to another contract. The two known misroutes are `ESPD_`-prefixed files (that prefix spans SLB and Baker Hughes, not Summit) and any file whose PDF `Producer` reads `Skia/PDF` — the `Skia/PDF m124…m147` spread is a **Chrome build number and nothing else**, and two Baker ProLift documents print through it. Neither misroute prints the header block above, so the check still catches them.

> [!warning] **Amended 2026-08-16 (CF-19).** A **`Summit ESP Representative`** block was previously required here, and it HALTed **5 of this family's own 30 documents** — `ADOBE CHEVELLE B 332H`, `DOC SEUSS B 332H`, `MORAN 9 FEDERAL COM 504H`, `MORAN 9 FEDERAL COM 506H`, `WYATT EARP 22-21 UNIT A 332H`. All five carry `SIZING REPORT` and the full header block; what they lack is the page-1 disclaimer paragraph the string sits inside. PREP established this at n=30. The block is now **recorded if present, never required**: emit `summit_representative_block_present` true/false in Extraction Notes.

## 2. Pages

**Required spine — present in all 30, minimum layout is the 9-page `HALEY NW E 333H`:**

| Page | Title | What it carries |
|---|---|---|
| p1 | `SIZING REPORT` | document identity, design date |
| p2–4 | `Design Schematic` | the string (`PUMPS / HOUSINGS`), depths, per-scenario surface conditions — **repeats once per scenario** |
| p5 | `Design Overview` | fluid properties, bubble point, depths — **per scenario** |
| p6 | `Theoretical Production Data` | operating frequency, total system efficiency |
| p7 | `Pumps` | per-section model, stages, efficiency, α |
| p8 | `Motor` | power factor, motor efficiency, motor kW |
| p9–11 | `Multi-Frequency Head Curve` | Variable Frequency Analysis table — 6 frequencies |
| p12–14 | `Multi-Frequency BHP Curve` | Operating HP Required at 6 frequencies |
| p15–17 | `Single Pump Charts` | **the per-section curve source** — ROR, Lift, Q-INT/DIS, Free Allowed Gas, Best Efficiency, Operating Range |

**Optional pages — may be absent; absence is not an error and must not be reported as one:**

`Multiscenario Pump Curve` (p18 — three composite `(flow, head)` pairs per scenario, the Test A target) · `Multifrequency Tapered Pumps` (p19–21) · `Motor Performance` · `Cable Temperature` · `Cross Section Analysis` · `Pressure Traverse` · `IPR` · **`Correlations List`** · `VSD`.

`Oryx Roan State H 1303H` (34 p) is the maximal case and carries all seven optional groups. Record which optional pages were present in the report's `pages_present` list — M6 needs it to explain coverage gaps.

**Page numbers above are the minimal-spine positions.** Scenario repetition shifts everything after p4. **Locate pages by title, never by number.**

---

##### Continuation sheets — added 2026-08-16 (CF-20)

**A chart group spans one page per scenario, and only the first page of the group carries a title.** `Multi-Frequency Head Curve` at p9–11 is not three sections of one chart — it is scenario 1, scenario 2, scenario 3, and pages 10 and 11 print no heading of their own. The same holds for `Multi-Frequency BHP Curve`, `Single Pump Charts` and `Multifrequency Tapered Pumps`.

"Locate pages by title, never by number" (above) is correct for finding the *start* of a group and **cannot reach the rest of it**. An extractor obeying only that rule reads scenario 1 and silently drops the others. PREP counted **267 such pages across F1, F2, F3, F4, F5 and F7** — this is the largest single source of silent data loss available to this project.

**The resolution rule:** a page with no heading of its own belongs to the nearest titled page above it, and carries the next scenario of that role. `prep/prep-manifest.csv` records this per page — the `notes` column reads `continues <role>` and `original_page_no` gives the source page. **Read every page in the sliced PDF; the manifest tells you what each one is.**

## 3. Sections — pump bodies only

The section list comes from the `PUMPS / HOUSINGS` block on `Design Schematic`, cross-read against the `Pumps` page and the `Single Pump Charts` table. All three must name the same models in the same order.

- **In scope as a section:** centrifugal pump bodies (`P1`, `P2`, …) and **gas handlers** (a gas handler produces head and is a pump body for this project).
- **Out of scope, never a section row:** `INTAKES I1 I2`, `SEALS S1 S2`, `MOTOR`, `SENSOR`, `MOTOR LEAD EXTENSION`, `CABLES`, `VSD`, `Step Up Transformer`, gas *separators*.
- `section_order` **1 = deepest (intake side)**, ascending toward discharge. SpyGlass prints the string top→bottom on the schematic; **reverse it**, and state in the report which direction the page printed so Pass B can check.
- `section_role` ∈ `primary | taper | gas_handler`. Assign `gas_handler` from the printed equipment name; assign `primary` to the deepest pump body and `taper` to the rest **only when the models differ** — where every pump body is the same model the string is segmented-identical and all rows are `primary`.

`stages_basis`: SpyGlass prints **both** a per-`Pn` stage count and a string total next to `PUMPS / HOUSINGS`. Emit the per-row value with `stages_basis = 'per_housing'` (`F1-05`) **and** the total as a document-level cross-check (`F1-06`). Do not reconcile them at extraction.

---

## 4. Model grammar

```
^(SD|SF|SFGH)\d{3,4}[A-Z]?(\s+TS\d)?(\s+XR)?(\s+\(HS\s+Shaft\))?$
```

Observed forms: `SF3550 TS4 XR (HS Shaft)` · `SF3550 TS4 XR` · `SF1750` · `SF5800` · `SFGH2500` · `SD2000`.

- The model is **multi-part**. `TS4` is *part of the printed model*, not a separate token — R1 counted `TS4` alone 979 times, which is what made it look like a token rather than a component. `XR` and `(HS Shaft)` likewise.
- Capture the **entire printed string** into `pump_model_as_printed`. Do not strip, split, reorder, or canonicalize (D8, D31).
- The `Sizing` field on p1 (`F1-02`) may carry a whole-string quote token such as `SF3550-SF4300-HFGS-400hp 420MTR`. That is **not a model** — it names a whole string plus motor. Emit it to `stg_alias_evidence` with `evidence_kind = 'composite_string_token'`. It must never resolve to a single model.
- The **same model appears in three places** — `Design Schematic`, `Pumps`, `Single Pump Charts`. If the three disagree in any character, **FLAG**; do not pick one.

---

## 5. Scenarios

SpyGlass repeats `Design Schematic`, `Design Overview`, `Head Curve`, `BHP Curve`, `Single Pump Charts`, `Tapered Pumps` and `Gas Curve` **once per scenario**. The true grain of every `per_section` row in this family is therefore **section × scenario**.

- Emit **one row per section per scenario**, with `scenario_label_staged` = the scenario title **as printed**, and `scenario_ordinal` = 1-based order of appearance.
- Emit `scenario_title_as_printed` and the **table-sourced** `frequency_hz_as_printed` as two separate fields on every scenario. See §6 T1.
- **Design-scenario selector** (used by M5 for the well-grained sidecar, per D16):
  1. the scenario whose `Design Schematic` `Surface Rate (STB/D)` equals the `Design Overview` design/target rate;
  2. else the scenario whose table frequency equals the `Theoretical Production Data` `Operating Frequency` at the document level;
  3. else **scenario_ordinal = 1**, recorded as `scenario_selection_rule = 'first_printed_fallback'`.

  Emit `is_design_scenario` per scenario and `scenario_selection_rule` once. The extractor **applies the ladder mechanically and records which rung fired** — that is selection, not conflict resolution, and it is permitted here because the rule is fixed in advance.

---

## 6. Traps — mandatory guardrails

**T1 — Table over page title, always. Absolute.**
`HALEY NE I 154H` is titled `1575bpd 51.98hz` while every table in that scenario reports **41.99 Hz** — 10 Hz out. `THUNDERBALL FEDERAL COM 323H` is titled `450bpd 53.39hz` while its table reports **53.36 Hz** — 0.03 Hz out. **The 0.03 Hz case is the dangerous one: it passes any plausibility check.** There is no tolerance and no exception. Take the table value for `frequency_hz_as_printed` in every case, including when the two agree. Record the title verbatim in `scenario_title_as_printed` and set `scenario_title_frequency_disagrees = true` on **any** difference, however small. Never average, never round, never prefer the title because it looks tidier.

**T2 — `Lift (ft)` is a section total; per-stage head is a division and the division is M5's.**
Emit `Lift (ft)` (`F1-49`) as `head_as_printed` with `head_basis_as_printed = 'per_section_total'`, and `Stages` (`F1-31`) as `stages_at_observation`. **Do not divide.** D8 puts every arithmetic operation in the transform layer; a per-stage number computed at extraction cannot be re-derived when the basis changes.

**T3 — `Free Allowed Gas` is not NPSHr.**
Record it under its own name (`F1-48`) as a model-level property. **D12**: it is gas-handling capability, not cavitation margin, and no mapping to `NPSHr` is permitted in any layer.

**T4 — `bbl/d` vs `STB/D`, recorded as printed.**
The `Single Pump Charts` table prints `ROR (bbl/d)` and `Q-INT (bbl/d)`; `Best Efficiency` and `Operating Range` print `(STB/D)`. **Both are correct as printed and they are not the same quantity** — one is downhole, one is stock-tank. Capture `flow_unit_as_printed` on every flow value. Never reconcile at extraction (D8).

**T5 — The `*` marker is data.**
`* denotes corrected viscosity rate` (`F1-51`). Any rate carrying the asterisk is viscosity-corrected and **must not** enter a water-basis fit unlabelled. Set `viscosity_corrected = true` on the affected observation rows and record which rows carried the marker.

**T6 — Bubble point moves with the scenario.**
`Oryx Roan` prints `Bubble Point 1,800 PSIA` in its `1000 pip` scenario and `1,200 PSIA` in its `500 pip` scenario, tracking `Static Datum Pressure` exactly. Emit bubble point **per scenario** (`F1-24`) alongside `Static Datum Pressure` (`F1-26`). Do not collapse, do not pick. The D16 load rule in [[m2-extraction-method]] handles it.

**T7 — Bubble-point unit is required, not optional.**
F1 prints **`PSIA`**. Emit `bubble_point_unit_as_printed = 'PSIA'` on every row. A null unit is a validator hard failure (`V-04`): the diagnostic's bands are ±10 % and 14.7 psi sits inside the decision margin on a low-pressure well.

**T8 — Depth has four printed candidates. Do not merge them.**
`Intake Depth MD (PSD)` (`F1-13`), `Intake Depth (TVD)` (`F1-14`), `Intake Set Depth` (`F1-17`), `Bottom of Equipment MD` (`F1-16`), `Top of Perfs MD/VD` (`F1-18`). Each goes to its own column. `depth_reference_basis = 'intake'` for this family — printed literally, not inferred.

**T9 — Motor amperage is present on the page and is out of scope.**
`F1-07` and `F1-38` name Horsepower / Voltage / Amperage together because they are printed together. Extract HP and Volts; **omit Amperage** (D2 — duplicates `esp_well_configuration_v2.motor_rated_amps`). This is one of five composite inventory rows with an internal exclusion; the others are `F1-38`, `F3-38`, `F4-18`, `F7-12`, `F7-42`.

---

## 7. Library-held models — do not re-extract the head curve

MC converted 13 models and they are the **source of truth** for the 60 Hz head curve and the BEP / ROR envelope:

`400DAL650H · 400DAL1200 · 400DAL1200H · 400DAL1750H · 400DAL3000H · 400DAL4300H · SD2000 · SF900 · SF1750 · SF2700 · SF4300 · SFGH2500 · SFGH4300`

**This family hits the list — `SF1750`, `SF2700`, `SF4300`, `SFGH2500`, `SFGH4300`, `SF900` and `SD2000` are all Summit models.** Rule:

- Still emit the observation row. Extraction does not skip.
- Set `library_row_exists = true` and `curve_observation_role = 'cross_check'` on every **head, BEP and ROR** row whose model matches the 13.
- Set `curve_observation_role = 'contribution'` on **power and efficiency** rows for the same models — MC left `ideal_power_c1…c6` null, so power and efficiency are exactly what these documents uniquely add (amendment 8).
- M5/M6 must never let a `cross_check` row overwrite a library head-curve or envelope value.

`SF3550` (R1 token count 316) and `SF5800` (90) are **not** in the 13 and are genuine extraction targets — their head curves are a real contribution.

---

## 8. Fields to extract

**57 fields.** Every one is a `proposed = yes` row of `m1-field-inventory.csv` with `family = SpyGlass`. No field outside this table may be extracted.

| ID | Field, as printed | Page | Cardinality | Class (D14) | Trace | Target |
|---|---|---|---|---|---|---|
| `F1-01` | Well | p1 SIZING REPORT | `per_document` | vendor input | join key -> well_id (all contracts) | *(join key — all four)* |
| `F1-02` | Sizing | p1 SIZING REPORT | `per_document` | vendor input | D31 alias evidence for pump_model_canonical | `stg_alias_evidence` |
| `F1-03` | Date Generated | p1 SIZING REPORT | `per_document` | vendor input | D18 effective_from | *(join key — all four)* |
| `F1-04` | PUMPS / HOUSINGS Pn <housings> / <stages> <manufacturer> / <model> | p2-4 Design Schematic | `per_section` | vendor input | C-NARROW + C-BEP + C-IDEAL + K-TIER1 | `stg_pump_config` + `stg_alias_evidence` |
| `F1-05` | stages (per Pn row) | p2-4 Design Schematic | `per_section` | vendor input | C-IDEAL (stages) + K-TIER1 | `stg_pump_config` |
| `F1-06` | stages (string total next to PUMPS / HOUSINGS) | p2-4 Design Schematic | `per_document` | vendor derived | K-TIER1 cross-check on section stage sum | `stg_pump_config` |
| `F1-07` | MOTOR Horsepower / Voltage / Amperage (catalog) | p2-4 Design Schematic | `per_document` | vendor input | C-BHP nameplate context; amperage excluded (duplicates v2 motor_rated_amps) | `stg_design_context` |
| `F1-08` | Surface Rate (STB/D) | p2-4 Design Schematic | `per_scenario` | vendor input | C-BEP (design flow) + K-CURVEFIT scenario anchor | `stg_curve_observations` |
| `F1-09` | OIL / WATER (split of surface rate) | p2-4 Design Schematic | `per_scenario` | vendor input | C-SG (water cut basis) | `stg_design_context` |
| `F1-10` | Discharge Pressure | p2-4 Design Schematic | `per_scenario` | vendor derived | C-DISCH benchmark (D13) | `stg_design_context` |
| `F1-11` | Total Dynamic Head (TDH) | p2-4 Design Schematic | `per_scenario` | vendor derived | K-TIER1 / Test A benchmark | `stg_design_context` + `stg_curve_observations` |
| `F1-12` | Free Gas into Pump | p2-4 Design Schematic | `per_scenario` | vendor derived | K-TIER2P (string-level alpha) | `stg_design_context` |
| `F1-13` | Intake Depth MD (PSD) | p2-4 Design Schematic | `per_document` | vendor input | C-HYD + C-DPREC (well_depth_ft) | `stg_design_context` |
| `F1-14` | Intake Depth (TVD) | p2-4 Design Schematic | `per_document` | vendor input | C-HYD (true vertical column for hydrostatic) | `stg_design_context` |
| `F1-15` | Motor HP @ <f> hz | p2-4 Design Schematic | `per_scenario` | vendor derived | C-ENERGY benchmark (D13) | `stg_design_context` |
| `F1-16` | Bottom of Equipment MD | p2-4 Design Schematic | `per_document` | vendor input | C-HYD depth-provenance disambiguation (intake vs bottom vs perf) | `stg_design_context` |
| `F1-17` | Intake Set Depth | p5 Design Overview | `per_document` | vendor input | C-HYD + C-DPREC (well_depth_ft) | `stg_design_context` |
| `F1-18` | Top of Perfs Measured Depth / Vertical Depth | p5 Design Overview | `per_document` | vendor input | C-HYD depth-provenance disambiguation | `stg_design_context` |
| `F1-19` | Oil API | p5 Design Overview | `per_scenario` | vendor input | C-SG (sg_oil) + C-BUBBLE (API) | `stg_design_context` |
| `F1-20` | Water Specific Gravity | p5 Design Overview | `per_scenario` | vendor input | C-SG (sg_water) | `stg_design_context` |
| `F1-21` | Gas Specific Gravity | p5 Design Overview | `per_scenario` | vendor input | C-BUBBLE (gamma_g - replaces the 0.75 default) | `stg_design_context` |
| `F1-22` | Oil Rate / Water Rate / Total Liquid Rate | p5 Design Overview | `per_scenario` | vendor input | C-SG (water cut) + C-BEP design flow | `stg_design_context` + `stg_curve_observations` |
| `F1-23` | Water Cut | p5 Design Overview | `per_scenario` | vendor derived | C-SG (water_cut) | `stg_design_context` |
| `F1-24` | Bubble Point | p5 Design Overview | `per_scenario` | vendor assumption | C-BUBBLE (selected_bubble_point_psi) | `stg_design_context` |
| `F1-25` | Datum Point | p5 Design Overview | `per_scenario` | vendor input | C-BUBBLE pressure-reference provenance | `stg_design_context` |
| `F1-26` | Static Datum Pressure | p5 Design Overview | `per_scenario` | vendor assumption | C-BUBBLE provenance test (bubble point == static datum tell) | `stg_design_context` |
| `F1-27` | Surface Temperature | p5 Design Overview | `per_scenario` | vendor input | C-BUBBLE (T_f context - replaces the 150 F default with a gradient endpoint) | `stg_design_context` |
| `F1-28` | Operating Frequency | p6 Theoretical Production Data | `per_scenario` | vendor input | K-AFFNORM (D10 as-printed frequency) | `stg_design_context` + `stg_curve_observations` |
| `F1-29` | Total System Efficiency | p6 Theoretical Production Data | `per_scenario` | vendor derived | C-EFFPROXY + C-ENERGY benchmark (D13) | `stg_design_context` |
| `F1-30` | Name (Pumps page) | p7 Pumps | `per_section` | vendor input | C-NARROW + D31 model grammar | `stg_pump_config` + `stg_alias_evidence` |
| `F1-31` | Stages (Pumps page) | p7 Pumps | `per_section` | vendor input | C-IDEAL (stages) + K-CURVEFIT per-stage normalization | `stg_pump_config` + `stg_curve_observations` |
| `F1-32` | Q at Intake / Q at Discharge | p7 Pumps | `per_section` | vendor derived | K-TIER1 (equal-flow assumption check across sections) | `stg_curve_observations` |
| `F1-33` | Pump Efficiency | p7 Pumps | `per_section` | vendor derived | C-EFFPROXY benchmark + K-CURVEFIT (efficiency at design point) | `stg_curve_observations` |
| `F1-34` | % Free Gas at Inlet / % Free Gas at Discharge | p7 Pumps | `per_section` | vendor derived | K-TIER2P (per-section alpha) | `stg_curve_observations` |
| `F1-35` | Operating HP @ Design Hz | p8 Motor | `per_scenario` | vendor derived | C-ENERGY benchmark (D13) | `stg_design_context` |
| `F1-36` | Motor Volts | p8 Motor | `per_scenario` | vendor derived | C-BHP context (operating voltage behind the amp x volt proxy) | `stg_design_context` |
| `F1-37` | Motor Catalog HP Rating | p8 Motor | `per_document` | vendor input | C-ENERGY nameplate context | `stg_design_context` |
| `F1-38` | Motor Catalog Volts Rating | p8 Motor | `per_document` | vendor input | C-BHP nameplate context | `stg_design_context` |
| `F1-39` | Motor Input HP | p8 Motor | `per_scenario` | vendor derived | C-ENERGY benchmark (electrical-to-shaft ratio) | `stg_design_context` |
| `F1-40` | System Power @ Wellhead | p8 Motor | `per_scenario` | vendor derived | C-ENERGY (motor_power_kw benchmark for the proxy path) | `stg_design_context` |
| `F1-41` | Motor Power Factor | p8 Motor | `per_scenario` | vendor derived | C-BHP (replaces default pf = 0.90) + C-AFFINITY power check | `stg_design_context` |
| `F1-42` | Motor Efficieny Ratio | p8 Motor | `per_scenario` | vendor derived | C-ENERGY benchmark (motor efficiency term) | `stg_design_context` |
| `F1-43` | Frequency (Hz) / Liquid Flow / TDH (Variable Frequency Analysis) | p9-11 Multi-Frequency Head Curve | `per_scenario` | vendor derived | K-AFFNORM + K-CURVEFIT (composite head at 6 frequencies) | `stg_curve_observations` |
| `F1-44` | Operating HP Required (BHP Variable Frequency Analysis) | p12-14 Multi-Frequency BHP Curve | `per_scenario` | vendor derived | K-CURVEFIT (power at 6 frequencies - the design docs unique contribution per amendment 8) | `stg_curve_observations` |
| `F1-45` | Pump (Single Pump Charts table) | p15-17 Single Pump Charts | `per_section` | vendor input | D31 model grammar | `stg_alias_evidence` + `stg_curve_observations` |
| `F1-46` | ROR (bbl/d) | p15-17 Single Pump Charts | `per_section` | vendor derived | K-WINDOW (D29 intersection vs vendor value) + C-BEP | `stg_curve_observations` |
| `F1-47` | Q - INT (bbl/d) / Q - DIS (bbl/d) | p15-17 Single Pump Charts | `per_section` | vendor derived | K-TIER1 equal-flow check | `stg_curve_observations` |
| `F1-48` | Free Allowed Gas | p15-17 Single Pump Charts | `per_section` | vendor derived | recorded under its own name as a model-level property (D12) - explicitly NOT mapped to NPSHr | `stg_curve_observations` |
| `F1-49` | Lift (ft) | p15-17 Single Pump Charts | `per_section` | vendor derived | K-CURVEFIT (head per section; / stages gives ft/stage) + K-TIER1 | `stg_curve_observations` |
| `F1-50` | Density @ INT (lb/ft3) / Density @ DIS (lb/ft3) | p15-17 Single Pump Charts | `per_section` | vendor derived | C-SG benchmark (calc_mixture_sg check at section conditions) | `stg_curve_observations` |
| `F1-51` | * denotes corrected viscosity rate | p15-17 Single Pump Charts | `per_section` | vendor derived | K-CURVEFIT quality flag (viscosity-corrected observations must not enter a water-basis fit) | `stg_curve_observations` |
| `F1-52` | Best Efficiency (STB/D) Value | p15-17 Single Pump Charts | `per_scenario` | vendor derived | C-BEP (bep_bpd) + K-CURVEFIT | `stg_curve_observations` |
| `F1-53` | Operating Range (STB/D) Min / Max | p15-17 Single Pump Charts | `per_scenario` | vendor derived | C-BEP (min/max_recommended_bpd) + K-WINDOW | `stg_curve_observations` |
| `F1-54` | ROR Min / ROR Bep / ROR Max (Flow Rate + Head pairs) | p18 Multiscenario Pump Curve | `per_scenario` | vendor derived | Test A composite benchmark + K-CURVEFIT (3 composite points per scenario) | `stg_curve_observations` |
| `F1-55` | Multifrequency Tapered Pumps: per-section Intake/Discharge Flow Rate + TDH | p19-21 Multifrequency Tapered Pumps | `per_section` | vendor derived | K-TIER1 (the vendor performing the composite decomposition) + K-CURVEFIT | `stg_curve_observations` |
| `F1-56` | Motor Performance page (Efficiency / Power Factor / Percent Current / Speed RPM vs % Load) | optional page (Oryx Roan p20) | `per_scenario` | vendor derived | C-BHP (power factor at load) + C-ENERGY | `stg_design_context` |
| `F1-57` | Correlations List (Bubble Point / Solution GOR / Oil FVF / Oil Density / Water FVF / Gas Compressibility / Tubing Flow / viscosities / Natural Gas Separation) | optional page (Oryx Roan p31-32) | `per_scenario` | vendor assumption | C-BUBBLE (names the correlation behind the printed bubble point - benchmarks the apps Standing implementation) | `stg_design_context` |

---

##### Amended 2026-08-18 — five fields with no destination column

Sonnet, running F1 with the schemas inlined, found five proposed fields whose contract target column does not exist in `stg_design_context`, and quietly wrote them into report prose instead. Prose is invisible to the loader, so those values were neither extracted nor recorded as absent. Resolved as follows rather than by adding columns.

**Four fields dropped, recorded in Not extracted (D22).** All four are **vendor derived** (D14) — outputs of the sizing program, not inputs to it — and D13 admits a derived field only where it benchmarks a named calculation of ours. None does:

| Field | Why dropped |
|---|---|
| `Oil Rate` / `Water Rate` (raw split) | Redundant — `water_cut_design_frac` carries the ratio, the design point carries the total. |
| `Motor Volts` (operating, per scenario) | Motor-side. `motor_nameplate_volts` exists and is the input; the operating value is a vendor computation, and the motor is outside the tapered-pump calculator's scope. |
| `Motor Input HP` | As above. `motor_nameplate_hp` is the input. |
| `Operating HP at design frequency` | Duplicates the BHP curve, which already carries operating power per frequency at a finer grain. |

**One field re-mapped, not dropped.** `Discharge Pressure` moves from `stg_design_context` to **`stg_curve_observations`**. Intake and discharge pressure bracket the pump's ΔP, which is what Test A checks against composed head — so it is a benchmark value (D13) at the observation grain, and its target column already exists. **A mis-mapping in the contract, not a missing column.**

> [!warning] **The general rule this establishes — applies to every family, not just F1.**
> A field whose target column does not exist is **never** written into report prose. Prose is not a data destination: a value there is invisible to the loader *and* absent from the not-extracted list, so it is neither extracted nor recorded as missing — the worst of the three outcomes, because nothing downstream can detect it.
> Where a target column is missing, the extractor **names the field, its contract-stated target, and the fact that the column is absent**, under a dedicated `## Schema gaps` heading in Extraction Notes. It does not invent a column and does not bury the value in prose.
> The gap is then resolved **in the contract**, by exactly one of three moves: **drop** with a D22 reason · **re-map** to an existing column · **escalate** for a schema change.
> This is the second instance of the pattern. See **CF-16** — `stg_gas_cascade`, 19 fields, found by M2 on 2026-08-14, still open and blocking M5's load.

## 9. Target tables and cardinality

Extraction writes to **staging**. M5 owns every collapse, normalization and division.

| Staging target | Grain emitted by this contract | Destination |
|---|---|---|
| `stg_pump_config` | section × scenario | `pump_config` (well × section × epoch) after the **D17** collapse |
| `stg_design_context` | scenario | `esp_well_design_context` (well) after the **D16** selection |
| `stg_curve_observations` | model × point × scenario | `curve_observations` (model × point) |
| `stg_gas_cascade` | cascade stage × scenario | ⚠️ **no destination column yet — schema gap G1** |
| `stg_alias_evidence` | printed string | the **D31** lookup (a component, not a table) |

---

## 10. Family validator rules

Global rules `V-01` … `V-20` in [[m2-validator-spec]] apply to every family. The family-specific rules for F1 are `V-F1-*` in that document's §4. Pass B runs both sets.

---

## Not extracted

Per **D22**, the not-extracted list for this family lives in `m1-field-inventory.csv` (`family = SpyGlass`, `proposed = no`, 54 rows, each with a one-line reason) and in the R1 template atlas. It is **not** restated here and **not** restated per document. A per-document report notes only *anomalies against this contract*.

Corpus-wide exclusions that apply here without exception: **PI** and **PIP** (D11) · **NPSHr** — no mapping from any field, including `Free Allowed Gas` (D12) · motor amperage (D2, duplicates `esp_well_configuration_v2.motor_rated_amps`) · producing GOR/GLR · cable / VSD / transformer / seal / sensor selection strings · design-time tubing and casing setpoints.

---

## Log

| Date | Update |
|---|---|
| 2026-08-14 | Contract issued at M2. Not yet run — M3 pilots it on `HALEY NE I 154H DESIGN.pdf`. |

| 2026-08-16 | **Contract amended in chat (not by executor).** §1 — `Summit ESP Representative` demoted from required to recorded-if-present (CF-19); it HALTed 5 of this family's 30 documents. §2 — continuation-sheet rule added (CF-20); untitled pages are scenario continuations and the PREP manifest names what each continues. |

| 2026-08-18 | **Five destination-column gaps resolved (§8).** Four vendor-derived fields dropped with D22 reasons — oil/water raw split, operating motor volts, motor input HP, operating HP at design frequency. `Discharge Pressure` re-mapped from `stg_design_context` to `stg_curve_observations` as a Test A benchmark value. **General rule established:** a field with no destination column is reported under `## Schema gaps`, never written into prose, and resolved in the contract by drop / re-map / escalate. Second instance of the pattern after CF-16. |
