"""Look for the well list's client ids in the lab tree.  Prints file and line, never the id.

    python tools/check_client_names.py <well list CSV> [changed file ...] [--root <dir>]

Every distinct well id is matched as a fixed string and every organisation id as a whole word,
both case-insensitively.  Ids of 4 characters or fewer, and the organisation `Test Organization`,
are skipped.  .git, .venv, caches and the paths .gitignore names are not searched.  Hits are split
into the changed files named and the rest (pre-existing); the exit status is 1 only when a changed
file has a hit.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from fnmatch import fnmatch
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from app.wells import load  # noqa: E402

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}
SKIP_ORGS = {"Test Organization"}


def ignored(rel: Path, patterns) -> bool:
    # ponytail: the two .gitignore forms this repository uses (`dir/` and a file-name glob);
    # negation and anchored paths are not handled, add them if the file starts using them.
    return any(pattern.rstrip("/") in rel.parts[:-1] if pattern.endswith("/") else fnmatch(rel.name, pattern)
               for pattern in patterns)


def tree(root: Path, skip: Path):
    gitignore = root / ".gitignore"
    patterns = [line.strip() for line in gitignore.read_text().splitlines()
                if line.strip() and not line.startswith("#")] if gitignore.is_file() else []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and path.resolve() != skip and not SKIP_DIRS & set(rel.parts) and not ignored(rel, patterns):
            yield rel


def client_ids(pairs):
    """The ids looked for: the well ids, case-folded, and each organisation id with its whole-word pattern."""
    wells = {well.casefold() for _, well in pairs if len(well) > 4}
    orgs = {org: re.compile(rf"(?<!\w){re.escape(org)}(?!\w)", re.IGNORECASE)
            for org, _ in pairs if len(org) > 4 and org not in SKIP_ORGS}
    return wells, orgs


def names_a_client(line: str, wells, orgs) -> bool:
    folded = line.casefold()
    return any(well in folded for well in wells) or any(org.search(line) for org in orgs.values())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("changed", nargs="*", type=Path, help="files changed this step")
    ap.add_argument("--root", type=Path, default=LAB)
    args = ap.parse_args(argv)
    root = args.root.resolve()
    wells, orgs = client_ids(load(args.snapshot).pairs)
    changed = {path.resolve() for path in args.changed}
    hits = {True: [], False: []}
    for rel in tree(root, args.snapshot.resolve()):
        for n, line in enumerate((root / rel).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if names_a_client(line, wells, orgs):
                hits[(root / rel).resolve() in changed].append((rel, n))
    print(f"ids checked: {len(wells)} well ids, {len(orgs)} organisation ids (longer than 4 characters)")
    for is_changed, label in ((True, "files changed this step"), (False, "pre-existing")):
        found = hits[is_changed]
        print(f"{label}: {len(found)} hits in {len({rel for rel, _ in found})} files")
        for rel, count in sorted(Counter(rel for rel, _ in found).items()):
            print(f"  {count:4d}  {rel}")
        for rel, n in found:
            print(f"        {rel}:{n}")
    return 1 if hits[True] else 0


if __name__ == "__main__":
    raise SystemExit(main())
