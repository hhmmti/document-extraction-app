---
title: F7 Valiant — Extraction Contract
created: 2026-08-14
status: corrected 2026-08-20 against the staging-schema audit — M4 runs this version
milestone: M2
family: F7
vendor_family: valiant
documents: 1
proposed_fields: 46
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - contract
  - valiant
related:
  - "[[m2-extraction-method]]"
  - "[[m1-extraction-spec]]"
  - "[[design-doc-extraction-plan]]"
  - "[[design-doc-extraction-kickoff]]"
---

# F7 Valiant — Extraction Contract

> [!note] **One of seven. Not interchangeable with any other contract.** The umbrella is [[m2-extraction-method]]; the field record is `m1-field-inventory.csv` (46 rows with `proposed = yes`, family `Valiant`); the decisions are locked in [[design-doc-extraction-kickoff]]. Extraction is **verbatim** (D8) — no unit conversion, no arithmetic, no canonicalization, no conflict resolution. Every transform belongs to M5.

## 0. Applicability

**Vendor:** Valiant Artificial Lift Solutions. **Tool:** ZONE®. **Documents: 1.**

`HALEY NE G 412H Zone Summary.pdf`

> Prints through `Microsoft: Print To PDF`, the same driver as **F6 XSize**. They have nothing else in common. **This contract must not be merged with F6.**

> **The cleanest numeric envelope in the corpus** — `Operating Range: Min / BEP / Max` printed as numbers, plus `Efficiency: BEP / Design`, plus a per-housing `Order` column that states the section stacking explicitly.

---

## 1. Page-1 signature — confirm before extracting

Page 1 must show:

- **`Valiant ZONE® Summary Sheet`**
- a `GENERAL` block (`Customer / Lease / Well / Date / Cust Rep / Quote#`)
- a `System Information` block with `Frequency Min / Design / Max`
- a `Well Information` block

If page 1 reads `XSize Design Program` / `An Extract Technology`, this is **F6** — HALT and route to `f6-xsize.md`.

---

## 2. Pages

**~20 pages.**

| Page | Contents |
|---|---|
| p1 | `GENERAL` · `System Information` · `Well Information` · `Design Properties` |
| p2 | `Production Summary` |
| p3 | `Equipment Summary` — pumps, intake/separators, motor |
| p4 | `PRODUCTION FLUIDS` · `WELL PRODUCTIVITY` · `WELL CHARACTERISTICS` · `DESIGN SEPARATION` |
| p5 | `PVT Correlations` list |
| p6 | **`Pump Detail`** — per-section: operating range, pressures, flows, Cq/Ch/Cbhp, SG, efficiency, power |
| p7 | `Pump Summary` — `Housing Pressure` table and `Shaft Power` table, both with an **`Order`** column |
| p8 | `Intake/Separator Detail` · `Pump Intake` — Turpin PHI, Dunbar PHI |
| p9 | `Motor Detail` |
| p10 | `Electrical Detail` |
| p20 | `Equipment List` — `Description / Start MD / Stop MD / OD / WT / Length / Part#` |

Plot-only, read no numbers: `VSD Tornado Curve`, `Freq Charts`, `Inflow Curve`, `Motor Chart`.

---

## 3. Sections — pump bodies only

Three agreeing sources — use all three:
- **p3 `Equipment Summary: Pump(s)`** — `Series / Model / <n> Housing(s) / <n> stage(s)` (`F7-10`).
- **p6 `Pump Detail`** — per-section hydraulics (`F7-27` … `F7-35`).
- **p7 `Pump Summary` tables** — per-housing `Description / Housing / Stages / **Order**` (`F7-36`, `F7-37`).

> **The `Order` column states the stacking explicitly.** Use it. It is the only family that removes the top-vs-bottom guess entirely. Map `Order` to `section_order` with **1 = deepest**; if the printed `Order` runs the other way, record `printed_order_direction` and reverse — do not silently reinterpret.

Cross-check against the p20 `Equipment List` `Start MD / Stop MD` columns (`F7-46`) for `top_md_ft` / `bottom_md_ft`, measured from the wellhead.

Never a section row: intakes, upper/lower **separators**, seals, motor, sensor, cable. `F7-11` names the separator block because its efficiencies trace to `K-TIER2P`; they go to `stg_gas_cascade`, never to `stg_pump_config`.

**`Stages Out` is a real deduction** (`F7-28`): `Total: <n> stages / <n> Stages Out`. Emit **both** — `stages` and `stages_out` — verbatim. Do not subtract. The net stage count is arithmetic and belongs to M5 (D8).

---

## 4. Model grammar

```
^VC\d{4}(\s*-\s*[A-Z]{2})?(\s+Modular)?(\s+EHP)?$
```

Observed: `VC4300 - AR Modular EHP`. `Series` and `Model` are printed as separate labels on p3; p6 prints `Series/Model @ <f>Hz (<n> RPM)` as one string.

- Emit the **whole printed string** into `pump_model_as_printed`, and the p3 `Series` into `pump_series_as_printed`.
- `AR`, `Modular` and `EHP` are build attributes, not hydraulic identity — but **capture them**, and let D31 drop them into the canonical form at M5. Do not strip them at extraction.

---

## 5. Scenarios

**Single-scenario family.** `System Information` prints `Frequency Min / Design / Max` and `Total Flow Min / Design / Max` — those are an **envelope on one design**, not three scenarios. `scenario_label_staged = null`, `scenario_ordinal = 1`, `is_design_scenario = true`.

**D16 and D17 cannot fire on F7.** If either does, the document is not what this contract describes — HALT.

---

##### `pump_config` grain — `model_grouped` *(CF-28, stated 2026-08-20)*

Emit **one row per distinct model**, not one row per housing. Set `section_grain = 'model_grouped'`, `housing_count`, `stages_basis = 'group_total'`, and `stages_per_housing_as_printed`.

F7 prints **both** numbers — `Housings #n <n> stgs` per housing and `Total: <n> stages` for the group — so both must survive. Do not multiply, divide or reconcile them; where they disagree emit all three values and set `extraction_status = 'conflict'`.

**Where housings are not uniform, emit every printed count in section order** (`"87, 87, 42"`), never a modal value. F2's GOUDA prints `42 + 93×4 = 414` and a dominant `93` lost a housing and made `housing_count × stages_per_housing` disagree with `stages` for a reason that was not a real disagreement.

**`<n> Stages Out` gets its own column — `stages_out`.** §3 already requires both to be emitted verbatim and states that the net count is arithmetic belonging to M5 (D8). Until the F7 audit added `stages_out` there was no such column, so the only way to obey §3 was the subtraction it forbids.

**Absence is an omitted key, not a null.** §5 says `scenario_label_staged = null`; under the JSONL rules an unprinted value is an **omitted key** — never `null`, `""` or `"-"`. The single-scenario finding stands; only the encoding differs.

## 6. Traps — mandatory guardrails

**T1 — This family's bubble-point correlation is *not* Standing, and the app implements Standing.**
`PVT Correlations: Pb/Rs: Al-Shammasi (1999)` (`F7-26`). Emit `bubble_point_correlation = 'Al-Shammasi (1999)'` verbatim.
- The printed `Pb` (`F7-18`) is therefore classified **`vendor derived`**, not `vendor assumption`.
- **Do not compare it to a Standing result and do not "correct" it.** Recording the correlation name is the whole point: `C-BUBBLE` benchmarks the app's Standing implementation against a document that used a different correlation, and that comparison is only meaningful if the name survives extraction.

**T2 — `Avg Cq / Avg Ch / Avg Cbhp` are all `1.0000`, and that is the tell that the printed curve is viscosity-*un*corrected.**
`F7-32`. Emit all three verbatim and set `viscosity_corrected = false` when all three are 1.0000. If any differs from 1.0000, set `viscosity_corrected = true` — the curve is corrected and must not enter a water-basis fit unlabelled. **This is a data field, not a formatting artifact.** An extractor that skips "boring" all-ones values destroys the only evidence that the curve is usable as-is.

**T3 — Two gas-degradation correlations printed, and only one is implemented.**
`Turpin PHI` **and** `Dunbar PHI` (`F7-40`). Emit **both**, each labelled. Turpin is the D26 correlation the Tier 2′ ladder names; Dunbar is a second correlation not implemented in the app. Do not drop Dunbar because it has no consumer today — it is the only place in the corpus a second correlation is printed alongside Turpin, which makes it the only available cross-check on D26's choice.

**T4 — Gas SG is printed on two bases and they are different numbers.**
`Spg Gas (HC)` and `Spg Gas (Mix)` (`F7-15`). Emit both with `gas_sg_basis` ∈ `hydrocarbon | mixture`. `C-BUBBLE`'s `gamma_g` wants the **hydrocarbon** basis. F7 is the only family printing both, which is why the schema carries `gas_sg_basis` at all.

**T5 — Three temperatures printed; they are not interchangeable.**
`Temp @ Intake` / `Temp @ Reservoir` / `Temp @ Surface` (`F7-23`) → `intake_temp_f` / `reservoir_temp_f` / `surface_temp_f`, three columns. `C-BUBBLE`'s `T_f` is evaluated at pump conditions, so having the intake temperature printed means this family can replace the 150 °F default with a *measured-basis* value rather than a reservoir one. Do not collapse them to one.

**T6 — Bubble-point unit.** F7 prints `psi`. Required field (`V-04`).

**T7 — `Operating Range: Min / BEP / Max` are the real thing. Emit three separate `curve_observations` rows.**
`F7-29` — `Min: 2,000 bbl/d / BEP: 4,600 bbl/d / Max: 5,200 bbl/d`. `point_type` ∈ `ror_min | bep | ror_max`, `flow_unit_as_printed = 'bbl/d'` **as printed** — note it is `bbl/d` here, where F1's envelope prints `STB/D`. Never reconcile (D8).
Emit `Efficiency: BEP % / Design %` (`F7-34`) as `efficiency_pct` on the matching rows: the BEP efficiency belongs to the `bep` row, the design efficiency to the design point, and they must not be swapped.

**T8 — Per-section `Pressure @Intake` is a section boundary, not PIP.**
`F7-30`. Same rule as F6 T9: emit as `section_intake_pressure_psi_as_printed` on the observation row only. **Never** to any `pump_intake_pressure_psi` column, never to `esp_well_design_context` (D11).

**T9 — `QHCF / HPCF` are zero at n=1 and unconsumed. Not proposed. Do not extract.**

**T10 — Motor amperage: `F7-12` and `F7-42` name `A` fields alongside HP and V. They are excluded (D2). Extract HP, V and F; omit A.**

---

##### Trap corrections — 2026-08-20 *(schema audit; originals above left standing)*

**T3 — both PHI indicators now have somewhere to go.** F7 prints `Turpin PHI` and `Dunbar PHI` at the **same** cascade stage (Pump Intake), so unlike F6's two indicators — which are two stages and therefore two rows — these collide on `stage_order`. A rename to a correlation-neutral column with the grain extended to stage × correlation was proposed and **overruled**: F1 is 30 of the 47 in-scope documents and a `stg_gas_cascade` grain change would invalidate that extraction at ~$1 per document to re-run.

**Resolution: `dunbar_indicator_as_printed` alongside `turpin_indicator_as_printed`**, both paired with `correlation_as_printed`. Grain unchanged. Emit each PHI into its own column; do not put a Dunbar value in the Turpin column. T3's reason survives — Dunbar has no consumer today and is the corpus's only cross-check on D26's choice of Turpin, which is worth nothing if the two arrive indistinguishable.

*Recorded cost:* the pair does not generalize — a third correlation needs a third column — and `correlation_as_printed` is now partly redundant with the column names.

**T4 — `gas_sg_basis` is a flag on one column, not a second column.** F7 prints `Spg Gas (HC)` and `Spg Gas (Mix)` as different numbers, and there is one `gas_sg` slot. The F7 audit added **`gas_sg_mixture`**: `gas_sg` keeps the hydrocarbon value that C-BUBBLE's `gamma_g` wants, and the mixture value gets its own key. M1's own schema comment reads "hydrocarbon | mixture (F7 prints both)" while the column could hold one of them.

**T2 — the three viscosity factors now have a destination.** `Avg Cq`, `Avg Ch` and `Avg Cbhp` go to **`viscosity_correction_factors_as_printed`** as one verbatim string; `viscosity_corrected` remains the derived boolean. The boolean is a judgment made from three printed numbers, and D8's posture is that the inputs to a judgment survive it — and where a factor differs from 1.0000 the magnitude matters to a later correction, not just the fact of it.

**§4's `pump_series_as_printed` exists.** The column was created by the F4 audit; F4 and F7 are its two users. Emit the p3 `Series` there and do not fold it into the model token.

## 7. Library-held models — do not re-extract the head curve

`400DAL650H · 400DAL1200 · 400DAL1200H · 400DAL1750H · 400DAL3000H · 400DAL4300H · SD2000 · SF900 · SF1750 · SF2700 · SF4300 · SFGH2500 · SFGH4300`

**No current F7 model matches** — `VC4300` is Valiant and is absent from the 13. Run the check on every section anyway via D31 `proposed_canonical`; where it hits, head/BEP/ROR carry `curve_observation_role = 'cross_check'` and never overwrite a library row, while power and efficiency carry `role = 'contribution'`.

For F7 specifically the contribution is unusually clean: a numeric BEP/ROR envelope (T7), a per-section `Pump Power Consumption` (`F7-35`), and the `viscosity_corrected = false` flag (T2) that says the envelope can be trusted as printed.

---

> **Corrected 2026-08-20 (schema audit).** This section instructed the extractor to set `library_row_exists` and to mark rows `curve_observation_role = 'cross_check'` and `'contribution'`. **Both are wrong:**
>
> - **`library_row_exists` is a join computed at load**, not an extracted field. Extracting it bakes a point-in-time answer into the record, which goes stale the moment MC's library changes.
> - **`cross_check` and `contribution` are not enum values.** `curve_observation_role` is `head_curve | bhp_curve | single_pump_chart | multiscenario | tapered_composite | design`. Library precedence is an M5/M6 weighting concern, not a staging role. **Every F7 observation row is `'design'`.**
>
> The same two errors sat in `f3-slb.md` §7 and `f6-xsize.md` §7; F3's is corrected, F6's is not yet.

## 8. Fields to extract

**46 fields.** Every one is a `proposed = yes` row of `m1-field-inventory.csv` with `family = Valiant`. No field outside this table may be extracted.

| ID | Field, as printed | Page | Cardinality | Class (D14) | Trace | Target |
|---|---|---|---|---|---|---|
| `F7-01` | Customer / Lease / Well / Date / Cust Rep / Quote# | p1-2 GENERAL | `per_document` | vendor input | join key -> well_id + D18 effective_from; rep and quote not proposed | *(join key — all four)* |
| `F7-02` | System Information: Frequency Min / Design / Max | p1 | `per_document` | vendor input | K-AFFNORM (frequency envelope for normalized observations) | `stg_design_context` + `stg_curve_observations` |
| `F7-03` | System Information: Total Flow Min / Design / Max | p1 | `per_document` | vendor input | C-BEP design flow envelope | `stg_curve_observations` |
| `F7-04` | Well Information: Casing / Tubing / BH Temp / Pump Depth | p1 | `per_document` | vendor input | BH Temp traces to C-BUBBLE (T_f); Pump Depth to C-HYD; casing and tubing not proposed | `stg_design_context` |
| `F7-05` | Design Properties: Oil / Water / Gas | p1 | `per_document` | vendor input | C-SG (water cut basis) | `stg_design_context` |
| `F7-06` | Production Summary: Oil / Water / Gas / Total (Oil + Water) | p2 | `per_document` | vendor input | C-SG (water cut basis) + C-BEP design flow | `stg_design_context` + `stg_curve_observations` |
| `F7-07` | Production Summary: Pump Intake Flow / Pump Discharge Flow | p2 | `per_document` | vendor derived | K-CURVEFIT (downhole flow at the design point) | `stg_curve_observations` |
| `F7-08` | Production Summary: Pump Discharge Pressure | p2 | `per_document` | vendor derived | C-DISCH benchmark (D13) | `stg_design_context` |
| `F7-09` | Production Summary: Pump TDH | p2 | `per_document` | vendor derived | K-TIER1 / Test A benchmark | `stg_design_context` + `stg_curve_observations` |
| `F7-10` | Equipment Summary: Pump(s) Series / Model / <n> Housing(s) / <n> stage(s) | p3 | `per_section` | vendor input | C-NARROW + C-IDEAL + K-TIER1 + D31 model grammar | `stg_pump_config` + `stg_alias_evidence` |
| `F7-11` | Equipment Summary: Intake/Separator(s) (Upper / Lower Separator / Efficiency / HP / ft) | p3 and p8 | `per_section` | vendor derived | K-TIER2P (separation stages in the alpha cascade) | `stg_gas_cascade` ⚠️G1 |
| `F7-12` | Equipment Summary: Motor(s) Model / Nameplate HP / V / A | p3 | `per_document` | vendor input | C-ENERGY nameplate context; the A field would duplicate v2 motor_rated_amps and is excluded | `stg_design_context` |
| `F7-13` | PRODUCTION FLUIDS: Oil API / Spg Oil | p4 | `per_document` | vendor input | C-SG (sg_oil) + C-BUBBLE (API) - both printed together | `stg_design_context` |
| `F7-14` | PRODUCTION FLUIDS: Spg Water | p4 | `per_document` | vendor input | C-SG (sg_water) | `stg_design_context` |
| `F7-15` | PRODUCTION FLUIDS: Spg Gas (HC) / Spg Gas (Mix) | p4 | `per_document` | vendor input | C-BUBBLE (gamma_g - replaces the 0.75 default); the HC-vs-Mix distinction must be carried | `stg_design_context` |
| `F7-16` | PRODUCTION FLUIDS: Water Cut | p4 | `per_document` | vendor derived | C-SG (water_cut) | `stg_design_context` |
| `F7-17` | PRODUCTION FLUIDS: Rsb | p4 | `per_document` | vendor input | C-BUBBLE (R_so at bubble point - replaces the producing-GOR proxy the contract only allows on user confirmation) | `stg_design_context` |
| `F7-18` | PRODUCTION FLUIDS: Pb | p4 | `per_document` | vendor derived | C-BUBBLE (selected_bubble_point_psi) | `stg_design_context` |
| `F7-19` | PRODUCTION FLUIDS: Desired Rate | p4 | `per_document` | vendor input | C-BEP design flow | `stg_curve_observations` |
| `F7-20` | WELL PRODUCTIVITY: Pr / Test Pressure / Test Flow / Design Flow (Qsc) / Design Pwf | p4 | `per_document` | vendor input | Design Flow traces to C-BEP; reservoir test data not proposed | `stg_curve_observations` |
| `F7-21` | WELL CHARACTERISTICS: Pump Depth (MD) / Pump Depth TVD | p4 | `per_document` | vendor input | C-HYD + C-DPREC (well_depth_ft) - MD and TVD printed separately | `stg_design_context` |
| `F7-22` | WELL CHARACTERISTICS: TPI Depth (MD) / KOP | p4 | `per_document` | vendor input | C-HYD depth-provenance disambiguation | `stg_design_context` |
| `F7-23` | WELL CHARACTERISTICS: Temp @ Intake / Temp @ Reservoir / Temp @ Surface | p4 | `per_document` | vendor derived | C-BUBBLE (T_f - three temperatures let the contract use intake temperature rather than a default) | `stg_design_context` |
| `F7-24` | WELL CHARACTERISTICS: cf HW / Flowing Gradient / Tuning Factor / P Res | p4 | `per_document` | vendor assumption | Flowing Gradient traces to C-HYD provenance (names the multiphase correlation); the tuning constants are not proposed | `stg_design_context` |
| `F7-25` | DESIGN SEPARATION: Natural / Mechanical / Total | p4 | `per_document` | vendor assumption | K-TIER2P (separation stages in the alpha cascade) | `stg_gas_cascade` ⚠️G1 |
| `F7-26` | PVT Correlations list (Pb/Rs: Al-Shammasi (1999) / Oil Density Standing / viscosity and Z correlations) | p5 | `per_document` | vendor assumption | C-BUBBLE provenance - names the correlation behind the printed Pb and shows it is NOT Standing (the correlation the app implements) | `stg_design_context` |
| `F7-27` | Pump Detail: Series/Model @ <f>Hz (<n> RPM) | p6 | `per_section` | vendor input | K-AFFNORM (D10 as-printed frequency) + D31 model grammar | `stg_pump_config` + `stg_alias_evidence` + `stg_curve_observations` |
| `F7-28` | Pump Detail: Housings #<n> <n> stgs/ ... (Total: <n> stages / <n> Stages Out) | p6 | `per_section` | vendor input | C-IDEAL (stages) + K-TIER1 (per-housing decomposition; Stages Out is a real deduction) | `stg_pump_config` |
| `F7-29` | Pump Detail: Operating Range Min / BEP / Max | p6 | `per_section` | vendor derived | C-BEP (bep_bpd + min/max_recommended_bpd) + K-WINDOW | `stg_curve_observations` |
| `F7-30` | Pump Detail: Pressure @Intake / PHI / Discharge | p6 | `per_section` | vendor derived | C-DISCH benchmark; PHI traces to K-TIER2P | `stg_curve_observations` |
| `F7-31` | Pump Detail: Flow @Intake / @Discharge / Avg Flow | p6 | `per_section` | vendor derived | K-CURVEFIT (downhole flow at the design point) + K-TIER1 equal-flow check | `stg_curve_observations` |
| `F7-32` | Pump Detail: Avg Cq / Avg Ch / Avg Cbhp | p6 | `per_section` | vendor derived | K-CURVEFIT quality flag - viscosity correction factors; all 1.0000 at n=1 meaning the printed curve is uncorrected | `stg_curve_observations` |
| `F7-33` | Pump Detail: Spg Fluid / Avg Spg Fluid / Avg Pump Spg Fluid | p6 | `per_section` | vendor derived | C-SG benchmark (calc_mixture_sg check) + C-HEADDP (sg_for_dp candidate) | `stg_curve_observations` + `stg_design_context` |
| `F7-34` | Pump Detail: Efficiency BEP % / Design % | p6 | `per_section` | vendor derived | C-EFFPROXY benchmark + K-CURVEFIT (efficiency at BEP and at the design point) | `stg_curve_observations` |
| `F7-35` | Pump Detail: Pump Power Consumption HP / Intake Shaft Power HP | p6 | `per_section` | vendor derived | K-CURVEFIT (section power - the design docs unique contribution per amendment 8) + C-ENERGY | `stg_curve_observations` |
| `F7-36` | Pump Summary - Housing Pressure table (Description / Housing / Stages / Order / Pressure @100%H2O / Limit / Load % / Selected) | p7 | `per_section` | vendor derived | K-TIER1 (per-housing Order gives the section stacking order explicitly) | `stg_pump_config` |
| `F7-37` | Pump Summary - Shaft Power table (Description / Housing / Stages / Order / Shaft Load / Limit / Load %) | p7 | `per_section` | vendor derived | K-TIER1 (cumulative shaft power per housing confirms the series stacking) | `stg_pump_config` |
| `F7-38` | Intake/Separator Detail (Free Gas Before Separation / Liquid Flow / Gas Flow / Efficiency / Specific Gravity per separator) | p8 | `per_section` | vendor derived | K-TIER2P (separation stages) + C-SG benchmark | `stg_gas_cascade` ⚠️G1 |
| `F7-39` | Pump Intake: Natural / Mechanical / Total Separation | p8 | `per_section` | vendor derived | K-TIER2P (alpha cascade) | `stg_gas_cascade` ⚠️G1 |
| `F7-40` | Pump Intake: Turpin PHI / Dunbar PHI | p8 | `per_section` | vendor derived | K-TIER2P (names Turpin as the degradation correlation and gives its indicator - D26; Dunbar is a second correlation not implemented in the app) | `stg_gas_cascade` ⚠️G1 |
| `F7-41` | Pump Intake: PFG / Total Flow / Total Liq Flow / Total Gas Flow / Specific Gravity | p8 | `per_section` | vendor derived | K-TIER2P (free gas fraction) + K-CURVEFIT (downhole flow) | `stg_gas_cascade` ⚠️G1 + `stg_curve_observations` |
| `F7-42` | Motor Detail: Nameplate 400 HP / V / A / Operating HP / V / A / F | p9 | `per_document` | vendor input | C-ENERGY nameplate and operating context; the A fields would duplicate v2 motor_rated_amps and are excluded | `stg_design_context` |
| `F7-43` | Motor Detail: Eff | p9 | `per_document` | vendor derived | C-ENERGY benchmark (motor efficiency term) | `stg_design_context` |
| `F7-44` | Motor Detail: Pf | p9 | `per_document` | vendor derived | C-BHP (replaces default pf = 0.90) + C-AFFINITY power check | `stg_design_context` |
| `F7-45` | Electrical Detail (Surface / Downhole / MLE cable temperatures / voltage drops / TCF / VSD / PST Transformer / Motor KW / Cable KW / Total KW / Surface KVA) | p10 | `per_document` | vendor derived | Motor KW and Total KW trace to C-ENERGY (motor_power_kw benchmark); the cable and drive detail is not proposed | `stg_design_context` |
| `F7-46` | Equipment List (Description / Start MD / Stop MD / OD / WT / Length / Part#) | p20 | `per_section` | vendor input | K-TIER1 (per-housing depths confirm section order) + C-IDEAL (stages per housing) | `stg_pump_config` |

---

##### Row corrections — 2026-08-20 *(schema audit; the table above is left standing)*

| Row | Was | **Is** | Why |
|---|---|---|---|
| `F7-08` `Pump Discharge Pressure` | `stg_design_context` | **`stg_curve_observations`** → `discharge_pressure_psi_as_printed` | Design context has no discharge column, so this was homeless as well as mis-targeted. With intake pressure it brackets the pump's ΔP — an observation-grain Test A benchmark. Settled in `f1-spyglass.md` §8. `F7-30`'s per-section `Discharge` was already correct. |
| `F7-30` `PHI` | `stg_curve_observations` | **`stg_gas_cascade`**, with `F7-40` | Observations have no indicator column. The pressures on that row stay where they are. |
| `F7-15` `Spg Gas (HC)` / `(Mix)` | one `gas_sg` | **`gas_sg`** (hydrocarbon) **+ `gas_sg_mixture`** | Two printed numbers, one slot. See T4. |
| `F7-28` `<n> Stages Out` | no destination | **`stages_out`** | §3 requires it verbatim; the net count is M5 arithmetic. |
| `F7-32` `Avg Cq` / `Ch` / `Cbhp` | only the derived boolean | **`viscosity_correction_factors_as_printed`** + `viscosity_corrected` | T2 says emit all three verbatim. |
| `F7-40` `Dunbar PHI` | shared `turpin_indicator_as_printed` | **`dunbar_indicator_as_printed`** | Same stage, two correlations. See T3. |
| `F7-10` `Series` | `stg_pump_config` | unchanged, key is **`pump_series_as_printed`** | §4 names it; the column exists as of the F4 audit. |
| `F7-21` `Pump Depth (MD)` / `TVD` | `intake_set_depth_*` | **`pump_setting_md_ft`** / **`pump_setting_vd_ft`** | A pump-setting depth by its printed name. F7 is the first family to print the pair in both MD and TVD. |
| `F7-31` `Avg Flow` | no discriminator | **`point_type = 'design_point'`** | `Flow @Intake` / `@Discharge` map to `q_intake` / `q_discharge`; the average is the design point the vendor evaluated Cq/Ch/Cbhp at. No new column. |
| `F7-11`, `F7-25`, `F7-38`, `F7-39`, `F7-40`, `F7-41` | ⚠️G1 | **`stg_gas_cascade`** | **G1 is closed** (CF-16). Strike the ⚠️. |

##### Dropped from the 46 — D22, no contract upgraded, no calculation benchmarked

**Six whole rows: `F7-03`, `F7-04`, `F7-05`, `F7-06`, `F7-19`, `F7-20`. Count 46 → 40.**

The surface- and system-rate group. F7 prints `Water Cut` directly (`F7-16`) **and** downhole flows (`F7-07`, `F7-31`), which is the condition under which every previous family's raw and surface rates were dropped — F1, F2, F4, F5 and F6-13. On that rule: `F7-03` `Total Flow Min/Design/Max`, `F7-05` `Oil / Water / Gas`, `F7-06` `Production Summary` rates and total, `F7-19` `Desired Rate`, `F7-20` `Design Flow (Qsc)`. `F7-04` goes for a different reason — its `BH Temp` and `Pump Depth` duplicate `F7-23` and `F7-21`, and casing and tubing pressures were never proposed. **The section envelope C-BEP wants is `F7-29`, printed as numbers.**

**Sub-field narrowings within rows that otherwise stand:**

- `F7-36` `Pressure @100%H2O` / `Limit` / `Load %` / `Selected`, and `F7-37` `Shaft Load` / `Limit` / `Load %` — housing and shaft **rating** checks that upgrade no contract. **`Description`, `Housing`, `Stages` and `Order` stay** — `Order` is the whole reason §3 calls this the one family with no stacking guess.
- `F7-11` separator `HP` / `ft` · `F7-12` motor `Model` (F2/F5 precedent) · `F7-22` `KOP`, with `TPI Depth (MD)` → `perf_top_md_ft` staying · `F7-33` `Avg Spg Fluid`, an intermediate between the two values that do land (`Spg Fluid` → `fluid_sg_at_observation`, `Avg Pump Spg Fluid` → `vendor_mixture_sg`) · `F7-35` `Intake Shaft Power HP`, cumulative shaft load already covered by `F7-37` · `F7-38` separator `Liquid Flow` and per-separator `Specific Gravity` · `F7-41` `Total Flow` / `Total Liq Flow` / `Specific Gravity`, with `PFG` and `Total Gas Flow` going to the cascade · `F7-42` `Operating HP` / `V` / `F`, the exact set F1 dropped as vendor-derived duplicates · `F7-45` `Total KW`, with `Motor KW` → `vendor_motor_power_kw` staying as the C-ENERGY term · `F7-46` `OD` / `WT` / `Length` / `Part#`, with `Start MD` / `Stop MD` → `top_md_ft` / `bottom_md_ft` staying.

Set the batch script's field-count band against the **CSV's count**, which still carries the six dropped rows; the prompt's inlined field list is what binds the extractor.

## 9. Target tables and cardinality

Extraction writes to **staging**. M5 owns every collapse, normalization and division.

| Staging target | Grain emitted by this contract | Destination |
|---|---|---|
| `stg_pump_config` | **model** (`model_grouped`, CF-28; single scenario) | `pump_config` (well × section × epoch) |
| `stg_design_context` | one row — **single-scenario family** | `esp_well_design_context` (well) |
| `stg_curve_observations` | model × point | `curve_observations` (model × point) |
| `stg_gas_cascade` | cascade stage — **two PHI columns at the intake stage**, Turpin and Dunbar | pump-body rows resolve into `curve_observations.free_gas_at_inlet_pct`; separator stages stay in staging as the audit record (**CF-16 closed**) |
| `stg_alias_evidence` | printed string | the **D31** lookup (a component, not a table) |

> **G1 is closed.** This table previously marked `stg_gas_cascade` as having no destination column. `staging-schemas.md` defines it, and `m2-validator-spec` §9's matching instruction — that M5 must not load cascade rows — is stale for the same reason and still needs correcting.

**D16 and D17 cannot fire on F7.** Single scenario, so the collapse rules are formalities here. If either fires, the document is not what this contract describes.

**F7 is the one family that prints stacking `Order` explicitly**, so `section_order` is read, never inferred — see §3.

## 10. Family validator rules

Global rules `V-01` … `V-20` in [[m2-validator-spec]] apply to every family. The family-specific rules for F7 are `V-F7-*` in that document's §4. Pass B runs both sets.

---

## Not extracted

Per **D22**, the not-extracted list for this family lives in `m1-field-inventory.csv` (`family = Valiant`, `proposed = no`, 25 rows, each with a one-line reason) and in the R1 template atlas. It is **not** restated here and **not** restated per document. A per-document report notes only *anomalies against this contract*.

Corpus-wide exclusions that apply here without exception: **PI** and **PIP** (D11) · **NPSHr** — no mapping from any field, including `Free Allowed Gas` (D12) · motor amperage (D2, duplicates `esp_well_configuration_v2.motor_rated_amps`) · producing GOR/GLR · cable / VSD / transformer / seal / sensor selection strings · design-time tubing and casing setpoints.

---

## Log

| Date | Update |
|---|---|
| 2026-08-14 | Contract issued at M2. Not yet run — M3 pilots it on `HALEY NE G 412H Zone Summary.pdf`. || 2026-08-20 | **Corrected against the staging-schema audit, ahead of the M4 F7 run.** §5 gains the `model_grouped` grain statement (CF-28) with `stages_out` and the non-uniform-housing rule, and the CSV-era `null` language is superseded by omitted keys. §6 gains trap corrections: T3's two PHI indicators resolved by **adding `dunbar_indicator_as_printed`** rather than renaming the Turpin column — the rename would have changed the `stg_gas_cascade` grain and invalidated F1's 30 documents, and was overruled on cost; T4 gains `gas_sg_mixture` because `gas_sg_basis` is a flag on one column and F7 prints two numbers; T2's three viscosity factors gain `viscosity_correction_factors_as_printed`. §7 rewritten — it had told the extractor to emit `library_row_exists` and to use `cross_check` / `contribution`, neither of which is valid; the same errors remain in `f6-xsize.md`. §8 gains row corrections: `F7-08`'s `Discharge Pressure` moves to `stg_curve_observations`, `F7-30`'s `PHI` moves to `stg_gas_cascade`, `F7-21` moves to the `pump_setting_*` pair, `F7-31`'s `Avg Flow` becomes the `design_point`, and six ⚠️G1 markers are struck. Six whole rows dropped under D22 (`F7-03`, `F7-04`, `F7-05`, `F7-06`, `F7-19`, `F7-20` — the surface- and system-rate group), taking the count **46 → 40**, plus sub-field narrowings in eleven further rows, mostly housing and shaft rating checks. §9 rewritten for the closed cascade and the two-PHI stage. Original text left standing throughout; corrections override rather than replace. |
