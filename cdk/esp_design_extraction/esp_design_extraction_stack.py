"""The design-extraction Lambda and the confirmation writer (each with its role, S3 trigger and log group), the run-log
table, the well-entries table and its current-entry view in the Glue catalogue, and the product-upload bucket with its
starter (role, trigger and log group), which turns each design PDF the ESP product pushes into a run.  And the image
route, which transcribes a scan the extraction could not read: its network, cluster and Fargate task, the function that
starts the task for each request, and the one that ends a request whose task stopped without ending it."""

import base64
import importlib.util
import json
from pathlib import Path

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
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
TRANSCRIPTIONS_PREFIX = "esp-design-extraction/transcriptions/"
FUNCTION_NAME = "esp-design-extraction"
WRITER_NAME = "esp-design-extraction-writer"
STARTER_NAME = "esp-design-extraction-product-intake"
# The image route: its cluster and its task's family, as image_route/task.py names the task in the run log.
TRANSCRIPTION_NAME = "esp-design-extraction-transcription"
TRANSCRIPTION_STARTER_NAME = f"{TRANSCRIPTION_NAME}-start"
TRANSCRIPTION_WATCHER_NAME = f"{TRANSCRIPTION_NAME}-watch"
TRANSCRIPTION_CONTAINER = "transcription"
# The route's own network.  The account's other VPCs hold 10.0.0.0/16, 10.2.0.0/16 and 10.3.0.0/24.  The zones are
# named, so the synth looks nothing up.
VPC_CIDR = "10.4.0.0/24"
ZONES = ["us-east-1a", "us-east-1b"]
PUSH_BUCKET = "esp-design-extraction-product-uploads-640168431387"
# The ESP product's accounts, each putting only under its own prefix: products dev and products prd.
PUSHERS = {"dev/": "411237692998", "prd/": "826771500263"}
TABLE_NAME = "esp_design_extraction_logging"         # in the layout of the ESP product's esp_logging_v2
# The write-back's login to the ESP product (extraction/write_back.py reads its dev block only).  Secrets Manager ends
# a secret's ARN with six characters of its own, so the grant names them as six wildcards.
WRITEBACK_SECRET = "esp-api/ds-writeback-credentials"
WRITEBACK_SECRET_ARN = f"arn:aws:secretsmanager:us-east-1:640168431387:secret:{WRITEBACK_SECRET}-??????"
PROFILE_ID = "us.anthropic.claude-opus-5-5"
PROFILE_ARN = f"arn:aws:bedrock:us-east-1:640168431387:inference-profile/{PROFILE_ID}"
# The regions the US profile routes to; a request may land in any of them.
MODEL_ARNS = [f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-opus-5-5"
              for region in ("us-east-1", "us-east-2", "us-west-2")]
BUILD_DIR = Path(__file__).resolve().parents[1] / "build" / "lambda"     # the image context build_lambda.py stages
TASK_DIR = BUILD_DIR.parent / "task"                                     # and the transcription task's, beside it
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
    @property
    def availability_zones(self) -> list:
        """The stack's zones, named.  CDK checks the VPC's zones against these, and would otherwise look the account's
        up: a context lookup, which needs credentials and writes cdk.context.json."""
        return ZONES

    def __init__(self, scope: Construct, construct_id: str, *, code_dir: Path = BUILD_DIR, task_dir: Path = TASK_DIR,
                 **kwargs) -> None:
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
                iam.PolicyStatement(actions=["s3:PutObject"], resources=[
                    f"arn:aws:s3:::{BUCKET}/{RUNS_PREFIX}*",
                    f"arn:aws:s3:::{BUCKET}/{TRANSCRIPTIONS_PREFIX}*/request.json"]),
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
            # The transcriptions prefix turns the image route on: an IMAGE_ONLY run asks for its transcription there.
            role=role, log_group=log_group,
            environment={**ENVIRONMENT, "ESP_TRANSCRIPTIONS_PREFIX": TRANSCRIPTIONS_PREFIX},
        )

        # The confirmation writer.  Its role has no s3:ListBucket, so S3 answers 403, not 404, for a key that is not
        # there, and the writer reads that as not there; it has no Glue, Athena or Bedrock permission.  It may read the
        # write-back's secret, which is on AWS's own key, so it needs no key statement.  Naming the secret in the
        # writer's environment turns the write-back on.
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
                iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"], resources=[WRITEBACK_SECRET_ARN]),
            ])},
        )
        writer = lambda_.DockerImageFunction(
            self, "Writer", function_name=WRITER_NAME, architecture=lambda_.Architecture.X86_64,
            code=image("lambda_function.confirm_handler"),
            timeout=Duration.seconds(120), memory_size=2048, reserved_concurrent_executions=1, retry_attempts=0,
            role=writer_role, log_group=writer_log_group,
            environment={"ESP_RUN_LOG_TABLE": TABLE_NAME, "ESP_WRITEBACK_SECRET": WRITEBACK_SECRET},
        )

        # No function's output can trigger itself: the extraction writes only under runs/ and a transcription's
        # request.json, the writer under design-records/, well-entries/ and confirmations/<id>/result.json.  The starter
        # (below) writes only intake/<run_id>.pdf, which starts the extraction: that is what it is for.  The image route
        # (at the end) is the one loop, and it closes once: a request starts the task, and the task's transcription is
        # an intake upload, which the extraction never asks to have transcribed.
        bucket = s3.Bucket.from_bucket_name(self, "Bucket", BUCKET)
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(function),
            s3.NotificationKeyFilter(prefix=INTAKE_PREFIX, suffix=".pdf"),
        )
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(writer),
            s3.NotificationKeyFilter(prefix=CONFIRMATIONS_PREFIX, suffix="request.json"),
        )

        # The product-upload bucket, owned by this stack: the ESP product's dev account may put only under dev/, its
        # prd account only under prd/; nothing else may (CDK's deny refuses any request without TLS).  Each PDF put
        # there starts the starter, which reads that version and puts it at intake/<run_id>.pdf in roam-ds-assets with
        # its labels, create-only.  Its role can list nothing, and cannot write to the bucket it reads.
        push_bucket = s3.Bucket(
            self, "ProductUploads", bucket_name=PUSH_BUCKET, versioned=True, encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True, removal_policy=RemovalPolicy.RETAIN,
        )
        for prefix, account in PUSHERS.items():       # the account principal as a literal ARN, as this stack's ARNs are
            push_bucket.add_to_resource_policy(iam.PolicyStatement(
                actions=["s3:PutObject"], principals=[iam.ArnPrincipal(f"arn:aws:iam::{account}:root")],
                resources=[f"arn:aws:s3:::{PUSH_BUCKET}/{prefix}*"]))
        starter_log_group = logs.LogGroup(
            self, "StarterLogGroup", log_group_name=f"/aws/lambda/{STARTER_NAME}",
            retention=logs.RetentionDays.THREE_MONTHS, removal_policy=RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
        )
        starter_role = iam.Role(
            self, "StarterRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={STARTER_NAME: iam.PolicyDocument(statements=[
                iam.PolicyStatement(actions=["s3:GetObject", "s3:GetObjectVersion"], resources=[
                    f"arn:aws:s3:::{PUSH_BUCKET}/{prefix}*" for prefix in PUSHERS]),
                iam.PolicyStatement(actions=["s3:PutObject"], resources=[f"arn:aws:s3:::{BUCKET}/{INTAKE_PREFIX}*"]),
                iam.PolicyStatement(actions=["dynamodb:PutItem", "dynamodb:UpdateItem"], resources=[table.table_arn]),
                iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                    resources=[starter_log_group.log_group_arn]),
            ])},
        )
        # Retried twice: a start is the same run each time (the run id comes from the event), and its put is
        # create-only, so a retry starts it or finds it started.
        starter = lambda_.DockerImageFunction(
            self, "Starter", function_name=STARTER_NAME, architecture=lambda_.Architecture.X86_64,
            code=image("lambda_function.product_upload_handler"),
            timeout=Duration.seconds(120), memory_size=2048, retry_attempts=2,
            role=starter_role, log_group=starter_log_group,
            environment={"ESP_BUCKET": BUCKET, "ESP_INTAKE_PREFIX": INTAKE_PREFIX, "ESP_RUN_LOG_TABLE": TABLE_NAME,
                         "ESP_PUSH_BUCKET": PUSH_BUCKET},
        )
        push_bucket.add_event_notification(s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(starter),
                                           s3.NotificationKeyFilter(suffix=".pdf"))

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

        # ---- the image route ----
        # The task's network, the stack's own: two public subnets, an internet gateway and no NAT.  The task reaches
        # Bedrock, ECR and CloudWatch Logs by its public address, which the starter asks for, and S3 and DynamoDB by
        # the gateway endpoints.  Its security group lets nothing in.  The VPC's default security group stays as AWS
        # makes it: CDK would clear it through a function and a role of its own.
        vpc = ec2.Vpc(
            self, "Vpc", ip_addresses=ec2.IpAddresses.cidr(VPC_CIDR), availability_zones=ZONES, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC,
                                                          cidr_mask=25, map_public_ip_on_launch=False)],
            gateway_endpoints={"S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3),
                               "DynamoDB": ec2.GatewayVpcEndpointOptions(
                                   service=ec2.GatewayVpcEndpointAwsService.DYNAMODB)},
            restrict_default_security_group=False,
        )
        task_security_group = ec2.SecurityGroup(
            self, "TaskSecurityGroup", vpc=vpc, allow_all_outbound=True,
            description="ESP design extraction transcription task: nothing in, anything out")
        cluster = ecs.Cluster(self, "Cluster", cluster_name=TRANSCRIPTION_NAME, vpc=vpc)

        task_log_group = logs.LogGroup(
            self, "TaskLogGroup", log_group_name=f"/ecs/{TRANSCRIPTION_NAME}",
            retention=logs.RetentionDays.THREE_MONTHS, removal_policy=RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
        )
        # Built from task_dir, which build_lambda.py stages beside the Lambda's context: the Lambda's base and
        # packages, with the route's code, the skill and the two packages the skill's build needs.
        task_image = ecr_assets.DockerImageAsset(self, "TaskImage", directory=str(task_dir),
                                                 platform=ecr_assets.Platform.LINUX_AMD64)
        result_arn = f"arn:aws:s3:::{BUCKET}/{TRANSCRIPTIONS_PREFIX}*/result.json"
        request_arn = f"arn:aws:s3:::{BUCKET}/{TRANSCRIPTIONS_PREFIX}*/request.json"
        # What the task itself calls: the scan and its run.json read, the transcription put at intake, the request
        # ended, and the pages read by converse, which never streams (ESP_IMAGE_ROUTE_STREAM stays unset).
        task_role = iam.Role(
            self, "TaskRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            inline_policies={f"{TRANSCRIPTION_NAME}-task": iam.PolicyDocument(statements=[
                iam.PolicyStatement(actions=["s3:GetObject"], resources=[
                    f"arn:aws:s3:::{BUCKET}/{INTAKE_PREFIX}*", f"arn:aws:s3:::{BUCKET}/{RUNS_PREFIX}*/run.json"]),
                iam.PolicyStatement(actions=["s3:PutObject"], resources=[
                    f"arn:aws:s3:::{BUCKET}/{INTAKE_PREFIX}*", result_arn]),
                iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=[PROFILE_ARN]),
                iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=MODEL_ARNS,
                                    conditions={"StringEquals": {"bedrock:InferenceProfileArn": PROFILE_ARN}}),
                iam.PolicyStatement(actions=["dynamodb:UpdateItem"], resources=[table.table_arn]),
            ])},
        )
        # What ECS calls to start the task: the image pulled and the log group written.  ecr:GetAuthorizationToken is
        # the stack's one "*": AWS grants it on nothing narrower.
        execution_role = iam.Role(
            self, "TaskExecutionRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            inline_policies={f"{TRANSCRIPTION_NAME}-execution": iam.PolicyDocument(statements=[
                iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]),
                iam.PolicyStatement(
                    actions=["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
                    resources=[task_image.repository.repository_arn]),
                iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                    resources=[task_log_group.log_group_arn]),
            ])},
        )
        # Each role is handed over closed to additions: the image and the log driver would each grant the execution
        # role its rights in a separate policy, and the statements above are those rights.
        task_definition = ecs.FargateTaskDefinition(
            self, "TaskDefinition", family=TRANSCRIPTION_NAME, cpu=1024, memory_limit_mib=2048,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.X86_64,
                                                 operating_system_family=ecs.OperatingSystemFamily.LINUX),
            task_role=task_role.without_policy_updates(), execution_role=execution_role.without_policy_updates(),
        )
        # The command is the image's own, python -m image_route.task.  The starter passes the request's four variables
        # as overrides.  Cache points are set on, and the region is set for boto3, which the task's S3 and DynamoDB
        # clients take from the environment.
        task_definition.add_container(
            TRANSCRIPTION_CONTAINER, image=ecs.ContainerImage.from_docker_image_asset(task_image),
            logging=ecs.LogDrivers.aws_logs(stream_prefix=TRANSCRIPTION_CONTAINER, log_group=task_log_group),
            environment={"ESP_BUCKET": BUCKET, "ESP_INTAKE_PREFIX": INTAKE_PREFIX, "ESP_RUNS_PREFIX": RUNS_PREFIX,
                         "ESP_TRANSCRIPTIONS_PREFIX": TRANSCRIPTIONS_PREFIX, "ESP_RUN_LOG_TABLE": TABLE_NAME,
                         "ESP_INFERENCE_PROFILE_ID": PROFILE_ID, "ESP_IMAGE_ROUTE_CACHE": "1",
                         "AWS_DEFAULT_REGION": self.region},
        )

        # The starter: each request.json under transcriptions/ starts one task, in the public subnets, with a public
        # address and the security group.  It may run this task's family in this cluster only, and pass the task's two
        # roles to ECS tasks only.  The right names the family, not the revision: while a deploy replaces the revision,
        # the function still starts the one before, and a right to the new one alone would refuse it.
        transcription_starter_log_group = logs.LogGroup(
            self, "TranscriptionStarterLogGroup", log_group_name=f"/aws/lambda/{TRANSCRIPTION_STARTER_NAME}",
            retention=logs.RetentionDays.THREE_MONTHS, removal_policy=RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
        )
        transcription_starter_role = iam.Role(
            self, "TranscriptionStarterRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={TRANSCRIPTION_STARTER_NAME: iam.PolicyDocument(statements=[
                iam.PolicyStatement(actions=["s3:GetObject"], resources=[request_arn]),
                iam.PolicyStatement(actions=["s3:PutObject"], resources=[result_arn]),
                iam.PolicyStatement(actions=["dynamodb:PutItem", "dynamodb:UpdateItem"], resources=[table.table_arn]),
                iam.PolicyStatement(
                    actions=["ecs:RunTask"],
                    resources=[f"arn:aws:ecs:{self.region}:{self.account}:task-definition/{TRANSCRIPTION_NAME}:*"],
                    conditions={"ArnEquals": {"ecs:cluster": cluster.cluster_arn}}),
                iam.PolicyStatement(actions=["iam:PassRole"],
                                    resources=[task_role.role_arn, execution_role.role_arn],
                                    conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}),
                iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                    resources=[transcription_starter_log_group.log_group_arn]),
            ])},
        )
        # The timeout covers the handler's three tries, each up to 25 s, and the 6 s it waits between them: 81 s, and
        # then the request's read and its row before them and the request's ending after.  Retried twice, as the
        # product's starter is: a retry finds the request's row and starts nothing, or starts the one task.
        transcription_starter = lambda_.DockerImageFunction(
            self, "TranscriptionStarter", function_name=TRANSCRIPTION_STARTER_NAME,
            architecture=lambda_.Architecture.X86_64, code=image("lambda_function.transcription_start_handler"),
            timeout=Duration.seconds(180), memory_size=2048, retry_attempts=2,
            role=transcription_starter_role, log_group=transcription_starter_log_group,
            environment={"ESP_BUCKET": BUCKET, "ESP_TRANSCRIPTIONS_PREFIX": TRANSCRIPTIONS_PREFIX,
                         "ESP_RUN_LOG_TABLE": TABLE_NAME,
                         "ESP_TRANSCRIPTION_CLUSTER": cluster.cluster_arn,
                         "ESP_TRANSCRIPTION_TASK_DEFINITION": task_definition.task_definition_arn,
                         "ESP_TRANSCRIPTION_CONTAINER": TRANSCRIPTION_CONTAINER,
                         "ESP_TRANSCRIPTION_SUBNETS": ",".join(subnet.subnet_id for subnet in vpc.public_subnets),
                         "ESP_TRANSCRIPTION_SECURITY_GROUPS": task_security_group.security_group_id,
                         "ESP_TRANSCRIPTION_ASSIGN_PUBLIC_IP": "ENABLED"},
        )
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(transcription_starter),
            s3.NotificationKeyFilter(prefix=TRANSCRIPTIONS_PREFIX, suffix="request.json"),
        )

        # The watcher: ECS tells it of each task of the route's cluster that stopped, and it ends the request when the
        # task had not.  It reads the request only when an event comes without the starter's overrides.
        transcription_watcher_log_group = logs.LogGroup(
            self, "TranscriptionWatcherLogGroup", log_group_name=f"/aws/lambda/{TRANSCRIPTION_WATCHER_NAME}",
            retention=logs.RetentionDays.THREE_MONTHS, removal_policy=RemovalPolicy.RETAIN_ON_UPDATE_OR_DELETE,
        )
        transcription_watcher_role = iam.Role(
            self, "TranscriptionWatcherRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            inline_policies={TRANSCRIPTION_WATCHER_NAME: iam.PolicyDocument(statements=[
                iam.PolicyStatement(actions=["s3:GetObject"], resources=[request_arn]),
                iam.PolicyStatement(actions=["s3:PutObject"], resources=[result_arn]),
                iam.PolicyStatement(actions=["dynamodb:UpdateItem"], resources=[table.table_arn]),
                iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                    resources=[transcription_watcher_log_group.log_group_arn]),
            ])},
        )
        # Retried twice: an ending is create-only, so a retry ends the request or finds it ended.
        transcription_watcher = lambda_.DockerImageFunction(
            self, "TranscriptionWatcher", function_name=TRANSCRIPTION_WATCHER_NAME,
            architecture=lambda_.Architecture.X86_64, code=image("lambda_function.transcription_watch_handler"),
            timeout=Duration.seconds(120), memory_size=2048, retry_attempts=2,
            role=transcription_watcher_role, log_group=transcription_watcher_log_group,
            environment={"ESP_BUCKET": BUCKET, "ESP_TRANSCRIPTIONS_PREFIX": TRANSCRIPTIONS_PREFIX,
                         "ESP_RUN_LOG_TABLE": TABLE_NAME},
        )
        events.Rule(
            self, "TaskStopped", rule_name=f"{TRANSCRIPTION_NAME}-stopped",
            description="Each task of the transcription cluster that stopped, to the watcher",
            event_pattern=events.EventPattern(source=["aws.ecs"], detail_type=["ECS Task State Change"],
                                              detail={"clusterArn": [cluster.cluster_arn], "lastStatus": ["STOPPED"]}),
            targets=[targets.LambdaFunction(transcription_watcher)],
        )
