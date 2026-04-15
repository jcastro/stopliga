FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

RUN python -m venv /opt/venv

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip setuptools wheel \
    && pip install .


FROM python:3.12-slim AS runtime

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
