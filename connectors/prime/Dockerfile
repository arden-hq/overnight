FROM overnight-agent-base AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git pkg-config \
    && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain 1.95.0 --no-modify-path
COPY connectors/prime /tmp/prime-connector
RUN uv pip install --system --no-cache --no-deps /tmp/prime-connector
COPY --from=prime-git . /tmp/prime-source/.git
ENV UV_PYTHON_INSTALL_DIR=/opt/prime-python UV_CACHE_DIR=/tmp/uv-cache
RUN git config --global --add safe.directory /tmp/prime-source \
    && python3 /tmp/prime-connector/build_native.py \
        --source /tmp/prime-source --out /opt/prime --jobs 2 \
    && rm -rf /opt/prime/source /tmp/uv-cache

FROM overnight-agent-base
COPY --from=runtime /opt/prime-python /opt/prime-python
COPY --from=runtime /opt/prime /opt/prime
COPY connectors/prime /tmp/prime-connector
RUN uv pip install --system --no-cache --no-deps /tmp/prime-connector \
    && rm -rf /tmp/prime-connector
