FROM node:24-slim AS runtime
WORKDIR /opt/pi
COPY connectors/pi/package.json connectors/pi/package-lock.json ./
RUN npm ci --no-audit --no-fund --omit=dev

FROM overnight-agent-base
COPY --from=runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=runtime /opt/pi /opt/pi
COPY connectors/pi /tmp/pi-connector
RUN uv pip install --system --no-cache --no-deps /tmp/pi-connector && rm -rf /tmp/pi-connector
