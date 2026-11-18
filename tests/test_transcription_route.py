"""The image route past its reader, offline: the request a run that fails IMAGE_ONLY makes, the starter, the task with
its supervisor and its hand-back, and the watcher.  S3 and DynamoDB are test_lambda's fakes, the table with the one
condition the starter puts under.  ECS is a real client under botocore's Stubber, which checks each RunTask against
the service's own model and sends nothing.  Bedrock is test_image_route's stub, and the scans are its synthetic ones.
The supervisor's children are real processes that sleep or exit.  No AWS, no model, no network.
"""

import copy
import dataclasses
import hashlib
import io
import itertools
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import quote

import boto3
import pytest
from botocore.stub import Stubber
from pypdf import PdfReader, PdfWriter

import app.transcription as T
import lambda_function as L
import test_image_route as TI
import test_lambda as TL
from extraction.bedrock import MODEL_ID
from extraction.errors import ExtractionError
from extraction.intake import decode_labels, encode_labels, intake_run_id, optional_labels
from image_route import converse, handback, render, task
from image_route.settings import PREFIX, PROFILE_ENV
from test_transcription_skill import transcribe

BUCKET, INTAKE, RUNS, RUN_ID, KEY = TL.BUCKET, TL.INTAKE, TL.RUNS, TL.RUN_ID, TL.KEY
TRANSCRIPTIONS = "esp-design-extraction/transcriptions/"
ASKED = "20261018T120005Z"                           # the request's id: the second it was made
AT = datetime(2026, 10, 18, 12, 0, 5, tzinfo=timezone.utc)
REQUEST_KEY, RESULT_KEY = (f"{TRANSCRIPTIONS}{RUN_ID}/{ASKED}/{name}.json" for name in ("request", "result"))
ORG, WELL, SOURCE = "org-1", "WELL 1", "Désign scan 1.pdf"
TYPED_NAME = "Désign scan 1 - transcription.pdf"     # as the skill's build names it
# The row's key, worked out here as the starter's docstring says: the request's second, and microseconds from the hash.
MICROSECOND = 1 + int(hashlib.sha256(f"{RUN_ID}/{ASKED}".encode()).hexdigest()[:8], 16) % 999_999
ROW_KEY = {"organization_id": ORG, "timestamp": f"2026-10-18T12:00:05.{MICROSECOND:06d}"}
STARTER, WATCHER, TASK = (f"esp-design-extraction-transcription-{part}" for part in ("start", "watch", "task"))
CLUSTER, TASK_DEFINITION, CONTAINER = "esp-design-extraction", "esp-design-extraction-transcription:7", "transcription"
ARN = "arn:aws:ecs:us-east-1:123456789012:task/esp-design-extraction/0123456789abcdef0123456789abcdef"
STARTED = {"tasks": [{"taskArn": ARN, "lastStatus": "PROVISIONING"}], "failures": []}
NO_CAPACITY = {"tasks": [], "failures": [{"reason": "RESOURCE:ENI", "detail": "none left in the subnet"}]}
FRESH = ["20261018T130000Z-aaaaaaaa", "20261018T130000Z-bbbbbbbb", "20261018T130000Z-cccccccc"]


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    """The stack's settings for every part; no function name, which only a Lambda has (`named`)."""
    for name in [n for n in os.environ if n.startswith((PREFIX, L.TASK_ENV))] + ["AWS_LAMBDA_FUNCTION_NAME"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in {
            "ESP_BUCKET": BUCKET, "ESP_INTAKE_PREFIX": INTAKE, "ESP_RUNS_PREFIX": RUNS, "ESP_ASSETS_PREFIX": TL.ASSETS,
            L.TRANSCRIPTIONS_ENV: TRANSCRIPTIONS, "ESP_RUN_LOG_TABLE": "esp_design_extraction_logging",
            PROFILE_ENV: MODEL_ID,
            "ESP_TRANSCRIPTION_CLUSTER": CLUSTER, "ESP_TRANSCRIPTION_TASK_DEFINITION": TASK_DEFINITION,
            "ESP_TRANSCRIPTION_CONTAINER": CONTAINER, "ESP_TRANSCRIPTION_SUBNETS": "subnet-0aaa1111, subnet-0bbb2222",
            "ESP_TRANSCRIPTION_SECURITY_GROUPS": "sg-0ccc3333", "ESP_TRANSCRIPTION_TASK_NAME": TASK}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(L, "WORK", tmp_path / "tmp" / "esp-design-extraction")
    # Detect fails a scan before any asset is read, so the tests need no asset.
    monkeypatch.setattr(L, "fetch_assets", lambda *args, **kwargs: None)

    def refuse(*_a, **_k):
        raise AssertionError("a test reached for a real Bedrock client")
    monkeypatch.setattr(converse, "make_client", refuse)


def named(function):
    """As inside the Lambda of that name."""
    return mock.patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_NAME": function})


def ticking(monkeypatch, at=AT):
    """The handlers' clock: `at`, a microsecond later at each reading, so no two rows are dated alike."""
    ticks = itertools.count()
    monkeypatch.setattr(L, "now", lambda: at + timedelta(microseconds=next(ticks)))


class Table(TL.FakeTable):
    """test_lambda's table, which also takes the starter's condition: the row must not be there."""

    def put_item(self, *, Item, ConditionExpression=None):
        assert ConditionExpression in (None, "attribute_not_exists(organization_id)")
        if ConditionExpression and tuple(Item[k] for k in self.KEY) in self.rows:
            raise TL.client_error(400, "ConditionalCheckFailedException", "PutItem")
        super().put_item(Item=Item)


class Ecs:
    """An ECS client that sends nothing: botocore's Stubber answers each RunTask in turn (a dict, or an error's code)
    after checking its parameters against the service's model.  `calls` holds each RunTask's parameters."""

    def __init__(self, *answers):
        self.client = boto3.client("ecs", region_name="us-east-1", aws_access_key_id="testing",
                                   aws_secret_access_key="testing")
        self.stubber, self.calls = Stubber(self.client), []
        self.client.meta.events.register_first("before-parameter-build.ecs.RunTask",
                                               lambda params, **_: self.calls.append(copy.deepcopy(params)))
        for answer in answers:
            if isinstance(answer, str):
                self.stubber.add_client_error("run_task", answer, "the test's refusal", http_status_code=500)
            else:
                self.stubber.add_response("run_task", answer)
        self.stubber.activate()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stored(s3, key):
    return json.loads(s3.objects[key])


def under(s3, prefix):
    return sorted(key for key in s3.objects if key.startswith(prefix))


def uploaded(s3, tmp_path, *, pages=2, **optional) -> bytes:
    """A scan at intake, with its labels: an upload the extraction has not run yet."""
    scan = TI.scan(tmp_path, pages, "scan.pdf").read_bytes()
    s3.objects[KEY] = scan
    s3.metadata[KEY] = {**encode_labels(ORG, WELL, SOURCE, sha(scan)),
                        **{name: quote(value, safe="") for name, value in optional.items()}}
    return scan


def original(s3, tmp_path, *, pages=2, error="IMAGE_ONLY", status="failed", **optional) -> bytes:
    """The original run as the extraction leaves it: the scan at intake with its labels, and its failed run.json."""
    scan = uploaded(s3, tmp_path, pages=pages, **optional)
    s3.objects[f"{RUNS}{RUN_ID}/run.json"] = L.dump({
        "run_id": RUN_ID, "status": status, "organization_id": ORG, "well_id": WELL,
        "error": error and {"code": error, "stage": "detect", "message": "page 1 has no usable text layer"}})
    return scan


def request(**changes) -> dict:
    return {"run_id": RUN_ID, "request_id": ASKED, "made_at": "2026-10-18T12:00:05Z", "made_by": "extraction",
            "organization_id": ORG, "well_id": WELL, **changes}


def extract(s3, table=None, key=KEY, **kwargs):
    """The extraction Lambda on one intake object: (what it returns, the table)."""
    with named(TL.FUNCTION):
        return TL.invoke(s3, table=table or Table(), event=TL.s3_event(key), **kwargs)


def start(s3, table, ecs, key=REQUEST_KEY, sleeps=None):
    with named(STARTER):
        return L.transcription_start_handler(TL.s3_event(key), TL.Context(120_000), s3=s3, table=table, ecs=ecs.client,
                                             sleep=([] if sleeps is None else sleeps).append)


def watch(s3, table, event):
    with named(WATCHER):
        return L.transcription_watch_handler(event, TL.Context(60_000), s3=s3, table=table)


def passed(deadline_at="2026-10-18T13:00:05Z") -> list:
    """The four variables the starter passes the task, as RunTask and ECS's events carry them."""
    return [{"name": L.TASK_ENV + name, "value": value}
            for name, value in zip(L.PASSED, (RUN_ID, ASKED, json.dumps(ROW_KEY), deadline_at))]


def running(monkeypatch, table, *, variables=None) -> task.Job:
    """The request as the starter leaves it: its row RUNNING, and the task's four variables set.  The deadline is an
    hour from now unless `variables` gives another."""
    with named(STARTER):
        table.put_item(Item=L.log_row(ORG, RUN_ID, WELL, "RUNNING", {"request_key": REQUEST_KEY},
                                      L.transcription_row_at(RUN_ID, ASKED)))
    for variable in variables or passed(L.utc(datetime.now(timezone.utc) + timedelta(hours=1))):
        monkeypatch.setenv(variable["name"], variable["value"])
    return task.job()


def typing(page, turn, attempt):
    """The model types each page at once; the cache is written."""
    return TI.reply(TI.write(), cache=(0, 5000))


def do(job, s3, table, answer=typing):
    """The work, with Bedrock stubbed: (its exit code, the stub)."""
    stub = TI.Stub(answer)
    return task.work(job, s3=s3, table=table, client=stub, sleep=lambda seconds: None), stub


def assert_ended(s3, table, *, by, code, stage, status="FAILED"):
    """The request ended once: its result.json, create-only, and its one row, updated in place.  Returns both."""
    result, [row] = stored(s3, RESULT_KEY), table.items
    assert s3.headers[RESULT_KEY]["IfNoneMatch"] == "*"
    assert {k: result[k] for k in ("run_id", "request_id", "status", "ended_by", "error_code", "stage")} == {
        "run_id": RUN_ID, "request_id": ASKED, "status": status, "ended_by": by, "error_code": code, "stage": stage}
    assert set(row) == TL.ENDED and {name: row[name] for name in L.ROW_KEY} == ROW_KEY
    assert (row["type"], row["organization_id_type"]) == ("PIPELINE_EXECUTION", f"{ORG}#PIPELINE_EXECUTION")
    assert (row["pipeline_execution_id"], row["uuid"], row["well_ids"]) == (RUN_ID, RUN_ID, [WELL])
    assert (row["status"], row["success"], row["resource_name"]) == (status, status == "SUCCEEDED", by)
    assert row["end_timestamp"] >= row["timestamp"]
    end = TL.data(row)
    assert (end.get("error_code"), end.get("stage"), end["request_key"]) == (code, stage, REQUEST_KEY)
    assert end.get("message") == result["message"]
    return result, row


# ---- the row's key ----

def test_the_rows_key_follows_from_the_request_alone_in_the_run_logs_timestamp_form():
    at = L.transcription_row_at(RUN_ID, ASKED)
    assert at == AT.replace(microsecond=MICROSECOND) and 1 <= MICROSECOND <= 999_999
    assert L.transcription_row_key(ORG, RUN_ID, ASKED) == ROW_KEY
    assert L.transcription_row_key(None, RUN_ID, ASKED)["organization_id"] == "_unknown"
    with named(STARTER):                                   # the key is what log_row writes for that time, today
        row = L.log_row(ORG, RUN_ID, WELL, "RUNNING", {}, at)
    assert {name: row[name] for name in L.ROW_KEY} == ROW_KEY and tuple(ROW_KEY) == L.ROW_KEY == Table.KEY
    assert ROW_KEY["timestamp"] == L.standard_timestamp(at) == at.isoformat().split("+")[0]
    # Unique per request: another run, or another second, is another key; the fraction is always written.
    keys = {L.transcription_row_key(ORG, run, asked)["timestamp"]
            for run in (RUN_ID, "20261018T120000Z-0123abce") for asked in (ASKED, "20261018T120006Z")}
    assert len(keys) == 4 and all(len(key) == 26 for key in keys)
    assert L.transcription_ids(REQUEST_KEY) == (RUN_ID, ASKED)
    for key in (RESULT_KEY, f"{TRANSCRIPTIONS}{RUN_ID}/request.json", f"{TRANSCRIPTIONS}{RUN_ID}/{RUN_ID}/request.json",
                f"{TRANSCRIPTIONS}{RUN_ID}/20261340T000000Z/request.json", f"x/{RUN_ID}/{ASKED}/request.json"):
        assert L.transcription_ids(key) is None, key
    assert len(f"{RUN_ID}/{ASKED}") == 42                  # within startedBy's 128 and the client token's 64


# ---- the request ----

@pytest.fixture
def s3():
    return TL.Pushes()


def test_a_run_that_fails_image_only_asks_for_its_transcription_after_its_own_records(s3, tmp_path, monkeypatch,
                                                                                      capsys):
    ticking(monkeypatch)
    uploaded(s3, tmp_path)
    out, table = extract(s3)
    assert out == [{"run_id": RUN_ID, "status": "failed"}]
    assert stored(s3, TL.RUN_JSON)["error"]["code"] == "IMAGE_ONLY"
    assert TL.put_keys(s3)[-2:] == [TL.RUN_JSON, REQUEST_KEY]            # after the run's record, and last
    assert s3.headers[REQUEST_KEY]["IfNoneMatch"] == "*"
    assert stored(s3, REQUEST_KEY) == request()
    assert L.REQUEST_ID.fullmatch(ASKED) and L.RUN_ID.fullmatch(f"{ASKED}-0123abcd")    # the run id's own second
    [row] = table.items                                                  # the run's row, FAILED as today: no other
    assert [op for op, _, _ in TL.writes(table)] == ["PutItem", "UpdateItem"]
    assert (row["status"], TL.data(row)["error_code"]) == ("FAILED", "IMAGE_ONLY")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["stage"] for line in lines][-2:] == ["end", "transcription_request"]
    assert lines[-1] == {"run_id": RUN_ID, "stage": "transcription_request", "key": REQUEST_KEY}


@pytest.mark.parametrize("case,code", [("hash mismatch", "INTAKE_INVALID"), ("not a PDF", "PREP_FAILED")])
def test_no_request_for_another_failure(s3, tmp_path, case, code):
    uploaded(s3, tmp_path)
    if case == "hash mismatch":
        s3.metadata[KEY]["sha256"] = "0" * 64
    else:
        s3.objects[KEY] = b"not a pdf"
        s3.metadata[KEY]["sha256"] = sha(b"not a pdf")
    _, table = extract(s3)
    assert stored(s3, TL.RUN_JSON)["error"]["code"] == code
    assert under(s3, TRANSCRIPTIONS) == [] and len(table.items) == 1


def test_no_request_for_an_upload_that_is_itself_a_transcription(s3, tmp_path):
    uploaded(s3, tmp_path)
    s3.metadata[KEY][L.TRANSCRIBED_FROM] = "20261017T090000Z-89abcdef"
    _, table = extract(s3)
    assert stored(s3, TL.RUN_JSON)["error"]["code"] == "IMAGE_ONLY"
    assert under(s3, TRANSCRIPTIONS) == [] and len(table.items) == 1


def test_no_request_while_the_stack_names_no_place_for_them(s3, tmp_path, monkeypatch):
    monkeypatch.delenv(L.TRANSCRIPTIONS_ENV)
    uploaded(s3, tmp_path)
    _, table = extract(s3)
    assert stored(s3, TL.RUN_JSON)["error"]["code"] == "IMAGE_ONLY"
    assert TL.put_keys(s3)[-1] == TL.RUN_JSON and len(table.items) == 1


def test_a_request_that_cannot_be_put_gets_a_failed_row_of_its_own(s3, tmp_path, monkeypatch):
    ticking(monkeypatch)
    uploaded(s3, tmp_path)
    s3.fail["PutObject", REQUEST_KEY] = TL.client_error(500, "InternalError", "PutObject")
    out, table = extract(s3)
    assert out == [{"run_id": RUN_ID, "status": "failed"}] and under(s3, TRANSCRIPTIONS) == []
    run_row, own = table.items
    assert [(op, status) for op, status, _ in TL.writes(table)] == [("PutItem", "RUNNING"), ("UpdateItem", "FAILED"),
                                                                    ("PutItem", "FAILED")]
    assert TL.data(run_row)["error_code"] == "IMAGE_ONLY"                # the run's row, as it was
    assert (own["organization_id"], own["timestamp"]) != (run_row["organization_id"], run_row["timestamp"])
    with named(TL.FUNCTION):
        TL.assert_row(own, status="FAILED")                              # today's fields, the run's ids
    assert own["end_timestamp"] == own["timestamp"]
    assert {k: TL.data(own)[k] for k in ("error_code", "stage", "request_key", "key")} == {
        "error_code": "TRANSCRIPTION_REQUEST_FAILED", "stage": "request", "request_key": REQUEST_KEY, "key": KEY}
    assert "InternalError" in TL.data(own)["message"] and "Traceback" in TL.data(own)["stack_trace"]
    assert TL.put_keys(s3)[-1] == TL.RUN_JSON                            # the run's files were not written again


def test_a_repeat_asks_once(s3, tmp_path, monkeypatch, capsys):
    ticking(monkeypatch)
    uploaded(s3, tmp_path)
    _, table = extract(s3)
    puts = TL.put_keys(s3)
    again, _ = extract(s3, table)                                        # the event again: the run is there
    assert again == [{"run_id": RUN_ID, "status": "duplicate"}]
    assert TL.put_keys(s3) == puts and under(s3, TRANSCRIPTIONS) == [REQUEST_KEY] and len(table.items) == 1


def test_a_request_already_there_is_left_and_no_row_is_written(s3, tmp_path, monkeypatch, capsys):
    ticking(monkeypatch)
    uploaded(s3, tmp_path)
    s3.objects[REQUEST_KEY] = b"the first request"
    _, table = extract(s3)
    assert s3.objects[REQUEST_KEY] == b"the first request" and len(table.items) == 1
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last == {"run_id": RUN_ID, "stage": "transcription_request", "key": REQUEST_KEY, "duplicate": True}


# ---- the starter ----

def run_task(deadline_at="2026-10-18T13:00:05Z") -> dict:
    return {"cluster": CLUSTER, "taskDefinition": TASK_DEFINITION, "launchType": "FARGATE", "count": 1,
            "startedBy": f"{RUN_ID}/{ASKED}", "clientToken": f"{RUN_ID}/{ASKED}",
            "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-0aaa1111", "subnet-0bbb2222"],
                                                             "securityGroups": ["sg-0ccc3333"],
                                                             "assignPublicIp": "DISABLED"}},
            "overrides": {"containerOverrides": [{"name": CONTAINER, "environment": passed(deadline_at)}]}}


@pytest.fixture
def asked(s3):
    s3.objects[REQUEST_KEY] = L.dump(request())
    return s3


def test_a_request_starts_one_task_with_its_overrides_started_by_and_network(asked, monkeypatch, capsys):
    ticking(monkeypatch)
    table, ecs = Table(), Ecs(STARTED)
    out = start(asked, table, ecs)
    assert out == [{"run_id": RUN_ID, "request_id": ASKED, "status": "started", "task_arn": ARN}]
    assert ecs.calls == [run_task()]                                     # the deadline: this start plus an hour
    ecs.stubber.assert_no_pending_responses()
    [row] = table.items
    assert TL.writes(table) == [("PutItem", "RUNNING", tuple(ROW_KEY.values()))]
    assert set(row) == TL.ROW and {name: row[name] for name in L.ROW_KEY} == ROW_KEY
    assert (row["pipeline_execution_id"], row["uuid"], row["well_ids"], row["resource_name"], row["success"]) == (
        RUN_ID, RUN_ID, [WELL], STARTER, False)
    assert TL.data(row) == {"request_id": TL.REQUEST_ID, "request_key": REQUEST_KEY, "made_by": "extraction",
                            "deadline_at": "2026-10-18T13:00:05Z"}
    assert RESULT_KEY not in asked.objects and asked.puts == []          # the task ends it
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["stage"] for line in lines] == ["run_log", "start"] and lines[-1]["task_arn"] == ARN


def test_the_settings_shape_the_deadline_and_the_network(asked, monkeypatch):
    ticking(monkeypatch)
    monkeypatch.setenv("ESP_TRANSCRIPTION_CAP_S", "1800")
    monkeypatch.setenv("ESP_TRANSCRIPTION_ASSIGN_PUBLIC_IP", "ENABLED")
    monkeypatch.delenv("ESP_TRANSCRIPTION_SECURITY_GROUPS")
    ecs = Ecs(STARTED)
    start(asked, Table(), ecs)
    [call] = ecs.calls
    assert call["networkConfiguration"] == {"awsvpcConfiguration": {
        "subnets": ["subnet-0aaa1111", "subnet-0bbb2222"], "assignPublicIp": "ENABLED"}}
    assert call["overrides"]["containerOverrides"][0]["environment"] == passed("2026-10-18T12:30:05Z")


def test_a_repeated_event_starts_no_second_task(asked, monkeypatch):
    table, ecs = Table(), Ecs(STARTED)
    first = start(asked, table, ecs)
    rows = copy.deepcopy(table.items)
    again = start(asked, table, ecs)
    assert first[0]["status"] == "started"
    assert again == [{"run_id": RUN_ID, "request_id": ASKED, "status": "duplicate"}]
    assert len(ecs.calls) == 1 and table.items == rows and RESULT_KEY not in asked.objects


def test_a_start_that_fails_is_tried_again_as_the_same_task(asked):
    table, ecs, sleeps = Table(), Ecs("ServerException", NO_CAPACITY, STARTED), []
    out = start(asked, table, ecs, sleeps=sleeps)
    assert out[0]["status"] == "started" and sleeps == [2, 4]
    assert len(ecs.calls) == 3 and ecs.calls[0] == ecs.calls[1] == ecs.calls[2]     # one client token: one task
    assert [status for _, status, _ in TL.writes(table)] == ["RUNNING"] and RESULT_KEY not in asked.objects


def test_a_task_that_cannot_be_started_ends_the_request(asked, monkeypatch):
    monkeypatch.setenv("ESP_TRANSCRIPTION_START_TRIES", "2")
    table, ecs, sleeps = Table(), Ecs("ServerException", NO_CAPACITY), []
    out = start(asked, table, ecs, sleeps=sleeps)
    assert out == [{"run_id": RUN_ID, "request_id": ASKED, "status": "failed"}]
    assert len(ecs.calls) == 2 and sleeps == [2]
    result, row = assert_ended(asked, table, by=STARTER, code="TRANSCRIPTION_START_FAILED", stage="start")
    assert result["message"] == ("the task could not be started in 2 tries; the last: RESOURCE:ENI (none left in the "
                                 "subnet)")
    assert result["new_run_id"] is None
    assert TL.writes(table) == [("PutItem", "RUNNING", tuple(ROW_KEY.values())),
                                ("UpdateItem", "FAILED", tuple(ROW_KEY.values()))]
    assert start(asked, table, Ecs())[0]["status"] == "duplicate"        # and the event again changes nothing
    assert stored(asked, RESULT_KEY) == result and table.items == [row]


@pytest.mark.parametrize("body,why", [
    (b"{not json", "JSONDecodeError"),
    (L.dump(request(run_id="20261018T120000Z-ffffffff")), "does not name this run and this request"),
    (L.dump(request(organization_id=" ")), "with an organisation and a well"),
    (L.dump([request()]), "does not name this run"),
    (None, "AccessDenied"),
], ids=["not JSON", "another run's", "no organisation", "not an object", "not there"])
def test_a_request_that_cannot_be_read_is_ended_and_starts_nothing(s3, body, why):
    if body is not None:
        s3.objects[REQUEST_KEY] = body
    table, ecs = Table(), Ecs()
    assert start(s3, table, ecs)[0]["status"] == "failed" and ecs.calls == []
    result, [row] = stored(s3, RESULT_KEY), table.items
    assert (result["error_code"], result["stage"]) == ("TRANSCRIPTION_START_FAILED", "request")
    assert why in result["message"]
    assert set(row) == TL.ENDED and (row["organization_id"], row["timestamp"]) == ("_unknown", ROW_KEY["timestamp"])
    assert (row["status"], row["uuid"], row["resource_name"]) == ("FAILED", RUN_ID, STARTER)


def test_a_key_that_is_no_request_gets_one_failed_row(s3):
    key = f"{TRANSCRIPTIONS}{RUN_ID}/request.json"
    table, ecs = Table(), Ecs()
    assert start(s3, table, ecs, key=key) == [{"key": key, "status": "rejected"}]
    [row] = table.items
    assert ecs.calls == [] and s3.calls == [] and row["uuid"] == f"ADHOC#{TL.REQUEST_ID}"
    assert TL.data(row) == {"request_id": TL.REQUEST_ID, "request_key": key, "stack_trace": None,
                            "error_code": "TRANSCRIPTION_START_FAILED", "stage": "key",
                            "message": f"the key is not {TRANSCRIPTIONS}<run id>/<request id>/request.json"}


def test_a_row_that_cannot_be_put_ends_the_request_and_starts_nothing(asked):
    table = Table(error=TL.client_error(500, "InternalServerError", "PutItem"), fail=("PutItem",))
    ecs = Ecs()
    assert start(asked, table, ecs)[0]["status"] == "failed" and ecs.calls == []
    result, row = assert_ended(asked, table, by=STARTER, code="TRANSCRIPTION_START_FAILED", stage="row")
    assert [op for op, _, _ in TL.writes(table)] == ["UpdateItem"]       # the update made the row whole


def test_a_missing_setting_raises_before_anything_is_written(asked, monkeypatch):
    monkeypatch.delenv("ESP_TRANSCRIPTION_SUBNETS")
    table, ecs = Table(), Ecs()
    with pytest.raises(KeyError, match="ESP_TRANSCRIPTION_SUBNETS"):
        start(asked, table, ecs)
    assert table.writes == [] and asked.puts == [] and ecs.calls == []


# ---- the task: the work ----

def test_the_work_reads_the_scan_and_hands_its_transcription_back(s3, tmp_path, monkeypatch):
    scan = original(s3, tmp_path)
    table = Table()
    job = running(monkeypatch, table)
    assert "AWS_LAMBDA_FUNCTION_NAME" not in os.environ                  # Fargate has none
    code, stub = do(job, s3, table)
    assert code == 0 and len(stub.calls) == 2
    assert all(call["system"][-1] == {"cachePoint": {"type": "default"}} for call in stub.calls)   # on by default

    [new_key] = [key for key in under(s3, INTAKE) if key != KEY]         # the hand-back: one upload of its own
    new_run_id = intake_run_id(INTAKE, new_key)
    assert L.RUN_ID.fullmatch(new_run_id) and new_run_id != RUN_ID
    assert s3.headers[new_key] == {"ContentType": "application/pdf", "IfNoneMatch": "*"}
    typed = s3.objects[new_key]
    assert T.recognise(typed) == T.Scan(name=SOURCE, pages=2, sha256=sha(scan), unsure=(1, 2), original=scan)
    assert s3.metadata[new_key] == {**encode_labels(ORG, WELL, TYPED_NAME, sha(typed)), "transcribed-from": RUN_ID}
    assert decode_labels(s3.metadata[new_key]) == {"organization-id": ORG, "well-id": WELL, "source-name": TYPED_NAME,
                                                   "sha256": sha(typed)}
    assert optional_labels(s3.metadata[new_key]) == {}                   # no environment stays none

    result, row = assert_ended(s3, table, by=TASK, code=None, stage=None, status="SUCCEEDED")
    assert TL.writes(table) == [("PutItem", "RUNNING", tuple(ROW_KEY.values())),
                                ("UpdateItem", "SUCCEEDED", tuple(ROW_KEY.values()))]
    assert result["new_run_id"] == new_run_id and result["stack_trace"] is None
    assert result["transcription"] == {"key": new_key, "source_name": TYPED_NAME, "sha256": sha(typed),
                                       "bytes": len(typed)}
    assert [set(page) for page in result["pages"]] == [set(task.PAGE)] * 2
    assert [(p["page"], p["status"], p["calls"], p["input_tokens"], p["output_tokens"], p["cache_write_tokens"])
            for p in result["pages"]] == [(1, "ok", 1, 1000, 200, 5000), (2, "ok", 1, 1000, 200, 5000)]
    assert all(p["wall_s"] >= 0 and p["cost_usd"] == 0.033 for p in result["pages"])     # the cache write priced
    assert result["total"]["cost_usd"] == result["cost_usd"] == 0.066 and result["total"]["pages"] == 2
    end = TL.data(row)
    assert set(end) == {"request_key", "deadline_at", "new_run_id", "transcription", "pages", "total", "cost_usd"}
    assert (end["pages"], end["total"], end["new_run_id"]) == (result["pages"], result["total"], new_run_id)
    assert s3.puts[-1][0] == RESULT_KEY and TL.put_keys(s3) == [new_key, RESULT_KEY]     # nothing else was written


def test_what_the_task_puts_passes_the_extractions_own_label_checks(s3, tmp_path, monkeypatch):
    original(s3, tmp_path)
    do(running(monkeypatch, Table()), s3, Table())
    [new_key] = [key for key in under(s3, INTAKE) if key != KEY]
    new_run_id, got = intake_run_id(INTAKE, new_key), {}

    def pipeline(**kwargs):                                              # reached only past the labels and the hash
        got.update(kwargs, text_layer=kwargs["pdf"].read_bytes() == s3.objects[new_key])
        raise ExtractionError("MODEL_ERROR", "the test stops here")
    monkeypatch.setattr(L, "run", pipeline)
    out, table = extract(s3, key=new_key)
    assert out == [{"run_id": new_run_id, "status": "failed"}]
    assert (got["organization_id"], got["well_id"], got["source_name"], got["run_id"], got["text_layer"]) == (
        ORG, WELL, TYPED_NAME, new_run_id, True)
    record = stored(s3, f"{RUNS}{new_run_id}/run.json")
    assert (record["error"]["code"], record["error"]["stage"]) == ("MODEL_ERROR", "run")     # not INTAKE_INVALID
    assert record["source"]["pdf"]["sha256"] == sha(s3.objects[new_key])
    assert under(s3, TRANSCRIPTIONS) == [RESULT_KEY]                     # and it asks for nothing


def test_the_originals_optional_labels_are_carried_and_only_the_valid_ones(s3, tmp_path, monkeypatch):
    carried = {"origin": "product-upload", "environment": "dev", "uploaded-by": "engineer 7@example.com",
               "pushed-from": "s3://a-bucket/dev/org-1/well-documents/WELL 1/esp_design/a.pdf?versionId=v1"}
    original(s3, tmp_path, **carried)
    s3.metadata[KEY]["another-key"] = "kept out"
    do(running(monkeypatch, Table()), s3, Table())
    [new_key] = [key for key in under(s3, INTAKE) if key != KEY]
    assert optional_labels(s3.metadata[new_key]) == carried
    assert set(s3.metadata[new_key]) == {*decode_labels(s3.metadata[new_key]), *carried, "transcribed-from"}

    labels = handback.labels({**s3.metadata[KEY], "environment": "qa"}, TYPED_NAME, "0" * 64, RUN_ID)
    assert "environment" not in labels and optional_labels(labels) == {k: v for k, v in carried.items()
                                                                       if k != "environment"}
    with pytest.raises(ExtractionError, match="INTAKE_INVALID"):         # the original's required labels must decode
        handback.labels({**s3.metadata[KEY], "well-id": "%FF"}, TYPED_NAME, "0" * 64, RUN_ID)


def test_the_cache_points_can_be_turned_off(s3, tmp_path, monkeypatch):
    monkeypatch.setenv(PREFIX + "CACHE", "0")
    original(s3, tmp_path, pages=1)
    code, stub = do(running(monkeypatch, Table()), s3, Table())
    assert code == 0 and json.dumps(stub.calls, default=str).count("cachePoint") == 0


@pytest.mark.parametrize("status,error,said", [("succeeded", None, "succeeded"),
                                               ("failed", "PREP_FAILED", "failed, PREP_FAILED"),
                                               ("running", None, "running")])
def test_an_original_that_did_not_fail_image_only_is_not_transcribed(s3, tmp_path, monkeypatch, status, error, said):
    original(s3, tmp_path, status=status, error=error)
    table = Table()
    code, stub = do(running(monkeypatch, table), s3, table)
    assert code == 1 and stub.calls == [] and under(s3, INTAKE) == [KEY]
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_FAILED", stage="original")
    assert result["message"] == f"the run did not fail IMAGE_ONLY: its run.json says {said}" and "pages" not in result


@pytest.mark.parametrize("gone", [KEY, TL.RUN_JSON], ids=["the scan", "its run.json"])
def test_an_original_that_cannot_be_read_fails_at_the_original(s3, tmp_path, monkeypatch, gone):
    original(s3, tmp_path)
    del s3.objects[gone]
    table = Table()
    code, stub = do(running(monkeypatch, table), s3, table)
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_FAILED", stage="original")
    assert code == 1 and stub.calls == []
    assert "AccessDenied" in result["message"] and "Traceback" in result["stack_trace"]


def test_a_failed_page_fails_the_transcription_and_what_it_cost_is_kept(s3, tmp_path, monkeypatch):
    original(s3, tmp_path)
    table = Table()
    code, _ = do(running(monkeypatch, table), s3, table, lambda page, turn, attempt: typing(page, turn, attempt)
                 if page == 1 else TI.reply({"text": "I could not read it."}, stop="end_turn"))
    assert code == 1 and under(s3, INTAKE) == [KEY]                      # nothing handed back
    result, row = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_FAILED", stage="pages")
    assert result["message"] == ("nothing built: page 2: the model ended without write_page (stop reason end_turn)")
    assert [(p["status"], p["cost_usd"] > 0) for p in result["pages"]] == [("ok", True), ("failed", True)]
    assert result["total"]["pages_failed"] == 1 and TL.data(row)["pages"] == result["pages"]
    assert result["cost_usd"] == TL.data(row)["cost_usd"] == pytest.approx(0.033 + 0.008)


def test_a_refused_build_fails_at_the_build(s3, tmp_path, monkeypatch):
    build = render.scripts(TI.SKILL_DIR).B

    def refuse(*args, **kwargs):
        raise build.Refused("nothing built, as the pages folder does not hold the scan's pages")
    monkeypatch.setattr(build, "build", refuse)
    original(s3, tmp_path)
    table = Table()
    code, stub = do(running(monkeypatch, table), s3, table)
    assert code == 1 and len(stub.calls) == 2 and under(s3, INTAKE) == [KEY]
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_FAILED", stage="build")
    assert result["message"].startswith("nothing built, as the pages folder") and result["total"]["pages_failed"] == 0


@pytest.mark.parametrize("built,why", [("the scan again", "Detect finds no text layer in the transcription"),
                                       ("a PDF with no mark", "does not carry the transcription mark")])
def test_a_transcription_that_fails_a_check_is_not_put(s3, tmp_path, monkeypatch, built, why):
    scan = original(s3, tmp_path)
    body = scan if built == "the scan again" else TL.PDF

    def build(pdf, model, *, pages=None, out=None, now=None):            # the build's file, as either check refuses it
        (out / "built - transcription.pdf").write_bytes(body)
        return out / "built - transcription.pdf"
    monkeypatch.setattr(render.scripts(TI.SKILL_DIR).B, "build", build)
    table = Table()
    code, _ = do(running(monkeypatch, table), s3, table)
    assert code == 1 and under(s3, INTAKE) == [KEY] and TL.put_keys(s3) == [RESULT_KEY]
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_FAILED", stage="check")
    assert why in result["message"] and len(result["pages"]) == 2


def test_the_two_checks(tmp_path):
    (tmp_path / "made").mkdir()
    scan, typed = (path.read_bytes() for path in transcribe(tmp_path / "made", ["PUMP DATA\n  Model   SF2700"]))
    assert handback.problem(typed) is None
    assert handback.problem(scan).startswith("Detect finds no text layer in the transcription: page 1 has no usable")
    assert handback.problem(TL.PDF) == "the built PDF does not carry the transcription mark"
    reader, writer, unattached = PdfReader(io.BytesIO(typed)), PdfWriter(), io.BytesIO()
    writer.add_page(reader.pages[0])
    writer.add_metadata({T.MARK: reader.metadata[T.MARK]})
    writer.write(unattached)
    assert handback.problem(unattached.getvalue()) == ("the built PDF is a broken transcription: the original scan is "
                                                       "not attached to it")


def test_a_key_that_is_another_uploads_is_left_and_a_fresh_id_is_taken(s3, tmp_path, monkeypatch):
    original(s3, tmp_path, pages=1)
    fresh = iter(FRESH)
    monkeypatch.setattr(handback, "new_run_id", lambda now: next(fresh))
    s3.objects[f"{INTAKE}{FRESH[0]}.pdf"], s3.metadata[f"{INTAKE}{FRESH[0]}.pdf"] = b"another upload", {"well-id": "x"}
    table = Table()
    code, _ = do(running(monkeypatch, table), s3, table)
    result, _ = assert_ended(s3, table, by=TASK, code=None, stage=None, status="SUCCEEDED")
    assert code == 0 and result["new_run_id"] == FRESH[1]
    assert s3.objects[f"{INTAKE}{FRESH[0]}.pdf"] == b"another upload" and f"{INTAKE}{FRESH[2]}.pdf" not in s3.objects


def test_fresh_ids_that_are_all_taken_fail_at_the_put(s3, tmp_path, monkeypatch):
    original(s3, tmp_path, pages=1)
    fresh = iter(FRESH)
    monkeypatch.setattr(handback, "new_run_id", lambda now: next(fresh))
    for run_id in FRESH:
        s3.objects[f"{INTAKE}{run_id}.pdf"] = b"another upload"
    table = Table()
    code, _ = do(running(monkeypatch, table), s3, table)
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_FAILED", stage="put")
    assert code == 1 and "3 fresh run ids were each taken" in result["message"] and len(result["pages"]) == 1


def test_a_key_found_with_this_puts_own_labels_is_this_put(s3, tmp_path, monkeypatch):
    """The put landed, its answer was lost, and the SDK's second try met the object: one upload, not two."""
    scan = original(s3, tmp_path, pages=1)
    fresh, key = iter(FRESH), f"{INTAKE}{FRESH[0]}.pdf"
    monkeypatch.setattr(handback, "new_run_id", lambda now: next(fresh))
    s3.objects[key], s3.metadata[key] = b"the transcription", handback.labels(s3.metadata[KEY], TYPED_NAME,
                                                                             sha(b"the transcription"), RUN_ID)
    assert handback.put(s3, BUCKET, INTAKE, b"the transcription", s3.metadata[KEY], TYPED_NAME, RUN_ID) == FRESH[0]
    assert s3.puts == [] and under(s3, INTAKE) == sorted([KEY, key])


def test_a_put_that_fails_ends_the_request_with_the_pages_account(s3, tmp_path, monkeypatch):
    original(s3, tmp_path, pages=1)
    monkeypatch.setattr(handback, "new_run_id", lambda now: FRESH[0])
    s3.fail["PutObject", f"{INTAKE}{FRESH[0]}.pdf"] = TL.client_error(500, "InternalError", "PutObject")
    table = Table()
    code, _ = do(running(monkeypatch, table), s3, table)
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_FAILED", stage="put")
    assert code == 1 and "InternalError" in result["message"] and result["cost_usd"] == 0.033
    assert under(s3, INTAKE) == [KEY]


def test_past_the_soft_limit_no_page_is_started(s3, tmp_path, monkeypatch):
    original(s3, tmp_path)
    table = Table()
    job = dataclasses.replace(running(monkeypatch, table), soft=datetime.now(timezone.utc) - timedelta(seconds=1))
    code, stub = do(job, s3, table)
    assert code == 1 and stub.calls == [] and under(s3, INTAKE) == [KEY]
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_TIMEOUT", stage="pages")
    assert "had passed before a page was started" in result["message"] and "pages" not in result


def test_at_the_soft_limit_the_running_call_finishes_and_no_other_starts(s3, tmp_path, monkeypatch):
    monkeypatch.setenv(PREFIX + "CONCURRENCY", "1")                      # page 2 waits for page 1
    original(s3, tmp_path)
    table = Table()
    job = dataclasses.replace(running(monkeypatch, table), soft=datetime.now(timezone.utc) + timedelta(seconds=2))

    def slow(page, turn, attempt):
        while datetime.now(timezone.utc) <= job.soft:                    # page 1's call outlasts the soft limit
            time.sleep(0.05)
        return typing(page, turn, attempt)
    code, stub = do(job, s3, table, slow)
    assert code == 1 and len(stub.calls) == 1 and under(s3, INTAKE) == [KEY]
    result, row = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_TIMEOUT", stage="pages")
    assert result["message"].startswith(f"the soft limit, {L.utc(job.soft)}, passed with pages unread; nothing built: "
                                        "page 2: the deadline passed before the call could start")
    assert [(p["status"], p["calls"]) for p in result["pages"]] == [("ok", 1), ("failed", 0)]
    assert result["cost_usd"] == TL.data(row)["cost_usd"] == 0.033       # the page that was read is still paid for


def test_the_limits_are_counted_back_from_the_deadline_by_margins_that_can_be_set(monkeypatch):
    job = running(monkeypatch, Table(), variables=passed("2026-10-18T13:00:05Z"))
    assert (L.utc(job.soft), L.utc(job.hard)) == ("2026-10-18T12:50:05Z", "2026-10-18T12:58:05Z")   # 50 and 58 minutes
    assert (job.run_id, job.request_id, job.row, job.by) == (RUN_ID, ASKED, ROW_KEY, TASK)
    assert job.data == {"request_key": REQUEST_KEY, "deadline_at": "2026-10-18T13:00:05Z"}
    monkeypatch.setenv("ESP_TRANSCRIPTION_SOFT_MARGIN_S", "900")
    monkeypatch.setenv("ESP_TRANSCRIPTION_HARD_MARGIN_S", "30.5")
    monkeypatch.delenv("ESP_TRANSCRIPTION_TASK_NAME")
    job = task.job()
    assert (L.utc(job.soft), job.hard.isoformat(), job.by) == (
        "2026-10-18T12:45:05Z", "2026-10-18T12:59:34.500000+00:00", task.NAME)


@pytest.mark.parametrize("name,value", [("RUN_ID", "20261018T120000Z"), ("REQUEST_ID", "yesterday"),
                                        ("ROW_KEY", '{"organization_id": "org-1"}'), ("ROW_KEY", "{not json"),
                                        ("DEADLINE_AT", "in an hour"), ("DEADLINE_AT", None)])
def test_a_task_started_without_its_four_variables_raises_and_ends_nothing(monkeypatch, name, value):
    running(monkeypatch, Table())
    if value is None:
        monkeypatch.delenv(L.TASK_ENV + name)
    else:
        monkeypatch.setenv(L.TASK_ENV + name, value)
    with pytest.raises((KeyError, ValueError)):
        task.job()


# ---- the task: the supervisor ----

def child(code: str) -> list:
    return [sys.executable, "-c", code]


def test_a_hung_child_is_killed_at_the_hard_limit_and_the_request_ended(s3, monkeypatch):
    table = Table()
    job = dataclasses.replace(running(monkeypatch, table), hard=datetime.now(timezone.utc) + timedelta(seconds=1))
    began = time.monotonic()
    code = task.supervise(job, s3=s3, table=table, command=child("import time; time.sleep(120)"))
    assert code == 1 and 0.9 < time.monotonic() - began < 60             # killed; it would have slept two minutes
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_TIMEOUT", stage="supervisor")
    assert result["message"] == (f"the work was still running at the hard limit, {L.utc(job.hard)}, and was killed")
    assert "pages" not in result                                         # a kill loses the pages' account


def test_a_child_that_is_past_the_hard_limit_at_its_start_is_killed_at_once(s3, monkeypatch):
    table = Table()
    job = dataclasses.replace(running(monkeypatch, table), hard=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert task.supervise(job, s3=s3, table=table, command=child("import time; time.sleep(120)")) == 1
    assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_TIMEOUT", stage="supervisor")


def test_a_crashed_child_ends_the_request_with_its_exit_code(s3, monkeypatch):
    table = Table()
    job = running(monkeypatch, table)
    assert task.supervise(job, s3=s3, table=table, command=child("raise SystemExit(7)")) == 1
    result, _ = assert_ended(s3, table, by=TASK, code="TRANSCRIPTION_FAILED", stage="supervisor")
    assert result["message"] == "the work ended with exit code 7, and had not ended the request"


def test_a_child_that_ended_the_request_itself_is_left_as_it_ended(s3, monkeypatch):
    table = Table()
    job = running(monkeypatch, table)
    s3.objects[RESULT_KEY] = b"the work's own ending"
    rows = copy.deepcopy(table.items)
    assert task.supervise(job, s3=s3, table=table, command=child("raise SystemExit(1)")) == 1
    assert s3.objects[RESULT_KEY] == b"the work's own ending" and table.items == rows and s3.puts == []


def test_a_child_that_succeeds_is_the_tasks_success_and_the_supervisor_writes_nothing(s3, monkeypatch):
    table = Table()
    job = running(monkeypatch, table)
    assert task.supervise(job, s3=s3, table=table, command=child("pass")) == 0
    assert s3.puts == [] and len(table.writes) == 1


def test_the_command_is_the_supervisor_and_with_work_the_work(monkeypatch):
    job, seen = running(monkeypatch, Table()), []
    monkeypatch.setattr(task, "boto3", types.SimpleNamespace(
        client=lambda name: f"the {name} client",
        resource=lambda name: types.SimpleNamespace(Table=lambda table: f"the table {table}")))
    monkeypatch.setattr(task, "work", lambda job, **how: seen.append(("work", job, how)) or 0)
    monkeypatch.setattr(task, "supervise", lambda job, **how: seen.append(("supervise", job, how)) or 1)
    assert (task.main([]), task.main(["work"])) == (1, 0)
    how = {"s3": "the s3 client", "table": "the table esp_design_extraction_logging"}
    assert seen == [("supervise", job, how), ("work", job, how)]


def test_the_supervisors_child_is_the_work_in_this_python(monkeypatch):
    job, started = running(monkeypatch, Table()), []
    monkeypatch.setattr(task.subprocess, "Popen", lambda command: started.append(command) or types.SimpleNamespace(
        wait=lambda timeout: 0))
    assert task.supervise(job, s3=None, table=None) == 0
    assert started == [[sys.executable, "-m", "image_route.task", "work"]]


# ---- the watcher ----

def stopped(*, overrides=True, started_by=f"{RUN_ID}/{ASKED}", last="STOPPED", variables=None) -> dict:
    """ECS's "Task State Change" for one of the route's tasks, as EventBridge delivers it: the task, as ECS
    describes one."""
    variables = passed() if variables is None else variables
    return {"version": "0", "detail-type": "ECS Task State Change", "source": "aws.ecs", "detail": {
        "taskArn": ARN, "lastStatus": last, "desiredStatus": "STOPPED", "launchType": "FARGATE",
        "startedBy": started_by, "stopCode": "EssentialContainerExited",
        "stoppedReason": "Essential container in task exited",
        "containers": [{"name": CONTAINER, "lastStatus": "STOPPED", "exitCode": 137,
                        "reason": "OutOfMemoryError: Container killed due to memory usage"}],
        "overrides": {"containerOverrides": [{"name": CONTAINER,
                                              **({"environment": variables} if overrides else {})}]}}}


def test_a_task_that_stopped_without_ending_its_request_is_ended_by_the_watcher(asked, monkeypatch):
    table = Table()
    running(monkeypatch, table)
    assert watch(asked, table, stopped()) == {"run_id": RUN_ID, "request_id": ASKED, "status": "ended"}
    result, row = assert_ended(asked, table, by=WATCHER, code="TRANSCRIPTION_TASK_STOPPED", stage="task")
    assert result["message"] == ("the task stopped without ending the request: Essential container in task exited "
                                 "(transcription exit code 137)")
    ecs_said = {"task_arn": ARN, "stop_code": "EssentialContainerExited",
                "stopped_reason": "Essential container in task exited",
                "containers": [{"name": CONTAINER, "exit_code": 137,
                                "reason": "OutOfMemoryError: Container killed due to memory usage"}]}
    assert {k: result[k] for k in ecs_said} == ecs_said == {k: TL.data(row)[k] for k in ecs_said}
    assert TL.data(row)["deadline_at"] == "2026-10-18T13:00:05Z" and TL.data(row)["request_id"] == TL.REQUEST_ID
    assert asked.calls == [("PutObject", RESULT_KEY)]                    # found from the overrides: nothing was read


def test_a_stop_with_a_result_changes_nothing(asked, monkeypatch):
    table = Table()
    running(monkeypatch, table)
    asked.objects[RESULT_KEY] = b"the task's own ending"
    rows = copy.deepcopy(table.items)
    assert watch(asked, table, stopped())["status"] == "ended already"
    assert watch(asked, table, stopped(overrides=False))["status"] == "ended already"
    assert asked.objects[RESULT_KEY] == b"the task's own ending" and table.items == rows and asked.puts == []


@pytest.mark.parametrize("event", [
    stopped(overrides=False),
    stopped(variables=passed()[:2]),                                     # the row's key is not carried
    stopped(variables=[v if not v["name"].endswith("ROW_KEY") else {**v, "value": "{not json"} for v in passed()]),
    stopped(variables=[v if not v["name"].endswith("RUN_ID") else {**v, "value": "../elsewhere"} for v in passed()]),
], ids=["no overrides", "no row key", "a row key that is not JSON", "a run id that is not one"])
def test_without_the_overrides_the_request_and_its_row_are_found_from_started_by(asked, monkeypatch, event):
    table = Table()
    running(monkeypatch, table)
    assert watch(asked, table, event)["status"] == "ended"
    assert_ended(asked, table, by=WATCHER, code="TRANSCRIPTION_TASK_STOPPED", stage="task")     # the same row
    assert asked.calls == [("GetObject", REQUEST_KEY), ("PutObject", RESULT_KEY)]


@pytest.mark.parametrize("event", [stopped(last="RUNNING"), stopped(last="DEPROVISIONING"), {},
                                   stopped(overrides=False, started_by=None),
                                   stopped(overrides=False, started_by="ecs-svc/0123456789012345678"),
                                   stopped(overrides=False, started_by=f"{RUN_ID}/{ASKED}/more")],
                         ids=["running", "deprovisioning", "no detail", "no startedBy", "a service's", "three parts"])
def test_an_event_that_is_no_stop_or_names_no_request_is_ignored(asked, monkeypatch, event):
    table = Table()
    running(monkeypatch, table)
    assert watch(asked, table, event) == {"status": "ignored"}
    assert asked.calls == [] and len(table.writes) == 1


# ---- a request, end to end ----

def test_a_scan_goes_from_its_failed_run_to_a_new_run_of_its_transcription(s3, tmp_path, monkeypatch):
    """Each part handed what the part before it really made: the request, the overrides, the upload."""
    scan = TI.scan(tmp_path, 1, "scan.pdf").read_bytes()
    s3.objects[KEY], s3.metadata[KEY] = scan, {**encode_labels(ORG, WELL, SOURCE, sha(scan)), "environment": "prd"}
    table, ecs = Table(), Ecs(STARTED)
    extract(s3, table)                                                   # the run fails IMAGE_ONLY and asks
    [request_key] = under(s3, TRANSCRIPTIONS)
    start(s3, table, ecs, key=request_key)                               # the starter runs the task
    [override] = ecs.calls[0]["overrides"]["containerOverrides"]
    for variable in override["environment"]:                             # the task, with what the starter passed
        monkeypatch.setenv(variable["name"], variable["value"])
    code, _ = do(task.job(), s3, table)
    assert code == 0
    run_id, asked_at = L.transcription_ids(request_key)
    result = stored(s3, L.transcription_key(run_id, asked_at, "result.json"))
    new_key = f"{INTAKE}{result['new_run_id']}.pdf"
    assert (result["status"], run_id) == ("SUCCEEDED", RUN_ID) and T.recognise(s3.objects[new_key]).original == scan
    assert optional_labels(s3.metadata[new_key]) == {"environment": "prd"}
    event = stopped()
    event["detail"].update(startedBy=ecs.calls[0]["startedBy"], overrides=ecs.calls[0]["overrides"])
    assert watch(s3, table, event)["status"] == "ended already"          # the task's normal stop
    run_row, request_row = sorted(table.items, key=lambda row: row["resource_name"])
    assert (run_row["status"], run_row["resource_name"]) == ("FAILED", TL.FUNCTION)
    assert (request_row["status"], request_row["resource_name"], request_row["uuid"]) == ("SUCCEEDED", TASK, RUN_ID)
    assert len({(row["organization_id"], row["timestamp"]) for row in table.items}) == 2
