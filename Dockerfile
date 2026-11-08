ARG PYTHON_BASE=python:3.12-alpine@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
ARG VERSION=0.1.31
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

RUN pip install --upgrade pip==26.3 setuptools==82.0.1 wheel==0.46.3 \
    && pip install --no-cache-dir .

RUN rm -rf /opt/venv/lib/python*/site-packages/pip* \
    /opt/venv/lib/python*/site-packages/setuptools* \
    /opt/venv/lib/python*/site-packages/wheel* \
    /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.* \
    && find /opt/venv -type d -name '__pycache__' -prune -exec rm -rf {} +


FROM ${PYTHON_BASE} AS runtime

ARG VERSION=0.1.31
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
      org.opencontainers.image.description="Synchronize a managed router route or rule with live block status and a public IP feed." \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"

RUN addgroup -S -g 10001 stopliga \
    && adduser -S -D -u 10001 -G stopliga -h /home/stopliga stopliga \
    && mkdir -p /data /home/stopliga \
    && chown stopliga:stopliga /data /home/stopliga

COPY --from=builder /opt/venv /opt/venv
COPY --chmod=0555 docker/entrypoint.py /usr/local/bin/docker-entrypoint.py

USER 10001:10001
WORKDIR /app
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["/opt/venv/bin/python", "-m", "stopliga.healthcheck"]

ENTRYPOINT ["/opt/venv/bin/python", "-I", "/usr/local/bin/docker-entrypoint.py"]
