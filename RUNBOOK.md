# ESP design extraction: runbook, round 1

Round 1 of the design-document extraction pipeline. A PDF uploaded from the app to S3 starts one Lambda, which detects the vendor template, extracts the numbers with Opus on Bedrock, validates and stages them, then writes the run's folder to S3 and a row to the run log.

## Where things live

| What | Where |
|---|---|
| Account, region | datascience, `640168431387`, `us-east-1` |
| Bucket | `roam-ds-assets`, under `esp-design-extraction/`: `intake/` (uploads, `<run_id>.pdf`), `runs/<run_id>/` (one folder per run), `assets/` (the four data assets) |
| Function | Lambda `esp-design-extraction`, in stack `EspDesignExtractionStack` |
| Run log | DynamoDB `esp_design_extraction_logging`, one row per run |
| Logs | CloudWatch `/aws/lambda/esp-design-extraction`, kept 90 days |
| Deploy | GitHub Actions workflow `ds-deploy-esp-design-extraction.yaml` (*DS Deploy ESP Design Extraction*); its source is in `cdk/workflows/` |
| App | `app/main.py` (Streamlit), run locally in S3 mode; not hosted |

## Trigger a run

From `esp_design_extraction/`, with `.venv` built once (`python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`):

```bash
aws sso login --profile roam-ai
export ESP_APP_MODE=s3
export ESP_BUCKET=roam-ds-assets
export ESP_INTAKE_PREFIX=esp-design-extraction/intake/
export ESP_RUNS_PREFIX=esp-design-extraction/runs/
export ESP_WELL_LIST_PATH="<well list CSV>"
AWS_PROFILE=roam-ai PYTHONDONTWRITEBYTECODE=1 .venv/bin/streamlit run app/main.py
```

`roam-ai` is an SSO profile for the datascience account, role `RoamDataScientist`, region `us-east-1`. If the app says S3 could not be read, log in again and check the variables. The well list is a CSV (`organization_id`, `well_id`) exported from Athena, `roam_prd_ddb.default.esp_well_configuration_v2`, and kept outside the repository. To refresh it, export it again; the form shows its row count, date and hash.

**New run from a PDF:** choose the organisation, then the well (filtered by organisation), add the PDF, tick *I understand this starts a paid model call* and press **Start the run**. Organisations that differ only by case are listed apart: pick the exact one. The form warns when the same PDF, organisation and well is already queued or running. The app puts the PDF at `intake/<run_id>.pdf` with its labels, the upload starts the function, and the app follows the run:

- **queued**: uploaded, no `run.json` yet. One run at a time (reserved concurrency 1), so a second upload waits here. The first run after a deploy waits about 40 s here for the cold start.
- **running**: `run.json` says `running`; elapsed time shows against `deadline_at`, about 15 minutes after the start. The record names no stage until the end; CloudWatch has one line per stage.
- **final**: **Pipeline: completed**, or **failed at** the stage that failed, with the error code and message. **Document review** is separate: a completed run can still need review. **Open the run** shows everything stored.

## Where failures show

Nothing alerts anyone. Look in four places:

1. **The app:** the run list's *Pipeline* and *Error* columns, and the run page.
2. **`runs/<run_id>/run.json`:** `status`, and `error` with `code`, `message`, `details` and `stage`. It is created as `running` and rewritten last.
3. **The run-log row:** `pipeline_execution_id` is the run id; `status` goes `RUNNING` → `SUCCEEDED` or `FAILED`; on failure `data_json` holds `error_code`, `stage`, `message` and `stack_trace`. The row is under organisation `_unknown` when its label was not read or does not decode, and has `ADHOC#<request id>` as its id when the key holds no run id. The app reads S3 only, never this table.
4. **CloudWatch:** one JSON line per stage with `run_id` and `stage`, the trace of an `INTERNAL_ERROR`, and Lambda's own lines, including its timeout.

```bash
aws s3api list-objects-v2 --bucket roam-ds-assets --prefix esp-design-extraction/runs/<run_id>/ --profile roam-ai
aws dynamodb scan --table-name esp_design_extraction_logging --profile roam-ai
aws logs tail /aws/lambda/esp-design-extraction --since 1h --profile roam-ai
```

| Code or state | Meaning | What to do |
|---|---|---|
| `IMAGE_ONLY` | Page 1 has no usable text layer. No model call | Cannot be extracted in round 1 |
| `AMBIGUOUS_TEMPLATE` | Page 1 matches more than one vendor template. No model call | Data science fixes the signatures |
| `PREP_FAILED` | The PDF cannot be read, or a kept page has unmapped glyphs or no text. No model call | Data science |
| `GENERIC_TOO_LARGE` | On the generic route, the prompt is over its 322,766-byte cap. No model call | Data science: the document needs a template, or the cap revisited |
| `UNKNOWN_TEMPLATE`, `REQUIRED_PAGES_MISSING` | Not run failures: the document takes the generic route, and its run page says *Generic extraction: review every value* | Review every value |
| `MODEL_TIMEOUT` | The model's budget (at most 780 s) ran out, or the read timed out | Upload again; data science if it repeats |
| `MODEL_TRUNCATED` | The report stopped at `max_tokens`, or has no `## row_counts` footer | Upload again; data science if it repeats |
| `MODEL_ERROR` | Bedrock refused or dropped the call (access, throttling, network, service); `error.details.aws_error_code` names it | Upload again; data science if it repeats |
| `FAMILY_MISMATCH` | The report's `vendor_family` is not the family Detect assigned | Data science |
| `REPORT_INVALID` | The report breaks its contract: no frontmatter, a `stg_*` block missing, or no `row_counts` footer. Report and findings are stored | Data science |
| `EXTRACTION_EMPTY` | No pump-configuration and no design-context rows. Report, findings and review are stored | Data science checks the contract against the document's layout |
| `LOAD_HALTED` | The report's scenarios give different pump strings. Grains, findings and review are stored; the load is halted | Data science |
| `ASSET_INVALID` | A data asset under `assets/` is missing, unreadable or off its pinned hash. It is checked before Detect, so every run fails. The role has no `s3:ListBucket`, so a **missing** asset reads as `AccessDenied` | Check the four assets (*Routine*), then upload again |
| `STORE_FAILED` | A write under `runs/` failed; `store.failed_at` names the file. If `run.json` could not even be created, only the run-log row and CloudWatch show it, and the app shows *not picked up* | Data science |
| `INTAKE_INVALID` | The key is not `intake/<run_id>.pdf`, a label is missing or does not decode, or the PDF's SHA-256 is not its label's. The app checks labels before uploading, so this points to an upload made outside it | Upload again through the app |
| `INTERNAL_ERROR` | Anything unexpected; the trace is in CloudWatch | Data science, with the trace |
| *not picked up* | Queued over 5 minutes while no run is running | Look for the run id in the run log and CloudWatch. A `FAILED` row says why the function could not start the run; neither means the function was not invoked: tell data science |
| *expired* | Queued over 6 hours, Lambda's limit for a waiting event. It will not run | Upload again |
| *failed (stale)* | `running` past `deadline_at`: stopped at the 15-minute timeout, or the last `run.json` could not be written. The run-log row stays `RUNNING` | Read CloudWatch; upload again; data science if it repeats |

## Rerun

Nothing reruns on its own: asynchronous retries are 0, and a run id runs once. To rerun, upload the same PDF again through the app. It gets a new run id; the old run stays as it is.

- **Helps** when the cause has passed or been fixed: the `MODEL_*` codes, `INTAKE_INVALID`, `ASSET_INVALID` once the assets are right, *expired*, *failed (stale)*.
- **Does not help** when the document is the cause. Detect and Prep are deterministic, so `IMAGE_ONLY`, `AMBIGUOUS_TEMPLATE`, `PREP_FAILED` and `GENERIC_TOO_LARGE` fail again the same way.
- **Ask data science first** for the other codes.

A run that reaches the model is a paid call: the app quotes about USD 0.55–1.32 a run.

## Routine

**Replacing an asset.** The four assets are `m2-d31-seed-aliases.csv`, `alias-evidence-v1.json` and `universal_catalog.parquet`, pinned in `extraction/stage/MANIFEST.txt`, and `m1-field-inventory.csv`, pinned in `extraction/assets/MANIFEST.txt`. A replacement must have the pinned SHA-256, the second column of its manifest line. Check it, then upload it under the same name; no metadata is needed:

```bash
shasum -a 256 <file>
aws s3 cp <file> s3://roam-ds-assets/esp-design-extraction/assets/<name> --profile roam-ai
```

Each run fetches the assets afresh, so the next run uses it. The manifests ship in the image: a new version of an asset needs a new pin and a deploy, and every run fails `ASSET_INVALID` while the bucket and the pin disagree.

**Deploying.** Only through the workflow, never from a laptop: `cdk/README.md`, *Deploy*. The next run has the cold start.

**Deleting the stack** removes the function, both roles, CDK's notification helper and the bucket notification. It keeps the run-log table and the log group (delete them by hand if wanted; until then a new deploy fails on their fixed names), the helper's own log group, the bucket with everything in it, and the image in the CDK bootstrap repository.

## Limits

- One run at a time; further uploads wait as *queued*, up to 6 hours.
- A 15-minute timeout; the model gets at most 780 s of it.
- No alerting: no SNS, no email. The run log's `status_timestamp` index can list failures, but nothing watches it.
- Image-only documents fail with `IMAGE_ONLY`.

Round 2, writing to product storage, adds its part of this runbook when it is built.
