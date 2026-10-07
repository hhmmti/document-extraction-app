"""Extract, part 1: assemble the extraction prompt for one document.

A port of the per-family batch runners (`f<n>-batch.sh`), which built each
prompt in bash and saved it before sending. This module produces the same
bytes, so that a model comparison measures the model and nothing else. Each
shell effect the runners relied on is reproduced on purpose:

- `$(...)` strips trailing newlines; `printf '%s\\n'` and `echo` add one back.
- `cat` copies a file as-is, so a page whose text lacks a final newline runs
  straight into its END marker.
- sed's replacement text expands `&` to the matched placeholder, so a source
  name containing `&` carries the placeholder text, as the runners' prompts do.

The assets under `assets/` are verbatim copies; see `assets/MANIFEST.txt`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date
from pathlib import Path, PurePosixPath

from .families import MANIFEST_COLUMNS
from .prep import PrepResult

ASSETS = Path(__file__).with_name("assets")
VALIDATOR = "m2-validator-spec.md"
M1SPEC = "m1-extraction-spec.md"
STAGING = "staging-schemas.md"
INVENTORY = "m1-field-inventory.csv"

# Everything that differs between the six runners. `staging` is False only
# for F1, whose runner predates the second schema source and prints a single
# schema block under a different banner.
RUNNERS = {
    "F1": {"prompt": "f1-pass-a.md", "contract": "f1-spyglass.md", "family_pat": "SpyGlass",
           "staging": False, "contract_notes": []},
    "F2": {"prompt": "f2-pass-a.md", "contract": "f2-championx.md", "family_pat": "ChampionX",
           "staging": True, "contract_notes": []},
    "F3": {"prompt": "f3-pass-a.md", "contract": "f3-slb.md", "family_pat": "SLB",
           "staging": True, "contract_notes": []},
    "F4": {"prompt": "f4-pass-a.md", "contract": "f4-els.md", "family_pat": "ELS",
           "staging": True, "contract_notes": [
               "NOTE: §5, §6 and §8 carry correction subsections dated 2026-08-20.",
               "Where the original text and a correction disagree, the CORRECTION wins.",
           ]},
    "F5": {"prompt": "f5-pass-a.md", "contract": "f5-baker.md", "family_pat": "BakerProLift",
           "staging": True, "contract_notes": [
               "NOTE: §5, §6 and §8 carry correction subsections dated 2026-08-20.",
               "Where the original text and a correction disagree, the CORRECTION wins.",
               "In particular §4's stages_basis paragraph is SUPERSEDED by §5's grain",
               "statement: one row, group_total, with the per-housing count carried",
               "separately.",
           ]},
    "F7": {"prompt": "f7-pass-a.md", "contract": "f7-valiant.md", "family_pat": "Valiant",
           "staging": True, "contract_notes": [
               "NOTE: §5, §6, §7 and §8 carry correction subsections dated 2026-08-20.",
               "Where the original text and a correction disagree, the CORRECTION wins.",
           ]},
}

RUNTIME_OVERRIDE = [
    "# ===== RUNTIME OVERRIDE =====",
    "Every input you need is inlined below. Do NOT read, open, list or",
    "search for any file. Do NOT create any file.",
    "IGNORE the file path given under 'Output format' above — you cannot",
    "write files. Emit the report to STDOUT ONLY, starting with the YAML",
    "frontmatter and ending with the row_counts block.",
    "Emit nothing else: no preamble, no commentary, no summary.",
]


def _asset(name: str) -> str:
    # Bytes, then decode: text mode would translate line endings.
    return (ASSETS / name).read_bytes().decode("utf-8")


def asset_files(family: str) -> list[str]:
    r = RUNNERS[family]
    return [r["prompt"], r["contract"], VALIDATOR, M1SPEC, *([STAGING] if r["staging"] else []), INVENTORY]


def clarification_file(family: str) -> Path | None:
    """The family's approved contract clarification (`assets/amendments/f<n>.md`), if it has one."""
    path = ASSETS / "amendments" / f"{family.lower()}.md"
    return path if path.is_file() else None


def clarification_hashes(family: str) -> dict[str, str]:
    """SHA-256 of the clarification a prompt with clarifications on includes; empty where there is none."""
    path = clarification_file(family)
    return {} if path is None else {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}


def clarification_block(family: str) -> str:
    """`# ===== CONTRACT CLARIFICATIONS: <contract> =====` and the clarification file's body
    (everything after its title line), then a blank line; empty where the family has none."""
    path = clarification_file(family)
    if path is None:
        return ""
    text = path.read_bytes().decode("utf-8")
    body = text.split("\n", 1)[1].lstrip("\n") if text.startswith("# ") else text
    return f"# ===== CONTRACT CLARIFICATIONS: {RUNNERS[family]['contract']} =====\n" + body.rstrip("\n") + "\n\n"


def asset_hashes(family: str) -> dict[str, str]:
    """SHA-256 of every asset this family's prompt is built from."""
    return {n: hashlib.sha256((ASSETS / n).read_bytes()).hexdigest() for n in asset_files(family)}


def _records(text: str) -> list[str]:
    """Lines as awk reads them: a final newline ends a record, it does not start one."""
    lines = text.split("\n")
    return lines[:-1] if text.endswith("\n") else lines


def _subst(records: list[str]) -> str:
    """awk's printed records, captured by `$(...)`."""
    return "".join(r + "\n" for r in records).rstrip("\n")


def fenced_body(text: str) -> str:
    """awk '/^````$/{f=!f; next} f' — every line inside four-backtick fences."""
    out, inside = [], False
    for r in _records(text):
        if r == "````":
            inside = not inside
        elif inside:
            out.append(r)
    return _subst(out)


def section(text: str, start: str, stop: str) -> str:
    """awk '/^<start>/{f=1;next} f&&/^<stop>/{exit} f' for literal prefixes."""
    out, inside = [], False
    for r in _records(text):
        if r.startswith(start):
            inside = True
            continue
        if inside and r.startswith(stop):
            break
        if inside:
            out.append(r)
    return _subst(out)


def field_list(family_pat: str) -> str:
    """The runners' inline python: this family's proposed inventory rows, re-written as CSV."""
    buf = io.StringIO()
    out = csv.writer(buf, lineterminator="\n")
    r = csv.reader(io.StringIO(_asset(INVENTORY), newline=""))
    hdr = next(r)
    out.writerow(hdr)
    fi, pi = hdr.index("family"), hdr.index("proposed")
    for row in r:
        if len(row) > max(fi, pi) and family_pat in row[fi] and row[pi].strip().lower() == "yes":
            out.writerow(row)
    return buf.getvalue().rstrip("\n")


def well_slug(stem: str) -> str:
    """tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-' | sed 's/-\\{2,\\}/-/g; s/^-//; s/-$//'"""
    s = "".join(c.lower() if "A" <= c <= "Z" else "-" if c == " " else c for c in stem)
    s = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "", s))
    return re.sub(r"-$", "", re.sub(r"^-", "", s))


def sed_replace(text: str, placeholder: str, replacement: str) -> str:
    """sed "s|<placeholder>|<replacement>|g": `&` is the match, `\\x` is a literal x."""
    out, i = [], 0
    while i < len(replacement):
        c = replacement[i]
        if c == "\\" and i + 1 < len(replacement):
            out.append(replacement[i + 1])
            i += 2
            continue
        out.append(placeholder if c == "&" else c)
        i += 1
    return text.replace(placeholder, "".join(out))


def manifest_csv(rows: list[dict]) -> str:
    """Header plus rows, serialised as the slicing step's csv.DictWriter wrote them."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=MANIFEST_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def assemble_prompt(family: str, prep_result: PrepResult, source_file: str,
                    well_id: str | None = None, extraction_date: str | None = None,
                    clarifications: bool = True) -> str:
    """The full prompt for one document, byte for byte as the family's runner built it.

    `source_file` is the uploaded file's name; any directory part is dropped.
    `well_id=None` renders as `null` with match status `unmatched`, as the
    runners sent it. A given well id is rendered with status `exact`.
    `extraction_date` defaults to today's local date, as `date +%F` gave.
    By default the family's approved contract clarification, if it has one, goes
    after the contract and before the report-contract banner. `clarifications=False`
    leaves the prompt byte for byte as the runners built it.
    """
    if family not in RUNNERS:
        raise ValueError(f"no extraction prompt for family {family!r}")
    r = RUNNERS[family]
    stem = re.sub(r"\.[Pp][Dd][Ff]$", "", PurePosixPath(source_file).name)

    body = fenced_body(_asset(r["prompt"])) + "\n"
    for placeholder, value in [
        ("{{source_document}}", f"{stem}.pdf"),
        ("{{source_stem}}", stem),
        ("{{well_id}}", "null" if well_id is None else str(well_id)),
        ("{{well_id_match_status}}", "unmatched" if well_id is None else "exact"),
        ("{{extraction_date}}", extraction_date or date.today().isoformat()),
        ("{{well_slug}}", well_slug(stem)),
    ]:
        body = sed_replace(body, placeholder, value)

    m1spec = _asset(M1SPEC)
    lines = ["", *RUNTIME_OVERRIDE, "", f"# ===== CONTRACT: {r['contract']} =====", *r["contract_notes"]]
    parts = [body, "\n".join(lines) + "\n", _asset(r["contract"]), "\n",
             clarification_block(family) if clarifications else "",
             f"# ===== REPORT CONTRACT: {VALIDATOR} =====\n", _asset(VALIDATOR), "\n"]
    if r["staging"]:
        parts += [
            "# ===== BASE LOAD SCHEMAS (M1) =====\n",
            "The base column list for each table. Keys are spelled EXACTLY as here.\n",
            section(m1spec, "## Schemas", "## ") + "\n", "\n",
            "# ===== STAGING COLUMNS — these ADD to the base schemas above =====\n",
            "A table's legal keys are the UNION of this block and the one above.\n",
            section(_asset(STAGING), "# Staging schemas", "## Log") + "\n", "\n",
        ]
    else:
        parts += [
            "# ===== STAGING SCHEMAS (authoritative CSV column lists) =====\n",
            section(m1spec, "## Schemas", "## ") + "\n", "\n",
        ]
    parts += [
        "# ===== FIELD LIST (closed — extract nothing outside this) =====\n",
        field_list(r["family_pat"]) + "\n", "\n",
        "# ===== PAGE MANIFEST for this document =====\n",
        manifest_csv(prep_result.manifest), "\n",
        "# ===== PAGE TEXT =====\n",
    ]
    for p in sorted(prep_result.kept_pages):
        parts += [f"----- BEGIN p{p} -----\n", prep_result.page_text[p], f"----- END p{p} -----\n"]
    return "".join(parts)
