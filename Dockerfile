ARG PYTHON_BASE=python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286

FROM ${PYTHON_BASE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

RUN python -m venv /opt/venv

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip==26.0.1 setuptools==82.0.1 wheel==0.46.3 \
    && pip install .


FROM ${PYTHON_BASE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    STOPLIGA_STATE_FILE=/data/state.json \
    STOPLIGA_LOCK_FILE=/data/stopliga.lock

RUN groupadd --system --gid 10001 stopliga \
    && useradd --system --uid 10001 --gid stopliga --create-home --home-dir /home/stopliga stopliga \
    && mkdir -p /data \
    && chown -R stopliga:stopliga /data /home/stopliga

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER stopliga
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["stopliga", "--healthcheck"]

ENTRYPOINT ["stopliga"]
CMD ["--once"]
