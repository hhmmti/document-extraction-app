"""The wells the upload form offers: the product's live well lists, read from Athena, one per environment.

    cached(environment) -> (WellList, None) or (None, the error)     read once, kept until refresh(environment)

prd is the product's production list, dev its test list: the Athena data sources roam_prd_ddb and roam_dev_ddb in the
datascience account, each a DynamoDB connector over default.esp_well_configuration_v2.  One query per environment
(`read`), through an Athena client of the app's one AWS session (app/hosted.py): start it, poll it, page through its
results.  ESP_ATHENA_OUTPUT_LOCATION and
ESP_ATHENA_WORKGROUP name the query results location and the workgroup.  A failure is kept as well, until the form's
Retry reads again; nothing falls back to a CSV.

Every value is kept exactly as returned: no strip, no case-folding, no collapsing of spaces.  Exact duplicate pairs are
dropped; a row with an empty organization_id or well_id is skipped and counted.  Names that differ only by case or by
characters that don't show stay separate (`near`).  The screens show each such character as its sign (`marked`, SIGNS),
say what the signs that appear mean (`key`), and never show two names of one list alike (`labels`).

`load` reads the older snapshot, the same columns exported from Athena as CSV and named by ESP_WELL_LIST_PATH, for
tools/detect_survey.py and tools/check_client_names.py; the form no longer reads it.  It keeps every value the same way.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WELL_LIST_ENV = "ESP_WELL_LIST_PATH"
COLUMNS = ("organization_id", "well_id")


class SnapshotError(Exception):
    """The snapshot cannot be used.  The message says why, without quoting any value."""


def display_order(value: str):
    """Case-insensitive; the exact value breaks ties, so the order is stable."""
    return value.casefold(), value


@dataclass(frozen=True)
class Snapshot:
    modified: datetime
    sha256: str
    rows: int                  # data rows as exported
    pairs: tuple               # (organization_id, well_id), exact duplicates removed

    def organisations(self) -> list[str]:
        return sorted({org for org, _ in self.pairs}, key=display_order)

    def wells(self, organisation: str) -> list[str]:
        return sorted({well for org, well in self.pairs if org == organisation}, key=display_order)

    def case_variants(self, organisation: str) -> list[str]:
        return [org for org in self.organisations()
                if org != organisation and org.casefold() == organisation.casefold()]


def load(path) -> Snapshot:
    path = Path(path)
    try:
        data = path.read_bytes()
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError as err:
        raise SnapshotError(f"it cannot be read ({err.strerror or type(err).__name__})") from err
    try:
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader, None)
        if header is None:
            raise SnapshotError("it is empty: there is no header row")
        missing = [col for col in COLUMNS if col not in header]
        if missing:
            raise SnapshotError(f"it has no {' or '.join(missing)} column")
        org, well = header.index(COLUMNS[0]), header.index(COLUMNS[1])
        rows = []
        for row in reader:
            if not row:                               # a blank line holds no row
                continue
            if len(row) != len(header):
                raise SnapshotError(f"line {reader.line_num} has {len(row)} fields; the header has {len(header)}")
            rows.append((row[org], row[well]))
    except (UnicodeDecodeError, csv.Error) as err:
        raise SnapshotError(f"it is not a readable CSV ({type(err).__name__})") from err
    return Snapshot(modified, hashlib.sha256(data).hexdigest(), len(rows), tuple(dict.fromkeys(rows)))


# ---- the live lists ----

OUTPUT_ENV, OUTPUT_DEFAULT = "ESP_ATHENA_OUTPUT_LOCATION", "s3://aws-athena-query-results-640168431387-us-east-1/"
WORKGROUP_ENV, WORKGROUP_DEFAULT = "ESP_ATHENA_WORKGROUP", "primary"
REGION = "us-east-1"                          # the datascience account's, where both data sources are
SOURCES = {"prd": "roam_prd_ddb", "dev": "roam_dev_ddb"}
POLL_S, TIMEOUT_S = 1, 120
SPACE, NO_BREAK, TAB, OTHER_SPACE, NO_WIDTH_SIGN = "␣", "°", "→", "▫", "¦"
SIGNS = {SPACE: "a space that would not show", NO_BREAK: "a no-break space", TAB: "a tab",   # what each sign marks
         OTHER_SPACE: "another kind of space", NO_WIDTH_SIGN: "a character with no width"}
# A character with no width: a control character that is no space, or one Unicode says a renderer may show as nothing
# (DerivedCoreProperties.txt, Default_Ignorable_Code_Point: zero-width spaces and joiners, direction marks, soft hyphen,
# variation selectors, fillers).
NO_WIDTH = re.compile(r"[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
                      r"\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
                      r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")


@dataclass(frozen=True)
class WellList:
    environment: str
    read_at: datetime
    pairs: tuple               # (organization_id, well_id), exactly as returned, exact duplicates dropped
    skipped: int               # rows with an empty organization_id or well_id

    def organisations(self) -> list[str]:
        return sorted({org for org, _ in self.pairs}, key=display_order)

    def wells(self, organisation: str) -> list[str]:
        return sorted({well for org, well in self.pairs if org == organisation}, key=display_order)


def query(environment: str) -> str:
    return f'SELECT organization_id, well_id FROM "{SOURCES[environment]}"."default"."esp_well_configuration_v2"'


def athena_client():
    """An Athena client from the app's one AWS session (app/hosted.py), loaded only when the form reads a list."""
    from app import hosted
    return hosted.session().client("athena", region_name=REGION)


def read(environment: str, client=None, environ=os.environ) -> WellList:
    """The environment's list, read now: one query, polled every POLL_S until it ends (stopped after TIMEOUT_S), its
    results read a page at a time.  Raises what the client raises, or RuntimeError for a query that did not succeed."""
    client = client or athena_client()
    qid = client.start_query_execution(
        QueryString=query(environment), WorkGroup=environ.get(WORKGROUP_ENV) or WORKGROUP_DEFAULT,
        ResultConfiguration={"OutputLocation": environ.get(OUTPUT_ENV) or OUTPUT_DEFAULT})["QueryExecutionId"]
    end = time.monotonic() + TIMEOUT_S
    while True:
        status = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if status["State"] == "SUCCEEDED":
            break
        if status["State"] in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"query {qid} {status['State']}: {status.get('StateChangeReason', 'no reason given')}")
        if time.monotonic() > end:
            client.stop_query_execution(QueryExecutionId=qid)
            raise RuntimeError(f"query {qid} still {status['State']} after {TIMEOUT_S} s: stopped")
        time.sleep(POLL_S)
    rows, kwargs = [], {"QueryExecutionId": qid}
    while True:
        page = client.get_query_results(**kwargs)
        rows += [[cell.get("VarCharValue", "") for cell in row["Data"]] for row in page["ResultSet"]["Rows"]]
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    header, *data = rows or [[]]                  # Athena's first row holds the column names
    if header != list(COLUMNS):
        raise RuntimeError(f"query {qid} returned the columns {header}, not {list(COLUMNS)}")
    pairs = [(org, well) for org, well in data if org != "" and well != ""]     # a NULL reads as ""
    return WellList(environment, datetime.now(timezone.utc), tuple(dict.fromkeys(pairs)), len(data) - len(pairs))


_LISTS: dict[str, tuple] = {}       # environment -> (WellList, None) or (None, the error), for this app process


def cached(environment: str) -> tuple:
    """(the list, None), or (None, the error): read once per environment, then kept until `refresh`."""
    if environment not in _LISTS:
        try:
            _LISTS[environment] = (read(environment), None)
        except Exception as err:  # noqa: BLE001 - credentials, permissions, the query: the form says so, then Retry
            _LISTS[environment] = (None, err)
    return _LISTS[environment]


def refresh(environment: str) -> None:
    """Forget the environment's list, or its failure: the next `cached` reads it again."""
    _LISTS.pop(environment, None)


def near(names) -> set[str]:
    """The names that differ from another of `names` only by case or by characters that don't show."""
    groups = defaultdict(set)
    for name in names:
        groups[" ".join(NO_WIDTH.sub("", name).split()).casefold()].add(name)
    return {name for group in groups.values() if len(group) > 1 for name in group}


def sign(char: str) -> str | None:
    """The sign `marked` shows for a character that would not show, whatever its place; None for any other, a plain
    space too (`marked` signs it only where it would not show)."""
    if char in "\u00a0\t":
        return NO_BREAK if char == "\u00a0" else TAB
    if char.isspace() and char != " ":
        return OTHER_SPACE
    return NO_WIDTH_SIGN if NO_WIDTH.fullmatch(char) else None


def marked(name: str) -> str:
    """The name with each character that would not show as its sign: ° a no-break space, → a tab, ▫ any other space
    character, ¦ a character with no width, and ␣ a plain space at an edge or in a run of two or more.  One sign per
    character, so the name keeps its length."""
    return re.sub(r"^ +| +$| {2,}", lambda m: SPACE * len(m[0]), "".join(sign(c) or c for c in name))


def key(names) -> str:
    """One line, in plain words, on the signs `marked` shows in these names; "" when it shows none."""
    shown = {s for name in names for c, s in zip(name, marked(name)) if s != c}
    said = [f"{s} {words}" for s, words in SIGNS.items() if s in shown]
    if not said:
        return ""
    said[0] = said[0].replace(" ", " marks ", 1)
    return (f"{', '.join(said[:-1])} and {said[-1]}" if len(said) > 1 else said[0]) + "."


def character_words(char: str) -> str:
    return unicodedata.name(char, "").lower() or {"\t": "tab"}.get(char) or f"U+{ord(char):04X}"


def labels(names) -> dict[str, str]:
    """Each name of one list as the list shows it, no two alike, so a choice maps back to exactly its name: `marked`,
    and where two still mark alike (two kinds of space behind ▫, say, or a sign written in a name), each followed by the
    characters behind its signs, in words.  A name written as another's label is then numbered."""
    names = list(dict.fromkeys(names))
    shown = {name: marked(name) for name in names}
    alike, out, used = Counter(shown.values()), {}, set()
    for name in names:
        label = shown[name]
        if alike[label] > 1:
            label += f" ({', '.join(character_words(c) for c, s in zip(name, label) if s in SIGNS)})"
        base, n = label, 1
        while label in used:
            n += 1
            label = f"{base} ({n})"
        out[name] = label
        used.add(label)
    return out
