import os

import aws_cdk as cdk

for line in open(os.path.join(os.path.dirname(__file__), "..", ".env")) if os.path.exists(os.path.join(os.path.dirname(__file__), "..", ".env")) else []:
    key, sep, value = line.strip().partition("=")
    if sep and not key.startswith("#"):
        os.environ.setdefault(key.strip(), value.strip())

from stacks.pipeline import PhotoSyncPipeline

app = cdk.App()
PhotoSyncPipeline(
    app,
    "PhotoSyncPipeline",
    env=cdk.Environment(account=os.environ["CDK_DEFAULT_ACCOUNT"], region="us-east-1"),
)
app.synth()
