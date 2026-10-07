"""Parity: a new extraction report against the old batch report for the same document.

Deterministic. No model is involved anywhere in the comparison.

The `stg_*` blocks of each report are JSONL under a `## stg_<table>` heading.

1. **Scenarios are aligned first.** Each report maps its rows to a scenario
   ordinal using its own scenario table: every row that prints a label beside
   an ordinal. A row with an ordinal keeps it; a row with only a label takes
   the label's ordinal; a row with no scenario key belongs to the report's only
   scenario when the report has exactly one. Anything else is `unaligned`.
2. **Rows are matched by key** (`row_key`, `MATCH_KEY` for curves; curve rows
   left over also pair when they agree on every key field both print), and every
   field that differs on a matched row gets exactly one category:
   `added` (absent in old), `dropped` (absent in new), `format` (same parsed
   number(s), and the same unit token ignoring case or a unit on one side
   only) or `conflict` (anything else, including any differing unit token). Outside the curves table, a row on one side
   only is one `added` or `dropped` difference.
3. **Verdict.** `stg_pump_config` and the trap fields fail on `conflict` or
   `dropped`. Curves fail when the row count moves more than 5 %, an old curve
   is missing, or a matched row carries a `conflict`. Other tables inform.
4. **Structural differences** among curve rows are listed separately: a
   per-section row against a composite row for the same point, and rows split
   or merged on the same identity.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import types
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# The six parity documents, one per family, in run order (shortest assembled
# prompt first). Identified by the SHA-256 of the PDF, so no document name has
# to live in this repository.
PARITY_DOCS = [
    ("F4", "d630530e4b6b7c79b6b2cdf5948db6c408cbdef6266e32617323e63303c95292"),
    ("F5", "e073658133bc50398df7849ba7243be74f79722af9729aafb435ab767cb8d237"),
    ("F2", "c55ecf1b0789577878d2c2dc97b50ec6ceeead4772a42dd64c45c18ed1dba62d"),
    ("F3", "bb366c921b50d1b93c15b6e8c969021f131f81135d1b072807e0f04673634f0b"),
    ("F7", "83cc3aefeb0a77f8b2845e5cc461aea026a4bf3127bcaa0faca51bd35ebcbd5f"),
    ("F1", "1ad7b6c31418dd3d26e94f136cd5be53a509aaa8bf28b3cea4d7656da61ae578"),
]

# Trap fields: every staging column a contract's §6 trap names (corrections
# included), checked against the staging schemas. Traps that name no column,
# only an inventory row, are not listed; they are in each contract's §6.
TRAP_FIELDS = {
    "F1": [
        ("frequency_hz_as_printed", "f1-spyglass.md:§6 T1"),
        ("head_as_printed", "f1-spyglass.md:§6 T2"),
        ("head_basis_as_printed", "f1-spyglass.md:§6 T2"),
        ("stages_at_observation", "f1-spyglass.md:§6 T2"),
        ("flow_unit_as_printed", "f1-spyglass.md:§6 T4"),
        ("viscosity_corrected", "f1-spyglass.md:§6 T5"),
        ("bubble_point_unit_as_printed", "f1-spyglass.md:§6 T7"),
        ("depth_reference_basis", "f1-spyglass.md:§6 T8"),
    ],
    "F2": [
        ("motor_power_factor", "f2-championx.md:§6 T1"),
        ("motor_power_factor_basis", "f2-championx.md:§6 T1"),
        ("surface_power_factor", "f2-championx.md:§6 T1"),
        ("envelope_status", "f2-championx.md:§6 T3"),
        ("digitization_candidate_page", "f2-championx.md:§6 T3"),
        ("depth_reference_basis", "f2-championx.md:§6 T5"),
        ("observation_is_composite", "f2-championx.md:§6 T6"),
        ("head_basis_as_printed", "f2-championx.md:§6 T6"),
        ("bubble_point_correlation", "f2-championx.md:§6 T7"),
    ],
    "F3": [
        ("vendor_mixture_gradient_psi_per_ft", "f3-slb.md:§6 T3"),
        ("motor_power_factor", "f3-slb.md:§6 T4"),
        ("envelope_status", "f3-slb.md:§6 T5"),
        ("speed_rpm_as_printed", "f3-slb.md:§6 T7 (correction)"),
    ],
    "F4": [
        ("bubble_point_calculated_flag", "f4-els.md:§6 T1"),
        ("bubble_point_provenance", "f4-els.md:§6 T1"),
        ("gas_rate_as_printed", "f4-els.md:§6 T2 (correction)"),
        ("gas_rate_unit_as_printed", "f4-els.md:§6 T2 (correction)"),
        ("bubble_point_unit_as_printed", "f4-els.md:§6 T3"),
        ("motor_power_factor", "f4-els.md:§6 T4"),
        ("envelope_status", "f4-els.md:§6 T5"),
        ("observation_is_composite", "f4-els.md:§6 T5"),
        ("power_basis_as_printed", "f4-els.md:§6 T5"),
        ("vendor_mixture_sg", "f4-els.md:§6 T7 (correction)"),
    ],
    "F5": [
        ("motor_frequency_hz", "f5-baker.md:§6 T1"),
        ("frequency_hz_as_printed", "f5-baker.md:§6 T1"),
        ("design_frequency_hz", "f5-baker.md:§6 T1"),
        ("motor_type", "f5-baker.md:§6 T1"),
        ("depth_reference_basis", "f5-baker.md:§6 T2"),
        ("intake_set_depth_md_ft", "f5-baker.md:§6 T2"),
        ("pump_setting_md_ft", "f5-baker.md:§6 T2 (correction)"),
        ("extraction_status", "f5-baker.md:§6 T3"),
        ("stages", "f5-baker.md:§6 T3 (correction)"),
        ("stages_per_housing_as_printed", "f5-baker.md:§6 T3 (correction)"),
        ("sensitivity_case", "f5-baker.md:§6 T4"),
        ("scenario_label_staged", "f5-baker.md:§6 T4"),
        ("is_design_scenario", "f5-baker.md:§6 T4"),
        ("scenario_selection_rule", "f5-baker.md:§6 T4"),
        ("bubble_point_psi", "f5-baker.md:§6 T5"),
        ("bubble_point_unit_as_printed", "f5-baker.md:§6 T5"),
        ("bubble_point_provenance", "f5-baker.md:§6 T5"),
        ("envelope_status", "f5-baker.md:§6 T6"),
        ("head_basis_as_printed", "f5-baker.md:§6 T6"),
        ("observation_is_composite", "f5-baker.md:§6 T6"),
    ],
    "F7": [
        ("bubble_point_correlation", "f7-valiant.md:§6 T1"),
        ("viscosity_corrected", "f7-valiant.md:§6 T2"),
        ("viscosity_correction_factors_as_printed", "f7-valiant.md:§6 T2 (correction)"),
        ("turpin_indicator_as_printed", "f7-valiant.md:§6 T3 (correction)"),
        ("dunbar_indicator_as_printed", "f7-valiant.md:§6 T3 (correction)"),
        ("correlation_as_printed", "f7-valiant.md:§6 T3 (correction)"),
        ("gas_sg_basis", "f7-valiant.md:§6 T4"),
        ("gas_sg", "f7-valiant.md:§6 T4 (correction)"),
        ("gas_sg_mixture", "f7-valiant.md:§6 T4 (correction)"),
        ("intake_temp_f", "f7-valiant.md:§6 T5"),
        ("reservoir_temp_f", "f7-valiant.md:§6 T5"),
        ("surface_temp_f", "f7-valiant.md:§6 T5"),
        ("point_type", "f7-valiant.md:§6 T7"),
        ("flow_unit_as_printed", "f7-valiant.md:§6 T7"),
        ("efficiency_pct", "f7-valiant.md:§6 T7"),
        ("section_intake_pressure_psi_as_printed", "f7-valiant.md:§6 T8"),
    ],
}

# Curve identity and row match key, from the staging schema: curve_observations
# has grain model × point (M1); staging adds scenario identity,
# `curve_observation_role` (which printed table) and, for F5, `sensitivity_case`.
# `scenario` is the aligned ordinal. Frequency and flow are compared as parsed
# numbers, so `59.00` and `59.00 (Hz)` are one curve.
SCENARIO_LABEL_KEYS = ("scenario_label_staged", "scenario_label")
CURVE_IDENTITY = ("scenario", "curve_observation_role", "pump_model_as_printed",
                  "frequency_hz_as_printed", "sensitivity_case")
MATCH_KEY = CURVE_IDENTITY + ("point_type", "flow_as_printed", "head_basis_as_printed", "power_basis_as_printed")
CURVE_COUNT_TOLERANCE_PCT = 5.0


def _load_values():
    """values.py imports its unit tables from the loader's `schemas` module,
    which is not ported. It runs here against empty tables: a value with a
    plain unit reads as one number, while one whose remainder holds a digit,
    such as `0.70 (0-1)`, does not, and `numbers()` takes every number printed."""
    shim = types.ModuleType("schemas")
    shim.FRACTION_COLUMNS, shim.UNIT_DIMENSION = frozenset(), {}
    shim.column_dimension = lambda column: None
    prior = sys.modules.get("schemas")
    sys.modules["schemas"] = shim
    try:
        return importlib.import_module(f"{__package__}.values")
    finally:
        if prior is None:
            del sys.modules["schemas"]
        else:
            sys.modules["schemas"] = prior


values = _load_values()

_BLOCK = re.compile(r"^## (stg_\w+)[ \t]*\n```[a-z]*\n(.*?)^```", re.M | re.S)


def stg_blocks(text: str) -> tuple[dict[str, list[dict]], list[str]]:
    """{table: rows} for every `## stg_*` JSONL block, and any lines that are not JSON objects."""
    blocks, errors = {}, []
    for m in _BLOCK.finditer(text):
        rows = []
        for n, line in enumerate(m[2].splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                row = None
            if isinstance(row, dict):
                rows.append(row)
            else:
                errors.append(f"{m[1]} line {n}: not a JSON object")
        blocks[m[1]] = rows
    return blocks, errors


def raw(v) -> str | None:
    return v if v is None or isinstance(v, str) else json.dumps(v)


def canon(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


CATEGORIES = ("added", "dropped", "format", "conflict")
FAILING = ("conflict", "dropped")
_NUMBER = re.compile(r"[-+]?[\d,]*\.?\d+")  # values.py's number grammar, unanchored


def numbers(s: str) -> list[float]:
    """The number(s) in a raw value: values.py's scalar where it reads one, else every number printed."""
    p = values.parse_scalar(s, "")
    if p.value is not None:
        return [p.value]
    out = []
    for x in _NUMBER.findall(s):
        try:
            out.append(float(x.replace(",", "")))
        except ValueError:
            pass
    return out


def _rest(s: str) -> str:
    """What is left once the numbers are gone: unit text, words, punctuation, case-folded."""
    return " ".join(re.sub(r"[()\[\]]", " ", _NUMBER.sub(" ", s)).split()).casefold()


def classify(old, new) -> str:
    """The category of one differing field; `old` or `new` is None where the key is absent."""
    if old is None:
        return "added"
    if new is None:
        return "dropped"
    a, b = raw(old), raw(new)
    na, nb = numbers(a), numbers(b)
    # Format only where both sides read as the same number(s) and the unit token is the
    # same ignoring case, or printed on one side only. A differing unit is a conflict.
    if na and na == nb:
        ra, rb = _rest(a), _rest(b)
        if ra == rb or not ra or not rb:
            return "format"
    return "conflict"


def _num(v):
    if v is None:
        return None
    n = numbers(raw(v))
    return n[0] if len(n) == 1 else raw(v)


def _ord(v) -> str:
    n = _num(v)
    return str(int(n)) if isinstance(n, float) and n == int(n) else raw(v)


def _flag(v) -> bool:
    return v is not None and raw(v).strip().casefold() == "true"


@dataclass
class Alignment:
    """One report's own scenario table, and how its curve and section rows were aligned to it."""
    labels: dict = field(default_factory=dict)        # label -> ordinal
    ambiguous: dict = field(default_factory=dict)     # label printed beside more than one ordinal -> ordinals
    ordinals: set = field(default_factory=set)
    methods: Counter = field(default_factory=Counter)    # ordinal | label | sole scenario | unaligned
    unaligned: Counter = field(default_factory=Counter)  # label -> rows

    @classmethod
    def of(cls, blocks: dict) -> "Alignment":
        al, beside = cls(), defaultdict(set)
        for rows in blocks.values():
            for r in rows:
                if "scenario_ordinal" in r:
                    o = _ord(r["scenario_ordinal"])
                    al.ordinals.add(o)
                    for k in SCENARIO_LABEL_KEYS:
                        if k in r:
                            beside[raw(r[k])].add(o)
        for label, ords in beside.items():
            if len(ords) == 1:
                al.labels[label] = next(iter(ords))
            else:
                al.ambiguous[label] = sorted(ords)
        return al

    def scenario(self, row: dict) -> tuple[str, str]:
        """(aligned scenario, how). An unmapped scenario is tagged `unaligned`, never guessed."""
        if "scenario_ordinal" in row:
            return _ord(row["scenario_ordinal"]), "ordinal"
        label = next((raw(row[k]) for k in SCENARIO_LABEL_KEYS if k in row), None)
        if label is not None:
            if label in self.labels:
                return self.labels[label], "label"
            return f"unaligned: {label}", "unaligned"
        if len(self.ordinals) == 1:
            return next(iter(self.ordinals)), "sole scenario"
        return "unaligned: (no scenario key)", "unaligned"


def row_key(table: str, s: str, r: dict) -> tuple:
    """How rows of a non-curve table are matched across the two reports."""
    if table == "stg_pump_config":
        return (s, _num(r.get("section_order")), _flag(r.get("sensitivity_case")))
    if table == "stg_design_context":
        return (s, _flag(r.get("sensitivity_case")))
    if table == "stg_gas_cascade":
        return (s, _num(r.get("stage_order")), _flag(r.get("sensitivity_case")))
    if table == "stg_alias_evidence":
        return (raw(r.get("model_as_printed")), raw(r.get("evidence_kind")))
    return (canon(r),)


def curve_id(s: str, r: dict) -> tuple:
    # A composite row is a whole-string point and is never attributed to one model
    # (f2-championx.md §6 T6), so its model is not part of its identity.
    model = None if _flag(r.get("observation_is_composite")) else raw(r.get("pump_model_as_printed"))
    return (s, raw(r.get("curve_observation_role")), model,
            _num(r.get("frequency_hz_as_printed")), _flag(r.get("sensitivity_case")))


def match_key(s: str, r: dict) -> tuple:
    return curve_id(s, r) + (raw(r.get("point_type")), _num(r.get("flow_as_printed")),
                             raw(r.get("head_basis_as_printed")), raw(r.get("power_basis_as_printed")))


def point_id(s: str, r: dict) -> tuple:
    """One printed point, with or without a pump model: where per-section and composite rows meet."""
    return (s, raw(r.get("curve_observation_role")), raw(r.get("point_type")),
            _num(r.get("frequency_hz_as_printed")), _num(r.get("flow_as_printed")), _flag(r.get("sensitivity_case")))


def _composite(r: dict) -> bool:
    return _flag(r.get("observation_is_composite")) or "pump_model_as_printed" not in r


@dataclass(frozen=True)
class Diff:
    table: str
    key: tuple        # the matched rows' key; for a trap field, the trap citation
    field: str        # "(row)" for a whole row present on one side only
    old: str | None
    new: str | None
    category: str
    old_page: str | None = None   # source_page of the old row(s) carrying the value
    new_page: str | None = None


@dataclass
class Comparison:
    family: str
    parse_errors: dict = field(default_factory=dict)   # side -> [errors]
    alignment: dict = field(default_factory=dict)      # side -> Alignment
    diffs: list = field(default_factory=list)          # [Diff], every table
    trap_diffs: list = field(default_factory=list)     # [Diff], trap fields across tables
    structural: list = field(default_factory=list)     # (kind, key, old rows, new rows)
    curve_old: int = 0
    curve_new: int = 0
    curve_delta_pct: float | None = 0.0
    missing_curves: list = field(default_factory=list)
    missing_rows: dict = field(default_factory=dict)   # missing curve id -> its old rows
    extra_curves: list = field(default_factory=list)
    unmatched_old: list = field(default_factory=list)  # curve rows on one side, not structural
    unmatched_new: list = field(default_factory=list)

    def counts(self, table: str | None = None) -> Counter:
        return Counter(d.category for d in self.diffs if table is None or d.table == table)

    def trap_counts(self) -> Counter:
        return Counter(d.category for d in self.trap_diffs)

    @property
    def pump_ok(self) -> bool:
        return not any(self.counts("stg_pump_config")[k] for k in FAILING)

    @property
    def traps_ok(self) -> bool:
        return not any(self.trap_counts()[k] for k in FAILING)

    @property
    def curve_conflicts(self) -> int:
        return self.counts("stg_curve_observations")["conflict"]

    @property
    def curve_count_ok(self) -> bool:
        return self.curve_delta_pct is not None and abs(self.curve_delta_pct) <= CURVE_COUNT_TOLERANCE_PCT

    @property
    def curves_ok(self) -> bool:
        return self.curve_count_ok and not self.missing_curves and self.curve_conflicts == 0

    @property
    def passed(self) -> bool:
        return self.pump_ok and self.traps_ok and self.curves_ok and not any(self.parse_errors.values())


def _buckets(pairs: list, key) -> dict:
    b = defaultdict(list)
    for s, r in pairs:
        b[key(s, r)].append(r)
    return {k: sorted(v, key=canon) for k, v in b.items()}


def _page(r: dict | None) -> str | None:
    return None if r is None else raw(r.get("source_page"))


def _field_diffs(table: str, key: tuple, a: dict, b: dict) -> list[Diff]:
    return [Diff(table, key, f, raw(a.get(f)), raw(b.get(f)), classify(a.get(f), b.get(f)), _page(a), _page(b))
            for f in sorted(a.keys() | b.keys()) if raw(a.get(f)) != raw(b.get(f))]


def _pair(old_only: list, new_only: list) -> list[tuple]:
    """(old, new, category): format matches first, then the rest pair up in order; leftovers are dropped/added."""
    new_left, rest, out = list(new_only), [], []
    for a in old_only:
        twin = next((b for b in new_left if classify(a, b) == "format"), None)
        if twin is None:
            rest.append(a)
        else:
            new_left.remove(twin)
            out.append((a, twin, "format"))
    out += [(a, b, classify(a, b)) for a, b in zip(rest, new_left)]
    out += [(a, None, "dropped") for a in rest[len(new_left):]]
    return out + [(None, b, "added") for b in new_left[len(rest):]]


def _compare_table(c: Comparison, table: str, o: list, n: list) -> None:
    ob, nb = _buckets(o, lambda s, r: row_key(table, s, r)), _buckets(n, lambda s, r: row_key(table, s, r))
    for k in sorted(ob.keys() | nb.keys(), key=str):
        a_rows, b_rows = ob.get(k, []), nb.get(k, [])
        for a, b in zip(a_rows, b_rows):
            c.diffs += _field_diffs(table, k, a, b)
        c.diffs += [Diff(table, k, "(row)", canon(r), None, "dropped", _page(r)) for r in a_rows[len(b_rows):]]
        c.diffs += [Diff(table, k, "(row)", None, canon(r), "added", None, _page(r)) for r in b_rows[len(a_rows):]]


def _compatible(sa: str, a: dict, sb: str, b: dict) -> bool:
    """Same scenario and kind, and every key field printed on both sides agrees."""
    if sa != sb or _composite(a) != _composite(b):
        return False
    return all(x is None or y is None or x == y for x, y in zip(match_key(sa, a), match_key(sb, b)))


def _compare_curves(c: Comparison, o: list, n: list) -> None:
    table = "stg_curve_observations"
    c.curve_old, c.curve_new = len(o), len(n)
    c.curve_delta_pct = (0.0 if not o and not n else None if not o
                         else round((len(n) - len(o)) / len(o) * 100, 2))
    matched_old, matched_new = set(), set()  # curve ids with a row paired across the reports

    def pair(sa, a, sb, b, key):
        c.diffs.extend(_field_diffs(table, key, a, b))
        matched_old.add(curve_id(sa, a))
        matched_new.add(curve_id(sb, b))

    # 1. Exact match key. A key with a different row count on each side is a split or merge.
    ob, nb = _buckets(o, match_key), _buckets(n, match_key)
    left_o, left_n = defaultdict(list), defaultdict(list)
    for k in sorted(ob.keys() | nb.keys(), key=str):
        a_rows, b_rows = ob.get(k, []), nb.get(k, [])
        # ponytail: rows sharing a key pair in canonical order; a finer key would pair more reliably.
        for a, b in zip(a_rows, b_rows):
            pair(k[0], a, k[0], b, k)
        if a_rows and b_rows and len(a_rows) != len(b_rows):
            c.structural.append(("split" if len(b_rows) > len(a_rows) else "merged", k, a_rows, b_rows))
            continue
        for r in a_rows[len(b_rows):]:
            left_o[point_id(k[0], r)].append(r)
        for r in b_rows[len(a_rows):]:
            left_n[point_id(k[0], r)].append(r)

    # 2. Unmatched rows printed for the same point: per-section against composite, or one
    # row against several.
    rest_o, rest_n = [], []
    for p in sorted(left_o.keys() | left_n.keys(), key=str):
        a_rows, b_rows = left_o.get(p, []), left_n.get(p, [])
        if a_rows and b_rows and {_composite(r) for r in a_rows} != {_composite(r) for r in b_rows}:
            c.structural.append(("per-section vs composite", p, a_rows, b_rows))
        elif a_rows and b_rows and len(a_rows) != len(b_rows):
            c.structural.append(("split" if len(b_rows) > len(a_rows) else "merged", p, a_rows, b_rows))
        else:
            rest_o += [(p[0], r) for r in a_rows]
            rest_n += [(p[0], r) for r in b_rows]

    # 3. Rows that agree on every key field both print: a key field one side lacks is an
    # added or dropped field, not a different point.
    for sa, a in rest_o:
        twin = next(((sb, b) for sb, b in rest_n if _compatible(sa, a, sb, b)), None)
        if twin is None:
            c.unmatched_old.append(a)
        else:
            rest_n.remove(twin)
            pair(sa, a, twin[0], twin[1], match_key(sa, a))
    c.unmatched_new += [r for _s, r in rest_n]

    old_ids, new_ids = {curve_id(s, r) for s, r in o}, {curve_id(s, r) for s, r in n}
    c.missing_curves = sorted(old_ids - new_ids - matched_old, key=str)
    c.missing_rows = {k: [r for s, r in o if curve_id(s, r) == k] for k in c.missing_curves}
    c.extra_curves = sorted(new_ids - old_ids - matched_new, key=str)


def _compare_traps(c: Comparison, old: dict, new: dict) -> None:
    for key, cite in TRAP_FIELDS.get(c.family, []):
        for table in sorted(old.keys() | new.keys()):
            ov = Counter(raw(r[key]) for r in old.get(table, []) if key in r)
            nv = Counter(raw(r[key]) for r in new.get(table, []) if key in r)
            if ov != nv:
                c.trap_diffs += [Diff(table, (cite,), key, a, b, cat,
                                      _pages(old.get(table, []), key, a), _pages(new.get(table, []), key, b))
                                 for a, b, cat in _pair(sorted((ov - nv).elements()), sorted((nv - ov).elements()))]


def _pages(rows: list, key: str, value) -> str | None:
    """The source pages of every row carrying this value of a trap field."""
    if value is None:
        return None
    pages = sorted({_page(r) for r in rows if key in r and raw(r[key]) == value and _page(r) is not None})
    return "; ".join(pages) or None


def compare(family: str, old_text: str, new_text: str) -> Comparison:
    (old, old_err), (new, new_err) = stg_blocks(old_text), stg_blocks(new_text)
    c = Comparison(family, parse_errors={"old": old_err, "new": new_err},
                   alignment={"old": Alignment.of(old), "new": Alignment.of(new)})

    def aligned(side: str, table: str, rows: list) -> list:
        al, out = c.alignment[side], []
        for r in rows:
            s, how = al.scenario(r)
            if table in ("stg_pump_config", "stg_curve_observations"):
                al.methods[how] += 1
                if how == "unaligned":
                    al.unaligned[s.removeprefix("unaligned: ")] += 1
            out.append((s, r))
        return out

    for table in sorted(old.keys() | new.keys()):
        o, n = aligned("old", table, old.get(table, [])), aligned("new", table, new.get(table, []))
        if table == "stg_curve_observations":
            _compare_curves(c, o, n)
        else:
            _compare_table(c, table, o, n)
    _compare_traps(c, old, new)
    return c


def run_metrics(result, report_bytes: int) -> dict:
    """The per-call metrics recorded in run.json, from a bedrock.ExtractResult."""
    out, ttft = result.output_tokens, result.time_to_first_token_s
    gen_s = result.duration_s - ttft if ttft is not None else None
    return {
        "input_tokens": result.input_tokens,
        "output_tokens": out,
        "reasoning_tokens": result.reasoning_tokens,
        "reasoning_tokens_note": "ConverseStream usage reports no separate reasoning count; "
                                 "output_tokens includes reasoning" if result.reasoning_tokens is None else "",
        "cache_read_input_tokens": result.cache_read_input_tokens,
        "cache_write_input_tokens": result.cache_write_input_tokens,
        "time_to_first_token_s": ttft,
        "time_to_first_text_s": result.time_to_first_text_s,
        "model_time_s": result.duration_s,
        "server_latency_ms": result.server_latency_ms,
        # Output tokens over the generation window: model time less time to first token.
        "output_tokens_per_s": round(out / gen_s, 2) if out and gen_s and gen_s > 0 else None,
        "report_bytes": report_bytes,
        "bytes_per_output_token": round(report_bytes / out, 3) if out else None,
        "stop_reason": result.stop_reason,
        "max_tokens": result.max_tokens,
        "max_tokens_share": round(out / result.max_tokens, 4) if out is not None else None,
        "reasoning_chars": len(result.reasoning_text),
    }


def model_prices(pricing: dict, model_id: str | None) -> dict:
    """One model's entry in pricing.json; empty, so cost is pending, where the file has none."""
    return pricing.get("models", {}).get(model_id or "", {})


def cost_usd(input_tokens, output_tokens, pricing: dict) -> float | None:
    """None while either price, or either token count, is unknown."""
    pin, pout = pricing.get("input_usd_per_1k_tokens"), pricing.get("output_usd_per_1k_tokens")
    if None in (pin, pout, input_tokens, output_tokens):
        return None
    return input_tokens / 1000 * pin + output_tokens / 1000 * pout


# --------------------------------------------------------------------------
# Adjudication worksheet: one row per failing-category item, verdict left blank.
# --------------------------------------------------------------------------

WORKSHEET_COLUMNS = ["doc", "family", "table", "row_key", "field", "category", "old_value", "new_value",
                     "old_source_page", "new_source_page", "is_trap", "verdict", "note"]
_GROUPS = ("stg_pump_config", "traps", "stg_design_context", "stg_curve_observations")


def _sheet_row(doc: str, c: Comparison, group: str, d: Diff, is_trap: bool) -> tuple:
    row = {"doc": doc, "family": c.family, "table": d.table,
           "row_key": d.key[0] if group == "traps" else json.dumps(list(d.key), ensure_ascii=False),
           "field": d.field, "category": d.category, "old_value": d.old or "", "new_value": d.new or "",
           "old_source_page": d.old_page or "", "new_source_page": d.new_page or "",
           "is_trap": "true" if is_trap else "false", "verdict": "", "note": ""}
    return (doc, _GROUPS.index(group), row["row_key"], row["field"], row["old_value"], row["new_value"]), row


def _items(doc: str, c: Comparison, categories: tuple) -> list[tuple]:
    traps = {k for k, _ in TRAP_FIELDS.get(c.family, [])}
    out = [_sheet_row(doc, c, d.table, d, d.field in traps)
           for d in c.diffs if d.table in _GROUPS and d.category in categories]
    return out + [_sheet_row(doc, c, "traps", d, True) for d in c.trap_diffs if d.category in categories]


def _rows_pages(rows: list) -> str | None:
    return "; ".join(sorted({p for p in map(_page, rows) if p})) or None


def worksheet(doc: str, c: Comparison) -> list[dict]:
    """Every conflict and dropped item in pump_config, design context and curves, every
    conflict and dropped trap field in any table, every structural item, and every missing
    curve, sorted by doc, then pump_config, traps, design_context, curves, then row key."""
    items = _items(doc, c, FAILING)
    for kind, key, old_rows, new_rows in c.structural:
        d = Diff("stg_curve_observations", key, kind,
                 json.dumps(old_rows, ensure_ascii=False, sort_keys=True),
                 json.dumps(new_rows, ensure_ascii=False, sort_keys=True), "structural",
                 _rows_pages(old_rows), _rows_pages(new_rows))
        items.append(_sheet_row(doc, c, "stg_curve_observations", d, False))
    for cid in c.missing_curves:
        ident = json.dumps(list(cid), ensure_ascii=False)
        rows = c.missing_rows[cid]
        d = Diff("stg_curve_observations", (cid,), ident, json.dumps(rows, ensure_ascii=False, sort_keys=True),
                 None, "missing", _rows_pages(rows), None)
        k, row = _sheet_row(doc, c, "stg_curve_observations", d, False)
        row["row_key"] = ident
        if str(cid[0]).startswith("unaligned: "):
            row["note"] = f"unaligned scenario label: {cid[0].removeprefix('unaligned: ')}"
        items.append(((k[0], k[1], ident) + k[3:], row))
    return [row for _k, row in sorted(items, key=lambda t: t[0])]


def added_sample(doc: str, c: Comparison, per_field: int = 5) -> list[dict]:
    """The first `per_field` added items for each field, in worksheet order, from the same tables."""
    out, seen = [], Counter()
    for k, row in sorted(_items(doc, c, ("added",)), key=lambda t: t[0]):
        group = (k[1], row["table"], row["field"])
        if seen[group] < per_field:
            seen[group] += 1
            out.append(row)
    return out
