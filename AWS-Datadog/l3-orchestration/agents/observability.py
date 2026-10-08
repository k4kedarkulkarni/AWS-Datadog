"""Datadog LLM Observability (Agent Observability) — agent-pattern spans for the
live A2A runtimes (L5 Observability).

This is the piece that makes the Datadog **Agent Observability** page fill from the
DEPLOYED agents instead of from a notebook. It has two parts:

  1. ``enable_llmobs()`` — turns LLM Observability on, in the running container,
     routed through the in-container Datadog **trace-agent sidecar** (NOT agentless).
  2. ``LLMObsAgentHooks`` — a Strands ``HookProvider`` that structures each agent run
     as the **agent pattern**: an ``agent`` span (the whole invocation) with the model
     calls and tool calls nested underneath it.

Why sidecar-routed and NOT agentless
-------------------------------------
``LLMObs.enable(agentless_enabled=True)`` does more than change where LLM Obs spans
go: with APM tracing at its default (on), ddtrace also switches the **APM** span
writer to agentless, sending ordinary APM spans to Datadog's public intake instead
of to ``DD_TRACE_AGENT_URL``. That would divert the L4 AI Guard ``ai_guard`` APM span
off the sidecar we deliberately stood up to carry it. So we enable with
``agentless_enabled=False``: LLM Obs spans ride the trace-agent's EVP proxy on
``localhost:8126`` (``/evp_proxy/v2/api/v2/llmobs``) and the ``ai_guard`` APM span
keeps its existing sidecar path — one egress, two span channels.

Why a HookProvider (and not a decorator)
----------------------------------------
The A2A server drives the agent via ``agent.stream_async(...)``, an async generator —
LLM Obs decorators can't wrap it cleanly. So the ``agent`` span is opened on
``BeforeInvocationEvent`` and finished on ``AfterInvocationEvent``. While it is open it
is the active span on the tracer, so the model spans (emitted automatically by
ddtrace's litellm integration) and the tool spans opened here **nest under it** — the
agent pattern, with no hand-instrumentation of the LLM calls.

Model spans are free: ``strands.models.litellm.LiteLLMModel`` calls litellm, which
ddtrace's litellm integration traces as an LLM Obs ``llm`` span (model, provider,
messages, tokens). litellm reaches Bedrock over its own signed-HTTP client, not the
instrumented boto path, so there is no duplicate Bedrock span.

Everything here is best-effort: an observability failure must never break the agent,
so every callback is wrapped and swallows its own errors.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ML app = the Datadog LLM Observability "application" grouping. One value for the
# whole customer-support system; per-agent DD_SERVICE (anycompany-order-agent /
# anycompany-refund-agent) distinguishes the runtimes within it. Matches the value
# the L5 Agent Observability notebook and its screenshots use.
DEFAULT_ML_APP = "anycompany-customer-support"

# Keys under which live span handles are stashed on the per-invocation
# ``invocation_state`` dict so the paired After* callback can finish them.
_AGENT_SPAN_KEY = "_llmobs_agent_span"
_TOOL_SPAN_PREFIX = "_llmobs_tool_span:"  # + toolUseId (multiple tool calls per run)


def _ml_app() -> str:
    return os.getenv("DD_LLMOBS_ML_APP") or DEFAULT_ML_APP


def enable_llmobs() -> bool:
    """Enable LLM Observability sidecar-routed (agent-proxy mode). Idempotent.

    Reads DD_SITE / DD_API_KEY / DD_SERVICE / DD_ENV from the container environment
    (the entrypoint sets them) and DD_LLMOBS_ML_APP for the ML-app grouping. Returns
    True if LLM Obs is enabled on return, False if enabling failed (agent keeps
    running either way).
    """
    try:
        from ddtrace.llmobs import LLMObs
    except Exception as exc:  # noqa: BLE001 - ddtrace should always be present; never crash startup
        logger.warning("LLMObs unavailable, Agent Observability disabled: %s", exc)
        return False

    if LLMObs.enabled:
        return True

    # The workshop package is named `agents`, which SHADOWS the OpenAI Agents SDK
    # package (also imported as `agents`) that ddtrace's `openai_agents` integration
    # targets. With that integration enabled and the real SDK absent, ddtrace tries to
    # patch OUR package as if it were that SDK and raises ModuleNotFoundError
    # ('agents.tracing'), aborting LLMObs.enable(). We don't use the OpenAI Agents SDK,
    # so disable just that integration. setdefault => an explicit env override wins, and
    # the entrypoint sets the same var so `ddtrace-run`'s patch pass skips it too.
    os.environ.setdefault("DD_TRACE_OPENAI_AGENTS_ENABLED", "false")

    try:
        LLMObs.enable(
            ml_app=_ml_app(),
            integrations_enabled=True,   # litellm integration emits the nested llm spans
            agentless_enabled=False,     # sidecar-routed via trace-agent EVP proxy (see module docstring)
        )
        logger.info(
            "LLM Observability ENABLED (ml_app=%s, sidecar-routed via trace-agent EVP proxy)",
            _ml_app(),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - fail open: telemetry must not break the agent
        logger.warning("LLMObs.enable failed, Agent Observability disabled: %s", exc)
        return False


class LLMObsAgentHooks:
    """Strands ``HookProvider`` that emits the agent pattern to Datadog LLM Obs.

    * ``BeforeInvocationEvent`` -> open an ``agent`` span, annotate the input.
    * ``BeforeToolCallEvent`` / ``AfterToolCallEvent`` -> open/close a ``tool`` span
      (nested under the agent span), annotating tool input and output.
    * ``AfterInvocationEvent`` -> annotate the agent output and finish the span.

    The model (``llm``) spans are produced automatically by ddtrace's litellm
    integration and nest under the open agent span; we do not create them here.

    Args:
        agent_name: label for the agent span; defaults to the Strands ``agent.name``.
        ml_app: LLM-Obs application tag; defaults to DD_LLMOBS_ML_APP / DEFAULT_ML_APP.
    """

    def __init__(self, *, agent_name: Optional[str] = None, ml_app: Optional[str] = None) -> None:
        # Lazy import AFTER strands has been imported by the agent module (same
        # discipline as aiguard_hooks: eager imports can capture stale identities).
        from ddtrace.llmobs import LLMObs

        self._LLMObs = LLMObs
        self._agent_name = agent_name
        self._ml_app = ml_app or _ml_app()

    # ------------------------------------------------------------------ #
    # HookProvider contract
    # ------------------------------------------------------------------ #
    def register_hooks(self, registry, **kwargs) -> None:
        from strands.hooks.events import (
            AfterInvocationEvent,
            AfterToolCallEvent,
            BeforeInvocationEvent,
            BeforeToolCallEvent,
        )

        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)
        registry.add_callback(AfterInvocationEvent, self._on_after_invocation)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool)

    # ------------------------------------------------------------------ #
    # Agent-root span (the whole invocation)
    # ------------------------------------------------------------------ #
    def _on_before_invocation(self, event) -> None:
        if not self._LLMObs.enabled:
            return
        try:
            name = self._agent_name or getattr(event.agent, "name", None) or "agent"
            span = self._LLMObs._instance._start_span("agent", name=name, ml_app=self._ml_app)
            event.invocation_state[_AGENT_SPAN_KEY] = span
            inp = self._latest_user_text(getattr(event, "messages", None) or getattr(event.agent, "messages", None))
            if inp:
                self._LLMObs.annotate(span=span, input_data=inp)
        except Exception as exc:  # noqa: BLE001 - observability must never break the agent
            logger.warning("LLMObs before_invocation failed: %s", exc)

    def _on_after_invocation(self, event) -> None:
        span = None
        try:
            span = event.invocation_state.pop(_AGENT_SPAN_KEY, None)
            if span is None:
                return
            out = self._result_text(getattr(event, "result", None))
            if out:
                self._LLMObs.annotate(span=span, output_data=out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLMObs after_invocation annotate failed: %s", exc)
        finally:
            if span is not None:
                try:
                    span.finish()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LLMObs agent span finish failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Tool spans (nested under the agent span)
    # ------------------------------------------------------------------ #
    def _on_before_tool(self, event) -> None:
        if not self._LLMObs.enabled:
            return
        try:
            tool_use = getattr(event, "tool_use", None) or {}
            name = tool_use.get("name", "tool")
            # Pass ml_app explicitly, exactly as the agent span does. In the A2A
            # stream_async async-generator flow the tool callback often does NOT see
            # the agent span as the active LLM Obs span, so this tool span becomes an
            # effective root. A root LLM Obs span with no ml_app is dropped (ml_app is
            # only inherited from an active parent / DD_LLMOBS_ML_APP at a true root),
            # which is why span.kind:tool showed "No spans found" while the agent span
            # — which passes ml_app — was queryable. See the LLM Obs Python SDK ref:
            # ml_app must be supplied when starting a root span for a new trace.
            span = self._LLMObs._instance._start_span("tool", name=name, ml_app=self._ml_app)
            args = tool_use.get("input", {})
            self._LLMObs.annotate(
                span=span,
                input_data=args if isinstance(args, str) else json.dumps(args, default=str),
            )
            event.invocation_state[_TOOL_SPAN_PREFIX + tool_use.get("toolUseId", "")] = span
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLMObs before_tool failed: %s", exc)

    def _on_after_tool(self, event) -> None:
        span = None
        try:
            tool_use = getattr(event, "tool_use", None) or {}
            span = event.invocation_state.pop(_TOOL_SPAN_PREFIX + tool_use.get("toolUseId", ""), None)
            if span is None:
                return
            out = self._stringify_tool_result(getattr(event, "result", None))
            if out:
                self._LLMObs.annotate(span=span, output_data=out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLMObs after_tool annotate failed: %s", exc)
        finally:
            if span is not None:
                try:
                    span.finish()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LLMObs tool span finish failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Text extractors (best-effort; Strands content is a list of ContentBlocks)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _text_from_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content or []:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        return "\n".join(parts)

    @classmethod
    def _latest_user_text(cls, messages: Any) -> str:
        """Text of the most recent user turn (the prompt driving this invocation)."""
        for msg in reversed(messages or []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                txt = cls._text_from_content(msg.get("content"))
                if txt:
                    return txt
        return ""

    @classmethod
    def _result_text(cls, result: Any) -> str:
        """Text of the agent's final answer (AgentResult.message, else str())."""
        if result is None:
            return ""
        message = getattr(result, "message", None)
        if isinstance(message, dict):
            txt = cls._text_from_content(message.get("content"))
            if txt:
                return txt
        try:
            return str(result)
        except Exception:  # noqa: BLE001
            return ""

    @classmethod
    def _stringify_tool_result(cls, tool_result: Any) -> str:
        """Flatten a Strands ToolResult ({'content':[{'text'|'json':..}]}) to a string."""
        if tool_result is None:
            return ""
        if isinstance(tool_result, str):
            return tool_result
        parts: list[str] = []
        for c in (tool_result.get("content", []) if isinstance(tool_result, dict) else []) or []:
            if isinstance(c, dict):
                if "text" in c:
                    parts.append(c["text"])
                elif "json" in c:
                    parts.append(json.dumps(c["json"], default=str))
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(tool_result, default=str)
        except Exception:  # noqa: BLE001
            return str(tool_result)
