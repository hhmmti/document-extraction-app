# ESP design extraction: runbook

## 1. What it is

The pipeline reads an ESP design document (a PDF) into a proposed pump and wellbore entry for one well. An engineer checks it against the document in the app and confirms it. Confirming writes the well's entry to a well table and keeps a design record of the document, both in data science.
A run starts from the app's upload form, or on its own from an ESP-design upload in the ESP product.
A confirmation on Test wells is also written back to the product's test system: the entry's values go into the well's settings there, merged with what the well already holds (the write-back; sections 9, 10 and 12). A confirmation on Live wells is not written back. Its values stay in data science, in the well table and the design record, and nothing in the production product reads them.

## 2. For engineers

**Open the app** at https://internal.roam-ai-apps.io/enterprise/esp-design-extraction/ and sign in. The run list shows every run, newest first.

**Start a run.** Press **New run from a PDF** and choose **Live wells** (the product's production wells) or **Test wells** (its test system's wells). Then choose the organisation and the well, add the PDF, tick *I've checked the organisation, the well and the document* and press **Start the run**. For any trial, use Test wells with the organisation `lab-check` and type a well name. Nothing confirmed is ever deleted.

**Watch it.** The page follows the run: *waiting to start*, then running, which takes 3 to 8 minutes. It ends with **Run: completed** and how many values are left to check, or with **Run: failed** and what happened in plain words. You can leave the page and find the run in the list. **Open the run** opens its review.

**Review it.** The document's pages sit beside the values; a page chip turns to the page a value came from. Values come in groups: the pump string, **Found**, **Not found**, then, folded, **Settled or corrected** and **Kept as read, not checked**. Each value shows its status: *to check*, *blocks publish*, *settled*, *corrected* or *kept as read* (section 13). For each value: **Settle** it as read; **Correct** it (the value, a unit the product takes, from the design document or outside it, an optional note); **Settle as empty** when the document does not print it; or, when it *blocks publish* and you keep it uncorrected, **Accept anyway** with a written reason. **Check group by group** works through up to three pop-ups, one each for the string, the values found and the values not found, then opens Write. The ticked values are settled together, and the button's sentence is recorded with them. Unticked values stay to check and are marked *left out of a group check*. A value that blocks publish cannot be ticked.

**The pump string.** Settle it as proposed, accept it anyway, or press **Edit sections**: one row per section, 1 the deepest. Each section takes a model from the pump catalogue, or is marked **Not in the catalogue** with every name the document prints for it. **Save**, **Cancel** and **Back to the string as read** do what they say. A string with a section not in the catalogue, or with no stage count, is recorded but not written to the well's entry, and Save asks why.

**Confirm with Write.** Confirming is the **Write to Live wells** (or **Write to Test wells**) button. It opens a pop-up that shows where the entry goes, who is confirming, the document and its design date, and what will be recorded; its own Write button confirms. Write stays off until nothing is left to check and its checks pass, and the line beside it says why. Writing saves the well's entry and the design record in data science, and the run then opens read-only. On Test wells the entry's values then go to the well's settings in the product's test system; the app does not say whether that went through, and data science reads it in the run log (section 9). On Live wells nothing goes to the product. **Older-design warning:** when this document's printed design date is older than the date on the well's current entry in the same wells, the header and the Write pop-up warn you: writing makes the older design the current entry. If either date is missing they say *can't compare*.

**Enter by hand.** A run that ended with nothing read offers **Enter by hand**. Every value from the document starts empty and to check. Type each value the document prints with **Correct**, build the string with **Edit sections**, and settle the rest as empty. Pressures are absolute (psia): add 14.7 to a gauge reading and put the printed value in the note. Write stays off until something is typed.

**A scanned (image-only) document.** The run fails and says to transcribe the scan. Install the **transcribe-design-scan** skill from Roam's Claude organisation directory and give it the scan. Upload the one PDF it returns as a new run. The review opens on the original scan; check every value against it. Or press **Enter by hand** on the failed run.

**Runs started automatically.** An ESP-design upload in the product can start a run here by itself. Such a run is marked *Started automatically by an upload in the product* and writes to the wells the upload came from. Review it like any other run. This works from the product's test system. From production it is expected once the product's release arrives there, and it has not been tried yet.

**Whom to contact.** Failure messages name whom to tell. Give them the document name, the organisation, the well and the time shown.

## 3. Where things live

| What | Where |
|---|---|
| Accounts | datascience `640168431387`, `us-east-1`: the backend, its data, the well lists' queries. Products dev `411237692998` and products prd `826771500263` push design PDFs |
| The app | prod `https://internal.roam-ai-apps.io/enterprise/esp-design-extraction/` (datascience; the platform repo's `master`); dev `https://devinternal.roam-ai-apps.io/enterprise/esp-design-extraction/` (account `717279722427`; the platform repo's `dev`). One image and config: both read and write the same store, and both offer Live and Test wells |
| Main bucket | `s3://roam-ds-assets/esp-design-extraction/`: `intake/<run_id>.pdf` (the run's input), `runs/<run_id>/` (its files), `assets/` (four data files), `confirmations/<id>/` (request and result), `design-records/<document sha256>/`, `well-entries/environment=<dev\|prd>/` |
| Push bucket | `s3://esp-design-extraction-product-uploads-640168431387/`, under `dev/` and `prd/`: versioned, SSE-S3, public access blocked, kept if the stack is deleted |
| Functions (Lambda, one image, stack `EspDesignExtractionStack`) | `esp-design-extraction`, the extraction; `esp-design-extraction-writer`, the writer; `esp-design-extraction-product-intake`, the starter |
| Logs (CloudWatch, 90 days) | `/aws/lambda/esp-design-extraction`, `…-writer`, `…-product-intake` |
| Run log (DynamoDB) | `esp_design_extraction_logging`: one row per run, per confirmation request, per write-back of a written confirmation, and per push the starter refuses or fails to start |
| Well table (Athena) | `esp_design_extraction.well_entries`, and the view `esp_design_extraction.well_entries_current`, each well's current entry |
| Well lists (Athena) | `roam_prd_ddb` (Live) and `roam_dev_ddb` (Test), `default.esp_well_configuration_v2`; results in `s3://esp-athena-results-v2-640168431387/` when hosted, `s3://aws-athena-query-results-640168431387-us-east-1/` on a laptop; workgroup `primary` |
| Deploys | backend: GitHub Actions workflow `ds-deploy-esp-design-extraction.yaml` (source `cdk/workflows/`); app: `container/build.py` into the platform repo's `apps/enterprise/esp-design-extraction/` |
| Skill | `transcribe-design-scan`, source `skills/transcribe-design-scan/`, published in the organisation directory of Roam's company Claude account |

Four things are called dev or prod, and none of them stands for another:

| Term | What it means |
|---|---|
| The environment, `dev` or `prd` | Where a run's confirmation writes. **Test wells** is `dev` and **Live wells** is `prd`. It is set by the form's switch, or by a push's folder |
| The app's address | The dev or prod address above. Either one writes to either environment |
| The AWS account | Datascience holds the data and runs the prod address; the dev address runs in `717279722427` |
| The platform repo's `dev` and `master` branches | The source of the two addresses |

## 4. The two intakes

| | The app's form | The product's push |
|---|---|---|
| Started by | an engineer: **New run from a PDF** | the product's ESP-design upload (`POST /well-documents/esp-design/upload`), which first saves the product's own copy |
| The PDF goes to | `intake/<run_id>.pdf`, put by the app | the push bucket at `<dev\|prd>/<organization_id>/well-documents/<well_id>/esp_design/current.pdf`; the starter puts the event's version at `intake/<run_id>.pdf`, create-only (it never overwrites an existing object) |
| Run id | the UTC second and 8 random hex digits | the event's second and 8 hex digits of a hash of bucket, key and version. A repeated event is the same run; each new version of `current.pdf` is a new run |
| Labels (the intake object's metadata) | `organization-id`, `well-id`, `source-name` (the file name), `sha256`, `environment` | the same four, with `source-name` the product's `original-filename` when that is a plain `.pdf` name (else `current.pdf`), plus `origin` = `product-upload`, `environment`, `uploaded-by` (when sent) and `pushed-from` (the pushed object and its version) |
| Environment | the switch: Live wells is `prd`, Test wells `dev` | the key's first folder. The product takes the folder from `DESIGN_EXTRACTION_UPLOAD_PREFIX`, else `AWS_ENV_NAME`, and with neither it pushes nothing |
| Refused | a name or file name the labels cannot carry: refused on the form, before any upload | a key of another shape, an object that cannot be read, or a label the rules refuse: one `FAILED` run-log row, and nothing put |
| Status | working | **dev: verified end to end.** prd: expected with the product's release v2.10.1 once its prd deploy runs; not yet exercised |

Either upload starts the extraction, one run at a time. The extraction reads only the first four labels. The app and the writer also read `environment`, and the writer refuses a confirmation for another environment. For a run whose upload carries no `environment` label, the app writes to Test wells for `lab-check` and to Live wells otherwise.

The push bucket's policy refuses requests without TLS. It lets the whole products dev account put under `dev/*`, and the whole products prd account under `prd/*`. **That grant is not final:** data science is to narrow it to the product's push roles: `esp-optimization-api-role-v2` (App Runner) and `esp-api-v2-ecs-task-role-dev` / `-prd` (ECS). The starter takes only `.pdf` keys under `esp_design/`. **Whether photo or other-document uploads ever push has not been checked.** Who uploaded is kept as a label and never shown on screen.

A pushed PDF exists as four distinct copies: the product's stored PDF (its own, saved first), the push bucket's (every version kept), the run's input `intake/<run_id>.pdf`, and the design record's `document.pdf`, which Write makes, one per document SHA-256.

## 5. Confirmation and the writer

**Request and result.** Write, or the confirm CLI, puts `confirmations/<confirmation_id>/request.json` create-only. That starts the writer, which runs one request at a time, for up to 120 s, with no retry. The writer first creates `result.json` as `running`, then ends it `written`, `refused` (with its reasons) or `failed`. A `running` result past its deadline reads as `stale`. The app waits up to 180 s and then shows one of: *Written to Test wells*, *Not written: Write refused it* with the reasons in words, *Not written: Write failed*, or *No answer yet*. A stale result reads as written when the row is there, because writing the row is the commit point.

**What the writer refuses.** A refused request writes only its result. It is refused when:
- it breaks the contract; its entry method is not `extracted`, `transcription` or `manual`; or its environment is not `dev` or `prd`, or not the upload's `environment` label;
- `run.json`, `well_entry.json` (not for an entry by hand) or `intake/<run_id>.pdf` is missing, or its organisation, well or document SHA-256 differs from the run's (the writer cannot list the bucket, so a 403 reads as missing);
- a value is missing, extra, left to check, or changed without being marked corrected; or it blocks publish and is neither corrected nor accepted with a reason;
- the string's order check fails with no accept-anyway reason, or another string check fails while the string is neither settled nor corrected;
- `design_document_ref` is given, a pump model is not spelled as the catalogue spells it, a map breaks the product's field rules, or the row does not fit the table's types.

**`lab-check` goes to Test wells only, and only the app enforces it.** Write is off for `lab-check` anywhere else. The writer and the CLI do not check this.

**What it records without writing.** A pump string whose final value is empty, because a section is not in the catalogue or has no stage count, is recorded but not written. The row gets no `pump_sections`, while the version keeps the final sections, their checks and the reason. Any value settled as empty is left out of the row the same way, and the version keeps it.

**The row** is `well-entries/environment=<env>/<confirmation_id>.json`, one JSON line, written create-only and never overwritten or deleted: `organization_id`, `well_id`, `pump_parameters` and `wellbore_parameters` (each value `{value, unit, source, as_of, note}`), `run_id`, `document_sha256`, `design_date` (the report's printed date, or null), `entry_method`, `confirmation_id`, `confirmed_at`, `confirmed_by` and `environment`.

**The current entry** is a well's row with the latest `confirmed_at` in its environment, the highest `confirmation_id` breaking a tie. The view `well_entries_current` gives it. A newer confirmation is always current, whatever its design date.

**The design record**, under `design-records/<document_sha256>/`: `document.pdf`, the run's intake PDF copied create-only, so one document is stored once; and `versions/<confirmation_id>.json`, one per confirmation, with each value as proposed and as confirmed, its starting status, history and accept-anyway reason, the string's proposed and final sections with their checks, the labels (with `origin`, `uploaded_by` and `pushed_from` for a pushed run), and the row. The row's `design_document_ref` points at its version.

**Corrections come back on a re-run.** A new run of the same document (the same SHA-256) for the same well, in the same environment, starts from the latest confirmed version: every correction comes back, shown beside the new reading where the reading moved; a settlement or accept-anyway reason comes back where the proposal is unchanged; and the header says how many values come back, and from whose confirmation.

**A new design replaces them.** A different document has a different SHA-256, so nothing carries over. Once confirmed, its row is the well's current entry. The older-design warning (section 2) is the only check on dates.

**The confirm CLI** (`extraction/confirm.py`) confirms a run's stored entry, as extracted. Its options are `--as-proposed`, `--set KEY=VALUE`, `--accept-anyway REASON` and `--wait` (default 180 s). Its environment, `--environment`, **defaults to `prd`**: pass `dev` for anything on Test wells. With `ESP_APP_MODE=s3` it writes the request to the bucket, and `--confirmed-by` defaults to the AWS session name. In local mode `--confirmed-by` is required. It has no entry-by-hand or transcription mode.

**Who confirms (`confirmed_by`).** Hosted: the signed-in user, exactly as the platform's `X-Forwarded-User` header sends it (the signed-in email); without the header, Write is off. A laptop in S3 mode: the AWS session name. The CLI: `--confirmed-by`, else the AWS session name. `container/run_local.sh`: the profile's session name.

## 6. Manual entry

- **Which runs offer it:** a run that has ended (failed, succeeded, or still running past its deadline), stored no `well_entry.json`, names its organisation, well and document SHA-256, and whose PDF the app can find. This covers a scan that failed `IMAGE_ONLY`, a model timeout and an unreadable PDF. For a model timeout or model error the page adds that uploading again retries the extraction.
- **What the writer refuses**, for `manual`: a run that has not ended; a run that stored `well_entry.json` ("this run has an extraction to review"); a request with nothing typed (no value corrected to a value, and no section in the string); and anything its other checks refuse, run against the empty proposal.
- **No design date.** Nothing reads a report, so `design_date` is null. The header says *none, entered by hand*, so the older-design note can only say *can't compare*, and never *older*.
- **Pressures are typed as absolute psia.** This applies to bubble point, reservoir pressure and vapour pressure. Where the document prints a gauge reading, add 14.7 and give the printed value in the note, for instance `printed 1,500 psig`. The Correct form says so for these three keys on every run.
- **Entering the same document again.** A written run opens read-only. Upload the document again. When that run also ends with nothing read, Enter by hand opens with the earlier typed values carried back, within the same wells.
- **No CLI mode.** The CLI needs a stored `well_entry.json`. The request goes as `entry_method` `manual`, a transcription's too.

## 7. The transcription route

- **The skill.** `transcribe-design-scan` is published to the organisation directory of Roam's company Claude account. Anyone with a seat installs it from there. Claude types each scan page as one page and returns `<scan name> - transcription.pdf`, with the scan attached byte for byte and each unsure reading marked `[?]`. If a page file is missing or empty, it writes nothing.
- **The mark and what the app checks.** The transcription carries `/RoamTranscription` in its document information: schema 1, the original's name, SHA-256, page count and size, the model, and the unsure readings. The app takes the file as a transcription only when the mark parses, exactly one attachment has the mark's SHA-256, and that attachment's page count matches the mark's and the transcription's. The form then names the original and its page count.
- **The original is kept and shown.** The review and every pop-up open on the original scan, with a switch to the transcription. Page N is page N in both. The design record's `document.pdf` is the transcription, which keeps the original scan inside it.
- **Every value starts to check.** On a vendor template, every value read from the document, every section and the string start to check (unless they block publish), each with "Typed from a scan: check it against the original." A value settled one by one records `checked against the original scan`, and the request goes as `transcription`.
- **A marked file without its original** is a broken transcription ("the original scan is not attached to it"). The form refuses it and asks for the file exactly as the skill made it. A run made from one opens with a warning, and Write stays off.

## 8. Live and Test wells

- **The switch.** **Live wells** reads the product's production list (`roam_prd_ddb`) and **Test wells** its test list (`roam_dev_ddb`). It starts on Live wells, then on the side last chosen in the session; switching clears the organisation and well. It sets the run's environment: Live wells is `prd` and Test wells `dev`. It is not the app's address: both addresses offer both lists.
- **Refresh.** Each list is one Athena query, read once per app process. The form says when it was read, how many wells it holds and how many rows it left out (no organisation or no well). **Refresh** reads it again.
- **`lab-check` under Test wells.** It is listed only there. Its well is typed, any name, sent exactly as typed, and it writes to Test wells only.
- **Marks for hidden characters.** Names show exactly as the product holds them, with signs for characters that would not show: ␣ a space at an edge or in a run of two or more, ° a no-break space, → a tab, ▫ another kind of space, ¦ a character with no width. A line says what the signs mean, and when names differ only by case or by such characters. No two names of a list show alike.
- **Refusals.** The upload's labels take printable characters only. A name with °, → or ▫, or with ¦ for a zero-width or control character, starts no run: the form says so as soon as the name is chosen, and **Start the run** stays off. A file name is checked the same way once the file is added: the form says to rename the file and add it again.
- **Retry.** If a list cannot be read, the form stays off and offers **Retry**. Nothing falls back to a CSV. The error is under the page's technical section, or in the app's log.

## 9. Where failures show

Nothing alerts anyone. Where to look:
- **The app.** The list's **Run** column, and the watch page's plain words. With `ESP_SHOW_TECHNICAL=1`, the run id, stage and error code show under *Details (technical)* and *Run details (technical)*. Without it they go to the app's log: on a laptop, standard error; hosted, the container's log on the platform side (its location is not recorded).
- **The run's files.** `runs/<run_id>/run.json` holds `status`, and `error` with `code`, `message`, `details` and `stage`.
- **The run log.** The `pipeline_execution_id` is the run id or the confirmation id, and `status` is `RUNNING`, `SUCCEEDED` or `FAILED`. A written confirmation has two rows under its id: its own, with `type` `PIPELINE_EXECUTION`, and its write-back's, with `type` `WRITE_BACK`, which is described below. On failure, a run's or a confirmation's `data_json` holds `error_code`, `stage`, `message` and `stack_trace`. A row whose labels were not read has the organisation `_unknown`; a row whose key names no id has the id `ADHOC#<request id>`. The app never reads this table.
- **CloudWatch.** One JSON line per step, each with its run or confirmation id.
- **A write-back.** Each written confirmation has a second row in the run log, under the same confirmation id, with `type` `WRITE_BACK`. It is written once, when the write-back ends, and the confirmation stands whatever it says.
  - `SUCCEEDED` is written or skipped; `data_json` says which in `outcome`, with the `reason` for a skip: a confirmation on Live wells, a `lab-check` well, or a well the product does not hold.
  - For a write, `maps` lists the keys written to each map and the stored keys kept, and `pump_string` says whether the stored string was `replaced`, `added` or `kept`. Kept means the entry had no string, so an older design's string may now sit beside a newer design's values.
  - `FAILED` gives the `stage` (`secret`, `auth`, `get` or `update`), the product's `status_code` when it answered, and its `message`.
  - The row holds keys, never values: the values are in the entry's own file, which `row_key` names. The writer's log has one line per call, with `stage` `write_back`.

```bash
aws sso login --profile roam-ai
aws s3api list-objects-v2 --bucket roam-ds-assets --prefix esp-design-extraction/runs/<run_id>/ --profile roam-ai
aws dynamodb query --table-name esp_design_extraction_logging --index-name pipeline_execution_id_timestamp --key-condition-expression "pipeline_execution_id = :id" --expression-attribute-values '{":id":{"S":"<run_id or confirmation_id>"}}' --profile roam-ai
aws logs tail /aws/lambda/esp-design-extraction --since 1h --profile roam-ai
aws logs tail /aws/lambda/esp-design-extraction-writer --since 1h --profile roam-ai
aws logs tail /aws/lambda/esp-design-extraction-product-intake --since 1h --profile roam-ai
aws s3api list-object-versions --bucket esp-design-extraction-product-uploads-640168431387 --prefix dev/ --profile roam-ai
aws s3 cp s3://roam-ds-assets/esp-design-extraction/confirmations/<confirmation_id>/result.json - --profile roam-ai
# the current entry of one lab-check well on Test wells (a SELECT; Athena keeps its result file), then its rows
aws athena start-query-execution --work-group primary --result-configuration OutputLocation=s3://aws-athena-query-results-640168431387-us-east-1/ --query-string "SELECT * FROM esp_design_extraction.well_entries_current WHERE environment = 'dev' AND organization_id = 'lab-check' AND well_id = '<well>'" --profile roam-ai
aws athena get-query-results --query-execution-id <QueryExecutionId> --profile roam-ai
# one confirmation's write-back row, then every write-back for one organisation
aws dynamodb query --table-name esp_design_extraction_logging --index-name pipeline_execution_id_timestamp --key-condition-expression "pipeline_execution_id = :id" --filter-expression "#t = :t" --expression-attribute-names '{"#t":"type"}' --expression-attribute-values '{":id":{"S":"<confirmation_id>"},":t":{"S":"WRITE_BACK"}}' --profile roam-ai
aws dynamodb query --table-name esp_design_extraction_logging --index-name organization_id_type_timestamp --key-condition-expression "organization_id_type = :k" --expression-attribute-values '{":k":{"S":"<organization_id>#WRITE_BACK"}}' --profile roam-ai
```

| Where, code or state | Meaning | What to do |
|---|---|---|
| `IMAGE_ONLY` | No usable text layer on page 1; no model call | Transcribe with the skill (section 7), or Enter by hand |
| `AMBIGUOUS_TEMPLATE`, `PREP_FAILED` | More than one vendor template matches; or the PDF, a kept page's glyphs or its text cannot be read. No model call | Enter by hand; data science |
| `GENERIC_TOO_LARGE` | With no template, the prompt is over 322,766 bytes; no model call | Enter by hand; data science |
| `UNKNOWN_TEMPLATE`, `REQUIRED_PAGES_MISSING` | Not failures: the run reads the document without a template, and every value needs checking | Review every value |
| `MODEL_TIMEOUT`, `MODEL_TRUNCATED`, `MODEL_ERROR` | The model's time ran out (at most 780 s), or its report stopped short, or Bedrock refused or dropped the call | Upload again; data science if it repeats |
| `FAMILY_MISMATCH`, `REPORT_INVALID`, `EXTRACTION_EMPTY` | The report names another vendor, breaks its contract, or holds no rows | Data science |
| `LOAD_HALTED` | The document's scenarios give different pump strings. The values read are still there to review | Check which scenario is the design, then review |
| `ASSET_INVALID` | A data asset is missing or off its pinned hash, so every run fails. A missing one reads as `AccessDenied` | Check the assets (section 11); upload again |
| `STORE_FAILED` | A write under `runs/` failed; `store.failed_at` names the file | Data science |
| `INTAKE_INVALID` (extraction) | The key, a label or the PDF's hash is wrong: an upload made outside the app | Upload again through the app |
| `INTERNAL_ERROR` | Anything unexpected; the trace is in the function's log | Data science, with the trace |
| *not picked up*, *expired* | Waiting over 5 minutes with no run running; waiting over 6 hours | Upload again; if it is not picked up again, look for its run id in the run log and the log |
| *stopped without finishing* | Still `running` past its deadline: the 15-minute timeout, or the last `run.json` was not written | Read the log; upload again |
| Writer: *refused* | `result.json` lists the reasons, and the app shows them in words | Fix what it says; Write again |
| Writer: *failed* | An error at a stage, such as `ASSET_INVALID` or `INTERNAL_ERROR`, with the trace in the writer's log | Write again; data science if it repeats |
| Writer: *stale*, *no answer yet* | No final result within its time. If the row is there it is written | Reopen the run later; do not redo a written one |
| Write-back: *skipped* | Not a failure: Live wells, a `lab-check` well, or a well the product does not hold | Nothing. For a well the product should hold, check its organisation and name there |
| Write-back: *failed*, 403 at `get` | The product's login does not reach the well's organisation | Data science: the login's access in the product, then rerun the write (section 10) |
| Write-back: *failed*, other | 401: the login or its token was refused. 422: the product refused a value. 429, 500 or a timeout: the product did not take the call. `secret`: the secret could not be read. *not started*: too little of the writer's 120 s was left | Rerun the write (section 10); data science if it repeats |
| Write-back: no row | The writer stopped before the row was written, or the row's write failed; the writer's log says | Run the dry run (section 10), which shows what the well holds |
| App: *Write is off* | Not sent: the line beside Write says why. On a laptop, the catalogue or credentials may be missing | Follow the line; tell the contact |
| Starter: `INTAKE_INVALID` | A key of another shape, no run id in the event, an object that is not there (403, 404), or a label refused. One `FAILED` row, nothing put | Data science checks the key and the product's push |
| Starter: `INTERNAL_ERROR` (read), `STORE_FAILED` (intake) | A read or put failed for a reason that may pass. A `FAILED` row is written, then Lambda retries twice, as the same run | Read the starter's log. If both retries fail, there is no dead-letter queue: data science |
| Push not made | The product's push failed. Its upload still succeeds, with `datascience_pushed=False`, and nothing reaches data science: no row, no log here. A folder other than `dev/` or `prd/` is refused by the bucket policy | Check the product's response; upload again in the product, or use the app's form |

## 10. Rerun

- **The app's form.** Upload the same PDF again. It gets a new run id, and the old run stays as it is. The extraction never retries on its own.
  - This **helps** once the cause has passed: the `MODEL_*` codes, `INTAKE_INVALID`, `ASSET_INVALID` once the assets are right, *expired*, and *stopped without finishing*.
  - It **does not help** for `IMAGE_ONLY`, `AMBIGUOUS_TEMPLATE`, `PREP_FAILED` or `GENERIC_TOO_LARGE`. Detect and Prep read only the PDF's bytes, so they fail the same way again; use the skill or Enter by hand instead.
  - Ask data science first for the other codes.
- **The product's push.** The starter retries a failed read or put twice, as the same run. After that, upload the design again in the product, which makes a new version and so a new run. Or start a form run on the same side.
- **A confirmation.** The writer never retries. Refused: fix the reasons and press Write again, which makes a new confirmation id; failed: Write again; with the CLI, run it again. A written one cannot be redone: to change a well's entry, start a new run of the same document and confirm that one, and its corrections come back.
- **A write-back.** The writer never retries it. Rerun it from a laptop, in this folder, as data science. Without `--go` it is a dry run: it logs in to the product's test system, reads the well, and prints whether the product holds it and the login reaches it, each key's stored and new value, and what happens to the pump string. It sends nothing and writes no row.
  ```bash
  ESP_WRITEBACK_SECRET=esp-api/ds-writeback-credentials .venv/bin/python -m extraction.write_back --confirmation <confirmation_id> --profile roam-ai
  ESP_WRITEBACK_SECRET=esp-api/ds-writeback-credentials ESP_RUN_LOG_TABLE=esp_design_extraction_logging .venv/bin/python -m extraction.write_back --confirmation <confirmation_id> --go --well "<organization_id>" "<well_id>" --profile roam-ai
  ```
  `--go` writes only the wells named with `--well`, and leaves a `WRITE_BACK` row as the writer's does. A rerun lays that confirmation's values over what the well holds now, so rerunning an older confirmation overwrites a newer one's keys: read the dry run first.
- **The one-time pass.** `--pass` in place of `--confirmation <confirmation_id>` takes every well's current entry on Test wells, each read from its own file under `well-entries/environment=dev/`. Without `--go` it is the dry run of them all. With `--go` it writes only the wells named, one `--well` for each, and refuses to run with none named.
- Each run that reaches the model is a paid call. Its cost is in `run.json` (`cost_usd`), and in the list's **Cost** column with the technical views on.

## 11. Changing things

- **Backend** (the three functions, the push bucket, the run log and the well table): only through the GitHub Actions workflow `ds-deploy-esp-design-extraction.yaml`, started by hand from a branch, never from a laptop (`cdk/README.md`, *Deploy*). It stops unless the target is `us-east-1` in `640168431387`.
- **The app:** the lab → the DS repo's branch → `container/build.py` run from that commit (it refuses uncommitted shipped files; README, *Run it in a container*) → a PR to the platform repo's `dev`, owned and reviewed by the platform's reviewer, which deploys the dev address → that reviewer's `dev` → `master` PR, which deploys the prod address. `container/run_local.sh` tries a build on a laptop.
- **AWS access for the app.** Hosted, the app assumes `RcpCrossAccountDataRole` in datascience; its own config asks only for `sts:AssumeRole`. New access goes in the platform's role file and reaches AWS only on `master`. README, *Run the app against S3*, lists what the app needs.
- **Assets.** These are `m2-d31-seed-aliases.csv`, `alias-evidence-v1.json` and `universal_catalog.parquet` (pinned in `extraction/stage/MANIFEST.txt`) and `m1-field-inventory.csv` (pinned in `extraction/assets/MANIFEST.txt`). Each run, and each confirmation for the catalogue, fetches them from `assets/` and checks them against the pins. To replace one, check `shasum -a 256 <file>` against its manifest line, then upload it under the same name. A new version needs a new pin and a backend deploy, and every run fails `ASSET_INVALID` until both agree.
- **The skill.** Build its zip from `skills/transcribe-design-scan/` without `__MACOSX/` entries; a Finder zip adds them, and README, *The transcription route*, has the command. Never commit the zip. Upload it under Customize → Skills. How a new version replaces the old one is not recorded.
- **The technical views.** `ESP_SHOW_TECHNICAL=1` shows run ids, codes and Run details. Unset, they are hidden and what they hold goes to the app's log. The hosted config does not set it: change it through the app's route above.
- **Who confirms.** Hosted, the value of the header that `ESP_SIGNED_IN_USER_HEADER` names (`X-Forwarded-User`, in `container/app.config.json`). With the data role set and no header named, Write is off. Change it through the app's route. The contact engineers are told about comes from `build.py --contact`.
- **Deleting the stack** removes the functions, their roles and the stack's notifications. It keeps the run log, the three log groups, the push bucket with every version, the Glue database, table and view, and `roam-ds-assets` with everything in it.

## 12. Limits

- One run at a time; other uploads wait up to 6 hours, then expire. One run has 15 minutes, of which the model gets at most 780 s. The writer handles one request at a time and the app waits 180 s for it.
- No alerting: no alarm, no email. The run log's `status_timestamp` index can list failures, but nothing watches it.
- **The write-back reaches the product's test system only.** Production gets nothing until that is agreed.
- **The `demo` organisation.** The write-back cannot tell a synthetic document from a client's. So on `demo` wells, the rule that only synthetic documents go there stays a practice: nothing enforces it.
- **An edit made in the product between the write-back's read and its write is lost.** They are two calls, with no lock.
- The prd push is not yet exercised. The push grant is account-wide until it is narrowed. Photo and other-document pushes are unchecked. A failed push is lost on the product side, and the starter has no dead-letter queue.
- Image-only documents have no automatic route: use the skill or enter by hand.
- Both addresses write to one store, so a confirmation made on the dev address is as real as one made on prod.
- Rows and versions are never deleted or overwritten (test rows too). A mistaken entry is superseded only by a newer confirmation for the well.
- Only the app keeps `lab-check` on Test wells; the CLI defaults to `prd`.
- A well added in the product shows on the form only after **Refresh**.

## 13. The app's words

| Words | Meaning |
|---|---|
| **Run** (list column, watch page) | Whether the document was read: *completed*, *failed*, *running*, or *stopped without finishing* (still recorded as running past its deadline). An upload not started yet is *waiting to start*, *not picked up* or *expired* |
| **Document review** (list, watch page) | *confirmed: Live wells, <day>* (or Test wells); *N values left to check*, the count the review opens with, with the string among them and carried values counted as decided; *nothing left to check*; *not yet*, while the run is running; or *nothing to review*, when no entry was stored |
| **Wells** (list column) | Where the run writes when confirmed: *Live wells* (`prd`) or *Test wells* (`dev`) |
| **Check group by group** | Opens the string's, the found values' and the not-found values' pop-ups in turn, then Write |
| *kept as read* | Settled by the extraction with nothing flagged, and checked by no one. It is never shown as settled and is written as read unless corrected. The folded group is *Kept as read, not checked* |
| Statuses | *to check*; *blocks publish* (only a correction that passes, or *accept anyway* with a reason, clears it); *settled*; *corrected* (`manually corrected` in the record); *kept as read*; *not from the document* (the design-record link Write fills in, and the vapour pressure operations enter). Badges: *accepted anyway*, *left out of a group check*, *kept from before* |
| *Started automatically by an upload in the product* | The upload's `origin` label is `product-upload`. The list's **How it started** column says this, *Started here, with New run*, or *not recorded* |
