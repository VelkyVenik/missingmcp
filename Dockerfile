FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app

# Pin the unmodified garmin_mcp worker to a reviewed commit (override at build
# time). Bumping this is a deliberate, reviewed action: the worker runs with each
# user's decrypted Garmin tokens, so a floating ref would run unreviewed code.
ARG GARMIN_MCP_REF=2974244bfda1595b00836b3f942f579ec2d6f7d6
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
CMD ["missingmcp"]
EXPOSE 8080
# No VOLUME directive: Railway's builder rejects it ("use Railway Volumes") and
# provides /data via a platform-managed volume; docker-compose mounts /data via
# its own `volumes:` mapping. Persistence is supplied by the runtime, not the image.
