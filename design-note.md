# Design-document extraction app — design note

**Task:** B1, make design-document extraction a pipeline rather than a one-off. GitHub issue `roam-datascience-esp #98`.
**Status:** approved by Keith; restructured into two rounds on 2026-10-05; updated at the close of Step 2 (2026-10-06), Step 3 (2026-10-13) and Step 4 (2026-10-13). Owner: Hamed.

A design document from a known vendor template is uploaded through a Streamlit app. Template detection runs first, then Opus on Bedrock extracts the numbers. The run is logged and every failure is reported. B1 is built in two rounds, and is done when round 2 is done:

- **Round 1, datascience account.** A PDF goes in through the app; the numbers are stored by code in datascience and are visible in the app, with a run log. The tapered ideal calculator does not read them this round.
- **Round 2, product.** The same path, writing to product storage, plus manual entry, edit-afterwards and a version store.

**How to read this note.** *Confirmed* means checked against the running system. *Proposed* is a design choice still open to change. Names in `<placeholders>` do not exist yet.

**What is reused.** The extraction method already lives in this repository at `esp_design_extraction_method/`: template triage, page slicing, the per-vendor extraction contracts and prompts, the report validator, and the loader. This app turns that method into a pipeline. Step 2 confirmed the port is exact. Detection and page text match the method's own outputs byte for byte, and the assembled prompts match the ones the batch runs sent, for every document in the corpus.

---

# Round 1 — datascience

## 1. Shape

```
Streamlit app ──► S3 intake ──(object-created)──► one Lambda ──► run store (S3, per run)
   ▲  form: org + existing well + PDF                 │                │
   │                                                  ▼                │
   └──── run viewer ◄──── DynamoDB run record  +  CloudWatch logs      │
         (run, source document, extracted numbers) ◄──────────────────┘
```

There is one entry kind: a PDF. The Lambda runs *Detect → Prep → Extract → Validate and stage → Store → Record*. The viewer reads the run record and the run store. Nothing writes to `tapered_ideal_v2`.

**In Step 4 the app runs locally.** The shape is the same, with a detached local process in place of the S3 trigger and the Lambda, and a local folder in place of S3. Step 5 swaps those two parts.

## 2. Stages — inputs, outputs, failure

### Intake (app)

*Rewritten at the close of Step 4, 2026-10-13. Built and run locally; Step 5 moves it onto S3 and the Lambda.*

- **In:** an organisation and an existing well, chosen from dropdowns, plus the PDF.
- **The well list** is a snapshot of the product's well configuration (`roam_prd_ddb.default.esp_well_configuration_v2`), production only. It is exported unedited from Athena and read from a file outside the code tree (`ESP_WELL_LIST_PATH`).
  - **Columns:** `organization_id` and `well_id`. *Confirmed* in production and dev; `well_id` is human-readable, so the dropdown shows it as is.
  - **Values are kept exactly as exported.** Nothing is stripped, case-folded, or has its spaces collapsed; only exact duplicate rows are dropped.
  - **Organisations that differ only by case** are listed separately, with a hint naming the variant, so a document isn't filed under the wrong spelling.
  - **Freshness:** the form shows the file's modification time, hash and row count. Refresh is manual in round 1.
- **The form** asks for the organisation, then a well filtered by it, then the PDF.
  - A file that is empty, or doesn't start with `%PDF-`, is refused before any launch.
  - A cost line and a confirm box gate the submit button.
  - A warning shows when the same PDF, organisation and well already has a live run.
- **What it does, locally:**
  1. **Copies the PDF** into the store as `intake/<sha256>.pdf`, atomically. An existing copy is hash-checked and reused; a mismatching one is refused. No path is ever built from an organisation or well id.
  2. **Launches the command-line run as a detached process.** The app generates the run id. The process gets an argument list in `--flag=value` form with no shell, including `--run-id` and `--source-name` (the uploaded file's name). Its output goes to `launch/<run_id>.log`.
  3. **Watches the run:**
     - it shows *starting* until the run record appears, then the run's status and elapsed time against its deadline, then the detail page once the run is final;
     - a launch is *failed* when the process exits with no record;
     - a live process with no record after 120 s shows *no record yet, still running*;
     - after an app restart, when the process can no longer be seen, a launch with no record after 120 s is failed.
- **For Step 5:**
  - The hosted app writes the PDF to `<intake prefix>`, and the S3 trigger replaces the local process.
  - A run record written as `submitted` before the upload remains **proposed**. Locally, the run writes `running` first, and the launch log covers the gap.

### Detect (Lambda, deterministic)

- **In:** the PDF.
- **Does:**
  1. **Text layer.** Counts non-whitespace characters per page. If page 1, or every page, falls under the image-route floor of 20 characters → `IMAGE_ONLY`.
  2. **Signature.** Matches each vendor family's page-1 markers. Exactly one family must match. None → `UNKNOWN_TEMPLATE`; more than one → `AMBIGUOUS_TEMPLATE`.
  3. **Required pages.** Every page role the family's contract marks `required` must be found in the document. Otherwise → `REQUIRED_PAGES_MISSING`, naming the missing roles.
- **Out:** the family and its page roles, or an error. No LLM.
- **SpyGlass (F1) detection drops one marker.** The `Summit ESP Representative` marker sits in a disclaimer that some real SpyGlass documents don't print. Detection relies on `SIZING REPORT` plus the required-page check.
- **XSize reports as image-only.** XSize documents have no text layer, and their PDF producer string is shared with another vendor, so they can't be told apart from any other scanned PDF.
- **An unreadable PDF fails here, with `PREP_FAILED`,** because this is the first stage that opens the file.
- **Confirmed in Step 2:**
  - Every corpus document in the six supported families detects as its family, the SpyGlass documents without the dropped marker included.
  - The XSize document raises `IMAGE_ONLY`.
  - A procedure document raises `UNKNOWN_TEMPLATE`.
- **Found in Step 4:** the signatures match the generator versions and export paths of the corpus they were built from, and real uploads differ. See §4.

### Prep (Lambda)

- **In:** the PDF and its family.
- **Does:** the single-document form of the existing slicing step:
  - slices to the family's pages, dropping only pages the contract marks plot-only;
  - extracts layout text per kept page;
  - translates the private-use glyphs the vendors' fonts emit;
  - writes manifest rows.
- **Does not:** use a fixed file list, render images, write a sliced PDF, or keep corpus-wide state.
- **Out:** per-page text keyed by original page number, and the page manifest. The page numbers become the run's page references.
- **Fail:** `PREP_FAILED`, for any of:
  - an unreadable PDF;
  - private-use glyphs left unmapped;
  - a kept page under the 20-character floor in a text-layer family. There is no vision path, so it is not routed to an image.
- **Confirmed in Step 2:** kept pages, every manifest field, and every kept page's text are byte-identical to the method's outputs, on the whole corpus.

### Extract (Lambda → Bedrock, Opus)

- **In:**
  - the page text and the manifest;
  - the family's prompt and contract;
  - the validator spec, the staging schemas and the closed field list;
  - the family's contract clarifications, where they exist.
- **Does:**
  - **Assembles the prompt** in the same order the existing batch runners used. Step 2 confirmed this byte for byte against the prompts the batch runs sent.
  - **Adds contract clarifications, on by default.** For three families (SLB, Baker, Valiant), a `CONTRACT CLARIFICATIONS` block follows the contract text. Each clarification only makes an existing contract requirement explicit. Their hashes are recorded on the run.
  - **Calls Opus on Bedrock as a streaming call**, bounded three ways: by `max_tokens`; by a read timeout of `min(120, budget_s)`; and by a deadline checked on every stream event, which is the binding limit.
  - **No retries.** The prompt is sent without its final newline, as the batch runners sent it.
- **Placeholders.** The batch runs sent the well id as null. Production fills it with the Roam well id from the form, with match status `exact`.
- **Out:**
  - the report, from its YAML frontmatter to its `row_counts` footer;
  - the stop reason;
  - input and output tokens (output includes reasoning) and time to first token;
  - hashes of the prompt, contract and clarification files.
- **Fail:**
  - `MODEL_TIMEOUT` — the deadline or the read timeout fired.
  - `MODEL_TRUNCATED` — the stop reason was `max_tokens`, or the `row_counts` footer is missing.
  - `MODEL_ERROR` — access, throttling or a service error; it carries the AWS error code.
- **Configuration, confirmed in Step 2:**
  - **Model:** the inference profile `us.anthropic.claude-opus-5-5`. The `global.` profile is denied by an organization policy.
  - **Effort:** medium, with adaptive thinking. High effort was tested and rejected: it fixed none of the systematic differences, introduced new ones, and doubled the thinking time.
  - **Limits:** `max_tokens` 128,000; budget 780 s. Reports run at about 1 byte per output token.
  - **Measured:** about USD 0.80 per document, from USD 0.55 to 1.32. Model time was 4–8 minutes, including 1.5–4.5 minutes of thinking before the first token. That projects to about USD 80 per 100 documents.

### Validate and stage (Lambda)

*Rewritten at the close of Step 3, 2026-10-13.*

- **In:** the report, the organisation and well from the form, and the run's *Detect* and *Prep* results (page roles and page text).
- **Does:**
  1. **Structure guards, before anything is staged.** The frontmatter, the five data blocks and a parseable `row_counts` footer must be present; otherwise `REPORT_INVALID`. A footer that disagrees with the parsed rows is a warning.
  2. **Family check.** The report's `vendor_family` must agree with *Detect*'s family; otherwise `FAMILY_MISMATCH`.
  3. **The existing loader's per-document transform, with its logic unchanged.**
     - Aliases are resolved before a pump string is collapsed.
     - Bad values are quarantined, never coerced.
     - A value that varies across scenarios is a warning, apart from a short list of columns allowed to vary.
     - A contradictory pump-string identity halts the run.
     - **Confirmed in Step 3:** over the 46 corpus reports, it reproduces the earlier batch loader's outputs exactly (76 pump-config rows, 42 design-context rows, 1,834 curve observations, and every log). The one deliberate change: the provenance column is no longer compared across scenarios, because it varies by construction. That turns two wells' spurious halts into warnings.
  4. **Model names** are resolved to their canonical names through the loader's alias lookup. A separate normalizer step is not needed; in Step 3 every SpyGlass section resolved.
  5. **Validation against the method's validator specification.**
     - 52 of its 80 rules can be checked from the report, and all 52 are implemented. The rest need the PDF or other documents, or do not apply.
     - Four rules were narrowed where real documents showed them too broad. For example, the SLB rule against head values now ignores printed design-point total dynamic head.
     - **Guards:** scenario-label consistency. Each SLB, Baker or Valiant run carries that family's **known-issue notice**.
  6. **Unknown models,** flagged against a read-only snapshot of `universal_catalog`: `absent`, `no_head_curve` (in the catalogue but with no head curve), or `unresolved`. Nothing is written to the catalogue.
  7. **Review list.** Per value, for the pump string and the well-item fields only. A value needs review when any deterministic signal fires:
     - its verbatim text is not on the page it cites;
     - a validation rule fired;
     - the extractor marked the row a conflict (one item per row; informational when both printed forms are the same model);
     - it was quarantined;
     - it is a known-issue field;
     - the family prints it but the report has none;
     - SpyGlass's title-versus-table frequency trap.

     The list never blocks a run. On the twelve Step-2 test reports, 12% of values needed review.
- **Severity in round 1:**
  - **Structure → `REPORT_INVALID`;** nothing is staged.
  - **Identity halt → `LOAD_HALTED`;** the grains are still stored.
  - **Everything else is a warning on the run.**
  - Each finding keeps the specification's own severity, and the run reports the document's `spec_validation_status` (`passed`, `needs_review` or `failed`) without failing on it.
  - Section-order and identity findings carry `blocks_publish`, which round 2's well writer must honour.
- **Out:**
  - the three grains;
  - quarantine, findings, notices and unknown models;
  - the review list;
  - the scenario-variance, identity and gas logs.
- **Fail:** `REPORT_INVALID`, `FAMILY_MISMATCH`, `LOAD_HALTED`, `ASSET_INVALID`.

### Store (Lambda)

*Rewritten at the close of Step 3. Built and tested locally; Step 5 writes it to S3.*

- **In:** everything the run produced.
- **Does:** writes `runs/<run_id>/`:
  - `run.json` and `source.json` (source path and SHA-256);
  - `report.md` and `prep_manifest.csv`;
  - the three grains as Parquet;
  - `quarantine.jsonl`, `findings.jsonl`, `unknown_models.jsonl`, `review.jsonl` and `notices.jsonl`;
  - the scenario-variance, identity and gas logs.
- **Each file is written atomically.** On S3, every object is written first and `run.json` last.
- **A failed run stores what it has.** Runs are independent: there is no "current" version and no rebuild.
- **`run_id`:** `YYYYMMDDTHHMMSSZ-<8 hex>`.
- **Reproducible:** two runs on the same input give byte-identical stores, apart from the run id, timestamps and durations. The prompt, contract, clarification and asset hashes are recorded.
- **Data assets** sit outside the code repository: the alias evidence, the seed alias list, the catalogue snapshot and the field inventory. Each is checked against its SHA-256 at load; a mismatch is `ASSET_INVALID`.
- **Location, proposed:** `s3://roam-ds-assets/esp-design-extraction/runs/<run_id>/` for runs, and `…/assets/` for the assets.
- **Fail:** `STORE_FAILED`, naming the file it failed at.

### Record (Lambda)

*Rewritten at the close of Step 3. This is the shape Step 5 writes to DynamoDB.*

- **Status:** `running` (written first, with a `deadline_at`; the default budget is 900 s) → `succeeded` | `failed`. A `running` record past its deadline reads as failed and stale, which covers a Lambda killed by its timeout.
- **`run.json` carries:**
  - the mode (PDF or report), family, organisation and well;
  - the source hashes, the uploaded file name (`source_name`, added in Step 4) and the page references;
  - rows per grain and the quarantine count;
  - findings by round-1 and by specification severity;
  - `spec_validation_status` and `blocks_publish`;
  - review counts and unknown models;
  - the error (code, stage, message);
  - the model settings, stop reason, time to first token and model time;
  - tokens and `cost_usd`;
  - the hashes;
  - the duration of each stage, and the start and end times;
  - the store's file list.
- **The run's status and the document's status are separate.** A run succeeds when the pipeline worked; `spec_validation_status` describes the document.
- **Added in Step 4:**
  - **`source_name`** sits in the record's source block and in `source.json`. It is the name passed with `--source-name`, otherwise the PDF's file name. Prep and prompt assembly read the same value, so the name recorded is the name sent. Older records without it still render.
  - **`--run-id`** lets the caller set the id. An existing run folder is refused before any AWS call.

### Viewer (app)

*Rewritten at the close of Step 4, 2026-10-13.* Read only in round 1. It sits on a storage layer with a local backend; Step 5 adds an S3 backend behind the same interface.

- **Run list:**
  - every run, newest first, filterable by organisation, well and status;
  - the run's status and the document's `spec_validation_status`, shown separately with plain-language labels. Stale runs are computed at read time;
  - the latest succeeded run per organisation and well, marked. Runs with no well are never marked;
  - launches that never wrote a record, listed as such.
- **Run detail:**
  - the record summary, including the uploaded file name;
  - **the source PDF, found by SHA-256 only:** the intake copy first, then an optional list of local folders (`ESP_SOURCE_PDF_DIRS`). Paths recorded in the run are shown as text and never opened. Pages are rendered as images with `pypdfium2`, because Streamlit's PDF element cannot open at a given page;
  - the three grains with their page references; each reference jumps the PDF pane to that page (the first page, for a multi-page reference);
  - findings with severity. **Findings carry no page,** so the page comes from the staging row they name, read from the stored report;
  - notices, unknown models with their level, and the review list with its reasons;
  - the prep manifest and the logs;
  - the report as plain text, so that its internal link markup never renders as links.
- **A missing or malformed file** shows a notice in its own panel, and the rest of the run renders.
- **Charts: head against flow per curve,** from the curve observations.
  - A chart's curve identity is the staging identity without frequency: one series per frequency, with markers by point type and the page reference on hover.
  - Whole-string, per-section and unstated rows are drawn apart.
  - A curve with no drawable point is listed with the reason.
  - **No catalogue overlay.** The catalogue stores head only as unlabelled fifth-order coefficients, at 60 Hz and per stage, with no documented form. Nothing is fitted, scaled or interpolated.

## 3. Account and resources

Everything is in **roam-ai datascience (640168431387)**, region **us-east-1**. The region is confirmed by the inference profile's ARN.

| Resource | Status |
|---|---|
| `universal_catalog` in `tapered_ideal_v2` (719 rows) | exists; **read only**. The pipeline reads a hashed snapshot of it, not Athena |
| `roam_prd_ddb.default.esp_well_configuration_v2` | exists; the source for the well-list snapshot, with `organization_id` and `well_id` confirmed |
| Bedrock inference profile `us.anthropic.claude-opus-5-5` | invokable; confirmed |
| Bedrock inference profile `global.anthropic.claude-opus-5-5` | denied by organization policy |
| Run store and assets: `s3://roam-ds-assets/esp-design-extraction/runs/` and `…/assets/` | proposed in Step 3; written from Step 5 |
| `<intake prefix>` | new; Step 5 |
| Lambda `<extraction lambda>` and its execution role | new; Step 5 |
| DynamoDB `<run table>` | new, or the product's run-log shape if one fits; Step 5 |
| CloudWatch log group | created with the Lambda |
| App hosting | the tapered calculator's route on `roam-container-platform`, under `apps/enterprise/` |

**Lambda configuration, proposed:**
- **Timeout:** 15 minutes, with a 780 s budget. Step 2 measured up to 487 s of model time.
- **Asynchronous retries: 0.** S3's asynchronous invoke otherwise retries a failed run twice, which is two more paid Opus calls.
- **An idempotent run id,** taken from the S3 object key and version. A run that has already finished is skipped, since S3 can deliver an event more than once.
- **Reserved concurrency: 1,** as a cost and throttling guard. Runs are independent, so correctness doesn't need it.
- **The Bedrock client is built per invocation,** so that each run's timeouts follow its own budget.

**Permissions, round 1** (the actions are listed here; the ARNs are set when the roles are created):
- **Lambda role:**
  - read the intake prefix;
  - write the run store;
  - read `universal_catalog`'s data;
  - Bedrock invoke, streaming, on the `us.` inference profile. A geographic profile may also need the model in each region it routes to; check AWS's documentation in Step 5;
  - DynamoDB put and update on the run table;
  - CloudWatch logs.
- **App task role:**
  - write the intake prefix;
  - read the run store and the run table.
  - With the snapshot, it needs no cross-account grant.

**Packaging:** a zip or a container image, decided in Step 5. The PDF stack (`pdfplumber`, `pypdf`) is pure Python. The Lambda needs no page renderer.

**At the close of Step 4:**
- **The app runs locally,** against a local store, and makes no AWS call itself. Its dependencies are `streamlit` 1.65.0, `plotly` 7.1.0 and `pypdfium2` 5.14.0.
- **The app, unlike the Lambda, needs a page renderer** (`pypdfium2`), so Step 6's hosting image must carry it.
- **The app loads its fonts from Google Fonts at runtime,** with a plain fallback offline. Step 6 decides whether hosting allows that, or whether the fonts are bundled.

## 4. Where errors are reported

There is **no push notification**: no SNS, no email, no alerting. A failure is visible in two places:
- **the run record,** shown in the app's viewer: status, code, message, the stage that failed, and the source link;
- **CloudWatch logs,** with the full trace, keyed by run id.

| Code | Stage | Meaning | Action |
|---|---|---|---|
| `IMAGE_ONLY` | Detect | No text layer (includes XSize) | Can't be extracted; manual entry in round 2 |
| `UNKNOWN_TEMPLATE` | Detect | No family signature matched | A new template is follow-on work (but see below) |
| `AMBIGUOUS_TEMPLATE` | Detect | More than one matched | DS fixes the signatures |
| `REQUIRED_PAGES_MISSING` | Detect | Known family, spine incomplete | Check the document |
| `PREP_FAILED` | Detect, Prep | Unreadable PDF, unmapped glyphs, or a kept page with no text | DS |
| `MODEL_TIMEOUT` · `MODEL_TRUNCATED` · `MODEL_ERROR` | Extract | No complete report | Re-upload; DS if it repeats |
| `REPORT_INVALID` · `LOAD_HALTED` | Validate and stage | Off-contract, or the identity halt | DS |
| `STORE_FAILED` | Store | Outputs not written | DS |
| `FAMILY_MISMATCH` | Validate and stage | The report's family disagrees with *Detect* | DS |
| `ASSET_INVALID` | Extract (prompt assembly), Validate and stage | A data asset is missing, or its hash doesn't match | DS |

**The one failure the Lambda can't report itself** is being killed at 15 minutes. **Proposed:** at start, the Lambda writes the record as `running` with a deadline. The viewer shows a `running` record past its deadline as **timed out**, and CloudWatch carries the platform's timeout line.

**Bedrock reliability, observed in Step 2.** At high effort, calls were repeatedly refused, or their streams dropped with no error recorded by Bedrock. The no-retry policy surfaced every one as `MODEL_ERROR` or `MODEL_TIMEOUT`.

**Built in Step 3:**
- The `running`-with-deadline record above exists: past its deadline, a `running` record reads as stale.
- A failure forced at each of seven points (*Detect*, *Prep*, *Extract*, an invalid report, an identity halt, *Store*, *Record*) stores what it has, with the right code.
- Validation findings below `REPORT_INVALID` and `LOAD_HALTED` never fail a run.

**At the close of Step 4:**
- **The local app adds one state of its own: launch failed,** when the process exits without writing a record. The viewer shows the last lines of the launch log. In Step 5, the Lambda's record and CloudWatch take its place.
- **Seen in the app:** `IMAGE_ONLY` on an XSize document, and `UNKNOWN_TEMPLATE` on two real design documents from the product's upload bucket. Both stopped before any model call, at no cost.
- **`UNKNOWN_TEMPLATE` covers more than new templates.**
  - One of the two was a newer generator version of the ChampionX template: its page-1 representative line changed, and the vendor name appears only in the logo.
  - The other was the second half of an SLB report, re-saved through Word, with no schematics page. It should fail as `REQUIRED_PAGES_MISSING`.
  - Neither is changed in Step 4. How widely the product's uploads differ from the corpus is measured before any signature is changed (§6).

## 5. Round 1 decisions

**Set by Keith:**
- Streamlit intake with an existing well.
- S3 plus one Lambda: no step function, no API Gateway.
- Opus via Bedrock.
- No vision: image-only documents fail.
- No approval gate and no SNS. Runs store what they wrote, the source link and the page references.
- Logging to DynamoDB plus CloudWatch.
- The datascience account first.
- Round 1 is whatever gets extraction running, without matching product storage.
- The calculator does not read round 1's numbers.
- Manual entry, edit-afterwards and the version store are round 2.

**Standing from planning:**
- Detection is deterministic.
- XSize, vision and new-template creation are out of scope.
- No catalog refit: unknown models are flagged.
- The app is separate from the tapered calculator.
- Numbers are compared deterministically, never by an LLM.

**Settled in Step 2:**
1. **Model:** `us.anthropic.claude-opus-5-5`, medium effort with adaptive thinking, `max_tokens` 128,000.
2. **Contract clarifications are on by default** for three families.
3. ***Validate and stage* gains a required-field guard and a scenario-label consistency guard.** Step 2 also listed a model-name normalizer; Step 3 found it unnecessary (see below). The required-field guard became the validator specification's own rules.
4. **The single Lambda stands.** The longest document's model time is under the 10-minute fallback threshold.
5. **The well list is a snapshot in round 1.** The app writes `submitted` before upload; the Lambda writes `running`, with a deadline, at start. Runs are stored independently under their run id.

**Settled in Step 3:**
1. **The stage is the earlier batch loader's per-document transform, with its logic unchanged.** Parity is exact on the corpus, with one named deviation (the provenance column).
2. **Alias evidence in production:** the corpus evidence, frozen as a versioned asset, plus the run's own report.
3. **Severity:** structure fails the run; the identity halt fails it but stores the grains; everything else is a warning. The specification's severity is kept and reported, not enforced.
4. **Validation implements every specification rule that can be checked from the report.** Each family with an accepted known issue gets a notice on every run.
5. **Review is deterministic and non-blocking,** limited to the fields that matter. There is no model confidence score and no second model pass. Capturing reviewer verdicts is round 2.
6. **No separate model-name normalizer;** the alias lookup resolves canonical names.
7. **Data assets are hashed and kept outside the code repository.**
8. **A file name containing `&` is now substituted literally** in the prompt; the earlier batch prompts had placeholder text there for two SLB documents.
9. **Run status is separate from document status.**

**Settled in Step 4:**
1. **The well list is a production-only snapshot,** with values kept exactly as exported.
2. **Local intake launches the command-line run as a detached process and watches the run store.** This is Step 5's shape, with the process in place of the Lambda.
3. **The viewer finds the source PDF by hash,** never by a recorded path.
4. **The document's status is shown apart from the run's status,** in plain language.
5. **Charts show extracted points only:** no catalogue overlay, and no fitting or scaling.
6. **Every run is kept and listed;** the latest succeeded run per well is marked.

## 6. Verification

### Step 2: parity with the earlier batch extraction

One document per vendor family was extracted with the new path and compared with its earlier batch report. The comparison was deterministic. Every difference in pump configuration and in a contract's known-trap fields was checked against the PDF.

| Families | Result |
|---|---|
| SpyGlass (F1), ChampionX (F2), ELS (F4) | Pass. For SpyGlass, the earlier reports had been post-processed by the model-name normalizer. Step 3 found the loader's alias lookup already resolves those names, so no separate normalizer step is needed. |
| SLB (F3), Baker (F5), Valiant (F7) | **Known issues, accepted:** |

The three known issues:
- **SLB:** `pump_model_as_printed` drops an in-cell stage annotation, `(<n>stg)`. The stage count survives in its own column.
- **Baker:** on some rows, `scenario_label_staged` carries a short label the document never prints, instead of the full case name. The scenario-label guard flags this on every run.
- **Valiant:** one gas-cascade indicator value is unexplained. The cascade indicator feeds no load column.

**Not verified in Step 2:** fields the new reports filled where the earlier ones left them empty. Step 6 checks a sample against the PDF.

### Step 3: validate, stage and store

- **Loader parity:** exact on all 46 corpus reports, across all ten outputs, with the one named deviation.
- **Determinism:** two runs on the same input give identical stores, apart from ids and times.
- **Forced failures:** seven failure points, each storing what it has, with the right code.
- **First live end-to-end run** (an ELS document, through the command-line tool):
  - succeeded in 240 s of model time, at USD 0.53;
  - staged its pump string, design context and curve points;
  - the specification status was `passed`, with 1 of 23 values flagged for review.

### Step 4: the local app

Checked by Hamed in the app:
- **The form, on the real well-list snapshot:** every row loaded; organisations that differ only by case carry the hint; an id containing two consecutive spaces is kept exactly; submit stays locked until the cost box is ticked.
- **Failure uploads:**
  - an XSize document, filed under the test organisation, showed `IMAGE_ONLY`;
  - two design documents from the product's upload bucket showed `UNKNOWN_TEMPLATE` (§4).
  - Each run's error code, stage and message showed in the viewer.
- **A live upload:** an ELS document from the corpus, under its real organisation and well.
  - It succeeded. The viewer showed the run as it progressed, then its numbers, page references, review list and unknown models.
  - It staged 7 curve observations, against 6 in Step 2's run of the same document. The extra row came from a chart title and carries no point type and no value, so the model's output on one document can vary between runs.
  - Its document status was `failed` under the validator specification. Under the round-1 severity rule this is a review signal, not a run failure.
- **Tests:** the viewer and intake suites pass with synthetic runs and wells only; the process launch is stubbed in every test; the earlier suites are unchanged. No client identifier is added to the tracked tree.

### Step 6: end to end

**The test:**
- Upload a known-template document through the app; its numbers and run show in the viewer and the log.
- Upload an unrecognised or image-only document; the reported error shows.

**The documents:**
- **Chord's design documents** were located in the product's upload bucket, one design PDF per well.
- **Chord is live,** so its wells are in the dropdown. The wells of the original corpus are in the snapshot too.
- The one tried in Step 4 failed detection as a partial document, so it is not yet known which of them serve the known-template test.
- **Proposed before Step 6:** run *Detect* alone over every design PDF in the product's upload bucket. It is deterministic and makes no model calls. It counts the documents by family, near miss, generator and page count.
  - Documents that match a known family serve the known-template test.
  - The rest size the work on new template versions, and serve the unrecognised-template test.

---

# Round 2 — product

**Status: a target, not a design yet.** Keith set the write shape as his current thinking. Round 2 is designed in detail once the questions in §9 are answered, and that design is added to this note in Step 8.

## 7. Target shape

```
same app + Lambda path (Detect → Prep → Extract → Validate and stage)
        │
        ├──► DynamoDB, on the well (alongside the rest of well config)
        │      pump string: model, housings, stages, setting depths
        │      design context: SGs, water cut, bubble point, temperatures, motor,
        │                      design frequency, vendor design numbers
        │
        └──► Athena: curve observations only
               one table in each product account
               partitioned organization_id / well_id
               an upload writes that well's partition
```

- **One upload writes one well.** No whole-corpus rebuild, and no table swap per PDF.
- **Round 2's app features,** built once against product storage: manual entry (image-only documents included), edit-afterwards, and the version store.
- **Mapping, proposed for Step 8:**
  - pump configuration and design context become well-config attributes;
  - curve observations become the partitioned table.
  - How a multi-section string sits on one well record is part of Step 8.
- **The dropdown** reads the well list live.

## 8. What carries over from round 1

**Unchanged:** *Detect*, *Prep*, *Extract*, *Validate and stage*, the run record, the error codes, and the viewer.

**Changed:** round 2 replaces *Store* with the two product writers, and adds the app features above.

## 9. Open before round 2 is built

1. **Well config.** Which DynamoDB table holds well config? Does it already have pump-string fields? Writing to it needs its owner's agreement.
2. **Versions.** Does "every version kept" still hold when an upload overwrites a well's partition?
3. **Reads.** Where do `universal_catalog` and the tapered calculator read from once the data is in product storage?
4. **Monorepo.** Does round 2 include the move into the ESP monorepo?
5. **Compute.** Which account runs round 2's Lambda?
6. **B2.** How does round 2 divide with B2, the well-to-pump linking task?
7. **Duplicate wells.** The product's well list holds organisations that differ only by case, and wells that differ only by case or spacing. Which record a round-2 write goes to is decided with the list's owner.

---

## Build steps

| Step | Round | Ends in | Status |
|---|---|---|---|
| 1 | — | This design note | done |
| 2 | 1 | Extraction core: detection with its errors; single-document prep; Opus on Bedrock with bounds; parity on one document per family; the longest document timed | **done 2026-10-06** |
| 2.5 | — | Well-item field list and its brief, for the product's well configuration | **done 2026-10-12** |
| 3 | 1 | Validate, stage and store by code: validation, guards and review list; the three grains per run; unknown-model flag; run store; run record; command-line run | **done 2026-10-13** |
| 4 | 1 | Streamlit app, local: upload form with the well-list snapshot; detached run and live watch; run viewer with page-linked PDF, read-only review list and curve charts | **done 2026-10-13** |
| 5 | 1 | Lambda deployed: S3 trigger, run log, failures logged | next |
| 6 | 1 | App hosted; end to end; both verification uploads; sample check of added fields | |
| 7 | 1 | Runbook, round 1 | |
| 8 | 2 | Round-2 design: the questions in §9 answered, added to this note | |
| 9 | 2 | Product writers: well config to DynamoDB, curves to the well's Athena partition | |
| 10 | 2 | Manual entry, edit-afterwards, version store | |
| 11 | 2 | Deployed to product end to end; round-2 verification | |
| 12 | 2 | Runbook covering both rounds | |
