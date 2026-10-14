// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * AgentCore Runtime requires `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` to
 * be at least 33 characters (enforced server-side — a short value 400s with
 * "Member must have length greater than or equal to 33").
 *
 * OIDC subjects cannot safely be used as the header: they may be shorter than
 * the minimum, expose a principal value, and create indefinite worker affinity.
 */
const MIN_RUNTIME_SESSION_ID_LENGTH = 33;

/**
 * Create a fresh AgentCore transport session id for one invocation.
 *
 * This is deliberately NOT the application's logical conversation session.
 * COA sends that session id in the request body and persists its state in
 * AgentCore Memory/DynamoDB. Reusing a transport id adds no continuity, but it
 * does pin every future browser request to the runtime worker/version that
 * accepted the first request. After a deployment, the same user can therefore
 * keep reaching an old worker indefinitely.
 *
 * A UUID is 36 characters, already above AgentCore's 33-character minimum.
 * The `rt-` prefix makes the transport-only purpose visible in service logs.
 * An optional nonce keeps the function deterministic in unit tests.
 *
 * Returns undefined if `sub` is falsy (caller omits the header entirely).
 */
export function deriveRuntimeSessionId(
  sub: string | undefined,
  nonce?: string,
): string | undefined {
  if (!sub) return undefined;
  const runtimeSessionId = `rt-${nonce ?? crypto.randomUUID()}`;
  if (runtimeSessionId.length < MIN_RUNTIME_SESSION_ID_LENGTH) {
    throw new Error(
      `Runtime session nonce must produce at least ${MIN_RUNTIME_SESSION_ID_LENGTH} characters`,
    );
  }
  return runtimeSessionId;
}
