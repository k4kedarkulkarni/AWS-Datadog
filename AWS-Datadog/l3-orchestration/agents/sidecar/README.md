# Datadog trace-agent sidecar (L3 agent telemetry)

These assets turn on **Datadog telemetry** for the L3 A2A agents (order / refund)
by running a Datadog **trace-agent** inside the AgentCore Runtime container,
alongside the agent. The L5 notebook `0_datadog_apm_setup.ipynb` copies the
`Dockerfile` into the `l3-orchestration/` working dir at deploy time — the
AgentCore starter toolkit then uses it instead of its generated template, so
`configure()` / `launch()` stay the deploy verbs.

## Why a sidecar (not agentless OTLP)

AgentCore's built-in observability runs the container under
`opentelemetry-instrument` (ADOT) and exports to CloudWatch/X-Ray. You can point
that ADOT exporter at Datadog's OTLP intake — and that works for ordinary agent
traces. But the Datadog **AI Guard** SDK emits its verdict as a native `ddtrace`
`ai_guard` APM span, and `ddtrace` collides with the ADOT `opentelemetry-instrument`
wrapper, so on the OTLP path the AI Guard span is dropped (verified 2026-06-29).

Running the Datadog trace-agent in-container and pointing `ddtrace` at
`localhost:8126` — with ADOT disabled — delivers the AI Guard span to Datadog
APM and the AI Guard Security UI. Verified end-to-end on a managed AgentCore
Runtime 2026-06-30.

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage: copies only the ~22 MB trace-agent binary onto the workshop agent base (no ADOT). Image stays under the 2 GB AgentCore limit. |
| `supervisord.conf` | Runs `trace-agent` + the agent (`ddtrace-run python -m $AGENT_MODULE`). |
| `entrypoint.sh` | Writes the trace-agent config from env, binds A2A on `0.0.0.0`, starts supervisord. |

## Deploy-time env (set via `launch(env_vars=...)`)

| Var | Example | Purpose |
|---|---|---|
| `AGENT_MODULE` | `agents.order_agent_a2a` | Which agent the container runs. |
| `DD_API_KEY` | (from SSM/Secrets) | trace-agent → Datadog intake. |
| `DD_SITE` | `datadoghq.com` | Datadog site. |
| `DD_SERVICE` | `anycompany-order-agent` | Per-agent service (AI Guard policy targets this). |
| `DD_ENV` | `demo` | Environment tag. |
| `AI_GUARD_ENABLED` | `false` (baseline) / `true` (L4) | Enables the AI Guard hook (see `../aiguard_hooks.py`). Read UN-prefixed by the agents; set to `true` in `l4-security/2_aiguard_agent_protection.ipynb`. The baseline L3 deploy leaves it unset. |
| `DD_APP_KEY` | (from Secrets) | Required only when `AI_GUARD_ENABLED=true`; the AI Guard client needs an app key scoped `ai_guard_evaluate`. |

## Trade-offs

- Disabling ADOT means this track loses AgentCore's built-in CloudWatch GenAI
  traces; Datadog becomes the in-container telemetry path. (Datadog's AWS
  CloudWatch metric polling is unaffected — that is service-side.)
- The trace-agent runs per session (one per AgentCore microVM); note this for scale.
- This diverges the Datadog track's container from the other tracks
  (aws-only / aws-nvidia / aws-fireworks), which keep the toolkit-generated image.
