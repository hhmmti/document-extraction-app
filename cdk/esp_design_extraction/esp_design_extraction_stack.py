"""The design-extraction Lambda, its role, its S3 trigger, its log group and its run-log table."""

from pathlib import Path

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
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
FUNCTION_NAME = "esp-design-extraction"
TABLE_NAME = "esp_design_extraction_logging"         # in the layout of the ESP product's esp_logging_v2
PROFILE_ID = "us.anthropic.claude-opus-5-5"
PROFILE_ARN = f"arn:aws:bedrock:us-east-1:640168431387:inference-profile/{PROFILE_ID}"
# The regions the US profile routes to; a request may land in any of them.
MODEL_ARNS = [f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-opus-5-5"
              for region in ("us-east-1", "us-east-2", "us-west-2")]
BUILD_DIR = Path(__file__).resolve().parents[1] / "build" / "lambda"     # the image context build_lambda.py stages

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
            removal_policy=RemovalPolicy.RETAIN,
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
            retention=logs.RetentionDays.THREE_MONTHS, removal_policy=RemovalPolicy.RETAIN,
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

        function = lambda_.DockerImageFunction(
            self, "Function", function_name=FUNCTION_NAME, architecture=lambda_.Architecture.X86_64,
            code=lambda_.DockerImageCode.from_image_asset(
                str(code_dir), platform=ecr_assets.Platform.LINUX_AMD64, cmd=["lambda_function.lambda_handler"]),
            timeout=Duration.seconds(900), memory_size=2048, reserved_concurrent_executions=1, retry_attempts=0,
            role=role, log_group=log_group, environment=ENVIRONMENT,
        )

        # The Lambda writes only under runs/, so its own output cannot trigger it.
        s3.Bucket.from_bucket_name(self, "Bucket", BUCKET).add_event_notification(
            s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(function),
            s3.NotificationKeyFilter(prefix=INTAKE_PREFIX, suffix=".pdf"),
        )
