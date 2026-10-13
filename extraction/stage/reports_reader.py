"""Read M4 reports into staging rows. Read-only; the reports are the record.

Parsing follows m2-validator-spec.md §8: five `## stg_*` headings, each followed
immediately by a ```jsonl fence, one JSON object per line, absent keys omitted,
every value a verbatim string. Two corpus realities the spec did not anticipate:

  * a block can *restart* its fence mid-stream (the generator re-emitted
    ```jsonl), so a block region runs to the next `## ` heading, not to the
    first closing fence -- reading to the first fence silently truncated
    one F1 corpus report to 8 of 18 rows;
  * a restart can repeat rows already emitted, so byte-identical objects within
    one block are deduplicated. Without this the D17 collapse sees one scenario
    with twice the sections of its siblings and halts on an artefact.

A line that does not parse is never repaired and never guessed at: it is
returned in `truncated` for the caller to quarantine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

BLOCKS = (
    "stg_pump_config",
    "stg_design_context",
    "stg_curve_observations",
    "stg_gas_cascade",
    "stg_alias_evidence",
)


@dataclass
class Report:
    path: Path
    slug: str
    frontmatter: dict
    blocks: dict                      # block name -> list[dict]
    row_counts: dict                  # footer, as declared
    truncated: list = field(default_factory=list)   # (block, raw_line)
    duplicates: dict = field(default_factory=dict)  # block -> n dropped
    family_code: str = ""             # F1..F7, checked by the caller; was the file-name prefix

    @property
    def vendor_family(self) -> str:
        return str(self.frontmatter.get("vendor_family", "")).strip()

    @property
    def source_document(self) -> str:
        return str(self.frontmatter.get("source_document", "")).strip()


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out, current = {}, None
    for line in m.group(1).split("\n"):
        if re.match(r"^\s+-\s", line) and current is not None:
            out[current].append(line.strip()[2:].strip())
            continue
        km = re.match(r"^([A-Za-z_0-9]+):\s*(.*)$", line)
        if not km:
            continue
        key, value = km.group(1), km.group(2).strip()
        if value == "":
            out[key], current = [], key
        else:
            out[key], current = value, None
    return out


def _regions(text: str) -> dict:
    """Heading -> list of line-lists, one per data block bearing that heading.

    A heading only opens a data block when a ```jsonl fence follows immediately;
    the prose `## stg_gas_cascade` narrative section in one F1 corpus report is
    skipped by that test rather than by position.
    """
    lines = text.split("\n")
    heads = [(i, ln[3:].strip()) for i, ln in enumerate(lines) if ln.startswith("## ")]
    out: dict = {}
    for n, (i, name) in enumerate(heads):
        if name not in BLOCKS + ("row_counts",):
            continue
        if i + 1 >= len(lines) or not lines[i + 1].strip().startswith("```jsonl"):
            continue
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        out.setdefault(name, []).append(lines[i + 2:end])
    return out


def read_report(text: str, family_code: str = "") -> Report:
    regions = _regions(text)

    row_counts = {}
    for seg in regions.get("row_counts", []):
        for line in seg:
            s = line.strip()
            if s.startswith("{"):
                try:
                    row_counts = json.loads(s)
                except ValueError:
                    pass

    blocks, truncated, duplicates = {}, [], {}
    for block in BLOCKS:
        rows, seen, dropped = [], set(), 0
        for seg in regions.get(block, []):
            for line in seg:
                s = line.strip()
                if not s or s.startswith("```"):
                    continue
                if not s.startswith("{"):
                    truncated.append((block, s))
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    truncated.append((block, s))
                    continue
                fingerprint = json.dumps(obj, sort_keys=True)
                if fingerprint in seen:
                    dropped += 1
                    continue
                seen.add(fingerprint)
                rows.append(obj)
        blocks[block] = rows
        if dropped:
            duplicates[block] = dropped

    return Report(
        path=None,
        slug="",
        frontmatter=_frontmatter(text),
        blocks=blocks,
        row_counts=row_counts,
        truncated=truncated,
        duplicates=duplicates,
        family_code=family_code,
    )
