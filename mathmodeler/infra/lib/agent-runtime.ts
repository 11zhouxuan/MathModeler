import * as path from 'path';
import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Platform } from 'aws-cdk-lib/aws-ecr-assets';
import { CfnRuntime } from 'aws-cdk-lib/aws-bedrockagentcore';

const REPO_ROOT = path.join(__dirname, '..', '..'); // mathmodeler/

export interface AgentRuntimeProps {
  /** Logical Runtime name (e.g. mm-analyst). */
  name: string;
  /** Agent dir under mathmodeler/agents (e.g. analyst). Docker build context = repo root. */
  agentDir: string;
  bucket: s3.IBucket;
  memory: agentcore.Memory;
  /** Extra env vars (e.g. sub-agent ARNs for the Orchestrator). */
  extraEnv?: { [k: string]: string };
  /** Solver needs the Code Interpreter permissions. */
  needsCodeInterpreter?: boolean;
}

/**
 * One AgentCore Runtime + its execution role (tech-design §7.2).
 *
 * Docker build context is the monorepo root so the image can ``COPY common`` and
 * ``COPY HMML``; the per-agent Dockerfile lives at agents/<dir>/Dockerfile.
 */
export class AgentRuntime extends Construct {
  public readonly arn: string;
  public readonly role: iam.Role;

  constructor(scope: Construct, id: string, props: AgentRuntimeProps) {
    super(scope, id);

    this.role = new iam.Role(this, 'ExecRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
    });

    // Bedrock inference: claude-opus via a cross-region inference profile +
    // the Nova MME embedding model (us-east-1). A cross-region inference
    // profile fans out to the underlying foundation-model in MULTIPLE regions
    // (us-east-1/us-east-2/us-west-2/...), so the foundation-model resource and
    // the inference-profile resource must allow all regions (``bedrock:*``),
    // not just us-west-2 — otherwise ConverseStream is AccessDenied when the
    // profile routes to a peer region.
    this.role.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [
        'arn:aws:bedrock:*::foundation-model/anthropic.claude-*',
        'arn:aws:bedrock:*:*:inference-profile/*',
        'arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0',
      ],
    }));


    // S3 document bus.
    props.bucket.grantReadWrite(this.role);

    // AgentCore Memory read/write.
    this.role.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:RetrieveMemoryRecords',
      ],
      resources: [props.memory.memoryArn],
    }));

    // Code Interpreter (Solver only).
    if (props.needsCodeInterpreter) {
      this.role.addToPolicy(new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:StartCodeInterpreterSession',
          'bedrock-agentcore:InvokeCodeInterpreter',
          'bedrock-agentcore:StopCodeInterpreterSession',
        ],
        resources: ['*'],
      }));
    }

    // CloudWatch Logs.
    this.role.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchLogsFullAccess'),
    );

    const runtime = new agentcore.Runtime(this, 'Runtime', {
      runtimeName: props.name,
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromAsset(REPO_ROOT, {
        platform: Platform.LINUX_ARM64,
        file: path.join('agents', props.agentDir, 'Dockerfile'),
      }),
      executionRole: this.role,
      environmentVariables: {
        AWS_REGION: 'us-west-2',
        // Claude Opus requires a cross-region inference profile (on-demand
        // foundation-model id is rejected by ConverseStream).
        MODEL_ID: 'us.anthropic.claude-opus-4-6-v1',
        DOC_BUCKET: props.bucket.bucketName,

        MEMORY_ID: props.memory.memoryId,
        // Workspace root for session artifacts (Managed filesystem in Runtime).
        MM_WORKSPACE_ROOT: "/mnt/workspace/jobs",
        ...(props.extraEnv ?? {}),
      },
    });

    // Escape hatch: L2 construct doesn't expose these yet.
    const cfnRuntime = runtime.node.defaultChild as CfnRuntime;
    cfnRuntime.addPropertyOverride('FilesystemConfigurations', [
      { SessionStorage: { MountPath: '/mnt/workspace' } },
    ]);
    cfnRuntime.addPropertyOverride('LifecycleConfiguration', {
      IdleRuntimeSessionTimeout: 28800,
      MaxLifetime: 28800,
    });

    this.arn = runtime.agentRuntimeArn;
  }
}
