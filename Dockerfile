# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder stage: build a wheel for stateless-hydra and cache every runtime
# dependency as a wheel, so the runtime stage needs no compiler or network.
# The project uses the hatchling build backend (see pyproject.toml); pip's
# build isolation fetches hatchling automatically.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Copy only what the wheel actually needs so the layer cache stays stable.
COPY pyproject.toml ./
COPY src ./src

# Build the project wheel *and* download all (including extra) dependencies
# as wheels into /wheels. pip uses PEP 517 build isolation, so hatchling is
# fetched automatically and no separate build tooling is needed.
# "uvicorn[standard]" brings uvloop/httptools and "httpx[socks]" brings
# socksio; all ship manylinux wheels for this base image.
RUN pip wheel --wheel-dir /wheels .

# ---------------------------------------------------------------------------
# Runtime stage: minimal image, non-root user, the app installed from wheels.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SH_APP_CONFIG=/config/app.yaml \
    SH_INDEXERS_FILE=/config/indexers.yaml \
    SH_API_KEYS_FILE=/config/api-keys.yaml \
    SH_REDIS_URL=redis://redis:6379/0 \
    SH_HOST=0.0.0.0 \
    SH_PORT=5076

# Non-root runtime user (matches the Kubernetes runAsUser in k8s/deployment.yaml).
RUN groupadd --gid 10001 hydra \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin hydra

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels stateless-hydra \
    && rm -rf /wheels

USER 10001:10001

EXPOSE 5076

# Probe /healthz without curl (slim images do not include it); stdlib only.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5076/healthz', timeout=3).status == 200 else 1)"]

LABEL org.opencontainers.image.source="https://github.com/stateless-hydra/stateless-hydra" \
      org.opencontainers.image.title="stateless-hydra" \
      org.opencontainers.image.description="Stateless, Kubernetes-native nzbhydra2/Newznab-compatible Usenet indexer proxy" \
      org.opencontainers.image.licenses=""

# uvicorn is a project dependency. exec makes uvicorn PID 1 so it receives
# SIGTERM directly; the SH_HOST/SH_PORT ENV defaults above (overridable by the
# orchestrator) drive the bind address.
CMD ["sh", "-c", "exec uvicorn stateless_hydra.main:app --host \"${SH_HOST:-0.0.0.0}\" --port \"${SH_PORT:-5076}\""]
