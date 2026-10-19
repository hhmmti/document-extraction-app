"""The Lambda handler, offline.  S3 and DynamoDB are hand-written fakes; Bedrock is test_bedrock's stub
client, built per run as the handler builds the real one.  A run that gets past the assets needs the real
ones (ESP_EXTRACTION_ASSETS_DIR), which the fake bucket then serves; the rest need nothing."""

import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote

import pytest
from botocore.exceptions import ClientError

import extraction.run as R
import lambda_function as L
from extraction.assets_dir import ASSETS_ENV
from test_bedrock import FakeClient, events
from test_run import GOOD
from test_unit import F4_P1, F4_P2, make_pdf

BUCKET = "roam-ds-assets"
INTAKE, RUNS, ASSETS = (f"esp-design-extraction/{p}/" for p in ("intake", "runs", "assets"))
FUNCTION = "esp-design-extraction"
RUN_ID = "20261018T120000Z-0123abcd"
KEY = f"{INTAKE}{RUN_ID}.pdf"
RUN_JSON = f"{RUNS}{RUN_ID}/run.json"
PDF = make_pdf([F4_P1, F4_P2])
SOURCE_NAME = "Désign report 1.pdf"     # arrives percent-encoded
needs_assets = pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"the pipeline reads the assets; set {ASSETS_ENV}")


REQUEST_ID = "c0ffee00-1234-4abc-8def-000000000001"
UPLOADER = {"principalId": "AWS:AIDAEXAMPLEUPLOADER"}         # what an S3 event says about who uploaded
UPLOADER_IP = "203.0.113.7"
# A few of DynamoDB's reserved words: the run log's three (status, type, timestamp) and some common ones.
RESERVED = {"STATUS", "TYPE", "TIMESTAMP", "DATA", "NAME", "KEY", "VALUE", "DATE", "TIME", "SIZE", "COUNT"}
ROW = {"organization_id", "timestamp", "type", "organization_id_type", "pipeline_execution_id", "uuid",
       "resource_name", "well_ids", "status", "success", "data_json"}
ENDED = ROW | {"end_timestamp"}


def client_error(status, code, operation, message=None):
    return ClientError({"Error": {"Code": code, "Message": message or code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class FakeS3:
    """One bucket.  PutObject with IfNoneMatch="*" answers 412 when the key exists; `fail` makes a call raise."""

    def __init__(self):
        self.objects, self.metadata, self.calls, self.puts, self.fail = {}, {}, [], [], {}

    def _call(self, operation, bucket, key):
        assert bucket == BUCKET
        self.calls.append((operation, key))
        if (operation, key) in self.fail:
            raise self.fail[operation, key]

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None):
        self._call("PutObject", Bucket, Key)
        if IfNoneMatch == "*" and Key in self.objects:
            raise client_error(412, "PreconditionFailed", "PutObject")
        self.objects[Key] = Body
        self.puts.append((Key, Body))

    def head_object(self, *, Bucket, Key):
        self._call("HeadObject", Bucket, Key)
        if Key not in self.objects:
            raise client_error(403, "403", "HeadObject")
        return {"Metadata": self.metadata.get(Key, {})}

    def get_object(self, *, Bucket, Key):
        self._call("GetObject", Bucket, Key)
        if Key not in self.objects:
            raise client_error(403, "AccessDenied", "GetObject")    # without ListBucket a missing key is a 403
        return {"Body": io.BytesIO(self.objects[Key]), "Metadata": self.metadata.get(Key, {})}


class FakeTable:
    """The run-log table, keyed on organization_id and timestamp.  UpdateItem takes `SET` assignments and
    refuses what DynamoDB refuses: a reserved word without an alias, a key attribute in the expression, and
    a name or value placeholder that is undefined or unused.  `error` makes the writes in `fail` raise."""

    KEY = ("organization_id", "timestamp")

    def __init__(self, error=None, fail=("PutItem", "UpdateItem")):
        self.rows, self.writes, self.error, self.fail = {}, [], error, fail

    @property
    def items(self):
        return list(self.rows.values())

    def put_item(self, *, Item):
        if self.error and "PutItem" in self.fail:
            raise self.error
        self.writes.append(("PutItem", dict(Item)))
        self.rows[tuple(Item[k] for k in self.KEY)] = dict(Item)

    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        if self.error and "UpdateItem" in self.fail:
            raise self.error
        names, values = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}

        def refuse(message):
            return client_error(400, "ValidationException", "UpdateItem", message)
        if not UpdateExpression.startswith("SET "):
            raise refuse("this fake takes SET assignments only")
        updates = {}
        for assignment in UpdateExpression.removeprefix("SET ").split(","):
            path, _, placeholder = (part.strip() for part in assignment.partition("="))
            if not path.startswith("#") and path.upper() in RESERVED:
                raise refuse(f"Attribute name is a reserved keyword; reserved keyword: {path}")
            if (path.startswith("#") and path not in names) or placeholder not in values:
                raise refuse(f"An expression attribute name or value used in an expression is not defined: {assignment}")
            name = names.get(path, path)
            if name in self.KEY:
                raise refuse(f"Cannot update attribute {name}. This attribute is part of the key")
            updates[name] = values[placeholder]
        if set(names) - set(re.findall(r"#\w+", UpdateExpression)) or set(values) - set(re.findall(r":\w+", UpdateExpression)):
            raise refuse("Value provided in ExpressionAttributeNames or ExpressionAttributeValues unused in expressions")
        self.writes.append(("UpdateItem", dict(Key), updates))
        self.rows.setdefault(tuple(Key[k] for k in self.KEY), dict(Key)).update(updates)


class Context:
    aws_request_id = REQUEST_ID

    def __init__(self, remaining_ms):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


def labels(**raw):
    """The intake object's metadata, percent-encoded; `raw` replaces values as stored (None drops one)."""
    values = {"organization-id": "org-1", "well-id": "WELL 1", "source-name": SOURCE_NAME, "sha256": R.sha256(PDF)}
    stored = {**{k: quote(v, safe="") for k, v in values.items()}, **raw}
    return {k: v for k, v in stored.items() if v is not None}


def s3_event(key=KEY):
    return {"Records": [{"eventSource": "aws:s3", "eventName": "ObjectCreated:Put", "userIdentity": UPLOADER,
                         "requestParameters": {"sourceIPAddress": UPLOADER_IP},
                         "s3": {"bucket": {"name": BUCKET}, "object": {"key": quote_plus(key, safe="/"), "size": 1}}}]}


@pytest.fixture
def s3(tmp_path, monkeypatch):
    for name, value in {"ESP_BUCKET": BUCKET, "ESP_INTAKE_PREFIX": INTAKE, "ESP_RUNS_PREFIX": RUNS,
                        "ESP_ASSETS_PREFIX": ASSETS, "ESP_RUN_LOG_TABLE": "esp_design_extraction_logging",
                        "ESP_INFERENCE_PROFILE_ID": R.MODEL_ID, "AWS_LAMBDA_FUNCTION_NAME": FUNCTION}.items():
        monkeypatch.setenv(name, value)
    assets_dir = os.environ.get(ASSETS_ENV)
    monkeypatch.setenv(ASSETS_ENV, assets_dir or "")        # the handler repoints it; restored after the test
    monkeypatch.setattr(L, "WORK", tmp_path / "tmp" / "esp-design-extraction")     # a /tmp/ path, as on Lambda
    fake = FakeS3()
    fake.objects[KEY], fake.metadata[KEY] = PDF, labels()
    for name in L.ASSET_MANIFESTS if assets_dir else ():
        fake.objects[ASSETS + name] = (Path(assets_dir) / name).read_bytes()
    return fake


def invoke(s3, *, remaining_ms=900_000, table=None, event=None, bedrock=None):
    table = table or FakeTable()
    out = L.lambda_handler(event or s3_event(), Context(remaining_ms), s3=s3, table=table,
                           bedrock=bedrock or (lambda budget_s: FakeClient(events(GOOD))))
    return out, table


def stored(s3, name):
    return json.loads(s3.objects[f"{RUNS}{RUN_ID}/{name}"])


def data(row):
    return json.loads(row["data_json"])


def assert_row(row, *, status, organization_id="org-1", pipeline_execution_id=RUN_ID, well_ids=("WELL 1",)):
    """A finished row, as the file-upload pipeline's PIPELINE_EXECUTION row."""
    assert set(row) == ENDED
    assert (row["type"], row["organization_id_type"]) == ("PIPELINE_EXECUTION", f"{organization_id}#PIPELINE_EXECUTION")
    assert (row["organization_id"], row["pipeline_execution_id"], row["uuid"], row["well_ids"]) == (
        organization_id, pipeline_execution_id, pipeline_execution_id, list(well_ids))
    assert (row["status"], row["success"], row["resource_name"]) == (status, status == "SUCCEEDED", FUNCTION)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?", row[t]) for t in ("timestamp", "end_timestamp"))
    assert row["end_timestamp"] >= row["timestamp"]
    assert data(row)["request_id"] == REQUEST_ID


def writes(table):
    """Each write, as (operation, the status it writes, the row's key)."""
    out = []
    for write in table.writes:
        key, attributes = (write[1], write[1]) if write[0] == "PutItem" else (write[1], write[2])
        out.append((write[0], attributes["status"], tuple(key[k] for k in FakeTable.KEY)))
    return out


def put_keys(s3):
    return [key for key, _ in s3.puts]


def assert_no_local_paths(s3, tmp_path):
    for key, body in s3.objects.items():
        if key.endswith(("/run.json", "/source.json")):
            assert "/tmp/" not in body.decode() and str(tmp_path) not in body.decode(), key


# ---- stopped before a run exists ----

@pytest.mark.parametrize("status", [412, 409])
def test_a_duplicate_stops_with_no_run_and_no_row(s3, status):
    if status == 412:
        s3.objects[RUN_JSON] = b"{}"
    else:
        s3.fail["PutObject", RUN_JSON] = client_error(409, "ConditionalRequestConflict", "PutObject")
    out, table = invoke(s3)
    assert out == [{"run_id": RUN_ID, "status": "duplicate"}]
    assert s3.calls == [("PutObject", RUN_JSON)] and s3.puts == [] and table.items == []


@pytest.mark.parametrize("key", [
    f"{INTAKE}report.pdf",
    f"{INTAKE}20261018T120000Z-0123ABCD.pdf",      # upper-case hex is not the run id format
    f"{INTAKE}x/{RUN_ID}.pdf",
    f"{INTAKE}{RUN_ID} (1).pdf",                    # sent URL-encoded, "+%281%29"; decoded before the check
])
def test_an_invalid_key_writes_only_a_failure_row(s3, key):
    out, table = invoke(s3, event=s3_event(key))
    assert out == [{"key": key, "status": "rejected"}] and s3.calls == []
    [row] = table.items
    assert writes(table) == [("PutItem", "FAILED", (row["organization_id"], row["timestamp"]))]
    assert_row(row, status="FAILED", organization_id="_unknown", pipeline_execution_id=f"ADHOC#{REQUEST_ID}",
               well_ids=())
    assert row["end_timestamp"] == row["timestamp"]
    assert data(row) == {"request_id": REQUEST_ID, "bucket": BUCKET, "key": key, "error_code": "INTAKE_INVALID",
                         "stage": "key", "message": f"the key is not {INTAKE}<run id>.pdf", "stack_trace": None}


# ---- INTAKE_INVALID after the create ----

@pytest.mark.parametrize("raw,bad", [
    ({"well-id": None}, ["well-id"]),                                  # missing
    ({"organization-id": "%FF"}, ["organization-id"]),                 # not UTF-8
    ({"source-name": "report%2.pdf"}, ["source-name"]),                # a broken escape
    ({"source-name": quote("x/y.pdf", safe="")}, ["source-name"]),     # not a plain file name
    ({"sha256": "abc", "well-id": ""}, ["well-id", "sha256"]),
])
def test_a_missing_or_undecodable_label_fails_as_intake_invalid(s3, raw, bad):
    s3.metadata[KEY] = labels(**raw)
    out, table = invoke(s3)
    record = stored(s3, "run.json")
    assert (record["status"], record["error"]["code"], record["error"]["stage"]) == ("failed", "INTAKE_INVALID", "labels")
    assert record["error"]["details"] == {"labels": bad}
    [row] = table.items                                 # one FAILED row: the labels come before the RUNNING row
    assert [op for op, _, _ in writes(table)] == ["PutItem"] and row["end_timestamp"] == row["timestamp"]
    assert_row(row, status="FAILED", organization_id="_unknown" if "organization-id" in bad else "org-1",
               well_ids=() if "well-id" in bad else ("WELL 1",))              # each label as far as it decodes
    assert data(row)["error_code"] == "INTAKE_INVALID"


def test_a_hash_mismatch_fails_as_intake_invalid(s3):
    s3.metadata[KEY] = labels(sha256="0" * 64)
    out, table = invoke(s3)
    record = stored(s3, "run.json")
    assert (record["status"], record["error"]["code"], record["error"]["stage"]) == ("failed", "INTAKE_INVALID", "download")
    assert record["error"]["details"] == {"sha256": R.sha256(PDF), "label": "0" * 64}
    [row] = table.items                                 # the RUNNING row, updated in place
    key = (row["organization_id"], row["timestamp"])
    assert writes(table) == [("PutItem", "RUNNING", key), ("UpdateItem", "FAILED", key)]
    assert_row(row, status="FAILED")
    assert data(row)["error_code"] == "INTAKE_INVALID"


# ---- every failure point: a failed run.json, uploaded last, and a failure row ----

def labels_missing(s3, monkeypatch):
    s3.metadata[KEY] = {}


def head_fails(s3, monkeypatch):
    s3.fail["HeadObject", KEY] = client_error(500, "InternalError", "HeadObject")


def download_fails(s3, monkeypatch):
    s3.fail["GetObject", KEY] = client_error(500, "InternalError", "GetObject")


def hash_mismatch(s3, monkeypatch):
    s3.metadata[KEY] = labels(sha256="0" * 64)


def asset_missing(s3, monkeypatch):
    s3.objects.pop(ASSETS + L.CATALOGUE, None)


def assets_changed(s3, monkeypatch):
    for name in L.ASSET_MANIFESTS:
        s3.objects[ASSETS + name] = b"changed"


def no_time_left(s3, monkeypatch):
    return {"remaining_ms": 60_000}


def model_refuses(s3, monkeypatch):
    return {"bedrock": lambda budget_s: FakeClient(error=client_error(403, "AccessDeniedException", "ConverseStream"))}


def pipeline_crashes(s3, monkeypatch):
    def prep(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(R, "prep", prep)


def upload_fails(s3, monkeypatch):
    s3.fail["PutObject", f"{RUNS}{RUN_ID}/report.md"] = client_error(500, "InternalError", "PutObject")


@pytest.mark.parametrize("setup,code,stage,started", [
    (labels_missing, "INTAKE_INVALID", "labels", False),
    (head_fails, "INTERNAL_ERROR", "labels", False),
    (download_fails, "INTERNAL_ERROR", "download", True),
    (hash_mismatch, "INTAKE_INVALID", "download", True),
    (asset_missing, "ASSET_INVALID", "assets", True),
    (assets_changed, "ASSET_INVALID", "assets", True),
    pytest.param(no_time_left, "MODEL_TIMEOUT", "run", True, marks=needs_assets),
    pytest.param(model_refuses, "MODEL_ERROR", "extract", True, marks=needs_assets),     # recorded by the pipeline
    pytest.param(pipeline_crashes, "INTERNAL_ERROR", "run", True, marks=needs_assets),
    pytest.param(upload_fails, "STORE_FAILED", "upload", True, marks=needs_assets),
], ids=lambda v: v.__name__ if callable(v) else None)
def test_every_failure_point_leaves_a_failed_run_json_and_a_failure_row(s3, monkeypatch, tmp_path, setup, code,
                                                                        stage, started):
    out, table = invoke(s3, **(setup(s3, monkeypatch) or {}))
    assert out == [{"run_id": RUN_ID, "status": "failed"}]
    keys = put_keys(s3)
    assert keys[0] == keys[-1] == RUN_JSON and keys.count(RUN_JSON) == 2
    record = stored(s3, "run.json")
    assert (record["status"], record["error"]["code"], record["error"]["stage"]) == ("failed", code, stage)
    assert {f"{RUNS}{RUN_ID}/{name}" for name in record["store"]["files"]} == set(keys)    # the record names what is there
    assert record["store"]["failed_at"] == ("report.md" if code == "STORE_FAILED" else None)
    [row] = table.items
    key = (row["organization_id"], row["timestamp"])
    assert writes(table) == ([("PutItem", "RUNNING", key), ("UpdateItem", "FAILED", key)] if started
                             else [("PutItem", "FAILED", key)])
    assert_row(row, status="FAILED", organization_id="org-1" if started else "_unknown",
               well_ids=("WELL 1",) if started else ())
    end = data(row)
    assert set(end) == {"request_id", "bucket", "key", "run_prefix", "deadline_at", "error_code", "stage", "message",
                        "stack_trace", *(["source_name"] if started else [])}
    assert (end["error_code"], end["stage"], end["message"], end["bucket"], end["key"]) == (
        code, stage, record["error"]["message"], BUCKET, KEY)
    assert (end["stack_trace"] is None) == (code == "MODEL_ERROR")    # the pipeline's own failures carry no trace
    assert_no_local_paths(s3, tmp_path)


# ---- success ----

@needs_assets
def test_a_success_uploads_the_folder_run_json_last_with_start_and_success_rows(s3, tmp_path, capsys):
    out, table = invoke(s3)
    assert out == [{"run_id": RUN_ID, "status": "succeeded"}]
    keys = put_keys(s3)
    assert keys[0] == keys[-1] == RUN_JSON and keys.count(RUN_JSON) == 2
    names = [key.removeprefix(f"{RUNS}{RUN_ID}/") for key in keys[1:]]
    assert sorted(names) == sorted(["run.json", "source.json", "prep_manifest.csv", "report.md",
                                    *(f"{g}.parquet" for g in R.GRAINS), "quarantine.jsonl",
                                    *(f"{n}.jsonl" for n in R.LISTS + R.LOGS)])
    record, created = stored(s3, "run.json"), json.loads(s3.puts[0][1])
    assert (created["status"], record["status"], record["store"]["files"]) == ("running", "succeeded", names)
    assert (record["started_at"], record["deadline_at"]) == (created["started_at"], created["deadline_at"])
    assert (record["organization_id"], record["well_id"], record["family"], record["rows"]["pump_config"]) == (
        "org-1", "WELL 1", "F4", 1)
    source = {"path": f"s3://{BUCKET}/{KEY}", "sha256": R.sha256(PDF)}
    assert record["source"]["pdf"] == stored(s3, "source.json")["pdf"] == source
    assert record["source"]["source_name"] == SOURCE_NAME
    [row] = table.items                                 # created RUNNING, updated SUCCEEDED on the same key
    key = (row["organization_id"], row["timestamp"])
    assert writes(table) == [("PutItem", "RUNNING", key), ("UpdateItem", "SUCCEEDED", key)]
    first = table.writes[0][1]
    assert set(first) == ROW and (first["status"], first["success"]) == ("RUNNING", False)
    assert_row(row, status="SUCCEEDED")
    assert data(row) == {"request_id": REQUEST_ID, "bucket": BUCKET, "key": KEY, "source_name": SOURCE_NAME,
                         "run_prefix": f"s3://{BUCKET}/{RUNS}{RUN_ID}/", "deadline_at": record["deadline_at"],
                         "route": "template", "family": "F4", "rows": record["rows"], "review": record["review"],
                         "cost_usd": record["cost_usd"]}
    assert record["cost_usd"] > 0
    assert_no_local_paths(s3, tmp_path)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {line["run_id"] for line in lines} == {RUN_ID}
    assert [line["stage"] for line in lines if line["stage"] != "run_log"] == [
        "create", "labels", "download", "assets", "run", "upload", "end"]


@needs_assets
def test_a_failed_run_log_write_is_logged_and_never_fails_the_run(s3, capsys):
    out, table = invoke(s3, table=FakeTable(error=client_error(400, "ValidationException", "PutItem")))
    assert out == [{"run_id": RUN_ID, "status": "succeeded"}] and stored(s3, "run.json")["status"] == "succeeded"
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["write"], line["status"]) for line in lines if "ValidationException" in (line.get("error") or "")] == [
        ("put", "RUNNING"), ("update", "SUCCEEDED")]
    assert table.items == []


@pytest.mark.parametrize("succeeds", [False, pytest.param(True, marks=needs_assets)])
def test_a_failed_start_write_still_ends_with_a_whole_row(s3, succeeds):
    if not succeeds:
        s3.metadata[KEY] = labels(sha256="0" * 64)
    _, table = invoke(s3, table=FakeTable(error=client_error(500, "InternalServerError", "PutItem"), fail=("PutItem",)))
    [row] = table.items
    assert [op for op, _, _ in writes(table)] == ["UpdateItem"]      # the PutItem failed; the update made the row
    assert_row(row, status="SUCCEEDED" if succeeds else "FAILED")


def test_a_create_that_fails_for_another_reason_writes_one_failed_row(s3):
    s3.fail["PutObject", RUN_JSON] = client_error(500, "InternalError", "PutObject")
    out, table = invoke(s3)
    assert out == [{"run_id": RUN_ID, "status": "failed"}]
    [row] = table.items
    assert [op for op, _, _ in writes(table)] == ["PutItem"] and row["end_timestamp"] == row["timestamp"]
    assert_row(row, status="FAILED", organization_id="_unknown", well_ids=())    # the labels are read after the create
    assert (data(row)["error_code"], data(row)["stage"]) == ("STORE_FAILED", "create")


@pytest.mark.parametrize("case", ["invalid key", "failed create", "bad labels", "hash mismatch",
                                  pytest.param("success", marks=needs_assets)])
def test_the_row_never_holds_the_event_the_metadata_a_token_or_input_json(s3, case):
    if case == "failed create":
        s3.fail["PutObject", RUN_JSON] = client_error(500, "InternalError", "PutObject")
    elif case == "bad labels":
        s3.metadata[KEY] = labels(**{"sha256": "abc"})
    elif case == "hash mismatch":
        s3.metadata[KEY] = labels(sha256="0" * 64)
    _, table = invoke(s3, event=s3_event(f"{INTAKE}report.pdf" if case == "invalid key" else KEY))
    assert table.writes
    for write in table.writes:
        attributes = write[-1]                          # PutItem: the item; UpdateItem: what it sets
        assert not {"token", "input_json", "input_event"} & (set(attributes) | set(json.loads(attributes["data_json"])))
    written = json.dumps(table.writes, default=str)
    raw = [value for value in labels().values() if value != unquote(value)]        # the labels as stored
    assert raw and all(leak not in written for leak in ("Records", "userIdentity", UPLOADER["principalId"],
                                                        UPLOADER_IP, *raw))


# ---- the arithmetic: budget, deadline, the row's formats ----

def test_the_model_budget_is_the_time_left_less_the_reserve_at_most_780_s():
    assert [L.model_budget(ms) for ms in (900_000, 870_000, 869_999, 500_000, 90_000)] == [780, 780, 779.999, 410, 0]


@needs_assets
def test_the_run_gets_that_budget_and_a_client_built_for_it(s3):
    built = []

    def bedrock(budget_s):
        built.append((budget_s, FakeClient(events(GOOD))))
        return built[-1][1]
    invoke(s3, remaining_ms=500_000, bedrock=bedrock)
    [(budget_s, client)] = built
    assert budget_s == 410 and stored(s3, "run.json")["budget_s"] == 410
    assert client.calls[0]["modelId"] == R.MODEL_ID


def test_run_json_is_created_running_with_the_invocation_deadline(s3, monkeypatch):
    monkeypatch.setattr(L, "now", lambda: datetime(2026, 10, 18, 12, 0, 0, 250_000, tzinfo=timezone.utc))
    s3.metadata[KEY] = {}          # stops at the labels, after the create
    invoke(s3, remaining_ms=899_500)
    created = json.loads(s3.puts[0][1])
    assert (created["run_id"], created["status"], created["started_at"], created["deadline_at"]) == (
        RUN_ID, "running", "2026-10-18T12:00:00Z", "2026-10-18T12:14:59Z")


def test_the_rows_timestamp_and_ids_are_the_file_upload_rows(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", FUNCTION)
    at = datetime(2026, 10, 18, 12, 0, 0, 123_456, tzinfo=timezone.utc)
    # get_standard_timestamp(): UTC isoformat with the offset cut off, so no fraction when it is zero.
    assert [L.standard_timestamp(t) for t in (at, at.replace(microsecond=0))] == [
        "2026-10-18T12:00:00.123456", "2026-10-18T12:00:00"]
    assert L.standard_timestamp(at) == at.isoformat().split("+")[0]
    assert L.log_row("org-1", RUN_ID, "WELL 1", "RUNNING", {"b": 1, "a": None}, at) == {
        "organization_id": "org-1", "timestamp": "2026-10-18T12:00:00.123456", "type": "PIPELINE_EXECUTION",
        "organization_id_type": "org-1#PIPELINE_EXECUTION", "pipeline_execution_id": RUN_ID, "uuid": RUN_ID,
        "resource_name": FUNCTION, "well_ids": ["WELL 1"], "status": "RUNNING", "success": False,
        "data_json": '{"a": null, "b": 1}'}
    adhoc = L.log_row(None, f"ADHOC#{REQUEST_ID}", None, "FAILED", {}, at)
    assert (adhoc["organization_id"], adhoc["organization_id_type"], adhoc["pipeline_execution_id"], adhoc["uuid"],
            adhoc["well_ids"]) == ("_unknown", "_unknown#PIPELINE_EXECUTION", f"ADHOC#{REQUEST_ID}",
                                   f"ADHOC#{REQUEST_ID}", [])


def test_the_fake_table_refuses_what_dynamodb_would():
    table, key = FakeTable(), {"organization_id": "org-1", "timestamp": "2026-10-18T12:00:00.123456"}
    for expression, names, values, refusal in [
        ("SET status = :s", {}, {":s": "FAILED"}, "reserved keyword"),
        ("SET #o = :o", {"#o": "organization_id"}, {":o": "org-2"}, "part of the key"),
        ("SET #t = :t", {"#t": "timestamp"}, {":t": "later"}, "part of the key"),
        ("SET #s = :s", {"#s": "status", "#x": "type"}, {":s": "FAILED"}, "unused"),
    ]:
        with pytest.raises(ClientError, match=refusal):
            table.update_item(Key=key, UpdateExpression=expression, ExpressionAttributeNames=names,
                              ExpressionAttributeValues=values)
    assert table.items == []
    table.update_item(Key=key, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": "FAILED"})
    assert table.items == [{**key, "status": "FAILED"}]
