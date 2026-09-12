#!/usr/bin/env bash
exec gunicorn -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8080 app.main:app