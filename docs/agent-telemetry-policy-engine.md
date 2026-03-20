# Agent Telemetry & Policy Engine — Design Ideation

## Context

This document captures the design thinking behind building a policy engine that can deterministically assess agent behavior based on telemetry. The work began as a hands-on exploration of OTEL and LangSmith to build concrete knowledge before making architectural decisions.

---

## Guiding Principles

1. **Standard over proprietary.** Agents we evaluate will not instrument to our schema. We must meet them where they are. Any standard we depend on must be one agent developers are already adopting or willing to adopt.

2. **Market momentum.** The standard must have genuine and growing adoption across agent frameworks, not just theoretical support.

3. **Easy to implement.** Agents that don't already support the standard should be able to add it with minimal effort — ideally a thin wrapper or a one-page guide. Compliance cannot be a significant engineering investment.

4. **Deterministic policy evaluation.** Policy assessment must be reproducible. The same telemetry must produce the same policy result every time. This rules out LLM-based evaluation at the extraction/normalization layer (though LLM-based policies over structured inputs are acceptable).

5. **High fidelity over convenience.** When choosing between telemetry patterns, prefer the one that preserves more information. Lossy telemetry constrains which policies are evaluable — that constraint should be a deliberate choice by the agent developer, not an accident of the standard.

---

## Standards Landscape

### OpenTelemetry (OTEL)

- **What it is:** Vendor-neutral open standard for distributed tracing, metrics, and logs. The Gen AI semantic conventions define specific attributes and span structures for AI/LLM workloads.
- **Key attributes:** `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.tool.name`, etc.
- **Market momentum:** Strong and accelerating. OpenAI, Anthropic, Microsoft (Semantic Kernel), Google, and major cloud providers are contributing to the Gen AI semantic conventions working group. The trajectory is clear.
- **Challenges:** The spec is still evolving. Current framework implementations vary in completeness and structural choices. "OTEL support" does not guarantee structural consistency.
- **Instrumentation cost:** Low. A minimal instrumentation is one root span with a handful of attributes. Wrapper libraries exist for Python, TypeScript, Java, Go, and others.

### LangSmith

- **What it is:** LangChain's proprietary observability platform. Defines a Run schema (`run_type: llm | tool | chain | agent`, hierarchical parent/child runs, structured inputs/outputs).
- **Market momentum:** Strong within the LangChain ecosystem (LangChain, LangGraph, LlamaIndex). Limited outside it.
- **Challenges:** Proprietary. Agents not using LangChain must explicitly integrate. The schema is richer and more consistent than OTEL within its ecosystem, but has no governance beyond LangChain Inc.
- **LangSmith → OTEL:** LangSmith is moving toward OTEL as an ingestion and export format. Their newer SDK versions use OTEL internally. This makes LangSmith a viable *source* of OTEL data rather than a competing standard.
- **Instrumentation cost:** Low for LangChain users (built-in). Higher for non-LangChain agents.

### Decision: OTEL as the standard

OTEL is the right long-term bet. It is vendor-neutral, has the broadest framework coverage, is backed by major industry players, and is where the ecosystem is converging. LangSmith is treated as an ingestion source (via its OTLP export) rather than a competing standard.

---

## Challenges

### Structural variability

"OTEL support" does not mean structural consistency. Two agents both emitting OTEL can produce fundamentally different span hierarchies for the same semantic operation. This is the central challenge.

### No control over agents

We cannot require agents to instrument to our schema. We can influence but not mandate. Any solution must handle agents that are partially compliant or use rational but non-canonical patterns.

### Information loss in normalization

Converting from a lower-fidelity pattern to a higher-fidelity canonical structure is not possible — you cannot recover information that was never captured. Normalization can only transform structure, not restore lost data.

### LangSmith fidelity gap

Our hands-on LangSmith integration revealed that pure OTLP → LangSmith produces limited fidelity. Inputs/outputs are recoverable via `input.value`/`output.value` attributes and `gen_ai.choice` events, but tool call detail requires child spans (Pattern A), not events on a parent span. LangSmith's OTLP ingestion maps model inputs/outputs reasonably but does not surface tool execution detail from span events.

---

## OTEL Span Patterns for Agent Telemetry

Four rational patterns exist for representing agent telemetry in OTEL. "Rational" means structurally coherent and intentional — not arbitrary or random.

### Pattern A: Hierarchical (Gen AI Semconv canonical)

```
react.run  (root)
├── model.call       (child, one per LLM invocation)
│   attributes: gen_ai.request.model, input_tokens, input.value, output.value
├── tool.call        (child of model.call, one per tool invocation)
│   attributes: gen_ai.tool.name, tool.server, input.value, output.value
└── tool.call
```

- **Produced by:** OpenAI Agents SDK, Anthropic SDK (emerging), this implementation
- **Fidelity:** Highest. Per-operation timing, input, output, and status. True causal hierarchy.
- **Policy evaluability:** All policy types supported.

### Pattern B: Monolithic (single span)

```
react.run  (single span)
  events: gen_ai.output_item.done (tool calls as events)
  attributes: input.value, output.value
```

- **Produced by:** Simple/early implementations, agents prioritizing low instrumentation overhead
- **Fidelity:** Lowest. Tool calls present but per-tool timing and output not isolatable.
- **Policy evaluability:** Run-level policies only. Tool-level policies not evaluable.
- **Conversion to A:** Partial. Tool call names recoverable; timing and per-tool output not.

### Pattern C: LangSmith hierarchy

```
chain  (root)
├── llm   (typed child, run_type=llm)
└── tool  (typed child, run_type=tool, includes output)
```

- **Produced by:** LangChain, LangGraph via LangSmith OTLP export
- **Fidelity:** Matches Pattern A. Per-operation timing, input, output, and status preserved.
- **Conversion to A:** Lossless. `run_type` → span kind, parent/child relationships map directly.

### Pattern D: Flat siblings (workflow engines)

```
step.1.model  ─┐
step.2.tool   ─┤─ same trace_id, no parent/child
step.3.tool   ─┘
```

- **Produced by:** Some workflow and orchestration engines
- **Fidelity:** High. Per-operation timing and output preserved. True hierarchy inferred from sequence.
- **Conversion to A:** Near-lossless. Hierarchy reconstructed from timestamps and sequence numbers.

### Fidelity comparison

| | Per-op timing | Per-tool output | Causal hierarchy | Op-level status |
|---|---|---|---|---|
| A: Hierarchical | ✓ | ✓ | ✓ | ✓ |
| B: Monolithic | ✗ | ✗ | ✗ | ✗ |
| C: LangSmith | ✓ | ✓ | ✓ | ✓ |
| D: Flat siblings | ✓ | ✓ | inferred | ✓ |

### Decision: Pattern A as canonical

Pattern A preserves the highest fidelity and aligns with the direction of the OTEL Gen AI semconv. It is the canonical structure for the policy engine. Patterns C and D convert losslessly. Pattern B converts with data loss — agents producing Pattern B should be flagged as degraded, and policies requiring per-tool detail are not evaluable from Pattern B sources.

---

## Policy Engine Architecture

### The core problem

Raw OTEL spans are too variable and too low-level for policy authors to write rules against directly. Requiring policy authors to understand span structure differences across agents is unworkable.

### Solution: Semantic extraction layer

```
Raw OTEL spans  (any pattern)
      ↓
Concept Extractor  (pattern-aware, produces canonical concepts)
      ↓
PolicyContext  (fixed semantic schema)
      ↓
Policy Engine  (evaluates rules against concepts)
```

Policy authors write against semantic concepts, not spans. Extractors absorb structural variability. New agent patterns require updating an extractor, not rewriting policies.

### PolicyContext schema

```
Run
├── id, agent_id, model, started_at, duration_ms, status
├── input: string           # user message
├── output: string          # final agent response
└── signals (pre-computed)
    ├── tool_call_count
    ├── approval_request_count
    ├── human_engagement_count
    ├── output_word_count
    └── iteration_count

Steps[]  (ordered, typed)
├── model_call:  { input, output, model, tokens, duration_ms }
├── tool_call:   { name, server, input, output, duration_ms }
├── approval:    { server, status, outcome }
└── message:     { role, content }
```

### Policy examples

```yaml
# Process policy — requires span-level detail (Pattern A or C or D)
policy: approval_before_large_action
  condition:
    before(
      approval(status="approved"),
      tool_call(name="create_invoice")
    )

# Output quality policy — run-level, works with any pattern
policy: concise_response
  condition:
    run.signals.output_word_count < 200

# Interaction pattern policy — run-level
policy: low_human_engagement
  condition:
    run.signals.human_engagement_count <= 1

# Content policy — LLM-evaluated over structured input
policy: brand_tone
  condition:
    llm_eval(run.output, "Does this response follow a professional, concise tone?")
```

### Degraded mode

Agents producing Pattern B can still be evaluated against run-level policies (`output_word_count`, `human_engagement_count`, `brand_tone`). Tool-level policies (`approval_before_large_action`) are not evaluable and should be flagged as `unevaluable` rather than `pass` or `fail`.

---

## Open Questions

1. **Ingestion paths.** Beyond direct OTLP, should we support LangSmith API polling as an ingestion source? This would cover the LangChain ecosystem without requiring agents to change their instrumentation.

2. **Pattern detection.** Can pattern detection (A vs B vs C vs D) be made automatic, or does it require agent developers to declare which pattern they use?

3. **Tool call output capture.** Our current implementation captures tool call outputs from the Responses API response items. Agents using other execution models (e.g., function calling with separate tool result messages) may not include tool outputs in the same place. The extractor needs to handle this.

4. **Conformance profile.** Should we publish a formal OTEL conformance profile that agents can validate against, making the "does this agent produce evaluable telemetry" question answerable before onboarding?

5. **LLM-evaluated policies.** Where in the pipeline should LLM-based evaluation occur? The extraction layer must remain deterministic, but content policies (tone, brand adherence) may require LLM evaluation over the structured `PolicyContext`.

---

## Implementation Notes

This application (Agent Playground) currently implements Pattern A. Each agent run produces:
- A root `react.run` span with `input.value` (user message) and `output.value` (final response)
- A `prepare.prompt` child span
- A `model.call` child span with full token usage and `input.value`/`output.value`
  - One `tool.call` child span per MCP tool invocation, with `input.value` and `output.value`
- A `final.answer` child span

All spans are stored in the local Postgres `otel_spans` table, which serves as the source of truth for local telemetry. OTLP export to Jaeger and LangSmith are available as secondary sinks.
