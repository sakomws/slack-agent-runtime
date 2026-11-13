# syntax=docker/dockerfile:1.7

FROM python:3.15-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY . .
RUN python -m pip wheel --wheel-dir /wheels ".[persistence]"

FROM python:3.15-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=6016

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index \
    --find-links=/wheels "slack-agent-runtime[persistence]" \
    && rm -rf /wheels

USER 10001:10001
EXPOSE 6016

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"6016\")}/healthcheck', timeout=3)"]

CMD ["sh", "-c", "exec uvicorn slack_agent_runtime.entrypoints.app:app --host 0.0.0.0 --port \"${PORT}\""]
