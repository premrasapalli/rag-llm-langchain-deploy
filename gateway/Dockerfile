# API gateway = FastAPI + gunicorn/uvicorn.
FROM python:3.12-slim

WORKDIR /gateway

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.sh .

EXPOSE 8080

CMD ["bash", "run.sh"]