"""The one catalogue-name lookup, shared by the unknown-model check and the well entry.

    exact      the name is a catalogue name
    case       it equals exactly one catalogue name but for case and outer spaces
    key        its upper-case letters-and-digits key is the key of exactly one catalogue name
    ambiguous  a case or key match names more than one catalogue name; `candidates` lists them
    none       nothing matches

A case or key match is accepted only when it names exactly one catalogue name, and it returns the
catalogue's own spelling.  Staging and D31 do not use this: the grains keep D31's canonical name,
and a case or key match is recorded only where it is used.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

EXACT, CASE, KEY, AMBIGUOUS, NONE = "exact", "case", "key", "ambiguous", "none"


@dataclass(frozen=True)
class Match:
    name: str | None                    # the catalogue's spelling; None when nothing, or more than one name, matched
    how: str                            # exact, case, key, ambiguous or none
    candidates: tuple[str, ...] = ()    # the names an ambiguous match could mean


def key(name) -> str:
    """The upper-case letters-and-digits key: `400 DAL1200` and `400-dal-1200` are both `400DAL1200`."""
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


class CatalogueNames:
    """An index over the catalogue's model names; `lookup` is the shared function."""

    def __init__(self, names):
        self.names = frozenset(n for n in names if n)
        self._case, self._key = defaultdict(set), defaultdict(set)
        for name in self.names:
            self._case[name.strip().casefold()].add(name)
            self._key[key(name)].add(name)

    def lookup(self, name) -> Match:
        if name is None or not str(name).strip():
            return Match(None, NONE)
        name = str(name)
        if name in self.names:
            return Match(name, EXACT)
        for how, hits in ((CASE, self._case.get(name.strip().casefold())), (KEY, self._key.get(key(name)))):
            if hits and len(hits) == 1:
                return Match(next(iter(hits)), how)
            if hits:
                return Match(None, AMBIGUOUS, tuple(sorted(hits)))
        return Match(None, NONE)
