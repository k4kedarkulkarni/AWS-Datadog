# L7 — Agentic Operations (Datadog track)

Operating an agent after it ships: draft a change, prove it helps, then decide whether to
deploy it.

| # | Notebook | What it does |
|---|----------|--------------|
| 1 | `1_agent_optimization.ipynb` | Reads the Harness's live system prompt, drafts a revision with Bedrock, and records both as immutable configuration bundle versions |
| 2 | `2_datadog_experiments.ipynb` | Scores both prompts against a versioned Datadog dataset built from real orders, so "is v2 better" has a number |

The split: **AgentCore versions the configuration, Datadog decides what ships.**

---

## Why this track differs from `aws-only`

The Datadog track turns off AgentCore's ADOT telemetry in two places:

- The specialist agents deploy with `disable_otel=True`. AI Guard's `ai_guard` span is a native
  ddtrace span and collides with ADOT's instrumentation wrapper, so telemetry goes through the
  in-container Datadog trace-agent instead.
- The managed Harness has its OTLP trace exporter pointed at Datadog.

The consequence is that **no OpenTelemetry spans reach CloudWatch**. Verified on live runs:
zero span records for a Harness session across both `aws/spans` and the runtime log group.

The `aws-only` L7 notebooks read those spans for everything they do — `evaluate()` and
`start_recommendation()` take `sessionSpans` inline, and batch evaluation and simulation read
via `CloudWatchDataSourceConfig`. With nothing to fetch they fail with
`Invalid length for parameter evaluationInput.sessionSpans, value: 0`.

This is the workshop's pluggability premise rather than a defect: replace the observability
layer and the evaluation layer built on top of it goes with it.

## What replaced what

| `aws-only` capability | On this track |
|---|---|
| Configuration bundles and version history | **Unchanged** — span-free, and Datadog has no equivalent |
| `start_recommendation` — draft a better prompt | Bedrock, called directly (notebook 1, Step 2) |
| `evaluate()` — score a session | Datadog **Experiments** over a saved dataset |
| Built-in quality evaluators | `LLMJudge(provider="bedrock")` plus a deterministic ground-truth check |
| Online evaluation | Datadog managed evaluations — already covered in `l6-observability/4_datadog_quality_evaluations.ipynb` |
| Dataset evaluation | Datadog **Datasets** — versioned, and can be fed from production traces via Automations |

## Not available here

These have no Datadog equivalent and remain in the `aws-only` track:

- **Simulation** — LLM-backed actor driving multi-turn conversations
- **Batch and on-demand session evaluation**
- **Five of the seventeen built-in evaluators** — `ToolSelectionAccuracy`,
  `ToolParameterAccuracy`, and the three `Trajectory*Match` variants

Tool-choice and trajectory scoring is the real gap. Datadog captures the Tool spans, but ships
no evaluators over them, so you would write a `BaseEvaluator` yourself.

## Order

Run notebook 1 before notebook 2 — notebook 2 reads the candidate prompt out of the bundle
notebook 1 writes.

Neither notebook modifies the running Harness. Promoting a winning prompt with
`update_harness` is left as a deliberate manual step, described at the end of notebook 2.
