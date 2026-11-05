"""Build the transcription: one PDF of typed pages, one per scan page, the scan attached byte for byte, and the mark.

    python3 scripts/build_transcription.py SCAN.pdf --model "<the model that typed it>"

It reads the page files page-001.txt, page-002.txt, ... from the pages folder ("<scan name> - pages", in the output
folder) and refuses on any gap: a page file missing or empty, or a count of page files that is not the scan's page
count.  Nothing is written then.

Each typed page starts with the notice line, then a blank line, then the page file's lines as typed, in DejaVu Sans
Mono 12 pt on 13 pt lines.  Each character sits on pdfplumber's layout grid (7.25 pt columns, 13 pt lines), so the
design-extraction app reads every line back with its columns.  A page grows from Letter to fit its longest line and
its line count, so no line wraps and each scan page stays one page.

The output, "<scan name without .pdf> - transcription.pdf", goes to /mnt/user-data/outputs/ when that folder exists,
else beside the scan.  Its document information holds /RoamTranscription, one JSON object (MARK_FIELDS), the Title
"Transcription of <original name>", the skill and its version as Creator and Producer, the mark's transcribed_at (UTC)
as both dates, and no Author.  Prints the file, its page count and the readings marked unsure ([?]) by page.

Packages (pypdf, reportlab): in claude.ai's sandbox (/mnt/user-data/ exists) the missing are installed with pip,
with --break-system-packages only when plain pip refuses (exit 2 if they are still missing).  Anywhere else nothing is
installed: it names what is missing and the commands that set up a virtual environment in the working folder (SETUP),
and stops with exit 4.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL, SKILL_VERSION, SCHEMA = "transcribe-design-scan", "1", 1
SOFTWARE = f"{SKILL} skill, version {SKILL_VERSION}"      # the document information's Creator and Producer
MARK = "/RoamTranscription"
MARK_FIELDS = ("schema", "skill", "skill_version", "original", "attachment", "transcribed_at", "model", "unsure")
NOTICE = "Transcription - page {n} of {m} - check every value against the original scan attached to this file."
UNSURE = "[?]"
FONT = Path(__file__).resolve().parents[1] / "fonts" / "DejaVuSansMono.ttf"
SIZE = 12                     # pt
PITCH, LINE = 7.25, 13        # pt per column and per line: pdfplumber 0.11.9's DEFAULT_X_DENSITY and DEFAULT_Y_DENSITY
LEFT, TOP = 5, 3              # margins, in columns and lines, on every side
LETTER = (612, 792)
SANDBOX = Path("/mnt/user-data")     # claude.ai's sandbox: the only place the scripts install packages
OUTPUTS = SANDBOX / "outputs"
SETUP = ("python3 -m venv .venv", ".venv/bin/pip install pypdf reportlab pypdfium2 pillow")   # anywhere else
FALLBACK = "original-scan.pdf"
PAGE_FILE = re.compile(r"page-(\d+)\.txt")
NEEDS = (("pypdf", "pypdf"), ("reportlab", "reportlab"))


class Refused(Exception):
    """The transcription cannot be built as it stands; the message says why."""


# ---- names and folders, shared with render_pages.py ----

def output_dir(scan: Path) -> Path:
    return OUTPUTS if OUTPUTS.is_dir() else scan.resolve().parent


def attachment_name(scan: Path) -> str:
    """The scan's own file name, unless it cannot name an attachment: then original-scan.pdf."""
    name = scan.name
    usable = name.isprintable() and name.lower().endswith(".pdf") and len(name) > 4 and not set(name) & set("/\\:")
    return name if usable else FALLBACK


def stem(scan: Path) -> str:
    return attachment_name(scan)[:-4]


def pages_dir(scan: Path, out: Path | None = None) -> Path:
    return (out or output_dir(scan)) / f"{stem(scan)} - pages"


def page_file(n: int) -> str:
    return f"page-{n:03d}.txt"


def ensure(needs) -> list[str]:
    """Each (module, pip name) importable; the pip names still missing.  In claude.ai's sandbox it first installs the
    missing with pip, with --break-system-packages only when plain pip refuses; anywhere else it installs nothing."""
    missing = [pip for module, pip in needs if importlib.util.find_spec(module) is None]
    for extra in ([], ["--break-system-packages"]) if SANDBOX.is_dir() else ():
        if not missing:
            break
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *extra, *missing], capture_output=True)
        importlib.invalidate_caches()
        missing = [pip for module, pip in needs if pip in missing and importlib.util.find_spec(module) is None]
    return missing


def say_missing(missing) -> int:
    """Name what is missing; the exit code: 2 in claude.ai's sandbox, where pip could not install it, and 4 anywhere
    else, where nothing is installed, with the commands that set up a virtual environment in the working folder."""
    if SANDBOX.is_dir():
        print(f"missing and not installable here: {', '.join(missing)}; install them, then run this again")
        return 2
    print(f"missing: {', '.join(missing)}.  Outside claude.ai these scripts install nothing.  Set up a virtual "
          "environment in the working folder, then run both scripts with .venv/bin/python, never the machine's own:")
    print("\n".join(f"    {command}" for command in SETUP))
    return 4


# ---- the pages ----

def read_pages(folder: Path, count: int) -> list[list[str]]:
    """Every page file's lines, tabs expanded, trailing spaces trimmed; Refused on any gap."""
    found = {int(m[1]): p for p in folder.glob("page-*.txt") if (m := PAGE_FILE.fullmatch(p.name))} \
        if folder.is_dir() else {}
    problems = [f"{page_file(n)} is missing" for n in range(1, count + 1) if n not in found]
    pages = []
    for n in range(1, count + 1):
        if n in found:
            lines = [line.expandtabs().rstrip() for line in found[n].read_text(encoding="utf-8").splitlines()]
            while lines and not lines[-1]:
                lines.pop()
            if not lines:
                problems.append(f"{page_file(n)} is empty (a blank scan page is typed as [Blank page])")
            pages.append(lines)
    if len(found) != count:
        problems.append(f"there are {len(found)} page files for a scan of {count} pages")
    if problems:
        raise Refused(f"nothing built, as {folder} does not hold the scan's pages: " + "; ".join(problems))
    return pages


def unsure(pages) -> list[dict]:
    """One {page, line} per [?], the line counted in its page file, from 1."""
    return [{"page": n, "line": k} for n, lines in enumerate(pages, 1) for k, line in enumerate(lines, 1)
            for _ in range(line.count(UNSURE))]


def typed_pdf(pages) -> bytes:
    """The typed pages: each line's words placed on the layout grid, the notice first."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    if "DejaVuSansMono" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVuSansMono", str(FONT)))
    face = pdfmetrics.getFont("DejaVuSansMono").face
    # pdfplumber puts a character's top a font size above its descent, so each baseline is set to give a top of
    # exactly LINE * row: then the row it reads is the row typed.
    drop = SIZE + face.descent * SIZE / 1000
    advance = pdfmetrics.stringWidth("0", "DejaVuSansMono", SIZE)
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=LETTER, pageCompression=1)
    for n, lines in enumerate(pages, 1):
        rows = [NOTICE.format(n=n, m=len(pages)), "", *lines]
        width = max(LETTER[0], (2 * LEFT + max(map(len, rows))) * PITCH)
        height = max(LETTER[1], (2 * TOP + len(rows)) * LINE)
        pdf.setPageSize((width, height))
        text = pdf.beginText()
        text.setFont("DejaVuSansMono", SIZE)
        text.setCharSpace(PITCH - advance)                  # the glyphs on the grid inside a word too
        for row, line in enumerate(rows, TOP):
            for word in re.finditer(r"\S+", line):
                text.setTextOrigin((LEFT + word.start()) * PITCH, height - row * LINE - drop)
                text.textOut(word[0])
        pdf.drawText(text)
        pdf.showPage()
    pdf.save()
    return out.getvalue()


# ---- the transcription ----

def build(scan: Path, model: str, *, pages: Path | None = None, out: Path | None = None, now=None) -> Path:
    """Write the transcription of `scan` from the page files in `pages` (default: its pages folder) to `out` (default:
    the output folder); return its path.  Refused on any gap, and when `model` is empty."""
    from pypdf import PdfReader, PdfWriter

    scan = Path(scan)
    data = scan.read_bytes()
    try:
        count = len(PdfReader(io.BytesIO(data)).pages)
    except Exception as err:  # noqa: BLE001 - say so in words, build nothing
        raise Refused(f"the scan {scan.name} cannot be read as a PDF ({type(err).__name__})") from err
    if not model.strip():
        raise Refused("name the model that typed the pages with --model")
    typed = read_pages(pages or pages_dir(scan, out), count)
    name, when = attachment_name(scan), now or datetime.now(timezone.utc)
    mark = {"schema": SCHEMA, "skill": SKILL, "skill_version": SKILL_VERSION,
            "original": {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "pages": count, "bytes": len(data)},
            "attachment": name,
            "transcribed_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": model.strip(), "unsure": unsure(typed)}
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(typed_pdf(typed))))
    writer.add_attachment(name, data)
    date = when.strftime("D:%Y%m%d%H%M%SZ")                 # transcribed_at, as a PDF date
    writer.metadata = {"/Title": f"Transcription of {name}", "/Creator": SOFTWARE, "/Producer": SOFTWARE,
                       "/CreationDate": date, "/ModDate": date, MARK: json.dumps(mark)}   # in place of reportlab's
    target = (out or output_dir(scan)) / f"{stem(scan)} - transcription.pdf"
    with open(target, "wb") as f:
        writer.write(f)
    return target


def unsure_words(entries) -> str:
    """`page 2: lines 14 and 30; page 5: line 3`, or that none is marked."""
    by_page = {}
    for u in entries:
        by_page.setdefault(u["page"], []).append(u["line"])
    if not by_page:
        return "no reading marked unsure"
    parts = []
    for page, lines in by_page.items():
        lines = list(dict.fromkeys(lines))
        parts.append(f"page {page}: line{'s' * (len(lines) > 1)} "
                     + (str(lines[0]) if len(lines) == 1 else ", ".join(map(str, lines[:-1])) + f" and {lines[-1]}"))
    return "; ".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scan", type=Path, help="the scanned design document (PDF)")
    ap.add_argument("--model", required=True, help="the model that typed the pages, as it names itself")
    args = ap.parse_args(argv)
    missing = ensure(NEEDS)
    if missing:
        return say_missing(missing)
    if not FONT.is_file():
        print(f"the font is missing: {FONT}")
        return 2
    try:
        target = build(args.scan, args.model)
    except Refused as err:
        print(f"refused: {err}")
        return 1
    from pypdf import PdfReader
    reader = PdfReader(target)
    mark = json.loads(reader.metadata[MARK])
    print(f"written: {target}")
    print(f"pages: {len(reader.pages)} typed, one per scan page; the scan attached as {mark['attachment']}")
    print(f"unsure: {unsure_words(mark['unsure'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
