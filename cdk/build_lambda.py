"""Stage the Lambda image's build context in build/lambda/, and the transcription task's in build/task/.

    python build_lambda.py

Wipes build/lambda/, then copies in the Dockerfile, requirements-lambda.txt, the handler stub and
the whole pipeline package, including the contracts, prompts and pricing it reads, without tests
or caches. The data assets (alias evidence, seed aliases, catalogue snapshot, field inventory) are
not staged: the function reads them from S3, under assets/.

Then wipes build/task/ and stages the image route's task the same way: Dockerfile.task as its
Dockerfile, both requirements files, the handler stub and the pipeline package as above, the
image_route package, the transcribe-design-scan skill (its two .md files, scripts/ and fonts/), and
of the app only __init__.py and transcription.py, which the hand-back checks a transcription with.

CDK builds both images, with Docker, when the stack is deployed.
"""

import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
OUT = HERE / "build" / "lambda"
TASK_OUT = HERE / "build" / "task"
# Never staged: tests, caches, and the data assets, should a copy ever sit in the tree.
SKIP = ("tests", "__pycache__", "*.pyc", ".pytest_cache",
        "alias-evidence-v*.json", "m2-d31-seed-aliases.csv", "universal_catalog.parquet", "m1-field-inventory.csv")
TASK_FOLDERS = ("extraction", "image_route", "skills/transcribe-design-scan")
TASK_APP = ("__init__.py", "transcription.py")


def main() -> int:
    skip = shutil.ignore_patterns(*SKIP)
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for path in (HERE / "Dockerfile", HERE / "requirements-lambda.txt", LAB / "lambda_function.py"):
        shutil.copy2(path, OUT)
    shutil.copytree(LAB / "extraction", OUT / "extraction", ignore=skip)
    print(f"staged {OUT}: {', '.join(sorted(p.name for p in OUT.iterdir()))}")

    shutil.rmtree(TASK_OUT, ignore_errors=True)
    (TASK_OUT / "app").mkdir(parents=True)
    shutil.copy2(HERE / "Dockerfile.task", TASK_OUT / "Dockerfile")
    for path in (HERE / "requirements-lambda.txt", HERE / "requirements-task.txt", LAB / "lambda_function.py"):
        shutil.copy2(path, TASK_OUT)
    for folder in TASK_FOLDERS:
        shutil.copytree(LAB / folder, TASK_OUT / folder, ignore=skip)
    for name in TASK_APP:
        shutil.copy2(LAB / "app" / name, TASK_OUT / "app")
    print(f"staged {TASK_OUT}: {', '.join(sorted(p.name for p in TASK_OUT.iterdir()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
