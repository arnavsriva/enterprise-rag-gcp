# One image for the API (default command) and the jobs (command overridden by Cloud Run Jobs).
# Dependencies install from the pinned lockfile, in their own layer so code changes rebuild fast.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080 \
    DATA_DIR=/tmp/data

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY common ./common
COPY ingest ./ingest
COPY rag ./rag
COPY api ./api
COPY eval ./eval
COPY bench ./bench

# Run as an unprivileged user; only /tmp is writable (Cloud Run's filesystem is in-memory).
RUN useradd --uid 10001 --no-create-home app
USER app

# Our middleware writes one structured log line per request, so uvicorn's access log is off.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --no-access-log --timeout-graceful-shutdown 10"]
