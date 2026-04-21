ARG PYTHON_BASE=python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286
ARG VERSION=0.1.12
ARG BUILD_DATE=unknown
ARG VCS_REF=unknown
ARG SOURCE_URL=https://github.com/jcastro/stopliga

FROM ${PYTHON_BASE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

RUN python -m venv /opt/venv

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip==26.0.1 setuptools==82.0.1 wheel==0.46.3 \
    && pip install .


FROM ${PYTHON_BASE} AS runtime

ARG VERSION=0.1.12
ARG BUILD_DATE=unknown
ARG VCS_REF=unknown
ARG SOURCE_URL=https://github.com/jcastro/stopliga

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HOME="/home/stopliga" \
    STOPLIGA_STATE_FILE=/data/state.json \
    STOPLIGA_LOCK_FILE=/data/stopliga.lock

LABEL org.opencontainers.image.title="StopLiga" \
      org.opencontainers.image.description="Synchronize a UniFi policy-based route with a public GitHub IP feed." \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"

RUN groupadd --system --gid 10001 stopliga \
    && useradd --system --uid 10001 --gid stopliga --create-home --home-dir /home/stopliga stopliga \
    && mkdir -p /data \
    && chown -R stopliga:stopliga /data /home/stopliga

COPY --from=builder /opt/venv /opt/venv
COPY --chmod=0555 docker/entrypoint.py /usr/local/bin/docker-entrypoint.py

WORKDIR /app
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["stopliga", "--healthcheck"]

ENTRYPOINT ["/opt/venv/bin/python", "-I", "/usr/local/bin/docker-entrypoint.py"]
