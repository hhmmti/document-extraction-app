"""The app's S3 mode, offline.  S3 is test_lambda's fake, extended with what the app calls (a listing,
two keys a page so that paging runs, and uploads with their content type and labels).  Where a run's
own records are needed, the Lambda handler writes them into the same fake.  No AWS, no process."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.intake as intake
import app.store as S
import extraction.run as R
import lambda_function as L
import test_lambda as TL
from app.wells import WELL_LIST_ENV
from extraction import ExtractionError
from extraction.assets_dir import ASSETS_ENV
from extraction.intake import LABELS, RUN_ID, decode_labels, encode_labels, intake_key, intake_run_id
from test_bedrock import FakeClient, events
from test_intake import WELLS, app
from test_run import GOOD
from test_viewer import FULL, PDF, PDF_SHA, record, texts

BUCKET, INTAKE, RUNS = TL.BUCKET, TL.INTAKE, TL.RUNS
ORG, WELL = "Org Alpha", "Well  #1"            # test_intake's invented ids, the form's first choices below
COST = ("This calls Opus on Bedrock in the datascience account; recent runs cost about USD 0.55–1.32 and "
        "take 3–8 minutes.")
needs_assets = pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"the pipeline reads the assets; set {ASSETS_ENV}")


def rid(n: int) -> str:
    return f"20990101T0000{n:02d}Z-000000{n:02d}"


class FakeS3(TL.FakeS3):
    """test_lambda's fake, plus a listing (two keys a page) and uploads with their content type, labels and
    condition.  Every object has a LastModified."""

    def __init__(self):
        super().__init__()
        self.modified, self.headers = {}, {}

    def add(self, key, body, *, age=timedelta(0), metadata=None):
        """An object as if put `age` ago."""
        self.objects[key] = body.encode() if isinstance(body, str) else body
        self.modified[key] = datetime.now(timezone.utc) - age
        self.metadata[key] = metadata or {}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, ContentType=None, Metadata=None):
        super().put_object(Bucket=Bucket, Key=Key, Body=Body, IfNoneMatch=IfNoneMatch)
        self.modified[Key], self.metadata[Key] = datetime.now(timezone.utc), dict(Metadata or {})
        self.headers[Key] = {"ContentType": ContentType, "IfNoneMatch": IfNoneMatch}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken="0"):
        self._call("ListObjectsV2", Bucket, Prefix)
        keys, start = sorted(k for k in self.objects if k.startswith(Prefix)), int(ContinuationToken)
        page = {"Contents": [{"Key": k, "LastModified": self.modified[k]} for k in keys[start:start + 2]],
                "IsTruncated": start + 2 < len(keys)}
        return {**page, "NextContinuationToken": str(start + 2)} if page["IsTruncated"] else page


def no_process(*args, **kwargs):
    raise AssertionError("S3 mode starts no process")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """The app in S3 mode on a fake bucket, with test_intake's well list; the store is built afresh."""
    (tmp_path / "wells.csv").write_text(WELLS)
    for name, value in {S.MODE_ENV: "s3", "ESP_BUCKET": BUCKET, "ESP_INTAKE_PREFIX": INTAKE,
                        "ESP_RUNS_PREFIX": RUNS, WELL_LIST_ENV: str(tmp_path / "wells.csv")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(S.STORE_ENV, raising=False)
    bucket = FakeS3()
    monkeypatch.setattr(S, "s3_client", lambda: bucket)
    monkeypatch.setattr(S, "_S3_STORES", {})
    monkeypatch.setattr(intake.subprocess, "Popen", no_process)
    return bucket


@pytest.fixture
def handler(fake, tmp_path, monkeypatch):
    """The Lambda handler, called on the fake bucket as S3 calls it for one upload; returns the run-log table."""
    assets = os.environ.get(ASSETS_ENV)
    for name, value in {"ESP_ASSETS_PREFIX": TL.ASSETS, "ESP_RUN_LOG_TABLE": "esp_design_extraction_logging",
                        "ESP_INFERENCE_PROFILE_ID": R.MODEL_ID, "AWS_LAMBDA_FUNCTION_NAME": TL.FUNCTION}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(ASSETS_ENV, assets or "")             # the handler repoints it; restored after the test
    monkeypatch.setattr(L, "WORK", tmp_path / "tmp" / "esp-design-extraction")

    def handle(run_id, *, with_assets=False):
        for name in L.ASSET_MANIFESTS if with_assets else ():
            fake.add(TL.ASSETS + name, (Path(assets) / name).read_bytes())
        table = TL.FakeTable()
        L.lambda_handler(TL.s3_event(intake_key(INTAKE, run_id)), TL.Context(900_000), s3=fake, table=table,
                         bedrock=lambda budget_s: FakeClient(events(GOOD)))
        return table
    return handle


def upload(fake, run_id, *, age=timedelta(minutes=1), well=WELL, metadata=None, pdf=PDF):
    """An upload as the form makes it, put `age` ago."""
    labels = encode_labels(ORG, well, "doc.pdf", R.sha256(pdf)) if metadata is None else metadata
    fake.add(intake_key(INTAKE, run_id), pdf, age=age, metadata=labels)


def stored_run(fake, run_id, rec, files=None):
    for name, data in {"run.json": json.dumps(rec), **(files or {})}.items():
        fake.add(f"{RUNS}{run_id}/{name}", data)


def as_running(fake, handle, run_id, *, stale=False):
    """The run as the handler leaves it while it runs: its first run.json, which names no labels yet."""
    handle(run_id)                                  # no assets in the bucket: it fails; its first put is the record
    key = f"{RUNS}{run_id}/run.json"
    first = json.loads(next(body for k, body in fake.puts if k == key))
    fake.add(key, json.dumps({**first, **({"deadline_at": "2000-01-01T00:00:00Z"} if stale else {})}))
    return first


def form(name="doc.pdf"):
    at = app(upload="1")
    at.selectbox(key="new-org").select_index(1).run()
    at.selectbox(key="new-well").select_index(0).run()
    at.file_uploader(key="new-pdf").upload(name, PDF, "application/pdf").run()
    return at


def start_button(at):
    return next(b for b in at.button if b.label == "Start the run")


def markdown(at) -> str:
    return " ".join(texts(at.markdown))


# ---- the label codec, shared with the handler ----

def test_labels_round_trip_and_the_key_and_run_id_rules_are_the_handlers():
    metadata = encode_labels(ORG, WELL, "Désign report 1.pdf", PDF_SHA)
    assert list(metadata) == list(LABELS) and all(v.isascii() for v in metadata.values())
    assert decode_labels(metadata) == {"organization-id": ORG, "well-id": WELL, "source-name": "Désign report 1.pdf",
                                       "sha256": PDF_SHA}
    assert RUN_ID.pattern == R.RUN_ID.pattern
    assert intake_run_id(INTAKE, intake_key(INTAKE, rid(1))) == rid(1)
    assert [intake_run_id(INTAKE, key) for key in (f"{INTAKE}report.pdf", f"{INTAKE}x/{rid(1)}.pdf",
                                                    f"{RUNS}{rid(1)}.pdf")] == [None, None, None]


@pytest.mark.parametrize("values,bad", [
    ((ORG, "  ", "doc.pdf", PDF_SHA), ["well-id"]),
    ((ORG, WELL, "x/doc.pdf", PDF_SHA), ["source-name"]),
    (("Org\tAlpha", WELL, "doc.pdf", "ABC"), ["organization-id", "sha256"]),
])
def test_encoding_refuses_what_the_handler_would(values, bad):
    with pytest.raises(ExtractionError) as err:
        encode_labels(*values)
    assert (err.value.code, err.value.details) == ("INTAKE_INVALID", {"labels": bad})


# ---- upload ----

def test_the_forms_checks_stay_and_nothing_is_sent_unconfirmed(fake):
    at = form()
    assert COST in texts(at.warning) and start_button(at).disabled
    for name, content, message in (("a.pdf", b"", "empty"), ("b.pdf", b"not a pdf", "does not start with %PDF-")):
        at.file_uploader(key="new-pdf").upload(name, content, "application/pdf").run()
        assert any(message in e for e in texts(at.error)) and start_button(at).disabled
    assert not fake.puts


def test_an_upload_has_the_key_type_and_labels_and_the_handler_accepts_them(fake, handler):
    at = form("Désign report 1.pdf")
    at.checkbox(key="new-confirm").check().run()
    start_button(at).click().run()
    [key] = [k for k, _ in fake.puts]
    run_id = intake_run_id(INTAKE, key)
    assert run_id and fake.objects[key] == PDF
    assert fake.headers[key] == {"ContentType": "application/pdf", "IfNoneMatch": "*"}
    assert fake.metadata[key] == encode_labels(ORG, WELL, "Désign report 1.pdf", PDF_SHA)
    assert any(i.startswith("Queued for") for i in texts(at.info))              # now on the watch view
    operation, first = handler(run_id).writes[0]     # the handler's own validation: its RUNNING row follows it
    assert (operation, first["status"], first["organization_id"], first["well_ids"]) == ("PutItem", "RUNNING", ORG, [WELL])
    assert json.loads(first["data_json"])["source_name"] == "Désign report 1.pdf"


# ---- the viewer: a pipeline record and the handler's failure record ----

def test_the_viewer_reads_a_pipeline_record_and_its_source_from_s3(fake):
    run_id = rid(1)
    source = {"pdf": {"path": f"s3://{BUCKET}/{intake_key(INTAKE, run_id)}", "sha256": PDF_SHA}, "report": None,
              "source_name": "doc.pdf"}
    upload(fake, run_id, age=timedelta(minutes=9))
    stored_run(fake, run_id, record(run_id, source=source), {**FULL, "source.json": json.dumps(source)})
    at = app(run=run_id)
    assert "Pipeline: completed" in markdown(at) and "Document review: 1 item to check" in markdown(at)
    assert not at.error and not [i for i in texts(at.info) if "not stored" in i]
    assert len(at.image) == 1 and "Page 1 of 2" in at.image[0].proto.imgs[0].caption


@pytest.mark.parametrize("labelled,code,stage", [(True, "ASSET_INVALID", "assets"), (False, "INTAKE_INVALID", "labels")])
def test_the_viewer_reads_the_handlers_failure_record(fake, handler, labelled, code, stage):
    run_id = rid(2)
    upload(fake, run_id, metadata=None if labelled else {})
    handler(run_id)                                      # no assets in the bucket: it fails before the pipeline
    at = app(run=run_id)
    assert f"Pipeline: failed at {stage}" in markdown(at)
    assert texts(at.error)[0].startswith(f"**{code}** at {stage}: ")
    assert any("using the copy in run.json" in c for c in texts(at.caption))   # it stored no source.json
    if labelled:                                         # the upload, read from S3 and hash-checked
        assert len(at.image) == 1
    else:                                                # no SHA-256 to check it against: the location, as text
        assert "Source not in store" in texts(at.warning) and f"s3://{BUCKET}/{intake_key(INTAKE, run_id)}" in texts(at.code)


@needs_assets
def test_a_run_the_handler_completes_renders_in_full(fake, handler):
    run_id = rid(3)
    upload(fake, run_id, pdf=TL.PDF)
    handler(run_id, with_assets=True)
    at = app(run=run_id)
    assert "Pipeline: completed" in markdown(at) and not at.error
    assert len(at.image) == 1 and not [i for i in texts(at.info) if "not stored" in i]


# ---- states ----

def test_queue_state_thresholds():
    assert intake.queue_state(intake.PICKUP_S, False) == "queued"
    assert intake.queue_state(intake.PICKUP_S + 1, False) == "not picked up"
    assert intake.queue_state(intake.PICKUP_S + 1, True) == "queued"            # behind a running run
    assert intake.queue_state(intake.EXPIRY_S + 1, True) == intake.queue_state(intake.EXPIRY_S + 1, False) == "expired"


def test_the_list_shows_each_state(fake, handler):
    done, failed, stale, queued, late, expired = (rid(n) for n in range(1, 7))
    for run_id in (done, failed, stale):
        upload(fake, run_id, age=timedelta(hours=1), metadata={} if run_id == failed else None)
    stored_run(fake, done, record(done))
    handler(failed)                                                             # INTAKE_INVALID at labels
    as_running(fake, handler, stale, stale=True)
    upload(fake, queued, age=timedelta(minutes=2))
    upload(fake, late, age=timedelta(minutes=10))
    upload(fake, expired, age=timedelta(hours=7))
    fake.add(f"{INTAKE}report.pdf", PDF)                                        # not <run_id>.pdf: ignored
    fake.add(f"{INTAKE}sub/{rid(9)}.pdf", PDF)
    df = app().dataframe[0].value.set_index("Run id")
    assert list(df.index) == [expired, late, queued, stale, failed, done]
    assert df.loc[[done, failed, stale], "Pipeline"].tolist() == ["completed", "failed at labels", "failed (stale)"]
    assert df.loc[failed, "Error"] == "INTAKE_INVALID at labels"
    assert df.loc[[queued, late, expired], "Pipeline"].tolist() == ["queued", "not picked up", "expired"]
    assert df.loc[[queued, late, expired], "Waited"].tolist() == ["2 min", "10 min", "7 h 0 min"]
    assert df.loc[[queued, stale], "Organisation"].tolist() == [ORG, ORG]      # from the uploads' labels


def test_with_a_run_running_a_long_wait_is_still_queued(fake, handler):
    running, late = rid(1), rid(2)
    upload(fake, running, age=timedelta(minutes=12))
    as_running(fake, handler, running)
    upload(fake, late, age=timedelta(minutes=10))
    df = app().dataframe[0].value.set_index("Run id")
    assert df.loc[running, ["Pipeline", "Well"]].tolist() == ["running", WELL]
    assert df.loc[late, "Pipeline"] == "queued"
    metrics = {m.label: m.value for m in app(run=running).metric}            # its page names them the same way
    assert (metrics["Organisation"], metrics["Well"]) == (ORG, WELL)


@pytest.mark.parametrize("age,running,element,shown", [
    (timedelta(minutes=1), False, "info", "Queued for 1 min"),
    (timedelta(minutes=10), True, "info", "Queued for 10 min"),
    (timedelta(minutes=10), False, "warning", "Not picked up: queued for 10 min"),
    (timedelta(hours=7), False, "error", "Expired: queued for 7 h 0 min"),
])
def test_the_watch_view_shows_an_uploads_wait(fake, handler, age, running, element, shown):
    run_id = rid(5)
    upload(fake, run_id, age=age)
    if running:
        upload(fake, rid(1), age=timedelta(minutes=12))
        as_running(fake, handler, rid(1))
    messages = [t for t in texts(getattr(app(watch=run_id), element)) if t.startswith(shown)]
    assert len(messages) == 1
    if element == "warning":
        assert "run log" in messages[0] and "CloudWatch" in messages[0]


def test_the_watch_view_follows_an_upload_to_its_final_record(fake):
    run_id = rid(5)
    upload(fake, run_id)
    at = app(watch=run_id)
    assert any(i.startswith("Queued for") for i in texts(at.info))
    stored_run(fake, run_id, {"run_id": run_id, "status": "running", "started_at": R.utc(datetime.now(timezone.utc)),
                              "deadline_at": "2999-01-01T00:00:00Z"})
    at.run()
    assert "Pipeline: running" in markdown(at)
    stored_run(fake, run_id, record(run_id))
    at.run()
    assert "Pipeline: completed" in markdown(at)
    next(b for b in at.button if b.label == "Open the run").click().run()
    assert [t.label for t in at.tabs][:1] == ["Summary"]                       # the run's own page


# ---- the duplicate warning ----

@pytest.mark.parametrize("case,warned", [
    ("running", "is still running"),
    ("queued", "is waiting for the function (queued)"),
    ("not picked up", "is waiting for the function (not picked up)"),
    ("expired", None),
    ("stale", None),
    ("another well", None),
])
def test_the_duplicate_warning_reads_running_records_and_waiting_uploads(fake, handler, case, warned):
    run_id = rid(1)
    age = {"not picked up": timedelta(minutes=10), "expired": timedelta(hours=7)}.get(case, timedelta(minutes=1))
    upload(fake, run_id, age=age, well="well #2" if case == "another well" else WELL)
    if case in ("running", "stale"):
        assert as_running(fake, handler, run_id, stale=case == "stale")["organization_id"] is None
    mine = [w for w in texts(form().warning) if run_id in w]
    if warned is None:
        assert mine == []
    else:
        assert len(mine) == 1 and warned in mine[0]


# ---- the store: cached reads, its prefixes, its configuration ----

def test_reads_are_cached_until_refresh_which_keeps_final_runs(fake, handler):
    done, running = rid(1), rid(2)
    for run_id in (done, running):
        upload(fake, run_id, age=timedelta(minutes=3))
    stored_run(fake, done, record(done))
    as_running(fake, handler, running)
    fake.calls.clear()
    at = app()
    seen = len(fake.calls)
    at.run()                                                                    # a rerun lists and reads nothing
    assert len(fake.calls) == seen
    at.sidebar.button[0].click().run()
    again = fake.calls[seen:]
    assert ("ListObjectsV2", RUNS) in again and ("ListObjectsV2", INTAKE) in again
    assert ("GetObject", f"{RUNS}{running}/run.json") in again
    assert ("GetObject", f"{RUNS}{done}/run.json") not in again                # final: kept


def test_the_store_reads_only_inside_its_prefixes(fake):
    run_id = rid(1)
    location = f"s3://{BUCKET}/{intake_key(INTAKE, run_id)}"
    upload(fake, run_id)
    stored_run(fake, run_id, record(run_id), {"copy.pdf": PDF})                # the same bytes, outside intake/
    store = S.s3_store_from_env()
    assert store.source_pdf(PDF_SHA, location) == PDF and store.source_pdf("0" * 64, location) is None
    for elsewhere in (f"s3://other-bucket/{intake_key(INTAKE, run_id)}", f"s3://{BUCKET}/{RUNS}{run_id}/copy.pdf",
                      "/nowhere/recorded.pdf", None):
        assert store.source_pdf(PDF_SHA, elsewhere) is None
    assert ("GetObject", f"{RUNS}{run_id}/copy.pdf") not in fake.calls
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            store.read(run_id, bad)
    with pytest.raises(FileNotFoundError):
        store.read(run_id, "report.md")                                         # not listed: never fetched
    assert ("GetObject", f"{RUNS}{run_id}/report.md") not in fake.calls


def test_an_unknown_mode_is_refused_and_s3_needs_its_variables(fake, monkeypatch):
    monkeypatch.setenv(S.MODE_ENV, "aws")
    assert any("must be local or s3" in e for e in texts(app().error))
    monkeypatch.setenv(S.MODE_ENV, "s3")
    monkeypatch.setenv("ESP_RUNS_PREFIX", RUNS.rstrip("/"))
    assert any("ESP_BUCKET, ESP_INTAKE_PREFIX, ESP_RUNS_PREFIX" in i for i in texts(app().info))
    assert not fake.calls


def test_an_s3_failure_is_said_plainly(fake):
    fake.fail["ListObjectsV2", INTAKE] = TL.client_error(400, "ExpiredToken", "ListObjectsV2")
    assert any(e.startswith("S3 could not be read (ClientError") and "aws sso login" in e for e in texts(app().error))
