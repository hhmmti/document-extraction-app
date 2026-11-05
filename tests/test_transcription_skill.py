"""The transcribe-design-scan skill (skills/transcribe-design-scan/): its build script and its render script on a small
synthetic scan the tests make, each page drawn as a picture with no text layer.  No model, no AWS, no network: every
package the scripts need is installed, and outside claude.ai's sandbox they install nothing.  The tests of the
installs fake the missing import and the pip call (`fake_packages`): no pip runs, and the machine's Python is not used.

`make_scan`, `type_pages` and `transcribe` are shared with tests/test_transcription.py, whose inputs are built with the
skill's own build script too.
"""

import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
import pdfplumber.utils.text as layout
import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from extraction.detect import detect, route
from extraction.errors import ExtractionError
from extraction.prep import prep_generic

SKILL = Path(__file__).resolve().parents[1] / "skills" / "transcribe-design-scan"
NOW = datetime(2026, 11, 4, 12, 30, 5, tzinfo=timezone.utc)


def script(name):
    """One of the skill's scripts, loaded under a module name of its own."""
    spec = importlib.util.spec_from_file_location(f"skill_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = script("build_transcription")
TABLE = ("PUMP DATA\n"
         "                 Case 1      Case 2\n"
         "  Model          SF2700      SF2700\n"
         "  Stages         120         128 [?]\n"
         "  Setting depth  9,850 ft    9,850 ft\n"
         "\n"
         "[Drawing: labels from top to bottom]\n"
         "Tubing 2 7/8 in\n"
         "Pump  [?]\n"
         "[Handwritten: checked 3/4]   [Stamp: APPROVED]   [Signature]")


def make_scan(folder: Path, pages: int = 2, name: str = "Design scan.pdf") -> Path:
    """A scan: each page a picture of boxes and strokes, with no text layer."""
    images = []
    for n in range(pages):
        image = Image.new("L", (612, 792), 255)
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 60 + 20 * n, 550, 320), outline=0, width=3)
        draw.line((80, 420, 520, 440 + 10 * n), fill=0, width=2)
        images.append(image)
    path = folder / name
    images[0].save(path, "PDF", save_all=True, append_images=images[1:], resolution=72)
    return path


def type_pages(scan: Path, out: Path, pages) -> Path:
    """The page files, as the skill's method leaves them in the pages folder."""
    folder = B.pages_dir(scan, out)
    folder.mkdir(parents=True, exist_ok=True)
    for n, text in enumerate(pages, 1):
        (folder / B.page_file(n)).write_text(text, encoding="utf-8")
    return folder


def transcribe(folder: Path, pages, *, name="Design scan.pdf", model="the test's model") -> tuple[Path, Path]:
    """(the scan, its transcription), built by the skill's build script."""
    scan = make_scan(folder, len(pages), name)
    type_pages(scan, folder, pages)
    return scan, B.build(scan, model, out=folder, now=NOW)


def layout_lines(pdf: Path) -> list[list[str]]:
    """Each page's layout text, as Prep reads it, line by line with trailing spaces trimmed."""
    with pdfplumber.open(pdf) as doc:
        return [[line.rstrip() for line in (p.extract_text(layout=True) or "").split("\n")] for p in doc.pages]


def placed(rows) -> list[str]:
    """The lines a page's layout text holds for `rows`, typed from its first row: the top margin, then each row in its
    columns, the left margin before it."""
    return [""] * B.TOP + [(" " * B.LEFT + row).rstrip() for row in rows]


def rows_of(n, m, text) -> list[str]:
    return [B.NOTICE.format(n=n, m=m), "", *(line.rstrip() for line in text.split("\n"))]


def mark_of(pdf: Path) -> dict:
    return json.loads(PdfReader(pdf).metadata[B.MARK])


def fake_packages(monkeypatch, gone: set, pip=lambda argv: 1) -> list:
    """The modules in `gone` not installed, as importlib's find_spec tells the scripts; every subprocess.run recorded
    and none run, `pip(argv)` giving its exit code, an exit 0 installing them.  The calls made."""
    find, calls = importlib.util.find_spec, []
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name in gone else find(name, *a))

    def run(argv, **_):
        calls.append(argv)
        if (code := pip(argv)) == 0:
            gone.clear()
        return subprocess.CompletedProcess(argv, code)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


# ---- the layout grid is pdfplumber's ----

def test_the_grid_is_pdfplumbers_layout_pitch():
    assert pdfplumber.__version__ == "0.11.9"
    assert (layout.DEFAULT_X_DENSITY, layout.DEFAULT_Y_DENSITY) == (B.PITCH, B.LINE) == (7.25, 13)
    assert (B.SIZE, B.LINE) == (12, 13) and B.FONT.is_file() and (B.FONT.parent / "LICENSE").is_file()


# ---- what it makes ----

def test_one_typed_page_per_scan_page_each_starting_with_the_notice(tmp_path):
    scan, out = transcribe(tmp_path, ["first page", "[Blank page]", "third page"])
    assert out == tmp_path / "Design scan - transcription.pdf" and len(PdfReader(out).pages) == 3
    for n, lines in enumerate(layout_lines(out), 1):
        assert lines[B.TOP] == " " * B.LEFT + f"Transcription - page {n} of 3 - check every value against the original " \
                                              "scan attached to this file."
        assert not any(lines[:B.TOP])
    with pytest.raises(ExtractionError) as err:
        detect(scan.read_bytes())
    assert err.value.code == "IMAGE_ONLY"                       # the scan itself has no text layer


def test_the_mark_and_the_attachment(tmp_path):
    scan, out = transcribe(tmp_path, [TABLE, "page two"])
    data = scan.read_bytes()
    mark = mark_of(out)
    assert list(mark) == list(B.MARK_FIELDS)
    assert mark == {"schema": 1, "skill": "transcribe-design-scan", "skill_version": "1",
                    "original": {"name": "Design scan.pdf", "sha256": hashlib.sha256(data).hexdigest(), "pages": 2,
                                 "bytes": len(data)},
                    "attachment": "Design scan.pdf", "transcribed_at": "2026-11-04T12:30:05Z",
                    "model": "the test's model", "unsure": [{"page": 1, "line": 4}, {"page": 1, "line": 9}]}
    attached = PdfReader(out).attachments
    assert list(attached) == ["Design scan.pdf"] and attached["Design scan.pdf"] == [data]   # byte for byte
    assert hashlib.sha256(attached["Design scan.pdf"][0]).hexdigest() == mark["original"]["sha256"]


def test_the_document_information_names_the_transcription_and_the_skill(tmp_path):
    scan, out = transcribe(tmp_path, ["one"])
    info = PdfReader(out).metadata
    assert sorted(info) == sorted(["/Title", "/Creator", "/Producer", "/CreationDate", "/ModDate", B.MARK])  # no Author
    assert info.title == "Transcription of Design scan.pdf"
    assert info.creator == info.producer == "transcribe-design-scan skill, version 1"
    assert info["/CreationDate"] == info["/ModDate"] == "D:20261104123005Z"                    # UTC
    assert info.creation_date == info.modification_date == datetime.fromisoformat(mark_of(out)["transcribed_at"])
    assert not re.search(rb"(?i)anonymous|untitled", out.read_bytes().replace(scan.read_bytes(), b""))


def test_a_name_that_cannot_name_a_pdf_attaches_as_original_scan(tmp_path):
    _, out = transcribe(tmp_path, ["one"], name="scan-without-extension")
    assert out.name == "original-scan - transcription.pdf"
    assert list(PdfReader(out).attachments) == ["original-scan.pdf"] == [mark_of(out)["attachment"]]
    assert mark_of(out)["original"]["name"] == "original-scan.pdf"


def test_it_writes_to_the_outputs_folder_when_there_is_one(tmp_path, monkeypatch):
    scan = make_scan(tmp_path, 1)
    assert B.output_dir(scan) == tmp_path
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setattr(B, "OUTPUTS", outputs)
    assert B.output_dir(scan) == outputs and B.pages_dir(scan) == outputs / "Design scan - pages"
    type_pages(scan, outputs, ["one"])
    assert B.build(scan, "m", now=NOW) == outputs / "Design scan - transcription.pdf"


# ---- the round trip: Detect and Prep read every typed line back with its columns ----

def test_layout_text_and_prep_give_back_each_typed_line_with_its_columns(tmp_path):
    pages = [TABLE, "  indented 2\n\n     five spaces in, then    four more\nend"]
    _, out = transcribe(tmp_path, pages)
    data = out.read_bytes()
    prepped = prep_generic(data)
    assert prepped.kept_pages == [1, 2]
    for n, (lines, text) in enumerate(zip(layout_lines(out), pages), 1):
        want = placed(rows_of(n, 2, text))
        assert lines[:len(want)] == want and not any(lines[len(want):])
        assert [line.rstrip() for line in prepped.page_text[n].split("\n")] == lines
    assert route(data).route == "generic"                       # read as a PDF with a text layer: not IMAGE_ONLY


def test_unsure_readings_are_listed_one_per_mark(tmp_path):
    _, out = transcribe(tmp_path, ["a\nb [?]\n[?] and [?]", "[Blank page]", "x\ny\nz [?]"])
    assert mark_of(out)["unsure"] == [{"page": 1, "line": 2}, {"page": 1, "line": 3}, {"page": 1, "line": 3},
                                      {"page": 3, "line": 3}]
    assert B.unsure_words(mark_of(out)["unsure"]) == "page 1: lines 2 and 3; page 3: line 3"
    assert B.unsure_words([]) == "no reading marked unsure"


def test_characters_outside_latin_1_survive(tmp_path):
    text = "BHT 212 °F   Motor® 450 hp   ½ in   ΔP ≥ 1,200 psi   gas 0.7 º"
    _, out = transcribe(tmp_path, [text])
    assert layout_lines(out)[0][B.TOP + 2] == " " * B.LEFT + text
    assert prep_generic(out.read_bytes()).page_text[1].split("\n")[B.TOP + 2].rstrip() == " " * B.LEFT + text


def test_a_long_line_and_a_long_page_grow_the_page(tmp_path):
    long_line = "COL" + "".join(f"{k:>8}" for k in range(1, 26))            # 203 characters
    long_page = "\n".join(f"row {k:3d}   {k * 12.5:9.2f} psia" for k in range(1, 121))
    _, out = transcribe(tmp_path, [long_line, long_page, "short"])
    with pdfplumber.open(out) as doc:
        sizes = [(p.width, p.height) for p in doc.pages]
    notice = len(B.NOTICE.format(n=1, m=3))
    assert sizes[0] == ((2 * B.LEFT + 203) * B.PITCH, 792)                  # wider than Letter for its line
    assert sizes[1] == ((2 * B.LEFT + notice) * B.PITCH, (2 * B.TOP + 122) * B.LINE)    # taller for its 122 rows
    assert sizes[2][1] == 792 and len(sizes) == 3                            # each scan page still one page
    lines = layout_lines(out)
    assert lines[0][B.TOP + 2] == " " * B.LEFT + long_line                   # no line wraps
    assert lines[1][:B.TOP + 122] == placed(rows_of(2, 3, long_page))


# ---- refusals: nothing is built ----

@pytest.mark.parametrize("change,problem", [
    (lambda folder: (folder / "page-002.txt").unlink(), "page-002.txt is missing"),
    (lambda folder: (folder / "page-004.txt").write_text("a fourth page"), "there are 4 page files for a scan of 3 pages"),
    (lambda folder: (folder / "page-003.txt").write_text("  \n\n"), "page-003.txt is empty"),
], ids=["a page file missing", "a page count that is not the scan's", "an empty page file"])
def test_a_gap_is_refused_and_nothing_is_written(tmp_path, change, problem):
    scan = make_scan(tmp_path, 3)
    change(type_pages(scan, tmp_path, ["one", "two", "three"]))
    with pytest.raises(B.Refused, match=re.escape(problem)):
        B.build(scan, "m", out=tmp_path)
    assert not list(tmp_path.glob("*transcription*"))
    with pytest.raises(B.Refused, match="name the model"):
        B.build(scan, "  ", out=tmp_path)


def test_the_build_command_refuses_with_its_reason(tmp_path, monkeypatch, capsys):
    scan = make_scan(tmp_path, 2)
    type_pages(scan, tmp_path, ["one"])
    assert B.main([str(scan), "--model", "m"]) == 1
    assert capsys.readouterr().out.startswith("refused: nothing built, as ") and not list(tmp_path.glob("*.pdf"))[1:]
    type_pages(scan, tmp_path, ["one", "two [?]"])
    assert B.main([str(scan), "--model", "m"]) == 0
    said = capsys.readouterr().out
    assert "pages: 2 typed, one per scan page" in said and "unsure: page 2: line 1" in said


# ---- packages: pip only in claude.ai's sandbox ----

@pytest.mark.parametrize("name,gone", [("build_transcription", "reportlab"), ("render_pages", "pypdf")])
def test_outside_claude_ai_nothing_is_installed_and_the_virtual_environment_is_named(tmp_path, monkeypatch, capsys,
                                                                                   name, gone):
    module = script(name)
    monkeypatch.setattr(getattr(module, "B", module), "SANDBOX", tmp_path / "user-data")   # not there: not claude.ai
    scan = make_scan(tmp_path, 1)
    type_pages(scan, tmp_path, ["one"])
    calls = fake_packages(monkeypatch, {gone})
    assert module.main([str(scan), *(["--model", "m"] if name == "build_transcription" else [])]) == 4
    said = capsys.readouterr().out
    assert calls == [] and said.startswith(f"missing: {gone}.") and not list(tmp_path.glob("*transcription*"))
    assert [line.strip() for line in said.splitlines()[-2:]] == list(B.SETUP)


@pytest.mark.parametrize("plain,forced,code", [(0, None, 0), (1, 0, 0), (1, 1, 2)],
                         ids=["plain pip installs", "plain pip refuses", "neither installs"])
def test_in_claude_ais_sandbox_pip_installs_what_is_missing(tmp_path, monkeypatch, capsys, plain, forced, code):
    monkeypatch.setattr(B, "SANDBOX", tmp_path)                                             # there: claude.ai's sandbox
    scan = make_scan(tmp_path, 1)
    type_pages(scan, tmp_path, ["one"])
    calls = fake_packages(monkeypatch, {"reportlab"},
                          lambda argv: forced if "--break-system-packages" in argv else plain)
    assert B.main([str(scan), "--model", "m"]) == code
    pip = [sys.executable, "-m", "pip", "install", "--quiet"]
    assert calls == [pip + ["reportlab"], pip + ["--break-system-packages", "reportlab"]][:1 + bool(plain)]
    assert (tmp_path / "Design scan - transcription.pdf").is_file() == (code == 0)
    said = capsys.readouterr().out
    assert said.startswith("written: " if code == 0 else "missing and not installable here: reportlab")


# ---- rendering ----

def test_render_pages_and_a_crop_turned_upright(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    render = script("render_pages")
    scan, out = transcribe(tmp_path, ["one", "two"])
    assert render.main([str(scan)]) == 0
    said = capsys.readouterr().out
    assert f"sha256: {hashlib.sha256(scan.read_bytes()).hexdigest()}\npages: 2" in said
    assert "typed already: 1, 2; every page is typed" in said
    pngs = sorted((tmp_path / "tmp").glob("transcribe-*/page-*.png"))
    assert [p.name for p in pngs] == ["page-001.png", "page-002.png"]
    assert Image.open(pngs[0]).size == (1700, 2200)                        # 200 dpi
    crop = render.crop(scan, 1, (0, 0, 0.5, 0.25), 400, 90, tmp_path)
    assert Image.open(crop).size == (1100, 1700)                           # 400 dpi, a quarter turn
    assert render.main([str(out)]) == 3                                     # page 1 has text: nothing rendered
    assert "needs no transcription" in capsys.readouterr().out


# ---- the method ----

def test_the_method_says_who_reads_and_never_asks_to_extract():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    front = dict(re.findall(r"^(\w+): (.+)$", text.split("---")[1], re.M))
    assert front["name"] == SKILL.name and "design-extraction app" in front["description"]
    assert "scanned or image-only ESP design document" in front["description"]
    assert "written for Claude Opus 5.5" in text and "No OCR engine" in text
    assert all(command in text for command in B.SETUP)                  # the virtual environment the scripts name
    for name in ("SKILL.md", "reading-aids.md"):
        said = (SKILL / name).read_text(encoding="utf-8")
        said = said.replace("design-extraction", "").replace("Extract Production Services", "")   # names
        assert not re.search(r"(?i)\bextract", said), name
    assert "XSize" in (SKILL / "reading-aids.md").read_text(encoding="utf-8")
