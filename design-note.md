# Design-document extraction app — design note

**Task:** B1, make design-document extraction a pipeline rather than a one-off. GitHub issue `roam-datascience-esp #98`.
**Status:** approved by Keith; restructured into two rounds on 2026-10-05; updated at the close of Step 2 (2026-10-06), Steps 3 and 4 (2026-10-13), Steps 4.5 to 4.7 (2026-10-15) and Step 5 (2026-10-19). Owner: Hamed.

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

**Built in Step 5.** The S3 trigger and the Lambda replace the local process, and S3 replaces the local folder. Two details differ from the drawing:
- **The viewer reads S3 only:** the run store, plus the intake prefix for uploads not yet picked up. It does not read the run log.
- **The DynamoDB run log is for operations,** in the same layout as the product's own pipeline log (*Record*).

The app still runs locally in round 1. Everything is built and tested offline; deployment is Step 6.

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

**Built in Step 5: S3 mode.** Tested offline against a stand-in for S3; it is first used against the real bucket in Step 6.
- **A mode switch,** `ESP_APP_MODE`: `local` (the default, unchanged) or `s3`. S3 mode takes the bucket and prefixes from the same variables as the Lambda, and the user's own AWS credentials.
- **The app is not hosted in round 1.** It runs on the analyst's machine against the datascience account, and hosting moves to round 2. "The hosted app" in the bullets above is therefore round 2.
- **Upload:** one PUT to `s3://roam-ds-assets/esp-design-extraction/intake/<run_id>.pdf`, with content type `application/pdf` and a create-only condition, so nothing is overwritten. The app creates the run id, as it did locally.
- **Labels** travel as object metadata, each value percent-encoded UTF-8: `organization-id`, `well-id`, `source-name` and `sha256`. The app and the Lambda share one encoding module, and the form refuses any label set the Lambda would refuse.
- **The pre-checks stay:** an empty or non-PDF file is refused; the cost line and confirm box gate the upload; the live-run warning now reads the run records and the uploads still waiting.
- **The `submitted` record proposed above is dropped.** A waiting upload is derived instead: an intake object with no run record yet (states under *Viewer*).

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

**Changed in Step 4.6 (2026-10-15): tolerant detection for new versions of known vendors.**
- **ChampionX (F2)** is detected by its report title alone. The representative line, which a newer generator version prints differently, is no longer a marker.
- **SLB (F3)** is detected by `SLB Engineer` plus its `SLB | … Report` footer, so an SLB report missing its Schematics page is still recognised as SLB.
- **Identity is still confirmed by the required-page check.** A marker may now be a regular expression, matched within one line.
- **An unrecognised document's error names its near misses** (family, markers matched, required pages found) and the PDF producer.
- **Confirmed:**
  - every corpus document detects exactly as before, with no ambiguous match;
  - the corpus suite passes, the prompt byte check included;
  - the newer ChampionX document from the product's upload bucket now detects as ChampionX;
  - the two partial SLB documents detect as SLB, with the Schematics page missing. From Step 4.7 they go to the generic path.
- **Known gaps, carried as future issues:** for some families, detection's required pages and the contract's must-have pages don't line up exactly. For example, the ChampionX main-pump block isn't checked at detection, and a `Motor` title can match a different page. A document that passes detection but that the model then refuses now fails visibly as `EXTRACTION_EMPTY` (see *Validate and stage*).

**Added in Step 4.7 (2026-10-15): routing.** Detection now chooses a route instead of only accepting or failing:

| Detection finds | Route |
|---|---|
| a family, with all its required pages | the family's template |
| a family, with required pages missing | generic, recording the family as a hint and the missing pages |
| no family, with a text layer | generic, recording the near misses |
| no text layer, two families at once, or an unreadable file | the run fails, as before |

An unrecognised template or a missing page no longer ends a run. A command-line flag, `--force-generic`, sends a recognised document down the generic route for measurement only; the app never uses it.

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

**Added in Step 4.7: generic prep.** With no page map, every page that has text is kept. Pages under the 20-character floor are dropped without failing the run. An unmapped glyph still fails `PREP_FAILED`.

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

**Changed in Step 4.6 (2026-10-15): four families now carry an amendment, and ChampionX's adds rules.** The statement above that each clarification only makes an existing requirement explicit still holds for SLB, Baker and Valiant. **It does not hold for ChampionX.**

ChampionX's amendment is a contract revision for the newer generator version:
- the representative block may print as `Account Manager | Customer Rep`, with the vendor named only in the logo;
- **the page count is not fixed:** missing final plot pages get a note, not a halt, and blocks are read by their titles on whatever page they print;
- **a charge pump** (a second pump model below the main pump, where a gas handler would otherwise be) is in scope, as `section_role = taper`;
- a "GAS HANDLING" equipment string names the separator, not a gas-handler pump.

**The original contract files are unchanged,** so the prompt byte check is unaffected. A regression run on an older ChampionX document is planned in Step 4.7.

**Added in Step 4.7: the generic contract.** A new contract for documents without a usable template. It uses the same model, effort and time budget as the template route, and it was approved before any paid run.
- **Output:** the same five data blocks, with the vendor family recorded as `generic`. The family hint and missing pages, when present, go into the prompt as one line of context.
- **Rules:**
  - every value verbatim, with its printed unit and a page reference;
  - a value that isn't printed is left out, never guessed or defaulted;
  - nothing is read off a plot;
  - conflicting values are both kept;
  - one row per distinct pump model, with section 1 the deepest;
  - the traps the vendor contracts share (abbreviated plot labels, nameplate versus operating values, measured versus vertical depth, design point versus curve point) are restated.
- **The only halt:** the document holds no ESP design or installation content at all.
- **Size cap:** a generic prompt over 322,766 bytes (the size of the largest corpus template prompt) fails as `GENERIC_TOO_LARGE`, before any model call. To fit the largest corpus document, the field list in the generic prompt is cut to its name columns.

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

**Added in Step 4.6 (2026-10-15): empty-extraction guards.** The first run on the newer ChampionX document halted under its contract, wrote every data block empty, and was recorded as succeeded with the document checks passed. Three guards close that gap:
- **No pump rows and no design-context rows:** the run fails with `EXTRACTION_EMPTY`. The report, findings and review are stored, and the message names the halt rule the model declared, if any.
- **No pump rows, but other rows present:** the run succeeds, with the document status at least `needs_review` and a review item "no pump rows extracted".
- **The model declared a halt, but rows were still extracted:** the document status is at least `needs_review`, with a review item naming the rule.

The guards set a minimum status; they carry no validator-specification severity. Halt detection is a heuristic, because the prompts fix no wording for a halt. The no-pump-rows notice now names its real reason (unmatched well, empty extraction, no pump rows, or a model halt), where it previously always said "unmatched well".

**Added in Step 4.7: validation on the generic route.**
- **Only the specification's general rules run,** with the stage's own checks and the empty-extraction guards. Vendor-specific rules are skipped.
- **Model names resolve by exact match only:** to a catalogue name, or to a seed alias every vendor agrees on. Anything else is flagged as an unknown model.
- **Every in-scope value goes on the review list** as needing review, and the document status is at least `needs_review`.
- **A notice on the run:** "Extracted without a vendor template: every value is unverified."

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

**Built in Step 5: written to S3 by the Lambda.**
- The pipeline runs into the Lambda's temporary storage. The Lambda then uploads `runs/<run_id>/`, every other file first and `run.json` last.
- `run.json` and `source.json` hold `s3://` locations, never temporary paths, and the source is the intake object.
- **Data assets** are downloaded by name from `esp-design-extraction/assets/`, and the hash checks stay the gate (`ASSET_INVALID`). They are uploaded before the first deployed run.
- The locations above are no longer only proposed: they are what the stack's permissions allow.

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

**Added in Step 4.7:**
- **New fields:** the route; the family hint and missing pages; the near misses; whether the generic route was forced; and `staged`, the rows per table before loading.
- **A run with no well** has staged rows but no loaded rows. The command line prints both.

**Built in Step 5.**
- **`run.json` is the duplicate guard.** At start, the Lambda creates `runs/<run_id>/run.json` as `running`, with a create-only condition and a `deadline_at` set to the invocation's start plus its remaining time. If the file already exists, the event is a duplicate (S3 can deliver one event more than once): it is logged, and nothing else is written. This replaces the id from the object key and version proposed in §3.
- **A failure before the pipeline starts,** such as an invalid label or a hash mismatch, leaves the Lambda's own smaller record: status, code, stage, message and source. The viewer renders it.
- **The run log** is a DynamoDB table in datascience, `esp_design_extraction_logging`. It copies the layout of the product's pipeline log, `esp_logging_v2`, and follows the product's file-upload pipeline, the product pipeline closest to this one:
  - **one row per run,** of type `PIPELINE_EXECUTION`: written as `RUNNING` once the labels are read, then updated in place to `SUCCEEDED` or `FAILED`, with `success` and `end_timestamp`;
  - a failure before that point writes one `FAILED` row; a duplicate writes none; a failed log write never fails the run;
  - **keys:** `organization_id` and `timestamp`, in the product's timestamp format (UTC, microseconds, no offset). The table carries the product's four indexes: by `uuid`, by pipeline execution id, by organisation and type, and by status;
  - `pipeline_execution_id` and `uuid` are the run id, or `ADHOC#<request id>` when the upload's name holds no run id. The organisation is `_unknown` when its label is missing;
  - the row also holds `well_ids`, `resource_name` (the function's name) and `data_json`: the request id, bucket and key, the run's location, deadline and file name, then the outcome;
  - **the raw S3 event is not stored.** The table keeps rows indefinitely, as the product's does, and the event carries the uploader's identity and IP address.
- **The run record and the run log use different status words:** `running`, `succeeded` and `failed` in `run.json`, and the product's `RUNNING`, `SUCCEEDED` and `FAILED` in the log.

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

**Changed in Step 4.7: status wording.** Engineers may read "run succeeded" next to "document checks failed" as a contradiction, so the wording was changed:
- **The list and the run header** now read "**Pipeline: completed**" (or "failed at <stage>") and "**Document review: N items to check**".
- **The run page carries the banner:** *"Pipeline says whether extraction ran. Document review says whether the values need an engineer's check. A completed run can still need review."*
- **Generic runs carry a "Generic extraction: review every value" badge,** with the family hint when there is one.
- **The run list** can be filtered by route.

**Built in Step 5: the S3 backend,** behind the same interface.
- **States:**
  - *queued:* an upload named `<run_id>.pdf` with no run record yet, showing how long it has waited;
  - *not picked up:* queued for more than 5 minutes while no run is running. The hint names the run log and the log group to check;
  - *expired:* queued for more than 6 hours, Lambda's default limit for an event waiting to run. This overrides the two above;
  - *running* and *stale:* a `running` record, before or after its deadline;
  - *final:* `succeeded` or `failed`, on the existing detail page.
  - Uploads with any other name are ignored.
- **Whether a file exists comes from the bucket listing,** never from a failed read. The first load lists every run and reads each record once, which suits hundreds of runs.
- **Reads are cached** until the user presses Refresh. Finished runs, which never change, stay cached even then.
- **The source PDF** is read only from the intake prefix, and shown only when its SHA-256 matches the record.
- **A `running` record names no organisation or well yet,** because the Lambda writes it before reading the labels. For those runs, the viewer uses the upload's labels.

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

**Built in Step 5,** as an AWS CDK app in `esp_design_extraction/cdk/`. Tested offline; nothing is deployed until Step 6.

| Resource | As built |
|---|---|
| Lambda `esp-design-extraction` | a container image for linux/amd64, on the Lambda Python 3.12 base image. Timeout 900 s, memory 2048 MB, reserved concurrency 1, asynchronous retries 0 |
| Its execution role | explicit, with one inline policy (below) |
| Trigger | object created in `roam-ds-assets` under `esp-design-extraction/intake/`, with suffix `.pdf`. CDK sets it through its own helper Lambda, which keeps any existing notifications. The Lambda writes only under `runs/`, so it cannot trigger itself |
| Run log | DynamoDB `esp_design_extraction_logging` (*Record*): on demand, point-in-time recovery, retained if the stack is deleted, no expiry |
| Log group | `/aws/lambda/esp-design-extraction`, 90-day retention, retained |
| Intake, run store, assets | `s3://roam-ds-assets/esp-design-extraction/intake/`, `…/runs/` and `…/assets/` |

- **The role allows only:**
  - reading `intake/` and `assets/`, and writing `runs/`;
  - invoking the `us.` inference profile, plain and streaming, and the model in each of the profile's three US regions, but only through that profile, as AWS documents for geographic profiles;
  - putting and updating rows in the run log;
  - writing to its own log group.
- **No other grant is needed.** The bucket uses S3-managed encryption (confirmed), so there is no KMS grant. The catalogue is read from the snapshot asset, so there is no Athena or Glue grant.
- **Packaging:** a zip came to 319 MB unpacked, over Lambda's 250 MB limit, so the Lambda is a container image. Its pinned libraries are installed into the function directory, which Lambda searches first. The image is 325 MB compressed.
- **The model's budget** is the remaining time minus 90 s, capped at 780 s.
- **The app is not hosted in round 1.** It runs locally, with permission to list and read `intake/` and `runs/` and to write `intake/` (listing confirmed). The hosting row above, the app task role and the run-table read are round 2.
- **Deploy:** only through a GitHub Actions workflow, never from a laptop. It follows the pattern of the ESP monorepo's datascience deploys:
  - it is started by hand, behind a confirm box;
  - credentials come from the repository's datascience secrets;
  - it stops unless the account is 640168431387 and the region us-east-1;
  - a pinned CDK CLI builds the image on the runner, then deploys.
  - The CDK app reads the account and region from the environment, like the monorepo's apps. The account's CDK bootstrap bucket and image repository are confirmed to exist.

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

**Added in Step 4.6:** `EXTRACTION_EMPTY` (*Validate and stage*). No pump-configuration and no design-context rows were extracted, typically because the model halted under its contract. **Action:** DS checks the contract against the document's layout; a new version of a known template usually needs a contract revision.

**Changed in Step 4.7:**
- **New code `GENERIC_TOO_LARGE`,** raised before *Extract*: the generic prompt is over the size cap. **Action:** DS; the document needs a template, or the cap needs revisiting.
- **`UNKNOWN_TEMPLATE` and `REQUIRED_PAGES_MISSING` no longer fail a run.** They send it to the generic route.

**Added in Step 5:** two codes, raised by the Lambda before the pipeline runs.

| Code | Stage | Meaning | Action |
|---|---|---|---|
| `INTAKE_INVALID` | Lambda, before *Detect* | The upload isn't named `<run_id>.pdf`, a label is missing or doesn't decode, or the PDF's SHA-256 doesn't match its label | Upload again through the app |
| `INTERNAL_ERROR` | Lambda | Anything unexpected; the trace goes to CloudWatch | DS |

**Where a failure shows now depends on how far the run got:**
- **after the run record exists:** the failed record in the viewer, a `FAILED` row in the run log, and CloudWatch;
- **before it** (a badly named upload, or a run record that couldn't be created): only the run-log row and CloudWatch. The viewer shows the second case as *not picked up*, and its hint names both places;
- **killed at 15 minutes:** the record reads as stale in the viewer, as built in Step 3. The run-log row stays `RUNNING`;
- **never invoked:** *not picked up*, then *expired* after 6 hours.

**Still no alerting.** The run log can be queried by status, but nothing watches it.

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

**Settled in Step 4.6:**
1. **Tolerant signatures for ChampionX and SLB,** with identity confirmed by required pages.
2. **Contract revisions** live in the family's amendment file, and the original contract stays byte-pinned.
3. **A charge pump maps to `section_role = taper`.**
4. **An empty extraction fails.** A partial or halted one is at least `needs_review`.

**Planned for Step 4.7** (generic path): a document with text but no recognised family, or a known family with a required page missing, goes to a generic extraction. It is marked `needs_review`, with every value on the review list. Image-only documents still fail.

**Settled in Step 4.7:**
1. **The generic route,** as planned above.
2. **The size cap** is set in prompt bytes.
3. **Generic output is never trusted by default.**
4. **Generic contract v1 is accepted as is,** without tuning it to the test documents. Its measured misses are carried as future work, together with a proposal to promote recurring layouts to their own templates.
5. **The status wording** shown in the viewer.

**Settled in Step 5:**
1. **Deploys go only through GitHub Actions,** in the pattern the ESP monorepo uses (Keith). The workflow reaches `main` through a small PR of its own, ahead of round 1's code PR.
2. **Round 1 is not hosted:** the app runs locally against the datascience account (confirmed with Keith). Hosting is round 2.
3. **Intake:** `intake/<run_id>.pdf`, named by the app, with the labels as object metadata.
4. **The viewer reads S3 only.** A waiting upload is an intake object with no run record, and it expires after 6 hours.
5. **The duplicate guard is a create-only `run.json`.**
6. **The run log follows the product's file-upload pipeline,** in the product's logging layout. Moving into the product's own table later is then a configuration change, plus the points in §9.
7. **The Lambda is a container image.**
8. **Of Step 2's decision 5,** the `submitted` record is dropped (decision 4 above); the rest stands.

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

#### Step 4.5: real uploads surveyed

Detection alone was run over every design PDF in the product's production upload bucket: 6 documents. None matched a known template as it stood:
- 1 newer ChampionX version;
- 2 partial SLB reports sharing one layout;
- 2 documents from no known vendor (an installation report and a printed schematic);
- 1 scanned document with no text layer.

The same survey over the corpus reproduced its known results exactly.

#### Step 4.6: tier 1

- **Detection:** the newer ChampionX document now detects, and the corpus is unchanged.
- **First paid run:** it halted under the old contract's fixed page count and was wrongly shown as a success. That led to the contract revision and the empty-extraction guards.
- **Second paid run:** it succeeded. The charge pump and the main pump, with their stage counts and the design context, were checked against the PDF. The document status was `needs_review`, from one flow value without its unit and one model name not yet in the alias seed list.
- **Replay:** the first run's stored report now fails as `EXTRACTION_EMPTY`.

#### Step 4.7: tier 2, the generic route

- **The yardstick.** The six parity documents (one per vendor family) were sent down the generic route and compared, value by value, with their template extraction.
  - **On rows that pair, the two agree 72% of the time.** The raw figure is 51%. Most of the gap is structural: the generic contract groups pump sections by model, misses a scenario key on one document, and makes a few deliberate vocabulary choices.
  - **35 values are genuinely missed or wrong,** mostly setting and intake depths, motor power in kW, and a few model strings and per-housing stage counts.
  - **Design context** (fluid properties, motor nameplate, design head, water cut) is mostly right.
  - **Reading:** a usable fallback whose pump string and depths always need an engineer's check, which is how it is flagged.
  - **Cost:** USD 4.66 for the six.
- **ChampionX regression.** The revised ChampionX contract, run on an older 9-page ChampionX document, gives design context identical to the earlier extraction, and pump rows agreeing at 94%, with the same two findings as before.
- **Production documents.** The four production documents that reach the generic route all succeeded with pump rows, the two partial SLB reports included. They were spot-checked against their PDFs.
  - **Section order:** both partial SLB runs carry a section-order finding. That sets the publish block, so their pump strings cannot reach a well record without review.
  - **Cost:** USD 2.17.
- **Corpus suite:** still passes, the prompt byte check included.

### Step 5: built and tested offline

Everything in Step 5 ran against stand-ins for S3 and DynamoDB; nothing has run in AWS yet.
- **The Lambda:**
  - a duplicate event stops;
  - a badly named upload or a hash mismatch fails as `INTAKE_INVALID`;
  - a failure after the run record exists leaves a failed record and a `FAILED` log row, and an earlier one leaves the log row;
  - a success uploads the run with `run.json` last and leaves one `SUCCEEDED` row;
  - no stored file holds a temporary path.
- **The run log:** the stand-in table refuses what DynamoDB would, such as an unescaped reserved word or a key attribute in an update. A failed first write still ends with a complete row.
- **The app's S3 mode:** every state, from set waiting times; the upload's key, type and labels, accepted by the Lambda's own check; and the viewer reading both kinds of failure record.
- **The image** builds, and the pinned AWS libraries load from the function directory.
- **The CDK app** synthesises offline, and its template tests pass. Reading the account and region from the environment leaves the template unchanged.
- **Earlier suites are unchanged,** and no client identifier was added to the tracked tree.
- **Left for Step 6:** paging, the create-only upload and the user's credentials against the real bucket; the deployed image loading the handler; the workflow's credentials publishing the image; the trigger firing; and one log row per run.
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

**Updated in Step 4.6:** the detection survey proposed above was run in Step 4.5. The product's production bucket holds only 6 design documents, and only one of them is a Chord document. Step 6 may use any production document that has a text layer.

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

**Added in Step 5, for moving the run log into the product's own table:**
8. **Missing organisation.** The product files log rows with no organisation under `super_admin`; round 1 uses `_unknown`.
9. **Second log row.** The product's file-upload Lambda also writes a `LOG` row on every invocation, which the health dashboard counts. Should this pipeline write one too? If it does, it leaves out the event and any token.
10. **Which pipeline wrote a row.** The product marks it with a prefix on the execution id (`FILE_UPLOAD#`, `ADHOC#`). This pipeline's ids have none; a prefix would be agreed with the product team.

## Build steps

| Step | Round | Ends in | Status |
|---|---|---|---|
| 1 | — | This design note | done |
| 2 | 1 | Extraction core: detection with its errors; single-document prep; Opus on Bedrock with bounds; parity on one document per family; the longest document timed | **done 2026-10-06** |
| 2.5 | — | Well-item field list and its brief, for the product's well configuration | **done 2026-10-12** |
| 3 | 1 | Validate, stage and store by code: validation, guards and review list; the three grains per run; unknown-model flag; run store; run record; command-line run | **done 2026-10-13** |
| 4 | 1 | Streamlit app, local: upload form with the well-list snapshot; detached run and live watch; run viewer with page-linked PDF, read-only review list and curve charts | **done 2026-10-13** |
| 4.5 | 1 | Detection survey over the product's real uploads | **done 2026-10-14** |
| 4.6 | 1 | Tier 1: tolerant detection for new versions of known vendors; ChampionX contract revision; empty-extraction guards | **done 2026-10-15** |
| 4.7 | 1 | Tier 2: generic extraction route, flagged for review; status wording; yardstick against template extraction | **done 2026-10-15** |
| 5 | 1 | The Lambda, its role, the S3 trigger and the run log in CDK; the app's S3 mode; the deploy workflow. Built and tested offline | **done 2026-10-19** |
| 6 | 1 | Deployed through the workflow; end to end; verification uploads; sample check of added fields. Hosting moves to round 2 | next |
| 7 | 1 | Runbook, round 1 | |
| 7.5 | — | Walkthrough material for the round-1 review: demo, examples, production and future-issue proposal, template-generator proposal, round-2 plan | |
| 8 | 2 | Round-2 design: the questions in §9 answered, added to this note | |
| 9 | 2 | Product writers: well config to DynamoDB, curves to the well's Athena partition | |
| 10 | 2 | Manual entry, edit-afterwards, version store | |
| 11 | 2 | Deployed to product end to end; round-2 verification | |
| 12 | 2 | Runbook covering both rounds | |
