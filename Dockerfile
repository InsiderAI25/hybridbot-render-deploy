# Cloud Run / generic OCI image for HybridBot / Monique File Manager.
# FastAPI + Uvicorn only — Gunicorn is permanently banned per architecture rules.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY *.py ./

# Drop root for runtime.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Cloud Run honours $PORT; the directive's exact CMD spec.
CMD exec uvicorn render_main:app --host 0.0.0.0 --port $PORT --workers 1
