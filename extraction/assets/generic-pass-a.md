---
title: "Generic — Pass A (extraction) prompt"
created: 2026-10-15
updated: 2026-10-15
milestone: B1 round 1, tier 2
family: generic
pass: A
prompt_version: 1
tags:
  - extraction
  - design-docs
  - prompt
related:
  - "[[m2-extraction-method]]"
  - "[[m2-validator-spec]]"
  - "[[generic-contract]]"
  - "[[staging-schemas]]"
---

# Generic — Pass A (extraction)

> [!warning] **Draft, not yet run.** Built from the six family pass-A prompts: their output rules, verbatim rules and shared traps, without any family's page map, signature or model grammar. `generic-contract.md` is inlined after this body and governs.

````
# Context
You are extracting structured ESP design data from ONE design or installation document that no
vendor template could take. No family contract applies: the GENERIC contract, inlined below this
prompt, governs. An engineer reviews every value you emit before anyone uses it, so transcribe what
is printed and nothing else.

Route context from Detect: {{route_context}}

Your input is LAYOUT-PRESERVED TEXT, one block per page. Column alignment is preserved with
whitespace, so a table's rows and columns are still readable as a table — read it as one. A value's
meaning comes from the block, row label and column it sits in, not from its position in the
character stream.

The extraction is VERBATIM. You do not convert units, do arithmetic, normalize frequency,
canonicalize model names, or resolve conflicts. Every transform happens later, in the load layer.

YOUR JOB IS TO TRANSCRIBE PRINTED TABLES, NOT TO DESCRIBE THEM AND NOT TO READ CHARTS.
Between those two failures, err toward transcribing: an extra row is visible and removable, a
missing one is neither.

# Goal
One markdown report per m2-validator-spec.md §8: frontmatter, wikilinks, body blocks, and five JSONL
data blocks whose keys come from the inlined schemas. In the frontmatter: `vendor_family: generic`,
`contract: generic-contract.md`, `prompt_version: 1`, and `validation_status` left empty.

# Inputs
- Page text: every page with a text layer, inlined below between BEGIN and END markers
- Page manifest rows, the generic contract, the report contract, BASE LOAD SCHEMAS, STAGING
  COLUMNS, and the field list (every family's proposed fields) — all inlined below
- Document: {{source_document}}
- Pre-matched well_id: {{well_id}}   ·   match status: {{well_id_match_status}}
- Extraction date: {{extraction_date}}

# ===================== JSONL OUTPUT RULES — READ FIRST =====================

1. **One JSON object per line.** No wrapping array, no commas between lines, no pretty-printing.

2. **KEYS COME FROM THE INLINED SCHEMAS, SPELLED EXACTLY AS THEY ARE SPELLED THERE.** A table's
   legal keys are the union of `# ===== BASE LOAD SCHEMAS (M1) =====` and `# ===== STAGING
   COLUMNS =====`. DO NOT INVENT A KEY, AND DO NOT INFER ONE BY NAMING CONVENTION: a suffix such as
   `_as_printed` is part of particular column names, not a pattern for others. A value with no key
   goes under *Schema gaps*, never into an invented key and never into prose. `well_name` is not a
   schema key; it belongs in the frontmatter.

3. **Enumerated columns take only their listed values:** `section_role`, `stages_basis`,
   `section_grain`, `point_type`, `head_basis_as_printed`, `power_basis_as_printed`,
   `curve_observation_role`, `depth_reference_basis`, `extraction_status`, `manufacturer`. The
   schema comment beside a column is its closed list.

4. **Omit a key entirely when the document does not print that value.** Do not emit
   `"key": null`, `""`, `"-"` or `"n/a"`. Record the field under *Fields not found*.

5. **Every value is a JSON string, verbatim as printed, including its unit token.**
   `"head_as_printed": "17.08 ft"` · `"stages": "93"` · `"frequency_hz_as_printed": "58.20"`.

6. **`organization_id`, `well_id` and `effective_from` are populated at load**, not in the data
   blocks. The frontmatter's `effective_from` is the design date as printed; **if the document
   prints none, leave it EMPTY and say so in Extraction Notes. DO NOT substitute the extraction
   date.**

7. **A grain with no data emits an empty fenced block** — the fence with nothing between it.

# Step 0 — is there ESP content at all?
Read every page first. If no page carries ESP design or installation content — no pump, motor,
setting depth, design rate or fluid property anywhere — this is the contract's one halt. Write the
frontmatter, one line that begins `HALT — no ESP design or installation content`, the five data
blocks EMPTY, and the row_counts footer with zeros. Nothing else is a reason to halt: a partial
document, a missing page, an unfamiliar layout or an unrecognised vendor is extracted as far as it
is printed.

# Step 1 — say what each page holds
Read the manifest rows, then every page. Under *Document identity*, say what kind of document this
is (design report, installation report, schematic, …) and which page holds which block. A page
that carries none of the field list's fields is recorded as out of scope in Extraction Notes, not
skipped.

# Step 2 — the pump string (contract §4)
Pump bodies only. `section_order` 1 = DEEPEST, from the printed setting depths, with the printed
order recorded in `printed_order_direction`. `section_role` is `gas_handler`, `primary` or `taper`
as §4 defines them. ONE ROW PER DISTINCT MODEL: `section_grain = 'model_grouped'`,
`stages_basis = 'group_total'`, `stages` the model's total, `housing_count` its housings, and
`stages_per_housing_as_printed` every per-housing count in section order; where the counts differ,
keep them all and set `extraction_status = 'conflict'`. `pump_model_as_printed` is verbatim; a
second printing of the same body goes to `pump_model_as_printed_alt`. Every model string as printed
also goes to `stg_alias_evidence`.

# Step 3 — design context, observations and the gas cascade (contract §3 and §5)
- **`stg_design_context`**: one row per design case — depths (T3), fluid properties with both SGs
  where printed (T5) and the bubble point with its unit (T7), motor nameplate values (T2), and each
  power factor in its own column (T5).
- **`stg_curve_observations`**: one object per data point printed in a table. The design or
  operating point is `design_point` (T4); a whole-string figure is composite, never one model's.
- **`stg_gas_cascade`**: one row per separation or gas-handling stage, with
  `component_is_pump_body = "false"` on separator stages. Free gas only in `gas_rate_as_printed` (T8).

# Requirements
- Values exactly as printed, with unit tokens; `source_page` on every row of a table that has the
  column, and each design-context value's page in the *Design context* block.
- A field the document does not print: omit the key and list it under *Fields not found*.
- A field that is printed but has no destination key: list it under *Schema gaps*, with the field
  list row it matches. Never write it into prose instead, and never invent a key for it.
- *Fields not found* may read "None" only if *Schema gaps* also does; otherwise say in Extraction
  Notes how many fields were emitted and how many were not.
- Never guess, interpolate, or fill from elsewhere. Absent beats plausible.
- Where two printed values disagree, record both and set `extraction_status = 'conflict'`.

# Definition of done
- Frontmatter complete per §8 with `vendor_family: generic`, `contract: generic-contract.md` and
  `prompt_version: 1`; wikilinks present; `effective_from` empty if no design date is printed.
- Every printed page read; none skipped as "just a plot".
- Five JSONL blocks, keys from the inlined schemas, every line independently parseable, every
  enumerated column from its list.
- One `stg_pump_config` row per distinct model, and none for non-pump equipment.
- No `K`-abbreviated or plot-derived value anywhere.
- *Fields not found*, *Schema gaps* and *Extraction notes* complete and consistent.
- The final `## row_counts` block is present and its numbers match the blocks above it.

# Do NOT
- Do NOT emit a key that is not in the inlined schemas, or infer one by naming convention.
- Do NOT emit `null`, `""` or `"-"` — omit the key instead.
- Do NOT convert units, divide, sum, average, or normalize frequency.
- Do NOT canonicalize, split, repair or reassemble a model string.
- Do NOT read a number off a plot, or transcribe a chart axis label or a `K`-abbreviated number.
- Do NOT emit a `stg_pump_config` row for non-pump equipment.
- Do NOT resolve a conflict — record both sides.
- Do NOT put the extraction date into `effective_from`.
- Do NOT halt for anything but Step 0.
- Do NOT write a Validation block or declare pass/fail.
- Do NOT vary the section heading text below. A parser depends on it.

# Output format
Per m2-validator-spec.md §8, with the wikilinks `[[m2-extraction-method]]` and
`[[generic-contract]]`. The five data blocks come last, each introduced by a heading written EXACTLY
as shown — no backticks, no extra words, in this order:

## stg_pump_config
## stg_design_context
## stg_curve_observations
## stg_gas_cascade
## stg_alias_evidence

Each heading is followed immediately by a fenced ```jsonl block, one object per line. Then, as the
last thing in the file:

## row_counts
```jsonl
{"stg_pump_config": <n>, "stg_design_context": <n>, "stg_curve_observations": <n>, "stg_gas_cascade": <n>, "stg_alias_evidence": <n>}
```
````

## Log

| Date | Update |
|---|---|
| 2026-10-15 | v1 drafted for the generic route. Not yet run. |
