"""Read-only access to the run store, behind an interface an S3 backend can implement later.

A run lives in `<root>/runs/<run_id>/`, in the layout `extraction/run.py` writes (see the
README).  Nothing here writes.  Nothing here opens a path recorded in `run.json` or
`source.json`: the source PDF is found by its SHA-256 only, first as
`<root>/intake/<sha256>.pdf`, then by hashing the PDFs under the directories
ESP_SOURCE_PDF_DIRS names (path-separated).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

STORE_ENV = "ESP_RUN_STORE_DIR"
PDF_DIRS_ENV = "ESP_SOURCE_PDF_DIRS"
SHA256 = re.compile(r"[0-9a-f]{64}")


class RunStore(ABC):
    """What the viewer needs from a run store.  A backend implements the three primitives."""

    @abstractmethod
    def run_ids(self) -> list[str]:
        """Every run id in the store, newest first (run ids sort by their UTC start)."""

    @abstractmethod
    def read(self, run_id: str, name: str) -> bytes:
        """One stored file of one run.  FileNotFoundError when the run did not store it."""

    @abstractmethod
    def source_pdf(self, sha256: str) -> bytes | None:
        """The source PDF with this SHA-256, or None when the store cannot find it."""

    def record(self, run_id: str) -> dict:
        """The run record as stored; `run_status` gives the status a reader should see."""
        record = json.loads(self.read(run_id, "run.json"))
        if not isinstance(record, dict):
            raise ValueError("run.json does not hold a JSON object")
        return record

    def launch_ids(self) -> list[str]:
        """Runs this app launched, record or not, newest first.  A backend without launches has none."""
        return []

    def launch_log(self, run_id: str) -> bytes:
        """What a launched process printed.  FileNotFoundError when there is no log."""
        raise FileNotFoundError(run_id)


def _plain(name: str) -> str:
    """A single path component, so a run id or file name from a URL cannot leave the store."""
    if not name or name.startswith(".") or Path(name).name != name:
        raise ValueError(f"not a plain file name: {name!r}")
    return name


_HASHES: dict[tuple[str, int, int], str] = {}


def file_sha256(path: Path) -> str:
    """SHA-256 of a file, cached by path, modification time and size."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _HASHES:
        with open(path, "rb") as f:
            _HASHES[key] = hashlib.file_digest(f, "sha256").hexdigest()
    return _HASHES[key]


class LocalRunStore(RunStore):
    def __init__(self, root: Path, pdf_dirs=()):
        self.root = Path(root)
        self.pdf_dirs = [Path(d) for d in pdf_dirs]

    def run_ids(self) -> list[str]:
        runs = self.root / "runs"
        if not runs.is_dir():
            return []
        return sorted((p.name for p in runs.iterdir() if p.is_dir() and not p.name.startswith(".")), reverse=True)

    def read(self, run_id: str, name: str) -> bytes:
        return (self.root / "runs" / _plain(run_id) / _plain(name)).read_bytes()

    def launch_ids(self) -> list[str]:
        logs = self.root / "launch"
        return sorted((p.stem for p in logs.glob("*.log") if not p.name.startswith(".")), reverse=True) \
            if logs.is_dir() else []

    def launch_log(self, run_id: str) -> bytes:
        return (self.root / "launch" / f"{_plain(run_id)}.log").read_bytes()

    def source_pdf(self, sha256: str) -> bytes | None:
        if not SHA256.fullmatch(str(sha256 or "")):
            return None
        for path in self._candidates(sha256):
            try:
                if path.is_file() and file_sha256(path) == sha256:
                    return path.read_bytes()
            except OSError:          # unreadable candidate: not this one
                continue
        return None

    def _candidates(self, sha256: str):
        yield self.root / "intake" / f"{sha256}.pdf"
        for top in self.pdf_dirs:
            for folder, _, names in sorted(os.walk(top)):
                for name in sorted(names):
                    if name.lower().endswith(".pdf"):
                        yield Path(folder) / name


def local_store_from_env(environ=os.environ) -> LocalRunStore | None:
    """The local store ESP_RUN_STORE_DIR names, or None when it is not set."""
    root = environ.get(STORE_ENV)
    if not root:
        return None
    dirs = [d for d in environ.get(PDF_DIRS_ENV, "").split(os.pathsep) if d]
    return LocalRunStore(Path(root), dirs)


def run_status(record: dict, now: datetime | None = None) -> tuple[str | None, bool]:
    """(status, stale) as a reader should see it: `running` past its `deadline_at` is failed and stale.

    The rule `extraction.run.read_record` applies; restated here because that module imports
    the Bedrock client, and the viewer stays clear of the AWS SDK.
    """
    status, deadline = record.get("status"), record.get("deadline_at")
    now = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if status == "running" and isinstance(deadline, str) and now > deadline:
        return "failed", True
    return status, False
