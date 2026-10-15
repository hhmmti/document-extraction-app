"""Detect-only survey of a folder of design PDFs.  Detect and its page readers only: no model, no AWS.

    export ESP_SURVEY_DIR=<folder> ESP_SURVEY_OUT=<output folder> ESP_WELL_LIST_PATH=<well list CSV>
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tools/detect_survey.py [--corpus]

Every PDF under the folder, at any depth, gets a row in <output folder>/survey.csv.  summary.md beside
it has the totals, one row per unique document named `<organisation>/#<n>`, the layout fingerprints two
or more unique documents share, and the exceptions.  With --corpus it has only the totals and whether
they match the corpus results, and the exit status is 1 when they don't.  survey.csv holds paths, which
carry well names, so the output folder must be outside this folder; nothing else is written.

- Image-only: page 1 under Detect's text floor, as Detect decides it.
- Near miss: how many of a family's page-1 markers are found; for a family with any, which of its
  required page roles are found, by Detect's own signature_hits and required_found.  The best has the
  largest share found.
- Headings: page-1 rows set larger than the body text, as Detect's title lines are, and in one font: a
  row that mixes fonts is a label and its value, and the value can name a person, a customer or a well.
  Also left out: a row with a digit (more often a value than a heading; an API number identifies a well
  no well list names) and a row that names a client in the well list, by tools/check_client_names.py's
  matching and by its ids again with case, spaces and punctuation ignored (a PDF can print a well id
  without its '#').
- Layout fingerprint, for a document with no family: SHA-256 (first 12 hex) of the producer and the
  first three headings, case-folded.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True  # nothing lands under this folder, even without PYTHONDONTWRITEBYTECODE

LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from app.wells import WELL_LIST_ENV, SnapshotError, load  # noqa: E402
from extraction import ExtractionError, detect  # noqa: E402
from extraction.detect import n_chars, page_lines, read_pdf, required_found, signature_hits  # noqa: E402
from extraction.families import IMAGE_ROUTE_CHARS, PAGE1_SIGNATURE, PAGE_MAP  # noqa: E402
from tools.check_client_names import client_ids, names_a_client  # noqa: E402

DIR_ENV, OUT_ENV = "ESP_SURVEY_DIR", "ESP_SURVEY_OUT"
FAMILIES = list(PAGE1_SIGNATURE)
HEADINGS = 3
# The corpus results (design-note.md, Detect, "Confirmed in Step 2"): 46 documents in the six
# families, the XSize document IMAGE_ONLY and the procedure document UNKNOWN_TEMPLATE.
CORPUS = {"in one of the six families": 46, "IMAGE_ONLY": 1, "UNKNOWN_TEMPLATE": 1, "anything else": 0}
COLUMNS = ["path", "sha256", "organisation", "bytes", "pages", "producer", "creator",
           "chars_page1", "chars_min", "chars_total", "image_only", "detection", "required_pages",
           *(f"{fam}_{what}" for fam in FAMILIES for what in ("markers", "required_found")),
           "fingerprint", "error"]


def one_line(value) -> str:
    return " ".join(str(value or "").split())


def squeezed(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def print_ids(pairs):
    """check_client_names's ids, plus each squeezed to letters and digits (when that leaves more than 4)."""
    wells, orgs = client_ids(pairs)
    return wells, orgs, {s for s in map(squeezed, [*wells, *orgs]) if len(s) > 4}


def names_a_client_in_print(line: str, ids) -> bool:
    wells, orgs, loose = ids
    line_squeezed = squeezed(line)
    return names_a_client(line, wells, orgs) or any(s in line_squeezed for s in loose)


def heading_rows(page) -> list[str]:
    """title_lines' rows set larger than the body, kept only when set in one font.  title_lines returns
    text alone, so its grouping is repeated here to see the fonts."""
    rows = defaultdict(list)
    for ch in page.chars:
        rows[round(ch["top"], 1)].append(ch)
    if not rows:
        return []
    body = Counter(round(ch["size"], 1) for ch in page.chars).most_common(1)[0][0]
    return ["".join(c["text"] for c in chars) for _top, chars in sorted(rows.items())
            if max(round(c["size"], 1) for c in chars) > body and len({c["fontname"] for c in chars}) == 1]


def headings(lines, ids) -> list[str]:
    """The first HEADINGS distinct lines with no digit and no client name."""
    distinct = dict.fromkeys(one_line(line) for line in lines)
    return [h for h in distinct if h and not re.search(r"\d", h) and not names_a_client_in_print(h, ids)][:HEADINGS]


def reading(data: bytes, ids) -> dict:
    """Metadata, text layer, near misses and page-1 headings, read with Detect's own page readers."""
    with read_pdf(data) as pdf:
        meta, pages = pdf.metadata or {}, pdf.pages
        chars = [n_chars(p.extract_text(layout=True) or "") for p in pages]
        lines = [page_lines(p) for p in pages]
        hits = signature_hits("\n".join(lines[0]) if lines else "")
        near = {fam: (n, len(PAGE1_SIGNATURE[fam]), required_found(fam, pages, lines) if n else None)
                for fam, n in hits.items()}
        heads = heading_rows(pages[0]) if pages else []
    first = chars[0] if chars else 0
    return {"pages": len(pages), "producer": one_line(meta.get("Producer")), "creator": one_line(meta.get("Creator")),
            "chars_page1": first, "chars_min": min(chars, default=0), "chars_total": sum(chars),
            "image_only": "yes" if first < IMAGE_ROUTE_CHARS else "no", "near": near,
            "headings": headings(heads, ids)}


def survey(path: Path, root: Path, ids) -> dict:
    data = path.read_bytes()
    rel = path.relative_to(root)
    row = {"path": rel.as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
           "organisation": rel.parts[0] if len(rel.parts) > 1 else "", "bytes": len(data), "errors": []}
    try:
        row["detection"], row["required_pages"] = detect(data).family, "all found"
    except ExtractionError as err:
        row["detection"] = err.code
        row["required_pages"] = (f"{err.details['family']} missing: " + "; ".join(err.details["missing_roles"])
                                 if err.code == "REQUIRED_PAGES_MISSING" else "not reached")
    except Exception as err:  # Detect broke rather than classified: the PDF is listed with the exception
        row["detection"], row["required_pages"] = "exception", ""
        row["errors"].append(("detect", err))
    try:
        row.update(reading(data, ids))
    except Exception as err:  # read_pdf wraps what it meets as PREP_FAILED; the cause is the exception
        row["errors"].append(("survey", err.__cause__ or err))
    if row["detection"] not in FAMILIES and "producer" in row:
        key = "\n".join([row["producer"], *(h.casefold() for h in row["headings"])])
        row["fingerprint"] = hashlib.sha256(key.encode()).hexdigest()[:12]
    return row


def csv_row(row: dict) -> dict:
    out = {col: row.get(col, "") for col in COLUMNS}
    for fam, (hit, total, found) in row.get("near", {}).items():
        out[f"{fam}_markers"] = f"{hit}/{total}"
        out[f"{fam}_required_found"] = "" if found is None else "; ".join(found) or "none"
    out["error"] = "; ".join(f"{where}: {type(err).__name__}: {one_line(err)}" for where, err in row["errors"])
    return out


def best_near_miss(row: dict) -> tuple[str, str]:
    """The family with the largest share of its page-1 markers found (the first, on a tie), and its
    required page roles found."""
    near = row.get("near")
    if not near:
        return "–", "–"
    fam = max(FAMILIES, key=lambda f: (near[f][0] / near[f][1], near[f][0]))
    hit, total, found = near[fam]
    if not hit:
        return "none", "–"
    required = [role for role, status, _m in PAGE_MAP[fam] if status == "required"]
    missing = [role for role in required if role not in found]
    return f"{fam}, {hit} of {total}", f"{len(found)} of {len(required)}" + (
        f"; missing: {'; '.join(missing)}" if missing else "")


def table(header: list[str], rows) -> list[str]:
    def line(cells):
        return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"
    return [line(header), "|" + "---|" * len(header), *map(line, rows), ""]


def summary(rows: list[dict], title: str, corpus: bool) -> tuple[str, bool]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["sha256"], []).append(row)
    repeated = sum(len({PurePosixPath(r["path"]).parent for r in rs}) > 1 for rs in groups.values())
    md = [f"# Detect survey: {title}", "",
          *table(["Documents", "Unique SHA-256", "Hashes in more than one well folder"],
                 [(len(rows), len(groups), repeated)])]
    failed = [r for r in rows if r["errors"]]

    if corpus:
        found = Counter("in one of the six families" if r["detection"] in FAMILIES else r["detection"]
                        if r["detection"] in CORPUS else "anything else" for r in rows)
        checks = [(what, found[what], want) for what, want in CORPUS.items()]
        checks += [("families", len({r["detection"] for r in rows} & set(FAMILIES)), len(FAMILIES)),
                   ("PDFs with an exception", len(failed), 0)]
        ok = all(got == want for _w, got, want in checks)
        md += [*table(["Result", "Found", "Expected"], checks),
               "Per family: " + ", ".join(f"{fam} {sum(r['detection'] == fam for r in rows)}" for fam in FAMILIES), "",
               f"**{'Matches' if ok else 'Does not match'} the corpus results.**"]
        return "\n".join(md) + "\n", ok

    labels, per_org = {}, Counter()
    for sha, rs in groups.items():
        org = rs[0]["organisation"]
        per_org[org] += 1
        labels[sha] = f"{org}/#{per_org[org]}" if org else f"#{per_org[org]}"

    def heading_cell(row):
        return ", ".join(f"`{h}`" for h in row.get("headings", [])) or "–"

    docs = []
    for sha, rs in groups.items():
        row, orgs = rs[0], sorted({r["organisation"] for r in rs})
        near, required = best_near_miss(row)
        docs.append((labels[sha], f"{len(rs)}" + (f" ({', '.join(orgs)})" if len(orgs) > 1 else ""),
                     row["detection"] + (f" ({row['required_pages']})"
                                         if row["detection"] == "REQUIRED_PAGES_MISSING" else ""),
                     near, required, row.get("producer") or "–", row.get("pages", "–"),
                     row.get("chars_page1", "–"), row.get("image_only", "–"), heading_cell(row)))
    md += ["## Unique documents", "",
           *table(["Document", "Wells", "Detection", "Best near miss", "Its required pages found", "Producer",
                   "Pages", "Page-1 characters", "Image-only", "Page-1 headings"], docs)]

    shared: dict[str, list[str]] = {}
    for sha, rs in groups.items():
        if rs[0].get("fingerprint"):
            shared.setdefault(rs[0]["fingerprint"], []).append(sha)
    clusters = [(fp, ", ".join(labels[s] for s in shas), groups[shas[0]][0]["producer"] or "–",
                 heading_cell(groups[shas[0]][0])) for fp, shas in shared.items() if len(shas) > 1]
    md += ["## Shared layouts", ""]
    md += (table(["Fingerprint", "Unique documents", "Producer", "Page-1 headings"], clusters) if clusters
           else ["No layout fingerprint is shared by two or more unique documents with no family.", ""])

    md += ["## Exceptions", ""]
    kinds = Counter((where, type(err).__name__) for r in failed for where, err in r["errors"])
    md += (table(["Stage", "Error type", "PDFs"], [(w, t, n) for (w, t), n in sorted(kinds.items())]) if kinds
           else ["None: every PDF was read and classified.", ""])
    return "\n".join(md), True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", action="store_true", help="check the totals against the corpus results")
    args = ap.parse_args(argv)
    unset = [name for name in (DIR_ENV, OUT_ENV, WELL_LIST_ENV) if not os.environ.get(name)]
    if unset:
        ap.error(f"set {', '.join(unset)}")
    root = Path(os.environ[DIR_ENV]).expanduser().resolve()
    out = Path(os.environ[OUT_ENV]).expanduser().resolve()
    if not root.is_dir():
        ap.error(f"{DIR_ENV} is not a folder")
    if out == LAB or LAB in out.parents:
        ap.error(f"{OUT_ENV} is inside {LAB}: survey.csv holds paths with well names, so it goes outside")
    try:
        ids = print_ids(load(Path(os.environ[WELL_LIST_ENV]).expanduser()).pairs)
    except SnapshotError as err:
        ap.error(f"the well list cannot be used: {err}")

    rows = [survey(path, root, ids)
            for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")]
    text, ok = summary(rows, "corpus self-check" if args.corpus else root.name, args.corpus)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "survey.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, COLUMNS)
        writer.writeheader()
        writer.writerows(csv_row(r) for r in rows)
    (out / "summary.md").write_text(text, encoding="utf-8")
    print(f"{len(rows)} PDFs; wrote {out / 'survey.csv'} and {out / 'summary.md'}")
    if args.corpus:
        print("corpus self-check:", "matches" if ok else "DOES NOT MATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
