"""The shared catalogue-name lookup: exact, then case, then the letters-and-digits key, a case or key match
taken only when it names exactly one catalogue name; and the unknown-model check that uses it.  The
pinned-catalogue cases read the catalogue snapshot and skip without ESP_EXTRACTION_ASSETS_DIR."""

import os

import pytest

from extraction.assets_dir import ASSETS_ENV
from extraction.catalogue import CatalogueNames, Match, key
from extraction.validate.validate import catalogue, unknown_models

needs_assets = pytest.mark.skipif(not os.environ.get(ASSETS_ENV), reason=f"the catalogue is an asset; set {ASSETS_ENV}")

# The seven keys the pinned catalogue spells two ways, all Alnas.
ALNAS = {"L800": ("L 800", "L 800*"), "S125": ("S-125", "S125*"), "S18": ("S-18", "S18*"), "S200": ("S-200", "S200*"),
         "S30": ("S-30", "S30*"), "S60": ("S-60", "S60*"), "S80": ("S-80", "S80*")}


def test_each_step_in_order():
    names = CatalogueNames(["400 DAL1200", "SF1750", "Ab-1", "AB1*", "Q-9", "q-9"])
    assert names.lookup("SF1750") == Match("SF1750", "exact")
    assert names.lookup(" sf1750 ") == Match("SF1750", "case")
    assert names.lookup("400DAL1200") == names.lookup("400-dal-1200") == Match("400 DAL1200", "key")
    assert names.lookup("q-9") == Match("q-9", "exact")                          # exact wins before case
    assert names.lookup("Q-9 ") == Match(None, "ambiguous", ("Q-9", "q-9"))       # two names but for case
    assert names.lookup("ab1") == Match(None, "ambiguous", ("AB1*", "Ab-1"))      # two names on one key
    for nothing in ("NO SUCH 9", "---", " ", None):
        assert names.lookup(nothing) == Match(None, "none")
    assert key("400 dal-1200") == "400DAL1200"


@needs_assets
def test_the_pinned_catalogue():
    names = CatalogueNames(catalogue())
    assert names.lookup("400DAL1200") == Match("400 DAL1200", "key")
    for printed, candidates in ALNAS.items():
        assert names.lookup(printed) == Match(None, "ambiguous", candidates), printed
        assert names.lookup(candidates[0]) == Match(candidates[0], "exact")


def test_the_unknown_model_check_uses_the_lookup():
    models = {"400 DAL1200": True, "S-18": True, "S18*": False, "VC4300": False}
    rows = [{"pump_model_canonical": "400DAL1200", "pump_model_as_printed": "400-DAL-1200"},   # key: a catalogue name
            {"pump_model_canonical": None, "pump_model_as_printed": "400 dal1200"},          # D31 gave none; case
            {"pump_model_canonical": "S18", "pump_model_as_printed": "S 18"},                # two names on one key
            {"pump_model_canonical": "vc4300", "pump_model_as_printed": "VC4300"},           # case, no head curve
            {"pump_model_canonical": "XYZ1", "pump_model_as_printed": "x"},
            {"pump_model_canonical": None, "pump_model_as_printed": "NO SUCH 9"}]
    assert unknown_models(rows, models) == [
        {"model": "S18", "class": "ambiguous", "pump_model_as_printed": "S 18", "candidates": ["S-18", "S18*"]},
        {"model": "VC4300", "class": "no_head_curve", "pump_model_as_printed": "VC4300"},
        {"model": "XYZ1", "class": "absent", "pump_model_as_printed": "x"},
        {"model": "NO SUCH 9", "class": "unresolved", "pump_model_as_printed": "NO SUCH 9"}]
