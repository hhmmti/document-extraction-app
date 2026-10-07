# Where the clarifications would go (proposal, not applied)

The drafts in this folder change no run. Nothing reads them yet, and `assemble_prompt` is unchanged.

**Proposed placement.** Directly after the family contract in the assembled prompt, before the `# ===== REPORT CONTRACT: m2-validator-spec.md =====` banner, as one block:

```
# ===== CONTRACT CLARIFICATIONS: f5-baker.md =====
<the clarification file's body: the quoted contract sentences, the clarification, the example>
```

That keeps each clarification next to the contract text it clarifies, and after it, so it reads as a reading of that text rather than as a new instruction. It mirrors where the F4, F5 and F7 runners already put their contract `NOTE` lines.

**Keeping the byte check pinned.** `assemble_prompt` would take `clarifications: bool = False`. With the default, the output stays byte for byte what the batch runners built, so `pytest -m corpus` keeps comparing against the original asset copies unchanged. Only a run that passes `clarifications=True` (a `--clarifications` flag on `parity_run.py`) gets the block. Its file's SHA-256 joins `asset_sha256` in `run.json`, so every run records which clarification text it saw.

**Tests when applied.** One unit test that the default output is unchanged by the folder's presence, and one that `clarifications=True` inserts exactly this block at exactly this point.

These files are drafts written here, not copies, so they are not listed in `assets/MANIFEST.txt`.
