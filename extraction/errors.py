"""The one exception a run raises. Its code is what the run record carries."""

from __future__ import annotations

CODES = frozenset({
    "IMAGE_ONLY",              # Detect: no usable text layer
    "UNKNOWN_TEMPLATE",        # Detect: no family signature matched
    "AMBIGUOUS_TEMPLATE",      # Detect: more than one family signature matched
    "REQUIRED_PAGES_MISSING",  # Detect: known family, required page roles absent
    "PREP_FAILED",             # Prep: unreadable PDF, unmapped glyphs, or a kept page with no text
    "MODEL_TIMEOUT",           # Extract: the time budget ran out, or the socket read timed out
    "MODEL_TRUNCATED",         # Extract: stopped at max_tokens, or no row_counts footer
    "MODEL_ERROR",             # Extract: access, throttling, network or service error, or a stream that ended unfinished
    "FAMILY_MISMATCH",         # Stage: the report's vendor_family is not the family Detect assigned
    "ASSET_INVALID",           # Stage: an asset is missing, or its SHA-256 is not the one MANIFEST.txt records
    "REPORT_INVALID",          # Stage: no frontmatter, a stg_* block missing, or no parseable row_counts footer
    "LOAD_HALTED",             # Run: D17-HALT; grains, findings and review are stored, the load is halted
    "STORE_FAILED",            # Run: a run-store write failed; the record names the file
})


class ExtractionError(Exception):
    """A coded failure. `details` holds plain values only, so it serialises as-is."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        if code not in CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}
