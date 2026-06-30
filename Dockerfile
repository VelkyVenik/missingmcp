FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app

# Pin the unmodified garmin_mcp worker by commit (override at build time).
ARG GARMIN_MCP_REF=main
ENV GARMIN_MCP_REF=${GARMIN_MCP_REF}

# git: uv installs the pinned garmin_mcp worker from a git ref.
# tini: reaps the many worker subprocesses the gateway spawns.
RUN apt-get update && apt-get install -y --no-install-recommends git tini && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts
RUN uv pip install --system . && \
    uv pip install --system "garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp@${GARMIN_MCP_REF}"
ENTRYPOINT ["tini", "--"]
CMD ["garmin-gateway"]
EXPOSE 8080
VOLUME ["/data"]
