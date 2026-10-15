"""Detect: which vendor family a design document is, and the role of each page.

Deterministic: no network, no model, no file list. Works on the PDF's bytes in
memory and writes nothing.
"""

from __future__ import annotations

import io
import logging
import re
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass

import pdfplumber

from .errors import ExtractionError
from .families import IMAGE_ROUTE_CHARS, PAGE1_SIGNATURE, PAGE_MAP

# Real design documents carry thousands of malformed font descriptors. They say
# nothing about extractability.
logging.getLogger("pypdf").setLevel(logging.ERROR)
logging.getLogger("pdfminer").setLevel(logging.ERROR)


GENERIC = "generic"


@dataclass(frozen=True)
class Detection:
    family: str
    page_count: int
    # Original page number -> every page-map entry the page matches, in
    # page-map order, as (role, status, matched line). The first is the
    # page's primary role; an empty list means no role resolved.
    page_roles: dict[int, list[tuple[str, str, str]]]


@dataclass(frozen=True)
class Routing:
    """Where Detect sends a document.  `detection` is set on the template route only."""
    route: str                          # template | generic
    detection: Detection | None
    family_hint: str | None = None      # the family matched on page 1 whose required pages are missing
    missing_roles: tuple = ()
    near_misses: tuple = ()             # UNKNOWN_TEMPLATE's, when no family matched
    forced: bool = False                # a template document sent down the generic route on purpose
    forced_from: str | None = None      # the family Detect matched, when forced


@contextmanager
def read_pdf(pdf_bytes: bytes):
    """pdfplumber over in-memory bytes. A PDF that cannot be parsed is PREP_FAILED."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            yield pdf
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(
            "PREP_FAILED", "the PDF could not be read",
            {"reason": f"{type(exc).__name__}: {exc}"},
        ) from exc


def n_chars(text: str) -> int:
    """Non-whitespace characters: layout text is padded to a rectangle."""
    return len("".join(text.split()))


def page_lines(page) -> list[str]:
    """Non-empty stripped lines of a page's default text extraction."""
    return [ln.strip() for ln in (page.extract_text() or "").split("\n") if ln.strip()]


def title_lines(page) -> list[str]:
    """Lines set in a glyph larger than the page's body text, plus line 1.

    F1 stacks several equipment blocks on one page and repeats block words
    ("Motor", "Pumps") inside data rows. The vendor sets block titles one point
    larger than the body, which is the only signal that separates a block
    heading from a row label without a judgement call.
    """
    rows: dict[float, list] = defaultdict(list)
    for ch in page.chars:
        rows[round(ch["top"], 1)].append(ch)
    if not rows:
        return []
    body = Counter(round(ch["size"], 1) for ch in page.chars).most_common(1)[0][0]
    out = []
    for top in sorted(rows):
        chars = rows[top]
        text = "".join(c["text"] for c in chars).strip()
        if not text:
            continue
        if max(round(c["size"], 1) for c in chars) > body:
            out.append(text)
    first = page_lines(page)
    if first:
        out.append(first[0])
    return out


def matched_line(matcher, lines: list[str], titles: list[str]) -> str | None:
    """The first line this matcher matches, verbatim, or None."""
    kind, needle = matcher
    haystack = titles if kind == "title" else lines
    for ln in haystack:
        if kind == "line" and ln == needle:
            return ln
        if kind in ("start", "title") and ln.startswith(needle):
            return ln
        if kind == "re" and re.search(needle, ln):
            return ln
    return None


def resolve_roles(family: str, lines: list[str], titles: list[str]):
    """(role, status, matched_line) for every page-map entry this page matches."""
    out = []
    for role, status, matcher in PAGE_MAP[family]:
        hit = matched_line(matcher, lines, titles)
        if hit is not None:
            out.append((role, status, hit))
    return out


def marker_found(marker, page1: str) -> bool:
    """A string marker anywhere in page 1's lines; a pattern marker within one of them."""
    return marker.search(page1) is not None if isinstance(marker, re.Pattern) else marker in page1


def signature_hits(page1: str) -> dict[str, int]:
    """How many of each family's page-1 markers page 1's lines carry."""
    return {f: sum(marker_found(m, page1) for m in sig) for f, sig in PAGE1_SIGNATURE.items()}


def required_found(family: str, pages, lines) -> list[str]:
    """The family's required page roles that some page resolves, in page-map order."""
    seen = {role for p, ln in zip(pages, lines)
            for role, _s, _m in resolve_roles(family, ln, title_lines(p) if family == "F1" else [])}
    return [r for r, status, _m in PAGE_MAP[family] if status == "required" and r in seen]


def unknown_template(hits: dict[str, int], pages, lines, metadata) -> ExtractionError:
    """UNKNOWN_TEMPLATE with its near misses, each family that has some of its page-1 markers (how
    many, and which required pages the document has), and the PDF producer. No document text."""
    near = []
    for family, n in hits.items():
        if n:
            required = [r for r, status, _m in PAGE_MAP[family] if status == "required"]
            found = required_found(family, pages, lines)
            near.append({"family": family, "markers_matched": n, "markers": len(PAGE1_SIGNATURE[family]),
                         "required_found": found, "required_missing": [r for r in required if r not in found]})
    producer = " ".join(str((metadata or {}).get("Producer") or "").split()) or "not recorded"
    misses = "; ".join(
        f"{m['family']} with {m['markers_matched']} of {m['markers']} page-1 markers and "
        f"{len(m['required_found'])} of {len(m['required_found']) + len(m['required_missing'])} required pages"
        + (f" (missing {', '.join(m['required_missing'])})" if m["required_missing"] else "")
        for m in near)
    return ExtractionError(
        "UNKNOWN_TEMPLATE",
        "no family's page-1 signature matched; "
        + (f"near misses: {misses}" if near else "no family has any of its page-1 markers")
        + f"; PDF producer: {producer}",
        {"families": [], "near_misses": near, "producer": producer},
    )


def detect(pdf_bytes: bytes) -> Detection:
    with read_pdf(pdf_bytes) as pdf:
        pages = pdf.pages
        # "Page 1 or every page under the floor": page 1 is one of every page,
        # so page 1 alone decides both.
        first = n_chars(pages[0].extract_text(layout=True) or "") if pages else 0
        if first < IMAGE_ROUTE_CHARS:
            raise ExtractionError(
                "IMAGE_ONLY",
                f"page 1 has no usable text layer ({first} non-whitespace characters, "
                f"floor {IMAGE_ROUTE_CHARS}); the document cannot be extracted",
                {"page": 1, "n_chars": first, "floor": IMAGE_ROUTE_CHARS},
            )

        lines = [page_lines(p) for p in pages]
        hits = signature_hits("\n".join(lines[0]))
        families = [f for f, n in hits.items() if n == len(PAGE1_SIGNATURE[f])]
        if not families:
            raise unknown_template(hits, pages, lines, pdf.metadata)
        if len(families) > 1:
            raise ExtractionError(
                "AMBIGUOUS_TEMPLATE",
                f"page-1 signatures of {len(families)} families matched: {', '.join(families)}",
                {"families": families},
            )
        family = families[0]

        page_roles = {
            i: resolve_roles(family, ln, title_lines(p) if family == "F1" else [])
            for i, (p, ln) in enumerate(zip(pages, lines), 1)
        }

    # Any hit on a page counts, not only the page's primary role.
    seen = {role for hits in page_roles.values() for role, _s, _m in hits}
    missing = [r for r, status, _m in PAGE_MAP[family] if status == "required" and r not in seen]
    if missing:
        raise ExtractionError(
            "REQUIRED_PAGES_MISSING",
            f"{family} document is missing required page(s): {'; '.join(missing)}",
            {"family": family, "missing_roles": missing},
        )
    return Detection(family, len(lines), page_roles)


def route(pdf_bytes: bytes, *, force_generic: bool = False) -> Routing:
    """The template route for a family matched with all its required pages; the generic route for a
    family matched with required pages missing (that family is the hint), or for a text layer no
    family matches.  IMAGE_ONLY, AMBIGUOUS_TEMPLATE and PREP_FAILED still raise.  `force_generic`
    sends a template document down the generic route without a hint, as an unknown one would go."""
    try:
        detection = detect(pdf_bytes)
    except ExtractionError as err:
        if err.code == "REQUIRED_PAGES_MISSING":
            return Routing(GENERIC, None, err.details["family"], tuple(err.details["missing_roles"]))
        if err.code == "UNKNOWN_TEMPLATE":
            return Routing(GENERIC, None, near_misses=tuple(err.details["near_misses"]))
        raise
    if force_generic:
        return Routing(GENERIC, None, forced=True, forced_from=detection.family)
    return Routing("template", detection)
