"""D31 for the generic route: exact matches only.

A family's lookup tries its own seed rows (keyed by vendor family), then its
contract's §4 grammar, then the same two on a tidied string.  A generic report
has no family, so none of that applies.  Here a printed model resolves only to
- a canonical model name, matched exactly (case and outer spaces aside), from the
  catalogue snapshot or the seed's canonical column, or
- a seed row's printed alias, matched the same way, when every family that
  seeds it maps it to one canonical.
Anything else stays unresolved, and the validator lists it as an unknown model.
"""

from __future__ import annotations

from collections import defaultdict

import pyarrow.parquet as pq

from .aliases import COMPOSITE_SENTINEL, AliasLookup, Resolution

GENERIC = "generic"
CANONICAL = "exact canonical name"


def canonical_names(catalogue_path) -> set[str]:
    column = pq.read_table(catalogue_path, columns=["pump_model_canonical"]).column(0).to_pylist()
    return {name for name in column if name}


class GenericLookup(AliasLookup):
    def __init__(self, seed_csv, canonical: set[str]):
        super().__init__(seed_csv)
        aliases = defaultdict(set)
        for (_family, printed), (name, basis) in self.seed.items():
            aliases[printed].add((name, basis))
        exact = {}
        for printed, hits in aliases.items():
            if len({name for name, _ in hits}) == 1:      # one canonical, whichever family seeds it
                name, basis = min(hits)
                exact[printed] = (name, f"seed alias: {basis}")
        for name in canonical | {n for n, _ in self.seed.values() if n != COMPOSITE_SENTINEL}:
            exact.setdefault(name.strip().lower(), (name, CANONICAL))
        # Keyed like a family's seed rows, so V-X3's seed check reads this table too.
        self.seed.update({(GENERIC, printed): hit for printed, hit in exact.items()})

    def _resolve(self, raw: str, family: str) -> Resolution:
        if family != GENERIC:
            return super()._resolve(raw, family)
        marked = self.evidence_flags.get(raw.lower())
        if marked in ("composite", "not_a_model"):
            return Resolution(None, f"stg_alias_evidence: {marked}", marked)
        hit = self.seed.get((GENERIC, raw.lower()))
        if hit is None:
            return Resolution(None, "generic route: no exact canonical-name or seed-alias match", "unresolved")
        name, basis = hit
        if name == COMPOSITE_SENTINEL:
            return Resolution(None, basis, "composite")
        return Resolution(name, basis, "clean" if basis == CANONICAL else "seed")
