// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import {
  AGENTCORE_RESTRICTED_AZ_IDS,
  parseAgentCoreAzNames,
  resolveAgentCoreAzNames,
  resolveEndpointServiceAzNames,
  agentCoreSupportedSubnets,
} from "../../lib/utils/agentcore-az";

describe("parseAgentCoreAzNames", () => {
  it("parses the existing deployment's standard AZs", () => {
    expect(
      parseAgentCoreAzNames("us-east-1b, us-east-1c", "us-east-1"),
    ).toEqual(["us-east-1b", "us-east-1c"]);
  });

  it("returns undefined when no override is configured", () => {
    expect(parseAgentCoreAzNames(undefined, "us-east-1")).toBeUndefined();
    expect(parseAgentCoreAzNames("", "us-east-1")).toBeUndefined();
  });

  it("rejects fewer than two AZs", () => {
    expect(() => parseAgentCoreAzNames("us-east-1b", "us-east-1")).toThrow(
      /at least 2 AZs/,
    );
  });

  it("rejects duplicates", () => {
    expect(() =>
      parseAgentCoreAzNames("us-east-1b,us-east-1b", "us-east-1"),
    ).toThrow(/must not contain duplicates/);
  });

  it("rejects another region and local zones", () => {
    expect(() =>
      parseAgentCoreAzNames("us-east-1b,us-east-1-bos-1a", "us-east-1"),
    ).toThrow(/standard AZs in us-east-1/);
  });
});

function fakeEc2(pairs: Array<[string, string]>) {
  return {
    send: async () => ({
      AvailabilityZones: pairs.map(([ZoneName, ZoneId]) => ({
        ZoneName,
        ZoneId,
        ZoneType: "availability-zone",
      })),
    }),
  } as unknown as Parameters<typeof resolveAgentCoreAzNames>[2];
}

describe("resolveAgentCoreAzNames", () => {
  it("resolves supported AZ names for us-east-1 (testing-account shape)", async () => {
    const client = fakeEc2([
      ["us-east-1a", "use1-az4"],
      ["us-east-1b", "use1-az6"],
      ["us-east-1c", "use1-az1"],
      ["us-east-1d", "use1-az2"],
    ]);
    await expect(
      resolveAgentCoreAzNames("us-east-1", 2, client),
    ).resolves.toEqual(["us-east-1a", "us-east-1c"]);
  });

  it("resolves supported AZ names for us-east-1 (CI-account shape)", async () => {
    const client = fakeEc2([
      ["us-east-1a", "use1-az1"],
      ["us-east-1b", "use1-az2"],
      ["us-east-1c", "use1-az4"],
      ["us-east-1d", "use1-az6"],
    ]);
    await expect(
      resolveAgentCoreAzNames("us-east-1", 2, client),
    ).resolves.toEqual(["us-east-1a", "us-east-1b"]);
  });

  it("returns undefined for unrestricted regions (no filtering needed)", async () => {
    await expect(
      resolveAgentCoreAzNames("us-west-2", 2, fakeEc2([])),
    ).resolves.toBeUndefined();
  });

  it("returns undefined for unknown regions (safe default)", async () => {
    await expect(
      resolveAgentCoreAzNames("ap-south-2", 2, fakeEc2([])),
    ).resolves.toBeUndefined();
  });

  it("respects a custom count", async () => {
    const client = fakeEc2([
      ["us-east-1a", "use1-az4"],
      ["us-east-1b", "use1-az6"],
      ["us-east-1c", "use1-az1"],
    ]);
    const names = await resolveAgentCoreAzNames("us-east-1", 1, client);
    expect(names).toHaveLength(1);
    expect(names![0]).toBe("us-east-1a");
  });

  it("throws when fewer than count supported AZs exist in a restricted region", async () => {
    const client = fakeEc2([
      ["us-east-1a", "use1-az1"],
      ["us-east-1b", "use1-az3"],
      ["us-east-1c", "use1-az5"],
    ]);
    await expect(
      resolveAgentCoreAzNames("us-east-1", 2, client),
    ).rejects.toThrow(/only 1 AgentCore-supported AZ/);
  });

  it("ignores non-standard zone types (Local/Wavelength)", async () => {
    const client = {
      send: async () => ({
        AvailabilityZones: [
          {
            ZoneName: "us-east-1a",
            ZoneId: "use1-az1",
            ZoneType: "availability-zone",
          },
          {
            ZoneName: "us-east-1b",
            ZoneId: "use1-az2",
            ZoneType: "availability-zone",
          },
          {
            ZoneName: "us-east-1-bos-1a",
            ZoneId: "use1-az4",
            ZoneType: "local-zone",
          },
        ],
      }),
    } as unknown as Parameters<typeof resolveAgentCoreAzNames>[2];
    await expect(
      resolveAgentCoreAzNames("us-east-1", 2, client),
    ).resolves.toEqual(["us-east-1a", "us-east-1b"]);
  });

  it("intersects with requiredAlsoIn BEFORE slicing (AOSS AZ gap)", async () => {
    // AgentCore supports {1a,1b,1c}; without the constraint the sorted
    // slice would pick {1a,1b}. AOSS control-plane is offered only in
    // {1b,1c,1d}, so the intersection must be {1b,1c} — never 1a.
    const client = fakeEc2([
      ["us-east-1a", "use1-az1"],
      ["us-east-1b", "use1-az2"],
      ["us-east-1c", "use1-az4"],
      ["us-east-1d", "use1-az6"],
    ]);
    await expect(
      resolveAgentCoreAzNames("us-east-1", 2, client, [
        "us-east-1b",
        "us-east-1c",
        "us-east-1d",
      ]),
    ).resolves.toEqual(["us-east-1b", "us-east-1c"]);
  });

  it("throws when the intersection with requiredAlsoIn is smaller than count", async () => {
    // AgentCore {1a,1b,1c} ∩ AOSS {1d} = {} → cannot satisfy count=2.
    const client = fakeEc2([
      ["us-east-1a", "use1-az1"],
      ["us-east-1b", "use1-az2"],
      ["us-east-1c", "use1-az4"],
      ["us-east-1d", "use1-az6"],
    ]);
    await expect(
      resolveAgentCoreAzNames("us-east-1", 2, client, ["us-east-1d"]),
    ).rejects.toThrow(/requires at least 2/);
  });

  it("ignores an empty requiredAlsoIn (no additional constraint)", async () => {
    const client = fakeEc2([
      ["us-east-1a", "use1-az1"],
      ["us-east-1b", "use1-az2"],
      ["us-east-1c", "use1-az4"],
    ]);
    await expect(
      resolveAgentCoreAzNames("us-east-1", 2, client, []),
    ).resolves.toEqual(["us-east-1a", "us-east-1b"]);
  });
});

describe("resolveEndpointServiceAzNames", () => {
  function fakeSvc(azs: string[] | undefined) {
    return {
      send: async () => ({
        ServiceDetails: azs === undefined ? [] : [{ AvailabilityZones: azs }],
      }),
    } as unknown as Parameters<typeof resolveEndpointServiceAzNames>[2];
  }

  it("returns the service's supported AZ names, sorted", async () => {
    const client = fakeSvc(["us-east-1d", "us-east-1b", "us-east-1c"]);
    await expect(
      resolveEndpointServiceAzNames(
        "us-east-1",
        "com.amazonaws.us-east-1.aoss",
        client,
      ),
    ).resolves.toEqual(["us-east-1b", "us-east-1c", "us-east-1d"]);
  });

  it("returns undefined when the service is not found", async () => {
    const client = fakeSvc(undefined);
    await expect(
      resolveEndpointServiceAzNames(
        "us-east-1",
        "com.amazonaws.us-east-1.aoss",
        client,
      ),
    ).resolves.toBeUndefined();
  });

  it("returns undefined when the service lists no AZs", async () => {
    const client = fakeSvc([]);
    await expect(
      resolveEndpointServiceAzNames(
        "us-east-1",
        "com.amazonaws.us-east-1.aoss",
        client,
      ),
    ).resolves.toBeUndefined();
  });
});

describe("agentCoreSupportedSubnets", () => {
  function vpcWith(stack: cdk.Stack, azs: string[]) {
    return new ec2.Vpc(stack, "Vpc", {
      availabilityZones: azs,
      natGateways: azs.length > 1 ? 1 : 0,
      subnetConfiguration: [
        {
          name: "Private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
        { name: "Public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });
  }

  it("filters subnets to the supported AZ names", () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, "TestStack", {
      env: { account: "111111111111", region: "us-east-1" },
    });
    const vpc = vpcWith(stack, ["us-east-1a", "us-east-1b"]);
    const filtered = agentCoreSupportedSubnets(vpc, ["us-east-1a"]);
    expect(filtered.map((s) => s.availabilityZone)).toEqual(["us-east-1a"]);
  });

  it("throws when the supported set filters every subnet away", () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, "TestStack", {
      env: { account: "111111111111", region: "us-east-1" },
    });
    const vpc = vpcWith(stack, ["us-east-1b"]);
    expect(() => agentCoreSupportedSubnets(vpc, ["us-east-1a"])).toThrow(
      /No AgentCore-supported subnets found/,
    );
  });

  it("passes everything through when no supported names are provided (unrestricted region)", () => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, "TestStack", {
      env: { account: "111111111111", region: "us-east-1" },
    });
    const vpc = vpcWith(stack, ["us-east-1a", "us-east-1b"]);
    expect(agentCoreSupportedSubnets(vpc, undefined)).toHaveLength(2);
    expect(agentCoreSupportedSubnets(vpc, [])).toHaveLength(2);
  });
});

describe("AGENTCORE_RESTRICTED_AZ_IDS", () => {
  it("only restricts us-east-1", () => {
    expect(Object.keys(AGENTCORE_RESTRICTED_AZ_IDS)).toEqual(["us-east-1"]);
  });

  it("lists the correct supported AZ-IDs for us-east-1", () => {
    expect(AGENTCORE_RESTRICTED_AZ_IDS["us-east-1"]).toEqual([
      "use1-az1",
      "use1-az2",
      "use1-az4",
    ]);
  });
});
