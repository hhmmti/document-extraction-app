---
title: "F2 ChampionX — Extraction Contract"
created: 2026-08-14
status: issued — M3 runs this unchanged
milestone: M2
family: F2
vendor_family: championx
documents: 5
proposed_fields: 42
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - contract
  - championx
related:
  - "[[m2-extraction-method]]"
  - "[[m1-extraction-spec]]"
  - "[[design-doc-extraction-plan]]"
  - "[[design-doc-extraction-kickoff]]"
---

# F2 ChampionX — Extraction Contract

> [!note] **One of seven. Not interchangeable with any other contract.** The umbrella is [[m2-extraction-method]]; the field record is `m1-field-inventory.csv` (42 rows with `proposed = yes`, family `ChampionX`); the decisions are locked in [[design-doc-extraction-kickoff]]. Extraction is **verbatim** (D8) — no unit conversion, no arithmetic, no canonicalization, no conflict resolution. Every transform belongs to M5.

## 0. Applicability

**Vendor:** ChampionX. **Tool:** ESPReport (Telerik Reporting 17.2.23). **Documents: 5.**

`GOUDA FEDERAL COM 605H C1V1_ESPReport.pdf` · `Haley NE F 334H NWT C1Final_ESPReport.pdf` · `Moran 9 Fed Com 603H C12025_ESPReport Jan 23.pdf` · `Moran 9 Fed Com 702H_ESPReport Jan 23.pdf` · `Moran 9 Fed Com 704H C1_ESPReport Jan 18 2025.pdf`

> **This contract must not be merged with F4 ELS.** Both print through Telerik. They share no field names, no page titles and no model grammar. R1 named the combined row "ESPReport" off a filename convention only these five follow.

---

## 1. Page-1 signature — confirm before extracting

Page 1 must show **all** of:

- the document title **`EQUIPMENT AND PERFORMANCE REPORT`**
- a **`ChampionX Representative`** block
- the section headings `WELL DEPTHS`, `OPERATING PERFORMANCE`, `MAIN PUMP`, `FLUID PROPERTIES`, `DOWNHOLE OPERATING PERFORMANCE` on the same page

If page 1 reads `Well Name :` with a `Powered by LiftXP` footer, this is **F4 ELS** — HALT and route to `f4-els.md`. Producer string alone never decides the family.

---

## 2. Pages

**9 pages, fixed. This family does not vary its page count** (n=5).

| Page | Contents |
|---|---|
| p1 | header · `WELL DEPTHS` · `OPERATING PERFORMANCE` · `MAIN PUMP` · equipment strings · `FLUID PROPERTIES` · `DOWNHOLE OPERATING PERFORMANCE` |
| p2 | `Summary` · `Pump Series` block (`PUMP MSC_…` per-housing rows) · accessory blocks |
| p3–4 | `Performance Curve` — plotted, with a text-layer title and `Target Conditions` |
| p5 | `Gas Separator / BOI` block · `Gas Handler` block · `Main Pump` block |
| p6 | `Motor` block |
| p7 | `Surface Equipment` block |
| p8–9 | remaining plots |

No optional page groups. A missing page is a **HALT**, not an absence.

---

## 3. Sections — pump bodies only

Two sources, both required, and they must agree:
- **p1 `MAIN PUMP`** — model + total stages, string level.
- **p2 `Pump Series` block** — per-housing `PUMP MSC_<model>_<n> STG_…` rows with PN, length, weight and set depth. This is where `housing_count` and per-housing `section_order` come from.
- **p5 `Main Pump` / `Gas Handler` blocks** — the design-point hydraulics per body.

**Gas handlers are pump bodies and are in scope** — this family is the reason that rule is stated, because the ChampionX gas handler is the **only** body here that prints a per-stage lift (`Lift / Stage 17.08 ft`, `F2-31`).

Never a section row: `GAS SEPARATOR`, `PROTECTOR`, `MOTOR`, `SENSOR`, `CONTROLLER`, `CABLE`, `De-Sander`, `Tail Pipe`, `Accessories Above ESP`. `F2-09` and `F2-26` name these blocks because the *pump-body* rows inside them are in scope; every non-pump row is carried as **section context only**, never as a `pump_config` row.

`section_order` **1 = deepest**. The p2 `Set Depth` column orders the housings; use it, and record the printed direction.

---

## 4. Model grammar

```
^\d{3}[A-Z]{2,4}\d{2,4}[A-Z]?$
```

Observed: `400UNB35H`, `400DAL1750H`. Digits-first, series embedded in the model token.

**Two grammars in one document, and neither is canonical:**
- p1 `MAIN PUMP` prints `400UNB35H` → `pump_model_as_printed`.
- p2 prints `PUMP MSC_400UNB_35H_93 STG_…` → `pump_model_as_printed_alt`, **verbatim, including the `MSC_` prefix and the underscores**.

**Do not reassemble `MSC_400UNB_35H_` into `400UNB35H` at extraction.** That is a normalization and belongs to M5 via D31 (see `m2-d31-seed-aliases.csv`). Emit both strings and let the lookup do it.

---

## 5. Scenarios

**Single-scenario family.** Every `per_section` row means exactly one row per section. `scenario_label_staged = null`, `scenario_ordinal = 1`, `is_design_scenario = true`.

**Consequence: D16 and D17 cannot fire on F2.** If either does, the document is not what this contract describes — HALT and report it.

---

## 6. Traps — mandatory guardrails

**T1 — ChampionX prints two different power factors and they are different quantities.**
- `Operating Power Factor 0.811` — **p6, Motor block** (`F2-40`). This is the motor power factor. **This is the one `C-BHP` wants**, replacing the `pf = 0.90` default.
- `Power Factor 0.77` — **p7, Surface Equipment block** (`F2-42`). This is surface equipment.

They must **never** substitute for each other. Emit them to separate columns with `motor_power_factor_basis = 'operating'` and a distinct `surface_power_factor` carrying `basis = 'surface'`. If only one is found, record which page it came from and **do not** assume it is the motor one. A power factor extracted from p7 into `motor_power_factor` silently corrupts every BHP proxy for this well.

**T2 — Two SGs printed side by side, and they answer different questions.**
`Fluid Composite SG 0.8162` (`F2-18`) and `Liquid Phase SG 1.0059` (`F2-19`). Emit **both**, each with `sg_basis` (`composite` / `liquid_phase`). Do not choose. The M2 selection rule, stated once in [[m2-extraction-method]] and repeated here because this is the family that forces it:
- `sg_for_dp` (`C-HEADDP`) takes the **composite** value — head is produced against the actual mixture, gas included.
- the `calc_mixture_sg` benchmark (`C-SG`) compares against the **liquid-phase** value — the app's oil+water weighting has no gas term, so composite is the wrong comparator.

**T3 — No numeric BEP and no numeric operating range. This is expected.**
The text layer holds only the reversed label strings `MinOperatingFlow / BEPFlow / MaxOperatingFlow` with **no numbers** (n=5). **Do not read numbers off the plotted curve.** Emit no BEP/ROR rows and set `envelope_status = 'graphical_only'` with `digitization_candidate_page = 'p3-4 Performance Curve'`. This family is the strongest D7 digitization candidate in the corpus; M6 needs the marker, not a guess.

**T4 — Bubble-point unit.**
F2 prints **`psi`**. Required field, never null (`V-04`). Note it is a *different token* from F1's `PSIA` and F3's `psig` — record what is printed, reconcile nowhere.

**T5 — Depth: `Pump Setting MD` / `Pump Setting VD` (p1) are not `Intake Set Depth` (p2).**
`F2-03`, `F2-04` are `vendor input`; `F2-23` is `vendor derived`. Separate columns, separate provenance. `depth_reference_basis = 'pump_setting'` for this family, printed literally.

**T6 — `Target Conditions: <q> bpd at <h> ft` is a whole-string composite point.**
`F2-28`. Set `observation_is_composite = true` and `head_basis_as_printed = 'string_total'`. It is **not** a per-section observation and must never be attributed to a single model.

**T7 — `Regional PVT: United States(Permian Basin)` is provenance, not a number.**
`F2-10` names the PVT basis behind the printed bubble point. Capture the string verbatim into `bubble_point_correlation`-adjacent provenance; it is the only thing that tells `C-BUBBLE` this document's bubble point is a regional correlation rather than a lab measurement.

**T8 — Motor amperage: `F2-38` names nameplate power and voltage. There is no amperage in the proposed set. Do not add one.**

---

## 7. Library-held models — do not re-extract the head curve

`400DAL650H · 400DAL1200 · 400DAL1200H · 400DAL1750H · 400DAL3000H · 400DAL4300H · SD2000 · SF900 · SF1750 · SF2700 · SF4300 · SFGH2500 · SFGH4300`

**This family hits the list — the `400DAL*` models are ChampionX.** Where a section's model matches, set `library_row_exists = true`; head/BEP/ROR rows carry `curve_observation_role = 'cross_check'` and never overwrite a library row. Power and efficiency (`F2-31`, `F2-32`, `F2-33`) carry `role = 'contribution'`.

Given T3, this family produces almost no envelope rows anyway — its real contribution is the gas-handler per-stage lift and power, and the motor electrical block.

---

## 8. Fields to extract

**42 fields.** Every one is a `proposed = yes` row of `m1-field-inventory.csv` with `family = ChampionX`. No field outside this table may be extracted.

| ID | Field, as printed | Page | Cardinality | Class (D14) | Trace | Target |
|---|---|---|---|---|---|---|
| `F2-01` | Customer / Well Name / Project Name | p1 header | `per_document` | vendor input | join key -> well_id (all contracts) | *(join key — all four)* |
| `F2-02` | Well API Number | p1 header | `per_document` | vendor input | join key hardening for well_id resolution | *(join key — all four)* |
| `F2-03` | Pump Setting MD | p1 WELL DEPTHS | `per_document` | vendor input | C-HYD + C-DPREC (well_depth_ft) | `stg_design_context` |
| `F2-04` | Pump Setting VD | p1 WELL DEPTHS | `per_document` | vendor input | C-HYD (true vertical column) | `stg_design_context` |
| `F2-05` | DATUM | p1 WELL DEPTHS | `per_document` | vendor input | C-BUBBLE pressure-reference provenance | `stg_design_context` |
| `F2-06` | Production Rate / BOPD / BWPD / Gas Production | p1 OPERATING PERFORMANCE | `per_document` | vendor input | C-SG (water cut basis) + C-BEP design flow | `stg_design_context` + `stg_curve_observations` |
| `F2-07` | Operating Frequency | p1 OPERATING PERFORMANCE | `per_document` | vendor input | K-AFFNORM (D10 as-printed frequency) | `stg_design_context` + `stg_curve_observations` |
| `F2-08` | MAIN PUMP <model>  <n> Stages | p1 MAIN PUMP | `per_section` | vendor input | C-NARROW + C-IDEAL + K-TIER1 + D31 model grammar | `stg_pump_config` + `stg_alias_evidence` |
| `F2-09` | GAS HANDLER / GAS SEPARATOR / PROTECTOR / MOTOR / SENSOR / CONTROLLER / CABLE (p1 equipment strings) | p1 equipment blocks | `per_document` | vendor input | pump-body rows only (GAS HANDLER carries head); the rest carry no head and are recorded as section context | `stg_pump_config` |
| `F2-10` | Regional PVT | p1 FLUID PROPERTIES | `per_document` | vendor assumption | C-BUBBLE provenance (names the PVT basis behind the printed bubble point) | `stg_design_context` |
| `F2-11` | Oil Api / SpGrOil | p1 FLUID PROPERTIES | `per_document` | vendor input | C-SG (sg_oil) + C-BUBBLE (API) - both printed together | `stg_design_context` |
| `F2-12` | Water Gravity | p1 FLUID PROPERTIES | `per_document` | vendor input | C-SG (sg_water) | `stg_design_context` |
| `F2-13` | Water Cut | p1 FLUID PROPERTIES | `per_document` | vendor input | C-SG (water_cut) | `stg_design_context` |
| `F2-14` | Bottom Hole Temp | p1 FLUID PROPERTIES | `per_document` | vendor input | C-BUBBLE (T_f - replaces the 150 F default) | `stg_design_context` |
| `F2-15` | Surface Fluid Temp | p1 FLUID PROPERTIES | `per_document` | vendor input | C-BUBBLE (T_f gradient endpoint) | `stg_design_context` |
| `F2-16` | Gas Gravity | p1 FLUID PROPERTIES | `per_document` | vendor input | C-BUBBLE (gamma_g - replaces the 0.75 default) | `stg_design_context` |
| `F2-17` | Bubble Point | p1 FLUID PROPERTIES | `per_document` | vendor assumption | C-BUBBLE (selected_bubble_point_psi) | `stg_design_context` |
| `F2-18` | Fluid Composite SG | p1 FLUID PROPERTIES | `per_document` | vendor derived | C-SG benchmark (calc_mixture_sg check) + C-HEADDP (sg_for_dp candidate) | `stg_design_context` |
| `F2-19` | Liquid Phase SG | p1 FLUID PROPERTIES | `per_document` | vendor derived | C-SG benchmark (liquid-only SG vs the apps oil+water weighting) | `stg_design_context` |
| `F2-20` | Total volume at intake / Oil Rate / Water Rate / Gas Rate (DOWNHOLE OPERATING PERFORMANCE) | p1 DOWNHOLE block | `per_document` | vendor derived | K-CURVEFIT (downhole flow at the design point - the curve x-axis) | `stg_curve_observations` |
| `F2-21` | % Free gas at intake / % Free gas at main pump | p1 DOWNHOLE block | `per_document` | vendor derived | K-TIER2P (alpha entering the pump) | `stg_design_context` |
| `F2-22` | Required System TDH / Total System TDH | p1 DOWNHOLE block | `per_document` | vendor derived | K-TIER1 / Test A benchmark | `stg_design_context` + `stg_curve_observations` |
| `F2-23` | Intake Set Depth | p2 Summary | `per_document` | vendor derived | C-HYD depth-provenance disambiguation (intake vs pump setting vs sensor) | `stg_design_context` |
| `F2-24` | Design Frequency / Startup Freq / Min Freq at Departure / Max Frequency | p2 Summary | `per_document` | vendor input | K-AFFNORM (frequency envelope for normalized observations) | `stg_design_context` + `stg_curve_observations` |
| `F2-25` | PUMP MSC_<model>_<n> STG_... PN / Length / Weight / Set Depth | p2 Pump Series block | `per_section` | vendor input | K-TIER1 (per-housing stage counts and order) + C-IDEAL (stages) | `stg_pump_config` + `stg_alias_evidence` |
| `F2-26` | Accessories Above ESP / Gas Handling / Gas Separator / Protectors / Motor / Sensors / De-Sander / Tail Pipe rows | p2 equipment blocks | `per_section` | vendor input | section ordering and role for pump_config; non-pump rows carried as context only | `stg_pump_config` |
| `F2-27` | Performance Curve: <model> / <n> Stages / <f> Hz | p3-4 Performance Curve | `per_section` | vendor input | K-CURVEFIT (names the model + stages + frequency the plotted curve belongs to) | `stg_curve_observations` + `stg_alias_evidence` |
| `F2-28` | Target Conditions: <q> bpd at <h> ft | p3-4 Performance Curve | `per_document` | vendor derived | K-CURVEFIT (one composite design point) + Test A benchmark | `stg_curve_observations` |
| `F2-29` | Gas Separator / BOI block (Model No. / No. of separators / Free gas at intake / at discharge / Intake volume / Discharge volume / Pressure at intake / at discharge) | p5 | `per_section` | vendor derived | K-TIER2P (separation stage in the alpha cascade) | `stg_gas_cascade` ⚠️G1 |
| `F2-30` | Gas Handler block: Model No. / No of Stages | p5 | `per_section` | vendor input | C-IDEAL + K-TIER1 (gas handler is a head-producing body) | `stg_pump_config` + `stg_alias_evidence` |
| `F2-31` | Gas Handler block: Lift / Stage | p5 | `per_section` | vendor derived | K-CURVEFIT (per-stage head - the only per-stage head ChampionX prints) | `stg_curve_observations` |
| `F2-32` | Gas Handler block: BHP Gas Handler Only / Total Power | p5 | `per_section` | vendor derived | K-CURVEFIT (power - the design docs unique contribution per amendment 8) | `stg_curve_observations` |
| `F2-33` | Gas Handler block: Efficiency | p5 | `per_section` | vendor derived | C-EFFPROXY benchmark | `stg_curve_observations` |
| `F2-34` | Gas Handler block: TDH | p5 | `per_section` | vendor derived | K-TIER1 (section head contribution) | `stg_curve_observations` |
| `F2-35` | Main Pump block: Model No. / No Of Stages | p5 | `per_section` | vendor input | C-NARROW + C-IDEAL + K-TIER1 | `stg_pump_config` + `stg_alias_evidence` |
| `F2-36` | Main Pump block: Intake volume / Discharge volume / Pressure at intake / at discharge | p5 | `per_section` | vendor derived | K-CURVEFIT (downhole flow at the design point) + K-TIER1 | `stg_curve_observations` |
| `F2-37` | Main Pump block: Free gas at intake / at discharge | p5 | `per_section` | vendor derived | K-TIER2P (per-section alpha) | `stg_curve_observations` |
| `F2-38` | Motor block: Model No. / Nameplate power / Name plate voltage | p6 | `per_document` | vendor input | C-ENERGY nameplate context | `stg_design_context` |
| `F2-39` | Motor block: Operating Efficiency | p6 | `per_document` | vendor derived | C-ENERGY benchmark (motor efficiency term) | `stg_design_context` |
| `F2-40` | Motor block: Operating Power Factor | p6 | `per_document` | vendor derived | C-BHP (replaces default pf = 0.90) + C-AFFINITY power check | `stg_design_context` |
| `F2-41` | Motor block: Total power demand / Operating Power / kW % @ <f> Hz | p6 | `per_document` | vendor derived | C-ENERGY (motor_power_kw benchmark for the proxy path) | `stg_design_context` |
| `F2-42` | Surface Equipment: Power Factor | p7 | `per_document` | vendor derived | C-BHP disambiguation - surface PF is a different quantity from motor PF and must not be substituted for it | `stg_design_context` |

---

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

Global rules `V-01` … `V-20` in [[m2-validator-spec]] apply to every family. The family-specific rules for F2 are `V-F2-*` in that document's §4. Pass B runs both sets.

---

## Not extracted

Per **D22**, the not-extracted list for this family lives in `m1-field-inventory.csv` (`family = ChampionX`, `proposed = no`, 29 rows, each with a one-line reason) and in the R1 template atlas. It is **not** restated here and **not** restated per document. A per-document report notes only *anomalies against this contract*.

Corpus-wide exclusions that apply here without exception: **PI** and **PIP** (D11) · **NPSHr** — no mapping from any field, including `Free Allowed Gas` (D12) · motor amperage (D2, duplicates `esp_well_configuration_v2.motor_rated_amps`) · producing GOR/GLR · cable / VSD / transformer / seal / sensor selection strings · design-time tubing and casing setpoints.

---

## Log

| Date | Update |
|---|---|
| 2026-08-14 | Contract issued at M2. Not yet run — M3 pilots it on `GOUDA FEDERAL COM 605H C1V1_ESPReport.pdf`. |
