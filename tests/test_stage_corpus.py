"""Stage parity over the corpus, against the batch loader's committed outputs.

Run with `pytest -m corpus`. Needs ESP_OLD_REPORTS_DIR (the corpus extraction
reports), ESP_LOAD_REF_DIR (the batch loader's output directory),
ESP_CORPUS_DIR (the source PDFs, for Detect's family) and ESP_EXTRACTION_ASSETS_DIR
(the stage assets). See README.
"""

import os
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

from extraction.assets_dir import ASSETS_ENV
from extraction.compare import stage_parity as SP
from extraction.stage import corpus_evidence, frozen_evidence, production_evidence

OLD = os.environ.get("ESP_OLD_REPORTS_DIR", "")
REF = os.environ.get("ESP_LOAD_REF_DIR", "")
CORPUS = os.environ.get("ESP_CORPUS_DIR", "")

pytestmark = [
    pytest.mark.corpus,
    pytest.mark.skipif(not (OLD and REF and CORPUS and os.environ.get(ASSETS_ENV)),
                       reason=f"stage corpus tests need ESP_OLD_REPORTS_DIR, ESP_LOAD_REF_DIR, "
                              f"ESP_CORPUS_DIR and {ASSETS_ENV} set; see README"),
]


@cache
def corpus():
    texts = [t for _, t in SP.read_reports(OLD)]
    families = [SP.detected_family(t, CORPUS) for t in texts]
    wells = [SP.matched_well(t) for t in texts]
    return texts, families, wells


@cache
def artifacts(mode):
    texts, families, wells = corpus()
    evidence = corpus_evidence(texts) if mode == "corpus" else production_evidence
    return SP.combine(SP.run(texts, families, wells, evidence))


def test_corpus_mode_reproduces_every_reference_artifact_but_flag_2():
    outcomes = {v.artifact: SP.parity_outcome(v) for v in SP.compare(artifacts("corpus"), REF)}
    assert outcomes == {a: "flag 2 (expected)" if a in SP.FLAG2 else "identical" for a in SP.ARTIFACTS}, outcomes


def test_production_mode_equals_corpus_mode():
    corpus_mode, production_mode = artifacts("corpus"), artifacts("production")
    for name in SP.GRAINS:
        assert corpus_mode[name].equals(production_mode[name]), name
    for name in SP.LOGS:
        assert corpus_mode[name] == production_mode[name], name


def test_frozen_evidence_is_the_corpus_build():
    assert frozen_evidence() == corpus_evidence(corpus()[0])


DIGEST = """
import hashlib, sys
from extraction.compare import stage_parity as SP
from extraction.stage import corpus_evidence
from extraction.stage.reports_reader import read_report
from extraction.stage.stage import FAMILY_LABEL
texts = [t for _, t in SP.read_reports(sys.argv[1])]
families = [FAMILY_LABEL[read_report(t).vendor_family] for t in texts]
art = SP.combine(SP.run(texts, families, [SP.matched_well(t) for t in texts], corpus_evidence(texts)))
h = hashlib.sha256()
for name in SP.ARTIFACTS:
    rows = art[name].to_dict("records") if name in SP.GRAINS else art[name]
    h.update("\\n".join(SP.jsonl_lines(rows)).encode())
print(h.hexdigest())
"""


def test_two_processes_with_different_hash_seeds_agree():
    lab = Path(__file__).resolve().parents[1]
    digests = {
        subprocess.run([sys.executable, "-c", DIGEST, OLD], cwd=lab, check=True, capture_output=True, text=True,
                       env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}).stdout
        for seed in ("1", "2")
    }
    assert len(digests) == 1
