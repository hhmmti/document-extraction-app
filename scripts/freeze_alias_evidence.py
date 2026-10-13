"""Freeze the corpus alias evidence into a versioned asset. No model, no AWS.

    .venv/bin/python scripts/freeze_alias_evidence.py --version 1

Reads the corpus extraction reports in file-name order, learns their
`stg_alias_evidence` exactly as the batch loader did (`corpus_evidence`), and
writes alias-evidence-v<N>.json into $ESP_EXTRACTION_ASSETS_DIR. An existing version is
never overwritten: new evidence is a new version. Prints the SHA-256 for
extraction/stage/MANIFEST.txt.

Format: one JSON object, keys sorted, with `version`, `n_reports` and `flags`,
which maps each printed string (lowercased, as the lookup keys it) to
`composite` or `not_a_model`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from extraction.compare.stage_parity import read_reports  # noqa: E402
from extraction.stage.stage import ASSETS_ENV, corpus_evidence  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--old-reports", type=Path, default=os.environ.get("ESP_OLD_REPORTS_DIR"),
                    help="directory of corpus extraction reports (default: $ESP_OLD_REPORTS_DIR)")
    args = ap.parse_args()
    if not args.old_reports or not Path(args.old_reports).is_dir():
        sys.exit("set --old-reports or ESP_OLD_REPORTS_DIR to the directory of corpus reports")

    if not os.environ.get(ASSETS_ENV):
        sys.exit(f"set {ASSETS_ENV} to the stage asset directory")
    path = Path(os.environ[ASSETS_ENV]) / f"alias-evidence-v{args.version}.json"
    if path.exists():
        sys.exit(f"{path.name} exists; a new corpus is a new version")
    texts = [text for _, text in read_reports(args.old_reports)]
    asset = {"version": args.version, "n_reports": len(texts), "flags": corpus_evidence(texts)}
    data = (json.dumps(asset, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(data)
    print(f"{hashlib.sha256(data).hexdigest()}  {path.name}  ({len(texts)} reports, {len(asset['flags'])} flags)")


if __name__ == "__main__":
    main()
