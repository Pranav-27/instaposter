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
              if s["Action"] == "lambda:InvokeFunction"]
    assert len(invoke) == 1
    assert "Evaluator" in json.dumps(invoke[0]["Resource"]) and "Indexer" not in json.dumps(invoke[0]["Resource"])


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
    assert len(fns) == 2
    assert fns[0]["Code"]["ImageUri"] == fns[1]["Code"]["ImageUri"]
    manifest = json.load(open(os.path.join(outdir, "T.assets.json")))
    assert len(manifest["dockerImages"]) == 1
