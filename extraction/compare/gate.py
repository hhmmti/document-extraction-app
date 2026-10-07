"""Gate re-check: do a later run's values fix the gate fields an adjudicated worksheet marked wrong?

Deterministic; no model. The worksheet is read, never rewritten.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .parity import Alignment, _field_diffs, canon, classify, match_key, raw, row_key

GATE_FAMILIES = ("F1", "F3", "F5", "F7")
GATE_VERDICTS = ("old right", "both wrong")


def read_worksheet(path: Path, encoding: str = "mac_roman") -> tuple[list[str], list[dict]]:
    """(header, rows) of a worksheet a spreadsheet has saved. Cells the spreadsheet left
    alone keep the comparator's UTF-8 bytes; cells typed into it are in the spreadsheet's
    own encoding. Each cell is decoded on its own: UTF-8 where its bytes are valid UTF-8,
    `encoding` otherwise. Unnamed trailing columns are dropped."""
    def cell(v: str) -> str:
        b = v.encode("latin-1")
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode(encoding)

    # latin-1 maps each byte to one character, so every cell can be re-encoded exactly.
    reader = csv.reader(io.StringIO(Path(path).read_bytes().decode("latin-1"), newline=""))
    header = [cell(h) for h in next(reader)]
    keep = [i for i, h in enumerate(header) if h.strip()]
    rows = [{header[i]: cell(r[i]) if i < len(r) else "" for i in keep} for r in reader if any(r)]
    return [header[i] for i in keep], rows


def _norm(v: str) -> str:
    return " ".join((v or "").split()).casefold()


def selected(rows: list[dict], families: tuple = GATE_FAMILIES) -> list[dict]:
    """Rows of `families` on a gate field (pump_config, or any trap field) judged `old right` or `both wrong`."""
    return [r for r in rows if r["family"].strip() in families
            and (r["table"].strip() == "stg_pump_config" or _norm(r["is_trap"]) == "true")
            and _norm(r["verdict"]) in GATE_VERDICTS]


def _key(table: str, s: str, r: dict) -> list:
    k = match_key(s, r) if table == "stg_curve_observations" else row_key(table, s, r)
    return json.loads(json.dumps(list(k), ensure_ascii=False))


def high_values(blocks: dict, table: str, key_text: str, field: str) -> list[str]:
    """The later report's values for one worksheet item: the rows with the same row key, or,
    for a trap item (its row key is the trap citation), the field across the whole table."""
    rows = blocks.get(table, [])
    if not key_text.startswith("["):
        return [raw(r[field]) for r in rows if field in r]
    al, key = Alignment.of(blocks), json.loads(key_text)
    rows = [r for r in rows if _key(table, al.scenario(r)[0], r) == key]
    return [canon(r) for r in rows] if field == "(row)" else [raw(r[field]) for r in rows if field in r]


def _same(field: str, a: str, b: str) -> bool:
    """Equal under the comparator's rules: identical, or differing only as `format`."""
    if not a or not b:
        return False
    if field == "(row)":
        return all(d.category == "format" for d in _field_diffs("", (), json.loads(a), json.loads(b)))
    return a == b or classify(a, b) == "format"


def recheck(row: dict, blocks: dict | None) -> tuple[str, str]:
    """(high_value, status) for one worksheet row against the later report's `stg_*` blocks.

    `high_value` is every distinct value found for the item, joined with "; ". The status
    compares that whole value with the old and the medium value under the comparator's
    `format` rule, so a list of several values never matches a single one. A non-empty
    `high_value` is never `absent`."""
    if blocks is None:
        return "", "absent"
    field, old, medium = row["field"], row["old_value"], row["new_value"]
    values = list(dict.fromkeys(high_values(blocks, row["table"], row["row_key"], field)))
    high = "; ".join(values)
    if not values:
        return "", "absent"

    def same(target: str) -> bool:
        if field == "(row)":  # whole rows compare field by field, and only one at a time
            return len(values) == 1 and _same(field, target, values[0])
        return _same(field, target, high)

    if same(old):
        # For `both wrong`, reproducing the old value is not a fix.
        return high, "needs_review" if _norm(row["verdict"]) == "both wrong" else "matches_old"
    if same(medium):
        return high, "matches_medium"
    return high, "other"
