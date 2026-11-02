// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// @examples for com.amazon.semanticcontext.datalayer operations.
// Extracted from the service definition to keep operation/shape
// definitions readable. `apply` merges the @examples trait back onto
// each operation, so the doc-coverage gate in linters.smithy still holds.
$version: "2"

namespace com.amazon.semanticcontext.datalayer

apply Query @examples([
    {
        title: "Ask a natural-language question"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", query: "What was total revenue in Q3 2025?", execute: true }
        output: {
            result: {
                tier: 1
                confidence: { score: 0.94, rationale: "Exact metric match on 'total revenue'" }
                resultRows: [
                    {
                        period: "Q3 2025"
                        revenue: "12450000"
                    }
                ]
                synthesizedAnswer: "Total revenue in Q3 2025 was $12.45M."
                trace: [
                    {
                        step: "metric-match"
                        status: "ok"
                        durationMs: 42
                    }
                ]
            }
            requestId: "req-01HZY8K3M4N5P6Q7R8S9T0V1W2"
        }
    }
])

apply PlanSemanticExecution @examples([
    {
        title: "Compile a question for source-native execution"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", query: "Show the current status for project P-123" }
        output: {
            plan: {
                planVersion: "1.0"
                status: "RESOLVED"
                namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
                profileMode: "ADOPT"
                profileVersion: "profile-v1"
                originalQuestion: "Show the current status for project P-123"
                groundedQuestion: "Show the current delivery status for canonical project P-123"
                ontologyVersion: "ontology-2026-10-14"
                sourceDirectives: [
                    {
                        targetId: "project-data"
                        targetKind: "GOVERNED_QUERY"
                        language: "SEMANTIC_INTENT"
                        query: "Show the current delivery status for canonical project P-123"
                        readOnly: true
                        requiresCompilation: true
                        expectedEvidence: ["ROW", "PROVENANCE"]
                    }
                ]
                expectedEvidence: ["ROW", "PROVENANCE"]
                generationTarget: "portkey"
            }
            trace: [
                {
                    step: "semantic-plan"
                    status: "ok"
                    durationMs: 240
                }
            ]
            requestId: "req-01HZY8K3M4N5P6Q7R8S9T0V1W2"
        }
    }
])

apply TranslateSPARQL @examples([
    {
        title: "Preview the SPARQL for a question"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", query: "List all customers in the enterprise segment" }
        output: {
            sparqlQuery: "SELECT ?customer WHERE { ?customer a :Customer ; :segment \"enterprise\" }"
            confidence: { score: 0.88, rationale: "Class and property matched from ontology" }
            trace: [
                {
                    step: "nl-to-sparql"
                    status: "ok"
                    durationMs: 310
                }
            ]
            ontologyVersion: "2025-09-01"
        }
    }
])

apply KBSearch @examples([
    {
        title: "Search the knowledge base"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", query: "refund policy for enterprise contracts", topK: 3, minScore: 0.5 }
        output: {
            chunks: [
                {
                    chunkId: "doc-42#p7"
                    text: "Enterprise contracts allow refunds within 30 days of invoice."
                    sourceDocumentId: "src-6f8b4f12"
                    sourceDocumentName: "Enterprise Terms.pdf"
                    relevanceScore: 0.82
                }
            ]
            trace: [
                {
                    step: "kb-search"
                    status: "ok"
                    durationMs: 128
                }
            ]
            queryEmbeddingModel: "amazon.titan-embed-text-v2"
        }
    }
])

apply GraphTraverse @examples([
    {
        title: "Traverse from an entity"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", startUri: "urn:coa:customer:acme-corp", maxDepth: 2, direction: "outgoing" }
        output: {
            entities: [
                {
                    uri: "urn:coa:customer:acme-corp"
                    label: "Acme Corp"
                    type: "Customer"
                }
                {
                    uri: "urn:coa:order:o-1001"
                    label: "Order 1001"
                    type: "Order"
                }
            ]
            relationships: [
                {
                    sourceUri: "urn:coa:customer:acme-corp"
                    predicateUri: "urn:coa:rel:placed"
                    targetUri: "urn:coa:order:o-1001"
                    predicateLabel: "placed"
                }
            ]
            trace: [
                {
                    step: "graph-traverse"
                    status: "ok"
                    durationMs: 66
                }
            ]
        }
    }
])

apply GraphExpandChunks @examples([
    {
        title: "Expand the graph from retrieved Knowledge Base chunks"
        input: {
            namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
            chunkIds: ["Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw"]
            scope: "SEED_DOCUMENTS"
            maxFacts: 100
        }
        output: {
            entities: [
                {
                    id: "aws:0f1e2d3c4b5a469788aa:7e1a9c20:0001"
                    label: "Project Atlas"
                    class: "Project"
                }
                {
                    id: "aws:0f1e2d3c4b5a469788aa:7e1a9c20:0002"
                    label: "R&D Basel"
                    class: "Department"
                }
            ]
            facts: [
                {
                    id: "aws:0f1e2d3c4b5a469788aa:7e1a9c20:f001"
                    text: "Project Atlas HAS BUDGET 1.2M EUR (2026)"
                    subjectId: "aws:0f1e2d3c4b5a469788aa:7e1a9c20:0001"
                    chunkId: "Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw"
                    sourceDocumentId: "s3://knowledge-hub/finance/budget-2026.md"
                }
                {
                    id: "aws:0f1e2d3c4b5a469788aa:7e1a9c20:f002"
                    text: "Project Atlas CHARGED TO R&D Basel"
                    subjectId: "aws:0f1e2d3c4b5a469788aa:7e1a9c20:0001"
                    objectId: "aws:0f1e2d3c4b5a469788aa:7e1a9c20:0002"
                    chunkId: "Ukhx_oqNvL1daHhnb6mECpjhUOdYSeMwVnj6HnadKZw"
                    sourceDocumentId: "s3://knowledge-hub/finance/budget-2026.md"
                }
            ]
            relatedChunks: []
            withheld: { seedChunks: 0, facts: 1, relatedChunks: 1 }
            trace: [
                {
                    step: "t3.graph_expand"
                    status: "success"
                    durationMs: 84
                }
            ]
        }
    }
])

apply DescribeSchema @examples([
    {
        title: "Describe the queryable schema"
        input: { namespaceId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", includeProperties: true }
        output: {
            classes: [
                {
                    uri: "urn:coa:class:Customer"
                    label: "Customer"
                    description: "A purchasing organization or individual"
                    properties: [
                        {
                            uri: "urn:coa:prop:segment"
                            label: "segment"
                            range: "xsd:string"
                        }
                    ]
                }
            ]
            ontologyVersion: "2025-09-01"
        }
    }
])
