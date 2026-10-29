"""The design-extraction Lambda and the confirmation writer (each with its role, S3 trigger and log group), the run-log
table, and the well-entries table and its current-entry view in the Glue catalogue."""

import base64
import importlib.util
import json
from pathlib import Path

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from constructs import Construct

BUCKET = "roam-ds-assets"
INTAKE_PREFIX = "esp-design-extraction/intake/"
RUNS_PREFIX = "esp-design-extraction/runs/"
ASSETS_PREFIX = "esp-design-extraction/assets/"
CONFIRMATIONS_PREFIX = "esp-design-extraction/confirmations/"
RECORDS_PREFIX = "esp-design-extraction/design-records/"
ENTRIES_PREFIX = "esp-design-extraction/well-entries/"
FUNCTION_NAME = "esp-design-extraction"
WRITER_NAME = "esp-design-extraction-writer"
TABLE_NAME = "esp_design_extraction_logging"         # in the layout of the ESP product's esp_logging_v2
PROFILE_ID = "us.anthropic.claude-opus-5-5"
PROFILE_ARN = f"arn:aws:bedrock:us-east-1:640168431387:inference-profile/{PROFILE_ID}"
# The regions the US profile routes to; a request may land in any of them.
MODEL_ARNS = [f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-opus-5-5"
              for region in ("us-east-1", "us-east-2", "us-west-2")]
BUILD_DIR = Path(__file__).resolve().parents[1] / "build" / "lambda"     # the image context build_lambda.py stages
DATABASE, TABLE, VIEW = "esp_design_extraction", "well_entries", "well_entries_current"
SCHEMA = Path(__file__).resolve().parents[2] / "extraction" / "well_entries_schema.py"    # the table's columns
VIEW_SQL = Path(__file__).resolve().parent / "well_entries_current.sql"
JSON_SERDE = "org.openx.data.jsonserde.JsonSerDe"
TEXT_INPUT = "org.apache.hadoop.mapred.TextInputFormat"
TEXT_OUTPUT = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

ENVIRONMENT = {
    "ESP_BUCKET": BUCKET,
    "ESP_INTAKE_PREFIX": INTAKE_PREFIX,
    "ESP_RUNS_PREFIX": RUNS_PREFIX,
    "ESP_ASSETS_PREFIX": ASSETS_PREFIX,
    "ESP_RUN_LOG_TABLE": TABLE_NAME,
    "ESP_INFERENCE_PROFILE_ID": PROFILE_ID,
}
INVOKE = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]


def string_key(name: str) -> dynamodb.Attribute:
    return dynamodb.Attribute(name=name, type=dynamodb.AttributeType.STRING)


def load_schema():
    """extraction/well_entries_schema.py, loaded by path: it imports nothing, so the stack needs none of the
    pipeline's dependencies."""
    spec = importlib.util.spec_from_file_location("well_entries_schema", SCHEMA)
    schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)
    return schema


def presto_type(hive: str) -> str:
    """A Hive type as an Athena view types its column, in Trino's type syntax: string varchar, double double, int
    integer, struct row, array array."""
    if hive.startswith("array<"):
        return f"array({presto_type(hive[len('array<'):-1])})"
    if hive.startswith("struct<"):
        fields, depth, start = [], 0, len("struct<")
        for i in range(start, len(hive) - 1):
            depth += {"<": 1, ">": -1}.get(hive[i], 0)
            if hive[i] == "," and depth == 0:
                fields.append(hive[start:i])
                start = i + 1
        fields.append(hive[start:-1])
        return "row(" + ", ".join(f'"{name}" {presto_type(t)}' for name, t in (f.split(":", 1) for f in fields)) + ")"
    return {"string": "varchar", "double": "double", "int": "integer"}[hive]


def presto_view(sql: str, columns) -> str:
    """An Athena view's ViewOriginalText: the view's JSON, base64, between Trino's markers, as Trino's Hive connector
    writes it (ViewReaderUtil.encodeViewData)."""
    view = {"originalSql": sql, "catalog": "awsdatacatalog", "schema": DATABASE,
            "columns": [{"name": name, "type": presto_type(hive)} for name, hive in columns], "runAsInvoker": False}
    return "/* Presto View: " + base64.b64encode(json.dumps(view).encode()).decode() + " */"


def glue_columns(columns) -> list:
    return [glue.CfnTable.ColumnProperty(name=name, type=hive) for name, hive in columns]


class EspDesignExtractionStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, code_dir: Path = BUILD_DIR, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The run log: one row per run, in esp_logging_v2's layout, so that moving to the product's table is
        # a configuration change.  No TTL and no stream, as there.
        table = dynamodb.Table(
            self, "RunLog", table_name=TABLE_NAME,
            partition_key=string_key("organization_id"), sort_key=string_key("timestamp"),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
        )
        for index, partition, sort in (("uuid", "uuid", None),
                                       ("pipeline_execution_id_timestamp", "pipeline_execution_id", "timestamp"),
                                       ("organization_id_type_timestamp", "organization_id_type", "timestamp"),
                                       ("status_timestamp", "status", "timestamp")):
            table.add_global_secondary_index(index_name=index, partition_key=string_key(partition),
                                             sort_key=sort and string_key(sort),
                                             projection_type=dynamodb.ProjectionType.ALL)

        log_group = logs.LogGroup(
            self, "LogGroup", log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
            retention=logs.RetentionDays.THREE_MONTHS, removal_policy=RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
        )

        # Inline statements only: anything CDK added on its own would land in a separate policy,
        # which the template tests refuse.
        role = iam.Role(
            self, "Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={"esp-design-extraction": iam.PolicyDocument(statements=[
                iam.PolicyStatement(actions=["s3:GetObject"], resources=[
                    f"arn:aws:s3:::{BUCKET}/{INTAKE_PREFIX}*", f"arn:aws:s3:::{BUCKET}/{ASSETS_PREFIX}*"]),
                iam.PolicyStatement(actions=["s3:PutObject"], resources=[f"arn:aws:s3:::{BUCKET}/{RUNS_PREFIX}*"]),
                iam.PolicyStatement(actions=INVOKE, resources=[PROFILE_ARN]),
                iam.PolicyStatement(actions=INVOKE, resources=MODEL_ARNS,
                                    conditions={"StringEquals": {"bedrock:InferenceProfileArn": PROFILE_ARN}}),
                iam.PolicyStatement(actions=["dynamodb:PutItem", "dynamodb:UpdateItem"], resources=[table.table_arn]),
                iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                    resources=[log_group.log_group_arn]),
            ])},
        )

        def image(cmd: str) -> lambda_.DockerImageCode:     # one image, built from code_dir; each function its command
            return lambda_.DockerImageCode.from_image_asset(str(code_dir), platform=ecr_assets.Platform.LINUX_AMD64,
                                                            cmd=[cmd])

        function = lambda_.DockerImageFunction(
            self, "Function", function_name=FUNCTION_NAME, architecture=lambda_.Architecture.X86_64,
            code=image("lambda_function.lambda_handler"),
            timeout=Duration.seconds(900), memory_size=2048, reserved_concurrent_executions=1, retry_attempts=0,
            role=role, log_group=log_group, environment=ENVIRONMENT,
        )

        # The confirmation writer.  Its role has no s3:ListBucket, so S3 answers 403, not 404, for a key that is not
        # there, and the writer reads that as not there; it has no Glue, Athena or Bedrock permission.
        writer_log_group = logs.LogGroup(
            self, "WriterLogGroup", log_group_name=f"/aws/lambda/{WRITER_NAME}",
            retention=logs.RetentionDays.THREE_MONTHS, removal_policy=RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
        )
        writer_role = iam.Role(
            self, "WriterRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={WRITER_NAME: iam.PolicyDocument(statements=[
                iam.PolicyStatement(actions=["s3:GetObject"], resources=[
                    f"arn:aws:s3:::{BUCKET}/{prefix}*"
                    for prefix in (CONFIRMATIONS_PREFIX, RUNS_PREFIX, INTAKE_PREFIX, ASSETS_PREFIX)]),
                iam.PolicyStatement(actions=["s3:PutObject"], resources=[
                    f"arn:aws:s3:::{BUCKET}/{RECORDS_PREFIX}*", f"arn:aws:s3:::{BUCKET}/{ENTRIES_PREFIX}*",
                    f"arn:aws:s3:::{BUCKET}/{CONFIRMATIONS_PREFIX}*/result.json"]),
                iam.PolicyStatement(actions=["dynamodb:PutItem", "dynamodb:UpdateItem"], resources=[table.table_arn]),
                iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                    resources=[writer_log_group.log_group_arn]),
            ])},
        )
        writer = lambda_.DockerImageFunction(
            self, "Writer", function_name=WRITER_NAME, architecture=lambda_.Architecture.X86_64,
            code=image("lambda_function.confirm_handler"),
            timeout=Duration.seconds(120), memory_size=2048, reserved_concurrent_executions=1, retry_attempts=0,
            role=writer_role, log_group=writer_log_group, environment={"ESP_RUN_LOG_TABLE": TABLE_NAME},
        )

        # Neither function's output can trigger it: the extraction writes only under runs/, the writer under
        # design-records/, well-entries/ and confirmations/<id>/result.json.
        bucket = s3.Bucket.from_bucket_name(self, "Bucket", BUCKET)
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(function),
            s3.NotificationKeyFilter(prefix=INTAKE_PREFIX, suffix=".pdf"),
        )
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(writer),
            s3.NotificationKeyFilter(prefix=CONFIRMATIONS_PREFIX, suffix="request.json"),
        )

        # The well-entries table, its columns from the schema module, and the view of each well's current entry.
        schema = load_schema()
        database = glue.CfnDatabase(self, "Database", catalog_id=self.account,
                                    database_input=glue.CfnDatabase.DatabaseInputProperty(name=DATABASE))
        entries = glue.CfnTable(
            self, "WellEntries", catalog_id=self.account, database_name=database.ref,
            table_input=glue.CfnTable.TableInputProperty(
                name=TABLE, table_type="EXTERNAL_TABLE",
                parameters={
                    "classification": "json", "EXTERNAL": "TRUE", "projection.enabled": "true",
                    "projection.environment.type": "enum", "projection.environment.values": "dev,prd",
                    # Literal: Athena fills in ${environment} when it reads the table; nothing here substitutes it.
                    "storage.location.template": f"s3://{BUCKET}/{ENTRIES_PREFIX}environment=${{environment}}/",
                },
                partition_keys=glue_columns(schema.PARTITION_KEYS),
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    columns=glue_columns(schema.COLUMNS), location=f"s3://{BUCKET}/{ENTRIES_PREFIX}",
                    input_format=TEXT_INPUT, output_format=TEXT_OUTPUT,
                    serde_info=glue.CfnTable.SerdeInfoProperty(serialization_library=JSON_SERDE)),
            ),
        )
        view_columns = schema.COLUMNS + schema.PARTITION_KEYS
        view = glue.CfnTable(
            self, "WellEntriesCurrent", catalog_id=self.account, database_name=database.ref,
            table_input=glue.CfnTable.TableInputProperty(
                name=VIEW, table_type="VIRTUAL_VIEW",
                # Trino reads a view as its own only with both parameters (ViewReaderUtil.isTrinoView).
                parameters={"presto_view": "true", "comment": "Presto View"},
                view_original_text=presto_view(VIEW_SQL.read_text().strip(), view_columns),
                view_expanded_text="/* Presto View */",
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(columns=glue_columns(view_columns)),
            ),
        )
        for resource in (database, entries, view):
            resource.apply_removal_policy(RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE)
