#!/bin/bash
set -e

# Start the FastAPI server
# Railway injects PORT; default to 8000
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
