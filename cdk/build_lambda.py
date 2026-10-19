"""Stage the Lambda image's build context in build/lambda/.

    python build_lambda.py

Wipes build/lambda/, then copies in the Dockerfile, requirements-lambda.txt, the handler stub and
the whole pipeline package, including the contracts, prompts and pricing it reads, without tests
or caches. The data assets (alias evidence, seed aliases, catalogue snapshot, field inventory) are
not staged: the function reads them from S3, under assets/. CDK builds the image, with Docker,
when the stack is deployed.
"""

import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
OUT = HERE / "build" / "lambda"
# Never staged: tests, caches, and the data assets, should a copy ever sit in the tree.
SKIP = ("tests", "__pycache__", "*.pyc", ".pytest_cache",
        "alias-evidence-v*.json", "m2-d31-seed-aliases.csv", "universal_catalog.parquet", "m1-field-inventory.csv")


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for path in (HERE / "Dockerfile", HERE / "requirements-lambda.txt", LAB / "lambda_function.py"):
        shutil.copy2(path, OUT)
    shutil.copytree(LAB / "extraction", OUT / "extraction", ignore=shutil.ignore_patterns(*SKIP))
    print(f"staged {OUT}: {', '.join(sorted(p.name for p in OUT.iterdir()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
