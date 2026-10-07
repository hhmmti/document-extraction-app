"""Prep: the single-document form of the corpus slicing step.

Keeps the family's pages, extracts layout text per kept page, translates the
private-use glyphs, and builds the page manifest. Renders nothing and writes
nothing: the kept page numbers are the slice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .detect import Detection, n_chars, read_pdf
from .errors import ExtractionError
from .families import IMAGE_ROUTE_CHARS, LOW_YIELD_CHARS, MANIFEST_COLUMNS, PUA_MAP

_PUA_TABLE = str.maketrans(PUA_MAP)
_PUA_LEFT = re.compile(r"[-]")


@dataclass(frozen=True)
class PrepResult:
    kept_pages: list[int]       # original page numbers, in source order
    page_text: dict[int, str]   # original page number -> PUA-fixed layout text
    manifest: list[dict]        # one row per source page, keyed by MANIFEST_COLUMNS


def fix_pua(text: str) -> str:
    """Translate known PUA codepoints. Anything still in the PUA range after
    this is unmapped and must be reported, never silently passed through."""
    return text.translate(_PUA_TABLE)


def decide(detection: Detection) -> list[dict]:
    """Primary role and inclusion per page, from the detected page roles."""
    decisions = []
    carried = ""  # last resolved role, for untitled continuation pages
    for i in range(1, detection.page_count + 1):
        hits = detection.page_roles[i]
        note_bits = []
        if hits:
            role, status, title = hits[0]
            if len(hits) > 1:
                note_bits.append("also carries: " + "; ".join(r for r, _, _ in hits[1:]))
            # Excluded only when every role on the page is plot-only.
            included = not all(s == "plot_only" for _r, s, _m in hits)
            if not included:
                note_bits.append("contract marks this page plot-only: read no numbers from it")
            if status == "unmapped":
                note_bits.append(
                    "printed by this family but named by no page-map entry; "
                    "included by default rather than dropped"
                )
            carried = role
        else:
            role, status, title, included = "", "", "", True
            if carried:
                note_bits.append(
                    f"no page title matched; continuation of '{carried}'; included by default"
                )
            else:
                note_bits.append("no page title matched; role unresolved; included by default")
        decisions.append({
            "page": i, "title": title, "role": role,
            "status": "" if status == "unmapped" else status,
            "included": included, "note": " · ".join(note_bits),
        })
    return decisions


def prep(pdf_bytes: bytes, detection: Detection, *, source_file: str = "") -> PrepResult:
    with read_pdf(pdf_bytes) as pdf:
        if len(pdf.pages) != detection.page_count:
            raise ExtractionError(
                "PREP_FAILED",
                f"PDF has {len(pdf.pages)} pages; its detection has {detection.page_count}",
                {"pdf_pages": len(pdf.pages), "detected_pages": detection.page_count},
            )
        layouts = [p.extract_text(layout=True) or "" for p in pdf.pages]

    decisions = decide(detection)
    kept = [d["page"] for d in decisions if d["included"]]
    sliced_no = {p: k for k, p in enumerate(kept, 1)}

    page_text, rows, thin, unmapped = {}, [], {}, {}
    for d, layout in zip(decisions, layouts):
        i, chars = d["page"], n_chars(layout)
        notes = [d["note"]] if d["note"] else []
        low = ""
        if d["included"]:
            # No vision path: a kept page without a text layer cannot be extracted.
            if chars < IMAGE_ROUTE_CHARS:
                thin[i] = chars
            txt = fix_pua(layout)
            leftover = set(_PUA_LEFT.findall(txt))
            if leftover:
                unmapped[i] = sorted(f"U+{ord(c):04X}" for c in leftover)
            page_text[i] = txt
            if chars < LOW_YIELD_CHARS:
                low = "yes"
                notes.append(
                    f"low yield: {chars} non-whitespace characters, under the "
                    f"{LOW_YIELD_CHARS}-character threshold; text layer is thin but present"
                )
        rows.append(dict(zip(MANIFEST_COLUMNS, [
            source_file, detection.family, "detected", detection.page_count,
            i, sliced_no.get(i, ""), d["title"], d["role"],
            d["status"], "yes" if d["included"] else "no",
            "yes" if d["included"] else "no", chars, "text" if d["included"] else "",
            low, " · ".join(notes),
        ])))

    if thin or unmapped:
        problems = []
        if thin:
            problems.append(f"kept page(s) under the {IMAGE_ROUTE_CHARS}-character floor: "
                            + ", ".join(f"p{p}" for p in thin))
        if unmapped:
            problems.append("unmapped private-use glyphs on: "
                            + ", ".join(f"p{p}" for p in unmapped))
        raise ExtractionError(
            "PREP_FAILED", "; ".join(problems),
            {"thin_pages": thin, "unmapped_pua": unmapped},
        )
    return PrepResult(kept, page_text, rows)
