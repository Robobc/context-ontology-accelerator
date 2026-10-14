# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# python:3.12-slim, pinned for deterministic builds.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

WORKDIR /app

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd --no-create-home --shell /bin/false appuser

# Install shared common library (changes less often -> better layer caching)
COPY libs/common/ /tmp/common/
RUN pip install --no-cache-dir /tmp/common/ && rm -rf /tmp/common/

COPY packages/outer-agent/pyproject.toml ./
COPY packages/outer-agent/src/ ./src/

# Strip [tool.uv.sources] before installing: `coa-common = { workspace = true }`
# resolves only inside the repo's uv workspace, which does not exist in this
# image — coa-common is already installed from libs/common above.
RUN pip install --no-cache-dir uv && \
    sed -i '/\[tool.uv.sources\]/,$ d' pyproject.toml && \
    uv pip install --system --no-cache . && \
    rm -rf /root/.cache

EXPOSE 8080
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping')" || exit 1

CMD ["python", "-m", "coa_outer_agent"]
