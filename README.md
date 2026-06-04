# MathModeler

> A multi-agent mathematical modeling system built on **Amazon Bedrock AgentCore** — give it an open-ended math modeling problem, and it automatically runs the full pipeline of *Problem Analysis → Mathematical Modeling → Computational Solving → Solution Reporting*, streaming the reasoning and results in real time.

**English** | [简体中文](README.zh-CN.md)

---

## ✨ Highlights

MathModeler is an end-to-end multi-agent application that runs entirely on **Amazon Bedrock AgentCore**. It decomposes a complex modeling task into specialized roles, coordinated by an orchestrator agent that drives collaboration along the task's dependencies:

- **Analyst** — understands the problem, decomposes it into sub-problems, and builds the dependency DAG between them.
- **Modeler** — retrieves candidate methods from a hierarchical modeling-method library and iteratively refines the mathematical model in an actor-critic loop.
- **Solver** — automatically writes and executes code, retries on errors, and produces computational results.
- **Reporter** — aggregates every stage's output into a structured, complete solution report.

All collaboration, reasoning, and solving is hosted on AgentCore — no need to build your own container orchestration or session-state management.

---

## 🏗️ Architecture

![Architecture](docs/architecture.png)

The system makes full use of the core capabilities of Amazon Bedrock AgentCore:

| AgentCore capability | Role in this project |
| --- | --- |
| **AgentCore Runtime** | Each agent (the Orchestrator + 4 sub-agents) is deployed as an independent serverless Runtime, exposing `/invocations` (POST) and `/ping` (GET) via FastAPI, with SSE streaming, running on `LINUX_ARM64`. |
| **AgentCore Code Interpreter** | Provides a controlled sandbox execution environment for the Solver to safely run model-generated solver code. |
| **AgentCore Memory** | Manages short-term session events and long-term preferences, enabling context memory across steps. |
| **Amazon S3 (document bus)** | Agents exchange intermediate artifacts through S3 (organized by `session_id`) for decoupled collaboration. |

The web portal is served by a **FastAPI container on AWS Fargate behind an Application Load Balancer** (HTTP). The ALB streams chunked/SSE responses without buffering, so the four-stage progress reaches the browser in real time; the portal also proxies `/api/solve` to the Orchestrator Runtime (`InvokeAgentRuntime`, SigV4) and gates access with a single admin login.

Agents are organized with the **Strands Agents** framework in an *agents-as-tools* pattern. The Orchestrator both constructs the supervisor agent and drives the sub-agents through a deterministic pipeline.


---

## 🚀 Deploy to AWS (via CDK)

**Prerequisites**

- An AWS account, with credentials configured locally — e.g. `aws configure` (or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` environment variables, or an SSO profile). Without valid credentials the deployment will fail.
- The target region (e.g. `us-west-2`) must have **model access enabled** in Amazon Bedrock for the models this project uses.
- [Node.js](https://nodejs.org/) 18+ and [Docker](https://www.docker.com/) installed (CDK builds the agent container images locally; an `arm64`/Graviton build host is recommended since the runtimes target `LINUX_ARM64`).

**Deploy**

```bash
# 1. Configure your AWS credentials first
aws configure   # or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN

# 2. Deploy with CDK (supply the portal admin password via CDK context)
cd mathmodeler/infra
npm install
npx cdk bootstrap     # first time only, per account/region
npx cdk deploy -c adminUser=admin -c adminPassword='<your-password>'
```

After deployment, open the `PortalEndpoint` output (an `http://<alb>.<region>.elb.amazonaws.com` URL),
log in with the admin credentials you provided, and submit a problem to watch the four stages stream live.

> **Note:** Without a custom domain / ACM certificate the ALB serves over **HTTP** (the default
> `*.elb.amazonaws.com` name cannot be issued a certificate), so the login password is sent in clear
> text — acceptable for a demo. Add an HTTPS listener once you have a domain.

---


## 🙏 Acknowledgements

Thanks to **[MM-Agent](https://arxiv.org/abs/2505.14148)** for the methodology that inspired this project.
 