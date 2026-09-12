# RAG pipeline image: ingestion worker + retrieval service.
FROM python:3.12-slim

WORKDIR /rag

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove build-essential gcc g++ cpp && rm -rf /root/.cache/pip

COPY . .

# Default runs the ingestion job; use `python -m service` for the HTTP service.
CMD ["python", "-m", "ingest", "--dir", "/data/docs"]