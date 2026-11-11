"""Every test starts in the app's local mode, whatever the shell exports; the S3 tests opt in.  No test reads a well
list from Athena: one that needs a list installs its fake (test_intake.lists)."""

import pytest

from app import wells
from app.store import MODE_ENV


@pytest.fixture(autouse=True)
def local_mode(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)


def no_athena():
    raise AssertionError("a test reached for Athena without a fake")


@pytest.fixture(autouse=True)
def no_aws_well_lists(monkeypatch):
    monkeypatch.setattr(wells, "_LISTS", {})
    monkeypatch.setattr(wells, "athena_client", no_athena)
