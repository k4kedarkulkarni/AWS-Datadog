# L4 — Security Layer (Datadog)

Defense-in-depth security for the workshop's multi-agent system: AWS-native PII masking
(Bedrock Guardrails) plus Datadog AI Guard runtime protection and gateway circuit-breaker.

## What it adds

- **PII masking** (`1_sensitive_data_masking.ipynb`): Bedrock Guardrails anonymize email, address,
  phone in tool responses via a Gateway RESPONSE interceptor. Complements AI Guard.
- **AI Guard runtime protection** (`2_aiguard_agent_protection.ipynb`): enables the env-gated
  `aiguard_hooks.AIGuardHooks` on both agents. Evaluates every model/tool step; in *Block* mode
  cancels unsafe calls inline. A high-severity verdict (`ABORT`, or `DENY` tagged
  `data-exfiltration`) trips a DynamoDB **circuit breaker**.
- **Gateway circuit breaker** (`3_aiguard_gateway_lock.ipynb`): an AgentCore Gateway REQUEST
  interceptor returns `403` on tool calls while the breaker is open, with **read-time TTL**
  auto-release.
- **Enforcement tests** (`4_aiguard_enforcement_tests.ipynb`): end-to-end scenarios (benign,
  injection, ABORT) to verify the full chain.
- **UI configuration** (`5_aiguard_ui_config.ipynb`): Datadog AI Guard console tour — service
  policy (monitor/block), sensitivity, tool allowlists.

## Notebook Sequence

| # | Notebook | What It Does |
|---|----------|--------------|
| 1 | `1_sensitive_data_masking.ipynb` | PII RESPONSE interceptor (Bedrock Guardrails) |
| 2 | `2_aiguard_agent_protection.ipynb` | Enable AI Guard on agents (env update, no rebuild) |
| 3 | `3_aiguard_gateway_lock.ipynb` | Gateway REQUEST interceptor (circuit breaker) |
| 4 | `4_aiguard_ui_config.ipynb` | Datadog AI Guard console configuration (service policies, sensitivity) |
| 5 | `5_aiguard_enforcement_tests.ipynb` | Test scenarios end-to-end (policies must be set first) |

## Prerequisites

- L3 notebooks completed — agents deployed on the Datadog sidecar image with `AI_GUARD_ENABLED=false`.
- `DD_API_KEY`, `DD_APP_KEY`, `DD_SITE` stored in SSM (by `l3-orchestration/1_setup_resources.ipynb` Step 6).
- Datadog org with AI Guard access and the app key scoped to `ai_guard_evaluate`.

## How it works with the sidecar

The agents are deployed on the Datadog trace-agent sidecar image in L3 (Notebooks 2–3).
AI Guard's verdict is a native `ddtrace` `ai_guard` APM span — it requires the sidecar
(ADOT would drop it due to the ddtrace/OTel collision). L4 Notebook 2 simply flips
`AI_GUARD_ENABLED=true` via env update — no image rebuild needed.

## Notes

- AI Guard is a Datadog **Preview** feature: it needs a per-organization feature flag and a
  `resource_name:ai_guard` retention filter (100%) for its spans to be retained.
- The service policy (Datadog console) is the on/off switch — *Monitor* vs *Block*.
- With realistic PII in the sample data, tune AI Guard **evaluation sensitivity** or a tool
  allowlist so benign order lookups aren't flagged as `data-exfiltration`.
