# Slim, non-root, healthchecked image of the trading daemon (design §16.1).
# Build from the repo root, targeting the deploy arch (the VPS is x86_64):
#   docker build --platform=linux/amd64 -f deploy/Dockerfile -t trader:dev .
FROM python:3.11-slim AS runtime

# tzdata: the scheduler localizes slot times to America/New_York (§7.1).
# ca-certificates: TLS to the Schwab API. No compilers — all deps ship manylinux wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRADER_CONFIG=/app/config/default.yaml

# Non-root runtime user. /state (SQLite + tokens) and /data (Parquet cache) are mounted
# volumes (compose, M4.9); the daemon writes there, so they must be owned by the user.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin trader \
    && mkdir -p /state /data \
    && chown -R trader:trader /state /data

WORKDIR /app

# Install pinned runtime deps first (own cache layer), wheel-only so a missing wheel fails
# the build LOUDLY instead of silently trying to compile from source (no toolchain here).
COPY deploy/requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

# Then the package itself, WITHOUT re-resolving deps (reproducible). NO dev deps, NO secrets.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir --no-deps .

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER trader

# Liveness backed by the M4.6 heartbeat: fresh beat => exit 0, stale/missing => non-zero
# so Docker marks the container unhealthy. Shell form so $TRADER_CONFIG expands.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD trader status --healthcheck --config "$TRADER_CONFIG"

# Exec form so trader is PID 1 and SIGTERM reaches it for a clean shutdown.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["run"]
