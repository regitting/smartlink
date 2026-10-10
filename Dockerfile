# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Environment settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy source code
COPY . .

# Expose port
EXPOSE 8000

# Healthcheck (calls your /health route)
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/ready || exit 1

# Migrations are explicit: run alembic upgrade head before starting this image.
# Fail clearly if the database is unavailable or not migrated.
# Start app with Gunicorn
CMD ["sh", "-c", "flask --app wsgi:app db-check && exec gunicorn wsgi:app --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 60"]
