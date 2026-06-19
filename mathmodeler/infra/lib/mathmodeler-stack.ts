import { Construct } from 'constructs';
import { Stack, StackProps, RemovalPolicy, Duration, CfnOutput } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';

import { AgentRuntime } from './agent-runtime';
import { PortalFargate } from './portal/fargate';

/**
 * Top-level MathModeler stack (tech-design §7; agents-as-tools §4/§8):
 * S3 doc bus + AgentCore Memory + ONE Runtime (the merged Orchestrator that runs
 * the supervisor and all four sub-agents in-process via agents-as-tools) +
 * portal on ALB + Fargate (FastAPI, real-time SSE). One ``cdk deploy`` provisions
 * all.
 *
 * The four sub-agents are no longer separate Runtimes: ``mm_common.tools._dispatch``
 * runs them in-process (``MM_INPROCESS=1``, the image default). The legacy
 * per-agent Dockerfiles/app.py remain in the repo for the ``MM_INPROCESS=0``
 * fallback but are not provisioned here.
 */


export class MathModelerStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // --- S3 document bus (§7.4) ---
    const bucket = new s3.Bucket(this, 'DocBus', {
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    // --- AgentCore Memory (§7.4) ---
    const memory = new agentcore.Memory(this, 'Memory', {
      memoryName: 'MathModelerMemory',
      description: 'short-term subtask conclusions + long-term user preferences',
      expirationDuration: Duration.days(30),
    });

    // --- Single merged Runtime (agents-as-tools §4/§8) ---
    // The Orchestrator supervisor + all four sub-agents run in ONE process.
    // It needs the Code Interpreter (Solver) permissions; the merged image
    // carries HMML + bedrock-agentcore + numpy. No sub-agent ARNs and no
    // cross-Runtime InvokeAgentRuntime permission are required (in-process).
    const orchestrator = new AgentRuntime(this, 'Orchestrator', {
      name: 'mm_orchestrator', agentDir: 'orchestrator', bucket, memory,
      needsCodeInterpreter: true,
      extraEnv: {
        MM_INPROCESS: '1',
        MM_ORCHESTRATION: 'supervisor',
      },
    });


    // --- DynamoDB: Chat History (cross-browser session persistence) ---
    const chatHistoryTable = new dynamodb.Table(this, 'ChatHistory', {
      tableName: 'MathModeler-ChatHistory',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // --- Portal: ALB + Fargate (FastAPI, real-time SSE; tech-design §7.5/§8.2) ---
    // P1 admin credentials are supplied at deploy time via CDK context:
    //   cdk deploy -c adminUser=admin -c adminPassword=<secret>
    const adminUser = (this.node.tryGetContext('adminUser') as string) || 'admin';
    const adminPassword = (this.node.tryGetContext('adminPassword') as string) || '';
    if (!adminPassword) {
      throw new Error(
        'Portal admin password is required. Deploy with: ' +
        'cdk deploy -c adminUser=<user> -c adminPassword=<password>',
      );
    }
    const portal = new PortalFargate(this, 'Portal', {
      orchestratorArn: orchestrator.arn,
      bucket,
      memory,
      chatHistoryTable,
      adminUser,
      adminPassword,
    });

    // --- Outputs ---
    new CfnOutput(this, 'DocBucketName', { value: bucket.bucketName });
    new CfnOutput(this, 'MemoryId', { value: memory.memoryId });
    new CfnOutput(this, 'OrchestratorArn', { value: orchestrator.arn });
    new CfnOutput(this, 'ChatHistoryTableName', { value: chatHistoryTable.tableName });
    new CfnOutput(this, 'PortalEndpoint', { value: portal.url });
  }
}

