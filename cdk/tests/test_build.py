"""build_lambda.py's two image contexts, staged into a temporary folder, and the task's Dockerfile and requirements
read as text. No Docker, no build."""

from pathlib import Path

import pytest

import build_lambda

CDK = Path(__file__).resolve().parents[1]
LAB = CDK.parent
# What the task's context holds besides the pipeline package: the files the image route's task needs, and no others.
TASK_FILES = [
    "Dockerfile", "requirements-lambda.txt", "requirements-task.txt", "lambda_function.py",
    "app/__init__.py", "app/transcription.py",
    *(f"image_route/{name}.py" for name in ("__init__", "converse", "handback", "prompt", "reader", "render",
                                            "settings", "task", "trial")),
    *(f"skills/transcribe-design-scan/{name}" for name in (
        "SKILL.md", "reading-aids.md", "scripts/build_transcription.py", "scripts/render_pages.py",
        "fonts/DejaVuSansMono.ttf", "fonts/LICENSE")),
]


def stage(monkeypatch, out, lab=None) -> dict:
    """Both contexts staged under `out`, from `lab` when one is given: each context's files, as sorted relative
    paths."""
    monkeypatch.setattr(build_lambda, "OUT", out / "lambda")
    monkeypatch.setattr(build_lambda, "TASK_OUT", out / "task")
    if lab:
        monkeypatch.setattr(build_lambda, "LAB", lab)
    assert build_lambda.main() == 0
    return {name: sorted(p.relative_to(out / name).as_posix() for p in (out / name).rglob("*") if p.is_file())
            for name in ("lambda", "task")}


def instructions(path: Path) -> list:
    return [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def test_the_tasks_context_holds_exactly_these(monkeypatch, tmp_path):
    staged = stage(monkeypatch, tmp_path)
    pipeline = [f for f in staged["task"] if f.startswith("extraction/")]
    assert [f for f in staged["task"] if f not in pipeline] == sorted(TASK_FILES)
    # The pipeline package is the Lambda's copy, file for file, with the pricing the reader costs a page from.
    assert pipeline == [f for f in staged["lambda"] if f.startswith("extraction/")]
    assert {"extraction/run.py", "extraction/detect.py", "extraction/compare/pricing.json"} <= set(pipeline)
    assert (tmp_path / "task" / "Dockerfile").read_text() == (CDK / "Dockerfile.task").read_text()
    for context in staged.values():                      # the lab's tree has caches: none came along
        assert not [f for f in context if "__pycache__" in f or f.endswith(".pyc") or "tests/" in f]


def test_tests_caches_and_data_assets_are_never_staged(monkeypatch, tmp_path):
    """From a tree that has them all, beside the app's other modules and a second skill."""
    kept = ["lambda_function.py", "extraction/__init__.py", "extraction/assets/prompt.md", "image_route/task.py",
            "skills/transcribe-design-scan/SKILL.md", "skills/transcribe-design-scan/scripts/render_pages.py",
            "app/__init__.py", "app/transcription.py"]
    left = ["extraction/tests/test_run.py", "extraction/__pycache__/run.cpython-312.pyc", "extraction/run.pyc",
            "extraction/.pytest_cache/README.md", "extraction/assets/alias-evidence-v1.json",
            "extraction/assets/m2-d31-seed-aliases.csv", "extraction/assets/universal_catalog.parquet",
            "extraction/assets/m1-field-inventory.csv", "image_route/tests/test_task.py",
            "image_route/__pycache__/task.cpython-312.pyc", "skills/transcribe-design-scan/tests/test_build.py",
            "skills/transcribe-design-scan/scripts/__pycache__/render_pages.cpython-312.pyc",
            "skills/another-skill/SKILL.md", "app/main.py", "app/static/fonts/MANIFEST.txt", "tests/test_task.py"]
    for name in kept + left:
        (tmp_path / "lab" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "lab" / name).write_text(name)
    staged = stage(monkeypatch, tmp_path / "out", tmp_path / "lab")
    own = ["Dockerfile", "requirements-lambda.txt", "requirements-task.txt"]
    assert staged["task"] == sorted(own + kept)
    assert staged["lambda"] == sorted(["Dockerfile", "requirements-lambda.txt", "lambda_function.py",
                                       "extraction/__init__.py", "extraction/assets/prompt.md"])


def test_the_tasks_dockerfile_is_the_lambdas_with_its_packages_files_and_entrypoint():
    """Line by line: the Lambda's base and steps, the task's requirements file beside the Lambda's, three folders
    more, and the entrypoint where the Lambda's has its handler."""
    base, task = instructions(CDK / "Dockerfile"), instructions(CDK / "Dockerfile.task")
    assert base[0].startswith("FROM ") and base[-1] == 'CMD ["lambda_function.lambda_handler"]'
    expected = [line.replace("COPY requirements-lambda.txt ./", "COPY requirements-lambda.txt requirements-task.txt ./")
                .replace("-r requirements-lambda.txt && rm requirements-lambda.txt",
                         "-r requirements-task.txt && rm requirements-lambda.txt requirements-task.txt")
                for line in base[:-1]]
    expected += [f"COPY {folder}/ ${{LAMBDA_TASK_ROOT}}/{folder}/" for folder in ("image_route", "skills", "app")]
    assert task == expected + ['ENTRYPOINT ["python", "-m", "image_route.task"]']
    assert sum("requirements-task.txt" in line for line in task) == 2      # copied in, then installed and removed


def test_the_tasks_requirements_are_the_lambdas_and_two_more():
    """The Lambda's pins by reference, and the two packages at the versions the lab's tests ran."""
    assert instructions(CDK / "requirements-task.txt") == ["-r requirements-lambda.txt", "pypdf==6.12.1",
                                                            "reportlab==5.1.0"]
    assert {"pypdf==6.12.1", "reportlab==5.1.0"} <= set(instructions(LAB / "requirements.txt"))
