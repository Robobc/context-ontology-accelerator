// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as iam from "aws-cdk-lib/aws-iam";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import { Construct } from "constructs";
import { SCLStack } from "../../constructs";
import { resolveContext } from "../../context";
import { Paths } from "../../paths";
import type { OuterAgentConfig } from "../../types";
import { brandEnv } from "../../utils/brand-env";
import { parseEcrImageUri } from "../../utils/ecr-utils";

export interface OuterAgentStackProps extends cdk.StackProps {
  readonly config: OuterAgentConfig;
  readonly portkeyBaseUrl?: string;
  readonly portkeyModelId: string;
  readonly portkeySecretArn: string;
}

const TARGET_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const KNOWLEDGE_BASE_ID_PATTERN = /^[A-Za-z0-9]{10}$/;

/**
 * Trusted composition boundary: semantic plan -> native source execution ->
 * bounded evidence -> one final generation. Source target IDs are resolved
 * from administrator configuration, never from caller-supplied endpoints.
 */
export class OuterAgentStack extends SCLStack {
  public readonly agentRuntimeArn: string;

  constructor(scope: Construct, id: string, props: OuterAgentStackProps) {
    super(scope, id, props);
    this.addComponentTag("outer-agent");

    this.validateConfig(props.config);
    if (!props.portkeyModelId.trim()) {
      throw new Error("OuterAgentStack requires portkeyModelId");
    }
    if (!props.portkeySecretArn.trim()) {
      throw new Error("OuterAgentStack requires portkeySecretArn");
    }

    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;
    const { ssmPrefix } = resolveContext(this.node);
    const issuerUrl = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/issuer`,
    );
    const webClientId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/userpool-client-id`,
    );
    const mcpClientId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/mcp-client-id`,
    );
    const groupClaimName = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/authentication-group-token-name`,
    );
    const coaMcpRuntimeArn = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/mcp/runtime-arn`,
    );
    const guardrailId = ssm.StringParameter.valueForStringParameter(
      this,
      `${ssmPrefix}/bedrock/guardrail-id`,
    );

    const explicitImageUri = this.node.tryGetContext(
      "outer_agent_image_uri",
    ) as string | undefined;
    let imageUri: string;
    let dockerAsset: DockerImageAsset | undefined;
    if (explicitImageUri) {
      imageUri = explicitImageUri;
    } else {
      dockerAsset = new DockerImageAsset(this, "OuterAgentImage", {
        directory: Paths.root,
        file: "packages/outer-agent/Dockerfile",
        platform: Platform.LINUX_ARM64,
      });
      imageUri = dockerAsset.imageUri;
    }

    const runtime = new bedrockagentcore.Runtime(this, "OuterAgentRuntime", {
      runtimeName: this.prefixed("outer-agent").replace(/-/g, "_"),
      agentRuntimeArtifact:
        bedrockagentcore.AgentRuntimeArtifact.fromImageUri(imageUri),
      // @secure_recommendation: AgentCore validates the registered JWT before
      // application code derives Managed-KB userContext from its claims.
      // @amsec-loaf: mcp_authentication
      authorizerConfiguration:
        bedrockagentcore.RuntimeAuthorizerConfiguration.usingJWT(
          `${issuerUrl}/.well-known/openid-configuration`,
          undefined,
          [webClientId, mcpClientId],
        ),
      networkConfiguration:
        bedrockagentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      requestHeaderConfiguration: {
        allowlistedHeaders: ["Authorization"],
      },
      environmentVariables: {
        ...brandEnv(this.node),
        ENVIRONMENT:
          (this.node.tryGetContext("env") as string | undefined) ?? "dev",
        LOG_LEVEL: "INFO",
        AWS_REGION: region,
        GROUP_CLAIM_NAME: groupClaimName,
        COA_MCP_RUNTIME_ARN: coaMcpRuntimeArn,
        SOURCE_TARGETS_JSON: JSON.stringify(props.config.sourceTargets),
        PORTKEY_BASE_URL: props.portkeyBaseUrl ?? "https://api.portkey.ai/v1",
        PORTKEY_MODEL_ID: props.portkeyModelId,
        PORTKEY_SECRET_ARN: props.portkeySecretArn,
        BEDROCK_GUARDRAIL_ID: guardrailId,
        BEDROCK_GUARDRAIL_VERSION: "DRAFT",
        MAX_EVIDENCE_ITEMS: String(props.config.maxEvidenceItems ?? 12),
        MAX_EVIDENCE_CHARACTERS: String(
          props.config.maxEvidenceCharacters ?? 30000,
        ),
      },
      description:
        "Semantic planning, source-native retrieval, evidence assembly, and final generation",
    });

    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      }),
    );
    if (explicitImageUri) {
      const ecr = parseEcrImageUri(explicitImageUri);
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: [
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage",
            "ecr:BatchCheckLayerAvailability",
          ],
          resources: [ecr.repositoryArn],
        }),
      );
    } else if (dockerAsset) {
      dockerAsset.repository.grantPull(runtime.role!);
    }

    // @secure_recommendation: the runtime reads one imported rotating secret;
    // secret values never enter CloudFormation, SSM, or model context.
    secretsmanager.Secret.fromSecretCompleteArn(
      this,
      "PortkeySecret",
      props.portkeySecretArn,
    ).grantRead(runtime.role!);
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:ApplyGuardrail"],
        resources: [
          `arn:${cdk.Aws.PARTITION}:bedrock:${region}:${account}:guardrail/*`,
        ],
      }),
    );
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:InvokeAgentRuntimeForUser",
        ],
        resources: [coaMcpRuntimeArn, `${coaMcpRuntimeArn}/*`],
      }),
    );

    const knowledgeBaseArns = Object.values(props.config.sourceTargets)
      .flatMap((target) => target.knowledgeBaseIds ?? [])
      .map(
        (knowledgeBaseId) =>
          `arn:${cdk.Aws.PARTITION}:bedrock:${region}:${account}:knowledge-base/${knowledgeBaseId}`,
      );
    if (knowledgeBaseArns.length) {
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["bedrock:GetKnowledgeBase", "bedrock:Retrieve"],
          resources: [...new Set(knowledgeBaseArns)],
        }),
      );
      runtime.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ["bedrock:AgenticRetrieveStream"],
          // AgenticRetrieveStream currently has no resource-level IAM key;
          // target KB IDs are fixed in SOURCE_TARGETS_JSON and Retrieve itself
          // remains scoped to the exact KB ARNs above.
          resources: ["*"],
        }),
      );
    }

    new ssm.StringParameter(this, "OuterAgentRuntimeArnParam", {
      parameterName: `${ssmPrefix}/outer-agent/runtime-arn`,
      stringValue: runtime.agentRuntimeArn,
      description: "Source-native outer-agent Runtime ARN",
    });

    this.agentRuntimeArn = runtime.agentRuntimeArn;
    new cdk.CfnOutput(this, "OuterAgentRuntimeArn", {
      value: runtime.agentRuntimeArn,
    });
  }

  private validateConfig(config: OuterAgentConfig): void {
    const entries = Object.entries(config.sourceTargets);
    if (!entries.length || entries.length > 32) {
      throw new Error("OuterAgent sourceTargets must contain 1-32 targets");
    }
    for (const [targetId, target] of entries) {
      if (!TARGET_ID_PATTERN.test(targetId)) {
        throw new Error(`Invalid outer-agent target ID: ${targetId}`);
      }
      const knowledgeBaseIds = target.knowledgeBaseIds ?? [];
      if (target.kind === "MANAGED_KB") {
        if (
          !knowledgeBaseIds.length ||
          knowledgeBaseIds.length > 10 ||
          knowledgeBaseIds.some(
            (knowledgeBaseId) =>
              !KNOWLEDGE_BASE_ID_PATTERN.test(knowledgeBaseId),
          )
        ) {
          throw new Error(
            `Managed KB target ${targetId} requires 1-10 valid knowledgeBaseIds`,
          );
        }
      } else if (knowledgeBaseIds.length) {
        throw new Error(
          `Source target ${targetId} does not accept knowledgeBaseIds`,
        );
      }
      if (
        target.maxAgentIterations !== undefined &&
        (!Number.isInteger(target.maxAgentIterations) ||
          target.maxAgentIterations < 1 ||
          target.maxAgentIterations > 20)
      ) {
        throw new Error(
          `maxAgentIterations for ${targetId} must be an integer from 1 to 20`,
        );
      }
    }
    for (const [name, value] of [
      ["maxEvidenceItems", config.maxEvidenceItems],
      ["maxEvidenceCharacters", config.maxEvidenceCharacters],
    ] as const) {
      if (value !== undefined && (!Number.isInteger(value) || value < 1)) {
        throw new Error(`${name} must be a positive integer`);
      }
    }
  }
}
