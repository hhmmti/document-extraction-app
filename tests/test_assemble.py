"""Prompt assembly on a synthetic prep result. No corpus."""

import pytest

from extraction.assemble import assemble_prompt, fenced_body, section, well_slug
from extraction.families import MANIFEST_COLUMNS
from extraction.prep import PrepResult


def row(page, sliced, included, notes=""):
    return dict(zip(MANIFEST_COLUMNS, [
        "Test Doc & Co.PDF", "F2", "detected", 3, page, sliced, "", "", "",
        included, included, 200, "text" if included == "yes" else "", "", notes,
    ]))


PREP = PrepResult(
    kept_pages=[1, 3],
    page_text={1: "alpha\nbeta", 3: "gamma\n"},
    manifest=[row(1, 1, "yes", "a, b"), row(2, "", "no"), row(3, 2, "yes")],
)


def test_assembly_order_markers_and_shell_effects():
    p = assemble_prompt("F2", PREP, "uploads/Test Doc & Co.PDF", extraction_date="2026-01-02")
    banners = [
        "# ===== RUNTIME OVERRIDE =====\n",
        "# ===== CONTRACT: f2-championx.md =====\n",
        "# ===== REPORT CONTRACT: m2-validator-spec.md =====\n",
        "# ===== BASE LOAD SCHEMAS (M1) =====\n",
        "# ===== STAGING COLUMNS — these ADD to the base schemas above =====\n",
        "# ===== FIELD LIST (closed — extract nothing outside this) =====\n",
        "# ===== PAGE MANIFEST for this document =====\n",
        "# ===== PAGE TEXT =====\n",
    ]
    at = [p.index(b) for b in banners]
    assert at == sorted(at)
    # well_id None renders as the runners sent it; the date lands; sed's `&` expands.
    assert "Pre-matched well_id: null   ·   match status: unmatched" in p
    assert "2026-01-02" in p
    assert "Test Doc {{source_document}} Co.pdf" in p
    assert "Test Doc {{source_stem}} Co" in p
    # Manifest: DictWriter serialisation, then the echo's blank line.
    header = ",".join(MANIFEST_COLUMNS)
    assert (f"# ===== PAGE MANIFEST for this document =====\n{header}\n"
            'Test Doc & Co.PDF,F2,detected,3,1,1,,,,yes,yes,200,text,,"a, b"\n') in p
    assert 'Test Doc & Co.PDF,F2,detected,3,3,2,,,,yes,yes,200,text,,\n\n# ===== PAGE TEXT' in p
    # A page without a final newline runs into its END marker.
    assert p.endswith(
        "# ===== PAGE TEXT =====\n"
        "----- BEGIN p1 -----\nalpha\nbeta----- END p1 -----\n"
        "----- BEGIN p3 -----\ngamma\n----- END p3 -----\n"
    )


def test_given_well_id_is_exact():
    p = assemble_prompt("F2", PREP, "x.pdf", well_id="W-1", extraction_date="2026-01-02")
    assert "Pre-matched well_id: W-1   ·   match status: exact" in p


def test_f1_has_one_schema_block_and_f5_its_contract_note():
    f1 = assemble_prompt("F1", PREP, "x.pdf")
    assert "# ===== STAGING SCHEMAS (authoritative CSV column lists) =====\n" in f1
    assert "BASE LOAD SCHEMAS" not in f1
    f5 = assemble_prompt("F5", PREP, "x.pdf")
    assert "# ===== CONTRACT: f5-baker.md =====\nNOTE: §5, §6 and §8" in f5


def test_family_without_a_prompt_is_refused():
    with pytest.raises(ValueError):
        assemble_prompt("F6", PREP, "x.pdf")


def test_shell_helpers():
    assert fenced_body("a\n````\nb\n\n\n````\nc\n````\nd") == "b\n\n\nd"
    assert section("x\n## Schemas\n### t\ny\n## Next\nz\n", "## Schemas", "## ") == "### t\ny"
    assert well_slug("-Ab  C_d (1) é-") == "ab-cd-1"


def test_clarifications_are_on_by_default_and_can_be_turned_off():
    for family in ("F1", "F2", "F3", "F4", "F5", "F7"):
        off = assemble_prompt(family, PREP, "x.pdf", extraction_date="2026-01-02", clarifications=False)
        on = assemble_prompt(family, PREP, "x.pdf", extraction_date="2026-01-02", clarifications=True)
        assert assemble_prompt(family, PREP, "x.pdf", extraction_date="2026-01-02") == on
        assert "CONTRACT CLARIFICATIONS" not in off
        if family in ("F1", "F2", "F4"):  # no approved clarification: no block either way
            assert on == off


def test_clarification_block_sits_after_the_contract_once():
    from extraction.assemble import ASSETS, RUNNERS
    for family in ("F3", "F5", "F7"):
        base = assemble_prompt(family, PREP, "x.pdf", extraction_date="2026-01-02", clarifications=False)
        p = assemble_prompt(family, PREP, "x.pdf", extraction_date="2026-01-02")
        banner = f"# ===== CONTRACT CLARIFICATIONS: {RUNNERS[family]['contract']} =====\n"
        assert p.count("# ===== CONTRACT CLARIFICATIONS:") == 1
        contract = (ASSETS / RUNNERS[family]["contract"]).read_bytes().decode("utf-8")
        assert contract + "\n" + banner in p                                  # right after the contract
        draft = (ASSETS / "amendments" / f"{family.lower()}.md").read_text(encoding="utf-8")
        body = draft.split("\n", 1)[1].lstrip("\n").rstrip("\n")
        assert banner + body + "\n\n# ===== REPORT CONTRACT: m2-validator-spec.md =====\n" in p  # then the validator
        assert p.replace(banner + body + "\n\n", "", 1) == base               # and nothing else changed
