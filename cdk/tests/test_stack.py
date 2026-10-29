"""Template tests for the stack. The image context is a temporary folder: no build and no Docker are needed."""

import base64
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from esp_design_extraction.esp_design_extraction_stack import EspDesignExtractionStack

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
VIEW_SQL = Path(__file__).resolve().parents[1] / "esp_design_extraction" / "well_entries_current.sql"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "extraction" / "well_entries_schema.py"
S3 = "arn:aws:s3:::roam-ds-assets/esp-design-extraction/"
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


@pytest.fixture(scope="module")
def schema():
    """extraction/well_entries_schema.py, loaded by path as the stack loads it."""
    spec = importlib.util.spec_from_file_location("well_entries_schema", SCHEMA_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resources_are_exactly_these(template):
    # Ours: two functions, each with its invoke config, role, log group and S3 invoke permission; the run-log table;
    # the notification; the Glue database and its two tables (the table and the view).
    # CDK's notification helper: its function, role and policy.
    assert Counter(r["Type"] for r in template.values()) == Counter({
        "AWS::Lambda::Function": 3, "AWS::IAM::Role": 3, "AWS::IAM::Policy": 1,
        "AWS::Lambda::EventInvokeConfig": 2, "AWS::Lambda::Permission": 2, "AWS::Logs::LogGroup": 2,
        "AWS::DynamoDB::Table": 1, "Custom::S3BucketNotifications": 1, "AWS::Glue::Database": 1,
        "AWS::Glue::Table": 2})


def test_function(template):
    fn_id, fn = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName="/aws/lambda/esp-design-extraction")
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


def test_writer(template):
    fn_id, fn = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction-writer")
    _, extraction = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName="/aws/lambda/esp-design-extraction-writer")
    p = fn["Properties"]
    assert (p["PackageType"], p["Architectures"]) == ("Image", ["x86_64"])
    assert "Handler" not in p and "Runtime" not in p
    assert p["Code"] == extraction["Properties"]["Code"]                 # the same image
    assert p["ImageConfig"] == {"Command": ["lambda_function.confirm_handler"]}
    assert (p["Timeout"], p["MemorySize"], p["ReservedConcurrentExecutions"]) == (120, 2048, 1)
    assert p["LoggingConfig"] == {"LogGroup": {"Ref": log_id}}
    assert p["Environment"]["Variables"] == {"ESP_RUN_LOG_TABLE": "esp_design_extraction_logging"}
    _, invoke = only(template, "AWS::Lambda::EventInvokeConfig", FunctionName={"Ref": fn_id})
    assert invoke["Properties"]["MaximumRetryAttempts"] == 0


def test_image(stack):
    assembly = stack.node.root.synth()
    manifest = json.loads((Path(assembly.directory) / f"{stack.artifact_id}.assets.json").read_text())
    [image] = manifest["dockerImages"].values()                          # one image, for both functions
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


def role_statements(template, function_name):
    """The statements of the function's role: its own inline policy only, no managed or attached policy."""
    _, fn = only(template, "AWS::Lambda::Function", FunctionName=function_name)
    role_id = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    role = template[role_id]["Properties"]
    assert "ManagedPolicyArns" not in role and "PermissionsBoundary" not in role
    assert role["AssumeRolePolicyDocument"]["Statement"] == [
        {"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}}]
    attached = [r for r in template.values() if r["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy")
                and {"Ref": role_id} in r["Properties"].get("Roles", [])]
    assert attached == [], "a policy outside the role's own inline policy is attached to it"
    [policy] = role["Policies"]
    assert policy["PolicyName"] == function_name
    statements = policy["PolicyDocument"]["Statement"]
    for s in map(json.loads, map(canonical, statements)):
        assert not any("*" in action for action in s["Action"]) and "*" not in s["Resource"]
    return statements


def test_role_has_only_these_statements(template):
    """The extraction function's role, unchanged."""
    statements = role_statements(template, "esp-design-extraction")
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName="/aws/lambda/esp-design-extraction")
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


def test_writer_role_has_only_these_statements(template):
    statements = role_statements(template, "esp-design-extraction-writer")
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName="/aws/lambda/esp-design-extraction-writer")
    expected = [
        {"Effect": "Allow", "Action": "s3:GetObject",
         "Resource": [f"{S3}confirmations/*", f"{S3}runs/*", f"{S3}intake/*", f"{S3}assets/*"]},
        {"Effect": "Allow", "Action": "s3:PutObject",
         "Resource": [f"{S3}design-records/*", f"{S3}well-entries/*", f"{S3}confirmations/*/result.json"]},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
         "Resource": {"Fn::GetAtt": [table_id, "Arn"]}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": {"Fn::GetAtt": [log_id, "Arn"]}},
    ]
    assert sorted(map(canonical, statements)) == sorted(map(canonical, expected))
    actions = [a for s in map(json.loads, map(canonical, statements)) for a in s["Action"]]
    assert "s3:ListBucket" not in actions                   # implied by the equality; kept as the stated rules
    assert not any(a.split(":")[0] in ("glue", "athena", "bedrock") for a in actions)


def test_triggers(template):
    """Both configurations through the one notification helper: the intake trigger as it was, and the writer's."""
    fn_id, _ = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    writer_id, _ = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction-writer")
    _, notification = only(template, "Custom::S3BucketNotifications")
    p = notification["Properties"]
    assert (p["BucketName"], p["Managed"]) == ("roam-ds-assets", False)
    assert p["NotificationConfiguration"] == {"LambdaFunctionConfigurations": [{
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": ".pdf"},
                                           {"Name": "prefix", "Value": "esp-design-extraction/intake/"}]}},
        "LambdaFunctionArn": {"Fn::GetAtt": [fn_id, "Arn"]},
    }, {
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": "request.json"},
                                           {"Name": "prefix", "Value": "esp-design-extraction/confirmations/"}]}},
        "LambdaFunctionArn": {"Fn::GetAtt": [writer_id, "Arn"]},
    }]}
    for function_id in (fn_id, writer_id):
        _, permission = only(template, "AWS::Lambda::Permission", FunctionName={"Fn::GetAtt": [function_id, "Arn"]})
        assert permission["Properties"]["Principal"] == "s3.amazonaws.com"
        assert permission["Properties"]["SourceAccount"] == "640168431387"
        assert permission["Properties"]["SourceArn"] == {
            "Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":s3:::roam-ds-assets"]]}


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


@pytest.mark.parametrize("name", ["esp-design-extraction", "esp-design-extraction-writer"])
def test_log_groups(template, name):
    _, group = only(template, "AWS::Logs::LogGroup", LogGroupName=f"/aws/lambda/{name}")
    assert group["Properties"]["RetentionInDays"] == 90
    assert (group["DeletionPolicy"], group["UpdateReplacePolicy"]) == ("RetainExceptOnCreate", "Retain")


def retained(resource):
    return (resource["DeletionPolicy"], resource["UpdateReplacePolicy"]) == ("RetainExceptOnCreate", "Retain")


def glue_table(template, name):
    db_id, _ = only(template, "AWS::Glue::Database")
    found = [r for r in template.values() if r["Type"] == "AWS::Glue::Table" and r["Properties"]["TableInput"]["Name"] == name]
    assert len(found) == 1
    assert (found[0]["Properties"]["CatalogId"], found[0]["Properties"]["DatabaseName"]) == ("640168431387", {"Ref": db_id})
    assert retained(found[0])
    return found[0]["Properties"]["TableInput"]


def test_glue_database(template):
    _, database = only(template, "AWS::Glue::Database")
    assert database["Properties"] == {"CatalogId": "640168431387", "DatabaseInput": {"Name": "esp_design_extraction"}}
    assert retained(database)


def test_glue_table(template, schema):
    table = glue_table(template, "well_entries")
    assert table["TableType"] == "EXTERNAL_TABLE"
    assert table["Parameters"] == {
        "classification": "json", "EXTERNAL": "TRUE", "projection.enabled": "true",
        "projection.environment.type": "enum", "projection.environment.values": "dev,prd",
        "storage.location.template": "s3://roam-ds-assets/esp-design-extraction/well-entries/environment=${environment}/"}
    assert "Fn::" not in json.dumps(table)                  # nothing substitutes ${environment}
    assert table["PartitionKeys"] == [{"Name": "environment", "Type": "string"}] == [
        {"Name": n, "Type": t} for n, t in schema.PARTITION_KEYS]
    assert table["StorageDescriptor"] == {
        "Columns": [{"Name": n, "Type": t} for n, t in schema.COLUMNS],
        "Location": "s3://roam-ds-assets/esp-design-extraction/well-entries/",
        "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
        "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
        "SerdeInfo": {"SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe"}}


def presto(hive):
    """The Presto type of a Hive type, by rewriting its text (the stack parses it instead)."""
    text = re.sub(r"(\w+):", r'"\1" ', hive).replace("struct<", "row(").replace("array<", "array(")
    text = text.replace(">", ")").replace(",", ", ")
    return re.sub(r"\b(string|int)\b", lambda m: {"string": "varchar", "int": "integer"}[m[1]], text)


def test_view_header_decodes_to_the_schemas_columns_and_types(template, schema):
    view = glue_table(template, "well_entries_current")
    assert view["TableType"] == "VIRTUAL_VIEW"
    assert view["Parameters"] == {"presto_view": "true", "comment": "Presto View"}
    assert view["ViewExpandedText"] == "/* Presto View */"
    header = re.fullmatch(r"/\* Presto View: ([A-Za-z0-9+/=]+) \*/", view["ViewOriginalText"])
    assert header
    decoded = json.loads(base64.b64decode(header[1], validate=True))
    columns = schema.COLUMNS + schema.PARTITION_KEYS
    assert decoded == {"originalSql": VIEW_SQL.read_text().strip(), "catalog": "awsdatacatalog",
                       "schema": "esp_design_extraction", "runAsInvoker": False,
                       "columns": [{"name": n, "type": presto(t)} for n, t in columns]}
    assert decoded["columns"][-1] == {"name": "environment", "type": "varchar"}
    assert presto("array<struct<a:int,b:string>>") == 'array(row("a" integer, "b" varchar))'
    assert view["StorageDescriptor"] == {"Columns": [{"Name": n, "Type": t} for n, t in columns]}


def test_view_sql_lists_every_column_and_keeps_each_wells_latest_row(schema):
    sql = " ".join(VIEW_SQL.read_text().split())
    selected = re.match(r"SELECT (.*?) FROM \(", sql)[1]
    assert [c.strip() for c in selected.split(",")] == [n for n, _ in schema.COLUMNS + schema.PARTITION_KEYS]
    assert ("row_number() OVER (PARTITION BY environment, organization_id, well_id "
            "ORDER BY confirmed_at DESC, confirmation_id DESC) AS latest FROM esp_design_extraction.well_entries )"
            " WHERE latest = 1") in sql
    assert ";" not in sql
