# Design-document extraction app — design note

**Task:** B1, make design-document extraction a pipeline rather than a one-off. GitHub issue `roam-datascience-esp #98`.
**Status:** approved by Keith; restructured into two rounds on 2026-10-05; updated at the close of Step 2 on 2026-10-06. Owner: Hamed.

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

## 2. Stages — inputs, outputs, failure

### Intake (app)

- **In:** organisation and an existing well, chosen from dropdowns, plus the PDF.
- **Does:** writes the DynamoDB run record with status `submitted`. Then it writes the PDF to `<intake prefix>`, with the run's metadata: organisation, Roam well id, uploader. Because the record is written first, a trigger that never fires shows up as a run stuck in `submitted`, not as nothing. **Proposed.**
- **Out:** the S3 object; the run record in `submitted`.
- **Dropdown source:** `roam_prd_ddb.default.esp_well_configuration_v2`.
  - *Confirmed* readable from datascience (174 rows).
  - **Not yet confirmed:** that it carries an organisation column and the canonical Roam well id.
  - **Proposed for round 1:** the app reads a snapshot of that well list, exported once into its configuration, so the app role needs no cross-account read. Round 2 replaces the snapshot with a live read.

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

- **In:** the report.
- **Does:**
  - **Runs the existing loader's report reader and staging transform,** with the logic unchanged.
    - Bad values are quarantined, never coerced.
    - A design-condition anomaly is recorded on the run as a warning: a bubble point that tracks the static datum.
    - Contradictory pump-string identity across a document's scenarios halts the run.
  - **Normalizes model names.** It runs the method's model-name normalizer to fill `pump_model_canonical`. The extractor is not meant to emit the canonical name; it is resolved after extraction through the alias lookup. In Step 2 this recovered every canonical name it was tested on.
  - **Runs two deterministic guards.** Each failure is a warning on the run, not a halt, until Step 3 settles severity:
    - **Required fields:** a field the family contract requires is present wherever its table carries it.
    - **Scenario labels:** each scenario number carries one label across every table in the report.
  - **Collapses the staged rows into the three grains:** `pump_config`, `esp_well_design_context` and `curve_observations`. It resolves aliases before collapsing a pump string, keeps one row per string, and lets the latest design date win.
  - **Flags every pump model absent from `universal_catalog`.** The catalog is read only; the flag goes on the run record.
- **Out:** the three grains for this run, the quarantine list, warnings (guard results included), and the unknown-model flags.
- **Fail:** `REPORT_INVALID` (unparseable or off-schema); `LOAD_HALTED` (the identity halt).

### Store (Lambda)

- **In:** everything the run produced.
- **Does:** writes the run's outputs under `<run store>/<run id>/`:
  - the source link, the report and the manifest;
  - the three grains, as Parquet;
  - the quarantine list and the flags.
- A failed run stores what it has up to the stage that failed. Runs are independent: there is no "current" version and no rebuild. **Proposed.**
- **Reproducible:** the stored hashes, plus the source PDF, re-run the same extraction.
- **Out:** a run directory the viewer reads directly from S3. Round 1 uses no Athena and no Glue.
- **Fail:** `STORE_FAILED`.

### Record (Lambda)

Moves the run record to `succeeded` or `failed`. It carries:
- the family, the source link and the page references;
- rows per grain and the quarantine count;
- warnings and unknown-model flags;
- the error code and message;
- the hashes;
- token counts and the duration of each stage.

### Viewer (app)

Lists runs. For each one it shows the record, opens the source PDF, and shows the extracted numbers per grain, with page references and flags. Read only in round 1.

## 3. Account and resources

Everything is in **roam-ai datascience (640168431387)**, region **us-east-1**. The region is confirmed by the inference profile's ARN.

| Resource | Status |
|---|---|
| `universal_catalog` in `tapered_ideal_v2` (719 rows) | exists; **read only** |
| `roam_prd_ddb.default.esp_well_configuration_v2` | exists; the source for the well-list snapshot |
| Bedrock inference profile `us.anthropic.claude-opus-5-5` | invokable; confirmed |
| Bedrock inference profile `global.anthropic.claude-opus-5-5` | denied by organization policy |
| `<intake prefix>`, `<run store prefix>` | new; Step 3 |
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

**Packaging:** a zip or a container image, decided in Step 5. The PDF stack (`pdfplumber`, `pypdf`) is pure Python. No page renderer is needed.

## 4. Where errors are reported

There is **no push notification**: no SNS, no email, no alerting. A failure is visible in two places:
- **the run record,** shown in the app's viewer: status, code, message, the stage that failed, and the source link;
- **CloudWatch logs,** with the full trace, keyed by run id.

| Code | Stage | Meaning | Action |
|---|---|---|---|
| `IMAGE_ONLY` | Detect | No text layer (includes XSize) | Can't be extracted; manual entry in round 2 |
| `UNKNOWN_TEMPLATE` | Detect | No family signature matched | A new template is follow-on work |
| `AMBIGUOUS_TEMPLATE` | Detect | More than one matched | DS fixes the signatures |
| `REQUIRED_PAGES_MISSING` | Detect | Known family, spine incomplete | Check the document |
| `PREP_FAILED` | Detect, Prep | Unreadable PDF, unmapped glyphs, or a kept page with no text | DS |
| `MODEL_TIMEOUT` · `MODEL_TRUNCATED` · `MODEL_ERROR` | Extract | No complete report | Re-upload; DS if it repeats |
| `REPORT_INVALID` · `LOAD_HALTED` | Validate and stage | Off-contract, or the identity halt | DS |
| `STORE_FAILED` | Store | Outputs not written | DS |

**The one failure the Lambda can't report itself** is being killed at 15 minutes. **Proposed:** at start, the Lambda writes the record as `running` with a deadline. The viewer shows a `running` record past its deadline as **timed out**, and CloudWatch carries the platform's timeout line.

**Bedrock reliability, observed in Step 2.** At high effort, calls were repeatedly refused, or their streams dropped with no error recorded by Bedrock. The no-retry policy surfaced every one as `MODEL_ERROR` or `MODEL_TIMEOUT`.

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
3. ***Validate and stage* gains three additions:** model-name normalization, a required-field guard and a scenario-label consistency guard.
4. **The single Lambda stands.** The longest document's model time is under the 10-minute fallback threshold.
5. **The well list is a snapshot in round 1.** The app writes `submitted` before upload; the Lambda writes `running`, with a deadline, at start. Runs are stored independently under their run id.

## 6. Verification

### Step 2: parity with the earlier batch extraction

One document per vendor family was extracted with the new path and compared with its earlier batch report. The comparison was deterministic. Every difference in pump configuration and in a contract's known-trap fields was checked against the PDF.

| Families | Result |
|---|---|
| SpyGlass (F1), ChampionX (F2), ELS (F4) | Pass. For SpyGlass, the earlier reports had been post-processed by the model-name normalizer, which is why it moves into *Validate and stage*. |
| SLB (F3), Baker (F5), Valiant (F7) | **Known issues, accepted:** |

The three known issues:
- **SLB:** `pump_model_as_printed` drops an in-cell stage annotation, `(<n>stg)`. The stage count survives in its own column.
- **Baker:** on some rows, `scenario_label_staged` carries a short label the document never prints, instead of the full case name. The scenario-label guard flags this on every run.
- **Valiant:** one gas-cascade indicator value is unexplained. The cascade indicator feeds no load column.

**Not verified in Step 2:** fields the new reports filled where the earlier ones left them empty. Step 6 checks a sample against the PDF.

### Step 6: end to end

**The test:**
- Upload a known-template document through the app; its numbers and run show in the viewer and the log.
- Upload an unrecognised or image-only document; the reported error shows.

**The documents:**
- **Chord's design documents** exist for the whole organisation. They were uploaded through the ESP app's UI to an S3 location not yet identified.
- **Chord is live,** so its wells should be in the dropdown.
- **Once the documents are located,** *Detect* runs over them on its own. It is deterministic and makes no model calls.
  - Documents that match a known family serve the known-template test.
  - The rest serve the unrecognised-template test.

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

---

## Build steps

| Step | Round | Ends in | Status |
|---|---|---|---|
| 1 | — | This design note | done |
| 2 | 1 | Extraction core: detection with its errors; single-document prep; Opus on Bedrock with bounds; parity on one document per family; the longest document timed | **done 2026-10-06** |
| 3 | 1 | Validate, stage and store by code: normalizer and guards; the three grains per run; unknown-model flag; run store; run record; run locally | next |
| 4 | 1 | Streamlit app, local: upload form and run viewer | |
| 5 | 1 | Lambda deployed: S3 trigger, run log, failures logged | |
| 6 | 1 | App hosted; end to end; both verification uploads; sample check of added fields | |
| 7 | 1 | Runbook, round 1 | |
| 8 | 2 | Round-2 design: the questions in §9 answered, added to this note | |
| 9 | 2 | Product writers: well config to DynamoDB, curves to the well's Athena partition | |
| 10 | 2 | Manual entry, edit-afterwards, version store | |
| 11 | 2 | Deployed to product end to end; round-2 verification | |
| 12 | 2 | Runbook covering both rounds | |
