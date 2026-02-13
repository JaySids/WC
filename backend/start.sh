#!/bin/bash
set -e

# Install Playwright Chromium browser if not already present
playwright install --with-deps chromium

# Start the FastAPI server
# Railway injects PORT; default to 8000
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
