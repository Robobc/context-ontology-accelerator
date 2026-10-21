# ✦ Context Ontology Accelerator

An open-source semantic context layer for AWS that combines knowledge graphs, formal ontologies, and rule-based systems with modern AI — enabling agents to retrieve context, validate it against business logic, and determine correct actions.

## Architecture

The system follows a **Scan → Model → Serve** workflow:

- **Scan** — Connect data sources, discover schemas, enrich metadata, ingest unstructured documents
- **Model** — Induce and manage ontologies, define metrics, build a unified semantic graph
- **Serve** — Query via SPARQL federation (VKG), traverse the knowledge graph, serve context to AI agents via MCP

For brownfield domains, COA can also operate as a governed semantic control
plane: `semantic_execution_plan` returns a versioned, non-executing plan while
an optional outer/domain agent invokes administrator-configured native sources
and performs one evidence-cited generation. Per-namespace Accelerate, Adopt, and
Federate profiles keep existing knowledge bases, structured platforms, and
production graphs authoritative. See
[Semantic control plane and source-native execution](docs/architecture/semantic-control-plane.md).

Access is governed by namespace isolation and role-based access control: namespace-scoped roles (owner, maintainer, data-steward, data-analyst) plus **platform-level roles** (`platform-admin`, `platform-viewer`) that apply across all namespaces. See the [control-plane docs](packages/control-plane/README.md) for the grants and authorization model.

## Quick Start

### Prerequisites

- Python 3.12, Node.js 22+, Docker
- [pnpm](https://pnpm.io/) (Node package manager — installed via `mise` or `npm install -g pnpm`)
- Java 17–24 (21 recommended) and Gradle (for Smithy codegen), plus Maven (for the
  Databricks connector `make deploy-dev` builds). JDK 25+ is not supported yet: the Athena
  Federation SDK the connector bundles fails its tests on it, and `make preflight` rejects it.
  **`brew install maven` also installs the newest OpenJDK and runs Maven on it** — after
  installing, check `mvn -v` and, if it shows Java 25+, point Maven at JDK 21:
  `export JAVA_HOME=$(/usr/libexec/java_home -v 21)` (`brew install --cask corretto@21` if
  you don't have it). `mise install` provides a supported JDK and Maven from `.mise.toml`.
- [uv](https://docs.astral.sh/uv/) (Python package manager)

### Setup

> **Use a release, not the tip of `main`.** We recommend starting from a tagged
> release — [github.com/aws/context-ontology-accelerator/releases](https://github.com/aws/context-ontology-accelerator/releases).
> `main` tracks ongoing development.

```bash
# Clone at the latest release tag (replace <tag> with a release from the link above)
git clone --branch <tag> https://github.com/aws/context-ontology-accelerator.git
cd context-ontology-accelerator

make setup      # install Python + CDK TypeScript dependencies
make format     # auto-format code
make lint       # check linting
make test       # run unit tests
```

**Full developer guide:** [external-docs/content/getting-started.md](external-docs/content/getting-started.md)

## Repository Structure

```
semantic-context/
├── models/              # Smithy API models (source of truth for API contracts)
├── smithy-generated/    # Auto-generated from Smithy (OpenAPI, Python interfaces, TS client)
├── infra/               # AWS CDK (TypeScript) — foundation + per-service stacks
├── packages/
│   ├── control-plane/           # Control Plane APIs
│   ├── data-layer/              # Data Layer APIs (query, retrieval, traversal)
│   ├── sources/                 # Unified data source ingestion (database + documents)
│   ├── ontology-engine/         # Ontology induction, reasoning (HermiT/ELK)
│   ├── metric-service/          # Metric authoring and resolution
│   ├── vkg/                     # Virtual Knowledge Graph (Ontop)
│   ├── mcp-server/              # MCP tools for AI agents
│   ├── context-manager/         # Serve layer Context Manager (query orchestration, upstream clients)
│   ├── outer-agent/             # Optional semantic-plan + source-native execution runtime
│   └── web-app/                 # React + Cloudscape frontend
├── libs/common/         # Shared Python config, logging, exceptions
├── libs/ts-shared/      # Shared TypeScript interfaces and constants
├── connectors/          # Athena federation connectors: toolkit, reference connector, Databricks
├── scripts/             # CI-agnostic build/test/deploy scripts
└── external-docs/       # Published documentation (getting-started, deployment guides)
```

## Tech Stack

| Layer                  | Technology                                               |
| ---------------------- | -------------------------------------------------------- |
| Languages              | Python 3.12, TypeScript                                  |
| API Contracts          | Smithy → OpenAPI + Python interfaces + TypeScript client |
| IaC                    | AWS CDK (TypeScript)                                     |
| Frontend               | React + Cloudscape Design System                         |
| Package Management     | uv (Python), pnpm (TypeScript)                           |
| Monorepo Orchestration | Nx                                                       |
| Testing                | pytest (unit + integ)                                    |
| Linting                | ruff, mypy (strict)                                      |
| Codegen                | Smithy CLI (Java 17 + Gradle)                            |

## Contributing

This repository is published as a read-only mirror. We are not accepting pull requests at this time. You are welcome to report bugs and share feedback through GitHub Issues. See [CONTRIBUTING.md](CONTRIBUTING.md) for details including direction for Amazon employees.

## What this fork changes

This fork tracks [`aws/context-ontology-accelerator`](https://github.com/aws/context-ontology-accelerator) and adds two features plus a short list of fixes found while running the accelerator against a 35-table Databricks catalog on Amazon Nova models. Upstream does not accept pull requests, so each change is a separate, revertable commit filed as an upstream issue and dropped on the next rebase once upstream ships its own.

**Branch model** (one release branch per upstream tag; `main` is an unmodified mirror of upstream):

| Branch | Upstream base | Status |
|---|---|---|
| `release/v0.3.6-eba` | `v0.3.6` (`00fed8d`) | current; patch series rebased, all suites green |
| `release/v0.3.5-eba` | `v0.3.5` (`052b5a2`) | frozen except hotfixes; `feature/semantic-control-plane-v035` is an alias of its handover tag |

Deploy from a tag, never from a branch tip. Each upstream release is a rebase of the patch series onto the new tag (`scripts/fork-drift-report.sh` measures the distance; `scripts/identifier-scan.sh` guards the series). Patches are listed by commit subject because hashes change on every rebase; `git log <upstream-tag>..release/<x>` lists the series in order.

**Features**

- *Semantic control plane integrations* (nine commits: Entra OIDC with app roles; integration profiles and model-text helpers; Portkey generation and managed-KB retrieval adapters; integration profile on the namespace contract; semantic execution plan action and MCP tool; source-native outer agent; infra wiring; logical vs transport sessions; architecture docs): typed `SemanticExecutionPlan` contract for outer agents, per-namespace Accelerate / Adopt / Federate profiles, structured query mappings preserved through acceptance (*preserve structured query mappings*).
- *`translate_sql`: governed SQL without execution* (`translateSql action returns governed SQL without executing it`; `translate_sql tool exposes governed SQL to agents`). An agent calls the MCP tool `translate_sql(query, namespaceId, dataSourceId?, target)` and receives the statement COA would have run — ontology-grounded, firewall-checked (read-only, namespace-scoped, per-principal table allowlist / column denylist, Cedar) — together with `dialect`, `executionTarget`, `tablesTouched`, `confidence`, the firewall decision and the trace. COA never executes it; the agent runs it on the source under the end user's own identity, so the source's row-level controls apply to that user. `target: native` rewrites a single-source Athena-federated statement into the engine's own dialect and three-part name (Databricks today) and fails closed otherwise (`cross_source`, `engine_not_transpilable`). `compile()` is the exact pre-execution prefix of the `query` path (shared helper), so the two cannot drift; `query` is unchanged. Not carried over from `query`: the execution-error correction shot (a `correct_sql` tool is the planned follow-up).

**Fixes (behaviour-neutral for Claude deployments)**

| Commit subject | Change | Why | Status |
|---|---|---|---|
| `fix(common): clamp maxTokens to the model's output ceiling` | `coa_common.bedrock` clamps `maxTokens` to the model's measured ceiling (Nova: 10000) | Nova rejects over-limit requests with `ValidationException`; cross-source relationship inference failed on every scan | carried |
| `fix(sources): raise single-table enrichment budget to 8192 tokens` | 4096 → 8192 | Nova Lite overflowed on a 73-column table; written back unenriched | carried |
| `docs(deploy): building with Finch instead of Docker` | `CDK_DOCKER=finch`, ECR Public login, VM disk pruning | | carried |
| `chore: ignore Maven target/ output of the Java connectors` | `.gitignore` | build output | carried |
| `fix(vkg): alias dotted column names so Ontop loads the mapping` | `packages/vkg/r2rml-ontop-compat.py` rewrites the R2RML Ontop sees | Ontop 5.x rejects `rr:column` / template / join columns whose name contains a dot even as delimited identifiers (`Fully qualified columns as "..." are not accepted`), so a mapping with one such column left the namespace's VKG permanently DEGRADED. Real ERP exports have such columns. The published mapping is untouched | carried |
| `feat(induction): force flag to re-induce after metadata-only steward edits` | `StartInduction` gains `force` (skip the structural-duplicate short-circuit); the Ontology page's "already inducted" alert offers *Induce anyway* | The duplicate check fingerprints table names, column types and FKs only, so steward edits to source metadata never change it: once a proposal was accepted it could not be superseded without a schema change (`duplicate_of`; `UpdateProposal` / `RejectProposals` refuse accepted proposals). Default unchanged | carried |
| preflight: resumed deployment reuses its own VPC when the quota is full | was part of the infra wiring commit | upstream `v0.3.6` ships `scripts/check-vpc-limit.sh` with the same check | **absorbed in v0.3.6**, dropped |
| `chore(fork): drift report and identifier scan for the release process` | `scripts/fork-drift-report.sh`, `scripts/identifier-scan.sh` (+ allowlist) | maintenance tooling for the rebase runbook | fork-only |

**Operator knobs (upstream default unchanged; set per deployment with `-c key=value`)**

| CDK context | Default | Effect |
|---|---|---|
| `retrieval_guardrail_prompt_attack_input_strength` (`feat(infra): retrieval guardrail PROMPT_ATTACK strength as a context knob`) | `MEDIUM` | PROMPT_ATTACK strength on the *retrieval* guardrail. Steward-authored column comments are instruction-shaped and get classified as attacks; `NONE` unblocks metadata enrichment at the cost of indirect-injection screening on Tier-3 documents |
| `serve_nl2sql_exec_timeout_s` (`feat(serve): SERVE_NL2SQL_EXEC_TIMEOUT_S override for per-statement timeout`) | `35` | Per-statement SQL execution timeout on the AgentCore path (env `SERVE_NL2SQL_EXEC_TIMEOUT_S`, clamped 5..300). Federated Athena→Databricks statements cost 20-40 s in metadata rediscovery alone |

Customer-specific material (schemas, golden question sets, onboarding kits, deployment values) is deliberately **not** in this fork.

**Release tags (deploy from a tag)**

| Tag | Contents |
|---|---|
| `v0.3.5-eba.1` | upstream `v0.3.5` + semantic control plane integrations + Nova fixes + operator knobs |
| `v0.3.5-eba.2` | `v0.3.5-eba.1` + `translate_sql` (serve action + MCP tool + native transpile) |
| `v0.3.5-eba.3` | `v0.3.5-eba.2` + VKG dotted-column fix (Ontop) + `force` re-induction + housekeeping. **Handover release.** |
| `v0.3.6-eba.1` | the same series rebased onto upstream `v0.3.6` (namespace language, revisioned proposal uploads, ENI pre-warming, VPC-limit preflight, …); the big integrations commit split into nine topical commits; preflight VPC-reuse fix dropped as absorbed. Verified by the full suites; first deployment pending |

## External Dependencies

This package depends on and may incorporate or retrieve a number of third-party
software packages (such as open source packages) at install-time or build-time
or run-time ("External Dependencies"). The External Dependencies are subject to
license terms that you must accept in order to use this package. If you do not
accept all of the applicable license terms, you should not use this package. We
recommend that you consult your company’s open source approval policy before
proceeding.

Provided below is a list of External Dependencies and the applicable license
identification as indicated by the documentation associated with the External
Dependencies as of Amazon's most recent review.

THIS INFORMATION IS PROVIDED FOR CONVENIENCE ONLY. AMAZON DOES NOT PROMISE THAT
THE LIST OR THE APPLICABLE TERMS AND CONDITIONS ARE COMPLETE, ACCURATE, OR
UP-TO-DATE, AND AMAZON WILL HAVE NO LIABILITY FOR ANY INACCURACIES. YOU SHOULD
CONSULT THE DOWNLOAD SITES FOR THE EXTERNAL DEPENDENCIES FOR THE MOST COMPLETE
AND UP-TO-DATE LICENSING INFORMATION.

YOUR USE OF THE EXTERNAL DEPENDENCIES IS AT YOUR SOLE RISK. IN NO EVENT WILL
AMAZON BE LIABLE FOR ANY DAMAGES, INCLUDING WITHOUT LIMITATION ANY DIRECT,
INDIRECT, CONSEQUENTIAL, SPECIAL, INCIDENTAL, OR PUNITIVE DAMAGES (INCLUDING
FOR ANY LOSS OF GOODWILL, BUSINESS INTERRUPTION, LOST PROFITS OR DATA, OR
COMPUTER FAILURE OR MALFUNCTION) ARISING FROM OR RELATING TO THE EXTERNAL
DEPENDENCIES, HOWEVER CAUSED AND REGARDLESS OF THE THEORY OF LIABILITY, EVEN
IF AMAZON HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES. THESE LIMITATIONS
AND DISCLAIMERS APPLY EXCEPT TO THE EXTENT PROHIBITED BY APPLICABLE LAW.

| External Dependency | License | Source |
| ------------------- | ------- | ------ |
| owlready2 | LGPL-3.0 | https://pypi.org/project/owlready2/ |

## License

This project is licensed under the Apache License 2.0 — see [LICENSE](LICENSE) for details.
