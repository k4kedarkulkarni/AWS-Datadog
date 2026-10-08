# L6 — Observability with Datadog

End-to-end monitoring for the AnyCompany multi-agent customer support system using
Datadog APM, Metrics, LLM Observability, and Quality Evaluations.

---

## How It Works

Amazon Bedrock AgentCore has a built-in **ADOT (AWS Distro for OpenTelemetry)** SDK
that, by default, sends OTel spans to CloudWatch/X-Ray. You can redirect that ADOT
exporter to Datadog's OTLP endpoint, and that works for ordinary agent traces.

This track instead runs a **Datadog trace-agent inside the AgentCore Runtime
container**, alongside the agent, and disables ADOT. There is one decisive reason:
the Datadog **AI Guard** SDK (used by the L4 security layer) emits its verdict as a
native `ddtrace` `ai_guard` APM span, and `ddtrace` collides with AgentCore's ADOT
`opentelemetry-instrument` wrapper — so on the agentless OTLP path the AI Guard span
is dropped. Running a trace-agent in-container and pointing `ddtrace` at
`localhost:8126` delivers it. (Verified end-to-end on a managed AgentCore Runtime.)

```
AgentCore Runtime container (Order Agent, Refund Agent)
    │
    ├─ agent process  (ddtrace-run python -m agents.<order|refund>_agent_a2a)
    │     │  emits APM + ai_guard spans  ──► localhost:8126
    │     ▼
    └─ Datadog trace-agent (sidecar)  ──► Datadog trace intake (APM)
            │
            ├─► Datadog APM (full traces, incl. the AI Guard ai_guard span)
            ├─► Datadog Metrics (auto-derived from traces)
            └─► Datadog Agent / LLM Observability spans
```

**What gets captured end-to-end:**

```
User prompt
    │
    ▼
Harness (orchestrator)        ← root span
    │  model call              ← LLM span
    │  delegate to agent       ← child span
    ▼
Gateway                       ← child span
    │  route to Lambda tool
    ▼
Lambda (order / refund)       ← child span
    │  DynamoDB / KB query
    ▼
Response streamed back
```

The agent spans (and the AI Guard span) ship via the in-container trace-agent. The **Harness** is AWS-managed and has no sidecar, so it exports its own APM traces directly to Datadog's **OTLP traces intake** via OpenTelemetry env vars (set in L3 nb4) — only its *trace* signal is redirected; its logs and metrics stay in CloudWatch.

> **Trade-off:** disabling ADOT means this track loses AgentCore's built-in CloudWatch
> GenAI traces; Datadog becomes the in-container telemetry path. Datadog's AWS CloudWatch
> *metric polling* is unaffected (it is service-side). The sidecar runs per AgentCore
> session (one per microVM).

---

## Prerequisites

- **L1–L3 completed** — agents deployed on the Datadog sidecar image, Harness running.
- **L4 completed** (optional for AI Guard spans) — enables `AI_GUARD_ENABLED=true`.
- **L5 completed** — chatbot available for generating traffic.
- **Datadog credentials in SSM** — stored by `l3-orchestration/1_setup_resources.ipynb` Step 6.

After L3 is complete, these SSM parameters will exist (required by L6):
- `/anycompany/agentcore/harness_arn`
- `/anycompany/agentcore/gateway_url`
- `/anycompany/agentcore/cognito_client_id`
- `/anycompany/agentcore/user_password`
- `/anycompany/agentcore/dd_api_key`
- `/anycompany/agentcore/dd_app_key`
- `/anycompany/agentcore/dd_site`

### (Optional) Datadog AWS Integration

For AWS metrics (Bedrock tokens, Lambda invocations) to appear in Datadog:
1. Go to `https://app.datadoghq.com/integrations/amazon-web-services`
2. Add your AWS account (CloudFormation method is easiest)
3. Enable namespaces: AWS/Bedrock, AWS/Lambda, AWS/DynamoDB

This is optional — APM traces and LLM Observability work without it.

---

## Notebooks

| # | Notebook | What It Does |
|---|----------|--------------|
| 1 | `1_datadog_multi_agent_tracing.ipynb` | Invoke Harness, query Datadog APM traces, display span waterfall, latency breakdown |
| 2 | `2_datadog_metrics_and_cost.ipynb` | Query AWS metrics via Datadog, calculate cost, create Monitors + Dashboard |
| 3 | `3_datadog_agent_observability.ipynb` | Tour LLM Observability — Agent/LLM/Tool spans emitted by the deployed agents |
| 4 | `4_datadog_quality_evaluations.ipynb` | Submit quality evaluations (relevance, faithfulness) linked to traced spans |
| 5 | `5_datadog_service_map_and_errors.ipynb` | Service Map topology, Error Tracking issue groups, SLO with error budget |

> For the AWS-native observability path (X-Ray + CloudWatch GenAI Observability), see the **`aws-only`** track — this track disables ADOT in favor of the Datadog sidecar.

---

## What Each Notebook Produces in Datadog

### Notebook 1: Multi-Agent Tracing
- APM traces visible at **APM > Traces**
- Span waterfall showing: Harness → Agent → Gateway → Lambda
- Latency breakdown by service

### Notebook 2: Metrics, Cost & Operations
- AWS metrics: Bedrock invocations, tokens, latency; Lambda invocations, duration, errors
- Cost analysis based on token usage
- 3 Datadog Monitors (Lambda errors, Bedrock latency, token budget)
- Operational dashboard with widget groups

### Notebook 3: LLM Observability
- Agent Observability traces at **LLM Observability > Traces**
- Nested spans: Agent → LLM (with prompt/completion) → Tool
- Full input/output capture for debugging
- Enabled at deploy time via `LLMOBS_ENABLED=true` in L3; this notebook drives traffic and tours traces

### Notebook 4: Quality Evaluations
- Custom evaluations attached to spans (relevance, faithfulness, completeness)
- Categorical evaluations (correct/incorrect tool selection)
- Automated evaluation example using response validation

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `ParameterNotFound: harness_arn` | L3 notebook 4 not completed | Run `l3-orchestration/4_orchestrator_agent.ipynb` |
| `ParameterNotFound: dd_api_key` | L3 notebook 1 Step 6 not completed | Run `l3-orchestration/1_setup_resources.ipynb` |
| `No spans found` | No recent invocations | Use the chatbot (L5) or invoke Harness from notebook 1 |
| `ModuleNotFoundError` | Package not installed | `pip install datadog-api-client boto3 --upgrade` |
| `403 Failed permission authorization checks` | App key missing a scope (`monitors_write`, `dashboards_write`, `slos_write`) | Add the scope (see `l3-orchestration/1_setup_resources.ipynb` Step 6), or create the object in the UI from the JSON the cell prints |

---

## Key Design Decisions

1. **Sidecar over agentless OTLP** — The AI Guard `ai_guard` span requires ddtrace, which collides with ADOT. The sidecar resolves this.
2. **Credentials in SSM** — Stored once in L3/nb1 Step 6. All notebooks read from SSM. Production should use Secrets Manager with auto-rotation.
3. **Agent Observability on the live agents** — `LLMOBS_ENABLED=true` (set at deploy in L3/nb2,nb3) makes `observability.py` emit Agent/LLM/Tool spans from the running containers. Sidecar-routed (not agentless).
4. **Harness traces via OTLP-direct** — The Harness is AWS-managed (no sidecar), so it exports its APM traces straight to Datadog's **OTLP traces intake** using OpenTelemetry env vars (set in L3 nb4). Only the *trace* signal is redirected; the Harness's logs and metrics stay in CloudWatch. (Datadog's direct OTLP traces intake is in Preview and access-gated — the org must be allowlisted or the intake returns 403.)
5. **Trace-based metrics** — Datadog auto-derives throughput, latency, and error rate from APM traces. No separate metric pipeline needed.
