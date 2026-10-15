"""The generic route: routing, prep, the size cap, alias resolution, validation and review, the forced
flag, the yardstick and the viewer.  Synthetic documents and reports only; no model, no AWS.  Tests that
stage read the assets and skip without ESP_EXTRACTION_ASSETS_DIR."""

import os
from collections import Counter

import pytest

import extraction.run as R
from app.store import STORE_ENV
from extraction import ExtractionError
from extraction.__main__ import main
from extraction.assets_dir import ASSETS_ENV
from extraction.detect import GENERIC, route
from extraction.families import PAGE_MAP
from extraction.prep import prep_generic
from extraction.stage.generic import GenericLookup, canonical_names
from extraction.stage.reports_reader import read_report
from extraction.stage.stage import CATALOGUE, asset, stage
from extraction.validate import spec_validation_status, validate
from test_run import fake_extract
from test_unit import F4_P1, F4_P2, F4_PLOT, make_pdf
from test_validate import report_text
from test_viewer import app, record, texts, write_run
from tools import yardstick as Y

needs_assets = pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"staging reads the assets; set {ASSETS_ENV}")

NEAR = [["SLB Engineer", "General Report", "a design page with no report footer"]]   # one of F3's two markers
TABLE = ["Pump performance table", "flow 100 bpd head 2000 ft"]
PUMP = [{"section_order": "1", "pump_model_as_printed": "ESP-X 100", "stages": "100", "source_page": "p1"}]
CONTEXT = [{"oil_api_deg": "40", "depth_reference_basis": "intake"}]     # this table has no page column


def generic_report(**blocks):
    return report_text("F4", fm={"vendor_family": GENERIC}, **blocks)


# ---- routing and prep ----

def test_routing_branches():
    template = route(make_pdf([F4_P1, F4_P2]))
    assert (template.route, template.detection.family, template.family_hint) == ("template", "F4", None)
    partial = route(make_pdf([F4_P1, F4_PLOT]))
    assert (partial.route, partial.detection, partial.family_hint) == (GENERIC, None, "F4")
    assert list(partial.missing_roles) == [PAGE_MAP["F4"][1][0]]
    unknown = route(make_pdf(NEAR))
    assert (unknown.route, unknown.family_hint, [m["family"] for m in unknown.near_misses]) == (GENERIC, None, ["F3"])
    forced = route(make_pdf([F4_P1, F4_P2]), force_generic=True)
    assert (forced.route, forced.forced, forced.forced_from, forced.family_hint) == (GENERIC, True, "F4", None)
    for pdf, code in ((make_pdf([[], []]), "IMAGE_ONLY"), (b"not a pdf", "PREP_FAILED"),
                      (make_pdf([["SIZING REPORT", "SLB Engineer", "SLB | General Report 1"]]), "AMBIGUOUS_TEMPLATE")):
        with pytest.raises(ExtractionError) as err:
            route(pdf)
        assert err.value.code == code


def test_generic_prep_keeps_pages_with_text_and_drops_thin_ones():
    out = prep_generic(make_pdf([F4_P1, [], TABLE]), source_file="x.pdf")
    rows = {r["original_page_no"]: r for r in out.manifest}
    assert out.kept_pages == [1, 3] and [rows[p]["included"] for p in (1, 2, 3)] == ["yes", "no", "yes"]
    assert (rows[1]["sliced_page_no"], rows[3]["sliced_page_no"]) == (1, 2)
    assert {r["page_role"] for r in out.manifest} == {"unmapped"} and {r["family"] for r in out.manifest} == {GENERIC}
    assert rows[2]["notes"].startswith("dropped: 0 non-whitespace characters")


# ---- alias resolution, validation and review ----

@pytest.fixture
def lookup(tmp_path):
    seed = tmp_path / "seed.csv"
    seed.write_text("pump_model_as_printed,vendor_family,proposed_canonical,basis\n"
                    "ESP-X 100,els,X100,test\nESP-X 100,championx,X100,test\n"
                    "TWO WAYS,els,A1,test\nTWO WAYS,slb,A2,test\nWHOLE STRING,spyglass,__COMPOSITE__,test\n")
    return GenericLookup(seed, {"CAT123"})


def test_generic_alias_resolution_is_exact_only(lookup):
    def got(printed):
        r = lookup.resolve(printed, GENERIC)
        return r.canonical, r.flag
    assert got("esp-x 100") == ("X100", "seed")              # a seed alias every family maps the same way
    assert got(" cat123 ") == ("CAT123", "clean")           # a canonical name, case and outer spaces aside
    assert got("X100") == ("X100", "clean")                 # the seed's canonical names count too
    assert got("TWO WAYS") == (None, "unresolved")          # families disagree: no guess
    assert got("CAT123 TS4") == (None, "unresolved")         # no grammar and no tidying
    assert got("WHOLE STRING") == (None, "composite")
    assert lookup.resolve("ESP-X 100", "els").canonical == "X100"      # a family's lookup is unchanged


def checked(lookup, text, family=GENERIC):
    return validate(read_report(text, family), family, lookup, [], [], [], [], [], report_text=text)


def test_only_the_general_rules_run_on_generic(lookup):
    blocks = dict(pc=[{**PUMP[0], "scenario_ordinal": "2"}], dc=[{"bubble_point_psi": "1800", **CONTEXT[0]}])
    as_f2 = {f["rule_id"] for f in checked(lookup, report_text("F2", **blocks), "F2")["findings"]}
    as_generic = {f["rule_id"] for f in checked(lookup, generic_report(**blocks))["findings"]}
    assert "V-F2-5" in as_f2 and "V-04" in as_f2                       # the same rows fire a family rule as F2
    assert "V-04" in as_generic and not any(r.startswith("V-F") for r in as_generic)


def test_every_generic_value_needs_review_and_the_run_says_why(lookup):
    out = checked(lookup, generic_report(pc=PUMP, dc=CONTEXT))
    assert spec_validation_status(out["findings"]) == "passed" and out["spec_validation_status"] == "needs_review"
    assert out["review"] and all(r["needs_review"] and "generic_path" in r["reasons"] for r in out["review"])
    assert {"kind": "generic_route", "rule_id": "generic", "family": GENERIC, "field": None,
            "message": "Extracted without a vendor template: every value is unverified."} in out["notices"]


@needs_assets
def test_staging_a_generic_report_flags_what_does_not_resolve():
    known = sorted(canonical_names(asset(CATALOGUE)))[0]
    pumps = [{**PUMP[0], "pump_model_as_printed": known}, {**PUMP[0], "section_order": "2", "pump_model_as_printed": "NO SUCH 9"}]
    result = stage(generic_report(pc=pumps, dc=CONTEXT), "org-1", "W", GENERIC, {})
    assert list(result.pump_config["pump_model_canonical"]) == [known, None]
    assert [(u["class"], u["pump_model_as_printed"]) for u in result.unknown_models] == [("unresolved", "NO SUCH 9")]
    with pytest.raises(ExtractionError) as err:                         # the family check expects generic
        stage(report_text("F4", pc=PUMP), "org-1", "W", GENERIC, {})
    assert err.value.code == "FAMILY_MISMATCH"


# ---- runs: the cap, the generic route end to end, the forced flag ----

def capture(text):
    seen = {}

    def extract(prompt, **kw):
        seen["prompt"] = prompt
        return fake_extract(text)(prompt, **kw)
    return extract, seen


@needs_assets
def test_an_oversized_generic_prompt_fails_before_any_model_call(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(make_pdf(NEAR))
    monkeypatch.setattr(R, "GENERIC_PROMPT_CAP", 1000)
    monkeypatch.setattr(R, "extract", lambda *a, **kw: pytest.fail("the model was called"))
    record = R.run(organization_id="org-1", well_id="W", pdf=pdf, store_root=tmp_path / "store")
    assert (record["status"], record["error"]["code"], record["error"]["stage"]) == ("failed", "GENERIC_TOO_LARGE", "assemble")
    assert record["error"]["details"]["cap"] == 1000 < record["error"]["details"]["prompt_bytes"]
    assert (record["route"], record["model"], record["near_misses"][0]["family"]) == (GENERIC, None, "F3")


@needs_assets
def test_a_partial_document_runs_the_generic_route_with_its_hint(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(make_pdf([F4_P1, F4_PLOT]))
    extract, seen = capture(generic_report(pc=PUMP, dc=CONTEXT))
    monkeypatch.setattr(R, "extract", extract)
    record = R.run(organization_id="org-1", well_id="W", pdf=pdf, store_root=tmp_path / "store")
    missing = PAGE_MAP["F4"][1][0]
    assert (record["status"], record["route"], record["family"], record["family_hint"]) == ("succeeded", GENERIC, GENERIC, "F4")
    assert (record["missing_roles"], record["forced"], record["spec_validation_status"]) == ([missing], False, "needs_review")
    assert {"generic-pass-a.md", "generic-contract.md"} <= set(record["hashes"]["contract"])
    assert "# ===== CONTRACT: generic-contract.md =====" in seen["prompt"]
    assert (f"Route context from Detect: page 1 carries the signature of vendor family F4, but its required "
            f"page(s) are missing: {missing}. Context only: this contract governs.") in seen["prompt"]


@needs_assets
def test_the_forced_flag_sends_a_template_document_down_the_generic_route(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(make_pdf([F4_P1, F4_P2]))
    extract, seen = capture(generic_report(pc=PUMP, dc=CONTEXT))
    monkeypatch.setattr(R, "extract", extract)
    record = R.run(organization_id="org-1", well_id="W", pdf=pdf, store_root=tmp_path / "store", force_generic=True)
    assert (record["route"], record["forced"], record["forced_from"], record["family_hint"]) == (GENERIC, True, "F4", None)
    assert record["status"] == "succeeded" and "Route context from Detect: none" in seen["prompt"]
    with pytest.raises(SystemExit) as stop:
        main(["--report", "r.md", "--org", "o", "--no-well", "--force-generic"])
    assert stop.value.code == 2


# ---- the yardstick ----

def pump(case, order, **values):
    return {"scenario_ordinal": case, "section_order": order, **values}


# Case 1 pairs row for row; case 2 is on the template side only; case 3 has two template sections
# against one generic model group.
TEMPLATE_SIDE = report_text("F4", pc=[
    pump("1", "1", pump_model_as_printed="ELS-1234", stages="100", housing_count="2", manufacturer="ELS", source_page="p1"),
    pump("2", "1", pump_model_as_printed="ELS-1234", stages="100"),
    pump("3", "1", pump_model_as_printed="ELS-1234", stages="50"),
    pump("3", "2", pump_model_as_printed="ELS-5678", stages="60"),
], dc=[
    {"scenario_ordinal": "1", "oil_api_deg": "40", "water_sg": "1.05", "gas_sg": "0.7",
     "intake_set_depth_md_ft": "9000 ft", "depth_reference_basis": "intake"},
    {"scenario_ordinal": "3", "oil_api_deg": "40", "depth_reference_basis": "intake"},
])
GENERIC_SIDE = generic_report(pc=[
    pump("1", "1", pump_model_as_printed="ELS-1234", stages="100 stages", source_page="p2"),
    pump("3", "1", pump_model_as_printed="ELS-1234", stages="110"),
], dc=[
    {"scenario_ordinal": "1", "oil_api_deg": "40", "water_sg": "1.10", "pump_setting_md_ft": "9000 ft",
     "depth_reference_basis": "intake"},
    {"scenario_ordinal": "3", "oil_api_deg": "40"},
])


def test_yardstick_classifies_every_dropped_or_conflicting_value():
    shape, values = Y.compare_rows(TEMPLATE_SIDE, GENERIC_SIDE)
    assert shape["stg_pump_config"] == {"rows": (4, 2), "matched": 2, "cases": ("1, 2, 3", "1, 3")}
    assert shape["stg_design_context"] == {"rows": (2, 2), "matched": 2, "cases": ("1, 3", "1, 3")}
    got = Counter((Y.case_name(v.case), v.table[4:], v.field, v.category, v.cause) for v in values if v.category != "same")
    assert got == Counter([
        ("1", "pump_config", "stages", "format", None),                     # same number, unit on one side
        ("1", "pump_config", "housing_count", "dropped", "genuine"),
        ("1", "pump_config", "manufacturer", "dropped", "vocabulary"),      # the generic contract omits it
        ("2", "pump_config", "pump_model_as_printed", "dropped", "scenario"),
        ("2", "pump_config", "section_order", "dropped", "scenario"),
        ("2", "pump_config", "stages", "dropped", "scenario"),
        ("3", "pump_config", "stages", "conflict", "grain"),                # section 1 against the one group
        ("3", "pump_config", "pump_model_as_printed", "dropped", "grain"),  # section 2 has no partner
        ("3", "pump_config", "section_order", "dropped", "grain"),
        ("3", "pump_config", "stages", "dropped", "grain"),
        ("1", "design_context", "water_sg", "conflict", "genuine"),
        ("1", "design_context", "gas_sg", "dropped", "genuine"),
        ("1", "design_context", "intake_set_depth_md_ft", "dropped", "vocabulary"),   # under another key
        ("1", "design_context", "pump_setting_md_ft", "added", None),
        ("3", "design_context", "depth_reference_basis", "dropped", "vocabulary"),    # printed once, in case 1
    ])
    assert Y.agreement([v for v in values if v.matched]) == 8 / 15


def test_yardstick_prints_the_breakdown_and_compares_two_runs_directly(tmp_path, capsys):
    sha = "a" * 64
    for side, route_, text in (("t", "template", TEMPLATE_SIDE), ("g", GENERIC, GENERIC_SIDE)):
        write_run(tmp_path / side, f"20990101T00000{len(side)}Z-0000000{len(side)}",
                  {"route": route_, "family": "F4" if side == "t" else GENERIC, "source": {"pdf": {"sha256": sha}}},
                  {"report.md": text})
    assert Y.main(["--template", str(tmp_path / "t"), "--generic", str(tmp_path / "g")]) == 0
    out = capsys.readouterr().out
    assert "| F4 aaaaaaaaaaaa | pump_config | 4 | 2 | 2 | 1, 2, 3 | 1, 3 |" in out
    assert "| F4 aaaaaaaaaaaa | 53% | 4 | 3 | 3 | 3 | 1 |" in out
    assert "| `design_context.gas_sg` | 1 | 0 | F4 |" in out and "| `design_context.water_sg` | 0 | 1 | F4 |" in out
    run_dirs = [next((tmp_path / side / "runs").iterdir()) for side in ("t", "t")]   # any two runs, any route
    assert Y.main(["--template", str(run_dirs[0]), "--generic", str(run_dirs[1]), "--diff"]) == 0
    assert "| F4 aaaaaaaaaaaa | 100% | 0 | 0 | 0 | 0 | 0 |" in capsys.readouterr().out


# ---- the viewer: the wording, the badge, the route filter ----

GENERIC_RUN, TEMPLATE_RUN = "20990101T000009Z-00000011", "20990101T000008Z-00000012"


def test_the_viewer_marks_a_generic_run(tmp_path, monkeypatch):
    root = tmp_path / "store"
    write_run(root, GENERIC_RUN, record(GENERIC_RUN, family=GENERIC, route=GENERIC, forced=False, family_hint="F3",
                                        missing_roles=["Schematics Report"], near_misses=[],
                                        review={"items": 4, "needs_review": 4, "informational": 0}), {})
    write_run(root, TEMPLATE_RUN, record(TEMPLATE_RUN, route="template",
                                         review={"items": 2, "needs_review": 0, "informational": 0}), {})
    monkeypatch.setenv(STORE_ENV, str(root))
    at = app(GENERIC_RUN)
    md = " ".join(texts(at.markdown))
    assert "Pipeline: completed" in md and "Document review: 4 items to check" in md
    assert any("Generic extraction: review every value" in m.value for m in at.markdown)
    assert "Family hint: F3 (slb). Missing required pages: Schematics Report." in texts(at.caption)
    assert ("Pipeline says whether extraction ran. Document review says whether the values need an engineer's "
            "check. A completed run can still need review.") in texts(at.info)
    template = app(TEMPLATE_RUN)
    assert "Document review: no issues" in " ".join(texts(template.markdown))
    assert not any("Generic extraction" in m.value for m in template.markdown)
    at = app()
    assert dict(zip(at.dataframe[0].value["Run id"], at.dataframe[0].value["Route"])) == {
        GENERIC_RUN: GENERIC, TEMPLATE_RUN: "template"}
    at.multiselect(key="route").set_value([GENERIC]).run()
    assert list(at.dataframe[0].value["Run id"]) == [GENERIC_RUN]
