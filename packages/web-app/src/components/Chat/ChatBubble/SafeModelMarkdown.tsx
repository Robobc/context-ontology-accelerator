// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const MODEL_MARKDOWN_COMPONENTS: Components = {
  // @amsec-loaf: SDO-182
  // Model-controlled links and images can issue outbound requests or steer users
  // to untrusted destinations. Preserve their labels, never their destinations.
  a: ({ children }) => <span>{children}</span>,
  img: ({ alt }) => <span>{alt || "Image omitted"}</span>,
};

export const SafeModelMarkdown = ({ children }: { children: string }) => (
  <Markdown
    components={MODEL_MARKDOWN_COMPONENTS}
    remarkPlugins={[remarkGfm]}
    skipHtml
  >
    {children}
  </Markdown>
);
