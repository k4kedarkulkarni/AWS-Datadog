# Datadog-telemetry container for the L3 A2A agents (order / refund).
#
# This is the BYO ("bring your own") Dockerfile the AgentCore starter toolkit
# uses INSTEAD of its generated template when this file is copied into the
# l3-orchestration working dir (the toolkit's generate_dockerfile() prefers an
# existing project-root Dockerfile). It is introduced by the L5 notebook
# `0_datadog_apm_setup.ipynb` as an explicit, pluggable "turn on Datadog
# telemetry" step — the baseline L3 deploys in 2_/3_ stay on the toolkit's
# generated image, so the workshop's other tracks are unaffected.
#
# Why a sidecar instead of agentless OTLP: the Datadog AI Guard SDK emits its
# verdict as a native ddtrace `ai_guard` APM span (ddtrace tracer.trace), and
# ddtrace COLLIDES with AgentCore's built-in ADOT `opentelemetry-instrument`
# wrapper — so the agentless OTLP path drops the AI Guard span. Running a
# Datadog trace-agent in-container and pointing ddtrace at localhost:8126 (with
# ADOT disabled) delivers the span. Verified end-to-end on a managed AgentCore
# Runtime 2026-06-30.
#
# Slim by design: a multi-stage build copies ONLY the ~22 MB trace-agent binary
# (not the full Datadog Agent's ~650 MB embedded Python), keeping the image
# under the 2 GB AgentCore image limit. The trace-agent alone forwards APM spans
# (incl. the ai_guard span) straight to Datadog's trace intake.

# ---- Stage 1: harvest the trace-agent binary from a full DD Agent install ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS ddsource
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN DD_API_KEY=placeholder DD_INSTALL_ONLY=true \
    bash -c "$(curl -L https://s3.amazonaws.com/dd-agent/scripts/install_script_agent7.sh)"

# ---- Stage 2: the agent image (workshop base) + trace-agent sidecar ----------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app

# Same env block as the toolkit-generated workshop image (minus ADOT).
ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    AWS_REGION=us-east-1 \
    AWS_DEFAULT_REGION=us-east-1

# supervisor runs the multi-process container (trace-agent + agent); procps for diagnostics.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates supervisor procps \
    && rm -rf /var/lib/apt/lists/*

# Copy ONLY the trace-agent binary (no embedded Python) to stay slim.
COPY --from=ddsource /opt/datadog-agent/embedded/bin/trace-agent /opt/datadog-agent/embedded/bin/trace-agent
RUN mkdir -p /etc/datadog-agent /opt/datadog-agent/run

# Install BOTH agents' requirements (the image ships both modules; AGENT_MODULE
# selects which one runs at startup). `uv pip install` accepts multiple -r files;
# the union resolves cleanly (refund is a subset of order minus mcp).
COPY agents/requirements_order_a2a.txt /app/requirements_order_a2a.txt
COPY agents/requirements_refund_a2a.txt /app/requirements_refund_a2a.txt
RUN uv pip install -r /app/requirements_order_a2a.txt -r /app/requirements_refund_a2a.txt

# NO aws-opentelemetry-distro and NO `opentelemetry-instrument` CMD — ADOT is off
# so ddtrace owns instrumentation with no collision.

COPY agents/sidecar/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY agents/sidecar/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Bring the whole l3-orchestration project (agents/, etc.) — matches the toolkit's `COPY . .`.
COPY . .

# 9000 = A2A server (the agent binds here); 8080 kept for parity with the toolkit image.
EXPOSE 9000
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
