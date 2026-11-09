FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv
WORKDIR /opt/overnight
COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-dev --extra acp --no-emit-project --no-hashes -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY src ./src
RUN uv pip install --system --no-cache --no-deps . && rm -rf /opt/overnight/src
