"""The container platform's app folder (container/): what build.py ships and what it refuses, and the files it puts
beside the app.  No Docker, no network, no AWS: build.py runs on a small git repository made in a temporary folder,
which holds this repository's own container/ files and .gitignore, and writes into a stand-in for the platform
repository.  Every name, account and id here is a placeholder."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LAB = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("container_build", LAB / "container" / "build.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)

ACCOUNT, ROLE_NAME, EXTERNAL = "0" * 12, "an-invented-role", "an-invented-external-id"
MASTER = {"accountId": ACCOUNT, "crossAccountDataRole": {"roleName": ROLE_NAME, "externalId": EXTERNAL}}
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"
CONTACT, OWNER = 'the "ESP" data team, desk 4\\5 (ext. 12)', "An Owner"       # with what JSON has to escape
APP = "apps/enterprise/esp-design-extraction"
SHIPS = {"app/main.py": "print('the app')\n", "app/static/fonts/a.ttf": "a font", "extraction/__init__.py": "",
         "extraction/assets/contract.md": "a prompt\n", ".streamlit/config.toml": "[server]\n"}
STAYS = {".streamlit/secrets.toml": "secret = 1\n", "tests/test_app.py": "", "cdk/app.py": "", "scripts/run.py": "",
         "tools/check.py": "", "skills/a/SKILL.md": "", "lambda_function.py": "", "README.md": "", "RUNBOOK.md": "",
         "requirements.txt": "", "pytest.ini": ""}
BESIDE = {"Dockerfile", "requirements.txt", ".dockerignore", "app.config.json", "SOURCE.txt"}
FARGATE = {256: (512, 1024, 2048), 512: range(1024, 4097, 1024), 1024: range(2048, 8193, 1024),
           2048: range(4096, 16385, 1024), 4096: range(8192, 30721, 1024)}


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=a test", "-c", "user.email=none",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout


def write(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def tree(folder) -> dict:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in sorted(folder.rglob("*"))
            if path.is_file()}


@pytest.fixture
def source(tmp_path, monkeypatch):
    """A committed repository like this one: the shipped paths, everything that must stay behind, a cache."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)         # the developer's own git settings change nothing
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "source"
    write(repo, {**SHIPS, **STAYS, "app/__pycache__/main.cpython-312.pyc": "a cache"})
    shutil.copytree(LAB / "container", repo / "container", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(LAB / ".gitignore", repo / ".gitignore")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a commit")
    return repo


@pytest.fixture
def target(tmp_path):
    """A stand-in for the platform repository: another app, and the data role in cdk.json."""
    root = tmp_path / "platform"
    write(root, {"apps/enterprise/another-app/keep.txt": "kept", "apps/OWNERS": "owners",
                 "cdk.json": json.dumps({"context": {"master": MASTER}})})
    return root


def note(dest) -> list[str]:
    return (dest / "SOURCE.txt").read_text().splitlines()


def test_only_the_allow_list_is_shipped(source, target):
    write(target, {f"{APP}/left-from-before.txt": "old"})
    before = tree(target)
    dest = B.build(target, CONTACT, OWNER, source=source)
    assert dest == target / APP
    assert set(tree(dest)) == set(SHIPS) | BESIDE                  # no test, cdk, script, tool, skill, handler or doc;
    assert {name: data.decode() for name, data in tree(dest).items() if name in SHIPS} == SHIPS   # no cache, no secret
    after = tree(target)
    assert {name: data for name, data in after.items() if not name.startswith(APP)} == \
        {name: data for name, data in before.items() if not name.startswith(APP)}       # and nothing else touched
    assert note(dest)[:2] == ["source: local", f"commit: {git(source, 'rev-parse', 'HEAD').strip()}"]
    assert note(dest)[3:] == ["shipped paths:", "  app", "  extraction", "  .streamlit/config.toml",
                              "  container/Dockerfile", "  container/requirements.txt", "  container/.dockerignore",
                              "  container/app.config.json"]


@pytest.mark.parametrize("path, text", [
    ("app/main.py", "print('changed')\n"), ("app/new.py", "new = 1\n"), ("extraction/__init__.py", "changed = 1\n"),
    (".streamlit/config.toml", "[theme]\n"), ("container/Dockerfile", "FROM scratch\n")])
def test_an_uncommitted_shipped_path_is_refused_and_a_local_test_is_marked(source, target, path, text):
    write(target, {f"{APP}/from-before.txt": "an earlier build"})
    write(source, {path: text, "app/.DS_Store": "a folder view"})
    with pytest.raises(B.Refused, match=re.escape(path)):
        B.build(target, CONTACT, OWNER, source=source)
    assert tree(target / APP) == {"from-before.txt": b"an earlier build"}          # nothing written
    dest = B.build(target, CONTACT, OWNER, local_test=True, source=source)
    assert B.LOCAL_TEST == "uncommitted: local test only" and B.LOCAL_TEST in note(dest)
    shipped = path.removeprefix("container/")
    assert tree(dest)[shipped] == text.encode() and ".DS_Store" not in " ".join(tree(dest))


def test_a_change_outside_the_shipped_paths_is_not_refused_and_a_commit_is_not_marked(source, target):
    write(source, {"README.md": "changed\n", "tests/test_new.py": "", "container/build.py": "changed = 1\n"})
    git(source, "remote", "add", "origin", "https://name:token@host.invalid/a/repository.git")
    dest = B.build(target, CONTACT, OWNER, source=source)
    assert B.LOCAL_TEST not in note(dest) and len(note(dest)) == 11
    assert note(dest)[0] == "source: https://host.invalid/a/repository.git"        # a credential in the URL is left out
    assert B.LOCAL_TEST in note(B.build(target, CONTACT, OWNER, local_test=True, source=source))   # the flag marks


@pytest.mark.parametrize("template, contact, words", [
    ('{"owner": "{{OWNER}}", "environment": {"ESP_CONTACT": "{{CONTACT}}", "ESP_OTHER": "{{OTHER}}"}}', CONTACT,
     "{{OTHER}}"),
    ('{"owner": "{{OWNER}}", "environment": {"ESP_CONTACT": "{{CONTACT}}", "ESP_OTHER": "{{}}"}}', CONTACT, "{{}}"),
    (None, "  ", "CONTACT")], ids=["a name build.py does not fill", "an empty placeholder", "an empty contact"])
def test_a_placeholder_left_is_refused(source, target, template, contact, words):
    write(target, {f"{APP}/from-before.txt": "an earlier build"})
    if template:
        write(source, {"container/app.config.json": template})
        git(source, "commit", "-q", "-a", "-m", "a template with a placeholder nothing fills")
    with pytest.raises(B.Refused, match=re.escape(words)):
        B.build(target, contact, OWNER, source=source)
    assert tree(target / APP) == {"from-before.txt": b"an earlier build"}


def test_the_role_comes_from_the_targets_cdk_json_and_the_names_from_the_arguments(source, target):
    config = json.loads((B.build(target, CONTACT, OWNER, source=source) / "app.config.json").read_text())
    assert config["owner"] == OWNER and config["environment"]["ESP_CONTACT"] == CONTACT        # exactly as given
    assert config["environment"] == {
        "ESP_APP_MODE": "s3", "ESP_BUCKET": "roam-ds-assets", "ESP_INTAKE_PREFIX": "esp-design-extraction/intake/",
        "ESP_RUNS_PREFIX": "esp-design-extraction/runs/",
        "ESP_ATHENA_OUTPUT_LOCATION": f"s3://esp-athena-results-v2-{ACCOUNT}/", "ESP_ATHENA_WORKGROUP": "primary",
        "ESP_SIGNED_IN_USER_HEADER": "X-Forwarded-User", "ESP_CROSS_ACCOUNT_ROLE_ARN": ROLE_ARN,
        "ESP_CROSS_ACCOUNT_EXTERNAL_ID": EXTERNAL, "ESP_CONTACT": CONTACT}                # the switch is not set
    assert config["iam"] == {"statements": [{"actions": ["sts:AssumeRole"], "resources": [ROLE_ARN]}]}
    assert "{{" not in json.dumps(config)


@pytest.mark.parametrize("cdk", [
    None, "not json", {"context": {"dev": MASTER}}, {"context": {"master": {"accountId": ACCOUNT}}},
    {"context": {"master": {**MASTER, "crossAccountDataRole": {"roleName": ROLE_NAME}}}},
    {"context": {"master": {**MASTER, "crossAccountDataRole": {"roleName": ROLE_NAME, "externalId": ""}}}}],
    ids=["no cdk.json", "not JSON", "no master", "no data role", "no external id", "an empty external id"])
def test_a_cdk_json_without_the_role_is_refused(source, target, cdk):
    (target / "cdk.json").unlink()
    if cdk is not None:
        (target / "cdk.json").write_text(cdk if isinstance(cdk, str) else json.dumps(cdk))
    with pytest.raises(B.Refused):
        B.build(target, CONTACT, OWNER, source=source)
    assert not (target / APP).exists()


def test_the_same_source_gives_the_same_files(source, target, tmp_path):
    first = tree(B.build(target, CONTACT, OWNER, source=source))
    other = tmp_path / "another-platform"
    shutil.copytree(target, other, ignore=shutil.ignore_patterns("esp-design-extraction"))
    second = tree(B.build(other, CONTACT, OWNER, source=source))
    built = [{name: data for name, data in files.items() if name != "SOURCE.txt"} for files in (first, second)]
    assert built[0] == built[1] and set(first) == set(second)
    lines = [[line for line in files["SOURCE.txt"].decode().splitlines() if not line.startswith("built: ")]
             for files in (first, second)]
    assert lines[0] == lines[1] and len(lines[0]) == 10            # all of SOURCE.txt but its time


def test_the_files_beside_the_app():
    """container/'s own files, as the platform needs them; no name, address or account number in any of them."""
    folder = LAB / "container"
    wanted = [line for line in (LAB / "requirements.txt").read_text().splitlines()
              if not line.startswith(("pytest", "reportlab"))]
    assert (folder / "requirements.txt").read_text().splitlines() == wanted and len(wanted) == 9   # the same pins
    template = (folder / "app.config.json").read_text()
    assert set(re.findall(r"\{\{.*?\}\}", template)) == {
        "{{OWNER}}", "{{CONTACT}}", "{{ROLE_ARN}}", "{{EXTERNAL_ID}}", "{{ACCOUNT_ID}}"}
    config = json.loads(template)
    assert (config["name"], config["zone"], config["published"], config["stripPrefix"]) == \
        ("esp-design-extraction", "enterprise", True, False)
    assert B.FOLDER == f"apps/{config['zone']}/{config['name']}" == APP
    assert config["title"] and config["description"] and "ESP_SHOW_TECHNICAL" not in config["environment"]
    assert config["memoryLimitMiB"] in FARGATE[config["cpu"]]                       # a pair Fargate takes
    stack = (LAB / "cdk/esp_design_extraction/esp_design_extraction_stack.py").read_text()
    for name, setting in (("BUCKET", "ESP_BUCKET"), ("INTAKE_PREFIX", "ESP_INTAKE_PREFIX"),
                          ("RUNS_PREFIX", "ESP_RUNS_PREFIX")):                       # as the stack gives the Lambda
        assert f'\n{name} = "{config["environment"][setting]}"\n' in stack and f'"{setting}": {name},' in stack
    dockerfile = (folder / "Dockerfile").read_text()
    command = json.loads(dockerfile[dockerfile.index("CMD ") + 4:].replace("\\\n", ""))
    assert command == ["streamlit", "run", "--server.port", "80", "--server.address", "0.0.0.0",
                       "--server.enableCORS", "false", "--server.enableXsrfProtection", "false",
                       "--server.headless", "true", "--server.fileWatcherType", "none",
                       "--server.baseUrlPath", f"/{config['zone']}/{config['name']}", "app/main.py"]
    assert "FROM public.ecr.aws/docker/library/python:3.12-slim\nWORKDIR /app\n" in dockerfile
    assert dockerfile.index("pip install") < dockerfile.index("COPY app app")       # requirements first
    for path in folder.iterdir():
        if path.is_file():
            assert not re.search(r"\d{12}|[\w.-]+@[\w-]+\.\w+", path.read_text()), path.name


def test_run_local_refuses_without_a_profile(tmp_path):
    script = str(LAB / "container" / "run_local.sh")
    for arguments in ([], [str(tmp_path)], [str(tmp_path), ""]):
        done = subprocess.run(["bash", script, *arguments], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin"})
        assert done.returncode == 2 and "AWS profile" in done.stderr and not done.stdout


def test_run_local_passes_the_settings_less_four_and_the_credentials_by_name_only(tmp_path):
    """With stand-ins for the AWS CLI and Docker, which note how they were called: no credential, no container.
    The settings are the template's own, so one added there is passed on, or the test says it is not."""
    settings = json.loads((LAB / "container" / "app.config.json").read_text())["environment"] | {"ESP_CONTACT": CONTACT}
    left_out = {"ESP_CROSS_ACCOUNT_ROLE_ARN", "ESP_CROSS_ACCOUNT_EXTERNAL_ID", "ESP_SIGNED_IN_USER_HEADER",
                "ESP_ATHENA_OUTPUT_LOCATION"}
    assert left_out < set(settings)
    write(tmp_path, {
        "folder/app.config.json": json.dumps({"environment": settings}),
        "bin/aws": '#!/bin/sh\necho "$@" > "$NOTES/aws"\n'
                   + "".join(f"echo 'export AWS_{name}=an-invented-{name}'\n"
                             for name in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "SESSION_TOKEN")),
        "bin/docker": '#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$NOTES/docker"\n'
                      '[ "$1" = run ] && env | grep "^AWS_" | sort > "$NOTES/environment"\nexit 0\n'})
    for name in ("aws", "docker"):
        (tmp_path / "bin" / name).chmod(0o755)
    done = subprocess.run(
        ["bash", str(LAB / "container" / "run_local.sh"), str(tmp_path / "folder"), "a-profile", "8123"],
        capture_output=True, text=True,
        env={"PATH": f"{tmp_path / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin", "NOTES": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    said = done.stdout.split("http://localhost:8123/enterprise/esp-design-extraction/")[0]      # before it starts
    assert all(words in said for words in ("as the developer, not as the platform", "profile's session name",
                                           "the app's default results location"))
    assert (tmp_path / "aws").read_text().split() == ["configure", "export-credentials", "--profile", "a-profile",
                                                       "--format", "env"]
    docker = (tmp_path / "docker").read_text().splitlines()
    build, run = docker[:docker.index("run")], docker[docker.index("run"):]
    assert build == ["build", "--platform", "linux/amd64", "-t", run[-1], str(tmp_path / "folder")]
    assert run[:6] == ["run", "--rm", "--platform", "linux/amd64", "-p", "127.0.0.1:8123:80"]
    passed = [run[i + 1] for i, word in enumerate(run) if word == "-e"]
    assert passed[:-3] == [f"{name}={value}" for name, value in settings.items() if name not in left_out]  # each whole
    assert [setting.split("=")[0] for setting in passed[:-3]] == [
        "ESP_APP_MODE", "ESP_BUCKET", "ESP_INTAKE_PREFIX", "ESP_RUNS_PREFIX", "ESP_ATHENA_WORKGROUP", "ESP_CONTACT"]
    assert passed[-3:] == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]   # by name: no value written
    assert "an-invented" not in "".join(docker) + done.stdout + done.stderr
    assert (tmp_path / "environment").read_text().splitlines() == [                # Docker has them in its environment
        "AWS_ACCESS_KEY_ID=an-invented-ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY=an-invented-SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN=an-invented-SESSION_TOKEN"]
