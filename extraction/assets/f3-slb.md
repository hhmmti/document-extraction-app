---
title: F3 SLB — Extraction Contract
created: 2026-08-14
status: corrected 2026-08-20 against the staging-schema audit — M4 runs this version
milestone: M2
family: F3
vendor_family: slb
documents: 5
proposed_fields: 39
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - contract
  - slb
related:
  - "[[m2-extraction-method]]"
  - "[[m1-extraction-spec]]"
  - "[[design-doc-extraction-plan]]"
  - "[[design-doc-extraction-kickoff]]"
---

# F3 SLB — Extraction Contract

> [!note] **One of seven. Not interchangeable with any other contract.** The umbrella is [[m2-extraction-method]]; the field record is `m1-field-inventory.csv` (39 rows with `proposed = yes`, family `SLB`); the decisions are locked in [[design-doc-extraction-kickoff]]. Extraction is **verbatim** (D8) — no unit conversion, no arithmetic, no canonicalization, no conflict resolution. Every transform belongs to M5.

## 0. Applicability

**Vendor:** SLB / REDA. **Tool:** ESPdesign. **Documents: 5.**

`ESPD_Permian Resources_Cheddar 502H_…_RC1000 (4) & 406 MOTOR_202512.pdf` · `PERMIA~1.PDF` · `Permian Resources_BRAVE STATE 132H_Schematics & General Report_202603.pdf` · `Permian Resources_Midway 45-46 Unit 2H_…_202511.pdf` · `Permian Resources_Midway 45-46 Unit 3H_…_202511.pdf`

> **`PERMIA~1.PDF` is in scope and is a normal F3 document.** It is an 8.3 short filename, which is why R1's token extraction fell back to the body and produced `MID STATES EAST UNIT 37 5 6D`. Page 1 reads `Company: Permian Resources`, `Project: Mid-States East Unit 37-5 6D`, same SLB engineer as `Midway 2H`. The `6D` suffix is real. **Its `well_id` join must use the body-sourced well name, never the filename** — set `well_name_source = 'document_body'` on this document.

> **This contract must not be merged with F5 Baker.** Both carry the `ESPD_` filename prefix; that prefix spans two vendors and is not a family signal.

---

## 1. Page-1 signature — confirm before extracting

Page 1 must show **all** of:

- the document title **`Schematics Report`**
- an **`SLB Engineer:`** field
- an **`SLB | … Report`** footer

If page 1 reads `ProLift Summary Report` with an `ESP B …` model, this is **F5 Baker** — HALT and route to `f5-baker.md`.

---

## 2. Pages

**Required spine:**

| Page | Title | What it carries |
|---|---|---|
| p1 | `Schematics Report` | the `Item / Description / Length / Top Depth / Bottom Depth` table — **the section source** |
| p4–7 | `General Report` | `Input Data`, `Equipment and Results`, `Conditions at Operating Frequency` |

**Optional:**

- **`Case Comparison Report` (p2–3)** — `Initial / Future / Max` scenario columns. **Present on `Midway 2H` (14 p), absent on `BRAVE STATE 132H` (11 p)**, which goes `Schematics → General Report` directly. Its absence is normal; it means the document is single-scenario and rows `F3-05` … `F3-24` come from the `General Report` instead. Record `case_comparison_present = false`.
- Seven **chart-only** pages: `Inflow Performance`, `Inflow/Outflow`, `TDH Curve`, `VSD H-Q Curve`, `Actual Bottom Pump Curve`, `Actual Top Pump Curve`, **`Catalog Top Pump Curve`**.

> **`Catalog Top Pump Curve` is the only catalog-basis curve SLB prints** and is this family's strongest D7 digitization candidate. Do not read numbers off it — record `digitization_candidate_page = 'Catalog Top Pump Curve'` and move on.

---

## 3. Sections — pump bodies only

Primary source: the **p1 `Item / Description / Length / Top Depth / Bottom Depth`** table. Rows where `Item = Pump` are sections (`F3-02`); `AGH` / `MGH` rows are **gas handlers and are pump bodies** (`F3-03`).

Cross-read against **p6 `Equipment and Results` → `Bottom / Top Pump Information`** (`F3-33`) and, when present, **p2 `Case Comparison` → `Pump (Bottom) / Pump (Top) Information`** (`F3-18`, `F3-19`). All must name the same models.

Never a section row: `Casing`, `Producing Perforations`, `Tubing`, gas **separators**, seals, sensors, motor. `F3-04` and `F3-36` name those rows because the table contains them; they are **depth-provenance and gas-cascade context only**.

**`Bottom` = deepest = `section_order` 1.** SLB names its pumps `Bottom` and `Top` explicitly, which makes this family the easy one — but take the order from the **`Top Depth` / `Bottom Depth` columns**, and confirm the naming agrees. Where the label and the depths disagree, **FLAG**; do not choose.

> This is Track 1's exact failure mode. `bottom_md_ft` must be **measured depth from the wellhead**, taken from the table's own `Bottom Depth` column — **not** a running tally down the assembly. If the deepest section's `bottom_md_ft` is a small number (tens or hundreds of feet) on a well whose intake depth is ~9,600 ft, the wrong column was read. Sanity band: the deepest pump body's `bottom_md_ft` should sit within a few hundred feet of `Intake Depth`.

---

## 4. Model grammar

```
^REDA\s+\d{3}\s+[A-Z]{1,3}\d{3,4}$
```

Observed: `REDA 400 RC1000`, `REDA 400 DN1750`. Manufacturer, series and model as three space-separated parts of one printed string. Capture the **whole string** verbatim.

### `Staging Configuration` — decided: **not part of the model identity**

`Staging Configuration CR-CT` vs `C-CT` distinguishes build variants of one model. **Decision: it is a build attribute, not identity.**

- It goes to its own column, `staging_configuration_as_printed` (`F3-34`), on `stg_pump_config`.
- `pump_model_as_printed` and the D31 `proposed_canonical` **ignore it**.
- **Why:** it describes how the same hydraulic model is assembled (compression vs floater staging and its thrust path), not what curve the stage produces. Folding it into identity would fragment the D31 lookup, split one model's observations into two under-populated fits, and block the join to any library row — for a distinction the head curve does not see.
- **Why it is still carried:** it changes the shaft and thrust rating, so a later mechanical check needs it, and losing it at extraction would be unrecoverable.

If a future fit shows two staging configurations of one model with materially different head, this decision reopens — trigger recorded in the umbrella's fail-loud section, owner **Hamed at M6**.

---

## 5. Scenarios

Where `Case Comparison Report` is present, the document is **multi-scenario** with printed columns `Initial / Future / Max`. Per-section blocks repeat per scenario, so the true grain of `F3-18` … `F3-22` is **section × scenario**.

**Design-scenario selector for this family: `Initial`.** It is the as-installed design case; `Future` and `Max` are forward look-aheads at conditions that do not exist yet. Set `is_design_scenario = true` on `Initial`, `scenario_selection_rule = 'named_case_initial'`. Where `Case Comparison` is absent, the `General Report` values are the single scenario.

---

##### `pump_config` grain — `model_grouped` *(CF-28, stated 2026-08-20)*

Emit **one row per distinct model per scenario**, not one row per housing. `housing_count` and `stages_basis = group_total` make the expansion recoverable; `stages_per_housing_as_printed` carries the per-housing counts.

SLB names its bodies `Bottom` and `Top`, which are two distinct models, so grouped and per-section coincide in the ordinary case — state it anyway. **Where housings are not uniform, emit every printed count in section order** (`"73, 73, 42"`), never a modal value: F2's GOUDA prints `42 + 93×4 = 414`, and taking the dominant `93` loses a housing and makes `V-09` fail for a reason that is not a real disagreement.

## 6. Traps — mandatory guardrails

**T1 — SLB's bubble point is computed and can be nonsense.**
`Midway 45-46 Unit 2H` prints `Bubble Point 19985.3 psig` against `GOR 12485.71 SCF/STB`. `PERMIA~1` prints a plausible `2130 psig` at `GOR 2788.46`. Classified **`vendor derived`** for this family, not `vendor assumption`. Extract the printed value verbatim — **do not correct it, do not null it** — and let validator rule `V-12` range-check it (`0 < Pb ≤ 10,000 psi` plausible; above that, `needs_review` and `bubble_point_plausible = false`). A derived-from-GOR bubble point above 10,000 psi must never upgrade `C-BUBBLE` silently.

**T2 — Bubble-point unit is `psig` here.**
Not `psi`, not `PSIA`. Required field (`V-04`). `psig` vs `PSIA` is a 14.7 psi offset and the diagnostic's bands are ±10 % — on a low-pressure well that is inside the decision margin. Record the token, convert nowhere.

**T3 — `Mixture Gradient (psi/ft)` is a benchmark and must not become an input.**
`Midway 2H` prints `0.433 psi/ft` while its own `Water Cut 93 %` and `Water Spec. Gravity 1.1` imply ≈ `0.464`. `PERMIA~1` prints `0.407`, so it is **not** a hardcoded constant — it varies. Leading hypothesis: it is the **flowing** mixture gradient in the tubing, including free gas, not the pumped-liquid gradient.
- Emit to `vendor_mixture_gradient_psi_per_ft` (`F3-32`) with `role = 'benchmark'`.
- **Never** feed it to `C-HYD`, and never back an SG out of it.
- Pass B computes the SG-implied gradient from this document's own `Oil Gravity` / `Water Spec. Gravity` / `Water Cut` and records the delta as a **finding** (`V-F3-2`), not a correction. `|delta| > 5 %` → `F3-GRADIENT-DIVERGENCE`.
- Resolution deferred to **M6, owner Hamed**, once all 5 documents are in hand. n=2 cannot distinguish "vendor default" from "different mixture basis"; n=5 with a spread of water cuts can.

**T4 — No power factor anywhere in this family (0/5).**
`C-BHP` cannot be upgraded from F3. This is a **finding, not a gap in the contract** — do not go looking for one, do not substitute `Load Factor` or `Slip`, both of which are motor detail with no contract. Set `motor_power_factor = null` and `power_factor_absent_confirmed = true`.

**T5 — No curve numbers anywhere (0/5 on BEP, 0/5 on ROR).**
Seven pages are chart-only. F3 contributes `pump_config` and `esp_well_design_context` only, plus per-section `Required Power` (`F3-20`) and `Pump Efficiency` (`F3-21`) as observations. **Emit no head observations.** Set `envelope_status = 'absent'`.

**T6 — `Comments / Comments-Purpose` carries vendor overrides and is data.**
`F3-05` — e.g. *separation efficiency lowered to 60 %*. Capture the text verbatim. It is the only place this family records that a printed value was overridden by the engineer, which is a D14 provenance signal for `C-SG` and `C-BUBBLE`.

**T7 — `Operation Speed` (RPM) is a cross-check on the printed frequency, not a second frequency.**
`F3-22`. Emit as RPM with its unit. Do not convert to Hz (D8); do not use it for `K-AFFNORM` where `Operating Frequency` is printed.

**T8 — Motor amperage: `F3-38` names `Volts / Power / Speed / Rating Factor / Winding Number`. `Amp` is excluded (D2). Do not extract it.**

---

##### Trap corrections — 2026-08-20 *(schema audit; originals above left standing)*

The traps are right; three of them named keys that did not exist or were the wrong kind of output. Corrected here rather than rewritten above, per the standing convention.

**T1 — `bubble_point_plausible` is validator output, not extraction output.** The extractor's job ends at emitting `19985.3 psig` verbatim. `V-12` range-checks it and records the flag in the validation findings; M5 can recompute it from the value at any time. Do not emit the key from Pass A.

**T4 — `power_factor_absent_confirmed` is not a column, and `motor_power_factor = null` is not how absence is recorded.** A proposed field absent from the document goes under ***Fields not found***, which is exactly the checked-and-absent record T4 wants. Under the JSONL rules an absent value is an **omitted key** — never `null`, `""` or `"-"`. The finding that F3 has no power factor at 0/5 stands unchanged.

**T7 — the trap is now implementable.** `speed_rpm_as_printed` exists on `stg_curve_observations` (added by the F3 audit, 2026-08-18). Before it, the only way to store `Operation Speed` was the Hz conversion T7 forbids. Emit RPM with its unit token into that key.

**§2 — `case_comparison_present` is not a column either.** The report frontmatter's `pages_present` already carries it, and `scenario_ordinal` carries the consequence. Record the absence in *Extraction notes*.

## 7. Library-held models — do not re-extract the head curve

`400DAL650H · 400DAL1200 · 400DAL1200H · 400DAL1750H · 400DAL3000H · 400DAL4300H · SD2000 · SF900 · SF1750 · SF2700 · SF4300 · SFGH2500 · SFGH4300`

**No current F3 model matches** — `RC1000` and `DN1750` are REDA and are absent from the 13. Run the check anyway on every section, matching by D31 `proposed_canonical`.

> **Corrected 2026-08-20 (schema audit).** This section previously instructed the extractor to set `library_row_exists = true` and to mark head rows `curve_observation_role = 'cross_check'` and power rows `'contribution'`. **Both instructions were wrong:**
>
> - **`library_row_exists` is a join computed at load**, not an extracted field. Extracting it bakes a point-in-time answer into the record, which goes stale the moment MC's library changes. Pass A must not emit it.
> - **`cross_check` and `contribution` are not values in the enum.** `curve_observation_role` is `head_curve | bhp_curve | single_pump_chart | multiscenario | tapered_composite | design`. Library precedence is an M5/M6 weighting concern, not a staging role.
>
> The same two errors sit in `f6-xsize.md` §7 and `f7-valiant.md` §7. Given T5, F3 emits no head rows at all, so the practical effect here is confined to the per-section `Required Power` and `Pump Efficiency` observations — which carry `curve_observation_role = 'design'`.

## 8. Fields to extract

**39 fields.** Every one is a `proposed = yes` row of `m1-field-inventory.csv` with `family = SLB`. No field outside this table may be extracted.

| ID | Field, as printed | Page | Cardinality | Class (D14) | Trace | Target |
|---|---|---|---|---|---|---|
| `F3-01` | Company / Well Number / Date | p1 Schematics Report header | `per_document` | vendor input | join key -> well_id + D18 effective_from | *(join key — all four)* |
| `F3-02` | Item / Description / Length / Top Depth / Bottom Depth table - Pump: <model> <n> stages rows | p1 Schematics Report | `per_section` | vendor input | C-NARROW + C-IDEAL + K-TIER1 (section order and stage counts) + D31 model grammar | `stg_pump_config` + `stg_alias_evidence` |
| `F3-03` | Item table - AGH / MGH rows | p1 Schematics Report | `per_section` | vendor input | K-TIER1 (gas handler is a head-producing body) + K-TIER2P | `stg_pump_config` + `stg_alias_evidence` |
| `F3-04` | Item table - Casing / Producing Perforations / Tubing rows | p1 Schematics Report | `per_document` | vendor input | C-HYD depth-provenance disambiguation (perf depth vs intake depth) | `stg_design_context` |
| `F3-05` | Comments / Comments-Purpose | p2 Case Comparison | `per_scenario` | vendor assumption | C-BUBBLE and C-SG provenance (records vendor overrides such as separation efficiency lowered to 60%) | `stg_design_context` |
| `F3-06` | Bottomhole Temperature | p2 Case Comparison | `per_scenario` | vendor input | C-BUBBLE (T_f - replaces the 150 F default) | `stg_design_context` |
| `F3-07` | Oil Gravity | p2 Case Comparison | `per_scenario` | vendor input | C-SG (sg_oil) + C-BUBBLE (API) | `stg_design_context` |
| `F3-08` | Water Spec. Gravity | p2 Case Comparison | `per_scenario` | vendor input | C-SG (sg_water) | `stg_design_context` |
| `F3-09` | Water Cut | p2 Case Comparison | `per_scenario` | vendor input | C-SG (water_cut) | `stg_design_context` |
| `F3-10` | Intake Depth | p2 Case Comparison | `per_scenario` | vendor input | C-HYD + C-DPREC (well_depth_ft) | `stg_design_context` |
| `F3-11` | Design Rate / Operation Rate | p2 Case Comparison | `per_scenario` | vendor input | C-BEP design flow | `stg_curve_observations` |
| `F3-12` | Total Rate at Inlet / Liquid Rate at Inlet / Gas Rate into Pump | p2 Case Comparison | `per_scenario` | vendor derived | K-CURVEFIT (downhole flow at the design point) | `stg_curve_observations` |
| `F3-13` | Inlet Gas Volume Fraction / Gas Volume Fraction at Intake | p2 Case Comparison | `per_scenario` | vendor derived | K-TIER2P (alpha entering the pump) | `stg_design_context` |
| `F3-14` | Natural Separation Efficiency / Total Separation Efficiency | p2 Case Comparison | `per_scenario` | vendor derived | K-TIER2P (separation stages in the alpha cascade) | `stg_gas_cascade` ⚠️G1 |
| `F3-15` | Total Dynamic Head | p2 Case Comparison | `per_scenario` | vendor derived | K-TIER1 / Test A benchmark | `stg_design_context` + `stg_curve_observations` |
| `F3-16` | Discharge Pressure | p2 Case Comparison | `per_scenario` | vendor derived | C-DISCH benchmark (D13) | `stg_design_context` |
| `F3-17` | Operating Frequency | p2 Case Comparison | `per_scenario` | vendor input | K-AFFNORM (D10 as-printed frequency) | `stg_design_context` + `stg_curve_observations` |
| `F3-18` | Pump (Bottom) / Pump (Top) Information: Type | p2 Case Comparison | `per_section` | vendor input | C-NARROW + D31 model grammar (REDA 400 <model> form) | `stg_pump_config` + `stg_alias_evidence` |
| `F3-19` | Pump (Bottom) / Pump (Top) Information: Number of Stages | p2 Case Comparison | `per_section` | vendor input | C-IDEAL (stages) + K-TIER1 | `stg_pump_config` + `stg_curve_observations` |
| `F3-20` | Pump (Bottom) / Pump (Top) Information: Required Power | p2 Case Comparison | `per_section` | vendor derived | K-CURVEFIT (per-section power - the design docs unique contribution per amendment 8) | `stg_curve_observations` |
| `F3-21` | Pump (Bottom) / Pump (Top) Information: Pump Efficiency | p2 Case Comparison | `per_section` | vendor derived | C-EFFPROXY benchmark | `stg_curve_observations` |
| `F3-22` | Pump (Bottom) / Pump (Top) Information: Operation Speed | p2 Case Comparison | `per_section` | vendor derived | K-AFFNORM (RPM cross-check on the printed frequency) | `stg_curve_observations` |
| `F3-23` | Motor Information (Type / Motor Horse Power / Motor Speed / Motor Voltage) | p2 Case Comparison | `per_scenario` | vendor input | C-ENERGY nameplate context | `stg_design_context` |
| `F3-24` | Total Motor Load / Load Factor / Efficiency / Slip | p3 Case Comparison | `per_scenario` | vendor derived | C-ENERGY benchmark (motor efficiency term); Slip and Load Factor not proposed | `stg_design_context` |
| `F3-25` | API Well Reg. # | p4 General Report | `per_document` | vendor input | join key hardening for well_id resolution | *(join key — all four)* |
| `F3-26` | Gas Specific Gravity | p5 Input Data | `per_document` | vendor input | C-BUBBLE (gamma_g - replaces the 0.75 default) | `stg_design_context` |
| `F3-27` | Bubble Point | p5 Input Data | `per_document` | vendor derived | C-BUBBLE (selected_bubble_point_psi) - implausible at n=1 (19985.3 psig on Midway 2H) so it must carry a validator range check | `stg_design_context` |
| `F3-28` | Wellhead Temperature / Bottom Hole Temperature | p5 Input Data | `per_document` | vendor input | C-BUBBLE (T_f) | `stg_design_context` |
| `F3-29` | Perforation Depth | p5 Input Data | `per_document` | vendor input | C-HYD depth-provenance disambiguation | `stg_design_context` |
| `F3-30` | Desired Operating Conditions (Intake Depth / Frequency / Design Rate / Wellhead Pressure / Pump Speed) | p5 Input Data | `per_document` | vendor input | C-HYD (intake depth) + K-AFFNORM (frequency); wellhead pressure not proposed | `stg_design_context` + `stg_curve_observations` |
| `F3-31` | Pumping Conditions: Production Rate / TDH / Discharge Pressure / Pump Speed | p6 Equipment and Results | `per_document` | vendor derived | K-TIER1 / Test A benchmark + C-DISCH benchmark | `stg_design_context` + `stg_curve_observations` |
| `F3-32` | Pumping Conditions: Mixture Gradient (psi/ft) | p6 Equipment and Results | `per_document` | vendor derived | C-HYD benchmark - a direct psi/ft gradient is exactly what calc_hydrostatic_pressure_psi computes from SG; ambiguous at n=2 (0.433 on Midway 2H against an SG-implied 0.464) | `stg_design_context` |
| `F3-33` | Bottom / Top Pump Information: Device Information / Stages | p6 Equipment and Results | `per_section` | vendor input | C-NARROW + C-IDEAL + K-TIER1 | `stg_pump_config` + `stg_alias_evidence` |
| `F3-34` | Bottom / Top Pump Information: Staging Configuration / Staging Type | p6 Equipment and Results | `per_section` | vendor input | D31 alias evidence (CR-CT vs C-CT distinguishes build variants of one model) | `stg_pump_config` + `stg_alias_evidence` |
| `F3-35` | Gas Handler #1 / #2 Device Information | p6 Equipment and Results | `per_section` | vendor input | K-TIER1 (head-producing body) + D31 model grammar | `stg_pump_config` + `stg_alias_evidence` |
| `F3-36` | Gas Separators (Device Information / Power / Natural Separation Efficiency / Separator 1 and 2 Efficiency / Total Separation Efficiency) | p6 Equipment and Results | `per_section` | vendor derived | K-TIER2P (separation stages in the alpha cascade) | `stg_gas_cascade` ⚠️G1 |
| `F3-37` | Free Gas Into Pump | p6 Equipment and Results | `per_document` | vendor derived | K-TIER2P (alpha entering the pump) | `stg_design_context` |
| `F3-38` | Motor Nameplate Information (Device Information / Volts / Power / Speed / Rating Factor / Winding Number) | p6 Equipment and Results | `per_document` | vendor input | C-ENERGY nameplate context; Amp excluded (duplicates v2 motor_rated_amps) | `stg_design_context` |
| `F3-39` | Conditions at Operating Frequency (Operating Frequency / Motor Amp / Total Motor Load / Volts @ Junction Box / KVA @ Junction Box / Motor Volts) | p7 General Report | `per_document` | vendor derived | C-ENERGY (total motor load); amps and kVA not proposed | `stg_design_context` |

---

##### Row corrections — 2026-08-20 *(schema audit; the table above is left standing)*

Four corrections to the targets above. The rows are not rewritten; these override them.

| Row | Printed as | Was | **Is** | Why |
|---|---|---|---|---|
| `F3-16` | `Discharge Pressure` (p2) | `stg_design_context` | **`stg_curve_observations`** → `discharge_pressure_psi_as_printed` | Intake and discharge pressure bracket the pump's ΔP, which is an **observation-grain** Test A benchmark, not a well-grain property. `f1-spyglass.md` §8 settled this on 2026-08-18 and F3 is the family that disagreed with it. |
| `F3-31` | `Pumping Conditions: … Discharge Pressure …` (p6) | both targets | `Production Rate`, `TDH` and `Pump Speed` stay as listed; **`Discharge Pressure` → `stg_curve_observations`** only | Same reasoning. The rest of the row is unaffected. |
| `F3-14` | separation efficiencies (p2) | `stg_gas_cascade` ⚠️G1 | **`stg_gas_cascade`** → `separation_efficiency_pct` | **G1 is closed** (CF-16). `stg_gas_cascade` is defined in `staging-schemas.md`; its `component_is_pump_body = true` rows resolve into `curve_observations.free_gas_at_inlet_pct` at load, and the separator stages stay in staging as the audit record. Strike the ⚠️. |
| `F3-36` | gas separators (p6) | `stg_gas_cascade` ⚠️G1 | **`stg_gas_cascade`** | Same. Strike the ⚠️. |

**Two columns the table names only in prose, both of which now exist:**

- `F3-05` `Comments / Comments-Purpose` → **`vendor_comments_as_printed`** on `stg_design_context`. Added by the F3 audit; before it, T6's vendor overrides had no destination and prose is not one.
- `F3-34` `Staging Configuration` → **`staging_configuration_as_printed`** on `stg_pump_config`. §4 named the column and the schema had no such thing until the F3 audit created it. The §8 row's `stg_alias_evidence` half stands — the printed string is D31 evidence — but the identity-bearing copy lives on `stg_pump_config`.

## 9. Target tables and cardinality

Extraction writes to **staging**. M5 owns every collapse, normalization and division.

| Staging target | Grain emitted by this contract | Destination |
|---|---|---|
| `stg_pump_config` | **model × scenario** (`model_grouped`, CF-28) | `pump_config` (well × section × epoch) after the **D17** collapse |
| `stg_design_context` | scenario | `esp_well_design_context` (well) after the **D16** selection |
| `stg_curve_observations` | model × point × scenario | `curve_observations` (model × point) |
| `stg_gas_cascade` | cascade stage × scenario | pump-body rows resolve into `curve_observations.free_gas_at_inlet_pct`; separator stages stay in staging as the audit record (**CF-16 closed**) |
| `stg_alias_evidence` | printed string | the **D31** lookup (a component, not a table) |

> **G1 is closed.** This table previously marked `stg_gas_cascade` as having no destination column. `staging-schemas.md` defines it, and `m2-validator-spec` §9's matching instruction — that M5 must not load cascade rows — is stale for the same reason and still needs correcting.

**F3 is a multi-scenario family and the only one of F2–F4 where D16 and D17 can fire.** Where `Case Comparison Report` is present the scenario grain is real, `Initial` is the design case, and a collapse is a genuine decision rather than a formality. Where it is absent the document is single-scenario and neither rule applies.

## 10. Family validator rules

Global rules `V-01` … `V-20` in [[m2-validator-spec]] apply to every family. The family-specific rules for F3 are `V-F3-*` in that document's §4. Pass B runs both sets.

---

## Not extracted

Per **D22**, the not-extracted list for this family lives in `m1-field-inventory.csv` (`family = SLB`, `proposed = no`, 24 rows, each with a one-line reason) and in the R1 template atlas. It is **not** restated here and **not** restated per document. A per-document report notes only *anomalies against this contract*.

Corpus-wide exclusions that apply here without exception: **PI** and **PIP** (D11) · **NPSHr** — no mapping from any field, including `Free Allowed Gas` (D12) · motor amperage (D2, duplicates `esp_well_configuration_v2.motor_rated_amps`) · producing GOR/GLR · cable / VSD / transformer / seal / sensor selection strings · design-time tubing and casing setpoints.

---

## Log

| Date | Update |
|---|---|
| 2026-08-14 | Contract issued at M2. Not yet run — M3 pilots it on `Permian Resources_Midway 45-46 Unit 2H_Schematic+Comparison+General Report_202511.pdf`. || 2026-08-20 | **Corrected against the staging-schema audit, ahead of the M4 F3 run.** §5 gains the `model_grouped` grain statement (CF-28) with the non-uniform-housing rule F2 forced. §6 gains trap corrections: `bubble_point_plausible` is validator output not extraction output (T1), `power_factor_absent_confirmed` is not a column and absence is an omitted key not a null (T4), and T7 is now implementable because `speed_rpm_as_printed` exists. §7 rewritten — it had told the extractor to emit `library_row_exists` (a load-time join) and to use `curve_observation_role = 'cross_check' / 'contribution'` (not enum values); the same two errors remain in `f6-xsize.md` and `f7-valiant.md` §7. §8 gains row corrections: `Discharge Pressure` moves to `stg_curve_observations` on F3-16 and F3-31, the ⚠️G1 markers on F3-14 and F3-36 are struck, and F3-05 / F3-34 are pinned to `vendor_comments_as_printed` and `staging_configuration_as_printed`. §9 rewritten for the closed cascade and the corrected grain. Original text left standing throughout; corrections override rather than replace. |
