"""Detect and Prep on PDFs built in memory. Needs no corpus."""

import io

import pytest
from pypdf import PdfWriter
from pypdf.constants import PageAttributes as PG
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from extraction import ExtractionError, detect, prep
from extraction.families import PAGE_MAP


def make_pdf(pages: list[list[str]]) -> bytes:
    """One page per list; each string is a Helvetica text line, top to bottom."""
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for lines in pages:
        page = writer.add_blank_page(612, 792)
        if not lines:
            continue
        page[NameObject(PG.RESOURCES)] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        stream.set_data("".join(
            f"BT /F1 12 Tf 72 {720 - 24 * k} Td ({ln}) Tj ET\n" for k, ln in enumerate(lines)
        ).encode("latin-1"))
        page.replace_contents(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# A minimal F4 document: signature and first required role on p1, the second
# required role on p2, and a plot-only page that prep must drop.
F4_P1 = ["Well Name : Test Well", "Powered by LiftXP", "Manufacture", "Pumps Top Bottom"]
F4_P2 = ["OPERATING PARAMETERS", "Pump Intake Pressure 450 psi"]
F4_PLOT = ["Frequency Head chart"]


def detect_error(pdf_bytes: bytes) -> ExtractionError:
    with pytest.raises(ExtractionError) as err:
        detect(pdf_bytes)
    return err.value


def test_two_families_on_page_1_is_ambiguous():
    err = detect_error(make_pdf([["SIZING REPORT", "SLB Engineer", "SLB | Schematics Report 1"]]))
    assert err.code == "AMBIGUOUS_TEMPLATE"
    assert err.details["families"] == ["F1", "F3"]


def test_known_family_without_a_required_page():
    err = detect_error(make_pdf([F4_P1, F4_PLOT]))
    assert err.code == "REQUIRED_PAGES_MISSING"
    assert err.details["missing_roles"] == [PAGE_MAP["F4"][1][0]]


def test_blank_pdf_is_image_only():
    assert detect_error(make_pdf([[], []])).code == "IMAGE_ONLY"


def test_text_without_markers_is_unknown():
    err = detect_error(make_pdf([["This page carries ordinary text and no template markers."]]))
    assert err.code == "UNKNOWN_TEMPLATE"


# A newer F2 generator: the title, a relabelled representative block, no vendor name in the text.
F2_NEWER = [["EQUIPMENT AND PERFORMANCE REPORT", "Account Manager | Customer Rep"], ["Summary"],
            ["Performance Curve: 60 Hz"], ["Gas Separator / BOI"], ["Surface Equipment"], ["Motor Rating 100"]]
F3_P1 = ["Schematics Report", "SLB Engineer", "SLB | Schematics Report 1"]
F3_GENERAL = ["General Report", "SLB Engineer", "SLB | General Report 2"]


def test_f2_is_its_title_and_its_required_pages():
    assert detect(make_pdf(F2_NEWER)).family == "F2"
    err = detect_error(make_pdf(F2_NEWER[:2]))
    assert (err.code, err.details["family"]) == ("REQUIRED_PAGES_MISSING", "F2")
    assert err.details["missing_roles"] == [r for r, s, _m in PAGE_MAP["F2"][2:6]]


def test_f3_is_the_engineer_and_the_report_footer():
    assert detect(make_pdf([F3_P1, F3_GENERAL])).family == "F3"
    # A partial document, its schematics pages left out, is still F3 and fails on the missing page.
    err = detect_error(make_pdf([F3_GENERAL]))
    assert (err.code, err.details) == ("REQUIRED_PAGES_MISSING",
                                       {"family": "F3", "missing_roles": ["Schematics Report"]})
    # The footer must read `SLB | … Report`.
    assert detect_error(make_pdf([[*F3_GENERAL[:2], "SLB | General 2"]])).code == "UNKNOWN_TEMPLATE"


def test_unknown_template_names_its_near_misses_and_producer():
    err = detect_error(make_pdf([F3_GENERAL[:2] + ["Prepared for Jo Example"]]))
    assert err.code == "UNKNOWN_TEMPLATE"
    assert err.details == {"families": [], "producer": "pypdf", "near_misses": [
        {"family": "F3", "markers_matched": 1, "markers": 2,
         "required_found": ["General Report"], "required_missing": ["Schematics Report"]}]}
    assert err.message == ("no family's page-1 signature matched; near misses: F3 with 1 of 2 page-1 markers "
                           "and 1 of 2 required pages (missing Schematics Report); PDF producer: pypdf")
    err = detect_error(make_pdf([["This page carries ordinary text and no template markers."]]))
    assert err.message == ("no family's page-1 signature matched; no family has any of its page-1 markers; "
                           "PDF producer: pypdf")


def test_unreadable_bytes_fail_prep():
    assert detect_error(b"not a pdf").code == "PREP_FAILED"
    det = detect(make_pdf([F4_P1, F4_P2]))
    with pytest.raises(ExtractionError) as err:
        prep(b"not a pdf", det)
    assert err.value.code == "PREP_FAILED"


def test_complete_document_detects_and_drops_plot_only_page():
    pdf = make_pdf([F4_P1, F4_P2, F4_PLOT])
    det = detect(pdf)
    assert det.family == "F4"
    out = prep(pdf, det, source_file="synthetic")
    assert out.kept_pages == [1, 2]
    assert sorted(out.page_text) == [1, 2]
    assert "Powered by LiftXP" in out.page_text[1]
    assert [(r["included"], r["sliced_page_no"], r["required_or_optional"]) for r in out.manifest] == [
        ("yes", 1, "required"), ("yes", 2, "required"), ("no", "", "plot_only"),
    ]
    assert {r["family_source"] for r in out.manifest} == {"detected"}


def test_unmapped_private_use_codepoint_fails_prep(monkeypatch):
    """The residual check, fed layout text directly: a mapped glyph is translated,
    an unmapped one is PREP_FAILED."""
    import importlib
    from contextlib import contextmanager

    from extraction.detect import Detection

    prep_module = importlib.import_module("extraction.prep")
    det = Detection("F4", 1, {1: [("role", "required", "line")]})

    def fake_pdf(text):
        page = type("Page", (), {"extract_text": lambda self, layout=False: text})()

        @contextmanager
        def read_pdf(_bytes):
            yield type("Pdf", (), {"pages": [page]})()
        return read_pdf

    monkeypatch.setattr(prep_module, "read_pdf", fake_pdf("Pump Intake Pressure 450 psi"))
    assert prep(b"", det).page_text[1] == "Pump Intake Pressure (450) psi"

    monkeypatch.setattr(prep_module, "read_pdf", fake_pdf("Pump Intake Pressure 450 psi "))
    with pytest.raises(ExtractionError) as err:
        prep(b"", det)
    assert err.value.code == "PREP_FAILED"
    assert err.value.details["unmapped_pua"] == {1: ["U+E0FF"]}
