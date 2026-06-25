import { Construct } from 'constructs';
import { Stack, StackProps, RemovalPolicy, Duration, CfnOutput, Aws } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3files from 'aws-cdk-lib/aws-s3files';
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
    // Note: versioning is enforced by S3 Files (cannot be toggled once attached).
    // Do NOT set versioned:true here — it would trigger a no-op update that S3
    // rejects with "versioning state cannot be changed".
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

    // --- VPC + S3 Files (workspace filesystem) ---
    // Use default VPC (public subnets have internet; no NAT Gateway cost).
    const vpc = ec2.Vpc.fromLookup(this, 'DefaultVpc', { isDefault: true });

    const s3filesSg = new ec2.SecurityGroup(this, 'S3FilesSG', {
      vpc,
      description: 'SG for S3 Files mount targets + AgentCore Runtime',
      allowAllOutbound: true,
    });

    // S3 Files service role — assumes elasticfilesystem.amazonaws.com to sync
    // data between the file system and the backing S3 bucket.
    const s3filesRole = new iam.Role(this, 'S3FilesRole', {
      assumedBy: new iam.ServicePrincipal('elasticfilesystem.amazonaws.com'),
    });
    s3filesRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket*'],
      resources: [bucket.bucketArn],
    }));
    s3filesRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        's3:AbortMultipartUpload', 's3:DeleteObject', 's3:GetObject*',
        's3:List*', 's3:PutObject*',
      ],
      resources: [bucket.arnForObjects('*')],
    }));
    s3filesRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'events:DeleteRule', 'events:DisableRule', 'events:EnableRule',
        'events:PutRule', 'events:PutTargets', 'events:RemoveTargets',
      ],
      resources: [`arn:${Aws.PARTITION}:events:*:*:rule/DO-NOT-DELETE-S3-Files*`],
      conditions: { StringEquals: { 'events:ManagedBy': 'elasticfilesystem.amazonaws.com' } },
    }));
    s3filesRole.addToPolicy(new iam.PolicyStatement({
      actions: ['events:DescribeRule', 'events:ListRuleNamesByTarget', 'events:ListRules', 'events:ListTargetsByRule'],
      resources: [`arn:${Aws.PARTITION}:events:*:*:rule/*`],
    }));

    // S3 Files filesystem backed by our bucket (prefix scoped to "jobs/").
    const fileSystem = new s3files.CfnFileSystem(this, 'WorkspaceFS', {
      bucket: bucket.bucketArn,
      roleArn: s3filesRole.roleArn,
      acceptBucketWarning: true,
    });

    // Mount targets — one per public subnet (max 3 to stay within limits).
    // Mount targets take 1-3 minutes to become available; the Runtime must
    // wait for them via an explicit dependency.
    const subnets = vpc.publicSubnets.slice(0, 3);
    const mountTargets = subnets.map((subnet, i) =>
      new s3files.CfnMountTarget(this, `MountTarget${i}`, {
        fileSystemId: fileSystem.attrFileSystemId,
        subnetId: subnet.subnetId,
        securityGroups: [s3filesSg.securityGroupId],
      }),
    );

    // Access point — root user, root directory at /
    const accessPoint = new s3files.CfnAccessPoint(this, 'WorkspaceAP', {
      fileSystemId: fileSystem.attrFileSystemId,
      posixUser: { gid: '0', uid: '0' },
      rootDirectory: {
        path: '/',
        creationPermissions: { ownerGid: '0', ownerUid: '0', permissions: '755' },
      },
    });
    // Access point depends on mount targets being ready
    mountTargets.forEach(mt => accessPoint.addDependency(mt));

    // --- Single merged Runtime (agents-as-tools §4/§8) ---
    // The Orchestrator supervisor + all four sub-agents run in ONE process.
    // It needs the Code Interpreter (Solver) permissions; the merged image
    // carries HMML + bedrock-agentcore + numpy. No sub-agent ARNs and no
    // cross-Runtime InvokeAgentRuntime permission are required (in-process).
    // Optional: user can pass an existing S3 Files access point ARN via context
    // to skip creating new S3 Files resources. Useful for reusing an existing filesystem.
    const externalAccessPointArn = this.node.tryGetContext('s3FilesAccessPointArn') as string | undefined;
    const effectiveAccessPointArn = externalAccessPointArn || accessPoint.attrAccessPointArn;

    const orchestrator = new AgentRuntime(this, 'Orchestrator', {
      name: 'mm_orchestrator', agentDir: 'orchestrator', bucket, memory,
      needsCodeInterpreter: true,
      vpc,
      securityGroup: s3filesSg,
      s3FilesAccessPointArn: effectiveAccessPointArn,
      extraEnv: {
        MM_INPROCESS: '1',
        MM_ORCHESTRATION: 'supervisor',
      },
    });
    // Runtime must wait until mount targets + access point are fully available.
    if (!externalAccessPointArn) {
      orchestrator.node.addDependency(accessPoint);
    }


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

