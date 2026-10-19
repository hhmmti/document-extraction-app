"""Keeps the lab's own pytest run (from the folder above) out of this CDK app.

Run these tests from this folder; pytest.ini here makes it the rootdir. Any other rootdir collects
nothing here: tests/ needs the CDK venv, and cdk.out/ holds a copy of the Lambda package, third-party
test suites included. `__init__.py` makes this folder a package, so importing this file does not put
the folder, and its app.py, ahead of the lab's `app` package on sys.path.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent


def pytest_ignore_collect(collection_path, config):
    return True if config.rootpath != HERE else None
