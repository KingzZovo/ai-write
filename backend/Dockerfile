# syntax=docker/dockerfile:1.7

# -------- builder stage --------
FROM python:3.11-slim AS builder

WORKDIR /build

# Build-only deps (removed from runtime layer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install into a prefix we can copy cleanly to the runtime image
COPY pyproject.toml ./
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./

RUN pip install --prefix=/install .

# -------- runtime stage --------
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime-only apt deps (libs needed by compiled wheels, no compilers).
# libmagic1: mobi / file sniffing. curl: healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:${PATH}"

# Copy installed python packages + console scripts from builder
COPY --from=builder /install /usr/local

# Copy application source (kept out of the site-packages layer so code changes
# don't invalidate the deps cache).
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Version stamping (populated at build time from docker-compose build args).
# Written to /build-info/ (not /app/) so a dev-mode host volume mount over /app
# does NOT hide these files. Primary read channel is env vars.
ARG GIT_SHA=unknown
ARG GIT_TAG=unknown
ARG BUILD_TIME=unknown
ENV GIT_SHA=${GIT_SHA} \
    GIT_TAG=${GIT_TAG} \
    BUILD_TIME=${BUILD_TIME}
RUN mkdir -p /build-info && \
    printf '%s\n' "git_sha=${GIT_SHA}" "git_tag=${GIT_TAG}" "build_time=${BUILD_TIME}" > /build-info/git_sha

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
