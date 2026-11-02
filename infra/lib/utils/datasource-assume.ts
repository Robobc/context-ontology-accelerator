// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as iam from "aws-cdk-lib/aws-iam";

import { RESERVED_DATASOURCE_ROLE_SEGMENT } from "../aspects/reserved-role-prefix";

/**
 * Fork: the one `sts:AssumeRole` grant shape every consumer of a customer-owned
 * datasource-access role holds, for consumers outside the sources stack.
 *
 * `SourcesStack.assumeDatasourceAccessRoleStatement` (upstream, private) is the
 * same statement for the discovery Lambda, the enrichment task, the sources API
 * and the doc KG-build task. The serve runtime is the fifth consumer: it reads a
 * `BEDROCK_MANAGED_KB` source's Knowledge Base through the source's
 * `crossAccountRoleArn` on every request. The shape is duplicated here rather
 * than lifted out of the upstream class so the fork does not edit it; the
 * serve-stack test pins the two statements byte-equal.
 *
 * Two properties are load-bearing, as upstream documents them:
 *
 *   - **No account restriction.** The customer's role may live in any account.
 *     What bounds COA is the reserved NAME prefix (kept reserved by
 *     `ReservedRoleNamePrefix`) plus the target role's own trust policy.
 *   - **`Null: {"sts:ExternalId": "false"}`.** The role ARN is caller-supplied at
 *     source-create; the ExternalId, derived from the requesting namespace and
 *     never accepted from a request, binds an assume to the namespace entitled to
 *     it. An assume presenting none is denied, so a regression fails closed.
 *
 * @param reservedRolePrefix fully prefixed, e.g. `coa-dev-datasource-access-`
 *   (`this.prefixed(RESERVED_DATASOURCE_ROLE_SEGMENT)` on an `SCLStack`).
 * @param sid retained per call site so deployed policies keep their Sids.
 */
export function assumeDatasourceAccessRoleStatement(
  reservedRolePrefix: string,
  sid: string,
): iam.PolicyStatement {
  if (!reservedRolePrefix.endsWith(RESERVED_DATASOURCE_ROLE_SEGMENT)) {
    throw new Error(
      `reservedRolePrefix must end with "${RESERVED_DATASOURCE_ROLE_SEGMENT}", got "${reservedRolePrefix}"`,
    );
  }
  return new iam.PolicyStatement({
    sid,
    actions: ["sts:AssumeRole"],
    resources: [`arn:aws:iam::*:role/${reservedRolePrefix}*`],
    conditions: {
      Null: { "sts:ExternalId": "false" },
    },
  });
}
