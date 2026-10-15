"""The detect survey over PDFs built in memory, with an invented well list. No corpus, no AWS."""

import csv
import io

import pytest
from pypdf import PdfWriter
from pypdf.constants import PageAttributes as PG
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from test_unit import F4_P1, F4_P2, make_pdf
from tools import detect_survey as survey

WELLS = ("organization_id,well_id\norgalpha,Alpha Well 7H\norgalpha,Alpha Well 8H\n"
         "betaorg,Beta Well 1\nbetaorg,Beta Well 2\nbetaorg,Beta Well 3\n")
# One of F3's two page-1 markers, and one of its two required pages: a near miss.
F3_NEAR = [["Design for Beta Well 1", "SLB Engineer", "General Report"]]
# Both F3 markers and no schematics page: F3, failing on the missing page.
F3_PARTIAL = [["General Report", "SLB Engineer", "SLB | General Report 1"]]


def sheet(rows) -> bytes:
    """One page; each row is a list of (font, size, text) runs on one baseline. R is Helvetica, B Helvetica-Bold."""
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    page[NameObject(PG.RESOURCES)] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject(f"/{key}"): DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject(base)})
        for key, base in (("R", "/Helvetica"), ("B", "/Helvetica-Bold"))})})
    ops = []
    for k, runs in enumerate(rows):
        x = 72
        for font, size, text in runs:
            ops.append(f"BT /{font} {size} Tf {x} {720 - 30 * k} Td ({text}) Tj ET")
            x += size * len(text)
    stream = DecodedStreamObject()
    stream.set_data("\n".join(ops).encode("latin-1"))
    page.replace_contents(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def design_sheet(flow: str) -> bytes:
    """An unknown layout: a title and a section heading, one font each, and a label with its value."""
    return sheet([[("B", 16, "ACME DESIGN SHEET")],
                  [("B", 14, "Prepared by"), ("R", 14, "Jo Example")],
                  [("B", 14, "Pump Data")],
                  [("R", 10, f"flow {flow} barrels per day at the pump intake")],
                  [("R", 10, "rated for the design case only")]])


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "wells.csv").write_text(WELLS)
    f4 = make_pdf([F4_P1, F4_P2])
    docs = {("orgalpha", "alpha-7h"): f4, ("orgalpha", "alpha-8h"): f4,
            ("betaorg", "beta-1"): make_pdf(F3_NEAR),
            ("betaorg", "beta-2"): design_sheet("100"), ("betaorg", "beta-3"): design_sheet("200"),
            ("betaorg", "beta-4"): make_pdf(F3_PARTIAL)}
    for (org, well), data in docs.items():
        path = tmp_path / "in" / org / "well-documents" / well / "esp_design" / "current.pdf"
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
    return tmp_path


def run(folder, monkeypatch, *argv):
    monkeypatch.setenv("ESP_SURVEY_DIR", str(folder / "in"))
    monkeypatch.setenv("ESP_SURVEY_OUT", str(folder / "out"))
    monkeypatch.setenv("ESP_WELL_LIST_PATH", str(folder / "wells.csv"))
    return survey.main(list(argv))


def test_rows_near_misses_and_summary(folder, monkeypatch):
    assert run(folder, monkeypatch) == 0
    with open(folder / "out" / "survey.csv", newline="") as fh:
        rows = {r["path"].split("/")[2]: r for r in csv.DictReader(fh)}
    assert (rows["alpha-7h"]["detection"], rows["alpha-7h"]["required_pages"]) == ("F4", "all found")
    assert rows["alpha-7h"]["sha256"] == rows["alpha-8h"]["sha256"] and not rows["alpha-7h"]["fingerprint"]
    near = rows["beta-1"]
    assert (near["detection"], near["F3_markers"], near["F2_markers"]) == ("UNKNOWN_TEMPLATE", "1/2", "0/1")
    assert (near["F3_required_found"], near["F2_required_found"]) == ("General Report", "")
    assert (near["producer"], near["image_only"]) == ("pypdf", "no")
    partial = rows["beta-4"]
    assert (partial["detection"], partial["required_pages"], partial["F3_markers"]) == (
        "REQUIRED_PAGES_MISSING", "F3 missing: Schematics Report", "2/2")
    # Same producer and headings, different values: one layout. The near miss, all one size, has no heading.
    assert rows["beta-2"]["fingerprint"] == rows["beta-3"]["fingerprint"] != near["fingerprint"]

    text = (folder / "out" / "summary.md").read_text()
    assert "| 6 | 5 | 1 |" in text
    assert "| betaorg/#1 | 1 | UNKNOWN_TEMPLATE | F3, 1 of 2 | 1 of 2; missing: Schematics Report |" in text
    assert ("| betaorg/#4 | 1 | REQUIRED_PAGES_MISSING (F3 missing: Schematics Report) | F3, 2 of 2 "
            "| 1 of 2; missing: Schematics Report |") in text
    assert "| orgalpha/#1 | 2 | F4 | F4, 3 of 3 | 2 of 2 | pypdf | 2 |" in text
    assert (f"| {rows['beta-2']['fingerprint']} | betaorg/#2, betaorg/#3 | pypdf | "
            "`ACME DESIGN SHEET`, `Pump Data` |") in text
    assert "None: every PDF was read and classified." in text
    for name in ("alpha well", "beta well", "alpha-7h", "beta-1", "jo example"):
        assert name not in text.casefold()


def test_headings_leave_out_client_names_and_numbers():
    ids = survey.print_ids([("betaorg", "Beta Well #1")])
    lines = ["Well NameBETA WELL 1",           # the well id printed without its '#'
             "CustomerBetaorg Operating",      # a label run into its value
             "Well API Number00-000-00000",   # a number no well list holds
             "General Report", "General  Report", "Technical Design", "Summary", "Extra"]
    assert survey.headings(lines, ids) == ["General Report", "Technical Design", "Summary"]


def test_corpus_mode_reports_a_mismatch(folder, monkeypatch):
    assert run(folder, monkeypatch, "--corpus") == 1
    text = (folder / "out" / "summary.md").read_text()
    assert "| in one of the six families | 2 | 46 |" in text and "| families | 1 | 6 |" in text
    assert "**Does not match the corpus results.**" in text and "Unique documents" not in text


def test_output_inside_the_lab_is_refused(folder, monkeypatch):
    monkeypatch.setattr(survey, "LAB", folder)
    with pytest.raises(SystemExit) as stop:
        run(folder, monkeypatch)
    assert stop.value.code == 2 and not (folder / "out").exists()
