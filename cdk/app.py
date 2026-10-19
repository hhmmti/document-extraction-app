#!/usr/bin/env python3
import os
import sys

import aws_cdk as cdk

from esp_design_extraction.esp_design_extraction_stack import EspDesignExtractionStack

# As the ESP monorepo's CDK apps do; the deploy workflow sets both from its DS_* secrets.
account, region = os.environ.get("AWS_ACCOUNT_NUMBER"), os.environ.get("AWS_DEFAULT_REGION")
if not (account and region):
    sys.exit("app.py: set AWS_ACCOUNT_NUMBER and AWS_DEFAULT_REGION (the target account and region)")

app = cdk.App()
EspDesignExtractionStack(
    app,
    "EspDesignExtractionStack",
    env=cdk.Environment(account=account, region=region),
)

app.synth()
