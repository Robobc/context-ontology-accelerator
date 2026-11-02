# ADR 0014: A typed Decider for the bounded decisions in deep reasoning, and a seeded first tool per sub-question

## Status

Proposed, design only. Nothing in this ADR is implemented; it records the
design agreed on 2026-11-02 so that the implementation can be reviewed against
it. Follows ADR 0013, whose deployed A/B is the evidence this design answers.

## Context

ADR 0013 fixed the deep-reasoning mis-routing of a tabular sub-question by
rewriting the planner prompt (`structured_first`) and resolving the ontology
once before the fan-out. The A/B showed where that stops working:

- The modality rule is a sentence the planner may or may not follow. It went
  from 0/2 to 2/2 after one rewrite, on one model, on one question. A prompt
  rule is a probabilistic control over a decision that is in fact a bounded,
  typed one: *is this sub-question tabular, documentary, or both?*
- Even when the planner routes correctly it hands the structured tool its own
  paraphrase of the sub-question. Round 2 of the A/B returned 51 rows from the
  same engine and the same 13-table context that return one row for the
  sub-question verbatim. The decision "which tool" was right; the free-form
  "with what input" was not.
- Every run still ended `max_steps_reached` because the document half spent
  three or four steps on evidence the user is not entitled to see. The graph
  tool knew (it reported "7 statements and 2 entities withheld by document
  entitlement"); the loop did not act on it.
- Each planner step costs 1.5–3 s of planner inference before any tool runs.

Sandy's proposal of a `check_answerable` MCP tool, modelled on NVIDIA Auto
Ontology's pre-flight, asks for the same kind of decision one step earlier:
*does this semantic layer cover the concepts in this question, and with which
of its parts?* And the Jev-style classifiers now being discussed internally
(TypeSafe AI's Jev; Sark's experimental `Judgement` emulation on Bedrock) are
built for exactly this shape: a text state, typed questions, an answer as a
choice from a list or a 0–1 belief, in tens to hundreds of milliseconds, with a
failure mode that is a wrong pick rather than a wandering plan.

Constraints that shape the design:

- COA is upstream open source and deployed in a customer's own account. A
  third-party classifier reached over the public internet (Jev today is served
  through OpenRouter, is not approved for Amazon-internal use, and is not on
  Bedrock) cannot be a dependency. Whatever ships must work with nothing but the
  generation client the deployment already has.
- Syngenta runs generation through Portkey (ADR 0002). Any decision path that
  only works on Bedrock Converse is an optimisation, not the baseline.
- The fork rule: additive, opt-in, defaults unchanged.
- The literature brief behind ADR 0013 rated "decomposer tags modality,
  controller seeds the first tool deterministically" (D) as the best-supported
  durable fix (Decomposed Prompting, ReWOO, LLMCompiler; pre-retrieval routing
  in Adaptive-RAG), with two cautions: seed only the first tool and let the loop
  continue, and define a fallback when the seeded tool returns nothing.

## Decision

### 1. One `Decider` abstraction, three backends, off by default

A small protocol in `coa_serve/tier3/agentic/decider.py`, modelled on the Jev
primitives but owned by COA:

```python
@dataclass(frozen=True)
class Choice:
    option: str            # one of the offered options, or "" when abstaining
    confidence: float      # 0.0–1.0

class Decider(Protocol):
    async def choose(self, *, state: str, question: str, options: Sequence[str]) -> Choice: ...
    async def believe(self, *, state: str, question: str) -> float: ...
    async def choose_many(self, *, state: str, questions: Sequence[tuple[str, Sequence[str]]]) -> list[Choice]: ...
```

`choose` is Jev's Choice, `believe` is its Noul, `choose_many` batches several
questions over one state in one call (the internal Jev benchmark measured
batching three questions at about a third of the cost of three calls; the same
holds for an LLM backend, where the state is sent once). A `Decider` never
raises into the loop: a timeout, a parse failure or an unknown option returns an
abstention (`option=""`, `confidence=0.0`), and every caller treats abstention
as "do what you did before this ADR existed".

Backends, selected by `DECIDER_BACKEND`:

| Value | What runs | When to use it |
|---|---|---|
| `off` (**default**) | No decider is built. Every caller takes its pre-ADR path. | Default; nothing moves. |
| `llm` | `LLMDecider` over the deployment's generation client (`LLMClient.converse`, so Bedrock *or* Portkey), `temperature=0`, a system prompt that permits only a JSON object `{"answers":[{"option":"…","confidence":0.0}]}`, options passed as a closed list, model `DECIDER_MODEL_ID` (default: the deployment's generation model, so no new model access is needed; Nova Micro / Lite or Claude Haiku are the intended cheap settings). On Bedrock, when the model supports forced tool use, the same class may send a `toolConfig` with `toolChoice={tool: decide}` so the shape is guaranteed rather than parsed; this is an optimisation behind `DECIDER_STRUCTURED_OUTPUT=auto|on|off`, never a requirement. | The baseline. What Syngenta would run. |
| `jev` | `JevDecider`, an HTTP adapter speaking a Jev-class API (state + typed questions → typed answers), endpoint `DECIDER_ENDPOINT`, credential from `DECIDER_SECRET_ARN`, same `Decider` surface. | Only when a Jev-class model is reachable from inside the deployment's trust boundary (for example once such a model is available on Bedrock or a private endpoint). Shipped as an adapter with a contract test against a recorded fixture, not exercised against a live third-party service from this repository. |

Shared knobs: `DECIDER_TIMEOUT_S` (default 3), `DECIDER_MIN_CONFIDENCE`
(default 0.7; below it a choice is treated as an abstention),
`DECIDER_MAX_STATE_CHARS` (default 6000). All exposed as CDK context
(`decider_backend`, `decider_model_id`, …) and `SCL_DECIDER_*` in
`scripts/deploy.sh`, following ADR 0013's knobs. `ServiceConfig` validates the
backend name and falls back to `off` with a warning, as it does for the planner
profile.

Observability: every decision records one trace step, `decider`, with
`detail="<purpose>: <option> (<confidence>) via <backend> in <ms>"` and a
`skipped` status with the reason when abstaining or off. The state the decider
saw is logged at debug level, truncated, so a wrong pick is a debuggable event
(the state and the option list are both inspectable), which is the property the
Jev benchmark singled out.

### 2. Phase D: the decomposer tags modality, the controller seeds the first tool

A third planner profile value, `seeded`, added to `PLANNER_PROFILES`
(`options.plannerProfile`, `DEEP_REASONING_PLANNER_PROFILE`). It includes
everything `structured_first` does (modality rule v2 in the prompt, ontology
pre-resolve) and adds:

**D1. Modality per sub-question.** After `decompose` and before the fan-out,
`AgenticRetriever.resolve` calls `decider.choose_many` once over all
sub-questions (state = the original question, the sub-questions, the names of
the registered-and-not-withheld tools, the catalog's table business names
already available from the structured tier's retrieval context, and the
pre-resolved ontology's class names, bounded by `DECIDER_MAX_STATE_CHARS`), one
question per sub-question: *"Is this sub-question answered by a figure, count,
amount, date or status in the relational tables (`tabular`), by what documents
say or how entities relate across them (`document`), or does it need both
(`mixed`)?"* Options: `tabular | document | mixed`. The result is written on
the sub-question: `SubQuestion.modality: str = ""` and
`SubQuestion.modality_confidence: float = 0.0` (both additive; the dataclass is
frozen, so `dataclasses.replace`). Trace: `subquestion_modality` with the
option and confidence per sub-question.

Why a separate decider call rather than asking the decomposer LLM to emit the
tag in the same JSON: the decomposer is a generative call on the chat model
whose output we already parse leniently; the modality is a bounded choice we
want from the cheapest, most constrained path, logged on its own, and swappable
for a Jev-class backend without touching the decomposer. The extra call is one
batched request (one state, N questions), 0.3–1 s on a small Bedrock model,
and it replaces at least one planner round-trip per tabular sub-question.

**D2. Seeded first step.** In `ReasoningController.run_subquestion`, before the
first `propose_steps`, when `sub_question.modality in {"tabular", "mixed"}`,
`modality_confidence >= DECIDER_MIN_CONFIDENCE`, and `nl_to_sql` is registered
and not withheld for the request, the controller consumes one step and invokes
`nl_to_sql` with `inputs={"query": sub_question.text}`, the sub-question
**verbatim**, recording `next_step_select` with
`detail="nl_to_sql: seeded (modality tabular 0.93)"`. No planner call is made
for that step. The loop then continues exactly as today: the rows land in the
accumulated context (the planner's context summary already renders them and
says "if these answer the sub-question, no further tools are needed"), the
sufficiency check runs, and if the seeded tool returned no rows the planner
takes over with the `structured_first` prompt, whose rule (c) tries
`nl_to_sparql` before the graph. `document` sub-questions get no seed. A
`mixed` sub-question is seeded structured-first because that step is the cheap
one and its rows then inform the graph half.

This is the ReWOO / LLMCompiler pattern cut down to its safe core: commit the
first tool at planning time, keep the reactive loop for everything after it,
and make the fallback the existing one.

**D3. Entitlement-aware stop.** `ToolResult` gains `withheld: int = 0`
(additive); `graph_tool.py` sets it from the counts it already computes for its
detail string, and the chunk expander / rag tool do the same where they gate.
In the controller's no-progress handling, a step that added nothing visible and
reported `withheld > 0` records `no_progress_step` with
`detail="… withheld by entitlement"` and counts as a REPEAT toward
`max_no_progress_steps` even when the approach was novel, because trying a new
path to the same withheld evidence is not escalation. This is deterministic and
needs no decider. A decider `believe` question (*"given these attempts, is
further graph exploration likely to surface evidence this user may see?"*) is
the optional refinement for the cases the counts do not catch, behind
`DECIDER_ENTITLEMENT_GATE=true`, default off.

**D4. The planner is still the planner.** Seeding changes one step; the
planner profile prompt, the controller's preference ordering, the
ontology-constrained traversal and every budget stay as they are. With
`DECIDER_BACKEND=off`, `seeded` degrades to `structured_first` and records
`decider skipped: backend off`, so a misconfiguration cannot change behaviour
silently.

### 3. `check_answerable`: the same decider, exposed before the call

A new data-layer operation `CheckAnswerable` (Smithy, so REST, streaming and MCP
get it from one definition), handler in `coa_serve`, MCP tool
`check_answerable(question, namespaceId)` on the mcp-server, placed in the
server's tool-sequencing instructions ahead of `query` the way Auto Ontology
does ("cheap pre-flight before the primary tool"). Steps, all as the signed-in
user:

1. Embed the question (the `query.embed` step, ~200 ms today).
2. **Tables**: the structured tier's existing retrieval of table/column
   business names, synonyms and glossary over the namespace catalog
   (`nl_to_sql_retrieve_hits`), filtered by the user's grants (`tableAllowlist`).
3. **Ontology**: class and property label match against the namespace T-Box
   (namespace-level, no entitlement).
4. **Documents**: a `Retrieve` with small `k` against the namespace's Managed
   KB sources as the user (ADR 0011's pattern), so document coverage is
   entitlement-aware by construction.
5. **Decide**: one `choose_many` over a state made of the question plus the
   top candidates from 2–4, with questions *"which of these covers concept X"*
   per extracted concept (Choice over the candidate list, abstaining when none
   does) and *"is the question answerable from what is covered"* (`believe`).

Response:

```json
{
  "answerable": "yes | partial | no",
  "modality": "tabular | document | mixed",
  "covered":   [{"concept": "full cost", "by": "table", "name": "project.full_cost", "entitled": true},
                {"concept": "stage-gate memo", "by": "document", "name": "ppm-docs", "entitled": false}],
  "uncovered": ["…"],
  "suggested": {"tool": "query", "mode": "deep-reasoning", "plannerProfile": "seeded"},
  "confidence": 0.82
}
```

`covered[].entitled` is the field NVIDIA's tool has no equivalent for and the
one that matters here: ADR 0013's A/B showed a semantic layer that *covers* the
memo for a user who may not *see* it. `check_answerable` must say "covered,
not entitled" rather than "answerable", or the outer agent will call `query`
and get a correct "no information" after 30 s. With `DECIDER_BACKEND=off` the
operation still works with retrieval scores alone: `answerable` from
thresholds on the hit scores, `modality` from which strata produced hits, no
`confidence`; the decider only upgrades the verdict. This is also the
component the outer agent (ADR 0008) and Agent Studio can use to choose
`rag_retrieval` vs `query` vs `query mode=deep-reasoning`, which addresses the
Standard-mode cascade finding from 2026-11-01 (a document question answered by
Tier 2 SQL) without touching the cascade.

Budget: embed + three retrievals + one decider call, 2–4 s on today's dev
numbers against 30–50 s for a deep-reasoning request. It is deliberately not
free; its job is to avoid the expensive call, not to be invisible.

### 4. Where things live

| Piece | Location |
|---|---|
| `Decider` protocol, `Choice`, `LLMDecider`, `JevDecider`, `build_decider(config, llm_client)` | `packages/context-manager/src/coa_serve/tier3/agentic/decider.py` |
| Config + env parsing (`decider_backend`, `decider_model_id`, `decider_endpoint`, `decider_secret_arn`, `decider_timeout_s`, `decider_min_confidence`, `decider_structured_output`, `decider_entitlement_gate`) | `coa_serve/config.py`; CDK `infra/lib/stacks/services/serve-stack.ts`; `scripts/deploy.sh` |
| `SubQuestion.modality`, `.modality_confidence`; `ToolResult.withheld` | `tier3/agentic/models.py`, `tier3/agentic/tools/base.py` |
| D1 call site | `AgenticRetriever.resolve`, after `answer_language`, before `_preresolve_ontology` |
| D2 seed, D3 stop | `ReasoningController.run_subquestion` |
| `seeded` profile | `tier3/agentic/planner.py` (`PLANNER_PROFILES`), `models.py` validator, mcp `PlannerProfileLiteral` |
| `CheckAnswerable` | Smithy model under `smithy/`, handler `coa_serve/check_answerable.py`, REST route via generated OpenAPI, MCP tool in `coa_mcp/server.py` + `tools/discovery.py` |

### 5. Rollout

1. `Decider` + `llm` backend + D1/D2/D4 + `seeded` profile, unit-tested with a
   scripted decider (the same double pattern as `ScriptedPlanner`), including
   the ADR 0013 regression pair extended with a third arm: `seeded` calls
   `nl_to_sql` with the sub-question verbatim as step 1 and never calls the
   planner for it. Deploy to dev with `DECIDER_BACKEND=llm`,
   `DECIDER_MODEL_ID` = Nova Micro, profile default still `legacy`.
2. Labelled set: 50–150 sub-questions over `ppm360-poc` and `ppm360-kb-test`
   with gold modality and gold first tool (the harness the literature brief
   asked for), half of them paraphrases. Report decider accuracy and confusion
   matrix separately from end-to-end answer correctness, first-tool accuracy,
   steps to first useful observation, budget-exhaustion rate, latency and cost
   per arm (`legacy`, `structured_first`, `seeded`).
3. D3 deterministic gate, measured on the same set by budget-exhaustion rate
   on un-entitled document questions.
4. `check_answerable` on the data layer and MCP, measured on coverage
   precision/recall against the labelled set plus a sample of out-of-scope
   questions (the "no" class).
5. `JevDecider` adapter with contract tests only, documented as "when a
   Jev-class endpoint is reachable from the deployment"; no live third-party
   call from this repository.

## Alternatives considered

- **Ask the decomposer LLM to emit the modality tag in its JSON.** Zero extra
  calls, and the simplest diff. Rejected as the primary path because it keeps
  the decision on the generative chat model and inside a leniently parsed
  payload, and because it cannot be swapped for a classifier backend. Kept as a
  possible `DECIDER_BACKEND=decomposer` later if the extra call proves to be the
  cost that matters.
- **Use Sark's experimental `Judgement`.** It is the right interface on
  Bedrock, but Amazon-internal, experimental ("do not build production-critical
  decision paths on it yet"), and coupled to Strands. COA is upstream open
  source; a 60-line protocol with the same shape is the better dependency.
- **Use TypeSafe's Jev directly.** Not reachable from a customer deployment
  without data egress to a third party; not approved internally; not on
  Bedrock. The `jev` backend exists so the day one is reachable the change is a
  configuration value.
- **Seed every step from a plan (full ReWOO).** Brittle when observations
  disagree with the plan, as ReWOO itself notes. Seeding only the first tool
  keeps the reactive loop and the existing fallbacks.
- **Make `seeded` the default.** Not before step 2 of the rollout produces
  numbers; the fork rule and ADR 0013's lesson (prompt behaviour varies with
  wording and model) both argue for opt-in until measured.

## Consequences

- Defaults unchanged: `DECIDER_BACKEND=off`, `plannerProfile=legacy`. The new
  dataclass fields default to empty/zero and no existing code reads them.
- Under `seeded`, a tabular sub-question's first step costs one tool call and
  no planner inference, with the sub-question text intact. The two failure modes
  the A/B exposed (rule not followed; input paraphrased) are removed by
  construction rather than by prompt wording, and a wrong modality is a logged
  event with a confidence and a bounded consequence (the structured tool returns
  no rows, the planner resumes).
- A Jev-class model becomes a configuration choice, not a redesign.
- `check_answerable` gives the outer agent and Agent Studio a 2–4 s way to
  choose the right COA call and to tell "not covered" from "covered but not
  entitled", at the price of one more operation on the data-layer surface.
- New latency added when on: one batched decider call per deep-reasoning
  request; one `check_answerable` call per outer-agent turn that uses it.
- To watch: decider accuracy on paraphrases and on genuinely mixed
  sub-questions; over-seeding (a `document` question tagged `tabular` costs one
  wasted SQL call before the planner resumes); state size on wide namespaces.
