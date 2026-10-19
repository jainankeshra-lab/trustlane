# TrustLane server image: the cloud server (default) or the store edge (command: python -m server.edge ...).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    TRUSTLANE_DATA_DIR=/data TRUSTLANE_CATALOG=/data/catalog.json TRUSTLANE_OFFERS=/data/promotions.json \
    TRUSTLANE_LOG_DIR=/data/logs TRUSTLANE_LOG_STDOUT=1 TRUSTLANE_PACKAGE_MODEL=/data/models/shelf-classify.onnx

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server ./server
COPY web ./web
COPY gate_agent ./gate_agent
COPY data/catalog.json data/promotions.json ./defaults/
COPY deploy/entrypoint.sh /entrypoint.sh

# Run as an unprivileged user; /data holds the ledger, keys, catalog and invoices (mount a volume).
RUN useradd --system --uid 10001 --home /app trustlane && mkdir -p /data && chown trustlane /data \
    && chmod +x /entrypoint.sh
USER trustlane
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/config', timeout=4); sys.exit(0)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "server.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
