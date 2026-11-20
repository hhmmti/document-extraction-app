"""Template tests for the stack. The two image contexts are temporary folders: no build and no Docker are needed."""

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
PUSH = "esp-design-extraction-product-uploads-640168431387"
PROFILE_ARN = "arn:aws:bedrock:us-east-1:640168431387:inference-profile/us.anthropic.claude-opus-5-5"
INVOKE = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
MODELS = [f"arn:aws:bedrock:{r}::foundation-model/anthropic.claude-opus-5-5"
          for r in ("us-east-1", "us-east-2", "us-west-2")]
ROUTE = "esp-design-extraction-transcription"            # the image route: its cluster, its task's family, its names
ROUTE_ENV = {"ESP_BUCKET": "roam-ds-assets", "ESP_TRANSCRIPTIONS_PREFIX": "esp-design-extraction/transcriptions/",
             "ESP_RUN_LOG_TABLE": "esp_design_extraction_logging"}
# The stack's one statement on "*": AWS grants this action on nothing narrower.
GET_TOKEN = {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    contexts = {}
    for name in ("lambda", "task"):                      # two contexts that differ, so two images
        contexts[name] = tmp_path_factory.mktemp(name)
        (contexts[name] / "Dockerfile").write_text(f"FROM scratch\n# {name}\n")
    return EspDesignExtractionStack(cdk.App(), "Test", code_dir=contexts["lambda"], task_dir=contexts["task"],
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
    # Ours: five functions, each with its invoke config, role, log group and invoke permission (S3's, or the rule's
    # for the watcher); the run-log table; a notification on each bucket; the Glue database and its two tables (the
    # table and the view); the product-upload bucket and its policy.  The image route's: the VPC, its gateway and the
    # attachment, two subnets each with a route table, an association and a route, two gateway endpoints and the
    # task's security group; the cluster, the task definition, its two roles and its log group; and the rule.  CDK's
    # notification helper: its function and role, and a policy for each bucket.  No NAT gateway and no address.
    assert Counter(r["Type"] for r in template.values()) == Counter({
        "AWS::Lambda::Function": 6, "AWS::IAM::Role": 8, "AWS::IAM::Policy": 2,
        "AWS::Lambda::EventInvokeConfig": 5, "AWS::Lambda::Permission": 5, "AWS::Logs::LogGroup": 6,
        "AWS::DynamoDB::Table": 1, "Custom::S3BucketNotifications": 2, "AWS::Glue::Database": 1,
        "AWS::Glue::Table": 2, "AWS::S3::Bucket": 1, "AWS::S3::BucketPolicy": 1,
        "AWS::EC2::VPC": 1, "AWS::EC2::InternetGateway": 1, "AWS::EC2::VPCGatewayAttachment": 1,
        "AWS::EC2::Subnet": 2, "AWS::EC2::RouteTable": 2, "AWS::EC2::SubnetRouteTableAssociation": 2,
        "AWS::EC2::Route": 2, "AWS::EC2::VPCEndpoint": 2, "AWS::EC2::SecurityGroup": 1,
        "AWS::ECS::Cluster": 1, "AWS::ECS::TaskDefinition": 1, "AWS::Events::Rule": 1})


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
        "ESP_TRANSCRIPTIONS_PREFIX": "esp-design-extraction/transcriptions/",      # turns the image route on
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
    assert p["Environment"]["Variables"] == {"ESP_RUN_LOG_TABLE": "esp_design_extraction_logging",
                                             "ESP_WRITEBACK_SECRET": "esp-api/ds-writeback-credentials"}
    _, invoke = only(template, "AWS::Lambda::EventInvokeConfig", FunctionName={"Ref": fn_id})
    assert invoke["Properties"]["MaximumRetryAttempts"] == 0


def test_starter(template):
    fn_id, fn = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction-product-intake")
    _, extraction = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName="/aws/lambda/esp-design-extraction-product-intake")
    p = fn["Properties"]
    assert (p["PackageType"], p["Architectures"]) == ("Image", ["x86_64"])
    assert "Handler" not in p and "Runtime" not in p
    assert p["Code"] == extraction["Properties"]["Code"]                 # the same image
    assert p["ImageConfig"] == {"Command": ["lambda_function.product_upload_handler"]}
    assert (p["Timeout"], p["MemorySize"]) == (120, 2048) and "ReservedConcurrentExecutions" not in p
    assert p["LoggingConfig"] == {"LogGroup": {"Ref": log_id}}
    assert p["Environment"]["Variables"] == {
        "ESP_BUCKET": "roam-ds-assets", "ESP_INTAKE_PREFIX": "esp-design-extraction/intake/",
        "ESP_RUN_LOG_TABLE": "esp_design_extraction_logging", "ESP_PUSH_BUCKET": PUSH}
    _, invoke = only(template, "AWS::Lambda::EventInvokeConfig", FunctionName={"Ref": fn_id})
    assert invoke["Properties"]["MaximumRetryAttempts"] == 2


def test_image(stack):
    assembly = stack.node.root.synth()
    manifest = json.loads((Path(assembly.directory) / f"{stack.artifact_id}.assets.json").read_text())
    images = list(manifest["dockerImages"].values())                     # one for all five functions, one for the task
    assert len(images) == 2
    assert all(image["source"]["platform"] == "linux/amd64" for image in images)
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


def inline_statements(template, role_id, policy_name, service, star=None):
    """The statements of a role `service` assumes: its own inline policy only, no managed or attached policy.  No
    action and no resource is "*", but in `star`, the one statement let through."""
    role = template[role_id]["Properties"]
    assert "ManagedPolicyArns" not in role and "PermissionsBoundary" not in role
    assert role["AssumeRolePolicyDocument"]["Statement"] == [
        {"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": service}}]
    attached = [r for r in template.values() if r["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy")
                and {"Ref": role_id} in r["Properties"].get("Roles", [])]
    assert attached == [], "a policy outside the role's own inline policy is attached to it"
    [policy] = role["Policies"]
    assert policy["PolicyName"] == policy_name
    statements = policy["PolicyDocument"]["Statement"]
    for s in map(json.loads, map(canonical, statements)):
        if star is None or s != json.loads(canonical(star)):
            assert not any("*" in action for action in s["Action"]) and "*" not in s["Resource"]
    return statements


def role_statements(template, function_name):
    """The statements of the function's role, in the inline policy named as the function is."""
    _, fn = only(template, "AWS::Lambda::Function", FunctionName=function_name)
    return inline_statements(template, fn["Properties"]["Role"]["Fn::GetAtt"][0], function_name, "lambda.amazonaws.com")


def test_role_has_only_these_statements(template):
    """The extraction function's role: as it was, and a transcription's request, which it may put."""
    statements = role_statements(template, "esp-design-extraction")
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName="/aws/lambda/esp-design-extraction")
    expected = [
        {"Effect": "Allow", "Action": "s3:GetObject",
         "Resource": ["arn:aws:s3:::roam-ds-assets/esp-design-extraction/intake/*",
                      "arn:aws:s3:::roam-ds-assets/esp-design-extraction/assets/*"]},
        {"Effect": "Allow", "Action": "s3:PutObject",
         "Resource": ["arn:aws:s3:::roam-ds-assets/esp-design-extraction/runs/*",
                      "arn:aws:s3:::roam-ds-assets/esp-design-extraction/transcriptions/*/request.json"]},
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
        # The write-back's login: this one secret, whatever six characters Secrets Manager ended its ARN with.
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
         "Resource": "arn:aws:secretsmanager:us-east-1:640168431387:secret:esp-api/ds-writeback-credentials-??????"},
    ]
    assert sorted(map(canonical, statements)) == sorted(map(canonical, expected))
    actions = [a for s in map(json.loads, map(canonical, statements)) for a in s["Action"]]
    assert "s3:ListBucket" not in actions                   # implied by the equality; kept as the stated rules
    assert not any(a.split(":")[0] in ("glue", "athena", "bedrock") for a in actions)


def test_starter_role_has_only_these_statements(template):
    """It reads a push's version under dev/ or prd/, writes only the intake object, and lists nothing."""
    statements = role_statements(template, "esp-design-extraction-product-intake")
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName="/aws/lambda/esp-design-extraction-product-intake")
    expected = [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": [f"arn:aws:s3:::{PUSH}/dev/*", f"arn:aws:s3:::{PUSH}/prd/*"]},
        {"Effect": "Allow", "Action": "s3:PutObject", "Resource": f"{S3}intake/*"},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
         "Resource": {"Fn::GetAtt": [table_id, "Arn"]}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": {"Fn::GetAtt": [log_id, "Arn"]}},
    ]
    assert sorted(map(canonical, statements)) == sorted(map(canonical, expected))
    actions = [a for s in map(json.loads, map(canonical, statements)) for a in s["Action"]]
    assert not [a for a in actions if a.startswith("s3:List")]          # implied by the equality; the stated rule


def test_product_upload_bucket(template):
    """Owned by the stack: versioned, S3-managed encryption, no public access, ACLs off, retained, no lifecycle."""
    bucket_id, bucket = only(template, "AWS::S3::Bucket")
    assert bucket["Properties"] == {
        "BucketName": PUSH,
        "VersioningConfiguration": {"Status": "Enabled"},
        "BucketEncryption": {"ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                           "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
    }
    assert (bucket["DeletionPolicy"], bucket["UpdateReplacePolicy"]) == ("Retain", "Retain")


def test_product_upload_bucket_policy(template):
    """CDK's TLS-only deny, and one PutObject allow for each of the product's accounts, on its own prefix only."""
    bucket_id, _ = only(template, "AWS::S3::Bucket")
    _, policy = only(template, "AWS::S3::BucketPolicy", Bucket={"Ref": bucket_id})
    arn = {"Fn::GetAtt": [bucket_id, "Arn"]}
    assert policy["Properties"]["PolicyDocument"]["Statement"] == [
        {"Action": "s3:*", "Condition": {"Bool": {"aws:SecureTransport": "false"}}, "Effect": "Deny",
         "Principal": {"AWS": "*"}, "Resource": [arn, {"Fn::Join": ["", [arn, "/*"]]}]},
        {"Action": "s3:PutObject", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::411237692998:root"},
         "Resource": f"arn:aws:s3:::{PUSH}/dev/*"},
        {"Action": "s3:PutObject", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::826771500263:root"},
         "Resource": f"arn:aws:s3:::{PUSH}/prd/*"},
    ]


def test_the_product_upload_trigger(template):
    """Every .pdf created in the product-upload bucket, to the starter, through CDK's helper for an owned bucket."""
    bucket_id, _ = only(template, "AWS::S3::Bucket")
    starter_id, _ = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction-product-intake")
    _, notification = only(template, "Custom::S3BucketNotifications", BucketName={"Ref": bucket_id})
    assert (notification["Properties"]["Managed"], notification["Properties"]["NotificationConfiguration"]) == (True, {
        "LambdaFunctionConfigurations": [{
            "Events": ["s3:ObjectCreated:*"], "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": ".pdf"}]}},
            "LambdaFunctionArn": {"Fn::GetAtt": [starter_id, "Arn"]}}]})
    _, permission = only(template, "AWS::Lambda::Permission", FunctionName={"Fn::GetAtt": [starter_id, "Arn"]})
    assert permission["Properties"]["Principal"] == "s3.amazonaws.com"
    assert permission["Properties"]["SourceAccount"] == "640168431387"
    assert permission["Properties"]["SourceArn"] == {"Fn::GetAtt": [bucket_id, "Arn"]}
    [helper] = [r for r in template.values() if r["Type"] == "AWS::IAM::Policy"
                and r["Properties"]["PolicyDocument"]["Statement"][0]["Resource"] == {"Fn::GetAtt": [bucket_id, "Arn"]}]
    assert helper["Properties"]["PolicyDocument"]["Statement"] == [
        {"Action": "s3:PutBucketNotification", "Effect": "Allow", "Resource": {"Fn::GetAtt": [bucket_id, "Arn"]}}]


def test_triggers(template):
    """All three configurations through the one notification helper: the intake trigger as it was, the writer's, and
    the transcription starter's.  The notification on roam-ds-assets keeps its logical id."""
    fn_id, _ = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    writer_id, _ = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction-writer")
    starter_id, _ = only(template, "AWS::Lambda::Function", FunctionName=f"{ROUTE}-start")
    notification_id, notification = only(template, "Custom::S3BucketNotifications", BucketName="roam-ds-assets")
    assert notification_id == "BucketNotifications8F2E257D"
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
    }, {
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": "request.json"},
                                           {"Name": "prefix", "Value": "esp-design-extraction/transcriptions/"}]}},
        "LambdaFunctionArn": {"Fn::GetAtt": [starter_id, "Arn"]},
    }]}
    for function_id in (fn_id, writer_id, starter_id):
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


@pytest.mark.parametrize("name", ["/aws/lambda/esp-design-extraction", "/aws/lambda/esp-design-extraction-writer",
                                  "/aws/lambda/esp-design-extraction-product-intake", f"/aws/lambda/{ROUTE}-start",
                                  f"/aws/lambda/{ROUTE}-watch", f"/ecs/{ROUTE}"])
def test_log_groups(template, name):
    _, group = only(template, "AWS::Logs::LogGroup", LogGroupName=name)
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


# ---- the image route ----

def route(template):
    """The logical ids the route's parts name each other by: its cluster, task definition, the task's two roles (its
    own, then the execution role), its security group and its two subnets, by zone."""
    cluster_id, _ = only(template, "AWS::ECS::Cluster")
    task_id, task = only(template, "AWS::ECS::TaskDefinition")
    group_id, _ = only(template, "AWS::EC2::SecurityGroup")
    return {"cluster": cluster_id, "task": task_id, "group": group_id,
            "roles": [task["Properties"][key]["Fn::GetAtt"][0] for key in ("TaskRoleArn", "ExecutionRoleArn")],
            "subnets": [only(template, "AWS::EC2::Subnet", AvailabilityZone=zone)[0]
                        for zone in ("us-east-1a", "us-east-1b")]}


def test_synth_needs_no_lookup(stack):
    """The zones are named, so nothing is asked of the account: the assembly lists no missing context."""
    manifest = json.loads((Path(stack.node.root.synth().directory) / "manifest.json").read_text())
    assert "missing" not in manifest and stack.availability_zones == ["us-east-1a", "us-east-1b"]


def test_network(template):
    """The route's own VPC, clear of the account's others: two public subnets in two named zones, each routed to the
    internet gateway, and gateway endpoints for S3 and DynamoDB on both route tables.  The counts show no NAT."""
    vpc_id, vpc = only(template, "AWS::EC2::VPC")
    assert vpc["Properties"]["CidrBlock"] == "10.4.0.0/24"
    assert (vpc["Properties"]["EnableDnsSupport"], vpc["Properties"]["EnableDnsHostnames"]) == (True, True)
    gateway_id, _ = only(template, "AWS::EC2::InternetGateway")
    only(template, "AWS::EC2::VPCGatewayAttachment", VpcId={"Ref": vpc_id}, InternetGatewayId={"Ref": gateway_id})
    tables = []
    for zone, cidr in (("us-east-1a", "10.4.0.0/25"), ("us-east-1b", "10.4.0.128/25")):
        subnet_id, subnet = only(template, "AWS::EC2::Subnet", AvailabilityZone=zone)
        assert (subnet["Properties"]["VpcId"], subnet["Properties"]["CidrBlock"]) == ({"Ref": vpc_id}, cidr)
        assert subnet["Properties"]["MapPublicIpOnLaunch"] is False      # the starter asks for the task's address
        _, association = only(template, "AWS::EC2::SubnetRouteTableAssociation", SubnetId={"Ref": subnet_id})
        table = association["Properties"]["RouteTableId"]
        _, default = only(template, "AWS::EC2::Route", RouteTableId=table)
        assert default["Properties"] == {"RouteTableId": table, "DestinationCidrBlock": "0.0.0.0/0",
                                         "GatewayId": {"Ref": gateway_id}}
        tables.append(table)
    for service in ("s3", "dynamodb"):
        _, endpoint = only(template, "AWS::EC2::VPCEndpoint", ServiceName={
            "Fn::Join": ["", ["com.amazonaws.", {"Ref": "AWS::Region"}, f".{service}"]]})
        p = endpoint["Properties"]
        assert (p["VpcEndpointType"], p["VpcId"], p["RouteTableIds"]) == ("Gateway", {"Ref": vpc_id}, tables)


def test_the_tasks_security_group_lets_nothing_in(template):
    vpc_id, _ = only(template, "AWS::EC2::VPC")
    _, group = only(template, "AWS::EC2::SecurityGroup")
    p = group["Properties"]
    assert p["VpcId"] == {"Ref": vpc_id} and "SecurityGroupIngress" not in p
    assert p["SecurityGroupEgress"] == [
        {"CidrIp": "0.0.0.0/0", "Description": "Allow all outbound traffic by default", "IpProtocol": "-1"}]
    assert not [r for r in template.values() if r["Type"] == "AWS::EC2::SecurityGroupIngress"]


def test_cluster_and_task_definition(template):
    """The route's cluster and its one task: Fargate, 1 vCPU and 2 GB, x86, one container on the task's own image,
    which runs the image's own command (the definition sets none)."""
    ids = route(template)
    assert template[ids["cluster"]]["Properties"] == {"ClusterName": ROUTE}
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName=f"/ecs/{ROUTE}")
    _, extraction = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    p = template[ids["task"]]["Properties"]
    assert (p["Family"], p["Cpu"], p["Memory"]) == (ROUTE, "1024", "2048")
    assert (p["NetworkMode"], p["RequiresCompatibilities"]) == ("awsvpc", ["FARGATE"])
    assert p["RuntimePlatform"] == {"CpuArchitecture": "X86_64", "OperatingSystemFamily": "LINUX"}
    assert len(set(ids["roles"])) == 2
    [container] = p["ContainerDefinitions"]
    functions_image = extraction["Properties"]["Code"]["ImageUri"]["Fn::Sub"]
    repository, tag = container["Image"]["Fn::Sub"].rsplit(":", 1)
    assert repository == functions_image.rsplit(":", 1)[0] and tag != functions_image.rsplit(":", 1)[1]
    assert {key: value for key, value in container.items() if key != "Image"} == {
        "Name": "transcription", "Essential": True,
        "Environment": [{"Name": name, "Value": value} for name, value in {
            "ESP_BUCKET": "roam-ds-assets",
            "ESP_INTAKE_PREFIX": "esp-design-extraction/intake/",
            "ESP_RUNS_PREFIX": "esp-design-extraction/runs/",
            "ESP_TRANSCRIPTIONS_PREFIX": "esp-design-extraction/transcriptions/",
            "ESP_RUN_LOG_TABLE": "esp_design_extraction_logging",
            "ESP_INFERENCE_PROFILE_ID": "us.anthropic.claude-opus-5-5",
            "ESP_IMAGE_ROUTE_CACHE": "1",                # cache points on
            "AWS_DEFAULT_REGION": "us-east-1",           # boto3's region
        }.items()],
        "LogConfiguration": {"LogDriver": "awslogs", "Options": {
            "awslogs-group": {"Ref": log_id}, "awslogs-stream-prefix": "transcription",
            "awslogs-region": "us-east-1"}},
    }


def test_task_role_has_only_these_statements(template):
    """What the task itself calls.  It lists nothing, never streams, and under transcriptions/ writes only a result."""
    statements = inline_statements(template, route(template)["roles"][0], f"{ROUTE}-task", "ecs-tasks.amazonaws.com")
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    expected = [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": [f"{S3}intake/*", f"{S3}runs/*/run.json"]},
        {"Effect": "Allow", "Action": "s3:PutObject",
         "Resource": [f"{S3}intake/*", f"{S3}transcriptions/*/result.json"]},
        {"Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": PROFILE_ARN},
        {"Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": MODELS,
         "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": PROFILE_ARN}}},
        {"Effect": "Allow", "Action": "dynamodb:UpdateItem", "Resource": {"Fn::GetAtt": [table_id, "Arn"]}},
    ]
    assert sorted(map(canonical, statements)) == sorted(map(canonical, expected))


def test_execution_role_has_only_these_statements(template):
    """What ECS calls to start the task: the image pulled from the bootstrap's repository, and the task's log group
    written.  CDK's own grants for the image and the log driver are stopped, so no separate policy is attached."""
    statements = inline_statements(template, route(template)["roles"][1], f"{ROUTE}-execution",
                                   "ecs-tasks.amazonaws.com", star=GET_TOKEN)
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName=f"/ecs/{ROUTE}")
    expected = [
        GET_TOKEN,
        {"Effect": "Allow",
         "Action": ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
         "Resource": {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":ecr:us-east-1:640168431387:repository/"
                                        "cdk-hnb659fds-container-assets-640168431387-us-east-1"]]}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": {"Fn::GetAtt": [log_id, "Arn"]}},
    ]
    assert sorted(map(canonical, statements)) == sorted(map(canonical, expected))


def test_only_the_execution_role_names_a_star(template):
    """Across the stack's seven roles of its own, one statement is on "*", and no action is a wildcard."""
    own = [r["Properties"]["Policies"] for r in template.values()
           if r["Type"] == "AWS::IAM::Role" and "Policies" in r["Properties"]]
    assert len(own) == 7                                 # the eighth role is CDK's notification helper's
    statements = [s for [policy] in own for s in policy["PolicyDocument"]["Statement"]]
    assert [s for s in statements if "*" in json.loads(canonical(s))["Resource"]] == [GET_TOKEN]
    assert not [s for s in statements if any("*" in action for action in json.loads(canonical(s))["Action"])]


def test_transcription_starter(template):
    ids = route(template)
    fn_id, fn = only(template, "AWS::Lambda::Function", FunctionName=f"{ROUTE}-start")
    _, extraction = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName=f"/aws/lambda/{ROUTE}-start")
    p = fn["Properties"]
    assert (p["PackageType"], p["Architectures"]) == ("Image", ["x86_64"])
    assert "Handler" not in p and "Runtime" not in p and "VpcConfig" not in p
    assert p["Code"] == extraction["Properties"]["Code"]                 # the same image
    assert p["ImageConfig"] == {"Command": ["lambda_function.transcription_start_handler"]}
    # The handler's three tries take up to 25 s each, with 2 s and 4 s between them: 81 s.
    assert (p["Timeout"], p["MemorySize"]) == (180, 2048) and "ReservedConcurrentExecutions" not in p
    assert p["LoggingConfig"] == {"LogGroup": {"Ref": log_id}}
    task = template[ids["task"]]["Properties"]
    assert task["Family"] == ROUTE                        # the family the role may run; the variable names its revision
    [container] = task["ContainerDefinitions"]
    assert p["Environment"]["Variables"] == {
        **ROUTE_ENV,
        "ESP_TRANSCRIPTION_CLUSTER": {"Fn::GetAtt": [ids["cluster"], "Arn"]},
        "ESP_TRANSCRIPTION_TASK_DEFINITION": {"Ref": ids["task"]},
        "ESP_TRANSCRIPTION_CONTAINER": container["Name"],
        "ESP_TRANSCRIPTION_SUBNETS": {"Fn::Join": ["", [{"Ref": ids["subnets"][0]}, ",", {"Ref": ids["subnets"][1]}]]},
        "ESP_TRANSCRIPTION_SECURITY_GROUPS": {"Fn::GetAtt": [ids["group"], "GroupId"]},
        "ESP_TRANSCRIPTION_ASSIGN_PUBLIC_IP": "ENABLED",
    }
    _, invoke = only(template, "AWS::Lambda::EventInvokeConfig", FunctionName={"Ref": fn_id})
    assert invoke["Properties"]["MaximumRetryAttempts"] == 2


def test_transcription_starter_role_has_only_these_statements(template):
    """It reads a request, ends one, writes the request's row and runs the route's task: any revision of this family,
    so a deploy that replaces the revision refuses no start, in this cluster only.  It may pass the task's two roles,
    and only to ECS tasks."""
    ids = route(template)
    statements = role_statements(template, f"{ROUTE}-start")
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName=f"/aws/lambda/{ROUTE}-start")
    expected = [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"{S3}transcriptions/*/request.json"},
        {"Effect": "Allow", "Action": "s3:PutObject", "Resource": f"{S3}transcriptions/*/result.json"},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
         "Resource": {"Fn::GetAtt": [table_id, "Arn"]}},
        {"Effect": "Allow", "Action": "ecs:RunTask",
         "Resource": f"arn:aws:ecs:us-east-1:640168431387:task-definition/{ROUTE}:*",
         "Condition": {"ArnEquals": {"ecs:cluster": {"Fn::GetAtt": [ids["cluster"], "Arn"]}}}},
        {"Effect": "Allow", "Action": "iam:PassRole",
         "Resource": [{"Fn::GetAtt": [role_id, "Arn"]} for role_id in ids["roles"]],
         "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": {"Fn::GetAtt": [log_id, "Arn"]}},
    ]
    assert sorted(map(canonical, statements)) == sorted(map(canonical, expected))


def test_transcription_watcher(template):
    fn_id, fn = only(template, "AWS::Lambda::Function", FunctionName=f"{ROUTE}-watch")
    _, extraction = only(template, "AWS::Lambda::Function", FunctionName="esp-design-extraction")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName=f"/aws/lambda/{ROUTE}-watch")
    p = fn["Properties"]
    assert (p["PackageType"], p["Architectures"]) == ("Image", ["x86_64"])
    assert "Handler" not in p and "Runtime" not in p and "VpcConfig" not in p
    assert p["Code"] == extraction["Properties"]["Code"]                 # the same image
    assert p["ImageConfig"] == {"Command": ["lambda_function.transcription_watch_handler"]}
    assert (p["Timeout"], p["MemorySize"]) == (120, 2048) and "ReservedConcurrentExecutions" not in p
    assert p["LoggingConfig"] == {"LogGroup": {"Ref": log_id}}
    assert p["Environment"]["Variables"] == ROUTE_ENV
    _, invoke = only(template, "AWS::Lambda::EventInvokeConfig", FunctionName={"Ref": fn_id})
    assert invoke["Properties"]["MaximumRetryAttempts"] == 2


def test_transcription_watcher_role_has_only_these_statements(template):
    """It ends a request, and reads one only when an event comes without the starter's overrides.  It starts nothing
    and puts no row."""
    statements = role_statements(template, f"{ROUTE}-watch")
    table_id, _ = only(template, "AWS::DynamoDB::Table")
    log_id, _ = only(template, "AWS::Logs::LogGroup", LogGroupName=f"/aws/lambda/{ROUTE}-watch")
    expected = [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"{S3}transcriptions/*/request.json"},
        {"Effect": "Allow", "Action": "s3:PutObject", "Resource": f"{S3}transcriptions/*/result.json"},
        {"Effect": "Allow", "Action": "dynamodb:UpdateItem", "Resource": {"Fn::GetAtt": [table_id, "Arn"]}},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": {"Fn::GetAtt": [log_id, "Arn"]}},
    ]
    assert sorted(map(canonical, statements)) == sorted(map(canonical, expected))


def test_stopped_tasks_go_to_the_watcher(template):
    """One rule: ECS's "Task State Change" events whose task has STOPPED, from the route's cluster only, to the
    watcher, which EventBridge may invoke through this rule alone."""
    ids = route(template)
    watcher_id, _ = only(template, "AWS::Lambda::Function", FunctionName=f"{ROUTE}-watch")
    rule_id, rule = only(template, "AWS::Events::Rule")
    p = rule["Properties"]
    assert (p["Name"], p["State"]) == (f"{ROUTE}-stopped", "ENABLED")
    assert p["EventPattern"] == {
        "source": ["aws.ecs"], "detail-type": ["ECS Task State Change"],
        "detail": {"clusterArn": [{"Fn::GetAtt": [ids["cluster"], "Arn"]}], "lastStatus": ["STOPPED"]}}
    assert p["Targets"] == [{"Arn": {"Fn::GetAtt": [watcher_id, "Arn"]}, "Id": "Target0"}]
    _, permission = only(template, "AWS::Lambda::Permission", FunctionName={"Fn::GetAtt": [watcher_id, "Arn"]})
    assert permission["Properties"]["Principal"] == "events.amazonaws.com"
    assert permission["Properties"]["SourceArn"] == {"Fn::GetAtt": [rule_id, "Arn"]}
