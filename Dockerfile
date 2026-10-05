FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app

# Pin the unmodified garmin_mcp worker to a reviewed commit (override at build
# time). Bumping this is a deliberate, reviewed action: the worker runs with each
# user's decrypted Garmin tokens, so a floating ref would run unreviewed code.
# e8554bc (2026-09-01): reviewed 2026-09-03 — no dependency additions, no new
# network destinations; NOTE the worker now logs in on a background thread and
# answers /healthz before the sign-in resolves, which is why WorkerManager gates
# spawns on the sign-in log lines (forward.login_outcome).
ARG GARMIN_MCP_REF=cfc5d799ab0f165e837f1188a1d093c65838aaf7
ENV GARMIN_MCP_REF=${GARMIN_MCP_REF}

# git: uv installs the pinned garmin_mcp worker from a git ref.
# tini: reaps the many worker subprocesses the gateway spawns.
RUN apt-get update && apt-get install -y --no-install-recommends git tini && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts
# mail campaign texts — scripts/campaign.py create reads them inside the container
COPY campaigns ./campaigns
# mcp<2: garmin_mcp is written against the mcp 1.x API (mcp.server.fastmcp) and
# doesn't bound its own dependency — mcp 2.0.0 (2026-07-28) removed that module,
# and the first image rebuild after the release crashed every worker spawn with
# ModuleNotFoundError (2026-07-31 incident). The worker's other deps float too;
# pin here, in the same resolve, whenever one of them breaks the same way.
RUN uv pip install --system . && \
    uv pip install --system "garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp@${GARMIN_MCP_REF}" "mcp<2"
# -e 143: uvicorn (>=0.29) re-raises the SIGTERM it shut down on, so a clean
# stop exits 143 and Railway mails "Deploy Crashed!" on every deploy. Only the
# platform sends SIGTERM here; real crashes (OOM = SIGKILL, 137) still surface.
ENTRYPOINT ["tini", "-e", "143", "--"]
CMD ["missingmcp"]
EXPOSE 8080
# No VOLUME directive: Railway's builder rejects it ("use Railway Volumes") and
# provides /data via a platform-managed volume; self-hosters mount /data with
# `docker run -v`. Persistence is supplied by the runtime, not the image.
