FROM python:3.11-slim

# Playwright's --with-deps needs these base tools to run apt internally
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium + all its OS-level dependencies automatically
RUN playwright install --with-deps chromium

# Copy application code
COPY . .

# Railway injects PORT env var at runtime; default to 8000 for local builds
ENV PORT=8000

EXPOSE 8000

CMD ["./start.sh"]
