// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { OuterAgentStack } from "../../lib/stacks/services/outer-agent-stack";

const PORTKEY_SECRET =
  "arn:aws:secretsmanager:us-east-1:123456789012:secret:portkey-test-AbCdEf";
const ENV = { account: "123456789012", region: "us-east-1" };

function props(overrides: Record<string, unknown> = {}) {
  return {
    env: ENV,
    config: {
      sourceTargets: {
        "domain-kb": {
          kind: "MANAGED_KB" as const,
          knowledgeBaseIds: ["ABCDEFGHIJ"],
          maxAgentIterations: 5,
        },
        "analytics-query": { kind: "GOVERNED_QUERY" as const },
      },
      maxEvidenceItems: 12,
      maxEvidenceCharacters: 30000,
    },
    portkeyModelId: "model-through-gateway",
    portkeySecretArn: PORTKEY_SECRET,
    ...overrides,
  };
}

function app(): cdk.App {
  return new cdk.App({
    context: {
      resource_prefix: "coa",
      env: "test",
      outer_agent_image_uri:
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/outer-agent:latest",
    },
  });
}

function createTemplate(): Template {
  return Template.fromStack(new OuterAgentStack(app(), "OuterAgent", props()));
}

describe("OuterAgentStack", () => {
  it("uses JWT inbound auth and forwards only Authorization", () => {
    createTemplate().hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      AgentRuntimeName: "coa_test_outer_agent",
      AuthorizerConfiguration: {
        CustomJWTAuthorizer: {
          DiscoveryUrl: Match.anyValue(),
          AllowedAudience: Match.anyValue(),
        },
      },
      RequestHeaderConfiguration: {
        RequestHeaderAllowlist: ["Authorization"],
      },
    });
  });

  it("injects only the administrator-owned logical target registry", () => {
    createTemplate().hasResourceProperties(
      "AWS::BedrockAgentCore::Runtime",
      Match.objectLike({
        EnvironmentVariables: Match.objectLike({
          COA_MCP_RUNTIME_ARN: Match.anyValue(),
          SOURCE_TARGETS_JSON: JSON.stringify(props().config.sourceTargets),
          PORTKEY_MODEL_ID: "model-through-gateway",
          PORTKEY_SECRET_ARN: PORTKEY_SECRET,
        }),
      }),
    );
  });

  it("scopes dependent KB actions and enables direct agentic retrieval", () => {
    const policies = createTemplate().findResources("AWS::IAM::Policy");
    const statements = Object.values(policies).flatMap(
      (policy: any) => policy.Properties.PolicyDocument.Statement,
    );
    const kb = statements.find((statement: any) =>
      JSON.stringify(statement.Action).includes("bedrock:Retrieve"),
    );
    expect(JSON.stringify(kb.Resource)).toContain("knowledge-base/ABCDEFGHIJ");
    expect(kb.Resource).not.toBe("*");
    const agentic = statements.find((statement: any) =>
      JSON.stringify(statement.Action).includes(
        "bedrock:AgenticRetrieveStream",
      ),
    );
    expect(agentic.Resource).toBe("*");
    expect(
      statements.some((statement: any) =>
        JSON.stringify(statement.Action).includes("InvokeGateway"),
      ),
    ).toBe(false);
  });

  it("rejects malformed or unbound source targets", () => {
    expect(
      () =>
        new OuterAgentStack(
          app(),
          "BadTarget",
          props({
            config: {
              sourceTargets: {
                "../bad": {
                  kind: "MANAGED_KB",
                  knowledgeBaseIds: ["ABCDEFGHIJ"],
                },
              },
            },
          }) as any,
        ),
    ).toThrow("Invalid outer-agent target ID");

    expect(
      () =>
        new OuterAgentStack(
          app(),
          "MissingKb",
          props({
            config: { sourceTargets: { kb: { kind: "MANAGED_KB" } } },
          }) as any,
        ),
    ).toThrow("requires 1-10 valid knowledgeBaseIds");
  });

  it("grants Retrieve on the KB ARNs of the target's own Region", () => {
    const template = Template.fromStack(
      new OuterAgentStack(
        app(),
        "CrossRegion",
        props({
          config: {
            sourceTargets: {
              "hub-eu": {
                kind: "MANAGED_KB",
                knowledgeBaseIds: ["KBTEST0001"],
                region: "eu-central-1",
                dataSourceIds: ["DSTEST0001"],
              },
              "hub-local": {
                kind: "MANAGED_KB",
                knowledgeBaseIds: ["ABCDEFGHIJ"],
              },
            },
          },
        }) as any,
      ),
    );
    const statements = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ).flatMap((policy: any) => policy.Properties.PolicyDocument.Statement);
    const kb = statements.find((statement: any) =>
      JSON.stringify(statement.Action).includes("bedrock:Retrieve"),
    );
    const resources = JSON.stringify(kb.Resource);
    expect(resources).toContain(
      ":bedrock:eu-central-1:123456789012:knowledge-base/KBTEST0001",
    );
    expect(resources).toContain(
      ":bedrock:us-east-1:123456789012:knowledge-base/ABCDEFGHIJ",
    );
    expect(resources).not.toContain(
      "eu-central-1:123456789012:knowledge-base/ABCDEFGHIJ",
    );
    // The scope reaches the runtime verbatim; the grant stays on the KB.
    template.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      EnvironmentVariables: Match.objectLike({
        SOURCE_TARGETS_JSON: Match.stringLikeRegexp(
          '"dataSourceIds":\\["DSTEST0001"\\]',
        ),
      }),
    });
  });

  it("rejects region and data source scope outside the grammar or on a non-KB target", () => {
    const build = (id: string, target: Record<string, unknown>) => () =>
      new OuterAgentStack(
        app(),
        id,
        props({ config: { sourceTargets: { t: target } } }) as any,
      );
    expect(
      build("BadRegion", {
        kind: "MANAGED_KB",
        knowledgeBaseIds: ["ABCDEFGHIJ"],
        region: "eu-central-1; DROP",
      }),
    ).toThrow("region must be an AWS Region name");
    expect(
      build("BadDs", {
        kind: "MANAGED_KB",
        knowledgeBaseIds: ["ABCDEFGHIJ"],
        dataSourceIds: ["not valid"],
      }),
    ).toThrow("dataSourceIds must be 0-10 valid data source ids");
    expect(
      build("TooManyDs", {
        kind: "MANAGED_KB",
        knowledgeBaseIds: ["ABCDEFGHIJ"],
        dataSourceIds: Array(11).fill("DSTEST0001"),
      }),
    ).toThrow("dataSourceIds must be 0-10 valid data source ids");
    expect(
      build("GraphWithRegion", { kind: "COA_GRAPH", region: "eu-central-1" }),
    ).toThrow("does not accept knowledgeBaseIds, region or dataSourceIds");
  });
});
