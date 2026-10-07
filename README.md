# esp_design_extraction

The extraction core of the design-document pipeline described in `design-note.md`. This folder holds its first three stages, *Detect*, *Prep* and *Extract* (Round 1 → Stages). Detect and Prep are deterministic: no AWS, no network, no model. Every stage takes its input in memory and writes nothing to disk.

- `extraction.detect(pdf_bytes)` returns a `Detection`: the vendor family, the page count, and each page's resolved roles.
- `extraction.prep(pdf_bytes, detection, source_file="")` returns a `PrepResult`: the kept original page numbers, the layout text for each kept page with the private-use glyphs translated, and one manifest row per source page.
- `extraction.assemble.assemble_prompt(family, prep_result, source_file, well_id=None, extraction_date=None, clarifications=True)` returns the extraction prompt. By default it adds each family's approved contract clarification (`extraction/assets/amendments/f<n>.md`; today F3, F5 and F7) after the contract; with `clarifications=False` it is byte for byte what the per-family batch runners built, which the corpus byte check tests. Its inputs are the verbatim copies in `extraction/assets/`, whose hashes are in `assets/MANIFEST.txt`.
- `extraction.bedrock.extract(prompt, budget_s=..., max_tokens=..., model_id=..., effort="medium", client=None)` makes one streaming Converse call to Opus on Bedrock, with adaptive thinking at the given effort, no retries, and bounded by `budget_s`. It returns the report text and, separately, any reasoning text; the stop reason, token counts, time to first token, total model time, model id and the prompt's SHA-256.
- Each failure raises `extraction.ExtractionError`, which carries `code`, `message` and `details`. The codes are `IMAGE_ONLY`, `UNKNOWN_TEMPLATE`, `AMBIGUOUS_TEMPLATE`, `REQUIRED_PAGES_MISSING`, `PREP_FAILED`, `MODEL_TIMEOUT`, `MODEL_TRUNCATED` and `MODEL_ERROR`.

The signatures, page maps and glyph map are in `extraction/families.py`. The parity comparator is `extraction/compare/parity.py`; it parses values with the loader's `values.py`, copied verbatim beside it (hash in `extraction/compare/MANIFEST.txt`), and prices calls from `extraction/compare/pricing.json`.

## Tests

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest              # unit tests; corpus tests skip; no AWS
```

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
