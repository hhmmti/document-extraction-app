"""Stage on synthetic reports: the family check, the alias-evidence modes and asset loading.

Needs no corpus. Staging needs the two stage assets: set ESP_EXTRACTION_ASSETS_DIR
(see README); without it those tests skip and only the asset-error tests run.
"""

import json
import os

import pytest

from extraction import ExtractionError
from extraction.stage import corpus_evidence, frozen_evidence, production_evidence, stage
from extraction.assemble import INVENTORY, _path
from extraction.stage.stage import ASSETS_ENV, EVIDENCE, asset
from extraction.validate.validate import catalogue

pytestmark = pytest.mark.skipif(not os.environ.get(ASSETS_ENV),
                                reason=f"staging needs the stage assets; set {ASSETS_ENV}")

PUMP = "ELS-1234"   # resolves through the F4 grammar to ELS1234


def report(vendor_family="els", evidence=()):
    """A minimal complete report: frontmatter, one pump section, optional alias evidence rows,
    the five blocks and the footer (without them the stage refuses the report)."""
    section = {"section_order": "1", "section_role": "pump", "pump_model_as_printed": PUMP,
               "stages": "100", "housing_count": "1", "source_page": "1"}
    lines = ["---", "source_document: test.pdf", f"vendor_family: {vendor_family}",
             "effective_from: 2025-01-02", "---", "",
             "## stg_pump_config", "```jsonl", json.dumps(section), "```", "",
             "## stg_design_context", "```jsonl", json.dumps({"oil_api_deg": "40"}), "```", "",
             "## stg_curve_observations", "```jsonl", "```", "",
             "## stg_gas_cascade", "```jsonl", "```", "",
             "## stg_alias_evidence", "```jsonl", *[json.dumps(e) for e in evidence], "```", "",
             "## row_counts", "```jsonl",
             json.dumps({"stg_pump_config": 1, "stg_design_context": 1, "stg_curve_observations": 0,
                         "stg_gas_cascade": 0, "stg_alias_evidence": len(evidence)}), "```", ""]
    return "\n".join(lines)


COMPOSITE = {"model_as_printed": PUMP, "evidence_kind": "composite_string_token"}
NOT_A_MODEL = {"model_as_printed": PUMP, "model_normalization_flag": "not_a_model"}


# ---- family check ----

def test_family_matching_detect_stages():
    result = stage(report(), "org-1", "WELL 1", "F4", {})
    assert result.pump_config["pump_model_canonical"].tolist() == ["ELS1234"]


def test_family_mismatch_raises_typed_error():
    with pytest.raises(ExtractionError) as err:
        stage(report("els"), None, "WELL 1", "F1", {})
    assert err.value.code == "FAMILY_MISMATCH"
    assert err.value.details == {"vendor_family": "els", "report_family": "F4", "detected_family": "F1"}


def test_missing_or_unknown_vendor_family_is_a_mismatch():
    for vendor_family in ("", "acme"):
        with pytest.raises(ExtractionError) as err:
            stage(report(vendor_family), None, "WELL 1", "F4", {})
        assert err.value.code == "FAMILY_MISMATCH"


# ---- interface ----

def test_organisation_and_well_come_from_the_caller():
    result = stage(report(), "org-1", "WELL 1", "F4", {})
    for frame in (result.pump_config, result.esp_well_design_context):
        assert frame["organization_id"].tolist() == ["org-1"]
        assert frame["well_id"].tolist() == ["WELL 1"]


def test_unmatched_well_gets_reconciliation_and_no_load_rows():
    result = stage(report(), None, None, "F4", {})
    assert result.pump_config.empty and result.esp_well_design_context.empty
    assert result.curve_observations.empty
    assert len(result.log["reconciliation"]) == 5
    assert result.log["d16_log"] == [] and result.log["d17_log"] == []


# ---- alias evidence ----

def test_evidence_decides_resolution():
    clean = stage(report(), None, "WELL 1", "F4", {})
    flagged = stage(report(), None, "WELL 1", "F4", {PUMP.lower(): "composite"})
    assert clean.pump_config["pump_model_canonical"].tolist() == ["ELS1234"]
    assert flagged.pump_config["pump_model_canonical"].tolist() == [None]
    assert flagged.pump_config["pump_model_alias_flag"].tolist() == ["composite"]
    assert [q["reason"] for q in flagged.quarantine] == ["model_normalization_composite"]


def test_corpus_evidence_later_report_wins():
    assert corpus_evidence([report(evidence=[COMPOSITE]), report(evidence=[NOT_A_MODEL])]) \
        == {PUMP.lower(): "not_a_model"}
    assert corpus_evidence([report(evidence=[NOT_A_MODEL]), report(evidence=[COMPOSITE])]) \
        == {PUMP.lower(): "composite"}


def test_production_evidence_is_frozen_plus_own_report_learned_last():
    frozen = {"other": "composite", PUMP.lower(): "composite"}
    assert production_evidence(report(), frozen) == frozen
    assert production_evidence(report(evidence=[NOT_A_MODEL]), frozen) \
        == {"other": "composite", PUMP.lower(): "not_a_model"}
    assert frozen[PUMP.lower()] == "composite"   # the frozen dict is not modified


def test_production_evidence_defaults_to_the_frozen_asset():
    frozen = frozen_evidence()
    assert production_evidence(report(), None) == frozen
    assert frozen and set(frozen.values()) <= {"composite", "not_a_model"}


def test_frozen_asset_passes_its_manifest_check():
    assert json.loads(asset(EVIDENCE).read_text(encoding="utf-8"))["version"] == 1


def test_catalogue_and_inventory_load_through_their_hash_check():
    models = catalogue()
    assert len(models) == 713
    assert sorted(m for m, has_curve in models.items() if not has_curve) == [
        "4001750PK", "400DN1750", "400RC1000", "400VAPRO3500PK", "ELS1750", "ELS3000", "SFNPSH78", "VC4300"]
    assert _path(INVENTORY).name == INVENTORY


def test_stage_result_carries_validation():
    result = stage(report(), "org-1", "WELL 1", "F4", {})
    assert result.spec_validation_status in ("passed", "needs_review", "failed")
    assert {f["rule_id"] for f in result.findings} >= {"V-19"}
    assert result.review and all(r["grounding"] == "not_evaluated" for r in result.review)
    assert {n["rule_id"] for n in result.notices} >= {"review.grounding"}

