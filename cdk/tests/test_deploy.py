"""app.py's environment check, and the deploy workflow read as text (no YAML parser in this venv). No synth, no AWS."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

CDK = Path(__file__).resolve().parents[1]
WORKFLOW = (CDK / "workflows" / "ds-deploy-esp-design-extraction.yaml").read_text()
MESSAGE = "app.py: set AWS_ACCOUNT_NUMBER and AWS_DEFAULT_REGION (the target account and region)"


def block(text, key, indent):
    """The lines under `key:` at this indent, up to the next line indented no deeper."""
    pad = " " * indent
    return re.search(rf"^{pad}{key}:\n((?:{pad} .*\n|\n)*)", text, re.M).group(1)


JOBS = block(WORKFLOW, "jobs", 0)
DEPLOY = block(JOBS, "deploy", 2)


@pytest.mark.parametrize("given", [{}, {"AWS_ACCOUNT_NUMBER": "640168431387"}, {"AWS_DEFAULT_REGION": "us-east-1"}])
def test_app_exits_without_account_and_region(given):
    env = {k: v for k, v in os.environ.items() if k not in ("AWS_ACCOUNT_NUMBER", "AWS_DEFAULT_REGION")}
    run = subprocess.run([sys.executable, "app.py"], cwd=CDK, env=env | given, capture_output=True, text=True)
    assert run.returncode != 0 and MESSAGE in run.stderr.splitlines()


def test_dispatch_only_behind_the_confirmation():
    on = block(WORKFLOW, "on", 0)
    assert re.findall(r"^  (\S+):", on, re.M) == ["workflow_dispatch"]
    assert re.search(r"^      confirm_deploy:\n(?:        .*\n)*        type: boolean\n", on, re.M)
    assert re.search(r"if: \$\{\{ inputs\.confirm_deploy != true \}\}\n\s+run: \|\n(?:\s+.*\n)*?\s+exit 1\n",
                     block(JOBS, "validate", 2))
    assert "    needs: validate\n" in DEPLOY


def test_secrets_mapped_by_name():
    assert re.findall(r"^      (\w+): \$\{\{ secrets\.(\w+) \}\}$", block(DEPLOY, "env", 4), re.M) == [
        ("AWS_ACCESS_KEY_ID", "DS_AWS_ACCESS_KEY_ID"),
        ("AWS_SECRET_ACCESS_KEY", "DS_AWS_SECRET_ACCESS_KEY"),
        ("AWS_DEFAULT_REGION", "DS_AWS_DEFAULT_REGION"),
        ("AWS_ACCOUNT_NUMBER", "DS_ACCOUNT_NUMBER"),
    ]
    assert WORKFLOW.count("secrets.") == 4      # no step interpolates a secret itself


def test_account_and_region_checks_stop_the_job():
    assert "IDENTITY=$(aws sts get-caller-identity)" in DEPLOY
    for check in (r'"\$ACCOUNT" != "640168431387"', r'"\$AWS_DEFAULT_REGION" != "us-east-1"'):
        assert re.search(rf"if \[ {check} \]; then\n\s+echo .*\n\s+exit 1\n", DEPLOY)


def test_cli_pinned_to_the_readme_synth_version():
    pins = re.findall(r"npm install -g aws-cdk(\S*)", WORKFLOW)
    assert len(pins) == 1 and re.fullmatch(r"@\d+\.\d+\.\d+", pins[0])
    assert set(re.findall(r"npx aws-cdk(@\S+) ", (CDK / "README.md").read_text())) == set(pins)


def test_commands_in_order_in_the_cdk_folder_after_the_checks():
    assert "    defaults:\n      run:\n        working-directory: esp_design_extraction/cdk\n" in DEPLOY
    assert WORKFLOW.count("working-directory") == 1 and not re.search(r"^\s+cd ", WORKFLOW, re.M)
    at = {line.strip(): n for n, line in enumerate(DEPLOY.splitlines())}
    checks = [at['if [ "$AWS_DEFAULT_REGION" != "us-east-1" ]; then'], at['if [ "$ACCOUNT" != "640168431387" ]; then']]
    commands = [at[c] for c in ("pip install -r requirements.txt", "python build_lambda.py",
                                "cdk deploy --require-approval never")]
    assert max(checks) < commands[0] and commands == sorted(commands)
    assert "bootstrap" not in WORKFLOW
