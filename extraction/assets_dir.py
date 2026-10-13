"""Data assets kept outside the tree, each checked against a MANIFEST.txt hash.

The directory is named by ESP_EXTRACTION_ASSETS_DIR. A manifest line is
`<source SHA-256>  <copy SHA-256>  <file>`; the copy hash is the one checked.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .errors import ExtractionError

ASSETS_ENV = "ESP_EXTRACTION_ASSETS_DIR"


def asset(name: str, manifest: Path) -> Path:
    """The path of one asset, after checking its SHA-256 against `manifest`."""
    root = os.environ.get(ASSETS_ENV)
    if not root:
        raise ExtractionError("ASSET_INVALID", f"{ASSETS_ENV} is not set; it names the asset directory",
                              {"asset": name})
    path = Path(root) / name
    if not path.is_file():
        raise ExtractionError("ASSET_INVALID", f"{name} is not in {ASSETS_ENV}", {"asset": name})
    recorded = [line.split()[1] for line in Path(manifest).read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#") and line.split()[-1] == name]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if recorded != [actual]:
        raise ExtractionError("ASSET_INVALID", f"{name}: SHA-256 {actual} is not the one {Path(manifest).name} records",
                              {"asset": name, "sha256": actual, "manifest_sha256": recorded})
    return path
