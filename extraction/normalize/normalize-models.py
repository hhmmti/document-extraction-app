#!/usr/bin/env python3
"""
normalize-models.py — repair PREP's punctuation damage in model strings.

PREP's layout-mode text extraction drops the opening parenthesis and doubles the
closing one ("SF3550 TS4 XR HS Shaft))"), and drops some hyphens
("GS-334hp" -> "GS334hp"). The extractor transcribed what it was given, which is
D8 working correctly — the defect is upstream of the model, and numbers are
unaffected.

This writes a repaired form into `pump_model_canonical` and NEVER touches
`pump_model_as_printed`. The raw string stays as the audit trail; the canonical
string is what D31 joins on.

Classifications:
  clean        already matches the F1 grammar
  repaired     matched after deterministic punctuation repair
  composite    a p1 "Sizing" token naming several pumps + motor — NOT a model
  not_a_model  a well name or document title in the model field
  unresolved   none of the above; left alone and reported

Run from 21/tapered_pumps/ :
    python3 normalize-models.py m3-pilot/f1-*-pass-a.md            # report only
    python3 normalize-models.py --apply m3-pilot/f1-*-pass-a.md    # write canonical
"""

import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

MODEL_KEYS = ("pump_model_as_printed", "model_as_printed")

# F1 grammar, contract §4.
GRAMMAR = re.compile(
    r"^(SD|SF|SFGH|SFNPSH)\d{2,4}[A-Z]?(\s+TS\d)?(\s+[AX]R)?(\s+\((HS|STD) Shaft\))?$"
)

# A composite "Sizing" token: two or more model stems run together, or a stem
# followed by horsepower/motor text. Never a model — belongs in alias evidence.
COMPOSITE = re.compile(
    r"(SF|SD|SFGH)\d{2,4}.*(SF|SD|SFGH)\d{2,4}"      # two stems
    r"|\d+hp\b|\d+\s*MTR|SLUGGER|HF\s*GS|HFGS",       # motor / package text
    re.I,
)

# A well name or document title that landed in the model field.
NOT_A_MODEL = re.compile(r"^(PERMIAN|Permian)\b|\b\d{3}H\b")


def repair(s: str):
    """Deterministic punctuation repair. Returns (candidate, [notes])."""
    notes, out = [], s.strip()

    if out.startswith("Summit / "):
        out = out[len("Summit / "):]
        notes.append("stripped 'Summit / ' prefix (manufacturer has its own key)")

    m = re.match(r"^(.*?)\s*-\s*\d+\s+stages?$", out, re.I)
    if m:
        out = m.group(1).strip()
        notes.append("stripped '- N stages' suffix (stages has its own key)")

    # 'HS Shaft))' or 'HS Shaft)' -> '(HS Shaft)'
    m = re.search(r"\(?((?:HS|STD)\s+Shaft)\)+$", out)
    if m:
        out = out[: m.start()].rstrip() + f" ({m.group(1)})"
        notes.append("restored parentheses around shaft type")

    out = re.sub(r"\s{2,}", " ", out).strip()
    if out != s.strip() and "  " in s:
        notes.append("collapsed repeated whitespace")

    return out, notes


def classify(raw: str):
    s = raw.strip()
    if GRAMMAR.match(s):
        return "clean", s, []
    if NOT_A_MODEL.search(s) and not re.match(r"^(SD|SF|SFGH|SFNPSH)\d", s):
        return "not_a_model", None, ["well name or document title in the model field"]
    if COMPOSITE.search(s):
        return "composite", None, ["p1 Sizing token — belongs in stg_alias_evidence"]
    cand, notes = repair(s)
    if GRAMMAR.match(cand):
        return "repaired", cand, notes
    return "unresolved", None, notes


def main(argv):
    apply = "--apply" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 1

    counts, examples, touched = Counter(), {}, 0

    for p in sorted(paths):
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        changed, out = False, []

        for line in lines:
            s = line.strip()
            if not (s.startswith("{") and any(k in s for k in MODEL_KEYS)):
                out.append(line)
                continue
            try:
                o = json.loads(s)
            except Exception:
                out.append(line)
                continue

            key = next((k for k in MODEL_KEYS if k in o), None)
            raw = o.get(key)
            if not isinstance(raw, str) or not raw.strip():
                out.append(line)
                continue

            kind, canon, notes = classify(raw)
            counts[kind] += 1
            examples.setdefault(kind, set()).add(raw)

            if kind in ("clean", "repaired") and canon:
                if o.get("pump_model_canonical") != canon:
                    o["pump_model_canonical"] = canon
                    changed = True
            else:
                if o.get("model_normalization_flag") != kind:
                    o["model_normalization_flag"] = kind
                    changed = True

            indent = line[: len(line) - len(line.lstrip())]
            out.append(indent + json.dumps(o, ensure_ascii=False))

        if apply and changed:
            shutil.copy2(p, p.with_suffix(p.suffix + ".prenorm"))
            p.write_text("\n".join(out) + "\n", encoding="utf-8")
            touched += 1

    total = sum(counts.values())
    print(f"model strings seen: {total}   across {len(paths)} reports\n")
    for kind in ("clean", "repaired", "composite", "not_a_model", "unresolved"):
        n = counts.get(kind, 0)
        if not n:
            continue
        print(f"  {kind:<12} {n:>5}   ({len(examples.get(kind, ()))} distinct)")
    print()

    for kind in ("repaired", "composite", "not_a_model", "unresolved"):
        if kind not in examples:
            continue
        print(f"--- {kind} ---")
        for s in sorted(examples[kind])[:12]:
            if kind == "repaired":
                print(f"  {s!r:<45} -> {classify(s)[1]!r}")
            else:
                print(f"  {s!r}")
        if len(examples[kind]) > 12:
            print(f"  ... {len(examples[kind]) - 12} more")
        print()

    if apply:
        print(f"applied to {touched} report(s); .prenorm backups written")
        print("pump_model_as_printed is untouched — canonical written alongside it")
    else:
        print("report only. re-run with --apply to write pump_model_canonical")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
