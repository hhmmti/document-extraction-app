"""The well list the upload form offers: a snapshot of the well configuration, exported from
Athena as CSV and named by ESP_WELL_LIST_PATH.

Every value is kept exactly as exported: no strip, no case-folding, no collapsing of spaces.
Only exact duplicate rows are removed.  Organisations that differ only by case stay separate.
"""

from __future__ import annotations

import csv
import hashlib
import io
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
