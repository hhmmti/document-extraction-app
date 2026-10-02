# Design-document extraction app — design note

**Task:** B1 — make design-document extraction a pipeline, not a one-off. GitHub issue `roam-datascience-esp #98`.
**Status:** draft, 2026-10-02. Owner: Hamed.

A design document from a known vendor template, uploaded through a Streamlit app, lands in the Athena tables the tapered ideal calculator reads, with no hand steps and with every failure reported. Extraction becomes something that runs whenever a client sends a document, not a corpus built once.

**How to read this note.** *Confirmed* means checked against the running system. *Proposed* is a design choice for Keith to accept or change. Names written as `<placeholders>` do not exist yet and are fixed during the build.

**What is reused.** The extraction method already exists in this repository at `esp_design_extraction_method/`: template triage, page slicing, the per-vendor extraction contracts and prompts, the report validator, and the loader. This app turns that method into a pipeline. The extraction logic does not change.

---

## 1. Shape

```
Streamlit app ──► S3 intake ──(object-created)──► one Lambda ──► report store (versioned)
   ▲  form: org + existing well + PDF                 │                │
   │  manual entry · edit                             │                ▼
   │                                                  │      rebuild 3 grain tables
   │                                                  │      ──► new versioned prefix
   │                                                  │      ──► Glue swap (scratch → tapered_ideal_v2)
   └──── run viewer ◄── DynamoDB run record ◄─────────┘  +  CloudWatch logs
```

One Lambda, three entry kinds, all arriving as S3 objects in the intake area:
- **document** — a PDF from the upload form;
- **manual entry** — a JSON record from the manual-entry form;
- **edit** — an overlay record from the run viewer.

Documents run every stage. Manual entries and edits go straight to *Store → Rebuild → Publish → Record*. **Proposed.** It keeps "S3 plus one Lambda" for every write the app makes, so nothing but the Lambda writes the tables.

---

## 2. Stages — inputs, outputs, failure

Error codes are listed in §4.

### Intake (app)

- **In:** organisation and an existing well from dropdowns; the PDF, or a manual-entry or edit record.
- **Does:** writes the DynamoDB run record with status `submitted`, then writes the object to `<intake prefix>` with the run's metadata: organisation, Roam well id, uploader, entry kind. Writing the record first means a trigger that never fires shows as a run stuck in `submitted`, not as nothing. **Proposed.**
- **Out:** the S3 object; a run record in `submitted`.
- **Dropdown source.** **Proposed:** `roam_prd_ddb.default.esp_well_configuration_v2`, read through Athena in the datascience account. *Confirmed* readable from datascience (174 rows, 2026-09-24). **Not yet confirmed:** that it carries an organisation column and the Roam well id the product uses elsewhere. See §7.

### Detect (Lambda, deterministic)

- **In:** the PDF.
- **Does:**
  1. **Text layer.** Count non-whitespace characters per page. If page 1, or every page, falls under the image-route floor of 20 characters, fail `IMAGE_ONLY`.
  2. **Signature.** Match the page-1 markers of each vendor family. Exactly one family must match. None → `UNKNOWN_TEMPLATE`; more than one → `AMBIGUOUS_TEMPLATE`.
  3. **Required pages.** Every page role the family's contract marks `required` must be found somewhere in the document. Otherwise `REQUIRED_PAGES_MISSING`, naming the missing roles.
- **Out:** the family and the page roles, or an error. No LLM.
- **Two changes from the existing triage code, both proposed:**
  - **F1 (SpyGlass) detection drops one marker.** The `Summit ESP Representative` marker sits in a disclaimer that five real SpyGlass documents don't print. The existing code assigned families from a fixed file list, and the marker only confirmed it; the pipeline has no such list. So detection relies on `SIZING REPORT` plus the required-page check instead. Step 2 checks that every SpyGlass document in the corpus detects as F1 and nothing else does.
  - **XSize reports as image-only.** XSize documents have no text layer and no page-1 signature, and their PDF producer string is shared with another vendor, so they cannot be told apart from any other scanned PDF. They fail as `IMAGE_ONLY`, and the error points ops to manual entry.

### Prep (Lambda)

- **In:** the PDF and its family.
- **Does:** the single-document form of the existing slicing step. It slices to the family's pages, dropping only pages the contract marks plot-only; extracts layout text from each kept page; translates the private-use glyphs the corpus fonts emit; and writes manifest rows. No fixed file list, no image rendering, no corpus-wide state. **Proposed.**
- **Out:** per-page text keyed by original page number, and the page manifest. These page numbers become the run's page references.
- **Fail:** `PREP_FAILED` — an unreadable PDF, or private-use glyphs left unmapped.

### Extract (Lambda → Bedrock, Opus)

- **In:** the page text, the manifest, the family's prompt and contract, the validator spec, the staging schemas, and the closed field list.
- **Does:** assembles the prompt **in the same order** the existing batch runners do, so the parity test in Step 2 measures the model change and nothing else. Calls Opus on Bedrock.
  - **Proposed:** a streaming call, bounded by `max_tokens` and by a client read timeout. The timeout is set from the Lambda's remaining time, minus a fixed reserve for the stages after it.
  - The prompt also tells the model to finish within 12–13 minutes. That instruction is not a bound, because the model has no clock; the timeout is the bound.
- **Placeholders.** The batch runs sent the well id as null. Production fills it with the Roam well id from the form. The parity run keeps null, so old and new reports compare like for like.
- **Out:** the report (YAML frontmatter through to the `row_counts` footer), the stop reason, token counts, and hashes of the prompt and contract files used.
- **Fail:**
  - `MODEL_TIMEOUT` — the client timeout fired.
  - `MODEL_TRUNCATED` — the stop reason is `max_tokens`, or the `row_counts` footer is missing. This is the batch runners' existing completeness test.
  - `MODEL_ERROR` — access, throttling or a service error.
- **Model:** Opus 5.5 on Bedrock through a cross-region inference profile. *Confirmed* available in the datascience account (2026-10-02). The profile id is to be recorded.

### Validate and stage (Lambda)

- **In:** the report.
- **Does:** the existing loader's report reader and staging transform, with the logic unchanged.
  - Bad values are quarantined, never coerced.
  - The loader's two fail-loud rules stay:
    - a design-condition anomaly (a bubble point that tracks the static datum) is recorded on the run as a warning;
    - contradictory pump-string identity across a document's scenarios halts the load.
- **Out:** staged rows, the quarantine list, warnings.
- **Fail:** `REPORT_INVALID` (unparseable or off-schema); `LOAD_HALTED` (the identity halt).
- **Proposed:** a failed version is still stored but never becomes current. The well's previous version stays current, so a bad upload cannot erase a good one.

### Store (Lambda)

- **In:** the source document link, the report, the manifest, the staged rows, the run outputs.
- **Does:** writes an immutable version under `<report store>/<well>/<version>/`. The version id is the run id. "Current" for a well is its latest **successful** version: the latest upload wins, and "successful" is the proposed refinement.
- **Seed.** The 46 existing reports are loaded once as legacy versions, keyed by document name, with no Roam well id. See §7.
- **Out:** a version record. This is what the run viewer shows as "what the run wrote".

### Rebuild (Lambda)

- **In:** every well's current version, every manual-entry version, the edit overlay, and `universal_catalog`, which is read only.
- **Does:**
  1. Rebuilds the three per-well tables — `pump_config`, `esp_well_design_context`, `curve_observations` — from scratch with the existing loader's logic: alias resolution before collapsing a pump string, one row per string, and the latest design date wins.
  2. Applies the edit overlay. An edit binds to (well, table, natural row key, field). An edit whose key no longer exists in the rebuilt table is reported on the run as **orphaned** — never dropped silently, and never applied to a guessed row. **Proposed.**
  3. Flags every pump model in the run's well that is absent from `universal_catalog`. The flag goes on the run record, and the well lands blocked in the calculator. Nothing is written to the catalog.
  4. Writes Parquet to a **new versioned prefix** under the existing table root.
- **Out:** the three tables' Parquet at `<table root>/<build id>/`, row counts, flags.
- **Fail:** `REBUILD_FAILED`.
- **Parity anchor.** A seeded rebuild with no new uploads must reproduce today's tables: 76 / 42 / 1,834 rows. The calculator must read it unchanged.
- **To check in Step 3:** whether the unknown-model flag against `universal_catalog` reproduces the existing list of blocked wells (27 of 42). That list predates the catalog, so the two may not be the same test.

### Publish (Lambda)

- **In:** the new prefix.
- **Does:** repoints each table's location to the new prefix through the Glue API — first in `<scratch database>`, then in `tapered_ideal_v2` once Step 3's parity passes. Old prefixes are kept, so rollback is a repoint. **Proposed.**
- **Atomicity, stated plainly.** Each table's swap is atomic; the three together are not. They run back to back, so a reader can briefly see mixed builds. That is acceptable for a calculator ops refresh by hand, and it is written here so nobody assumes otherwise.
- **Fail:** `PUBLISH_FAILED`. The tables stay on the previous build.

### Record (Lambda)

- The run record moves to `succeeded` or `failed`. It carries the entry kind, family, version id, S3 link to the source, page references, rows written per table, quarantine count, warnings, unknown-model flags, orphaned edits, error code and message, prompt and contract hashes, token counts, and the duration of each stage.

---

## 3. Account and resources

Everything is in **roam-ai datascience (640168431387)**. Region: **proposed** us-east-1, where the account's SageMaker and MLflow resources are. The inference profile id, once recorded, confirms it for Bedrock.

| Resource | Status |
|---|---|
| Glue database `tapered_ideal_v2` (`AwsDataCatalog`), four tables | exists |
| `s3://roam-ds-assets/ideal-pump-library/V2-tapered/`, one prefix per table | exists |
| `universal_catalog` (719 rows) | exists; **read only** |
| Athena results bucket `esp-athena-results-v2-640168431387` | exists, shared with CurVE; used by the app only |
| `roam_prd_ddb.default.esp_well_configuration_v2` | exists; proposed dropdown source |
| Bedrock Opus 5.5, cross-region inference profile | available; profile id to record |
| `<intake prefix>`, `<report store prefix>` | new; placed in Step 3 |
| `<scratch database>` | new; Step 3 |
| Lambda `<extraction lambda>` and its execution role | new; Step 5 |
| DynamoDB `<run table>` | new, or the product's existing run-log table if one fits; Step 5 |
| CloudWatch log group | created with the Lambda |
| App hosting | the tapered calculator's route on `roam-container-platform`, under `apps/enterprise/` |

**Confirmed for the data scientist role (2026-10-02):** Opus 5.5 is invokable; S3 writes under the table root work; Glue create and drop database work. Steps 2 and 3 can therefore run locally before any new role exists.

**Lambda configuration, proposed:**
- 15-minute timeout.
- **Asynchronous retries 0.** S3 invokes Lambda asynchronously, and by default a failed run is retried twice — two more Opus calls and two more versions.
- **Reserved concurrency 1**, so two uploads cannot race the rebuild and swap; the second waits.
- **An idempotent run id**, built from the S3 object key and version. A run already in a terminal state is skipped, because S3 can deliver an event more than once.

**Permissions the new roles need** (actions; the ARNs are set when the roles are created):
- **Lambda role:**
  - read the intake prefix;
  - read and write the report store;
  - write the table root;
  - Glue get, create and update table, on the scratch database and `tapered_ideal_v2`;
  - Bedrock invoke, streaming, on the Opus inference profile;
  - DynamoDB put and update on the run table;
  - CloudWatch logs.
- **App task role:**
  - write the intake prefix;
  - read the report store and the run table;
  - Athena read on `tapered_ideal_v2` and on the dropdown source. That read is federated through a connector in the products prd account, so it is a **cross-account** grant.

**Packaging:** a zip or a container image, decided in Step 5. The PDF stack (`pdfplumber`, `pypdf`) is pure Python. The page renderer is not needed, because there is no vision extraction.

---

## 4. Where errors are reported

There is **no push notification**: no SNS, no email, no alerting. A failure is visible in two places, and only to someone who looks:

1. **The run record in DynamoDB**, shown in the app's run viewer: status, error code, message, the stage that failed, and the link to the source. This is where ops and the uploader see it.
2. **CloudWatch logs** for the Lambda: the full trace, keyed by run id.

| Code | Stage | Meaning | Ops action |
|---|---|---|---|
| `IMAGE_ONLY` | Detect | No text layer (includes XSize) | Manual entry |
| `UNKNOWN_TEMPLATE` | Detect | No family signature matched | Manual entry; a new template is follow-on work |
| `AMBIGUOUS_TEMPLATE` | Detect | More than one matched | DS fixes the signatures |
| `REQUIRED_PAGES_MISSING` | Detect | Known family, spine incomplete | Check the document; manual entry |
| `PREP_FAILED` | Prep | Unreadable PDF or unmapped glyphs | DS |
| `MODEL_TIMEOUT` · `MODEL_TRUNCATED` · `MODEL_ERROR` | Extract | No complete report from Bedrock | Re-upload; DS if it repeats |
| `REPORT_INVALID` · `LOAD_HALTED` | Validate and stage | Report off-contract, or the identity halt | DS |
| `REBUILD_FAILED` · `PUBLISH_FAILED` | Rebuild, Publish | Tables unchanged | DS |

**The one failure the Lambda cannot report itself** is being killed at 15 minutes. **Proposed:** the Lambda writes the record as `running`, with a deadline, when it starts. The viewer shows a `running` record past its deadline as **timed out**, and CloudWatch carries the platform's timeout line. The client timeout in *Extract* should make this rare, but it is the silent case, so it is the one that needs handling.

---

## 5. Decisions

**Set by Keith, 2026-09-30:**
- Intake is a Streamlit form with an existing well. A re-upload overrides the previous extraction, and every version is kept.
- S3 plus one Lambda; no step function, no API Gateway.
- Opus via Bedrock.
- No vision: an image-only PDF fails, and the app has a manual-entry form.
- No approval gate and no SNS. Runs write by default and store what they wrote, the source link and page references.
- Logging in DynamoDB plus CloudWatch.
- Built in the datascience account first.

**Standing from planning:**
- Detection is deterministic.
- XSize, vision and new-template creation are out of scope.
- The parity test for the Bedrock port, in Step 2:
  - one document per vendor family, including the longest;
  - pump configuration identical, and every known-trap field identical;
  - curve observations within a stated count difference, with values identical on matched rows;
  - every difference read, and either recorded as a correction to the old report or counted as a failure.
- The tables are rebuilt from stored reports, with a versioned-prefix swap, scratch database first.
- No catalog refit: unknown models are flagged, and the well is blocked.
- Edits are an overlay, reapplied on every rebuild.
- The app is separate from the tapered calculator.
- Numbers are compared deterministically, never by an LLM.

**New in this note, proposed:**
1. Manual entries and edits enter through the same S3 trigger as documents.
2. The app writes the run record before upload (`submitted`); the Lambda writes `running`, with a deadline, at start.
3. SpyGlass detection drops the `Summit ESP Representative` marker.
4. XSize reports as `IMAGE_ONLY`.
5. A failed version is stored but never current; "current" is the latest successful upload.
6. Orphaned edits are reported, not dropped.
7. Async retries 0, reserved concurrency 1, an idempotent run id.
8. The swap goes through the Glue API, per table, with old prefixes kept for rollback.
9. A streaming Bedrock call, bounded by a client timeout taken from the Lambda's remaining time.

---

## 6. Deferred

- Vision extraction, XSize, and new vendor templates — follow-ons.
- Writing new pump models to the catalog — that goes through the catalog's change process (A2).
- Linking the 42 legacy wells to Roam well ids, and well/pump configuration — B2.
- A superadmin-gated endpoint on the ESP FastAPI — only if an API is needed.
- The move to a product stack.
- Two Lambdas chained by S3 — only if Step 2's timing calls for it (§7, item 6).
- Any failure notification beyond the app and CloudWatch.

---

## 7. Open decisions

1. **Sign-off of this note** before the build proceeds past it.
2. **The well-id column — a schema change.** New rows carry the Roam well id from the dropdown; the 42 existing rows carry printed well names only. **Proposed:** one nullable Roam-well-id column on the three per-well tables, alongside the printed name. The calculator's contract is otherwise unchanged, and Step 3 checks that the calculator ignores the added column.
3. **Legacy wells and "override".** The 42 legacy rows have no Roam well id, so a re-upload for one of those wells would **not** replace its legacy row: the well would appear twice until B2 links them.
   - Options: accept that until B2; let the upload form optionally name the legacy report it supersedes; or map the 42 by hand once. **Proposed:** the second, because it is small and keeps "re-upload overrides" true.
   - **This may be moot.** The legacy corpus belongs to an operator that is not live on the product. If its wells are not in the product's well configuration, they can never be picked in the dropdown, and no re-upload can touch them. **The check:** look for those wells, or that organisation, in the dropdown source.
4. **The dropdown source.** **Proposed:** the prd well-configuration table above, because those are the real wells. It needs confirmation that it carries organisation and the canonical well id, and the app role's cross-account read.
5. **Access still to grant:**
   - the Lambda execution role (§3), including Bedrock invoke on the inference profile;
   - the app's task role and hosting access, including the cross-account dropdown read.
6. **The fallback.** If the longest document (34 pages, all kept by slicing) times close to the limit in Step 2, the pipeline becomes two Lambdas chained by S3: extract, then load. That is still no step function, but it is more than the one Lambda specified.

The product may already have a DynamoDB run-log shape for its pipelines. If so, the run table follows it. If not, it is a datascience-local table, aligned with the product's later.

---

## 8. Verification

**The test:** upload one known-template document through the app, then find its rows in the tables and its run in the log. Upload one unrecognised or image-only document, and see the reported error.

**The documents.**
- Chord's design documents exist for the whole organisation. They were uploaded through the ESP app's UI into an S3 location not yet identified; Zebra is a recent one.
- Chord is live, so its wells should be in the dropdown. That makes them the natural test input.
- The legacy corpus belongs to an operator that is not live, so its wells may not be selectable at all (§7, item 3).

**Whether Detect recognises them is not yet known.** The vendor families were built from one operator's documents.
- Once the Chord documents are located, *Detect* runs over them on its own. It is deterministic, makes no model calls and costs nothing.
- Documents that detect as a known family serve the known-template test.
- The rest serve the unrecognised-template test, and are counted as follow-on template work.
- If none detect, the known-template test needs a legacy-corpus document and a well the form can select.

**Where they live.** They were uploaded through the ESP app, so most likely in a products account. Testing needs only copies of the files, uploaded through this app; the pipeline needs no standing read on that bucket.

---

## Build steps

| Step | Ends in | Covers |
|---|---|---|
| 1 | This design note | Design |
| 2 | Extraction core: detection with its errors; single-document prep; Opus on Bedrock with bounds; parity on one document per family; the longest document timed | Detection, Bedrock |
| 3 | Load and publish by code: versioned report store seeded with the 46 reports; edit overlay; unknown-model flag; run record; DDL and swap to a scratch database; calculator parity | Athena load, unknown models |
| 4 | Streamlit app, local: upload form, run viewer with edit, manual entry | App |
| 5 | Lambda deployed: S3 trigger, run log, failures logged | Deployment, backend |
| 6 | App hosted, end to end, both verification uploads | Deployment, end to end |
| 7 | Runbook | Runbook |
