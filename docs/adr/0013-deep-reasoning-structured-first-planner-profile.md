# ADR 0013: Let the deep-reasoning planner route a tabular sub-question to the structured tools first

## Status

Proposed. Implemented on `feature/deep-reasoning-structured-first` behind an
opt-in planner profile (`options.plannerProfile`, deployment default
`DEEP_REASONING_PLANNER_PROFILE`), default `legacy`, so no existing deployment
or request changes behaviour until a caller or an operator asks for it. Unit
and regression coverage is in
`packages/context-manager/tests/unit/test_tier3_agentic_planner_profile.py`.
The deployed A/B on the mixed question that motivated the change is recorded
under Verification as it is run.

Touches the deep-reasoning path only (ADR 0005's reasoning loop, as shipped in
`tier3/agentic/`). Changes nothing in the Standard cascade, and nothing in how
ADR 0011/0012 build or gate the Knowledge-Base graph.

## Context

Deep reasoning is the one COA path that can combine a figure from the tables
with what a document says about it in a single answer. That is what the PPM
work needs: *"For project PI0013123: what is its full cost from the data
vault, and what did the latest stage-gate memo decide about it?"* On the
`ppm360-kb-test` namespace (Databricks data vault + ignite tables, plus a
Managed Knowledge Base of 28 stage-gate memos, 49 induced classes), Tier 2
alone answers the cost half correctly (`13,322,629.26`), and `rag_retrieval`
alone finds the memo. Asked together in deep-reasoning mode, the answer never
contained the figure.

The request trace (`coa-query-20261101T213727Z.json`, syngenta-eba-dev) shows
why. The decomposer produced two sub-questions, one tabular and one about the
memo, and ran them in parallel. The five reasoning steps the request had were
spent as: `ontology_lookup` ×2 (one per sub-question), `graph_traversal` ×3,
`nl_to_sql` ×0; stop reason `max_steps_reached`. The serve log confirms the
structured tools were registered and not withheld
(`structured_tools_registered`, no `withholding` entry). The planner was
offered `nl_to_sql`, able to answer with it, and never selected it.

Four things in the shipped code combine to produce that:

1. **The planner prompt has a graph-first prior.** `_PROPOSE_SYSTEM` in
   `planner.py` states a "RECOMMENDED APPROACH — your standing strategy for
   questions about entities and their relationships (most questions)": ontology
   lookup, entity anchoring, edge-typed traversal, "apply it even when the
   question does NOT explicitly ask for it". The structured tools appear in the
   catalog but under no rule that would make the planner prefer them for a
   figure. A model at temperature 0 follows the standing strategy.
2. **The step budget is per request, not per sub-question.** `BudgetClock`
   is shared by every fork (`Req 13.9`, verified by
   `TestAggregateBudgetsProperty`); `DEEP_REASONING_MAX_STEPS` defaults to 5
   and the dev stack did not set it. Two sub-questions get two or three steps
   each, and the graph-first opening costs two of them before any answer tool
   runs. (`external-docs/content/deploying.md` described the cap as per
   sub-question; it is aggregate, and this ADR corrects the text.)
3. **Each fork resolves the ontology itself.** `AccumulatedContext.fork()`
   shares `ontology` by reference, but the forks are created before either has
   resolved it, and `merge()` does not copy a later resolution back. So every
   sub-question pays a reasoning step for the same lookup.
4. **The decomposer does not say what kind of sub-question it produced.**
   Nothing downstream knows that "full cost from the data vault" is tabular
   until the planner decides so under the prompt above.

None of this is fork-specific. `git diff` between this fork and upstream
`main` (`bacd1aa`, v0.3.8) is empty for `planner.py`, `controller.py` and
`budget.py`; the four causes ship in upstream as they do here.

### What the literature says

A short brief was collected before choosing the fix (22 sources, kept with the
working notes). The relevant findings:

- Routing *before* retrieval, by question type, is the common pattern:
  Adaptive-RAG (Jeong et al., NAACL 2024, arXiv 2403.14403) classifies query
  complexity and picks the retrieval strategy first; LlamaIndex's
  `RouterQueryEngine` and LangGraph's adaptive-RAG recipe put an explicit route
  node ahead of any retriever. GraphRAG (Edge et al., arXiv 2404.16130) itself
  separates entity-anchored "local" queries from "global" ones rather than
  making graph-first universal.
- Table-and-text QA (HybridQA 2004.07347, TAT-QA 2105.07624, StructGPT
  2305.09645, Chain-of-Table 2401.04398) consistently finds that figures,
  counts and aggregates need structured operations and that text-first
  pipelines do poorly on them.
- Text-to-SQL pipelines do schema linking as a separate first stage (DIN-SQL
  2304.11015, RESDSQL 2302.05965), and planner/executor designs (ReWOO
  2305.18323, LLMCompiler 2312.04511) commit to tool choice at planning time,
  which supports resolving shared context once before parallel execution.
- Fixed step caps are the norm (ReAct 2210.03629, IRCoT 2212.10509); no
  controlled study compares per-branch and global budgets, and none quantifies
  how much a prescriptive "standing strategy" system prompt degrades tool
  selection. Both remain hypotheses that our own A/B has to settle.
- Typed decomposition (Decomposed Prompting 2210.02406; tool-annotated plans in
  ReWOO / LLMCompiler) is the strongest-supported pattern for the durable fix:
  the decomposer tags the sub-question's modality and the controller seeds its
  first tool deterministically, with a fallback when the seeded tool returns
  nothing.

## Decision

Add a **planner profile** with two values, selected per request
(`options.plannerProfile`) or per deployment (`DEEP_REASONING_PLANNER_PROFILE`,
CDK context `deep_reasoning_planner_profile`), default `legacy`.

### `legacy`

The planner prompt, the loop and the budget exactly as shipped. The default
profile reproduces the previous `_PROPOSE_SYSTEM` byte for byte
(`propose_system_for("legacy") is _PROPOSE_SYSTEM`), and under it the retriever
performs no pre-resolve and records no extra trace step. A deployment that sets
nothing, and a request that sends nothing, runs this.

### `structured_first`

Two changes, both scoped to this profile:

**A. The modality rule replaces the graph-first mandate.** The first decision
the planner is told to make for every step is the sub-question's modality: (a)
*tabular*, a figure, amount, count, total, average, ranking, date or status that
lives in the namespace's tables, in which case the single candidate is
`nl_to_sparql` when the question is phrased in the ontology's concept names and
`nl_to_sql` otherwise, with an explicit instruction not to resolve the ontology
or anchor entities first; (b) *documents / relationships*, which follow the
graph strategy; (c) a tabular sub-question whose structured tool returned no
rows is treated as (b) from then on. The graph strategy keeps its steps but is
retitled "for DOCUMENT / RELATIONSHIP sub-questions (modality (b)) only" and
loses the "(most questions) … apply it even when the question does NOT
explicitly ask for it" framing. The brief's warning was specific: a second rule
stacked on top of the standing strategy makes the outcome depend on rule order
and wording, so the rule *replaces* that paragraph rather than following it
(`str.replace` with an `assert` that the prompt changed).

**B. The ontology is resolved once, before the fan-out.** Right after answer
language resolution and before `_run_subquestions_parallel`, the retriever
invokes the registered `ontology_lookup` tool itself, under the per-tool
timeout, and caches the result on the shared context so every fork starts with
it. This costs wall clock and no reasoning step. It is skipped when the tool is
absent or withheld for the request (document-only composition, an explicit
exclusion), and a failure is recorded as a degraded source and the loop proceeds
as before. The trace records `ontology_preresolved` with `success`, `skipped`
or `failed`, and `planner_profile` whenever the profile is not `legacy`.

**C. The step cap becomes a deployment knob** (`DEEP_REASONING_MAX_STEPS`, CDK
context `deep_reasoning_max_steps`, `SCL_DEEP_REASONING_MAX_STEPS` in
`scripts/deploy.sh`). Its default stays 5 and the dev stack keeps it there for
the A/B. The brief is right that raising it hides mis-routing rather than fixing
it: the agent still burns steps on graph anchoring, then succeeds slowly, and
cost grows with fan-out. The knob exists so that an operator can size the cap to
their fan-out deliberately, not as the fix.

**D, deferred.** Decomposer-tagged modality with a deterministically seeded
first tool is the durable fix and the one the literature supports best. It is
not in this change because it moves the decision out of the planner into the
decomposer's output contract and the controller, and the cheaper A+B pair tests
the prompt-prior hypothesis directly first. Phase 2 should seed only the first
tool and let the loop continue, fall back when the seeded tool returns empty,
and define a policy for "mixed" sub-questions (two seeds, or a split).

### Where the switch lives

- `AccumulatedContext.planner_profile` (copied by `fork()`), set once per
  request in `AgenticRetriever.resolve` from `options.plannerProfile` when it
  names a known profile, else the deployment default.
- `BedrockStepPlanner.propose_steps` reads the profile off the context it is
  given and selects the system prompt with `propose_system_for`.
- `InvokeRequest` rejects any `plannerProfile` outside
  `{legacy, structured_first}` with 400; `ServiceConfig` falls back to `legacy`
  with a warning on an unknown env value; the factory passes the configured
  default into the retriever.
- The MCP `query` tool gains a `plannerProfile` argument forwarded verbatim as
  `options.plannerProfile`; the Context Manager validates it.
- One serve image serves both profiles, which is what makes a same-question
  A/B on one deployment possible.

## Alternatives considered

- **Change the planner prompt in place, for everyone.** Simplest, and what the
  literature would justify. Rejected for this change because the fork rule is
  to accept only additions, and because the effect of a prompt change on an
  LLM's tool selection has to be measured before it becomes the default; the
  profile gives both arms on one image.
- **Raise `DEEP_REASONING_MAX_STEPS` on dev.** Would likely make the observed
  question pass and would teach nothing about routing. Kept as a knob, not
  used as the fix.
- **A separate deployment for the experiment.** More isolation, but a second
  serve runtime and MCP stack to keep in step; the per-request flag gives the
  same A/B at no deployment cost.
- **Do D now.** The best-supported design, but a larger change to the
  decomposer contract and the controller; sequenced after A+B so that a
  decomposer tagging error can be told apart from a planner routing error.

## Consequences

- Nothing moves by default: `legacy` is byte-identical to the previous prompt,
  no pre-resolve runs, and the new config and request fields are optional.
  Existing tests pass unchanged (3,592 context-manager unit tests).
- Under `structured_first`, a mixed question's tabular half can reach
  `nl_to_sql` on its first step and the shared budget is no longer spent on
  one ontology lookup per fork. The ontology is still resolved, once, so the
  controller's ontology-constrained traversal (`Req 3.4`) keeps working for the
  document half.
- Risks the brief names, to watch in the A/B: prompt-rule compliance is
  probabilistic even at temperature 0, so routing accuracy has to be measured
  across paraphrases; a pre-resolved ontology is the whole T-Box, so a very wide
  namespace adds noise to the planner context (today the planner already
  resolves and injects it, only later); a "mixed" sub-question can be
  classified wrong, which rule (c) only partly mitigates.
- A related finding is **not** addressed here: in Standard mode the same
  namespace answered a pure document question ("what did the memo decide")
  through Tier 2 SQL (`Gate_Status: Passed`), because the cascade lets a
  confident structured tier answer before documents are consulted. That is
  cascade routing, not planner routing, and belongs with upstream issue #417.
- `external-docs/content/serve.md` documents `plannerProfile`;
  `deploying.md` documents both knobs and corrects the step-cap wording.

## Verification

### Unit and regression (`test_tier3_agentic_planner_profile.py`, 37 tests)

- `propose_system_for`: `legacy` is the unchanged object; unknown and empty
  fall back to it; `structured_first` contains the modality rule, no longer
  contains "(most questions)", and keeps the rest of the prompt.
- The Bedrock planner sends the legacy prompt byte for byte under a default
  context and the rewritten one when the context carries `structured_first`.
- `fork()` copies the profile; request option overrides the deployment
  default; invalid values at either level fall back as specified.
- Pre-resolve over the real retriever, controller and registry: under `legacy`
  the ontology tool is never called before the loop and no trace row appears;
  under `structured_first` it is called once, both forks' planner calls see the
  ontology, `ontology_preresolved success` is recorded and `ontology_lookup`
  never appears as a reasoning step; skipped when the tool is absent or
  withheld; a raising tool yields `failed` and a partial answer; a result
  without an ontology item yields `skipped`.
- The observed failure in miniature, same planner double (looks the ontology up
  when its fork has none, then routes by modality), same tools, `max_steps=3`:
  `legacy` pays reasoning steps for the lookups and ends `max_steps_reached`;
  `structured_first` calls `nl_to_sql` once with the tabular sub-question and
  `graph_traversal` once with the document one and ends `sufficiency_satisfied`.
- Request-model validation, `ServiceConfig` env parsing, factory wiring, MCP
  forwarding (`execution.py`, `server.py`), and the two CDK environment
  variables (`infra/test/services/serve-stack.test.ts`) each have their own
  cases. `make lint` is green.

### Deployed A/B (syngenta-eba-dev, `ppm360-kb-test`)

Pending. Procedure: deploy serve and MCP from this branch with the profile left
at `legacy`; run the mixed question above in deep-reasoning mode twice, with and
without `options.plannerProfile=structured_first`; compare the traces on
`nl_to_sql` invocations and rows (`13322629.26` expected), the stop reason,
`ontology_preresolved`, step count and wall clock, and whether the answer
contains both the figure and the memo decision. Repeat with two paraphrases.
Results to be recorded here.
