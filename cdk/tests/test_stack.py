"""Template tests for the stack. The image context is a temporary folder: no build and no Docker are needed."""

import json
import re
from collections import Counter
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from esp_design_extraction.esp_design_extraction_stack import EspDesignExtractionStack

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
PROFILE_ARN = "arn:aws:bedrock:us-east-1:640168431387:inference-profile/us.anthropic.claude-opus-5-5"
INVOKE = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    context = tmp_path_factory.mktemp("context")
    (context / "Dockerfile").write_text("FROM scratch\n")
    return EspDesignExtractionStack(cdk.App(), "Test", code_dir=context,
                                    env=cdk.Environment(account="640168431387", region="us-east-1"))


@pytest.fixture(scope="module")
def template(stack):
    return Template.from_stack(stack).to_json()["Resources"]


def only(resources, type_, **match):
    """The logical id and resource of the one resource of this type whose properties match."""
    found = [(k, r) for k, r in resources.items() if r["Type"] == type_
             and all(r["Properties"].get(p) == v for p, v in match.items())]
    assert len(found) == 1, f"{len(found)} {type_} resources match {match}"
    return found[0]


def test_resources_are_exactly_these(template):
    # Ours: function, invoke config, role, log group, table, S3 invoke permission, notification.
    # CDK's notification helper: its function, role and policy.
    assert Counter(r["Type"] for r in template.values()) == Counter({
        "AWS::Lambda::Function": 2, "AWS::IAM::Role": 2, "AWS::IAM::Policy": 1,
        "AWS::Lambda::EventInvokeConfig": 1, "AWS::Lambda::Permission": 1, "AWS::Logs::LogGroup": 1,
        "AWS::DynamoDB::Table": 1, "Custom::S3BucketNotifications": 1})


def test_function(template):
    fn_id, fn = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    log_id, _ = only(template, "AWS::Logs::LogGroup")
    p = fn["Properties"]
    assert (p["PackageType"], p["Architectures"]) == ("Image", ["x86_64"])
    assert "Handler" not in p and "Runtime" not in p
    assert p["ImageConfig"] == {"Command": ["lambda_function.lambda_handler"]}
    assert (p["Timeout"], p["MemorySize"], p["ReservedConcurrentExecutions"]) == (900, 2048, 1)
    assert p["LoggingConfig"] == {"LogGroup": {"Ref": log_id}}
    assert p["Environment"]["Variables"] == {
        "ESP_BUCKET": "roam-ds-assets",
        "ESP_INTAKE_PREFIX": "esp-design-extraction/intake/",
        "ESP_RUNS_PREFIX": "esp-design-extraction/runs/",
        "ESP_ASSETS_PREFIX": "esp-design-extraction/assets/",
        "ESP_RUN_LOG_TABLE": "esp_design_extraction_logging",
        "ESP_INFERENCE_PROFILE_ID": "us.anthropic.claude-opus-5-5",
    }
    _, invoke = only(template, "AWS::Lambda::EventInvokeConfig", FunctionName={"Ref": fn_id})
    assert invoke["Properties"]["MaximumRetryAttempts"] == 0


def test_image(stack):
    assembly = stack.node.root.synth()
    manifest = json.loads((Path(assembly.directory) / f"{stack.artifact_id}.assets.json").read_text())
    [image] = manifest["dockerImages"].values()
    assert image["source"]["platform"] == "linux/amd64"
    # The runtime is the lab's Python, from a dated tag rather than a moving one.
    assert re.search(r"^FROM public\.ecr\.aws/lambda/python:3\.12\.\d{4}\.\d{2}\.\d{2}\.\d{2}-x86_64$",
                     DOCKERFILE.read_text(), re.M)
    assert 'CMD ["lambda_function.lambda_handler"]' in DOCKERFILE.read_text()


def canonical(statement):
    """A statement as a comparable string; single values and one-item lists are the same."""
    s = dict(statement)
    for key in ("Action", "Resource"):
        values = s[key] if isinstance(s[key], list) else [s[key]]
        s[key] = sorted(values, key=json.dumps)
    return json.dumps(s, sort_keys=True)


def test_role_has_only_these_statements(template):
    fn_id, fn = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    role_id = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    role = template[role_id]["Properties"]
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    log_id, _ = only(template, "AWS::Logs::LogGroup")

    assert "ManagedPolicyArns" not in role and "PermissionsBoundary" not in role
    assert role["AssumeRolePolicyDocument"]["Statement"] == [
        {"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}}]
    attached = [r for r in template.values() if r["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy")
                and {"Ref": role_id} in r["Properties"].get("Roles", [])]
    assert attached == [], "a policy outside the role's own inline policy is attached to it"
    statements = [s for policy in role["Policies"] for s in policy["PolicyDocument"]["Statement"]]

    expected = [
        {"Effect": "Allow", "Action": "s3:GetObject",
         "Resource": ["arn:aws:s3:::roam-ds-assets/esp-design-extraction/intake/*",
                      "arn:aws:s3:::roam-ds-assets/esp-design-extraction/assets/*"]},
        {"Effect": "Allow", "Action": "s3:PutObject",
         "Resource": "arn:aws:s3:::roam-ds-assets/esp-design-extraction/runs/*"},
        {"Effect": "Allow", "Action": INVOKE, "Resource": PROFILE_ARN},
        {"Effect": "Allow", "Action": INVOKE,
         "Resource": [f"arn:aws:bedrock:{r}::foundation-model/anthropic.claude-opus-5-5"
                      for r in ("us-east-1", "us-east-2", "us-west-2")],
         "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": PROFILE_ARN}}},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
         "Resource": {"Fn::GetAtt": [table_id, "Arn"]}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": {"Fn::GetAtt": [log_id, "Arn"]}},
    ]
    assert sorted(map(canonical, statements)) == sorted(map(canonical, expected))
    for s in map(json.loads, map(canonical, statements)):    # implied by the equality; kept as the stated rule
        assert not any("*" in action for action in s["Action"]) and "*" not in s["Resource"]


def test_trigger(template):
    fn_id, _ = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    _, notification = only(template, "Custom::S3BucketNotifications")
    p = notification["Properties"]
    assert (p["BucketName"], p["Managed"]) == ("roam-ds-assets", False)
    assert p["NotificationConfiguration"] == {"LambdaFunctionConfigurations": [{
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": ".pdf"},
                                           {"Name": "prefix", "Value": "esp-design-extraction/intake/"}]}},
        "LambdaFunctionArn": {"Fn::GetAtt": [fn_id, "Arn"]},
    }]}
    _, permission = only(template, "AWS::Lambda::Permission")
    assert permission["Properties"]["Principal"] == "s3.amazonaws.com"
    assert permission["Properties"]["SourceAccount"] == "640168431387"


def test_table(template):
    """esp_logging_v2's layout, under its own name: the keys, the four indexes, all projecting every attribute."""
    _, table = only(template, "AWS::DynamoDB::Table", TableName="esp_design_extraction_logging")
    p = table["Properties"]
    assert p["KeySchema"] == [{"AttributeName": "organization_id", "KeyType": "HASH"},
                              {"AttributeName": "timestamp", "KeyType": "RANGE"}]
    assert sorted((a["AttributeName"], a["AttributeType"]) for a in p["AttributeDefinitions"]) == sorted(
        (name, "S") for name in ("organization_id", "timestamp", "uuid", "pipeline_execution_id",
                                 "organization_id_type", "status"))
    assert p["GlobalSecondaryIndexes"] == [
        {"IndexName": "uuid", "Projection": {"ProjectionType": "ALL"},
         "KeySchema": [{"AttributeName": "uuid", "KeyType": "HASH"}]},
        *({"IndexName": f"{name}_timestamp", "Projection": {"ProjectionType": "ALL"},
           "KeySchema": [{"AttributeName": name, "KeyType": "HASH"}, {"AttributeName": "timestamp", "KeyType": "RANGE"}]}
          for name in ("pipeline_execution_id", "organization_id_type", "status")),
    ]
    assert p["BillingMode"] == "PAY_PER_REQUEST"
    assert p["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert "TimeToLiveSpecification" not in p and "StreamSpecification" not in p
    assert (table["DeletionPolicy"], table["UpdateReplacePolicy"]) == ("RetainExceptOnCreate", "Retain")


def test_log_group(template):
    _, group = only(template, "AWS::Logs::LogGroup", LogGroupName="/aws/lambda/esp-design-extraction")
    assert group["Properties"]["RetentionInDays"] == 90
    assert (group["DeletionPolicy"], group["UpdateReplacePolicy"]) == ("RetainExceptOnCreate", "Retain")
