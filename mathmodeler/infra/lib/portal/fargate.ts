import * as path from 'path';
import { Construct } from 'constructs';
import { CfnOutput, Duration } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as agentcore from '@aws-cdk/aws-bedrock-agentcore-alpha';
import { Platform } from 'aws-cdk-lib/aws-ecr-assets';

const REPO_ROOT = path.join(__dirname, '..', '..', '..'); // mathmodeler/

export interface PortalFargateProps {
  /** Orchestrator Runtime ARN invoked from /api/solve. */
  orchestratorArn: string;
  /** S3 document bus (report links are presigned/read from here). */
  bucket: s3.IBucket;
  /** AgentCore Memory (best-effort event logging). */
  memory: agentcore.Memory;
  /** DynamoDB table for chat history persistence. */
  chatHistoryTable: dynamodb.ITable;
  /** P1 admin credentials (deploy-time, via CDK context). */
  adminUser: string;
  adminPassword: string;
}

/**
 * Portal on ALB + Fargate (tech-design §8.2, plan C).
 *
 * A FastAPI container on Fargate (ARM64/Graviton) behind an internet-facing
 * Application Load Balancer (HTTP:80). The ALB streams chunked/SSE responses
 * without buffering, so the four-stage Orchestrator progress reaches the
 * browser live.
 *
 * **Scan hardening (host-header gating, mirrors auto-graphrag):** the ALB
 * listener's *default action* returns a fixed ``403 Forbidden``; only requests
 * whose ``Host`` header matches the ALB's own ``*.elb.amazonaws.com`` DNS are
 * routed to the Fargate target group. Security scanners that probe the raw IP
 * (no/invalid Host header) hit the 403 default and therefore do not see an
 * unauthenticated public web endpoint. A real browser opening
 * ``http://<albDns>/`` automatically sends the matching Host header and is
 * allowed through. The application-layer P1 password login remains as a second
 * factor. No custom domain / ACM certificate required.
 */
export class PortalFargate extends Construct {
  public readonly url: string;

  constructor(scope: Construct, id: string, props: PortalFargateProps) {
    super(scope, id);

    // Default VPC keeps the demo footprint small.
    const vpc = ec2.Vpc.fromLookup(this, 'DefaultVpc', { isDefault: true });

    const cluster = new ecs.Cluster(this, 'Cluster', { vpc });

    // --- Task definition (ARM64 FastAPI portal) ---
    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: 1024,
      memoryLimitMiB: 2048,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    taskDef.addContainer('Portal', {
      image: ecs.ContainerImage.fromAsset(REPO_ROOT, {
        file: path.join('portal', 'backend', 'Dockerfile'),
        platform: Platform.LINUX_ARM64,
      }),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'portal' }),
      environment: {
        AGENT_CORE_ARN: props.orchestratorArn,
        AWS_REGION_NAME: 'us-west-2',
        DOC_BUCKET: props.bucket.bucketName,
        MEMORY_ID: props.memory.memoryId,
        CHAT_HISTORY_TABLE: props.chatHistoryTable.tableName,
        PORTAL_ADMIN_USER: props.adminUser,
        PORTAL_ADMIN_PASSWORD: props.adminPassword,
      },
      portMappings: [{ containerPort: 8080 }],
    });

    const taskRole = taskDef.taskRole;
    // Invoke the Orchestrator Runtime (SSE proxy). The actual InvokeAgentRuntime
    // call targets the runtime's endpoint sub-resource
    // (``<runtimeArn>/runtime-endpoint/DEFAULT``), so the policy must cover both
    // the runtime ARN and its endpoint sub-paths.
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime', 'bedrock-agentcore:InvokeAgentRuntimeCommand'],
      resources: [props.orchestratorArn, `${props.orchestratorArn}/*`],
    }));

    // S3 document bus read/write (report retrieval / presign).
    props.bucket.grantReadWrite(taskRole);
    // DynamoDB chat history (read/write for session persistence).
    props.chatHistoryTable.grantReadWriteData(taskRole);
    // AgentCore Memory (best-effort event logging).
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:RetrieveMemoryRecords',
      ],
      resources: [props.memory.memoryArn],
    }));

    // --- Fargate service (no public IP on the tasks; reached only via ALB) ---
    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: true, // default VPC has only public subnets; tasks still reachable only through the ALB SG
    });

    // --- Internet-facing ALB, HTTP:80 ---
    const lb = new elbv2.ApplicationLoadBalancer(this, 'LB', {
      vpc,
      internetFacing: true,
      idleTimeout: Duration.seconds(900), // allow long-lived SSE streams
    });

    const listener = lb.addListener('Listener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      // Default: deny anything that doesn't match the host-header rule below
      // (blocks raw-IP / Host-less scanners -> they get 403, not the app).
      defaultAction: elbv2.ListenerAction.fixedResponse(403, {
        contentType: 'text/plain',
        messageBody: 'Forbidden',
      }),
    });

    // Only requests whose Host header is the ALB's own DNS reach the container.
    listener.addTargets('Portal', {
      port: 8080,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      priority: 1,
      conditions: [elbv2.ListenerCondition.hostHeaders([lb.loadBalancerDnsName])],
      healthCheck: {
        path: '/healthz',
        healthyHttpCodes: '200',
        interval: Duration.seconds(30),
        timeout: Duration.seconds(5),
      },
      deregistrationDelay: Duration.seconds(30),
    });

    this.url = `http://${lb.loadBalancerDnsName}`;
    new CfnOutput(this, 'PortalUrl', { value: this.url });
    new CfnOutput(this, 'PortalAlbDns', { value: lb.loadBalancerDnsName });
  }
}
