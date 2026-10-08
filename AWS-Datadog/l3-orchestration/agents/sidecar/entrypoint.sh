#!/bin/bash
# Entrypoint for the Datadog-telemetry agent container (order / refund).
# Configures the in-container trace-agent and starts it + the A2A agent under
# supervisord. All Datadog values come from the container environment, which the
# deploy step (launch env_vars) populates — nothing account-specific is baked in.
set -euo pipefail
echo "=== AnyCompany agent (Datadog sidecar) starting: ${AGENT_MODULE:-<unset>} ==="

# --- A2A bind: the AgentCore container must serve on all interfaces ---
export AGENT_HOST="${AGENT_HOST:-0.0.0.0}"

# --- Which agent module to run (set per-deploy: agents.order_agent_a2a etc.) ---
if [[ -z "${AGENT_MODULE:-}" ]]; then
  echo "ERROR: AGENT_MODULE not set (expected e.g. agents.order_agent_a2a)." >&2
  exit 1
fi

# --- Datadog config ---
export DD_SERVICE="${DD_SERVICE:-anycompany-agent}"
export DD_ENV="${DD_ENV:-demo}"
export DD_SITE="${DD_SITE:-datadoghq.com}"
# AI Guard is OFF unless the deploy explicitly turns it on. The agent code reads the
# UN-prefixed AI_GUARD_ENABLED (order_agent_a2a.py / refund_agent_a2a.py); nb0 leaves it
# unset (baseline APM) and l3/6_aiguard_agent_protection.ipynb sets AI_GUARD_ENABLED=true.
# Pass it through unchanged — do NOT default it to true, or the baseline deploy would
# silently attach the hook.
export AI_GUARD_ENABLED="${AI_GUARD_ENABLED:-false}"

# DD_AI_GUARD_ENABLED is a SEPARATE, DD-prefixed flag read by ddtrace / the Datadog AI Guard
# product (docs: security/ai_guard/setup). It is NOT the SDK evaluate() gate — client.evaluate()
# emits the `ai_guard` span without it — but Datadog's docs require it for the service to register
# as an AI Guard integration and for the spans to be fully enriched in Security > AI Guard. It was
# historically conflated with the un-prefixed toggle above and then dropped, so it has been absent
# from deployed containers. Set it ONLY when AI Guard is actually on, so baseline
# (AI_GUARD_ENABLED=false) deploys stay clean.
# tr-lowercase (not bash ${x,,}) so this stays portable to bash 3.x and matches the agent
# code's os.getenv(...).lower()=="true" check.
if [[ "$(printf '%s' "$AI_GUARD_ENABLED" | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
  export DD_AI_GUARD_ENABLED=true
  echo "DD_AI_GUARD_ENABLED=true (Datadog AI Guard product registration)"
fi
export DD_TRACE_ENABLED=true
# Point ddtrace at the in-container trace-agent.
export DD_AGENT_HOST=localhost
export DD_TRACE_AGENT_PORT=8126
export DD_TRACE_AGENT_URL="http://localhost:8126"
# This project's package is literally named `agents`, which shadows the OpenAI Agents
# SDK that ddtrace's openai_agents integration targets. We don't use that SDK, so
# disable the integration — otherwise `ddtrace-run`'s startup patch pass tries to patch
# OUR package and errors importing `agents.tracing`. (observability.py also sets this
# defensively before LLMObs.enable(); setting it here skips the startup attempt too.)
export DD_TRACE_OPENAI_AGENTS_ENABLED=false

if [[ -z "${DD_API_KEY:-}" ]]; then
  echo "ERROR: DD_API_KEY not set. Pass it via the runtime environment." >&2
  exit 1
fi

# trace-agent reads api_key + site from this file and forwards APM spans
# (incl. the ai_guard span) to Datadog's trace intake.
mkdir -p /etc/datadog-agent
cat > /etc/datadog-agent/datadog.yaml << EOF
api_key: ${DD_API_KEY}
site: ${DD_SITE}
hostname: ${DD_SERVICE}
tags:
  - env:${DD_ENV}
  - service:${DD_SERVICE}
apm_config:
  enabled: true
  apm_non_local_traffic: false
  receiver_port: 8126
# EVP proxy (default on) lets ddtrace ship LLM Observability spans THROUGH this
# trace-agent (localhost:8126/evp_proxy/v2/api/v2/llmobs) when LLMOBS_ENABLED=true —
# the same sidecar path the APM (incl. ai_guard) spans use. Kept explicit so the LLM
# Obs egress is sidecar-routed, never agentless (agentless would divert APM spans off
# the sidecar). Requires the api_key above, which is already set.
evp_proxy_config:
  enabled: true
EOF

echo "AGENT_MODULE=$AGENT_MODULE  DD_SERVICE=$DD_SERVICE  DD_SITE=$DD_SITE  AI_GUARD_ENABLED=$AI_GUARD_ENABLED  (trace-agent only; ADOT disabled)"
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
