# Build context is the repository root (see docker-compose.yml).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATIC_DIR=/app/frontend \
    DATA_DIR=/data

# Upgrade the system CA store past the eMudhra intermediate CA
# compromise (October 2026): a cached base image would otherwise keep trusting them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/app ./app
COPY frontend ./frontend

RUN useradd --system --uid 10001 --home /app dashboard \
    && mkdir -p /data && chown dashboard /data
USER dashboard

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

# --proxy-headers only trusts X-Forwarded-* from FORWARDED_ALLOW_IPS (default 127.0.0.1);
# set that to your reverse proxy's address in Phase 6 so login rate limiting sees real clients.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
