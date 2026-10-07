"""Typing a verbatim staging string into a load value, or refusing to.

Every staging value arrives as a string with its printed unit token attached
(D8 layer 1).  This module is D8 layer 3 for scalars: it parses the number,
reads the token, and decides one of three things.

  accept      the token agrees with what the column promises
  convert     the token is the same physical dimension at a different scale
              (`84.62 %` into `water_cut_design_frac`) -- converted here,
              because D8 puts unit canonicalisation in the transform, and
              logged so that no conversion is hidden
  quarantine  the value cannot be loaded cleanly

Nothing is ever repaired.  An empty string is not zero, a range is not a
scalar, and `93.2 hp` in a kilowatt column is not 69.5 kW -- it is a value in
the wrong column, and only a human can say which.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from schemas import (
    FRACTION_COLUMNS,
    UNIT_DIMENSION,
    column_dimension,
)

_NUMBER = re.compile(r"^\s*([-+]?[\d,]*\.?\d+)\s*(.*)$", re.S)
_RANGE = re.compile(r"^[-+]?[\d,.]+\s*(?:-|–|to)\s*[\d,.]+")
_PARENTHETICAL = re.compile(r"\([^)]*\)")


@dataclass
class Parsed:
    value: float | None = None
    unit_token: str | None = None
    reason: str | None = None          # set when the value is quarantined
    conversion: str | None = None      # set when a scale conversion was applied


def _clean_token(token: str) -> str:
    token = token.strip().strip(".,;")
    return token.lower()


def parse_scalar(raw, column: str) -> Parsed:
    """Type one staging value for one load column."""
    if raw is None:
        return Parsed()
    if not isinstance(raw, str):
        return Parsed(value=float(raw)) if isinstance(raw, (int, float)) else Parsed(
            reason="value_not_a_string"
        )

    text = raw.strip()
    if text == "":
        # `"oil_sg": ""` in F5 PRIEST 233H.  Neither a value nor an absence;
        # coerced it becomes a real zero.
        return Parsed(reason="empty_string")

    if _RANGE.match(text):
        return Parsed(reason="range_not_scalar")

    match = _NUMBER.match(text)
    if not match:
        return Parsed(reason="not_numeric")

    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return Parsed(reason="not_numeric")

    remainder = match.group(2).strip()
    # `400 (400) hp` -- the vendor prints the value twice, once parenthesised.
    stripped = _PARENTHETICAL.sub("", remainder).strip()
    token = _clean_token(stripped if stripped else remainder)

    # A remainder holding another number is a second value, not a unit.
    if re.search(r"\d", token) and token not in UNIT_DIMENSION:
        return Parsed(reason="composite_value")

    promised = column_dimension(column)
    if not token:
        return Parsed(value=value, unit_token=None)

    if token not in UNIT_DIMENSION:
        return Parsed(value=value, unit_token=token, reason="unrecognized_unit_token")

    actual = UNIT_DIMENSION[token]
    if actual is None:                 # dimension-neutral token, e.g. a bare `°`
        return Parsed(value=value, unit_token=token)

    if promised is None:
        return Parsed(value=value, unit_token=token)

    if actual != promised:
        # `vendor_motor_power_kw: "93.2 hp"`, `power_as_printed: "... PSIA"`,
        # `fluid_sg_at_observation: "58.3 lb/ft3"`.
        return Parsed(value=None, unit_token=token, reason="unit_dimension_mismatch")

    # Same dimension: canonicalise scale where the two printed forms differ.
    if promised == "ratio":
        wants_fraction = column in FRACTION_COLUMNS
        if token == "%" and wants_fraction:
            return Parsed(value=value / 100.0, unit_token="%",
                          conversion=f"{value} % -> {value / 100.0} (fraction)")
        if token == "(0-1)" and not wants_fraction:
            return Parsed(value=value * 100.0, unit_token="(0-1)",
                          conversion=f"{value} (0-1) -> {value * 100.0} %")
        if token == "%" and not wants_fraction:
            return Parsed(value=value, unit_token="%")
        if token == "(0-1)" and wants_fraction:
            return Parsed(value=value, unit_token="(0-1)")
        # A bare number on a fraction column that reads like a percentage is
        # NOT rescaled -- guessing the scale from magnitude is exactly the kind
        # of judgement this module refuses to make.
        return Parsed(value=value, unit_token=token)

    return Parsed(value=value, unit_token=token)


def parse_int(raw, column: str) -> Parsed:
    parsed = parse_scalar(raw, column)
    if parsed.value is not None:
        if abs(parsed.value - round(parsed.value)) > 1e-9:
            return Parsed(value=None, unit_token=parsed.unit_token,
                          reason="non_integer_in_integer_column")
        parsed.value = int(round(parsed.value))
    return parsed


def parse_bool(raw):
    """Verbatim `"true"` / `"false"`; anything else is not a boolean."""
    if raw is None:
        return Parsed()
    text = str(raw).strip().lower()
    if text == "":
        return Parsed(reason="empty_string")
    if text in ("true", "yes", "y"):
        return Parsed(value=True)
    if text in ("false", "no", "n"):
        return Parsed(value=False)
    return Parsed(reason="not_boolean")


def parse_list(raw):
    """`stages_per_housing_as_printed` can be a comma-separated list.

    F2's GOUDA prints `42 + 93x4 = 414` as `"42, 93, 93, 93, 93"`.  Parsing it
    as a scalar reads 42 and silently loses 372 stages.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    return parts or None


def parse_date(raw):
    """`effective_from` from frontmatter.  Absent means absent -- never today."""
    if raw is None:
        return None
    if isinstance(raw, list):          # `effective_from:` with an empty value
        return None
    text = str(raw).strip().strip('"').strip("'")
    if text in ("", "null", "None", "[]", "-"):
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    return None


def is_absent_well_id(raw) -> bool:
    if raw is None or isinstance(raw, list):
        return True
    return str(raw).strip().strip('"') in ("", "null", "None", "[]", "-")
