# ADR 0014: A typed Decider for the bounded decisions in deep reasoning, and a seeded first tool per sub-question

## Status

Proposed. Rollout step 1 is implemented on `feature/deep-reasoning-structured-first`
(2026-11-02): the `Decider` protocol with the `off` and `llm` backends, the
live SSM-backed settings provider, the `seeded` planner profile (D1, D2, D4),
request-level `plannerProfile=seeded` and `deciderModelId`, the CDK and
`deploy.sh` knobs, and the unit coverage listed under Verification. D3, the
`check_answerable` operation and the labelled set are not yet implemented.
Follows ADR 0013, whose deployed A/B is the evidence this design answers.

One design change from the version first written: there is **no separate `jev`
HTTP backend**. The customer deployment routes all generation through Portkey (ADR 0002), and
a Jev-class model would be reached the same way: a model id the gateway
routes. The `llm` backend therefore takes any model id the deployment's
generation client can address (Bedrock model id, or a Portkey model slug), and
"use a Jev-class classifier" is a change of `modelId` in the SSM parameter, not
a code path. A dedicated adapter would only be needed for a classifier with a
non-chat API, which there is no reachable instance of today.

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
- The customer deployment runs generation through Portkey (ADR 0002). Any decision path that
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

Backends, selected by the live `backend` setting:

| Value | What runs | When to use it |
|---|---|---|
| `off` (**default**) | Nothing is called; every question is an abstention and every caller takes its pre-ADR path. | Default; nothing moves. |
| `llm` | `LLMDecider` over the deployment's generation client (`LLMClient.converse`, so Bedrock *or* Portkey), `temperature=0`, `max_tokens=512`, a system prompt that permits only `{"answers":[{"id":"q1","option":"…","confidence":0.0}]}`, options passed as a closed list and validated on the way back (an option outside the list is an abstention). `modelId` names the model: empty means the generation client's own default (no new model access needed); a Bedrock id such as Nova Micro / Lite or Claude Haiku is the intended cheap setting; through Portkey it can be any model slug the gateway routes, a Jev-class classifier included. The guardrail id is read through the same live provider the planner uses and the user-derived state is guard-tagged. | The baseline. What the customer deployment runs. |

Forced tool use on Bedrock Converse (so the JSON shape is guaranteed rather
than parsed) was considered and left out of step 1: Portkey is the baseline
route, the parser already treats any malformed reply as an abstention, and
the small models tested emit the object reliably at temperature 0. It remains
an optimisation to add behind a flag if parse failures show up in the trace.

**Live settings, no redeploy.** The settings are read on every call from a
provider, and the provider shipped is `DeciderConfigProvider`
(`coa_serve/decider_config.py`), a sibling of the guardrail provider: it
overlays ONE SSM parameter on the environment defaults on a 60 s TTL with
last-known-good on failure:

```
<SSM_PREFIX>/deep-reasoning/decider    (String, JSON; every key optional)
{"backend": "llm", "modelId": "amazon.nova-micro-v1:0",
 "minConfidence": 0.7, "timeoutS": 3, "maxStateChars": 6000}
```

Switching the decider on, off, or onto another model (a Jev-class slug
through Portkey, say) is one `aws ssm put-parameter`, effective on every warm
instance within a minute, and an operator can see it took effect in the
`decider_config_changed` log line and in the `decider` trace row, which names
the backend and model each request ran with. The environment defaults
(`DECIDER_BACKEND`, `DECIDER_MODEL_ID`, `DECIDER_MIN_CONFIDENCE`,
`DECIDER_TIMEOUT_S`; CDK context `decider_backend`, `decider_model_id`;
`SCL_DECIDER_BACKEND`, `SCL_DECIDER_MODEL_ID` in `scripts/deploy.sh`) only
apply when the parameter does not exist, so a deployment that never creates it
behaves exactly as its CDK context says. A parameter that is not valid JSON or
names an unknown backend is logged and ignored, never trusted. A request may
also pass `options.deciderModelId` to run one request on another model, which
is how two models are compared on one image without touching the parameter.

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

**D1. Modality per sub-question.** After `decompose` and the ontology
pre-resolve, before the fan-out, `AgenticRetriever.resolve` calls
`decider.choose_many` once over all sub-questions (state = the original
question, the numbered sub-questions, the structured tools registered and not
withheld for this request, and the pre-resolved ontology's class names capped
at 40, bounded by `maxStateChars`; the catalog's table business names are not
included in step 1 because the structured tier resolves them per call and the
ontology classes already name the domain), one question per sub-question: *"Is this sub-question answered by a figure, count,
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
| `Decider` protocol, `Choice`, `DeciderSettings`, `LLMDecider`, `parse_answers`, `modality_questions`, `modality_state` | `packages/context-manager/src/coa_serve/tier3/agentic/decider.py` |
| Live settings: `DeciderConfigProvider`, `settings_from_json`, `build_decider_config_provider` | `coa_serve/decider_config.py` (wired and primed in `main.py` beside the guardrail provider) |
| Env defaults (`decider_backend`, `decider_model_id`, `decider_min_confidence`, `decider_timeout_s`) | `coa_serve/config.py`; CDK `infra/lib/stacks/services/serve-stack.ts`; `scripts/deploy.sh` |
| `SubQuestion.modality`, `.modality_confidence`, `.seed_tool` | `tier3/agentic/models.py` |
| D1 call site (`_tag_modality`) | `AgenticRetriever.resolve`, after `_preresolve_ontology`, before the fan-out |
| D2 seed | `ReasoningController.run_subquestion`, first iteration |
| `seeded` profile | `tier3/agentic/planner.py` (`PLANNER_PROFILES`), `coa_serve/models.py` validator (+ `deciderModelId`), mcp `PlannerProfileLiteral` |
| D3 (`ToolResult.withheld`, entitlement-aware stop) | not yet implemented: `tier3/agentic/tools/base.py`, `graph_tool.py`, controller no-progress handling |
| `CheckAnswerable` | not yet implemented: Smithy model under `smithy/`, handler `coa_serve/check_answerable.py`, REST route via generated OpenAPI, MCP tool in `coa_mcp/server.py` + `tools/discovery.py` |

### 5. Rollout

1. **Done.** `Decider` + `llm` backend + live settings + D1/D2/D4 + `seeded`
   profile, unit-tested with a scripted decider (the same double pattern as
   `ScriptedPlanner`), including the ADR 0013 regression pair extended with a
   third arm: `seeded` calls `nl_to_sql` with the sub-question verbatim as step
   1 and never consults the planner for it. Deploy to dev with the stack
   defaults (`off`, profile `legacy`), then switch the decider on through the
   SSM parameter and run the A/B with `plannerProfile=seeded` per request.
2. Labelled set: 50–150 sub-questions over the two PPM test namespaces
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
- **Use TypeSafe's Jev directly, or ship a dedicated `jev` HTTP adapter.** Jev
  is not reachable from a customer deployment without data egress to a third
  party, is not approved internally, and is not on Bedrock. When a Jev-class
  model is reachable it will be through the gateway the deployment already
  trusts (Portkey for the customer deployment), as a model id, so the swap is one
  configuration value and no adapter is needed; one would be written only for
  a classifier whose API is not chat-shaped.
- **Seed every step from a plan (full ReWOO).** Brittle when observations
  disagree with the plan, as ReWOO itself notes. Seeding only the first tool
  keeps the reactive loop and the existing fallbacks.
- **Make `seeded` the default.** Not before step 2 of the rollout produces
  numbers; the fork rule and ADR 0013's lesson (prompt behaviour varies with
  wording and model) both argue for opt-in until measured.

## Verification

### Unit (step 1, `test_tier3_agentic_decider.py` 45 tests, `test_tier3_agentic_planner_profile.py` +24)

- `parse_answers`: valid answers, case-insensitive canonical options, an option
  outside the list, a missing answer, fenced/prose-wrapped JSON, confidence
  clamping and non-numeric confidence, positional answers without ids, garbage.
- `DeciderSettings`: default off, unknown backend → off, clamps;
  `settings_from_json` overlays defaults and rejects an unusable document.
- `LLMDecider`: implements the protocol; `off` abstains without a model call;
  call shape (state, closed options, `temperature=0`, model id, guard content,
  classifier system prompt); request model id outranks settings; empty model id
  means client default; state truncation; timeout, model error and guardrail
  block all abstain; a failing settings provider reads as `off`; settings are
  read live per call.
- `DeciderConfigProvider`: parameter path; absent parameter → env defaults;
  overlay; bad first read → env defaults; unknown backend ignored; SSM failure →
  last-known-good with immediate retry; TTL caching and refresh; `prime`.
- `seeded` profile over the real retriever, controller and registry: one
  batched decider call guard-tagged with the question; the tabular
  sub-question's first step is `nl_to_sql` with its text verbatim and the
  planner is not consulted for it; `decider` and `subquestion_modality` trace
  rows; pre-resolve still runs; backend `off` → `decider skipped`, behaves as
  `structured_first`; no decider wired → skipped; abstention → `failed`, no
  seed; low confidence → tagged, not seeded; `mixed` → seeded; structured
  tools withheld → no seed and the state says so; `options.deciderModelId`
  forwarded and named in the trace; `legacy` and `structured_first` never
  consult the decider; the controller ignores a seed for an unregistered tool;
  the ADR 0013 regression pair's third arm (`max_steps=3`, a planner that never
  proposes SQL: the figure comes from the seed alone, `sufficiency_satisfied`).
- Request model: `seeded` accepted, profiles pinned to `PLANNER_PROFILES`,
  `deciderModelId` validation; `ServiceConfig` env parsing and fallbacks;
  factory wiring with and without a live provider; MCP forwards `seeded`; CDK
  defaults (`DECIDER_BACKEND=off`, no empty `DECIDER_MODEL_ID`) and overrides.
- `make lint` green; 3,662 context-manager unit tests pass.

### Deployed, 2026-11-02 (dev deployment, PPM test namespace)

Serve v20/v21 and MCP v11/v12 deployed from `7b35895` then `d8184f4` with the
stack defaults (`DECIDER_BACKEND=off`, profile `legacy`). The Decider was then
switched on **without a redeploy** by writing
`<SSM_PREFIX>/deep-reasoning/decider` =
`{"backend":"llm","modelId":"<portkey-slug-for>/us.amazon.nova-micro-v1:0","minConfidence":0.7,"timeoutS":3}`
(dev generation runs through Portkey, so the model id is the Portkey slug); the
serve log showed `decider_config_loaded` with those values on every warm
instance within the TTL, and the parameter stays on for further testing. Same
mixed question and namespace as ADR 0013; `DEEP_REASONING_MAX_STEPS=5`.

| Run | Profile | Decider | First step of the tabular sub-question | `nl_to_sql` rows | Figure in answer |
|---|---|---|---|---|---|
| 1 | `seeded` | `q1=tabular(0.95) seed, q2=document(0.95)`, 1.2 s | `nl_to_sql: seeded`, verbatim | 51 | no |
| 2 | `seeded` | same tags, 1.1 s | seeded, verbatim; planner then re-ran `nl_to_sql` once (no new rows) | 51, 51 | no |
| 3 | `seeded` | same tags, 1.0 s | seeded, verbatim | 51 | no |
| 4 | `seeded` | same tags, 0.9 s | seeded, verbatim | **1** | **yes: 13322629.26** |
| 5 | `structured_first` | n/a | planner chose `nl_to_sql` on step 1 | 1 | yes |
| 6, 7 | `structured_first` | n/a | planner chose `nl_to_sql` on step 1 | 51 | no |

What the runs established:

- **D1 and D2 work as designed, every time.** Nova Micro through Portkey tagged
  both sub-questions correctly with confidence 0.95 in 4 of 4 runs, in about
  1 s for the batched call, and the controller made `nl_to_sql` the tabular
  sub-question's first step with the sub-question text verbatim in 4 of 4,
  with no planner call for that step. The planner-prompt route
  (`structured_first`) also chose `nl_to_sql` first in 3 of 3 runs today, so
  on this one question the two profiles now route identically; the seeded one
  does it without depending on prompt compliance and about a second faster.
- **The remaining variance is inside SQL generation, not in routing.** The
  `structured_tool_resolved` log line added in `d8184f4` shows two statements
  for the identical input text, 13-table context and model (Nova Pro via
  Portkey, temperature 0):
  `SELECT COALESCE(SUM(d.Full_Cost),0) … WHERE … = 'PI0013123'` (1 row, the
  figure) and
  `SELECT COALESCE(d.Full_Cost,0) … LEFT JOIN demand … WHERE … = 'PI0013123'`
  (51 rows, one per demand line, no project column). The 51 rows **are** the
  project's cost lines, but the row shape carries no project id, so the
  synthesizer, which sees the rows and not the SQL, concludes "none of the rows
  explicitly mention PI0013123". Across today's and yesterday's deep-reasoning
  runs the aggregate form came back in about 1 run of 4. This is a Tier-2
  property, not a planner one: it would affect Standard mode in the same
  proportion, and it is what a governed Tier-1 metric definition for "full
  cost" exists to remove.
- **Every run still ends `max_steps_reached`** on the document half, for the
  reason recorded in ADR 0013: the memo for PI0013123 is withheld by document
  entitlement for this user, and the planner spends 3–4 steps looking for it.
  D3 is the fix and is not yet implemented.
- Cost of the structured leg inside the loop: 20–23 s per `nl_to_sql` call
  (translation plus Databricks), so a planner re-run of the same SQL (run 2)
  costs a step and 20 s for nothing; the no-progress handling already counts
  it, and D3's "withheld" rule does not apply to it.

Verdict: the Decider and the seeded first step remove the two planner-side
failure modes by construction, and are switchable live. The figure now
depends on SQL generation choosing an aggregate, which routing cannot fix.

### Labelled set and harness, 2026-11-02 (rollout step 2)

`sim/ppm/eval/routing-dataset.yaml` (54 questions, 65 gold sub-questions: 26
tabular incl. 4 paraphrases, 17 document incl. 2 not entitled to the reader and
2 "figure named in a document" traps, 11 mixed with a gold decomposition) and
`sim/ppm/eval/routing_harness.py` (`decider` offline on Bedrock with the real
`LLMDecider`, `e2e` against the dev MCP per arm, `report`). Operational files,
not in the repository (customer-shaped data); the harness documents its
contract in `ROUTING-HARNESS.md`.

**Decider, offline, 3 repeats per sub-question (195 judgements per model):**

| Model | Accuracy | Tabular | Document | Confidence correct / wrong | Unstable sub-q | p50 call |
|---|---|---|---|---|---|---|
| Nova Micro | **97.4%** | 100% | 94.0% | 0.95 / 0.95 | 1/65 | 464 ms |
| Nova Lite | 90.3% | 100% | 77.4% | 0.93 / 0.92 | 1/65 | 534 ms |
| Claude Haiku 4.5 | 84.6% | 97.3% | 67.9% | 0.93 / 0.83 | 0/65 | 630 ms |

Every error is `document → tabular` (Haiku also `→ mixed`) on questions that
ask for a status or a figure *as reported in a document* ("what combined full
cost does the Seedcare summary report"); the `doc_vs_table_trap` tag scores
58% / 50% / 25%. Data-vault, budget and finance wording, paraphrases and
not-entitled items are 100% on Nova Micro. Two things follow: the smallest
model is the best classifier here, so the cheap default is also the accurate
one; and the confidence is not calibrated on Nova (0.95 on wrong answers), so
`minConfidence` cannot catch these errors and the modality question needs an
explicit rule that a named document source (report, memo, note, minutes,
summary) makes the sub-question `document` even when it asks for a number.
That rule is not applied yet, so that the next run measures it on held-out
items rather than on the ones that produced it.

**Deployed, 18 items × 3 arms (54 deep-reasoning runs, same reader):**

| | `legacy` | `structured_first` | `seeded` |
|---|---|---|---|
| `nl_to_sql` called when gold needs it (n=14) | 0% | 92.9% | **100%** |
| `nl_to_sql` in the first round | 0% | 92.9% | **100%** (all seeded) |
| `nl_to_sql` called on a document question (n=4) | 0% | 25% | 25% |
| SQL returned exactly one row (aggregate form) | n/a | 76.9% | 71.4% |
| `max_steps_reached` | 72.2% | 44.4% | 44.4% |
| mean tool steps | 4.6 | 3.2 | 3.2 |
| **answer correct, all** | 5.6% | 11.1% | 16.7% |
| answer correct, tabular (n=4) | 0% | 25% | 50% |
| answer correct, document (n=4) | 25% | 0% | 0% |
| answer correct, mixed (n=10) | 0% | 10% | 10% |
| wall time p50 / max | 30 / 50 s | 37 / 77 s | 38 / 88 s |

Reading it: the routing problem the two ADRs set out to fix is fixed. Under
`legacy` the structured tool was never reached on 14 questions that needed it;
under `seeded` it is reached first on all 14, the budget is exhausted in 44%
of runs instead of 72%, and 1.4 fewer tool steps are spent per question. The
deployed Decider agreed with the offline run (same tags, same single
over-routing on the Seedcare-summary trap).

End-to-end correctness is low in every arm, and the harness shows where it is
lost, which is the reason it scores the layers separately:

1. **SQL generation on the golden traps.** With routing right, the generated
   statement was wrong or unaggregated on `t_voi_mean_excl_cap` (10.95 and
   11.36 returned, two different statements, gold -388.2275: the 999 exclusion
   was not applied), `t_budget_pi0013064` (52 rows, no `SUM`, three times),
   `m_legacy_region_count` ("0 projects" for EAME/APAC, gold 8 and 17). These
   are Tier-2 properties that Standard mode shares; the governed-metric path
   (Tier 1) is what the product offers for them.
2. **Over-routing is worse than a miss.** On `d_seedcare_combined_cost` both
   non-legacy arms tagged the question tabular, ran SQL, and answered with a
   confident, plausible, wrong figure (462,926,640.94, the sum over every
   Seedcare project in the tables, where the document covers eight). A
   document question answered from the tables fails silently; the modality
   rule fix above is therefore the next Decider change.
3. **Document retrieval.** 0% on document questions in both non-legacy arms
   and 25% under `legacy` (the one correct answer is a refusal on a
   not-entitled document). `d_gate_decision_pi0013123` is the sharpest case:
   `stage-gate-memo-1` carries a per-document ACL that includes this reader,
   yet no arm surfaced its decision ("Terminated / Proposed"); the answers came
   from the status report's gate list, and several traces still report
   statements "withheld by document entitlement". Either the graph's
   entitlement gate keys on the library prefix and ignores the per-document
   override, or the memo chunk is not reachable from the anchored project
   entity. This is an ADR 0011/0012 question, not a routing one, and it is the
   largest single cause of the mixed questions scoring 10%: their tabular half
   is right (the figure is in the answer on 7 of 10) and their document half
   is empty.

The labelled set also exposed a cost of the fix: the structured leg is 20–40 s
inside the loop, so the arms that reach it are 7–8 s slower at p50 than
`legacy`, which failed faster.

Next, in order: the Decider modality rule for named document sources (then
re-run `decider` on items added after the rule, not on these); the
per-document ACL check on the KB graph gate; the governed metric for full
cost; D3.
Next, in order: (a) hand the synthesizer the structured leg's SQL (or its
WHERE filter) alongside the rows, so 51 cost lines filtered on PI0013123 are
read as PI0013123's cost lines rather than as unrelated data; (b) a governed
metric for full cost on the PPM namespaces, which makes the figure
deterministic and Tier 1; (c) D3; (d) the labelled set (rollout step 2) to
report decider accuracy on more than one question.

## Consequences

- Defaults unchanged: `backend=off`, `plannerProfile=legacy`. The new
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
