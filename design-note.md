# Design-document extraction app — design note

**Task:** B1, make design-document extraction a pipeline rather than a one-off. GitHub issue `roam-datascience-esp #98`.
**Status:** approved by Keith, restructured into two rounds on 2026-10-05. Owner: Hamed.

A design document from a known vendor template is uploaded through a Streamlit app. Template detection runs, then Opus on Bedrock extracts the numbers. The run is logged and every failure is reported. B1 is built in two rounds, and is done when round 2 is done:

- **Round 1, datascience account.** A PDF goes in through the app. The numbers are stored by code in datascience and are visible in the app, with a run log. The tapered ideal calculator does not read them this round.
- **Round 2, product.** The same path writes to product storage. Round 2 adds manual entry, edit-afterwards and a version store.

**How to read this note.** *Confirmed* means checked against the running system. *Proposed* is a design choice still open to change. Names written as `<placeholders>` do not exist yet.

**What is reused.** The extraction method already lives in this repository at `esp_design_extraction_method/`: template triage, page slicing, the per-vendor extraction contracts and prompts, the report validator, and the loader. This app turns that method into a pipeline. The extraction logic itself does not change.

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

- **In:** organisation and an existing well, picked from dropdowns, and the PDF.
- **Does:** writes the DynamoDB run record with status `submitted`. Then writes the PDF to `<intake prefix>`, with the run's metadata: organisation, Roam well id, uploader. Writing the record first means a trigger that never fires shows up as a run stuck in `submitted`, not as nothing. **Proposed.**
- **Out:** the S3 object, and the run record in `submitted`.
- **Dropdown source:** `roam_prd_ddb.default.esp_well_configuration_v2`, which is *confirmed* readable from datascience (174 rows). **Not yet confirmed:** that it carries an organisation column and the canonical Roam well id.
  - **Proposed for round 1:** the app reads a snapshot of that well list, exported once into the app's configuration. That way the app role needs no cross-account read this round.
  - Round 2 replaces the snapshot with a live read.

### Detect (Lambda, deterministic)

- **In:** the PDF.
- **Does:**
  1. **Text layer.** Counts non-whitespace characters per page. If page 1, or every page, falls under the image-route floor of 20 characters, the run fails with `IMAGE_ONLY`.
  2. **Signature.** Matches each vendor family's page-1 markers. Exactly one family must match. None → `UNKNOWN_TEMPLATE`; more than one → `AMBIGUOUS_TEMPLATE`.
  3. **Required pages.** Every page role the family's contract marks `required` must be found in the document. Otherwise the run fails with `REQUIRED_PAGES_MISSING`, naming the missing roles.
- **Out:** the family and its page roles, or an error. No LLM is involved.
- **Two changes from the existing triage code, both proposed:**
  - **SpyGlass (F1) detection drops one marker.** The `Summit ESP Representative` marker sits in a disclaimer that five real SpyGlass documents don't print.
    - The existing code assigned families from a fixed file list, and the marker only confirmed that assignment. The pipeline has no such list.
    - Detection therefore relies on `SIZING REPORT` plus the required-page check.
    - Step 2 checks that every SpyGlass document in the corpus detects as F1, and that nothing else does.
  - **XSize reports as image-only.** XSize documents have no text layer, and their PDF producer string is shared with another vendor. They can't be told apart from any other scanned PDF.
    - In round 1 the error says the document can't be extracted.
    - Manual entry for these documents arrives in round 2.

### Prep (Lambda)

- **In:** the PDF and its family.
- **Does:** the single-document form of the existing slicing step:
  - slices to the family's pages, dropping only pages the contract marks plot-only;
  - extracts layout text per kept page;
  - translates the private-use glyphs the vendors' fonts emit;
  - writes manifest rows.

  It uses no fixed file list, renders no images and keeps no corpus-wide state. **Proposed.**
- **Out:** per-page text keyed by original page number, and the page manifest. These page numbers become the run's page references.
- **Fail:** `PREP_FAILED` — an unreadable PDF, or private-use glyphs left unmapped.

### Extract (Lambda → Bedrock, Opus)

- **In:**
  - the page text and the manifest;
  - the family's prompt and contract;
  - the validator spec, the staging schemas and the closed field list.
- **Does:** assembles the prompt **in the same order** the existing batch runners do, then calls Opus on Bedrock. Keeping the order means Step 2's parity test measures the model change and nothing else.
  - **Proposed:** a streaming call, bounded by `max_tokens` and by a client read timeout. The timeout is set from the Lambda's remaining time, minus a fixed reserve for the later stages.
  - The prompt tells the model to finish within 12–13 minutes. That instruction is not a bound, because the model has no clock. The timeout is the bound.
- **Placeholders:**
  - The batch runs sent the well id as null.
  - Production fills it with the Roam well id from the form.
  - The parity run keeps null, so the comparison is like for like.
- **Out:**
  - the report, from its YAML frontmatter to its `row_counts` footer;
  - the stop reason and token counts;
  - hashes of the prompt and contract files used.
- **Fail:**
  - `MODEL_TIMEOUT` — the client timeout fired.
  - `MODEL_TRUNCATED` — the stop reason was `max_tokens`, or the `row_counts` footer is missing. This is the batch runners' existing completeness test.
  - `MODEL_ERROR` — access, throttling, or a service error.
- **Model:** Opus 5.5 on Bedrock, through a cross-region inference profile. *Confirmed* available in the datascience account. The profile id is still to be recorded.

### Validate and stage (Lambda)

- **In:** the report.
- **Does:**
  - Runs the existing loader's report reader and staging transform, with the logic unchanged.
    - Bad values are quarantined, never coerced.
    - A design-condition anomaly (a bubble point that tracks the static datum) is recorded on the run as a warning.
    - Contradictory pump-string identity across a document's scenarios halts the run.
  - Collapses the staged rows into three grains: `pump_config`, `esp_well_design_context`, `curve_observations`. This applies the loader's logic to one document: resolve aliases before collapsing a pump string, keep one row per string, and let the latest design date win.
  - Flags every pump model absent from `universal_catalog`. The catalog is read only; the flag goes on the run record, and nothing is written to the catalog.
- **Out:** the three grains for this run, the quarantine list, warnings, and unknown-model flags.
- **Fail:** `REPORT_INVALID` (unparseable or off-schema); `LOAD_HALTED` (the identity halt).

### Store (Lambda)

- **In:** everything the run produced.
- **Does:** writes the run's outputs under `<run store>/<run id>/`: the source link, the report, the manifest, the three grains as Parquet, the quarantine list and the flags.
  - A failed run stores what it has up to the stage that failed.
  - Runs are independent. There is no "current" version and no rebuild. **Proposed.**
- **Reproducible:** the stored prompt and contract hashes, plus the source PDF, re-run the same extraction.
- **Out:** a run directory the viewer reads directly from S3. Round 1 uses no Athena and no Glue.
- **Fail:** `STORE_FAILED`.

### Record (Lambda)

The Lambda moves the run record to `succeeded` or `failed`. The record carries:
- the family, the S3 link to the source, and the page references;
- rows per grain and the quarantine count;
- warnings and unknown-model flags;
- the error code and message, if any;
- prompt and contract hashes;
- token counts and the duration of each stage.

### Viewer (app)

Lists runs. For each one it shows the record, opens the source PDF, and shows the extracted numbers per grain, with page references and flags. Read only in round 1.

## 3. Account and resources

Everything is in **roam-ai datascience (640168431387)**. The region is **proposed** as us-east-1, where the account's SageMaker and MLflow resources are. The inference profile id, once recorded, confirms it for Bedrock.

| Resource | Status |
|---|---|
| `universal_catalog` in `tapered_ideal_v2` (719 rows) | exists; **read only** |
| `roam_prd_ddb.default.esp_well_configuration_v2` | exists; the source for the well-list snapshot |
| Bedrock Opus 5.5, cross-region inference profile | available; profile id to record |
| `<intake prefix>`, `<run store prefix>` | new; Step 3 |
| Lambda `<extraction lambda>` and its execution role | new; Step 5 |
| DynamoDB `<run table>` | new, or the product's run-log shape if one fits; Step 5 |
| CloudWatch log group | created with the Lambda |
| App hosting | the tapered calculator's route on `roam-container-platform`, under `apps/enterprise/` |

**Lambda configuration, proposed:**
- 15-minute timeout.
- **Asynchronous retries: 0.** By default, S3's asynchronous invoke retries a failed run twice. Each retry is another Opus call.
- **Idempotent run id,** built from the S3 object key and version. A run that has already finished is skipped, because S3 can deliver an event more than once.
- **Reserved concurrency: 1,** as a guard on cost and throttling. Runs are independent, so it isn't needed for correctness.

**Permissions for round 1** (the actions are listed; ARNs are set when the roles are created):
- **Lambda role:**
  - read the intake prefix and write the run store;
  - read `universal_catalog`'s data;
  - invoke Bedrock, streaming, on the inference profile;
  - put and update on the DynamoDB run table;
  - CloudWatch logs.
- **App task role:**
  - write the intake prefix;
  - read the run store and the run table.
  - With the snapshot in place, it needs no cross-account grant.

**Packaging:** a zip or a container image, decided in Step 5. The PDF stack (`pdfplumber`, `pypdf`) is pure Python. No page renderer is needed, because there is no vision extraction.

## 4. Where errors are reported

There is **no push notification**: no SNS, no email, no alerting. A failure is visible in two places, and only to someone who looks:

- **the run record,** shown in the app's viewer: status, error code, message, the stage that failed, and the source link;
- **CloudWatch logs,** with the full trace, keyed by run id.

| Code | Stage | Meaning | Action |
|---|---|---|---|
| `IMAGE_ONLY` | Detect | No text layer (includes XSize) | Can't be extracted; manual entry in round 2 |
| `UNKNOWN_TEMPLATE` | Detect | No family signature matched | A new template is follow-on work |
| `AMBIGUOUS_TEMPLATE` | Detect | More than one matched | DS fixes the signatures |
| `REQUIRED_PAGES_MISSING` | Detect | Known family, spine incomplete | Check the document |
| `PREP_FAILED` | Prep | Unreadable PDF or unmapped glyphs | DS |
| `MODEL_TIMEOUT` · `MODEL_TRUNCATED` · `MODEL_ERROR` | Extract | No complete report | Re-upload; DS if it repeats |
| `REPORT_INVALID` · `LOAD_HALTED` | Validate and stage | Off-contract, or the identity halt | DS |
| `STORE_FAILED` | Store | Outputs not written | DS |

**The one failure the Lambda can't report itself** is being killed at 15 minutes. **Proposed:**
- At start, the Lambda writes the record as `running`, with a deadline.
- The viewer shows a `running` record past its deadline as **timed out**.
- CloudWatch carries the platform's timeout line.

## 5. Round 1 decisions

**Set by Keith:**
- Streamlit intake against an existing well.
- S3 plus one Lambda: no step function, no API Gateway.
- Opus via Bedrock.
- No vision: image-only documents fail.
- No approval gate and no SNS. Runs store what they wrote, the source link and page references.
- Logging in DynamoDB plus CloudWatch.
- The datascience account first.
- Round 1 is whatever gets extraction running, without matching product storage.
- The calculator does not read round 1's numbers.
- Manual entry, edit-afterwards and the version store belong to round 2.

**Standing from planning:**
- Detection is deterministic.
- XSize, vision, and creating new templates are out of scope.
- **The parity test for the Bedrock port, in Step 2:**
  - one document per vendor family, including the longest;
  - pump configuration identical, and every known-trap field identical;
  - curve observations within a stated count difference, with values identical on matched rows;
  - every difference read, and either recorded as a correction to the old report or counted as a failure.
- No catalog refit: unknown models are flagged.
- The app is separate from the tapered calculator.
- Numbers are compared deterministically, never by an LLM.

**Proposed in this note:**
1. A well-list snapshot stands in for a live cross-account read in round 1.
2. The app writes `submitted` before upload; the Lambda writes `running`, with a deadline, at start.
3. SpyGlass detection drops the `Summit ESP Representative` marker.
4. XSize reports as `IMAGE_ONLY`.
5. Runs are independent, stored under their run id, and read straight from S3. No Athena and no Glue in round 1.
6. Async retries 0, an idempotent run id, and reserved concurrency 1 as a cost guard.
7. A streaming Bedrock call, bounded by a client timeout taken from the Lambda's remaining time.
8. **Fallback:** if the longest document (34 pages, all kept by slicing) runs close to the limit in Step 2, the pipeline becomes two Lambdas chained by S3: extract, then validate and store. That is still no step function, but it is more than the one Lambda specified.

## 6. Verification, round 1

**The test:**
- Upload a known-template document through the app, and see its numbers and its run in the viewer and in the log.
- Upload an unrecognised or image-only document, and see the reported error.

**The documents:**
- **Chord's design documents** exist for the whole organisation. They were uploaded through the ESP app's UI to an S3 location not yet identified; Zebra is a recent one.
- **Chord is live,** so its wells should be in the dropdown. That makes these documents the natural test input.
- **The legacy corpus** comes from an operator that isn't live, so its wells may not be selectable at all.
- **Once the Chord documents are located,** *Detect* runs over them on its own. It is deterministic and makes no model calls.
  - Documents that match a known family serve the known-template test.
  - The rest serve the unrecognised-template test, and count as follow-on template work.
  - If none match, the known-template test needs a legacy-corpus document and a well the form can select.
- **Testing needs only copies of the files,** uploaded through this app.

---

# Round 2 — product

**Status: a target, not a design yet.** Keith set the write shape and noted it as his current thinking. Round 2 is designed in detail once the questions in §9 are answered. That design is added to this note when round 2 starts, in Step 8.

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

- **One upload writes one well.** No whole-corpus rebuild, and no swapping of tables on each PDF.
- **Round 2's app features,** built once against product storage:
  - manual entry, including for image-only documents;
  - edit-afterwards;
  - the version store.
- **Mapping, proposed for Step 8:**
  - pump configuration and design context become well-config attributes;
  - curve observations become the partitioned table.
  - Pump configuration holds several rows per well: tapered sections, and successive design dates. How a multi-section string sits on one well record is part of Step 8.
- **The dropdown** reads the well list live, replacing round 1's snapshot.

## 8. What carries over from round 1

**Unchanged:** *Detect*, *Prep*, *Extract*, *Validate and stage*, the run record, the error codes, and the viewer.

**Changed:** round 2 replaces *Store* with the two product writers, and adds the app features above.

## 9. Open before round 2 is built

1. **Well config.** Which DynamoDB table holds well config? Is it the source behind `esp_well_configuration_v2`, and does it already hold pump-string fields? Writing to it needs the agreement of whoever owns it.
2. **Versions.** Does "every version kept" still hold when an upload overwrites a well's partition?
3. **Reads.** Where do `universal_catalog` and the tapered calculator read from, once the data is in product storage?
4. **Monorepo.** Does round 2 include the move into the ESP monorepo?
5. **Compute.** Which account runs round 2's Lambda?
6. **Overlap with B2.** How round 2 divides with B2, the well-to-pump linking task.

---

## Build steps

| Step | Round | Ends in |
|---|---|---|
| 1 | — | This design note |
| 2 | 1 | Extraction core: detection with its errors; single-document prep; Opus on Bedrock with bounds; parity on one document per family; the longest document timed |
| 3 | 1 | Validate, stage and store by code: the three grains per run, the unknown-model flag, the run store, the run record; run locally |
| 4 | 1 | Streamlit app, local: upload form and run viewer |
| 5 | 1 | Lambda deployed: S3 trigger, run log, failures logged |
| 6 | 1 | App hosted; end to end; both round-1 verification uploads |
| 7 | 1 | Runbook, round 1 |
| 8 | 2 | Round-2 design: the questions in §9 answered, written as an addendum to this note |
| 9 | 2 | Product writers: well config in DynamoDB, curve observations to the well's Athena partition |
| 10 | 2 | Manual entry, edit-afterwards, version store |
| 11 | 2 | Deployed to product end to end; round-2 verification |
| 12 | 2 | Runbook covering both rounds |
