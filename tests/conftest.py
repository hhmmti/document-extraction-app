"""Every test starts in the app's local mode, whatever the shell exports; the S3 tests opt in.  No test reads a well
list from Athena: one that needs a list installs its fake (test_intake.lists).  Every test also starts with the
technical views on (ESP_SHOW_TECHNICAL=1) and the other hosted settings unset: the suites written before the switch
test those views as they were; tests/test_hosted.py turns the switch off and sets the others.  And every test starts
with the write-back off (ESP_WRITEBACK_SECRET unset), whatever the shell exports: the writer's earlier tests pin one
run-log row per confirmation, and a test of the write-back names the secret itself and installs its fakes
(tests/test_write_back.py)."""

import pytest

from app import hosted, wells
from app.store import MODE_ENV
from extraction.write_back import SECRET_ENV


@pytest.fixture(autouse=True)
def local_mode(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)


@pytest.fixture(autouse=True)
def write_back_off(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)


@pytest.fixture(autouse=True)
def hosted_settings(monkeypatch):
    monkeypatch.setenv(hosted.SHOW_ENV, "1")
    for name in (hosted.CONTACT_ENV, hosted.ROLE_ENV, hosted.EXTERNAL_ENV, hosted.USER_HEADER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hosted, "_LOGGED", set())           # nothing logged, and no session built, by an earlier test
    hosted._session.cache_clear()


def no_athena():
    raise AssertionError("a test reached for Athena without a fake")


@pytest.fixture(autouse=True)
def no_aws_well_lists(monkeypatch):
    monkeypatch.setattr(wells, "_LISTS", {})
    monkeypatch.setattr(wells, "athena_client", no_athena)
