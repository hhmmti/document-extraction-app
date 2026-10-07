---
title: F5 Baker ProLift — Extraction Contract
created: 2026-08-14
status: corrected 2026-08-20 against the staging-schema audit — M4 runs this version
milestone: M2
family: F5
vendor_family: baker_prolift
documents: 2
proposed_fields: 30
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - contract
  - baker-prolift
related:
  - "[[m2-extraction-method]]"
  - "[[m1-extraction-spec]]"
  - "[[design-doc-extraction-plan]]"
  - "[[design-doc-extraction-kickoff]]"
---

# F5 Baker ProLift — Extraction Contract

> [!note] **One of seven. Not interchangeable with any other contract.** The umbrella is [[m2-extraction-method]]; the field record is `m1-field-inventory.csv` (30 rows with `proposed = yes`, family `BakerProLift`); the decisions are locked in [[design-doc-extraction-kickoff]]. Extraction is **verbatim** (D8) — no unit conversion, no arithmetic, no canonicalization, no conflict resolution. Every transform belongs to M5.

## 0. Applicability

**Vendor:** Baker Hughes. **Tool:** ProLift. **Documents: 2.**

`ESPD_PERMIAN RESOURCES OPERATING_PRIEST STATE UNIT 233H_11-24-2025_PMM.pdf` · `ESPD_PERMIAN RESOURCES OPERATING_Ramses MIPA Unit 1H_03-10-2025(1750 Option).pdf`

> **These two print through Chrome (`Skia/PDF`) and were grouped with F1 SpyGlass by their `ESPD_` filename prefix.** That prefix spans two vendors — the other five `ESPD_`-prefixed files are F3 SLB. Neither the producer string nor the filename decides the family. **This contract must not be merged with F1 or F3.**

> **The smallest and thinnest contract: 30 proposed fields, no bubble point, no BEP, no ROR, no per-stage anything, no curve observations.** F5 contributes `pump_config` and `esp_well_design_context` only. A family carrying none of a target field *is itself the finding* — `C-BUBBLE` cannot be upgraded from F5 (0/2), and that is why the bubble-point target lands on 6/7 rather than 7/7.

---

## 1. Page-1 signature — confirm before extracting

Page 1 must show **all** of:

- the document title **`ProLift Summary Report`**
- a model in the **`ESP B …`** grammar
- the blocks `Surface Electrical`, `Surface Production Data`, `Simulation Parameters`

If page 1 reads `Schematics Report` with an `SLB Engineer:` field, this is **F3** — HALT and route to `f3-slb.md`. If it reads `SIZING REPORT` with a `Summit ESP Representative` footer, it is **F1** — HALT and route to `f1-spyglass.md`.

---

## 2. Pages

**8 pages.**

| Page | Title / contents |
|---|---|
| p1 | `ProLift Summary Report` — the base case: electrical, production, pump(s), simulation parameters, GVF cascade, motor |
| p2 | `ProLift Detailed Report` — fluids, multiphase correlation, reservoir temperature, top of perforation |
| p5 | Motor page — `Power Factor %` |
| p7 | **`Sensitivity` case table** — no SpyGlass or SLB counterpart |
| p8 | **String diagram table** — `Description / PN / Q-ty / OD / Length / Mass / Bottom` |

`Pump Performance` chart is **plot-only**; read no numbers from it. Missing p1, p2, p7 or p8 is a **HALT**.

---

## 3. Sections — pump bodies only

Two sources, both required:
- **p1 `Pump(s)`** — the summary form, `ESP B 400 1750 PK 738 STG`, string-level total stages.
- **p8 string diagram** (`F5-30`) — per-housing rows with `Q-ty`, `Length` and `Bottom` depth. This is where `housing_count`, `section_order` and `bottom_md_ft` come from.

**`Bottom` column = measured depth from the wellhead.** Take `bottom_md_ft` from it directly. Track 1's second failure was reading a running tally down the assembly instead; the deepest pump body here should sit near the ~10,046 ft string bottom, not at tens of feet.

`section_order` **1 = deepest.** Never a section row: intakes, seals, gas separators, sensors, motor, cable, tubing.

---

## 4. Model grammar — the same pump printed two ways, in one document

| Source | Example | Field |
|---|---|---|
| p1 summary | `ESP B 400 1750 PK 738 STG` | `pump_model_as_printed` |
| p8 string diagram | `ESP B 4001750 CW 6.5M HSG 123 STG PK CT HSS XA3 MTSCa HT` | `pump_model_as_printed_alt` |

```
summary : ^ESP\s+B\s+(\d{3})\s+(\d{3,4})\s+([A-Z]{2})\s+(\d+)\s+STG$
diagram : ^ESP\s+B\s+(\d{3})(\d{3,4})\s+CW\s+[\d.]+M\s+HSG\s+(\d+)\s+STG\s+.*$
```

Note the series and model **run together** in the diagram form (`4001750`) and are **space-separated** in the summary form (`400 1750`). Same pump. Emit **both strings verbatim** — reconciling them is D31's job at M5, and `m2-d31-seed-aliases.csv` seeds both.

`stages_basis`: the summary prints `738 STG` (`total_reported`); the diagram prints six housings at `123 STG` each (`per_housing`). **6 × 123 = 738.**

---

## 5. Scenarios

**Two grains, and they are not the same thing.**

- The **p1 `ProLift Summary`** is the **base case** and the design scenario.
- The **p7 `Sensitivity` table** holds per-case rows. These are sensitivities, **not** alternative equipment designs.

Emit sensitivity rows to `stg_design_context` and `stg_curve_observations` with `scenario_label_staged = <case name>` and `sensitivity_case = true`. **Never emit a `stg_pump_config` row from p7.** See §6 T4 — this is the rule that stops D17 firing spuriously on `Setting Depth`.

`scenario_selection_rule = 'p1_summary_base_case'`.

---

##### `pump_config` grain — `model_grouped` *(CF-28, stated 2026-08-20)*

Emit **one row per distinct model per scenario**, not one row per housing. Set
`section_grain = 'model_grouped'`, `housing_count`, and `stages_basis = 'group_total'`.

**§4's `stages_basis` paragraph contradicts this and is superseded.** It reads: the summary prints `738 STG` (`total_reported`), the diagram prints six housings at `123 STG` each (`per_housing`). That is two bases for one model. Under `model_grouped` it is **one row** — `housing_count = "6"`, `stages = "738"`, `stages_basis = "group_total"` — with `123` carried in **`stages_per_housing_as_printed`**, a column added by the F5 audit precisely so T3's identity check has both printed numbers to compare.

**Where housings are not uniform, emit every printed count in section order** (`"123, 123, 123, 123, 123, 42"`), never a modal value. F2's GOUDA prints `42 + 93×4 = 414` and a dominant `93` lost a housing and made `housing_count × stages_per_housing` disagree with `stages` for a reason that was not a real disagreement.

**Sensitivity rows never produce a `pump_config` row at all** — see T4. The p7 table varies operating conditions, not the string.

## 6. Traps — mandatory guardrails

**T1 — The motor runs at 2× the shaft frequency. Take the shaft value for anything affinity-related.**
This is a **permanent-magnet** motor. `PRIEST 233H` prints `Motor Frequency 103.18 Hz` against `Pump Shaft Frequency 51.59 Hz`.
- `F5-04` `Motor Frequency` → `motor_frequency_hz`. **Never** to `frequency_hz_as_printed`, never to `design_frequency_hz`.
- `F5-11` `Pump Shaft Frequency` → `frequency_hz_as_printed` and `design_frequency_hz`. **This is the hydraulic frequency and the only one `K-AFFNORM` may consume.**
- `F5-14` `Motor: Type` is **required**, not optional — it is what tells a reader the 2× is physics rather than a typo. Emit `motor_type = 'permanent_magnet'` when the printed type says so.
- An extractor that grabs "the frequency" doubles every affinity normalization in this family. Since flow scales linearly and head quadratically, a 2× frequency error is a 2× flow error and a **4× head error** — large enough to look like a different pump, not like a bug.
- Validator `V-F5-1`: if `motor_type = 'permanent_magnet'` and `motor_frequency_hz / pump_shaft_frequency_hz` ∉ [1.9, 2.1] → `needs_review`.

**T2 — Depth is referenced to the pump *discharge*, not the intake. Set it, never infer it.**
`Simulation Parameters: Setting Depth from pump discharge 9851.95 ft` (`F5-07`), against a string running to ~10,046 ft. **Every other family references intake.**
- `depth_reference_basis = 'pump_discharge'` — a **literal, hardcoded** value for this contract. It is never inferred from the number, never defaulted to `intake`, and never left null.
- Emit the printed value into `intake_set_depth_md_ft` **unchanged** with that basis flag. Do not add the string length to convert it to an intake-referenced depth — that is arithmetic (D8) and belongs to M5, which has the p8 `Bottom` column to do it with.
- A discharge-referenced depth silently loaded as intake understates the hydrostatic column by roughly the pump length. On a taper that is a couple of hundred feet — small enough to pass review, large enough to bias `C-HYD`.

**T3 — The same pump, two grammars, and the validator checks the identity rather than trusting either number.**
Per §4: `housing_count × stages_per_housing == total_stages_reported` (`6 × 123 = 738`). Validator `V-09`. If it fails, **FLAG** — record both and set `extraction_status = 'conflict'`. Do not pick the one that looks rounder.

**T4 — The p7 `Sensitivity` table is not a `pump_config` scenario.**
Its cases vary `Water Cut`, `GLR`, `Static BHP`, `PI`, `Surface Rate` and **`Setting Depth`** — but not the pump string. Emit sensitivity rows with `scenario_label_staged` populated and `sensitivity_case = true`, and take the **p1 summary** as the design scenario (`is_design_scenario = true`, `scenario_selection_rule = 'p1_summary_base_case'`).
- **The string comes from p1 and p8 only.** No `pump_config` row is ever emitted from p7.
- Without this rule D17 fires spuriously on every F5 document, because `Setting Depth` varies per case.
- `PI` in the sensitivity table (`F5-24`) is **not extracted** — D11. The row names it because it is printed in the same table. `Intake Pressure` in `F5-26` is likewise **not extracted** — D11.

**T5 — No bubble point in this family (0/2). Do not manufacture one.**
`C-BUBBLE` gets nothing from F5. `bubble_point_psi`, `bubble_point_unit_as_printed`, `bubble_point_provenance` are all `null`, and `bubble_point_absent_confirmed = true`. **Do not derive it from `Static BHP` in the sensitivity table** — F1's `bubble point ≈ static datum` pattern is a *SpyGlass sizing-engineer assumption*, observed there, and importing it here would be exactly the laundering D14 exists to prevent.

**T6 — No BEP, no ROR, no per-stage head, no per-stage power. `envelope_status = 'absent'`, and F5 emits no head observations.**
`Simulation Parameters: Total Dynamic Head` (`F5-08`) is a **string-level** benchmark: `head_basis_as_printed = 'string_total'`, `observation_is_composite = true`.

**T7 — `H2S / CO2 / N2` compositional detail has no consumer and is not proposed. Do not extract it.**

---

##### Trap corrections — 2026-08-20 *(schema audit; originals above left standing)*

**T1 — `motor_frequency_hz` now exists.** When this contract was written there was no motor-side frequency column: design context carried `design_frequency_hz` / `_min_hz` / `_max_hz` only, so `Motor Frequency 103.18 Hz` had nowhere to go **except the two columns T1 forbids**. The column was added by the F5 audit. Emit the motor value there and the shaft value to `frequency_hz_as_printed` / `design_frequency_hz`, exactly as T1 says.

**`V-F5-1` names `pump_shaft_frequency_hz`, which is not a column.** The shaft value lives in `design_frequency_hz` and `frequency_hz_as_printed` per `F5-11`. The rule's ratio check is unchanged; only the column name was wrong.

**T3 / `V-09` — the identity check needs `stages_per_housing_as_printed`.** `housing_count × stages_per_housing == total_reported` (`6 × 123 = 738`) can only be checked if both printed numbers survive, and under `model_grouped` a single `stages` slot holds one of them. Recovering `123` as `738 / 6` is the arithmetic D8 forbids and defeats the purpose of checking rather than trusting.

**T4 — `sensitivity_case` now exists**, on `stg_design_context`, `stg_curve_observations` and `stg_gas_cascade`. This is the flag T4 and §5 both require and neither had. Set `sensitivity_case = "true"` on every row sourced from the p7 table. **It is F5-only** — not a corpus-wide flag; another family printing variation rows gets its own decision. Without it, p7's rows are indistinguishable from the p1 base case, D17 fires spuriously on `Setting Depth` in every document, and `F5-25` / `F5-26` curve rows get fitted as real operating points.

**T5 — `bubble_point_absent_confirmed` is not a column**, and the three bubble-point fields are not "`null`" — they are **omitted keys**. A proposed field absent from the document goes under ***Fields not found***, which is exactly the checked-and-absent record T5 wants. The finding that `C-BUBBLE` gets nothing from F5 at 0/2 stands unchanged, as does the ban on deriving a bubble point from `Static BHP`.

**T2 — depth is correct as written; do not "fix" it later.** M1's schema comment on `depth_reference_basis` reads `intake | pump_discharge (F5) | pump_setting` — the schema anticipated this family by name. Routing the discharge-referenced value into `intake_set_depth_md_ft` with the basis flag set is the intended design. **The F2 pair `pump_setting_md_ft` / `pump_setting_vd_ft` does NOT apply here**: it exists because F2 prints two distinct depths with different provenance, and F5 prints one.

## 7. Library-held models — do not re-extract the head curve

`400DAL650H · 400DAL1200 · 400DAL1200H · 400DAL1750H · 400DAL3000H · 400DAL4300H · SD2000 · SF900 · SF1750 · SF2700 · SF4300 · SFGH2500 · SFGH4300`

**No current F5 model matches** — the `ESP B 400 1750 PK` grammar is Baker Hughes and is absent from the 13. Run the check on every section anyway via D31 `proposed_canonical`. Given T6 this family emits no head, BEP or ROR rows, so the rule cannot bite today. It is stated because `4001750` is superficially close to `400DAL1750H`, and a fuzzy canonicalization that collapsed them would silently overwrite a manufacturer-grade library row with a Baker string total.

---

## 8. Fields to extract

**30 fields.** Every one is a `proposed = yes` row of `m1-field-inventory.csv` with `family = BakerProLift`. No field outside this table may be extracted.

| ID | Field, as printed | Page | Cardinality | Class (D14) | Trace | Target |
|---|---|---|---|---|---|---|
| `F5-01` | Project name / Target Well / Client / Case name | p1-8 page header | `per_document` | vendor input | join key -> well_id | *(join key — all four)* |
| `F5-02` | Prepared by / Date | p1-8 page footer | `per_document` | vendor input | D18 effective_from; preparer name not proposed | *(join key — all four)* |
| `F5-03` | Surface Electrical: System Power Consumption kW | p1 ProLift Summary | `per_document` | vendor derived | C-ENERGY (motor_power_kw benchmark for the proxy path) | `stg_design_context` |
| `F5-04` | Surface Electrical: Motor Frequency | p1 ProLift Summary | `per_document` | vendor input | K-AFFNORM - must not be confused with Pump Shaft Frequency; this family runs a permanent-magnet motor at 2x shaft Hz (103.18 vs 51.59 at n=1) | `stg_design_context` |
| `F5-05` | Surface Production Data: Surface Flow Rate / Tubing Pressure / Casing Pressure | p1 ProLift Summary | `per_document` | vendor input | Surface Flow Rate traces to C-BEP design flow; pressures not proposed | `stg_curve_observations` |
| `F5-06` | Pump(s): <model> <n> STG | p1 ProLift Summary | `per_section` | vendor input | C-NARROW + C-IDEAL + D31 model grammar (ESP B <series><model> PK form) | `stg_pump_config` + `stg_alias_evidence` |
| `F5-07` | Simulation Parameters: Setting Depth from pump discharge | p1 ProLift Summary | `per_document` | vendor input | C-HYD + C-DPREC (well_depth_ft) - note the basis is pump discharge not intake; the offset must be carried | `stg_design_context` |
| `F5-08` | Simulation Parameters: Total Dynamic Head | p1 ProLift Summary | `per_document` | vendor derived | K-TIER1 / Test A benchmark | `stg_design_context` + `stg_curve_observations` |
| `F5-09` | Simulation Parameters: Average Mixture Flow Rate | p1 ProLift Summary | `per_document` | vendor derived | K-CURVEFIT (downhole flow at the design point - the curve x-axis for this family) | `stg_curve_observations` |
| `F5-10` | Simulation Parameters: Pump Discharge Pressure | p1 ProLift Summary | `per_document` | vendor derived | C-DISCH benchmark (D13) | `stg_design_context` |
| `F5-11` | Pump Shaft Frequency | p1 ProLift Summary | `per_document` | vendor derived | K-AFFNORM (D10 as-printed frequency - the hydraulic frequency) | `stg_design_context` + `stg_curve_observations` |
| `F5-12` | Overall Pump Efficiency | p1 ProLift Summary | `per_document` | vendor derived | C-EFFPROXY benchmark | `stg_design_context` |
| `F5-13` | Intake/GS Gas Volume Fraction: Before Separation / After Natural Separation / Into Pump | p1 ProLift Summary | `per_document` | vendor derived | K-TIER2P (three-stage alpha cascade) | `stg_gas_cascade` ⚠️G1 + `stg_design_context` |
| `F5-14` | Motor: <model> / Type / Nameplate Power / Nameplate Voltage / Nameplate at rpm | p1 ProLift Summary | `per_document` | vendor input | C-ENERGY nameplate context; Type (Permanent Magnet) is required to read the frequency correctly | `stg_design_context` |
| `F5-15` | Motor: Operating Power / Motor Load / Motor Efficiency | p1 ProLift Summary | `per_document` | vendor derived | C-ENERGY benchmark (motor efficiency term) | `stg_design_context` |
| `F5-16` | Top of Perforation | p1-2 | `per_document` | vendor input | C-HYD depth-provenance disambiguation | `stg_design_context` |
| `F5-17` | Reservoir temperature | p1-2 | `per_document` | vendor input | C-BUBBLE (T_f - replaces the 150 F default) | `stg_design_context` |
| `F5-18` | Fluids: Oil Gravity (API) | p2 ProLift Detailed | `per_document` | vendor input | C-SG (sg_oil) + C-BUBBLE (API) | `stg_design_context` |
| `F5-19` | Fluids: Gas SG | p2 ProLift Detailed | `per_document` | vendor input | C-BUBBLE (gamma_g - replaces the 0.75 default) | `stg_design_context` |
| `F5-20` | Fluids: Water SG | p2 ProLift Detailed | `per_document` | vendor input | C-SG (sg_water) | `stg_design_context` |
| `F5-21` | Fluids: Water Cut | p2 ProLift Detailed | `per_document` | vendor input | C-SG (water_cut) | `stg_design_context` |
| `F5-22` | Multiphase Flow (Vertical / Horizontal correlation / Swap angle / Tuning Factor) | p2 ProLift Detailed | `per_document` | vendor assumption | C-HYD provenance (names the multiphase correlation behind the printed TDH) | `stg_design_context` |
| `F5-23` | Motor page: Power Factor % | p5 | `per_document` | vendor derived | C-BHP (replaces default pf = 0.90) + C-AFFINITY power check | `stg_design_context` |
| `F5-24` | Sensitivity table: Water Cut / GLR / Static BHP / PI / Surface Rate / Setting Depth per case | p7 | `per_scenario` | vendor input | C-SG and C-HYD scenario variance (feeds the D16 fail-loud rule on invariant fields) | `stg_design_context` |
| `F5-25` | Sensitivity table: Pump Shaft Frequency / Motor Frequency / Speed rpm per case | p7 | `per_scenario` | vendor derived | K-AFFNORM (three frequencies per document for the same string) | `stg_design_context` + `stg_curve_observations` |
| `F5-26` | Sensitivity table: Total Dynamic Head / Discharge Pressure / Intake Pressure / Flowing BHP per case | p7 | `per_scenario` | vendor derived | K-TIER1 / Test A benchmark; intake pressure excluded (D11) | `stg_design_context` + `stg_curve_observations` |
| `F5-27` | Sensitivity table: GVF at Intake / GVF into Pump per case | p7 | `per_scenario` | vendor derived | K-TIER2P (alpha at three operating points) | `stg_gas_cascade` ⚠️G1 |
| `F5-28` | Sensitivity table: Pump Efficiency / Motor Efficiency / Overall Efficiency per case | p7 | `per_scenario` | vendor derived | C-EFFPROXY + C-ENERGY benchmark | `stg_design_context` |
| `F5-29` | Sensitivity table: Operating Power / Motor Load / Surface kVA / Surface Voltage / Motor Current / VSD Current / Power Cons. kW per case | p7 | `per_scenario` | vendor derived | C-ENERGY (Operating Power and Power Cons. kW); currents and voltages not proposed | `stg_design_context` |
| `F5-30` | String diagram table: Description / PN / Q-ty / OD / Length / Mass / Bottom | p8 | `per_section` | vendor input | C-IDEAL (per-housing stage counts) + K-TIER1 (section order and depth) + D31 model grammar | `stg_pump_config` + `stg_alias_evidence` |

---

##### Row corrections — 2026-08-20 *(schema audit; the table above is left standing)*

| Row | Was | **Is** | Why |
|---|---|---|---|
| `F5-10` `Pump Discharge Pressure` | `stg_design_context` | **`stg_curve_observations`** → `discharge_pressure_psi_as_printed` | Design context has **no discharge column at all**, so this was homeless as well as mis-targeted. With intake pressure it brackets the pump's ΔP — an observation-grain Test A benchmark, not a well-grain property. Settled in `f1-spyglass.md` §8, 2026-08-18. |
| `F5-26` per-case `Discharge Pressure` | both targets | **`stg_curve_observations`** for the discharge pressure | Same reasoning. `Total Dynamic Head` and the rest of the row are unaffected. `Intake Pressure` remains excluded (D11). |
| `F5-04` `Motor Frequency` | `stg_design_context` | unchanged, key is **`motor_frequency_hz`** | Added by the F5 audit. Before it, T1's "never to `frequency_hz_as_printed`, never to `design_frequency_hz`" left the value with no legal destination. |
| `F5-06`, `F5-30` | `stg_pump_config` | unchanged, add **`stages_per_housing_as_printed`** | The per-housing `123 STG` from the p8 diagram, so `V-09` can compare it against the summary's `738`. |
| `F5-13`, `F5-27` | `stg_gas_cascade` ⚠️G1 | **`stg_gas_cascade`** | **G1 is closed** (CF-16). Pump-body rows resolve into `curve_observations.free_gas_at_inlet_pct` at load; separator and intake stages stay in staging as the audit record. Strike the ⚠️. |
| `F5-24` | includes `GLR` | **`GLR` not extracted** | Producing GOR/GLR is a corpus-wide exclusion listed in this contract's own *Not extracted* section. Name it in the row and exclude it explicitly, as `f4-els.md` does for GOR. `Water Cut`, `Static BHP` and `Setting Depth` are extracted; `PI` remains excluded (D11). |
| `F5-24` … `F5-29` | per-scenario rows | unchanged, add **`sensitivity_case = "true"`** | Every row sourced from the p7 table. See T4. |
| `F5-22` | `vendor_multiphase_correlation` | unchanged — **both correlations as ONE verbatim string** | F5 prints a vertical and a horizontal correlation. Emit what is printed as a single string (`"Vertical: … / Horizontal: …"`); provenance is read at audit, not parsed, and D8 already makes every value a verbatim string. Hamed's call, 2026-08-20. |

##### Dropped from the 30 — D22, no contract upgraded, no calculation benchmarked

**One whole row: `F5-05`.** Only `Surface Flow Rate` was proposed from it, and this contract itself names `F5-09` `Average Mixture Flow Rate` as the downhole flow at the design point — the curve x-axis for this family. A surface rate is not a point on a pump curve, and `Water Cut` carries the split. Matches the raw- and surface-rate drops in F1, F2, F4 and F6. **Row count 30 → 29.**

**Sub-field narrowings within five rows that otherwise stand:**

- `F5-14` — motor `<model>` and `Nameplate at rpm`. The F2 audit dropped motor `Model No.`; no contract consumes nameplate rpm, since T1 reads the 2× off `motor_type`. `Type`, `Nameplate Power` and `Nameplate Voltage` stay, and `Type` is **required** per T1.
- `F5-15` and `F5-29` — `Operating Power` and `Motor Load`. F1 precedent, which dropped `Motor Input HP` and `Operating HP at design frequency` as vendor-derived duplicates. `Motor Efficiency` and `Power Cons. kW` stay.
- `F5-22` — `Swap angle` and `Tuning Factor`. The weakest of the drops: a *tuned* correlation is arguably a D13 qualifier on the printed TDH. Dropped on Hamed's call, flagged here so it can be reopened.
- `F5-26` — `Flowing BHP`. No column, and it sits in the same row as the D11-excluded intake pressure.
- `F5-30` — `PN`, `OD`, `Length`, `Mass`. The F2 audit dropped pump `PN` / `Length` / `Weight`. **`Q-ty` and `Bottom` stay** — they are where `housing_count`, `section_order` and `bottom_md_ft` come from, and §3 is explicit that `Bottom` is measured depth from the wellhead, not a running tally.

Set the batch script's field-count band against the **CSV's 30**, which still carries `F5-05`; the prompt's inlined field list is what binds the extractor.

## 9. Target tables and cardinality

Extraction writes to **staging**. M5 owns every collapse, normalization and division.

| Staging target | Grain emitted by this contract | Destination |
|---|---|---|
| `stg_pump_config` | **model** (`model_grouped`, CF-28) — **p1 and p8 only, never p7** | `pump_config` (well × section × epoch) |
| `stg_design_context` | base case + one row per **sensitivity case** (`sensitivity_case = "true"`) | `esp_well_design_context` (well) after the **D16** selection |
| `stg_curve_observations` | model × point, plus per-sensitivity-case rows carrying the same flag | `curve_observations` (model × point) |
| `stg_gas_cascade` | cascade stage, base case and per sensitivity case | pump-body rows resolve into `curve_observations.free_gas_at_inlet_pct`; separator and intake stages stay in staging as the audit record (**CF-16 closed**) |
| `stg_alias_evidence` | printed string | the **D31** lookup (a component, not a table) |

> **G1 is closed.** This table previously marked `stg_gas_cascade` as having no destination column. `staging-schemas.md` defines it, and `m2-validator-spec` §9's matching instruction — that M5 must not load cascade rows — is stale for the same reason and still needs correcting.

**The p7 sensitivity table is not a scenario in the D17 sense.** It varies `Water Cut`, `GLR`, `Static BHP`, `PI`, `Surface Rate` and `Setting Depth` — operating conditions, not equipment. The string comes from p1 and p8 only. `sensitivity_case = "true"` is what keeps D17 from firing spuriously on `Setting Depth` in every document, and `V-F5-*` in `m2-validator-spec` §4 records F5 as the known-safe D17 exception (§6.3 there).

## 10. Family validator rules

Global rules `V-01` … `V-20` in [[m2-validator-spec]] apply to every family. The family-specific rules for F5 are `V-F5-*` in that document's §4. Pass B runs both sets.

---

## Not extracted

Per **D22**, the not-extracted list for this family lives in `m1-field-inventory.csv` (`family = BakerProLift`, `proposed = no`, 21 rows, each with a one-line reason) and in the R1 template atlas. It is **not** restated here and **not** restated per document. A per-document report notes only *anomalies against this contract*.

Corpus-wide exclusions that apply here without exception: **PI** and **PIP** (D11) · **NPSHr** — no mapping from any field, including `Free Allowed Gas` (D12) · motor amperage (D2, duplicates `esp_well_configuration_v2.motor_rated_amps`) · producing GOR/GLR · cable / VSD / transformer / seal / sensor selection strings · design-time tubing and casing setpoints.

---

## Log

| Date | Update |
|---|---|
| 2026-08-14 | Contract issued at M2. Not yet run — M3 pilots it on `ESPD_PERMIAN RESOURCES OPERATING_PRIEST STATE UNIT 233H_11-24-2025_PMM.pdf`. || 2026-08-20 | **Corrected against the staging-schema audit, ahead of the M4 F5 run.** §5 gains the `model_grouped` grain statement (CF-28), which **supersedes §4's `stages_basis` paragraph** — one row with `group_total`, not two bases for one model, with `123` carried in the new `stages_per_housing_as_printed`. §6 gains trap corrections: `motor_frequency_hz` now exists so T1 has a legal destination for `103.18 Hz`; `V-F5-1`'s `pump_shaft_frequency_hz` is not a column; `sensitivity_case` now exists on three tables, which is the flag T4 and §5 both required and neither had; `bubble_point_absent_confirmed` is not a column and absence is an omitted key; and T2's depth routing is confirmed **correct as written** — M1's `depth_reference_basis` enum names `pump_discharge (F5)` explicitly, so the F2 `pump_setting_*` pair does not apply. §8 gains row corrections: `Discharge Pressure` moves to `stg_curve_observations` on F5-10 and F5-26, ⚠️G1 struck from F5-13 and F5-27, `GLR` excluded explicitly in F5-24, `sensitivity_case` added to the p7 rows, and F5-22's two correlations emitted as one verbatim string. One whole row dropped under D22 (**F5-05**, surface flow rate), taking the count **30 → 29**, plus sub-field narrowings in F5-14, F5-15/F5-29, F5-22, F5-26 and F5-30. §9 rewritten for the closed cascade, the corrected grain, and the sensitivity-versus-scenario distinction. Original text left standing throughout; corrections override rather than replace. |
