// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Fork (ADR 0011): a BEDROCK_MANAGED_KB document source reads back as the
// knowledge base it is, not as an S3 source with three empty cells.

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SourceDetail } from "./SourceDetail";

const mockGetSource = vi.hoisted(() => vi.fn());
const mockGetSourceScanJob = vi.hoisted(() => vi.fn());

vi.mock("@api-hooks", () => ({
  useGetSource: mockGetSource,
  useDeleteSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRescanSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useListSourceTables: () => ({ data: { items: [] }, isLoading: false }),
  useApproveSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRejectSource: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useGetSourceScanJob: mockGetSourceScanJob,
  useKeepRescanRemoval: () => ({ mutate: vi.fn(), isPending: false }),
  usePutSourceRescanSchedule: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
    isSuccess: false,
  }),
  usePutSourceEventRescan: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  }),
}));

vi.mock("@coa/control-plane-client", () => ({
  ReviewDecision: { APPROVED: "APPROVED", REJECTED: "REJECTED" },
  ReviewStatus: {
    APPROVED: "APPROVED",
    PENDING_REVIEW: "PENDING_REVIEW",
    REJECTED: "REJECTED",
  },
  SourceStatus: {
    PENDING_REVIEW: "PENDING_REVIEW",
    APPROVED: "APPROVED",
    APPROVING: "APPROVING",
    REJECTING: "REJECTING",
    SCANNING: "SCANNING",
    ENRICHING: "ENRICHING",
    SCAN_FAILED: "SCAN_FAILED",
    APPROVAL_FAILED: "APPROVAL_FAILED",
    REJECTION_FAILED: "REJECTION_FAILED",
  },
  ReviewSourceTableCommand: class {
    input: Record<string, unknown>;
    constructor(input: Record<string, unknown>) {
      this.input = input;
    }
  },
}));

vi.mock("@components/ControlPlaneClientProvider", () => ({
  useControlPlaneClient: () => ({ send: vi.fn() }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={["/namespaces/ns-1/sources/src-1"]}>
      <Routes>
        <Route
          path="/namespaces/:namespaceId/sources/:sourceId"
          element={children}
        />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>
);

const KB_ROLE_ARN =
  "arn:aws:iam::222222222222:role/coa-dev-datasource-access-kb";

function setSource(kbConfig: Record<string, unknown>) {
  mockGetSource.mockReturnValue({
    data: {
      body: {
        sourceId: "src-1",
        name: "Knowledge Hub",
        sourceType: "DOCUMENTS",
        sourceSubType: "BEDROCK_MANAGED_KB",
        status: "APPROVED",
        createdAt: "2026-01-01T00:00:00Z",
        documentDetails: {
          s3Prefixes: [],
          bedrockKnowledgeBaseConfiguration: kbConfig,
        },
      },
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  });
}

describe("SourceDetail — BEDROCK_MANAGED_KB sub-type", () => {
  beforeEach(() => {
    mockGetSource.mockReset();
    mockGetSourceScanJob.mockReset();
    mockGetSourceScanJob.mockReturnValue({ data: undefined });
  });

  it("names the source as a knowledge base and shows its configuration", () => {
    setSource({
      knowledgeBaseId: "ABCDEFGHIJ",
      region: "eu-central-1",
      dataSourceIds: ["KLMNOPQRST", "UVWXYZ0123"],
      crossAccountRoleArn: KB_ROLE_ARN,
    });
    render(<SourceDetail />, { wrapper });

    expect(screen.getByText("Bedrock Knowledge Base")).toBeInTheDocument();
    expect(screen.queryByText("S3 bucket")).not.toBeInTheDocument();
    expect(screen.getByText("ABCDEFGHIJ")).toBeInTheDocument();
    expect(screen.getByText("eu-central-1")).toBeInTheDocument();
    expect(screen.getByText("KLMNOPQRST")).toBeInTheDocument();
    expect(screen.getByText("UVWXYZ0123")).toBeInTheDocument();
    expect(screen.getByText(KB_ROLE_ARN)).toBeInTheDocument();
    // The S3 trio is not the shape of this source.
    expect(screen.queryByText("Source Bucket ARN")).not.toBeInTheDocument();
    expect(screen.queryByText("S3 Prefixes")).not.toBeInTheDocument();
  });

  it("reads the omitted optionals as what the server does, not as dashes", () => {
    setSource({ knowledgeBaseId: "ABCDEFGHIJ" });
    render(<SourceDetail />, { wrapper });

    expect(screen.getByText("(this deployment's)")).toBeInTheDocument();
    expect(screen.getByText("(all)")).toBeInTheDocument();
    expect(screen.getByText("(this deployment's account)")).toBeInTheDocument();
  });
});
