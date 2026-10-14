# Semantic control plane and source-native execution

## Decision

COA owns ontology lifecycle, semantic grounding, namespace admission, and the versioned `SemanticExecutionPlan`. It does not become the privileged data plane for enterprise systems that already own retrieval, authorization, freshness, and execution.

The consuming domain agent follows this sequence:

1. Submit the original question and namespace to `semantic_execution_plan` over MCP or `POST /namespaces/{namespaceId}/semantic-plan` over REST.
2. Reject any plan whose status is not `RESOLVED`, whose version is unknown, or whose target is not present in the administrator-owned target registry.
3. Execute every read-only source directive through its native adapter under trusted caller context.
4. Assemble bounded evidence with target, source, and provenance identifiers.
5. Invoke the configured generation provider once. Every generated claim must reference known evidence IDs.

The existing end-to-end `query` operation remains available as a turnkey compatibility path.

## Stable contracts

### Namespace integration profile

Each namespace persists:

- `mode`: `ACCELERATE`, `ADOPT`, or `FEDERATE`.
- `profileVersion`: version of routing/configuration.
- `ontologyVersion`: immutable approved semantic snapshot used for planning.
- `sourceTargets`: 1–8 logical, read-only target IDs and adapter kinds.
- `generationTarget`: logical final-generation provider.

Profiles never contain endpoints, role ARNs, tokens, or credentials. Adapters resolve those deployment details from administrator configuration. Namespaces created before this contract read as `legacy-unversioned`; semantic planning returns `AMBIGUOUS` and no directives until an owner binds an approved ontology version.

### Semantic execution plan

Version `1.0` preserves:

- original and ontology-grounded question;
- namespace, profile mode/version, and ontology version;
- resolved concepts and canonical entities;
- explicit ambiguities;
- logical source targets, directive language, read-only flag, scope reference, and expected evidence;
- final generation target.

A plan is interpretation and routing provenance, not evidence and not authorization.

## Profiles

| Profile | Starting point | COA responsibility | Authoritative execution |
|---|---|---|---|
| Accelerate | No shared ontology | Induce/import, review, validate, publish | Managed KB, governed documents, or COA reference path |
| Adopt | Existing ontology/model | Import and govern the TBox; hold only approved identity links | Existing KB and structured-data tools |
| Federate | Existing production graph | Ground with the team vocabulary and emit bounded graph intent | Team-owned graph/query API |

## Source adapter status

- **Managed KB:** implemented with direct `AgenticRetrieveStream`, `generateResponse=false`, administrator-bound KB IDs, and token-derived `userContext`.
- **Governed query service:** contract defined, but execution intentionally fails closed until the owner supplies its read-only tool, OAuth OBO/query-broker contract, approved objects, and result/provenance schema.
- **External graph:** contract defined, but execution intentionally fails closed until the owner supplies the read-only endpoint, identity/delegation contract, graph scope, query limits, and result/provenance schema.
- **COA graph:** plan contract defined. Production ABox/entity resolution remains separate from ontology catalog embeddings.

## Evidence controls

The outer agent executes every required directive or stops; it never generates from a partial evidence set. Managed KB output is normalized and bounded before model use. Final model output must be a strict claim list, and each claim must cite one or more evidence IDs present in the authorized context. Model input/output strips Unicode tag characters and orphaned surrogates; prompt canary leakage fails closed. UI Markdown preserves formatting but renders model-controlled links/images as inert labels.

## Entity resolution

`canonicalEntities` is part of the plan contract, but this distribution does not fabricate canonical entities from labels in generated SPARQL. A production linker must be namespace- and authorization-aware and retain identifiers, aliases, confidence, provenance, freshness, and merge/split decisions. Until connected, the field is empty and domain acceptance tests must not claim enterprise resolution.

## Continuous context updates

COA 0.3.5 already provides important primitives: recurring/event-driven source rescans, source fingerprints and diffs, ontology induction, R2RML/SHACL validation, proposal review, and versioned ontology artifacts. It does not yet provide the complete impact-and-approval engine across TBox, R2RML, SHACL, ABox, entity indexes, and retrieval indexes.

The production change engine must: detect → fingerprint → calculate impact → generate candidate delta → validate → approve → publish/reconcile. Additive metadata changes may be automated after contract tests. Semantic, identity, constraint, and breaking changes require owner approval and rollback data.

## Upgrade policy

- Pin an upstream release tag; this branch starts at `v0.3.5`.
- Keep customer-neutral protocols, Smithy contracts, adapters, and contract tests additive.
- Do not import private COA internals from customer-owned source adapters.
- Rebase onto a clean upstream tag, regenerate Smithy, run package/contract tests, and synthesize before promotion.
- Keep deployment-specific target IDs and credentials outside public source.

## Security invariants

- Inbound human identity is validated by AgentCore Runtime; request bodies cannot supply identity.
- COA namespace admission never replaces source authorization.
- Source target IDs are logical allowlist keys, not endpoints.
- All semantic directives are read-only.
- Secrets stay in Secrets Manager and never enter model context.
- The direct Managed KB call uses the runtime role for API authorization and the validated human identity for document filtering.
- No model output reaches SQL, SPARQL, shell, or another execution sink without the owning adapter's parser and policy validation.
