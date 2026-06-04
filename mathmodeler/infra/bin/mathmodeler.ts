#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { MathModelerStack } from '../lib/mathmodeler-stack';

const app = new cdk.App();

new MathModelerStack(app, 'MathModelerStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'us-west-2',
  },
  description: 'MathModeler — multi-agent mathematical modeling system on Amazon Bedrock AgentCore',
});

app.synth();
