"""The write-back: a confirmed entry's values into the well's settings in the ESP product's test system.

    write(row)                             the write for one entry.  lambda_function.confirm_handler calls it after a
                                           confirmation is written; the CLI calls it for a rerun and for the pass
    python -m extraction.write_back ...    the CLI, at the end of this file

`row` is a confirmed entry as its own file holds it (well-entries/environment=dev/<confirmation_id>.json:
extraction/confirmation.py).  It is already in the product's keys, units and shapes, so nothing is translated.

Only entries on Test wells are written, to the product's test system: the environment `dev`, with the `dev` block of
the secret that ESP_WRITEBACK_SECRET names.  Any other environment stops before anything is read, and nothing here
reads or writes production.

One write is one login and at most three POSTs to the block's `api_base_url`, each given TIMEOUT_S and tried once:
    /auth          {"auth_params": {"USERNAME", "PASSWORD"}} -> {"token"}; the next two send it in the header `token`
    /wells/get     {"organization_id", "well_id"} -> {"wells": [the well]}; an empty list: the product does not hold it
    /wells/update  {"organization_id", "well_id", "update_values": {<map>: {...}}}

The merge (`merge`).  The product replaces each map it is sent whole, so for pump_parameters and wellbore_parameters
the map sent is the stored map with the entry's keys laid over it: every other stored key stays, the retired pump_id,
stages and setting_depth_ft among them.  A map the entry has no key in is not sent, since an empty map sent would
delete the stored one.  An entry with no pump_sections therefore leaves the stored string in place.  The read and the
write are two calls with no lock: an edit made in the product between them is lost.

The outcome (`write` returns it, and never raises):
    written   the update was sent and answered 200
    skipped   nothing to send, with `reason`: an entry not on Test wells (no call is made); a well of lab-check, data
              science's own organisation, which the product never holds (no call); a well the product does not hold
    failed    anything else that stopped it, with `stage` (secret, auth, get or update: the call that failed, or was
              not started for want of time), the product's `status_code` when it answered, and `message`
    dry run   with go=False: read and merged, nothing sent
`maps` says, for each map, whether it was sent, the keys written and the stored keys kept; `pump_string` whether the
stored string was replaced, added, kept or none.  `stored` holds the two maps as read, for the dry run to print.

The run log gets one row per write (`log_row`): laid out as the confirmation's (lambda_function.log_row), under the
same confirmation id, with the type WRITE_BACK, written once when the write ends: SUCCEEDED for written or skipped,
FAILED otherwise.  Its data_json holds RECORDED of the outcome: keys, never values, which the entry's file holds.

A failure's message is the product's `message` and nothing else of its answer: a 422 echoes the request in `input` and
`details`, the login's too.  Nothing here logs or returns the login or the token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import confirmation as C
from .intake import RUN_ID
from .well_entries_schema import MAP_OF, PARAMETERS

SECRET_ENV = "ESP_WRITEBACK_SECRET"     # names the secret; the stack sets it on the writer
TABLE_ENV = "ESP_RUN_LOG_TABLE"
ENVIRONMENT = "dev"                     # Test wells: the one environment written, and the one block of the secret read
LAB_CHECK = "lab-check"                 # data science's own organisation (app/review.py's LAB_CHECK, restated)
TIMEOUT_S = 10                          # each call's
RESERVE_S = 15                          # kept beyond a call's timeout: a slow connect, the row and the last log line
TYPE = "WRITE_BACK"                     # the row's type in the run log
WRITTEN, SKIPPED, FAILED, DRY_RUN = "written", "skipped", "failed", "dry run"
RECORDED = ("outcome", "reason", "maps", "pump_string", "stage", "status_code", "message")
CLI = "python -m extraction.write_back"
STRING = {"replaced": "the stored one is replaced by the entry's", "added": "the entry's is added; none was stored",
          "kept": "the stored one is kept; the entry has none", "none": "none stored, and none in the entry"}


class Stop(Exception):
    """What stopped a write: a message, and the product's status code when it answered."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message, self.status_code = message, status_code


def now() -> datetime:
    return datetime.now(timezone.utc)


def secrets_client(session=None, timeout_s: float = TIMEOUT_S):
    """A Secrets Manager client that tries once, within a call's time."""
    import boto3
    from botocore.config import Config
    return (session or boto3).client("secretsmanager", config=Config(
        connect_timeout=timeout_s, read_timeout=timeout_s, retries={"total_max_attempts": 1}))


def login(environment: str, secrets=None, timeout_s: float = TIMEOUT_S) -> dict:
    """The product's address and login for `environment`: the secret's `dev` block, and no other block.  Any other
    environment is refused before anything is read."""
    if environment != ENVIRONMENT:
        raise Stop(f"environment {environment!r} is not {ENVIRONMENT}: nothing else is written back")
    name = os.environ.get(SECRET_ENV)
    if not name:
        raise Stop(f"{SECRET_ENV} is not set")
    try:
        secret = (secrets or secrets_client(timeout_s=timeout_s)).get_secret_value(SecretId=name)["SecretString"]
        block = json.loads(secret)[ENVIRONMENT]
        found = {field: block[field] for field in ("USERNAME", "PASSWORD", "api_base_url")}
    except Exception as err:            # noqa: BLE001 - an AWS error names the secret; a parse error a place or a field
        raise Stop(f"the secret cannot be read: {type(err).__name__}: {err}") from None
    if not all(isinstance(v, str) and v for v in found.values()) or not found["api_base_url"].startswith("https://"):
        raise Stop(f"the secret's {ENVIRONMENT} block needs USERNAME, PASSWORD and an https api_base_url")
    return found


def post(url: str, body: dict, *, token: str | None = None, timeout_s: float = TIMEOUT_S) -> dict:
    """One POST to the product, and its JSON answer.  Tried once, and a redirect is not followed.  Anything but a 200
    holding a JSON object stops, with the status code and the answer's `message` alone."""
    import urllib3
    try:
        response = urllib3.request("POST", url, json=body, headers={"token": token} if token else None,
                                   timeout=urllib3.Timeout(total=timeout_s), retries=False, redirect=False)
    except urllib3.exceptions.HTTPError as err:     # a timeout, or no connection: it names the host, never the body
        raise Stop(f"{type(err).__name__}: {err}") from None
    try:
        answer = response.json()
    except ValueError:
        answer = None
    if response.status != 200 or not isinstance(answer, dict):
        message = answer.get("message") if isinstance(answer, dict) else None
        raise Stop(str(message)[:500] if message else "the answer has no message", response.status)
    return answer


def merge(row: dict, held: dict) -> tuple[dict, dict, str]:
    """(update_values, what happens to each map, what happens to the stored string), for the entry `row` laid over
    the well as /wells/get returned it."""
    update, maps = {}, {}
    for field in PARAMETERS:
        entry, stored = row.get(field) or {}, held.get(field) or {}
        maps[field] = {"sent": bool(entry), "written": list(entry), "kept": [key for key in stored if key not in entry]}
        if entry:
            update[field] = {**stored, **entry}
    field = MAP_OF[C.PUMP_SECTIONS]
    had, has = C.PUMP_SECTIONS in (held.get(field) or {}), C.PUMP_SECTIONS in (row.get(field) or {})
    return update, maps, ("replaced" if had else "added") if has else ("kept" if had else "none")


def write(row: dict, *, go: bool = True, secrets=None, timeout_s: float = TIMEOUT_S, time_left=None, log=None) -> dict:
    """Write one entry back, or with go=False only read and merge.  `secrets` is a Secrets Manager client (by default
    one built here).  `time_left()` gives the seconds the caller still has: no call starts with less than its timeout
    and RESERVE_S.  `log(call, **fields)` is told of each call."""
    out = {"outcome": None, "reason": None, "maps": None, "pump_string": None, "stage": None, "status_code": None,
           "message": None, "stored": None}
    say = log or (lambda call, **fields: None)
    stage = None

    def start(call: str) -> None:
        nonlocal stage
        stage = call
        if time_left is not None and (left := time_left()) < timeout_s + RESERVE_S:
            raise Stop(f"not started: {left:.0f} s left, and a call is given {timeout_s + RESERVE_S:g} s")

    try:
        organization_id, well_id, environment = row["organization_id"], row["well_id"], row["environment"]
        if environment != ENVIRONMENT:
            return {**out, "outcome": SKIPPED,
                    "reason": f"environment {environment}: only {ENVIRONMENT}, Test wells, is written back"}
        if organization_id == LAB_CHECK:
            return {**out, "outcome": SKIPPED,
                    "reason": f"{LAB_CHECK} is data science's own organisation: the product does not hold its wells"}
        start("secret")
        account = login(environment, secrets, timeout_s)
        say("secret")
        base, well = account["api_base_url"].rstrip("/"), {"organization_id": organization_id, "well_id": well_id}
        start("auth")
        token = post(f"{base}/auth", {"auth_params": {name: account[name] for name in ("USERNAME", "PASSWORD")}},
                     timeout_s=timeout_s).get("token")
        if not (isinstance(token, str) and token):
            raise Stop("the answer holds no token", 200)
        say("auth", status_code=200)
        start("get")
        wells = post(f"{base}/wells/get", well, token=token, timeout_s=timeout_s).get("wells")
        if not isinstance(wells, list):
            raise Stop("the answer holds no list of wells", 200)
        say("get", status_code=200, wells=len(wells))
        held = next((w for w in wells if isinstance(w, dict) and w.get("well_id") == well_id), None)
        if held is None:
            return {**out, "outcome": SKIPPED,
                    "reason": "the product does not hold this well (the login reaches its organisation)"}
        update, maps, string = merge(row, held)
        out = {**out, "maps": maps, "pump_string": string,
               "stored": {field: held.get(field) or {} for field in PARAMETERS}}
        if not update:
            return {**out, "outcome": SKIPPED, "reason": "the entry holds no value"}
        if not go:
            return {**out, "outcome": DRY_RUN}
        start("update")
        post(f"{base}/wells/update", {**well, "update_values": update}, token=token, timeout_s=timeout_s)
        say("update", status_code=200)
        return {**out, "outcome": WRITTEN}
    except Exception as err:            # noqa: BLE001 - a write never raises: the caller's own work is already final
        code = err.status_code if isinstance(err, Stop) else None
        message = err.message if isinstance(err, Stop) else f"{type(err).__name__}: {err}"
        say(stage, status_code=code, error=message)
        return {**out, "outcome": FAILED, "stage": stage, "status_code": code, "message": message}


def log_row(row: dict, outcome: dict, *, resource_name: str, started: datetime, ended: datetime, data=None) -> dict:
    """The write's row in the run log: the confirmation's layout and ids, the type TYPE, and in data_json `data`, the
    entry's run and environment, and RECORDED of the outcome."""
    organization_id, cid = row["organization_id"], row["confirmation_id"]
    status = "FAILED" if outcome["outcome"] == FAILED else "SUCCEEDED"
    return {"organization_id": organization_id, "timestamp": C.timestamp(started), "type": TYPE,
            "organization_id_type": f"{organization_id}#{TYPE}", "pipeline_execution_id": cid, "uuid": cid,
            "resource_name": resource_name, "well_ids": [row["well_id"]], "status": status,
            "success": status == "SUCCEEDED", "end_timestamp": C.timestamp(ended),
            "data_json": json.dumps({**(data or {}), "run_id": row["run_id"], "environment": row["environment"],
                                     **{name: outcome[name] for name in RECORDED}}, sort_keys=True)}


# ---- the CLI: a rerun of one confirmation's write, and the one-time pass ----

HELP = f"""Write confirmed entries on Test wells back to the ESP product's test system: one confirmation, or the pass.

    {CLI} --confirmation ID [--well ORGANISATION WELL] [--go] [--profile NAME]
    {CLI} --pass [--well ORGANISATION WELL ...] [--go] [--profile NAME]

--confirmation reads that confirmation's entry, well-entries/environment=dev/<ID>.json.  --pass reads every entry
there and takes each well's current one: its latest confirmed_at, the confirmation id breaking a tie.

Without --go it is a dry run.  For each well it logs in and reads the well, then prints whether the product holds it
and the login reaches it, each key's stored and new value, and what happens to the pump string.  It sends no update
and writes no run-log row.

With --go it writes only the wells named with --well, and refuses to run with none named, or with one that has no
entry in what was read.  Each write leaves its WRITE_BACK row in the run log.

Set {SECRET_ENV} to the secret's name, and with --go {TABLE_ENV} to the run log's table.
Exit status: 0 nothing failed; 1 a well's read, its write or its row failed; 2 nothing was run.
"""


def confirmation_id(value: str) -> str:
    if not RUN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError(f"not a confirmation id (YYYYMMDDTHHMMSSZ-<8 hex>): {value!r}")
    return value


def aws(profile: str | None, timeout_s: float):
    """The bucket as objects, a Secrets Manager client and DynamoDB, as the profile's role."""
    import boto3

    from .bedrock import REGION
    session = boto3.Session(profile_name=profile, region_name=REGION)
    return C.S3Objects(session.client("s3")), secrets_client(session, timeout_s), session.resource("dynamodb")


def current_entries(objects) -> list[dict]:
    """Each well's current entry on Test wells, each read from its own file: the rule of the view
    well_entries_current (`C.latest`)."""
    # ponytail: reads every row under environment=dev/; ask the view for the keys once they number thousands
    current = {}
    for key in objects.keys(C.row_prefix(ENVIRONMENT)):
        row = C.load(objects.get(key))
        well = row["organization_id"], row["well_id"]
        if well not in current or C.latest(row) > C.latest(current[well]):
            current[well] = row
    return [current[well] for well in sorted(current)]


def bare(value):
    """A value less its null fields: the product returns as_of, note and section_role as null where it holds none."""
    if isinstance(value, dict):
        return {k: bare(v) for k, v in value.items() if v is not None}
    return [bare(v) for v in value] if isinstance(value, list) else value


def show(envelope) -> str:
    return "nothing" if envelope is None else json.dumps(bare(envelope), ensure_ascii=False, sort_keys=True)


def report(row: dict, outcome: dict) -> list[str]:
    """One well's outcome in lines: whether the product holds it and the login reaches it, each key's stored and new
    value, and what happens to the string."""
    out = [f"{row['organization_id']} / {row['well_id']}   confirmation {row['confirmation_id']}: {outcome['outcome']}"]
    if outcome["outcome"] == SKIPPED:
        out.append(f"  skipped: {outcome['reason']}")
    if outcome["outcome"] == FAILED:
        code = outcome["status_code"]
        out.append(f"  failed at {outcome['stage']}{f' with {code}' if code else ''}: {outcome['message']}")
        if code == 403:
            out.append("  the login does not reach this organisation")
    if outcome["stored"] is None:
        return out
    out.append("  the product holds this well, and the login reaches it")
    for field in PARAMETERS:
        entry, stored, what = row.get(field) or {}, outcome["stored"][field], outcome["maps"][field]
        out.append(f"  {field}: {'sent' if what['sent'] else 'not sent, the entry has no key in it'}")
        for key in what["written"]:
            same = "  (the same)" if key in stored and bare(stored[key]) == bare(entry[key]) else ""
            out.append(f"    {key}: stored {show(stored.get(key))} -> new {show(entry[key])}{same}")
        out += [f"    {key}: stored {show(stored[key])}, kept" for key in what["kept"]]
    return [*out, f"  pump string: {STRING[outcome['pump_string']]}"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=CLI, description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--confirmation", metavar="ID", type=confirmation_id, help="one confirmation on Test wells")
    which.add_argument("--pass", dest="every", action="store_true", help="every well's current entry on Test wells")
    ap.add_argument("--well", nargs=2, action="append", default=[], metavar=("ORGANISATION", "WELL"),
                    help="only this well (repeatable); --go writes only the wells named")
    ap.add_argument("--go", action="store_true", help="write the named wells; without it, a dry run")
    ap.add_argument("--profile", help="the AWS profile to run as")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_S, help=f"seconds for each call (default {TIMEOUT_S})")
    args = ap.parse_args(argv)
    named = [tuple(well) for well in args.well]
    if args.go and not named:
        print("--go writes only the wells named: give --well ORGANISATION WELL for each; nothing run", file=sys.stderr)
        return 2
    unset = [name for name in ((SECRET_ENV, TABLE_ENV) if args.go else (SECRET_ENV,)) if not os.environ.get(name)]
    if unset:
        print(f"set {' and '.join(unset)}; nothing run", file=sys.stderr)
        return 2

    objects, secrets, dynamodb = aws(args.profile, args.timeout)
    if args.every:
        rows = current_entries(objects)
    else:
        try:
            rows = [C.load(objects.get(C.row_key(ENVIRONMENT, args.confirmation)))]
        except FileNotFoundError:
            print(f"{args.confirmation}: no entry on Test wells has this confirmation id; nothing run", file=sys.stderr)
            return 2
    wells = [(row["organization_id"], row["well_id"]) for row in rows]
    unknown = [well for well in named if well not in wells]
    if unknown:
        print("no entry read for " + "; ".join(f"{o} / {w}" for o, w in unknown) + "; nothing run", file=sys.stderr)
        return 2
    rows = [row for row, well in zip(rows, wells) if not named or well in named]
    print(f"{'writing' if args.go else 'dry run of'} {len(rows)} of {len(wells)} entries on Test wells")
    table, failed = dynamodb.Table(os.environ[TABLE_ENV]) if args.go else None, False
    for row in rows:
        started = now()
        outcome = write(row, go=args.go, secrets=secrets, timeout_s=args.timeout)
        print("\n".join(report(row, outcome)))
        failed = failed or outcome["outcome"] == FAILED
        if args.go:
            try:
                table.put_item(Item=log_row(row, outcome, resource_name=CLI, started=started, ended=now()))
            except Exception as err:    # noqa: BLE001 - said, and the next well still runs
                print(f"  its run-log row was not written: {type(err).__name__}: {err}", file=sys.stderr)
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
