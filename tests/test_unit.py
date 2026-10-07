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
    err = detect_error(make_pdf([["SIZING REPORT", "Schematics Report", "SLB Engineer"]]))
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
