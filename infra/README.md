# Deployment notes

The prototype runs as a single FastAPI process (SQLite + local artifact directory + in-process
job threads) and serves the built web console from `apps/web/dist`. That is sufficient for a
workstation / single-VM deployment.

## Single-host

```bash
cd apps/web && npm ci && npm run build
cd ../../backend && pip install -r requirements.txt
OT_DATA_DIR=/srv/oceantrace/data python -m uvicorn oceantrace.api.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Keep `--workers 1`: jobs run in threads inside the API process and SQLite is opened in WAL mode.
Put nginx/Caddy in front for TLS and upload limits (`client_max_body_size 1g` for GeoTIFFs).

## Scaling path (doc 07 architecture)

| concern | prototype | production |
|---|---|---|
| store | SQLite (`core/db.py`) | PostgreSQL + PostGIS (same schema; swap the connection layer) |
| artifacts | `OT_DATA_DIR` | S3/MinIO bucket (paths already go through `service._case_dir`) |
| jobs | `core/jobs.py` thread pool | Celery/RQ workers + Redis; `Job` records and the `/jobs/{id}` polling contract stay unchanged |
| inference | CPU onnxruntime | onnxruntime-gpu / Triton behind the same adapter |
| tiles | OpenFreeMap / Esri / OSM (keyless) | self-hosted OpenMapTiles for offline networks |

## Dockerfile sketch

```Dockerfile
FROM node:20 AS web
WORKDIR /app/apps/web
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web .
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY configs configs
COPY data/static data/static
COPY data/fixtures data/fixtures
COPY --from=web /app/apps/web/dist apps/web/dist
ENV OT_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "oceantrace.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
```
