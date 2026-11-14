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
def template():
    app = cdk.App(outdir=tempfile.mkdtemp())
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
