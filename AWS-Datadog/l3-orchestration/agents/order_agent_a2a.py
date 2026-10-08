"""Order Agent — A2A Server for AgentCore Runtime.

Exposes the Order Agent via the A2A protocol so it can be discovered
and invoked by other agents or orchestrators.

The Order Agent calls tools via the AgentCore Gateway.
Gateway inbound auth: Cognito token (obtained at startup).
Gateway outbound auth: IAM role (Gateway invokes Lambda).
"""
import os
import logging
import json
import uuid

import boto3
import httpx
import uvicorn
from fastapi import FastAPI
from strands import Agent, tool
from strands.models.litellm import LiteLLMModel
from strands.multiagent.a2a import A2AServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from SSM Parameter Store
# ---------------------------------------------------------------------------
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/anycompany/agentcore")

ssm_client = boto3.client("ssm")


def _get_ssm(name: str, default: str = None) -> str:
    try:
        return ssm_client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except ssm_client.exceptions.ParameterNotFound:
        if default is not None:
            return default
        raise


AWS_REGION = boto3.session.Session().region_name or "us-east-1"
# LiteLLM uses AWS_REGION_NAME (not AWS_REGION) for Bedrock routing.
# Set it before any LiteLLMModel call so it picks the right region.
os.environ.setdefault("AWS_REGION_NAME", AWS_REGION)

MODEL_ID = _get_ssm(f"{SSM_PREFIX}/model_id")
GATEWAY_URL = _get_ssm(f"{SSM_PREFIX}/gateway_url")
COGNITO_USER_POOL_ID = _get_ssm(f"{SSM_PREFIX}/cognito_user_pool_id", default="")
COGNITO_CLIENT_ID = _get_ssm(f"{SSM_PREFIX}/cognito_client_id", default="")
# Workshop test-user password — stored in SSM as SecureString
# (encrypted at rest with KMS). The _get_ssm helper passes WithDecryption=True.
USER_PASSWORD = _get_ssm(f"{SSM_PREFIX}/user_password", default="")

# Gateway service (M2M) auth. AgentCore Gateway (CUSTOM_JWT) authorizes on the
# token's client_id + scope, so a USER_PASSWORD ID token (no scope) is rejected
# with 403 insufficient_scope. Mint a client_credentials access token from the
# Cognito M2M client created in L3 nb1. The tiered users remain for chatbot identity.
GATEWAY_M2M_CLIENT_ID = _get_ssm(f"{SSM_PREFIX}/gateway_m2m_client_id", default="")
GATEWAY_M2M_CLIENT_SECRET = _get_ssm(f"{SSM_PREFIX}/gateway_m2m_client_secret", default="")
GATEWAY_TOKEN_ENDPOINT = _get_ssm(f"{SSM_PREFIX}/gateway_token_endpoint", default="")
GATEWAY_SCOPE = _get_ssm(f"{SSM_PREFIX}/gateway_scope", default="")

runtime_url = os.environ.get("AGENTCORE_RUNTIME_URL", "http://127.0.0.1:9000/")
host, port = os.environ.get("AGENT_HOST", "127.0.0.1"), 9000  # nosec B104 - configurable; containers override via AGENT_HOST=0.0.0.0

logger.info(f"Config loaded — Model: {MODEL_ID}, Region: {AWS_REGION}")
logger.info(f"Gateway URL: {GATEWAY_URL}")


# ---------------------------------------------------------------------------
# Get a Cognito M2M (service) access token at startup for Gateway auth
# ---------------------------------------------------------------------------
def _get_gateway_token() -> str:
    """Mint a Cognito M2M access token via the OAuth2 client_credentials grant.

    AgentCore Gateway (CUSTOM_JWT) authorizes on the token's client_id + scope, so a
    USER_PASSWORD ID token (which carries no scope) is rejected with 403
    insufficient_scope. The client_credentials access token carries the gateway scope.
    """
    if not (GATEWAY_M2M_CLIENT_ID and GATEWAY_TOKEN_ENDPOINT):
        logger.warning("Gateway M2M auth not configured")
        return ""
    import base64

    basic = base64.b64encode(
        f"{GATEWAY_M2M_CLIENT_ID}:{GATEWAY_M2M_CLIENT_SECRET}".encode()
    ).decode()
    data = {"grant_type": "client_credentials"}
    if GATEWAY_SCOPE:
        data["scope"] = GATEWAY_SCOPE
    resp = httpx.post(
        GATEWAY_TOKEN_ENDPOINT,
        data=data,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    logger.info("Obtained Cognito M2M access token for Gateway auth")
    return token


ACCESS_TOKEN = _get_gateway_token()


# ---------------------------------------------------------------------------
# AI Guard (L4 Security): decode the fixed service identity from the gateway
# token. This `sub` is the circuit-breaker key — the SAME value the L4 Gateway
# REQUEST interceptor independently derives from the request Authorization header.
# Unverified decode is safe here: it is our own token and the gateway authorizer
# validates it on the request path; we only read the subject claim.
# ---------------------------------------------------------------------------
def _decode_jwt_sub(token: str) -> str:
    if not token:
        return ""
    try:
        import base64

        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad to a multiple of 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("sub", "")
    except Exception as exc:  # noqa: BLE001 - never block startup on decode
        logger.warning(f"Could not decode service sub from token: {exc}")
        return ""


SERVICE_SUB = _decode_jwt_sub(ACCESS_TOKEN)


# ---------------------------------------------------------------------------
# Gateway helper — calls tools via Gateway with the token
# ---------------------------------------------------------------------------
def _call_gateway_tool(tool_name: str, arguments: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",  # required by MCP streamable-HTTP
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }

    mcp_request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": str(uuid.uuid4()),
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    try:
        resp = httpx.post(GATEWAY_URL, headers=headers, json=mcp_request, timeout=30.0)
        resp.raise_for_status()
        result = resp.json()

        if "error" in result:
            return {"status": "error", "message": result["error"].get("message", str(result["error"]))}

        content = result.get("result", {}).get("content", [])
        for item in content:
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    return {"status": "success", "result": item["text"]}

        return {"status": "success", "raw": result.get("result", result)}

    except httpx.HTTPStatusError as e:
        logger.error(f"Gateway error: {e.response.status_code}")
        return {"status": "error", "message": f"Gateway returned {e.response.status_code}"}
    except Exception as e:
        logger.error(f"Gateway call failed: {e}")
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@tool
def check_order_details(
    order_id: str = "",
    customer_id: str = "",
) -> dict:
    """Look up order details or customer orders from the database.

    Provide at least one parameter:
    - order_id: Look up a specific order (e.g. ORD-10001)
    - customer_id: Get all orders for a customer (e.g. CUST-789)

    Args:
        order_id: The order identifier (e.g. ORD-10001).
        customer_id: The customer identifier (e.g. CUST-789).
    """
    args = {}
    if order_id:
        args["order_id"] = order_id
    if customer_id:
        args["customer_id"] = customer_id

    if not args:
        return {"status": "error", "message": "Provide at least one of: order_id or customer_id."}

    return _call_gateway_tool("order-tools___check_order_details", args)


# ---------------------------------------------------------------------------
# Agent + A2A Server
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the Order Agent for a retail customer-support system.

Your responsibilities:
1. Look up order details by order ID (e.g. ORD-10001).
2. Retrieve all orders for a given customer ID (e.g. CUST-789).

Rules:
- Use the check_order_details tool to answer all queries.
- ALWAYS pass customer_id alongside order_id when looking up a specific order.
- If the user asks about their orders or a customer's orders, pass the customer_id parameter.
- Return structured, factual information from the database.
- Do not make up order data — only return what the database provides.
- Be concise and helpful.
"""

model = LiteLLMModel(model_id=MODEL_ID)

_agent_kwargs = dict(
    model=model,
    tools=[check_order_details],
    system_prompt=SYSTEM_PROMPT,
    name="Order Agent",
    description="Handles order lookups and customer order history.",
)

# AI Guard (L4 Security) — env-gated. When AI_GUARD_ENABLED=true, attach the
# custom HookProvider that evaluates every model/tool checkpoint and trips the
# DynamoDB circuit breaker on an ABORT verdict. Baseline-equivalent when unset.
# Import lazily (AFTER `from strands import Agent` at module top) to avoid the
# stale-hook-class-identity bug.
if os.getenv("AI_GUARD_ENABLED", "").lower() == "true":
    # The AgentCore container runs this as the package module `agents.order_agent_a2a`
    # (WORKDIR /app, `python -m agents.order_agent_a2a`), so the sibling lives at
    # `agents.aiguard_hooks`. Local unit tests put `agents/` on sys.path and import it
    # bare. Try the package path first, fall back to the bare name.
    try:
        from agents.aiguard_hooks import AIGuardHooks
    except ImportError:
        from aiguard_hooks import AIGuardHooks

    _agent_kwargs["hooks"] = [
        AIGuardHooks(
            principal_id=SERVICE_SUB,
            risk_table=_get_ssm(f"{SSM_PREFIX}/risk_table", default=""),
            region=AWS_REGION,
        )
    ]
    logger.info("AI Guard hooks ENABLED for Order Agent (principal=%s)", SERVICE_SUB)

# LLM Observability (L5) — env-gated. When LLMOBS_ENABLED=true, turn on Datadog
# Agent Observability (sidecar-routed, NOT agentless — agentless would divert the
# AI Guard APM span off the sidecar) and attach the agent-pattern hook so THIS live
# runtime emits Agent/LLM/Tool spans to the Datadog Agent Observability page.
# Orthogonal to AI Guard: works with or without AI_GUARD_ENABLED. Same lazy
# package-path-then-bare import discipline as the AI Guard hook above.
if os.getenv("LLMOBS_ENABLED", "").lower() == "true":
    try:
        from agents.observability import LLMObsAgentHooks, enable_llmobs
    except ImportError:
        from observability import LLMObsAgentHooks, enable_llmobs

    if enable_llmobs():
        _agent_kwargs.setdefault("hooks", []).append(LLMObsAgentHooks(agent_name="Order Agent"))
        logger.info("LLM Observability ENABLED for Order Agent (agent-pattern spans)")

agent = Agent(**_agent_kwargs)

a2a_server = A2AServer(
    agent=agent,
    http_url=runtime_url,
    serve_at_root=True,
)

app = FastAPI()


@app.get("/ping")
def ping():
    return {"status": "healthy"}


app.mount("/", a2a_server.to_fastapi_app())

if __name__ == "__main__":
    uvicorn.run(app, host=host, port=port)
