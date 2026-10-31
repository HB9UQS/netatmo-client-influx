# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM alpine:3.22

# Setup env
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /usr/local/bin/

ARG UID=10001
RUN adduser -D -H -h /app -u "${UID}" appuser

USER appuser
WORKDIR /app

COPY --chown=${UID} pyproject.toml uv.lock src/* /app/
# --locked fails the build if uv.lock does not match pyproject.toml;
# data/ is created here so a named volume mounted there is writable for appuser
RUN --mount=type=cache,uid=${UID},target=/app/.cache \
    uv sync --locked --no-dev && \
    mkdir -p /app/data

# After every run that fetched and wrote all data, the client writes the Unix time until which it
# counts as healthy into this file (two intervals plus a minute). The check fails while the file is
# missing or that time has passed, e.g. because the token expired or InfluxDB is unreachable.
ENV HEARTBEAT_FILE=/tmp/netatmo-heartbeat
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD test "$(date +%s)" -lt "$(cat "$HEARTBEAT_FILE" 2>/dev/null || echo 0)"

# --no-sync: start with the environment built above, never resolve or download at runtime
CMD [ "uv", "run", "--no-sync", "client.py" ]
