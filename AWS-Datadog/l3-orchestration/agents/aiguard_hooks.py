"""Datadog AI Guard — custom Strands HookProvider (L3 in-runtime protection).

Secure-by-construction half of the workshop's AI Guard defense-in-depth pattern.
A custom HookProvider (NOT the canned ``AIGuardStrandsPlugin``) so we can:
  1. write a DynamoDB **circuit-breaker LOCK** on an AI Guard ``ABORT`` verdict
     (the cross-agent containment the L4 Gateway interceptor enforces), and
  2. keep per-checkpoint control of the block posture.

Evaluation runs on all four checkpoints with ``Options(block=True)`` (Hybrid D):
the Datadog **server-side service policy** is the real on/off switch — with the
policy in *Block* mode ``evaluate`` raises ``AIGuardAbortError`` on DENY/ABORT and
we enforce inline; in *Monitor* mode it returns the ``Evaluation`` without raising
and we observe only. Either way, an ABORT verdict trips the breaker (the lock-write
is our own deterministic action, independent of Datadog's block toggle).

Block mechanism is per-event, matching the live ``strands-agents==1.43.0`` API
(verified by introspection — only the *Before* events expose a graceful cancel):
  * BeforeModelCallEvent  -> ``event.cancel``      (graceful)
  * BeforeToolCallEvent   -> ``event.cancel_tool`` (graceful)
  * AfterModelCallEvent   -> re-raise (no cancel field; only ``retry`` is writable)
  * AfterToolCallEvent    -> overwrite ``event.result`` with an error ToolResult
                             (no cancel field; ``result``/``retry`` are writable)

Trust model: this is OUR deterministic Python in the Runtime container, not
LLM-controlled. The lock-write uses the Runtime IAM execution role; a
prompt-injected agent cannot forge or suppress it (it never executes the write,
never touches credentials).

Lazy imports: ddtrace AI Guard + Strands event classes are imported INSIDE
``__init__``/``register_hooks`` (after ``from strands import Agent`` has run in the
agent module) to avoid the stale-hook-class-identity bug where eager imports
capture the wrong class objects and callbacks silently never fire.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import boto3

logger = logging.getLogger(__name__)

# Lock time-to-live: the breaker auto-releases after this many seconds (DynamoDB
# TTL). Coarse by design — see the production-hardening note in the L4 notebook.
LOCK_TTL_SECONDS = 120  # 2 minutes — self-releases fast if the breaker trips

# Key written into the shared per-invocation ``invocation_state`` dict when the
# BeforeModelCall checkpoint blocks. AfterModelCall fires even after a graceful
# BeforeModelCall cancel (verified against strands 1.43.0), re-presenting only the
# synthetic cancel message — re-evaluating it would double-call AI Guard and turn a
# clean graceful block into a propagated exception. AfterModelCall honors this flag
# and skips. ``invocation_state`` is the same writable dict across both checkpoints.
_MODEL_BLOCKED_FLAG = "_aiguard_model_blocked"

# Same idea for the tool checkpoints: AfterToolCall fires even when BeforeToolCall
# cancelled the tool (verified against strands 1.43.0) — the tool never ran and the
# result already carries the cancel error, so re-evaluating it would double-call
# AI Guard (skewing Datadog signal counts) and could write a duplicate ABORT lock.
# Keyed per toolUseId because multiple tool calls share one invocation_state.
_TOOL_BLOCKED_PREFIX = "_aiguard_tool_blocked:"

# Tags that escalate a DENY verdict to a CIRCUIT-BREAKER trip (service-wide LOCK), not just a
# per-call block. A DENY trips the breaker only when it carries ALL of these tags TOGETHER —
# the lethal-trifecta signature (sensitive-data access + jailbreak intent). An ABORT verdict
# would always trip the breaker, but empirically this build's AI Guard emits only ALLOW/DENY
# (never ABORT — verified in the Playground and the datadog.ai_guard.evaluations metric), so the
# breaker keys on the DENY tag signature. Requiring BOTH tags separates the three cases cleanly:
#   * benign bulk order lookup -> data-exfiltration ALONE (no jailbreak) -> NO trip (per-call only)
#   * prompt injection (Scenario 2) / destructive (Scenario 3) -> jailbreak WITHOUT data-exfiltration
#     -> NO trip (per-call block, as designed)
#   * malicious exfil trifecta (Scenario 4) -> data-exfiltration AND jailbreak -> TRIP the breaker
# Requires the Datadog service policy at Balanced sensitivity (at Aggressive a benign bulk lookup
# can cross the jailbreak threshold and false-trip).
BREAKER_DENY_REQUIRED_TAGS = {"data-exfiltration", "jailbreak"}


class AIGuardHooks:
    """Custom Strands ``HookProvider`` enforcing Datadog AI Guard verdicts.

    Args:
        principal_id: The fixed service ``sub`` (decoded from the agent's gateway
            token at startup). The circuit-breaker key — the SAME value the L4
            interceptor independently derives from the request Authorization header.
        risk_table: DynamoDB table name for the LOCK record (from SSM).
        region: AWS region for the DynamoDB client.
        ml_app: Datadog ML app tag (observability grouping); informational.
        client: (test seam) inject a pre-built AI Guard client; else one is created.
        ddb_client: (test seam) inject a boto3 DynamoDB client; else one is created.
    """

    def __init__(
        self,
        *,
        principal_id: str,
        risk_table: str,
        region: str,
        ml_app: str = "anycompany-agents",
        client: Optional[Any] = None,
        ddb_client: Optional[Any] = None,
    ) -> None:
        # Lazy import AFTER strands has been imported by the agent module.
        from ddtrace.appsec.ai_guard import (
            AIGuardAbortError,
            Function,
            Message,
            Options,
            ToolCall,
            new_ai_guard_client,
        )

        self._client = client if client is not None else new_ai_guard_client()
        # Stash the AI Guard types so handlers don't re-import on every call.
        self._AIGuardAbortError = AIGuardAbortError
        self._Options = Options
        self._Message = Message
        self._ToolCall = ToolCall
        self._Function = Function

        self._principal_id = principal_id
        self._risk_table = risk_table
        self._ml_app = ml_app
        self._ddb = ddb_client if ddb_client is not None else boto3.client(
            "dynamodb", region_name=region
        )

    # ------------------------------------------------------------------ #
    # HookProvider contract
    # ------------------------------------------------------------------ #
    def register_hooks(self, registry, **kwargs) -> None:
        # Lazy import — event class identities must match the ones the agent loop
        # dispatches on (eager import at module load captures stale identities).
        from strands.hooks.events import (
            AfterModelCallEvent,
            AfterToolCallEvent,
            BeforeModelCallEvent,
            BeforeToolCallEvent,
        )

        registry.add_callback(BeforeModelCallEvent, self._on_before_model)
        registry.add_callback(AfterModelCallEvent, self._on_after_model)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool)

    # ------------------------------------------------------------------ #
    # Checkpoint handlers
    # ------------------------------------------------------------------ #
    def _on_before_model(self, event) -> None:
        self._evaluate_and_enforce(event, lambda: self._messages_from(event.agent), kind="before_model")

    def _on_after_model(self, event) -> None:
        # AfterModelCall fires even after a graceful BeforeModelCall cancel,
        # re-presenting only the synthetic cancel text. Skip — the threat was
        # already caught (and the breaker already tripped) at before_model.
        if event.invocation_state.get(_MODEL_BLOCKED_FLAG):
            return
        self._evaluate_and_enforce(event, lambda: self._messages_from(event.agent), kind="after_model")

    def _on_before_tool(self, event) -> None:
        # The pending tool call is in event.tool_use (not yet in agent.messages);
        # synthesize an assistant tool-call message so AI Guard sees the action.
        def build():
            pending = {
                "name": event.tool_use["name"],
                "input": event.tool_use.get("input", {}),
                "toolUseId": event.tool_use.get("toolUseId", ""),
            }
            return self._messages_from(event.agent, pending_tool=pending)
        self._evaluate_and_enforce(event, build, kind="before_tool")

    def _on_after_tool(self, event) -> None:
        # Skip if BeforeToolCall already blocked THIS tool call — the tool never
        # ran and the result is our injected cancel error; re-evaluating would
        # double-count signals and risk a duplicate ABORT lock.
        tool_use_id = ""
        try:
            tool_use_id = event.tool_use.get("toolUseId", "")
        except Exception:  # noqa: BLE001
            pass
        if tool_use_id and event.invocation_state.get(_TOOL_BLOCKED_PREFIX + tool_use_id):
            return
        # CRITICAL: at AfterToolCall the just-produced output is in event.result and
        # is NOT yet in agent.messages. This checkpoint exists to catch indirect
        # injection in the tool OUTPUT, so we must feed event.result to the converter
        # — evaluating agent.messages alone would screen a conversation that doesn't
        # contain the output we're meant to inspect.
        def build():
            pending_result = None
            try:
                if event.result is not None:
                    pending_result = event.result
            except Exception:  # noqa: BLE001
                pass
            return self._messages_from(event.agent, pending_result=pending_result)
        self._evaluate_and_enforce(event, build, kind="after_tool")

    # ------------------------------------------------------------------ #
    # Core evaluate + enforce
    # ------------------------------------------------------------------ #
    def _evaluate_and_enforce(self, event, build_messages, *, kind: str) -> None:
        # build_messages() is invoked INSIDE the try so a converter failure also
        # fails closed (it runs before evaluate and must not escape unhandled).
        try:
            msgs = build_messages()
            evaluation = self._client.evaluate(msgs, self._Options(block=True))
        except self._AIGuardAbortError as err:
            # Block mode + DENY/ABORT: the server policy is enforcing. AIGuardAbortError
            # is BaseException-derived, so it is caught HERE, before generic Exception.
            reason = getattr(err, "reason", "") or "policy violation"
            action = getattr(err, "action", "DENY")
            tags = getattr(err, "tags", None) or []
            logger.warning(
                "AI Guard %s BLOCK at %s: action=%s tags=%s reason=%s",
                action, kind, action, tags, reason,
            )
            # Same breaker-trip rule as the monitor-mode path: ABORT always trips (defensive —
            # this build never emits ABORT), and a DENY trips only when it carries ALL of
            # BREAKER_DENY_REQUIRED_TAGS together (data-exfiltration AND jailbreak — the
            # lethal-trifecta signature). A benign bulk lookup (data-exfiltration alone) or a
            # lone prompt injection (jailbreak alone) does NOT trip. See BREAKER_DENY_REQUIRED_TAGS.
            if action == "ABORT" or (action == "DENY" and BREAKER_DENY_REQUIRED_TAGS.issubset(set(tags))):
                self._write_lock(reason=f"{kind}:{action}:{reason}")
            # Block-mode path already enforces inline via _block_event below, so a
            # failed lock-write here still leaves the current call blocked.
            self._block_event(event, kind, reason=reason, error=err)
            return
        except Exception as err:  # noqa: BLE001 - fail-closed on API/network error
            logger.error("AI Guard evaluate failed at %s (fail-closed): %s", kind, err)
            self._block_event(event, kind, reason=f"ai-guard-unavailable: {err}")
            return

        # Returned an Evaluation (monitor mode, or ALLOW). We observe; we do NOT
        # cancel inline (the org chose monitor) — EXCEPT an ABORT verdict still
        # trips the breaker, because the lock-write is our own deterministic
        # control, independent of Datadog's block toggle. Evaluation is a dict.
        action = evaluation.get("action")
        tags = evaluation.get("tags") or []
        if action and action != "ALLOW":
            logger.warning(
                "AI Guard %s (monitor) at %s: tags=%s reason=%s",
                action, kind, tags, evaluation.get("reason"),
            )
        # Trip the cross-agent circuit breaker (service-wide LOCK) when EITHER:
        #   * action == ABORT (defensive — this build's AI Guard never emits ABORT), or
        #   * action == DENY carrying ALL of BREAKER_DENY_REQUIRED_TAGS together
        #     (data-exfiltration AND jailbreak — the lethal-trifecta signature). This is
        #     what makes the malicious Scenario 4 trip the breaker on a DENY, while the
        #     benign bulk lookup (data-exfiltration ALONE) and a lone prompt injection
        #     (jailbreak ALONE) stay per-call blocks. See BREAKER_DENY_REQUIRED_TAGS.
        trips_breaker = action == "ABORT" or (
            action == "DENY" and BREAKER_DENY_REQUIRED_TAGS.issubset(set(tags))
        )
        if trips_breaker:
            # In monitor mode the lock-write is the ONLY containment action (server not
            # blocking inline). If it fails to persist, containment would be silently
            # lost — escalate to a fail-closed inline block on this call.
            locked = self._write_lock(reason=f"{kind}:{action}:{evaluation.get('reason', '')}")
            if not locked:
                logger.error(
                    "AI Guard monitor-mode %s at %s but lock-write FAILED — "
                    "failing closed on this call.", action, kind,
                )
                self._block_event(event, kind, reason="breaker-lock-write-failed")

    def _block_event(self, event, kind: str, *, reason: str, error=None) -> None:
        """Apply the per-checkpoint block using the live writable field for each event."""
        message = f"Blocked by AI Guard: {reason}"
        if kind == "before_model":
            event.cancel = message
            # Tell the paired AfterModelCall to skip (avoids a redundant second
            # evaluate + a spurious exception propagating past a graceful block).
            try:
                event.invocation_state[_MODEL_BLOCKED_FLAG] = True
            except Exception:  # noqa: BLE001 - never let bookkeeping crash the hook
                pass
        elif kind == "before_tool":
            event.cancel_tool = message
            # Tell the paired AfterToolCall (same toolUseId) to skip re-evaluation.
            try:
                tuid = event.tool_use.get("toolUseId", "")
                if tuid:
                    event.invocation_state[_TOOL_BLOCKED_PREFIX + tuid] = True
            except Exception:  # noqa: BLE001
                pass
        elif kind == "after_tool":
            # No cancel field; overwrite the tool result so the (possibly poisoned)
            # output never reaches the model.
            tool_use_id = ""
            try:
                tool_use_id = event.tool_use.get("toolUseId", "")
            except Exception:  # noqa: BLE001
                pass
            event.result = {
                "toolUseId": tool_use_id,
                "status": "error",
                "content": [{"text": message}],
            }
        elif kind == "after_model":
            # No cancel field and no safe replacement; re-raise to halt the
            # invocation. AIGuardAbortError is BaseException-derived so it
            # propagates past the agent loop's generic except Exception.
            raise error if error is not None else self._AIGuardAbortError(
                action="DENY", reason=reason
            )

    # ------------------------------------------------------------------ #
    # Circuit-breaker lock write (Path A — unforgeable)
    # ------------------------------------------------------------------ #
    def _write_lock(self, *, reason: str) -> bool:
        """Write the circuit-breaker LOCK record. Returns True iff it persisted."""
        ttl = int(time.time()) + LOCK_TTL_SECONDS
        try:
            self._ddb.put_item(
                TableName=self._risk_table,
                Item={
                    "principalId": {"S": self._principal_id},
                    "status": {"S": "LOCKED"},
                    "reason": {"S": reason[:500]},
                    "ts": {"N": str(int(time.time()))},
                    "ttl": {"N": str(ttl)},
                },
            )
            logger.warning(
                "AI Guard circuit breaker TRIPPED for principal=%s (ttl=%ds)",
                self._principal_id, LOCK_TTL_SECONDS,
            )
            return True
        except Exception as err:  # noqa: BLE001 - never let the write crash the agent
            logger.error("Failed to write AI Guard lock record: %s", err)
            return False

    # ------------------------------------------------------------------ #
    # Strands ContentBlock[]  ->  AI Guard Message[]  converter
    # ------------------------------------------------------------------ #
    def _messages_from(
        self,
        agent,
        pending_tool: Optional[dict] = None,
        pending_result: Optional[dict] = None,
    ) -> list:
        """Convert the agent's Strands conversation into AI Guard Message dicts.

        Strands ``content`` is a list of ContentBlock dicts ({'text':..},
        {'toolUse':..}, {'toolResult':..}). AI Guard wants flat-string content,
        plus structured ``tool_calls`` on assistant turns and a ``tool`` role for
        tool results. ``pending_tool`` (BeforeToolCall) is appended as a synthetic
        assistant tool-call so the not-yet-executed call is evaluated.
        ``pending_result`` (AfterToolCall) is a Strands ``ToolResult`` dict that is
        not yet in ``agent.messages`` — appended as a synthetic tool message so the
        just-produced output is screened for indirect injection.
        """
        out: list = []
        sys_prompt = getattr(agent, "system_prompt", None)
        if sys_prompt:
            out.append(self._Message(role="system", content=str(sys_prompt)))

        for msg in getattr(agent, "messages", []) or []:
            role = msg.get("role", "user")
            content = msg.get("content", [])
            if isinstance(content, str):
                out.append(self._Message(role=role, content=content))
                continue

            text_parts: list[str] = []
            tool_calls: list = []
            for block in content:
                if not isinstance(block, dict):
                    # Defensive: a malformed/non-dict block must not crash the
                    # converter (it runs BEFORE the fail-closed evaluate boundary);
                    # stringify it so its content is still surfaced to AI Guard.
                    text_parts.append(str(block))
                    continue
                if "text" in block:
                    text_parts.append(block["text"])
                elif "reasoningContent" in block:
                    # Extended-thinking / reasoning: a plan can be embedded here, so
                    # include its text so AI Guard scores the reasoning, not just the
                    # final answer. Shape: {'reasoningContent': {'reasoningText': {'text': ...}}}.
                    rc = block.get("reasoningContent") or {}
                    rtext = ""
                    if isinstance(rc, dict):
                        rtext = (rc.get("reasoningText") or {}).get("text", "") if isinstance(rc.get("reasoningText"), dict) else ""
                    if rtext:
                        text_parts.append(rtext)
                elif "toolUse" in block:
                    tu = block["toolUse"]
                    tool_calls.append(
                        self._ToolCall(
                            id=tu.get("toolUseId", ""),
                            function=self._Function(
                                name=tu.get("name", ""),
                                arguments=json.dumps(tu.get("input", {})),
                            ),
                        )
                    )
                elif "toolResult" in block:
                    tr = block["toolResult"]
                    out.append(
                        self._Message(
                            role="tool",
                            content=self._stringify_tool_result(tr),
                            tool_call_id=tr.get("toolUseId", ""),
                        )
                    )

            if text_parts or tool_calls:
                m = self._Message(role=role, content="\n".join(text_parts))
                if tool_calls:
                    m["tool_calls"] = tool_calls
                out.append(m)

        if pending_tool:
            out.append(
                self._Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        self._ToolCall(
                            id=pending_tool.get("toolUseId", ""),
                            function=self._Function(
                                name=pending_tool["name"],
                                arguments=json.dumps(pending_tool.get("input", {})),
                            ),
                        )
                    ],
                )
            )

        if pending_result:
            # AfterToolCall: the output isn't in agent.messages yet. Append it as a
            # tool message so AI Guard screens the actual tool output.
            out.append(
                self._Message(
                    role="tool",
                    content=self._stringify_tool_result(pending_result),
                    tool_call_id=pending_result.get("toolUseId", ""),
                )
            )
        return out

    @staticmethod
    def _stringify_tool_result(tool_result: dict) -> str:
        parts: list[str] = []
        for c in tool_result.get("content", []) or []:
            if "text" in c:
                parts.append(c["text"])
            elif "json" in c:
                parts.append(json.dumps(c["json"]))
        return "\n".join(parts) if parts else json.dumps(tool_result)
