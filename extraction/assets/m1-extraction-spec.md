---
title: M1 — Extraction Spec (demand × supply)
created: 2026-08-14
status: closed — families confirmed, schemas ready for plan §5
milestone: M1
phase: post-VE
tags:
  - extraction
  - design-docs
  - tapered_pumps
  - pump-config
  - permian
  - spec
related:
  - "[[design-doc-extraction-plan]]"
  - "[[design-doc-extraction-kickoff]]"
  - "[[design-doc-extraction-build]]"
  - "[[physics_input_contracts_v1]]"
  - "[[track1-taperedpumps-plan]]"
---

# M1 — Extraction Spec (demand × supply)

> [!note] **The extraction contract, written as a gap analysis.** Demand from [[physics_input_contracts_v1]] §4, supply from the corpus itself. Decisions **D1–D32** are locked in [[design-doc-extraction-kickoff]] and cited, not re-derived; the milestone DoD is [[design-doc-extraction-plan]] §4. The full machine-readable record is `m1-field-inventory.csv` — **495 fields, 290 proposed, 205 not extracted**. §5 of the plan is written from §"Schemas" below; paste it there.
>
> **Headline:** the six vendor families are **eight**. Two of the six splits were unsuspected. Every proposed field traces to a named contract or a named calculation.

---

## Families confirmed

The §3 family table was built off `producer_or_app`. That axis is a print path, and it was wrong in both directions: it **merged** two vendors that print through Chrome, and it **merged** two more that print through Telerik. Classification below is on **page-1 document signature and page structure**, run over all 48 PDFs plus both `.docx` — so family membership is **n=all**, not a sample.

| Family | Vendor / tool | Signature (page 1) | n | Was |
|---|---|---|---|---|
| **F1 SpyGlass** | Summit ESP (Halliburton) | `SIZING REPORT` + `Summit ESP Representative` footer | **30** | 32 (`Skia/PDF`) |
| **F2 ChampionX ESPReport** | ChampionX | `EQUIPMENT AND PERFORMANCE REPORT` + `ChampionX Representative` | **5** | 8 (`Telerik`) |
| **F3 SLB ESPdesign** | SLB / REDA | `Schematics Report` + `SLB Engineer:` + `SLB \| … Report` footer | **5** | 5 (blank producer) |
| **F4 ELS LiftXP** | Endurance Lift Solutions | `Well Name :` block + `Powered by LiftXP` footer | **3** | ⊂ 8 (`Telerik`) |
| **F5 Baker ProLift** | Baker Hughes | `ProLift Summary Report` + `ESP B …` model grammar | **2** | ⊂ 32 (`Skia/PDF`) |
| **F6 XSize** | Extract Production Services | `XSize Design Program` / `An Extract Technology` (image only) | **1** | 1 (`MS Print To PDF`) |
| **F7 Valiant ZONE** | Valiant Artificial Lift Solutions | `Valiant ZONE® Summary Sheet` | **1** | 1 (`MS Print To PDF`) |
| **F8 Procedures** | operator-authored | rig procedure prose | **3** | 3 |

30 + 5 + 5 + 3 + 2 + 1 + 1 + 3 = **50**. ✓

### Evidence per split or merge

**F1 SpyGlass — merge, and page count is not a layout signal.** Read in full at **n=4**: `HALEY NE I 154H DESIGN` (23 p), `THUNDERBALL FEDERAL COM 323H DESIGN` (23 p), `Oryx Roan State H 1303H design` (34 p), `PERMIAN RESOURCES-HALEY NW E 333H-SF5800 DESIGN` (9 p). All four are **one layout**. Page count varies on two independent axes: scenario count (the `Design Schematic`, `Head Curve`, `BHP Curve`, `Single Pump Charts`, `Tapered Pumps` and `Gas Curve` pages repeat once per scenario) and an **optional page set** the sizing engineer enables. `HALEY NW E 333H` at 9 pages is the minimal spine — `SIZING REPORT · Design Schematic · Design Overview · Theoretical Production Data · Pumps · Motor · Multi-Frequency Head Curve · Multi-Frequency BHP Curve · Single Pump Charts` — and every longer document is a superset of it. `Oryx Roan` at 34 pages adds seven optional groups (`Motor Performance`, `Cable Temperature`, `Cross Section Analysis`, `Pressure Traverse`, `IPR`, **`Correlations List`**, `VSD`). One contract covers all 30, with optional-page handling. **R1's 24-way `Skia/PDF m124…m147` spread is a Chrome build number and nothing else.**

**F5 Baker ProLift — split out of `Skia/PDF`.** `ESPD_PERMIAN RESOURCES OPERATING_PRIEST STATE UNIT 233H` and `…_Ramses MIPA Unit 1H` print through Chrome and so landed in the SpyGlass row, but they are Baker Hughes **ProLift** reports: `ProLift Summary Report` / `ProLift Detailed Report`, model grammar `ESP B 400 1750 PK`, prepared by an SLB-unrelated engineer, and an 8-page structure with a **Sensitivity** case table that has no SpyGlass counterpart. This is the split risk #8 predicted, found in the direction nobody flagged — the two files were grouped by the **filename prefix `ESPD_`**, not by their producer, and that prefix spans two vendors.

**F3 SLB — the `ESPD` family was two vendors.** `Permian Resources_Midway 45-46 Unit 2H` (read in full) is SLB/REDA: `SLB | Schematics Report`, `REDA 400 RC1000` / `REDA 400 DN1750`, `Case Comparison Report` with `Initial / Future / Max` scenario columns, `General Report` pages 4-7 and seven chart-only pages. Baker's ProLift shares none of those page titles. Confirmed at n=2 by `PERMIA~1.PDF`, same layout and the same SLB engineer (Sebastian Munoz). Within-family page count varies the same way SpyGlass does — `BRAVE STATE 132H` (11 p) omits the `Case Comparison Report` entirely and goes `Schematics → General Report`; `Midway 2H` (14 p) carries it. **One layout, optional page groups.**

**F4 ELS LiftXP — split out of `Telerik Reporting`.** The eight Telerik files are two unrelated products. Five (`GOUDA 605H`, `Haley NE F 334H`, `Moran 603H`, `702H`, `704H`, all Telerik 17.2.23, 9 pages) are **ChampionX** `EQUIPMENT AND PERFORMANCE REPORT`. Three (`Batman Fed Com 134H`, `Doc Gardner C 19H`, `Moran 9 Fed Com 172H`, Telerik 16.1.22 / 17.0.23, 5-6 pages) are **Endurance**: `Manufacture ELS` / `Model ELS-1750` / footer `Powered by LiftXP`. They share no field names, no page titles and no model grammar. R1 named this row "ESPReport" off a filename convention that only the ChampionX five follow.

**F6 / F7 — the known `Microsoft: Print To PDF` split, confirmed.** `Permian Resources Robin 127H RBOCT25` is **XSize** (Extract Production Services), read as page images (10/10, no text layer). `HALEY NE G 412H Zone Summary` is **Valiant ZONE®**. Nothing in common beyond the print driver.

**Contract count for M2: 7.** F1–F7 each need one extraction contract. **F8 gets none** — see *Corpus dispositions*.

> **Cost note for M2.** The count went 6 → 7 contracts, but the *shape* got easier, not harder: the two new families (F4 ELS, F5 Baker) are the two thinnest documents in the corpus — 28 and 30 proposed fields against SpyGlass's 57 — and neither contributes a single curve observation.

---

## Demand — contracts a design doc can upgrade

Filtered per **D11** to §4 contracts currently resolving Proxy / Fallback / Placeholder. **20 of the 26 active contracts** qualify: 18 carry an explicit fallback, proxy or placeholder in their own contract text, and two more — `Pump candidate narrowing` and `BEP position diagnostic` — carry no fallback but are structurally blocked, because the pump is **guessed from liquid rate** and the BEP diagnostic then inherits the guess. Including those two is the one judgment call in this filter, and it is the one that `pump_config` exists to fix.

Of the 20: **13 get at least one input upgraded**, 1 is upgraded partially, 2 gain a benchmark but no input, and 4 cannot be touched by a design document at all.

The **working target — SG, depth, bubble point, power factor — is confirmed**, and it is not the whole list. The extensions below are not new fields I went looking for; each is a **named default sitting inside one of those four contracts** that the documents also close.

### Confirmed target, and where it lands

| Contract (§4) | Current resolution | Design-doc input | Families carrying it |
|---|---|---|---|
| **Mixture specific gravity** | Fallback `sg_oil=0.85`, `sg_water=1.00` | Oil API / SG, Water SG, Water Cut | **7 / 7** |
| **Hydrostatic pressure correction** | Fallback **`5000.0 ft`** canonical default | Intake set depth (MD **and** TVD) | **7 / 7** |
| **Bubble-Point / Gas Breakout** | Research prototype; Standing from user inputs | Bubble point, stated | **6 / 7** — absent from F5 Baker |
| **Electrical HP / BHP proxy** | Proxy, default **`pf = 0.90`** | Motor power factor | **5 / 7** — absent from F3 SLB (0/5) and F4 ELS (0/3) |

*The `5000.0 ft` figure is the value in `physics_input_contracts_v1` §4 (Hydrostatic pressure correction → Allowed fallback), not the 10,000 ft quoted in the kickoff's §2 evidence table.*

### Extensions — defaults inside those same contracts

| Extension | Where it hides | Design-doc input | Families |
|---|---|---|---|
| **Reservoir / bottomhole temperature** | Bubble-point contract: `T_f` default **150 °F**, "allowed as a suggestion when user confirms" | `Bottom Hole Temp` / `Reservoir Temp` / `Bottomhole Temperature` | **7 / 7** |
| **Gas specific gravity** | Bubble-point contract: `gamma_g` default **0.75** | `Gas Specific Gravity` / `Gas Gravity` / `Spg Gas` | **7 / 7** |
| **Oil API, direct** | Bubble-point contract derives API from `sg_oil` via `(141.5/sg)−131.5` | `Oil API` printed directly, alongside SG in F2/F7 | **7 / 7** |
| **Solution GOR `R_so`** | Bubble-point contract allows producing GOR as an `R_so` proxy **only on user confirmation** | `Rsb` (F7), `Solution Gas-Oil Ratio (Rso)` (F6) | **2 / 7** |
| **`sg_for_dp`** | Ideal head→ΔP conversion: fallback **`1.0`** | Same mixture SG; vendor-computed values printed in F2/F3/F4/F6/F7 | **7 / 7** |
| **Pump identity + stage count** | `Pump candidate narrowing` today **guesses the pump from liquid rate**; `BEP position diagnostic` then inherits the guess | Model + stages per section | **7 / 7** |
| **Power coefficients** | `Ideal pump curve generation` needs `ideal_power_c1…c6`; MC left them null on all 13 converted models (amendment 8) | Per-stage / per-section power | **F1, F2, F3, F6, F7** |

### The 13 upgraded

`Mixture specific gravity` · `Hydrostatic pressure correction` · `Downhole discharge pressure` · `Pump delta-P, recommendation analysis` · `Scenario surface delta-P enrichment` · `Electrical HP / BHP proxy` · `Ideal head-to-delta-P conversion` · `Ideal pump curve generation` · `Pump candidate narrowing` · `BEP position diagnostic` · `Affinity Law validator` · `Energy / Efficiency diagnostic` · `Bubble-Point / Gas Breakout Prototype`

### The 1 partial, and the 4 that cannot be upgraded

| Contract | Why not |
|---|---|
| **NPSH / Cavitation** *(partial)* | **NPSHr has no design-doc equivalent (D12)** and, checked across all eight families, **`vapor_pressure_psi_abs` is printed by none of them**. Its `sg_mixture` input *is* upgraded via the SG contract, so NPSH goes from two placeholders to one — the placeholder NPSHr and the manual vapor pressure both survive. |
| Pump delta-P, preprocessed analysis | Its gap is the `tubing/0.45` **intake** fallback. PIP is excluded by **D11**. |
| Electrical power proxy (`amp_x_volt`) | Needs measured telemetry amps and volts. Design values are the sizing engineer's target, not an observation. |
| Fluid segmentation | Needs a telemetry water-cut / GOR **time series**. A document gives one design point. |
| Gas-Interference Trend Screen | Needs historical trends by construction. |

### The 2 benchmarked, not upgraded (D13)

`Observed efficiency proxy ratio` and `Hydraulic HP proxy` are proxies by construction — no design-doc field replaces them. They get **vendor efficiency and vendor BHP as a check**, which is a *check* contract, not an *input* one.

---

## Supply — field inventory

Per-family summary. `m1-field-inventory.csv` is the full record — field as printed, page location, cardinality, proposed y/n, trace, classification, reason.

**A note on cardinality that the single CSV column cannot carry.** In F1, F3, F5 and F6 the per-section blocks **repeat once per scenario**. The true extraction grain there is *section × scenario*; the CSV says `per_section` and the collapse to well-grain happens at load per **D17**. F2, F4 and F7 are single-scenario documents, so `per_section` there means exactly one row per section.

| Family | Rows | Proposed | Curve observations? | Numeric BEP / ROR? | Per-stage head? | Per-stage power? |
|---|---|---|---|---|---|---|
| **F1 SpyGlass** (n=30) | 111 | **57** | yes | **yes** — `Operating Range Min/Max`, `Best Efficiency`, per-section `ROR (bbl/d)` | derived: `Lift (ft)` ÷ `Stages` | string-level only (`Req BHP`) |
| **F2 ChampionX** (n=5) | 71 | **42** | partial | **no** — plot annotations only | gas handler only (`Lift / Stage`) | gas handler only |
| **F3 SLB** (n=5) | 63 | **39** | no | **no** (0/5) | no | per-section `Required Power` |
| **F4 ELS** (n=3) | 45 | **28** | no | **no** (0/3) | no | string-level (`Total Required BHP`) |
| **F5 Baker** (n=2) | 51 | **30** | no | **no** (0/2) | no | no |
| **F6 XSize** (n=1) | 72 | **48** | **yes — richest** | **yes**, at **both 65 and 45 Hz** | **yes** (`Head per Stage ft/stg`) | **yes** (`Power per Stage HP/stg`) |
| **F7 Valiant** (n=1) | 71 | **46** | yes | **yes** — `Operating Range: Min / BEP / Max` | no | per-section `Pump Power Consumption` |
| **F8 Procedures** (n=3) | 11 | **0** | — | — | — | — |

**37 documents contribute curve observations** (F1 30 + F2 5 + F6 1 + F7 1). **10 contribute `pump_config` and `esp_well_design_context` only** (F3 5 + F4 3 + F5 2). Three are out of scope.

### Per-family notes worth carrying into M2

**F1 SpyGlass.** The `Single Pump Charts` table is the per-section curve source: `ROR (bbl/d)`, `Lift (ft)`, `Q-INT` / `Q-DIS`, `Free Allowed Gas`, `Density @ INT/DIS`. Head per stage comes from `Lift ÷ Stages` on the `Pumps` page. The `Multiscenario Pump Curve` page gives three composite `(flow, head)` pairs per scenario — the Test A target. The optional `Correlations List` page names `Bubble Point → Standing`, which is exactly the correlation `compute/bubble_point_screen.py` implements.

**F2 ChampionX.** Rich on fluid properties and electrical, thin on curves. `Bubble Point`, `Regional PVT: United States(Permian Basin)`, `Fluid Composite SG`, `Liquid Phase SG`, `Oil Api° / SpGrOil` (both printed together) and **two different power factors** — `Operating Power Factor 0.811` on the motor block (p6) and `Power Factor 0.77` on the surface-equipment block (p7). These are different quantities and must not be interchanged; the `bhp_proxy` contract wants the motor one. The main pump prints **no** per-stage lift; only the gas handler does (`Lift / Stage 17.08 ft`). BEP and operating range exist **only as plot annotations** — the text layer holds the reversed label strings `MinOperatingFlow / BEPFlow / MaxOperatingFlow` and no numbers (n=5).

**F3 SLB.** No curve numbers anywhere; seven of its pages are chart-only. Its distinctive contribution is `Mixture Gradient (psi/ft)` — a *direct* hydrostatic gradient, which is what `calc_hydrostatic_pressure_psi` computes from SG. Ambiguous at n=2: `Midway 2H` prints **0.433 psi/ft** while its own `Water Cut 93 %` and `Water Spec. Gravity 1.1` imply ≈ 0.464. Recorded as a benchmark, not an input, until that is resolved.

**F4 ELS.** The thinnest family, and the only one that prints its own provenance: `Calculated Pb` **`No`** alongside `Pb 1800.00 (psi)` (`Batman 134H`, n=1). That flag is a direct D14 classifier — no other family states whether the bubble point was typed or computed. Also note `Free gas at Intake 8.43 (mcf/d)` — free gas in **mcf/d**, not a percentage, unlike every other family.

**F5 Baker.** No bubble point (0/2), no BEP, no ROR, no per-stage anything. Two traps: the motor is a **permanent-magnet** type running at **2× shaft frequency** (`Motor Frequency 103.18 Hz` against `Pump Shaft Frequency 51.59 Hz`, `PRIEST 233H`) — take the shaft value for affinity work; and `Setting Depth from pump discharge 9851.95 ft` is referenced to **discharge**, not intake, unlike every other family's depth.

**F6 XSize.** The richest single document in the corpus, and the only one with no text layer. Per-section pages carry `Head per Stage`, `Power per Stage`, `Efficiency`, `BHP`, `TDH`, and min / BEP / max at **both 65 Hz and 45 Hz**. The full gas cascade is tabulated — 36 % below intake → 34 % at intake → 20 % after separator 1 → 11 % after separator 2 → 7 % after the gas handler → 1 % after the taper → 0 % after the primary — with **Turpin** named and indicators 0.26 and 0.07. **Critical caveat: every XSize curve is plotted `Pump Performance - 30% Worn`, and `Assumed Pump Wear 30 %` is tabulated per production point.** XSize head and power observations are therefore *worn*, not ideal, and must carry that flag or they will corrupt an ideal-curve fit. The per-section SG also differs down the string — 0.685 (gas handler) / 0.71 (taper) / 0.785 (primary) — so "the" mixture SG is genuinely ambiguous in this family.

**F7 Valiant.** Numeric envelope printed cleanly: `Operating Range: Min: 2,000 bbl/d / BEP: 4,600 bbl/d / Max: 5,200 bbl/d`, plus `Efficiency: BEP: 66.63 % / Design: 64.54 %` and a per-housing `Order` column that states the section stacking explicitly. It names its bubble-point correlation as `Pb/Rs: Al-Shammasi (1999)` — **not** Standing, which is what the app implements. It also prints `Turpin PHI` **and** `Dunbar PHI`. `Avg Cq / Avg Ch / Avg Cbhp` are all `1.0000`, which is the tell that the printed curve is viscosity-**un**corrected.

---

## The cross — proposed fields

**290 fields proposed across seven families. Every one carries a trace; none is proposed without one.** Trace codes: `C-*` = a named §4 contract, `K-*` = a named calculation it benchmarks (D13).

| Code | Contract / calculation |
|---|---|
| `C-SG` | Mixture specific gravity |
| `C-HYD` | Hydrostatic pressure correction |
| `C-DISCH` | Downhole discharge pressure |
| `C-DPREC` | Pump delta-P, recommendation analysis |
| `C-BHP` | Electrical HP / BHP proxy |
| `C-EFFPROXY` | Observed efficiency proxy ratio |
| `C-ENERGY` | Energy / Efficiency diagnostic |
| `C-AFFINITY` | Affinity Law validator |
| `C-BUBBLE` | Bubble-Point / Gas Breakout Prototype |
| `C-IDEAL` | Ideal pump curve generation |
| `C-HEADDP` | Ideal head-to-delta-P conversion |
| `C-NARROW` | Pump candidate narrowing |
| `C-BEP` | BEP position diagnostic |
| `K-TIER1` | Tier 1 composite, heads add (M7 / Test A) |
| `K-TIER2P` | Tier 2′ α-degraded composite (M7, D26) |
| `K-WINDOW` | Flow window = intersection, vendor value alongside (D29) |
| `K-CURVEFIT` | `curve_observations` → fitted curve (M6) |
| `K-AFFNORM` | 60 Hz affinity normalization (D10) |

### The load-bearing fields

| Field (as printed) | Family | Trace | Class |
|---|---|---|---|
| `Intake Set Depth` / `Intake Depth (MD)` / `Pump Setting MD` / `Pump Depth (MD)` | F1 F2 F3 F4 F6 F7 | `C-HYD` + `C-DPREC` | vendor input |
| `Intake Depth (TVD)` / `Pump Setting VD` / `Pump Depth TVD` | F1 F2 F7 | `C-HYD` (true vertical column) | vendor input |
| `Setting Depth from pump discharge` | F5 | `C-HYD` — **discharge-referenced**, offset must be carried | vendor input |
| `Oil API` / `Oil Gravity` / `Oil Api° / SpGrOil` | all 7 | `C-SG` + `C-BUBBLE` | vendor input |
| `Water Specific Gravity` / `Water Gravity` / `Spg Water` | all 7 | `C-SG` | vendor input |
| `Water Cut` | all 7 | `C-SG` | input (F1, F7: derived) |
| `Gas Specific Gravity` / `Spg Gas (HC)` | all 7 | `C-BUBBLE` (`gamma_g`, replaces 0.75) | vendor input |
| `Bottom Hole Temp` / `Reservoir Temp` / `Bottomhole Temperature` | all 7 | `C-BUBBLE` (`T_f`, replaces 150 °F) | vendor input |
| `Bubble Point` / `Pb` / `Bubble Point Press` | F1 F2 F3 F4 F6 F7 | `C-BUBBLE` | **assumption** (F3, F7: derived) |
| `Calculated Pb (Yes/No)` | F4 | `C-BUBBLE` provenance — the only explicit D14 flag in the corpus | vendor input |
| `Static Datum Pressure` / `Static Bottomhole Press` | F1 F6 | `C-BUBBLE` provenance test | **assumption** |
| Correlation lists (`Bubble Point Standing`; `Pb/Rs: Al-Shammasi (1999)`; `Solution Gas-Oil Ratio (Rso) — Standing`) | F1 F6 F7 | `C-BUBBLE` provenance | **assumption** |
| `Rsb` / `Solution Gas-Oil Ratio (Rso)` | F6 F7 | `C-BUBBLE` (`R_so`, replaces the producing-GOR proxy) | input / derived |
| `Motor Power Factor` / `Operating Power Factor` / `Power Factor %` / `Pf` / `Max Power Factor` | F1 F2 F5 F6 F7 | `C-BHP` (replaces `pf = 0.90`) + `C-AFFINITY` | **derived** |
| `Power Factor` (surface equipment) | F2 | `C-BHP` **disambiguation** — a different quantity, must not substitute | **derived** |
| Model + stages per section | all 7 | `C-NARROW` + `C-IDEAL` + `K-TIER1` + D31 | vendor input |
| `Lift (ft)` ÷ `Stages` | F1 | `K-CURVEFIT` (per-stage head, ft — D9) | **derived** |
| `Head per Stage (ft/stg)` | F6 | `K-CURVEFIT` + `K-TIER1` | **derived** |
| `Power per Stage (HP/stg)` | F6 | `K-CURVEFIT` — fills `ideal_power_c1…c6`, which MC could not (amendment 8) | **derived** |
| `Operating Range (STB/D) Min / Max`; `Operating Range: Min / BEP / Max`; `Minimum/Maximum Flow at 65 Hz / 45 Hz` | F1 F6 F7 | `C-BEP` + `K-WINDOW` | **derived** |
| `Best Efficiency (STB/D)`; `Best Efficiency at 65 Hz / 45 Hz` | F1 F6 | `C-BEP` (`bep_bpd`) + `K-AFFNORM` | **derived** |
| `ROR Min / ROR Bep / ROR Max` (flow, head) | F1 | Test A composite benchmark + `K-CURVEFIT` | **derived** |
| `Total Dynamic Head` / `Pump TDH` / `Required System TDH` | all 7 | `K-TIER1` / Test A benchmark | **derived** |
| `Total System Efficiency`; `Pump Efficiency`; `Overall Pump Efficiency`; `Efficiency: BEP / Design` | all 7 | `C-EFFPROXY` + `C-ENERGY` benchmark | **derived** |
| `Fluid Composite SG` / `Composite SG` / `Spg Fluid` / `Mixture Gradient (psi/ft)` / `Density @ INT` / `Fluid Density (ρ_L)` | F1 F2 F3 F4 F6 F7 | `C-SG` benchmark + `C-HEADDP` (`sg_for_dp`) | **derived** |
| `% Free Gas at Inlet / at Discharge`; `Free Gas exiting Sep. #1 / #2 / Gas Handler / Taper / Primary`; `GVF at Intake / into Pump` | all 7 | `K-TIER2P` | **derived** |
| `Turpin Indicator at Intake` / `Turpin PHI` / `Dunbar PHI` | F6 F7 | `K-TIER2P` — names the D26 correlation and gives its indicator | **derived** |
| `Free Allowed Gas` | F1 | recorded **under its own name** (D12) — explicitly **not** NPSHr | **derived** |
| `Assumed Pump Wear` | F6 | `K-CURVEFIT` quality flag — 30 % worn | **assumption** |
| `* denotes corrected viscosity rate` | F1 | `K-CURVEFIT` quality flag | **derived** |
| `Avg Cq / Avg Ch / Avg Cbhp` | F7 | `K-CURVEFIT` quality flag (all 1.0000 ⇒ uncorrected) | **derived** |
| Operating frequency (`Operating Frequency` / `System Speed` / `Pump Shaft Frequency`) | all 7 | `K-AFFNORM` (D10 as-printed) | input / derived |

### Classification split (D14)

| Class | Count | Reading |
|---|---|---|
| vendor **input** | 142 | typed by the engineer — casing size, oil API, gas rate, model, stages |
| vendor **derived** | 131 | tool-computed — TDH, efficiencies, free gas %, ROR, per-stage head |
| vendor **assumption** | 17 | an accepted default — bubble point, static pressures, separator efficiencies, pump wear, correlation choices |

**Every derived field above is proposed as a benchmark, never as ground truth**, per D13. The one class that needs watching is `vendor assumption`: it is small (17) and it is where laundering happens. Two concrete cases:

- **Bubble point ≈ static datum pressure.** `HALEY NE I 154H` prints `Bubble Point 3,999.99 PSIA` against `Static Datum Pressure 4,000.00 PSIA`. `Oryx Roan` prints `1,800 / 1,800` and `1,200 / 1,200` across its two scenarios. **n=2 documents, 3 scenario pairs, all equal to the printed digit.** This is a saturated-reservoir assumption typed by the sizing engineer, not a PVT measurement — which is why `static_datum_pressure_psi` and a `bubble_point_equals_static_datum` flag are both in the sidecar schema.
- **SLB's bubble point is computed from the entered GOR and can be nonsense.** `Midway 45-46 Unit 2H` prints `Bubble Point 19985.3 psig` against `GOR 12485.71 SCF/STB`. `PERMIA~1` prints a plausible `2130 psig` at `GOR 2788.46`. Classified **vendor derived** for F3, and it must carry a validator range check before it can upgrade anything.

---

## Not extracted

**205 entries, every one with a reason.** Full list in the CSV; this is the shape of it.

### Corpus-wide exclusions

| Excluded | Reason | Families |
|---|---|---|
| `Productivity Index` / `PI` | **D11** — carries no information; `3.575 / 1.575 / 4.74 stb/d/psi` is exactly rate ÷ 1,000 because drawdown is assumed 1,000 psi in every scenario | all 7 |
| `Pump Intake Pressure` / `PIP` / `Calculated Pip` / `Desired Pip` | **D11** — design-time only, not needed | all 7 |
| **NPSHr** | **D12** — no design-doc equivalent. `Free Allowed Gas` (F1: 36.6 % / 39.3 %) is the nearest thing and is a **different quantity** — gas-handling capability, not cavitation margin. **It is extracted, under its own name, explicitly not mapped.** No defensible NPSHr approximation was found in any of the eight families; recorded as checked and absent. | all 7 |
| Vapor pressure | Searched across all eight families; **printed by none**. The NPSH contract's `vapor_pressure_psi_abs` stays manual. | all 7 |
| `Motor Amperage` / `Motor Amps` / `Name plate current` / `NP A` | **D2** — would duplicate `esp_well_configuration_v2.motor_rated_amps` | all 7 |
| Producing `GOR` / `GLR` | Already `Direct` from production data; not lab `R_so` | all 7 |
| Cable / VSD / transformer / seal / sensor / protector selection strings | No contract | all 7 |
| Clearance checks, DLS surveys, thermal checks, emissions, economics | No contract | all 7 |
| Tubing / casing pressures (design-time setpoints) | Telemetry supplies the measured values | all 7 |

### Family-specific

- **F1 SpyGlass** — the `Gas Curve` pages are plot-only; per-section α is already captured numerically on the `Pumps` page. `IPR` and `Pressure Traverse` optional pages carry no numeric text. `Water Salinity` is zero in every document read (n=4) and no contract consumes it.
- **F2 ChampionX** — `Min Operating Flow / BEP Flow / Max Operating Flow` are **plot annotations only**, no numbers in the text layer (n=5). This is the family that would most benefit from the D7 digitization fallback. `Oil Contraction` chamber percentages are seal sizing.
- **F3 SLB** — seven chart-only pages (`Inflow Performance`, `Inflow/Outflow`, `TDH Curve`, `VSD H-Q Curve`, `Actual Bottom/Top Pump Curve`, **`Catalog Top Pump Curve`**). The last is the only *catalog-basis* curve SLB prints and is the strongest D7 fallback candidate in the family. `Slip` / `Load Factor` are motor detail.
- **F4 ELS** — `Frequency Head chart` / `Frequency Power chart` are plot-only. **No power factor is printed anywhere** (0/3), so F4 cannot upgrade `C-BHP`. `Email` is a literal placeholder string at n=3.
- **F5 Baker** — **no bubble point** (0/2), so F5 cannot upgrade `C-BUBBLE`; a family carrying none of a target field is itself the finding. `Pump Performance` chart is plot-only. `H2S / CO2 / N2` compositional detail has no consumer.
- **F6 XSize** — `Oil FVF (Bo)`, `Gas FVF (Bg)`, `Gas Compressibility (Z)` and all viscosity rows are not consumed by any **active** contract (the app applies no viscosity correction today). `Motor Composite Curve` gives PF only graphically; the tabulated value is a **maximum**, not an operating point.
- **F7 Valiant** — `QHCF / HPCF` are zero at n=1 and unconsumed. `VSD Tornado Curve`, `Freq Charts`, `Inflow Curve`, `Motor Chart` are plot-only.
- **F8 Procedures** — all 11 inventoried fields not extracted; see below.

### Already supplied by the consolidated library — do not extract

MC converted **13 models** and each already carries a degree-5 60 Hz head fit plus `min_recommended_bpd`, `bep_bpd`, `max_recommended_bpd` (amendment 8):

`400DAL650H · 400DAL1200 · 400DAL1200H · 400DAL1750H · 400DAL3000H · 400DAL4300H · SD2000 · SF900 · SF1750 · SF2700 · SF4300 · SFGH2500 · SFGH4300`

**Rule for M2 and M5:** for these models the 60 Hz head curve and the BEP / ROR envelope are **already held and are the source of truth**. Design-doc BEP/ROR for them is loaded to `curve_observations` as a **cross-check only** and must never overwrite the library row. The design documents' **unique** contribution for these models is **power and efficiency** — exactly what MC could not supply. This narrows M6's "overlap models get the better row" to what amendment 8 left open.

*The overlap is material: `SF1750` (485 body tokens), `SF2700` (305), `SF4300` (124) and `SFGH2500` (52) are among the most frequent tokens in the corpus (R1 token counts), while `SF3550` (316) and `SF5800` (90) are **not** in the converted 13 and are genuine extraction targets.*

---

## Corpus dispositions

### `PERMIA~1.PDF` — **in scope**

R1 resolved the well token to `MID STATES EAST UNIT 37 5 6D` with no `nnnH` pattern and flagged it as possibly not a Permian Resources well. Reading it settles both questions:

- **It is a Permian Resources well.** Page 1: `Company: Permian Resources`, `Project: Mid-States East Unit 37-5 6D`, `Company Engineer: Christian Martin`, `Field: Midland, TX`. The operator is named on all 13 pages.
- **It is a normal F3 SLB document.** Same `Schematics Report → Case Comparison Report → General Report` structure, same `REDA 400 RC1000` grammar, and the **same SLB engineer (Sebastian Munoz)** as `Midway 2H` and `BRAVE STATE 132H`.
- **The `6D` is a real well-name suffix, not a defect.** The filename is an 8.3 short name (`PERMIA~1`), which is why R1's token extraction had to fall back to the body. Nothing about the well name affects extractability.

It carries a full demand set — `Oil Gravity 40.5 °API`, `Water Spec. Gravity 1.08`, `Gas Specific Gravity 0.89`, `Intake Depth 9664 ft`, `Bubble Point 2130 psig`, `Mixture Gradient 0.407 psi/ft` — and a three-scenario `INITIAL / FUTURE / MAX` case table. **Keep it. Extract it under the F3 contract.** The one carry-forward is that its `well_id` join will need the body-sourced well name, not the filename.

### The three procedure documents — **out of scope**

`2023.7.14 Truk 23-14 Unit 2H ESP Conversion.docx` · `Thorny Rose 333 Install Procedure.docx` · `Los Vaqueros 300H Procedure FINAL.pdf`

All three read in full. They are **rig execution procedures**, and they fail the extraction test on the one axis that matters:

- **No pump model and no stage count.** The closest any of them comes is Truk's tubing-detail row `4 | Pumps | 4" | Summit | 87 | 8,918` — a *count* of pump bodies, an OD, a vendor name and a running depth. No model, no stages. `pump_config` cannot be populated from that, and without a model there is no curve to attach.
- **No curve data of any kind** — no ROR, no BEP, no head, no power, no efficiency (0/1 on every probe for Los Vaqueros; the two `.docx` carry none either).
- **No fluid properties.** No API, no water SG, no gas SG, no bubble point, no water cut. The demand side gets nothing.
- What they *do* carry — AFE numbers, contact tables, driving directions, safety sections, deviation surveys, economics — has no contract.

**This is exactly the D19 rationale for the Chord install sheets: as-installed execution documents, no curves, different document family.** Excluding them here is consistent with that decision rather than a new one. They stay in the atlas with their not-extracted rows so "add them later" remains a one-line diff (D22).

**Consequence for M2: 7 extraction contracts over 47 documents**, not 8 over 50.

---

## Schemas

Ready to paste into [[design-doc-extraction-plan]] §5. Written from the inventory above, not before it.

### `pump_config` — *(grain: well × section × epoch)*

```sql
organization_id         varchar     -- FK, matches esp_well_configuration_v2
well_id                 varchar     -- FK -> esp_well_configuration_v2.well_id
effective_from          date        -- = design date; one date only, latest wins, older retained (D18)
section_order           int         -- 1 = deepest (intake side), ascending toward discharge
section_role            varchar     -- primary | taper | gas_handler   (pump bodies only)
pump_model_as_printed   varchar     -- verbatim, no normalization (D8)
pump_model_canonical    varchar     -- resolved at load via the alias lookup (D31)
manufacturer            varchar     -- Summit | ChampionX | SLB/REDA | Endurance | Baker Hughes | Extract/XSize | Valiant
vendor_family           varchar     -- spyglass | championx | slb | els | baker_prolift | xsize | valiant
housing_count           int         -- null where the document reports only a total
stages                  int
stages_basis            varchar     -- per_housing | total_reported   (F1 prints both; F5 only the total)
top_md_ft               double      -- null where the document gives no per-section depth
bottom_md_ft            double
source_document         varchar
source_page             varchar
scenario_label_staged   varchar     -- retained from staging; null after the D17 collapse
extraction_status       varchar     -- extracted | validated | conflict
provenance              varchar     -- D14: vendor input | vendor assumption | vendor derived
```

> `esp_well_configuration_v2` already holds a flat `pump_model`. `pump_config` is a **separate table at a different grain** (D1), not a sidecar column, so there is no D2 conflict — but the flat v2 column is exactly the Track-1 failure mode and must not be read as authoritative for a taper.

### `esp_well_design_context` — *(grain: well; sidecar, new columns only)*

Checked against the documented `esp_well_configuration_v2` field set — `organization_id`, `well_id`, `active`, `description`, `edge_device_id`, `telemetry_boundaries`, `telemetry_default_values`, `goal_function`, `total_economics_weights`, `max_step_sizes`, `autopilot`, `virtual_engineer`, `pump_model`, `motor_rated_amps`. **No column below duplicates one of those** (D2); `pump_model` and `motor_rated_amps` are deliberately absent.

```sql
organization_id                     varchar     -- key only
well_id                             varchar     -- PK, FK -> esp_well_configuration_v2.well_id
effective_from                      date        -- = design date (D18)

-- depth  (C-HYD, C-DPREC — replaces the 5000.0 ft canonical default)
intake_set_depth_md_ft              double
intake_set_depth_tvd_ft             double
depth_reference_basis               varchar     -- intake | pump_discharge (F5) | pump_setting
perf_top_md_ft                      double
datum_depth_ft                      double
bottom_of_equipment_md_ft           double

-- fluid  (C-SG, C-HEADDP — replaces sg_oil=0.85 / sg_water=1.00 / sg_for_dp=1.0)
oil_api_deg                         double
oil_sg                              double
water_sg                            double
gas_sg                              double
gas_sg_basis                        varchar     -- hydrocarbon | mixture (F7 prints both)
water_cut_design_frac               double

-- bubble point  (C-BUBBLE)
bubble_point_psi                    double
bubble_point_unit_as_printed        varchar     -- PSIA | psig | psi — verbatim (D8); 14.7 psi matters at this margin
bubble_point_provenance             varchar     -- vendor input | vendor assumption | vendor derived (D14)
bubble_point_correlation            varchar     -- Standing | Al-Shammasi (1999) | null
bubble_point_calculated_flag        varchar     -- F4 'Calculated Pb' Yes/No; null elsewhere
bubble_point_equals_static_datum    boolean     -- the saturated-reservoir tell
static_datum_pressure_psi           double
solution_gor_rso_scf_stb            double      -- replaces the producing-GOR proxy (F6, F7)

-- temperature  (C-BUBBLE — replaces the 150 degF default)
reservoir_temp_f                    double
intake_temp_f                       double
surface_temp_f                      double

-- electrical  (C-BHP, C-ENERGY — replaces pf = 0.90)
motor_power_factor                  double
motor_power_factor_basis            varchar     -- operating | max (F6) | surface (F2 — never substitute)
motor_efficiency_ratio              double
motor_nameplate_hp                  double
motor_nameplate_volts               double
motor_type                          varchar     -- induction | permanent_magnet (F5 runs at 2x shaft Hz)
design_frequency_hz                 double
design_frequency_min_hz             double
design_frequency_max_hz             double

-- vendor-derived benchmarks  (D13 — checks, never inputs)
vendor_mixture_sg                   double
vendor_mixture_gradient_psi_per_ft  double
vendor_tdh_at_design_ft             double
vendor_pump_efficiency_pct          double
vendor_total_system_efficiency_pct  double
vendor_motor_power_kw               double
vendor_free_gas_into_pump_pct       double
vendor_multiphase_correlation       varchar

-- provenance
vendor_family                       varchar
source_document                     varchar
provenance_json                     varchar     -- per-field D14 class, one entry per column above
```

> A view left-joins `esp_well_configuration_v2` to this sidecar. **Precedence rule:** v2 wins on any column it owns; the sidecar is additive only and never shadows v2. Where both could plausibly answer a question (e.g. pump identity), the consumer reads `pump_config`, not either of these.

### `curve_observations` — *(grain: model × point)*

```sql
observation_id              varchar     -- surrogate
pump_model_as_printed       varchar     -- verbatim (D8)
pump_model_canonical        varchar     -- D31
manufacturer                varchar
point_type                  varchar     -- ror_min | bep | ror_max | design_point | shutoff

-- as printed (D10) — never discard
frequency_hz_as_printed     double
flow_as_printed             double
flow_unit_as_printed        varchar     -- bbl/d | STB/D | bpd — recorded as printed, not reconciled
head_as_printed             double
head_unit_as_printed        varchar     -- ft; never psi (D9)
head_basis_as_printed       varchar     -- per_stage | per_section_total | string_total
power_as_printed            double
power_unit_as_printed       varchar     -- hp
power_basis_as_printed      varchar     -- per_stage | per_section_total | string_total
stages_at_observation       int
efficiency_pct              double

-- normalized (D10) — the transform layer's output, re-runnable
flow_bpd_60hz               double
head_ft_per_stage_60hz      double
power_hp_per_stage_60hz     double
normalization_basis         varchar     -- affinity_from_<f>hz

-- quality flags — without these an observation cannot be trusted into a fit
assumed_pump_wear_pct       double      -- F6 prints 30% on every curve
viscosity_corrected         boolean     -- F1 '*' marker; F7 Avg Cq/Ch/Cbhp != 1.0
fluid_sg_at_observation     double      -- F6 differs per section (0.685 / 0.71 / 0.785)
free_gas_at_inlet_pct       double
observation_is_composite    boolean     -- true for whole-string points (F1 Multiscenario, F2 Target Conditions)

-- provenance
vendor_family               varchar
source_document             varchar
source_page                 varchar
scenario_label              varchar
well_id                     varchar     -- the document it came from, for audit; not a join key for fitting
provenance                  varchar     -- D14
```

### `ideal_pump_library_v1` — added columns *(provenance/validity, D5)*

Already applied by MC; carried here unchanged for completeness: `curve_source` (`manufacturer_workbook | tabulated_import | design_doc_fit | digitized`), `fit_n_points`, `fit_degree`, `fit_rmse`, `valid_flow_min_bpd`, `valid_flow_max_bpd`.

---

## Open questions for M2

Ordered by how much damage each does if the method does not handle it.

1. **Bubble point is not invariant across scenarios, and D16 assumes it is.** `Oryx Roan State H 1303H` prints `Bubble Point 1,800 PSIA` in its `1000 pip` scenario and `1,200 PSIA` in its `500 pip` scenario — tracking `Static Datum Pressure` exactly. The D16 fail-loud rule **will fire on real data**, not as a defect but as designed behaviour. M2 must state the load rule: which scenario's value becomes the well-grained sidecar row. Recommend the scenario matching the design/target case, with the variance recorded.
2. **`PSIA` vs `psig` vs `psi`.** F1 prints `PSIA`, F3 prints `psig`, F2/F4/F7 print `psi`. The bubble-point diagnostic compares against `pump_intake_pressure_psi` (gauge) and its status bands are ±10 %, so 14.7 psi is inside the decision margin on a low-pressure well. D8 requires the unit token be carried verbatim; M2 must make that a **required** field, not an optional one, and M5 must reconcile explicitly.
3. **XSize curves are 30 % worn.** Every F6 head and power number is a degraded value. If these enter an ideal-curve fit unflagged they will bias it low by construction. `assumed_pump_wear_pct` is in `curve_observations` for this reason; M2's contract must require it and the validator must reject an F6 curve row without it.
4. **The scenario-title bug is real and recurs.** `HALEY NE I 154H` is titled `1575bpd 51.98hz` while every table in that scenario reports **41.99 Hz** — a 10 Hz error. `THUNDERBALL FEDERAL COM 323H` is titled `450bpd 53.39hz` while its table reports **53.36 Hz** — a 0.03 Hz error. **n=2, two orders of magnitude apart.** The small one is the dangerous one: it will pass any plausibility check. Take the table value, always, and never the page title.
5. **Which SG is "the" mixture SG?** F6 prints a different SG per section (0.685 gas handler / 0.71 taper / 0.785 primary) because gas compresses out down the string. F2 prints `Fluid Composite SG 0.8162` and `Liquid Phase SG 1.0059` side by side. The `sg_for_dp` contract takes one scalar. M2 should record all of them with their basis and leave the selection to M5.
6. **SLB's `Mixture Gradient` disagrees with its own fluid properties.** `Midway 2H`: printed `0.433 psi/ft`, SG-implied ≈ `0.464` at `Water Cut 93 %` / `Water SG 1.1`. Either the vendor applies a default gradient or the separation assumption changes the mixture. Extracted as a benchmark; M2 must not treat it as an input until this resolves.
7. **F5's depth is discharge-referenced.** `Setting Depth from pump discharge 9851.95 ft` against a string that runs to 10,046 ft. Every other family references intake. `depth_reference_basis` exists for this; M2's F5 contract must set it, not infer it.
8. **F5's motor frequency is 2× its shaft frequency.** Permanent-magnet motor: `103.18 Hz` vs `51.59 Hz`. An extractor that grabs "the frequency" will double every affinity normalization in the family.
9. **Per-family model grammars, confirmed by inspection** (this closes amendment 7's requirement with observed forms): F1 `SF3550 TS4 XR (HS Shaft)` — multi-part, `TS4` is *part of the model*; F2 `400UNB35H` and `PUMP MSC_400UNB_35H_93 STG_…`; F3 `REDA 400 RC1000` / `400 DN1750`, with `Staging Configuration CR-CT` vs `C-CT` distinguishing build variants of one model; F4 `ELS-1750` with `Manufacture` and `Series` printed as separate fields; F5 `ESP B 400 1750 PK 738 STG` (summary) vs `ESP B 4001750 CW 6.5M HSG 123 STG PK CT HSS XA3 MTSCa HT` (string diagram) — **the same pump, two grammars, in one document**; F6 `400XPS1750HD` — digits first; F7 `VC4300 - AR Modular EHP`.
10. **F5 prints the same pump two ways and the stage counts differ in basis.** `PRIEST 233H` summary says `ESP B 400 1750 PK 738 STG`; the string diagram lists six housings at `123 STG` each (6 × 123 = 738). `stages_basis` handles it, but the validator must check the identity rather than trust either number alone.
11. **ChampionX has no numeric BEP or ROR** (0/5) and F3/F4/F5 have none either (0/5, 0/3, 0/2). That is **10 of 47 documents with no envelope at all**, and 5 more with a graphical-only envelope. M6's coverage bucketing should expect this; the D7 digitization trigger is more likely to fire than the plan assumed.
12. **`Robin 127H`'s TDH discrepancy is still open** and is inherited by M8: per-model TDH sums to 9,271 ft (7,870 + 1,009 + 392) against 6,578 ft reported at production point 1. Confirmed in this reading — the three per-section pages and the production-point table both say what the plan recorded. It is exactly the shape Test A exists to catch, and it is worth noting the reported value is the **system** TDH while the per-model values are per-section produced head at different flows.

---

## Log

| Date | Update |
|---|---|
| 2026-08-14 | M1 executed and closed. Corpus classified at n=all by page-1 signature; six vendor families resolved to **eight** (F4 ELS and F5 Baker ProLift split out of `Telerik` and `Skia/PDF` respectively; SpyGlass confirmed as one layout at n=4). Field inventory built per family — **495 fields, 290 proposed, 205 not extracted**, every proposed field traced and classified. Demand side confirmed the SG / depth / bubble-point / power-factor target and extended it with seven defaults hiding inside those same contracts; of the 20 Proxy/Fallback/Placeholder contracts, **13 upgraded**, 1 partial (NPSH), 2 benchmark-only, 4 un-upgradable. `PERMIA~1.PDF` dispositioned **in scope** (a Permian Resources well, normal F3 SLB layout); the three procedure documents **out of scope** on the D19 rationale. Three schemas written. **M2 writes 7 contracts over 47 documents.** |
