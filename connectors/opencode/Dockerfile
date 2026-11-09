FROM oven/bun:1.4.4-slim AS bun

FROM overnight-agent-base AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends git patch ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
COPY connectors/opencode /tmp/opencode-connector
RUN uv pip install --system --no-cache --no-deps /tmp/opencode-connector
COPY --from=opencode-git . /tmp/opencode-source/.git
RUN git config --global --add safe.directory /tmp/opencode-source \
    && python3 /tmp/opencode-connector/build_native.py \
        --source /tmp/opencode-source --out /opt/opencode

FROM overnight-agent-base
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
COPY --from=runtime /opt/opencode /opt/opencode
COPY connectors/opencode /tmp/opencode-connector
RUN uv pip install --system --no-cache --no-deps /tmp/opencode-connector \
    && rm -rf /tmp/opencode-connector
