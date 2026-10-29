"""Read-only access to the run store, behind one interface with two backends.  Nothing here writes.

`LocalRunStore` reads a directory: a run lives in `<root>/runs/<run_id>/`, in the layout
`extraction/run.py` writes (see the README).  It opens no path recorded in `run.json` or
`source.json`: the source PDF is found by its SHA-256 only, first as `<root>/intake/<sha256>.pdf`,
then by hashing the PDFs under the directories ESP_SOURCE_PDF_DIRS names (path-separated).

`S3RunStore` reads the bucket the Lambda writes, in the same layout: runs under ESP_RUNS_PREFIX, and
uploads under ESP_INTAKE_PREFIX, each naming a run that may have no record yet.  Whether a file
exists comes from the listing, never from a failed read.  The source PDF is read from the location
`source.json` records, only under the intake prefix, and only when its SHA-256 matches.

Both also read confirmed entries, in extraction/confirmation.py's layout (their store root, or the bucket's
esp-design-extraction/): `current_entry`, `current_entries`, `latest_version` and `result`.  Rows and versions
never change, so each is read once and kept; listings and results are read each time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from extraction import confirmation as C
from extraction.errors import ExtractionError
from extraction.intake import RUN_ID, decode_labels, intake_key, intake_run_id

STORE_ENV = "ESP_RUN_STORE_DIR"
PDF_DIRS_ENV = "ESP_SOURCE_PDF_DIRS"
MODE_ENV = "ESP_APP_MODE"                                       # local (the default) or s3
S3_ENV = ("ESP_BUCKET", "ESP_INTAKE_PREFIX", "ESP_RUNS_PREFIX")  # the Lambda's own variables
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
    def source_pdf(self, sha256: str, location: str | None = None) -> bytes | None:
        """The source PDF with this SHA-256, or None when the store cannot find it.  `location` is
        where the run recorded it; a backend may ignore it."""

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

    def uploaded_at(self, run_id: str) -> datetime | None:
        """When the upload naming this run arrived.  None for a backend without uploads."""
        return None

    def intake_labels(self, run_id: str) -> dict | None:
        """The labels this run was uploaded with.  None for a backend without uploads, or labels it cannot read."""
        return None

    def refresh(self, run_id: str | None = None) -> None:
        """Forget cached reads, of one run or of everything.  A backend that caches nothing has nothing to do."""

    # ---- confirmed entries: `objects` is the store as extraction/confirmation.py's objects ----

    objects = None

    def _kept(self, key: str) -> dict:
        """A row or a version, read once: neither ever changes."""
        kept = self.__dict__.setdefault("_kept_reads", {})
        if key not in kept:
            kept[key] = C.load(self.objects.get(key))
        return kept[key]

    def current_entries(self, environment: str) -> dict:
        """{(organization_id, well_id): the current row} in this environment: each well's latest confirmed row."""
        if environment not in C.ENVIRONMENTS:
            raise ValueError(f"not an environment: {environment!r}")
        current = {}
        for key in self.objects.keys(C.row_prefix(environment)):
            row = self._kept(key)
            well = (row["organization_id"], row["well_id"])
            if well not in current or C.latest(row) > C.latest(current[well]):
                current[well] = row
        return current

    def current_entry(self, environment: str, organization_id: str, well_id: str) -> dict | None:
        """The well's current row, its ids exactly as given; None when it has none."""
        return self.current_entries(environment).get((organization_id, well_id))

    def latest_version(self, document_sha256: str, organization_id: str, well_id: str) -> dict | None:
        """The document's latest version for this well whose row exists: a version without one never committed."""
        if not SHA256.fullmatch(str(document_sha256 or "")):
            return None
        versions = [self._kept(key) for key in self.objects.keys(f"{C.record_prefix(document_sha256)}versions/")]
        rows = {}
        for version in sorted(versions, key=C.latest, reverse=True):
            if (version["labels"]["organization_id"], version["labels"]["well_id"]) != (organization_id, well_id):
                continue
            prefix = C.row_prefix(version["environment"])
            if prefix not in rows:
                rows[prefix] = set(self.objects.keys(prefix))
            if version["row_key"] in rows[prefix]:
                return version
        return None

    def result(self, confirmation_id: str) -> dict | None:
        """A confirmation's result, a running one past its deadline_at read as stale; None while there is none."""
        return C.read_result(self.objects, confirmation_id) if RUN_ID.fullmatch(str(confirmation_id or "")) else None


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
        self.objects = C.LocalObjects(self.root)

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

    def source_pdf(self, sha256: str, location: str | None = None) -> bytes | None:
        if not SHA256.fullmatch(str(sha256 or "")):            # `location` is never opened
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


FINAL = ("succeeded", "failed")


class S3RunStore(RunStore):
    """The bucket the Lambda writes.  Reads are cached until `refresh`, so a Streamlit rerun lists and
    reads nothing again; a final run's files never change, so a refresh keeps them."""

    def __init__(self, client, bucket: str, intake_prefix: str, runs_prefix: str):
        self.client, self.bucket = client, bucket
        self.intake_prefix, self.runs_prefix = intake_prefix, runs_prefix
        self.objects = C.S3Objects(client, bucket)
        self._listing = None        # (listed at, {run id: its file names}, {run id: upload time})
        self._reads = {}            # (run id, file name) -> bytes
        self._labels = {}           # run id -> the upload's labels, or None
        self._pdfs = {}             # (location, sha256) -> bytes, or None when the content has another hash

    def _objects(self, prefix: str):
        """Every object under `prefix`, a page at a time."""
        kwargs = {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            page = self.client.list_objects_v2(**kwargs)
            yield from page.get("Contents", [])
            if not page.get("IsTruncated"):
                return
            kwargs["ContinuationToken"] = page["NextContinuationToken"]

    def _runs(self, prefix: str) -> dict:
        """{run id: file names} under `prefix`: the runs prefix, or one run's."""
        runs = {}
        for obj in self._objects(prefix):
            run_id, _, name = obj["Key"][len(self.runs_prefix):].partition("/")
            if run_id and not run_id.startswith(".") and name and "/" not in name:
                runs.setdefault(run_id, set()).add(name)
        return runs

    def _list(self):
        # ponytail: lists every run's files and reads each record once; a run index is the upgrade if the
        # bucket ever holds thousands of runs.
        if self._listing is None:
            uploads = {}
            for obj in self._objects(self.intake_prefix):
                run_id = intake_run_id(self.intake_prefix, obj["Key"])
                if run_id:
                    uploads[run_id] = obj["LastModified"]
            self._listing = (datetime.now(timezone.utc), self._runs(self.runs_prefix), uploads)
        return self._listing

    @property
    def listed_at(self) -> datetime | None:
        """When the cached listing was made; None before the first."""
        return self._listing[0] if self._listing else None

    def run_ids(self) -> list[str]:
        return sorted(self._list()[1], reverse=True)

    def launch_ids(self) -> list[str]:
        """Every upload, with a record or not: the runs started through the bucket."""
        return sorted(self._list()[2], reverse=True)

    def uploaded_at(self, run_id: str) -> datetime | None:
        return self._list()[2].get(run_id)

    def read(self, run_id: str, name: str) -> bytes:
        key = (_plain(run_id), _plain(name))
        if key not in self._reads:
            if name not in self._list()[1].get(run_id, ()):
                raise FileNotFoundError(f"{run_id}/{name}")
            body = self.client.get_object(Bucket=self.bucket, Key=f"{self.runs_prefix}{run_id}/{name}")["Body"]
            self._reads[key] = body.read()
        return self._reads[key]

    def intake_labels(self, run_id: str) -> dict | None:
        if run_id not in self._labels:
            self._labels[run_id] = None
            if run_id in self._list()[2]:
                try:
                    head = self.client.head_object(Bucket=self.bucket, Key=intake_key(self.intake_prefix, run_id))
                    self._labels[run_id] = decode_labels(head.get("Metadata", {}))
                except Exception:  # noqa: BLE001 - unreadable or refused labels: nothing to show or compare
                    pass
        return self._labels[run_id]

    def source_pdf(self, sha256: str, location: str | None = None) -> bytes | None:
        """The upload `location` names, when it is under the intake prefix and its content has this SHA-256."""
        root = f"s3://{self.bucket}/"
        key = location[len(root):] if isinstance(location, str) and location.startswith(root) else ""
        if not SHA256.fullmatch(str(sha256 or "")) or intake_run_id(self.intake_prefix, key) not in self._list()[2]:
            return None
        if (location, sha256) not in self._pdfs:
            data = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            self._pdfs[location, sha256] = data if hashlib.sha256(data).hexdigest() == sha256 else None
        return self._pdfs[location, sha256]

    def refresh(self, run_id: str | None = None) -> None:
        """Forget what may have changed: the listings, and every read of a run that is not final.  With
        `run_id`, only that run is listed and read again."""
        if run_id is None:
            final = {r for r, name in self._reads if name == "run.json" and self._final(r)}
            self._listing = None
            self._labels.clear()
            self._reads = {k: v for k, v in self._reads.items() if k[0] in final}
        elif self._listing is not None and not self._final(run_id):
            files = self._runs(f"{self.runs_prefix}{_plain(run_id)}/").get(run_id)
            self._listing[1].pop(run_id, None)
            if files:
                self._listing[1][run_id] = files
            self._reads = {k: v for k, v in self._reads.items() if k[0] != run_id}
            self._labels.pop(run_id, None)

    def _final(self, run_id: str) -> bool:
        try:
            return json.loads(self._reads[run_id, "run.json"])["status"] in FINAL
        except Exception:  # noqa: BLE001 - not read, or not a record: not known to be final
            return False


def s3_client():
    """An S3 client from the standard AWS environment: AWS_PROFILE, or the other usual sources."""
    import boto3                     # S3 mode only: local mode never loads the AWS SDK
    return boto3.client("s3")


_S3_STORES: dict[tuple, S3RunStore] = {}


def s3_store_from_env(environ=os.environ) -> S3RunStore | None:
    """The S3 store the Lambda's variables name, one per process, so its cache outlives a Streamlit rerun.
    None when one is not set, or a prefix does not end in `/`."""
    bucket, intake_prefix, runs_prefix = (environ.get(name, "") for name in S3_ENV)
    if not (bucket and intake_prefix.endswith("/") and runs_prefix.endswith("/")):
        return None
    key = (bucket, intake_prefix, runs_prefix)
    if key not in _S3_STORES:
        _S3_STORES[key] = S3RunStore(s3_client(), *key)
    return _S3_STORES[key]


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
