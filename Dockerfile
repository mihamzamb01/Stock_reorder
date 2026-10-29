# syntax=docker/dockerfile:1
FROM python:3.11-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies for psycopg (PostgreSQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt requirements-dev.txt ./
RUN pip install --upgrade pip && pip install -r requirements-dev.txt

# Copy application code
COPY . .

# Install the package (exposes `stock-reorder` CLI)
RUN pip install -e .

FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/stock_reorder.db \
    RUNS_DIR=/runs

# Install only runtime dependencies (no dev tools)
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Create data and runs directories
RUN mkdir -p /data /runs && chmod 777 /data /runs

VOLUME ["/data", "/runs"]

# Health check: verify the CLI responds
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python main.py --routing > /dev/null || exit 1

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]