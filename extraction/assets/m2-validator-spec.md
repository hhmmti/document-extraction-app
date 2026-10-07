---
title: M2 — Validator Specification
created: 2026-08-14
status: issued — implementable without further judgment
milestone: M2
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - validator
  - spec
related:
  - "[[m2-extraction-method]]"
  - "[[m1-extraction-spec]]"
  - "[[design-doc-extraction-plan]]"
  - "[[design-doc-extraction-kickoff]]"
---
	
# M2 — Validator Specification

> [!note] **Precise enough for M5 to implement without further judgment.** Every rule below states its **id**, **scope**, **condition**, **severity** and **action**. Nothing is left to the implementer's taste. The two-pass shape is in [[m2-extraction-method]]; the per-family contracts are in `contracts/`. Rules run in **two places**: Pass B runs them against the source document (§2–§4), and M5 runs the loadable subset again against staging (§5–§6). A rule that can only be checked against the rendered page is marked **PassB-only**; a rule that can only be checked across rows is marked **Load-only**.

---

## 1. Vocabulary

### 1.1 Severity

| Severity | Meaning | Effect on `validation_status` |
|---|---|---|
| `HALT` | The document is not what the contract describes, or an observation is structurally unusable. | `failed`. No corrected block emitted. The family run stops (plan §4, M4: *halt, revise, restart the affected family*). |
| `ERROR` | A field is wrong or missing and the source gives an unambiguous correct value. | `needs_review`. Pass B **corrects** it and emits a corrected block. |
| `WARN` | A discrepancy Pass B cannot resolve from the source alone. | `needs_review`. Pass B **flags** it; `extraction_status = 'conflict'`. Nothing is chosen. |
| `NOTE` | Recorded for downstream use; not a defect. | No change to status. |

### 1.2 Statuses

- `validation_status` ∈ `passed | needs_review | failed` — one per document, in the report frontmatter.
- `extraction_status` ∈ `extracted | validated | conflict` — one per row, per `pump_config` §5 of the plan.
- A document is `passed` only when **every** rule at `HALT`/`ERROR`/`WARN` is clean. `NOTE` findings do not block `passed`.

### 1.3 Scope tokens

`DOC` = once per document · `SCEN` = once per scenario · `SEC` = once per section · `OBS` = once per `stg_curve_observations` row · `CTX` = once per `stg_design_context` row.

---

## 2. Global rules — structure and identity

| id | Scope | Condition (fails when…) | Severity | Action |
|---|---|---|---|---|
| **V-01** | DOC | The page-1 signature in §1 of the family contract is not present on page 1. | `HALT` | Name the signature that was expected and what was found. Do not fall through to another contract. Do not extract. |
| **V-02** | DOC | A page listed as **required** in §2 of the family contract is absent. | `HALT` | Name the missing page by **title**, not number. |
| **V-03** | DOC | A page listed as **optional** in §2 is absent. | `NOTE` | Record in `pages_present`. **Absence of an optional page is never an error** and must not be reported as one. |
| **V-04** | CTX | `bubble_point_psi` is non-null and `bubble_point_unit_as_printed` is null or empty. | `HALT` | **The unit is a required field, not an optional one.** `PSIA` (F1) vs `psig` (F3) vs `psi` (F2/F4/F6/F7) is a 14.7 psi spread; the bubble-point diagnostic's bands are ±10 %, so on a low-pressure well that offset sits inside the decision margin. Reject the row. Never default the token, never infer it from a sibling document. |
| **V-05** | OBS | `vendor_family = 'xsize'` and `assumed_pump_wear_pct` is null. | `HALT` | **Reject the row.** Every F6 curve is printed `Pump Performance - 30% Worn`; an unflagged worn observation biases an ideal-curve fit low by construction and does so plausibly. See `f6-xsize.md` T1. |
| **V-06** | SEC | `pump_model_as_printed` does not match the family's model-grammar regex in §4 of its contract. | `HALT` | Report the string and the regex. Do not repair, do not strip, do not canonicalize — canonicalization is M5 via D31. |
| **V-07** | SEC | A section row is not a pump body: it names an intake, seal/protector, sensor, gas **separator**, motor, cable, tubing, BOD, sand guard, discharge sub, de-sander or tail pipe. | `ERROR` | Delete the row from `stg_pump_config`. **Gas handlers are pump bodies and are in scope** — do not delete those. |
| **V-08** | SEC | `section_order` is not a contiguous 1..N with **1 = deepest**, or its direction disagrees with the printed depth columns. | `ERROR` | Correct from the depth columns. Record `printed_order_direction`. |
| **V-09** | SEC | `housing_count`, `stages` and `stages_basis` are mutually inconsistent — see §3.1. | `WARN` | Flag both numbers, set `extraction_status = 'conflict'`. **Do not pick one.** |
| **V-10** | DOC | A field appears in the output that is not a `proposed = yes` row of `m1-field-inventory.csv` for this family. | `HALT` | Name the field. Scope was widened; the contract, not the output, must change. |
| **V-11** | DOC | A `proposed = yes` field for this family is absent from the output **and** absent from the report's `fields_not_found` list. | `ERROR` | Every proposed field is either extracted or explicitly listed as not found on the page. Silence is not an answer. |

---

## 3. Global rules — values

### 3.1 Stage arithmetic (`V-09` detail)

Let `H` = `housing_count`, `s` = per-housing stage count, `T` = the string- or section-total stage count printed elsewhere in the same document.

| Situation | Check | Outcome |
|---|---|---|
| Both `s` and `T` printed, `stages_basis = 'per_housing'` | `H × s == T` | equal → `pass`; unequal → `V-09` `WARN` |
| Only `T` printed | none | `stages_basis = 'total_reported'`, `housing_count = null`, `pass` |
| Per-section rows sum to a printed string total | `Σ stagesᵢ == T_string` | unequal → `V-09` `WARN` |

**Worked case, F5 Baker** — `PRIEST 233H`: summary `ESP B 400 1750 PK **738** STG`; string diagram six housings at **123** STG. `6 × 123 = 738` → `pass`. This is the rule that checks the *identity* rather than trusting either printed number, per M1 open question 10.

**F7 exception:** `Stages Out` is a real deduction and is **not** subtracted at extraction. `V-09` compares against `Total: <n> stages`, never against `stages − stages_out`.

### 3.2 Remaining value rules

| id | Scope | Condition (fails when…) | Severity | Action |
|---|---|---|---|---|
| **V-12** | CTX | `bubble_point_psi` outside `(0, 10000]`. | `WARN` | Set `bubble_point_plausible = false`. **Keep the printed value verbatim** — do not null it, do not correct it. Fires on `Midway 45-46 Unit 2H` (`19985.3 psig` at `GOR 12485.71`), which is derived-from-GOR and must never upgrade `C-BUBBLE` silently. |
| **V-13** | OBS | `flow_as_printed` non-null and `flow_unit_as_printed` null. | `ERROR` | `bbl/d` and `STB/D` are different quantities (downhole vs stock-tank) and are **never reconciled at extraction** (D8). A flow without its unit is unusable. |
| **V-14** | OBS | `head_as_printed` non-null and (`head_unit_as_printed` ≠ `ft` **or** `head_basis_as_printed` null). | `ERROR` | Head stays in feet (D9). `head_basis_as_printed` ∈ `per_stage | per_section_total | string_total` and is mandatory — without it a section total silently reads as a per-stage value. |
| **V-15** | OBS | `power_as_printed` non-null and `power_basis_as_printed` null. | `ERROR` | Same reasoning as `V-14`. |
| **V-16** | OBS/CTX | Any value shows evidence of extractor-side arithmetic: a per-stage head where the page prints only `Lift` and `Stages`; a unit-converted value; a frequency normalized to 60 Hz; a resolved conflict. | `HALT` | **D8.** Extraction is verbatim. Emit the operands separately and let M5 divide. Re-extract the affected fields. |
| **V-17** | SCEN | `scenario_title_as_printed` carries a frequency and it differs from the table-sourced `frequency_hz_as_printed`, and `scenario_title_frequency_disagrees` is not `true`. | `ERROR` | Set the flag. **Table value wins, always, with no tolerance.** `HALEY NE I 154H` is 10 Hz out; `THUNDERBALL 323H` is 0.03 Hz out — the small one is the dangerous one because it passes any plausibility check. |
| **V-18** | SCEN | `frequency_hz_as_printed` was taken from a page title rather than a table. | `HALT` | Re-extract from the table. |
| **V-19** | CTX | `depth_reference_basis` is null, or is `intake` on a `baker_prolift` document. | `HALT` | F5 prints `Setting Depth from pump discharge`; its basis is the **literal** `pump_discharge`, never inferred and never defaulted. Every other family sets its own basis literally per its contract. |
| **V-20** | CTX | `motor_power_factor` is populated from a value printed under a **surface equipment** block. | `HALT` | ChampionX prints `Operating Power Factor 0.811` (p6, motor) and `Power Factor 0.77` (p7, surface). Different quantities; `C-BHP` wants the motor one. Re-extract from p6 and set `motor_power_factor_basis = 'operating'`. |

---

## 4. Family rules

Pass B runs the global set **and** its family's set.

### V-F1-* — SpyGlass

| id | Condition | Severity | Action |
|---|---|---|---|
| `V-F1-1` | The same model string differs across `Design Schematic`, `Pumps` and `Single Pump Charts`. | `WARN` | Record all three. Do not pick. |
| `V-F1-2` | A rate carries the `*` marker and `viscosity_corrected` is not `true`. | `ERROR` | Set the flag. `* denotes corrected viscosity rate`. |
| `V-F1-3` | `Lift (ft)` emitted with `head_basis_as_printed ≠ 'per_section_total'`, or a per-stage head appears anywhere in F1 output. | `HALT` | Per-stage head is `Lift ÷ Stages` and that division is M5's (D8). |
| `V-F1-4` | `Free Allowed Gas` mapped to `NPSHr`, `npshr_ft`, or any cavitation-margin field. | `HALT` | **D12.** It is gas-handling capability. Record under its own name only. |
| `V-F1-5` | `bubble_point_unit_as_printed ≠ 'PSIA'`. | `WARN` | F1 prints `PSIA` at n=4. A different token may be real — verify on the page, then record what is printed. |
| `V-F1-6` | Motor amperage appears in the output. | `ERROR` | Delete. D2 — duplicates `esp_well_configuration_v2.motor_rated_amps`. |
| `V-F1-7` | **PassB-only.** A bottomhole / reservoir temperature is printed on `Design Overview` or `Theoretical Production Data`. | `NOTE` | **Record the finding and the printed label. Do not extract it.** F1's inventory proposes only `Surface Temperature`; see the umbrella's *Inventory findings*. This note is how M3 settles whether the inventory has an omission. |

### V-F2-* — ChampionX

| id | Condition | Severity | Action |
|---|---|---|---|
| `V-F2-1` | Only one power factor found and its source page was not recorded. | `HALT` | See `V-20`. Never assume an unattributed PF is the motor one. |
| `V-F2-2` | `Fluid Composite SG` and `Liquid Phase SG` collapsed into one value, or either emitted without `sg_basis`. | `ERROR` | Emit both, each with its basis. `sg_for_dp` takes composite; the `calc_mixture_sg` benchmark takes liquid-phase. |
| `V-F2-3` | Numeric BEP or ROR values appear in F2 output. | `HALT` | They exist **only as plot annotations** at n=5 — the text layer holds `MinOperatingFlow / BEPFlow / MaxOperatingFlow` with no numbers. A number here was read off a plot or invented. Set `envelope_status = 'graphical_only'` instead. |
| `V-F2-4` | `pump_model_as_printed_alt` shows the `MSC_` prefix stripped or the underscores reassembled. | `ERROR` | Verbatim (D8). Reassembly is D31's, at M5. |
| `V-F2-5` | `scenario_ordinal > 1` or `scenario_label_staged` non-null. | `HALT` | F2 is single-scenario. A second scenario means the document is not what this contract describes. |

### V-F3-* — SLB

| id | Condition | Severity | Action |
|---|---|---|---|
| `V-F3-1` | `Staging Configuration` folded into `pump_model_as_printed` or into `proposed_canonical`. | `ERROR` | Move it to `staging_configuration_as_printed`. It is a build attribute, not identity — decided in `f3-slb.md` §4. |
| `V-F3-2` | `vendor_mixture_gradient_psi_per_ft` differs from the SG-implied gradient (computed from this document's own `Oil Gravity`, `Water Spec. Gravity`, `Water Cut`) by more than 5 %. | `NOTE` | Record `mixture_gradient_sg_implied_psi_per_ft` and the delta as `F3-GRADIENT-DIVERGENCE`. **A finding, never a correction.** Fires on `Midway 2H` (printed `0.433` vs implied ≈ `0.464`). |
| `V-F3-3` | `Mixture Gradient` used as an input to `C-HYD`, or an SG derived from it. | `HALT` | Benchmark only (D13), until M6 resolves it. |
| `V-F3-4` | `motor_power_factor` non-null. | `WARN` | No power factor is printed anywhere in F3 (0/5). A value here came from `Load Factor` or `Slip`, which are motor detail with no contract. Set null and `power_factor_absent_confirmed = true`. |
| `V-F3-5` | Head observations appear in F3 output. | `HALT` | 0/5 on BEP and ROR; seven pages are chart-only. `envelope_status = 'absent'`. |
| `V-F3-6` | Deepest section's `bottom_md_ft` differs from `Intake Depth` by more than 500 ft, or is under 1,000 ft on a well whose intake depth exceeds 5,000 ft. | `ERROR` | The wrong column was read — an assembly-relative running tally instead of MD from the wellhead. **This is Track 1's second failure mode.** Re-read the `Bottom Depth` column. |
| `V-F3-7` | `PERMIA~1.PDF` and `well_name_source ≠ 'document_body'`. | `ERROR` | Its 8.3 short filename carries no usable well token; the body does. |

### V-F4-* — ELS

| id | Condition | Severity | Action |
|---|---|---|---|
| `V-F4-1` | `Calculated Pb` present on the page but `bubble_point_calculated_flag` null. | `ERROR` | Extract it verbatim. It is the **only explicit D14 provenance flag in the corpus**. |
| `V-F4-2` | `bubble_point_provenance` disagrees with the flag: `No` must give `vendor input`, `Yes` must give `vendor derived`. | `ERROR` | Set from the flag. Never guess the flag itself. |
| `V-F4-3` | `Free gas at Intake` emitted without the `mcf/d` unit token. | `HALT` | Unique to F4 — every other family prints a percentage. A unitless `8.43` reads as 8.43 % free gas: wrong by orders of magnitude and plausible enough to survive review. |
| `V-F4-4` | `manufacturer` taken from the footer, the filename or `vendor_family` rather than from the printed **`Manufacture`** field. | `ERROR` | Re-extract. **Track 1's first failure mode**, and it is invisible here because Endurance authors the report *and* makes the pump. |
| `V-F4-5` | `Series` and `Model` concatenated into one token. | `ERROR` | Three separate printed fields, three separate columns. Concatenation is D31's at M5. |
| `V-F4-6` | `top_md_ft` or `bottom_md_ft` non-null. | `WARN` | F4 prints no per-section depths. A value here was derived from `Pump Setting Depth` — which is arithmetic (D8) and wrong at the section grain. |
| `V-F4-7` | `motor_power_factor` non-null. | `WARN` | 0/3 in this family. Same handling as `V-F3-4`. |

### V-F5-* — Baker ProLift

| id | Condition | Severity | Action |
|---|---|---|---|
| `V-F5-1` | `motor_type = 'permanent_magnet'` and `motor_frequency_hz / pump_shaft_frequency_hz` ∉ `[1.9, 2.1]`. | `WARN` | The 2× relationship is the family's signature (`103.18` vs `51.59`). A ratio near 1.0 means one of the two was mis-read. |
| `V-F5-2` | `frequency_hz_as_printed` or `design_frequency_hz` populated from **`Motor Frequency`**. | `HALT` | Take `Pump Shaft Frequency`. A 2× frequency error is a 2× flow error and a **4× head error** — large enough to read as a different pump rather than as a bug. |
| `V-F5-3` | `motor_type` null. | `ERROR` | Required for this family. It is what makes the 2× physics rather than a typo. |
| `V-F5-4` | `depth_reference_basis ≠ 'pump_discharge'`. | `HALT` | See `V-19`. Literal, never inferred. |
| `V-F5-5` | The printed discharge-referenced depth was adjusted by a string length before emission. | `HALT` | Arithmetic (D8). M5 does it, using the p8 `Bottom` column. |
| `V-F5-6` | A `stg_pump_config` row originates from the p7 `Sensitivity` table. | `HALT` | Sensitivity cases vary conditions, not equipment. The string comes from p1 and p8 only. Without this rule **D17 fires spuriously on every F5 document**, because `Setting Depth` varies per case. |
| `V-F5-7` | `bubble_point_psi` non-null. | `HALT` | 0/2 — F5 prints no bubble point. A value here was derived from `Static BHP`, importing F1's saturated-reservoir assumption into a family that never made it. Exactly the laundering D14 exists to prevent. Set `bubble_point_absent_confirmed = true`. |
| `V-F5-8` | Head observations, BEP or ROR appear in F5 output. | `HALT` | 0/2 on all three. `Total Dynamic Head` is `string_total` and composite only. |

### V-F6-* — XSize

| id | Condition | Severity | Action |
|---|---|---|---|
| `V-F6-1` | Any curve row missing `assumed_pump_wear_pct`. | `HALT` | See `V-05`. Reject the row. |
| `V-F6-2` | `assumed_pump_wear_pct` taken from the legend string rather than the tabulated `Assumed Pump Wear` field. | `ERROR` | Take the tabulated value; keep the legend as corroboration. |
| `V-F6-3` | A single mixture SG emitted at document level, or the three section SGs averaged. | `HALT` | `0.685 / 0.71 / 0.785` differ **because gas compresses out down the string** — physically correct and irreducible. Emit `fluid_sg_at_observation` per row from that row's own section header. |
| `V-F6-4` | Envelope rows exist at only one frequency. | `ERROR` | Min / BEP / Max are printed at **both 65 Hz and 45 Hz**. Two rows per point type per section. |
| `V-F6-5` | Any F6 value normalized to 60 Hz at extraction. | `HALT` | `K-AFFNORM` is a transform (D8, D10). |
| `V-F6-6` | The three TDH grains summed, reconciled, or reported as an error. | `HALT` | 9,271 ft (per-model sum) vs 6,578 ft (system, prod pt 1) is **open and deferred to M8**. Emit `F6-42`, `F6-20` and `F6-12` as three separate fields so Test A can compare like with like. |
| `V-F6-7` | `motor_power_factor_basis ≠ 'max'`, or a PF read off the `Motor Composite Curve` plot. | `ERROR` | The tabulated value is a **maximum**, not an operating point. |
| `V-F6-8` | Per-section `Intake Pressure` written to any `pump_intake_pressure_psi` column or to `stg_design_context`. | `HALT` | **D11.** It is a section boundary and lives only on the observation row, as `section_intake_pressure_psi_as_printed`. |
| `V-F6-9` | The absence of a text layer reported as a defect, or an OCR/text-extraction fallback attempted. | `NOTE` | Expected. The extractor reads rendered pages. |

### V-F7-* — Valiant

| id | Condition | Severity | Action |
|---|---|---|---|
| `V-F7-1` | `bubble_point_correlation` null or set to `Standing`. | `ERROR` | F7 prints `Pb/Rs: Al-Shammasi (1999)` — **not** the correlation the app implements. Recording the name is the whole point of the benchmark. |
| `V-F7-2` | The printed `Pb` compared to, or corrected toward, a Standing result. | `HALT` | D13 — benchmark, never an input, and never reconciled at extraction. |
| `V-F7-3` | `Avg Cq / Avg Ch / Avg Cbhp` omitted because all three are `1.0000`. | `ERROR` | The all-ones value **is** the datum: it is the tell that the printed curve is viscosity-**un**corrected. Emit all three and set `viscosity_corrected = false`. |
| `V-F7-4` | `Dunbar PHI` dropped. | `ERROR` | Both PHIs are emitted. Dunbar has no consumer today and is the corpus's only cross-check on D26's choice of Turpin. |
| `V-F7-5` | `gas_sg` emitted without `gas_sg_basis`, or `Spg Gas (Mix)` used for `gamma_g`. | `ERROR` | `C-BUBBLE` wants the **hydrocarbon** basis. F7 is the only family printing both. |
| `V-F7-6` | The three temperatures collapsed to one column. | `ERROR` | `Temp @ Intake` / `@ Reservoir` / `@ Surface` → three columns. |
| `V-F7-7` | `stages_out` subtracted from `stages`. | `HALT` | Arithmetic (D8). Emit both. |
| `V-F7-8` | `section_order` not taken from the printed **`Order`** column when that column is present. | `ERROR` | F7 is the only family that states the stacking explicitly. Use it. |
| `V-F7-9` | Per-section `Pressure @Intake` written to any PIP column. | `HALT` | Same as `V-F6-8`. |
| `V-F7-10` | Envelope flow emitted with `flow_unit_as_printed ≠ 'bbl/d'`. | `WARN` | F7's envelope prints `bbl/d` where F1's prints `STB/D`. Record as printed; never reconcile. |

---

## 5. Fail-loud rules — D16

**Load-only.** Runs at M5 on `stg_design_context`, grouped by `well_id`.

### 5.1 Rule

For each column of `esp_well_design_context`, compare its value across all scenarios of one document.

```
if n_distinct(value) == 1                       -> pass
elif column in SCENARIO_VARIANT_ALLOWLIST       -> D16-WARN   (resolve by selector, record variance)
else                                            -> D16-HALT   (do not load; reject to d16_conflicts)
```

### 5.2 `SCENARIO_VARIANT_ALLOWLIST`

Exactly these columns. Adding to this list is a decision, not a fix.

| Column | Why it legitimately varies |
|---|---|
| `bubble_point_psi` | Tracks static datum pressure across drawdown scenarios (F1). **Observed.** |
| `static_datum_pressure_psi` | The scenario *is* a drawdown case (F1 `1000 pip` / `500 pip`). |
| `water_cut_design_frac` | F3 `Initial / Future / Max` are life-of-well cases; F5 sensitivity cases vary it deliberately. |
| `oil_api_deg`, `water_sg`, `gas_sg` | F1 prints these `per_scenario`; a life-of-well case may re-enter them. |
| `reservoir_temp_f`, `intake_temp_f`, `surface_temp_f` | F3 prints these `per_scenario`. |

`design_frequency_hz`, `design_frequency_min_hz` and `design_frequency_max_hz` are **not** on the list and must not be: they are scenario-grained by nature and belong to `curve_observations`, not to the invariant sidecar. If they reach `stg_design_context` at all, that is an extraction error, not a D16 event.

### 5.3 The bubble-point load rule — stated so M5 does not invent one

**D16 will fire on real data.** `Oryx Roan State H 1303H` prints `Bubble Point 1,800 PSIA` in its `1000 pip` scenario and `1,200 PSIA` in its `500 pip` scenario, tracking `Static Datum Pressure` exactly. This is **designed behaviour, not a defect.**

The well-grained sidecar row takes:

1. **Value** — the bubble point of the scenario where `is_design_scenario = true`, selected by the family contract's §5 ladder:
   - **F1** — the scenario whose surface rate matches the `Design Overview` design rate → else the scenario whose *table* frequency matches the document-level operating frequency → else `scenario_ordinal = 1`.
   - **F3** — the named case **`Initial`** (as-installed; `Future`/`Max` are look-aheads).
   - **F5** — the **p1 `ProLift Summary`** base case; never a p7 sensitivity row. *(F5 has no bubble point, so this rung is defensive.)*
   - **F6** — bubble point is printed **once at document level**; D16 cannot fire.
   - **F2, F4, F7** — single-scenario; D16 cannot fire.
2. **`bubble_point_scenario_variance` = `true`.**
3. **`bubble_point_psi_min` / `bubble_point_psi_max`** — the observed span, retained.
4. **`scenario_selection_rule`** — which rung fired, verbatim.
5. **`provenance_json`** gains `{"bubble_point_psi": {"selected": <v>, "all_values": {<scenario>: <v>, …}, "selection_rule": "<rule>"}}`.
6. **`bubble_point_equals_static_datum`** is evaluated **per scenario** and set `true` only if it holds in **every** scenario. On `Oryx Roan` (1800/1800 and 1200/1200) it is `true` — which is the finding: this is a saturated-reservoir assumption typed by the sizing engineer, not a PVT measurement.

The same six-step treatment applies to every other allowlisted column, with the same selector.

### 5.4 D16 demonstration (plan §4, M5 DoD)

M5 must show D16 **firing**, not merely implemented. `Oryx Roan State H 1303H` is the demonstration case for `D16-WARN`. For `D16-HALT` no natural case is known; construct one by injecting a varying `intake_set_depth_md_ft` into a staged multi-scenario document and show the well rejected to `d16_conflicts`.

---

## 6. Fail-loud rules — D17

**Load-only.** Runs at M5 on `stg_pump_config`, grouped by `well_id`.

### 6.1 The identity tuple

```
identity = (section_order, section_role, pump_model_as_printed, stages, housing_count)
```

Build the ordered set of identity tuples per scenario and compare across scenarios.

```
if all scenarios yield identical identity sets
        -> collapse; scenario_label_staged = null; extraction_status = 'validated'
elif identity sets identical but non-identity fields differ (top_md_ft, bottom_md_ft, ...)
        -> collapse on identity; take the is_design_scenario row's values for the
           differing fields; extraction_status = 'extracted'; record the variance
else
        -> D17-HALT
```

### 6.2 `D17-HALT`

Do **not** collapse. Retain every scenario variant with `scenario_label_staged` populated, set `extraction_status = 'conflict'`, and list the well in `pump_config_conflicts`.

Per plan §7 risk 7 this is the signal that **a document is presenting equipment *options* rather than an installed string** — the assumption D17 was written to guard. It needs a human decision on whether `pump_config` gains a `selected` flag; it does not need a rule. Owner: **Hamed, at M5.**

### 6.3 Known-safe exception — F5

F5's p7 `Sensitivity` table varies `Setting Depth` per case but **never the pump string**. `V-F5-6` forbids emitting a `stg_pump_config` row from p7 at all, so no F5 sensitivity case ever reaches the D17 comparison. Without that rule D17 would fire spuriously on both F5 documents.

### 6.4 D17 demonstration

`Oryx Roan State H 1303H` (two scenarios, one string) demonstrates the **collapse** path. For `D17-HALT`, inject a differing `stages` value into one staged scenario and show the well rejected to `pump_config_conflicts`.

---

## 7. Cross-document rules

**Load-only**, run once per family after M4.

| id | Condition | Severity | Action |
|---|---|---|---|
| **V-X1** | A field populated in ≥ 80 % of a family's documents is null in this one. | `NOTE` | Flag for verification. Track 1's 1B check 12, and it is what catches a page silently skipped. |
| **V-X2** | Two documents give the same `well_id` a different `effective_from`. | `NOTE` | Expected — **D18**: latest wins, older rows retained. Not a defect. |
| **V-X3** | A `pump_model_as_printed` appears that has no row in `m2-d31-seed-aliases.csv`. | `NOTE` | Append it to the D31 lookup with its basis. The seed is a seed, not a closed set. |
| **V-X4** | A `curve_observations` head/BEP/ROR row for a model in the MC-13 carries `curve_observation_role ≠ 'cross_check'`. | `ERROR` | The library row is the source of truth for the 60 Hz head curve and the BEP/ROR envelope. Power and efficiency rows for the same model stay `contribution` — that is these documents' unique addition (amendment 8). |
| **V-X5** | A `cross_check` row overwrote a library head-curve or envelope value. | `HALT` | Never permitted. |
| **V-X6** | `Free Allowed Gas` or any other field mapped to `NPSHr` anywhere in the load. | `HALT` | **D12**, corpus-wide. |
| **V-X7** | `Productivity Index`, `PI`, `Pump Intake Pressure`, `PIP`, `Calculated Pip` or `Desired Pip` present in any loaded table. | `HALT` | **D11**, corpus-wide. The per-section boundary pressures of F6/F7 are exempt **only** under their contract-specified name and only on observation rows (`V-F6-8`, `V-F7-9`). |

---

## 8. Report contract

> [!warning] **Amended 2026-08-18 — CSV replaced by JSONL.** CSV cost four prompt versions and never converged: rows came back short by 1, 2, 3 and 6 fields against 31- and 46-column headers, every gap silently shifting later values onto the wrong columns, and unquoted thousands separators splitting cells. A missing value in CSV is positional and therefore invisible; in JSONL it is an absent key. The format was the defect, not the model.

Pass A and Pass B both write to `21/tapered_pumps/m3-pilot/reports/<document-slug>.md` (M3) or `…/m4-run/reports/` (M4).

**Frontmatter, required:**

```yaml
---
tags:
  - data_extraction
  - design-docs
  - tapered_pumps
  - report
source_document: <filename as on disk>
vendor_family: spyglass | championx | slb | els | baker_prolift | xsize | valiant
contract: f1-spyglass.md | ...
well_name: <as printed in the document body>
well_name_source: document_body | document_header
well_id: <pre-matched, or null>
well_id_match_status: exact | fuzzy | unmatched
effective_from: YYYY-MM-DD
n_scenarios: <int>
n_sections: <int>
pages_present: [<page titles>]
extraction_date: YYYY-MM-DD
prompt_version: <int>
validation_status: <set by Pass B only>
---
```

**Body, required, in order:** wikilink to `[[m2-extraction-method]]` and to the family contract · a *Document identity* block · a *Sections* table · a *Design context* block · a *Curve observations* table · a *Gas cascade* table · *Fields not found* (every proposed field absent from the page, `V-11`) · *Schema gaps* (any proposed field whose contract target column does not exist — never written into prose, see `f1-spyglass.md` §8) · *Extraction notes* (ambiguity, conflict, anomaly against the contract — **never** a not-extracted list, D22).

##### Data blocks — JSONL

Five blocks, in this order, each introduced by a heading written exactly as shown, with no backticks and no extra words:

```
## stg_pump_config
## stg_design_context
## stg_curve_observations
## stg_gas_cascade
## stg_alias_evidence
```

Each heading is followed immediately by a fenced ` ```jsonl ` block. Rules:

1. **One JSON object per line.** No wrapping array, no commas between lines, no pretty-printing. A line is a complete object or it is discarded.
2. **Keys are the staging schema's column names**, spelled exactly. A key not in the schema is a `V-10` violation. Key *order* within an object does not matter — only spelling.
3. **Omit absent keys entirely.** Do not emit `"key": null`, `""`, `"-"` or `"n/a"`. An absent key means the document does not print that value; that is a fact, and it is recorded in *Fields not found*. This is what replaces CSV's positional padding.
4. **Every value is a JSON string, verbatim as printed, including its unit token.** `"head_as_printed": "4,903.92 ft"`. Commas inside a value are ordinary characters and need no special handling. Do not coerce to number, do not strip separators or units, do not reformat.
5. **A grain with no data emits an empty fenced block** — the fence with nothing between it.

Example, `stg_pump_config`:

```jsonl
{"section_order": "1", "section_role": "gas_handler", "pump_model_as_printed": "SFGH2500 TS4 XR (HS Shaft)", "manufacturer": "Summit", "vendor_family": "spyglass", "housing_count": "15", "stages": "75", "stages_basis": "per_housing", "source_document": "DOC SEUSS B 332H DESIGN.pdf", "source_page": "p2", "scenario_label_staged": "1650bpd 54.64hz 1500pip", "scenario_ordinal": "1", "extraction_status": "extracted", "provenance": "vendor input"}
{"section_order": "2", "section_role": "primary", "pump_model_as_printed": "SF1750 TS4 XR (HS Shaft)", "manufacturer": "Summit", "vendor_family": "spyglass", "housing_count": "15", "stages": "124", "stages_basis": "per_housing", "source_document": "DOC SEUSS B 332H DESIGN.pdf", "source_page": "p2", "scenario_label_staged": "1650bpd 54.64hz 1500pip", "scenario_ordinal": "1", "extraction_status": "extracted", "provenance": "vendor input"}
```

`organization_id`, `well_id` and `effective_from` are populated at load and are normally absent from Pass A output.

##### Completeness footer

The last thing in the file, so a parser can detect truncation:

```
## row_counts
```jsonl
{"stg_pump_config": <n>, "stg_design_context": <n>, "stg_curve_observations": <n>, "stg_gas_cascade": <n>, "stg_alias_evidence": <n>}
```
```

A report without this footer stopped mid-stream and is rejected, not skipped.

**Pass B appends** a `## Validation` block and sets `validation_status`. Where findings exist it appends **corrected** JSONL blocks after the validation block. **The corrected blocks are the preferred artifact** for aggregation wherever they exist — Pass A's originals are retained, never edited.

---

### 9. Implementation notes for M5

- Rules are **ordered**: `HALT` rules short-circuit; a document failing `V-01` produces no other findings.
- `V-10` and `V-11` together enforce the coverage guarantee: the output field set equals the contract field set, exactly. They are the mechanical form of "no field is orphaned and none is duplicated".
- The regexes in §4 of each contract are the normative model grammars. Keep them in one module, keyed by `vendor_family`; **never write one regex for the corpus** (plan §4, M2 Do-NOT).
- `stg_gas_cascade` has **no destination column** in the three §5 schemas (gap **G1**, ledger CF-16). Until that is closed, the validator checks its rows for internal consistency only and M5 does not load them.
- **JSONL parsing (§8, from 2026-08-18):** parse each line independently and discard only the line that fails, never the block. An absent key means the value was not printed — it is **not** an error, and it must not be defaulted at load. A key not in the schema is `V-10`. Every value arrives as a verbatim string with its unit token attached; type coercion and unit handling belong to the transform, not the parser.

---

### Log

| Date | Update |
|---|---|
| 2026-08-14 | Validator specified at M2. 20 global rules, 53 family rules (F1 7 · F2 5 · F3 7 · F4 7 · F5 8 · F6 9 · F7 10), 7 cross-document rules, plus the D16 and D17 load rules with their demonstration cases. Not yet implemented — M5 implements; M3 runs the Pass B subset by hand. |
| 2026-08-18 | **§8 amended — CSV replaced by JSONL.** Four F1 prompt versions failed to make CSV rows field-count-correct: deltas of −1, −2, −3, −6 and +1, +2 across 586 rows, each gap silently shifting values onto neighbouring columns, plus unquoted thousands separators splitting cells. A missing value in CSV is positional and invisible; in JSONL it is an absent key. Sparse objects · every value a verbatim string · one object per line · empty block for an empty grain · `row_counts` footer as the truncation gate. Body gains a required *Schema gaps* section so a field with no destination column is reported rather than buried in prose. §9 gains a JSONL parsing note. |
| 2026-08-18 | *Correction, recorded not rewritten:* while applying the amendment above, Claude read `vault_get_document_map`, saw no §9 or Log heading, concluded both had been destroyed by the heading-scoped replace, and appended copies. **Neither had been destroyed** — the map had simply not returned them. The duplicates were then removed and this single copy retained. No rule text was lost at any point; the §8 edit itself was correct. |
