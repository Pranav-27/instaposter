import json
import os
import sys
import tempfile

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "infrastructure"))
from stacks.pipeline import PhotoSyncPipeline, load_zones  # noqa: E402

ZONES = [{"lat": 40.7, "lon": -74.0, "radius_km": 1}]


@pytest.fixture(scope="module")
def outdir():
    return tempfile.mkdtemp()


@pytest.fixture(scope="module")
def template(outdir):
    app = cdk.App(outdir=outdir)
    stack = PhotoSyncPipeline(app, "T", zones=json.dumps(ZONES), bucket="test-bucket", env=cdk.Environment(account="123456789012", region="us-east-1"))
    return Template.from_stack(stack)


@pytest.mark.parametrize("raw", [None, "", "[]"])
def test_empty_zones_fail(raw):
    with pytest.raises(ValueError, match="non-empty"):
        load_zones(raw)


def test_table(template):
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "TableName": "photo-sync",
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        "GlobalSecondaryIndexes": [Match.object_like({
            "IndexName": "gsi1",
            "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"}, {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        })],
    })
    template.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain"})


def test_lambda(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "PackageType": "Image",
        "Architectures": ["arm64"],
        "MemorySize": 2048,
        "Timeout": 180,
        "ImageConfig": {"Command": ["photo_sync.indexer.handler"]},
        "Environment": {"Variables": {
            "TABLE_NAME": "photo-sync",
            "BUCKET": "test-bucket",
            "TAGGING_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "INGEST_START_DATE": "2026-11-13",
            "HOME_ZONES_PARAM": "/photo-sync/home-zones",
        }},
    })
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {
        "MaximumRetryAttempts": 2,
        "DestinationConfig": {"OnFailure": {"Destination": Match.any_value()}},
    })


def test_dlq(template):
    template.has_resource_properties("AWS::SQS::Queue", {"QueueName": "photo-sync-indexer-dlq"})


def test_rule(template):
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {
            "source": ["aws.s3"],
            "detail-type": ["Object Created"],
            "detail": {
                "bucket": {"name": ["test-bucket"]},
                "object": {"key": [{"wildcard": "originals/*.heic"}, {"wildcard": "originals/*.HEIC"},
                                   {"wildcard": "originals/*.jp*g"}, {"wildcard": "originals/*.JP*G"}]},
            },
        },
    })


def test_zones_param(template):
    template.has_resource_properties("AWS::SSM::Parameter", {"Name": "/photo-sync/home-zones", "Value": json.dumps(ZONES)})


def test_alarms(template):
    template.has_resource_properties("AWS::CloudWatch::Alarm", {
        "MetricName": "Errors", "Namespace": "AWS/Lambda", "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "AlarmActions": [Match.string_like_regexp(r"arn:aws:sns:us-east-1:123456789012:photo-sync-cost-alerts")],
    })
    template.has_resource_properties("AWS::CloudWatch::Alarm", {
        "MetricName": "ApproximateNumberOfMessagesVisible", "Namespace": "AWS/SQS", "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "AlarmActions": [Match.string_like_regexp(r"photo-sync-cost-alerts")],
    })


def _statements(template):
    out = []
    for p in template.find_resources("AWS::IAM::Policy").values():
        out += p["Properties"]["PolicyDocument"]["Statement"]
    return out


def _actions(stmt):
    a = stmt["Action"]
    return a if isinstance(a, list) else [a]


def test_iam(template):
    stmts = _statements(template)
    actions = {a for s in stmts for a in _actions(s)}
    assert "s3:GetObject" in actions and "s3:PutObject" in actions
    assert {"ssm:GetParameter", "bedrock:InvokeModel", "geo-places:ReverseGeocode", "geo-places:Geocode", "dynamodb:PutItem", "dynamodb:Query"} <= actions
    assert "s3:*" not in actions and "dynamodb:*" not in actions
    blob = json.dumps(stmts)
    assert ":s3:::test-bucket/originals/*" in blob
    assert ":s3:::test-bucket/derived/*" in blob
    assert "inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0" in blob
    assert "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0" in blob
    assert "/index/*" in blob


def test_tags(template):
    template.has_resource_properties("AWS::DynamoDB::Table", {"Tags": Match.array_with([{"Key": "project", "Value": "photo-sync"}])})


def _evaluator_role_statements(template):
    fn = next(v for v in template.find_resources("AWS::Lambda::Function").values() if v["Properties"]["FunctionName"] == "photo-sync-evaluator")
    role = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    out = []
    for p in template.find_resources("AWS::IAM::Policy").values():
        if any(r["Ref"] == role for r in p["Properties"]["Roles"]):
            out += p["Properties"]["PolicyDocument"]["Statement"]
    return out


def test_evaluator_lambda(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "photo-sync-evaluator",
        "PackageType": "Image",
        "Architectures": ["arm64"],
        "MemorySize": 512,
        "Timeout": 120,
        "ImageConfig": {"Command": ["photo_sync.evaluator.handler"]},
        "Environment": {"Variables": {"TABLE_NAME": "photo-sync", "HOME_ZONES_PARAM": "/photo-sync/home-zones"}},
    })
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 0})


def test_evaluator_schedule(template):
    template.has_resource_properties("AWS::Scheduler::Schedule", {
        "Name": "photo-sync-evaluator-daily",
        "ScheduleExpression": "cron(0 9 * * ? *)",
        "ScheduleExpressionTimezone": "America/New_York",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {"Arn": Match.any_value(), "RoleArn": Match.any_value()},
    })
    template.has_resource_properties("AWS::IAM::Role", {
        "AssumeRolePolicyDocument": {"Statement": [Match.object_like({"Principal": {"Service": "scheduler.amazonaws.com"}})]},
    })
    invoke = [s for p in template.find_resources("AWS::IAM::Policy").values() for s in p["Properties"]["PolicyDocument"]["Statement"]
              if s["Action"] == "lambda:InvokeFunction" and "Evaluator" in json.dumps(s["Resource"])]
    assert len(invoke) == 1
    assert "Indexer" not in json.dumps(invoke[0]["Resource"])


def test_evaluator_iam(template):
    stmts = _evaluator_role_statements(template)
    actions = {a for s in stmts for a in _actions(s)}
    assert {"ssm:GetParameter", "dynamodb:PutItem", "dynamodb:Query"} <= actions
    assert not any(a.split(":")[0] in {"s3", "bedrock", "geo-places"} for a in actions)
    assert "dynamodb:*" not in actions
    blob = json.dumps(stmts)
    assert "parameter/photo-sync/home-zones" in blob and "parameter/photo-sync/*" not in blob
    assert "/index/*" in blob


def test_evaluator_alarm(template):
    template.has_resource_properties("AWS::CloudWatch::Alarm", {
        "AlarmName": "photo-sync-evaluator-errors", "MetricName": "Errors", "Namespace": "AWS/Lambda",
        "Threshold": 1, "Period": 300, "EvaluationPeriods": 1, "Statistic": "Sum",
        "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
        "AlarmActions": [Match.string_like_regexp(r"photo-sync-cost-alerts")],
    })


def test_single_image_asset(template, outdir):
    fns = [v["Properties"] for v in template.find_resources("AWS::Lambda::Function").values() if v["Properties"].get("PackageType") == "Image"]
    assert len(fns) == 4
    assert len({json.dumps(f["Code"]["ImageUri"], sort_keys=True) for f in fns}) == 1
    manifest = json.load(open(os.path.join(outdir, "T.assets.json")))
    assert len(manifest["dockerImages"]) == 1


def _fn(template, name):
    return next(v for v in template.find_resources("AWS::Lambda::Function").values() if v["Properties"]["FunctionName"] == name)


def _role_statements(template, name):
    role = _fn(template, name)["Properties"]["Role"]["Fn::GetAtt"][0]
    out = []
    for p in template.find_resources("AWS::IAM::Policy").values():
        if any(r["Ref"] == role for r in p["Properties"]["Roles"]):
            out += p["Properties"]["PolicyDocument"]["Statement"]
    return out


def _curation_env():
    return {
        "TABLE_NAME": "photo-sync", "BUCKET": "test-bucket",
        "CURATOR_MODEL": "us.anthropic.claude-opus-4-6-v1", "CAPTION_MODEL": "us.anthropic.claude-opus-4-6-v1",
        "TAGGING_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "BOT_FUNCTION": "photo-sync-bot",
        "HOME_PLACE_NAMES": "",
    }


@pytest.mark.parametrize("name,cmd", [("photo-sync-curator", "photo_sync.curator.handler"), ("photo-sync-bot", "photo_sync.bot.handler")])
def test_curation_lambdas(template, name, cmd):
    template.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": name, "PackageType": "Image", "Architectures": ["arm64"],
        "MemorySize": 2048, "Timeout": 300,
        "ImageConfig": {"Command": [cmd]},
        "Environment": {"Variables": _curation_env()},
    })
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {
        "FunctionName": {"Ref": Match.string_like_regexp(name == "photo-sync-bot" and "Bot" or "Curator")},
        "MaximumRetryAttempts": 0,
    })
    assert "ReservedConcurrentExecutions" not in _fn(template, name)["Properties"]


def test_bot_function_url(template):
    template.has_resource_properties("AWS::Lambda::Url", {"AuthType": "NONE", "TargetFunctionArn": Match.any_value()})
    urls = template.find_resources("AWS::Lambda::Url")
    assert len(urls) == 1 and "Cors" not in next(iter(urls.values()))["Properties"]
    template.has_output("BotFunctionUrl", {})


def test_bot_sns_subscription(template):
    template.has_resource_properties("AWS::SNS::Subscription", {
        "Protocol": "lambda",
        "TopicArn": Match.string_like_regexp(r"photo-sync-cost-alerts"),
        "Endpoint": Match.any_value(),
    })
    template.has_resource_properties("AWS::Lambda::Permission", {
        "Action": "lambda:InvokeFunction", "Principal": "sns.amazonaws.com",
        "FunctionName": Match.any_value(),
    })


def test_bot_self_invoke(template):
    invoke = [s for s in _role_statements(template, "photo-sync-bot") if "lambda:InvokeFunction" in _actions(s)]
    assert len(invoke) == 1
    assert "function:photo-sync-bot" in json.dumps(invoke[0]["Resource"])
    assert "photo-sync-curator" not in json.dumps(invoke[0]["Resource"])


@pytest.mark.parametrize("name", ["photo-sync-curator", "photo-sync-bot"])
def test_curation_iam(template, name):
    stmts = _role_statements(template, name)
    actions = {a for s in stmts for a in _actions(s)}
    assert {s.split(":")[0] for s in actions} <= {"s3", "dynamodb", "ssm", "bedrock", "lambda", "logs", "xray"}
    assert "geo-places:ReverseGeocode" not in actions and "dynamodb:*" not in actions and "s3:*" not in actions
    s3_stmts = [s for s in stmts if any(a.startswith("s3:") for a in _actions(s))]
    assert len(s3_stmts) == 2
    get = next(s for s in s3_stmts if _actions(s) == ["s3:GetObject"])
    put = next(s for s in s3_stmts if _actions(s) == ["s3:PutObject"])
    assert len(get["Resource"]) == 2
    assert ":s3:::test-bucket/originals/*" in json.dumps(get["Resource"]) and ":s3:::test-bucket/derived/*" in json.dumps(get["Resource"])
    assert ":s3:::test-bucket/renders/*" in json.dumps(put["Resource"])
    assert "originals" not in json.dumps(put["Resource"]) and "renders" not in json.dumps(get["Resource"])
    ssm_stmts = [s for s in stmts if any(a.startswith("ssm:") for a in _actions(s))]
    assert len(ssm_stmts) == 1 and _actions(ssm_stmts[0]) == ["ssm:GetParameter"]
    assert "parameter/photo-sync/telegram/*" in json.dumps(ssm_stmts[0]["Resource"])
    bedrock = next(s for s in stmts if "bedrock:InvokeModel" in _actions(s))
    blob = json.dumps(bedrock["Resource"])
    for needle in (
        "inference-profile/us.anthropic.claude-opus-4-6-v1", "inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-6-v1", "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    ):
        assert needle in blob
    assert len(bedrock["Resource"]) == 4
    assert "/index/*" in json.dumps(stmts)


def test_evaluator_curator_wiring(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "photo-sync-evaluator",
        "Environment": {"Variables": {"TABLE_NAME": "photo-sync", "HOME_ZONES_PARAM": "/photo-sync/home-zones", "CURATOR_FUNCTION": "photo-sync-curator"}},
    })
    stmts = _evaluator_role_statements(template)
    invoke = [s for s in stmts if "lambda:InvokeFunction" in _actions(s)]
    assert len(invoke) == 1 and "Curator" in json.dumps(invoke[0]["Resource"]) and "Bot" not in json.dumps(invoke[0]["Resource"])
    ssm_resources = json.dumps([s["Resource"] for s in stmts if "ssm:GetParameter" in _actions(s)])
    assert "parameter/photo-sync/telegram/*" in ssm_resources and "parameter/photo-sync/home-zones" in ssm_resources
    assert "parameter/photo-sync/*" not in ssm_resources
    actions = {a for s in stmts for a in _actions(s)}
    assert not any(a.split(":")[0] in {"s3", "bedrock", "geo-places"} for a in actions)


def test_ttl(template):
    template.has_resource_properties("AWS::DynamoDB::Table", {"TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True}})


@pytest.mark.parametrize("name", ["photo-sync-curator-errors", "photo-sync-bot-errors"])
def test_curation_alarms(template, name):
    template.has_resource_properties("AWS::CloudWatch::Alarm", {
        "AlarmName": name, "MetricName": "Errors", "Namespace": "AWS/Lambda",
        "Threshold": 1, "Period": 300, "EvaluationPeriods": 1, "Statistic": "Sum",
        "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
        "AlarmActions": [Match.string_like_regexp(r"photo-sync-cost-alerts")],
    })
