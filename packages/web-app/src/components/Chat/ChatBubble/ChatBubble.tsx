// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useState } from "react";
import {
  Alert,
  Badge,
  Box,
  Button,
  ExpandableSection,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextContent,
} from "@cloudscape-design/components";
import type {
  TraceStep,
  SupportingContentItem,
  GraphContextItem,
} from "@app-types";
import { LiveSteps } from "./LiveSteps";
import { EvidencePanel } from "./EvidencePanel";
import { SafeModelMarkdown } from "./SafeModelMarkdown";
import "../chat.css";

export type ChatBubbleState = "loading" | "error" | "success";

export interface ChatBubbleProps {
  role: "user" | "assistant";
  state: ChatBubbleState;
  content: string;
  timestamp: Date;
  /** Message ID for memoization keys. */
  messageId?: string;
  response?: {
    tier: number;
    confidence: number;
    rationale?: string;
    synthesizedAnswer?: string;
    resultRows?: Record<string, unknown>[];
    queryUsed?: string;
    sparqlGenerated?: string;
    latencyMs?: number;
    guardrailBlocked?: boolean;
    suppressedRows?: number;
    trace?: TraceStep[];
    supportingContent?: SupportingContentItem[];
    graphContext?: GraphContextItem;
  };
  errorMessage?: string;
  errorDetails?: {
    error: string;
    statusCode: number;
    requestId?: string;
    details?: { field: string; type: string }[];
  };
  // Streaming state
  stage?: string;
  streamingTokens?: string;
  liveTraceSteps?: TraceStep[];
  /** Callback to open the rationale panel for this message. */
  onRationaleClick?: () => void;
  /** Callback to retry the query when an error occurs. */
  onRetry?: () => void;
}

export const ChatBubble: React.FC<ChatBubbleProps> = ({
  role,
  state,
  content,
  response,
  errorMessage,
  errorDetails,
  messageId: _messageId,
  stage: _stage,
  streamingTokens: _streamingTokens,
  liveTraceSteps,
  onRationaleClick,
  onRetry,
}) => {
  const isUser = role === "user";

  if (isUser) {
    return <Box variant="p">{content}</Box>;
  }

  if (state === "loading") {
    return <LiveSteps steps={liveTraceSteps} />;
  }

  if (state === "error") {
    if (errorDetails && errorDetails.statusCode === 400) {
      return (
        <ValidationErrorBubble
          message={errorMessage || content}
          errorDetails={errorDetails}
        />
      );
    }
    return (
      <SpaceBetween size="xs">
        <StatusIndicator type="error">
          {errorMessage || content}
        </StatusIndicator>
        {onRetry && (
          <Button variant="inline-link" onClick={onRetry}>
            Retry
          </Button>
        )}
      </SpaceBetween>
    );
  }

  if (!response) return null;

  if (response.guardrailBlocked) {
    // Render the blocked answer through Markdown (same pipeline as the normal
    // path) so headings / bold / code / tables are formatted rather than shown
    // as raw Markdown syntax. Wrap in a warning Alert to keep the "blocked"
    // affordance. StatusIndicator (previously used here) renders plain text and
    // does not parse Markdown.
    return (
      <Alert type="warning" statusIconAriaLabel="Warning">
        <div style={{ fontSize: "14px", lineHeight: "20px" }}>
          <TextContent>
            <SafeModelMarkdown>
              {response.synthesizedAnswer || content}
            </SafeModelMarkdown>
          </TextContent>
        </div>
      </Alert>
    );
  }

  const suppressedText = response.suppressedRows
    ? ` (${response.suppressedRows.toLocaleString()} suppressed by SQL Firewall)`
    : "";

  const displayAnswer = response.synthesizedAnswer || content;

  // Which engine actually executed the SQL, read from the execute trace step's
  // engine tag (athena | redshift | jdbc). Falls back to a neutral label when the
  // tag is absent (older responses / non-SQL paths).
  const executionEngineLabel = ((): string => {
    const engineTag = (response.trace ?? [])
      .map((s) =>
        s && typeof s.detail === "object" && s.detail
          ? (s.detail as Record<string, unknown>).engine
          : undefined,
      )
      .find((e): e is string => typeof e === "string" && e.length > 0);
    switch ((engineTag ?? "").toLowerCase()) {
      case "athena":
        return "Athena";
      case "redshift":
        return "Redshift";
      case "jdbc":
        return "the JDBC engine";
      default:
        return "the query engine";
    }
  })();

  const sqlArtifactCaption = response.sparqlGenerated
    ? `The SPARQL the LLM emitted (Tier 2) and the SQL ${executionEngineLabel} executed.`
    : `The SQL ${executionEngineLabel} executed.`;

  return (
    <SpaceBetween size="s">
      <div style={{ fontSize: "14px", lineHeight: "20px" }}>
        <TextContent>
          <SafeModelMarkdown>{displayAnswer}</SafeModelMarkdown>
        </TextContent>
        {suppressedText && (
          <Box variant="span" fontSize="body-s" color="text-body-secondary">
            {suppressedText}
          </Box>
        )}
      </div>

      {response.resultRows && response.resultRows.length > 0 && (
        <ResultTable rows={response.resultRows} />
      )}

      <EvidencePanel
        supportingContent={response.supportingContent}
        graphContext={response.graphContext}
      />

      {(response.sparqlGenerated || response.queryUsed) && (
        <ExpandableSection
          headerText="Compiled artifacts"
          headerDescription={sqlArtifactCaption}
          variant="footer"
        >
          <SpaceBetween size="s">
            {response.sparqlGenerated && (
              <Box>
                <Box
                  variant="small"
                  fontWeight="bold"
                  padding={{ bottom: "xxs" }}
                >
                  SPARQL
                </Box>
                <CodeBlock code={response.sparqlGenerated} />
              </Box>
            )}
            {response.queryUsed && (
              <Box>
                <Box
                  variant="small"
                  fontWeight="bold"
                  padding={{ bottom: "xxs" }}
                >
                  SQL
                </Box>
                <CodeBlock code={response.queryUsed} />
              </Box>
            )}
          </SpaceBetween>
        </ExpandableSection>
      )}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        {onRationaleClick ? (
          <div style={{ marginLeft: "-12px" }}>
            <Button
              variant="link"
              iconName="angle-right"
              onClick={onRationaleClick}
            >
              Show trace
            </Button>
          </div>
        ) : (
          <span />
        )}
        <CopyButton content={displayAnswer} />
      </div>
    </SpaceBetween>
  );
};

// ── Internal components ─────────────────────────────────────────────

function ValidationErrorBubble({
  message,
  errorDetails,
}: {
  message: string;
  errorDetails: {
    error: string;
    statusCode: number;
    requestId?: string;
    details?: { field: string; type: string }[];
  };
}) {
  return (
    <SpaceBetween size="s">
      <Badge color="red">Validation error</Badge>
      <Alert
        type="error"
        header={`${errorDetails.statusCode} ${errorDetails.error}`}
      >
        <SpaceBetween size="s">
          <Box variant="p">{message}</Box>
          {errorDetails.details && errorDetails.details.length > 0 && (
            <KeyValuePairs
              columns={2}
              items={errorDetails.details.flatMap((d) => [
                { label: "Field", value: d.field },
                { label: "Type", value: d.type },
              ])}
            />
          )}
          <Box variant="small" color="text-body-secondary">
            Zero compute consumed. Boundary checks: namespace regex, query
            length, character allowlist.
          </Box>
        </SpaceBetween>
      </Alert>
      {errorDetails.requestId && (
        <Box variant="small" color="text-body-secondary">
          {errorDetails.requestId}
        </Box>
      )}
    </SpaceBetween>
  );
}

const MAX_DISPLAY_ROWS = 50;

function ResultTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (rows.length === 0) return null;
  const firstRow = rows[0];
  if (!firstRow || Object.keys(firstRow).length === 0) return null;
  const columns = Object.keys(firstRow);
  const displayRows = rows.slice(0, MAX_DISPLAY_ROWS);
  const truncated = rows.length > MAX_DISPLAY_ROWS;

  return (
    <SpaceBetween size="xs">
      <Table
        items={displayRows}
        columnDefinitions={columns.map((col) => ({
          id: col,
          header: col
            .replace(/_/g, " ")
            .replace(/\b\w/g, (c) => c.toUpperCase()),
          cell: (item: Record<string, unknown>) => String(item[col] ?? ""),
        }))}
        variant="embedded"
        wrapLines
        stripedRows
        contentDensity="compact"
      />
      {truncated && (
        <Box variant="span" fontSize="body-s" color="text-body-secondary">
          Showing {MAX_DISPLAY_ROWS} of {rows.length} rows
        </Box>
      )}
    </SpaceBetween>
  );
}

function CodeBlock({ code }: { code: string }) {
  return (
    <Box variant="code">
      <pre className="coa-code-block">{code}</pre>
    </Box>
  );
}

function CopyButton({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(content).catch(() => {
      // Silently fail if clipboard access is denied
    });
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Button
      iconName={copied ? "status-positive" : "copy"}
      variant="icon"
      ariaLabel="Copy answer"
      onClick={handleCopy}
    />
  );
}
