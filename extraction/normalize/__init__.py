"""The F1 model-string normaliser (`normalize-models.py`, copied verbatim; hash in
MANIFEST.txt), applied to rows in memory. Its `--apply` mode rewrites report files;
nothing here writes anything."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("normalize_models", Path(__file__).with_name("normalize-models.py"))
normalize_models = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(normalize_models)


def normalize_row(row: dict) -> dict:
    """A copy of `row` with the script's per-line edit applied: `pump_model_canonical`
    for a clean or repaired model string, `model_normalization_flag` otherwise."""
    out = dict(row)
    key = next((k for k in normalize_models.MODEL_KEYS if k in out), None)
    raw = out.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return out
    kind, canon, _notes = normalize_models.classify(raw)
    if kind in ("clean", "repaired") and canon:
        out["pump_model_canonical"] = canon
    else:
        out["model_normalization_flag"] = kind
    return out
