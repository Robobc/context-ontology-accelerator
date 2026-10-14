// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import { SafeModelMarkdown } from "./SafeModelMarkdown";

describe("SafeModelMarkdown", () => {
  it("preserves formatting but removes model-controlled link destinations", () => {
    const { container } = render(
      <SafeModelMarkdown>
        {
          "**Evidence** [source](https://attacker.example/x) ![pixel](https://attacker.example/p.png)"
        }
      </SafeModelMarkdown>,
    );

    expect(screen.getByText("Evidence").tagName).toBe("STRONG");
    expect(screen.getByText("source").tagName).toBe("SPAN");
    expect(screen.getByText("pixel").tagName).toBe("SPAN");
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("does not render raw model HTML", () => {
    const { container } = render(
      <SafeModelMarkdown>
        {"<img src=https://attacker.example/p>"}
      </SafeModelMarkdown>,
    );
    expect(container.querySelector("img")).toBeNull();
  });
});
