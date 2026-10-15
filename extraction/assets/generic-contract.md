---
title: "Generic — Extraction Contract (no vendor template)"
created: 2026-10-15
status: draft — needs Hamed's approval before any paid run
milestone: B1 round 1, tier 2
family: generic
vendor_family: generic
tags:
  - extraction
  - design-docs
  - contract
related:
  - "[[m2-extraction-method]]"
  - "[[m1-extraction-spec]]"
  - "[[m2-validator-spec]]"
  - "[[staging-schemas]]"
---

# Generic — Extraction Contract

> [!warning] **No vendor template checked this document, so every value is unverified.** This route takes what the family contracts cannot: a document from a known vendor with required pages missing, and a document no vendor template matches. Extraction is still **verbatim** — no unit conversion, no arithmetic, no canonicalization, no conflict resolution. An engineer reviews every value before it is used.

## 0. Applicability

- **Documents:** any ESP design or installation document that Detect sends to the generic route. That is one whose page-1 signature matched a vendor family but whose required pages are missing; one whose text layer matches no family; or a template document sent here on purpose, to measure this route.
- **Route context:** the prompt carries one line from Detect. When a family's signature matched, it names that family and the missing pages. It is context only: this contract governs, and no family contract applies.
- **`vendor_family: generic`**, in the frontmatter and on every row that has the key. `contract: generic-contract.md`.

## 1. Pages

There is no page map. Every page with a text layer is in the input; pages under the 20-character floor were dropped before you and are listed in the manifest. **Read every page.** A page that carries none of the fields below is recorded as out of scope in Extraction Notes, never skipped silently. Missing pages, an unfamiliar layout or an unrecognised vendor are not reasons to stop.

## 2. What to extract

The same five blocks as every family, with keys from the inlined base schemas and staging columns only:

| Block | What goes in it |
|---|---|
| `stg_pump_config` | One row per distinct pump model in the string (`section_grain = 'model_grouped'`). Pump bodies only. |
| `stg_design_context` | One row per design case: depths, fluid properties, motor and electrical values, design rates and TDH. |
| `stg_curve_observations` | One row per data point printed in a table: a design or operating point, a row of a performance table, a per-stage value. |
| `stg_gas_cascade` | One row per separation or gas-handling stage the document prints, with its free-gas and volume figures. |
| `stg_alias_evidence` | Every pump-model string as printed, with where it was printed (`evidence_kind`). |

The field list inlined below holds **every family's proposed fields**. A value is in scope when it means what one of those rows means, whatever this document calls it. Nothing outside the list is extracted; note candidates in Extraction Notes.

## 3. Values

- **Verbatim, with the printed unit.** Every value is a JSON string exactly as printed, unit token included (`"17.08 ft"`, `"4,250 bpd"`). Do not convert, round, re-format or strip separators.
- **A page for every value.** Rows of `stg_pump_config`, `stg_curve_observations`, `stg_gas_cascade` and `stg_alias_evidence` carry `source_page`, naming the page or pages their values are printed on (`p3`, `p2, p5`). `stg_design_context` has no page column: the report body's *Design context* block gives each of its values' pages.
- **Absent beats guessed.** A value that is not printed is omitted — no key, no `null`, no `""` — and listed under *Fields not found*. Never fill a value from another document, a typical value or a default.
- **No defaults.** No assumed frequency, power factor, specific gravity, efficiency, wear or depth basis. If the document does not print it, it is not emitted.
- **No values from plots.** A number whose only source is a plotted trace, an axis, a gridline or a `K`-abbreviated label is not data; record the plot page as graphical-only in Extraction Notes. A printed table next to a plot is still a table.
- **Model strings as printed.** `pump_model_as_printed` is the string as printed, whole. Never repair, split, canonicalize or reassemble it; that happens at load. A second printing of the same body goes to `pump_model_as_printed_alt`.
- **`manufacturer`** is filled only when the document names a maker that is one of the schema's listed values. Otherwise it is omitted, and the printed maker is named in Extraction Notes.
- **Conflicts are kept.** Where two printed values for one field disagree, emit both as printed and set `extraction_status = 'conflict'`. Do not choose.

## 4. Sections

- **Pump bodies only** become `stg_pump_config` rows. Separators, protectors and seals, intakes, motors, sensors, cables, controllers, de-sanders and tail pipes are section context only, never rows, whatever their label suggests: a separator labelled `GAS HANDLING…` is still a separator.
- **`section_order` 1 = deepest** (intake side), ascending toward discharge, from the printed setting depths. Record how the document ordered the sections in `printed_order_direction`.
- **`section_role`:** `gas_handler` for a body the document calls a gas handler or gas-handling pump; `primary` for the main production pump (where the document names none, the body with the most stages); `taper` for any other pump body of another model in the string.
- **Grain:** one row per distinct model. `stages` is that model's total, `housing_count` its housings, `stages_per_housing_as_printed` every printed per-housing count in section order (`"42, 93, 93"`), and `stages_basis = group_total`. Where the counts differ, keep every one and set `extraction_status = 'conflict'`; never pick a typical count.
- **One scenario** unless the document prints separate design cases. Then each case gets its own rows, labelled as printed, `scenario_ordinal` numbered from 1 in print order, and `is_design_scenario = "true"` only on a case the document itself names as the design.

## 5. Traps — the ones the family contracts share

**T1 — `K`-abbreviated and plotted numbers are not data.** `8.5K ft`, `1.09K bpd`, and any number that lines up with a gridline, come from a chart. Emit nothing from them.

**T2 — Nameplate is not operating or derated.** A motor's nameplate power, voltage and current are ratings. Operating, derated, loaded or at-design values are different quantities. A value goes into a nameplate column only when it is printed as nameplate; an operating or derated value never does.

**T3 — MD is not TVD.** Measured and true vertical depths go to their own columns (`…_md_ft`, `…_tvd_ft` or `…_vd_ft`) as labelled. Never convert one into the other, and never put an unlabelled depth into either; name it in Extraction Notes. `depth_reference_basis` takes the schema value matching the reference the document states (`intake`, `pump_discharge` or `pump_setting`), and is omitted when the document states none.

**T4 — A design point is not a curve point.** The document's design or operating point (rate, TDH, frequency at design) is `point_type = 'design_point'` with `curve_observation_role = 'design'`. Rows of a printed performance table are `curve_point`, with the role naming that table (`head_curve`, `bhp_curve`). A whole-string figure — total TDH, total power, target conditions — is composite: `observation_is_composite = "true"` and `head_basis_as_printed = 'string_total'`, never attributed to one model.

**T5 — Two power factors, two specific gravities.** A motor's operating power factor and a surface-equipment power factor are different quantities: `motor_power_factor` and `surface_power_factor`, never substituted for each other. A mixture SG and a liquid-phase SG are different too: `vendor_mixture_sg` and `vendor_liquid_phase_sg`. Emit each that is printed, and choose neither.

**T6 — Frequency comes from a table, never a title.** `frequency_hz_as_printed` comes from the cell the value sits beside. A chart title's frequency names a chart, not a design.

**T7 — A bubble point needs its unit.** Wherever `bubble_point_psi` is emitted, `bubble_point_unit_as_printed` carries the printed unit (`psi`, `psia`, `psig`). Different tokens are not reconciled.

**T8 — Free gas is not total fluid.** `gas_rate_as_printed` is a free-gas volume only. Where the document prints a free-gas percentage and no gas volume, emit `gas_pct_as_printed` and omit the volume.

**T9 — Excluded everywhere.** Productivity index, pump intake pressure (calculated or desired), NPSHr or any cavitation margin, and motor amperage are never extracted. `Free Allowed Gas` is gas-handling capability, under its own name, never NPSHr.

## 6. Halt

The only halt on this route: **the document holds no ESP design or installation content at all** — no pump, motor, setting depth, design rate or fluid property anywhere. Then write the frontmatter, one line that begins `HALT — no ESP design or installation content`, all five data blocks empty, and a `row_counts` footer of zeros. Everything else is extracted as far as it is printed.

## 7. Validation

The validator spec's general rules apply (`m2-validator-spec.md` §2, §3 and §7), and so do the D16 and D17 fail-loud rules (§5, §6). No family rule (`V-F*`) does. Every value is marked for an engineer's review, whatever the checks find.

---

## Log

| Date | Update |
|---|---|
| 2026-10-15 | Drafted from the six family contracts and their pass-A prompts, the staging schemas, the field inventory and the validator spec. Not yet run. |
