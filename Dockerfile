FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app

# Pin the unmodified garmin_mcp worker to a reviewed commit (override at build
# time). Bumping this is a deliberate, reviewed action: the worker runs with each
# user's decrypted Garmin tokens, so a floating ref would run unreviewed code.
# e8554bc (2026-09-01): the worker logs in on a background thread and answers
# /healthz before the sign-in resolves. 8e809d8 (2026-10-12) adds GET /readyz
# (ready|pending|failed), which WorkerManager polls (forward.readiness_path).
# e79df6f (tip of main, contains 8e809d8): reviewed 2026-10-12 — no new
# dependencies (lock bumps of anyio/pyjwt/urllib3 only), no new network
# destinations; +1 tool (set_activity_exercise_sets).
ARG GARMIN_MCP_REF=e79df6fc59b276d03c478398b91c7e40a791f46b
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
#
# The worker gets its OWN venv (/opt/garmin-mcp), symlinked onto PATH so the
# default GARMIN_MCP_CMD=garmin-mcp still resolves. garmin_mcp pins
# garminconnect==0.3.2; installed into the same site-packages it silently
# downgraded the gateway's sign-in library (prod ran 0.3.2 until 2026-10-12:
# skip_strategies ignored, email-MFA pages misread as "blocked"). Separate envs
# let the gateway run its pin while the worker keeps the version upstream tests.
RUN uv pip install --system . && \
    uv venv --python "$(command -v python3)" /opt/garmin-mcp && \
    uv pip install --python /opt/garmin-mcp/bin/python \
        "garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp@${GARMIN_MCP_REF}" "mcp<2" && \
    ln -s /opt/garmin-mcp/bin/garmin-mcp /usr/local/bin/garmin-mcp
# -e 143: uvicorn (>=0.29) re-raises the SIGTERM it shut down on, so a clean
# stop exits 143 and Railway mails "Deploy Crashed!" on every deploy. Only the
# platform sends SIGTERM here; real crashes (OOM = SIGKILL, 137) still surface.
ENTRYPOINT ["tini", "-e", "143", "--"]
CMD ["missingmcp"]
EXPOSE 8080
# No VOLUME directive: Railway's builder rejects it ("use Railway Volumes") and
# provides /data via a platform-managed volume; self-hosters mount /data with
# `docker run -v`. Persistence is supplied by the runtime, not the image.
