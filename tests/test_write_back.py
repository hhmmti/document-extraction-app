"""The write-back, offline: the merge, each outcome, the dev-only rule, the writer's step with its run-log row, and the
CLI.  The product is a fake at urllib3's `request`, answering as the product's routes and error handlers do; Secrets
Manager is a fake client; S3 and DynamoDB are test_lambda's fakes, and the stored run and its request test_confirm's.
Every organisation, well, login and address is invented.  No AWS and no network: a test that reaches for either
without its fake fails.
"""

import json
import types

import pytest
import urllib3

import app.review as R
import extraction.confirmation as C
import extraction.well_entries_schema as schema
import extraction.write_back as WB
import lambda_function as L
import test_confirm as TC
import test_lambda as TL
from test_confirm import bucket  # noqa: F401 - the writer's fake bucket, holding a stored run

ORG, WELL = TC.ORG, TC.WELL
OTHER = "org-2"                         # an organisation the login does not reach
CID = "20261120T100000Z-0000feed"
SECRET = "invented/write-back-login"
BASE = "https://product.invalid/v2"
LOGIN = {"USERNAME": "writer@example.invalid", "PASSWORD": "the-dev-password", "api_base_url": BASE,
         "application": "esp", "email": "writer@example.invalid", "organization_ids": [ORG], "role": "admin"}
PRD = {**LOGIN, "USERNAME": "prd-writer@example.invalid", "PASSWORD": "the-prd-password",
       "api_base_url": "https://prd.product.invalid/v2"}
TOKEN = "the-dev-token"
AT = "2026-11-20T10:00:00"


def envelope(value, unit, source="ops_entered", **more) -> dict:
    return {"value": value, "unit": unit, "source": source, **more}


def held(organization_id=ORG, well_id=WELL) -> dict:
    """A well as /wells/get returns it: the fields it holds none of are null."""
    return {"organization_id": organization_id, "well_id": well_id, "pump_model": "a label", "active": True,
            "pump_parameters": {
                "pump_id": envelope(41, "id", as_of=None, note=None),                   # retired: no entry carries it
                "stages": envelope(300, "count", as_of=None, note=None),
                "setting_depth_md_ft": envelope(9000, "ft", as_of="2026-01-01", note=None),
                "pump_sections": envelope([{"section_order": 1, "pump_model": "OLD100", "stages": 300,
                                            "section_role": None}], "none", as_of=None, note=None)},
            "wellbore_parameters": {"oil_sg": envelope(0.85, "sg", as_of=None, note=None),
                                    "a_key_of_the_products_own_ft": envelope(12, "ft", as_of=None, note=None)}}


def entry(**changes) -> dict:
    """A confirmed entry as its file holds it."""
    string = [{"section_order": 1, "pump_model": "ELS1750", "stages": 490, "section_role": "primary"}]
    row = {"organization_id": ORG, "well_id": WELL,
           "pump_parameters": {
               "pump_sections": envelope(string, "none", "design_document", note="section order as printed, 1 = deepest"),
               "setting_depth_md_ft": envelope(9100.0, "ft", "design_document", note="intake"),
               "design_document_ref": envelope(C.s3_uri(C.version_key("ab" * 32, CID)), "none", "design_document")},
           "wellbore_parameters": {"oil_sg": envelope(0.8251, "sg", "design_document", note="from API 40"),
                                   "bubble_point_psia": envelope(1514.7, "psia", "ops_entered")},
           "run_id": "20261120T090000Z-0123abcd", "document_sha256": "ab" * 32, "design_date": None,
           "entry_method": "extracted", "confirmation_id": CID, "confirmed_at": "2026-11-20T10:00:00.000001",
           "confirmed_by": "tester", "environment": "dev"}
    return {**row, **changes}


def without(row: dict, field: str, key: str) -> dict:
    return {**row, field: {k: v for k, v in row[field].items() if k != key}}


class Secrets:
    """Secrets Manager holding one secret, with a dev and a prd block; `error` makes the read raise."""

    def __init__(self, secret=None, error=None):
        self.secret = json.dumps({"dev": LOGIN, "prd": PRD}) if secret is None else secret
        self.error, self.calls = error, []

    def get_secret_value(self, *, SecretId):
        self.calls.append(SecretId)
        if self.error:
            raise self.error
        return {"SecretString": self.secret}


class Answer:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def json(self):
        if not isinstance(self.body, (dict, list)):
            raise ValueError("not JSON")
        return self.body


def refusal(status, message) -> tuple:
    """As the product's own exceptions answer: a 401, a 403, a 429."""
    return status, {"message": message, "details": message, "timestamp": AT, "status_code": status}


# A 422 echoes the request in `input` and in each error's `input`: here, a login's.
ERRORS = {401: refusal(401, "Token is expired"),
          403: refusal(403, "Invalid token for account requested"),
          422: (422, {"message": "Type: value_error. Value error, unit 'm' is not the unit of its key: body update_values. ",
                      "details": [{"type": "value_error", "loc": ["body"], "msg": "Value error", "input": dict(LOGIN)}],
                      "input": {"auth_params": dict(LOGIN)}, "timestamp": AT}),
          429: refusal(429, "Rate limit exceeded"),
          500: (500, {"message": "Internal error: boom", "details": [{"type": "InternalError", "msg": "boom"}],
                      "timestamp": AT}),
          502: (502, "<html>Bad Gateway</html>")}
TIMEOUT = urllib3.exceptions.ReadTimeoutError(None, "/v2/wells", "Read timed out. (read timeout=10)")


class Product:
    """The product's test system at urllib3.request: /auth, /wells/get and /wells/update.  `wells` is each well it
    holds, by (organisation, well); the login reaches `reach`; `fail` maps a route to the (status, body) it answers
    or the error the call raises.  An update replaces each map sent whole, and an empty one deletes it, as the
    product's does."""

    def __init__(self, wells=None, reach=(ORG,)):
        self.wells = {(ORG, WELL): held()} if wells is None else wells
        self.reach, self.fail, self.calls, self.secrets = reach, {}, [], Secrets()

    def __call__(self, method, url, *, json=None, headers=None, timeout=None, retries=None, redirect=None):
        assert (method, retries, redirect) == ("POST", False, False) and url.startswith(BASE + "/"), (method, url)
        route, token = url[len(BASE):], (headers or {}).get("token")
        self.calls.append((route, json, token, timeout.total))
        failure = self.fail.get(route)
        if isinstance(failure, Exception):
            raise failure
        if failure:
            return Answer(*failure)
        if route == "/auth":
            assert token is None and json == {"auth_params": {k: LOGIN[k] for k in ("USERNAME", "PASSWORD")}}
            return Answer(200, {"token": TOKEN})
        assert token == TOKEN and route in ("/wells/get", "/wells/update")
        well = json["organization_id"], json["well_id"]
        if well[0] not in self.reach:
            return Answer(*ERRORS[403])
        if route == "/wells/get":
            assert set(json) == {"organization_id", "well_id"}
            return Answer(200, {"organization_id": well[0], "wells": [self.wells[well]] if well in self.wells else []})
        assert set(json) == {"organization_id", "well_id", "update_values"}
        if well not in self.wells:
            return Answer(*ERRORS[500])
        for field, value in json["update_values"].items():
            self.wells[well] = {**self.wells[well], field: value} if value else without_field(self.wells[well], field)
        return Answer(200, {"organization_id": well[0], "status_code": 200, "message": f"successfully updated well: {well[1]}"})

    @property
    def routes(self) -> list:
        return [call[0] for call in self.calls]

    @property
    def sent(self) -> dict:
        """The one update's update_values."""
        [update] = [call[1] for call in self.calls if call[0] == "/wells/update"]
        return update["update_values"]


def without_field(well: dict, field: str) -> dict:
    return {k: v for k, v in well.items() if k != field}


def no_fake(*args, **kwargs):
    raise AssertionError("a test reached for AWS or the product without a fake")


@pytest.fixture(autouse=True)
def nothing_real(monkeypatch):
    monkeypatch.setattr(WB, "secrets_client", no_fake)
    monkeypatch.setattr(urllib3, "request", no_fake)
    monkeypatch.setenv(WB.SECRET_ENV, SECRET)


@pytest.fixture
def product(monkeypatch):
    """The product and Secrets Manager, in place of the real ones."""
    fake = Product()
    monkeypatch.setattr(urllib3, "request", fake)
    monkeypatch.setattr(WB, "secrets_client", lambda *args, **kwargs: fake.secrets)
    return fake


def test_the_fixtures_are_what_they_stand_for():
    assert schema.problems(entry()) == [] and schema.problems(entry(wellbore_parameters={})) == []
    assert WB.LAB_CHECK == R.LAB_CHECK and WB.ENVIRONMENT == "dev" and WB.ENVIRONMENT in C.ENVIRONMENTS
    assert list(schema.PARAMETERS) == ["pump_parameters", "wellbore_parameters"]


# ---- the merge ----

def test_the_entrys_keys_go_over_the_stored_map_and_every_other_key_stays(product):
    before, row = held(), entry()
    out = WB.write(row)
    assert out["outcome"] == WB.WRITTEN and product.routes == ["/auth", "/wells/get", "/wells/update"]
    sent = product.sent
    assert sent == {field: {**before[field], **row[field]} for field in schema.PARAMETERS}
    assert sent["pump_parameters"]["pump_id"] == before["pump_parameters"]["pump_id"]        # the retired key, as read
    assert sent["pump_parameters"]["stages"] == before["pump_parameters"]["stages"]
    assert sent["wellbore_parameters"]["a_key_of_the_products_own_ft"] == envelope(12, "ft", as_of=None, note=None)
    assert out["maps"] == {
        "pump_parameters": {"sent": True, "written": ["pump_sections", "setting_depth_md_ft", "design_document_ref"],
                            "kept": ["pump_id", "stages"]},
        "wellbore_parameters": {"sent": True, "written": ["oil_sg", "bubble_point_psia"],
                                "kept": ["a_key_of_the_products_own_ft"]}}
    assert out["pump_string"] == "replaced" and out["stored"] == {f: before[f] for f in schema.PARAMETERS}
    assert product.wells[ORG, WELL]["pump_model"] == "a label"                               # nothing else is sent


def test_values_go_exactly_as_the_entrys_file_holds_them(product):
    row = C.load(C.row_bytes(entry()))                  # through the file's bytes, as the writer and the CLI read it
    WB.write(row)
    for field in schema.PARAMETERS:
        for key, value in row[field].items():
            assert json.dumps(product.sent[field][key]) == json.dumps(value), key         # no field added or dropped
    assert "as_of" not in product.sent["pump_parameters"]["setting_depth_md_ft"]         # the stored as_of does not stay


def test_a_map_the_entry_has_no_key_in_is_not_sent(product):
    before = held()
    out = WB.write(entry(wellbore_parameters={}))
    assert out["outcome"] == WB.WRITTEN and list(product.sent) == ["pump_parameters"]
    assert product.wells[ORG, WELL]["wellbore_parameters"] == before["wellbore_parameters"]
    assert out["maps"]["wellbore_parameters"] == {"sent": False, "written": [],
                                                 "kept": ["oil_sg", "a_key_of_the_products_own_ft"]}


def test_an_entry_with_no_string_keeps_the_stored_one(product):
    out = WB.write(without(entry(), "pump_parameters", "pump_sections"))
    assert out["outcome"] == WB.WRITTEN and out["pump_string"] == "kept"
    assert product.sent["pump_parameters"]["pump_sections"] == held()["pump_parameters"]["pump_sections"]
    assert "pump_sections" in out["maps"]["pump_parameters"]["kept"]


@pytest.mark.parametrize("stored,given,words", [(True, True, "replaced"), (False, True, "added"), (True, False, "kept"),
                                                (False, False, "none")])
def test_the_outcome_says_what_happened_to_the_string(stored, given, words):
    well = held() if stored else without(held(), "pump_parameters", "pump_sections")
    row = entry() if given else without(entry(), "pump_parameters", "pump_sections")
    update, _, string = WB.merge(row, well)
    assert string == words and ("pump_sections" in update["pump_parameters"]) == (stored or given)


def test_a_well_with_no_maps_gets_the_entrys(product):
    product.wells[ORG, WELL] = {"organization_id": ORG, "well_id": WELL, "pump_parameters": None,
                                "wellbore_parameters": None}
    row = entry()
    assert WB.write(row)["outcome"] == WB.WRITTEN
    assert product.sent == {field: row[field] for field in schema.PARAMETERS}


# ---- each outcome ----

def test_a_write_is_one_login_and_three_calls_each_with_the_timeout(product):
    out = WB.write(entry(), timeout_s=7)
    assert (out["outcome"], out["reason"], out["stage"], out["status_code"], out["message"]) == (
        WB.WRITTEN, None, None, None, None)
    assert product.secrets.calls == [SECRET]
    assert [(route, token, timeout) for route, _, token, timeout in product.calls] == [
        ("/auth", None, 7), ("/wells/get", TOKEN, 7), ("/wells/update", TOKEN, 7)]
    assert product.calls[1][1] == {"organization_id": ORG, "well_id": WELL}
    assert WB.TIMEOUT_S == 10


def test_an_entry_on_live_wells_is_skipped_with_no_call(product):
    out = WB.write(entry(environment="prd"))
    assert out["outcome"] == WB.SKIPPED and "prd" in out["reason"]
    assert product.secrets.calls == [] and product.calls == []


def test_a_lab_check_well_is_skipped_with_no_call(product):
    out = WB.write(entry(organization_id=WB.LAB_CHECK))
    assert out["outcome"] == WB.SKIPPED and out["reason"].startswith("lab-check is data science's own organisation")
    assert product.secrets.calls == [] and product.calls == []


def test_a_well_the_product_does_not_hold_is_skipped_and_no_update_is_sent(product):
    out = WB.write(entry(well_id="a well only data science has"))
    assert out["outcome"] == WB.SKIPPED and out["reason"].startswith("the product does not hold this well")
    assert product.routes == ["/auth", "/wells/get"] and out["maps"] is None


@pytest.mark.parametrize("route,stage", [("/auth", "auth"), ("/wells/get", "get"), ("/wells/update", "update")])
@pytest.mark.parametrize("status", [401, 403, 422, 429, 500, 502])
def test_a_refusal_by_the_product_fails_with_the_stage_the_status_and_its_message(product, route, stage, status):
    before = repr(product.wells)
    product.fail[route] = ERRORS[status]
    out = WB.write(entry())
    body = ERRORS[status][1]
    assert (out["outcome"], out["stage"], out["status_code"]) == (WB.FAILED, stage, status)
    assert out["message"] == (body["message"] if isinstance(body, dict) else "the answer has no message")
    assert product.routes[-1] == route and repr(product.wells) == before         # the last call; nothing changed
    assert LOGIN["PASSWORD"] not in json.dumps(out) and TOKEN not in json.dumps(out)    # a 422's echo is never read


@pytest.mark.parametrize("route,stage", [("/auth", "auth"), ("/wells/get", "get"), ("/wells/update", "update")])
def test_a_timeout_fails_at_its_stage_with_no_status(product, route, stage):
    product.fail[route] = TIMEOUT
    out = WB.write(entry())
    assert (out["outcome"], out["stage"], out["status_code"]) == (WB.FAILED, stage, None)
    assert out["message"].startswith("ReadTimeoutError") and product.routes[-1] == route


def test_an_organisation_the_login_does_not_reach_fails_with_the_403(product):
    out = WB.write(entry(organization_id=OTHER))
    assert (out["outcome"], out["stage"], out["status_code"], out["message"]) == (
        WB.FAILED, "get", 403, "Invalid token for account requested")


@pytest.mark.parametrize("secrets,message", [
    (Secrets(error=TL.client_error(400, "AccessDeniedException", "GetSecretValue")), "the secret cannot be read: ClientError"),
    (Secrets(error=TimeoutError("timed out")), "the secret cannot be read: TimeoutError"),
    (Secrets(secret="not json"), "the secret cannot be read: JSONDecodeError"),
    (Secrets(secret=json.dumps({"prd": PRD})), "the secret cannot be read: KeyError: 'dev'"),
    (Secrets(secret=json.dumps({"dev": {k: v for k, v in LOGIN.items() if k != "PASSWORD"}})),
     "the secret cannot be read: KeyError: 'PASSWORD'"),
    (Secrets(secret=json.dumps({"dev": {**LOGIN, "api_base_url": "http://product.invalid/v2"}})),
     "the secret's dev block needs USERNAME, PASSWORD and an https api_base_url"),
], ids=["denied", "timeout", "not JSON", "no dev block", "no password", "not https"])
def test_a_secret_that_cannot_be_read_fails_before_any_call(product, secrets, message):
    out = WB.write(entry(), secrets=secrets)
    assert (out["outcome"], out["stage"], out["status_code"]) == (WB.FAILED, "secret", None)
    assert out["message"].startswith(message) and product.calls == []
    assert not any(secret in json.dumps(out) for secret in (LOGIN["PASSWORD"], PRD["PASSWORD"]))


def test_no_secret_named_fails_before_anything_is_read(product, monkeypatch):
    monkeypatch.delenv(WB.SECRET_ENV)
    out = WB.write(entry())
    assert (out["outcome"], out["stage"], out["message"]) == (WB.FAILED, "secret", "ESP_WRITEBACK_SECRET is not set")
    assert product.secrets.calls == [] and product.calls == []


@pytest.mark.parametrize("answer,message", [({"token": ""}, "the answer holds no token"), ({}, "the answer holds no token")])
def test_a_login_that_answers_without_a_token_fails(product, answer, message):
    product.fail["/auth"] = (200, answer)
    out = WB.write(entry())
    assert (out["outcome"], out["stage"], out["message"]) == (WB.FAILED, "auth", message) and product.routes == ["/auth"]


def test_a_read_that_answers_without_a_list_of_wells_fails(product):
    product.fail["/wells/get"] = (200, {"organization_id": ORG})
    out = WB.write(entry())
    assert (out["outcome"], out["stage"]) == (WB.FAILED, "get") and "/wells/update" not in product.routes


def test_a_dry_run_reads_and_merges_and_sends_no_update(product):
    before = repr(product.wells)
    out = WB.write(entry(), go=False)
    assert out["outcome"] == WB.DRY_RUN and product.routes == ["/auth", "/wells/get"]
    assert out["pump_string"] == "replaced" and out["stored"]["pump_parameters"] == held()["pump_parameters"]
    assert repr(product.wells) == before


def test_each_call_is_logged_once_without_the_login_or_the_token(product):
    lines = []
    WB.write(entry(), log=lambda call, **fields: lines.append((call, fields)))
    assert [call for call, _ in lines] == ["secret", "auth", "get", "update"]
    product.fail["/wells/update"] = ERRORS[422]
    WB.write(entry(), log=lambda call, **fields: lines.append((call, fields)))
    assert lines[-1] == ("update", {"status_code": 422, "error": ERRORS[422][1]["message"]})
    assert not any(secret in json.dumps(lines) for secret in (LOGIN["PASSWORD"], LOGIN["USERNAME"], TOKEN))


# ---- the time guard ----

def test_no_call_starts_with_too_little_time_left(product):
    out = WB.write(entry(), time_left=lambda: WB.TIMEOUT_S + WB.RESERVE_S - 1)
    assert (out["outcome"], out["stage"], out["status_code"]) == (WB.FAILED, "secret", None)
    assert out["message"] == "not started: 24 s left, and a call is given 25 s"
    assert product.secrets.calls == [] and product.calls == []


def test_the_update_is_not_sent_when_the_time_runs_short_before_it(product):
    left = iter([100, 100, 100, 5])
    before = repr(product.wells)
    out = WB.write(entry(), time_left=lambda: next(left))
    assert (out["outcome"], out["stage"]) == (WB.FAILED, "update") and out["message"].startswith("not started")
    assert product.routes == ["/auth", "/wells/get"] and repr(product.wells) == before


# ---- dev only ----

@pytest.mark.parametrize("environment", ["prd", "qa", "", None])
def test_the_login_refuses_any_other_environment_before_reading_anything(monkeypatch, environment):
    monkeypatch.delenv(WB.SECRET_ENV)                   # not even the secret's name is read
    secrets = Secrets()
    with pytest.raises(WB.Stop, match="is not dev"):
        WB.login(environment, secrets)
    assert secrets.calls == []


class DevBlockOnly(dict):
    """The secret's document: any read that could reach its prd block fails the test."""

    def __getitem__(self, key):
        assert key == "dev", f"the {key} block was read"
        return super().__getitem__(key)

    def refused(self, *args, **kwargs):
        raise AssertionError("the secret's blocks were read together")
    get = items = values = keys = __iter__ = pop = setdefault = copy = refused


def test_the_prd_block_is_never_read(product, monkeypatch):
    monkeypatch.setattr(WB, "json", types.SimpleNamespace(loads=lambda text: DevBlockOnly(json.loads(text)),
                                                          dumps=json.dumps))
    lines = []
    out = WB.write(entry(), log=lambda call, **fields: lines.append((call, fields)))
    assert out["outcome"] == WB.WRITTEN and product.secrets.calls == [SECRET]
    said = json.dumps([product.calls, out, lines])
    assert not any(value in said for value in (PRD["USERNAME"], PRD["PASSWORD"], PRD["api_base_url"]))
    assert WB.login("dev", product.secrets) == {k: LOGIN[k] for k in ("USERNAME", "PASSWORD", "api_base_url")}


# ---- one POST, at urllib3 ----

def test_a_post_sends_the_token_in_its_own_header_tries_once_and_follows_no_redirect(monkeypatch):
    seen = {}

    def request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return Answer(200, {"wells": []})
    monkeypatch.setattr(urllib3, "request", request)
    assert WB.post(f"{BASE}/wells/get", {"organization_id": ORG}, token=TOKEN, timeout_s=4) == {"wells": []}
    assert (seen["method"], seen["url"], seen["json"]) == ("POST", f"{BASE}/wells/get", {"organization_id": ORG})
    assert seen["headers"] == {"token": TOKEN} and (seen["retries"], seen["redirect"]) == (False, False)
    assert seen["timeout"].total == 4
    WB.post(f"{BASE}/auth", {})
    assert seen["headers"] is None and seen["timeout"].total == WB.TIMEOUT_S


@pytest.mark.parametrize("status", [301, 401, 403, 422, 429, 500, 502])
def test_a_post_stops_on_anything_but_a_200_with_the_products_message_alone(monkeypatch, status):
    body = ERRORS.get(status, (status, ""))[1]
    monkeypatch.setattr(urllib3, "request", lambda *args, **kwargs: Answer(status, body))
    with pytest.raises(WB.Stop) as stop:
        WB.post(f"{BASE}/auth", {"auth_params": dict(LOGIN)})
    assert stop.value.status_code == status
    assert stop.value.message == (body["message"] if isinstance(body, dict) else "the answer has no message")
    assert LOGIN["PASSWORD"] not in str(stop.value)


@pytest.mark.parametrize("error", [TIMEOUT, urllib3.exceptions.ConnectTimeoutError(None, "/v2/auth", "timed out"),
                                   urllib3.exceptions.ProtocolError("Connection aborted.")],
                         ids=["read timeout", "connect timeout", "connection dropped"])
def test_a_post_that_gets_no_answer_stops_with_no_status(monkeypatch, error):
    def request(*args, **kwargs):
        raise error
    monkeypatch.setattr(urllib3, "request", request)
    with pytest.raises(WB.Stop) as stop:
        WB.post(f"{BASE}/auth", {"auth_params": dict(LOGIN)})
    assert stop.value.status_code is None and stop.value.message.startswith(type(error).__name__)
    assert LOGIN["PASSWORD"] not in stop.value.message


# ---- the run-log row ----

def test_the_row_is_the_confirmations_layout_under_its_id_with_a_type_of_its_own(product):
    row = entry()
    out = WB.write(row)
    started = WB.now()
    logged = WB.log_row(row, out, resource_name="who wrote", started=started, ended=started, data={"request_id": "r-1"})
    assert set(logged) == TL.ENDED
    assert (logged["type"], logged["organization_id"], logged["organization_id_type"]) == ("WRITE_BACK", ORG, f"{ORG}#WRITE_BACK")
    assert (logged["pipeline_execution_id"], logged["uuid"], logged["well_ids"]) == (CID, CID, [WELL])
    assert (logged["status"], logged["success"], logged["resource_name"]) == ("SUCCEEDED", True, "who wrote")
    assert logged["timestamp"] == logged["end_timestamp"] == C.timestamp(started)
    assert TL.data(logged) == {"request_id": "r-1", "run_id": row["run_id"], "environment": "dev", "outcome": "written",
                               "reason": None, "maps": out["maps"], "pump_string": "replaced", "stage": None,
                               "status_code": None, "message": None}                      # keys, never values


@pytest.mark.parametrize("outcome,status", [(WB.WRITTEN, "SUCCEEDED"), (WB.SKIPPED, "SUCCEEDED"), (WB.FAILED, "FAILED")])
def test_the_rows_status_is_succeeded_for_written_or_skipped_and_failed_otherwise(outcome, status):
    out = {name: None for name in WB.RECORDED} | {"outcome": outcome}
    logged = WB.log_row(entry(), out, resource_name="who wrote", started=WB.now(), ended=WB.now())
    assert (logged["status"], logged["success"]) == (status, status == "SUCCEEDED")


# ---- the writer: after a written confirmation, and never touching it ----

def confirm(fake, q, *, remaining_ms=60_000, table=None):
    fake.objects[C.request_key(TC.CID)] = q if isinstance(q, bytes) else C.dump(q)
    table = table or TL.FakeTable()
    out = L.confirm_handler(TL.s3_event(C.request_key(TC.CID)), TL.Context(remaining_ms), s3=fake, table=table,
                            catalogue=lambda: TC.CATALOGUE)
    return out, table


def test_a_written_confirmation_on_test_wells_is_written_back_with_a_second_row(bucket, product, capsys):
    out, table = confirm(bucket, TC.request(environment="dev"))
    assert out == [{"confirmation_id": TC.CID, "status": C.WRITTEN}]
    row_key = C.row_key("dev", TC.CID)
    row = json.loads(bucket.objects[row_key])
    assert product.sent == {field: {**held()[field], **row[field]} for field in schema.PARAMETERS}     # the file's values
    confirmed, logged = table.items
    TC.assert_writer_row(confirmed, status="SUCCEEDED")
    assert [(op, status) for op, status, _ in TL.writes(table)] == [
        ("PutItem", "RUNNING"), ("UpdateItem", "SUCCEEDED"), ("PutItem", "SUCCEEDED")]
    assert set(logged) == TL.ENDED and logged["timestamp"] > confirmed["timestamp"]
    assert (logged["type"], logged["organization_id_type"]) == ("WRITE_BACK", f"{ORG}#WRITE_BACK")
    assert (logged["pipeline_execution_id"], logged["uuid"], logged["well_ids"]) == (TC.CID, TC.CID, [WELL])
    assert (logged["status"], logged["success"], logged["resource_name"]) == ("SUCCEEDED", True, TC.FUNCTION)
    data = TL.data(logged)
    assert (data["request_id"], data["request_key"], data["row_key"], data["run_id"], data["environment"]) == (
        TL.REQUEST_ID, C.request_key(TC.CID), row_key, TC.RUN_ID, "dev")
    assert (data["outcome"], data["pump_string"], data["maps"]["pump_parameters"]["kept"]) == (
        "written", "replaced", ["pump_id", "stages"])
    printed = capsys.readouterr().out
    lines = [json.loads(line) for line in printed.splitlines()]
    after = [line for line in lines[[line["stage"] for line in lines].index("end") + 1:]]
    assert {line["run_id"] for line in lines} == {TC.CID}
    assert [line.get("call") for line in after if line["stage"] == "write_back"] == ["secret", "auth", "get", "update", None]
    assert after[-1]["result"] == {name: data[name] for name in WB.RECORDED} and "error" not in after[-1]
    assert not any(secret in printed + json.dumps(table.writes) for secret in (LOGIN["PASSWORD"], LOGIN["USERNAME"], TOKEN))


@pytest.mark.parametrize("does,remaining_ms,second", [
    ("writes", 60_000, "SUCCEEDED"), ("fails at the update", 60_000, "FAILED"),
    ("cannot read the secret", 60_000, "FAILED"), ("has too little time", 20_000, "FAILED"), ("raises", 60_000, None)])
def test_the_confirmation_is_final_before_the_write_back_and_unchanged_whatever_it_does(bucket, product, monkeypatch,
                                                                                         does, remaining_ms, second):
    if does == "fails at the update":
        product.fail["/wells/update"] = ERRORS[500]
    if does == "cannot read the secret":
        product.secrets.error = TL.client_error(400, "AccessDeniedException", "GetSecretValue")
    table, seen, real = TL.FakeTable(), {}, WB.write

    def write(row, **kwargs):
        seen["result"], seen["row"] = bucket.objects[C.result_key(TC.CID)], dict(table.items[0])
        if does == "raises":
            raise RuntimeError("boom")
        return real(row, **kwargs)
    monkeypatch.setattr(WB, "write", write)
    out, _ = confirm(bucket, TC.request(environment="dev"), remaining_ms=remaining_ms, table=table)
    assert out == [{"confirmation_id": TC.CID, "status": C.WRITTEN}]
    assert json.loads(seen["result"])["status"] == C.WRITTEN and "deadline_at" not in json.loads(seen["result"])
    assert (seen["row"]["status"], "end_timestamp" in seen["row"]) == ("SUCCEEDED", True)          # final before it
    assert bucket.objects[C.result_key(TC.CID)] == seen["result"] and table.items[0] == seen["row"]  # and unchanged
    assert TL.put_keys(bucket)[-1] == C.result_key(TC.CID)                                           # nothing put since
    assert [(r["type"], r["status"]) for r in table.items[1:]] == ([("WRITE_BACK", second)] if second else [])


def test_too_little_of_the_lambdas_time_starts_no_call_and_is_recorded_failed(bucket, product):
    out, table = confirm(bucket, TC.request(environment="dev"), remaining_ms=24_000)
    assert out == [{"confirmation_id": TC.CID, "status": C.WRITTEN}]
    assert product.secrets.calls == [] and product.calls == []
    data = TL.data(table.items[1])
    assert (table.items[1]["status"], data["outcome"], data["stage"]) == ("FAILED", "failed", "secret")
    assert data["message"] == "not started: 24 s left, and a call is given 25 s"


def test_a_confirmation_on_live_wells_gets_a_skipped_row_and_no_call(bucket, product):
    out, table = confirm(bucket, TC.request())                          # test_confirm's request: Live wells
    assert out == [{"confirmation_id": TC.CID, "status": C.WRITTEN}]
    assert product.secrets.calls == [] and product.calls == []
    confirmed, logged = table.items
    assert (logged["type"], logged["status"], logged["success"]) == ("WRITE_BACK", "SUCCEEDED", True)
    assert (TL.data(logged)["outcome"], TL.data(logged)["environment"]) == ("skipped", "prd")
    assert "prd" in TL.data(logged)["reason"]


def refused(fake):
    return TC.request(environment="qa")


def failed(fake):
    fake.fail["GetObject", C.run_key(TC.RUN_ID, "run.json")] = TL.client_error(500, "InternalError", "GetObject")
    return TC.request(environment="dev")


def duplicate(fake):
    fake.objects[C.result_key(TC.CID)] = b"{}"
    return TC.request(environment="dev")


@pytest.mark.parametrize("make,status,rows", [(refused, C.REFUSED, 1), (failed, C.FAILED, 1), (duplicate, "duplicate", 0)])
def test_a_refused_a_failed_and_a_duplicate_confirmation_get_no_call_and_no_row(bucket, product, make, status, rows):
    out, table = confirm(bucket, make(bucket))
    assert out == [{"confirmation_id": TC.CID, "status": status}]
    assert product.secrets.calls == [] and product.calls == []
    assert len(table.items) == rows and all(row["type"] == "PIPELINE_EXECUTION" for row in table.items)


def test_with_no_secret_named_the_writer_makes_no_write_back(bucket, product, monkeypatch):
    monkeypatch.delenv(WB.SECRET_ENV)                                   # as before the stack named it
    out, table = confirm(bucket, TC.request(environment="dev"))
    assert out == [{"confirmation_id": TC.CID, "status": C.WRITTEN}]
    assert product.secrets.calls == [] and product.calls == [] and len(table.items) == 1


def test_a_run_log_write_that_fails_never_fails_the_confirmation(bucket, product):
    out, table = confirm(bucket, TC.request(environment="dev"),
                         table=TL.FakeTable(error=TL.client_error(400, "ValidationException", "PutItem")))
    assert out == [{"confirmation_id": TC.CID, "status": C.WRITTEN}] and table.items == []
    assert product.routes == ["/auth", "/wells/get", "/wells/update"]


# ---- the CLI ----

LATER = "20261120T110000Z-0000beef"
WELL_2, WELL_3 = "W 2", "W 3"


class Recorded(C.LocalObjects):
    """A local store that records each key and folder read."""

    def __init__(self, root):
        super().__init__(root)
        self.read = []

    def get(self, key):
        self.read.append(key)
        return super().get(key)

    def keys(self, prefix):
        self.read.append(prefix)
        return super().keys(prefix)


def store(objects, row) -> dict:
    objects.put(C.row_key(row["environment"], row["confirmation_id"]), C.row_bytes(row))
    return row


@pytest.fixture
def cli(monkeypatch, tmp_path, product):
    """The CLI's AWS, as fakes: a local store for the bucket, the product's Secrets Manager, and a run-log table."""
    objects, table, seen = Recorded(tmp_path / "store"), TL.FakeTable(), {}

    def aws(profile, timeout_s):
        seen.update(profile=profile, timeout_s=timeout_s)
        return objects, product.secrets, types.SimpleNamespace(Table=lambda name: seen.update(table=name) or table)
    monkeypatch.setattr(WB, "aws", aws)
    monkeypatch.setenv(WB.TABLE_ENV, "the-run-log")
    return types.SimpleNamespace(objects=objects, table=table, seen=seen)


def test_the_dry_run_shows_each_well_and_writes_nothing(cli, product, capsys):
    store(cli.objects, entry())
    store(cli.objects, entry(well_id=WELL_2, confirmation_id="20261120T100001Z-00000002"))
    store(cli.objects, entry(organization_id=WB.LAB_CHECK, confirmation_id="20261120T100002Z-00000003"))
    store(cli.objects, entry(organization_id=OTHER, confirmation_id="20261120T100003Z-00000004"))
    before = repr(product.wells)
    assert WB.main(["--pass", "--profile", "someone"]) == 1             # one well's read failed
    out = capsys.readouterr().out
    assert "/wells/update" not in product.routes and repr(product.wells) == before and cli.table.writes == []
    assert cli.seen == {"profile": "someone", "timeout_s": WB.TIMEOUT_S}
    assert out.startswith("dry run of 4 of 4 entries on Test wells\n")
    assert f"{ORG} / {WELL}   confirmation {CID}: dry run\n  the product holds this well, and the login reaches it\n" in out
    assert ('    setting_depth_md_ft: stored {"as_of": "2026-01-01", "source": "ops_entered", "unit": "ft", "value": 9000}'
            ' -> new {"note": "intake", "source": "design_document", "unit": "ft", "value": 9100.0}\n') in out
    assert '    bubble_point_psia: stored nothing -> new {"source": "ops_entered", "unit": "psia", "value": 1514.7}\n' in out
    assert '    pump_id: stored {"source": "ops_entered", "unit": "id", "value": 41}, kept\n' in out
    assert "  pump string: the stored one is replaced by the entry's\n" in out
    assert (f"{ORG} / {WELL_2}   confirmation 20261120T100001Z-00000002: skipped\n"
            "  skipped: the product does not hold this well") in out
    assert f"{WB.LAB_CHECK} / {WELL}   confirmation 20261120T100002Z-00000003: skipped\n  skipped: lab-check is" in out
    assert (f"{OTHER} / {WELL}   confirmation 20261120T100003Z-00000004: failed\n  failed at get with 403: Invalid token for "
            "account requested\n  the login does not reach this organisation\n") in out
    assert not any(secret in out for secret in (LOGIN["PASSWORD"], TOKEN))


def test_the_dry_run_marks_a_value_the_product_already_holds(cli, product, capsys):
    row = store(cli.objects, entry())
    product.wells[ORG, WELL]["wellbore_parameters"]["oil_sg"] = {**row["wellbore_parameters"]["oil_sg"], "as_of": None}
    assert WB.main(["--confirmation", CID]) == 0
    assert '"value": 0.8251}  (the same)\n' in capsys.readouterr().out


def test_the_pass_takes_each_wells_current_entry_from_its_own_file_and_reads_nothing_of_prd(cli, product, capsys):
    store(cli.objects, entry())
    later = store(cli.objects, entry(confirmation_id=LATER, confirmed_at="2026-11-20T11:00:00.000000",
                                     pump_parameters=without(entry(), "pump_parameters", "pump_sections")["pump_parameters"]))
    store(cli.objects, entry(confirmation_id="20261120T120000Z-00000009", confirmed_at="2026-11-20T12:00:00.000000",
                             environment="prd"))
    assert WB.current_entries(cli.objects) == [later]
    assert WB.main(["--pass"]) == 0
    out = capsys.readouterr().out
    assert f"confirmation {LATER}: dry run" in out and f"confirmation {CID}" not in out
    assert "pump string: the stored one is kept" in out
    assert cli.objects.read and all(key.startswith(C.row_prefix("dev")) for key in cli.objects.read)


def test_go_writes_only_the_wells_named_each_leaving_its_row(cli, product, capsys):
    for n, well in enumerate((WELL, WELL_2, WELL_3)):
        product.wells[ORG, well] = held(ORG, well)
        store(cli.objects, entry(well_id=well, confirmation_id=f"20261120T10000{n}Z-0000000{n}"))
    untouched = json.dumps([product.wells[ORG, WELL], product.wells[ORG, WELL_3]])
    assert WB.main(["--pass", "--go", "--well", ORG, WELL_2]) == 0
    assert [(call[0], call[1]["well_id"]) for call in product.calls[1:]] == [("/wells/get", WELL_2), ("/wells/update", WELL_2)]
    assert json.dumps([product.wells[ORG, WELL], product.wells[ORG, WELL_3]]) == untouched
    assert product.wells[ORG, WELL_2]["pump_parameters"]["setting_depth_md_ft"]["value"] == 9100.0
    [logged] = cli.table.items
    assert cli.table.writes[0][0] == "PutItem" and cli.seen["table"] == "the-run-log" and set(logged) == TL.ENDED
    assert (logged["type"], logged["status"], logged["resource_name"]) == ("WRITE_BACK", "SUCCEEDED", WB.CLI)
    assert (logged["pipeline_execution_id"], logged["well_ids"]) == ("20261120T100001Z-00000001", [WELL_2])
    assert TL.data(logged)["outcome"] == "written"
    out = capsys.readouterr().out
    assert out.startswith("writing 1 of 3 entries on Test wells\n") and f"{ORG} / {WELL_2}" in out and WELL_3 not in out


def test_go_refuses_to_run_with_no_well_named(cli, product, capsys):
    store(cli.objects, entry())
    assert WB.main(["--pass", "--go"]) == 2 and WB.main(["--confirmation", CID, "--go"]) == 2
    assert "give --well ORGANISATION WELL" in capsys.readouterr().err
    assert cli.seen == {} and product.secrets.calls == [] and product.calls == [] and cli.table.writes == []


def test_go_refuses_a_named_well_with_no_entry(cli, product, capsys):
    store(cli.objects, entry())
    assert WB.main(["--pass", "--go", "--well", ORG, WELL, "--well", ORG, "no such well"]) == 2
    assert f"no entry read for {ORG} / no such well; nothing run" in capsys.readouterr().err
    assert product.calls == [] and cli.table.writes == []


def test_one_confirmation_by_its_id_dry_then_written(cli, product, capsys):
    store(cli.objects, entry())
    store(cli.objects, entry(confirmation_id=LATER, confirmed_at="2026-11-20T11:00:00.000000", well_id=WELL_2))
    assert WB.main(["--confirmation", CID]) == 0
    assert "/wells/update" not in product.routes and cli.table.writes == []
    assert cli.objects.read == [C.row_key("dev", CID)]
    assert WB.main(["--confirmation", CID, "--go", "--well", ORG, WELL]) == 0
    assert product.routes.count("/wells/update") == 1 and [row["pipeline_execution_id"] for row in cli.table.items] == [CID]
    assert f"confirmation {CID}: written" in capsys.readouterr().out


def test_a_confirmation_with_no_entry_on_test_wells_runs_nothing(cli, product, capsys):
    store(cli.objects, entry(environment="prd"))                        # confirmed on Live wells: never looked for
    assert WB.main(["--confirmation", CID]) == 2
    assert "no entry on Test wells has this confirmation id" in capsys.readouterr().err
    assert cli.objects.read == [C.row_key("dev", CID)] and product.calls == []
    with pytest.raises(SystemExit):
        WB.main(["--confirmation", "../" + CID])


@pytest.mark.parametrize("args,unset,message", [
    (["--pass"], WB.SECRET_ENV, "set ESP_WRITEBACK_SECRET; nothing run"),
    (["--pass", "--go", "--well", ORG, WELL], WB.TABLE_ENV, "set ESP_RUN_LOG_TABLE; nothing run")])
def test_the_cli_needs_the_secret_named_and_with_go_the_table(cli, product, monkeypatch, capsys, args, unset, message):
    monkeypatch.delenv(unset)
    assert WB.main(args) == 2 and message in capsys.readouterr().err
    assert cli.seen == {} and product.calls == []


def test_a_failed_write_and_a_row_that_cannot_be_written_are_said_and_exit_1(cli, product, capsys):
    store(cli.objects, entry())
    product.fail["/wells/update"] = ERRORS[422]
    assert WB.main(["--pass", "--go", "--well", ORG, WELL]) == 1
    assert "failed at update with 422" in capsys.readouterr().out
    assert [(row["status"], TL.data(row)["status_code"]) for row in cli.table.items] == [("FAILED", 422)]
    del product.fail["/wells/update"]
    cli.table.error = TL.client_error(400, "AccessDeniedException", "PutItem")
    assert WB.main(["--pass", "--go", "--well", ORG, WELL]) == 1
    assert "its run-log row was not written: ClientError" in capsys.readouterr().err
