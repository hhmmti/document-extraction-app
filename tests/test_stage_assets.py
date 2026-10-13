"""Stage asset loading: missing directory, missing file, wrong hash. Needs no assets."""

import pytest

from extraction import ExtractionError
from extraction.assemble import INVENTORY, _path
from extraction.stage.stage import ASSETS_ENV, CATALOGUE, SEED, asset


def asset_error(name=SEED) -> ExtractionError:
    with pytest.raises(ExtractionError) as err:
        asset(name)
    assert err.value.code == "ASSET_INVALID"
    return err.value


def test_unset_directory(monkeypatch):
    monkeypatch.delenv(ASSETS_ENV, raising=False)
    assert ASSETS_ENV in asset_error().message


def test_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv(ASSETS_ENV, str(tmp_path))
    assert "is not in" in asset_error().message


def test_hash_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv(ASSETS_ENV, str(tmp_path))
    (tmp_path / SEED).write_text("pump_model_as_printed,vendor_family,proposed_canonical,basis\n")
    err = asset_error()
    assert err.details["manifest_sha256"] and err.details["sha256"] not in err.details["manifest_sha256"]


def test_inventory_and_catalogue_are_hash_checked(monkeypatch, tmp_path):
    monkeypatch.setenv(ASSETS_ENV, str(tmp_path))
    (tmp_path / INVENTORY).write_text("family,field_as_printed\n")
    (tmp_path / CATALOGUE).write_bytes(b"not the catalogue")
    for check in (lambda: _path(INVENTORY), lambda: asset(CATALOGUE)):
        with pytest.raises(ExtractionError) as err:
            check()
        assert err.value.code == "ASSET_INVALID"


def test_the_variable_is_ESP_EXTRACTION_ASSETS_DIR_with_no_alias(monkeypatch, tmp_path):
    assert ASSETS_ENV == "ESP_EXTRACTION_ASSETS_DIR"
    monkeypatch.delenv(ASSETS_ENV, raising=False)
    monkeypatch.setenv("ESP_STAGE_ASSETS_DIR", str(tmp_path))
    assert ASSETS_ENV in asset_error().message

