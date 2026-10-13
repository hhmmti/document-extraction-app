"""D31 -- `pump_model_as_printed` -> `pump_model_canonical`.

Built from three inputs, in this precedence:

  1. `m2-d31-seed-aliases.csv`      -- 28 seeded rows, hand-written, incl. the
                                      `__COMPOSITE__` sentinel
  2. each family's contract §4 grammar (below)
  3. `stg_alias_evidence` across the corpus, which supplies the printed forms
     the grammars are applied to and the `composite` / `not_a_model` flags

**This runs before the D17 collapse.**  F5 prints one pump two ways in one
document -- `ESP B 400 1750 PK 738 STG` and
`ESP B 4001750 CW 6.5M HSG 123 STG ...`, where 6 x 123 = 738.  Collapsing on the
raw strings makes that one pump look like two models and fires a false
`D17-HALT`.

A composite string (`SF3550SF4300HFGS400hp 420MTR`) names a whole string plus a
motor.  Splitting it is a judgement, not a repair, so it resolves to nothing at
all -- never to its first stem.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

COMPOSITE_SENTINEL = "__COMPOSITE__"
NOT_A_MODEL_SENTINEL = "__NOT_A_MODEL__"


@dataclass
class Resolution:
    canonical: str | None
    basis: str
    flag: str          # clean | repaired | seed | composite | not_a_model | unresolved


# --------------------------------------------------------------------------
# Pre-grammar tidying.  Strips containers the contracts say are not identity:
# a manufacturer prefix that has its own column, and a stage count that has its
# own column.  Neither is a value change.
# --------------------------------------------------------------------------

_PREFIXES = ("summit / ", "summit/", "pump: ")
_STAGE_SUFFIX = re.compile(r"[\s,]*[-,]?\s*\d+\s*stages?\s*$", re.I)
_STG_SUFFIX = re.compile(r",\s*\d+\s*stages?\b", re.I)


def _strip_containers(text: str) -> str:
    out = text.strip()
    lowered = out.lower()
    for prefix in _PREFIXES:
        if lowered.startswith(prefix):
            out = out[len(prefix):].strip()
            break
    out = _STAGE_SUFFIX.sub("", out).strip()
    out = _STG_SUFFIX.sub("", out).strip()
    return re.sub(r"\s{2,}", " ", out).strip()


# --------------------------------------------------------------------------
# Rejection: composites and non-models (CF-26)
# --------------------------------------------------------------------------

# Two model stems run together, or a stem followed by motor/package text.
_COMPOSITE = re.compile(
    r"(SF|SD|SFGH)\d{2,4}[A-Za-z-]*\s*(SF|SD|SFGH)\d{2,4}"
    r"|\d+\s*hp\b|\d+\s*h\s+\d+MTR|\d+\s*MTR|SLUGGER|HF\s*GS|HFGS"
    r"|@\s*[\d.]+\s*Hz",
    re.I,
)
_NOT_A_MODEL = re.compile(
    r"^(PERMIAN|Permian)\b"
    r"|BOLT-ON|DISCHARGE HEAD|HEAD_DISCHARGE"
    r"|VGSA"
    r"|^C-CT$|^CR-CT$",
    re.I,
)


def _rejected(text: str) -> str | None:
    if _NOT_A_MODEL.search(text):
        return "not_a_model"
    if _COMPOSITE.search(text):
        return "composite"
    # Two `PUMP MSC_` tokens joined by a comma name two bodies, even where both
    # happen to be the same model.  Splitting the string is still a judgement.
    if len(re.findall(r"MSC_", text, re.I)) > 1:
        return "composite"
    # A bare number is not a model token.
    if re.fullmatch(r"[\d,]+", text.strip()):
        return "not_a_model"
    return None


# --------------------------------------------------------------------------
# Per-family §4 grammars.  Never one regex for the corpus
# (plan §4, M2 Do-NOT; validator spec §9 implementation note).
# --------------------------------------------------------------------------

# f1-spyglass.md §4, widened to the forms the corpus actually prints: `AR` as
# well as `XR`, `STD Shaft` as well as `HS Shaft`, and the text-layer damage
# (`HS Shaft))`) that normalize-models.py already documents as upstream of the
# extractor.  `SFNPSH` is a recorded grammar gap, transcribed not repaired.
_F1 = re.compile(
    r"^(?P<stem>(?:SD|SF|SFGH|SFNPSH)\d{2,4}[A-Z]?)"
    r"(?:\s+TS\d)?"
    r"(?:\s+[AX]R)?"
    r"(?:\s*\(?(?:HS|STD)\s+Shaft\)*)?$",
    re.I,
)

# f2-championx.md §4
_F2_MAIN = re.compile(r"^(?P<model>\d{3}[A-Z]{2,4}\d{2,4}[A-Z]?)$", re.I)
_F2_MSC = re.compile(
    r"^PUMP\s+MSC_(?P<series>\d{3})(?P<family>[A-Z]+)_(?P<model>\d{2,4}[A-Z]?)_", re.I
)
_F2_CHART = re.compile(r"Performance Curve:\s*(?P<model>\d{3}[A-Z]{2,4}\d{2,4}[A-Z]?)\b", re.I)

# f3-slb.md §4.  `Staging Configuration` is deliberately not part of identity.
_F3 = re.compile(
    r"^(?:REDA\s+)?(?P<series>\d{3})\s+(?P<model>[A-Z]{1,3}\d{3,4})$", re.I
)

# f4-els.md §4.  Manufacture / Series / Model are three separate printed
# fields; the canonical does not carry the series.
_F4 = re.compile(r"^(?:ELS\s+\d{3}\s+)?ELS-(?P<model>\d{3,4})$", re.I)

# f5-baker.md §4 -- the same pump printed two ways in one document.
_F5_DIAGRAM = re.compile(
    r"^ESP\s+B\s+(?P<body>.+?)\s+CW\s+[\d.]+M\s+HSG\s+\d+\s+STG\s+(?P<code>[A-Z]{2})\b",
    re.I,
)
_F5_SUMMARY = re.compile(
    r"^ESP\s+B\s+(?P<body>.+?)\s+(?P<code>[A-Z]{2})\s+\d+(?:\s+STG)?$", re.I
)

# f7-valiant.md §4.  AR / Modular / EHP are build attributes, dropped here.
_F7 = re.compile(r"^(?P<model>VC\d{4})(?:\s*-?\s*[A-Z]{2})?(?:\s+Modular)?(?:\s+EHP)?$", re.I)


def _grammar(text: str, family: str):
    """Apply one family's §4 grammar.  Returns (canonical, basis) or None."""
    if family == "spyglass":
        m = _F1.match(text)
        if m:
            return m.group("stem").upper(), "f1 §4 grammar: stem"
    elif family == "championx":
        m = _F2_MAIN.match(text)
        if m:
            return m.group("model").upper(), "f2 §4 grammar: digits-first token"
        m = _F2_MSC.match(text)
        if m:
            return (
                f"{m.group('series')}{m.group('family')}{m.group('model')}".upper(),
                "f2 §4 grammar: MSC_ form reassembled (D31, not extraction)",
            )
        m = _F2_CHART.search(text)
        if m:
            return m.group("model").upper(), "f2 §4 grammar: chart title"
    elif family == "slb":
        m = _F3.match(text)
        if m:
            return (
                f"{m.group('series')}{m.group('model')}".upper(),
                "f3 §4 grammar: series + model",
            )
    elif family == "els":
        m = _F4.match(text)
        if m:
            return f"ELS{m.group('model')}".upper(), "f4 §4 grammar: model only, no series"
    elif family == "baker_prolift":
        for pattern, label in ((_F5_DIAGRAM, "diagram"), (_F5_SUMMARY, "summary")):
            m = pattern.match(text)
            if m:
                body = re.sub(r"[^A-Za-z0-9]", "", m.group("body")).upper()
                return (
                    f"{body}{m.group('code').upper()}",
                    f"f5 §4 grammar: {label} form -> series+model+code",
                )
    elif family == "valiant":
        m = _F7.match(text)
        if m:
            return m.group("model").upper(), "f7 §4 grammar: model, build attributes dropped"
    return None


# --------------------------------------------------------------------------
# The lookup
# --------------------------------------------------------------------------


class AliasLookup:
    def __init__(self, seed_csv: Path):
        self.seed: dict = {}
        with seed_csv.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = (row["vendor_family"].strip(), row["pump_model_as_printed"].strip().lower())
                self.seed[key] = (row["proposed_canonical"].strip(), row["basis"].strip())
        self.evidence_flags: dict = {}   # printed string (lower) -> flag
        self._cache: dict = {}
        self.unresolved: dict = {}       # printed string -> family

    def learn_evidence(self, printed: str, family: str, flag: str | None, kind: str | None):
        """Record what stg_alias_evidence says about a printed string."""
        flag = (flag or "").strip().lower()
        kind = (kind or "").strip().lower()
        # The explicit normalisation flag outranks evidence_kind: F1 files a
        # well name (an operator prefix plus a well name) under
        # evidence_kind = composite_string_token but flags it not_a_model, and
        # not_a_model is the more precise fact.
        if flag in ("composite", "not_a_model"):
            self.evidence_flags[printed.strip().lower()] = flag
        elif kind == "not_a_model":
            self.evidence_flags[printed.strip().lower()] = "not_a_model"
        elif kind == "composite_string_token":
            self.evidence_flags[printed.strip().lower()] = "composite"

    def resolve(self, printed, family: str) -> Resolution:
        if printed is None or not str(printed).strip():
            return Resolution(None, "no printed string", "unresolved")
        raw = str(printed).strip()
        cache_key = (family, raw)
        if cache_key in self._cache:
            return self._cache[cache_key]
        result = self._resolve(raw, family)
        if result.flag == "unresolved":
            self.unresolved[raw] = family
        self._cache[cache_key] = result
        return result

    def _resolve(self, raw: str, family: str) -> Resolution:
        # 1. the extractor's own flag, and the corpus-wide evidence flags
        marked = self.evidence_flags.get(raw.lower())
        if marked == "composite":
            return Resolution(None, "stg_alias_evidence: composite_string_token", "composite")
        if marked == "not_a_model":
            return Resolution(None, "stg_alias_evidence: not_a_model", "not_a_model")

        # 2. the seed table, on the string exactly as printed
        seeded = self.seed.get((family, raw.lower()))
        if seeded:
            canonical, basis = seeded
            if canonical == COMPOSITE_SENTINEL:
                return Resolution(None, f"seed: {basis}", "composite")
            return Resolution(canonical, f"m2-d31-seed-aliases.csv: {basis}", "seed")

        # 3. structural rejection, before any grammar gets a chance to find a
        #    stem inside a composite
        rejected = _rejected(raw)
        if rejected:
            return Resolution(None, f"{rejected} token, not resolvable to a model", rejected)

        # 4. the family grammar, on the string and on its tidied form
        hit = _grammar(raw, family)
        if hit:
            return Resolution(hit[0], hit[1], "clean")

        tidied = _strip_containers(raw)
        if tidied != raw:
            seeded = self.seed.get((family, tidied.lower()))
            if seeded and seeded[0] != COMPOSITE_SENTINEL:
                return Resolution(seeded[0], f"m2-d31-seed-aliases.csv (tidied): {seeded[1]}", "repaired")
            hit = _grammar(tidied, family)
            if hit:
                return Resolution(hit[0], f"{hit[1]} (after container strip)", "repaired")

        return Resolution(None, "no §4 grammar match; grammar gap", "unresolved")
