import json
import os

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from aws_cdk.aws_lambda_destinations import SqsDestination
from constructs import Construct

TOPIC = "photo-sync-cost-alerts"
TABLE = "photo-sync"
MODEL_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"
PROFILE_ID = f"us.{MODEL_ID}"
ZONES_PARAM = "/photo-sync/home-zones"
CURATOR = "photo-sync-curator"
BOT = "photo-sync-bot"
OPUS_ID = "anthropic.claude-opus-4-6-v1"
OPUS_PROFILE = f"us.{OPUS_ID}"
TELEGRAM_PARAM = "/photo-sync/telegram/*"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEY_PATTERNS = ["originals/*.heic", "originals/*.HEIC", "originals/*.jp*g", "originals/*.JP*G"]


def load_zones(raw: str | None) -> str:
    zones = json.loads(raw) if raw else []
    if not isinstance(zones, list) or not zones:
        raise ValueError("HOME_ZONES must be a non-empty JSON list of {lat, lon, radius_km}; set it in .env before synth/deploy")
    for z in zones:
        if set(z) != {"lat", "lon", "radius_km"}:
            raise ValueError(f"invalid home zone {z!r}; expected keys lat, lon, radius_km")
    return json.dumps(zones)


class PhotoSyncPipeline(cdk.Stack):
    def __init__(self, scope: Construct, id: str, zones: str | None = None, bucket: str | None = None, **kwargs):
        super().__init__(scope, id, **kwargs)
        cdk.Tags.of(self).add("project", "photo-sync")
        zones = load_zones(zones or os.environ.get("HOME_ZONES"))
        bucket_name = bucket or os.environ["BUCKET"]

        bucket = s3.Bucket.from_bucket_name(self, "Bucket", bucket_name)
        topic = sns.Topic.from_topic_arn(self, "AlertsTopic", f"arn:aws:sns:{self.region}:{self.account}:{TOPIC}")

        table = ddb.Table(
            self, "Table",
            table_name=TABLE,
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )
        table.add_global_secondary_index(
            index_name="gsi1",
            partition_key=ddb.Attribute(name="gsi1pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="gsi1sk", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )

        dlq = sqs.Queue(self, "IndexerDlq", queue_name="photo-sync-indexer-dlq", retention_period=cdk.Duration.days(14))

        ssm.StringParameter(self, "HomeZones", parameter_name=ZONES_PARAM, string_value=zones)

        image = aws_ecr_assets.DockerImageAsset(self, "Image", directory=ROOT, platform=aws_ecr_assets.Platform.LINUX_ARM64)

        def image_code(handler: str) -> _lambda.DockerImageCode:
            return _lambda.DockerImageCode.from_ecr(image.repository, tag_or_digest=image.asset_hash, cmd=[handler])

        fn = _lambda.DockerImageFunction(
            self, "Indexer",
            function_name="photo-sync-indexer",
            code=image_code("photo_sync.indexer.handler"),
            architecture=_lambda.Architecture.ARM_64,
            memory_size=2048,
            timeout=cdk.Duration.seconds(180),
            retry_attempts=2,
            on_failure=SqsDestination(dlq),
            environment={
                "TABLE_NAME": TABLE,
                "BUCKET": bucket_name,
                "TAGGING_MODEL": PROFILE_ID,
                "INGEST_START_DATE": "2026-11-13",
                "HOME_ZONES_PARAM": ZONES_PARAM,
            },
        )

        fn.add_to_role_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[bucket.arn_for_objects("originals/*")]))
        fn.add_to_role_policy(iam.PolicyStatement(actions=["s3:PutObject"], resources=[bucket.arn_for_objects("derived/*")]))
        table.grant_read_write_data(fn)
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/photo-sync/*"],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{PROFILE_ID}",
                f"arn:aws:bedrock:*::foundation-model/{MODEL_ID}",
            ],
        ))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["geo-places:ReverseGeocode", "geo-places:Geocode"],
            resources=["*"],
        ))

        events.Rule(
            self, "OriginalsCreated",
            rule_name="photo-sync-originals-created",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [bucket_name]},
                    "object": {"key": [{"wildcard": p} for p in KEY_PATTERNS]},
                },
            ),
            targets=[targets.LambdaFunction(fn)],
        )

        def ssm_telegram() -> iam.PolicyStatement:
            return iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{TELEGRAM_PARAM}"],
            )

        def curation_env() -> dict:
            return {
                "TABLE_NAME": TABLE,
                "BUCKET": bucket_name,
                "CURATOR_MODEL": OPUS_PROFILE,
                "CAPTION_MODEL": OPUS_PROFILE,
                "TAGGING_MODEL": PROFILE_ID,
                "BOT_FUNCTION": BOT,
                "HOME_PLACE_NAMES": os.environ.get("HOME_PLACE_NAMES", ""),
            }

        def grant_curation(f: _lambda.Function) -> None:
            f.add_to_role_policy(iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[bucket.arn_for_objects("originals/*"), bucket.arn_for_objects("derived/*")],
            ))
            f.add_to_role_policy(iam.PolicyStatement(actions=["s3:PutObject"], resources=[bucket.arn_for_objects("renders/*")]))
            table.grant_read_write_data(f)
            f.add_to_role_policy(ssm_telegram())
            f.add_to_role_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    *[f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{p}" for p in (OPUS_PROFILE, PROFILE_ID)],
                    *[f"arn:aws:bedrock:*::foundation-model/{m}" for m in (OPUS_ID, MODEL_ID)],
                ],
            ))

        curator = _lambda.DockerImageFunction(
            self, "Curator",
            function_name=CURATOR,
            code=image_code("photo_sync.curator.handler"),
            architecture=_lambda.Architecture.ARM_64,
            memory_size=2048,
            timeout=cdk.Duration.seconds(300),
            retry_attempts=0,
            environment=curation_env(),
        )
        grant_curation(curator)

        bot = _lambda.DockerImageFunction(
            self, "Bot",
            function_name=BOT,
            code=image_code("photo_sync.bot.handler"),
            architecture=_lambda.Architecture.ARM_64,
            memory_size=2048,
            timeout=cdk.Duration.seconds(300),
            retry_attempts=0,
            environment=curation_env(),
        )
        grant_curation(bot)
        bot.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:{BOT}"],
        ))
        bot_url = bot.add_function_url(auth_type=_lambda.FunctionUrlAuthType.NONE)
        cdk.CfnOutput(self, "BotFunctionUrl", value=bot_url.url)
        topic.add_subscription(subs.LambdaSubscription(bot))

        evaluator = _lambda.DockerImageFunction(
            self, "Evaluator",
            function_name="photo-sync-evaluator",
            code=image_code("photo_sync.evaluator.handler"),
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            timeout=cdk.Duration.seconds(120),
            retry_attempts=0,
            environment={"TABLE_NAME": TABLE, "HOME_ZONES_PARAM": ZONES_PARAM, "CURATOR_FUNCTION": CURATOR},
        )
        table.grant_read_write_data(evaluator)
        evaluator.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{ZONES_PARAM}"],
        ))

        evaluator.add_to_role_policy(ssm_telegram())
        evaluator.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[curator.function_arn],
        ))

        scheduler_role = iam.Role(self, "EvaluatorSchedulerRole", assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))
        evaluator.grant_invoke(scheduler_role)
        scheduler.CfnSchedule(
            self, "EvaluatorDaily",
            name="photo-sync-evaluator-daily",
            schedule_expression="cron(0 9 * * ? *)",
            schedule_expression_timezone="America/New_York",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(arn=evaluator.function_arn, role_arn=scheduler_role.role_arn),
        )

        action = cw_actions.SnsAction(topic)
        errors = fn.metric_errors(period=cdk.Duration.minutes(5), statistic="Sum").create_alarm(
            self, "IndexerErrors", alarm_name="photo-sync-indexer-errors", threshold=1,
            evaluation_periods=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        depth = dlq.metric_approximate_number_of_messages_visible(period=cdk.Duration.minutes(5), statistic="Maximum").create_alarm(
            self, "DlqDepth", alarm_name="photo-sync-indexer-dlq-depth", threshold=1,
            evaluation_periods=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        evaluator_errors = evaluator.metric_errors(period=cdk.Duration.minutes(5), statistic="Sum").create_alarm(
            self, "EvaluatorErrors", alarm_name="photo-sync-evaluator-errors", threshold=1,
            evaluation_periods=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        curator_errors, bot_errors = (
            f.metric_errors(period=cdk.Duration.minutes(5), statistic="Sum").create_alarm(
                self, cid, alarm_name=name, threshold=1,
                evaluation_periods=1, comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            for f, cid, name in ((curator, "CuratorErrors", "photo-sync-curator-errors"), (bot, "BotErrors", "photo-sync-bot-errors"))
        )
        for alarm in (errors, depth, evaluator_errors, curator_errors, bot_errors):
            alarm.add_alarm_action(action)
