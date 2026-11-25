"""The app's S3 mode, offline.  S3 is test_lambda's fake, extended with what the app calls (a listing,
two keys a page so that paging runs, and uploads with their content type and labels).  Where a run's
own records are needed, the Lambda handler writes them into the same fake.  No AWS, no process."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

import app.intake as intake
import app.store as S
import extraction.run as R
import lambda_function as L
import test_lambda as TL
from extraction import ExtractionError
from extraction.assets_dir import ASSETS_ENV
from extraction.intake import (LABELS, RUN_ID, decode_labels, encode_labels, intake_key, intake_run_id,
                               optional_labels)
from test_bedrock import FakeClient, events
from test_intake import NO_RUN, TRY_AGAIN, app, lists, no_id_or_code, technical
from test_run import GOOD
from test_screens import screen
from test_viewer import FULL, PDF, PDF_SHA, record, runs, texts

BUCKET, INTAKE, RUNS = TL.BUCKET, TL.INTAKE, TL.RUNS
# The run list's column (app/main.py) and its words (app/review.py) for a run an upload in the product started.
ORIGIN, AUTOMATIC = "How it started", "Started automatically by an upload in the product"
HERE = "Started here, with New run"
ORG, WELL = "Org Alpha", "Well  #1"            # test_intake's invented ids, the form's first choices below
DURATION = ("A run takes 3 to 8 minutes. When it finishes, you check what it read against the document, and confirm "
            "it.")
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
    """The app in S3 mode on a fake bucket, with test_intake's well lists from a fake Athena; the store is built
    afresh."""
    lists(monkeypatch)
    for name, value in {S.MODE_ENV: "s3", "ESP_BUCKET": BUCKET, "ESP_INTAKE_PREFIX": INTAKE,
                        "ESP_RUNS_PREFIX": RUNS}.items():
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


def form(name="doc.pdf", wells="Live wells", org=ORG, well=WELL):
    at = app(upload="1")
    at.get("button_group")[0].set_value(wells).run()
    at.selectbox(key="new-org").set_value(org).run()
    at.selectbox(key="new-well").set_value(well).run()
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
    assert DURATION in texts(at.info) and start_button(at).disabled
    for name, content, message in (("a.pdf", b"", "empty"), ("b.pdf", b"not a pdf", "does not start with %PDF-")):
        at.file_uploader(key="new-pdf").upload(name, content, "application/pdf").run()
        assert any(message in e for e in texts(at.error)) and start_button(at).disabled
    assert not fake.puts


@pytest.mark.parametrize("wells,org,well,environment", [("Live wells", ORG, WELL, "prd"),
                                                         ("Test wells", "Test Org", "T 1", "dev")])
def test_an_upload_has_the_key_type_and_labels_and_the_handler_accepts_them(fake, handler, wells, org, well,
                                                                            environment):
    at = form("Désign report 1.pdf", wells, org, well)
    at.checkbox(key="new-confirm").check().run()
    start_button(at).click().run()
    [key] = [k for k, _ in fake.puts]
    run_id = intake_run_id(INTAKE, key)
    assert run_id and fake.objects[key] == PDF
    assert fake.headers[key] == {"ContentType": "application/pdf", "IfNoneMatch": "*"}
    assert fake.metadata[key] == {**encode_labels(org, well, "Désign report 1.pdf", PDF_SHA), "environment": environment}
    assert S.s3_store_from_env().intake_labels(run_id)["environment"] == environment     # as the writer reads it
    assert optional_labels(fake.metadata[key]) == {"environment": environment}
    assert any(i.startswith("Waiting to start: ") for i in texts(at.info))      # now on the watch view
    operation, first = handler(run_id).writes[0]     # the handler's own validation: its RUNNING row follows it
    assert (operation, first["status"], first["organization_id"], first["well_ids"]) == ("PutItem", "RUNNING", org, [well])
    assert json.loads(first["data_json"])["source_name"] == "Désign report 1.pdf"


def test_a_name_the_labels_refuse_puts_nothing_and_a_put_that_fails_still_says_try_again(fake, monkeypatch):
    """S3 mode, where the upload starts the run: a name the labels refuse is said before any upload, Start stays off and
    nothing is put.  A failure a retry can fix, a put the bucket turns away for now, still says Try again, with the
    error in the technical section."""
    lists(monkeypatch, prd=[("Org\u00a0Alpha", "Well 1"), (ORG, WELL)])
    at = form(org="Org\u00a0Alpha", well="Well 1")
    assert texts(at.error) == [NO_RUN["organisation"]] and start_button(at).disabled and not fake.puts

    def slow_down(**kwargs):
        raise TL.client_error(503, "SlowDown", "PutObject")
    monkeypatch.setattr(fake, "put_object", slow_down)
    at = form()
    at.checkbox(key="new-confirm").check().run()
    start_button(at).click().run()
    assert texts(at.error) == [TRY_AGAIN] and "SlowDown" in technical(at) and not fake.puts


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
    at.button_group[0].set_value("Run details (technical)").run()          # it stored a proposed entry, so it opens in review
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
    at = app()
    df, raw = runs(at), at.dataframe[1].value.set_index("Run id")
    assert list(df.index) == [expired, late, queued, stale, failed, done]
    assert df.loc[[done, failed, stale], "Run"].tolist() == ["completed", "failed", "stopped without finishing"]
    assert (raw.loc[failed, "Pipeline, raw"], raw.loc[failed, "Error"]) == ("failed at labels", "INTAKE_INVALID at labels")
    assert df.loc[[queued, late, expired], "Run"].tolist() == ["waiting to start", "not picked up", "expired"]
    assert df.loc[[queued, late, expired], "Document review"].tolist() == ["not yet"] * 3
    assert df.loc[[queued, late, expired], "Waited"].tolist() == ["2 min", "10 min", "7 h 0 min"]
    assert df.loc[[queued, stale], "Organisation"].tolist() == [ORG, ORG]      # from the uploads' labels


def test_with_a_run_running_a_long_wait_is_still_queued(fake, handler):
    running, late = rid(1), rid(2)
    upload(fake, running, age=timedelta(minutes=12))
    as_running(fake, handler, running)
    upload(fake, late, age=timedelta(minutes=10))
    df = runs(app())
    assert df.loc[running, ["Run", "Well"]].tolist() == ["running", "Well\u2423\u2423#1"]     # spacing marked
    assert df.loc[late, "Run"] == "waiting to start"
    metrics = {m.label: m.value for m in app(run=running).metric}            # its technical page: exactly
    assert (metrics["Organisation"], metrics["Well"]) == (ORG, WELL)


@pytest.mark.parametrize("age,running,element,shown", [
    (timedelta(seconds=5), False, "info", "Waiting to start: the upload is waiting its turn."),   # no "for 5 s"
    (timedelta(minutes=1), False, "info", "Waiting to start, for 1 min so far: "),
    (timedelta(minutes=10), True, "info", "Waiting to start, for 10 min so far: "),
    (timedelta(minutes=10), False, "warning", "Not picked up: the upload has waited 10 min"),
    (timedelta(hours=7), False, "error", "Expired: the upload waited 7 h 0 min"),
])
def test_the_watch_view_shows_an_uploads_wait(fake, handler, age, running, element, shown):
    run_id = rid(5)
    upload(fake, run_id, age=age)
    if running:
        upload(fake, rid(1), age=timedelta(minutes=12))
        as_running(fake, handler, rid(1))
    at = app(watch=run_id)
    messages = [t for t in texts(getattr(at, element)) if t.startswith(shown)]
    assert len(messages) == 1
    if element == "warning":                            # where to look: technical
        assert "run log" in technical(at) and "CloudWatch" in technical(at) and run_id in technical(at)
    no_id_or_code(at, ORG, WELL, "doc.pdf")


def test_the_watch_view_follows_an_upload_to_its_final_record(fake):
    run_id = rid(5)
    upload(fake, run_id)
    at = app(watch=run_id)
    assert any(i.startswith("Waiting to start") for i in texts(at.info))
    stored_run(fake, run_id, {"run_id": run_id, "status": "running", "started_at": R.utc(datetime.now(timezone.utc)),
                              "deadline_at": "2999-01-01T00:00:00Z"})
    at.run()
    assert "Run: running" in markdown(at)
    stored_run(fake, run_id, record(run_id))
    at.run()
    assert "Run: completed" in markdown(at)
    next(b for b in at.button if b.label == "Open the run").click().run()
    assert [t.label for t in at.tabs][:1] == ["Summary"]                       # the run's own page


# ---- the duplicate warning ----

@pytest.mark.parametrize("case,warned", [
    ("running", "is still running"),
    ("queued", "has not started yet (waiting to start)"),
    ("not picked up", "has not started yet (not picked up)"),
    ("expired", None),
    ("stale", None),
    ("another well", None),
    ("queued for Test wells", None),                    # the same PDF, organisation and well, written to dev
    ("running for Test wells", None),
])
def test_the_duplicate_warning_reads_running_records_and_waiting_uploads(fake, handler, case, warned):
    run_id = rid(1)
    age = {"not picked up": timedelta(minutes=10), "expired": timedelta(hours=7)}.get(case, timedelta(minutes=1))
    dev = {**encode_labels(ORG, WELL, "doc.pdf", R.sha256(PDF)), "environment": "dev"}
    upload(fake, run_id, age=age, well="well #2" if case == "another well" else WELL,
           metadata=dev if case.endswith("Test wells") else None)
    if case.startswith(("running", "stale")):
        assert as_running(fake, handler, run_id, stale=case == "stale")["organization_id"] is None
    at = form()
    mine = [w for w in texts(at.warning) if "of this PDF for this organisation and well" in w]
    no_id_or_code(at, ORG, "org alpha", "Beta Org", WELL, "well #2", "doc.pdf")
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


# ---- the run list is read afresh each time its page opens ----

def test_the_list_is_read_when_its_page_opens_a_rerun_keeps_that_read_and_refresh_reads_it_again(fake):
    old, pushed, later = rid(1), rid(2), rid(3)
    upload(fake, old, age=timedelta(hours=1))
    stored_run(fake, old, record(old))
    at = app()                                                                  # a visit
    assert list(runs(at).index) == [old]
    upload(fake, pushed, metadata=pushed_labels())                              # pushed while the page sits open
    stored_run(fake, pushed, record(pushed))
    seen = len(fake.calls)
    at.run()                                                                    # a rerun within the page
    assert len(fake.calls) == seen and list(runs(at).index) == [old]
    at.sidebar.button[0].click().run()                                          # Refresh
    assert ("ListObjectsV2", RUNS) in fake.calls[seen:] and list(runs(at).index) == [pushed, old]
    upload(fake, later)
    assert list(runs(app()).index) == [later, pushed, old]                      # the next visit, or a reload


def test_coming_back_to_the_list_from_a_run_or_from_watching_a_new_one_reads_it_afresh(fake):
    old, pushed = rid(1), rid(2)
    upload(fake, old, age=timedelta(hours=1))
    stored_run(fake, old, record(old))
    at = app()
    upload(fake, pushed, metadata=pushed_labels())                              # pushed while the list is open
    at.query_params["run"] = old
    at.run()                                                                    # the run's page, as Open opens it
    next(b for b in at.button if b.label == "All runs").click().run()
    assert list(runs(at).index) == [pushed, old]
    at = form()                                                                 # a run started here
    at.checkbox(key="new-confirm").check().run()
    start_button(at).click().run()
    [new] = [intake_run_id(INTAKE, k) for k, _ in fake.puts]
    next(b for b in at.button if b.label == "All runs").click().run()           # from its watch page
    assert set(runs(at).index) == {new, pushed, old} and runs(at).loc[new, "Run"] == "waiting to start"


# ---- an upload's labels, once read, are kept for the process's life ----

def heads(fake) -> list[str]:
    """The runs whose upload's labels were read, a HEAD each."""
    return [intake_run_id(INTAKE, key) for op, key in fake.calls if op == "HeadObject"]


def test_an_open_reads_the_labels_only_of_an_upload_added_since_and_no_refresh_reads_them_again(fake):
    old, running = rid(1), rid(2)
    upload(fake, old, age=timedelta(hours=1), metadata=pushed_labels())
    stored_run(fake, old, record(old))
    app()                                                                       # the first open
    assert heads(fake) == [old]
    upload(fake, running)                                                       # added since
    stored_run(fake, running, record(running, status="running", deadline_at="2999-01-01T00:00:00Z"))
    fake.calls.clear()
    at = app()                                                                  # the next open, or a reload
    assert heads(fake) == [running] and runs(at)[ORIGIN].tolist() == ["not recorded", AUTOMATIC]
    fake.calls.clear()
    at.sidebar.button[0].click().run()                                          # Refresh
    app(watch=running)                                                          # its watch page refreshes that run
    assert {("ListObjectsV2", INTAKE), ("ListObjectsV2", f"{RUNS}{running}/")} <= set(fake.calls)
    assert heads(fake) == [] and runs(at)[ORIGIN].tolist() == ["not recorded", AUTOMATIC]


def test_a_label_read_that_fails_or_finds_nothing_is_tried_again_at_the_next_open_and_kept_once_read(fake):
    pushed, bare = rid(1), rid(2)
    upload(fake, pushed, metadata=pushed_labels())
    upload(fake, bare, metadata={})                                             # read, it finds no labels
    failing = ("HeadObject", intake_key(INTAKE, pushed))
    fake.fail[failing] = TL.client_error(503, "SlowDown", "HeadObject")
    at = app()
    assert sorted(heads(fake)) == [pushed, bare] and runs(at).loc[pushed, ORIGIN] == "not recorded"
    fake.calls.clear()
    at.run()                                                                    # a rerun keeps its open's read
    assert heads(fake) == []
    del fake.fail[failing]
    at = app()                                                                  # the next open tries both again
    assert sorted(heads(fake)) == [pushed, bare] and runs(at).loc[pushed, ORIGIN] == AUTOMATIC
    fake.calls.clear()
    app()
    assert heads(fake) == [bare]                                                # read, the labels are kept


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
    at = app()
    assert texts(at.error) == ["The runs could not be read. Reload the page; if it fails again, tell whoever looks "
                               "after this app."]
    assert "S3 could not be read (ClientError" in technical(at) and "aws sso login" in technical(at)
    no_id_or_code(at)


def test_the_list_upload_and_watch_pages_sidebar_says_only_when_the_runs_were_read(fake, handler):
    done, queued = rid(1), rid(2)
    upload(fake, done, age=timedelta(hours=1))
    stored_run(fake, done, record(done))
    upload(fake, queued)
    names = (ORG, "org alpha", "Beta Org", WELL, "well #2", "doc.pdf", "org-test", "well-a")
    for at in (app(), app(upload="1"), app(watch=queued), app(watch=done)):
        said = texts(at.sidebar.caption)
        assert len(said) == 1 and re.fullmatch(r"The run list is as of \d\d:\d\d UTC\. Refresh reads it again\.", said[0])
        assert [(b.label, b.help or None) for b in at.sidebar.button] == [("Refresh", None)]   # no tooltip to stay up
        no_id_or_code(at, *names)
    at = app(run=done)                                  # Run details, technical: where S3 mode reads
    assert any(BUCKET in c for c in texts(at.sidebar.caption))


# ---- a run an upload in the product started ----

UPLOADER = "engineer-7@example.com"


def pushed_labels() -> dict:
    """An upload as the product-upload starter labels it: the four, and the optional ones."""
    return {**encode_labels(ORG, WELL, "doc.pdf", PDF_SHA), "origin": "product-upload", "environment": "dev",
            "uploaded-by": quote(UPLOADER, safe=""), "pushed-from": quote("s3://b/dev/k.pdf?versionId=v", safe="")}


def test_the_store_gives_the_optional_labels_beside_the_four(fake):
    pushed, form = rid(1), rid(2)
    upload(fake, pushed, metadata=pushed_labels())
    upload(fake, form)
    store = S.s3_store_from_env()
    four = decode_labels(encode_labels(ORG, WELL, "doc.pdf", PDF_SHA))
    assert store.intake_labels(pushed) == {**four, "origin": "product-upload", "environment": "dev",
                                           "uploaded-by": UPLOADER, "pushed-from": "s3://b/dev/k.pdf?versionId=v"}
    assert store.intake_labels(form) == four


def test_a_run_the_product_started_says_so_in_the_list_and_on_its_queue_and_watch_pages(fake):
    pushed, form, queued, bare = rid(1), rid(2), rid(3), rid(4)
    upload(fake, form, metadata={**encode_labels(ORG, WELL, "doc.pdf", PDF_SHA), "environment": "prd"})   # as the form's
    upload(fake, bare)                                                 # the four labels alone: not the form's
    assert runs(app())[ORIGIN].tolist() == ["not recorded", HERE]      # never blank
    for run_id in (pushed, queued):
        upload(fake, run_id, metadata=pushed_labels())
    stored_run(fake, pushed, record(pushed))                           # one has its run, one is queued
    S.s3_store_from_env().refresh()
    at = app()
    df = runs(at)
    assert df.loc[[queued, pushed], ORIGIN].tolist() == [AUTOMATIC, AUTOMATIC]
    assert df.loc[[queued, pushed], "Run"].tolist() == ["waiting to start", "completed"] and df.loc[form, ORIGIN] == HERE
    assert df.loc[[queued, pushed, form], "Wells"].tolist() == ["Test wells", "Test wells", "Live wells"]
    assert list(df.columns[:4]) == ["Open", "Organisation", "Well", "Wells"] and ORIGIN in df     # the side: beside
                                                                                                # the organisation and well
    pages = [at]
    for run_id, says in ((queued, True), (form, False)):
        watch = app(watch=run_id)
        assert any(i.startswith("Waiting to start") for i in texts(watch.info))
        assert (AUTOMATIC in " ".join(texts(watch.markdown))) == says
        pages.append(watch)
    assert not [s for page in pages for s in screen(page) if UPLOADER in s]       # who uploaded is never shown
