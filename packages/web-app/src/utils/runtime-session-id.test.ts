// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { deriveRuntimeSessionId } from "./runtime-session-id";

const MIN_LENGTH = 33;

describe("deriveRuntimeSessionId", () => {
  it("returns undefined for a missing sub", () => {
    expect(
      deriveRuntimeSessionId(undefined, "11111111-1111-4111-8111-111111111111"),
    ).toBeUndefined();
  });

  it("uses a UUID transport nonce and satisfies the 33-char minimum", () => {
    const nonce = "11111111-1111-4111-8111-111111111111";
    const result = deriveRuntimeSessionId("noahpaig", nonce);
    expect(result).toBe(`rt-${nonce}`);
    expect(result!.length).toBeGreaterThanOrEqual(MIN_LENGTH);
  });

  it("does not expose the OIDC subject in the transport header", () => {
    const sub = "jane.doe@example.com";
    const result = deriveRuntimeSessionId(
      sub,
      "11111111-1111-4111-8111-111111111111",
    );
    expect(result).not.toContain(sub);
  });

  it("rotates the transport session for successive invocations by the same user", () => {
    expect(
      deriveRuntimeSessionId(
        "noahpaig",
        "11111111-1111-4111-8111-111111111111",
      ),
    ).not.toBe(
      deriveRuntimeSessionId(
        "noahpaig",
        "22222222-2222-4222-8222-222222222222",
      ),
    );
  });

  it("rejects a nonce that cannot satisfy AgentCore's minimum", () => {
    expect(() => deriveRuntimeSessionId("noahpaig", "too-short")).toThrow(
      /at least 33 characters/,
    );
  });
});
