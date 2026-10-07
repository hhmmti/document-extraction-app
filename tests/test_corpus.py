"""Detect and Prep over the reference corpus, against the slicing step's own output.

Run with `pytest -m corpus`. Needs two environment variables (see README):
ESP_CORPUS_DIR (the design documents) and ESP_PREP_REF_DIR (the reference
`prep-manifest.csv` and `text/<family-slug>/<stem>/p<n>.txt`). Every expected
value is read from the manifest at run time. The prompt byte check also needs
ESP_BATCH_PROMPT_DIR, the runners' saved `<stem>.prompt.txt` files.
"""

import csv
import glob
import os
import re
from collections import defaultdict
from functools import cache
from pathlib import Path

import pytest

from extraction import ExtractionError, detect, prep
from extraction.assemble import ASSETS, VALIDATOR, assemble_prompt
from extraction.families import PUA_MAP
from extraction.prep import fix_pua

CORPUS = os.environ.get("ESP_CORPUS_DIR", "")
REF = os.environ.get("ESP_PREP_REF_DIR", "")

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(
        not (CORPUS and REF),
        reason="corpus tests need ESP_CORPUS_DIR and ESP_PREP_REF_DIR set; see README",
    ),
]

FAMILIES = {"F1", "F2", "F3", "F4", "F5", "F7"}
PARITY_FIELDS = [
    "original_page_no", "sliced_page_no", "page_title_matched", "page_role",
    "required_or_optional", "included", "n_chars", "route", "low_yield_flag",
]


def _manifest() -> dict[str, list[dict]]:
    rows = defaultdict(list)
    if CORPUS and REF:
        with open(Path(REF) / "prep-manifest.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows[row["source_file"]].append(row)
    return rows


MANIFEST = _manifest()
_family = {name: rows[0]["family"] for name, rows in MANIFEST.items()}
# Unconfigured runs get one placeholder so the skip reason is shown, not "empty parameter set".
FAMILY_DOCS = sorted(n for n, f in _family.items() if f in FAMILIES) or [None]
IMAGE_ONLY_DOCS = sorted(n for n, f in _family.items() if f == "F6") or [None]
PROCEDURE_PDFS = sorted(n for n, f in _family.items() if not f and n.lower().endswith("pdf")) or [None]


def _bytes(name: str) -> bytes:
    return (Path(CORPUS) / name).read_bytes()


@cache
def _detect(name: str):
    return detect(_bytes(name))


@cache
def _prep(name: str):
    return prep(_bytes(name), _detect(name), source_file=name)


@pytest.mark.parametrize("name", FAMILY_DOCS)
def test_detects_manifest_family(name):
    assert _detect(name).family == _family[name]


@pytest.mark.parametrize("name", IMAGE_ONLY_DOCS)
def test_textless_document_is_image_only(name):
    with pytest.raises(ExtractionError) as err:
        detect(_bytes(name))
    assert err.value.code == "IMAGE_ONLY"


@pytest.mark.parametrize("name", PROCEDURE_PDFS)
def test_procedure_pdf_raises_non_family_error(name):
    with pytest.raises(ExtractionError) as err:
        detect(_bytes(name))
    print(f"{Path(name).stem}: {err.value.code}")


@pytest.mark.parametrize("name", FAMILY_DOCS)
def test_prep_parity(name):
    stem = Path(name).stem
    expected = MANIFEST[name]
    out = _prep(name)
    bad = []  # (page, field, expected, got)

    exp_kept = [int(r["original_page_no"]) for r in expected if r["included"] == "yes"]
    if out.kept_pages != exp_kept:
        bad.append(("-", "kept_pages", exp_kept, out.kept_pages))
    if len(out.manifest) != len(expected):
        bad.append(("-", "row_count", len(expected), len(out.manifest)))
    got_rows = {str(r["original_page_no"]): r for r in out.manifest}
    for exp in expected:
        got = got_rows.get(exp["original_page_no"], {})
        for field in PARITY_FIELDS:
            if str(got.get(field, "<missing>")) != exp[field]:
                bad.append((exp["original_page_no"], field, exp[field], got.get(field, "<missing>")))

    text_dirs = list((Path(REF) / "text").glob(f"*/{glob.escape(stem)}"))
    assert len(text_dirs) == 1, f"{stem}: expected one reference text directory, found {len(text_dirs)}"
    ref_pages = sorted(int(p.stem[1:]) for p in text_dirs[0].glob("p*.txt"))
    if ref_pages != out.kept_pages:
        bad.append(("-", "text_files", ref_pages, out.kept_pages))
    for p in out.kept_pages:
        ref_file = text_dirs[0] / f"p{p}.txt"
        if not ref_file.is_file():
            continue
        ref, got = ref_file.read_bytes(), out.page_text[p].encode("utf-8")
        if got != ref:
            at = next((k for k, (a, b) in enumerate(zip(ref, got)) if a != b), min(len(ref), len(got)))
            bad.append((p, "text", f"{len(ref)} bytes", f"{len(got)} bytes, first difference at byte {at}"))

    assert not bad, f"{stem}: {len(bad)} mismatch(es)\n" + "\n".join(
        f"  {stem} | p{p} | {field} | expected {e!r} | got {g!r}" for p, field, e, g in bad
    )


# --------------------------------------------------------------------------
# Prompt byte check: assemble_prompt against the prompts the runners saved.
#
# Allowed differences, each counted:
#   date            the extraction date
#   family_source   manifest rows: the runners' value vs `detected`
#   signature_note  manifest rows: the runners' page-1 signature note in `notes`
#   extra_rows      manifest rows of another document that `grep -F <stem>` matched
#   validator_line_18  the runs sent an empty line where the copied validator
#                   has a tab: that one substitution, at that one position
#   glyph_translation  F1 only: the runs sent untranslated private-use glyphs,
#                   so the saved page-text blocks are compared after fix_pua
# Anything else fails, reported with the differing byte range on each side.
# --------------------------------------------------------------------------

BATCH = os.environ.get("ESP_BATCH_PROMPT_DIR", "")
SAVED = {p.name[: -len(".prompt.txt")]: p for p in Path(BATCH).glob("*.prompt.txt")} if BATCH else {}
DATE = "\x00DATE\x00"
MAN_START = "# ===== PAGE MANIFEST for this document =====\n"
PAGES_START = "\n# ===== PAGE TEXT =====\n"
SIG_NOTE = re.compile(
    r"page-1 signature disagrees with atlas assignment; missing marker\(s\): .*?"
    r"; processed under the atlas assignment(?: · |$)"
)
VALIDATOR_BANNER = f"# ===== REPORT CONTRACT: {VALIDATOR} =====\n"
_validator_lines = (ASSETS / VALIDATOR).read_bytes().decode("utf-8").split("\n")
# Offset of line 18 in the validator, or None once that line is no longer a lone tab.
LINE_18_AT = sum(len(line) + 1 for line in _validator_lines[:17]) if _validator_lines[17] == "\t" else None
PAGE_BLOCK = re.compile(r"(----- BEGIN (p\d+) -----\n)(.*?)(----- END \2 -----\n)", re.S)
needs_batch = pytest.mark.skipif(not BATCH, reason="prompt byte check needs ESP_BATCH_PROMPT_DIR; see README")


def _stem(name: str) -> str:
    return re.sub(r"\.[Pp][Dd][Ff]$", "", name)


PROMPT_DOCS = [n for n in FAMILY_DOCS if n and _stem(n) in SAVED] or [None]


def _span(what: str, a: str, b: str, base_a: str, base_b: str) -> tuple:
    """(what, byte offset, expected range, got range, expected bytes, got bytes) after trimming
    the common prefix and suffix. base_* is the prompt text before each section."""
    x, y = a.encode("utf-8"), b.encode("utf-8")
    i, n = 0, min(len(x), len(y))
    while i < n and x[i] == y[i]:
        i += 1
    j = 0
    while j < n - i and x[-1 - j] == y[-1 - j]:
        j += 1
    oa, ob = len(base_a.encode("utf-8")), len(base_b.encode("utf-8"))
    return (what, oa + i, (oa + i, oa + len(x) - j), (ob + i, ob + len(y) - j),
            x[i:len(x) - j][:80], y[i:len(y) - j][:80])


def _compare(name: str, family: str, expected: str, got: str):
    stem, counts, bad = _stem(name), defaultdict(int), []
    ie, ig = expected.find(MAN_START), got.find(MAN_START)
    je, jg = expected.find(PAGES_START, ie), got.find(PAGES_START, ig)
    if min(ie, ig, je, jg) < 0:
        return counts, [("section markers", -1, (), (), b"", b"")]
    ie, ig = ie + len(MAN_START), ig + len(MAN_START)

    # Everything before the manifest: identical once the date is filled in.
    pre_e, pre_g = expected[:ie], got[:ig]
    at = pre_g.find(DATE)
    found = pre_e[at:at + 10] if at >= 0 else ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", found):
        counts["date"] += pre_g.count(DATE)
    pre_g = pre_g.replace(DATE, found)
    at = pre_g.find(VALIDATOR_BANNER)
    at = at + len(VALIDATOR_BANNER) + LINE_18_AT if at >= 0 and LINE_18_AT is not None else -1
    if at > 0 and pre_g[at:at + 2] == "\t\n" and pre_e[at - 1:at + 1] == "\n\n":
        pre_e = pre_e[:at] + "\t" + pre_e[at:]
        counts["validator_line_18"] += 1
    if pre_e != pre_g:
        bad.append(_span("before manifest", pre_e, pre_g, "", ""))

    # Manifest: same header; this document's rows equal apart from the allowed fields.
    lines_e, lines_g = expected[ie:je].split("\n"), got[ig:jg].split("\n")
    lines_e, lines_g = lines_e[:-1], lines_g[:-1]  # each block ends with a newline
    header = next(csv.reader([lines_g[0]]))
    if lines_e[0] != lines_g[0]:
        bad.append(_span("manifest header", lines_e[0], lines_g[0], expected[:ie], got[:ig]))
    own = []
    for line in lines_e[1:]:
        if next(csv.reader([line]))[0] == name:
            own.append(line)
        elif stem in line:
            counts["extra_rows"] += 1
        else:
            bad.append(("manifest row grep -F would not match", -1, (), (), line.encode()[:80], b""))
    if len(own) != len(lines_g) - 1:
        bad.append(("manifest row count", -1, (), (), str(len(own)).encode(), str(len(lines_g) - 1).encode()))
    for le, lg in zip(own, lines_g[1:]):
        if le == lg:
            continue
        de = dict(zip(header, next(csv.reader([le]))))
        dg = dict(zip(header, next(csv.reader([lg]))))
        if de.get("family_source") != dg.get("family_source"):
            counts["family_source"] += 1
        notes = SIG_NOTE.sub("", de.get("notes", ""), count=1)
        if notes != de.get("notes", ""):
            counts["signature_note"] += 1
        others = [k for k in header if k not in ("family_source", "notes") and de.get(k) != dg.get(k)]
        if others or notes != dg.get("notes"):
            bad.append(_span(f"manifest row {de.get('original_page_no')}: {others or ['notes']}",
                             le, lg, expected[:ie], got[:ig]))

    # Page text: identical; for F1, after translating glyphs inside the page blocks only.
    pages_e = expected[je:]
    if family == "F1":
        def translate(m):
            counts["glyph_translation"] += sum(c in PUA_MAP for c in m[3])
            return m[1] + fix_pua(m[3]) + m[4]
        pages_e = PAGE_BLOCK.sub(translate, pages_e)
    if pages_e != got[jg:]:
        bad.append(_span("page text", pages_e, got[jg:], expected[:je], got[:jg]))
    return counts, bad


@needs_batch
def test_saved_prompt_inventory():
    docs = [n for n in FAMILY_DOCS if n]
    missing = [_stem(n) for n in docs if _stem(n) not in SAVED]
    print(f"saved prompts: {len(docs) - len(missing)} of {len(docs)} documents; without one: {missing}")


@needs_batch
@pytest.mark.parametrize("name", PROMPT_DOCS)
def test_prompt_bytes_match_runner(name):
    out = _prep(name)
    got = assemble_prompt(_detect(name).family, out, name, extraction_date=DATE, clarifications=False)
    expected = SAVED[_stem(name)].read_bytes().decode("utf-8")
    counts, bad = _compare(name, _detect(name).family, expected, got)
    pages = len(out.kept_pages)
    print(f"{_stem(name)} | {_detect(name).family} | {len(got.replace(DATE, '0000-00-00').encode())} bytes"
          f" | {pages} pages | " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
          + f" | failures={len(bad)}")
    assert not bad, f"{_stem(name)}: {len(bad)} difference(s) outside the allowed categories\n" + "\n".join(
        f"  {what} | offset {off} | expected bytes {er} {eb!r} | got bytes {gr} {gb!r}"
        for what, off, er, gr, eb, gb in bad[:10]
    )
