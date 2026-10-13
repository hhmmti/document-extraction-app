# esp_design_extraction

The extraction core of the design-document pipeline described in `design-note.md`. This folder holds its first three stages, *Detect*, *Prep* and *Extract* (Round 1 → Stages), and the transform of *Validate and stage*. Detect, Prep and stage are deterministic: no AWS, no network, no model. Every stage takes its input in memory and writes nothing to disk.

- `extraction.detect(pdf_bytes)` returns a `Detection`: the vendor family, the page count, and each page's resolved roles.
- `extraction.prep(pdf_bytes, detection, source_file="")` returns a `PrepResult`: the kept original page numbers, the layout text for each kept page with the private-use glyphs translated, and one manifest row per source page.
- `extraction.assemble.assemble_prompt(family, prep_result, source_file, well_id=None, extraction_date=None, clarifications=True)` returns the extraction prompt. By default it adds each family's approved contract clarification (`extraction/assets/amendments/f<n>.md`; today F3, F5 and F7) after the contract; with `clarifications=False` it is byte for byte what the per-family batch runners built, which the corpus byte check tests, except that a file name containing `&` is now substituted literally: the runners' `sed` put placeholder text in its place, and the byte check carries one allowance for the two corpus documents that hit this. Its inputs are the verbatim copies in `extraction/assets/`, whose hashes are in `assets/MANIFEST.txt`.
- `extraction.bedrock.extract(prompt, budget_s=..., max_tokens=..., model_id=..., effort="medium", client=None)` makes one streaming Converse call to Opus on Bedrock, with adaptive thinking at the given effort, no retries, and bounded by `budget_s`. It returns the report text and, separately, any reasoning text; the stop reason, token counts, time to first token, total model time, model id and the prompt's SHA-256.
- `extraction.stage.stage(report_text, organization_id, well_id, family, evidence)` turns one report into the three load grains (`pump_config`, `esp_well_design_context`, `curve_observations`, as object-dtype DataFrames), the per-document log (`reconciliation`, `d16_log`, `d17_log`, `gas_log`, `d16_tier_a_fallback`, `unit_conversions`) and the quarantine rows. It is the corpus batch loader's per-document transform, copied with its logic unchanged; `extraction/stage/MANIFEST.txt` has the hashes and every edited line. `family` is the code Detect assigned; if the report's `vendor_family` names another family it raises `FAMILY_MISMATCH`. Organisation and well come from the caller; `well_id=None` is an unmatched well, which gets quarantine and reconciliation rows and no load rows. `evidence` is the alias evidence: `corpus_evidence(report_texts)` learns it from every report in the order given, as the batch loader did; `production_evidence(report_text)` is the frozen corpus evidence (`alias-evidence-v1.json`, rebuilt only as a new version by `scripts/freeze_alias_evidence.py`) plus the report's own, learned last. Four data assets are not kept here: that file, the D31 seed `m2-d31-seed-aliases.csv`, the pump catalogue snapshot `universal_catalog.parquet` (unknown models) and the field inventory `m1-field-inventory.csv` (prompt assembly). They are read from the directory `ESP_EXTRACTION_ASSETS_DIR` names, and each must match the SHA-256 in `extraction/stage/MANIFEST.txt` (the inventory: `extraction/assets/MANIFEST.txt`) or reading it raises `ASSET_INVALID`.
- Staging also validates. `stage(..., detection=None, prep=None)` takes the run's Detect and Prep results as keyword arguments; the rules that read page roles or page text, and per-value grounding, are not evaluated without them and say so in `notices`. Before staging, three structure guards (frontmatter, the five `stg_*` blocks, a parseable `row_counts` footer) raise `REPORT_INVALID`, and nothing is staged. After staging, `extraction/validate/` adds five fields to the result: `findings`, `unknown_models`, `review`, `notices` and `spec_validation_status`. Their rules, guards and signals are the approved design sheet's; the rule list is `extraction/validate/rules.py`. Record shapes:
  - **finding**: `rule_id`, `spec_severity` (the validator spec's, or null for a guard it does not have), `round1_severity` (`REPORT_INVALID` for the structure guards, `LOAD_HALTED` for D17, otherwise `warning`), `scope`, `table`, `row_key` (the row's 1-based position in its `stg_*` block, or null), `column`, `value`, `message`, `blocks_publish` (true for V-08 and D17).
  - **review**, one per in-scope staged value: `field`, `table`, `row_key`, `value`, `cited_page`, `grounding` (`cited_page`, `other_page`, `not_found` or `not_evaluated`), `reasons` (one string per signal that fired: `grounding`, `rule`, `conflict`, `quarantined`, `known_issue`, `frequency_trap`, `expected_missing`), `needs_review`. Where no page is cited, `other_page` means found on some kept page and is not a reason. A conflict row gives one item, on its model field (or on `extraction_status` where the row has none); it is `conflict_benign`, informational and not needing review, when both printed forms resolve through D31 to the same canonical model. Never-printed fields (staging vocabulary and metadata such as `extraction_status` and `source_page`) are never grounded. A field the family prints in every corpus report but this report does not stage gets a record with `row_key` and `value` null.
  - **unknown model**: `model`, `class` (`unresolved`, `absent` or `no_head_curve`, against the catalogue), `pump_model_as_printed`.
  - **notice**: `kind` (`known_issue` or `not_evaluated`), `rule_id`, `family`, `field`, `message`.
  - `spec_validation_status` follows the spec's §1.2: any HALT finding gives `failed`, any ERROR or WARN `needs_review`, otherwise `passed`.
- Each failure raises `extraction.ExtractionError`, which carries `code`, `message` and `details`. The codes are `IMAGE_ONLY`, `UNKNOWN_TEMPLATE`, `AMBIGUOUS_TEMPLATE`, `REQUIRED_PAGES_MISSING`, `PREP_FAILED`, `MODEL_TIMEOUT`, `MODEL_TRUNCATED`, `MODEL_ERROR`, `FAMILY_MISMATCH`, `ASSET_INVALID`, `REPORT_INVALID`, `LOAD_HALTED` and `STORE_FAILED`.

The signatures, page maps and glyph map are in `extraction/families.py`. The parity comparator is `extraction/compare/parity.py`; it parses values with the loader's `values.py`, copied verbatim beside it (hash in `extraction/compare/MANIFEST.txt`), and prices calls from `extraction/compare/pricing.json`.

## Tests

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest              # unit tests; corpus tests skip; no AWS
```

The stage tests and the prompt-assembly tests also need `ESP_EXTRACTION_ASSETS_DIR`; without it they skip.

The corpus tests check detection and prep parity against the output of the corpus slicing step this code was ported from, and check that assembled prompts match the ones the batch runners saved. They run with `-m corpus` and need these environment variables:

| Variable | Points at |
|---|---|
| `ESP_CORPUS_DIR` | the directory of source design documents |
| `ESP_PREP_REF_DIR` | the slicing step's output directory, holding `prep-manifest.csv` and `text/<family-slug>/<stem>/p<n>.txt` |
| `ESP_BATCH_PROMPT_DIR` | the runners' saved prompts, `<stem>.prompt.txt`; the prompt byte check skips without it |

```bash
export ESP_CORPUS_DIR="<corpus dir>"
export ESP_PREP_REF_DIR="<reference output dir>"
export ESP_BATCH_PROMPT_DIR="<saved prompts dir>"
.venv/bin/python -m pytest -m corpus
```

Every expected value is read from the manifest at run time. A full corpus run takes several minutes.

## Smoke run

`scripts/smoke_extract.py` runs one PDF through all three stages against Bedrock in the datascience account. It checks with STS that the credentials belong to account `640168431387`, and stops before Bedrock if they don't. It refuses an output directory inside this folder. It writes `report.md` and `run.json` (the result fields, including the model id used, stage durations and asset hashes). The model defaults to the US cross-region inference profile, `us.anthropic.claude-opus-5-5`; `--model-id` overrides it. Contract clarifications are on; `--no-clarifications` turns them off.

```bash
aws sso login --profile roam-ai
AWS_PROFILE=roam-ai .venv/bin/python scripts/smoke_extract.py "<smoke document>.pdf" /tmp/esp-extract-smoke
```

## Parity run

Six documents, one per family, rerun through the new path and compared with the old batch reports. The scripts find them in the corpus directory by the SHA-256 of the PDF (`PARITY_DOCS` in `extraction/compare/parity.py`), so no document name is kept here. In run order, shortest assembled prompt first:

| # | Family | Selection | PDF SHA-256 (prefix) | Prompt bytes | Kept pages |
|---|---|---|---|---|---|
| 1 | F4 | pilot brief's pick | `d630530e4b6b` | 132,270 | 5 |
| 2 | F5 | pilot brief's pick | `e073658133bc` | 151,786 | 7 |
| 3 | F2 | pilot brief's pick | `c55ecf1b0789` | 153,762 | 9 |
| 4 | F3 | pilot brief's pick | `bb366c921b50` | 156,093 | 7 |
| 5 | F7 | pilot brief's pick (the family's only document) | `83cc3aefeb0a` | 203,492 | 16 |
| 6 | F1 | the longest document | `1ad7b6c31418` | 322,766 | 34 |

`scripts/parity_run.py` calls STS and Bedrock; run it by hand. It keeps the account preflight, refuses an output directory inside this folder (outputs hold client data), and will not rerun a document whose `run.json` records a completed run unless given `--force`. Per document it writes `<out>/<stem>/report.md`, `run.json` (tokens, time to first token, model time, output tokens per second, stage durations, prompt and report bytes, bytes per output token, stop reason, share of `max_tokens`) and, when the stream carries reasoning text, `reasoning.txt`. Defaults: `--max-tokens 128000`, `--budget-s 1800`, effort `medium`.

`scripts/parity_compare.py` is deterministic and makes no calls. It compares each new report with the old one and writes `<out>/parity_report.md`: the verdict table, metrics and cost, the timing verdict, the F1 old-input check, and every difference.

```bash
aws sso login --profile roam-ai
export ESP_CORPUS_DIR="<corpus dir>"
export ESP_OLD_REPORTS_DIR="<old batch reports dir>"
export ESP_BATCH_PROMPT_DIR="<saved prompts dir>"
AWS_PROFILE=roam-ai .venv/bin/python scripts/parity_run.py --doc all --out /tmp/esp-parity
.venv/bin/python scripts/parity_compare.py --out /tmp/esp-parity
```

`--doc` also takes one stem, or family codes such as `F1,F3,F5` for those families' parity documents. `--effort high` sends `output_config.effort = high` and records it in `run.json` (the default is `medium`). Contract clarifications are on by default: each family's approved clarification (`extraction/assets/amendments/f<n>.md`, today F3, F5 and F7) goes in as a `# ===== CONTRACT CLARIFICATIONS: <contract> =====` block after the contract and before the report contract, and `run.json` records that they were on and each clarification's SHA-256. `--no-clarifications` sends the batch runners' prompt byte for byte. `smoke_extract.py` takes the same flag. Cost stays `pending` until both prices in `pricing.json` are filled in from the AWS Bedrock pricing page.

### High-effort rerun and gate re-check

Rerun the failing families at effort `high`, compare the new run with the old reports and with the medium run, then re-check the gate fields the adjudicated worksheet marked wrong. Only the first command calls AWS.

```bash
aws sso login --profile roam-ai
export ESP_CORPUS_DIR="<corpus dir>"
export ESP_OLD_REPORTS_DIR="<old batch reports dir>"
MEDIUM="<medium parity run dir>"   # holds the adjudicated adjudication.csv
HIGH="<high parity run dir>"
AWS_PROFILE=roam-ai .venv/bin/python scripts/parity_run.py --doc F1,F3,F5 --effort high --out "$HIGH"
.venv/bin/python scripts/parity_compare.py --out "$HIGH" --baseline-run "$MEDIUM"
.venv/bin/python scripts/gate_recheck.py --worksheet "$MEDIUM/adjudication.csv" --run "$HIGH" --out "$HIGH"
```

- `parity_compare.py --baseline-run` adds a section to `parity_report.md`: tokens, time to first token, model time, cost and share of `max_tokens` for each document in both runs, side by side, and every `conflict` or `dropped` on a gate field (pump_config or a trap field) that the baseline run did not have. A worksheet that already carries verdicts is never overwritten; a regenerated one goes to `adjudication.regenerated.csv`.
- `gate_recheck.py` (`--run`, alias `--high-run`; `--families`, default `F1,F3,F5,F7`) selects the chosen families' worksheet rows on pump_config or a trap field judged `old right` or `both wrong`, looks up the same row key and field in the high run, and writes `gate_recheck.csv`: the worksheet columns plus `high_value` and `status` (`matches_old`, `matches_medium`, `other`, `absent`; a `both wrong` row whose value returns to the old one is `needs_review`). The status compares the whole `high_value` (every distinct value found, joined) with the old and medium values under the comparator's `format` rule, so several values never match one; a non-empty `high_value` is never `absent`. The worksheet is only read. Cells a spreadsheet left untouched are read as UTF-8 and cells typed into it in `--encoding` (default `mac_roman`).

### Clarifications rerun

The same three steps for a medium-effort run; clarifications are on by default:

```bash
CLAR="<clarifications run dir>"
AWS_PROFILE=roam-ai .venv/bin/python scripts/parity_run.py --doc F3,F5,F7 --effort medium --out "$CLAR"
.venv/bin/python scripts/parity_compare.py --out "$CLAR" --baseline-run "$MEDIUM"
.venv/bin/python scripts/gate_recheck.py --worksheet "$MEDIUM/adjudication.csv" --run "$CLAR" --families F3,F5,F7 --out "$CLAR"
```

## Stage parity

`scripts/stage_parity.py` stages the corpus reports one at a time, with the batch loader's own well match and Detect's family, in both evidence modes, and compares the concatenated results with the batch loader's ten committed outputs (three Parquet grains, seven JSONL logs). Every artifact is compared as a multiset of rows, each row as canonical JSON; the Parquet grains are first put under the reference schema with a safe cast. It then compares production mode with corpus mode byte for byte, stages every `<run set>/<document>/report.md` under `--runs` in production mode (well: the corpus match for the same source document), and checks that every F1 pump section resolves to a canonical model. Outputs hold client data and must go outside this folder; `hashes.txt` lets two runs be compared.

```bash
export ESP_OLD_REPORTS_DIR="<corpus extraction reports dir>"
export ESP_LOAD_REF_DIR="<batch loader output dir>"
export ESP_CORPUS_DIR="<corpus dir>"
export ESP_RUNS_DIR="<run directories>"
export ESP_EXTRACTION_ASSETS_DIR="<extraction asset dir>"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/stage_parity.py --out <dir>
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -m corpus tests/test_stage_corpus.py
```

## Validate run

`scripts/validate_run.py` stages and validates the corpus reports (corpus evidence) and every new report under `--runs` (production evidence), with Detect and Prep run on each source PDF. Per report it writes `findings`, `review`, `unknown_models` and `notices` as JSONL plus `status.json`; for the corpus it also writes the ten stage artifacts and compares them with the batch loader's, where the only accepted difference is flag 2 (`provenance_json` left out of D16: two wells, three artifacts). `validate_run.md` holds the parity table, the corpus findings by rule, the unknown models against the summer's blocked models, and a line per new report. Same inputs as the stage parity run; outputs go outside this folder.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/validate_run.py --out <dir>
```

## Running a document: the run store and its record

```bash
export ESP_RUN_STORE_DIR="<run store dir>"          # outside this folder: runs hold client data
export ESP_EXTRACTION_ASSETS_DIR="<extraction asset dir>"
AWS_PROFILE=roam-ai .venv/bin/python -m extraction --pdf <pdf> --org <id> --well <id>     # full chain, one Bedrock call
.venv/bin/python -m extraction --report <report.md> [--pdf <pdf>] --org <id> --no-well     # no model call
```

`--pdf` alone runs Detect, Prep, Extract (after checking the credentials are the datascience account's), Validate and stage, Store and Record. `--report` reuses a report and calls no model; with `--pdf` as well, Detect and Prep run and grounding is evaluated, and without it the checks that need them are not evaluated and the notices say so. `--well <id>` or `--no-well` (an unmatched well: no load rows, no key minted) is required. Options: `--budget-s` (default 900), `--model-id`, `--effort`, `--max-tokens` (default 128000), `--no-clarifications`. The tool prints one line, starting with the `run_id`; it exits 0 when the run succeeded, 1 when it failed (its `run.json` says why) and 2 when it could not start.

A run lives in `$ESP_RUN_STORE_DIR/runs/<run_id>/`, where `run_id` is `YYYYMMDDTHHMMSSZ-<8 hex>` (the UTC start second, then random hex: sortable and unique). It holds `run.json`, `source.json` (path and SHA-256 of the PDF and of the report), `report.md`, `prep_manifest.csv`, the three grains as Parquet (`pump_config`, `esp_well_design_context`, `curve_observations`), `quarantine.jsonl`, `findings.jsonl`, `unknown_models.jsonl`, `review.jsonl`, `notices.jsonl`, `d16_log.jsonl`, `d17_log.jsonl` and `gas_log.jsonl`. Every file is written to a temporary name and renamed into place, and each is written as soon as it exists, so a failed run keeps what it produced. The layout is meant to map one to one onto an S3 prefix later; nothing is written to S3 now.

`run.json` is written first with `status: running` and a `deadline_at` (start plus the budget), and last with `succeeded` or `failed`. A reader (`extraction.run.read_record`) treats a `running` record past its deadline as failed and marks it `stale`. Fields:

- `run_id`, `status`, `started_at`, `deadline_at`, `ended_at`, `budget_s`, `mode` (`pdf` or `report`)
- `organization_id`, `well_id`, `family`, `family_source` (`detect` or `report_frontmatter`)
- `source` (`pdf` and `report`, each `path` and `sha256`, or null); `pages` (`page_count`, `kept_pages`, and each page's Detect roles)
- `rows` per grain; `quarantine` (count); `findings` (`total`, `by_round1_severity`, `by_spec_severity`; `none` for guards); `spec_validation_status`; `blocks_publish`; `review` (`items`, `needs_review`, `informational`); `unknown_models`
- `error` (`code`, `message`, `details`, `stage`) or null. `status` is the run's own outcome; the document's validation verdict is `spec_validation_status`
- `model` (`model_id`, `effort`, `max_tokens`, `clarifications`, `stop_reason`, `model_time_s`, `time_to_first_token_s`) and `tokens` (`input`, `output`) and `cost_usd` (from `extraction/compare/pricing.json`), all null without a model call
- `hashes`: `report_sha256`, `prompt_sha256`, `contract` (the prompt's asset hashes), `clarifications`, `stage_assets` (seed, evidence, catalogue)
- `stages_s` (each stage's duration: detect, prep, assemble, extract, stage, store, record) and `store` (`files` written, `failed_at`)

Failure codes are the extraction codes plus `REPORT_INVALID` (the report and its structure findings are stored), `LOAD_HALTED` (D17-HALT: grains, findings and review are stored; the load is halted) and `STORE_FAILED` (`store.failed_at` and `error.details.failed_at` name the file the run could not write). If even the last `run.json` cannot be written, the `running` record stays and reads as stale once its deadline passes.

