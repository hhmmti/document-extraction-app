"""Every test starts in the app's local mode, whatever the shell exports; the S3 tests opt in."""

import pytest

from app.store import MODE_ENV


@pytest.fixture(autouse=True)
def local_mode(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)
